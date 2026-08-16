# Wrapped OBSD on Solana — design and honest risks

Obsidion is its own Layer-1. Solana DEXes, DEXScreener and the rest index
*tokens on Solana*, so OBSD cannot appear there as itself. The only faithful
way to be present on Solana is a **wrapped token that is fully backed**: one
wOBSD exists on Solana if and only if one OBSD is locked on the Obsidion chain.

The alternative — minting a token out of nothing and calling it OBSD — is
faster and is what most projects do. It is also a premine. Obsidion's launch
says *no premine, no presale, every coin has to be mined*, and that statement
is either true or it is not. This document describes the version that keeps it
true.

---

## Read this before building any of it

**A lock-and-mint bridge means holding other people's money.** Users send real
OBSD to an address you control and trust you to give it back. Three
consequences follow, and none of them are engineering problems:

1. **It is custody.** In many jurisdictions holding customer funds and moving
   value between systems looks like money transmission, with licensing
   attached. Giving away a mineable coin is close to the lowest-risk thing in
   crypto; running a bridge is not. This needs a lawyer, and no part of this
   document is legal advice.
2. **It is centralised.** Whoever holds the custody key can take everything,
   and whoever holds the mint key can print unbacked wOBSD. In v1 that is one
   person. Say so publicly, in the token description, not in a footnote.
3. **Bridges are the most attacked component in crypto.** Billions have been
   lost to them, because the custody key is the entire prize in one place. A
   bridge holding little is barely worth attacking; one holding a lot is worth
   a great deal of someone's time.

The honest posture is to launch it small, cap what it can hold, and say plainly
that it is a trusted bridge operated by one person.

---

## Why unique deposit addresses, and not a memo

The obvious design is: user sends OBSD with their Solana address attached, and
the bridge reads it off the transaction. **Obsidion cannot do this.** There is
exactly one output type — pay-to-public-key-hash — and no scripting language
(`obsidion/transaction.py`). Arbitrary bytes exist only in a *coinbase* input,
which ordinary users never construct. There is nowhere to put a memo.

Adding one would be a consensus change: a new output type, a hard fork, and a
network split for anyone who did not upgrade. For a feature that is not about
consensus at all, that trade is indefensible.

So instead, **each destination gets its own Obsidion address**:

1. A user says "I want wOBSD at Solana address `S`".
2. The bridge derives a fresh Obsidion address `A` (the wallet already does
   this — `getnewaddress`) and records `A -> S` in its own database.
3. The user sends OBSD to `A`.
4. The bridge sees the deposit confirm, looks up `S`, and mints that many
   wOBSD to it.

No consensus change, no fork, no new transaction format. The mapping is the
bridge's private bookkeeping, and the chain stays exactly as it is. This is how
exchanges have credited deposits for a decade.

## The two directions

**Wrapping (Obsidion → Solana).** Deposit confirms at `A` → mint the same
number of wOBSD to `S`. The OBSD stays locked in custody, unspent, forever
matching the wOBSD in circulation.

**Unwrapping (Solana → Obsidion).** User burns wOBSD, naming an Obsidion
address. The bridge sees the burn and sends that many OBSD from custody. The
burn destroys the token; the release returns the coin.

The invariant, checkable by anyone at any time:

> **wOBSD total supply on Solana == OBSD held in the custody address.**

Publish the custody address. Anyone can verify the backing themselves against
the Solana mint supply, which is the only thing that makes a trusted bridge
tolerable.

## Confirmation depth

A deposit must not be minted the moment it appears. Obsidion resolves forks by
most cumulative work, so a recent block can be reorganised away — and a mint
against a deposit that later vanishes is unbacked wOBSD, which breaks the one
invariant that matters.

Coinbase maturity is already 100 blocks for this reason. Ordinary deposits do
not need that much, but they need more than zero. **v1 waits 20 confirmations**
(~50 minutes at 2.5-minute blocks) before minting, and treats a deposit that
disappears from the chain before then as never having happened.

Releases in the other direction wait for Solana finality before sending OBSD.

## Idempotency is the whole safety story

Every failure mode of a bridge is a double: mint twice for one deposit, or
release twice for one burn. The service will crash, restart, and re-see events
it has already handled — that is normal, not exceptional.

So the ledger is an **append-only record keyed by the event's own identity** —
the deposit's `(txid, index)`, the burn's Solana signature. Handling an event
is: look it up; if it is already recorded as done, do nothing; otherwise act
and record. Never "check a balance and decide", which races with itself.

This core is pure and holds no keys, so it is fully testable without a chain,
without Solana, and without money. It is `bridge/ledger.py`, and it is built
first for exactly that reason.

## Staging

**Stage 1 — the ledger core.** Deposit/burn bookkeeping, confirmation gating,
idempotency, reorg handling. Pure Python, no keys, no funds, no network. Fully
tested. *This is what exists now.*

**Stage 2 — the Obsidion watcher.** Polls `getaddressutxos` for the deposit
addresses over RPC, feeds confirmed deposits into the ledger. Read-only against
the chain; still holds nothing.

**Stage 3 — the Solana side.** An SPL token whose mint authority the bridge
holds; mint on confirmed deposit. Needs a funded Solana keypair, so it needs
real money and is the first stage that can lose any.

**Stage 4 — unwrapping.** Watch for burns, release from custody. This is the
stage that spends user funds and deserves the most review.

**Stage 5 — liquidity.** A pool so wOBSD is tradeable, which is what makes it
visible on DEXScreener. Real capital, entirely the operator's decision.

Stages 1 and 2 are safe to build and useful on their own. Stage 3 onward
involves custody, keys and money, and none of it should be rushed to reach a
listing sooner.

## Deliberately out of scope for v1

- **Trustlessness.** A real trust-minimised bridge needs light-client proofs of
  Obsidion state verified inside a Solana program. That is a large project on
  its own and pretending otherwise is how bridges get drained.
- **Multisig or threshold custody.** Correct for a bridge holding meaningful
  value, and the obvious next step, but it is not v1.
- **Automatic listing anywhere.** Listings follow liquidity and volume; they
  are not a build step.
