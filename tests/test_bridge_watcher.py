"""Tests for the deposit watcher.

`reconcile` is pure, so every rule it enforces can be checked with plain
dictionaries — no chain, no Solana, no money. That is the point: the rules
below are the difference between minting against a real deposit and minting
against something that was never there.
"""

from __future__ import annotations

import pytest

from bridge.ledger import BridgeLedger, Deposit, State
from bridge.watcher import DEFAULT_MINIMUM_SHARDS, Actions, reconcile

SOLANA = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
DEPOSIT_ADDRESS = "obsd1ayca5khxahcqsqvmj97un3racxeegj4hsuvktd"
COIN = 100_000_000


def utxo(txid="aa" * 32, index=0, amount="50", height=100, coinbase=False) -> dict:
    """A UTXO in the shape getaddressutxos returns: amounts as decimal strings."""
    return {
        "txid": txid,
        "index": index,
        "amount": amount,
        "height": height,
        "coinbase": coinbase,
    }


def run(seen, known=None, *, tip=200, destinations=None, minimum=DEFAULT_MINIMUM_SHARDS):
    return reconcile(
        tip,
        seen,
        known or {},
        destinations if destinations is not None else {DEPOSIT_ADDRESS: SOLANA},
        minimum_shards=minimum,
    )


# --------------------------------------------------------------------------
# Crediting the right things
# --------------------------------------------------------------------------


def test_a_deposit_is_recorded_against_the_right_solana_wallet():
    actions = run({DEPOSIT_ADDRESS: [utxo()]})

    assert len(actions.record) == 1
    deposit = actions.record[0]
    assert deposit.amount == 50 * COIN
    assert deposit.solana_address == SOLANA
    assert deposit.deposit_address == DEPOSIT_ADDRESS
    assert deposit.height == 100


def test_two_outputs_of_one_transaction_are_two_deposits():
    """A single transaction can pay the same deposit address twice, and both
    are real money. Keying on the outpoint rather than the txid is what makes
    that work."""
    actions = run({DEPOSIT_ADDRESS: [utxo(index=0, amount="10"), utxo(index=1, amount="25")]})

    assert {d.amount for d in actions.record} == {10 * COIN, 25 * COIN}


def test_amounts_in_scientific_notation_are_read_correctly():
    """format_amount emits '4E-8' for small values. A naive string split would
    credit the wrong number, which is the whole reason the bridge reuses the
    node's own parser instead of writing a third one."""
    actions = run({DEPOSIT_ADDRESS: [utxo(amount="1E-2")]}, minimum=1)

    assert actions.record[0].amount == 1_000_000  # 0.01 OBSD


def test_re_seeing_a_known_deposit_records_nothing_new():
    """The normal case on every single poll, and after every restart."""
    known = {f"{'aa' * 32}:0": State.PENDING}
    actions = run({DEPOSIT_ADDRESS: [utxo()]}, known)

    assert actions.record == []
    assert actions.abandon == []


# --------------------------------------------------------------------------
# Refusing to credit the wrong things
# --------------------------------------------------------------------------


def test_a_coinbase_output_is_never_credited():
    """A mining reward landing on a deposit address is not somebody's deposit.
    Crediting one would mint wrapped tokens nobody paid for."""
    actions = run({DEPOSIT_ADDRESS: [utxo(coinbase=True)]})

    assert actions.record == []
    assert any("coinbase" in reason for _, reason in actions.ignored)


def test_dust_below_the_minimum_is_ignored():
    """Minting costs a fee on the Solana side. Crediting dust costs the bridge
    more than the deposit is worth, which is a cheap way to bleed it."""
    actions = run({DEPOSIT_ADDRESS: [utxo(amount="0.001")]})

    assert actions.record == []
    assert any("minimum" in reason for _, reason in actions.ignored)


def test_coins_at_an_unassigned_address_are_never_credited():
    """Without a destination there is no payee. Inventing one is worse than
    doing nothing."""
    actions = run({"obsd1someoneelsesaddress": [utxo()]}, destinations={})

    assert actions.record == []
    assert any("no destination" in reason for _, reason in actions.ignored)


