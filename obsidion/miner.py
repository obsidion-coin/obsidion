"""Mining: assembling block templates and searching for valid proof-of-work.

Mining is two separate jobs, kept separate here:

**Template assembly** (`build_template`) is bookkeeping: take the highest-paying
mempool transactions that fit, build a coinbase claiming subsidy plus their
fees, and wrap it all in a header carrying the difficulty and timestamp the
consensus rules demand. Everything but the nonce is settled at this point.

**The search** (`grind`, or the `Miner` thread) is brute force: hash the header,
increment the nonce, repeat. Nothing about it is clever, and that is the point —
the only way to produce a valid block is to actually do the work.

The 32-bit nonce allows ~4.3 billion attempts per template. If a miner exhausts
it, the *extra nonce* in the coinbase is incremented instead, which changes the
merkle root and yields a fresh 4.3 billion. Between the two, the search space is
effectively unlimited.

The `Miner` thread checks between batches whether the chain tip moved (someone
else found the block first — start over on top of theirs) or the mempool grew
(rebuild to capture new fees). This is what makes mining a *race* rather than a
lottery drawn once.
"""

from __future__ import annotations

import threading
import time

from obsidion import consensus
from obsidion.block import Block, BlockHeader
from obsidion.chainstate import ChainState
from obsidion.mempool import Mempool
from obsidion.merkle import merkle_root
from obsidion.transaction import Transaction

#: Bytes reserved in a template for the coinbase transaction.
COINBASE_RESERVE = 250

#: Nonces tried between stop-flag and stale-tip checks, per algorithm.
#:
#: Sized so a batch lasts a fraction of a second. Too large and the miner keeps
#: grinding a template that someone else has already beaten; too small and it
#: spends its time taking locks instead of hashing. A memory-hard hash is some
#: four thousand times slower than SHA-256d, so one constant cannot serve both.
BATCH_BY_ALGORITHM = {
    "sha256d": 25_000,
    "scrypt-2mb": 64,
}
DEFAULT_BATCH = 256


def build_template(
    chain: ChainState,
    mempool: Mempool | None,
    miner_pubkey_hash: bytes,
    *,
    extra_nonce: int = 0,
    message: bytes = b"",
    now: float | None = None,
) -> Block:
    """Assemble a block ready for proof-of-work, built on the current tip."""
    if now is None:
        now = time.time()

    parent = chain.tip
    height = parent.height + 1

    entries = (
        mempool.select(chain, chain.params.max_block_weight - COINBASE_RESERVE)
        if mempool is not None
        else []
    )
    fees = sum(entry.fee for entry in entries)

    coinbase = Transaction.coinbase(
        height=height,
        reward=consensus.subsidy(height, chain.params) + fees,
        pubkey_hash=miner_pubkey_hash,
        extra_nonce=extra_nonce,
        message=message,
    )
    transactions = [coinbase, *(entry.tx for entry in entries)]

    header = BlockHeader(
        prev_hash=parent.hash,
        merkle_root=merkle_root([t.txid() for t in transactions]),
        # A timestamp must beat the median of recent blocks even if the local
        # clock lags — otherwise an honest miner on a skewed clock would
        # produce nothing but rejects.
        timestamp=max(int(now), chain.min_timestamp_for_next(parent.hash)),
        bits=chain.expected_bits_for_next(parent.hash),
    )
    return Block(header, transactions)


def grind(
    block: Block,
    algorithm: str,
    *,
    max_nonce: int = 0xFFFFFFFF,
    should_stop=None,
) -> bool:
    """Search the nonce space until the header meets its target.

    Returns True with the winning nonce left in place, or False if the space
    was exhausted or `should_stop` said to give up.
    """
    header = block.header
    while True:
        if header.satisfies_pow(algorithm):
            return True
        if header.nonce >= max_nonce:
            return False
        if should_stop is not None and should_stop():
            return False
        header.nonce += 1


def mine_block(
    chain: ChainState,
    mempool: Mempool | None,
    miner_pubkey_hash: bytes,
    *,
    message: bytes = b"",
    now: float | None = None,
) -> Block:
    """Build a template and grind until solved — the regtest workhorse.

    Rolls the extra nonce and rebuilds if a template's nonce space runs dry,
    so it always returns a solved block eventually.
    """
    extra_nonce = 0
    while True:
        block = build_template(
            chain,
            mempool,
            miner_pubkey_hash,
            extra_nonce=extra_nonce,
            message=message,
            now=now,
        )
        if grind(block, chain.params.pow_algorithm):
            return block
        extra_nonce += 1


class Miner:
    """A background mining thread.

    Found blocks are handed to `on_block` — typically the node's submit path,
    which validates and broadcasts them. The thread never touches chain state
    directly with a solved block; it only reads state to build templates, and
    the node decides what a solution means.
    """

    def __init__(
        self,
        chain: ChainState,
        mempool: Mempool | None,
        miner_pubkey_hash: bytes,
        on_block,
        *,
        message: bytes = b"",
        lock: threading.RLock | None = None,
    ):
        self.chain = chain
        self.mempool = mempool
        self.miner_pubkey_hash = miner_pubkey_hash
        self.on_block = on_block
        self.message = message
        #: Serializes chain access against the node's other threads. SQLite
        #: connections are not safe under concurrent use, so every read this
        #: thread makes must hold the same lock the RPC and network hold.
        self.lock = lock or threading.RLock()

        self.hashes_tried = 0
        self._started_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="obsidion-miner", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def hashrate(self) -> float:
        """Rough hashes per second since the miner started."""
        if self._started_at is None:
            return 0.0
        elapsed = time.monotonic() - self._started_at
        return self.hashes_tried / elapsed if elapsed > 0 else 0.0

    # ------------------------------------------------------------- internals

    def _run(self) -> None:
        extra_nonce = 0
        batch = BATCH_BY_ALGORITHM.get(
            self.chain.params.pow_algorithm, DEFAULT_BATCH
        )
        while not self._stop.is_set():
            with self.lock:
                template_tip = self.chain.tip_hash
                template_txs = len(self.mempool) if self.mempool is not None else 0
                block = build_template(
                    self.chain,
                    self.mempool,
                    self.miner_pubkey_hash,
                    extra_nonce=extra_nonce,
                    message=self.message,
                )

            solved = False
            while not self._stop.is_set():
                # The grind itself runs outside the lock — it touches nothing
                # but the header, and it is 99.9% of this thread's life.
                started_at_nonce = block.header.nonce
                batch_end = min(block.header.nonce + batch, 0xFFFFFFFF)
                solved = grind(
                    block, self.chain.params.pow_algorithm, max_nonce=batch_end
                )
                # Count nonces actually tried, not the batch size. Crediting a
                # full batch on a batch that solved early inflated the reported
                # hashrate, and that number is shown to users in the explorer.
                self.hashes_tried += block.header.nonce - started_at_nonce + 1
                if solved:
                    break
                if block.header.nonce >= 0xFFFFFFFF:
                    extra_nonce += 1  # nonce space dry; roll the coinbase
                    break
                # Stale template? Someone else extended the chain, or new fees
                # arrived. Rebuild rather than finish work that cannot win.
                with self.lock:
                    tip_moved = self.chain.tip_hash != template_tip
                    pool_changed = (
                        self.mempool is not None
                        and len(self.mempool) != template_txs
                    )
                if tip_moved or pool_changed:
                    break

            if solved:
                extra_nonce = 0
                self.on_block(block)
