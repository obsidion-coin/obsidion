"""Tests for the wallet + mining HUD, via Flask's test client against a live node.

The HUD is a private, loopback-only front-end onto a node whose token can
spend. These tests hold it to that contract: the numbers it reports must match
what the node actually holds, its mining controls must move the node's real
state, and it must refuse to bind anywhere a stranger could reach it.
"""

from __future__ import annotations

import json
import time

import pytest

from hud.app import build_state, create_app, main
from obsidion import crypto
from obsidion.node import ObsidionNode
from obsidion.params import REGTEST
from obsidion.rpc import RPCServer
from obsidion.wallet import Wallet


@pytest.fixture
def stack(tmp_path):
    wallet = Wallet.create(tmp_path / "hud.wallet", "pw", REGTEST)
    node = ObsidionNode(REGTEST, wallet=wallet)
    rpc = RPCServer(node, datadir=tmp_path)
    node.start()
    rpc.start()

    client = create_app(rpc.port, rpc.token, network="regtest").test_client()
    yield node, rpc, client

    if node.mining:
        node.stop_mining()
    rpc.stop()
    node.stop()


def _state(client) -> dict:
    response = client.get("/api/state")
    assert response.status_code == 200
    return json.loads(response.data)


def test_state_reports_the_wallet_balance_after_mining_and_maturity(stack):
    node, rpc, client = stack
    node.generate(1 + REGTEST.coinbase_maturity)

    with node.lock:
        wallet_balance = node.wallet.balance(node.chain)

    state = _state(client)
    # The HUD's balance must match what the wallet itself reports, to the shard.
    from obsidion.rpc import format_amount

    assert state["balance"]["total"] == format_amount(wallet_balance.total)
    assert float(state["balance"]["spendable"]) > 0
    # Height climbed with the blocks we mined.
    assert state["node"]["height"] == 1 + REGTEST.coinbase_maturity


def test_state_counts_every_coinbase_output_the_wallet_still_holds(stack):
    node, rpc, client = stack
    node.generate(3)  # three coinbases, all unspent, paid to the wallet

    state = _state(client)
    assert state["wallet"]["blocks_mined"] == 3
    # Total mined equals three block rewards (regtest era-0 subsidy).
    from obsidion import consensus

    expected = sum(consensus.subsidy(h, REGTEST) for h in (1, 2, 3))
    assert state["wallet"]["mined_total_shards"] == expected


def test_immature_coinbase_reports_blocks_until_it_matures(stack):
    node, rpc, client = stack
    node.generate(1)  # one fresh coinbase, freshly immature

    state = _state(client)
    immature = [u for u in state["wallet"]["utxos"] if u["coinbase"] and not u["mature"]]
    assert immature, "a just-mined coinbase should be immature"
    # Freshly mined at the tip: exactly `coinbase_maturity` blocks to go.
    assert immature[0]["blocks_to_mature"] == REGTEST.coinbase_maturity


def test_the_halving_countdown_is_present_and_correct(stack):
    node, rpc, client = stack
    node.generate(3)  # regtest halves every 10 → 7 blocks to go

    state = _state(client)
    assert state["halving"]["blocks_remaining"] == 7


def test_state_reflects_the_nodes_mining_state(stack):
    node, rpc, client = stack
    burn = crypto.hash160(b"burn-address-for-test")

    assert _state(client)["mining"]["active"] is False
    node.start_mining(burn)
    try:
        assert _state(client)["mining"]["active"] is True
    finally:
        node.stop_mining()
    assert _state(client)["mining"]["active"] is False


def test_the_action_endpoints_start_and_stop_mining(stack):
    node, rpc, client = stack

    started = client.post("/action/startmining")
    assert started.status_code == 200
    try:
        assert node.mining is True
    finally:
        stopped = client.post("/action/stopmining")
        assert stopped.status_code == 200
    assert node.mining is False


