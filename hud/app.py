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

from flask import Flask, jsonify, render_template_string, request, send_file

from obsidion.params import COIN, COIN_NAME, TICKER, get_network
from obsidion.rpc import _is_loopback, read_cookie
from obsidion.rpcclient import RPCClientError, rpc


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
            # Heights of the blocks we mined and still hold, for the KPI panel.
            "coinbase_heights": sorted(
                u["height"] for u in wallet_utxos if u["coinbase"]
            ),
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


def mining_kpis(
    info: dict,
    recent_blocks: list[dict],
    my_coinbase_heights: list[int],
    *,
    target_block_time: int,
) -> dict:
    """Derive the numbers a miner actually wants from data the node already has.

    `recent_blocks` is a small window of the newest blocks, each with `time` and
    `bits`. Everything below follows from three facts: how much work a block
    costs at the current target, how fast blocks are actually arriving, and how
    fast this machine hashes.

      * network hashrate ≈ work-per-block ÷ observed seconds-per-block. This is
        an estimate from a short window, so it is noisy — block discovery is a
        Poisson process and a handful of samples will swing it. Presented as an
        estimate, not a measurement.
      * share = our hashrate ÷ network hashrate, which is also our expected
        share of future blocks.
      * expected seconds per block *for us* = work-per-block ÷ our hashrate.
        This is a mean, not a countdown: mining is memoryless, so having waited
        an hour tells you nothing about the next minute.

    Returns zeros rather than raising when the chain is too short to measure —
    a fresh node should render a HUD, not an error.
    """
    from obsidion.block import compact_to_target, expected_hashes

    # An idle miner contributes nothing, whatever number the node reports for
    # it. Deriving share and projected earnings from a stale rate would put a
    # confident forecast next to a status of "idle", which is worse than a
    # blank: every figure below hangs off this one.
    my_hashrate = float(info.get("hashrate") or 0.0) if info.get("mining") else 0.0
    height = info.get("height", 0)

    # Work per block at the newest target we can see.
    work_per_block = 0.0
    if recent_blocks:
        try:
            work_per_block = expected_hashes(
                compact_to_target(int(recent_blocks[-1]["bits"], 16))
            )
        except (ValueError, KeyError):
            work_per_block = 0.0

    # Observed spacing. Timestamps are miner-supplied and only loosely ordered,
    # so use the span across the window rather than per-pair deltas, and clamp
    # at zero: a single back-dated block must not produce a negative rate.
    avg_block_seconds = 0.0
    if len(recent_blocks) >= 2:
        span = recent_blocks[-1]["time"] - recent_blocks[0]["time"]
        if span > 0:
            avg_block_seconds = span / (len(recent_blocks) - 1)

    network_hashrate = (
        work_per_block / avg_block_seconds if avg_block_seconds > 0 else 0.0
    )
    share = my_hashrate / network_hashrate if network_hashrate > 0 else 0.0
    # Our own share can exceed the estimate's precision on a tiny network;
    # a share above 1 is an artefact of the estimate, not a real >100%.
    share = min(share, 1.0) if network_hashrate > 0 else 0.0

    seconds_per_block_for_us = (
        work_per_block / my_hashrate if my_hashrate > 0 and work_per_block else 0.0
    )

    # Expected daily earnings at the current share and reward.
    reward = 0.0
    try:
        reward = float(info.get("halving", {}).get("block_reward") or 0)
    except (TypeError, ValueError):
        reward = 0.0
    blocks_per_day = 86400.0 / avg_block_seconds if avg_block_seconds > 0 else 0.0
    expected_daily = blocks_per_day * share * reward

    # Blocks we found recently, straight from the heights of the coinbase
    # outputs we still hold — no extra RPC, though it undercounts any reward
    # already spent.
    def found_within(window: int) -> int:
        floor = height - window
        return sum(1 for h in my_coinbase_heights if h > floor)

    return {
        "my_hashrate": my_hashrate,
        "network_hashrate": network_hashrate,
        "share": share,
        "work_per_block": work_per_block,
        "avg_block_seconds": avg_block_seconds,
        "target_block_time": target_block_time,
        "seconds_per_block_for_us": seconds_per_block_for_us,
        "expected_daily": expected_daily,
        "blocks_found_total": len(my_coinbase_heights),
        "blocks_found_last_100": found_within(100),
        "blocks_found_last_day": found_within(
            max(1, int(86400 / target_block_time))
        ),
        "sample_size": len(recent_blocks),
        # True while the chain is still climbing out of the difficulty floor,
        # which makes `expected_daily` wildly optimistic. The page warns on it;
        # flagged here so the judgement is tested, not buried in JavaScript.
        "difficulty_still_rising": bool(
            avg_block_seconds and avg_block_seconds < target_block_time * 0.75
        ),
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

  .send { margin-top:1.1rem; padding-top:.9rem; border-top:1px solid var(--line); }
  .send h3 { font-size:.78rem; color:var(--dim); text-transform:uppercase;
             letter-spacing:.09em; margin-bottom:.6rem; }
  .send input { width:100%; background:var(--bg); border:1px solid var(--line);
                color:var(--text); padding:.45rem .6rem; border-radius:7px;
                font:inherit; font-family:ui-monospace,monospace; font-size:.82rem;
                margin-bottom:.45rem; }
  .send input:focus { outline:none; border-color:var(--accent); }
  .send-amount-row { display:flex; align-items:center; gap:.6rem; }
  .send-amount-row input { flex:1; margin-bottom:0; }
  .send-amount-row .dim { font-size:.75rem; white-space:nowrap; }
  /* The confirm box is visually louder than the form on purpose: stage two
     should not look like more of stage one. */
  .confirm { margin-top:.8rem; padding:.8rem; border:1px solid var(--warn);
             border-radius:9px; background:rgba(230,181,102,.06); }
  .confirm-title { color:var(--warn); font-size:.8rem; text-transform:uppercase;
                   letter-spacing:.08em; margin-bottom:.5rem; }
  .confirm-row { display:flex; justify-content:space-between; margin-bottom:.25rem; }
  .amount-big { font-size:1.15rem; font-variant-numeric:tabular-nums; }
  /* Full address, wrapped — never truncated. An ellipsis here would hide the
     exact characters the user is being asked to check. */
  .confirm-addr { word-break:break-all; background:var(--bg); padding:.4rem .5rem;
                  border-radius:6px; margin:.2rem 0 .5rem; line-height:1.45; }
  .confirm-warn { color:var(--warn); font-size:.75rem; margin-bottom:.6rem; }
  button.danger { border-color:var(--warn); color:var(--warn); }
  button.danger:hover { background:var(--warn); color:#0d0b12; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  button:disabled:hover { background:var(--panel); color:var(--warn); }
  .send-result { font-size:.78rem; margin-top:.5rem; word-break:break-all; }

  .span2 { grid-column:1 / -1; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
          gap:.9rem 1rem; }
  .kpi .k { color:var(--dim); font-size:.7rem; text-transform:uppercase;
            letter-spacing:.08em; }
  .kpi .v { font-size:1.35rem; font-variant-numeric:tabular-nums; line-height:1.25; }
  .kpi .v.accent { color:var(--accent); }
  .kpi .sub { color:var(--dim); font-size:.68rem; margin-top:.05rem; }
  .kpi-note { font-size:.7rem; margin-top:.9rem; line-height:1.5; }

  .addr-row { align-items:center; gap:.5rem; }
  .addr-label { cursor:pointer; flex:1; }
  .addr-bal { white-space:nowrap; }
  .addr-tools { display:flex; gap:.3rem; }
  .addr-row.is-hidden { opacity:.45; }
  button.mini { padding:.12rem .4rem; font-size:.68rem; border-radius:5px; }
  button.mini.danger { border-color:var(--warn); color:var(--warn); }
  button.linky { border:0; background:none; color:var(--dim); padding:.3rem 0;
                 text-decoration:underline; }
  button.linky:hover { background:none; color:var(--accent); }

  /* Ctrl+Alt+Enter compact overlay: collapse to a small corner summary. */
  body.compact { background:transparent; }
  /* .detail carries the send form too, so the overlay cannot spend by
     accident — the compact view is for glancing, not transacting. */
  body.compact header, body.compact .panel .detail { display:none; }
  body.compact .span2 { display:none; }
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
      <button id="toggle-hidden" class="mini linky" hidden></button>

      <div class="send">
        <h3>Send</h3>
        <input id="send-address" placeholder="recipient address (obsd1…)"
               autocomplete="off" spellcheck="false">
        <div class="send-amount-row">
          <input id="send-amount" placeholder="amount" inputmode="decimal"
                 autocomplete="off">
          <span class="dim">spendable <span id="send-avail">—</span></span>
        </div>
        <div class="controls">
          <button id="review-send">Review send</button>
        </div>

        <!-- Stage two. Nothing has been sent at this point; this box only
             restates what stage one typed, in full, so a wrong address has a
             chance to look wrong before it becomes irreversible. -->
        <div id="confirm-box" class="confirm" hidden>
          <div class="confirm-title">Confirm this payment</div>
          <div class="confirm-row"><span class="dim">amount</span>
            <span id="confirm-amount" class="amount-big"></span></div>
          <div class="confirm-row"><span class="dim">to</span></div>
          <div id="confirm-address" class="mono confirm-addr"></div>
          <div class="confirm-warn">
            This cannot be undone. Check every character of the address.
          </div>
          <div class="controls">
            <button id="confirm-send" class="danger">Confirm send</button>
            <button id="cancel-send">Cancel</button>
          </div>
        </div>
        <div id="send-result" class="send-result"></div>
      </div>
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

  <section class="panel span2">
    <h2>Mining performance</h2>
    <div class="kpis">
      <div class="kpi"><div class="k">Blocks found</div>
        <div class="v" id="kpi-found">—</div>
        <div class="sub" id="kpi-found-sub">held, unspent</div></div>
      <div class="kpi"><div class="k">Coins mined</div>
        <div class="v good" id="kpi-mined">—</div>
        <div class="sub">total held</div></div>
      <div class="kpi"><div class="k">Last 24h</div>
        <div class="v" id="kpi-day">—</div>
        <div class="sub">blocks found</div></div>
      <div class="kpi"><div class="k">Your hashrate</div>
        <div class="v" id="kpi-hash">—</div>
        <div class="sub">this machine</div></div>
      <div class="kpi"><div class="k">Network hashrate</div>
        <div class="v" id="kpi-nethash">—</div>
        <div class="sub" id="kpi-nethash-sub">estimated</div></div>
      <div class="kpi"><div class="k">Your share</div>
        <div class="v accent" id="kpi-share">—</div>
        <div class="sub">of network power</div></div>
      <div class="kpi"><div class="k">Avg block time</div>
        <div class="v" id="kpi-blocktime">—</div>
        <div class="sub" id="kpi-blocktime-sub">—</div></div>
      <div class="kpi"><div class="k">Expected per block</div>
        <div class="v" id="kpi-eta">—</div>
        <div class="sub">mean wait, not a countdown</div></div>
      <div class="kpi"><div class="k">Projected daily</div>
        <div class="v good" id="kpi-daily">—</div>
        <div class="sub" id="kpi-daily-sub">at current share</div></div>
    </div>
    <div class="kpi-note dim" id="kpi-note"></div>
  </section>
</main>

<script>
const HALVING_INTERVAL = {{ halving_interval }};
const $ = id => document.getElementById(id);

function fmt(n){ return Number(n).toLocaleString(undefined,{maximumFractionDigits:8}); }
function shardsToCoin(s){ return (Number(s)/1e8); }

// A rate in hashes/second, in units a person can read.
function hashrate(h){
  if (!h) return '0 H/s';
  const units = ['H/s','kH/s','MH/s','GH/s','TH/s'];
  let i = 0;
  while (h >= 1000 && i < units.length - 1){ h /= 1000; i++; }
  return h.toFixed(h < 10 ? 2 : (h < 100 ? 1 : 0)) + ' ' + units[i];
}

// A span in seconds, in the largest unit that keeps it readable.
function duration(s){
  if (!s || !isFinite(s)) return '—';
  if (s < 90) return s.toFixed(s < 10 ? 1 : 0) + 's';
  if (s < 5400) return (s/60).toFixed(1) + ' min';
  if (s < 172800) return (s/3600).toFixed(1) + ' h';
  return (s/86400).toFixed(1) + ' d';
}

function renderKpis(s){
  const k = s.kpis; if (!k) return;
  const t = s.ticker;

  $('kpi-found').textContent = k.blocks_found_total;
  $('kpi-mined').textContent = fmt(shardsToCoin(s.wallet.mined_total_shards)) + ' ' + t;
  $('kpi-day').textContent   = k.blocks_found_last_day;
  $('kpi-hash').textContent  = hashrate(k.my_hashrate);
  $('kpi-nethash').textContent = k.network_hashrate ? hashrate(k.network_hashrate) : '—';
  $('kpi-nethash-sub').textContent = k.sample_size
    ? 'estimated from ' + k.sample_size + ' blocks' : 'not enough blocks yet';
  $('kpi-share').textContent = k.network_hashrate
    ? (k.share*100).toFixed(k.share > 0.1 ? 1 : 2) + '%' : '—';

  $('kpi-blocktime').textContent = k.avg_block_seconds
    ? duration(k.avg_block_seconds) : '—';
  // Compare observed spacing to the 2.5-minute target so a chain still
  // climbing out of the difficulty floor reads as expected, not broken.
  if (k.avg_block_seconds && k.target_block_time){
    const ratio = k.avg_block_seconds / k.target_block_time;
    $('kpi-blocktime-sub').textContent = ratio < 0.75
      ? 'faster than the ' + duration(k.target_block_time) + ' target'
      : (ratio > 1.33 ? 'slower than target' : 'on target');
  } else {
    $('kpi-blocktime-sub').textContent = 'target ' + duration(k.target_block_time);
  }

  $('kpi-eta').textContent = k.seconds_per_block_for_us
    ? duration(k.seconds_per_block_for_us) : '—';
  $('kpi-daily').textContent = k.expected_daily
    ? fmt(k.expected_daily.toFixed(4)) + ' ' + t : '—';

  // A young chain sits at the difficulty floor and produces blocks far faster
  // than target, which makes the daily projection wildly optimistic. Say so on
  // the tile itself — a number this large is worse than no number if the
  // reader does not know it is about to collapse.
  const climbing = k.avg_block_seconds
    && k.target_block_time
    && k.avg_block_seconds < k.target_block_time * 0.75;
  $('kpi-daily-sub').innerHTML = climbing
    ? '<span class="warn">difficulty still rising — will fall sharply</span>'
    : 'at current share and difficulty';

  $('kpi-note').textContent =
    'Network hashrate is estimated from the last ' + (k.sample_size||0)
    + ' blocks, so it is noisy on a small chain. "Expected per block" is an '
    + 'average wait, not a countdown — mining has no memory, so a long dry '
    + 'spell does not make the next block any closer. "Blocks found" counts '
    + 'rewards you still hold; spending one lowers it.'
    + (climbing
       ? ' Blocks are arriving far faster than the '
         + duration(k.target_block_time) + ' target because difficulty starts '
         + 'at the network floor and climbs up to 4x every '
         + 'retarget period. The daily projection assumes today\'s easy '
         + 'difficulty and will drop steeply as it catches up.'
       : '');
}

async function refresh(){
  let s;
  try { s = await (await fetch('/api/state')).json(); }
  catch(e){ $('net').textContent = 'node unreachable'; return; }

  $('net').textContent = s.node.network;
  $('net').dataset.ticker = s.ticker;
  // Keep the send pre-check honest against the live balance.
  spendable = Number(s.balance.spendable);
  $('send-avail').textContent = fmt(s.balance.spendable);
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

  renderKpis(s);

  renderAddresses(s);
}

// Hiding is a view preference and never touches the wallet: the key stays,
// the address still receives, it just stops cluttering this list. Deleting is
// the other thing entirely, and lives behind the wallet's own guards.
function hidden(){
  try { return new Set(JSON.parse(localStorage.getItem('hud-hidden') || '[]')); }
  catch(e){ return new Set(); }
}
function setHidden(set){
  localStorage.setItem('hud-hidden', JSON.stringify([...set]));
}

let showHidden = false;

function renderAddresses(s){
  const list = $('addresses');
  const hide = hidden();
  list.innerHTML = '';

  const rows = s.wallet.addresses.filter(a => showHidden || !hide.has(a.address));
  const hiddenCount = s.wallet.addresses.length - rows.filter(
    a => !hide.has(a.address)).length;

  for (const a of rows){
    const isHidden = hide.has(a.address);
    const isDefault = a.address === (s.wallet.addresses[0] || {}).address;
    const funded = Number(a.balance) > 0;

    const row = document.createElement('div');
    row.className = 'row addr-row' + (isHidden ? ' is-hidden' : '');

    const label = document.createElement('span');
    label.className = 'mono addr-label';
    label.title = a.address + '\nclick to copy';
    label.textContent = a.address.slice(0,24) + '…';
    label.onclick = () => copy(a.address);

    const bal = document.createElement('span');
    bal.className = 'good addr-bal';
    bal.textContent = fmt(a.balance);

    const tools = document.createElement('span');
    tools.className = 'addr-tools';

    const hideBtn = document.createElement('button');
    hideBtn.className = 'mini';
    hideBtn.textContent = isHidden ? 'unhide' : 'hide';
    hideBtn.title = 'Show or hide this address in this list. The key is not touched.';
    hideBtn.onclick = () => {
      const h = hidden();
      if (h.has(a.address)) h.delete(a.address); else h.add(a.address);
      setHidden(h); refresh();
    };
    tools.appendChild(hideBtn);

    // The default address and any funded address cannot be deleted at all —
    // the wallet refuses. Say so here instead of offering a button that fails.
    const delBtn = document.createElement('button');
    delBtn.className = 'mini danger';
    delBtn.textContent = 'delete key';
    if (isDefault){
      delBtn.disabled = true;
      delBtn.title = 'This is the default address: mining rewards and payment '
        + 'change land here. It cannot be deleted.';
    } else if (funded){
      delBtn.disabled = true;
      delBtn.title = 'Holds ' + fmt(a.balance) + ' — move the coins elsewhere '
        + 'first. Deleting the key would destroy them.';
    } else {
      delBtn.title = 'Permanently destroy this key. Irreversible.';
      delBtn.onclick = () => confirmDelete(a.address);
    }
    tools.appendChild(delBtn);

    row.append(label, bal, tools);
    list.appendChild(row);
  }

  const toggle = $('toggle-hidden');
  if (hiddenCount > 0 || showHidden){
    toggle.hidden = false;
    toggle.textContent = showHidden
      ? 'hide hidden addresses'
      : 'show ' + hiddenCount + ' hidden';
  } else {
    toggle.hidden = true;
  }
}

// Deleting a key is the one irreversible thing here, so it asks for the word
// typed out. A button that only needs a second click is not a decision.
async function confirmDelete(address){
  const typed = window.prompt(
    'PERMANENTLY DELETE this key?\n\n' + address + '\n\n'
    + 'This cannot be undone. If anyone sends coins to this address afterwards, '
    + 'they are lost forever — nobody can recover them.\n\n'
    + 'Type DELETE to confirm:');
  if (typed !== 'DELETE'){
    if (typed !== null) showResult('Not deleted — confirmation did not match.', 'dim');
    return;
  }
  const r = await fetch('/action/deleteaddress', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({address})
  });
  const body = await r.json();
  if (!r.ok){ showResult('Refused: ' + (body.error || 'unknown error'), 'warn'); }
  else {
    // Drop any stale hide entry so a re-created address is not born hidden.
    const h = hidden(); h.delete(address); setHidden(h);
    showResult('Key deleted.', 'dim');
  }
  refresh();
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
$('toggle-hidden').onclick = () => { showHidden = !showHidden; refresh(); };
$('start-mining').onclick = () => act('startmining');
$('stop-mining').onclick  = () => act('stopmining');

// ---- Sending: two stages, and only the second one moves money -------------
let spendable = 0;

function showResult(text, cls){
  const el = $('send-result');
  el.className = 'send-result ' + (cls || '');
  el.textContent = text;
}

function hideConfirm(){
  $('confirm-box').hidden = true;
  const btn = $('confirm-send');
  btn.disabled = false;
  btn.textContent = 'Confirm send';
}

// Stage one. Validates and *reveals* — it never contacts the node. The node
// re-checks all of this anyway; these checks exist to fail fast and to say
// something more useful than the RPC would.
$('review-send').onclick = () => {
  const address = $('send-address').value.trim();
  const amount  = $('send-amount').value.trim();
  showResult('');

  if (!address) return showResult('Enter a recipient address.', 'warn');
  if (!amount || !(Number(amount) > 0))
    return showResult('Enter an amount greater than zero.', 'warn');
  if (Number(amount) > spendable)
    return showResult('Only ' + fmt(spendable) + ' is spendable right now. '
      + 'Mined coins stay immature until they age past the maturity window.', 'warn');

  // Echo the address in full — the whole point of this step.
  $('confirm-amount').textContent = fmt(amount) + ' ' + ($('net').dataset.ticker || '');
  $('confirm-address').textContent = address;
  $('confirm-box').hidden = false;
};

$('cancel-send').onclick = () => { hideConfirm(); showResult('Cancelled.', 'dim'); };

// Stage two. The only path that spends.
$('confirm-send').onclick = async () => {
  const btn = $('confirm-send');
  // Disable immediately: a double-click would try to spend the same UTXOs
  // twice. The node would reject the second, but a clear UI beats a race.
  btn.disabled = true;
  btn.textContent = 'Sending…';

  const address = $('send-address').value.trim();
  const amount  = $('send-amount').value.trim();

  try {
    const r = await fetch('/action/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({address, amount})
    });
    const body = await r.json();
    if (!r.ok) { showResult('Refused: ' + (body.error || 'unknown error'), 'warn');
                 hideConfirm(); return; }
    showResult('Sent ' + fmt(amount) + ' — fee ' + body.fee
               + ', remaining ' + body.remaining + '. txid ' + body.txid, 'good');
    $('send-address').value = '';
    $('send-amount').value = '';
    hideConfirm();
    refresh();
  } catch (e) {
    showResult('Could not reach the node — nothing was sent.', 'warn');
    hideConfirm();
  }
};

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


#: How many recent blocks to sample when estimating network hashrate. Small
#: enough to stay cheap over RPC, large enough that one odd timestamp does not
#: dominate. The sample is re-fetched only when the tip changes.
KPI_WINDOW = 12


def create_app(rpc_port: int, token: str = "", *, network: str = "mainnet") -> Flask:
    app = Flask(__name__)
    params = get_network(network)

    def call(method, *params_):
        return rpc(rpc_port, method, *params_, token=token)

    # Sampling blocks costs two RPC round-trips each, and the page polls every
    # few seconds — but the answer only changes when a block arrives. Key the
    # cache on the tip hash so a quiet chain costs nothing at all.
    block_sample: dict[str, list[dict]] = {}

    def recent_blocks(info: dict) -> list[dict]:
        tip = info.get("tip", "")
        cached = block_sample.get(tip)
        if cached is not None:
            return cached

        height = info.get("height", 0)
        blocks = []
        for h in range(max(0, height - KPI_WINDOW + 1), height + 1):
            try:
                block = call("getblock", call("getblockhash", h))
            except RPCClientError:
                continue  # a reorg mid-walk; sample what we can
            blocks.append({"height": h, "time": block["time"], "bits": block["bits"]})

        block_sample.clear()  # only ever the current tip's sample
        block_sample[tip] = blocks
        return blocks

    @app.get("/")
    def index():
        return render_template_string(
            PAGE,
            coin=f"{COIN_NAME} ({TICKER})",
            halving_interval=params.halving_interval,
        )

    @app.get("/favicon.ico")
    def favicon():
        """The same gem the desktop shortcut wears.

        Browsers ask for this path unprompted, so serving it needs no markup in
        the page and no second copy of the artwork. Absent, the tab shows a
        blank sheet - which is cosmetic, so a missing file is a 404 and not an
        error worth interrupting anyone over.
        """
        icon = Path(__file__).resolve().parent.parent / "assets" / "obsidion.ico"
        if not icon.exists():
            return "", 404
        return send_file(icon, mimetype="image/x-icon")

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
        state["kpis"] = mining_kpis(
            info,
            recent_blocks(info),
            state["wallet"]["coinbase_heights"],
            target_block_time=params.target_block_time,
        )
        return jsonify(state)

    @app.post("/action/newaddress")
    def action_newaddress():
        return jsonify({"address": call("getnewaddress")})

    @app.post("/action/send")
    def action_send():
        """Relay a payment to the node, surfacing its refusal verbatim.

        The page puts a two-stage confirm in front of this, but that is a guard
        against slips, not a security boundary: the node validates the address,
        the amount, and the available funds itself, and is the only thing that
        can actually move coins. Whatever it refuses, we report in its own
        words rather than inventing our own — the node's message names the
        actual problem (wrong network, too fine, not enough mature funds).
        """
        payload = request.get_json(silent=True) or {}
        address = str(payload.get("address", "")).strip()
        amount = str(payload.get("amount", "")).strip()
        if not address or not amount:
            return jsonify({"error": "address and amount are both required"}), 400

        try:
            return jsonify(call("send", address, amount))
        except RPCClientError as exc:
            # A refusal, not a crash: nothing moved, and the user should see why.
            return jsonify({"error": str(exc)}), 400

    @app.post("/action/deleteaddress")
    def action_deleteaddress():
        """Permanently destroy a key, if the wallet allows it.

        Hiding an address is a view preference handled entirely in the browser;
        this is the other thing, and it cannot be undone. Every guard lives in
        the wallet, so this relays the refusal rather than second-guessing it.
        """
        payload = request.get_json(silent=True) or {}
        address = str(payload.get("address", "")).strip()
        if not address:
            return jsonify({"error": "no address given"}), 400
        try:
            return jsonify({"result": call("deleteaddress", address)})
        except RPCClientError as exc:
            return jsonify({"error": str(exc)}), 400

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
        token = read_cookie(args.datadir, args.network)
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
