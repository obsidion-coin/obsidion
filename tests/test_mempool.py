"""Tests for mempool admission, eviction, selection and reorg recovery."""

from __future__ import annotations

import pytest

from obsidion import consensus, crypto
from obsidion.chainstate import ChainState
from obsidion.consensus import ConsensusError
from obsidion.mempool import Mempool
from obsidion.miner import mine_block
from obsidion.params import COIN, REGTEST
from obsidion.transaction import OutPoint, Transaction, TxIn, TxOut

ALICE_PRIV = crypto.generate_private_key()
ALICE_PUB = crypto.private_to_public(ALICE_PRIV)
ALICE = crypto.hash160(ALICE_PUB)

BOB_PRIV = crypto.generate_private_key()
BOB_PUB = crypto.private_to_public(BOB_PRIV)
BOB = crypto.hash160(BOB_PUB)

FILLER = crypto.hash160(b"filler-miner")


@pytest.fixture
def chain():
    state = ChainState(REGTEST)
    yield state
    state.close()


@pytest.fixture
def pool():
    return Mempool(REGTEST)


def mine(chain, miner=FILLER, mempool=None):
    block = mine_block(chain, mempool, miner)
    chain.accept_block(block, now=block.header.timestamp + 600)
    return block


def fund_alice(chain, blocks=1):
    """Mine `blocks` rewards to Alice, then age them past regtest maturity.

    Returns the coinbase txids, each holding one full subsidy.
    """
    rewards = [mine(chain, miner=ALICE).coinbase().txid() for _ in range(blocks)]
    for _ in range(REGTEST.coinbase_maturity):
        mine(chain)
    return rewards


def spend(prev_txid, amount_in, outputs, prev_index=0, priv=ALICE_PRIV, pub=ALICE_PUB):
    tx = Transaction(
        inputs=[TxIn(OutPoint(prev_txid, prev_index))],
        outputs=[TxOut(amount, pkh) for amount, pkh in outputs],
    )
    tx.sign_input(0, priv, pub, amount=amount_in)
    return tx


REWARD = consensus.subsidy(1, REGTEST)  # every early regtest block pays this


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def test_a_valid_payment_is_admitted_and_its_fee_reported(chain, pool):
    (funding,) = fund_alice(chain)
    fee = COIN // 10
    payment = spend(funding, REWARD, [(REWARD - fee, BOB)])

    assert pool.accept(payment, chain) == fee
    assert payment.txid() in pool


def test_a_coinbase_is_refused(chain, pool):
    coinbase = Transaction.coinbase(1, REWARD, ALICE)
    with pytest.raises(ConsensusError, match="coinbase"):
        pool.accept(coinbase, chain)


def test_the_same_transaction_cannot_enter_twice(chain, pool):
    (funding,) = fund_alice(chain)
    payment = spend(funding, REWARD, [(REWARD, BOB)])

    pool.accept(payment, chain)
    with pytest.raises(ConsensusError, match="already"):
        pool.accept(payment, chain)


def test_a_double_spend_against_the_pool_is_refused(chain, pool):
    (funding,) = fund_alice(chain)
    first = spend(funding, REWARD, [(REWARD, BOB)])
    rival = spend(funding, REWARD, [(REWARD, ALICE)])

    pool.accept(first, chain)
    with pytest.raises(ConsensusError, match="double-spend"):
        pool.accept(rival, chain)


def test_spending_an_unknown_output_is_refused(chain, pool):
    ghost = spend(crypto.sha256d(b"nothing"), 50 * COIN, [(50 * COIN, BOB)])
    with pytest.raises(ConsensusError, match="unknown"):
        pool.accept(ghost, chain)


def test_an_immature_coinbase_is_refused(chain, pool):
    block = mine(chain, miner=ALICE)  # not aged at all
    payment = spend(block.coinbase().txid(), REWARD, [(REWARD, BOB)])

    with pytest.raises(ConsensusError, match="mature"):
        pool.accept(payment, chain)


def test_a_bad_signature_is_refused(chain, pool):
    (funding,) = fund_alice(chain)
    theft = spend(funding, REWARD, [(REWARD, BOB)], priv=BOB_PRIV, pub=BOB_PUB)

    with pytest.raises(ConsensusError, match="signature"):
        pool.accept(theft, chain)


def test_outputs_beyond_inputs_are_refused(chain, pool):
    (funding,) = fund_alice(chain)
    inflation = spend(funding, REWARD, [(REWARD + 1, BOB)])

    with pytest.raises(ConsensusError, match="flows in"):
        pool.accept(inflation, chain)


def test_a_child_of_a_pooled_transaction_is_admitted(chain, pool):
    """Unconfirmed chains: pay someone, and they can spend it immediately —
    both transactions simply ride into the same block."""
    (funding,) = fund_alice(chain)
    to_bob = spend(funding, REWARD, [(REWARD, BOB)])
    pool.accept(to_bob, chain)

    onward = spend(
        to_bob.txid(), REWARD, [(REWARD, ALICE)], priv=BOB_PRIV, pub=BOB_PUB
    )
    assert pool.accept(onward, chain) == 0
    assert len(pool) == 2


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def test_selection_prefers_the_better_fee_rate(chain, pool):
    cheap_funding, rich_funding = fund_alice(chain, blocks=2)
    cheap = spend(cheap_funding, REWARD, [(REWARD - 1_000, BOB)])
    rich = spend(rich_funding, REWARD, [(REWARD - 1_000_000, BOB)])
    pool.accept(cheap, chain)
    pool.accept(rich, chain)

    chosen = pool.select(chain, max_bytes=1_000_000)
    assert [entry.txid for entry in chosen] == [rich.txid(), cheap.txid()]


