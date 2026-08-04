"""The rules that define Obsidion.

Everything here answers one question: given a chain, is this block allowed? Every
node must answer identically, or the network splits into groups that disagree
about who owns what. That is why this module is pure — functions of their
arguments, with no clock, no randomness, no I/O, and no hidden state. Two nodes
running this code on the same inputs cannot reach different conclusions.

The two rules that give Obsidion its economics:

**Halving.** The block reward starts at 50 OBSD and halves every 210,000 blocks.
At 60-second blocks that is roughly every 146 days. After 64 halvings the reward
reaches zero and no new coins are ever created.

**The cap.** The cap is not a promise written in a whitepaper; it is arithmetic.
`subsidy()` is the only function in the entire system that creates coins, and a
block paying itself more than `subsidy()` allows is rejected by every node. The
supply limit is enforced by the same machinery that stops you spending someone
else's money.
"""

from __future__ import annotations

from obsidion.block import (
    Block,
    BlockHeader,
    compact_to_target,
    target_to_compact,
)
from obsidion.params import NetworkParams

#: After this many halvings the reward is zero. 50 OBSD right-shifted 64 times
#: is 0 regardless, but stating the limit makes the end of emission explicit and
#: bounds every loop over eras.
MAX_HALVINGS = 64


# --------------------------------------------------------------------------
# Emission
# --------------------------------------------------------------------------


def subsidy(height: int, params: NetworkParams) -> int:
    """The block reward at `height`, in shards.

    This is the only function in Obsidion that brings coins into existence.
    """
    if height < 0:
        raise ValueError(f"height cannot be negative: {height}")

    halvings = height // params.halving_interval
    if halvings >= MAX_HALVINGS:
        return 0

    # Integer right-shift, not division by two. Truncation is deliberate and is
    # why the true cap sits fractionally under the round number: 50 OBSD in
    # shards is 5 x 10^9, which carries only nine factors of two, so from the
    # tenth halving onward each era discards a remainder that is never minted.
    return params.initial_subsidy >> halvings


def halving_era(height: int, params: NetworkParams) -> int:
    """Which halving era `height` falls in. Era 0 pays the full reward."""
    return height // params.halving_interval


def next_halving_height(height: int, params: NetworkParams) -> int | None:
    """The height of the next halving, or None once emission has ended."""
    era = halving_era(height, params)
    if era >= MAX_HALVINGS - 1:
        return None
    return (era + 1) * params.halving_interval


def blocks_until_halving(height: int, params: NetworkParams) -> int | None:
    """Blocks remaining until the reward halves, or None if it never will again."""
    target = next_halving_height(height, params)
    return None if target is None else target - height


def total_supply(params: NetworkParams) -> int:
    """The exact number of shards that will ever exist.

    Computed rather than declared, by summing the reward across every halving
    era. Because each era's reward is truncated by integer division, the result
    is slightly below `initial_subsidy * halving_interval * 2`.
    """
    return sum(
        (params.initial_subsidy >> era) * params.halving_interval
        for era in range(MAX_HALVINGS)
    )


def circulating_supply(height: int, params: NetworkParams) -> int:
    """Total shards issued by all blocks from genesis through `height`."""
    if height < 0:
        return 0

    issued = 0
    era = 0
    while era < MAX_HALVINGS:
        era_start = era * params.halving_interval
        if era_start > height:
            break
        era_end = min((era + 1) * params.halving_interval - 1, height)
        issued += (params.initial_subsidy >> era) * (era_end - era_start + 1)
        era += 1
    return issued


# --------------------------------------------------------------------------
# Difficulty
# --------------------------------------------------------------------------


def expected_bits(
    height: int,
    params: NetworkParams,
    previous: BlockHeader,
    period_start: BlockHeader,
) -> int:
    """The difficulty target a block at `height` must use.

    Outside a retarget boundary the answer is simply the parent's target. On a
    boundary, the target is scaled by how far the last period's actual duration
    strayed from its intended one: blocks arriving too fast make the target
    smaller (harder), too slow makes it larger (easier).

    The adjustment is clamped to `max_retarget_factor` in each direction. Without
    a clamp, a miner with temporary majority hashpower could crash difficulty to
    near zero in a single period and then mint blocks unopposed.
    """
    if height == 0:
        return params.pow_limit_bits
    if height % params.retarget_interval != 0:
        return previous.bits

    intended = params.retarget_interval * params.target_block_time
    actual = previous.timestamp - period_start.timestamp

    lowest = intended // params.max_retarget_factor
    highest = intended * params.max_retarget_factor
    actual = max(lowest, min(highest, actual))

    new_target = compact_to_target(previous.bits) * actual // intended

    # Difficulty may never fall below the network's floor.
    limit = compact_to_target(params.pow_limit_bits)
    new_target = min(new_target, limit)

    return target_to_compact(new_target)


def retarget_period_start_height(height: int, params: NetworkParams) -> int:
    """First height of the retarget period that ends at `height`."""
    if height < params.retarget_interval:
        return 0
    return height - params.retarget_interval


