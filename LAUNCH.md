# Launching Obsidion

The software is finished. What remains is infrastructure and sequence. This is
the runbook, in the order the steps must happen.

**The one rule that cannot be undone: do not mine mainnet before the code and
the launch time are public.** Every block you mine before anyone else can join
is a coin nobody had a chance to compete for. Do it for a week and you own the
supply; do it for an hour and someone will still find it in the timestamps and
say so. A fair launch is the only launch you get one shot at.

---

## Step 1 — Publish the code (30 minutes, free)

Nothing else can happen first, because the seed node you are about to run
must be running the same published code everyone else will.

**Identity is already set up.** This repository commits as
`obsidioncoin-tech <312787066+obsidioncoin-tech@users.noreply.github.com>` —
the project account that owns the organisation, using its GitHub noreply
address so no personal inbox enters public git history. It is pinned in the
repo's local git config, so future commits keep it automatically. Do not
override it with `--author` or a global config, and never `git commit` here
from a shell that sets a different email.

**Push as `obsidioncoin-tech`, not as a personal account.** That account is
the organisation's only member, so Git Credential Manager authenticating as
anyone else will be refused with a permission error that looks like the repo
does not exist.

Create the organisation at <https://github.com/organizations/plan> (choose the
free plan), named **`obsidion-coin`**. Then:

```bash
git remote add origin https://github.com/obsidion-coin/obsidion.git
git push -u origin master
```

Or in one step with the GitHub CLI, once the org exists:

```bash
gh repo create obsidion-coin/obsidion --public --source=. --remote=origin --push
```

The result is `github.com/obsidion-coin/obsidion` — which is the URL that goes
in the announcement, the explorer footer, and every seed server's clone
command, so it should not change afterwards.

**Before you push, look at what you are publishing.** `git log -p` one last
time. Once it is public it is cloned, mirrored and indexed within hours, and
nothing is recallable.

The repository already contains `LICENSE` (MIT), a CI workflow that runs all
359 tests on Linux, Windows and macOS, and a README that states the known
limitations plainly. Leave that section in. A project that names its own weak
points is trusted more than one that does not, and everything in it is
discoverable anyway.

## Step 2 — Stand up a seed node (1 hour, free)

A new node needs someone to call. Without a reachable seed you can announce to
an audience that then cannot join.

**This costs nothing.** Oracle Cloud's Always Free tier includes a permanent
VM with a public IP — not a twelve-month trial. Google Cloud's free `e2-micro`
is a workable second choice; watch its 1 GB/month egress allowance, which
bills rather than stops.

One seed is enough to launch. Two, on different providers, is better — a
network whose only seed is offline is a network nobody new can join.

### Creating the instance

At <https://cloud.oracle.com>, create a **Compute instance**:

- **Shape:** `VM.Standard.E2.1.Micro` (AMD, 1 GB). Almost always available.
  The ARM `VM.Standard.A1.Flex` shapes are far more generous and frequently
  **out of capacity** in busy regions — that error is normal and not something
  you did wrong. Take the AMD micro and move on.
- **Image:** Canonical Ubuntu (22.04 or 24.04).
- **SSH keys:** save the private key Oracle offers; it is the only way in.

### Opening the port — the step that traps everyone

**Oracle has two independent firewalls, and you must open port 9444 in both.**
Miss either and the node runs perfectly, logs look healthy, and nobody in the
world can reach it.

**First, the Virtual Cloud Network security list** (in the console):
Networking → Virtual Cloud Networks → your VCN → Security Lists → Default →
**Add Ingress Rule**:

```
Source CIDR:      0.0.0.0/0
IP Protocol:      TCP
Destination Port: 9444
```

**Second, the instance's own firewall.** Oracle's Ubuntu images ship
`iptables` rules that drop everything, regardless of `ufw`:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 9444 -j ACCEPT
sudo netfilter-persistent save
```

If `ufw` is active, allow it there too:

```bash
sudo ufw allow 9444/tcp
```

**Open only 9444.** The wallet RPC stays on loopback and must never be
exposed — see `SECURITY.md`; use an SSH tunnel if you need it remotely.

### Installing the node

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
sudo useradd --system --create-home --home-dir /var/lib/obsidion obsidion
sudo -u obsidion -H bash -c '
  cd /var/lib/obsidion
  git clone https://github.com/obsidion-coin/obsidion.git
  cd obsidion
  python3 -m venv .venv
  .venv/bin/pip install ecdsa flask pytest
  .venv/bin/python -m pytest -q          # confirm it passes here too
'

sudo mkdir -p /etc/obsidion
printf 'OBSIDION_WALLET_PASSWORD=%s' 'a-long-random-password' \
  | sudo tee /etc/obsidion/password > /dev/null
sudo chown obsidion:obsidion /etc/obsidion/password
sudo chmod 600 /etc/obsidion/password

sudo cp /var/lib/obsidion/obsidion/deploy/obsidion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now obsidion
```

