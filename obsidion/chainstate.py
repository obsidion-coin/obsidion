"""The chain state: which blocks form the active chain, and who owns what.

This module holds the two responsibilities that make a blockchain a ledger
rather than a pile of valid blocks:

**The UTXO set.** Connecting a block deletes the outputs it spends and inserts
the outputs it creates. Every deletion is recorded in an *undo record* so the
block can later be disconnected — reinsert what was spent, delete what was
created — leaving state bit-identical to before it connected.

**Fork choice.** The active chain is the known branch with the most cumulative
work — not the most blocks. When a heavier branch appears, `accept_block` walks
the current chain back to the fork point, disconnects each block using its undo
record, connects the rival branch, and hands back the transactions that fell
out so the mempool can reconsider them.

The whole of a connection or reorg runs inside one SQLite transaction. If any
block in a rival branch turns out to be invalid — which can only be discovered
during the reorg, because side-chain transactions cannot be validated without
that branch's UTXO set — a single ROLLBACK restores the previous chain exactly.
This is the property the reorg tests lean on hardest, and the reason ChainState
deliberately keeps no state outside the database.

Transaction validation order within a block matters and is defined here: each
non-coinbase transaction is fully processed (inputs deleted, outputs inserted)
before the next begins, so a transaction may spend the outputs of an earlier
transaction in its own block, and a later double-spend of the same output fails
because the entry is already gone.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field, replace
from sqlite3 import IntegrityError

from obsidion import consensus
from obsidion.block import Block
from obsidion.consensus import ConsensusError
from obsidion.genesis import create_genesis_block
from obsidion.params import NetworkParams
from obsidion.storage import ChainDB, IndexEntry
from obsidion.transaction import OutPoint, Transaction, read_varint, write_varint

_TIP_KEY = "tip"

#: Serialized undo entry: txid, index, amount, pubkey_hash, height, coinbase flag.
_UNDO_FORMAT = "<32sIq20sIB"
_UNDO_SIZE = struct.calcsize(_UNDO_FORMAT)


class OrphanError(ConsensusError):
    """The block's parent is unknown.

    Split out from plain ConsensusError because the correct response differs:
    an invalid block is discarded and its sender penalised, while an orphan is
    a cue to fetch the missing parent — it may be perfectly valid.
    """


@dataclass(frozen=True)
class UTXOEntry:
    """One unspent output: an amount, its owner, and enough context to enforce
    coinbase maturity."""

    amount: int
    pubkey_hash: bytes
    height: int
    is_coinbase: bool


@dataclass
class ConnectResult:
    """What happened when a block was accepted.

    status is one of:
      'connected'  extended the active tip
      'reorged'    triggered a reorganisation to a heavier branch
      'sidechain'  stored, but its branch has less work than the active chain
      'duplicate'  seen before; nothing changed

    `disconnected` holds transactions orphaned by a reorg, newest block's first,
    so the mempool can reconsider them. `connected` holds the hashes of blocks
    that became active, oldest first, so the mempool can drop what they confirm.
    """

    status: str
    disconnected: list[Transaction] = field(default_factory=list)
    connected: list[bytes] = field(default_factory=list)


class ChainState:
    """The authoritative view of one network's chain, backed by SQLite."""

    def __init__(self, params: NetworkParams, path: str = ":memory:"):
        self.params = params
        self.db = ChainDB(path)
        self._ensure_genesis()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- properties

    @property
    def tip_hash(self) -> bytes:
        value = self.db.get_meta(_TIP_KEY)
        assert value is not None, "chain has no tip; genesis initialisation failed"
        return value

    @property
    def tip(self) -> IndexEntry:
        entry = self.db.get_index(self.tip_hash)
        assert entry is not None
        return entry

    @property
    def height(self) -> int:
        return self.tip.height

    # ---------------------------------------------------------------- lookups

    def index_entry(self, block_hash: bytes) -> IndexEntry | None:
        return self.db.get_index(block_hash)

    def get_block(self, block_hash: bytes) -> Block | None:
        raw = self.db.get_block_raw(block_hash)
        return Block.deserialize(raw) if raw is not None else None

    def block_at_height(self, height: int) -> Block | None:
        block_hash = self.db.active_hash_at_height(height)
        return self.get_block(block_hash) if block_hash is not None else None

    def get_utxo(self, outpoint: OutPoint) -> UTXOEntry | None:
        row = self.db.get_utxo(outpoint.txid, outpoint.index)
        if row is None:
            return None
        amount, pubkey_hash, height, coinbase = row
        return UTXOEntry(amount, pubkey_hash, height, bool(coinbase))

    def utxos_for_pubkey_hash(
        self, pubkey_hash: bytes
    ) -> list[tuple[OutPoint, UTXOEntry]]:
        """Every unspent output owned by `pubkey_hash` — a wallet's raw balance."""
        return [
            (
                OutPoint(txid, index),
                UTXOEntry(amount, pubkey_hash, height, bool(coinbase)),
            )
            for txid, index, amount, height, coinbase in self.db.utxos_for_pubkey_hash(
                pubkey_hash
            )
        ]

    def total_utxo_value(self) -> int:
        """Sum of every unspent output. Equals the circulating supply unless a
        miner has claimed less than their due (allowed, and their loss)."""
        return self.db.sum_utxos()

    def all_utxo_outpoints(self) -> list[tuple[bytes, int]]:
        return self.db.all_utxo_outpoints()

    # ------------------------------------------------- template-building help

    def expected_bits_for_next(self, parent_hash: bytes) -> int:
        """The difficulty a block extending `parent_hash` must declare.
        Used by the miner to build templates and by validation to check them."""
        parent = self.db.get_index(parent_hash)
        if parent is None:
            raise OrphanError(f"unknown parent {parent_hash[::-1].hex()}")

        height = parent.height + 1
        period_start = self._ancestor_at_height(
            parent, consensus.retarget_period_start_height(height, self.params)
        )
        return consensus.expected_bits(height, self.params, parent, period_start)

    def min_timestamp_for_next(self, parent_hash: bytes) -> int:
        """The earliest timestamp a child of `parent_hash` may carry."""
        parent = self.db.get_index(parent_hash)
        if parent is None:
            raise OrphanError(f"unknown parent {parent_hash[::-1].hex()}")
        return consensus.median_time_past(self._recent_timestamps(parent)) + 1

    # ------------------------------------------------------------- acceptance

    def accept_block(self, block: Block, now: float | None = None) -> ConnectResult:
        """Validate a block and fold it into the chain.

        Raises OrphanError if the parent is unknown, ConsensusError if any rule
        is broken. On success returns what happened — see ConnectResult.
        """
        if now is None:
            now = time.time()

        block_hash = block.hash()
        existing = self.db.get_index(block_hash)
        if existing is not None:
            if existing.status == "invalid":
                raise ConsensusError(
                    f"block {block.hash_hex()} was previously rejected"
                )
            return ConnectResult("duplicate")

        # Context-free rules first: cheap, and independent of chain state.
        consensus.check_block_structure(block, self.params)

        parent = self.db.get_index(block.header.prev_hash)
        if parent is None:
            raise OrphanError(
                f"block {block.hash_hex()} extends unknown parent "
                f"{block.header.prev_hash[::-1].hex()}"
            )
        if parent.status == "invalid":
            raise ConsensusError("block builds on a known-invalid block")

        height = parent.height + 1
        entry = IndexEntry(
            hash=block_hash,
            prev_hash=parent.hash,
            height=height,
            cumwork=parent.cumwork + block.header.work(),
            timestamp=block.header.timestamp,
            bits=block.header.bits,
            active=False,
            status="valid",
        )

        # Contextual rules that the block hash *does* commit to. Height, bits
        # and timestamp all live in the header or the coinbase, so a block
        # failing one of these can never be made to pass while keeping this
        # hash. That makes the verdict permanent and safe to cache.
        try:
            consensus.check_coinbase_height(block, height)
            consensus.check_expected_bits(
                block.header, self.expected_bits_for_next(parent.hash)
            )
            consensus.check_timestamp(
                block.header, self._recent_timestamps(parent), int(now), self.params
            )
        except ConsensusError:
            self._remember_invalid(block, entry)
            raise

        tip = self.tip
        self.db.begin()
        try:
            self.db.put_block(block_hash, block.serialize())
            self.db.put_index(entry)

            if entry.cumwork <= tip.cumwork:
                # Less work than the active chain: remember it and move on. Its
                # transactions stay unvalidated until its branch ever wins.
                self.db.commit()
                return ConnectResult("sidechain")

            disconnected, connected = self._reorg_to(entry)
            self.db.set_meta(_TIP_KEY, block_hash)
            self.db.commit()
        except ConsensusError:
            # One rollback erases every effect of the failed attempt, including
            # partial reorg work.
            #
            # Deliberately NOT cached as invalid. Failures down here come from
            # `_connect`, which checks signatures — and signature bytes are the
            # one part of a block the hash does not commit to, precisely because
            # txids are computed over the unsigned form to defeat malleability.
            # An attacker can therefore take a valid block, corrupt one
            # signature, and hand it over with an unchanged hash. Caching that
            # verdict would make the node reject the genuine block forever and
            # fork itself off the network permanently. Re-validating costs a few
            # signature checks; the peer that sent it is banned regardless.
            self.db.rollback()
            raise

        status = "reorged" if disconnected else "connected"
        return ConnectResult(status, disconnected, connected)

    # -------------------------------------------------------------- internals

    def _remember_invalid(self, block: Block, entry: IndexEntry) -> None:
        """Record a permanent verdict of invalid for a block.

        Only ever called for failures the block hash commits to. See the note
        in `accept_block` for why signature failures must not come through here.
        """
        self.db.begin()
        self.db.put_block(entry.hash, block.serialize())
        self.db.put_index(replace(entry, status="invalid"))
        self.db.commit()

    def _ensure_genesis(self) -> None:
        if self.db.get_meta(_TIP_KEY) is not None:
            return

        genesis = create_genesis_block(self.params)
        genesis_hash = genesis.hash()

        self.db.begin()
        self.db.put_block(genesis_hash, genesis.serialize())
        self.db.put_index(
            IndexEntry(
                hash=genesis_hash,
                prev_hash=b"\x00" * 32,
                height=0,
                cumwork=genesis.header.work(),
                timestamp=genesis.header.timestamp,
                bits=genesis.header.bits,
                active=True,
                status="valid",
            )
        )
        coinbase = genesis.coinbase()
        for index, output in enumerate(coinbase.outputs):
            self.db.add_utxo(
                coinbase.txid(), index, output.amount, output.pubkey_hash, 0, True
            )
        self.db.put_undo(genesis_hash, write_varint(0))
        self.db.set_meta(_TIP_KEY, genesis_hash)
        self.db.commit()

    def _ancestor_at_height(self, entry: IndexEntry, height: int) -> IndexEntry:
        """Walk parent links back to `height` along `entry`'s own branch."""
        cursor = entry
        while cursor.height > height:
            parent = self.db.get_index(cursor.prev_hash)
            assert parent is not None, "index contains a dangling parent link"
            cursor = parent
        return cursor

    def _recent_timestamps(self, parent: IndexEntry) -> list[int]:
        """Timestamps of the last `median_time_blocks` blocks ending at `parent`."""
        timestamps: list[int] = []
        cursor: IndexEntry | None = parent
        while cursor is not None and len(timestamps) < self.params.median_time_blocks:
            timestamps.append(cursor.timestamp)
            if cursor.height == 0:
                break
            cursor = self.db.get_index(cursor.prev_hash)
        return timestamps

    def _reorg_to(
        self, candidate: IndexEntry
    ) -> tuple[list[Transaction], list[bytes]]:
        """Make `candidate`'s branch the active chain. Runs inside the caller's
        database transaction; raises ConsensusError if any branch block fails
        validation, leaving rollback to the caller.

        Returns `(disconnected, connected)`: the non-coinbase transactions from
        disconnected blocks (newest first, for the mempool to reconsider) and
        the hashes of newly active blocks (oldest first).
        """
        # Walk from the candidate down to the first block still on the active
        # chain: that block is the fork point, and the walk is the branch to
        # connect. For a plain tip extension the branch is just the candidate.
        branch: list[IndexEntry] = []
        cursor = candidate
        while not cursor.active:
            branch.append(cursor)
            parent = self.db.get_index(cursor.prev_hash)
            assert parent is not None, "reorg walked off the known block tree"
            cursor = parent
        fork = cursor
        branch.reverse()

        # Disconnect the active chain, tip first, back to the fork point.
        disconnected: list[Transaction] = []
        tip_hash = self.tip_hash
        while tip_hash != fork.hash:
            disconnected.extend(self._disconnect(tip_hash))
            entry = self.db.get_index(tip_hash)
            assert entry is not None
            tip_hash = entry.prev_hash

        # Connect the rival branch, oldest first. Any failure propagates and
        # the caller's rollback undoes everything, disconnections included.
        for entry in branch:
            raw = self.db.get_block_raw(entry.hash)
            assert raw is not None, "indexed block has no stored body"
            self._connect(Block.deserialize(raw), entry.height)
            self.db.set_active(entry.hash, True)

        return disconnected, [entry.hash for entry in branch]

    def _connect(self, block: Block, height: int) -> None:
        """Apply one block to the UTXO set, or raise ConsensusError.

        This is where a block's transactions meet reality: every input must
        exist, be mature if a coinbase, and carry a valid signature; no
        transaction may emit more than it consumes; and the coinbase may claim
        at most subsidy plus the fees actually present in this block.
        """
        undo_entries: list[bytes] = []
        fees = 0

        for transaction in block.transactions[1:]:
            input_total = 0
            for index, tx_input in enumerate(transaction.inputs):
                prevout = tx_input.prevout
                row = self.db.get_utxo(prevout.txid, prevout.index)
                if row is None:
                    raise ConsensusError(
                        f"{transaction!r} input {index} spends {prevout!r}, "
                        "which is missing or already spent"
                    )
                amount, pubkey_hash, utxo_height, is_coinbase = row

                if is_coinbase and height - utxo_height < self.params.coinbase_maturity:
                    raise ConsensusError(
                        f"{transaction!r} spends a coinbase aged "
                        f"{height - utxo_height} blocks; maturity is "
                        f"{self.params.coinbase_maturity}"
                    )
                if not transaction.verify_input(index, pubkey_hash, amount):
                    raise ConsensusError(
                        f"{transaction!r} input {index} has a bad signature"
                    )

                undo_entries.append(
                    struct.pack(
                        _UNDO_FORMAT,
                        prevout.txid,
                        prevout.index,
                        amount,
                        pubkey_hash,
                        utxo_height,
                        is_coinbase,
                    )
                )
                self.db.delete_utxo(prevout.txid, prevout.index)
                input_total += amount

            output_total = transaction.total_output_value()
            if output_total > input_total:
                raise ConsensusError(
                    f"{transaction!r} outputs {output_total} but only "
                    f"{input_total} flows in — transactions cannot create money"
                )
            fees += input_total - output_total

            # Outputs become spendable immediately, so a later transaction in
            # this same block may consume them.
            self._add_outputs(transaction, height, is_coinbase=False)

        consensus.check_coinbase_reward(block, height, fees, self.params)
        self._add_outputs(block.coinbase(), height, is_coinbase=True)

        self.db.put_undo(
            block.hash(), write_varint(len(undo_entries)) + b"".join(undo_entries)
        )

    def _add_outputs(
        self, transaction: Transaction, height: int, is_coinbase: bool
    ) -> None:
        txid = transaction.txid()
        for index, output in enumerate(transaction.outputs):
            try:
                self.db.add_utxo(
                    txid, index, output.amount, output.pubkey_hash, height, is_coinbase
                )
            except IntegrityError:
                # BIP-30: a transaction id may not overwrite an existing unspent
                # entry. Coinbase height encoding makes this near-impossible,
                # but near-impossible is not a consensus rule; this is.
                raise ConsensusError(
                    f"{transaction!r} would overwrite an existing unspent output"
                ) from None

    def _disconnect(self, block_hash: bytes) -> list[Transaction]:
        """Remove the block at `block_hash` from the UTXO set using its undo
        record, and return its non-coinbase transactions.

        **The order of these two passes is a consensus rule, not a style
        choice.** A block may contain a chain of spends within itself — B
        spending an output A created a few transactions earlier. Connecting
        recorded an undo entry for that output, because from the UTXO set's
        point of view it really was spent. Disconnecting must therefore both
        restore what the block consumed and delete what it produced, and A's
        output is in *both* sets.

        Restoring first and deleting second resolves that overlap correctly:
        the resurrected entry is then removed again, because the block that
        created it is going away. Doing it the other way round leaves the
        entry alive — coins conjured out of nothing, and a node that silently
        disagrees with every peer that got it right.
        """
        raw = self.db.get_block_raw(block_hash)
        assert raw is not None, "cannot disconnect a block that was never stored"
        block = Block.deserialize(raw)

        # First, resurrect every output this block spent.
        undo_raw = self.db.get_undo(block_hash)
        assert undo_raw is not None, "block connected without an undo record"
        count, offset = read_varint(undo_raw, 0)
        for _ in range(count):
            txid, index, amount, pubkey_hash, height, coinbase = struct.unpack_from(
                _UNDO_FORMAT, undo_raw, offset
            )
            offset += _UNDO_SIZE
            self.db.add_utxo(txid, index, amount, pubkey_hash, height, bool(coinbase))

        # Then delete every output it created, including any the pass above
        # just restored because they were spent within this same block.
        for transaction in reversed(block.transactions):
            txid = transaction.txid()
            for index in range(len(transaction.outputs)):
                self.db.delete_utxo(txid, index)

        self.db.set_active(block_hash, False)
        return [t for t in block.transactions if not t.is_coinbase()]
