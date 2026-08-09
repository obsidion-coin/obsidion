"""Tests for the full node stack: daemon + RPC server, driven over real HTTP.

These are the closest thing to a user: every call here goes through the same
socket, JSON, and thread boundaries that obsidion-cli uses.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from obsidion.node import ObsidionNode
from obsidion.params import REGTEST
from obsidion.rpc import RPCServer
from obsidion.wallet import Wallet


@pytest.fixture
def stack(tmp_path):
    """A started node with a wallet and an RPC server, torn down afterwards."""
    wallet = Wallet.create(tmp_path / "node.wallet", "pw", REGTEST)
    node = ObsidionNode(REGTEST, wallet=wallet)
    rpc = RPCServer(node, datadir=tmp_path)
    node.start()
    rpc.start()
    yield node, rpc
    rpc.stop()
    node.stop()


def call(rpc: RPCServer, method: str, *params, token=None, headers=None):
    """Make an RPC request. Defaults to a well-formed, authenticated one;
    `token` and `headers` let a test misbehave deliberately."""
    request = json.dumps({"method": method, "params": list(params), "id": 7}).encode()
    sent = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {rpc.token if token is None else token}",
    }
    sent.update(headers or {})
    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{rpc.port}/", data=request, headers=sent
        ),
        timeout=30,
    ) as response:
        return json.loads(response.read())


def status_of(rpc: RPCServer, method: str, *params, token=None, headers=None) -> int:
    """The HTTP status a request comes back with, for the rejection paths.

    Retries once on a connection abort. Refusing a request means writing a
    short response and closing, and on Windows under load the close can reach
    the client before the body does — surfacing as ConnectionAbortedError
    rather than the HTTPError carrying the status. That is a race in this
    client, not in the server, and retrying distinguishes the two: a real
    failure to refuse fails on the second attempt as well.
    """
    for attempt in (1, 2):
        try:
            call(rpc, method, *params, token=token, headers=headers)
            return 200
        except urllib.error.HTTPError as exc:
            return exc.code
        except (ConnectionAbortedError, ConnectionResetError):
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def result(rpc, method, *params):
    reply = call(rpc, method, *params)
    assert reply["error"] is None, f"{method} failed: {reply['error']}"
    return reply["result"]


# --------------------------------------------------------------------------


def test_getinfo_reports_a_fresh_regtest_chain(stack):
    node, rpc = stack
    info = result(rpc, "getinfo")

    from obsidion import consensus
    from obsidion.rpc import format_amount

    assert info["network"] == "regtest"
    assert info["height"] == 0
    assert info["supply"] == "50"
    # Regtest's cap is ~1,000 OBSD (10-block halvings), not mainnet's 21M —
    # the assertion follows the network's own math rather than assuming.
    assert info["max_supply"] == format_amount(consensus.total_supply(REGTEST))
    assert info["halving"]["block_reward"] == "50"
    assert info["halving"]["next_halving_height"] == 10
    assert info["peers"] == 0
    assert info["mining"] is False


def test_generate_mines_real_blocks(stack):
    node, rpc = stack
    hashes = result(rpc, "generate", 3)

    assert len(hashes) == 3
    assert result(rpc, "getinfo")["height"] == 3
    # And the freshly mined rewards show up, still maturing.
    balance = result(rpc, "getbalance")
    assert balance["immature"] != "0"


def test_the_wallet_grows_addresses_over_rpc(stack):
    node, rpc = stack
    fresh = result(rpc, "getnewaddress")

    assert fresh.startswith("rtobsd1")
    assert fresh in result(rpc, "getaddresses")


def test_a_payment_travels_end_to_end_over_rpc(stack):
    """generate → send → mempool → mine → confirmed, all through the socket."""
    node, rpc = stack
    result(rpc, "generate", 1 + REGTEST.coinbase_maturity)

    # A recipient with their own wallet (no node needed to receive).
    recipient_wallet = Wallet(REGTEST, [b"\x11" * 32])
    recipient = recipient_wallet.addresses()[0]

    sent = result(rpc, "send", recipient, "12.5")
    assert sent["fee"] != "0"

    pool = result(rpc, "getmempoolinfo")
    assert sent["txid"] in pool["txids"]

    result(rpc, "generate", 1)
    confirmed = result(rpc, "gettransaction", sent["txid"])
    assert confirmed["confirmations"] == 1
    amounts = [output["amount"] for output in confirmed["outputs"]]
    assert "12.5" in amounts

    with node.lock:
        received = recipient_wallet.balance(node.chain)
    assert received.spendable == 12_5000_0000  # 12.5 OBSD in shards


def test_sending_more_than_the_balance_is_refused_cleanly(stack):
    node, rpc = stack
    result(rpc, "generate", 1 + REGTEST.coinbase_maturity)

    recipient = Wallet(REGTEST, [b"\x22" * 32]).addresses()[0]
    reply = call(rpc, "send", recipient, "9999")

    assert reply["result"] is None
    assert "insufficient funds" in reply["error"]["message"]


def test_amounts_finer_than_a_shard_are_refused(stack):
    node, rpc = stack
    recipient = Wallet(REGTEST, [b"\x33" * 32]).addresses()[0]
    reply = call(rpc, "send", recipient, "0.000000001")  # nine decimals

    assert reply["error"] is not None
    assert "finer than one shard" in reply["error"]["message"]


def test_blocks_are_inspectable_by_height_and_hash(stack):
    node, rpc = stack
    result(rpc, "generate", 2)

    block_hash = result(rpc, "getblockhash", 2)
    block = result(rpc, "getblock", block_hash)

    assert block["height"] == 2
    assert block["confirmations"] == 1
    assert block["transactions"][0]["coinbase"] is True
    assert block["previous"] == result(rpc, "getblockhash", 1)


def test_mining_can_be_started_and_stopped_over_rpc(stack):
    node, rpc = stack
    assert result(rpc, "startmining") == "mining started"
    assert result(rpc, "getinfo")["mining"] is True

    assert result(rpc, "stopmining") == "mining stopped"
    assert result(rpc, "getinfo")["mining"] is False


# --------------------------------------------------------------------------
# Access control
#
# This endpoint spends money. Everything below is a regression test for a real
# vulnerability: the server originally had no authentication at all, and a web
# page the operator merely visited could POST to 127.0.0.1 and drain the
# wallet. Localhost is not a boundary against a browser.
# --------------------------------------------------------------------------


def test_a_request_without_a_token_is_refused(stack):
    node, rpc = stack
    assert status_of(rpc, "getinfo", token="") == 401


def test_a_request_with_the_wrong_token_is_refused(stack):
    node, rpc = stack
    assert status_of(rpc, "getinfo", token="0" * 64) == 401


def test_an_unauthenticated_request_cannot_move_money(stack):
    """The attack itself: no token, and the wallet must be untouched after."""
    node, rpc = stack
    result(rpc, "generate", 1 + REGTEST.coinbase_maturity)
    before = result(rpc, "getbalance")["spendable"]

    recipient = Wallet(REGTEST, [b"\x55" * 32]).addresses()[0]
    assert status_of(rpc, "send", recipient, "10", token="") == 401

    assert result(rpc, "getbalance")["spendable"] == before


def test_a_browser_request_is_refused_even_with_a_valid_token(stack):
    """The defence that does not depend on the token staying secret.

    Browsers attach Origin to cross-origin requests and cannot suppress it.
    A command-line client never sends one. Refusing anything that carries it
    blocks the web-page attack outright.
    """
    node, rpc = stack
    assert (
        status_of(rpc, "getinfo", headers={"Origin": "https://evil.example"}) == 403
    )
    assert (
        status_of(rpc, "getinfo", headers={"Referer": "https://evil.example/x"}) == 403
    )


def test_a_browser_simple_request_content_type_is_refused(stack):
    """text/plain is what a page uses to dodge the CORS preflight. It must not
    be a way in, token or no token."""
    node, rpc = stack
    assert (
        status_of(rpc, "getinfo", headers={"Content-Type": "text/plain"}) == 415
    )


def test_the_cookie_file_is_written_and_matches_the_server(tmp_path):
    from obsidion.rpc import cookie_filename, read_cookie

    wallet = Wallet.create(tmp_path / "c.wallet", "pw", REGTEST)
    node = ObsidionNode(REGTEST, wallet=wallet)
    rpc = RPCServer(node, datadir=tmp_path)
    rpc.start()  # the token is published on start, not construction
    try:
        assert read_cookie(tmp_path, "regtest") == rpc.token
        assert len(rpc.token) == 64
        # Named for its network, so a node on another chain cannot overwrite it.
        assert (tmp_path / cookie_filename("regtest")).exists()
    finally:
        rpc.stop()
        node.stop()

    # Stopping removes it, so nothing stale is left implying it still works.
    assert not (tmp_path / cookie_filename("regtest")).exists()


def test_two_nodes_get_different_tokens(tmp_path):
    """A token is per-run, so one leaking never unlocks another node."""
    tokens = []
    for name in ("a", "b"):
        directory = tmp_path / name
        directory.mkdir()
        wallet = Wallet.create(directory / "w.wallet", "pw", REGTEST)
        node = ObsidionNode(REGTEST, wallet=wallet)
        rpc = RPCServer(node, datadir=directory)
        tokens.append(rpc.token)
        rpc.stop()
        node.stop()

    assert tokens[0] != tokens[1]


def test_binding_the_wallet_rpc_off_loopback_is_refused(tmp_path):
    """Exposing this port to a network exposes the wallet. It should take more
    than a typo."""
    wallet = Wallet.create(tmp_path / "b.wallet", "pw", REGTEST)
    node = ObsidionNode(REGTEST, wallet=wallet)
    try:
        with pytest.raises(ValueError, match="refusing to bind"):
            RPCServer(node, host="0.0.0.0", datadir=tmp_path)
    finally:
        node.stop()


def test_binding_off_loopback_is_possible_when_explicitly_demanded(tmp_path):
    wallet = Wallet.create(tmp_path / "d.wallet", "pw", REGTEST)
    node = ObsidionNode(REGTEST, wallet=wallet)
    rpc = None
    try:
        rpc = RPCServer(node, host="0.0.0.0", datadir=tmp_path, allow_remote=True)
        assert rpc.port > 0
    finally:
        if rpc is not None:
            rpc.stop()
        node.stop()


def test_unknown_methods_are_refused_not_crashed(stack):
    node, rpc = stack
    reply = call(rpc, "definitely_not_a_method")
    assert reply["error"]["code"] == -32601


def test_the_halving_countdown_moves_with_the_chain(stack):
    """Mine across a regtest halving boundary and watch the reward drop —
    the headline mechanic, observable over the same RPC the explorer uses."""
    node, rpc = stack
    result(rpc, "generate", 9)
    before = result(rpc, "getinfo")["halving"]
    assert before["era"] == 0
    assert before["block_reward"] == "25"  # the NEXT block is the halving block
    assert before["blocks_remaining"] == 1

    result(rpc, "generate", 1)  # height 10: the halving block itself
    after = result(rpc, "getinfo")["halving"]
    assert after["era"] == 1
    assert after["next_halving_height"] == 20


def test_two_networks_sharing_a_datadir_do_not_clobber_each_others_token(tmp_path):
    """Networks share a data directory by default; their tokens must not.

    Real incident: a regtest node started with the default datadir overwrote
    the running mainnet node's .rpccookie. The mainnet node kept running and
    relaying perfectly while every client - CLI, explorer, HUD - was locked out
    with a 401, which reads as a broken node rather than a clobbered file. The
    chain databases were already namespaced (mainnet-chain.db); the cookie was
    not.
    """
    from obsidion.node import ObsidionNode
    from obsidion.params import MAINNET, REGTEST
    from obsidion.rpc import RPCServer, read_cookie

    main_node = ObsidionNode(MAINNET)
    regtest_node = ObsidionNode(REGTEST)
    main_rpc = RPCServer(main_node, datadir=tmp_path)
    regtest_rpc = RPCServer(regtest_node, datadir=tmp_path)
    main_rpc.start()
    regtest_rpc.start()
    try:
        assert main_rpc.token != regtest_rpc.token

        # Each client reads its own network's token, not whoever wrote last.
        assert read_cookie(tmp_path, "mainnet") == main_rpc.token
        assert read_cookie(tmp_path, "regtest") == regtest_rpc.token

        # Stopping one must not disarm the other.
        regtest_rpc.stop()
        assert read_cookie(tmp_path, "mainnet") == main_rpc.token
    finally:
        main_rpc.stop()
        main_node.chain.close()
        regtest_node.chain.close()


def test_a_client_falls_back_to_a_legacy_shared_cookie(tmp_path):
    """A client pointed at a node from before the split must still work."""
    from obsidion.rpc import COOKIE_FILENAME, read_cookie

    (tmp_path / COOKIE_FILENAME).write_text("legacy-token")
    assert read_cookie(tmp_path, "mainnet") == "legacy-token"

    # But a network-specific file wins when both exist.
    (tmp_path / ".rpccookie-mainnet").write_text("current-token")
    assert read_cookie(tmp_path, "mainnet") == "current-token"


def test_a_missing_cookie_still_raises_file_not_found(tmp_path):
    from obsidion.rpc import read_cookie

    with pytest.raises(FileNotFoundError):
        read_cookie(tmp_path, "mainnet")


def test_a_node_that_never_starts_does_not_clobber_a_running_ones_token(tmp_path):
    """A cookie on disk must mean "a node is answering", not "one tried".

    RPCServer used to write its token in __init__, before binding. So a node
    that failed to finish starting - almost always a port already in use - had
    already overwritten the token of the node that was running. The survivor
    kept serving while every client got 401s, and the only cure was restarting
    the innocent node, because its token exists only in memory.

    Observed live: a launcher double-click wrote a fresh cookie, then died
    binding the P2P port, locking the CLI and HUD out of a perfectly healthy
    node.
    """
    from obsidion.rpc import RPCServer, read_cookie

    live_node = ObsidionNode(REGTEST)
    doomed_node = ObsidionNode(REGTEST)
    live = RPCServer(live_node, datadir=tmp_path)
    live.start()
    try:
        assert read_cookie(tmp_path, "regtest") == live.token

        # A second server for the same network that is constructed but never
        # started - exactly what a node aborting during start-up leaves behind.
        doomed = RPCServer(doomed_node, datadir=tmp_path)
        assert doomed.token != live.token

        assert read_cookie(tmp_path, "regtest") == live.token, (
            "a node that never started overwrote the running node's token"
        )
    finally:
        live.stop()
        live_node.chain.close()
        doomed_node.chain.close()
