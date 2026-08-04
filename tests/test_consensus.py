"""Tests for Obsidion's economic and proof-of-work rules.

The supply tests here are the most important in the repository. Everything else
is plumbing; these are the properties that make the coin scarce.
"""

from __future__ import annotations

import pytest

from obsidion import consensus
from obsidion.block import (
    BlockHeader,
    compact_to_target,
    target_to_compact,
    target_to_difficulty,
)
from obsidion.params import COIN, MAINNET, REGTEST, TESTNET

ALL_NETWORKS = [MAINNET, TESTNET, REGTEST]


# --------------------------------------------------------------------------
# Halving
# --------------------------------------------------------------------------


def test_first_block_pays_the_full_reward():
    assert consensus.subsidy(0, MAINNET) == 50 * COIN


def test_reward_holds_steady_until_the_halving_block():
    assert consensus.subsidy(209_999, MAINNET) == 50 * COIN


def test_reward_halves_exactly_at_the_interval():
    assert consensus.subsidy(210_000, MAINNET) == 25 * COIN


def test_reward_halves_again_at_the_second_interval():
    assert consensus.subsidy(420_000, MAINNET) == 1_250_000_000  # 12.5 OBSD


@pytest.mark.parametrize(
    "era, expected_obsd",
    [(0, 50), (1, 25), (2, 12.5), (3, 6.25), (4, 3.125), (10, 0.048828125)],
)
def test_reward_follows_the_published_schedule(era, expected_obsd):
    height = era * MAINNET.halving_interval
    assert consensus.subsidy(height, MAINNET) == int(expected_obsd * COIN)


def test_reward_reaches_zero_and_stays_there():
    """Emission must terminate. After 64 halvings no block may create coins."""
    final = MAX = consensus.MAX_HALVINGS * MAINNET.halving_interval
    assert consensus.subsidy(final, MAINNET) == 0
    assert consensus.subsidy(final + 1_000_000, MAINNET) == 0


def test_reward_never_increases_as_height_grows():
    """Monotonicity: no height anywhere may pay more than an earlier one."""
    previous = consensus.subsidy(0, MAINNET)
    for era in range(consensus.MAX_HALVINGS + 2):
        for offset in (0, 1, MAINNET.halving_interval - 1):
            height = era * MAINNET.halving_interval + offset
            current = consensus.subsidy(height, MAINNET)
            assert current <= previous
            previous = current


def test_negative_height_is_rejected():
    with pytest.raises(ValueError):
        consensus.subsidy(-1, MAINNET)


# --------------------------------------------------------------------------
# The supply cap — the property the whole design exists to guarantee
# --------------------------------------------------------------------------


def test_total_supply_never_exceeds_21_million():
    """The headline promise, checked by exhaustive summation over every era."""
    assert consensus.total_supply(MAINNET) <= 21_000_000 * COIN


def test_total_supply_is_just_under_21_million():
    """Integer truncation at each halving puts the true cap fractionally below
    the round number, at 20,999,999.9769 OBSD."""
    total = consensus.total_supply(MAINNET)
    assert 20_999_999 * COIN < total < 21_000_000 * COIN


def test_circulating_supply_matches_a_block_by_block_sum():
    """`circulating_supply` uses a closed form for speed. It must agree exactly
    with naively adding up every block's reward, or the explorer will lie."""
    params = REGTEST  # 10-block halvings, so this covers many eras cheaply
    running = 0
    for height in range(0, 200):
        running += consensus.subsidy(height, params)
        assert consensus.circulating_supply(height, params) == running


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=lambda p: p.name)
def test_no_network_can_ever_exceed_its_declared_cap(params):
    """Every network, not just mainnet, must respect its own ceiling."""
    assert consensus.total_supply(params) <= params.max_supply


@pytest.mark.parametrize("params", ALL_NETWORKS, ids=lambda p: p.name)
def test_circulating_supply_converges_to_total_supply(params):
    """Mine past the final halving and the circulating amount must equal the cap
    exactly — no coins unaccounted for, none created afterwards."""
    end = consensus.MAX_HALVINGS * params.halving_interval
    assert consensus.circulating_supply(end, params) == consensus.total_supply(params)
    assert (
        consensus.circulating_supply(end + 500, params)
        == consensus.total_supply(params)
    )


def test_supply_at_the_first_halving_is_half_the_eventual_total():
    """Half of all OBSD that will ever exist is mined in the first era — the same
    front-loaded emission curve the halving schedule produces."""
    at_first_halving = consensus.circulating_supply(
        MAINNET.halving_interval - 1, MAINNET
    )
    assert at_first_halving == 10_500_000 * COIN


# --------------------------------------------------------------------------
# Halving countdown, as the explorer reports it
# --------------------------------------------------------------------------


def test_blocks_until_halving_counts_down_correctly():
    assert consensus.blocks_until_halving(0, MAINNET) == 210_000
    assert consensus.blocks_until_halving(209_999, MAINNET) == 1
    assert consensus.blocks_until_halving(210_000, MAINNET) == 210_000


def test_next_halving_height_is_the_era_boundary():
    assert consensus.next_halving_height(5, MAINNET) == 210_000
    assert consensus.next_halving_height(210_001, MAINNET) == 420_000


def test_no_halving_remains_after_emission_ends():
    end = consensus.MAX_HALVINGS * MAINNET.halving_interval
    assert consensus.next_halving_height(end, MAINNET) is None
    assert consensus.blocks_until_halving(end, MAINNET) is None


