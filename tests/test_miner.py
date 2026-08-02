"""Tests for template assembly and the mining loop."""

from __future__ import annotations

import threading

import pytest

from obsidion import consensus, crypto
from obsidion.chainstate import ChainState
from obsidion.mempool import Mempool
from obsidion.miner import Miner, build_template, grind, mine_block
from obsidion.params import COIN, REGTEST
from obsidion.transaction import OutPoint, Transaction, TxIn, TxOut

ALICE_PRIV = crypto.generate_private_key()
ALICE_PUB = crypto.private_to_public(ALICE_PRIV)
ALICE = crypto.hash160(ALICE_PUB)

MINER_ADDR = crypto.hash160(b"the-miner")


@pytest.fixture
def chain():
    state = ChainState(REGTEST)
    yield state
    state.close()


def accept(chain, block):
    return chain.accept_block(block, now=block.header.timestamp + 600)


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------


def test_template_builds_on_the_tip_at_the_next_height(chain):
    template = build_template(chain, None, MINER_ADDR)

    assert template.header.prev_hash == chain.tip_hash
    assert template.height() == 1


def test_template_coinbase_claims_exactly_the_subsidy_when_there_are_no_fees(chain):
    template = build_template(chain, None, MINER_ADDR)
    assert template.coinbase().total_output_value() == consensus.subsidy(1, REGTEST)


def test_template_carries_the_required_bits_and_a_lawful_timestamp(chain):
    template = build_template(chain, None, MINER_ADDR)

    assert template.header.bits == chain.expected_bits_for_next(chain.tip_hash)
    assert template.header.timestamp >= chain.min_timestamp_for_next(chain.tip_hash)


def test_template_collects_mempool_transactions_and_their_fees(chain):
    pool = Mempool(REGTEST)
    funding_block = mine_block(chain, None, ALICE)
    accept(chain, funding_block)
    for _ in range(REGTEST.coinbase_maturity):
        accept(chain, mine_block(chain, None, MINER_ADDR))

    reward = consensus.subsidy(1, REGTEST)
    fee = COIN // 4
    payment = Transaction(
        inputs=[TxIn(OutPoint(funding_block.coinbase().txid(), 0))],
        outputs=[TxOut(reward - fee, MINER_ADDR)],
    )
    payment.sign_input(0, ALICE_PRIV, ALICE_PUB, amount=reward)
    pool.accept(payment, chain)

    template = build_template(chain, pool, MINER_ADDR)

    assert payment.txid() in {t.txid() for t in template.transactions}
    expected = consensus.subsidy(template.height(), REGTEST) + fee
    assert template.coinbase().total_output_value() == expected


def test_distinct_extra_nonces_give_distinct_search_spaces(chain):
    a = build_template(chain, None, MINER_ADDR, extra_nonce=0)
    b = build_template(chain, None, MINER_ADDR, extra_nonce=1)
    assert a.header.merkle_root != b.header.merkle_root


# --------------------------------------------------------------------------
# Grinding
# --------------------------------------------------------------------------


def test_grinding_solves_a_regtest_template(chain):
    template = build_template(chain, None, MINER_ADDR)
    assert grind(template, REGTEST.pow_algorithm)
    assert template.header.satisfies_pow(REGTEST.pow_algorithm)


def test_grinding_can_be_interrupted(chain):
    template = build_template(chain, None, MINER_ADDR, )
    template.header.bits = 0x1D00FFFF  # far too hard to solve by accident
    assert grind(template, REGTEST.pow_algorithm, should_stop=lambda: True) is False


def test_a_mined_block_is_accepted_by_the_chain_it_was_built_for(chain):
    block = mine_block(chain, None, MINER_ADDR)
    assert accept(chain, block).status == "connected"
    assert chain.height == 1


def test_mining_a_run_of_blocks_tracks_the_emission_schedule(chain):
    """Fifteen blocks on regtest crosses a halving boundary (interval 10), so
    this checks the miner keeps claiming the correct, shrinking reward."""
    for _ in range(15):
        accept(chain, mine_block(chain, None, MINER_ADDR))

    assert chain.height == 15
    assert chain.total_utxo_value() == consensus.circulating_supply(15, REGTEST)
    era_two_block = chain.block_at_height(12)
    assert era_two_block.coinbase().total_output_value() == consensus.subsidy(
        12, REGTEST
    )


# --------------------------------------------------------------------------
# The mining thread
# --------------------------------------------------------------------------


def test_the_miner_thread_finds_blocks_and_stops_cleanly(chain):
    found = []
    solved = threading.Event()

    def on_block(block):
        found.append(block)
        solved.set()

    miner = Miner(chain, None, MINER_ADDR, on_block)
    miner.start()
    assert solved.wait(timeout=30), "miner found nothing within 30 seconds"
    miner.stop()

    assert not miner.running
    assert miner.hashes_tried > 0

    # What it found must be a genuinely valid block.
    assert accept(chain, found[0]).status == "connected"
