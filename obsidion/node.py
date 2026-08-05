"""The Obsidion node: everything wired together and running.

A running node is four cooperating threads:

    main thread      starts everything, then sleeps until interrupted
    network thread   an asyncio event loop running the P2P layer
    rpc thread       an HTTP server answering JSON-RPC requests
    miner thread     (optional) grinding nonces

They share the chain state and mempool through one re-entrant lock. The P2P
loop holds it briefly around each chain operation; the RPC and miner threads
do the same. Blocks found by the miner and transactions submitted over RPC are
injected *into* the network loop with `run_coroutine_threadsafe`, so all
broadcasting happens in exactly one place.

Run it:

    obsidion-node --network regtest --mine
    obsidion-node --network mainnet --wallet my.wallet --connect 203.0.113.5:9444
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import threading
import time
from pathlib import Path

from obsidion import __version__
from obsidion.block import Block
from obsidion.chainstate import ChainState
from obsidion.genesis import genesis_hash
from obsidion.mempool import Mempool
from obsidion.miner import Miner
from obsidion.p2p import P2PNode
from obsidion.params import COIN_NAME, NetworkParams, TICKER, get_network
from obsidion.transaction import Transaction
from obsidion.wallet import Wallet, WalletError

log = logging.getLogger("obsidion.node")


class ObsidionNode:
    """One full node: chain, mempool, network, and optionally a wallet+miner."""

    def __init__(
        self,
        params: NetworkParams,
        datadir: str | Path | None = None,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int | None = None,
        wallet: Wallet | None = None,
        seeds: list[tuple[str, int]] | None = None,
    ):
        self.params = params
        self.lock = threading.RLock()

        if datadir is None:
            chain_path = ":memory:"
        else:
            datadir = Path(datadir)
            datadir.mkdir(parents=True, exist_ok=True)
            chain_path = str(datadir / f"{params.name}-chain.db")

        self.chain = ChainState(params, chain_path)
        self.mempool = Mempool(params)
        self.p2p = P2PNode(
            self.chain,
            self.mempool,
            params,
            host=listen_host,
            port=listen_port if listen_port is not None else 0,
            lock=self.lock,
        )
        self.wallet = wallet
        self.seeds = seeds or []
        # Peers given here are also the fallback the maintenance loop dials
        # when the address book runs dry, alongside any baked into params.
        for address in self.seeds:
            if address not in self.p2p.seeds:
                self.p2p.seeds.append(address)

        self.miner: Miner | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._net_thread: threading.Thread | None = None

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Bring up the network loop and connect to any seed peers."""
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run_network() -> None:
            asyncio.set_event_loop(self.loop)

            async def boot() -> None:
                await self.p2p.start()
                for host, port in self.seeds:
                    await self.p2p.connect(host, port)

            self.loop.run_until_complete(boot())
            ready.set()
            self.loop.run_forever()

        self._net_thread = threading.Thread(
            target=run_network, name="obsidion-net", daemon=True
        )
        self._net_thread.start()
        if not ready.wait(timeout=15):
            raise RuntimeError("network thread failed to start")

    def stop(self) -> None:
        self.stop_mining()
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.p2p.stop(), self.loop).result(10)
            self.loop.call_soon_threadsafe(self.loop.stop)
            assert self._net_thread is not None
            self._net_thread.join(timeout=10)
            self.loop.close()
            self.loop = None
        self.chain.close()

    def connect(self, host: str, port: int) -> None:
        """Dial a peer from outside the network thread."""
        assert self.loop is not None, "node is not started"
        asyncio.run_coroutine_threadsafe(
            self.p2p.connect(host, port), self.loop
        ).result(15)

    # ------------------------------------------------------------- submission

    def submit_block(self, block: Block) -> None:
        """Hand a locally produced block to the network loop. Raises if the
        block is invalid — a local block should never be."""
        assert self.loop is not None, "node is not started"
        asyncio.run_coroutine_threadsafe(
            self.p2p.submit_block(block), self.loop
        ).result(30)

    def submit_tx(self, tx: Transaction) -> bytes:
        """Admit and broadcast a transaction. Raises ConsensusError if the
        mempool refuses it. Returns the txid."""
        assert self.loop is not None, "node is not started"
        asyncio.run_coroutine_threadsafe(self.p2p.submit_tx(tx), self.loop).result(30)
        return tx.txid()

    # ----------------------------------------------------------------- mining

    def start_mining(self, pubkey_hash: bytes | None = None) -> None:
        if self.miner is not None and self.miner.running:
            return
        if pubkey_hash is None:
            if self.wallet is None:
                raise WalletError(
                    "no wallet is loaded; pass an address to mine to"
                )
            pubkey_hash = self.wallet.default_pubkey_hash()

        def on_block(block: Block) -> None:
            try:
                self.submit_block(block)
            except Exception:  # noqa: BLE001 — a mining race, not a crash
                log.exception("locally mined block was rejected")

        self.miner = Miner(
            self.chain, self.mempool, pubkey_hash, on_block, lock=self.lock
        )
        self.miner.start()

    def stop_mining(self) -> None:
        if self.miner is not None:
            self.miner.stop()

    @property
    def mining(self) -> bool:
        return self.miner is not None and self.miner.running

    def generate(self, count: int, pubkey_hash: bytes | None = None) -> list[str]:
        """Mine `count` blocks synchronously — the regtest development tool."""
        from obsidion.miner import mine_block

        if pubkey_hash is None:
            if self.wallet is None:
                raise WalletError("no wallet loaded; pass an address to pay")
            pubkey_hash = self.wallet.default_pubkey_hash()

        hashes = []
        for _ in range(count):
            with self.lock:
                block = mine_block(self.chain, self.mempool, pubkey_hash)
            self.submit_block(block)
            hashes.append(block.hash_hex())
        return hashes


