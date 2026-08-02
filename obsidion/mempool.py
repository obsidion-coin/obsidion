"""The mempool: transactions heard about but not yet mined.

The mempool is each node's private waiting room. Nothing in it is consensus —
two nodes may hold different mempools and still agree perfectly on the chain.
Its job is threefold:

* **Gatekeeping.** A transaction enters only if it would be valid in the next
  block: inputs exist (in the chain or earlier in the pool), signatures check
  out, coinbase maturity is respected, and it double-spends nothing the pool has
  already promised. Invalid traffic dies here instead of wasting relay.

* **Prioritising.** Miners take transactions in fee-per-byte order — block space
  is the scarce good, so a small transaction paying well outranks a large one
  paying the same total.

* **Reorg recovery.** When a reorg disconnects blocks, their transactions are
  offered back via `reconsider`; whatever is still valid on the new branch
  re-enters the pool, so a payment caught on the losing side of a fork is
  re-mined rather than lost.
"""

from __future__ import annotations

from dataclasses import dataclass

from obsidion.chainstate import ChainState
from obsidion.consensus import ConsensusError
from obsidion.params import NetworkParams
from obsidion.transaction import OutPoint, Transaction


@dataclass
class MempoolEntry:
    """One waiting transaction, with the numbers selection cares about."""

    tx: Transaction
    fee: int
    size: int

    @property
    def txid(self) -> bytes:
        return self.tx.txid()

    @property
    def fee_rate(self) -> float:
        """Shards per byte — the number miners actually rank by."""
        return self.fee / self.size if self.size else 0.0


