"""Regression tests for defects found by adversarial audit.

Each test here corresponds to a real bug that shipped in the first version and
was caught by reviewers attacking the code rather than exercising it. They are
grouped together deliberately: this file is the record of what went wrong and
the proof it stays fixed.
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading

import pytest

from obsidion import consensus, crypto
from obsidion.chainstate import ChainState
from obsidion.consensus import ConsensusError
from obsidion.mempool import Mempool
from obsidion.miner import mine_block
from obsidion.p2p import (
    MAX_LOCATOR,
    P2PNode,
    ProtocolError,
    pack_getblocks,
    unpack_getblocks,
)
from obsidion.params import COIN, MAINNET, REGTEST
from obsidion.transaction import OutPoint, Transaction, TxIn, TxOut, write_varint
from obsidion.wallet import Wallet, WalletError

ALICE_PRIV = crypto.generate_private_key()
ALICE_PUB = crypto.private_to_public(ALICE_PRIV)
ALICE = crypto.hash160(ALICE_PUB)
BOB = crypto.hash160(b"bob")
MINER = crypto.hash160(b"miner")
REWARD = consensus.subsidy(1, REGTEST)


@pytest.fixture
def chain():
    state = ChainState(REGTEST)
    yield state
    state.close()


def mine(chain, miner=MINER, mempool=None):
    block = mine_block(chain, mempool, miner)
    chain.accept_block(block, now=block.header.timestamp + 600)
    return block


def fund(chain, count=1):
    txids = [mine(chain, miner=ALICE).coinbase().txid() for _ in range(count)]
    for _ in range(REGTEST.coinbase_maturity):
        mine(chain)
    return txids


def spend(prev_txid, amount_in, outputs, index=0, priv=ALICE_PRIV, pub=ALICE_PUB):
    tx = Transaction(
        inputs=[TxIn(OutPoint(prev_txid, index))],
        outputs=[TxOut(amount, pkh) for amount, pkh in outputs],
    )
    tx.sign_input(0, priv, pub, amount=amount_in)
    return tx


# --------------------------------------------------------------------------
# A tiny message must not be able to freeze a node
# --------------------------------------------------------------------------


def test_a_nine_byte_getblocks_cannot_hang_the_node():
    """Was a critical denial of service.

    `unpack_getblocks` looped over an attacker-declared count, and slicing a
    bytes object past its end returns b'' instead of raising — so a message
    claiming 2**64 locator entries spun forever, allocating as it went. One
    packet, one dead node.
    """
    malicious = write_varint(0xFFFFFFFFFFFFFFFF)
    assert len(malicious) == 9

    with pytest.raises(ProtocolError, match="locator"):
        unpack_getblocks(malicious)


def test_a_locator_longer_than_the_payload_is_refused():
    lying = write_varint(1000) + b"\x00" * 32  # claims 1000, carries one
    with pytest.raises(ProtocolError):
        unpack_getblocks(lying)


def test_an_over_long_locator_is_refused():
    too_many = pack_getblocks([bytes(32)] * (MAX_LOCATOR + 1))
    with pytest.raises(ProtocolError, match="limit"):
        unpack_getblocks(too_many)


def test_an_honest_locator_still_round_trips():
    """The bound must not break real sync. A locator is exponentially spaced,
    so even an enormous chain produces only a few dozen entries."""
    honest = [crypto.sha256d(bytes([i])) for i in range(20)]
    assert unpack_getblocks(pack_getblocks(honest)) == honest


def test_a_hostile_getblocks_bans_the_sender_and_leaves_the_node_serving():
    """End to end over a real socket: the node survives and keeps working."""

    async def scenario():
        node = P2PNode(ChainState(REGTEST), Mempool(REGTEST), REGTEST)
        await node.start()
        try:
            from obsidion.p2p import pack_message, pack_version

            reader, writer = await asyncio.open_connection("127.0.0.1", node.port)
            writer.write(pack_message(REGTEST.magic, "version", pack_version(0, 0, 99)))
            writer.write(pack_message(REGTEST.magic, "verack"))
            writer.write(
                pack_message(
                    REGTEST.magic, "getblocks", write_varint(0xFFFFFFFFFFFFFFFF)
                )
            )
            await writer.drain()

            # The node must still answer for itself a moment later.
            await asyncio.sleep(0.6)
            assert node.chain.height == 0
            assert not node.handshaked_peers()
            writer.close()
        finally:
            await node.stop()
            node.chain.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# The mempool must not be free to fill
# --------------------------------------------------------------------------


def test_the_mempool_stops_growing_at_its_budget(chain):
    """Was a remote memory-exhaustion vector: valid zero-fee transactions
    could be created in bulk and nothing ever forced them out."""
    fundings = fund(chain, count=6)
    pool = Mempool(REGTEST, max_size=600)  # room for roughly three

    # All pay the same rate, so none can outbid another and the pool simply
    # fills up and starts refusing.
    refused = 0
    for funding in fundings:
        tx = spend(funding, REWARD, [(REWARD - 20_000, BOB)])
        try:
            pool.accept(tx, chain)
        except ConsensusError as exc:
            assert "full" in str(exc)
            refused += 1

    assert pool.total_size <= pool.max_size, "the budget was exceeded"
    assert len(pool) < len(fundings), "everything stayed resident"
    assert refused > 0, "nothing was ever refused"


def test_a_better_paying_transaction_evicts_a_worse_one(chain):
    """Past the budget, space goes to whoever pays for it — which is also what
    makes flooding cost money."""
    cheap_funding, rich_funding = fund(chain, count=2)
    pool = Mempool(REGTEST, max_size=260)  # one transaction at a time

    cheap = spend(cheap_funding, REWARD, [(REWARD - 1_000, BOB)])
    pool.accept(cheap, chain)
    assert cheap.txid() in pool

    rich = spend(rich_funding, REWARD, [(REWARD - 5_000_000, BOB)])
    pool.accept(rich, chain)

    assert rich.txid() in pool
    assert cheap.txid() not in pool
    assert pool.total_size <= pool.max_size


def test_a_worse_paying_transaction_cannot_evict_a_better_one(chain):
    cheap_funding, rich_funding = fund(chain, count=2)
    pool = Mempool(REGTEST, max_size=260)

    rich = spend(rich_funding, REWARD, [(REWARD - 5_000_000, BOB)])
    pool.accept(rich, chain)

    cheap = spend(cheap_funding, REWARD, [(REWARD - 1_000, BOB)])
    with pytest.raises(ConsensusError, match="does not outbid"):
        pool.accept(cheap, chain)

    assert rich.txid() in pool


def test_pool_size_accounting_returns_to_zero(chain):
    """Bookkeeping must be exact, or the pool slowly convinces itself it is
    full and starts refusing honest traffic."""
    (funding,) = fund(chain)
    pool = Mempool(REGTEST)

    payment = spend(funding, REWARD, [(REWARD - 1_000, BOB)])
    pool.accept(payment, chain)
    assert pool.total_size == payment.size()

    block = mine(chain, mempool=pool)
    pool.remove_confirmed(block)
    assert len(pool) == 0
    assert pool.total_size == 0


# --------------------------------------------------------------------------
# The wallet must never lose a key it has handed out
# --------------------------------------------------------------------------


def test_concurrent_address_creation_loses_no_keys(tmp_path):
    """Was a high-severity money-loss bug.

    The RPC server is threaded, so two clients could call getnewaddress at the
    same instant. Both wrote the shared temp file and one save overwrote the
    other — discarding a key whose address had already been given out. Anything
    sent there would have been unspendable forever.
    """
    path = tmp_path / "busy.wallet"
    wallet = Wallet.create(path, "pw", REGTEST)

    handed_out: list[str] = []
    errors: list[BaseException] = []

    def request_address():
        try:
            handed_out.append(wallet.new_address())
        except BaseException as exc:  # noqa: BLE001 — recorded and re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=request_address) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"address creation raised: {errors[0]!r}"
    assert len(set(handed_out)) == len(handed_out), "an address was issued twice"

    # Every address handed to a caller must be recoverable from disk.
    reloaded = Wallet.load(path, "pw")
    missing = set(handed_out) - set(reloaded.addresses())
    assert not missing, f"{len(missing)} issued addresses have no key on disk"


def test_no_stray_temp_files_survive_concurrent_saves(tmp_path):
    path = tmp_path / "clean.wallet"
    wallet = Wallet.create(path, "pw", REGTEST)

    threads = [threading.Thread(target=wallet.new_address) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != path.name]
    assert not leftovers, f"scratch files left behind: {leftovers}"


def test_the_wallets_network_cannot_be_edited_by_hand(tmp_path):
    """Was a low-severity but nasty bug: the network name sat outside the
    authentication tag, so anyone with a text editor could flip a testnet
    wallet to mainnet and the node would follow along, deriving addresses on
    the wrong chain."""
    path = tmp_path / "net.wallet"
    Wallet.create(path, "pw", REGTEST)

    envelope = json.loads(path.read_text())
    assert envelope["network"] == "regtest"
    envelope["network"] = "mainnet"
    path.write_text(json.dumps(envelope))

    with pytest.raises(WalletError):
        Wallet.load(path, "pw")


def test_an_untouched_wallet_still_loads(tmp_path):
    """The authentication must not be so eager it rejects honest files."""
    path = tmp_path / "fine.wallet"
    original = Wallet.create(path, "pw", REGTEST)
    original.new_address()

    reloaded = Wallet.load(path, "pw")
    assert reloaded.addresses() == original.addresses()
    assert reloaded.params.name == "regtest"


def test_configured_seeds_are_parsed_into_host_and_port():
    """Seeds are written as "host:port" strings but dialled as pairs.

    Shipped broken: P2PNode stored params.seed_nodes verbatim while annotating
    it list[tuple[str, int]], so self.seeds held strings. Nothing noticed until
    _top_up_outbound reached `for host, port in candidates`, which tore a
    hostname apart character by character and raised ValueError.

    It survived the suite because every network used in tests declares no
    seeds, and the integration tests hand their seeds in as tuples already -
    so the one line that converts them was never executed by a test.
    """
    from dataclasses import replace

    from obsidion.p2p import parse_address

    params = replace(
        REGTEST, seed_nodes=("seed.example.org:9444", "198.51.100.7:19444")
    )
    chain = ChainState(params)
    node = P2PNode(chain, Mempool(params), params)

    assert node.seeds == [("seed.example.org", 9444), ("198.51.100.7", 19444)]
    assert all(isinstance(port, int) for _, port in node.seeds)

    # The failure was in unpacking, so assert the shape the caller relies on.
    for host, port in node.seeds:
        assert isinstance(host, str) and isinstance(port, int)

    # Hostnames contain no colon; addresses do. Both must split on the last one.
    assert parse_address("example.org:1") == ("example.org", 1)
    assert parse_address("::1:9444") == ("::1", 9444)
    for bad in ("no-port", "host:", ":9444", "host:port"):
        with pytest.raises(ValueError):
            parse_address(bad)


def test_topping_up_peers_falls_back_to_seeds_without_crashing():
    """The bug only fired once the address book ran dry - i.e. on a new node.

    A first-time user has no peers and no addresses, which is exactly the state
    that reaches the seed fallback. So the one code path that mattered for
    bootstrapping was the one that raised.
    """
    from dataclasses import replace

    params = replace(REGTEST, seed_nodes=("127.0.0.1:1:",))
    with pytest.raises(ValueError):
        P2PNode(ChainState(params), Mempool(params), params)

    params = replace(REGTEST, seed_nodes=("127.0.0.1:9",))
    node = P2PNode(ChainState(params), Mempool(params), params)
    assert not node.addrbook, "the premise is an empty address book"

    # Nothing is listening on port 9; connect() must fail, not explode. The
    # original defect raised before a single connection was attempted.
    asyncio.run(node._top_up_outbound())
    assert node.seeds == [("127.0.0.1", 9)]


def test_a_dropped_dial_cannot_hang_the_node(monkeypatch):
    """A peer that drops our SYN must not stall the dialer.

    Shipped fragile: connect() called asyncio.open_connection with no timeout,
    so a silently *dropped* SYN — a firewall set to DROP not REJECT, a NAT
    hairpin, a dead IP that no longer sends RST — hung for the OS connect
    timeout (~21s on Windows). During boot that overran node.start()'s 15s
    budget and raised "network thread failed to start"; a real node hit exactly
    this dialing its own seed over a flaky hairpin. A refused connection always
    returned fast; only the dropped-SYN case hung.

    Here open_connection never returns. The bounded dial must still give up
    within the timeout and record the failure rather than block forever.
    """
    import asyncio

    from obsidion import p2p

    node = p2p.P2PNode(ChainState(REGTEST), Mempool(REGTEST), REGTEST)
    monkeypatch.setattr(p2p, "CONNECT_TIMEOUT_SECONDS", 0.2)

    async def never_returns(host, port):
        await asyncio.sleep(30)

    monkeypatch.setattr(p2p.asyncio, "open_connection", never_returns)

    async def run():
        # The outer bound is the test's safety net: if connect() were still
        # unbounded this would raise TimeoutError and fail loudly rather than
        # hang the suite.
        await asyncio.wait_for(node.connect("10.255.255.1", 9444), timeout=5)

    asyncio.run(run())

    assert ("10.255.255.1", 9444) in node.failed
    assert not node.peers


def test_an_unreachable_peer_is_announced_once_not_every_retry(monkeypatch, caplog):
    """A permanently dead address must not reprint every maintenance round.

    The maintenance loop retries while the node is short of peers, so a
    gossiped address that was never routable gets dialled every
    MAINTENANCE_INTERVAL forever. Logging each attempt at info buries genuine
    messages under thousands of identical lines — observed on a live node
    filling its console with one dead LAN address.

    The subtlety this pins: _top_up_outbound CLEARS `failed` whenever it runs
    out of fresh candidates, so keying "have I already said this?" off `failed`
    reprints anyway. The suppression must survive that reset, and must reset
    itself when the peer genuinely comes back.
    """
    import asyncio
    import logging

    from obsidion import p2p

    node = p2p.P2PNode(ChainState(REGTEST), Mempool(REGTEST), REGTEST)

    async def refused(host, port):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(p2p.asyncio, "open_connection", refused)

    with caplog.at_level(logging.INFO, logger="obsidion.p2p"):
        asyncio.run(node.connect("192.0.2.90", 9444))
        # Simulate what _top_up_outbound does when candidates run dry.
        node.failed.clear()
        for _ in range(5):
            asyncio.run(node.connect("192.0.2.90", 9444))

    announcements = [
        r for r in caplog.records
        if r.levelno >= logging.INFO and "could not reach 192.0.2.90" in r.getMessage()
    ]
    assert len(announcements) == 1, (
        f"expected one announcement, got {len(announcements)} — the log "
        "suppression did not survive failed.clear()"
    )

    # A peer that comes back must be reported, and must be able to be reported
    # as lost again later; otherwise a flapping peer goes silent forever.
    class _Writer:
        def close(self): pass

    async def accepted(host, port):
        return object(), _Writer()

    monkeypatch.setattr(p2p.asyncio, "open_connection", accepted)
    monkeypatch.setattr(p2p, "Peer", lambda *a, **k: object())

    def _discard(coro):
        # Close the coroutine we are choosing not to run, so it does not warn.
        coro.close()

    monkeypatch.setattr(p2p.asyncio, "ensure_future", _discard)

    with caplog.at_level(logging.INFO, logger="obsidion.p2p"):
        caplog.clear()
        asyncio.run(node.connect("192.0.2.90", 9444))

    assert any("reconnected" in r.getMessage() for r in caplog.records)
    assert ("192.0.2.90", 9444) not in node._reported_unreachable
