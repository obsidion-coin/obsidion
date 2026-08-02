"""Tests for transaction structure, serialization, ids and signing."""

from __future__ import annotations

import struct

import pytest

from obsidion import crypto
from obsidion.params import COIN
from obsidion.transaction import (
    COINBASE_INDEX,
    NULL_TXID,
    OutPoint,
    Transaction,
    TxIn,
    TxOut,
    read_varint,
    write_varint,
)


@pytest.fixture
def keypair():
    private = crypto.generate_private_key()
    return private, crypto.private_to_public(private)


def make_output(amount=10 * COIN, label=b"alice"):
    return TxOut(amount=amount, pubkey_hash=crypto.hash160(label))


def make_input(txid=None, index=0):
    return TxIn(prevout=OutPoint(txid or crypto.sha256d(b"prev"), index))


# --------------------------------------------------------------------------
# Variable-length integers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected_length",
    [(0, 1), (1, 1), (0xFC, 1), (0xFD, 3), (0xFFFF, 3), (0x10000, 5),
     (0xFFFFFFFF, 5), (0x100000000, 9)],
)
def test_varint_round_trips_at_every_size_boundary(value, expected_length):
    encoded = write_varint(value)
    assert len(encoded) == expected_length

    decoded, offset = read_varint(encoded, 0)
    assert decoded == value
    assert offset == expected_length


def test_negative_varint_is_rejected():
    with pytest.raises(ValueError):
        write_varint(-1)


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def test_transaction_round_trips_through_bytes():
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    assert Transaction.deserialize(tx.serialize()) == tx


def test_transaction_with_many_inputs_and_outputs_round_trips():
    tx = Transaction(
        inputs=[make_input(index=i) for i in range(5)],
        outputs=[make_output(amount=(i + 1) * COIN, label=bytes([i])) for i in range(4)],
        locktime=12345,
    )
    assert Transaction.deserialize(tx.serialize()) == tx


def test_signed_transaction_round_trips(keypair):
    private, public = keypair
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    tx.sign_input(0, private, public, amount=20 * COIN)

    restored = Transaction.deserialize(tx.serialize())
    assert restored == tx
    assert restored.inputs[0].signature == tx.inputs[0].signature


def test_deserializing_truncated_bytes_raises():
    """Peers send malformed data. It must be rejected, not silently accepted."""
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    with pytest.raises(ValueError):
        Transaction.deserialize(tx.serialize()[:-4])


def test_deserializing_with_trailing_bytes_raises():
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    with pytest.raises(ValueError):
        Transaction.deserialize(tx.serialize() + b"extra")


@pytest.mark.parametrize("keep", [0, 1, 4, 10, 20, 35, 40, 50, 60])
def test_truncation_at_any_point_raises_valueerror(keep):
    """A peer can cut a message off anywhere. Every truncation point must produce
    a clean ValueError — never struct.error, IndexError, or a partial object."""
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    with pytest.raises(ValueError):
        Transaction.deserialize(tx.serialize()[:keep])


def test_absurd_input_count_does_not_allocate():
    """A varint claiming four billion inputs must fail on the missing bytes
    rather than trying to build the list."""
    malicious = struct.pack("<i", 1) + b"\xfe\xff\xff\xff\xff"
    with pytest.raises(ValueError):
        Transaction.deserialize(malicious)


# --------------------------------------------------------------------------
# Transaction ids
# --------------------------------------------------------------------------


def test_txid_is_stable_across_serialization():
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    assert Transaction.deserialize(tx.serialize()).txid() == tx.txid()


def test_txid_ignores_signatures(keypair):
    """The id must not depend on signature bytes.

    If it did, anyone could alter a signature's encoding in flight, change the
    id, and break any chain of unconfirmed transactions that referenced it. This
    is the transaction malleability problem; committing the id over the unsigned
    form removes it structurally.
    """
    private, public = keypair
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])

    unsigned_txid = tx.txid()
    tx.sign_input(0, private, public, amount=20 * COIN)

    assert tx.txid() == unsigned_txid


