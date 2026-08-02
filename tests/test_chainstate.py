"""Tests for the chain state: UTXO tracking, block connection, and reorgs.

The reorg tests are the most important here. Fork resolution is where consensus
implementations historically break, because it is the only code path that must
*undo* what it previously did and end in a state identical to never having done
it at all.
"""

from __future__ import annotations

import pytest

from obsidion import consensus, crypto
from obsidion.block import Block, BlockHeader
from obsidion.chainstate import ChainState, OrphanError
from obsidion.consensus import ConsensusError
from obsidion.genesis import create_genesis_block
from obsidion.merkle import merkle_root
from obsidion.params import COIN, REGTEST
from obsidion.transaction import OutPoint, Transaction, TxIn, TxOut

# Deterministic-enough test identities, generated once per run.
ALICE_PRIV = crypto.generate_private_key()
ALICE_PUB = crypto.private_to_public(ALICE_PRIV)
ALICE = crypto.hash160(ALICE_PUB)

BOB_PRIV = crypto.generate_private_key()
BOB_PUB = crypto.private_to_public(BOB_PRIV)
BOB = crypto.hash160(BOB_PUB)

MINER = crypto.hash160(b"anonymous-miner")


@pytest.fixture
def chain():
    state = ChainState(REGTEST)
    yield state
    state.close()


def build_child(
    chain,
    parent_hash=None,
    txs=(),
    miner=MINER,
    reward_extra=0,
    coinbase_reward=None,
    bits=None,
    timestamp=None,
    claim_height=None,
):
    """Assemble and mine a block extending `parent_hash` (default: the tip).

    Every field is overridable so each test can break exactly one rule.
    """
    parent_hash = parent_hash or chain.tip_hash
    parent = chain.index_entry(parent_hash)
    height = claim_height if claim_height is not None else parent.height + 1

    if coinbase_reward is None:
        coinbase_reward = consensus.subsidy(height, REGTEST) + reward_extra
    coinbase = Transaction.coinbase(height, coinbase_reward, miner)

    transactions = [coinbase, *txs]
    header = BlockHeader(
        prev_hash=parent_hash,
        merkle_root=merkle_root([t.txid() for t in transactions]),
        timestamp=(
            timestamp
            if timestamp is not None
            else max(
                chain.min_timestamp_for_next(parent_hash),
                REGTEST.genesis_timestamp + parent.height + 1,
            )
        ),
        bits=bits if bits is not None else chain.expected_bits_for_next(parent_hash),
    )

    block = Block(header, transactions)
    while not block.header.satisfies_pow():
        block.header.nonce += 1
    return block


def add(chain, block):
    """Accept a block with a clock comfortably ahead of its timestamp."""
    return chain.accept_block(block, now=block.header.timestamp + 600)


def spend(prev_txid, prev_index, amount_in, outputs, priv=ALICE_PRIV, pub=ALICE_PUB):
    """Build and sign a one-input transaction."""
    tx = Transaction(
        inputs=[TxIn(OutPoint(prev_txid, prev_index))],
        outputs=[TxOut(amount, pkh) for amount, pkh in outputs],
    )
    tx.sign_input(0, priv, pub, amount=amount_in)
    return tx


def mine_to(chain, miner, count):
    """Extend the active chain by `count` blocks paying `miner`."""
    blocks = []
    for _ in range(count):
        block = build_child(chain, miner=miner)
        add(chain, block)
        blocks.append(block)
    return blocks


# --------------------------------------------------------------------------
# Initialisation
# --------------------------------------------------------------------------


def test_fresh_chain_starts_at_genesis(chain):
    genesis = create_genesis_block(REGTEST)
    assert chain.tip_hash == genesis.hash()
    assert chain.height == 0


def test_genesis_output_is_tracked_in_the_utxo_set(chain):
    genesis = create_genesis_block(REGTEST)
    entry = chain.get_utxo(OutPoint(genesis.coinbase().txid(), 0))
    assert entry is not None
    assert entry.amount == consensus.subsidy(0, REGTEST)


def test_utxo_total_matches_circulating_supply_from_the_start(chain):
    assert chain.total_utxo_value() == consensus.circulating_supply(0, REGTEST)


# --------------------------------------------------------------------------
# Extending the chain
# --------------------------------------------------------------------------


def test_extending_the_tip_connects(chain):
    block = build_child(chain)
    result = add(chain, block)

    assert result.status == "connected"
    assert chain.tip_hash == block.hash()
    assert chain.height == 1


