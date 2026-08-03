"""Peer-to-peer networking: how Obsidion nodes find each other and agree.

The protocol is deliberately small — nine message types over TCP, framed the
way Bitcoin frames them:

    [4B network magic][12B command][4B length][4B checksum][payload]

The magic bytes come first for a reason: a testnet node dialling a mainnet node
fails on the very first four bytes, before either side has revealed or believed
anything. The checksum is not security (TCP already checksums; an attacker can
recompute it) — it catches framing bugs, where reader and writer disagree about
where a message ends.

The conversation between two fresh peers:

    A → B   version    who I am, how tall my chain is, a random nonce
    B → A   version    likewise
    A ⇄ B   verack     handshake complete
    A → B   getaddr    who else do you know?          → addr
    A → B   getblocks  here's a sketch of my chain    → inv of what I lack
    A → B   getdata    send me those                  → block, block, …

After sync, gossip: a node that accepts a new block or transaction announces
the hash (`inv`) to every other peer, who request the body (`getdata`) only if
it is news to them. Data therefore floods the network in one direction, once.

Sync uses a *block locator*: exponentially spaced hashes from my tip back to
genesis. The peer finds the latest one on its own chain — the fork point — and
answers with what comes after. A few dozen hashes locate a fork in a chain of
any length. (Bitcoin Core now syncs headers-first to resist bandwidth-wasting
attacks; at Obsidion's scale the locator protocol is the same behaviour with
far less machinery, and the full validation of every block is identical.)

Peers are scored, not trusted: invalid blocks or malformed framing add to a
misbehaviour score, and 100 points bans the address for the session. An honest
node never scores a point; a lying one gets two or three tries.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import struct
import threading
import time

from obsidion import crypto
from obsidion.block import Block
from obsidion.chainstate import ChainState, ConnectResult, OrphanError
from obsidion.consensus import ConsensusError
from obsidion.mempool import Mempool
from obsidion.params import NetworkParams
from obsidion.transaction import Transaction, read_varint, write_varint

log = logging.getLogger("obsidion.p2p")

PROTOCOL_VERSION = 1
USER_AGENT = "/obsidion:0.1/"

#: Inventory entry types.
INV_TX = 1
INV_BLOCK = 2

#: Most block hashes returned for one getblocks request.
MAX_INV = 500

#: Most entries accepted in a peer's block locator. A locator is exponentially
#: spaced, so even a chain of billions of blocks needs well under this.
MAX_LOCATOR = 64

#: Most outstanding block requests tracked for one peer. Bounds the memory a
#: peer can consume by announcing blocks it never intends to deliver.
MAX_OUTSTANDING_BLOCKS = 2 * MAX_INV

#: Outbound connections a node tries to keep open. Bitcoin uses eight for the
#: same reason: enough that losing a few peers is survivable, few enough that
#: the network is not a mesh.
TARGET_OUTBOUND = 8

#: Seconds between rounds of connection upkeep.
MAINTENANCE_INTERVAL = 20

#: A peer silent for this long is pinged; silent again and it is dropped.
PING_AFTER_IDLE = 90

#: Ban threshold. An honest peer scores zero, ever.
BAN_SCORE = 100

MAX_ADDRS = 100
_HEADER_SIZE = 24


class ProtocolError(Exception):
    """The peer violated the wire protocol. Grounds for disconnection."""


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def pack_message(magic: bytes, command: str, payload: bytes = b"") -> bytes:
    encoded = command.encode("ascii")
    if len(encoded) > 12:
        raise ValueError(f"command too long: {command!r}")
    return (
        magic
        + encoded.ljust(12, b"\x00")
        + struct.pack("<I", len(payload))
        + crypto.sha256d(payload)[:4]
        + payload
    )


async def read_message(
    reader: asyncio.StreamReader, magic: bytes, max_payload: int
) -> tuple[str, bytes]:
    header = await reader.readexactly(_HEADER_SIZE)
    if header[:4] != magic:
        raise ProtocolError("wrong network magic — peer is on a different chain")

    command = header[4:16].rstrip(b"\x00").decode("ascii", errors="replace")
    (length,) = struct.unpack_from("<I", header, 16)
    if length > max_payload:
        raise ProtocolError(f"{command} message of {length} bytes exceeds limit")

    payload = await reader.readexactly(length)
    if crypto.sha256d(payload)[:4] != header[20:24]:
        raise ProtocolError(f"{command} message failed its checksum")
    return command, payload


# --------------------------------------------------------------------------
# Payload encodings
# --------------------------------------------------------------------------


def pack_version(height: int, listen_port: int, nonce: int) -> bytes:
    agent = USER_AGENT.encode()
    return (
        struct.pack("<IIHQ", PROTOCOL_VERSION, height, listen_port, nonce)
        + write_varint(len(agent))
        + agent
    )


def unpack_version(payload: bytes) -> tuple[int, int, int, int, str]:
    protocol, height, listen_port, nonce = struct.unpack_from("<IIHQ", payload, 0)
    length, offset = read_varint(payload, struct.calcsize("<IIHQ"))
    agent = payload[offset : offset + length].decode("ascii", errors="replace")
    return protocol, height, listen_port, nonce, agent


def pack_inv(entries: list[tuple[int, bytes]]) -> bytes:
    parts = [write_varint(len(entries))]
    parts.extend(struct.pack("<B32s", kind, item) for kind, item in entries)
    return b"".join(parts)


def unpack_inv(payload: bytes) -> list[tuple[int, bytes]]:
    count, offset = read_varint(payload, 0)
    entries = []
    for _ in range(count):
        kind, item = struct.unpack_from("<B32s", payload, offset)
        offset += struct.calcsize("<B32s")
        entries.append((kind, item))
    return entries


def pack_getblocks(locator: list[bytes]) -> bytes:
    parts = [write_varint(len(locator))]
    parts.extend(locator)
    return b"".join(parts)


def unpack_getblocks(payload: bytes) -> list[bytes]:
    count, offset = read_varint(payload, 0)

    # Fail closed on an impossible count before allocating anything.
    #
    # Slicing a bytes object past its end returns b'' rather than raising, so
    # without this check a nine-byte message declaring 2**64 locator entries
    # would spin for the rest of the process's life appending empty strings —
    # a whole node frozen by a packet smaller than this comment. An honest
    # locator is logarithmic in chain height and never approaches the cap.
    if count > MAX_LOCATOR or offset + count * 32 > len(payload):
        raise ProtocolError(
            f"getblocks declares {count} locator entries, which the payload "
            f"cannot contain (limit {MAX_LOCATOR})"
        )

    locator = []
    for _ in range(count):
        locator.append(payload[offset : offset + 32])
        offset += 32
    return locator


def pack_addrs(addresses: list[tuple[str, int]]) -> bytes:
    parts = [write_varint(len(addresses))]
    for host, port in addresses:
        encoded = host.encode()
        parts.append(write_varint(len(encoded)) + encoded + struct.pack("<H", port))
    return b"".join(parts)


def unpack_addrs(payload: bytes) -> list[tuple[str, int]]:
    count, offset = read_varint(payload, 0)
    addresses = []
    for _ in range(count):
        length, offset = read_varint(payload, offset)
        host = payload[offset : offset + length].decode("ascii", errors="replace")
        offset += length
        (port,) = struct.unpack_from("<H", payload, offset)
        offset += 2
        addresses.append((host, port))
    return addresses


def block_locator(chain: ChainState) -> list[bytes]:
    """Exponentially spaced hashes from the tip back to genesis: dense near the
    tip where forks are likely, sparse in deep history where they are not."""
    locator: list[bytes] = []
    height = chain.height
    step = 1
    while height > 0:
        block_hash = chain.db.active_hash_at_height(height)
        assert block_hash is not None
        locator.append(block_hash)
        if len(locator) >= 10:
            step *= 2
        height -= step
    locator.append(chain.db.active_hash_at_height(0))
    return locator


# --------------------------------------------------------------------------
# Peers
# --------------------------------------------------------------------------


class Peer:
    """One connected peer and everything known or owed to it."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        outbound: bool,
    ):
        self.reader = reader
        self.writer = writer
        self.host = host
        self.outbound = outbound

        self.handshaked = False
        self.height = 0
        self.listen_port: int | None = None
        self.agent = ""
        self.ban_score = 0
        self.last_seen = time.monotonic()
        self.awaiting_pong = False

        #: Block hashes we asked this peer for and have not yet received.
        self.requested_blocks: set[bytes] = set()
        #: The last inv batch was full, so more blocks likely remain.
        self.batch_full = False

    def __repr__(self) -> str:
        direction = "out" if self.outbound else "in"
        return f"<peer {self.host} {direction} height={self.height}>"