def median_time_past(timestamps: list[int]) -> int:
    """Median of recent block timestamps.

    A block's timestamp must exceed this. Using a median rather than the parent's
    timestamp means a single miner cannot move the network's notion of time —
    they would have to control a majority of the last several blocks.
    """
    if not timestamps:
        raise ValueError("cannot take the median of no timestamps")
    ordered = sorted(timestamps)
    return ordered[len(ordered) // 2]


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


class ConsensusError(Exception):
    """A block or transaction breaks a consensus rule.

    Distinct from ValueError so that "this peer sent us something invalid" is
    never confused with "our own code was called wrongly".
    """


def check_header_pow(header: BlockHeader, params: NetworkParams) -> None:
    """Confirm the header's mining hash meets the target it declares."""
    try:
        target = header.target()
    except ValueError as exc:
        raise ConsensusError(f"malformed difficulty bits: {exc}") from exc

    if target <= 0:
        raise ConsensusError("difficulty target is zero")
    if not header.satisfies_pow(params.pow_algorithm):
        raise ConsensusError(
            f"insufficient proof of work: block {header.hash_hex()} does not "
            f"meet its target under {params.pow_algorithm}"
        )


def check_block_structure(block: Block, params: NetworkParams) -> None:
    """Context-free block checks: true or false without reference to any chain.

    Anything checkable here is checked here, because these rules cost nothing and
    reject malformed blocks before the expensive signature and UTXO work begins.
    """
    if not block.transactions:
        raise ConsensusError("block contains no transactions")

    if not block.transactions[0].is_coinbase():
        raise ConsensusError("first transaction in a block must be the coinbase")

    for position, transaction in enumerate(block.transactions[1:], start=1):
        if transaction.is_coinbase():
            raise ConsensusError(
                f"extra coinbase transaction at position {position}; "
                "a block may create coins exactly once"
            )

    size = block.size()
    if size > params.max_block_weight:
        raise ConsensusError(
            f"block is {size} bytes, limit is {params.max_block_weight}"
        )

    for transaction in block.transactions:
        try:
            transaction.check_structure()
        except ValueError as exc:
            raise ConsensusError(f"invalid transaction {transaction!r}: {exc}") from exc

    # Duplicate transactions inside one block would each need spending twice.
    txids = block.txids()
    if len(set(txids)) != len(txids):
        raise ConsensusError("block contains the same transaction twice")

    try:
        computed = block.computed_merkle_root()
    except ValueError as exc:
        raise ConsensusError(f"cannot compute merkle root: {exc}") from exc

    if computed != block.header.merkle_root:
        raise ConsensusError(
            "merkle root does not match the transactions in the block"
        )

    check_header_pow(block.header, params)


def check_coinbase_reward(block: Block, height: int, fees: int, params: NetworkParams) -> None:
    """Confirm the miner paid itself no more than the rules allow.

    This single check is what enforces the 21 million cap. A miner may always
    claim *less* than the maximum — burning reward is wasteful but harmless, and
    forbidding it would reject otherwise valid blocks.
    """
    allowed = subsidy(height, params) + fees
    claimed = block.coinbase().total_output_value()

    if claimed > allowed:
        raise ConsensusError(
            f"coinbase claims {claimed} shards but only {allowed} are permitted "
            f"at height {height} ({subsidy(height, params)} subsidy + {fees} fees)"
        )


def check_coinbase_height(block: Block, height: int) -> None:
    """Confirm the coinbase records the height the block is actually being added at."""
    try:
        claimed = block.height()
    except ValueError as exc:
        raise ConsensusError(f"coinbase does not encode a height: {exc}") from exc

    if claimed != height:
        raise ConsensusError(
            f"coinbase claims height {claimed} but the block sits at {height}"
        )


def check_timestamp(
    header: BlockHeader,
    recent_timestamps: list[int],
    now: int,
    params: NetworkParams,
) -> None:
    """Confirm the block's timestamp is neither in the past nor far in the future.

    Timestamps matter because difficulty depends on them. A miner who could
    backdate blocks would make the network believe blocks are arriving slowly and
    lower the difficulty.
    """
    if recent_timestamps:
        floor = median_time_past(recent_timestamps)
        if header.timestamp <= floor:
            raise ConsensusError(
                f"timestamp {header.timestamp} is not after the median of recent "
                f"blocks ({floor})"
            )

    ceiling = now + params.max_future_block_time
    if header.timestamp > ceiling:
        raise ConsensusError(
            f"timestamp {header.timestamp} is more than "
            f"{params.max_future_block_time}s in the future"
        )


def check_expected_bits(header: BlockHeader, expected: int) -> None:
    """Confirm the block used the difficulty the network requires at this height."""
    if header.bits != expected:
        raise ConsensusError(
            f"block declares difficulty bits 0x{header.bits:08x} but the rules "
            f"require 0x{expected:08x} at this height"
        )