def test_connected_coinbase_enters_the_utxo_set(chain):
    block = build_child(chain, miner=ALICE)
    add(chain, block)

    entry = chain.get_utxo(OutPoint(block.coinbase().txid(), 0))
    assert entry is not None
    assert entry.amount == consensus.subsidy(1, REGTEST)
    assert entry.is_coinbase


def test_duplicate_block_is_reported_not_reapplied(chain):
    block = build_child(chain)
    add(chain, block)
    total_before = chain.total_utxo_value()

    assert add(chain, block).status == "duplicate"
    assert chain.total_utxo_value() == total_before


def test_block_with_unknown_parent_is_an_orphan(chain):
    stranger = build_child(chain)
    stranger.header.prev_hash = crypto.sha256d(b"never heard of it")
    while not stranger.header.satisfies_pow():
        stranger.header.nonce += 1

    with pytest.raises(OrphanError):
        add(chain, stranger)


# --------------------------------------------------------------------------
# Contextual rejection
# --------------------------------------------------------------------------


def test_wrong_coinbase_height_is_rejected(chain):
    block = build_child(chain, claim_height=7)
    with pytest.raises(ConsensusError, match="height"):
        add(chain, block)


def test_wrong_difficulty_bits_are_rejected(chain):
    block = build_child(chain, bits=0x207FFF00)  # still easy, but not the rule
    with pytest.raises(ConsensusError, match="bits"):
        add(chain, block)


def test_timestamp_not_beyond_median_is_rejected(chain):
    block = build_child(chain, timestamp=REGTEST.genesis_timestamp)
    with pytest.raises(ConsensusError, match="median"):
        add(chain, block)


def test_timestamp_too_far_in_the_future_is_rejected(chain):
    block = build_child(chain)
    with pytest.raises(ConsensusError, match="future"):
        chain.accept_block(
            block, now=block.header.timestamp - REGTEST.max_future_block_time - 60
        )


# --------------------------------------------------------------------------
# Spending
# --------------------------------------------------------------------------


def test_a_mature_coinbase_can_be_spent(chain):
    reward_block = build_child(chain, miner=ALICE)
    add(chain, reward_block)
    mine_to(chain, MINER, 1)  # age the reward past regtest maturity of 2

    reward = consensus.subsidy(1, REGTEST)
    payment = spend(reward_block.coinbase().txid(), 0, reward, [(reward, BOB)])
    add(chain, build_child(chain, txs=[payment]))

    assert chain.get_utxo(OutPoint(reward_block.coinbase().txid(), 0)) is None
    entry = chain.get_utxo(OutPoint(payment.txid(), 0))
    assert entry is not None and entry.pubkey_hash == BOB


def test_an_immature_coinbase_cannot_be_spent(chain):
    reward_block = build_child(chain, miner=ALICE)
    add(chain, reward_block)  # height 1; spending at 2 gives age 1 < maturity 2

    reward = consensus.subsidy(1, REGTEST)
    payment = spend(reward_block.coinbase().txid(), 0, reward, [(reward, BOB)])

    with pytest.raises(ConsensusError, match="matur"):
        add(chain, build_child(chain, txs=[payment]))


def test_spending_a_nonexistent_output_is_rejected(chain):
    ghost = spend(crypto.sha256d(b"never existed"), 0, 50 * COIN, [(50 * COIN, BOB)])
    tip_before = chain.tip_hash

    with pytest.raises(ConsensusError, match="missing|spent"):
        add(chain, build_child(chain, txs=[ghost]))
    assert chain.tip_hash == tip_before


def test_a_rejected_block_stays_rejected(chain):
    ghost = spend(crypto.sha256d(b"never existed"), 0, 50 * COIN, [(50 * COIN, BOB)])
    block = build_child(chain, txs=[ghost])

    with pytest.raises(ConsensusError):
        add(chain, block)
    with pytest.raises(ConsensusError, match="previously rejected"):
        add(chain, block)


def test_spending_with_the_wrong_key_is_rejected(chain):
    reward_block = build_child(chain, miner=ALICE)
    add(chain, reward_block)
    mine_to(chain, MINER, 1)

    reward = consensus.subsidy(1, REGTEST)
    theft = spend(
        reward_block.coinbase().txid(), 0, reward, [(reward, BOB)],
        priv=BOB_PRIV, pub=BOB_PUB,  # Bob signing for Alice's coin
    )

    with pytest.raises(ConsensusError, match="signature"):
        add(chain, build_child(chain, txs=[theft]))


