# Launch announcement — draft

Fill in the four bracketed values and post. Written plainly on purpose: the
audience for a new proof-of-work coin has read a thousand posts promising
revolution, and the ones they take seriously are the ones that sound like
software instead of a pitch.

**Post it at least 24 hours before mining starts**, so anyone interested can
install and be ready at block one. That waiting period *is* the fair launch.

---

## Obsidion (OBSD) — a CPU-mineable proof-of-work coin, launching [DATE] [TIME] UTC

No premine. No presale. No dev fund. No ICO. Every OBSD that will ever exist
has to be mined, starting at the time above, and I mine on the same terms as
everyone else.

*Obsidi**o**n, with an "o" — named for the volcanic glass. Not the note-taking
app, and not the unrelated coin trading as ODN. Getting that out of the way
first, because everyone asks.*

**What it is.** A Bitcoin-style chain built from scratch in Python — UTXO
ledger, secp256k1 signatures, proof-of-work mining, peer-to-peer gossip. Not a
token on someone else's chain, and not a fork with the constants changed. It
is about 8,500 lines you can read in an evening.

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
python -m venv .venv && .venv/bin/pip install ecdsa flask
.venv/bin/python -m obsidion.node --wallet my.wallet --create-wallet --mine
```

Python 3.11+. It finds the network by itself. There is a block explorer at
`python -m explorer.app` if you want to watch.

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
Explorer: [EXPLORER URL]
Tests run on every commit across Linux, macOS and Windows.

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
- *How do I know you didn't mine early?* — The announcement timestamp predates
  block one, and every block's timestamp is public. Check.
- *What makes it worth anything?* — Nothing yet, and say so. A coin worth
  something is a coin people chose to use.