# --------------------------------------------------------------------------
# Command line entry point
# --------------------------------------------------------------------------


def _load_or_create_wallet(args, params: NetworkParams) -> Wallet | None:
    if args.wallet is None:
        return None

    path = Path(args.wallet)
    password = os.environ.get("OBSIDION_WALLET_PASSWORD")

    if args.create_wallet and not path.exists():
        if password is None:
            password = getpass.getpass(f"New password for {path}: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                raise SystemExit("passwords did not match")
        wallet = Wallet.create(path, password, params)
        print(f"created wallet {path} with address {wallet.addresses()[0]}")
        return wallet

    if password is None:
        password = getpass.getpass(f"Password for {path}: ")
    return Wallet.load(path, password)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="obsidion-node",
        description=f"Run a {COIN_NAME} ({TICKER}) full node.",
    )
    parser.add_argument("--network", default="mainnet", help="mainnet, testnet or regtest")
    parser.add_argument("--datadir", default=str(Path.home() / ".obsidion"))
    parser.add_argument("--host", default="0.0.0.0", help="P2P listen address")
    parser.add_argument("--port", type=int, default=None, help="P2P listen port")
    parser.add_argument("--rpc-port", type=int, default=None)
    parser.add_argument(
        "--rpc-host",
        default="127.0.0.1",
        help="RPC listen address; loopback unless --rpc-allow-remote is given",
    )
    parser.add_argument(
        "--rpc-allow-remote",
        action="store_true",
        help="permit binding the wallet RPC off-loopback (you almost certainly "
        "want an SSH tunnel instead)",
    )
    parser.add_argument(
        "--connect",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="peer to connect to (repeatable)",
    )
    parser.add_argument("--wallet", default=None, help="path to a wallet file")
    parser.add_argument(
        "--create-wallet", action="store_true", help="create the wallet if missing"
    )
    parser.add_argument("--mine", action="store_true", help="start mining at once")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    params = get_network(args.network)
    wallet = _load_or_create_wallet(args, params)

    # Peers named on the command line take precedence; otherwise fall back to
    # the network's built-in seeds, which is how a first-time user joins
    # without being told an address by hand.
    specs = args.connect or list(params.seed_nodes)
    seeds = []
    for spec in specs:
        host, _, port = spec.rpartition(":")
        try:
            seeds.append((host, int(port)))
        except ValueError:
            raise SystemExit(f"malformed peer address {spec!r}; expected host:port")

    node = ObsidionNode(
        params,
        args.datadir,
        listen_host=args.host,
        listen_port=args.port if args.port is not None else params.default_port,
        wallet=wallet,
        seeds=seeds,
    )

    from obsidion.rpc import RPCServer

    rpc = RPCServer(
        node,
        host=args.rpc_host,
        port=args.rpc_port if args.rpc_port is not None else params.default_rpc_port,
        datadir=args.datadir,
        allow_remote=args.rpc_allow_remote,
    )

    node.start()
    rpc.start()

    with node.lock:
        current_height = node.chain.height
    print(f"{COIN_NAME} node {__version__} on {params.name}")
    print(f"  genesis  {genesis_hash(params)[::-1].hex()}")
    print(f"  height   {current_height}")
    print(f"  p2p      {node.p2p.host}:{node.p2p.port}")
    print(f"  rpc      127.0.0.1:{rpc.port}")
    if seeds:
        print(f"  peers    connecting to {len(seeds)}: " + ", ".join(
            f"{host}:{port}" for host, port in seeds[:3]
        ))
    else:
        print("  peers    none configured — this node is alone until someone")
        print("           connects to it, or you pass --connect host:port")
    if wallet is not None:
        print(f"  wallet   {wallet.addresses()[0]}")

    if args.mine:
        node.start_mining()
        print("  mining   started")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        rpc.stop()
        node.stop()


if __name__ == "__main__":
    main()
