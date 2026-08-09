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
from obsidion.params import MAINNET, REGTEST
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


# --------------------------------------------------------------------------
# Sending — the only HUD action that moves money, so it is tested hardest.
# Each failure case asserts not just the rejection but that the balance is
# untouched: a send that half-happens is worse than one that refuses.
# --------------------------------------------------------------------------


def _recipient_address() -> str:
    """An address belonging to somebody else, valid on regtest."""
    return Wallet(REGTEST, [b"\x51" * 32]).addresses()[0]


def _spendable(node) -> int:
    with node.lock:
        return node.wallet.balance(node.chain).spendable


def test_send_moves_coins_to_another_address(stack):
    node, rpc, client = stack
    burn = crypto.hash160(b"nobody-mines-here")

    # Pay exactly one coinbase to the wallet, then mine the rest to somebody
    # else. Otherwise every confirming block hands this wallet a fresh reward
    # and matures older ones, and the balance delta measures mining rather than
    # the payment under test.
    node.generate(1)
    node.generate(REGTEST.coinbase_maturity + 1, burn)

    before = _spendable(node)
    assert before > 0, "the mined coinbase should have matured by now"
    recipient = _recipient_address()

    response = client.post(
        "/action/send", json={"address": recipient, "amount": "10"}
    )
    assert response.status_code == 200, response.data
    body = json.loads(response.data)
    assert len(body["txid"]) == 64

    from decimal import Decimal

    fee = Decimal(body["fee"])
    assert fee > 0, "a real payment pays a real fee"

    # Confirm it on the chain, not just in the reply — again to the burn
    # address, so the only change to this wallet is the payment itself.
    node.generate(1, burn)

    pkh = crypto.address_to_pubkey_hash(recipient, REGTEST.bech32_hrp)
    with node.lock:
        owned = node.chain.utxos_for_pubkey_hash(pkh)
    assert sum(entry.amount for _, entry in owned) == 10 * 100_000_000

    # Down by exactly the amount plus the fee — change returned, nothing lost.
    after = _spendable(node)
    expected = Decimal(before) - Decimal(10 * 100_000_000) - fee * 100_000_000
    assert Decimal(after) == expected


def test_send_to_a_bad_address_is_rejected_without_moving_coins(stack):
    node, rpc, client = stack
    node.generate(1 + REGTEST.coinbase_maturity)
    before = _spendable(node)

    response = client.post(
        "/action/send", json={"address": "obsd1nonsense", "amount": "1"}
    )
    assert response.status_code == 400
    assert json.loads(response.data)["error"]
    assert _spendable(node) == before, "a refused send must not touch the balance"


def test_send_to_a_mainnet_address_is_rejected_on_regtest(stack):
    """Cross-network sends are the quiet way to burn coins forever."""
    node, rpc, client = stack
    node.generate(1 + REGTEST.coinbase_maturity)
    before = _spendable(node)

    mainnet_address = Wallet(MAINNET, [b"\x62" * 32]).addresses()[0]
    response = client.post(
        "/action/send", json={"address": mainnet_address, "amount": "1"}
    )
    assert response.status_code == 400
    assert _spendable(node) == before


