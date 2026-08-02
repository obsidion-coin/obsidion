"""Tests for the merkle tree that commits a block to its transactions."""

from __future__ import annotations

import pytest

from obsidion import crypto
from obsidion.merkle import (
    CVE_2012_2459_MESSAGE,
    merkle_proof,
    merkle_root,
    verify_proof,
)


def h(label: bytes) -> bytes:
    return crypto.sha256d(label)


def test_single_transaction_is_its_own_root():
    txid = h(b"coinbase")
    assert merkle_root([txid]) == txid


def test_two_transactions_hash_together():
    a, b = h(b"a"), h(b"b")
    assert merkle_root([a, b]) == crypto.sha256d(a + b)


def test_odd_row_duplicates_the_last_hash():
    a, b, c = h(b"a"), h(b"b"), h(b"c")
    expected = crypto.sha256d(crypto.sha256d(a + b) + crypto.sha256d(c + c))
    assert merkle_root([a, b, c]) == expected


def test_four_transactions_build_a_balanced_tree():
    a, b, c, d = h(b"a"), h(b"b"), h(b"c"), h(b"d")
    expected = crypto.sha256d(crypto.sha256d(a + b) + crypto.sha256d(c + d))
    assert merkle_root([a, b, c, d]) == expected


def test_order_matters():
    """Reordering transactions must change the root, or a miner could shuffle a
    block's contents while keeping its header valid."""
    a, b = h(b"a"), h(b"b")
    assert merkle_root([a, b]) != merkle_root([b, a])


def test_changing_any_transaction_changes_the_root():
    original = [h(b"a"), h(b"b"), h(b"c"), h(b"d")]
    tampered = [h(b"a"), h(b"b"), h(b"c"), h(b"TAMPERED")]
    assert merkle_root(original) != merkle_root(tampered)


def test_empty_block_is_rejected():
    """Every block carries at least a coinbase transaction, so an empty list is a
    programming error rather than a valid input."""
    with pytest.raises(ValueError):
        merkle_root([])


def test_duplicate_trailing_pair_is_rejected():
    """Guards CVE-2012-2459.

    Because odd rows duplicate their last hash, a list ending in an already
    duplicated pair produces the same root as the shorter honest list. Left
    unchecked, an attacker can take a valid block, append a copy of its trailing
    transactions, and produce a different block with an identical merkle root —
    which nodes then cache as invalid, splitting the network.
    """
    a, b = h(b"a"), h(b"b")
    with pytest.raises(ValueError, match=CVE_2012_2459_MESSAGE):
        merkle_root([a, b, b])


def test_honest_repeated_hash_elsewhere_is_allowed():
    """Only a duplicated *trailing pair* is dangerous. Identical hashes in other
    positions are merely improbable, not an attack, and must still validate."""
    a, b = h(b"a"), h(b"b")
    assert merkle_root([a, a, b]) is not None


# --------------------------------------------------------------------------
# Inclusion proofs — how a lightweight wallet checks a payment without the block
# --------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5, 8, 13])
@pytest.mark.parametrize("index", [0, -1])
def test_proof_verifies_for_first_and_last_transaction(count, index):
    txids = [h(f"tx{i}".encode()) for i in range(count)]
    position = index % count

    path = merkle_proof(txids, position)
    assert verify_proof(txids[position], path, merkle_root(txids))


def test_proof_verifies_for_every_position():
    txids = [h(f"tx{i}".encode()) for i in range(7)]
    root = merkle_root(txids)

    for position, txid in enumerate(txids):
        assert verify_proof(txid, merkle_proof(txids, position), root)


def test_proof_fails_for_a_transaction_not_in_the_block():
    txids = [h(f"tx{i}".encode()) for i in range(4)]
    path = merkle_proof(txids, 2)

    assert not verify_proof(h(b"never mined"), path, merkle_root(txids))


def test_proof_fails_against_a_different_block():
    txids = [h(f"tx{i}".encode()) for i in range(4)]
    other = [h(f"other{i}".encode()) for i in range(4)]

    path = merkle_proof(txids, 1)
    assert not verify_proof(txids[1], path, merkle_root(other))


def test_proof_rejects_an_out_of_range_index():
    with pytest.raises(IndexError):
        merkle_proof([h(b"a"), h(b"b")], 5)
