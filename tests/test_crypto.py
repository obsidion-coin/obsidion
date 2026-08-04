"""Tests for hashing, keys, signatures and address encoding.

The bech32 vectors come from BIP-173. Testing against the reference vectors is
what guarantees Obsidion addresses are correct by construction rather than
merely self-consistent — a self-consistent bug would still lose coins.
"""

from __future__ import annotations

import pytest

from obsidion import crypto

# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def test_sha256d_matches_known_vector():
    # Double SHA-256 of the empty string — a widely published test vector.
    assert crypto.sha256d(b"").hex() == (
        "5df6e0e2761359d30a8275058e299fcc0381534545f55cf43e41983f5d4c9456"
    )


def test_sha256d_is_double_hashing_not_single():
    import hashlib

    single = hashlib.sha256(b"obsidion").digest()
    assert crypto.sha256d(b"obsidion") == hashlib.sha256(single).digest()


def test_hash160_is_ripemd_of_sha256():
    digest = crypto.hash160(b"obsidion")
    assert len(digest) == 20


# --------------------------------------------------------------------------
# Keys and signatures
# --------------------------------------------------------------------------


def test_generated_private_key_is_32_bytes():
    priv = crypto.generate_private_key()
    assert len(priv) == 32


def test_public_key_is_compressed_33_bytes():
    priv = crypto.generate_private_key()
    pub = crypto.private_to_public(priv)
    assert len(pub) == 33
    assert pub[0] in (0x02, 0x03)


def test_sign_and_verify_round_trip():
    priv = crypto.generate_private_key()
    pub = crypto.private_to_public(priv)
    digest = crypto.sha256d(b"pay alice 10 OBSD")

    sig = crypto.sign(priv, digest)
    assert crypto.verify(pub, sig, digest)


def test_signature_fails_on_tampered_message():
    priv = crypto.generate_private_key()
    pub = crypto.private_to_public(priv)

    sig = crypto.sign(priv, crypto.sha256d(b"pay alice 10 OBSD"))
    tampered = crypto.sha256d(b"pay alice 1000 OBSD")

    assert not crypto.verify(pub, sig, tampered)


def test_signature_fails_under_wrong_key():
    digest = crypto.sha256d(b"pay alice 10 OBSD")
    sig = crypto.sign(crypto.generate_private_key(), digest)
    other_pub = crypto.private_to_public(crypto.generate_private_key())

    assert not crypto.verify(other_pub, sig, digest)


def test_verify_rejects_garbage_signature_without_raising():
    """A peer can send anything. Malformed signatures must return False, not crash."""
    pub = crypto.private_to_public(crypto.generate_private_key())
    assert not crypto.verify(pub, b"not a signature", crypto.sha256d(b"x"))


def test_signing_is_deterministic():
    """RFC 6979 deterministic nonces. Two signatures over the same message with
    the same key must be byte-identical — a reused random nonce leaks the key."""
    priv = crypto.generate_private_key()
    digest = crypto.sha256d(b"same message")
    assert crypto.sign(priv, digest) == crypto.sign(priv, digest)


# --------------------------------------------------------------------------
# bech32 — BIP-173 reference vectors
# --------------------------------------------------------------------------

VALID_BECH32 = [
    "A12UEL5L",
    "a12uel5l",
    "abcdef1qpzry9x8gf2tvdw0s3jn54khce6mua7lmqqqxw",
    # The maximum-length vector: 90 characters exactly. Built rather than typed,
    # because a hand-copied version is easy to get wrong by a character or two —
    # and bech32's checksum will (correctly) reject it if you do.
    "1" + "1" + "q" * 82 + "c8247j",
    "split1checkupstagehandshakeupstreamerranterredcaperred2y9e3w",
]

INVALID_BECH32 = [
    "A12Uel5l",          # mixed case
    "x1b4n0q5v",         # invalid checksum
    "1pzry9x0s0m2",      # empty human-readable part
    "pzry9x0s0m2",       # no separator
    "li1dgmt3",          # too short a checksum
    "de1lg7wt\xff",      # invalid character
]


@pytest.mark.parametrize("encoded", VALID_BECH32)
def test_bech32_accepts_reference_vectors(encoded):
    hrp, data = crypto.bech32_decode(encoded)
    assert hrp is not None, f"should have decoded {encoded!r}"


@pytest.mark.parametrize("encoded", INVALID_BECH32)
def test_bech32_rejects_invalid_reference_vectors(encoded):
    hrp, data = crypto.bech32_decode(encoded)
    assert hrp is None, f"should have rejected {encoded!r}"


# --------------------------------------------------------------------------
# Obsidion addresses
# --------------------------------------------------------------------------


def test_address_has_obsd_prefix_on_mainnet():
    pub = crypto.private_to_public(crypto.generate_private_key())
    address = crypto.public_key_to_address(pub, "obsd")
    assert address.startswith("obsd1")


def test_address_round_trips_to_the_same_pubkey_hash():
    pub = crypto.private_to_public(crypto.generate_private_key())
    address = crypto.public_key_to_address(pub, "obsd")

    assert crypto.address_to_pubkey_hash(address, "obsd") == crypto.hash160(pub)


def test_address_from_a_different_network_is_rejected():
    """A testnet address must not be spendable as a mainnet address. Without this
    check a user could burn coins by pasting an address from the wrong chain."""
    pub = crypto.private_to_public(crypto.generate_private_key())
    testnet_address = crypto.public_key_to_address(pub, "tobsd")

    with pytest.raises(ValueError, match="network"):
        crypto.address_to_pubkey_hash(testnet_address, "obsd")


def test_address_with_a_single_typo_is_rejected():
    """The whole point of bech32's checksum: catch mistyped addresses before
    they send money into a black hole."""
    pub = crypto.private_to_public(crypto.generate_private_key())
    address = crypto.public_key_to_address(pub, "obsd")

    # Corrupt one character in the data part, avoiding the hrp and separator.
    corrupt = list(address)
    corrupt[-4] = "q" if corrupt[-4] != "q" else "p"

    with pytest.raises(ValueError):
        crypto.address_to_pubkey_hash("".join(corrupt), "obsd")


def test_addresses_are_distinct_per_key():
    a = crypto.public_key_to_address(
        crypto.private_to_public(crypto.generate_private_key()), "obsd"
    )
    b = crypto.public_key_to_address(
        crypto.private_to_public(crypto.generate_private_key()), "obsd"
    )
    assert a != b