def test_selection_never_places_a_child_before_its_parent(chain, pool):
    """A child paying a huge fee cannot jump the queue past its zero-fee
    parent: without the parent in the block, the child is invalid."""
    (funding,) = fund_alice(chain)
    parent = spend(funding, REWARD, [(REWARD, BOB)])  # zero fee
    child = spend(
        parent.txid(), REWARD, [(REWARD - COIN, ALICE)],
        priv=BOB_PRIV, pub=BOB_PUB,  # massive fee
    )
    pool.accept(parent, chain)
    pool.accept(child, chain)

    chosen = [entry.txid for entry in pool.select(chain, max_bytes=1_000_000)]
    assert chosen.index(parent.txid()) < chosen.index(child.txid())


def test_selection_respects_the_size_budget(chain, pool):
    fundings = fund_alice(chain, blocks=3)
    for funding in fundings:
        pool.accept(spend(funding, REWARD, [(REWARD - 1_000, BOB)]), chain)

    one_tx_budget = pool.entries[next(iter(pool.entries))].size + 10
    chosen = pool.select(chain, max_bytes=one_tx_budget)
    assert len(chosen) == 1


# --------------------------------------------------------------------------
# Confirmation and conflict eviction
# --------------------------------------------------------------------------


def test_confirmed_transactions_leave_the_pool(chain, pool):
    (funding,) = fund_alice(chain)
    payment = spend(funding, REWARD, [(REWARD - 1_000, BOB)])
    pool.accept(payment, chain)

    mine(chain, mempool=pool)  # mines the payment
    block = chain.block_at_height(chain.height)
    assert payment.txid() in {t.txid() for t in block.transactions}

    pool.remove_confirmed(block)
    assert payment.txid() not in pool
    assert len(pool) == 0


def test_a_block_evicts_pooled_rivals_it_conflicts_with(chain, pool):
    """If a block confirms one spend of an output, any pooled rival spend of
    that same output is dead and must go — along with its descendants."""
    (funding,) = fund_alice(chain)

    pooled = spend(funding, REWARD, [(REWARD, BOB)])
    pool.accept(pooled, chain)
    descendant = spend(
        pooled.txid(), REWARD, [(REWARD, ALICE)], priv=BOB_PRIV, pub=BOB_PUB
    )
    pool.accept(descendant, chain)

    # A rival spend of the same funding, mined directly without the pool.
    rival = spend(funding, REWARD, [(REWARD, ALICE)])
    from obsidion.miner import build_template, grind

    block = build_template(chain, None, FILLER)
    block.transactions.append(rival)
    from obsidion.merkle import merkle_root

    block.header.merkle_root = merkle_root([t.txid() for t in block.transactions])
    assert grind(block)
    chain.accept_block(block, now=block.header.timestamp + 600)

    pool.remove_confirmed(block)
    assert pooled.txid() not in pool
    assert descendant.txid() not in pool


# --------------------------------------------------------------------------
# Reorg recovery
# --------------------------------------------------------------------------


def test_a_reorged_payment_returns_to_the_pool_and_gets_remined(chain, pool):
    """The full life of an unlucky payment: mined, orphaned by a reorg, back to
    the mempool, mined again on the winning branch."""
    (funding,) = fund_alice(chain)
    fork_tip = chain.tip_hash
    fork_height = chain.height

    payment = spend(funding, REWARD, [(REWARD - 1_000, BOB)])
    pool.accept(payment, chain)
    mined = mine_block(chain, pool, FILLER)
    result = chain.accept_block(mined, now=mined.header.timestamp + 600)
    pool.remove_confirmed(mined)
    assert payment.txid() in {t.txid() for t in mined.transactions}

    # A rival branch from the fork point outworks the payment's block.
    rival_chain = ChainState(REGTEST)
    try:
        # Rebuild the same prefix on a scratch chain to mine the rival branch.
        for height in range(1, fork_height + 1):
            block = chain.block_at_height(height)
            rival_chain.accept_block(block, now=block.header.timestamp + 600)

        rival_blocks = []
        for _ in range(2):
            rival = mine_block(rival_chain, None, crypto.hash160(b"rival"))
            rival_chain.accept_block(rival, now=rival.header.timestamp + 600)
            rival_blocks.append(rival)
    finally:
        rival_chain.close()

    for rival in rival_blocks[:-1]:
        chain.accept_block(rival, now=rival.header.timestamp + 600)
    result = chain.accept_block(
        rival_blocks[-1], now=rival_blocks[-1].header.timestamp + 600
    )
    assert result.status == "reorged"

    # The payment fell out of the chain; reconsider puts it back in the pool.
    assert payment.txid() in {t.txid() for t in result.disconnected}
    readmitted = pool.reconsider(result.disconnected, chain)
    assert readmitted == 1
    assert payment.txid() in pool

    # And the next block on the winning branch carries it home.
    remined = mine_block(chain, pool, FILLER)
    chain.accept_block(remined, now=remined.header.timestamp + 600)
    assert payment.txid() in {t.txid() for t in remined.transactions}


def test_reconsider_drops_what_the_new_branch_invalidated(chain, pool):
    """A spend of a coinbase that only existed on the losing branch cannot be
    readmitted — its money never existed on the winning chain."""
    ghost_funding = mine(chain, miner=ALICE).coinbase().txid()
    for _ in range(REGTEST.coinbase_maturity):
        mine(chain)
    doomed = spend(ghost_funding, REWARD, [(REWARD, BOB)])

    # Simulate the disconnect: the transaction arrives via reconsider but its
    # funding coinbase is treated as never having existed.
    fresh = ChainState(REGTEST)
    try:
        assert pool.reconsider([doomed], fresh) == 0
        assert doomed.txid() not in pool
    finally:
        fresh.close()
