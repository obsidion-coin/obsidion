"""The Obsidion wallet + mining HUD — your own node, at a glance.

Where the block explorer shows the *chain*, this shows *you*: your balance,
your addresses, the blocks you have mined, your live hashrate, and how close the
next halving is. It is meant to be left open in a corner while you mine.

Like the explorer it is a pure RPC client — no keys, no database, no chain
state of its own. But unlike the explorer it must never be exposed:

  * The explorer is designed to be put behind nginx and shown to the public
    (LAUNCH.md says so). The HUD shows private balances and can mint receive
    addresses, so it is a *separate* application that refuses to bind anywhere
    but loopback unless you explicitly override it.
  * It deliberately cannot send coins. A HUD is for watching; a send form on an
    always-open page is a footgun. It offers only viewing, a "new receive
    address" button, and start/stop mining — all reversible.

    python -m hud.app --network regtest
    → http://127.0.0.1:8081
"""

from __future__ import annotations

import argparse
from pathlib import Path

from flask import Flask, jsonify, render_template_string

from obsidion.params import COIN, COIN_NAME, TICKER, get_network
from obsidion.rpc import _is_loopback, read_cookie
from obsidion.rpcclient import rpc


# --------------------------------------------------------------------------
# State assembly — a pure function of RPC results, so it holds no node handle
# and can be tested with canned replies.
# --------------------------------------------------------------------------


def build_state(
    info: dict,
    balance: dict,
    addresses: list[str],
    utxos_by_address: dict[str, dict],
    peers: list[dict],
    *,
    coinbase_maturity: int,
) -> dict:
    """Fold the node's RPC replies into the single object the page renders.

    Everything here is arithmetic on data the node already vouched for; the HUD
    invents no truth of its own. The derived fields are:

      * per-UTXO maturity — an immature coinbase shows how many blocks remain
        before it can be spent (``height + maturity - tip``);
      * blocks_mined / mined_total — counted from *unspent* coinbase outputs the
        wallet still holds (a spent reward has left the UTXO set, so this is
        "mining coins you still have", not lifetime blocks found);
      * synced — whether any connected peer claims a greater height than ours.
    """
    tip = info["height"]

    wallet_utxos: list[dict] = []
    address_rows: list[dict] = []
    blocks_mined = 0
    mined_total = 0

    for address in addresses:
        entry = utxos_by_address.get(address, {"balance": "0", "utxos": []})
        address_rows.append({"address": address, "balance": entry["balance"]})
        for utxo in entry["utxos"]:
            confirmations = tip - utxo["height"]
            if utxo["coinbase"]:
                blocks_to_mature = max(0, coinbase_maturity - confirmations)
                mature = blocks_to_mature == 0
                blocks_mined += 1
                mined_total += _shards(utxo["amount"])
            else:
                blocks_to_mature = 0
                mature = True
            wallet_utxos.append(
                {
                    **utxo,
                    "address": address,
                    "confirmations": confirmations,
                    "mature": mature,
                    "blocks_to_mature": blocks_to_mature,
                }
            )

    peer_heights = [p["height"] for p in peers if "height" in p]
    best_peer = max(peer_heights) if peer_heights else 0
    synced = not peer_heights or tip >= best_peer

    return {
        "coin": info.get("coin", f"{COIN_NAME} ({TICKER})"),
        "ticker": TICKER,
        "balance": {
            "spendable": balance["spendable"],
            "immature": balance["immature"],
            "total": balance["total"],
        },
        "wallet": {
            "addresses": address_rows,
            "utxos": wallet_utxos,
            "blocks_mined": blocks_mined,
            "mined_total_shards": mined_total,
        },
        "mining": {
            "active": bool(info.get("mining")),
            "hashrate": info.get("hashrate", 0.0),
        },
        "node": {
            "network": info.get("network"),
            "height": tip,
            "difficulty": info.get("difficulty", 0.0),
            "peers": len(peers),
            "best_peer_height": best_peer,
            "synced": synced,
        },
        "halving": info.get("halving", {}),
    }


