"""Tests for the bridge ledger — every one of them a way to lose money.

A lock-and-mint bridge fails by doubling: minting twice for one deposit, or
releasing twice for one burn. Either breaks the only invariant that matters,

    wOBSD in circulation == OBSD held in custody

and both happen through ordinary events — a crash and restart, a timed-out RPC
that actually succeeded, a reorg under a credited deposit. So these tests
rehearse those exact situations rather than the happy path.
"""

from __future__ import annotations

import pytest

from bridge.ledger import Burn, BridgeLedger, Deposit, State

SOLANA = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
OBSD = "obsd1ayca5khxahcqsqvmj97un3racxeegj4hsuvktd"
COIN = 100_000_000


@pytest.fixture
def ledger():
    book = BridgeLedger(confirmations=20)
    yield book
    book.close()


def a_deposit(txid="aa" * 32, index=0, amount=50 * COIN, height=100) -> Deposit:
    return Deposit(
        txid=txid,
        index=index,
        amount=amount,
        deposit_address=OBSD,
        solana_address=SOLANA,
        height=height,
    )


# --------------------------------------------------------------------------
# Idempotency: the entire safety story
# --------------------------------------------------------------------------


def test_seeing_the_same_deposit_again_changes_nothing(ledger):
    """The normal case after a crash, not an exotic one."""
    deposit = a_deposit()
    assert ledger.record_deposit(deposit) is State.PENDING

    ledger.promote_confirmed(tip_height=200)
    ledger.mark_done(deposit.key, receipt="solana-mint-sig")

    # The watcher restarts and re-scans the chain from further back.
    assert ledger.record_deposit(deposit) is State.DONE
    assert ledger.wrapped_supply() == 50 * COIN, "re-seeing it minted twice"


def test_marking_done_twice_does_not_double_the_supply(ledger):
    """A retried call after a response was lost must be harmless."""
    deposit = a_deposit()
    ledger.record_deposit(deposit)
    ledger.promote_confirmed(tip_height=200)

    ledger.mark_done(deposit.key, receipt="sig-1")
    ledger.mark_done(deposit.key, receipt="sig-2-retry")

    assert ledger.wrapped_supply() == 50 * COIN


def test_a_done_event_is_never_offered_for_action_again(ledger):
    deposit = a_deposit()
    ledger.record_deposit(deposit)
    ledger.promote_confirmed(tip_height=200)
    assert len(ledger.actionable("deposit")) == 1

    ledger.mark_done(deposit.key, receipt="sig")
    assert ledger.actionable("deposit") == []


def test_the_same_outpoint_arriving_with_a_different_amount_is_refused(ledger):
    """An outpoint's amount is fixed by the chain. If it changes, something
    upstream is confused, and acting would mint the wrong quantity."""
    ledger.record_deposit(a_deposit(amount=50 * COIN))

    with pytest.raises(ValueError, match="refusing to guess"):
        ledger.record_deposit(a_deposit(amount=500 * COIN))


def test_two_outputs_of_one_transaction_are_separate_deposits(ledger):
    """Keyed on the outpoint, not the txid: one transaction can pay a deposit
    address twice, and both are real."""
    ledger.record_deposit(a_deposit(index=0, amount=10 * COIN))
    ledger.record_deposit(a_deposit(index=1, amount=25 * COIN))

    ledger.promote_confirmed(tip_height=200)
    for key, _, _ in ledger.actionable("deposit"):
        ledger.mark_done(key, receipt="sig-" + key)

    assert ledger.wrapped_supply() == 35 * COIN


# --------------------------------------------------------------------------
# Confirmation depth: minting against a block that can still disappear
# --------------------------------------------------------------------------


def test_a_shallow_deposit_is_not_actionable(ledger):
    """Obsidion resolves forks by most work, so a recent block can vanish.
    Minting against one creates wOBSD with nothing behind it."""
    ledger.record_deposit(a_deposit(height=100))

    ledger.promote_confirmed(tip_height=105)  # only 6 deep, needs 20
    assert ledger.actionable("deposit") == []


def test_a_deposit_becomes_actionable_at_exactly_the_required_depth(ledger):
    deposit = a_deposit(height=100)
    ledger.record_deposit(deposit)

    ledger.promote_confirmed(tip_height=118)  # 19 confirmations
    assert ledger.actionable("deposit") == []

    ledger.promote_confirmed(tip_height=119)  # 20 confirmations
    assert [key for key, _, _ in ledger.actionable("deposit")] == [deposit.key]


def test_depth_is_measured_against_the_tip_not_trusted_from_the_caller(ledger):
    """A stale tip must never promote something; it can only fail to."""
    ledger.record_deposit(a_deposit(height=100))
    ledger.promote_confirmed(tip_height=50)  # nonsense, behind the deposit
    assert ledger.actionable("deposit") == []


# --------------------------------------------------------------------------
# Reorgs
# --------------------------------------------------------------------------


def test_a_deposit_reorged_away_before_minting_is_abandoned_cleanly(ledger):
    deposit = a_deposit(height=100)
    ledger.record_deposit(deposit)

    ledger.abandon(deposit.key, reason="no longer in the active chain")

    assert ledger.state_of(deposit.key) is State.ABANDONED
    assert ledger.actionable("deposit") == []
    assert ledger.wrapped_supply() == 0


def test_a_deposit_already_minted_cannot_be_abandoned(ledger):
    """This is the case that needs a human. The wrapped tokens exist; quietly
    forgetting the deposit would leave them unbacked and the invariant broken
    with no record of why."""
    deposit = a_deposit()
    ledger.record_deposit(deposit)
    ledger.promote_confirmed(tip_height=200)
    ledger.mark_done(deposit.key, receipt="sig")

    with pytest.raises(ValueError, match="nothing behind them"):
        ledger.abandon(deposit.key, reason="deep reorg")

    assert ledger.state_of(deposit.key) is State.DONE


