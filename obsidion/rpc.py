"""JSON-RPC over HTTP: the node's control surface.

Everything that talks to a running node — the CLI, the block explorer, a
future GUI — talks through here. The protocol is JSON-RPC 2.0 over plain HTTP
POST, bound to localhost only: whoever can reach the port controls the wallet,
so the port must not be reachable from anywhere else. (Remote management wants
an SSH tunnel, not an exposed RPC socket.)

Amounts cross this boundary as *strings* ("12.50000000"), never JSON floats.
Floats cannot represent most decimal fractions exactly, and a wallet that
rounds someone's payment is broken in the worst possible way. Strings parse
through Decimal into integer shards, exactly.
"""

from __future__ import annotations

import json
import logging
import threading
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


class RPCServer:
    """A small threading HTTP server exposing the node's methods."""

    def __init__(self, node, host: str = "127.0.0.1", port: int = 0):
        self.node = node
        methods = self  # for the handler closure

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet; the node has its own logs
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
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

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="obsidion-rpc", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

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
        return self._require_wallet().new_address()

    def rpc_getaddresses(self) -> list[str]:
        return self._require_wallet().addresses()

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
        obsd1q… hold?'."""
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
