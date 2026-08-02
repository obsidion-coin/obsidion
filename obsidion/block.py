"""Blocks and block headers.

A block header is exactly 80 bytes and contains everything needed to verify the
proof of work:

    version      4 bytes
    prev_hash   32 bytes   links this block to its parent, forming the chain
    merkle_root 32 bytes   commits to every transaction in the block
    timestamp    4 bytes
    bits         4 bytes   the difficulty target, compactly encoded
    nonce        4 bytes   the number miners vary to search for a valid hash

Mining means hashing this 80-byte structure over and over with different nonces
until the result, read as a number, falls below the target. Because the header
commits to the merkle root, and the merkle root commits to the transactions, the
work protects the entire block. Changing one shard of one payment invalidates
every hash computed for that block and every block built on top of it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from obsidion import crypto
from obsidion.merkle import merkle_root
from obsidion.transaction import Transaction

HEADER_SIZE = 80

#: Largest possible target, used to normalise difficulty into a human number.
MAX_TARGET = 0xFFFF * 256 ** (0x1D - 3)


# --------------------------------------------------------------------------
# Compact target encoding ("bits")
# --------------------------------------------------------------------------


def compact_to_target(bits: int) -> int:
    """Expand the 4-byte compact form into a full 256-bit target.

    The encoding is a tiny floating-point format: the top byte is an exponent
    (a byte count) and the lower three bytes are the mantissa. It exists so the
    target fits in the fixed-size header.
    """
    exponent = bits >> 24
    mantissa = bits & 0x007FFFFF

    if bits & 0x00800000:
        raise ValueError(f"negative target in bits 0x{bits:08x}")
    if mantissa == 0:
        return 0
    if exponent > 34:
        raise ValueError(f"target overflows 256 bits: 0x{bits:08x}")

    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def target_to_compact(target: int) -> int:
    """Compress a target back into its 4-byte compact form."""
    if target < 0:
        raise ValueError("target cannot be negative")
    if target == 0:
        return 0

    size = (target.bit_length() + 7) // 8
    if size <= 3:
        mantissa = target << (8 * (3 - size))
    else:
        mantissa = target >> (8 * (size - 3))

    # The sign bit must stay clear, so shift down one byte if it would be set.
    if mantissa & 0x00800000:
        mantissa >>= 8
        size += 1

    return (size << 24) | mantissa


def target_to_difficulty(target: int) -> float:
    """Express a target as a difficulty multiple, for humans.

    Difficulty 1 is the easiest target Bitcoin ever used. Higher means harder.
    This value is cosmetic — consensus always works with the target itself.
    """
    if target <= 0:
        return float("inf")
    return MAX_TARGET / target


def expected_hashes(target: int) -> float:
    """Average number of hashes needed to find a block at this target."""
    if target <= 0:
        return float("inf")
    return 2**256 / (target + 1)


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------


@dataclass
class BlockHeader:
    """The 80 bytes that proof-of-work protects."""

    prev_hash: bytes
    merkle_root: bytes
    timestamp: int
    bits: int
    nonce: int = 0
    version: int = 1

    def serialize(self) -> bytes:
        if len(self.prev_hash) != 32:
            raise ValueError(f"prev_hash must be 32 bytes, got {len(self.prev_hash)}")
        if len(self.merkle_root) != 32:
            raise ValueError(
                f"merkle_root must be 32 bytes, got {len(self.merkle_root)}"
            )
        return struct.pack(
            "<i32s32sIII",
            self.version,
            self.prev_hash,
            self.merkle_root,
            self.timestamp,
            self.bits,
            self.nonce,
        )

    @staticmethod
    def deserialize(data: bytes) -> BlockHeader:
        if len(data) != HEADER_SIZE:
            raise ValueError(
                f"a block header is {HEADER_SIZE} bytes, got {len(data)}"
            )
        version, prev_hash, root, timestamp, bits, nonce = struct.unpack(
            "<i32s32sIII", data
        )
        return BlockHeader(prev_hash, root, timestamp, bits, nonce, version)

    def hash(self) -> bytes:
        """The block's identity — always SHA-256d, whatever the mining algorithm.

        Identity and proof-of-work are two different jobs and Obsidion keeps
        them apart. This hash is computed constantly: every parent link, every
        database lookup, every inventory message, every peer announcement. It
        has to be cheap. The mining hash is computed once per block when the
        work is checked, and is deliberately expensive. Litecoin draws the same
        line for the same reason.
        """
        return crypto.sha256d(self.serialize())

    def pow_digest(self, algorithm: str) -> bytes:
        """The mining hash — the one compared against the difficulty target."""
        return crypto.pow_hash(self.serialize(), algorithm)

    def hash_hex(self) -> str:
        """The hash as conventionally displayed — reversed, so it reads with
        leading zeros first."""
        return self.hash()[::-1].hex()

    def target(self) -> int:
        return compact_to_target(self.bits)

    def difficulty(self) -> float:
        return target_to_difficulty(self.target())

    def satisfies_pow(self, algorithm: str) -> bool:
        """True if this header's mining hash is below its own stated target.

        Note this only proves the header is internally consistent. Whether the
        target itself is the correct one for this height is a separate question,
        answered by `consensus.expected_bits`. A miner is free to claim an easy
        target; the network simply will not accept the block.
        """
        try:
            target = self.target()
        except ValueError:
            return False
        return target > 0 and crypto.hash_to_int(self.pow_digest(algorithm)) <= target

    def work(self) -> int:
        """How much expected effort this block represents.

        Chain selection sums this across every block. Using work rather than
        block count is what stops an attacker from winning with a long chain of
        trivially easy blocks.
        """
        try:
            target = self.target()
        except ValueError:
            return 0
        if target <= 0:
            return 0
        return (1 << 256) // (target + 1)

    def __repr__(self) -> str:
        return f"<header {self.hash_hex()[:16]}… nonce={self.nonce}>"


# --------------------------------------------------------------------------
# Block
# --------------------------------------------------------------------------


@dataclass
class Block:
    """A header plus the transactions it commits to."""

    header: BlockHeader
    transactions: list[Transaction] = field(default_factory=list)

    def hash(self) -> bytes:
        return self.header.hash()

    def hash_hex(self) -> str:
        return self.header.hash_hex()

    def txids(self) -> list[bytes]:
        return [transaction.txid() for transaction in self.transactions]

    def computed_merkle_root(self) -> bytes:
        return merkle_root(self.txids())

    def coinbase(self) -> Transaction:
        if not self.transactions:
            raise ValueError("block has no transactions")
        return self.transactions[0]

    def height(self) -> int:
        """The height this block claims, read from its coinbase (BIP-34)."""
        return self.coinbase().encoded_height()

    def serialize(self) -> bytes:
        from obsidion.transaction import write_varint

        parts = [self.header.serialize(), write_varint(len(self.transactions))]
        parts.extend(t.serialize() for t in self.transactions)
        return b"".join(parts)

    @staticmethod
    def deserialize(data: bytes) -> Block:
        from obsidion.transaction import read_varint

        if len(data) < HEADER_SIZE:
            raise ValueError("data is shorter than a block header")

        header = BlockHeader.deserialize(data[:HEADER_SIZE])
        count, offset = read_varint(data, HEADER_SIZE)

        transactions = []
        for _ in range(count):
            transaction, offset = Transaction.deserialize_from(data, offset)
            transactions.append(transaction)

        if offset != len(data):
            raise ValueError(
                f"{len(data) - offset} unexpected trailing bytes after block"
            )
        return Block(header, transactions)

    def size(self) -> int:
        return len(self.serialize())

    def total_fees(self, input_values: int) -> int:
        """Fees collected, given the total value of all inputs spent in the block."""
        spent = sum(
            t.total_output_value() for t in self.transactions if not t.is_coinbase()
        )
        return input_values - spent

    def __repr__(self) -> str:
        return f"<block {self.hash_hex()[:16]}… {len(self.transactions)}tx>"
