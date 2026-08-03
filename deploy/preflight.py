"""Check a seed node from the outside, the way a stranger's software will.

Run this from a machine that is *not* the server — your laptop, ideally on a
different network — before announcing anything:

    python deploy/preflight.py 203.0.113.10:9444
    python deploy/preflight.py 203.0.113.10:9444 --network testnet

It speaks the real protocol rather than just opening a socket, so it answers
the questions that actually matter and that you cannot answer from the server
itself:

  1. Is the port reachable from the public internet?  (firewalls, NAT, the
     node bound to 127.0.0.1 instead of 0.0.0.0)
  2. Does it speak this network?                      (wrong --network flag)
  3. Is it on the same chain you are?                 (it serves a genesis
                                                       block, and the hash is
                                                       compared to yours)
  4. How much chain does it have?

A seed that fails any of these is a seed nobody can join through, and you will
not find out from the announcement thread — you will find out from silence.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from obsidion.block import Block  # noqa: E402
from obsidion.genesis import genesis_hash  # noqa: E402
from obsidion.p2p import (  # noqa: E402
    INV_BLOCK,
    pack_inv,
    pack_message,
    pack_version,
    read_message,
    unpack_version,
)
from obsidion.params import get_network  # noqa: E402

OK = "  ok   "
BAD = " FAIL  "


async def probe(host: str, port: int, params, timeout: float) -> bool:
    expected_genesis = genesis_hash(params)
    healthy = True

    print(f"\nProbing {host}:{port} as a {params.name} peer\n")

    # ---- 1. reachable ----------------------------------------------------
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
    except (OSError, asyncio.TimeoutError) as exc:
        print(f"{BAD} cannot reach {host}:{port} — {exc}")
        print("\n       Check, in order:")
        print("         * the node is running          systemctl status obsidion")
        print("         * it listens on all interfaces --host 0.0.0.0")
        print("         * the firewall allows the port ufw allow 9444/tcp")
        print("         * the provider's own firewall / security group")
        return False
    print(f"{OK} reachable")

    try:
        # ---- 2. speaks this network -------------------------------------
        writer.write(
            pack_message(
                params.magic, "version", pack_version(0, 0, secrets.randbits(64))
            )
        )
        await writer.drain()

        peer_height = None
        agent = "?"
        deadline = asyncio.get_event_loop().time() + timeout
        max_payload = params.max_block_weight + 4096

        while peer_height is None:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            command, payload = await asyncio.wait_for(
                read_message(reader, params.magic, max_payload), remaining
            )
            if command == "version":
                _, peer_height, _, _, agent = unpack_version(payload)

        print(f"{OK} speaks {params.name} — agent {agent}, height {peer_height}")

        writer.write(pack_message(params.magic, "verack"))
        await writer.drain()

        # ---- 3. same chain ----------------------------------------------
        writer.write(
            pack_message(
                params.magic, "getdata", pack_inv([(INV_BLOCK, expected_genesis)])
            )
        )
        await writer.drain()

        deadline = asyncio.get_event_loop().time() + timeout
        served = None
        while served is None:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            command, payload = await asyncio.wait_for(
                read_message(reader, params.magic, max_payload), remaining
            )
            if command == "block":
                served = Block.deserialize(payload)

        if served.hash() == expected_genesis:
            print(f"{OK} same chain — genesis {served.hash_hex()}")
        else:
            healthy = False
            print(f"{BAD} DIFFERENT CHAIN")
            print(f"       it serves  {served.hash_hex()}")
            print(f"       you expect {expected_genesis[::-1].hex()}")
            print("\n       The server is running different parameters or a")
            print("       different build. Nobody using your published code can")
            print("       sync with it. Redeploy from the published commit.")

        # ---- 4. has chain to offer --------------------------------------
        if peer_height == 0:
            print(f"{OK} height 0 — correct before launch, wrong after")
        else:
            print(f"{OK} serving {peer_height} blocks")

    except asyncio.TimeoutError:
        healthy = False
        print(f"{BAD} connected, but the node stopped responding")
        print("       It accepted the connection and then went quiet — usually a")
        print("       node wedged or still starting up. Check journalctl.")
    except Exception as exc:  # noqa: BLE001 — a diagnostic tool reports, never crashes
        healthy = False
        print(f"{BAD} protocol error: {type(exc).__name__}: {exc}")
    finally:
        writer.close()

    return healthy


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="preflight",
        description="Verify an Obsidion seed node is joinable from outside.",
    )
    parser.add_argument("address", nargs="+", metavar="HOST:PORT")
    parser.add_argument("--network", default="mainnet")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    params = get_network(args.network)

    results = []
    for spec in args.address:
        host, _, port = spec.rpartition(":")
        if not port.isdigit():
            raise SystemExit(f"malformed address {spec!r}; expected host:port")
        results.append(asyncio.run(probe(host, int(port), params, args.timeout)))

    print()
    if all(results) and len(results) >= 2:
        print("All seeds healthy. Safe to publish these addresses.")
    elif all(results):
        print("Seed healthy. Run at least two, on different providers —")
        print("a network whose only seed is offline cannot be joined.")
        raise SystemExit(1)
    else:
        print(f"{sum(results)}/{len(results)} healthy. Do not announce yet.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