class P2PNode:
    """The networking half of an Obsidion node.

    Owns the listening socket, the peer set, the address book and the ban list.
    Chain and mempool access is guarded by `lock`, so a miner or RPC thread can
    share those objects with the event loop safely.
    """

    def __init__(
        self,
        chain: ChainState,
        mempool: Mempool,
        params: NetworkParams,
        host: str = "127.0.0.1",
        port: int = 0,
        lock: threading.RLock | None = None,
    ):
        self.chain = chain
        self.mempool = mempool
        self.params = params
        self.host = host
        self.port = port

        self.lock = lock or threading.RLock()
        self.nonce = secrets.randbits(64)  # detects accidental self-connection
        self.peers: set[Peer] = set()
        self.addrbook: set[tuple[str, int]] = set()
        self.banned: set[str] = set()
        self.server: asyncio.Server | None = None
        self.seeds: list[tuple[str, int]] = list(params.seed_nodes)
        #: Addresses dialled and found unreachable. Retried, but only after
        #: everything else has been tried.
        self.failed: set[tuple[str, int]] = set()
        self._maintenance: asyncio.Task | None = None

    # -------------------------------------------------------------- lifecycle

    async def start(self, maintain: bool = True) -> None:
        self.server = await asyncio.start_server(
            self._on_inbound, self.host, self.port
        )
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("listening on %s:%d", self.host, self.port)

        if maintain:
            self._maintenance = asyncio.ensure_future(self._maintain())

    async def stop(self) -> None:
        if self._maintenance is not None:
            self._maintenance.cancel()
            try:
                await self._maintenance
            except asyncio.CancelledError:
                pass
            self._maintenance = None

        # Order matters. Since Python 3.12, Server.wait_closed() waits for
        # every connection handler to finish — and the handlers sit blocked in
        # read_message until their peer disappears. Drop the peers first so the
        # handlers unwind, and only then wait for the server.
        if self.server is not None:
            self.server.close()
        for peer in list(self.peers):
            await self._drop(peer)
        if self.server is not None:
            await self.server.wait_closed()

    async def connect(self, host: str, port: int) -> None:
        """Dial a peer. Failures are logged, not raised — an unreachable peer
        is routine, and the address book has other names in it."""
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            log.info("could not reach %s:%d: %s", host, port, exc)
            self.failed.add((host, port))
            return

        self.failed.discard((host, port))
        peer = Peer(reader, writer, host, outbound=True)
        self.addrbook.add((host, port))
        self.peers.add(peer)
        asyncio.ensure_future(self._serve_peer(peer))

    # ------------------------------------------------------------ maintenance

    async def _maintain(self) -> None:
        """Keep the node connected, for as long as it is running.

        Without this a node dials its seeds once at start-up and, if those
        connections ever drop, sits alone forever while believing itself fine.
        On a long-lived network that is the difference between a node and an
        ornament. Two jobs, every `MAINTENANCE_INTERVAL` seconds:

        * **Notice dead peers.** TCP can hold a connection open long after the
          machine at the other end has gone. A peer that has said nothing for
          `PING_AFTER_IDLE` gets a ping; still nothing by the next round and it
          is dropped, freeing the slot for a peer that answers.
        * **Top up outbound connections.** Dial toward `TARGET_OUTBOUND` from
          the address book, falling back to the configured seeds when nothing
          else is known or everything known has failed.
        """
        while True:
            try:
                await asyncio.sleep(MAINTENANCE_INTERVAL)
                await self._check_liveness()
                await self._top_up_outbound()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — this loop must outlive its errors
                # One bad round must not silently end connection upkeep and
                # strand the node; log it and try again next interval.
                log.exception("error during connection maintenance")

    async def _check_liveness(self) -> None:
        now = time.monotonic()
        for peer in list(self.peers):
            if now - peer.last_seen < PING_AFTER_IDLE:
                continue
            if peer.awaiting_pong:
                log.info("%r stopped answering; dropping", peer)
                await self._drop(peer)
            else:
                peer.awaiting_pong = True
                await self._send(peer, "ping", struct.pack("<Q", secrets.randbits(64)))

    async def _top_up_outbound(self) -> None:
        needed = TARGET_OUTBOUND - sum(1 for peer in self.peers if peer.outbound)
        if needed <= 0:
            return

        engaged = {(peer.host, peer.listen_port) for peer in self.peers}
        candidates = [
            address
            for address in self.addrbook
            if address not in engaged
            and address not in self.failed
            and address[0] not in self.banned
        ]

        if not candidates:
            # Nothing fresh to try. Forget past failures and fall back to the
            # seeds — a seed that was unreachable an hour ago may be back, and
            # a node with no peers has nothing to lose by asking again.
            self.failed.clear()
            candidates = [
                address for address in self.seeds if address not in engaged
            ]

        for host, port in candidates[:needed]:
            await self.connect(host, port)

    def handshaked_peers(self) -> list[Peer]:
        return [peer for peer in self.peers if peer.handshaked]

    # ------------------------------------------------------------- submission

    async def submit_block(self, block: Block, source: Peer | None = None) -> None:
        """Accept a block (from the miner, RPC, or a peer) and gossip it on."""
        try:
            with self.lock:
                result = self.chain.accept_block(block)
        except OrphanError:
            if source is None:
                raise  # a locally produced orphan is a local bug — surface it
            # Missing ancestry: ask the sender to fill the gap from the
            # last point our chains agree.
            await self._request_blocks(source)
            return
        except ConsensusError as exc:
            if source is None:
                raise  # local submitters (miner, RPC) deserve the reason
            await self._misbehave(source, BAN_SCORE, str(exc))
            return

        if result.status == "duplicate":
            return
        self._absorb(result)
        await self._broadcast_inv([(INV_BLOCK, block.hash())], exclude=source)

    async def submit_tx(self, tx: Transaction, source: Peer | None = None) -> None:
        """Admit a transaction to the mempool and gossip it on."""
        try:
            with self.lock:
                self.mempool.accept(tx, self.chain)
        except ConsensusError:
            if source is None:
                raise  # the local sender needs to know their payment failed
            # From a peer this is often an honest race — the transaction just
            # confirmed, or a rival spend won. A few points discourage a flood
            # without punishing bad luck.
            await self._misbehave(source, 10, "rejected transaction")
            return
        await self._broadcast_inv([(INV_TX, tx.txid())], exclude=source)

    # -------------------------------------------------------------- plumbing

    def _absorb(self, result: ConnectResult) -> None:
        """Update the mempool for what a connect or reorg changed."""
        with self.lock:
            for block_hash in result.connected:
                block = self.chain.get_block(block_hash)
                if block is not None:
                    self.mempool.remove_confirmed(block)
            if result.disconnected:
                self.mempool.reconsider(result.disconnected, self.chain)

    async def _send(self, peer: Peer, command: str, payload: bytes = b"") -> None:
        try:
            peer.writer.write(pack_message(self.params.magic, command, payload))
            await peer.writer.drain()
        except (ConnectionError, OSError):
            await self._drop(peer)

    async def _broadcast_inv(
        self, entries: list[tuple[int, bytes]], exclude: Peer | None = None
    ) -> None:
        for peer in self.handshaked_peers():
            if peer is not exclude:
                await self._send(peer, "inv", pack_inv(entries))

    async def _drop(self, peer: Peer) -> None:
        self.peers.discard(peer)
        try:
            peer.writer.close()
            await peer.writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _misbehave(self, peer: Peer, points: int, reason: str) -> None:
        peer.ban_score += points
        log.warning("%r misbehaved (+%d, %s): %s", peer, points, peer.ban_score, reason)
        if peer.ban_score >= BAN_SCORE:
            log.warning("banning %s for the session", peer.host)
            self.banned.add(peer.host)
            await self._drop(peer)

    async def _request_blocks(self, peer: Peer) -> None:
        with self.lock:
            locator = block_locator(self.chain)
        await self._send(peer, "getblocks", pack_getblocks(locator))

    # ------------------------------------------------------------ connections

    async def _on_inbound(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host = writer.get_extra_info("peername")[0]
        if host in self.banned:
            writer.close()
            return
        peer = Peer(reader, writer, host, outbound=False)
        self.peers.add(peer)
        await self._serve_peer(peer)

    async def _serve_peer(self, peer: Peer) -> None:
        """One peer's whole lifetime: version first, then messages until
        disconnection or misbehaviour."""
        max_payload = self.params.max_block_weight + 4096
        try:
            with self.lock:
                height = self.chain.height
            await self._send(
                peer, "version", pack_version(height, self.port, self.nonce)
            )
            while peer in self.peers:
                command, payload = await read_message(
                    peer.reader, self.params.magic, max_payload
                )
                # Any message at all proves the peer is alive.
                peer.last_seen = time.monotonic()
                peer.awaiting_pong = False
                await self._dispatch(peer, command, payload)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass  # peer went away; that is what peers do
        except ProtocolError as exc:
            await self._misbehave(peer, BAN_SCORE, str(exc))
        finally:
            await self._drop(peer)

    # --------------------------------------------------------------- handlers

    async def _dispatch(self, peer: Peer, command: str, payload: bytes) -> None:
        handler = getattr(self, f"_handle_{command}", None)
        if handler is None:
            log.info("%r sent unknown command %r; ignoring", peer, command)
            return
        try:
            await handler(peer, payload)
        except (struct.error, ValueError, IndexError) as exc:
            raise ProtocolError(f"malformed {command} payload: {exc}") from exc

    async def _handle_version(self, peer: Peer, payload: bytes) -> None:
        _, height, listen_port, nonce, agent = unpack_version(payload)
        if nonce == self.nonce:
            log.info("connected to ourselves; dropping")
            await self._drop(peer)
            return

        peer.height = height
        peer.listen_port = listen_port
        peer.agent = agent
        if listen_port:
            self.addrbook.add((peer.host, listen_port))
        await self._send(peer, "verack")

    async def _handle_verack(self, peer: Peer, payload: bytes) -> None:
        peer.handshaked = True
        await self._send(peer, "getaddr")
        with self.lock:
            behind = peer.height > self.chain.height
        if behind:
            await self._request_blocks(peer)

    async def _handle_ping(self, peer: Peer, payload: bytes) -> None:
        await self._send(peer, "pong", payload)

    async def _handle_pong(self, peer: Peer, payload: bytes) -> None:
        # The liveness bookkeeping already happened in _serve_peer, which
        # timestamps every inbound message. Nothing further is required.
        pass

    async def _handle_getaddr(self, peer: Peer, payload: bytes) -> None:
        addresses = [
            (host, port)
            for host, port in self.addrbook
            if not (host == peer.host and port == peer.listen_port)
        ][:MAX_ADDRS]
        await self._send(peer, "addr", pack_addrs(addresses))

    async def _handle_addr(self, peer: Peer, payload: bytes) -> None:
        for host, port in unpack_addrs(payload)[:MAX_ADDRS]:
            if len(self.addrbook) < 1000 and host not in self.banned:
                self.addrbook.add((host, port))

    async def _handle_getblocks(self, peer: Peer, payload: bytes) -> None:
        locator = unpack_getblocks(payload)
        with self.lock:
            start_height = 1
            for block_hash in locator:
                entry = self.chain.index_entry(block_hash)
                if entry is not None and entry.active:
                    start_height = entry.height + 1
                    break

            hashes: list[bytes] = []
            height = start_height
            while len(hashes) < MAX_INV:
                block_hash = self.chain.db.active_hash_at_height(height)
                if block_hash is None:
                    break
                hashes.append(block_hash)
                height += 1

        if hashes:
            await self._send(
                peer, "inv", pack_inv([(INV_BLOCK, item) for item in hashes])
            )

    async def _handle_inv(self, peer: Peer, payload: bytes) -> None:
        entries = unpack_inv(payload)
        wanted: list[tuple[int, bytes]] = []
        block_count = 0

        with self.lock:
            for kind, item in entries:
                if kind == INV_BLOCK:
                    block_count += 1
                    if self.chain.index_entry(item) is None:
                        # Bounded: a peer that announces blocks forever and
                        # delivers none must not grow this set without limit.
                        if len(peer.requested_blocks) >= MAX_OUTSTANDING_BLOCKS:
                            continue
                        wanted.append((kind, item))
                        peer.requested_blocks.add(item)
                elif kind == INV_TX:
                    if item not in self.mempool:
                        wanted.append((kind, item))

        peer.batch_full = block_count >= MAX_INV
        if wanted:
            await self._send(peer, "getdata", pack_inv(wanted))

    async def _handle_getdata(self, peer: Peer, payload: bytes) -> None:
        for kind, item in unpack_inv(payload):
            if kind == INV_BLOCK:
                with self.lock:
                    raw = self.chain.db.get_block_raw(item)
                if raw is not None:
                    await self._send(peer, "block", raw)
            elif kind == INV_TX:
                with self.lock:
                    tx = self.mempool.get(item)
                if tx is not None:
                    await self._send(peer, "tx", tx.serialize())

    async def _handle_block(self, peer: Peer, payload: bytes) -> None:
        try:
            block = Block.deserialize(payload)
        except ValueError as exc:
            raise ProtocolError(f"undecodable block: {exc}") from exc

        peer.requested_blocks.discard(block.hash())
        await self.submit_block(block, source=peer)

        # Batch finished and it was full: there is probably more chain where
        # that came from.
        if not peer.requested_blocks and peer.batch_full:
            peer.batch_full = False
            await self._request_blocks(peer)

    async def _handle_tx(self, peer: Peer, payload: bytes) -> None:
        try:
            tx = Transaction.deserialize(payload)
        except ValueError as exc:
            raise ProtocolError(f"undecodable transaction: {exc}") from exc
        await self.submit_tx(tx, source=peer)
