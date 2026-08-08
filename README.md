# Obsidion (OBSD)

A peer-to-peer digital currency secured by CPU-friendly proof-of-work, built
from first principles in Python. Named for obsidian, the volcanic glass —
forged under pressure, holding the sharpest edge of any natural material.

**Obsidi*o*n, with an "o".** Not the note-taking app, not Obsidian
Entertainment, and not the unrelated coin trading as ODN. Different project,
different people, no connection to any of them.

Miners race to solve proof-of-work; whoever wins adds the next block and
collects the reward; the reward **halves on a fixed schedule**; and total
supply is **capped forever**. None of that is policy — it is enforced by every
node's validation of every block, which is what makes the scarcity provable
rather than promised.

| | |
|---|---|
| Ticker | **OBSD** |
| Smallest unit | 1 **shard** = 10⁻⁸ OBSD |
| Max supply | **20,999,999.9769 OBSD** (enforced by consensus, computed exactly) |
| Block time | 2.5 minutes |
| Initial reward | 50 OBSD |
| Halving | every 210,000 blocks (~1 year) — 64 eras, then zero forever |
| Proof of work | **scrypt, 2 MB working set** — memory-hard, CPU-friendly |
| Block identity | SHA-256d, kept cheap and separate from the mining hash |
| Difficulty | retargets every 120 blocks (~5 h), clamped to 4× per period |
| Ledger | UTXO model, pay-to-pubkey-hash, ECDSA/secp256k1 |
| Addresses | bech32: `obsd1q…` (mainnet), `tobsd1q…` (testnet) |
| Genesis (mainnet) | `69e33674de2c169233dbbdca69dcd1ede122207cd7ead83c5564e08172862a7a` |

Half of all OBSD that will ever exist mints in the first era. The cap lands at
20,999,999.9769 rather than a round 21 million because each halving is an
integer right-shift: after nine of them the reward stops dividing evenly, and
every subsequent era truncates a fraction of a shard that is never minted.

**Why scrypt and not SHA-256.** Obsidion is meant to be mined on computers
people already own. A SHA-256 ASIC is pure combinational logic and beats a CPU
by a factor of roughly a hundred million, so a chain using it is owned by
whoever buys one miner. scrypt at 2 MB cannot be won that way: every hashing
core needs its own two megabytes of real memory, and memory is the one thing
custom silicon cannot conjure. That narrows the gap to something like ten.

The cost is paid in verification — but only once per block, not once per hash.
A node checks ~53 minutes' worth of hashing per year of chain history on first
sync, which is why the block time is 2.5 minutes rather than one: at
60-second blocks that figure trebles, and the entire coin supply would also
mint out within about four years, leaving miners unpaid.

## Quickstart

Python 3.11+ required.

```
python -m venv .venv
.venv\Scripts\pip install ecdsa flask pytest        # (Linux/macOS: .venv/bin/pip)
```

**Try everything in five minutes** — a private regtest chain where blocks mine
instantly and halvings arrive every 10 blocks:

```
.venv\Scripts\python -m obsidion.node --network regtest ^
    --wallet my.wallet --create-wallet --mine
```

Then, in a second terminal:

```
.venv\Scripts\python -m obsidion.cli --network regtest getinfo
.venv\Scripts\python -m obsidion.cli --network regtest getbalance
.venv\Scripts\python -m obsidion.cli --network regtest send rtobsd1q... 12.5
```

And watch it live in a browser:

```
.venv\Scripts\python -m explorer.app --network regtest
→ http://127.0.0.1:8080
```

The explorer shows blocks as they are mined, circulating supply against the
cap, and the countdown to the next halving.

## Running a real network

Every node both serves and syncs; there are no special nodes. On the first
machine:

```
python -m obsidion.node --network mainnet --wallet my.wallet --create-wallet --mine
```

On every other machine, point at any machine already in the network:

```
python -m obsidion.node --network mainnet --wallet my.wallet --create-wallet ^
    --mine --connect 192.168.1.50:9444
```

New nodes handshake, learn other peers' addresses automatically, sync the
chain from genesis (validating every block themselves — nothing is taken on
trust), and join the mining race. Partitioned nodes converge on the
most-work chain when they reconnect; orphaned transactions return to the
mempool and are re-mined.

The wallet password can be supplied via the `OBSIDION_WALLET_PASSWORD`
environment variable for unattended nodes; otherwise you are prompted.

### Running at home

**You do not need to accept incoming connections to take part.** An
outbound-only node syncs, validates, mines and broadcasts like any other — it
simply never lets strangers initiate contact with your machine:

```
python -m obsidion.node --host 127.0.0.1 --wallet my.wallet --create-wallet --mine
```

A machine behind an ordinary router with no port forwarding is already
effectively outbound-only. Prefer running reachable, listening nodes on a
rented server rather than on a computer that holds anything you care about.

The RPC port controls the wallet. It is authenticated with a token written to
`.rpccookie` in the data directory, refuses anything carrying a browser
`Origin` header, and will not bind off-loopback without an explicit override.
See **[SECURITY.md](SECURITY.md)** for the full threat model — what a peer can
and cannot do to you, and why.

## Architecture