# --------------------------------------------------------------------------
# Compact target encoding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bits", [0x1D00FFFF, 0x1F00FFFF, 0x207FFFFF, 0x1B0404CB, 0x04123456, 0x05009234]
)
def test_compact_target_round_trips(bits):
    assert target_to_compact(compact_to_target(bits)) == bits


@pytest.mark.parametrize("bits, expected", [(0x00000000, 0), (0x01003456, 0)])
def test_degenerate_encodings_decode_to_zero(bits, expected):
    """Some compact values shift their mantissa away entirely and mean zero.
    They cannot round-trip, and every node must decode them identically — one
    that read them differently would accept blocks the rest of the network
    rejects, and split off the chain."""
    assert compact_to_target(bits) == expected


def test_known_compact_encoding():
    """The difficulty-1 reference target, a widely published constant."""
    assert compact_to_target(0x1D00FFFF) == 0x00FFFF * 256 ** (0x1D - 3)


def test_negative_target_is_rejected():
    with pytest.raises(ValueError):
        compact_to_target(0x1D80FFFF)


def test_oversized_target_is_rejected():
    with pytest.raises(ValueError):
        compact_to_target(0xFF00FFFF)


def test_easier_target_means_lower_difficulty():
    easy = target_to_difficulty(compact_to_target(0x207FFFFF))
    hard = target_to_difficulty(compact_to_target(0x1D00FFFF))
    assert easy < hard


# --------------------------------------------------------------------------
# Difficulty retargeting
# --------------------------------------------------------------------------


def header(timestamp: int, bits: int = 0x1F00FFFF) -> BlockHeader:
    return BlockHeader(
        prev_hash=b"\x00" * 32,
        merkle_root=b"\x11" * 32,
        timestamp=timestamp,
        bits=bits,
    )


def test_difficulty_is_unchanged_between_retargets():
    previous = header(1000, bits=0x1E00FFFF)
    result = consensus.expected_bits(5, MAINNET, previous, header(0))
    assert result == 0x1E00FFFF


def test_genesis_uses_the_network_floor():
    assert (
        consensus.expected_bits(0, MAINNET, header(0), header(0))
        == MAINNET.pow_limit_bits
    )


def test_blocks_arriving_too_fast_make_mining_harder():
    """Half the intended time means twice the hashpower showed up, so the target
    must shrink."""
    intended = MAINNET.retarget_interval * MAINNET.target_block_time
    start = header(0, bits=0x1E00FFFF)
    previous = header(intended // 2, bits=0x1E00FFFF)

    new_bits = consensus.expected_bits(
        MAINNET.retarget_interval, MAINNET, previous, start
    )
    assert compact_to_target(new_bits) < compact_to_target(0x1E00FFFF)


def test_blocks_arriving_too_slowly_make_mining_easier():
    intended = MAINNET.retarget_interval * MAINNET.target_block_time
    start = header(0, bits=0x1E00FFFF)
    previous = header(intended * 2, bits=0x1E00FFFF)

    new_bits = consensus.expected_bits(
        MAINNET.retarget_interval, MAINNET, previous, start
    )
    assert compact_to_target(new_bits) > compact_to_target(0x1E00FFFF)


def test_adjustment_is_clamped_when_blocks_arrive_absurdly_fast():
    """An attacker who mines a period in one second must not be able to drive
    difficulty up — or, on reversal, collapse it. The clamp bounds the change to
    the configured factor."""
    start = header(0, bits=0x1E00FFFF)
    previous = header(1, bits=0x1E00FFFF)

    new_bits = consensus.expected_bits(
        MAINNET.retarget_interval, MAINNET, previous, start
    )
    ratio = compact_to_target(0x1E00FFFF) / compact_to_target(new_bits)
    assert ratio <= MAINNET.max_retarget_factor + 0.01


def test_adjustment_is_clamped_when_blocks_arrive_absurdly_slowly():
    intended = MAINNET.retarget_interval * MAINNET.target_block_time
    start = header(0, bits=0x1E00FFFF)
    previous = header(intended * 1000, bits=0x1E00FFFF)

    new_bits = consensus.expected_bits(
        MAINNET.retarget_interval, MAINNET, previous, start
    )
    ratio = compact_to_target(new_bits) / compact_to_target(0x1E00FFFF)
    assert ratio <= MAINNET.max_retarget_factor + 0.01


def test_difficulty_never_falls_below_the_network_floor():
    """However slowly blocks arrive, the target may not exceed the pow limit."""
    intended = MAINNET.retarget_interval * MAINNET.target_block_time
    start = header(0, bits=MAINNET.pow_limit_bits)
    previous = header(intended * 4, bits=MAINNET.pow_limit_bits)

    new_bits = consensus.expected_bits(
        MAINNET.retarget_interval, MAINNET, previous, start
    )
    assert compact_to_target(new_bits) <= compact_to_target(MAINNET.pow_limit_bits)


# --------------------------------------------------------------------------
# Median time past
# --------------------------------------------------------------------------


def test_median_time_past_of_odd_count():
    assert consensus.median_time_past([30, 10, 20]) == 20


def test_median_time_past_ignores_a_single_liar():
    """One miner claiming an absurd timestamp cannot move the median."""
    honest = [100, 101, 102, 103, 104]
    assert consensus.median_time_past(honest + [999_999_999]) in (102, 103)


def test_median_time_past_of_nothing_is_an_error():
    with pytest.raises(ValueError):
        consensus.median_time_past([])
