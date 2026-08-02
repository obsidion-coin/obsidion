"""Transactions: how value moves in Obsidion.

Obsidion uses the UTXO model. There are no account balances anywhere in the
system. A transaction consumes whole *unspent transaction outputs* created by
earlier transactions and creates new ones, and a wallet's "balance" is simply the
sum of the outputs it can unlock. Double-spending is then structurally visible: a
UTXO either exists and can be spent once, or it does not exist.

Two design decisions differ from Bitcoin's original design, both fixing problems
Bitcoin later had to patch:

**Transaction ids exclude signatures.** Bitcoin's legacy txid covers the whole
serialized transaction including signature bytes, so altering a signature's
encoding changes the id without invalidating it — transaction malleability, which
broke exchanges and forced the segwit redesign. Obsidion computes the id over the
transaction with signature fields blank, so the id is fixed the moment the
transaction is constructed.

**Signatures commit to the amount being spent.** Following BIP-143, the digest
includes the value of the output being consumed. Without this an attacker can lie
to an offline signer about how much it is spending and steal the difference as
fee.

Only one kind of output exists: pay-to-public-key-hash. There is no scripting
language. That is a deliberate limitation — Bitcoin's script engine is the source
of a large share of its consensus complexity, and Obsidion does not need it to
be money.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace

from obsidion import crypto

#: The prevout txid used by coinbase transactions, which spend nothing.
NULL_TXID = b"\x00" * 32

#: The prevout index used by coinbase transactions.
COINBASE_INDEX = 0xFFFFFFFF

#: Default input sequence. Obsidion does not implement relative timelocks, so
#: this is always final.
SEQUENCE_FINAL = 0xFFFFFFFF

#: An absolute ceiling on any single output, used as an overflow guard. No
#: legitimate output can exceed the total supply that will ever exist.
MAX_MONEY = 21_000_000 * 100_000_000

#: Maximum bytes of miner-chosen data in a coinbase input.
MAX_COINBASE_DATA = 100


# --------------------------------------------------------------------------
# Variable-length integers
# --------------------------------------------------------------------------


def write_varint(value: int) -> bytes:
    """Encode a non-negative integer compactly, Bitcoin-style."""
    if value < 0:
        raise ValueError(f"varint cannot encode a negative value: {value}")
    if value < 0xFD:
        return bytes([value])
    if value <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", value)
    if value <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", value)
    if value <= 0xFFFFFFFFFFFFFFFF:
        return b"\xff" + struct.pack("<Q", value)
    raise ValueError(f"varint too large: {value}")


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a varint, returning `(value, new_offset)`."""
    if offset >= len(data):
        raise ValueError("truncated varint")

    prefix = data[offset]
    offset += 1
    if prefix < 0xFD:
        return prefix, offset

    widths = {0xFD: (2, "<H"), 0xFE: (4, "<I"), 0xFF: (8, "<Q")}
    width, fmt = widths[prefix]
    if offset + width > len(data):
        raise ValueError("truncated varint payload")
    return struct.unpack(fmt, data[offset : offset + width])[0], offset + width


def _read_exactly(data: bytes, offset: int, count: int) -> tuple[bytes, int]:
    if offset + count > len(data):
        raise ValueError(
            f"truncated: wanted {count} bytes at offset {offset}, "
            f"only {len(data) - offset} remain"
        )
    return data[offset : offset + count], offset + count


def _read_varbytes(data: bytes, offset: int) -> tuple[bytes, int]:
    length, offset = read_varint(data, offset)
    return _read_exactly(data, offset, length)


def _write_varbytes(payload: bytes) -> bytes:
    return write_varint(len(payload)) + payload


# --------------------------------------------------------------------------
# Structures
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OutPoint:
    """A reference to one specific output of one specific transaction."""

    txid: bytes
    index: int

    def serialize(self) -> bytes:
        return self.txid + struct.pack("<I", self.index)

    @staticmethod
    def deserialize(data: bytes, offset: int) -> tuple[OutPoint, int]:
        txid, offset = _read_exactly(data, offset, 32)
        raw, offset = _read_exactly(data, offset, 4)
        return OutPoint(txid, struct.unpack("<I", raw)[0]), offset

    def __repr__(self) -> str:
        return f"OutPoint({self.txid[::-1].hex()[:16]}…:{self.index})"


@dataclass
class TxIn:
    """An input: proof that the spender may consume a particular UTXO.

    `signature` and `pubkey` are empty until signed. For a coinbase input they
    carry arbitrary miner data instead, since there is nothing to prove.
    """

    prevout: OutPoint
    signature: bytes = b""
    pubkey: bytes = b""
    sequence: int = SEQUENCE_FINAL

    def serialize(self) -> bytes:
        return (
            self.prevout.serialize()
            + _write_varbytes(self.signature)
            + _write_varbytes(self.pubkey)
            + struct.pack("<I", self.sequence)
        )

    @staticmethod
    def deserialize(data: bytes, offset: int) -> tuple[TxIn, int]:
        prevout, offset = OutPoint.deserialize(data, offset)
        signature, offset = _read_varbytes(data, offset)
        pubkey, offset = _read_varbytes(data, offset)
        raw, offset = _read_exactly(data, offset, 4)
        return TxIn(prevout, signature, pubkey, struct.unpack("<I", raw)[0]), offset


