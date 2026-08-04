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

Nothing else can happen first, because the seed nodes you are about to run
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
350 tests on Linux, Windows and macOS, and a README that states the known
limitations plainly. Leave that section in. A project that names its own weak
points is trusted more than one that does not, and everything in it is
discoverable anyway.

## Step 2 — Stand up two seed nodes (1 hour, ~$10/month)

A new node needs someone to call. Two, on **different providers** — Hetzner
and DigitalOcean, or Vultr and Linode. Two nodes at one provider share one
outage.

The cheapest tier is enough: 1 vCPU, 1 GB RAM, 20 GB disk.

On each server:

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
sudo ufw allow 9444/tcp
```

Check it came up:

```bash
sudo journalctl -u obsidion -n 20
```

**Do not pass `--mine` yet.** These nodes should relay, not mine, until the
announced start time.

## Step 3 — Bake the seed addresses in (10 minutes)

Edit `obsidion/params.py`, in `MAINNET`:

```python
    seed_nodes=("203.0.113.10:9444", "198.51.100.20:9444"),
```

Commit, push, and **redeploy both servers** (`git pull && sudo systemctl
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

At the announced time, add `--mine` to the seed nodes' service file (or start
mining on your own machine) — no earlier.

## Step 5 — A public explorer (30 minutes, optional but worth it)

On one seed server:

```bash
/var/lib/obsidion/obsidion/.venv/bin/python -m explorer.app \
    --network mainnet --host 0.0.0.0 --port 8080
```

Put nginx and a Let's Encrypt certificate in front of it. People believe a
chain they can see. The halving countdown is the thing they will come back to
look at.

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
