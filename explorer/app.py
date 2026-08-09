"""The Obsidion block explorer — the chain, visible.

A small Flask application that renders what the node knows: recent blocks, any
block or transaction by hash, any address's holdings, and — front and centre —
the countdown to the next halving.

It talks to the node exclusively over JSON-RPC, exactly as the CLI does. It
holds no database, no keys, and no state of its own, which means it can crash,
restart, or run on another machine without the node caring, and it is
structurally incapable of corrupting the chain.

    python -m explorer.app --network regtest
    → http://127.0.0.1:8080
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, redirect, render_template_string, request

from obsidion.params import COIN_NAME, TICKER, get_network
from obsidion.rpc import read_cookie
from obsidion.rpcclient import rpc  # shared client; RPCClientError is a LookupError


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

LAYOUT = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{% if refresh %}<meta http-equiv="refresh" content="10">{% endif %}
<title>{{ title }} — Obsidion explorer</title>
<style>
  :root {
    --bg: #0d0b12; --panel: #16121f; --line: #2a2338;
    --text: #d8d3e3; --dim: #7a7290; --accent: #9b6dff; --good: #5fd39a;
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--text); min-height: 100vh;
         font: 15px/1.5 system-ui, "Segoe UI", sans-serif; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  header { display: flex; align-items: center; gap: 1rem; padding: 1rem 1.5rem;
           border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  header .logo { font-size: 1.25rem; font-weight: 700; letter-spacing: .04em;
                 color: var(--text); }
  header .logo span { color: var(--accent); }
  header form { margin-left: auto; }
  header input { background: var(--panel); border: 1px solid var(--line);
                 color: var(--text); padding: .5rem .75rem; border-radius: 6px;
                 width: min(26rem, 60vw); font-family: ui-monospace, monospace; }
  main { max-width: 68rem; margin: 0 auto; padding: 1.5rem; }
  h1 { font-size: 1.2rem; margin: 0 0 1rem; }
  h2 { font-size: 1rem; margin: 1.5rem 0 .5rem; color: var(--dim);
       text-transform: uppercase; letter-spacing: .08em; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
           gap: .75rem; margin-bottom: .75rem; }
  .card { background: var(--panel); border: 1px solid var(--line);
          border-radius: 10px; padding: .9rem 1rem; }
  .card .k { color: var(--dim); font-size: .78rem; text-transform: uppercase;
             letter-spacing: .08em; }
  .card .v { font-size: 1.25rem; margin-top: .15rem; font-variant-numeric: tabular-nums; }
  .card.halving { border-color: var(--accent); }
  .card.halving .v { color: var(--accent); }
  table { width: 100%; border-collapse: collapse; background: var(--panel);
          border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: .55rem .9rem; border-bottom: 1px solid var(--line);
           font-variant-numeric: tabular-nums; }
  th { color: var(--dim); font-size: .78rem; text-transform: uppercase;
       letter-spacing: .08em; }
  tr:last-child td { border-bottom: 0; }
  .hash { font-family: ui-monospace, "Cascadia Mono", monospace; font-size: .85rem; }
  .dim { color: var(--dim); }
  .amount { color: var(--good); }
  dl { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
       padding: 1rem; display: grid; grid-template-columns: max-content 1fr;
       gap: .4rem 1.25rem; }
  dt { color: var(--dim); }
  dd { word-break: break-all; font-family: ui-monospace, monospace; font-size: .9rem; }
</style>
</head>
<body>
<header>
  <a class="logo" href="/">◆ <span>Obsidion</span> explorer</a>
  <form action="/search"><input name="q"
    placeholder="height, block hash, txid, or address" /></form>
</header>
<main>{{ body }}</main>
</body>
</html>
"""

