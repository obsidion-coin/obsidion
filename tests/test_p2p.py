"""Tests for the peer-to-peer layer, run over real TCP sockets on localhost.

Each test builds genuine nodes — own chain state, own mempool, own listening
socket — connects them, and watches real messages flow. No mocks: the thing
being tested is precisely whether independent processes converge.
"""

from __future__ import annotations

import asyncio
import struct

from obsidion import consensus, crypto
from obsidion.chainstate import ChainState
from obsidion.mempool import Mempool
from obsidion.miner import mine_block
from obsidion.p2p import P2PNode, pack_message, pack_version
from obsidion.params import COIN, REGTEST, TESTNET
from obsidion.transaction import OutPoint, Transaction, TxIn, TxOut

ALICE_PRIV = crypto.generate_private_key()
ALICE_PUB = crypto.private_to_public(ALICE_PRIV)
ALICE = crypto.hash160(ALICE_PUB)
BOB = crypto.hash160(b"bob")
MINER = crypto.hash160(b"a-p2p-miner")


class Net:
    """A little harness that owns nodes and tears them down reliably."""

    def __init__(self):
        self.nodes: list[P2PNode] = []

    async def node(self, params=REGTEST) -> P2PNode:
        node = P2PNode(ChainState(params), Mempool(params), params)
        await node.start()
        self.nodes.append(node)
        return node

    async def close(self):
        for node in self.nodes:
            await node.stop()
            node.chain.close()