def test_outputs_exceeding_inputs_are_rejected(chain):
    """The rule that makes printing money impossible for everyone but subsidy()."""
    reward_block = build_child(chain, miner=ALICE)
    add(chain, reward_block)
    mine_to(chain, MINER, 1)

    reward = consensus.subsidy(1, REGTEST)
    inflation = spend(
        reward_block.coinbase().txid(), 0, reward, [(reward + 1, BOB)]
    )

    with pytest.raises(ConsensusError, match="cannot create money"):
        add(chain, build_child(chain, txs=[inflation]))


def test_fees_flow_to_the_miner(chain):
    reward_block = build_child(chain, miner=ALICE)
    add(chain, reward_block)
    mine_to(chain, MINER, 1)

    reward = consensus.subsidy(1, REGTEST)
    fee = COIN // 100
    payment = spend(
        reward_block.coinbase().txid(), 0, reward, [(reward - fee, BOB)]
    )

    block = build_child(chain, txs=[payment], reward_extra=fee)
    assert add(chain, block).status == "connected"


def test_claiming_more_than_subsidy_plus_fees_is_rejected(chain):
    block = build_child(chain, reward_extra=1)  # one shard too greedy
    with pytest.raises(ConsensusError, match="coinbase claims"):
        add(chain, block)


def test_two_transactions_cannot_spend_the_same_output_in_one_block(chain):
    reward_block = build_child(chain, miner=ALICE)
    add(chain, reward_block)
    mine_to(chain, MINER, 1)

    reward = consensus.subsidy(1, REGTEST)
    first = spend(reward_block.coinbase().txid(), 0, reward, [(reward, BOB)])
    second = spend(reward_block.coinbase().txid(), 0, reward, [(reward, ALICE)])

    with pytest.raises(ConsensusError, match="missing|spent"):
        add(chain, build_child(chain, txs=[first, second]))


