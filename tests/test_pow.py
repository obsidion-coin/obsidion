"""Tests for the memory-hard proof-of-work and the stored genesis nonces.

The rest of the suite runs on regtest, which deliberately keeps cheap SHA-256d
so that mining thousands of blocks stays a matter of seconds. These tests are
the ones that exercise the algorithm the real chain actually uses.
"""

from __future__ import annotations

import time

import pytest

from obsidion import crypto
from obsidion.block import BlockHeader
from obsidion.genesis import create_genesis_block, genesis_coinbase
from obsidion.merkle import merkle_root
from obsidion.params import MAINNET, REGTEST, TESTNET

ALL_NETWORKS = [MAINNET, TESTNET, REGTEST]
IDS = [p.name for p in ALL_NETWORKS]

HEADER = bytes(range(80))


# --------------------------------------------------------------------------
# The hash itself
# --------------------------------------------------------------------------


def test_scrypt_pow_returns_32_bytes():
    assert len(crypto.scrypt_pow(HEADER)) == 32


def test_scrypt_pow_is_deterministic():
    """Every node must derive the same digest or the network cannot agree."""
    assert crypto.scrypt_pow(HEADER) == crypto.scrypt_pow(HEADER)


def test_scrypt_pow_differs_from_sha256d():
    assert crypto.scrypt_pow(HEADER) != crypto.sha256d(HEADER)


def test_one_bit_of_header_change_changes_the_digest():
    other = bytes([HEADER[0] ^ 0x01]) + HEADER[1:]
    assert crypto.scrypt_pow(other) != crypto.scrypt_pow(HEADER)


def test_pow_hash_dispatches_by_algorithm_name():
    assert crypto.pow_hash(HEADER, "scrypt-2mb") == crypto.scrypt_pow(HEADER)
    assert crypto.pow_hash(HEADER, "sha256d") == crypto.sha256d(HEADER)


def test_an_unknown_algorithm_is_a_clear_error():
    with pytest.raises(ValueError, match="unknown proof-of-work algorithm"):
        crypto.pow_hash(HEADER, "definitely-not-real")


def test_scrypt_is_costly_enough_to_matter():
    """The whole defence rests on each hash being expensive. If this ever comes
    back fast, the memory parameters have silently been weakened."""
    start = time.perf_counter()
    for _ in range(5):
        crypto.scrypt_pow(HEADER)
    per_hash_ms = (time.perf_counter() - start) / 5 * 1000

    assert per_hash_ms > 1.0, f"only {per_hash_ms:.2f} ms per hash — too cheap"


# --------------------------------------------------------------------------
# Identity is separate from proof of work
# --------------------------------------------------------------------------


def test_block_identity_stays_cheap_sha256d():
    """A block's hash is used for every parent link, lookup and announcement,
    so it must not carry the mining algorithm's cost."""
    header = BlockHeader(b"\x00" * 32, b"\x11" * 32, 1_785_628_800, MAINNET.pow_limit_bits)

    assert header.hash() == crypto.sha256d(header.serialize())
    assert header.hash() != header.pow_digest("scrypt-2mb")


def test_the_same_header_is_judged_differently_by_each_algorithm():
    """A header meeting an easy target under SHA-256d generally does not meet
    it under scrypt — which is exactly why the algorithm is consensus."""
    header = BlockHeader(b"\x00" * 32, b"\x11" * 32, 1_785_628_800, 0x1D00FFFF)
    header.nonce = 12345

    assert header.satisfies_pow("sha256d") is False
    assert header.satisfies_pow("scrypt-2mb") is False


# --------------------------------------------------------------------------
# Genesis
# --------------------------------------------------------------------------


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_stored_genesis_nonce_is_the_one_mining_actually_finds(params):
    """The stored nonce is a cached answer, not an article of faith.

    Re-derives it the slow way — from nonce zero under the network's real
    algorithm — and requires the stored value to match. A wrong entry in
    params.py fails here rather than shipping to users.
    """
    coinbase = genesis_coinbase(params)
    header = BlockHeader(
        prev_hash=b"\x00" * 32,
        merkle_root=merkle_root([coinbase.txid()]),
        timestamp=params.genesis_timestamp,
        bits=params.pow_limit_bits,
        nonce=0,
    )
    while not header.satisfies_pow(params.pow_algorithm):
        header.nonce += 1

    assert header.nonce == params.genesis_nonce
    assert create_genesis_block(params).header.nonce == params.genesis_nonce


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_verification_is_fast_enough_for_start_up(params):
    """Nodes derive genesis on every launch. Searching for it under a
    memory-hard algorithm took nearly nine seconds; verifying takes one hash."""
    create_genesis_block.cache_clear()

    start = time.perf_counter()
    create_genesis_block(params)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 500, f"genesis took {elapsed_ms:.0f} ms to derive"


def test_a_wrong_stored_nonce_is_refused_rather_than_trusted():
    from dataclasses import replace

    broken = replace(MAINNET, genesis_nonce=MAINNET.genesis_nonce + 1)
    with pytest.raises(RuntimeError, match="does not satisfy"):
        create_genesis_block(broken)


# --------------------------------------------------------------------------
# The parameters themselves
# --------------------------------------------------------------------------


@pytest.mark.parametrize("params", [MAINNET, TESTNET], ids=["mainnet", "testnet"])
def test_real_networks_use_the_memory_hard_algorithm(params):
    assert params.pow_algorithm == "scrypt-2mb"


def test_regtest_stays_cheap_so_the_suite_stays_fast():
    assert REGTEST.pow_algorithm == "sha256d"


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_future_time_allowance_cannot_distort_a_retarget(params):
    """Regression, found by audit.

    The retarget measures a period's duration from raw timestamps. If a miner
    may post-date a block by as much as the whole retarget window, one block
    can halve the next difficulty. The allowance has to stay a small fraction
    of the window.
    """
    window = params.retarget_interval * params.target_block_time
    assert params.max_future_block_time * 4 <= window, (
        f"{params.name}: a block may be post-dated "
        f"{params.max_future_block_time}s against a {window}s retarget window"
    )