def test_the_new_address_action_returns_a_fresh_wallet_address(stack):
    node, rpc, client = stack
    before = set(node.wallet.addresses())

    response = client.post("/action/newaddress")
    assert response.status_code == 200
    address = json.loads(response.data)["address"]

    assert address.startswith(REGTEST.bech32_hrp + "1")
    assert address not in before
    assert address in node.wallet.addresses()


def test_the_page_renders_and_advertises_the_hotkey(stack):
    node, rpc, client = stack
    html = client.get("/").data.decode()

    assert "Obsidion" in html
    # The overlay hotkey must be discoverable on the page itself.
    assert "Ctrl" in html and "Alt" in html and "Enter" in html
    # Both panels are present.
    assert "hashrate" in html.lower()
    assert "balance" in html.lower()


def test_synced_flag_is_true_when_no_peer_is_ahead(stack):
    node, rpc, client = stack
    node.generate(2)
    # No peers in this isolated stack, so nothing is ahead of us.
    assert _state(client)["node"]["synced"] is True


def test_build_state_is_a_pure_function_of_rpc_results():
    """build_state must derive everything from RPC dicts, holding no node ref.

    Feeding it canned RPC replies proves the derivations (blocks_mined,
    maturity, synced) without standing up a node — and guarantees the HUD can
    never touch chain state directly.
    """
    info = {
        "height": 105,
        "difficulty": 1.5,
        "mining": True,
        "hashrate": 42.0,
        "halving": {
            "era": 0,
            "block_reward": "50",
            "blocks_remaining": 5,
            "estimated_seconds": 750,
        },
    }
    balance = {"spendable": "100", "immature": "50", "total": "150"}
    addresses = ["rtobsd1aaa", "rtobsd1bbb"]
    utxos = {
        "rtobsd1aaa": {
            "balance": "100",
            "utxos": [
                {"txid": "aa", "index": 0, "amount": "50", "height": 3, "coinbase": True},
                {"txid": "bb", "index": 0, "amount": "50", "height": 104, "coinbase": True},
            ],
        },
        "rtobsd1bbb": {
            "balance": "50",
            "utxos": [
                {"txid": "cc", "index": 0, "amount": "50", "height": 2, "coinbase": False},
            ],
        },
    }
    peers = [{"height": 105}, {"height": 90}]

    state = build_state(
        info, balance, addresses, utxos, peers, coinbase_maturity=100
    )

    assert state["wallet"]["blocks_mined"] == 2  # two coinbase outputs
    assert state["node"]["synced"] is True  # 105 >= max peer 105
    # The coinbase at height 3 is mature (105-3 >= 100); the one at 104 is not.
    mature = {u["height"]: u["mature"] for u in state["wallet"]["utxos"] if u["coinbase"]}
    assert mature[3] is True
    assert mature[104] is False


def test_mined_total_survives_scientific_notation_amounts():
    """format_amount emits '4E-8' for tiny amounts; the sum must still be exact.

    This is the regtest mining-storm bug that a naive decimal-point split hit:
    str(Decimal(shards)/COIN) uses exponent form for small values, so parsing
    must go through Decimal, not string surgery. Would fail on mainnet too for
    any dust-sized coinbase or fee.
    """
    from obsidion.rpc import format_amount

    tiny = format_amount(4)  # 4 shards → "4E-8"
    assert "E" in tiny.upper(), "precondition: this amount serialises in exponent form"

    info = {"height": 5, "halving": {}}
    balance = {"spendable": "0", "immature": "0", "total": "0"}
    utxos = {
        "rtobsd1x": {
            "balance": tiny,
            "utxos": [
                {"txid": "aa", "index": 0, "amount": tiny, "height": 1, "coinbase": True},
            ],
        }
    }

    state = build_state(info, balance, ["rtobsd1x"], utxos, [], coinbase_maturity=2)
    assert state["wallet"]["mined_total_shards"] == 4


def test_it_refuses_to_bind_off_loopback_without_permission():
    """The HUD reaches a spend-capable token; a public bind would be a giveaway."""
    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0", "--network", "regtest"])