def test_send_more_than_spendable_is_rejected(stack):
    node, rpc, client = stack
    node.generate(1 + REGTEST.coinbase_maturity)
    before = _spendable(node)

    absurd = str(before // 100_000_000 + 1_000_000)
    response = client.post(
        "/action/send", json={"address": _recipient_address(), "amount": absurd}
    )
    assert response.status_code == 400
    assert _spendable(node) == before


def test_send_of_a_sub_shard_amount_is_rejected(stack):
    """A shard is the indivisible unit; anything finer is not representable."""
    node, rpc, client = stack
    node.generate(1 + REGTEST.coinbase_maturity)
    before = _spendable(node)

    response = client.post(
        "/action/send",
        json={"address": _recipient_address(), "amount": "0.000000001"},
    )
    assert response.status_code == 400
    assert _spendable(node) == before


def test_send_of_a_negative_amount_is_rejected(stack):
    node, rpc, client = stack
    node.generate(1 + REGTEST.coinbase_maturity)
    before = _spendable(node)

    response = client.post(
        "/action/send", json={"address": _recipient_address(), "amount": "-5"}
    )
    assert response.status_code == 400
    assert _spendable(node) == before


def test_the_page_exposes_a_send_form_behind_a_confirm_step(stack):
    node, rpc, client = stack
    html = client.get("/").data.decode()

    assert "send-address" in html and "send-amount" in html
    # The two-stage flow must exist: reviewing is not sending.
    assert "Review send" in html
    assert "Confirm send" in html


# --------------------------------------------------------------------------
# Mining KPIs. The arithmetic is checked against hand-computable cases, because
# a plausible-looking wrong hashrate is worse than no hashrate at all.
# --------------------------------------------------------------------------


def _kpi_info(hashrate=100.0, height=200, reward="50"):
    return {
        "height": height,
        "hashrate": hashrate,
        "halving": {"block_reward": reward},
    }


def _window(count, spacing, bits, start_time=1_000_000):
    return [
        {"height": i, "time": start_time + i * spacing, "bits": bits}
        for i in range(count)
    ]


def test_network_hashrate_is_work_per_block_over_observed_spacing():
    """The core estimate, checked against a hand-computed value."""
    from obsidion.block import compact_to_target, expected_hashes
    from hud.app import mining_kpis

    bits = "1f0fffff"
    work = expected_hashes(compact_to_target(int(bits, 16)))

    # 11 gaps of 30s between 12 blocks.
    k = mining_kpis(
        _kpi_info(), _window(12, 30, bits), [], target_block_time=150
    )

    assert k["avg_block_seconds"] == 30
    assert k["network_hashrate"] == pytest.approx(work / 30)
    # Our 100 H/s against that network.
    assert k["share"] == pytest.approx(100.0 / (work / 30))
    # Mean wait for us is the whole block's work at our own rate.
    assert k["seconds_per_block_for_us"] == pytest.approx(work / 100.0)


def test_kpis_degrade_gracefully_on_a_chain_too_short_to_measure():
    """A fresh node must render a HUD, not divide by zero."""
    from hud.app import mining_kpis

    for window in ([], _window(1, 30, "1f0fffff")):
        k = mining_kpis(_kpi_info(), window, [], target_block_time=150)
        assert k["network_hashrate"] == 0.0
        assert k["share"] == 0.0
        assert k["expected_daily"] == 0.0


def test_a_backdated_block_cannot_produce_a_negative_rate():
    """Timestamps are miner-supplied and only loosely ordered.

    A block dated before its parent would give a negative span, and a negative
    'seconds per block' would render as a nonsense hashrate. Clamped to zero.
    """
    from hud.app import mining_kpis

    window = _window(6, 30, "1f0fffff")
    window[-1]["time"] = window[0]["time"] - 500  # last block dated far in the past

    k = mining_kpis(_kpi_info(), window, [], target_block_time=150)
    assert k["avg_block_seconds"] == 0.0
    assert k["network_hashrate"] == 0.0


def test_share_never_exceeds_one_hundred_percent():
    """On a tiny chain our own rate can exceed a noisy estimate; cap it."""
    from hud.app import mining_kpis

    # Absurdly fast local hashrate against a slow network estimate.
    k = mining_kpis(
        _kpi_info(hashrate=1e18), _window(12, 30, "1f0fffff"), [],
        target_block_time=150,
    )
    assert k["share"] == 1.0


def test_blocks_found_windows_count_only_recent_coinbases():
    from hud.app import mining_kpis

    # Tip 200; regtest-ish target of 150s means a day is 576 blocks.
    heights = [1, 50, 120, 199]
    k = mining_kpis(
        _kpi_info(height=200), _window(12, 150, "1f0fffff"), heights,
        target_block_time=150,
    )
    assert k["blocks_found_total"] == 4
    assert k["blocks_found_last_100"] == 2  # heights 120 and 199
    assert k["blocks_found_last_day"] == 4  # window is 576 blocks, all qualify


def test_state_exposes_kpis_against_a_live_node(stack):
    node, rpc, client = stack
    node.generate(4)

    state = _state(client)
    k = state["kpis"]

    assert k["blocks_found_total"] == 4
    assert k["sample_size"] > 0
    assert state["wallet"]["coinbase_heights"] == [1, 2, 3, 4]


def test_the_page_renders_the_kpi_panel(stack):
    node, rpc, client = stack
    html = client.get("/").data.decode()

    assert "Mining performance" in html
    for element in ("kpi-nethash", "kpi-share", "kpi-daily", "kpi-eta"):
        assert element in html


def test_a_young_chain_is_flagged_as_still_climbing_out_of_the_floor():
    """The daily projection is honest only with this caveat attached.

    A new chain starts at the difficulty floor and mints blocks far faster than
    target, so "projected daily" reads absurdly high — on the real mainnet
    launch it showed six figures a day, which will collapse as difficulty
    retargets. The flag drives a warning next to the number; without it the
    figure is worse than showing nothing.
    """
    from hud.app import mining_kpis

    fast = mining_kpis(
        _kpi_info(), _window(12, 20, "1f0fffff"), [], target_block_time=150
    )
    assert fast["difficulty_still_rising"] is True

    at_target = mining_kpis(
        _kpi_info(), _window(12, 150, "1f0fffff"), [], target_block_time=150
    )
    assert at_target["difficulty_still_rising"] is False

    # No samples means no claim either way.
    unknown = mining_kpis(_kpi_info(), [], [], target_block_time=150)
    assert unknown["difficulty_still_rising"] is False