async def wait_for(condition, timeout=10.0, message="condition"):
    """Poll until `condition()` is true or the timeout burns out."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not condition():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(0.02)


async def mine_on(node: P2PNode, count=1, miner=MINER):
    """Mine blocks through the node's own submit path, as a real miner would.

    Submitting (rather than accepting straight into the chain) matters: the
    submit path is what broadcasts. A block accepted directly is a duplicate by
    the time submit sees it, and duplicates are deliberately not re-announced.
    """
    blocks = []
    for _ in range(count):
        block = mine_block(node.chain, node.mempool, miner)
        await node.submit_block(block)
        blocks.append(block)
    return blocks


def run(coroutine):
    asyncio.run(coroutine)


# --------------------------------------------------------------------------
# Handshake and connection hygiene
# --------------------------------------------------------------------------


def test_two_nodes_complete_a_handshake():
    async def scenario():
        net = Net()
        try:
            a, b = await net.node(), await net.node()
            await a.connect("127.0.0.1", b.port)

            await wait_for(
                lambda: a.handshaked_peers() and b.handshaked_peers(),
                message="both sides handshaked",
            )
            assert len(a.handshaked_peers()) == 1
            assert len(b.handshaked_peers()) == 1
        finally:
            await net.close()

    run(scenario())


def test_nodes_on_different_networks_cannot_talk():
    """A testnet node dialling a regtest node dies on the first four bytes."""

    async def scenario():
        net = Net()
        try:
            regtest_node = await net.node(REGTEST)
            testnet_node = await net.node(TESTNET)
            await testnet_node.connect("127.0.0.1", regtest_node.port)

            await asyncio.sleep(0.5)
            assert not regtest_node.handshaked_peers()
            assert not testnet_node.handshaked_peers()
        finally:
            await net.close()

    run(scenario())


def test_a_node_refuses_its_own_reflection():
    """Connecting to yourself must not create a phantom peer."""

    async def scenario():
        net = Net()
        try:
            node = await net.node()
            await node.connect("127.0.0.1", node.port)

            await asyncio.sleep(0.5)
            assert not node.handshaked_peers()
        finally:
            await net.close()

    run(scenario())


def test_peers_gossip_addresses():
    """C connects only to A, yet learns where B lives."""

    async def scenario():
        net = Net()
        try:
            a, b, c = await net.node(), await net.node(), await net.node()
            await b.connect("127.0.0.1", a.port)
            await wait_for(lambda: a.handshaked_peers(), message="A sees B")

            await c.connect("127.0.0.1", a.port)
            await wait_for(
                lambda: ("127.0.0.1", b.port) in c.addrbook,
                message="C learning B's address from A",
            )
        finally:
            await net.close()

    run(scenario())


# --------------------------------------------------------------------------
# Block propagation and sync
# --------------------------------------------------------------------------


def test_a_mined_block_reaches_the_other_node():
    async def scenario():
        net = Net()
        try:
            a, b = await net.node(), await net.node()
            await a.connect("127.0.0.1", b.port)
            await wait_for(lambda: a.handshaked_peers() and b.handshaked_peers())

            (block,) = await mine_on(a)

            await wait_for(
                lambda: b.chain.tip_hash == block.hash(),
                message="B adopting A's block",
            )
            assert b.chain.height == 1
        finally:
            await net.close()

    run(scenario())


def test_a_late_joiner_syncs_the_whole_chain():
    """A mines alone; B arrives afterwards and must catch up from nothing."""

    async def scenario():
        net = Net()
        try:
            a = await net.node()
            await mine_on(a, count=8)

            b = await net.node()
            await b.connect("127.0.0.1", a.port)

            await wait_for(
                lambda: b.chain.height == 8, message="B syncing eight blocks"
            )
            assert b.chain.tip_hash == a.chain.tip_hash
            assert b.chain.total_utxo_value() == a.chain.total_utxo_value()
        finally:
            await net.close()

    run(scenario())


def test_blocks_flood_across_a_line_of_nodes():
    """A—B—C in a line: C has no connection to A, yet must receive A's block
    through B. This is gossip actually gossiping."""

    async def scenario():
        net = Net()
        try:
            a, b, c = await net.node(), await net.node(), await net.node()
            await a.connect("127.0.0.1", b.port)
            await c.connect("127.0.0.1", b.port)
            await wait_for(lambda: len(b.handshaked_peers()) == 2)

            (block,) = await mine_on(a)

            await wait_for(
                lambda: c.chain.tip_hash == block.hash(),
                message="the block crossing two hops",
            )
        finally:
            await net.close()

    run(scenario())


def test_partitioned_nodes_converge_on_the_heavier_chain():
    """The healing property: two nodes mine apart, connect, and the shorter
    chain yields. This is the network-level version of the reorg tests."""

    async def scenario():
        net = Net()
        try:
            a, b = await net.node(), await net.node()
            await mine_on(a, count=5, miner=MINER)
            await mine_on(b, count=2, miner=BOB)
            assert a.chain.tip_hash != b.chain.tip_hash

            await a.connect("127.0.0.1", b.port)
            await wait_for(
                lambda: b.chain.tip_hash == a.chain.tip_hash,
                message="B reorging onto A's heavier chain",
            )
            assert b.chain.height == 5
        finally:
            await net.close()

    run(scenario())


# --------------------------------------------------------------------------
# Transaction propagation
# --------------------------------------------------------------------------


def test_a_transaction_spreads_to_peers_mempools():
    async def scenario():
        net = Net()
        try:
            a, b = await net.node(), await net.node()
            await a.connect("127.0.0.1", b.port)
            await wait_for(lambda: a.handshaked_peers() and b.handshaked_peers())

            funding = (await mine_on(a, count=1, miner=ALICE))[0]
            await mine_on(a, count=REGTEST.coinbase_maturity)
            await wait_for(
                lambda: b.chain.height == a.chain.height,
                message="B syncing the funding blocks",
            )

            reward = consensus.subsidy(1, REGTEST)
            payment = Transaction(
                inputs=[TxIn(OutPoint(funding.coinbase().txid(), 0))],
                outputs=[TxOut(reward - 1_000, BOB)],
            )
            payment.sign_input(0, ALICE_PRIV, ALICE_PUB, amount=reward)
            await a.submit_tx(payment)

            await wait_for(
                lambda: payment.txid() in b.mempool,
                message="the payment reaching B's mempool",
            )
        finally:
            await net.close()

    run(scenario())


# --------------------------------------------------------------------------
# Hostile peers
# --------------------------------------------------------------------------


def test_a_peer_sending_an_invalid_block_is_banned():
    """A block with a too-generous coinbase is consensus-invalid. The node must
    reject it and ban the sender, not just shrug."""

    async def scenario():
        net = Net()
        try:
            victim = await net.node()

            # A rogue speaks just enough protocol to deliver poison: an
            # otherwise-valid block whose coinbase claims double the subsidy.
            scratch = ChainState(REGTEST)
            try:
                greedy = mine_block(scratch, None, MINER)
            finally:
                scratch.close()
            from obsidion.merkle import merkle_root
            from obsidion.transaction import Transaction as Tx

            bloated = Tx.coinbase(
                1, 2 * consensus.subsidy(1, REGTEST), MINER
            )
            greedy.transactions[0] = bloated
            greedy.header.merkle_root = merkle_root(
                [t.txid() for t in greedy.transactions]
            )
            while not greedy.header.satisfies_pow(REGTEST.pow_algorithm):
                greedy.header.nonce += 1

            reader, writer = await asyncio.open_connection("127.0.0.1", victim.port)
            magic = REGTEST.magic
            writer.write(
                pack_message(magic, "version", pack_version(99, 0, 12345))
            )
            writer.write(pack_message(magic, "verack"))
            writer.write(pack_message(magic, "block", greedy.serialize()))
            await writer.drain()

            await wait_for(
                lambda: "127.0.0.1" in victim.banned,
                message="the victim banning the rogue",
            )
            assert victim.chain.height == 0  # the poison never took
            writer.close()
        finally:
            await net.close()

    run(scenario())


def test_a_peer_sending_garbage_is_dropped():
    async def scenario():
        net = Net()
        try:
            victim = await net.node()

            reader, writer = await asyncio.open_connection("127.0.0.1", victim.port)
            writer.write(b"GET / HTTP/1.1\r\nHost: not-a-coin\r\n\r\n" * 10)
            await writer.drain()

            await asyncio.sleep(0.5)
            assert not victim.handshaked_peers()
            writer.close()
        finally:
            await net.close()

    run(scenario())


# --------------------------------------------------------------------------
# Staying connected
# --------------------------------------------------------------------------


def test_a_node_dials_back_up_to_its_outbound_target():
    """A node that loses its peers must reconnect on its own. The maintenance
    loop is what keeps a long-running node from quietly ending up alone."""

    async def scenario():
        net = Net()
        try:
            a, b = await net.node(), await net.node()
            # A knows where B lives but is not connected to it.
            a.addrbook.add(("127.0.0.1", b.port))
            assert not a.peers

            await a._top_up_outbound()

            await wait_for(
                lambda: a.handshaked_peers(), message="A dialling from its addrbook"
            )
        finally:
            await net.close()

    run(scenario())


def test_seeds_are_the_fallback_when_nothing_else_is_known():
    """The bootstrap case: a brand-new node knows nobody, so the configured
    seeds are the only way in."""

    async def scenario():
        net = Net()
        try:
            a, b = await net.node(), await net.node()
            a.seeds = [("127.0.0.1", b.port)]
            assert not a.addrbook

            await a._top_up_outbound()

            await wait_for(lambda: a.handshaked_peers(), message="A dialling a seed")
        finally:
            await net.close()

    run(scenario())


def test_seeds_are_retried_after_every_known_address_has_failed():
    """A seed unreachable an hour ago may be back. A node with no peers has
    nothing to lose by asking again."""

    async def scenario():
        net = Net()
        try:
            a, b = await net.node(), await net.node()
            # Everything A knows is marked dead, including the live node.
            a.addrbook.add(("127.0.0.1", b.port))
            a.failed.add(("127.0.0.1", b.port))
            a.seeds = [("127.0.0.1", b.port)]

            await a._top_up_outbound()

            assert not a.failed, "past failures should have been cleared"
            await wait_for(lambda: a.handshaked_peers(), message="A retrying the seed")
        finally:
            await net.close()

    run(scenario())


def test_an_unreachable_address_is_remembered_as_failed():
    async def scenario():
        net = Net()
        try:
            a = await net.node()
            # Port 1 on localhost: nothing listens there.
            await a.connect("127.0.0.1", 1)

            assert ("127.0.0.1", 1) in a.failed
            assert not a.peers
        finally:
            await net.close()

    run(scenario())


def test_a_silent_peer_is_pinged_and_then_dropped():
    """TCP will happily hold a connection to a machine that is gone. Idle
    peers get one ping; still silent by the next round and the slot is freed
    for a peer that answers."""

    async def scenario():
        net = Net()
        try:
            a, b = await net.node(), await net.node()
            await a.connect("127.0.0.1", b.port)
            await wait_for(lambda: a.handshaked_peers())

            (peer,) = a.handshaked_peers()
            peer.last_seen -= 10_000  # pretend it has been quiet for hours

            await a._check_liveness()
            assert peer.awaiting_pong, "an idle peer should have been pinged"

            # B is real and answers, so the peer is marked live again.
            await wait_for(
                lambda: not peer.awaiting_pong, message="B answering the ping"
            )
        finally:
            await net.close()

    run(scenario())


def test_a_peer_that_never_answers_a_ping_is_dropped():
    async def scenario():
        net = Net()
        try:
            a, b = await net.node(), await net.node()
            await a.connect("127.0.0.1", b.port)
            await wait_for(lambda: a.handshaked_peers())

            (peer,) = a.handshaked_peers()
            # Already pinged once and still silent — the second round evicts.
            peer.last_seen -= 10_000
            peer.awaiting_pong = True

            await a._check_liveness()
            assert peer not in a.peers
        finally:
            await net.close()

    run(scenario())


def test_an_oversized_message_is_refused():
    async def scenario():
        net = Net()
        try:
            victim = await net.node()

            reader, writer = await asyncio.open_connection("127.0.0.1", victim.port)
            # A header promising a payload far beyond the limit.
            huge = struct.pack("<I", 50_000_000)
            writer.write(
                REGTEST.magic + b"block".ljust(12, b"\x00") + huge + b"\x00" * 4
            )
            await writer.drain()

            await asyncio.sleep(0.5)
            assert not victim.handshaked_peers()
            writer.close()
        finally:
            await net.close()

    run(scenario())
