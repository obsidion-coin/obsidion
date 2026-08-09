"""obsidion-cli: talk to a running node from the command line.

Thin by design — every command is one JSON-RPC call, and the output is the
JSON the node returned. The node is the source of truth; this is a phone.

    obsidion-cli --network regtest getinfo
    obsidion-cli --network regtest getnewaddress
    obsidion-cli --network regtest send rtobsd1… 12.5
    obsidion-cli --network regtest generate 10
    obsidion-cli --network regtest startmining
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from obsidion.params import get_network
from obsidion.rpc import COOKIE_FILENAME, read_cookie

COMMANDS = {
    "getinfo": "node status, supply, and the halving countdown",
    "getnewaddress": "create a fresh receiving address",
    "getaddresses": "list the wallet's addresses",
    "getbalance": "spendable / immature / total balance",
    "send": "send <address> <amount> — pay someone",
    "startmining": "startmining [address] — begin mining",
    "stopmining": "stop mining",
    "generate": "generate <n> [address] — mine n blocks (regtest only)",
    "getblockhash": "getblockhash <height>",
    "getblock": "getblock <hash>",
    "gettransaction": "gettransaction <txid>",
    "getpeerinfo": "connected peers",
    "getmempoolinfo": "waiting transactions",
}


def rpc_call(port: int, method: str, params: list, datadir: str) -> object:
    try:
        token = read_cookie(datadir)
    except FileNotFoundError:
        raise SystemExit(
            f"no RPC token found in {datadir}. A running node writes one to "
            f"{COOKIE_FILENAME} on start-up — check obsidion-node is running "
            "and that --datadir matches the one it was given."
        ) from None

    request = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                data=request,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            ),
            timeout=60,
        ) as response:
            reply = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"node refused the request (HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"could not reach a node on RPC port {port} ({exc.reason}); "
            "is obsidion-node running?"
        ) from exc

    if reply.get("error"):
        error = reply["error"]
        raise SystemExit(f"node refused: {error['message']} (code {error['code']})")
    return reply["result"]


def _coerce(argument: str):
    """Ints travel as ints; everything else (addresses, hashes, decimal
    amounts) stays a string — amounts especially must never become floats."""
    try:
        return int(argument)
    except ValueError:
        return argument


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="obsidion-cli",
        description="Send commands to a running Obsidion node.",
        epilog="commands:\n"
        + "\n".join(f"  {name:16} {help}" for name, help in COMMANDS.items()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--network", default="mainnet")
    parser.add_argument("--rpc-port", type=int, default=None)
    parser.add_argument(
        "--datadir",
        default=str(Path.home() / ".obsidion"),
        help="where the node wrote its RPC token; must match the node's --datadir",
    )
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("args", nargs="*", help="command arguments")
    args = parser.parse_args(argv)

    port = (
        args.rpc_port
        if args.rpc_port is not None
        else get_network(args.network).default_rpc_port
    )

    result = rpc_call(
        port, args.command, [_coerce(a) for a in args.args], args.datadir
    )
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
