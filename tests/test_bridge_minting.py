"""Tests for the mint service — the first part of the bridge that can lose money.

A mint spans two systems that cannot be made atomic, so almost every test here
is a crash at a specific instant: after the intent is written, after the mint
lands, between the two. The fake minter can be told exactly when to die, which
is the only way to check that the recovery story actually holds.
"""

from __future__ import annotations

import pytest

from bridge.ledger import BridgeLedger, Deposit, State
from bridge.minting import MintService, memo_for

SOLANA = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
DEPOSIT_ADDRESS = "obsd1ayca5khxahcqsqvmj97un3racxeegj4hsuvktd"
COIN = 100_000_000


class FakeMinter:
    """A Solana stand-in that records mints and can be made to fail on cue."""

    def __init__(self):
        self.by_memo: dict[str, str] = {}
        self.calls: list[tuple[str, int, str]] = []
        #: Raise on the next mint, *after* pretending it reached the cluster.
        self.crash_after_landing = False
        #: Raise on the next mint without it landing at all.
        self.crash_before_landing = False
        #: Make find_mint raise, i.e. "I cannot tell".
        self.lookup_broken = False

    def mint(self, destination: str, amount: int, memo: str) -> str:
        self.calls.append((destination, amount, memo))
        if self.crash_before_landing:
            self.crash_before_landing = False
            raise RuntimeError("connection reset before the transaction was sent")

        signature = f"sig-{len(self.by_memo) + 1}"
        self.by_memo[memo] = signature

        if self.crash_after_landing:
            # The worst case: it really did mint, and then we lost the answer.
            self.crash_after_landing = False
            raise RuntimeError("timed out waiting for confirmation")
        return signature

    def find_mint(self, memo: str) -> str | None:
        if self.lookup_broken:
            raise RuntimeError("rpc unavailable")
        return self.by_memo.get(memo)


@pytest.fixture
def ledger():
    book = BridgeLedger(confirmations=1)
    book.assign_destination(DEPOSIT_ADDRESS, SOLANA)
    yield book
    book.close()


def ready_deposit(ledger, txid="aa" * 32, index=0, amount=50 * COIN) -> str:
    deposit = Deposit(
        txid=txid,
        index=index,
        amount=amount,
        deposit_address=DEPOSIT_ADDRESS,
        solana_address=SOLANA,
        height=10,
    )
    ledger.record_deposit(deposit)
    ledger.promote_confirmed(tip_height=100)
    return deposit.key


# --------------------------------------------------------------------------
# The happy path, and that it stays happy when repeated
# --------------------------------------------------------------------------


def test_a_confirmed_deposit_is_minted_to_its_solana_wallet(ledger):
    key = ready_deposit(ledger)
    minter = FakeMinter()

    report = MintService(ledger, minter).run_once()

    assert [k for k, _ in report.minted] == [key]
    assert minter.calls == [(SOLANA, 50 * COIN, memo_for(key))]
    assert ledger.state_of(key) is State.DONE
    assert ledger.wrapped_supply() == 50 * COIN


def test_running_again_mints_nothing_new(ledger):
    ready_deposit(ledger)
    service = MintService(ledger, FakeMinter())
    service.run_once()

    second = service.run_once()

    assert second.minted == []
    assert ledger.wrapped_supply() == 50 * COIN


def test_an_unconfirmed_deposit_is_not_minted(ledger):
    """Depth is the ledger's job, but the service must not reach past it."""
    deposit = Deposit(
        txid="bb" * 32,
        index=0,
        amount=50 * COIN,
        deposit_address=DEPOSIT_ADDRESS,
        solana_address=SOLANA,
        height=100,
    )
    ledger.record_deposit(deposit)  # never promoted

    report = MintService(ledger, FakeMinter()).run_once()

    assert report.minted == []
    assert ledger.state_of(deposit.key) is State.PENDING


# --------------------------------------------------------------------------
# Crashes. Each one is a real way to invent or strand money.
# --------------------------------------------------------------------------


def test_a_crash_after_the_mint_lands_is_recovered_not_repeated(ledger):
    """The expensive case: the tokens exist, but we never recorded it.

    Without the memo this is unrecoverable — the next run cannot tell a landed
    mint from one that never happened, and minting again doubles the supply.
    """
    key = ready_deposit(ledger)
    minter = FakeMinter()
    minter.crash_after_landing = True

    first = MintService(ledger, minter).run_once()
    assert first.failed, "the crash should be reported"
    assert ledger.state_of(key) is State.IN_FLIGHT
    assert len(minter.by_memo) == 1, "it really did mint"

    # Restart.
    second = MintService(ledger, minter).run_once()

    assert [k for k, _ in second.recovered] == [key]
    assert ledger.state_of(key) is State.DONE
    assert len(minter.by_memo) == 1, "recovery minted a second time"
    assert ledger.wrapped_supply() == 50 * COIN