def _shards(decimal_string: str) -> int:
    """Turn a decimal-string amount back into integer shards, exactly.

    Amounts cross RPC as strings so money never rides on a float. They are
    produced by ``rpc.format_amount`` as ``str(Decimal(shards) / COIN)``, which
    emits *scientific notation* for small values — 4 shards serialises as
    "4E-8", not "0.00000004". So this must parse with Decimal (the faithful
    inverse), never by splitting on the decimal point, which silently mangles
    the exponent form. Multiplying a Decimal by COIN and taking int() recovers
    the shard count with no rounding.
    """
    from decimal import Decimal

    return int(Decimal(decimal_string) * COIN)


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ coin }} — wallet HUD</title>
<style>
  :root {
    --bg:#0d0b12; --panel:#16121f; --line:#2a2338;
    --text:#d8d3e3; --dim:#7a7290; --accent:#9b6dff; --good:#5fd39a; --warn:#e6b566;
  }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--text); min-height:100vh;
         font:15px/1.5 system-ui,"Segoe UI",sans-serif; }
  a { color:var(--accent); text-decoration:none; }
  header { display:flex; align-items:center; gap:1rem; padding:1rem 1.5rem;
           border-bottom:1px solid var(--line); flex-wrap:wrap; }
  header .logo { font-size:1.2rem; font-weight:700; letter-spacing:.04em; }
  header .logo span { color:var(--accent); }
  header .hint { margin-left:auto; color:var(--dim); font-size:.8rem; }
  kbd { background:var(--panel); border:1px solid var(--line); border-radius:4px;
        padding:.05rem .35rem; font-family:ui-monospace,monospace; font-size:.75rem; }
  main { max-width:70rem; margin:0 auto; padding:1.5rem;
         display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
  @media (max-width:52rem){ main{ grid-template-columns:1fr; } }
  .panel { background:var(--panel); border:1px solid var(--line);
           border-radius:12px; padding:1.1rem 1.25rem; }
  .panel h2 { font-size:.8rem; color:var(--dim); text-transform:uppercase;
              letter-spacing:.09em; margin-bottom:.9rem; }
  .big { display:flex; gap:1.5rem; flex-wrap:wrap; margin-bottom:1rem; }
  .stat .k { color:var(--dim); font-size:.72rem; text-transform:uppercase;
             letter-spacing:.08em; }
  .stat .v { font-size:1.6rem; font-variant-numeric:tabular-nums; }
  .stat.big-accent .v { color:var(--accent); }
  .stat .v.good { color:var(--good); }
  .stat .v.warn { color:var(--warn); }
  .row { display:flex; justify-content:space-between; gap:1rem; padding:.35rem 0;
         border-top:1px solid var(--line); font-variant-numeric:tabular-nums; }
  .row:first-of-type { border-top:0; }
  .mono { font-family:ui-monospace,"Cascadia Mono",monospace; font-size:.82rem; }
  .dim { color:var(--dim); }
  .good { color:var(--good); } .warn { color:var(--warn); }
  button { background:var(--panel); border:1px solid var(--accent); color:var(--accent);
           padding:.4rem .8rem; border-radius:7px; cursor:pointer; font:inherit; }
  button:hover { background:var(--accent); color:#0d0b12; }
  button.stop { border-color:var(--warn); color:var(--warn); }
  button.stop:hover { background:var(--warn); color:#0d0b12; }
  .bar { height:8px; background:var(--bg); border:1px solid var(--line);
         border-radius:99px; overflow:hidden; margin-top:.4rem; }
  .bar > i { display:block; height:100%; background:var(--accent); width:0; }
  .dot { display:inline-block; width:.6rem; height:.6rem; border-radius:99px;
         background:var(--dim); margin-right:.4rem; vertical-align:middle; }
  .dot.on { background:var(--good); box-shadow:0 0 8px var(--good); }
  .controls { display:flex; gap:.5rem; margin-top:1rem; flex-wrap:wrap; }
  .addr-list { max-height:14rem; overflow:auto; }
  .copied { color:var(--good); font-size:.72rem; margin-left:.4rem; }

  /* Ctrl+Alt+Enter compact overlay: collapse to a small corner summary. */
  body.compact { background:transparent; }
  body.compact header, body.compact .panel .detail { display:none; }
  body.compact main { grid-template-columns:1fr; max-width:22rem; margin:0;
                      padding:.5rem; gap:.5rem; }
  body.compact .panel { padding:.6rem .8rem; }
  body.compact .big { gap:1rem; margin-bottom:.4rem; }
  body.compact .stat .v { font-size:1.15rem; }
</style>
</head>
<body>
<header>
  <div class="logo">◆ <span>Obsidion</span> wallet HUD</div>
  <div class="hint">
    <span id="net" class="mono dim"></span> ·
    <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Enter</kbd> overlay
  </div>
</header>
<main>
  <section class="panel">
    <h2>Wallet</h2>
    <div class="big">
      <div class="stat"><div class="k">Spendable</div>
        <div class="v good" id="spendable">—</div></div>
      <div class="stat"><div class="k">Immature</div>
        <div class="v warn" id="immature">—</div></div>
      <div class="stat"><div class="k">Total</div>
        <div class="v" id="total">—</div></div>
    </div>
    <div class="detail">
      <div class="row"><span class="dim">Blocks mined (unspent)</span>
        <span id="blocks_mined">—</span></div>
      <div class="row"><span class="dim">Mined total held</span>
        <span id="mined_total">—</span></div>
      <div class="controls">
        <button id="new-address">New receive address</button>
        <span id="copied" class="copied"></span>
      </div>
      <div class="addr-list" id="addresses"></div>
    </div>
  </section>

  <section class="panel">
    <h2>Mining</h2>
    <div class="big">
      <div class="stat"><div class="k">Status</div>
        <div class="v" id="mining-status"><span class="dot"></span>—</div></div>
      <div class="stat"><div class="k">Hashrate</div>
        <div class="v" id="hashrate">—</div></div>
      <div class="stat"><div class="k">Reward</div>
        <div class="v" id="reward">—</div></div>
    </div>
    <div class="detail">
      <div class="row"><span class="dim">Network height</span>
        <span id="height">—</span></div>
      <div class="row"><span class="dim">Difficulty</span>
        <span id="difficulty">—</span></div>
      <div class="row"><span class="dim">Peers</span>
        <span id="peers">—</span></div>
      <div class="row"><span class="dim">Sync</span>
        <span id="synced">—</span></div>
      <div class="row"><span class="dim">Next halving</span>
        <span id="halving">—</span></div>
      <div class="bar"><i id="halving-bar"></i></div>
      <div class="controls">
        <button id="start-mining">Start mining</button>
        <button id="stop-mining" class="stop">Stop mining</button>
      </div>
    </div>
  </section>
</main>

<script>
const HALVING_INTERVAL = {{ halving_interval }};
const $ = id => document.getElementById(id);

function fmt(n){ return Number(n).toLocaleString(undefined,{maximumFractionDigits:8}); }
function shardsToCoin(s){ return (Number(s)/1e8); }

async function refresh(){
  let s;
  try { s = await (await fetch('/api/state')).json(); }
  catch(e){ $('net').textContent = 'node unreachable'; return; }

  $('net').textContent = s.node.network;
  $('spendable').textContent = fmt(s.balance.spendable);
  $('immature').textContent  = fmt(s.balance.immature);
  $('total').textContent     = fmt(s.balance.total);
  $('blocks_mined').textContent = s.wallet.blocks_mined;
  $('mined_total').textContent  = fmt(shardsToCoin(s.wallet.mined_total_shards)) + ' ' + s.ticker;

  const on = s.mining.active;
  $('mining-status').innerHTML =
    '<span class="dot'+(on?' on':'')+'"></span>' + (on?'mining':'idle');
  $('hashrate').textContent = (on? s.mining.hashrate.toFixed(0):'0') + ' H/s';
  $('reward').textContent = (s.halving.block_reward||'—') + ' ' + s.ticker;

  $('height').textContent = s.node.height;
  $('difficulty').textContent = Number(s.node.difficulty).toPrecision(3);
  $('peers').textContent = s.node.peers;
  $('synced').innerHTML = s.node.synced
    ? '<span class="good">at the tip</span>'
    : '<span class="warn">behind ('+s.node.best_peer_height+')</span>';

  const rem = s.halving.blocks_remaining;
  if (rem === null || rem === undefined){
    $('halving').textContent = 'complete';
    $('halving-bar').style.width = '100%';
  } else {
    const secs = s.halving.estimated_seconds || 0;
    const eta = secs >= 86400 ? (secs/86400).toFixed(1)+'d'
              : secs >= 3600  ? (secs/3600).toFixed(1)+'h'
              : Math.round(secs/60)+'m';
    $('halving').textContent = rem + ' blocks (≈' + eta + ')';
    const done = HALVING_INTERVAL ? (HALVING_INTERVAL - rem)/HALVING_INTERVAL*100 : 0;
    $('halving-bar').style.width = Math.max(0,Math.min(100,done)) + '%';
  }

  const list = $('addresses');
  list.innerHTML = '';
  for (const a of s.wallet.addresses){
    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = '<span class="mono" style="cursor:pointer" title="click to copy">'
      + a.address.slice(0,28) + '…</span><span class="good">' + fmt(a.balance) + '</span>';
    row.firstChild.onclick = () => copy(a.address);
    list.appendChild(row);
  }
}

async function copy(text){
  try { await navigator.clipboard.writeText(text);
        $('copied').textContent = 'copied'; setTimeout(()=>$('copied').textContent='',1500);
  } catch(e){}
}
async function act(path){
  const r = await (await fetch('/action/'+path,{method:'POST'})).json();
  if (path === 'newaddress' && r.address) copy(r.address);
  refresh();
}
$('new-address').onclick  = () => act('newaddress');
$('start-mining').onclick = () => act('startmining');
$('stop-mining').onclick  = () => act('stopmining');

// Ctrl+Alt+Enter toggles the compact corner overlay; remembered across reloads.
if (localStorage.getItem('hud-compact') === '1') document.body.classList.add('compact');
window.addEventListener('keydown', e => {
  if (e.ctrlKey && e.altKey && e.key === 'Enter'){
    e.preventDefault();
    const on = document.body.classList.toggle('compact');
    localStorage.setItem('hud-compact', on ? '1' : '0');
  }
});

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------


def create_app(rpc_port: int, token: str = "", *, network: str = "mainnet") -> Flask:
    app = Flask(__name__)
    params = get_network(network)

    def call(method, *params_):
        return rpc(rpc_port, method, *params_, token=token)

    @app.get("/")
    def index():
        return render_template_string(
            PAGE,
            coin=f"{COIN_NAME} ({TICKER})",
            halving_interval=params.halving_interval,
        )

    @app.get("/api/state")
    def api_state():
        info = call("getinfo")
        balance = call("getbalance")
        addresses = call("getaddresses")
        utxos_by_address = {a: call("getaddressutxos", a) for a in addresses}
        peers = call("getpeerinfo")
        state = build_state(
            info,
            balance,
            addresses,
            utxos_by_address,
            peers,
            coinbase_maturity=params.coinbase_maturity,
        )
        return jsonify(state)

    @app.post("/action/newaddress")
    def action_newaddress():
        return jsonify({"address": call("getnewaddress")})

    @app.post("/action/startmining")
    def action_startmining():
        return jsonify({"result": call("startmining")})

    @app.post("/action/stopmining")
    def action_stopmining():
        return jsonify({"result": call("stopmining")})

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="obsidion-hud",
        description=f"Private wallet + mining HUD for a running {COIN_NAME} node.",
    )
    parser.add_argument("--network", default="mainnet")
    parser.add_argument("--rpc-port", type=int, default=None)
    parser.add_argument(
        "--datadir",
        default=str(Path.home() / ".obsidion"),
        help="where the node wrote its RPC token; must match the node's --datadir",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="permit binding off-loopback (you almost certainly do not want this: "
        "the HUD reaches a token that can spend your coins)",
    )
    args = parser.parse_args(argv)

    # Refuse a public bind before doing anything else. This check comes first so
    # it fires without needing a running node or a cookie on disk.
    if not _is_loopback(args.host) and not args.allow_remote:
        raise SystemExit(
            f"refusing to bind the HUD to {args.host!r}, which is reachable from "
            "outside this machine. It shows your balances and reaches a token that "
            "can spend your coins. Use an SSH tunnel for remote access, or pass "
            "--allow-remote if you genuinely mean it."
        )

    rpc_port = (
        args.rpc_port
        if args.rpc_port is not None
        else get_network(args.network).default_rpc_port
    )
    try:
        token = read_cookie(args.datadir)
    except FileNotFoundError:
        raise SystemExit(
            f"no RPC token in {args.datadir} — start obsidion-node first, and "
            "point --datadir at the same directory it uses."
        ) from None

    create_app(rpc_port, token, network=args.network).run(
        host=args.host, port=args.port
    )


if __name__ == "__main__":
    main()