@dataclass
class TxOut:
    """An output: an amount of value locked to whoever can produce the key."""

    amount: int
    """Value in shards. Always an integer — money and floats never mix."""

    pubkey_hash: bytes
    """The 20-byte hash160 of the public key permitted to spend this."""

    def serialize(self) -> bytes:
        if len(self.pubkey_hash) != 20:
            raise ValueError(
                f"pubkey_hash must be 20 bytes, got {len(self.pubkey_hash)}"
            )
        return struct.pack("<q", self.amount) + self.pubkey_hash

    @staticmethod
    def deserialize(data: bytes, offset: int) -> tuple[TxOut, int]:
        raw, offset = _read_exactly(data, offset, 8)
        pubkey_hash, offset = _read_exactly(data, offset, 20)
        return TxOut(struct.unpack("<q", raw)[0], pubkey_hash), offset


@dataclass
class Transaction:
    """A transfer of value: inputs consumed, outputs created."""

    inputs: list[TxIn] = field(default_factory=list)
    outputs: list[TxOut] = field(default_factory=list)
    version: int = 1
    locktime: int = 0

    # ----------------------------------------------------------------- shape

    def is_coinbase(self) -> bool:
        """True if this is a block's reward transaction, which creates new coins."""
        return (
            len(self.inputs) == 1
            and self.inputs[0].prevout.txid == NULL_TXID
            and self.inputs[0].prevout.index == COINBASE_INDEX
        )

    def total_output_value(self) -> int:
        return sum(output.amount for output in self.outputs)

    def check_structure(self) -> None:
        """Validate what can be checked without consulting the chain.

        These are context-free rules: a transaction failing any of them is
        invalid no matter which chain, block, or height it appears in. Raises
        ValueError describing the first problem found.
        """
        if not self.inputs:
            raise ValueError("transaction has no inputs")
        if not self.outputs:
            raise ValueError("transaction has no outputs")

        for output in self.outputs:
            if output.amount < 0:
                raise ValueError(f"output has a negative amount: {output.amount}")
            if output.amount > MAX_MONEY:
                raise ValueError(
                    f"output amount {output.amount} exceeds the total money supply"
                )
            if len(output.pubkey_hash) != 20:
                raise ValueError("output pubkey_hash is not 20 bytes")

        total = self.total_output_value()
        if total > MAX_MONEY:
            raise ValueError(f"total output value {total} exceeds the money supply")

        if self.is_coinbase():
            data_length = len(self.inputs[0].signature)
            if data_length > MAX_COINBASE_DATA:
                raise ValueError(
                    f"coinbase data is {data_length} bytes, "
                    f"limit is {MAX_COINBASE_DATA}"
                )
            return

        seen: set[OutPoint] = set()
        for tx_input in self.inputs:
            if tx_input.prevout in seen:
                raise ValueError(f"duplicate input spending {tx_input.prevout}")
            seen.add(tx_input.prevout)
            if tx_input.prevout.txid == NULL_TXID:
                raise ValueError("only a coinbase may spend the null outpoint")

    # --------------------------------------------------------- serialization

    def serialize(self, *, strip_signatures: bool = False) -> bytes:
        """Serialize to bytes.

        With `strip_signatures`, signature and pubkey fields are blanked. That
        form defines the transaction id — see the module docstring.
        """
        inputs = self.inputs
        if strip_signatures and not self.is_coinbase():
            inputs = [replace(i, signature=b"", pubkey=b"") for i in inputs]

        parts = [struct.pack("<i", self.version), write_varint(len(inputs))]
        parts.extend(i.serialize() for i in inputs)
        parts.append(write_varint(len(self.outputs)))
        parts.extend(o.serialize() for o in self.outputs)
        parts.append(struct.pack("<I", self.locktime))
        return b"".join(parts)

    @staticmethod
    def deserialize(data: bytes) -> Transaction:
        """Parse a transaction, rejecting anything malformed or with extra bytes.

        Trailing bytes are an error rather than an oversight: accepting them
        would let two different byte strings decode to the same transaction,
        which is a route to consensus disagreement.
        """
        transaction, offset = Transaction.deserialize_from(data, 0)
        if offset != len(data):
            raise ValueError(
                f"{len(data) - offset} unexpected trailing bytes after transaction"
            )
        return transaction

    @staticmethod
    def deserialize_from(data: bytes, offset: int) -> tuple[Transaction, int]:
        """Parse one transaction from a larger buffer, e.g. a block."""
        raw, offset = _read_exactly(data, offset, 4)
        version = struct.unpack("<i", raw)[0]

        count, offset = read_varint(data, offset)
        inputs = []
        for _ in range(count):
            tx_input, offset = TxIn.deserialize(data, offset)
            inputs.append(tx_input)

        count, offset = read_varint(data, offset)
        outputs = []
        for _ in range(count):
            output, offset = TxOut.deserialize(data, offset)
            outputs.append(output)

        raw, offset = _read_exactly(data, offset, 4)
        locktime = struct.unpack("<I", raw)[0]

        return Transaction(inputs, outputs, version, locktime), offset

    def txid(self) -> bytes:
        """The transaction's unique identifier, independent of its signatures."""
        return crypto.sha256d(self.serialize(strip_signatures=True))

    def size(self) -> int:
        return len(self.serialize())

    # ------------------------------------------------------------- signing

    def signature_hash(self, index: int, amount: int, pubkey_hash: bytes) -> bytes:
        """The digest signed to authorise spending input `index`.

        Commits to the transaction version, every input being spent, every
        output, the locktime, and — crucially — which input this is and how much
        that input is worth. A signature therefore authorises exactly one
        spend of exactly one output in exactly one transaction.
        """
        if not 0 <= index < len(self.inputs):
            raise IndexError(f"no input at index {index}")

        prevouts = b"".join(i.prevout.serialize() for i in self.inputs)
        sequences = b"".join(struct.pack("<I", i.sequence) for i in self.inputs)
        outputs = b"".join(o.serialize() for o in self.outputs)
        tx_input = self.inputs[index]

        preimage = (
            struct.pack("<i", self.version)
            + crypto.sha256d(prevouts)
            + crypto.sha256d(sequences)
            + tx_input.prevout.serialize()
            + pubkey_hash
            + struct.pack("<q", amount)
            + struct.pack("<I", tx_input.sequence)
            + crypto.sha256d(outputs)
            + struct.pack("<I", self.locktime)
            + struct.pack("<I", index)
        )
        return crypto.sha256d(preimage)

    def sign_input(
        self, index: int, private_key: bytes, public_key: bytes, amount: int
    ) -> None:
        """Sign one input in place, proving ownership of the output it spends."""
        pubkey_hash = crypto.hash160(public_key)
        digest = self.signature_hash(index, amount, pubkey_hash)
        self.inputs[index] = replace(
            self.inputs[index],
            signature=crypto.sign(private_key, digest),
            pubkey=public_key,
        )

    def verify_input(self, index: int, pubkey_hash: bytes, amount: int) -> bool:
        """Check that input `index` may spend an output worth `amount` locked to
        `pubkey_hash`.

        Two conditions, both required: the supplied public key must hash to the
        one the output was locked to, and the signature must verify against it.
        The first without the second would let anyone spend by naming a key; the
        second without the first would let a valid signature from the wrong key
        pass.
        """
        if not 0 <= index < len(self.inputs):
            return False

        tx_input = self.inputs[index]
        if not tx_input.signature or not tx_input.pubkey:
            return False
        if crypto.hash160(tx_input.pubkey) != pubkey_hash:
            return False

        digest = self.signature_hash(index, amount, pubkey_hash)
        return crypto.verify(tx_input.pubkey, tx_input.signature, digest)

    # ------------------------------------------------------------- coinbase

    @staticmethod
    def coinbase(
        height: int,
        reward: int,
        pubkey_hash: bytes,
        extra_nonce: int = 0,
        message: bytes = b"",
    ) -> Transaction:
        """Build the transaction that pays a miner for finding a block.

        The height is encoded into the input data, following BIP-34. This makes
        every coinbase unique: without it, a miner paying the same address the
        same reward twice would produce two transactions with the same id, and
        the second would overwrite the first in the UTXO set — destroying the
        earlier reward.
        """
        data = struct.pack("<i", height) + struct.pack("<Q", extra_nonce) + message
        if len(data) > MAX_COINBASE_DATA:
            raise ValueError(
                f"coinbase data is {len(data)} bytes, limit is {MAX_COINBASE_DATA}"
            )

        return Transaction(
            inputs=[TxIn(prevout=OutPoint(NULL_TXID, COINBASE_INDEX), signature=data)],
            outputs=[TxOut(amount=reward, pubkey_hash=pubkey_hash)],
        )

    def encoded_height(self) -> int:
        """Read back the height a coinbase claims to have been mined at."""
        if not self.is_coinbase():
            raise ValueError("only a coinbase transaction encodes a height")
        data = self.inputs[0].signature
        if len(data) < 4:
            raise ValueError("coinbase data too short to contain a height")
        return struct.unpack("<i", data[:4])[0]

    def __repr__(self) -> str:
        kind = "coinbase" if self.is_coinbase() else "tx"
        return (
            f"<{kind} {self.txid()[::-1].hex()[:16]}… "
            f"{len(self.inputs)}in {len(self.outputs)}out>"
        )
