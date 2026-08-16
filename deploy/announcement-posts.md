# Short launch posts — ready to paste

Two condensed versions of `announcement.md`, sized for where they go. Both keep
the honest timing disclosure: mining began **2026-08-09 03:35:32 UTC**, block 1
`a470dffe41a4c3ac9101ee695205aac1e899f4b14fbbf6da565b52e8b5438873`, and these
posts go up after that, not before. Both point readers at `getinfo` so the live
numbers are checked, not trusted.

---

## Bitcointalk — Alternate cryptocurrencies → Announcements (ALT)

**Subject:** `[ANN] Obsidion (OBSD) — CPU-mineable scrypt PoW, no premine, built from scratch in Python`

Obsidion is a proof-of-work cryptocurrency written from first principles in
Python — UTXO ledger, secp256k1 signatures, P2P gossip, the whole stack. Not a
fork, not a token on someone else's chain. ~8,500 lines you can read in an
evening.

**No premine. No presale. No dev fund. No ICO.** The genesis reward is locked
to a provably unspendable address; every other coin is mined on equal terms.

**Specs**
- Ticker: OBSD
- PoW: scrypt, 2 MB working set — memory-hard, CPU-friendly (~160 H/s on a laptop core)
- Block time: 2.5 minutes
- Reward: 50 OBSD, halving every 210,000 blocks (~1 year)
- Max supply: 20,999,999.9769
- Difficulty: retargets every 120 blocks, clamped 4×
- Addresses: bech32 (`obsd1…`)
- Genesis: `69e33674de2c169233dbbdca69dcd1ede122207cd7ead83c5564e08172862a7a`

**Fair-launch disclosure.** Mining went live 2026-08-09 03:35:32 UTC (block 1
`a470dffe41a4c3ac9101ee695205aac1e899f4b14fbbf6da565b52e8b5438873`), on a
genesis block published days earlier. This post goes up shortly after, not
before — the head start is disclosed, not hidden. Difficulty starts at the
network floor and self-throttles under the 4× clamp, so it is a fraction of a
percent of supply. Don't trust that: sync from the seed and read block 1's
timestamp and the current supply (`getinfo`) yourself.

**Run it** (Python 3.11+, no compiler, two dependencies):
```
git clone https://github.com/obsidion-coin/obsidion
cd obsidion
python -m venv .venv && .venv/bin/pip install ecdsa flask
.venv/bin/python -m obsidion.node --wallet my.wallet --create-wallet --mine
```
It finds the network through a built-in seed — no --connect needed. Behind a
home router, add `--host 127.0.0.1` to mine outbound-only (no port forwarding,
nothing exposed).

**Honest limitations** (also in the README, not buried):
- A young PoW chain with little hashpower is 51%-attackable. True here, true of
  every new chain.
- No professional audit yet. One adversarial review found and fixed two
  coin-counterfeiting bugs, both regression-tested; 450 tests run on CI across
  Linux/macOS/Windows.
- Pure-Python signature verification caps throughput to a few hundred tx/s per core.
- I am not selling anything and make no value claim. Scarcity is not value.

Code: https://github.com/obsidion-coin/obsidion

---

## Hacker News — Show HN

**Title:** `Show HN: Obsidion – a CPU-mineable cryptocurrency built from scratch in Python`

**URL:** `https://github.com/obsidion-coin/obsidion`

**Text:**

I wanted to understand how a cryptocurrency actually works, so I built one from
first principles in Python: UTXO ledger, secp256k1, Merkle trees, scrypt PoW,
P2P gossip with most-work fork choice, atomic reorgs, encrypted wallet, RPC,
block explorer. ~8,500 lines, 450 tests on CI.

Two things surprised me.

**Passing tests is not correctness.** An adversarial review of code already
passing 306 tests found two ways to counterfeit coins: a reorg that resurrected
an output spent within the same block, and a cache-poisoning fork via a
corrupted signature (txids exclude signatures to prevent malleability — so the
block hash doesn't commit to them either). Both fixed, both regression-tested.

**Memory is the thing silicon can't cheat.** scrypt with a 2 MB working set cuts
the ASIC advantage from ~10^8 to ~10, which is the difference between a chain
anyone can mine and one owned by whoever buys the first machine. A laptop core
does ~160 H/s and genuinely competes.

It's live as a real network — no premine, mining began 2026-08-09 03:35 UTC,
disclosed and checkable on-chain. But the code is the interesting part, not the
coin: a young PoW chain is 51%-attackable and this has had no professional
audit. Both are in the README rather than buried. Happy to go into any of the
design decisions.
