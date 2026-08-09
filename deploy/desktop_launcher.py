"""One double-click: start the node, open the wallet HUD, no terminal needed.

Obsidion normally wants two terminals — one running the node, one running the
HUD — which is a wall for anyone who does not use a shell. This is the on-ramp:
a single entry point behind a desktop icon that brings up everything and opens
the browser on the dashboard.

    python deploy/desktop_launcher.py            # mainnet
    python deploy/desktop_launcher.py regtest    # a private practice chain

Run `deploy/install-hud-shortcut.ps1` once to put the icon on the desktop.

**Why the console window stays open.** It *is* the node. Two things force it to
be visible rather than hidden in the background: the wallet is encrypted, so the
node asks for its password on the console at startup; and closing that window is
how you stop the node. If it were hidden, there would be nowhere to type the
password and no obvious way to stop mining.

The HUD needs no password of its own — it authenticates to the node with the
token the node writes to `.rpccookie` in the data directory. That is also why
the HUD cannot start first: the cookie does not exist until the node is up. So
the launcher waits for the node's RPC to actually answer before starting the
HUD, rather than sleeping a hopeful few seconds.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from obsidion.params import get_network  # noqa: E402
from obsidion.rpc import read_cookie  # noqa: E402
from obsidion.rpcclient import rpc  # noqa: E402

#: Where the HUD listens. Matches hud/app.py's default so the two agree.
HUD_PORT = 8081

#: How long to wait for the node to answer before giving up on the HUD. Long
#: enough to cover a slow first start and someone typing a password carefully.
NODE_WAIT_SECONDS = 300


def node_argv(network: str, datadir: Path) -> list[str]:
    """The arguments the node is started with on behalf of a first-time user.

    `--create-wallet` because someone opening this for the first time has no
    wallet and should be walked through making one rather than shown an error.
    `--mine` because the HUD's whole mining half is meaningless otherwise.

    Deliberately no `--connect`: a downloader finds the network through the
    seed baked into `params.py`. Pointing at a specific peer is a workaround for
    one machine's NAT, not something to ship to strangers.
    """
    return [
        "--network", network,
        "--datadir", str(datadir),
        "--wallet", str(datadir / f"{network}.wallet"),
        "--create-wallet",
        "--host", "127.0.0.1",
        "--mine",
    ]


def hud_url(port: int = HUD_PORT) -> str:
    return f"http://127.0.0.1:{port}"


def wait_for_node(rpc_port: int, datadir: Path, timeout: float) -> bool:
    """Block until the node's RPC answers, or the timeout expires.

    Polls rather than sleeping a fixed interval because the wait is genuinely
    unpredictable: the user has to type a password, and a cold start reads the
    chain from disk. Both the cookie file and a successful `getinfo` are
    required — the cookie can exist a moment before the server is listening.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rpc(rpc_port, "getinfo", token=read_cookie(str(datadir)))
            return True
        except Exception:  # noqa: BLE001 — every failure here means "not yet"
            time.sleep(0.5)
    return False


def _serve_hud(rpc_port: int, datadir: Path, network: str) -> None:
    """Start the HUD once the node is answering, then open a browser at it."""
    from hud.app import create_app

    if not wait_for_node(rpc_port, datadir, NODE_WAIT_SECONDS):
        print(
            f"\n  The node did not start within {NODE_WAIT_SECONDS // 60} minutes, "
            f"so the wallet HUD was not opened.\n"
            f"  The node itself may still be fine - check the messages above.\n"
        )
        return

    app = create_app(rpc_port, read_cookie(str(datadir)), network=network)
    # Printed text stays ASCII: this lands in a Windows console, whose default
    # code page turns a UTF-8 em dash into a replacement character.
    print(f"\n  Wallet HUD ready at {hud_url()} - opening your browser.")
    print("  Keep this window open; closing it stops the node and the HUD.\n")

    threading.Thread(
        target=lambda: app.run(
            host="127.0.0.1", port=HUD_PORT, use_reloader=False
        ),
        name="obsidion-hud",
        daemon=True,
    ).start()

    # Give the HUD a breath to bind before the browser asks for the page, so a
    # first-time user does not meet a connection error and assume it is broken.
    time.sleep(1.0)
    try:
        webbrowser.open(hud_url())
    except Exception:  # noqa: BLE001 — a headless box is not a failure
        print(f"  Could not open a browser automatically. Visit {hud_url()}")


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    network = args[0] if args and not args[0].startswith("-") else "mainnet"
    params = get_network(network)
    datadir = Path.home() / ".obsidion"
    datadir.mkdir(parents=True, exist_ok=True)

    print(f"  Obsidion - starting the {network} node and wallet HUD.")
    print("  If this is your first run you will be asked to choose a wallet")
    print("  password. Write it down: there is no way to recover it.\n")

    # The HUD waits in the background; the node owns the console so its password
    # prompt is visible and Ctrl+C / closing the window stops everything.
    threading.Thread(
        target=_serve_hud,
        args=(params.default_rpc_port, datadir, network),
        name="obsidion-hud-launcher",
        daemon=True,
    ).start()

    from obsidion.node import main as node_main

    node_main(node_argv(network, datadir))


if __name__ == "__main__":
    main()
