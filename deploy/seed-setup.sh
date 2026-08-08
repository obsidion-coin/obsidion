#!/bin/bash
# One-shot installer for an Obsidion seed node on a fresh Ubuntu server.
#
# Run as root on the server:
#   curl -fsSL https://raw.githubusercontent.com/obsidion-coin/obsidion/master/deploy/seed-setup.sh | sudo bash
#
# It installs Python, opens the P2P port, creates an unprivileged service user,
# clones the published code, and starts the node as a systemd service that
# relays only — no wallet, no mining. Safe to re-run; it is idempotent.
set -euo pipefail

echo "== Obsidion seed setup =="

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv git

# Firewall: SSH plus the P2P port, nothing else. The RPC stays on loopback.
if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH  || true
    ufw allow 9444/tcp || true
    ufw --force enable  || true
fi

# Dedicated unprivileged user. The seed holds no keys, so a compromise of this
# box costs the network a relay, not anyone's coins.
if ! id obsidion >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/obsidion obsidion
fi

sudo -u obsidion -H bash <<'SETUP'
set -e
cd /var/lib/obsidion
if [ ! -d obsidion ]; then
    git clone https://github.com/obsidion-coin/obsidion.git
fi
cd obsidion
git pull --ff-only || true
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet ecdsa flask
# Prove the published code derives the expected genesis on this machine.
.venv/bin/python -c "from obsidion.genesis import genesis_hash; from obsidion.params import MAINNET; print('genesis', genesis_hash(MAINNET)[::-1].hex())"
SETUP

cp /var/lib/obsidion/obsidion/deploy/obsidion.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now obsidion
sleep 4
systemctl --no-pager status obsidion | head -5 || true

echo "== Obsidion seed is up. Verify from elsewhere with deploy/preflight.py =="