def test_a_chain_of_spends_within_one_block_connects(chain):
    """Transaction B may spend transaction A's output in the same block,
    provided A comes first — exactly as in Bitcoin."""
    reward_block = build_child(chain, miner=ALICE)
    add(chain, reward_block)
    mine_to(chain, MINER, 1)

    reward = consensus.subsidy(1, REGTEST)
    first = spend(
        reward_block.coinbase().txid(), 0, reward,
        [(reward // 2, ALICE), (reward - reward // 2, ALICE)],
    )
    second = spend(first.txid(), 0, reward // 2, [(reward // 2, BOB)])

    add(chain, build_child(chain, txs=[first, second]))
    assert chain.get_utxo(OutPoint(second.txid(), 0)) is not None
    assert chain.get_utxo(OutPoint(first.txid(), 0)) is None
    assert chain.get_utxo(OutPoint(first.txid(), 1)) is not None


# --------------------------------------------------------------------------
# Fork choice and reorgs
# --------------------------------------------------------------------------


def test_a_lighter_branch_is_stored_as_a_side_chain(chain):
    genesis_hash = chain.tip_hash
    mine_to(chain, MINER, 2)
    tip_before = chain.tip_hash

    rival = build_child(chain, parent_hash=genesis_hash, miner=BOB)
    result = add(chain, rival)

    assert result.status == "sidechain"
    assert chain.tip_hash == tip_before
    # Side-chain outputs must not leak into the active UTXO set.
    assert chain.get_utxo(OutPoint(rival.coinbase().txid(), 0)) is None


def test_reorg_to_the_heavier_branch(chain):
    """The core scenario: a partitioned miner returns with more cumulative work,
    and the network must abandon its blocks and adopt the rival chain."""
    genesis_hash = chain.tip_hash

    # Active branch: three blocks, the last carrying a real payment.
    a1 = build_child(chain, miner=ALICE)
    add(chain, a1)
    mine_to(chain, MINER, 1)
    reward = consensus.subsidy(1, REGTEST)
    payment = spend(a1.coinbase().txid(), 0, reward, [(reward, BOB)])
    add(chain, build_child(chain, txs=[payment]))

    # Rival branch from genesis: four blocks, more total work.
    cursor = genesis_hash
    rivals = []
    for _ in range(4):
        rival = build_child(chain, parent_hash=cursor, miner=BOB)
        result = add(chain, rival)
        rivals.append(rival)
        cursor = rival.hash()

    assert result.status == "reorged"
    assert chain.tip_hash == rivals[-1].hash()
    assert chain.height == 4

    # The abandoned branch's coins are gone from the UTXO set...
    assert chain.get_utxo(OutPoint(a1.coinbase().txid(), 0)) is None
    assert chain.get_utxo(OutPoint(payment.txid(), 0)) is None
    # ...the new branch's coins are present...
    assert chain.get_utxo(OutPoint(rivals[0].coinbase().txid(), 0)) is not None
    # ...and the orphaned payment is handed back for the mempool.
    assert payment.txid() in {t.txid() for t in result.disconnected}


def test_reorg_leaves_the_utxo_set_identical_to_a_fresh_replay(chain):
    """The gold-standard property: after a reorg, state must equal what a
    from-scratch node syncing only the winning branch would compute."""
    genesis_hash = chain.tip_hash
    mine_to(chain, MINER, 2)

    cursor = genesis_hash
    rivals = []
    for _ in range(3):
        rival = build_child(chain, parent_hash=cursor, miner=BOB)
        add(chain, rival)
        cursor = rival.hash()
        rivals.append(rival)

    replay = ChainState(REGTEST)
    try:
        for rival in rivals:
            replay.accept_block(rival, now=rival.header.timestamp + 600)

        assert chain.tip_hash == replay.tip_hash
        assert chain.total_utxo_value() == replay.total_utxo_value()
        assert sorted(chain.all_utxo_outpoints()) == sorted(
            replay.all_utxo_outpoints()
        )
    finally:
        replay.close()


def test_reorg_onto_an_invalid_branch_is_refused_and_state_survives(chain):
    """A heavier branch hiding an invalid transaction must not win. The reorg
    attempt fails, and — critically — the original chain state is untouched."""
    genesis_hash = chain.tip_hash
    mine_to(chain, ALICE, 2)
    tip_before = chain.tip_hash
    total_before = chain.total_utxo_value()

    ghost = spend(crypto.sha256d(b"counterfeit"), 0, 50 * COIN, [(50 * COIN, BOB)])
    c1 = build_child(chain, parent_hash=genesis_hash, miner=BOB)
    add(chain, c1)
    c2 = build_child(chain, parent_hash=c1.hash(), miner=BOB, txs=[ghost])
    add(chain, c2)  # side chain: its transactions are not validated yet
    c3 = build_child(chain, parent_hash=c2.hash(), miner=BOB)

    with pytest.raises(ConsensusError):
        add(chain, c3)

    assert chain.tip_hash == tip_before
    assert chain.total_utxo_value() == total_before


def test_supply_invariant_holds_through_growth_and_reorg(chain):
    """At every height, the sum of all UTXOs equals the circulating supply.
    Fees move value between parties; only subsidy creates it."""
    genesis_hash = chain.tip_hash
    mine_to(chain, MINER, 3)
    assert chain.total_utxo_value() == consensus.circulating_supply(3, REGTEST)

    cursor = genesis_hash
    for _ in range(5):
        rival = build_child(chain, parent_hash=cursor, miner=BOB)
        add(chain, rival)
        cursor = rival.hash()

    assert chain.height == 5
    assert chain.total_utxo_value() == consensus.circulating_supply(5, REGTEST)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_chain_survives_a_restart(tmp_path):
    path = str(tmp_path / "chain.db")

    first = ChainState(REGTEST, path)
    try:
        blocks = mine_to(first, ALICE, 3)
        tip = first.tip_hash
        total = first.total_utxo_value()
    finally:
        first.close()

    second = ChainState(REGTEST, path)
    try:
        assert second.tip_hash == tip
        assert second.height == 3
        assert second.total_utxo_value() == total
        stored = second.get_block(blocks[1].hash())
        assert stored is not None and stored.hash() == blocks[1].hash()
    finally:
        second.close()


def test_wallet_can_find_its_coins_by_pubkey_hash(chain):
    mine_to(chain, ALICE, 2)
    found = chain.utxos_for_pubkey_hash(ALICE)

    assert len(found) == 2
    assert all(entry.pubkey_hash == ALICE for _, entry in found)
    assert sum(entry.amount for _, entry in found) == consensus.circulating_supply(
        2, REGTEST
    ) - consensus.subsidy(0, REGTEST)
