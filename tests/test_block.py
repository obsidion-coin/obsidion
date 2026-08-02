"""Tests for block headers, serialization and proof-of-work checking."""

from __future__ import annotations

import pytest

from obsidion import crypto
from obsidion.block import HEADER_SIZE, Block, BlockHeader, compact_to_target
from obsidion.consensus import ConsensusError, check_block_structure, check_header_pow
from obsidion.merkle import merkle_root
from obsidion.params import COIN, REGTEST
from obsidion.transaction import OutPoint, Transaction, TxIn, TxOut

MINER = crypto.hash160(b"miner")
EASY = REGTEST.pow_limit_bits


def coinbase(height=0, reward=50 * COIN, extra_nonce=0):
    return Transaction.coinbase(height, reward, MINER, extra_nonce=extra_nonce)


def build_block(transactions=None, bits=EASY, timestamp=1_785_628_800, prev=None):
    transactions = transactions or [coinbase()]
    header = BlockHeader(
        prev_hash=prev or b"\x00" * 32,
        merkle_root=merkle_root([t.txid() for t in transactions]),
        timestamp=timestamp,
        bits=bits,
    )
    return Block(header, transactions)


def mine(block, limit=2_000_000):
    """Grind the nonce until the header satisfies its own target."""
    for nonce in range(limit):
        block.header.nonce = nonce
        if block.header.satisfies_pow():
            return block
    raise AssertionError("could not find a valid nonce")


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------


def test_header_serializes_to_exactly_80_bytes():
    assert len(build_block().header.serialize()) == HEADER_SIZE


def test_header_round_trips():
    header = build_block().header
    assert BlockHeader.deserialize(header.serialize()) == header


def test_header_of_wrong_length_is_rejected():
    with pytest.raises(ValueError):
        BlockHeader.deserialize(b"\x00" * 79)


def test_changing_the_nonce_changes_the_hash():
    header = build_block().header
    first = header.hash()
    header.nonce += 1
    assert header.hash() != first


def test_hash_hex_is_displayed_reversed():
    """Block hashes are conventionally shown little-endian-reversed, so the
    leading zeros produced by mining appear at the front."""
    header = build_block().header
    assert header.hash_hex() == header.hash()[::-1].hex()


def test_header_with_a_short_prev_hash_is_rejected():
    header = build_block().header
    header.prev_hash = b"\x00" * 31
    with pytest.raises(ValueError):
        header.serialize()


# --------------------------------------------------------------------------
# Proof of work
# --------------------------------------------------------------------------


def test_mined_header_satisfies_its_target():
    assert mine(build_block()).header.satisfies_pow()


def test_unmined_header_at_hard_difficulty_fails():
    block = build_block(bits=0x1D00FFFF)
    block.header.nonce = 0
    assert not block.header.satisfies_pow()


def test_check_header_pow_raises_for_insufficient_work():
    block = build_block(bits=0x1D00FFFF)
    with pytest.raises(ConsensusError, match="insufficient proof of work"):
        check_header_pow(block.header)


def test_zero_target_is_rejected():
    block = build_block(bits=0x00000000)
    with pytest.raises(ConsensusError):
        check_header_pow(block.header)


def test_harder_target_yields_more_work():
    """Chain selection sums work, so a harder block must count for more."""
    easy = BlockHeader(b"\x00" * 32, b"\x11" * 32, 0, EASY)
    hard = BlockHeader(b"\x00" * 32, b"\x11" * 32, 0, 0x1D00FFFF)
    assert hard.work() > easy.work()


def test_work_is_inversely_proportional_to_target():
    header = BlockHeader(b"\x00" * 32, b"\x11" * 32, 0, 0x1D00FFFF)
    assert header.work() == (1 << 256) // (compact_to_target(0x1D00FFFF) + 1)


# --------------------------------------------------------------------------
# Block serialization
# --------------------------------------------------------------------------


def test_block_round_trips():
    block = mine(build_block())
    assert Block.deserialize(block.serialize()) == block


