# Launch announcement

Written plainly on purpose: the audience for a new proof-of-work coin has read
a thousand posts promising revolution, and the ones they take seriously are
the ones that sound like software instead of a pitch.

**What makes a launch fair is not the size of the head start — it is that the
head start is disclosed and verifiable, not hidden.** The ideal is to announce
first and wait 24 hours so everyone can be ready at block one. This launch did
not do that: mining began before this post, and rather than dress that up, the
exact facts are below and every one of them is checkable on the chain itself.
What ruins a launch is a claim the block timestamps contradict. So here are the
timestamps.

---

## Obsidion (OBSD) — a CPU-mineable proof-of-work coin, live since 2026-08-09 03:35 UTC

No premine. No presale. No dev fund. No ICO. Every OBSD that will ever exist
has to be mined, and I mine on the same terms as everyone else — the only
difference is the minutes between block one and this post, quantified below.

**Full disclosure on timing.** Mining began at **2026-08-09 03:35:32 UTC** —
block 1 is `a470dffe41a4c3ac9101ee695205aac1e899f4b14fbbf6da565b52e8b5438873`,
built on the genesis block published days earlier. This announcement goes up in
the minutes after that, not before, and I am not going to pretend otherwise.
There is no premine: the genesis reward of 50 OBSD is locked to an unspendable
hash of twenty zero bytes and can never move; every other coin was mined block
by block starting at the time above. Verify it yourself — sync from the seed
and read block 1's timestamp. If it predates 03:35:32 UTC on 2026-08-09, say so
loudly.

The head start this represents is small and self-limiting. Difficulty starts at
the network floor and retargets every 120 blocks, clamped 4× per period, so a
lone CPU cannot run away with it: by the time you read this the chain will be a
few dozen blocks in, a fraction of a percent of the 21 million cap. Don't take
my word for the number — `getinfo` reports the current height and circulating
supply, and you can watch it against the cap in the explorer. I would rather
hand you the arithmetic than have you find it.

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
Tests: 391, run on every commit across Linux, macOS and Windows.

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
- *How do I know how much you mined before announcing?* — Read it off the
  chain. Block 1 is timestamped 2026-08-09 03:35:32 UTC; this post went up
  minutes later; `getinfo` shows the height and circulating supply right now.
  The gap is disclosed, small, and self-limiting under the difficulty clamp —
  and none of it depends on trusting me, which is the whole point of a chain.
- *You mined before announcing.* — Yes, by a matter of minutes, and it says so
  up front rather than being buried. A concealed head start is a stealth
  premine; a disclosed and verifiable one is just the first blocks. Judge it on
  the timestamps, which are permanent.
- *Only one seed node?* — Yes, today, and it is the network's weakest point:
  if it goes offline, existing nodes carry on but newcomers cannot find their
  way in. More seeds are the most useful thing anyone can contribute. Seeds
  are published as hostnames rather than addresses, so adding or moving one
  never requires anybody to upgrade.
- *What makes it worth anything?* — Nothing yet, and say so. A coin worth
  something is a coin people chose to use.