def test_a_crash_before_the_mint_lands_is_retried(ledger):
    """The cheap case, but it must not strand the deposit forever."""
    key = ready_deposit(ledger)
    minter = FakeMinter()
    minter.crash_before_landing = True

    first = MintService(ledger, minter).run_once()
    assert first.minted == []
    assert ledger.state_of(key) is State.IN_FLIGHT
    assert minter.by_memo == {}, "nothing should have landed"

    second = MintService(ledger, minter).run_once()

    assert [k for k, _ in second.minted] == [key]
    assert ledger.state_of(key) is State.DONE
    assert ledger.wrapped_supply() == 50 * COIN


def test_the_intent_is_written_before_the_mint_is_attempted(ledger):
    """If the process died inside minter.mint(), the ledger must already show
    IN_FLIGHT — otherwise the restart has no idea to go looking."""
    key = ready_deposit(ledger)
    observed = {}

    class Observing(FakeMinter):
        def mint(self, destination, amount, memo):
            observed["state"] = ledger.state_of(key)
            return super().mint(destination, amount, memo)

    MintService(ledger, Observing()).run_once()
    assert observed["state"] is State.IN_FLIGHT


def test_a_failed_lookup_blocks_minting_rather_than_guessing(ledger):
    """"I cannot tell" is not "no". Treating it as no would mint twice."""
    key = ready_deposit(ledger)
    minter = FakeMinter()
    minter.crash_after_landing = True
    MintService(ledger, minter).run_once()  # leaves it in flight, already minted

    minter.lookup_broken = True
    report = MintService(ledger, minter).run_once()

    assert report.blocked
    assert report.unresolved == [key]
    assert ledger.state_of(key) is State.IN_FLIGHT
    assert len(minter.by_memo) == 1, "it minted again while unsure"


def test_unresolved_work_stops_unrelated_deposits_being_minted(ledger):
    """A bridge that keeps minting while one outcome is unknown will
    eventually double that one. Everything waits."""
    stuck = ready_deposit(ledger, txid="aa" * 32)
    minter = FakeMinter()
    minter.crash_after_landing = True
    MintService(ledger, minter).run_once()

    fresh = ready_deposit(ledger, txid="cc" * 32, amount=7 * COIN)
    minter.lookup_broken = True
    report = MintService(ledger, minter).run_once()

    assert report.blocked
    assert report.minted == []
    assert ledger.state_of(fresh) is State.READY, "minted while blocked"
    assert ledger.state_of(stuck) is State.IN_FLIGHT


def test_a_minter_returning_no_signature_is_treated_as_a_failure(ledger):
    """A DONE row without a receipt is indistinguishable from a lie."""
    key = ready_deposit(ledger)

    class Silent(FakeMinter):
        def mint(self, destination, amount, memo):
            return ""

    report = MintService(ledger, Silent()).run_once()

    assert report.minted == []
    assert report.failed
    assert ledger.state_of(key) is State.IN_FLIGHT


# --------------------------------------------------------------------------
# The memo, which is the whole recovery story
# --------------------------------------------------------------------------


def test_the_memo_is_deterministic_and_identifies_the_deposit():
    """Recovery works only because the same deposit always produces the same
    memo — and one recognisable among unrelated Solana memos."""
    assert memo_for("abc:0") == memo_for("abc:0")
    assert memo_for("abc:0") != memo_for("abc:1")
    assert "abc:0" in memo_for("abc:0")
    assert memo_for("abc:0").startswith("obsd-bridge:")


def test_each_deposit_is_minted_under_its_own_memo(ledger):
    first = ready_deposit(ledger, txid="11" * 32, amount=10 * COIN)
    second = ready_deposit(ledger, txid="22" * 32, amount=20 * COIN)
    minter = FakeMinter()

    MintService(ledger, minter).run_once()

    assert set(minter.by_memo) == {memo_for(first), memo_for(second)}
    assert ledger.wrapped_supply() == 30 * COIN


# --------------------------------------------------------------------------
# The ledger's own guards around the new state
# --------------------------------------------------------------------------


def test_an_in_flight_deposit_cannot_be_abandoned(ledger):
    """It may already have been minted. Abandoning it would be a guess, and one
    of the two possible guesses invents unbacked tokens."""
    key = ready_deposit(ledger)
    minter = FakeMinter()
    minter.crash_after_landing = True
    MintService(ledger, minter).run_once()

    with pytest.raises(ValueError, match="in flight"):
        ledger.abandon(key, "reorg")


def test_in_flight_work_is_visible_for_a_human_to_inspect(ledger):
    key = ready_deposit(ledger)
    minter = FakeMinter()
    minter.crash_before_landing = True
    MintService(ledger, minter).run_once()

    outstanding = ledger.in_flight("deposit")
    assert [k for k, _, _ in outstanding] == [key]
    assert outstanding[0][2]["solana_address"] == SOLANA
