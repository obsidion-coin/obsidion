"""Tests for the block explorer, via Flask's test client against a live node."""

from __future__ import annotations

import pytest

from explorer.app import create_app
from obsidion.node import ObsidionNode
from obsidion.params import REGTEST
from obsidion.rpc import RPCServer
from obsidion.wallet import Wallet


@pytest.fixture
def stack(tmp_path):
    wallet = Wallet.create(tmp_path / "explorer.wallet", "pw", REGTEST)
    node = ObsidionNode(REGTEST, wallet=wallet)
    rpc = RPCServer(node)
    node.start()
    rpc.start()

    client = create_app(rpc.port).test_client()
    yield node, rpc, client

    rpc.stop()
    node.stop()


def test_the_dashboard_shows_the_chain_and_the_halving_countdown(stack):
    node, rpc, client = stack
    node.generate(3)

    html = client.get("/").data.decode()
    assert "Obsidion" in html
    assert "next halving" in html
    assert "7 blocks" in html  # regtest halves every 10; height 3 → 7 to go
    assert "50" in html  # current reward on the dashboard


def test_a_block_page_renders_with_its_transactions(stack):
    node, rpc, client = stack
    node.generate(2)
    block_hash = node.chain.tip_hash[::-1].hex()

    html = client.get(f"/block/{block_hash}").data.decode()
    assert "Block 2" in html
    assert "coinbase" in html
    assert block_hash in html


def test_a_transaction_page_renders_a_real_payment(stack):
    node, rpc, client = stack
    node.generate(1 + REGTEST.coinbase_maturity)

    recipient = Wallet(REGTEST, [b"\x44" * 32]).addresses()[0]
    with node.lock:
        tx = node.wallet.create_transaction(node.chain, recipient, 5_00000000)
    txid = node.submit_tx(tx)[::-1].hex()
    node.generate(1)

    html = client.get(f"/tx/{txid}").data.decode()
    assert "1 confirmation" in html
    assert recipient in html
    assert "5" in html


def test_an_address_page_shows_holdings(stack):
    node, rpc, client = stack
    node.generate(2)
    miner_address = node.wallet.addresses()[0]

    html = client.get(f"/address/{miner_address}").data.decode()
    assert miner_address in html
    assert "100" in html  # two 50-OBSD rewards


def test_search_dispatches_heights_hashes_and_addresses(stack):
    node, rpc, client = stack
    node.generate(2)
    tip_hex = node.chain.tip_hash[::-1].hex()

    by_height = client.get("/search?q=2")
    assert by_height.status_code == 302 and tip_hex in by_height.headers["Location"]

    by_hash = client.get(f"/search?q={tip_hex}")
    assert by_hash.status_code == 302 and tip_hex in by_hash.headers["Location"]

    address = node.wallet.addresses()[0]
    by_address = client.get(f"/search?q={address}")
    assert by_address.status_code == 302 and address in by_address.headers["Location"]


def test_missing_things_are_404s_not_crashes(stack):
    node, rpc, client = stack
    assert client.get("/block/" + "ab" * 32).status_code == 404
    assert client.get("/tx/" + "cd" * 32).status_code == 404
    assert client.get("/search?q=99999").status_code == 404
