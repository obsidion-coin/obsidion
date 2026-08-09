"""JSON-RPC over HTTP: the node's control surface.

Everything that talks to a running node — the CLI, the block explorer, a
future GUI — talks through here. The protocol is JSON-RPC 2.0 over HTTP POST,
bound to loopback, and this endpoint controls the wallet: anything that can
successfully call it can spend your coins.

**Binding to localhost is not, by itself, a defence.** A web page you merely
visit can make your own browser POST to 127.0.0.1 — no exploit needed, that is
ordinary cross-origin behaviour. Without further checks such a page can call
`send` and drain the wallet of anyone who happened to be running a node. This
was a live hole here, demonstrated before it was closed.

Three independent defences, so no single mistake reopens it:

1. **A cookie.** A fresh random token is written to `<datadir>/.rpccookie` at
   start-up and required on every request. A web page cannot read a local file,
   so it cannot produce the token.
2. **Origin rejection.** Any request carrying `Origin` or `Referer` is refused
   outright. Browsers always attach those cross-origin; a CLI never does. This
   holds even if a cookie somehow leaks.
3. **A JSON content type.** A browser cannot set `Content-Type:
   application/json` cross-origin without a CORS preflight, which this server
   never approves.

Amounts cross this boundary as *strings* ("12.50000000"), never JSON floats.
Floats cannot represent most decimal fractions exactly, and a wallet that
rounds someone's payment is broken in the worst possible way. Strings parse
through Decimal into integer shards, exactly.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from obsidion import consensus, crypto
from obsidion.block import compact_to_target, target_to_difficulty
from obsidion.consensus import ConsensusError
from obsidion.params import COIN, TICKER
from obsidion.transaction import Transaction
from obsidion.wallet import WalletError

log = logging.getLogger("obsidion.rpc")


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def format_amount(shards: int) -> str:
    """Integer shards → exact decimal OBSD string."""
    return str(Decimal(shards) / COIN)


def parse_amount(value) -> int:
    """Decimal OBSD string → integer shards, refusing anything inexact."""
    try:
        shards = Decimal(str(value)) * COIN
    except InvalidOperation:
        raise RPCError(-3, f"unparseable amount: {value!r}") from None
    if shards != int(shards):
        raise RPCError(-3, f"amount {value!r} is finer than one shard")
    if shards <= 0:
        raise RPCError(-3, "amount must be positive")
    return int(shards)


COOKIE_FILENAME = ".rpccookie"

#: Largest request body accepted, so a local process cannot exhaust memory.
MAX_REQUEST_BYTES = 1_000_000


def cookie_filename(network: str | None) -> str:
    """The cookie file for one network, e.g. '.rpccookie-mainnet'.

    Namespaced per network for the same reason the chain databases are
    (`mainnet-chain.db`, `regtest-chain.db`): networks share a data directory
    by default, so a single shared file lets one node silently overwrite
    another's token. When that happened the second node kept running and
    relaying perfectly while every client — CLI, explorer, HUD — was locked out
    with a 401, which reads as a broken node rather than a clobbered file.

    `network=None` yields the legacy shared name, which is still accepted when
    reading so a client started against an older node keeps working.
    """
    return COOKIE_FILENAME if network is None else f"{COOKIE_FILENAME}-{network}"


def read_cookie(datadir: str | Path, network: str | None = None) -> str:
    """Read the auth token a running node wrote to its data directory.

    Clients call this; it is the whole of the authentication scheme from their
    side. Raises FileNotFoundError if no node has started, which is the honest
    answer to "why can I not connect".

    Prefers this network's own cookie and falls back to the legacy shared file,
    so a client pointed at a node from before the split still authenticates.
    """
    directory = Path(datadir)
    candidates = [directory / cookie_filename(network)]
    if network is not None:
        candidates.append(directory / COOKIE_FILENAME)

    for path in candidates:
        try:
            return path.read_text().strip()
        except FileNotFoundError:
            continue
    raise FileNotFoundError(candidates[0])


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class RPCServer:
    """A small threading HTTP server exposing the node's methods."""

    def __init__(
        self,
        node,
        host: str = "127.0.0.1",
        port: int = 0,
        datadir: str | Path | None = None,
        allow_remote: bool = False,
    ):
        if not _is_loopback(host) and not allow_remote:
            raise ValueError(
                f"refusing to bind the wallet RPC to {host!r}, which is reachable "
                "from outside this machine. Anything that can reach this port can "
                "spend your coins. Use an SSH tunnel for remote access, or pass "
                "allow_remote=True (--rpc-allow-remote) if you genuinely mean it."
            )

        self.node = node
        self.datadir = Path(datadir) if datadir is not None else None
        #: Fresh every start, so a leaked or stale cookie stops working.
        self.token = secrets.token_hex(32)
        self._write_cookie()

        methods = self  # for the handler closure

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet; the node has its own logs
                pass

            def _refuse(self, status: int, message: str) -> None:
                payload = json.dumps(
                    {"result": None, "error": {"code": -32600, "message": message},
                     "id": None}
                ).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self) -> None:
                # A browser attaches these cross-origin and a command-line
                # client never does, so their mere presence identifies a
                # request this endpoint should not be serving.
                for header in ("Origin", "Referer"):
                    if self.headers.get(header):
                        log.warning(
                            "refused an RPC request carrying %s: %r",
                            header,
                            self.headers.get(header),
                        )
                        self._refuse(
                            403,
                            f"requests carrying an {header} header are refused; "
                            "this endpoint is not for browsers",
                        )
                        return

                content_type = (self.headers.get("Content-Type") or "").split(";")[0]
                if content_type.strip().lower() != "application/json":
                    self._refuse(
                        415, "Content-Type must be application/json"
                    )
                    return

                supplied = (self.headers.get("Authorization") or "").removeprefix(
                    "Bearer "
                )
                if not hmac.compare_digest(supplied, methods.token):
                    self._refuse(
                        401,
                        "missing or invalid RPC token; it is written to "
                        f"{COOKIE_FILENAME} in the node's data directory",
                    )
                    return

                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_REQUEST_BYTES:
                    self._refuse(413, "request body too large")
                    return

                body = self.rfile.read(length)
                response = methods._handle(body)
                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def cookie_path(self) -> Path | None:
        """Where this node publishes its token — one file per network."""
        if self.datadir is None:
            return None
        return self.datadir / cookie_filename(self.node.params.name)

    def _write_cookie(self) -> None:
        """Publish the token where local clients — and only local clients —
        can read it. A remote attacker cannot read a file on your disk."""
        path = self.cookie_path
        if path is None:
            return
        self.datadir.mkdir(parents=True, exist_ok=True)
        path.write_text(self.token)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover — best effort on exotic filesystems
            pass

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="obsidion-rpc", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        # shutdown() waits for serve_forever() to return, so calling it on a
        # server that was never started blocks forever. Only signal a loop that
        # is actually running.
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self.httpd.server_close()
        # Leaving a token on disk after the node exits invites a client to
        # believe it is still valid. It is not — a restart mints a new one.
        # Only ever remove our own network's file: another network's node may
        # be running from this same data directory right now.
        path = self.cookie_path
        if path is not None:
            path.unlink(missing_ok=True)

    # -------------------------------------------------------------- dispatch

    def _handle(self, body: bytes) -> dict:
        request_id = None
        try:
            request = json.loads(body)
            request_id = request.get("id")
            method_name = request.get("method", "")
            params = request.get("params", [])

            method = getattr(self, f"rpc_{method_name}", None)
            if method is None:
                raise RPCError(-32601, f"unknown method {method_name!r}")

            result = method(*params)
            return {"result": result, "error": None, "id": request_id}
        except RPCError as exc:
            return self._error(exc.code, str(exc), request_id)
        except (ConsensusError, WalletError) as exc:
            return self._error(-26, str(exc), request_id)
        except (TypeError, ValueError) as exc:
            return self._error(-32602, f"bad parameters: {exc}", request_id)
        except Exception as exc:  # noqa: BLE001 — the boundary of the process
            log.exception("unhandled error in RPC")
            return self._error(-32603, f"internal error: {exc}", request_id)

    @staticmethod
    def _error(code: int, message: str, request_id) -> dict:
        return {
            "result": None,
            "error": {"code": code, "message": message},
            "id": request_id,
        }

    # ---------------------------------------------------------------- helpers

    def _require_wallet(self):
        if self.node.wallet is None:
            raise RPCError(-18, "this node has no wallet loaded")
        return self.node.wallet

    def _tx_to_dict(self, tx: Transaction, chain) -> dict:
        return {
            "txid": tx.txid()[::-1].hex(),
            "size": tx.size(),
            "coinbase": tx.is_coinbase(),
            "inputs": [
                {"coinbase": True}
                if tx.is_coinbase()
                else {
                    "txid": tx_input.prevout.txid[::-1].hex(),
                    "index": tx_input.prevout.index,
                }
                for tx_input in tx.inputs
            ],
            "outputs": [
                {
                    "amount": format_amount(output.amount),
                    "address": crypto.pubkey_hash_to_address(
                        output.pubkey_hash, self.node.params.bech32_hrp
                    ),
                }
                for output in tx.outputs
            ],
        }

    # ---------------------------------------------------------------- methods

    def rpc_getinfo(self) -> dict:
        node = self.node
        with node.lock:
            tip = node.chain.tip
            height = tip.height
            utxo_total = node.chain.total_utxo_value()
            mempool_size = len(node.mempool)

        params = node.params
        era = consensus.halving_era(height, params)
        blocks_left = consensus.blocks_until_halving(height, params)
        return {
            "coin": f"Obsidion ({TICKER})",
            "network": params.name,
            "height": height,
            "tip": tip.hash[::-1].hex(),
            "difficulty": target_to_difficulty(compact_to_target(tip.bits)),
            "supply": format_amount(consensus.circulating_supply(height, params)),
            "max_supply": format_amount(consensus.total_supply(params)),
            "utxo_total": format_amount(utxo_total),
            "mempool_transactions": mempool_size,
            "peers": len(node.p2p.handshaked_peers()),
            "halving": {
                "era": era,
                "block_reward": format_amount(consensus.subsidy(height + 1, params)),
                "next_halving_height": consensus.next_halving_height(height, params),
                "blocks_remaining": blocks_left,
                "estimated_seconds": (
                    None
                    if blocks_left is None
                    else blocks_left * params.target_block_time
                ),
            },
            "mining": node.mining,
            "hashrate": node.miner.hashrate() if node.miner else 0.0,
        }

    def rpc_getnewaddress(self) -> str:
        # Held under the node lock like every other wallet method. This server
        # is threaded, and two concurrent calls without it raced to write the
        # same key file — silently losing one of the keys, and with it any
        # coins later sent to that address.
        with self.node.lock:
            return self._require_wallet().new_address()

    def rpc_getaddresses(self) -> list[str]:
        with self.node.lock:
            return self._require_wallet().addresses()

    def rpc_deleteaddress(self, address: str) -> str:
        """Destroy a key permanently, if the wallet judges that safe.

        Irreversible and unrecoverable, so every guard lives in
        `Wallet.forget_address` — a funded address, the default address that
        receives mining rewards and change, and the wallet's last address are
        all refused there rather than here, so the CLI, the HUD and any future
        caller inherit the same protection instead of each inventing it.
        """
        with self.node.lock:
            self._require_wallet().forget_address(address, self.node.chain)
        return f"deleted {address}"

    def rpc_getbalance(self) -> dict:
        wallet = self._require_wallet()
        with self.node.lock:
            balance = wallet.balance(self.node.chain)
        return {
            "spendable": format_amount(balance.spendable),
            "immature": format_amount(balance.immature),
            "total": format_amount(balance.total),
        }

    def rpc_send(self, address: str, amount) -> dict:
        wallet = self._require_wallet()
        shards = parse_amount(amount)

        with self.node.lock:
            tx = wallet.create_transaction(self.node.chain, address, shards)
        try:
            txid = self.node.submit_tx(tx)
        except ConsensusError:
            wallet.release(tx)  # don't strand the coins on a failed send
            raise

        with self.node.lock:
            balance = wallet.balance(self.node.chain)
        fee = sum(
            entry.fee
            for entry in [self.node.mempool.entries.get(txid)]
            if entry is not None
        )
        return {
            "txid": txid[::-1].hex(),
            "fee": format_amount(fee),
            "remaining": format_amount(balance.spendable),
        }

    def rpc_startmining(self, address: str | None = None) -> str:
        pubkey_hash = None
        if address is not None:
            try:
                pubkey_hash = crypto.address_to_pubkey_hash(
                    address, self.node.params.bech32_hrp
                )
            except ValueError as exc:
                raise RPCError(-5, str(exc)) from exc
        self.node.start_mining(pubkey_hash)
        return "mining started"

    def rpc_stopmining(self) -> str:
        self.node.stop_mining()
        return "mining stopped"

    def rpc_generate(self, count: int, address: str | None = None) -> list[str]:
        if self.node.params.name != "regtest":
            raise RPCError(
                -32601, "generate exists only on regtest; real chains are mined"
            )
        pubkey_hash = None
        if address is not None:
            pubkey_hash = crypto.address_to_pubkey_hash(
                address, self.node.params.bech32_hrp
            )
        return self.node.generate(int(count), pubkey_hash)

    def rpc_getblockhash(self, height: int) -> str:
        with self.node.lock:
            block_hash = self.node.chain.db.active_hash_at_height(int(height))
        if block_hash is None:
            raise RPCError(-8, f"no active block at height {height}")
        return block_hash[::-1].hex()

    def rpc_getblock(self, hash_hex: str) -> dict:
        block_hash = bytes.fromhex(hash_hex)[::-1]
        with self.node.lock:
            block = self.node.chain.get_block(block_hash)
            entry = self.node.chain.index_entry(block_hash)
            tip_height = self.node.chain.height
        if block is None or entry is None:
            raise RPCError(-5, f"block {hash_hex} not found")

        return {
            "hash": hash_hex,
            "height": entry.height,
            "active": entry.active,
            "confirmations": (
                tip_height - entry.height + 1 if entry.active else 0
            ),
            "previous": block.header.prev_hash[::-1].hex(),
            "merkle_root": block.header.merkle_root[::-1].hex(),
            "time": block.header.timestamp,
            "bits": f"{block.header.bits:08x}",
            "nonce": block.header.nonce,
            "size": block.size(),
            "transactions": [self._tx_to_dict(tx, None) for tx in block.transactions],
        }

    def rpc_gettransaction(self, txid_hex: str) -> dict:
        txid = bytes.fromhex(txid_hex)[::-1]

        with self.node.lock:
            pooled = self.node.mempool.get(txid)
            if pooled is not None:
                found = self._tx_to_dict(pooled, self.node.chain)
                found["confirmations"] = 0
                return found

            # No transaction index at this scale — walk the active chain from
            # the tip. Recent transactions (the common query) are found fast.
            tip_height = self.node.chain.height
            for height in range(tip_height, -1, -1):
                block = self.node.chain.block_at_height(height)
                assert block is not None
                for tx in block.transactions:
                    if tx.txid() == txid:
                        found = self._tx_to_dict(tx, self.node.chain)
                        found["confirmations"] = tip_height - height + 1
                        found["block"] = block.hash_hex()
                        found["height"] = height
                        return found

        raise RPCError(-5, f"transaction {txid_hex} not found")

    def rpc_getpeerinfo(self) -> list[dict]:
        return [
            {
                "host": peer.host,
                "port": peer.listen_port,
                "direction": "outbound" if peer.outbound else "inbound",
                "height": peer.height,
                "agent": peer.agent,
                "ban_score": peer.ban_score,
            }
            for peer in self.node.p2p.handshaked_peers()
        ]

    def rpc_getaddressutxos(self, address: str) -> dict:
        """Every unspent output owned by an address — any address, not just the
        wallet's. This is what makes the explorer able to answer 'how much does
        obsd1… hold?'."""
        try:
            pubkey_hash = crypto.address_to_pubkey_hash(
                address, self.node.params.bech32_hrp
            )
        except ValueError as exc:
            raise RPCError(-5, str(exc)) from exc

        with self.node.lock:
            utxos = self.node.chain.utxos_for_pubkey_hash(pubkey_hash)
        return {
            "address": address,
            "balance": format_amount(sum(entry.amount for _, entry in utxos)),
            "utxos": [
                {
                    "txid": outpoint.txid[::-1].hex(),
                    "index": outpoint.index,
                    "amount": format_amount(entry.amount),
                    "height": entry.height,
                    "coinbase": entry.is_coinbase,
                }
                for outpoint, entry in utxos
            ],
        }

    def rpc_getmempoolinfo(self) -> dict:
        with self.node.lock:
            entries = list(self.node.mempool.entries.values())
        return {
            "transactions": len(entries),
            "bytes": sum(entry.size for entry in entries),
            "total_fees": format_amount(sum(entry.fee for entry in entries)),
            "txids": [entry.txid[::-1].hex() for entry in entries],
        }