Check it came up:

```bash
sudo journalctl -u obsidion -n 20
```

**Do not pass `--mine` yet.** These nodes should relay, not mine, until the
announced start time.

### Confirm it from somewhere else

```bash
python deploy/preflight.py <public-ip>:9444
```

**Run this from your laptop, never from the server.** A machine can always
reach itself, so checking locally proves nothing at all about either firewall.
This is the only test that shows the outside world can get in.

### Two notes on free tiers

Oracle may reclaim Always Free compute that sits genuinely idle. A running
node is not idle, but a seed disappearing is precisely the failure that stops
new people joining, so check on it occasionally.

If you ever lose the VM, a spare machine at home works as a fallback, because
**`seed_nodes` accepts hostnames as well as IP addresses** — verified by
`tests/test_integration.py::test_a_seed_may_be_a_hostname_rather_than_an_ip`.
Register a free DuckDNS name, point it at your home connection, forward port
9444, and put `yourname.duckdns.org:9444` in `seed_nodes`.

That path has real costs: your home IP becomes public, and a listening service
sits on your home network. Use a machine that holds nothing you care about —
an old laptop or a Raspberry Pi — **never your main computer.**

## Step 3 — Bake the seed addresses in (10 minutes)

Edit `obsidion/params.py`, in `MAINNET`:

```python
    seed_nodes=("203.0.113.10:9444", "198.51.100.20:9444"),
```

Commit, push, and **redeploy the seed** (`git pull && sudo systemctl
restart obsidion`) so they run the same code as everyone else.

From your own machine, prove a stranger can join:

```bash
.venv\Scripts\python -m obsidion.node --network mainnet --wallet test.wallet --create-wallet
```

It should print the seeds it is dialling, and `obsidion-cli getpeerinfo`
should list them. If that works from a machine that was told nothing, the
network is joinable.

## Step 4 — Announce, then mine (the launch itself)

Publish the repository link, the genesis hash, and a **start time at least 24
hours out**, so anyone interested can install and be ready to mine from block
one. Post where technical people actually are: r/CryptoCurrency,
r/gpumining, Bitcointalk's altcoin announcements board, Hacker News.

Include:

- **Genesis hash** `69e33674de2c169233dbbdca69dcd1ede122207cd7ead83c5564e08172862a7a`
- **Mining starts** at your announced UTC time — and that nobody, you
  included, mines before it
- **No premine, no presale, no dev fund.** Every OBSD in existence will have
  been mined. Say this plainly; it is your strongest claim and it is true.
- **scrypt at 2 MB, CPU-mineable.** ~166 H/s on an ordinary laptop core.
- **2.5-minute blocks, 50 OBSD, halving every 210,000 blocks (~1 year),
  capped at 20,999,999.9769.**

At the announced time, start mining on your own machine — no earlier:

```bash
python -m obsidion.node --network mainnet --wallet my.wallet --create-wallet     --host 127.0.0.1 --mine
```

Note `--host 127.0.0.1`: your miner makes outbound connections only and never
accepts inbound, so mining at home exposes nothing. The seed handles inbound
for the network.

## Step 5 — A public explorer (30 minutes, optional but worth it)

On one seed server:

```bash
/var/lib/obsidion/obsidion/.venv/bin/python -m explorer.app \
    --network mainnet --host 0.0.0.0 --port 8080
```

The explorer needs the node's RPC token, so pass the same `--datadir` the
node uses. Put nginx and a Let's Encrypt certificate in front of it. People
believe a chain they can see, and the halving countdown is what they come back
to look at.

Note the explorer is a read-only RPC client, but it runs with a token that can
spend. Keep it on the same host as the node, behind loopback, and let nginx be
the only thing facing the internet.

---

## Expect this in the first hours

- **Blocks will arrive far faster than 2.5 minutes at first.** Difficulty
  starts at the floor and climbs ~4× per 120-block retarget period until it
  finds the real hashrate. A few hundred fast blocks at the start is normal
  and self-correcting.
- **Nobody will mine at first except you.** This is the awkward truth of every
  new chain. It resolves through people showing up, or it does not.

## Things worth spending money on later, not now

- **Code-signing certificate** (~$200–400/year) and packaged installers.
  Only worth it once non-technical users are asking. Until then, source is
  fine — your early users are the kind who read it.
- **A professional security audit.** The adversarial review already run found
  two ways to counterfeit OBSD in code that passed 306 tests. The next
  reviewer may find a third.

## Two things I am not able to advise you on

- **Whether any of this is legal where you live.** Distributing a mineable
  coin with no premine and no sale is the lowest-risk model there is — it is
  what Bitcoin and Litecoin did — but selling OBSD, promoting it as an
  investment, or running an exchange for it raises securities and
  money-transmission questions that need an actual lawyer.
- **What it is worth.** Nothing here creates value; it creates scarcity. Those
  are not the same thing.
