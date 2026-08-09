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

import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from obsidion.params import get_network  # noqa: E402
from obsidion.rpc import read_cookie  # noqa: E402
from obsidion.rpcclient import rpc  # noqa: E402

#: Where the HUD listens. Matches hud/app.py's default so the two agree.
HUD_PORT = 8081

#: How long to wait for the node to answer before giving up on the HUD. Long
#: enough to cover a slow first start and someone typing a password carefully.
NODE_WAIT_SECONDS = 300


def find_wallet(network: str, datadir: Path) -> Path:
    """The wallet to open, preferring one that already exists.

    This must never quietly start a *second* wallet for someone who already has
    one. A new wallet means a new address, so mining would pay into keys the
    owner does not think of as theirs while their real balance sits untouched
    somewhere else — and they would only notice when the numbers stopped
    matching.

    Existing wallets are commonly in the project folder rather than the data
    directory, because the documented commands say `--wallet mainnet.wallet`
    and that resolves against whatever directory you ran them from. So look
    there too before concluding this is a first run.
    """
    name = f"{network}.wallet"
    for candidate in (datadir / name, REPO_ROOT / name):
        if candidate.exists():
            return candidate
    return datadir / name


def node_argv(network: str, datadir: Path) -> list[str]:
    """The arguments the node is started with on behalf of a first-time user.

    `--create-wallet` because someone opening this for the first time has no
    wallet and should be walked through making one rather than shown an error.
    It is harmless when a wallet already exists — the node opens the existing
    file and only creates one when the path is empty — but `find_wallet` is
    what makes sure the path points at the wallet they already own.
    `--mine` because the HUD's whole mining half is meaningless otherwise.

    Deliberately no `--connect`: a downloader finds the network through the
    seed baked into `params.py`. Pointing at a specific peer is a workaround for
    one machine's NAT, not something to ship to strangers.
    """
    return [
        "--network", network,
        "--datadir", str(datadir),
        "--wallet", str(find_wallet(network, datadir)),
        "--create-wallet",
        "--host", "127.0.0.1",
        "--mine",
    ]


def launcher_datadir(network: str) -> Path:
    """Where this network keeps its chain, wallet and token.

    Mainnet uses the node's own default so the icon and a hand-typed
    `obsidion-node` command share one wallet rather than quietly maintaining
    two. Every other network gets its own subdirectory: a practice chain must
    never be able to touch mainnet's files, and running both at once from one
    directory is exactly how a regtest node came to overwrite the live node's
    RPC token.
    """
    base = Path.home() / ".obsidion"
    return base if network == "mainnet" else base / network


def hud_url(port: int = HUD_PORT) -> str:
    return f"http://127.0.0.1:{port}"


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something is already listening there.

    Checked before starting anything, because the alternative is what a real
    user met: a node already running in another window, and the launcher
    exiting on a forty-line OSError traceback about socket addresses. The
    person this icon exists for cannot read that.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def wait_for_node(
    rpc_port: int, datadir: Path, timeout: float, network: str | None = None
) -> bool:
    """Block until the node's RPC answers, or the timeout expires.

    Polls rather than sleeping a fixed interval because the wait is genuinely
    unpredictable: the user has to type a password, and a cold start reads the
    chain from disk. Both the cookie file and a successful `getinfo` are
    required — the cookie can exist a moment before the server is listening.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rpc(rpc_port, "getinfo", token=read_cookie(str(datadir), network))
            return True
        except Exception:  # noqa: BLE001 — every failure here means "not yet"
            time.sleep(0.5)
    return False


def _serve_hud(rpc_port: int, datadir: Path, network: str) -> None:
    """Start the HUD once the node is answering, then open a browser at it."""
    from hud.app import create_app

    if not wait_for_node(rpc_port, datadir, NODE_WAIT_SECONDS, network):
        print(
            f"\n  The node did not start within {NODE_WAIT_SECONDS // 60} minutes, "
            f"so the wallet HUD was not opened.\n"
            f"  The node itself may still be fine - check the messages above.\n"
        )
        return

    app = create_app(rpc_port, read_cookie(str(datadir), network), network=network)
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
    datadir = launcher_datadir(network)
    datadir.mkdir(parents=True, exist_ok=True)

    # A node already running - a terminal someone forgot about, or the icon
    # double-clicked twice - must not end in a bind traceback. Attach to it:
    # the person wanted their wallet on screen, and it is the same wallet
    # whichever process serves it.
    if port_in_use(params.default_port) or port_in_use(params.default_rpc_port):
        print(f"  An Obsidion {network} node is already running on this machine.")
        if port_in_use(HUD_PORT):
            print(f"  The wallet HUD is already up too - opening it.\n")
            webbrowser.open(hud_url())
            input("  Press Enter to close this window. The node keeps running.")
            return
        print("  Using it: opening the wallet HUD against the running node.")
        print("  Closing THIS window will not stop that node.\n")
        _serve_hud(params.default_rpc_port, datadir, network)
        try:
            input("  Press Enter to close this window and its HUD. "
                  "The node keeps running.")
        except EOFError:
            pass
        return

    wallet = find_wallet(network, datadir)
    print(f"  Obsidion - starting the {network} node and wallet HUD.")
    print(f"  Wallet: {wallet}")
    if wallet.exists():
        # Naming the file matters: the alternative is someone typing a password
        # into what they assume is their wallet and quietly opening another.
        print("  Enter its password when asked below.\n")
    else:
        print("  No wallet found here, so a new one will be created and you")
        print("  will choose a password. Write it down: there is no recovery.")
        print("  If you already have a wallet, close this window and check")
        print("  the path above - opening the wrong one gives you a different")
        print("  address and your balance will appear to be missing.\n")

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
