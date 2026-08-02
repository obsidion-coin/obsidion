"""SQLite persistence for the chain.

This module knows SQL and nothing else. Consensus logic lives in `chainstate`;
this layer just stores what it is told and hands it back. Keeping every query in
one file means the schema can evolve without touching validation code, and
validation code can be read without wading through SQL.

One design decision worth stating: **the database is the only copy of chain
state.** ChainState keeps no in-memory UTXO cache. That costs some speed, but it
buys a property that matters far more here: wrapping a whole block connection —
or an entire multi-block reorg — in a single SQLite transaction makes it atomic.
If anything fails part-way, one ROLLBACK restores the exact prior state. Crash
recovery and reorg-failure recovery become the same trivial operation instead of
the hardest code in the system.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    hash    BLOB PRIMARY KEY,
    raw     BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS block_index (
    hash      BLOB PRIMARY KEY,
    prev_hash BLOB NOT NULL,
    height    INTEGER NOT NULL,
    cumwork   TEXT NOT NULL,          -- 256-bit int as hex; exceeds SQLite INTEGER
    timestamp INTEGER NOT NULL,
    bits      INTEGER NOT NULL,
    active    INTEGER NOT NULL DEFAULT 0,
    status    TEXT NOT NULL DEFAULT 'valid'
);
CREATE INDEX IF NOT EXISTS idx_index_height ON block_index(active, height);

CREATE TABLE IF NOT EXISTS utxos (
    txid        BLOB NOT NULL,
    idx         INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    pubkey_hash BLOB NOT NULL,
    height      INTEGER NOT NULL,
    coinbase    INTEGER NOT NULL,
    PRIMARY KEY (txid, idx)
);
CREATE INDEX IF NOT EXISTS idx_utxo_owner ON utxos(pubkey_hash);

CREATE TABLE IF NOT EXISTS undo (
    hash    BLOB PRIMARY KEY,
    raw     BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   BLOB NOT NULL
);
"""


@dataclass(frozen=True)
class IndexEntry:
    """One block's position in the tree of all known blocks.

    Carries `bits` and `timestamp` so difficulty and median-time checks can walk
    ancestors without deserializing full blocks. `consensus.expected_bits`
    accepts these entries directly for the same reason.
    """

    hash: bytes
    prev_hash: bytes
    height: int
    cumwork: int
    timestamp: int
    bits: int
    active: bool
    status: str


class ChainDB:
    """Thin, explicit wrapper around the SQLite chain database."""

    def __init__(self, path: str = ":memory:"):
        # isolation_level=None puts sqlite3 in autocommit mode, which hands
        # transaction control to us: nothing commits until we say COMMIT.
        # check_same_thread=False lets the miner thread read chain state; the
        # node serializes all access behind a single lock, so SQLite never sees
        # genuine concurrency.
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------- transactions

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    # ----------------------------------------------------------------- blocks

    def put_block(self, block_hash: bytes, raw: bytes) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO blocks(hash, raw) VALUES (?, ?)",
            (block_hash, raw),
        )

    def get_block_raw(self, block_hash: bytes) -> bytes | None:
        row = self.conn.execute(
            "SELECT raw FROM blocks WHERE hash = ?", (block_hash,)
        ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------ block index

    def put_index(self, entry: IndexEntry) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO block_index"
            "(hash, prev_hash, height, cumwork, timestamp, bits, active, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.hash,
                entry.prev_hash,
                entry.height,
                format(entry.cumwork, "x"),
                entry.timestamp,
                entry.bits,
                int(entry.active),
                entry.status,
            ),
        )

    def get_index(self, block_hash: bytes) -> IndexEntry | None:
        row = self.conn.execute(
            "SELECT hash, prev_hash, height, cumwork, timestamp, bits, active,"
            " status FROM block_index WHERE hash = ?",
            (block_hash,),
        ).fetchone()
        if row is None:
            return None
        return IndexEntry(
            hash=row[0],
            prev_hash=row[1],
            height=row[2],
            cumwork=int(row[3], 16),
            timestamp=row[4],
            bits=row[5],
            active=bool(row[6]),
            status=row[7],
        )

    def set_active(self, block_hash: bytes, active: bool) -> None:
        self.conn.execute(
            "UPDATE block_index SET active = ? WHERE hash = ?",
            (int(active), block_hash),
        )

    def set_status(self, block_hash: bytes, status: str) -> None:
        self.conn.execute(
            "UPDATE block_index SET status = ? WHERE hash = ?", (status, block_hash)
        )

    def active_hash_at_height(self, height: int) -> bytes | None:
        row = self.conn.execute(
            "SELECT hash FROM block_index WHERE active = 1 AND height = ?",
            (height,),
        ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ UTXOs

    def add_utxo(
        self,
        txid: bytes,
        index: int,
        amount: int,
        pubkey_hash: bytes,
        height: int,
        is_coinbase: bool,
    ) -> None:
        """Insert a UTXO. Raises sqlite3.IntegrityError on a duplicate outpoint,
        which the caller treats as the BIP-30 overwrite case."""
        self.conn.execute(
            "INSERT INTO utxos(txid, idx, amount, pubkey_hash, height, coinbase)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (txid, index, amount, pubkey_hash, height, int(is_coinbase)),
        )

    def get_utxo(self, txid: bytes, index: int) -> tuple | None:
        """Returns (amount, pubkey_hash, height, coinbase) or None."""
        return self.conn.execute(
            "SELECT amount, pubkey_hash, height, coinbase FROM utxos"
            " WHERE txid = ? AND idx = ?",
            (txid, index),
        ).fetchone()

    def delete_utxo(self, txid: bytes, index: int) -> None:
        self.conn.execute(
            "DELETE FROM utxos WHERE txid = ? AND idx = ?", (txid, index)
        )

    def utxos_for_pubkey_hash(self, pubkey_hash: bytes) -> list[tuple]:
        """Returns rows of (txid, idx, amount, height, coinbase)."""
        return self.conn.execute(
            "SELECT txid, idx, amount, height, coinbase FROM utxos"
            " WHERE pubkey_hash = ? ORDER BY height, txid, idx",
            (pubkey_hash,),
        ).fetchall()

    def sum_utxos(self) -> int:
        return self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM utxos"
        ).fetchone()[0]

    def all_utxo_outpoints(self) -> list[tuple[bytes, int]]:
        return self.conn.execute(
            "SELECT txid, idx FROM utxos ORDER BY txid, idx"
        ).fetchall()

    # ------------------------------------------------------------------- undo

    def put_undo(self, block_hash: bytes, raw: bytes) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO undo(hash, raw) VALUES (?, ?)",
            (block_hash, raw),
        )

    def get_undo(self, block_hash: bytes) -> bytes | None:
        row = self.conn.execute(
            "SELECT raw FROM undo WHERE hash = ?", (block_hash,)
        ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------- meta

    def get_meta(self, key: str) -> bytes | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: bytes) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )
