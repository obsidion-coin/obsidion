"""Pre-launch adversarial audit: can anyone counterfeit OBSD or forge a block?

Written the night of the mainnet launch and kept afterwards. Every test here is
an *attack*, not an exercise: it constructs the cheat as convincingly as the
attacker would and asserts the node refuses it. Where a rule is already covered
elsewhere the attack is still repeated at block level, because a rule enforced
in a helper but never reached from `accept_block` protects nobody.

The two questions this file exists to answer:

  1. Can coins be created from nothing, or spent twice?
  2. Can a block be forged - wrong work, wrong contents, wrong claim?

A failure here is not a failing test. It is a counterfeit.
"""

from __future__ import annotations

import pytest

from obsidion import consensus, crypto
from obsidion.block import Block, BlockHeader
from obsidion.chainstate import ChainState
from obsidion.consensus import ConsensusError
from obsidion.genesis import UNSPENDABLE, create_genesis_block
from obsidion.merkle import merkle_root
from obsidion.params import COIN, REGTEST
from obsidion.transaction import (
    MAX_MONEY,
    NULL_TXID,
    OutPoint,
    Transaction,
    TxIn,
    TxOut,
)

from tests.test_chainstate import (
    ALICE,
    ALICE_PRIV,
    ALICE_PUB,
    BOB,
    BOB_PRIV,
    BOB_PUB,
    MINER,
    add,
    build_child,
    mine_to,
    spend,
)


@pytest.fixture
def chain():
    state = ChainState(REGTEST)
    yield state
    state.close()


def mature_coin(chain, owner=ALICE):
    """Mine a coinbase to `owner` and age it past maturity. Returns (txid, amount)."""
    blocks = mine_to(chain, owner, 1)
    coinbase = blocks[0].coinbase()
    mine_to(chain, MINER, REGTEST.coinbase_maturity)
    return coinbase.txid(), coinbase.total_output_value()


# ==========================================================================
# 1. Creating coins from nothing
# ==========================================================================


def test_the_genesis_reward_can_never_be_spent(chain):
    """The launch claims "no premine". This is that claim, mechanically.

    Genesis pays 50 OBSD to twenty zero bytes. The coins exist in the UTXO set
    and count toward circulating supply, but no private key can produce a
    public key hashing to that value, so nobody - including whoever launched
    the chain - can ever move them.
    """
    genesis = create_genesis_block(REGTEST)
    outpoint = OutPoint(genesis.coinbase().txid(), 0)

    entry = chain.get_utxo(outpoint)
    assert entry is not None, "the genesis output should exist"
    assert entry.pubkey_hash == UNSPENDABLE == b"\x00" * 20

    # Try to spend it anyway, with a well-formed signature from a real key.
    mine_to(chain, MINER, REGTEST.coinbase_maturity)
    theft = spend(outpoint.txid, 0, entry.amount, [(entry.amount, ALICE)])

    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, txs=[theft]))

    # And it is still there, untouched.
    assert chain.get_utxo(outpoint) is not None


def test_a_transaction_cannot_spend_the_same_output_twice_within_itself(chain):
    """The classic: name one input twice and claim its value twice."""
    txid, amount = mature_coin(chain)

    doubled = Transaction(
        inputs=[TxIn(OutPoint(txid, 0)), TxIn(OutPoint(txid, 0))],
        outputs=[TxOut(amount * 2, ALICE)],
    )
    doubled.sign_input(0, ALICE_PRIV, ALICE_PUB, amount=amount)
    doubled.sign_input(1, ALICE_PRIV, ALICE_PUB, amount=amount)

    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, txs=[doubled]))


def test_an_output_already_spent_in_an_earlier_block_cannot_be_spent_again(chain):
    """Double-spend across blocks, not within one."""
    txid, amount = mature_coin(chain)

    first = spend(txid, 0, amount, [(amount, BOB)])
    add(chain, build_child(chain, txs=[first]))

    # The same coin, spent again to a different recipient.
    again = spend(txid, 0, amount, [(amount, ALICE)])
    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, txs=[again]))


def test_a_signature_is_bound_to_the_amount_it_was_signed_for(chain):
    """BIP-143's amount commitment, which is what stops fee-inflation fraud.

    Sign as though the input were worth far more than it is. If the amount were
    outside the signed data the signature would still verify, and the node
    would credit the difference as fee - minting coins into the miner's own
    coinbase.
    """
    txid, amount = mature_coin(chain)

    lie = Transaction(
        inputs=[TxIn(OutPoint(txid, 0))],
        outputs=[TxOut(amount, ALICE)],
    )
    lie.sign_input(0, ALICE_PRIV, ALICE_PUB, amount=amount * 1000)

    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, txs=[lie]))


def test_a_block_cannot_spend_an_output_its_own_later_transaction_creates(chain):
    """Order matters: a forward reference would conjure an input from the future."""
    txid, amount = mature_coin(chain)

    parent = spend(txid, 0, amount, [(amount, BOB)])
    # Spends parent's output, but is placed BEFORE parent in the block.
    child = spend(parent.txid(), 0, amount, [(amount, ALICE)], BOB_PRIV, BOB_PUB)

    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, txs=[child, parent]))


def test_the_coinbase_cannot_claim_a_subsidy_from_an_earlier_era(chain):
    """Halvings are enforced against the real height, not the miner's opinion."""
    # regtest halves every 10 blocks, so height 11 pays half of the era-0 reward.
    mine_to(chain, MINER, 10)
    height = chain.height + 1
    assert consensus.subsidy(height, REGTEST) < consensus.subsidy(1, REGTEST)

    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, coinbase_reward=consensus.subsidy(1, REGTEST)))


def test_a_transaction_output_cannot_exceed_the_money_supply(chain):
    txid, amount = mature_coin(chain)

    absurd = Transaction(
        inputs=[TxIn(OutPoint(txid, 0))],
        outputs=[TxOut(MAX_MONEY + 1, ALICE)],
    )
    absurd.sign_input(0, ALICE_PRIV, ALICE_PUB, amount=amount)

    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, txs=[absurd]))


def test_a_negative_output_cannot_manufacture_a_balancing_credit(chain):
    """A negative amount would let outputs 'sum' to less than they really claim."""
    txid, amount = mature_coin(chain)

    trick = Transaction(
        inputs=[TxIn(OutPoint(txid, 0))],
        outputs=[TxOut(amount * 100, ALICE), TxOut(-amount * 99, BOB)],
    )
    trick.sign_input(0, ALICE_PRIV, ALICE_PUB, amount=amount)

    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, txs=[trick]))


def test_only_the_coinbase_may_spend_the_null_outpoint(chain):
    """Spending nothing is how coins are minted; ordinary transactions may not."""
    mine_to(chain, MINER, 1)

    from_nowhere = Transaction(
        inputs=[TxIn(OutPoint(NULL_TXID, 0xFFFFFFFF))],
        outputs=[TxOut(50 * COIN, ALICE)],
    )

    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, txs=[from_nowhere]))


def test_a_block_may_mint_coins_exactly_once(chain):
    """A second coinbase would be a second subsidy."""
    mine_to(chain, MINER, 1)
    height = chain.height + 1

    extra = Transaction.coinbase(height, consensus.subsidy(height, REGTEST), ALICE)
    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, txs=[extra]))


# ==========================================================================
# 2. Forging blocks
# ==========================================================================


def test_a_block_without_enough_work_is_refused():
    """Proof-of-work is the only thing making history expensive to rewrite.

    regtest cannot express this attack: its target is so loose that nearly
    every nonce is a valid solution, so an unsolved header is hard to even
    construct. This uses a network with a real - but cheap - target, where a
    wrong nonce fails immediately, which is the situation on mainnet.
    """
    from dataclasses import replace

    params = replace(
        REGTEST,
        pow_limit_bits=0x1E00FFFF,  # ~16 bits of work: instant, but not free
        genesis_nonce=None,  # must be re-mined for the new target
    )
    state = ChainState(params)
    try:
        parent_hash = state.tip_hash
        parent = state.index_entry(parent_hash)
        height = parent.height + 1
        coinbase = Transaction.coinbase(height, consensus.subsidy(height, params), MINER)
        header = BlockHeader(
            prev_hash=parent_hash,
            merkle_root=merkle_root([coinbase.txid()]),
            timestamp=params.genesis_timestamp + 1,
            bits=state.expected_bits_for_next(parent_hash),
        )

        # Solve it honestly first, so the only difference is the work itself.
        while not header.satisfies_pow(params.pow_algorithm):
            header.nonce += 1
        solved_nonce = header.nonce
        assert state.accept_block(
            Block(header, [coinbase]), now=header.timestamp + 600
        )

        # Now the same block with the work removed.
        header.nonce = solved_nonce + 1
        while header.satisfies_pow(params.pow_algorithm):
            header.nonce += 1
        assert not header.satisfies_pow(params.pow_algorithm)

        with pytest.raises(ConsensusError):
            state.accept_block(Block(header, [coinbase]), now=header.timestamp + 600)
    finally:
        state.close()


def test_transactions_cannot_be_swapped_under_a_solved_header(chain):
    """Take a valid block, keep its proof-of-work, replace what it pays out."""
    txid, amount = mature_coin(chain)
    honest = build_child(chain, txs=[spend(txid, 0, amount, [(amount, BOB)])])

    # Same header - same work, same hash - but the money now goes elsewhere.
    stolen = Transaction(
        inputs=honest.transactions[1].inputs,
        outputs=[TxOut(amount, ALICE)],
    )
    stolen.sign_input(0, ALICE_PRIV, ALICE_PUB, amount=amount)
    forged = Block(honest.header, [honest.coinbase(), stolen])

    with pytest.raises(ConsensusError):
        add(chain, forged)


def test_duplicating_the_trailing_transaction_pair_is_rejected(chain):
    """CVE-2012-2459: a merkle tree that duplicates an odd tail can be forged.

    Appending a copy of the final transaction produces an identical merkle root
    for a different transaction list. Unguarded, a peer can mutate a block into
    one that hashes the same but is invalid, and a node that caches the hash as
    bad then rejects the honest block for good.
    """
    txid, amount = mature_coin(chain)
    honest = build_child(chain, txs=[spend(txid, 0, amount, [(amount, BOB)])])

    duplicated = [*honest.transactions, honest.transactions[-1]]

    # Stronger than merely producing a different root: the tree refuses to
    # compute one at all, so the ambiguous shape cannot exist even briefly.
    with pytest.raises(ValueError, match="duplicate trailing"):
        merkle_root([t.txid() for t in duplicated])

    forged = Block(honest.header, duplicated)
    with pytest.raises(ConsensusError):
        add(chain, forged)

    # And the honest block is still accepted afterwards - no cache poisoning.
    assert add(chain, honest)
    assert chain.tip_hash == honest.hash()


def test_a_block_cannot_choose_its_own_difficulty(chain):
    """Difficulty is computed by every node; a miner does not get to pick it.

    Note the encoding is checked for equality with the expected value, so a
    *harder* claim is refused too. That is deliberate: allowing a miner any
    freedom over the field would let it steer the next retarget.

    (pow_limit_bits + 1 would seem the obvious cheat, but 0x20800000 sets the
    mantissa's sign bit and decodes to a negative target, which satisfies_pow
    reports as simply unsolvable - so it is unmineable rather than unfair.)
    """
    from obsidion.block import compact_to_target

    different = REGTEST.pow_limit_bits - 1
    assert compact_to_target(different) > 0, "the alternative must still be mineable"
    assert different != chain.expected_bits_for_next(chain.tip_hash)

    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, bits=different))


def test_a_block_cannot_lie_about_its_height(chain):
    """BIP-34: the coinbase commits to the height, so subsidy cannot be gamed."""
    mine_to(chain, MINER, 1)
    with pytest.raises(ConsensusError):
        add(chain, build_child(chain, claim_height=1))


# ==========================================================================
# 3. The invariant that has to hold no matter what
# ==========================================================================


def test_total_coins_in_existence_always_equal_the_emission_schedule(chain):
    """Across halvings, spends, and fees: not one shard more than the schedule.

    regtest halves every 10 blocks, so this walks several eras in a moment.
    """
    for _ in range(3):
        blocks = mine_to(chain, ALICE, 1)
        coinbase = blocks[0].coinbase()
        mine_to(chain, MINER, REGTEST.coinbase_maturity)

        amount = coinbase.total_output_value()
        # Spend it, deliberately underpaying the outputs so the rest is fee.
        fee = amount // 10
        add(
            chain,
            build_child(
                chain,
                txs=[spend(coinbase.txid(), 0, amount, [(amount - fee, BOB)])],
                reward_extra=fee,
            ),
        )

        expected = consensus.circulating_supply(chain.height, REGTEST)
        assert chain.total_utxo_value() == expected, (
            f"at height {chain.height} the UTXO set holds "
            f"{chain.total_utxo_value()} but the schedule allows {expected}"
        )

    assert chain.total_utxo_value() <= consensus.total_supply(REGTEST)
