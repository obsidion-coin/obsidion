# Security

What running an Obsidion node does and does not expose, stated plainly. If you
are deciding whether to run this on a computer that matters to you, this page
is the honest answer.

## Reporting a vulnerability

Open a GitHub issue for anything already public. For anything that could be
exploited before a fix ships, use GitHub's **private vulnerability reporting**
on this repository rather than a public issue.

## What a peer can do to you

A connected peer sends bytes to your node's P2P port. Those bytes are parsed
into transactions and blocks, validated, and either accepted or discarded.

**A peer cannot read your files.** The peer-to-peer protocol contains no file
operations of any kind. Every file path in the codebase derives from a
command-line argument you supplied — `--datadir`, `--wallet`. Nothing a peer
sends ever becomes a path. Chain data reaches disk only through parameterised
SQLite statements.

**A peer cannot run code on your machine.** The codebase contains no
code-execution primitives at all: no `eval`, `exec`, `pickle`, `marshal`,
`os.system`, `subprocess`, `__import__` or `shell=True`. Peer messages are
decoded by hand-written byte readers into plain dataclasses. No untrusted
object is ever deserialized. Python has no buffer overflows, so the usual
memory-corruption route to code execution does not exist either.

**A peer can try to waste your resources.** Denial of service is the real
category of risk. Known instances have been fixed and carry regression tests
in `tests/test_hardening.py`: an unbounded `getblocks` locator that could
freeze a node with a nine-byte message, an unbounded mempool, and unbounded
per-peer request tracking. Others may remain.

**A peer learns your IP address.** Anyone you connect to, and anyone who
connects to you, sees it. This is inherent to peer-to-peer networking, not a
flaw. If that matters to you, run your node on a server rather than at home.

## The RPC port is the wallet

`obsidion-node` opens a JSON-RPC port. **Anything that can successfully call it
can spend your coins.** It is protected three ways, deliberately independent so
that no single mistake reopens it:

1. **A token.** A random 32-byte token is generated at every start and written
   to `.rpccookie` in the data directory, owner-readable. Every request must
   carry it as `Authorization: Bearer <token>`. Remote attackers cannot read a
   local file. The token changes on restart, so a leaked one expires.
2. **Origin rejection.** Any request carrying an `Origin` or `Referer` header
   is refused with HTTP 403. Browsers always attach these cross-origin;
   command-line clients never do.
3. **A JSON content type.** `Content-Type: application/json` is required. A
   browser cannot set it cross-origin without a CORS preflight, which this
   server never approves.

Layers 2 and 3 exist because **binding to localhost is not a defence against a
browser.** A web page you merely visit can make your own browser POST to
`127.0.0.1`. That is ordinary cross-origin behaviour, not an exploit. Before
these checks existed, such a page could have called `send` and emptied the
wallet of anyone running a node. That hole was real, was demonstrated, and is
closed.

The server refuses to bind anywhere but loopback unless you pass
`--rpc-allow-remote`. **Do not pass it.** For remote access use an SSH tunnel:

```bash
ssh -N -L 9445:127.0.0.1:9445 you@your-server
```

## Running safely

**Do not run a public-facing node on a computer that holds anything you care
about.** You do not need to. A node that only makes outbound connections is a
full participant — it syncs, validates, mines and broadcasts — it simply never
accepts incoming connections, so nothing on the internet can initiate contact
with it. Most of any peer-to-peer network runs this way.

At home:

```bash
obsidion-node --host 127.0.0.1 --wallet my.wallet
```

A machine behind an ordinary router with no port forwarding is already
effectively outbound-only. Do not forward port 9444 unless you have decided to
be a reachable node, and prefer doing that on a rented server.

Other habits worth keeping:

- **Keep mining rewards off your daily machine.** Anything that ever
  compromises that computer gets the keys too. Generate a receiving address in
  a wallet whose file lives elsewhere and mine to that.
- **Back up your wallet file, and remember the password.** The file is
  encrypted with scrypt; there is no recovery path. Lost keys mean coins that
  provably exist and can never move.
- **Run seed nodes as a dedicated system user** with the hardened unit in
  `deploy/obsidion.service`. Treat them as disposable.

## What has and has not been reviewed

An adversarial review has been run against this codebase. It found two critical
consensus bugs — a reorg path that could mint coins from nothing, and a cache
poisoning route that could permanently fork a node — in code that was passing
306 tests at the time. Both are fixed with regression tests. The RPC hole
described above was found separately, after that review.

**No professional security firm has audited this.** The cryptographic
primitives are standard and used conventionally (secp256k1 via `ecdsa`,
SHA-256, scrypt from OpenSSL, RIPEMD-160 checked against published vectors,
RFC 6979 deterministic signatures, bech32 against BIP-173 vectors), but
"standard primitives used correctly" is a claim that deserves independent
checking, and it has not had it.

A young proof-of-work chain with little hashpower can also be 51%-attacked.
That is true of every new chain and is not fixable in code.