def test_txid_changes_when_an_output_changes():
    a = Transaction(inputs=[make_input()], outputs=[make_output(amount=10 * COIN)])
    b = Transaction(inputs=[make_input()], outputs=[make_output(amount=11 * COIN)])
    assert a.txid() != b.txid()


def test_txid_changes_when_a_recipient_changes():
    a = Transaction(inputs=[make_input()], outputs=[make_output(label=b"alice")])
    b = Transaction(inputs=[make_input()], outputs=[make_output(label=b"bob")])
    assert a.txid() != b.txid()


# --------------------------------------------------------------------------
# Signing and verification
# --------------------------------------------------------------------------


def test_signed_input_verifies(keypair):
    private, public = keypair
    pubkey_hash = crypto.hash160(public)

    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    tx.sign_input(0, private, public, amount=20 * COIN)

    assert tx.verify_input(0, pubkey_hash, amount=20 * COIN)


def test_verification_fails_if_an_output_is_altered(keypair):
    """The signature commits to the outputs, so redirecting the money invalidates
    it. Without this, a relaying node could rewrite the recipient."""
    private, public = keypair
    pubkey_hash = crypto.hash160(public)

    tx = Transaction(inputs=[make_input()], outputs=[make_output(amount=10 * COIN)])
    tx.sign_input(0, private, public, amount=20 * COIN)

    tx.outputs[0] = TxOut(amount=10 * COIN, pubkey_hash=crypto.hash160(b"attacker"))
    assert not tx.verify_input(0, pubkey_hash, amount=20 * COIN)


def test_verification_fails_if_the_amount_is_misstated(keypair):
    """The signature commits to the value being spent, following BIP-143. A miner
    that lies about the input amount to inflate the fee is rejected."""
    private, public = keypair
    pubkey_hash = crypto.hash160(public)

    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    tx.sign_input(0, private, public, amount=20 * COIN)

    assert not tx.verify_input(0, pubkey_hash, amount=999 * COIN)


def test_verification_fails_against_someone_elses_pubkey_hash(keypair):
    """Spending requires a public key that hashes to the one in the output being
    spent — this is what "owning" a coin means."""
    private, public = keypair
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    tx.sign_input(0, private, public, amount=20 * COIN)

    assert not tx.verify_input(0, crypto.hash160(b"somebody else"), amount=20 * COIN)


def test_unsigned_input_does_not_verify():
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    assert not tx.verify_input(0, crypto.hash160(b"alice"), amount=20 * COIN)


def test_each_input_is_signed_independently(keypair):
    private, public = keypair
    pubkey_hash = crypto.hash160(public)

    tx = Transaction(
        inputs=[make_input(index=0), make_input(index=1)], outputs=[make_output()]
    )
    tx.sign_input(0, private, public, amount=20 * COIN)
    tx.sign_input(1, private, public, amount=5 * COIN)

    assert tx.inputs[0].signature != tx.inputs[1].signature
    assert tx.verify_input(0, pubkey_hash, amount=20 * COIN)
    assert tx.verify_input(1, pubkey_hash, amount=5 * COIN)


def test_a_signature_cannot_be_moved_to_another_input(keypair):
    """Each signature commits to its own input index, so lifting one signature
    into a different slot must fail."""
    private, public = keypair
    pubkey_hash = crypto.hash160(public)

    tx = Transaction(
        inputs=[make_input(index=0), make_input(index=1)], outputs=[make_output()]
    )
    tx.sign_input(0, private, public, amount=20 * COIN)
    tx.inputs[1] = TxIn(
        prevout=tx.inputs[1].prevout,
        signature=tx.inputs[0].signature,
        pubkey=public,
    )

    assert not tx.verify_input(1, pubkey_hash, amount=20 * COIN)


