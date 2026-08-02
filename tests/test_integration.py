"""The capstone test: three complete nodes running as they would in production.

Everything below uses full ObsidionNode stacks — network thread, miner thread,
shared locks, real TCP — not the bare components. If this passes, the pieces
do not merely work; they work *together*, which is the only kind of working
that matters for a currency.

Topology is a line, A—B—C, so C never talks to A directly. Every block and
every payment that reaches C has been relayed through B, exercising gossip,
sync, mempool relay, and confirmation end to end.
"""

from __future__ import annotations

import time

import pytest

from obsidion import consensus
from obsidion.node import ObsidionNode
from obsidion.params import COIN, REGTEST
from obsidion.wallet import Wallet


def wait_until(condition, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def height(node) -> int:
    """Read a node's height under its lock — SQLite connections tolerate no
    concurrent use, and the network thread writes while tests watch."""
    with node.lock:
        return node.chain.height


def tip(node) -> bytes:
    with node.lock:
        return node.chain.tip_hash


@pytest.fixture
def network(tmp_path):
    """Three started nodes in a line: A—B—C. Yields (a, b, c)."""
    nodes = []
    for name in "abc":
        wallet = Wallet.create(tmp_path / f"{name}.wallet", "pw", REGTEST)
        node = ObsidionNode(REGTEST, wallet=wallet)
        node.start()
        nodes.append(node)

    a, b, c = nodes
    a.connect("127.0.0.1", b.p2p.port)
    c.connect("127.0.0.1", b.p2p.port)
    wait_until(
        lambda: len(b.p2p.handshaked_peers()) == 2,
        what="B handshaking with both neighbours",
    )

    yield a, b, c
    for node in nodes:
        node.stop()


def test_a_whole_economy_functions_across_three_nodes(network):
    a, b, c = network

    # --- Mining on A funds A's wallet; the chain reaches C through B. -------
    a.generate(1 + REGTEST.coinbase_maturity)
    expected_height = 1 + REGTEST.coinbase_maturity
    wait_until(
        lambda: height(c) == expected_height,
        what="the chain crossing two hops to C",
    )
    assert tip(c) == tip(a)
    assert tip(b) == tip(a)

    # --- A pays C's wallet; the unconfirmed payment floods to everyone. -----
    recipient = c.wallet.addresses()[0]
    with a.lock:
        payment = a.wallet.create_transaction(a.chain, recipient, 10 * COIN)
    txid = a.submit_tx(payment)

    wait_until(lambda: txid in c.mempool, what="the payment reaching C's mempool")
    assert txid in b.mempool

    # --- B mines the block that confirms it (fees go to B, not A). ----------
    b.generate(1)
    wait_until(
        lambda: height(a) == expected_height + 1
        and height(c) == expected_height + 1,
        what="the confirming block propagating back out",
    )

    # --- Everyone agrees, and the money actually moved. ---------------------
    assert tip(a) == tip(b) == tip(c)

    with c.lock:
        received = c.wallet.balance(c.chain)
    assert received.spendable == 10 * COIN

    # The mempools drained everywhere once the payment confirmed.
    assert txid not in a.mempool
    assert txid not in b.mempool
    assert txid not in c.mempool

    # And the books balance: every node reports a UTXO total equal to the
    # circulating supply. Nothing was created, nothing was lost.
    supply = consensus.circulating_supply(expected_height + 1, REGTEST)
    for node in (a, b, c):
        with node.lock:
            assert node.chain.total_utxo_value() == supply


def test_background_miners_race_without_forking_forever(network):
    """A and C both mine with real miner threads for a few seconds — a genuine
    race, blocks colliding, the works. When the dust settles, all three nodes
    must agree on a single tip. This is the property the whole design exists
    to provide."""
    a, b, c = network

    a.start_mining()
    c.start_mining()
    try:
        wait_until(lambda: height(b) >= 8, what="the race producing blocks")
    finally:
        a.stop_mining()
        c.stop_mining()

    # Let in-flight blocks finish propagating. The race can end in a genuine
    # tie — two branches of equal cumulative work — which persists until the
    # next block breaks it. That is correct Bitcoin behaviour, not a bug, so
    # the test does what reality does: someone mines one more block.
    time.sleep(1.0)
    b.generate(1)
    wait_until(
        lambda: tip(a) == tip(b) == tip(c),
        what="all three nodes converging on the tiebreaker",
    )

    supply = consensus.circulating_supply(height(b), REGTEST)
    for node in (a, b, c):
        with node.lock:
            assert node.chain.total_utxo_value() == supply
