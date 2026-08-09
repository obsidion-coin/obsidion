# Launch announcement

Written plainly on purpose: the audience for a new proof-of-work coin has read
a thousand posts promising revolution, and the ones they take seriously are
the ones that sound like software instead of a pitch.

**Post this before mining starts, and state the gap honestly.** The waiting
period is what makes the launch fair — 24 hours is the ideal, because it lets
people in every timezone install and be ready at block one. This launch ran a
shorter window, and says so below rather than implying otherwise. What ruins a
launch is not a short window; it is a stated window that the block timestamps
contradict. Those timestamps are permanent and public, and someone always
checks.

---

## Obsidion (OBSD) — a CPU-mineable proof-of-work coin, launching 2026-08-09 03:00 UTC

No premine. No presale. No dev fund. No ICO. Every OBSD that will ever exist
has to be mined, starting at the time above, and I mine on the same terms as
everyone else.

**Full disclosure on timing.** This is going up roughly two hours before
mining starts, not the 24 hours I would have preferred. The chain is at height
0 as I post this — nothing has been mined, and the genesis block's 50 OBSD is
locked to an unspendable hash of twenty zero bytes. Verify both claims
yourself: sync from the seed and check the height, and check block one's
timestamp against this post. If you find a block dated before 03:00 UTC on
2026-08-09, I lied and you should say so loudly.

For what it is worth, a head start is worth much less here than it looks.
Difficulty retargets every 120 blocks and clamps at 4x, so a single CPU
mining alone throttles itself fast: two hours of solo mining is about 164
blocks, roughly 0.04% of the total supply. That is the arithmetic, not an
excuse — the window is short and I would rather you hear it from me.

*Obsidi**o**n, with an "o" — named for the volcanic glass. Not the note-taking
app, and not the unrelated coin trading as ODN. Getting that out of the way
first, because everyone asks.*

**What it is.** A proof-of-work chain built from scratch in Python — UTXO
ledger, secp256k1 signatures, peer-to-peer gossip, the lot. Not a token on
someone else's chain, and not a fork of an existing one with the constants
changed. It is about 8,500 lines you can read in an evening.

**Why scrypt.** SHA-256 coins belong to whoever buys ASICs; a single
second-hand unit out-hashes every CPU that would ever run a small chain, by a
factor near 10⁸. Obsidion uses scrypt with a 2 MB working set, so every hashing
core needs two megabytes of its own memory — the one thing custom silicon
cannot conjure. An ordinary laptop core does ~166 H/s and genuinely competes.

**The numbers**

| | |
|---|---|
| Ticker | OBSD |
| Max supply | 20,999,999.9769 |
| Block time | 2.5 minutes |
| Block reward | 50 OBSD |
| Halving | every 210,000 blocks (~1 year) |
| Proof of work | scrypt, N=2¹², r=4 (2 MB) |
| Difficulty retarget | every 120 blocks, clamped 4× |
| Smallest unit | 1 shard = 10⁻⁸ OBSD |
| Genesis | `69e33674de2c169233dbbdca69dcd1ede122207cd7ead83c5564e08172862a7a` |

The cap is not a promise in a whitepaper — it is the only function in the
codebase that creates coins, and every node rejects a block that claims more.

**Run it**

```
git clone https://github.com/obsidion-coin/obsidion
cd obsidion
python -m venv .venv
.venv/bin/pip install ecdsa flask          # Windows: .venv\Scripts\pip
.venv/bin/python -m obsidion.node --wallet my.wallet --create-wallet --mine
```

Python 3.11+, no compiler and no dependencies beyond those two. It finds the
network by itself through a seed node baked into the code — you do not need to
pass `--connect` or know anyone's address.

Mining from behind a home router needs no port forwarding and no inbound
connectivity: add `--host 127.0.0.1` and your node makes outbound connections
only, syncing, validating, mining and broadcasting like any other.

There is a block explorer — `python -m explorer.app`, then open
<http://127.0.0.1:8080> — showing blocks as they arrive, circulating supply
against the cap, and the countdown to the first halving. It runs locally
against your own node; there is no hosted one yet.

**What I will tell you that most launches will not**

- A new proof-of-work chain with little hashpower can be 51%-attacked. That is
  true here and it is true of every chain on its first day. It gets better only
  if people actually mine it.
- Nobody has professionally audited this. An adversarial review I ran found two
  ways to counterfeit coins in code that was passing 306 tests — both fixed,
  both with regression tests, and a third may be waiting. The full list of
  known weaknesses is in the README, not buried.
- I am not selling anything and I am not telling you it will be worth money.
  Scarcity is not value. If you mine it, mine it because building a currency
  from nothing is an interesting thing to be part of.

Code: https://github.com/obsidion-coin/obsidion
Tests: 360, run on every commit across Linux, macOS and Windows.

---

### Where to post

- **Bitcointalk → Alternate cryptocurrencies → Announcements (ALT)** — still
  where new-coin launches are expected; readers there will check your claims
- **r/gpumining, r/CryptoCurrency** — read each subreddit's self-promotion
  rules first, several ban launch posts outright
- **Hacker News** — lead with the engineering, not the coin. "I built a
  cryptocurrency from scratch in Python to understand how they work" is the
  honest framing and the one that gets read

### Answer these before you post

People will ask, and having no answer reads as evasion:

- *Why not just fork Litecoin?* — Because forking teaches you nothing and
  inherits code you cannot vouch for.
- *What's your premine?* — Nothing. Point at the genesis block: its 50 OBSD is
  locked to an unspendable hash of twenty zero bytes.
- *How do I know you didn't mine early?* — This post predates block one, and
  every block timestamp is public. Check block one against the time on this
  post. Do not take my word for it; the whole point of a chain is that you
  do not have to.
- *Two hours is not 24 hours.* — Correct, and it is stated up front rather
  than glossed. Judge the gap on the evidence: height 0 at posting time, and
  block one's timestamp afterwards.
- *Only one seed node?* — Yes, today, and it is the network's weakest point:
  if it goes offline, existing nodes carry on but newcomers cannot find their
  way in. More seeds are the most useful thing anyone can contribute. Seeds
  are published as hostnames rather than addresses, so adding or moving one
  never requires anybody to upgrade.
- *What makes it worth anything?* — Nothing yet, and say so. A coin worth
  something is a coin people chose to use.
