"""Tests for the genesis block.

Genesis is the one block accepted without justification, so it is the one block
whose properties must be checked directly rather than derived.
"""

from __future__ import annotations

import pytest

from obsidion.consensus import check_block_structure, subsidy
from obsidion.genesis import UNSPENDABLE, create_genesis_block, genesis_hash
from obsidion.params import MAINNET, REGTEST, TESTNET

ALL_NETWORKS = [MAINNET, TESTNET, REGTEST]
IDS = [p.name for p in ALL_NETWORKS]


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_is_structurally_valid(params):
    check_block_structure(create_genesis_block(params), params)


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_satisfies_its_proof_of_work(params):
    assert create_genesis_block(params).header.satisfies_pow()


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_has_no_parent(params):
    assert create_genesis_block(params).header.prev_hash == b"\x00" * 32


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_is_at_height_zero(params):
    assert create_genesis_block(params).height() == 0


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_pays_the_full_first_reward(params):
    block = create_genesis_block(params)
    assert block.coinbase().total_output_value() == subsidy(0, params)


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_reward_is_unspendable(params):
    """Nobody starts out holding coins. The first spendable OBSD is mined."""
    block = create_genesis_block(params)
    assert block.coinbase().outputs[0].pubkey_hash == UNSPENDABLE


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_carries_its_network_message(params):
    block = create_genesis_block(params)
    assert params.genesis_message in block.coinbase().inputs[0].signature


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_is_deterministic(params):
    """Two nodes must derive an identical genesis or they are not on the same
    network. Cleared cache to prove the result comes from the parameters, not
    from a value memoised by an earlier call."""
    first = create_genesis_block(params).serialize()
    create_genesis_block.cache_clear()
    assert create_genesis_block(params).serialize() == first


def test_each_network_has_a_distinct_genesis():
    """Distinct genesis hashes mean a testnet node syncing against mainnet is
    rejected at the very first block rather than part-way through."""
    hashes = {params.name: genesis_hash(params) for params in ALL_NETWORKS}
    assert len(set(hashes.values())) == len(ALL_NETWORKS)


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_uses_the_network_difficulty_floor(params):
    assert create_genesis_block(params).header.bits == params.pow_limit_bits


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=IDS)
def test_genesis_round_trips_through_serialization(params):
    from obsidion.block import Block

    block = create_genesis_block(params)
    assert Block.deserialize(block.serialize()) == block