def test_an_abandoned_deposit_that_reappears_stays_abandoned(ledger):
    """Re-seeing it must not silently resurrect it into the actionable queue."""
    deposit = a_deposit()
    ledger.record_deposit(deposit)
    ledger.abandon(deposit.key, reason="reorg")

    assert ledger.record_deposit(deposit) is State.ABANDONED
    ledger.promote_confirmed(tip_height=200)
    assert ledger.actionable("deposit") == []


# --------------------------------------------------------------------------
# Burns and the custody balance
# --------------------------------------------------------------------------


def test_a_burn_releases_once_and_only_once(ledger):
    burn = Burn(signature="5x" * 20, amount=10 * COIN, obsidion_address=OBSD)
    assert ledger.record_burn(burn) is State.PENDING

    ledger.mark_ready(burn.key)
    ledger.mark_done(burn.key, receipt="obsidion-txid")

    # Re-seen after a restart.
    assert ledger.record_burn(burn) is State.DONE
    assert len(ledger.actionable("burn")) == 0


def test_wrapped_supply_tracks_mints_less_releases(ledger):
    """The number that must equal both the Solana mint supply and the custody
    balance. If the three ever disagree, stop."""
    first = a_deposit(txid="11" * 32, amount=50 * COIN)
    second = a_deposit(txid="22" * 32, amount=30 * COIN)
    ledger.record_deposit(first)
    ledger.record_deposit(second)
    ledger.promote_confirmed(tip_height=200)
    ledger.mark_done(first.key, receipt="s1")
    ledger.mark_done(second.key, receipt="s2")
    assert ledger.wrapped_supply() == 80 * COIN

    burn = Burn(signature="bb" * 20, amount=30 * COIN, obsidion_address=OBSD)
    ledger.record_burn(burn)
    ledger.mark_ready(burn.key)
    ledger.mark_done(burn.key, receipt="released-txid")

    assert ledger.wrapped_supply() == 50 * COIN


def test_pending_work_does_not_count_toward_supply(ledger):
    """Only completed actions move the invariant; anything in flight must not."""
    ledger.record_deposit(a_deposit())
    ledger.promote_confirmed(tip_height=200)
    assert ledger.wrapped_supply() == 0, "counted before the mint happened"


# --------------------------------------------------------------------------
# Guards against acting on the wrong thing
# --------------------------------------------------------------------------


def test_marking_done_without_a_receipt_is_refused(ledger):
    """A DONE row with no proof is indistinguishable from a lie, and it is the
    row a future operator will rely on when reconciling."""
    deposit = a_deposit()
    ledger.record_deposit(deposit)
    ledger.promote_confirmed(tip_height=200)

    with pytest.raises(ValueError, match="receipt"):
        ledger.mark_done(deposit.key, receipt="")


def test_a_deposit_cannot_skip_the_confirmation_wait(ledger):
    """mark_done only accepts events that are READY, so nothing can be minted
    straight out of PENDING."""
    deposit = a_deposit(height=100)
    ledger.record_deposit(deposit)

    with pytest.raises(ValueError, match="pending"):
        ledger.mark_done(deposit.key, receipt="sig")


def test_an_unknown_event_cannot_be_completed(ledger):
    with pytest.raises(KeyError):
        ledger.mark_done("never-seen:0", receipt="sig")


# --------------------------------------------------------------------------
# Deposit addresses. Obsidion has no memo field, so the address IS the routing.
# --------------------------------------------------------------------------


def test_a_deposit_address_maps_to_its_destination(ledger):
    ledger.assign_destination(OBSD, SOLANA)
    assert ledger.destination_for(OBSD) == SOLANA


def test_an_assigned_address_cannot_be_repointed(ledger):
    """Addresses are public and can receive at any time. Repointing one would
    send a previous user's later deposit to a stranger."""
    ledger.assign_destination(OBSD, SOLANA)

    with pytest.raises(ValueError, match="already assigned"):
        ledger.assign_destination(OBSD, "SomeOtherSolanaWallet1111111111111111111111")

    assert ledger.destination_for(OBSD) == SOLANA


def test_reassigning_the_same_pair_is_harmless(ledger):
    """Retries must not fail; only genuine conflicts should."""
    ledger.assign_destination(OBSD, SOLANA)
    ledger.assign_destination(OBSD, SOLANA)
    assert ledger.destination_for(OBSD) == SOLANA


def test_an_unknown_deposit_address_has_no_destination(ledger):
    assert ledger.destination_for("obsd1nobodyassignedthis") is None


# --------------------------------------------------------------------------
# Durability
# --------------------------------------------------------------------------


def test_the_record_survives_a_restart(tmp_path):
    """A bridge that forgets what it minted will mint it again."""
    path = tmp_path / "bridge.db"
    deposit = a_deposit()

    book = BridgeLedger(path, confirmations=20)
    book.assign_destination(OBSD, SOLANA)
    book.record_deposit(deposit)
    book.promote_confirmed(tip_height=200)
    book.mark_done(deposit.key, receipt="sig")
    book.close()

    reopened = BridgeLedger(path, confirmations=20)
    try:
        assert reopened.state_of(deposit.key) is State.DONE
        assert reopened.wrapped_supply() == 50 * COIN
        assert reopened.destination_for(OBSD) == SOLANA
        # And it will not mint it a second time.
        assert reopened.record_deposit(deposit) is State.DONE
        assert reopened.actionable("deposit") == []
    finally:
        reopened.close()
