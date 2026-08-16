"""The bridge's memory: what has been minted, what has been released, once each.

Every way a lock-and-mint bridge loses money is a *double*. Mint twice for one
deposit and there is unbacked wOBSD in circulation; release twice for one burn
and the custody balance no longer covers what is outstanding. Both destroy the
single invariant the whole design rests on:

    wOBSD in circulation == OBSD held in custody

Doubles are not exotic. The service will crash mid-mint, restart, and re-see a
deposit it has already handled; an RPC call will time out after the node
actually did the work; a chain will reorganise a block out from under a deposit
that was already credited. All of that is ordinary operation.

So this module is deliberately dull. It is an append-only record keyed by each
event's *own* identity — a deposit is `(txid, index)`, a burn is its Solana
signature — and the only question it ever answers is "have I already done this
one?". It never reasons from balances, because a balance is a race: two
handlers reading the same balance both conclude they should act.

It holds no keys, touches no network, and knows nothing about Solana or the
Obsidion RPC. That is what makes the dangerous part of a bridge testable
without a chain, without a wallet, and without money.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class State(str, Enum):
    """Where an event has got to. Only ever moves forward."""

    #: Seen on-chain, not yet buried under enough blocks to be safe.
    PENDING = "pending"
    #: Confirmed deep enough to act on, but the action has not completed.
    READY = "ready"
    #: The other chain has been told. Never acted on again.
    DONE = "done"
    #: Disappeared in a reorg before it was acted on. Terminal, and harmless.
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class Deposit:
    """OBSD arriving at a deposit address, destined for a Solana wallet."""

    txid: str
    index: int
    #: Shards. Integers only — money never touches a float.
    amount: int
    #: The Obsidion address it landed on, which is what identifies the user.
    deposit_address: str
    #: Where the wrapped tokens should go.
    solana_address: str
    #: Block height it confirmed at, for measuring depth.
    height: int

    @property
    def key(self) -> str:
        """An outpoint can only ever be created once, so it is its own id."""
        return f"{self.txid}:{self.index}"


@dataclass(frozen=True)
class Burn:
    """wOBSD destroyed on Solana, asking for OBSD back on Obsidion."""

    #: The Solana transaction signature. Unique, and the natural identity.
    signature: str
    amount: int
    #: Where to send the released OBSD.
    obsidion_address: str

    @property
    def key(self) -> str:
        return self.signature


class BridgeLedger:
    """Durable, idempotent record of everything the bridge has acted on.

    Backed by SQLite so a crash between "sent the mint" and "wrote it down"
    cannot happen in the wrong order — the record is written first, inside a
    transaction, and only then is the other chain told. Doing it the other way
    round is how a restart mints twice.
    """

    def __init__(self, path: str | Path = ":memory:", *, confirmations: int = 20):
        #: Blocks a deposit must be buried under before it can be minted.
        #: Obsidion resolves forks by most cumulative work, so a shallow block
        #: can still be reorganised away — and minting against a deposit that
        #: later vanishes creates wOBSD nothing backs.
        self.confirmations = confirmations
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self.db:
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    key         TEXT PRIMARY KEY,
                    kind        TEXT NOT NULL,
                    state       TEXT NOT NULL,
                    amount      INTEGER NOT NULL,
                    payload     TEXT NOT NULL,
                    height      INTEGER,
                    receipt     TEXT
                )
                """
            )
            # Deposit addresses are handed out one per destination, because an
            # Obsidion transaction has nowhere to carry a memo. See BRIDGE.md.
            self.db.execute(
                """
                CREATE TABLE IF NOT EXISTS destinations (
                    deposit_address TEXT PRIMARY KEY,
                    solana_address  TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- addresses

    def assign_destination(self, deposit_address: str, solana_address: str) -> None:
        """Record that coins arriving at `deposit_address` belong to `solana_address`.

        Refuses to re-point an address that is already spoken for. Reassigning
        one would send a previous user's incoming deposit to somebody else, and
        addresses are public — an old one can receive at any time.
        """
        with self._lock, self.db:
            existing = self.destination_for(deposit_address)
            if existing is not None and existing != solana_address:
                raise ValueError(
                    f"{deposit_address} is already assigned to {existing}; "
                    "reassigning would misdirect a later deposit"
                )
            self.db.execute(
                "INSERT OR REPLACE INTO destinations VALUES (?, ?)",
                (deposit_address, solana_address),
            )

    def destination_for(self, deposit_address: str) -> str | None:
        row = self.db.execute(
            "SELECT solana_address FROM destinations WHERE deposit_address = ?",
            (deposit_address,),
        ).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- events

    def record_deposit(self, deposit: Deposit) -> State:
        """Note a deposit seen on-chain. Safe to call repeatedly.

        Returns its current state. Re-seeing a deposit that is already done is
        the normal case after a restart, and must change nothing.
        """
        return self._record(
            key=deposit.key,
            kind="deposit",
            amount=deposit.amount,
            height=deposit.height,
            payload={
                "deposit_address": deposit.deposit_address,
                "solana_address": deposit.solana_address,
            },
        )

    def record_burn(self, burn: Burn) -> State:
        """Note a burn seen on Solana. Safe to call repeatedly."""
        return self._record(
            key=burn.key,
            kind="burn",
            amount=burn.amount,
            height=None,
            payload={"obsidion_address": burn.obsidion_address},
        )

    def _record(self, *, key, kind, amount, height, payload) -> State:
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT state, amount FROM events WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                # An event's identity implies its amount. If the same key shows
                # up with a different one, something upstream is confused and
                # acting on it could mint the wrong quantity.
                if row[1] != amount:
                    raise ValueError(
                        f"{key} was recorded as {row[1]} shards but has now "
                        f"arrived as {amount}; refusing to guess which is real"
                    )
                return State(row[0])

            self.db.execute(
                "INSERT INTO events (key, kind, state, amount, payload, height) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, kind, State.PENDING.value, amount, json.dumps(payload), height),
            )
            return State.PENDING

    def state_of(self, key: str) -> State | None:
        row = self.db.execute(
            "SELECT state FROM events WHERE key = ?", (key,)
        ).fetchone()
        return State(row[0]) if row else None

    # ------------------------------------------------------------ transitions

    def promote_confirmed(self, tip_height: int) -> list[str]:
        """Move deposits buried deep enough from PENDING to READY.

        Depth is measured against the chain tip rather than trusted from the
        event, so a caller cannot accidentally mark something ready by passing
        a stale height.
        """
        with self._lock, self.db:
            rows = self.db.execute(
                "SELECT key, height FROM events "
                "WHERE kind = 'deposit' AND state = ?",
                (State.PENDING.value,),
            ).fetchall()

            promoted = []
            for key, height in rows:
                if height is None:
                    continue
                if tip_height - height + 1 >= self.confirmations:
                    self.db.execute(
                        "UPDATE events SET state = ? WHERE key = ?",
                        (State.READY.value, key),
                    )
                    promoted.append(key)
            return promoted

    def mark_ready(self, key: str) -> None:
        """Burns have no depth to wait for beyond Solana's own finality, so the
        watcher decides when they are actionable."""
        with self._lock, self.db:
            self._require_state(key, {State.PENDING})
            self.db.execute(
                "UPDATE events SET state = ? WHERE key = ?", (State.READY.value, key)
            )

    def mark_done(self, key: str, receipt: str) -> None:
        """Record that the other chain has been told, with proof.

        Write this *before* believing the job is finished, and only from a
        caller that has a receipt in hand — a mint signature, a released txid.
        A DONE row with no receipt is indistinguishable from a lie.
        """
        if not receipt:
            raise ValueError("refusing to mark done without a receipt")
        with self._lock, self.db:
            state = self._require_state(key, {State.READY, State.DONE})
            if state is State.DONE:
                return  # already finished; re-running must be harmless
            self.db.execute(
                "UPDATE events SET state = ?, receipt = ? WHERE key = ?",
                (State.DONE.value, receipt, key),
            )

    def abandon(self, key: str, reason: str) -> None:
        """A deposit that vanished in a reorg before it was acted on.

        Refused once the event is DONE: at that point the wOBSD exists, and
        pretending the deposit never happened would leave it unbacked. That
        situation needs a human, not a state transition.
        """
        with self._lock, self.db:
            state = self.state_of(key)
            if state is State.DONE:
                raise ValueError(
                    f"{key} was already acted on; abandoning it now would leave "
                    "wrapped tokens with nothing behind them"
                )
            if state is None:
                return
            self.db.execute(
                "UPDATE events SET state = ?, receipt = ? WHERE key = ?",
                (State.ABANDONED.value, f"abandoned: {reason}", key),
            )

    def _require_state(self, key: str, allowed: set[State]) -> State:
        state = self.state_of(key)
        if state is None:
            raise KeyError(f"unknown event {key}")
        if state not in allowed:
            raise ValueError(
                f"{key} is {state.value}; expected one of "
                f"{sorted(s.value for s in allowed)}"
            )
        return state

    # ----------------------------------------------------------------- views

    def actionable(self, kind: str) -> list[tuple[str, int, dict]]:
        """Events ready to be acted on: (key, amount, payload)."""
        rows = self.db.execute(
            "SELECT key, amount, payload FROM events "
            "WHERE kind = ? AND state = ? ORDER BY rowid",
            (kind, State.READY.value),
        ).fetchall()
        return [(key, amount, json.loads(payload)) for key, amount, payload in rows]

    def wrapped_supply(self) -> int:
        """Shards that *should* exist as wOBSD: minted, less released.

        The number to compare against the Solana mint's actual supply and the
        custody address's balance. Three sources that must agree; any
        disagreement means stop and investigate, not carry on.
        """
        minted = self._total("deposit", State.DONE)
        released = self._total("burn", State.DONE)
        return minted - released

    def _total(self, kind: str, state: State) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM events "
            "WHERE kind = ? AND state = ?",
            (kind, state.value),
        ).fetchone()
        return int(row[0])
