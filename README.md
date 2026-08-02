# Obsidion (OBSD)

A CPU-mineable, Bitcoin-style proof-of-work cryptocurrency, built from first
principles in Python. Obsidian is volcanic glass — forged under pressure,
holding the sharpest edge of any natural material.

Obsidion works the way Bitcoin works: miners race to solve proof-of-work,
the winner adds the next block and collects the reward, the reward **halves on
a fixed schedule**, and total supply is **capped forever**. None of that is
policy — it is enforced by every node's validation of every block.

| | |
|---|---|
| Ticker | **OBSD** |
| Smallest unit | 1 **shard** = 10⁻⁸ OBSD |
| Max supply | **20,999,999.9769 OBSD** (enforced by consensus, computed exactly) |
| Block time | 60 seconds |
| Initial reward | 50 OBSD |
| Halving | every 210,000 blocks (~146 days) — 64 eras, then zero forever |
| Proof of work | SHA-256d (pluggable — see *Honest limitations*) |
| Difficulty | retargets every 120 blocks (~2 h), clamped to 4× per period |
| Ledger | UTXO model, pay-to-pubkey-hash, ECDSA/secp256k1 |
| Addresses | bech32: `obsd1q…` (mainnet), `tobsd1q…` (testnet) |
| Genesis (mainnet) | `000043a545b9a010b56fe92dc3fadf67a6380f97e1b902d55ff3e5276cb91b52` |

Half of all OBSD that will ever exist mints in the first era. The emission
curve — and the true cap of 20,999,999.9769, a hair under 21M because each
halving truncates to whole shards — is bit-for-bit the same arithmetic as
Bitcoin's.

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

## Architecture

```
obsidion/
  params.py      every constant that defines the coin (rename/retune here)
  crypto.py      sha256d, hash160, secp256k1, bech32   [+ _ripemd160.py fallback]
  merkle.py      merkle root & inclusion proofs (CVE-2012-2459 guarded)
  transaction.py UTXO transactions; malleability-proof txids; BIP-143 amounts
  block.py       80-byte headers, compact targets, proof-of-work
  consensus.py   subsidy/halving, the supply cap, difficulty, validation rules
  chainstate.py  the UTXO set; connect/disconnect/reorg with atomic undo
  storage.py     SQLite persistence (the DB is the only copy of chain state)
  mempool.py     admission, fee ordering, conflict eviction, reorg recovery
  miner.py       template assembly + nonce grinding (threaded)
  p2p.py         asyncio TCP: handshake, gossip, locator sync, ban scoring
  wallet.py      encrypted keys (scrypt + HMAC-CTR), coin selection, signing
  rpc.py         JSON-RPC over localhost HTTP — the only door into a node
  node.py        the daemon that wires it all together     → obsidion-node
  cli.py         command-line client                       → obsidion-cli
explorer/
  app.py         Flask explorer, speaks only RPC           → obsidion-explorer
tests/           302 tests, unit through three-node integration
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

Three networks ship in `params.py`: `mainnet`, `testnet` (fast halvings,
throwaway coins), `regtest` (instant blocks for development). Nodes on
different networks cannot exchange a single message — the 4-byte network
magic fails first.

## Honest limitations

Read this section before telling anyone this is money.

- **SHA-256d is Bitcoin's algorithm, and Bitcoin ASICs exist.** One
  second-hand ASIC out-hashes every CPU that will ever run this code combined,
  so "easy to mine on a laptop" only holds until someone points an ASIC at
  it. The hash sits behind a single function (`crypto.pow_hash`) precisely so
  a memory-hard algorithm (Argon2/RandomX-style) can replace it — do that
  **before** a public launch; after launch it is a hard fork.
- **A small network is a vulnerable network.** Whoever holds >50% of hashpower
  can rewrite recent history and double-spend. This is true of every young
  PoW chain and is not fixable in code.
- **Unaudited cryptography-adjacent code.** The primitives are standard
  (secp256k1 via the `ecdsa` package, SHA-256, RIPEMD-160 validated against
  official vectors, RFC 6979 signatures, bech32 against BIP-173 vectors), but
  no external party has audited this codebase.
- **RPC is unauthenticated localhost HTTP.** Anyone who can reach the port
  controls the wallet. Never expose it; tunnel over SSH for remote use.
- **Pure-Python signature verification** (~ms per signature) caps realistic
  throughput far below Bitcoin's. Fine for thousands of users; wrong for
  millions.
- **No launch infrastructure.** Public release additionally needs seed nodes,
  packaged binaries with code signing, a fair-launch announcement (publish
  code + genesis before mining, or it is a stealth premine), and legal review
  of how coins are distributed. None of that is code.

## License

MIT — do what you like, at your own risk.