# --------------------------------------------------------------------------
# Coinbase transactions
# --------------------------------------------------------------------------


def test_coinbase_is_recognised():
    coinbase = Transaction.coinbase(
        height=1, reward=50 * COIN, pubkey_hash=crypto.hash160(b"miner")
    )
    assert coinbase.is_coinbase()
    assert coinbase.inputs[0].prevout.txid == NULL_TXID
    assert coinbase.inputs[0].prevout.index == COINBASE_INDEX


def test_ordinary_transaction_is_not_a_coinbase():
    tx = Transaction(inputs=[make_input()], outputs=[make_output()])
    assert not tx.is_coinbase()


def test_coinbases_at_different_heights_have_different_ids():
    """Two coinbases paying the same miner the same amount must not share an id,
    or the second would collide with the first in the UTXO set and be unspendable
    — the bug BIP-30 was written to fix."""
    miner = crypto.hash160(b"miner")
    a = Transaction.coinbase(height=1, reward=50 * COIN, pubkey_hash=miner)
    b = Transaction.coinbase(height=2, reward=50 * COIN, pubkey_hash=miner)
    assert a.txid() != b.txid()


def test_coinbase_extra_nonce_changes_the_id():
    """Miners exhaust the 32-bit header nonce quickly; rolling an extra nonce in
    the coinbase gives them a fresh search space."""
    miner = crypto.hash160(b"miner")
    a = Transaction.coinbase(height=1, reward=50 * COIN, pubkey_hash=miner, extra_nonce=0)
    b = Transaction.coinbase(height=1, reward=50 * COIN, pubkey_hash=miner, extra_nonce=1)
    assert a.txid() != b.txid()


def test_coinbase_round_trips():
    coinbase = Transaction.coinbase(
        height=7, reward=50 * COIN, pubkey_hash=crypto.hash160(b"miner"), message=b"hello"
    )
    assert Transaction.deserialize(coinbase.serialize()) == coinbase


def test_coinbase_records_its_height():
    """Height in the coinbase makes every coinbase unique and lets a validator
    confirm a block claims the height it was mined at (BIP-34)."""
    coinbase = Transaction.coinbase(
        height=42, reward=50 * COIN, pubkey_hash=crypto.hash160(b"miner")
    )
    assert coinbase.encoded_height() == 42


# --------------------------------------------------------------------------
# Structural validity
# --------------------------------------------------------------------------


def test_transaction_with_no_inputs_is_rejected():
    with pytest.raises(ValueError):
        Transaction(inputs=[], outputs=[make_output()]).check_structure()


def test_transaction_with_no_outputs_is_rejected():
    with pytest.raises(ValueError):
        Transaction(inputs=[make_input()], outputs=[]).check_structure()


def test_negative_output_amount_is_rejected():
    tx = Transaction(inputs=[make_input()], outputs=[TxOut(-1, crypto.hash160(b"a"))])
    with pytest.raises(ValueError, match="negative"):
        tx.check_structure()


def test_duplicate_inputs_are_rejected():
    """Spending the same output twice inside one transaction is a double-spend
    that never reaches the UTXO layer, so it must be caught structurally."""
    shared = make_input()
    tx = Transaction(inputs=[shared, shared], outputs=[make_output()])
    with pytest.raises(ValueError, match="duplicate"):
        tx.check_structure()


def test_output_amount_above_max_supply_is_rejected():
    """Guards against integer overflow tricks: an output larger than every coin
    that will ever exist cannot be legitimate."""
    tx = Transaction(
        inputs=[make_input()],
        outputs=[TxOut(21_000_001 * COIN, crypto.hash160(b"a"))],
    )
    with pytest.raises(ValueError):
        tx.check_structure()


def test_total_output_value_is_summed():
    tx = Transaction(
        inputs=[make_input()],
        outputs=[make_output(amount=3 * COIN), make_output(amount=4 * COIN, label=b"b")],
    )
    assert tx.total_output_value() == 7 * COIN