def test_block_with_several_transactions_round_trips():
    spend = Transaction(
        inputs=[TxIn(OutPoint(crypto.sha256d(b"prev"), 0))],
        outputs=[TxOut(3 * COIN, crypto.hash160(b"alice"))],
    )
    block = mine(build_block([coinbase(), spend]))
    assert Block.deserialize(block.serialize()) == block


def test_block_with_trailing_bytes_is_rejected():
    block = mine(build_block())
    with pytest.raises(ValueError):
        Block.deserialize(block.serialize() + b"junk")


def test_truncated_block_is_rejected():
    block = mine(build_block())
    with pytest.raises(ValueError):
        Block.deserialize(block.serialize()[:60])


def test_block_reports_its_claimed_height():
    block = build_block([coinbase(height=17)])
    assert block.height() == 17


# --------------------------------------------------------------------------
# Structural validation
# --------------------------------------------------------------------------


def test_well_formed_block_passes_structural_checks():
    check_block_structure(mine(build_block()), REGTEST)


def test_block_without_transactions_is_rejected():
    header = BlockHeader(b"\x00" * 32, b"\x11" * 32, 0, EASY)
    with pytest.raises(ConsensusError, match="no transactions"):
        check_block_structure(Block(header, []), REGTEST)


def test_block_whose_first_transaction_is_not_a_coinbase_is_rejected():
    spend = Transaction(
        inputs=[TxIn(OutPoint(crypto.sha256d(b"prev"), 0))],
        outputs=[TxOut(1 * COIN, MINER)],
    )
    with pytest.raises(ConsensusError, match="must be the coinbase"):
        check_block_structure(mine(build_block([spend])), REGTEST)


def test_block_with_two_coinbases_is_rejected():
    """A block may create coins exactly once. Two coinbases would double the
    reward while each looked individually valid."""
    with pytest.raises(ConsensusError, match="extra coinbase"):
        check_block_structure(
            mine(build_block([coinbase(height=1), coinbase(height=1, extra_nonce=9)])),
            REGTEST,
        )


def test_block_whose_merkle_root_does_not_match_is_rejected():
    """The header must genuinely commit to the transactions carried with it, or a
    miner could swap the contents of an already-mined block."""
    block = mine(build_block())
    block.transactions.append(
        Transaction(
            inputs=[TxIn(OutPoint(crypto.sha256d(b"sneaked in"), 0))],
            outputs=[TxOut(1 * COIN, MINER)],
        )
    )
    with pytest.raises(ConsensusError, match="merkle root"):
        check_block_structure(block, REGTEST)


def test_block_containing_the_same_transaction_twice_is_rejected():
    """Including one transaction twice would require its inputs to be spent
    twice. Placed away from the end of the block so the merkle tree builds
    normally and the duplicate-txid rule is what catches it."""
    duplicated = Transaction(
        inputs=[TxIn(OutPoint(crypto.sha256d(b"prev"), 0))],
        outputs=[TxOut(1 * COIN, MINER)],
    )
    other = Transaction(
        inputs=[TxIn(OutPoint(crypto.sha256d(b"other"), 0))],
        outputs=[TxOut(2 * COIN, MINER)],
    )
    block = mine(build_block([coinbase(height=3), duplicated, other, duplicated]))

    with pytest.raises(ConsensusError, match="same transaction twice"):
        check_block_structure(block, REGTEST)


def test_block_ending_in_a_duplicated_pair_is_rejected_as_a_consensus_error():
    """The CVE-2012-2459 malleability case must surface as a ConsensusError, not
    leak a raw ValueError — callers distinguish "bad peer" from "bad code"."""
    duplicated = Transaction(
        inputs=[TxIn(OutPoint(crypto.sha256d(b"prev"), 0))],
        outputs=[TxOut(1 * COIN, MINER)],
    )
    header = BlockHeader(b"\x00" * 32, b"\x11" * 32, 1_785_628_800, EASY)
    block = Block(header, [coinbase(height=3), duplicated, duplicated])

    with pytest.raises(ConsensusError):
        check_block_structure(block, REGTEST)


def test_oversized_block_is_rejected():
    from dataclasses import replace

    tiny_limit = replace(REGTEST, max_block_weight=100)
    with pytest.raises(ConsensusError, match="limit is 100"):
        check_block_structure(mine(build_block()), tiny_limit)