class Mempool:
    """Unconfirmed transactions, validated on entry and ordered by fee rate."""

    #: Total serialized bytes the pool will hold before it starts evicting.
    DEFAULT_MAX_SIZE = 50 * 1024 * 1024

    def __init__(self, params: NetworkParams, max_size: int | None = None):
        self.params = params
        self.max_size = max_size if max_size is not None else self.DEFAULT_MAX_SIZE
        self.entries: dict[bytes, MempoolEntry] = {}
        self.total_size = 0
        #: outpoint -> txid of the pooled transaction spending it. This is the
        #: double-spend index: one entry per outpoint, first spender wins.
        self.spends: dict[OutPoint, bytes] = {}

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, txid: bytes) -> bool:
        return txid in self.entries

    def get(self, txid: bytes) -> Transaction | None:
        entry = self.entries.get(txid)
        return entry.tx if entry else None

    # -------------------------------------------------------------- admission

    def accept(self, tx: Transaction, chain: ChainState) -> int:
        """Admit a transaction or raise ConsensusError. Returns the fee.

        Validation mirrors what `ChainState._connect` will enforce when the
        transaction is mined, evaluated as if it were in the next block. A
        transaction may spend the outputs of another pooled transaction, so
        chains of unconfirmed payments work.
        """
        if tx.is_coinbase():
            raise ConsensusError("a coinbase cannot enter the mempool")
        tx.check_structure()

        txid = tx.txid()
        if txid in self.entries:
            raise ConsensusError("transaction is already in the mempool")

        next_height = chain.height + 1
        input_total = 0

        for index, tx_input in enumerate(tx.inputs):
            outpoint = tx_input.prevout

            rival = self.spends.get(outpoint)
            if rival is not None:
                raise ConsensusError(
                    f"{tx!r} double-spends {outpoint!r}, already claimed by a "
                    "pooled transaction"
                )

            utxo = chain.get_utxo(outpoint)
            if utxo is not None:
                if (
                    utxo.is_coinbase
                    and next_height - utxo.height < self.params.coinbase_maturity
                ):
                    raise ConsensusError(
                        f"{tx!r} spends a coinbase that will not be mature in "
                        "the next block"
                    )
                amount, pubkey_hash = utxo.amount, utxo.pubkey_hash
            else:
                # Not in the chain — maybe it is the output of a transaction
                # waiting alongside it.
                parent = self.entries.get(outpoint.txid)
                if parent is None or outpoint.index >= len(parent.tx.outputs):
                    raise ConsensusError(
                        f"{tx!r} spends {outpoint!r}, which is unknown to the "
                        "chain and the mempool"
                    )
                output = parent.tx.outputs[outpoint.index]
                amount, pubkey_hash = output.amount, output.pubkey_hash

            if not tx.verify_input(index, pubkey_hash, amount):
                raise ConsensusError(f"{tx!r} input {index} has a bad signature")
            input_total += amount

        output_total = tx.total_output_value()
        if output_total > input_total:
            raise ConsensusError(
                f"{tx!r} outputs {output_total} but only {input_total} flows in"
            )

        fee = input_total - output_total
        entry = MempoolEntry(tx, fee, tx.size())

        # Bounded, or the pool is a remote memory-exhaustion target: valid
        # zero-fee transactions are free to create in bulk, and nothing ever
        # forced them out. Past the budget an arrival must outbid the cheapest
        # resident, which also makes flooding cost real money.
        if self.total_size + entry.size > self.max_size:
            if not self._make_room_for(entry):
                raise ConsensusError(
                    f"mempool is full ({self.total_size} bytes) and {tx!r} does "
                    f"not outbid the cheapest transaction in it"
                )

        self.entries[txid] = entry
        self.total_size += entry.size
        for tx_input in tx.inputs:
            self.spends[tx_input.prevout] = txid
        return fee

    def _make_room_for(self, incoming: MempoolEntry) -> bool:
        """Evict the worst-paying transactions until `incoming` fits.

        Returns False, changing nothing, if the incoming transaction does not
        pay better than what would have to be dropped for it.
        """
        candidates = sorted(self.entries.values(), key=lambda e: e.fee_rate)
        freed = 0
        doomed: list[bytes] = []

        for entry in candidates:
            if self.total_size - freed + incoming.size <= self.max_size:
                break
            if entry.fee_rate >= incoming.fee_rate:
                return False  # nothing left worth evicting
            doomed.append(entry.txid)
            freed += entry.size

        if self.total_size - freed + incoming.size > self.max_size:
            return False

        for txid in doomed:
            self._evict_with_descendants(txid)
        return True

    # ------------------------------------------------------------ maintenance

    def remove_confirmed(self, block) -> None:
        """Drop everything a newly connected block settles.

        Two kinds of removal: transactions the block *includes* (now confirmed,
        their pooled copies redundant), and transactions the block *conflicts
        with* — pooled spends of outpoints the block just consumed, which can
        never confirm now and are evicted along with anything built on them.
        """
        for tx in block.transactions:
            self._remove(tx.txid())
            for tx_input in tx.inputs:
                rival = self.spends.get(tx_input.prevout)
                if rival is not None:
                    self._evict_with_descendants(rival)

    def reconsider(self, txs: list[Transaction], chain: ChainState) -> int:
        """Offer reorg-disconnected transactions back to the pool.

        Whatever is valid on the new branch is readmitted; the rest (typically
        spends of coinbases that no longer exist) is dropped silently. Runs in
        passes because a child can only re-enter after its parent has.
        Returns how many were readmitted.
        """
        readmitted = 0
        pending = list(txs)
        for _ in range(len(pending) or 1):
            still_failing: list[Transaction] = []
            progressed = False
            for tx in pending:
                try:
                    self.accept(tx, chain)
                    readmitted += 1
                    progressed = True
                except ConsensusError:
                    still_failing.append(tx)
            pending = still_failing
            if not pending or not progressed:
                break
        return readmitted

    def _remove(self, txid: bytes) -> None:
        entry = self.entries.pop(txid, None)
        if entry is not None:
            self.total_size -= entry.size
            for tx_input in entry.tx.inputs:
                if self.spends.get(tx_input.prevout) == txid:
                    del self.spends[tx_input.prevout]

    def _evict_with_descendants(self, txid: bytes) -> None:
        """Evict a transaction and everything that spends its outputs — a child
        of an impossible parent is itself impossible."""
        entry = self.entries.get(txid)
        if entry is None:
            return
        for index in range(len(entry.tx.outputs)):
            child = self.spends.get(OutPoint(txid, index))
            if child is not None:
                self._evict_with_descendants(child)
        self._remove(txid)

    # -------------------------------------------------------------- selection

    def select(self, chain: ChainState, max_bytes: int) -> list[MempoolEntry]:
        """Choose transactions for a block template.

        Greedy by fee rate, with one structural constraint: a transaction whose
        inputs come from another pooled transaction can only be taken after its
        parent, whatever their relative fee rates. Entries that have quietly
        become invalid (their chain inputs vanished in a reorg) are skipped —
        selection double-checks rather than trusting admission-time state.
        """
        ranked = sorted(
            self.entries.values(), key=lambda e: (e.fee_rate, e.fee), reverse=True
        )

        chosen: list[MempoolEntry] = []
        chosen_ids: set[bytes] = set()
        provided: set[OutPoint] = set()
        used = 0

        progress = True
        while progress:
            progress = False
            for entry in ranked:
                if entry.txid in chosen_ids or used + entry.size > max_bytes:
                    continue

                satisfiable = all(
                    tx_input.prevout in provided
                    or chain.get_utxo(tx_input.prevout) is not None
                    for tx_input in entry.tx.inputs
                )
                if not satisfiable:
                    continue

                chosen.append(entry)
                chosen_ids.add(entry.txid)
                used += entry.size
                for index in range(len(entry.tx.outputs)):
                    provided.add(OutPoint(entry.txid, index))
                progress = True

        return chosen
