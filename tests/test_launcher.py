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


def test_the_wallet_lives_beside_the_chain_and_is_named_for_its_network(tmp_path):
    """Two networks must never share a wallet file: a testnet wallet opened as
    mainnet is refused by the wallet's own network binding, and the error would
    be baffling to a beginner."""
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

    monkeypatch.setattr(desktop_launcher, "read_cookie", lambda datadir: "token")
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