def test_an_unreadable_amount_is_ignored_rather_than_guessed():
    actions = run({DEPOSIT_ADDRESS: [utxo(amount="not-a-number")]})

    assert actions.record == []
    assert any("unreadable" in reason for _, reason in actions.ignored)


# --------------------------------------------------------------------------
# Reorgs
# --------------------------------------------------------------------------


def test_a_deposit_that_vanishes_before_minting_is_abandoned():
    known = {f"{'aa' * 32}:0": State.PENDING}
    actions = run({DEPOSIT_ADDRESS: []}, known)

    assert actions.abandon == [(f"{'aa' * 32}:0", "no longer in the active chain")]
    assert actions.alerts == []


def test_a_deposit_that_vanishes_after_minting_raises_an_alert_not_an_abandon():
    """The dangerous case. The wrapped tokens exist; the deposit backing them
    does not. No code should quietly resolve that - it needs a human."""
    key = f"{'aa' * 32}:0"
    actions = run({DEPOSIT_ADDRESS: []}, {key: State.DONE})

    assert actions.abandon == [], "must not abandon a deposit already minted"
    assert len(actions.alerts) == 1
    assert "unbacked" in actions.alerts[0]


def test_an_already_abandoned_deposit_is_not_abandoned_again():
    key = f"{'aa' * 32}:0"
    actions = run({DEPOSIT_ADDRESS: []}, {key: State.ABANDONED})

    assert actions.abandon == []
    assert actions.alerts == []


def test_a_still_present_deposit_is_never_abandoned():
    known = {f"{'aa' * 32}:0": State.READY}
    actions = run({DEPOSIT_ADDRESS: [utxo()]}, known)

    assert actions.abandon == []


# --------------------------------------------------------------------------
# Applying decisions to a real ledger
# --------------------------------------------------------------------------


@pytest.fixture
def ledger():
    book = BridgeLedger(confirmations=20)
    book.assign_destination(DEPOSIT_ADDRESS, SOLANA)
    yield book
    book.close()


def offline_watcher(ledger):
    """A real watcher that is simply never asked to poll.

    The constructor does no I/O, so apply() - the part that writes to the
    ledger - is exercised exactly as it runs in production, without inventing a
    stand-in that could drift from it.
    """
    from bridge.watcher import ObsidionWatcher

    return ObsidionWatcher(ledger, rpc_port=0, token="")


def test_a_deposit_becomes_ready_only_once_it_is_deep_enough(ledger):
    watcher = offline_watcher(ledger)

    actions = run({DEPOSIT_ADDRESS: [utxo(height=100)]}, tip=100)
    watcher.apply(actions, tip_height=100)
    assert ledger.actionable("deposit") == [], "credited at one confirmation"

    # Still shallow at 19 confirmations.
    watcher.apply(Actions(), tip_height=118)
    assert ledger.actionable("deposit") == []

    # Deep enough at 20.
    watcher.apply(Actions(), tip_height=119)
    assert len(ledger.actionable("deposit")) == 1


def test_polling_twice_over_the_same_chain_state_changes_nothing(ledger):
    """Idempotence against restarts, which is the ordinary case."""
    watcher = offline_watcher(ledger)
    seen = {DEPOSIT_ADDRESS: [utxo(height=100)]}

    for _ in range(3):
        known = {
            key: State(state)
            for key, state in ledger.db.execute(
                "SELECT key, state FROM events WHERE kind = 'deposit'"
            ).fetchall()
        }
        watcher.apply(run(seen, known, tip=200), tip_height=200)

    assert len(ledger.actionable("deposit")) == 1
    assert ledger.wrapped_supply() == 0, "nothing is minted until Solana says so"


def test_applying_an_abandon_marks_the_ledger(ledger):
    watcher = offline_watcher(ledger)
    watcher.apply(run({DEPOSIT_ADDRESS: [utxo(height=100)]}, tip=200), tip_height=200)
    key = f"{'aa' * 32}:0"
    assert ledger.state_of(key) is State.READY

    watcher.apply(run({DEPOSIT_ADDRESS: []}, {key: State.READY}, tip=201), 201)
    assert ledger.state_of(key) is State.ABANDONED
    assert ledger.actionable("deposit") == []