DASHBOARD = """
<div class="cards">
  <div class="card"><div class="k">height</div><div class="v">{{ info.height }}</div></div>
  <div class="card"><div class="k">circulating supply</div>
    <div class="v">{{ info.supply }} <span class="dim">/ {{ info.max_supply }}</span></div></div>
  <div class="card"><div class="k">block reward</div>
    <div class="v">{{ info.halving.block_reward }} {{ ticker }}</div></div>
  <div class="card halving"><div class="k">next halving</div>
    <div class="v">{{ halving_text }}</div></div>
  <div class="card"><div class="k">difficulty</div>
    <div class="v">{{ "%.3g"|format(info.difficulty) }}</div></div>
  <div class="card"><div class="k">mempool</div>
    <div class="v">{{ info.mempool_transactions }} tx</div></div>
  <div class="card"><div class="k">peers</div><div class="v">{{ info.peers }}</div></div>
  {% if info.mining %}
  <div class="card"><div class="k">hashrate (this node)</div>
    <div class="v">{{ "%.0f"|format(info.hashrate) }} H/s</div></div>
  {% endif %}
</div>

<h2>Recent blocks</h2>
<table>
  <tr><th>height</th><th>hash</th><th>age</th><th>txs</th><th>size</th></tr>
  {% for b in blocks %}
  <tr>
    <td>{{ b.height }}</td>
    <td class="hash"><a href="/block/{{ b.hash }}">{{ b.hash[:24] }}…</a></td>
    <td class="dim">{{ b.age }}</td>
    <td>{{ b.transactions|length }}</td>
    <td class="dim">{{ b.size }} B</td>
  </tr>
  {% endfor %}
</table>
"""

BLOCK_PAGE = """
<h1>Block {{ block.height }}</h1>
<dl>
  <dt>hash</dt><dd>{{ block.hash }}</dd>
  <dt>previous</dt><dd><a class="hash" href="/block/{{ block.previous }}">{{ block.previous }}</a></dd>
  <dt>time</dt><dd>{{ time }}</dd>
  <dt>confirmations</dt><dd>{{ block.confirmations }}</dd>
  <dt>merkle root</dt><dd>{{ block.merkle_root }}</dd>
  <dt>bits / nonce</dt><dd>{{ block.bits }} / {{ block.nonce }}</dd>
  <dt>size</dt><dd>{{ block.size }} bytes</dd>
</dl>

<h2>{{ block.transactions|length }} transaction(s)</h2>
<table>
  <tr><th>txid</th><th>type</th><th>outputs</th></tr>
  {% for tx in block.transactions %}
  <tr>
    <td class="hash"><a href="/tx/{{ tx.txid }}">{{ tx.txid[:24] }}…</a></td>
    <td>{{ "coinbase" if tx.coinbase else "payment" }}</td>
    <td>
      {% for out in tx.outputs %}
      <span class="amount">{{ out.amount }}</span>
      → <a class="hash" href="/address/{{ out.address }}">{{ out.address[:20] }}…</a><br>
      {% endfor %}
    </td>
  </tr>
  {% endfor %}
</table>
"""

TX_PAGE = """
<h1>Transaction</h1>
<dl>
  <dt>txid</dt><dd>{{ tx.txid }}</dd>
  <dt>status</dt><dd>{% if tx.confirmations %}{{ tx.confirmations }} confirmation(s),
    block <a href="/block/{{ tx.block }}">{{ tx.height }}</a>
    {% else %}in the mempool, unconfirmed{% endif %}</dd>
  <dt>size</dt><dd>{{ tx.size }} bytes</dd>
</dl>

<h2>Inputs</h2>
<table>
  <tr><th>source</th></tr>
  {% for i in tx.inputs %}
  <tr><td class="hash">
    {% if i.coinbase %}newly minted coins (coinbase)
    {% else %}<a href="/tx/{{ i.txid }}">{{ i.txid[:24] }}…</a> : {{ i.index }}{% endif %}
  </td></tr>
  {% endfor %}
</table>

<h2>Outputs</h2>
<table>
  <tr><th>amount</th><th>to</th></tr>
  {% for out in tx.outputs %}
  <tr>
    <td class="amount">{{ out.amount }} {{ ticker }}</td>
    <td class="hash"><a href="/address/{{ out.address }}">{{ out.address }}</a></td>
  </tr>
  {% endfor %}
</table>
"""

