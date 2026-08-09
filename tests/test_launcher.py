"""Tests for the one-click desktop launcher.

Most of the launcher is unavoidable glue — threads, sockets, a browser — and is
verified by running it. What *is* worth pinning is the argument list it hands
the node on a stranger's behalf, because every mistake there lands on someone
who has no terminal to diagnose it with.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))

from desktop_launcher import HUD_PORT, hud_url, node_argv, wait_for_node  # noqa: E402


def _pairs(argv: list[str]) -> dict[str, str]:
    """Flatten ['--a', '1', '--flag'] into {'--a': '1', '--flag': ''}."""
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        key = argv[i]
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            out[key] = argv[i + 1]
            i += 2
        else:
            out[key] = ""
            i += 1
    return out


def test_the_node_is_started_with_what_a_first_timer_needs(tmp_path):
    args = _pairs(node_argv("mainnet", tmp_path))

    assert args["--network"] == "mainnet"
    assert args["--datadir"] == str(tmp_path)
    # A first run has no wallet, so it must offer to create one rather than
    # failing at someone who cannot read a stack trace.
    assert "--create-wallet" in args
    # The mining half of the HUD is meaningless if the node is not mining.
    assert "--mine" in args
    # Outbound-only: a home machine must not start accepting strangers.
    assert args["--host"] == "127.0.0.1"


def test_the_wallet_lives_beside_the_chain_and_is_named_for_its_network(
    tmp_path, monkeypatch
):
    """Two networks must never share a wallet file: a testnet wallet opened as
    mainnet is refused by the wallet's own network binding, and the error would
    be baffling to a beginner.

    REPO_ROOT is pointed at an empty directory so this tests the fresh-install
    naming, not whichever wallets happen to exist in the developer's checkout.
    """
    import desktop_launcher

    empty_repo = tmp_path / "repo"
    empty_repo.mkdir()
    monkeypatch.setattr(desktop_launcher, "REPO_ROOT", empty_repo)

    mainnet = _pairs(node_argv("mainnet", tmp_path))["--wallet"]
    regtest = _pairs(node_argv("regtest", tmp_path))["--wallet"]

    assert mainnet == str(tmp_path / "mainnet.wallet")
    assert regtest == str(tmp_path / "regtest.wallet")
    assert mainnet != regtest


def test_the_launcher_never_hardcodes_a_peer(tmp_path):
    """--connect was one machine's NAT workaround. Shipping it to strangers
    would point every new node at a LAN address that does not exist for them;
    they must bootstrap through the seed baked into params.py."""
    assert "--connect" not in node_argv("mainnet", tmp_path)


def test_the_hud_url_is_loopback_only():
    """The HUD shows balances and reaches a spend-capable token. If this ever
    renders as a routable host, that is a leak, not a convenience."""
    assert hud_url() == f"http://127.0.0.1:{HUD_PORT}"
    assert hud_url(9999) == "http://127.0.0.1:9999"
    for url in (hud_url(), hud_url(9999)):
        assert "0.0.0.0" not in url and "localhost" not in url


def test_waiting_for_a_node_that_never_arrives_gives_up(tmp_path):
    """The wait must be bounded. A node that fails to start should leave the
    user with the node's own error on screen, not a launcher hung forever."""
    # Nothing is listening on this port and no cookie exists in tmp_path.
    assert wait_for_node(9, tmp_path, timeout=0.6) is False


def test_waiting_returns_as_soon_as_the_node_answers(tmp_path, monkeypatch):
    """And it must not sleep a fixed interval when the node is already up."""
    import desktop_launcher

    monkeypatch.setattr(
        desktop_launcher, "read_cookie", lambda datadir, network=None: "token"
    )
    monkeypatch.setattr(desktop_launcher, "rpc", lambda *a, **k: {"height": 0})

    assert wait_for_node(9445, tmp_path, timeout=5) is True


def test_everything_printed_to_the_console_is_ascii():
    """This text lands in a Windows console, not a UTF-8 terminal.

    The default code page turns a UTF-8 em dash into a replacement character,
    so the very first thing a non-technical user saw was
    "Obsidion ? starting the mainnet node". Comments are free to use whatever
    they like; anything inside a print() must survive cp1252.
    """
    import ast

    source = (Path(__file__).resolve().parent.parent
              / "deploy" / "desktop_launcher.py").read_text(encoding="utf-8")

    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        for literal in ast.walk(node):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                bad = sorted({c for c in literal.value if ord(c) > 127})
                if bad:
                    offenders.append((literal.lineno, bad))

    assert not offenders, f"non-ASCII in printed strings: {offenders}"


def test_a_practice_chain_never_shares_mainnets_data_directory():
    """regtest must not be able to touch mainnet's files.

    Real incident: the launcher ran regtest in ~/.obsidion, the same directory
    the live mainnet node was using, and the regtest node's RPC token
    overwrote mainnet's. The mainnet node kept relaying while every client was
    locked out with a 401. Namespaced cookies fixed the symptom; this keeps the
    two chains' files apart in the first place.

    Mainnet deliberately keeps the node's own default so the desktop icon and a
    hand-typed obsidion-node command share one wallet.
    """
    from desktop_launcher import launcher_datadir

    mainnet = launcher_datadir("mainnet")
    assert mainnet == Path.home() / ".obsidion"

    for other in ("regtest", "testnet"):
        assert launcher_datadir(other) != mainnet
        assert mainnet in launcher_datadir(other).parents

    # And the wallet paths that follow from them cannot collide either.
    wallets = {
        n: _pairs(node_argv(n, launcher_datadir(n)))["--wallet"]
        for n in ("mainnet", "testnet", "regtest")
    }
    assert len(set(wallets.values())) == 3, wallets


def test_an_existing_wallet_is_opened_rather_than_a_second_one_created(tmp_path):
    """The launcher must never quietly start a second wallet.

    A new wallet means a new address, so mining would pay into keys the owner
    does not think of as theirs while their real balance sits untouched
    elsewhere - noticed only when the numbers stop matching. Reported for real:
    the icon offered to create a wallet for someone who already had one, whose
    file was in the project folder rather than the data directory.
    """
    from desktop_launcher import find_wallet

    # Data directory wins when the wallet is there.
    (tmp_path / "mainnet.wallet").write_text("x")
    assert find_wallet("mainnet", tmp_path) == tmp_path / "mainnet.wallet"


def test_a_wallet_in_the_project_folder_is_found(tmp_path, monkeypatch):
    """Existing wallets usually live beside the code, because the documented
    command is `--wallet mainnet.wallet` run from that directory."""
    import desktop_launcher

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mainnet.wallet").write_text("x")
    monkeypatch.setattr(desktop_launcher, "REPO_ROOT", repo)

    empty_datadir = tmp_path / "data"
    empty_datadir.mkdir()

    found = desktop_launcher.find_wallet("mainnet", empty_datadir)
    assert found == repo / "mainnet.wallet"
    # And the node is told to use it, not the empty data directory path.
    assert _pairs(desktop_launcher.node_argv("mainnet", empty_datadir))["--wallet"] \
        == str(repo / "mainnet.wallet")


def test_a_genuinely_first_run_still_creates_one_in_the_data_directory(tmp_path,
                                                                       monkeypatch):
    import desktop_launcher

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(desktop_launcher, "REPO_ROOT", repo)

    assert desktop_launcher.find_wallet("mainnet", tmp_path) == tmp_path / "mainnet.wallet"
    assert "--create-wallet" in desktop_launcher.node_argv("mainnet", tmp_path)
