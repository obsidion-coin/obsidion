"""Tests for the full node stack: daemon + RPC server, driven over real HTTP.

These are the closest thing to a user: every call here goes through the same
socket, JSON, and thread boundaries that obsidion-cli uses.
"""

from __future__ import annotations

import json
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
    rpc = RPCServer(node)
    node.start()
    rpc.start()
    yield node, rpc
    rpc.stop()
    node.stop()


def call(rpc: RPCServer, method: str, *params):
    request = json.dumps({"method": method, "params": list(params), "id": 7}).encode()
    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{rpc.port}/",
            data=request,
            headers={"Content-Type": "application/json"},
        ),
        timeout=30,
    ) as response:
        return json.loads(response.read())


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
