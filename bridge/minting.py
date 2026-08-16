"""Mint wrapped OBSD on Solana against confirmed deposits.

This is the first part of the bridge that can lose money, so almost all of it
is about the one problem that makes bridges hard: **a mint spans two systems
that cannot be made atomic.**

Write the ledger first and crash, and the deposit is marked minted with nothing
behind it. Mint first and crash, and a restart mints again — inventing wrapped
tokens no OBSD backs. Neither ordering is safe, and no amount of care with
try/finally fixes it, because the process can die between any two instructions.

The way out is not a better ordering but a **self-identifying mint**. Every mint
carries the deposit's own key (`txid:index`) as a Solana memo, so after a crash
the question "did I already mint for this deposit?" stops being a guess and
becomes a query against Solana. Recovery is then mechanical:

  * `IN_FLIGHT` and a matching mint exists → record it DONE with that signature.
  * `IN_FLIGHT` and no matching mint → nothing happened; safe to mint.

Unresolved in-flight work is settled *before* any new deposit is touched. A
bridge that starts minting fresh deposits while an old one is unaccounted for
is a bridge that will eventually double one.

The Solana side is behind `SolanaMinter`, which is deliberately two methods
wide. That keeps every rule here testable against a fake that can be made to
crash at will, and keeps the library that talks to a real cluster — along with
the key that can print tokens — out of the logic entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from bridge.ledger import BridgeLedger, State

log = logging.getLogger("bridge.minting")


class MintFailed(Exception):
    """The mint did not land. The deposit stays in flight, deliberately.

    Not an error to swallow: after a failure it is genuinely unknown whether
    the transaction reached the cluster, so the next run must ask rather than
    assume.
    """


class SolanaMinter(Protocol):
    """The two things the bridge needs from Solana, and nothing else."""

    def mint(self, destination: str, amount: int, memo: str) -> str:
        """Mint `amount` base units to `destination`, tagged with `memo`.

        Returns the transaction signature. The memo must be carried on-chain
        and be findable by `find_mint`, because that is the entire recovery
        story.
        """
        ...

    def find_mint(self, memo: str) -> str | None:
        """The signature of an existing mint carrying `memo`, if there is one.

        Answers "did I already do this?" after a crash. Returning None must
        mean *no such mint exists*, not *I could not tell* — a false negative
        here mints twice. Implementations that cannot be sure should raise.
        """
        ...


@dataclass
class MintReport:
    """What one pass did, for logs and for a human to read."""

    minted: list[tuple[str, str]] = field(default_factory=list)  # (key, signature)
    recovered: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (key, reason)
    #: Deposits still unresolved after a recovery attempt. While this is
    #: non-empty the service refuses to mint anything new.
    unresolved: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.unresolved)


class MintService:
    """Drives confirmed deposits to wrapped tokens, once each."""

    def __init__(self, ledger: BridgeLedger, minter: SolanaMinter):
        self.ledger = ledger
        self.minter = minter

    def run_once(self) -> MintReport:
        """Resolve anything in flight, then mint what is ready.

        Order matters: unresolved work is settled first, and if any remains the
        pass stops without minting. Continuing past an unknown outcome is how a
        bridge doubles a mint.
        """
        report = MintReport()
        self._resolve_in_flight(report)
        if report.blocked:
            log.error(
                "refusing to mint: %d deposit(s) unresolved on Solana - %s",
                len(report.unresolved),
                ", ".join(report.unresolved),
            )
            return report

        for key, amount, payload in self.ledger.actionable("deposit"):
            self._mint_one(key, amount, payload["solana_address"], report)
        return report

    def _resolve_in_flight(self, report: MintReport) -> None:
        for key, _amount, _payload in self.ledger.in_flight("deposit"):
            try:
                signature = self.minter.find_mint(memo_for(key))
            except Exception as exc:  # noqa: BLE001 — cannot tell is not "no"
                # A lookup that failed is not evidence of absence. Treating it
                # as absence would mint a second time.
                log.exception("could not determine whether %s was minted", key)
                report.unresolved.append(key)
                report.failed.append((key, f"lookup failed: {exc}"))
                continue

            if signature:
                self.ledger.mark_done(key, signature)
                report.recovered.append((key, signature))
                log.warning(
                    "recovered %s: it had already been minted as %s", key, signature
                )
            else:
                # Nothing landed, so the intent can be safely retried. Back to
                # READY rather than minted here, so one place does the minting.
                self.ledger.db.execute(
                    "UPDATE events SET state = ? WHERE key = ?",
                    (State.READY.value, key),
                )
                self.ledger.db.commit()
                log.info("%s was never minted; returning it to the queue", key)

    def _mint_one(
        self, key: str, amount: int, destination: str, report: MintReport
    ) -> None:
        # Intent first. A crash from here on is recoverable because the memo
        # ties any resulting mint back to this deposit.
        self.ledger.mark_in_flight(key)
        try:
            signature = self.minter.mint(destination, amount, memo_for(key))
        except Exception as exc:  # noqa: BLE001 — the boundary with the cluster
            # Deliberately left IN_FLIGHT. The transaction may still land, and
            # the next pass will ask Solana rather than guess.
            log.exception("mint failed for %s; left in flight", key)
            report.failed.append((key, str(exc)))
            return

        if not signature:
            report.failed.append((key, "minter returned no signature"))
            return

        self.ledger.mark_done(key, signature)
        report.minted.append((key, signature))
        log.info("minted %d base units to %s for %s: %s", amount, destination, key, signature)


def memo_for(key: str) -> str:
    """The on-chain tag that ties a mint to the deposit that caused it.

    Prefixed so it is recognisable among unrelated memos, and identical for the
    same deposit every time — that determinism is what makes recovery possible.
    """
    return f"obsd-bridge:{key}"