ADDRESS_PAGE = """
<h1>Address</h1>
<dl>
  <dt>address</dt><dd>{{ info.address }}</dd>
  <dt>balance</dt><dd><span class="amount">{{ info.balance }} {{ ticker }}</span></dd>
  <dt>unspent outputs</dt><dd>{{ info.utxos|length }}</dd>
</dl>

<h2>Unspent outputs</h2>
<table>
  <tr><th>txid</th><th>amount</th><th>height</th><th>type</th></tr>
  {% for u in info.utxos %}
  <tr>
    <td class="hash"><a href="/tx/{{ u.txid }}">{{ u.txid[:24] }}…</a> : {{ u.index }}</td>
    <td class="amount">{{ u.amount }}</td>
    <td>{{ u.height }}</td>
    <td class="dim">{{ "coinbase" if u.coinbase else "payment" }}</td>
  </tr>
  {% endfor %}
</table>
"""


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------


def _age(timestamp: int) -> str:
    seconds = int(
        datetime.now(timezone.utc).timestamp() - timestamp
    )
    if seconds < 0:
        seconds = 0
    for unit, span in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= span:
            return f"{seconds // span}{unit} ago"
    return f"{seconds}s ago"


def _halving_text(info: dict) -> str:
    remaining = info["halving"]["blocks_remaining"]
    if remaining is None:
        return "complete — all coins minted"
    seconds = info["halving"]["estimated_seconds"]
    if seconds >= 86400:
        estimate = f"≈{seconds / 86400:.1f} days"
    elif seconds >= 3600:
        estimate = f"≈{seconds / 3600:.1f} hours"
    else:
        estimate = f"≈{seconds // 60} min"
    return f"{remaining} blocks ({estimate})"


def create_app(rpc_port: int, token: str = "") -> Flask:
    app = Flask(__name__)

    def call(method, *params):
        """Every RPC the explorer makes, carrying the node's auth token."""
        return rpc(rpc_port, method, *params, token=token)

    def page(title: str, template: str, refresh: bool = False, **context) -> str:
        body = render_template_string(template, ticker=TICKER, **context)
        return render_template_string(
            LAYOUT, title=title, body=body, refresh=refresh
        )

    @app.get("/")
    def dashboard():
        info = call("getinfo")
        blocks = []
        for height in range(info["height"], max(-1, info["height"] - 15), -1):
            block = call("getblock", call("getblockhash", height))
            block["age"] = _age(block["time"])
            blocks.append(block)
        return page(
            "dashboard",
            DASHBOARD,
            refresh=True,
            info=info,
            blocks=blocks,
            halving_text=_halving_text(info),
        )

    @app.get("/block/<hash_hex>")
    def block_page(hash_hex: str):
        try:
            block = call("getblock", hash_hex)
        except LookupError:
            abort(404)
        stamp = datetime.fromtimestamp(block["time"], tz=timezone.utc)
        return page(
            f"block {block['height']}",
            BLOCK_PAGE,
            block=block,
            time=stamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

    @app.get("/tx/<txid>")
    def tx_page(txid: str):
        try:
            tx = call("gettransaction", txid)
        except LookupError:
            abort(404)
        return page(f"tx {txid[:16]}…", TX_PAGE, tx=tx)

    @app.get("/address/<address>")
    def address_page(address: str):
        try:
            info = call("getaddressutxos", address)
        except LookupError:
            abort(404)
        return page(address[:24], ADDRESS_PAGE, info=info)

    @app.get("/search")
    def search():
        query = request.args.get("q", "").strip()
        if query.isdigit():
            try:
                return redirect(
                    f"/block/{call('getblockhash', int(query))}"
                )
            except LookupError:
                abort(404)
        if len(query) == 64:
            try:
                call("getblock", query)
                return redirect(f"/block/{query}")
            except LookupError:
                return redirect(f"/tx/{query}")
        if "1" in query:  # smells like bech32
            return redirect(f"/address/{query}")
        abort(404)

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="obsidion-explorer",
        description=f"Web explorer for a running {COIN_NAME} node.",
    )
    parser.add_argument("--network", default="mainnet")
    parser.add_argument("--rpc-port", type=int, default=None)
    parser.add_argument(
        "--datadir",
        default=str(Path.home() / ".obsidion"),
        help="where the node wrote its RPC token; must match the node's --datadir",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

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

    create_app(rpc_port, token).run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