```
obsidion/
  params.py      every constant that defines the coin (rename/retune here)
  crypto.py      sha256d, hash160, secp256k1, bech32, scrypt PoW  [+ _ripemd160.py]
  merkle.py      merkle root & inclusion proofs (CVE-2012-2459 guarded)
  transaction.py UTXO transactions; malleability-proof txids; BIP-143 amounts
  block.py       80-byte headers, compact targets, proof-of-work
  consensus.py   subsidy/halving, the supply cap, difficulty, validation rules
  chainstate.py  the UTXO set; connect/disconnect/reorg with atomic undo
  storage.py     SQLite persistence (the DB is the only copy of chain state)
  mempool.py     admission, fee ordering, size budget, reorg recovery
  miner.py       template assembly + nonce grinding (threaded)
  p2p.py         asyncio TCP gossip, locator sync, ban scoring
  wallet.py      encrypted keys (scrypt + HMAC-CTR), coin selection, signing
  rpc.py         JSON-RPC over localhost HTTP — the only door into a node
  node.py        the daemon that wires it all together     → obsidion-node
  cli.py         command-line client                       → obsidion-cli
explorer/
  app.py         Flask explorer, speaks only RPC           → obsidion-explorer
tests/           360 tests, unit through three-node integration
```

Design rules that hold everywhere:

- **Amounts are integers** (shards). Money never touches floating point; RPC
  carries amounts as decimal strings.
- **Consensus code is pure** — no clocks, no I/O, no randomness. Two nodes
  given the same inputs cannot disagree.
- **`subsidy()` is the only function that creates coins**, and
  `check_coinbase_reward` holds every block to it. That pair *is* the 21M cap.
- **Whole reorgs are single SQLite transactions.** A reorg that fails half-way
  rolls back to the exact prior state; a heavier-but-invalid branch cannot
  take the chain down with it.
- **The node is the only thing that touches chain state.** Wallet, CLI, and
  explorer are all RPC clients and cannot corrupt it.
- **The mining algorithm is one function.** `crypto.pow_hash` dispatches by
  name and nothing else knows what it does — which is how the switch from
  SHA-256d to scrypt touched two files.

## Development

```
.venv\Scripts\python -m pytest            # the full suite
.venv\Scripts\python -m pytest tests/test_consensus.py   # just the economics
```

Consensus properties with dedicated tests: total emission equals exactly
20,999,999.9769 OBSD and not one shard more; rewards never increase; the
halving countdown matches block-by-block summation; difficulty clamps at 4×
both directions; state after a reorg is identical to a fresh sync of the
winning branch; a partitioned three-node network converges; racing miners
cannot fork the network permanently; the UTXO total equals the circulating
supply at every height.

`tests/test_hardening.py` holds regressions for real defects found by an
adversarial audit — see below.

Three networks ship in `params.py`: `mainnet`, `testnet` (fast halvings,
throwaway coins), `regtest` (instant blocks and cheap SHA-256d, so the suite
runs in seconds). Nodes on different networks cannot exchange a single
message — the 4-byte network magic fails first.

## What an audit found

The first version passed 306 tests and still contained two critical bugs. Both
were found by reviewers attacking the code rather than exercising it, and both
are now fixed with regression tests:

- **Reorgs could mint coins from nothing.** Disconnecting a block deleted the
  outputs it created *before* restoring the ones it spent. When a block
  contained a chain of spends within itself, that order resurrected an
  intermediate output which should no longer exist — inflating supply and
  permanently splitting the node from every peer that got it right.
- **A single corrupted signature could fork a node forever.** Because txids
  exclude signature bytes (the fix for malleability), a block's hash does not
  commit to its signatures either. An attacker could take a valid block,
  corrupt one signature, and send it; the node cached that hash as invalid and
  then refused the genuine block for good.

Also fixed: a nine-byte message that froze a node by declaring 2⁶⁴ locator
entries; concurrent `getnewaddress` calls silently discarding keys whose
addresses had already been handed out; an unbounded mempool; a timestamp
allowance wide enough to halve the next difficulty; wallet saves without
`fsync`; and the wallet's network field sitting outside its authentication tag.

## Honest limitations

Read this section before telling anyone this is money.

- **A small network is a vulnerable network.** Whoever holds >50% of hashpower
  can rewrite recent history and double-spend. scrypt raises the cost of
  acquiring that hashpower but does not remove the risk, and no young
  proof-of-work chain is exempt.
- **Unaudited cryptography-adjacent code.** The primitives are standard
  (secp256k1 via the `ecdsa` package, SHA-256, scrypt from OpenSSL,
  RIPEMD-160 validated against official vectors, RFC 6979 signatures, bech32
  against BIP-173 vectors), and one adversarial review has been run against
  the codebase — but no professional security firm has audited it.
- **RPC is unauthenticated localhost HTTP.** Anyone who can reach the port
  controls the wallet. Never expose it; tunnel over SSH for remote use.
- **Pure-Python signature verification** (~ms per signature) caps realistic
  throughput to roughly a few hundred transactions per second per core. Fine
  for thousands of users; wrong for millions.
- **No launch infrastructure.** Public release additionally needs a seed node
  (any cheap VPS — see [LAUNCH.md](LAUNCH.md)),
  packaged binaries with code signing, a fair-launch announcement (publish
  code + genesis before mining, or it is a stealth premine), and legal review
  of how coins are distributed. None of that is code.
- **Consensus parameters are final at launch.** Block time, halving interval,
  supply cap and mining algorithm cannot be changed afterwards without a hard
  fork that splits the chain. They are settled now, deliberately, before
  anyone is mining.

## License

MIT — do what you like, at your own risk.
