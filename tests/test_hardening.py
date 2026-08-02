"""Regression tests for defects found by adversarial audit.

Each test here corresponds to a real bug that shipped in the first version and
was caught by reviewers attacking the code rather than exercising it. They are
grouped together deliberately: this file is the record of what went wrong and
the proof it stays fixed.
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading

import pytest

from obsidion import consensus, crypto
from obsidion.chainstate import ChainState
from obsidion.consensus import ConsensusError
from obsidion.mempool import Mempool
from obsidion.miner import mine_block
from obsidion.p2p import (
    MAX_LOCATOR,
    P2PNode,
    ProtocolError,
    pack_getblocks,
    unpack_getblocks,
)
from obsidion.params import COIN, MAINNET, REGTEST
from obsidion.transaction import OutPoint, Transaction, TxIn, TxOut, write_varint
from obsidion.wallet import Wallet, WalletError

ALICE_PRIV = crypto.generate_private_key()
ALICE_PUB = crypto.private_to_public(ALICE_PRIV)
ALICE = crypto.hash160(ALICE_PUB)
BOB = crypto.hash160(b"bob")
MINER = crypto.hash160(b"miner")
REWARD = consensus.subsidy(1, REGTEST)


@pytest.fixture
def chain():
    state = ChainState(REGTEST)
    yield state
    state.close()


def mine(chain, miner=MINER, mempool=None):
    block = mine_block(chain, mempool, miner)
    chain.accept_block(block, now=block.header.timestamp + 600)
    return block


def fund(chain, count=1):
    txids = [mine(chain, miner=ALICE).coinbase().txid() for _ in range(count)]
    for _ in range(REGTEST.coinbase_maturity):
        mine(chain)
    return txids


def spend(prev_txid, amount_in, outputs, index=0, priv=ALICE_PRIV, pub=ALICE_PUB):
    tx = Transaction(
        inputs=[TxIn(OutPoint(prev_txid, index))],
        outputs=[TxOut(amount, pkh) for amount, pkh in outputs],
    )
    tx.sign_input(0, priv, pub, amount=amount_in)
    return tx


# --------------------------------------------------------------------------
# A tiny message must not be able to freeze a node
# --------------------------------------------------------------------------


def test_a_nine_byte_getblocks_cannot_hang_the_node():
    """Was a critical denial of service.

    `unpack_getblocks` looped over an attacker-declared count, and slicing a
    bytes object past its end returns b'' instead of raising — so a message
    claiming 2**64 locator entries spun forever, allocating as it went. One
    packet, one dead node.
    """
    malicious = write_varint(0xFFFFFFFFFFFFFFFF)
    assert len(malicious) == 9

    with pytest.raises(ProtocolError, match="locator"):
        unpack_getblocks(malicious)


def test_a_locator_longer_than_the_payload_is_refused():
    lying = write_varint(1000) + b"\x00" * 32  # claims 1000, carries one
    with pytest.raises(ProtocolError):
        unpack_getblocks(lying)


def test_an_over_long_locator_is_refused():
    too_many = pack_getblocks([bytes(32)] * (MAX_LOCATOR + 1))
    with pytest.raises(ProtocolError, match="limit"):
        unpack_getblocks(too_many)


def test_an_honest_locator_still_round_trips():
    """The bound must not break real sync. A locator is exponentially spaced,
    so even an enormous chain produces only a few dozen entries."""
    honest = [crypto.sha256d(bytes([i])) for i in range(20)]
    assert unpack_getblocks(pack_getblocks(honest)) == honest


def test_a_hostile_getblocks_bans_the_sender_and_leaves_the_node_serving():
    """End to end over a real socket: the node survives and keeps working."""

    async def scenario():
        node = P2PNode(ChainState(REGTEST), Mempool(REGTEST), REGTEST)
        await node.start()
        try:
            from obsidion.p2p import pack_message, pack_version

            reader, writer = await asyncio.open_connection("127.0.0.1", node.port)
            writer.write(pack_message(REGTEST.magic, "version", pack_version(0, 0, 99)))
            writer.write(pack_message(REGTEST.magic, "verack"))
            writer.write(
                pack_message(
                    REGTEST.magic, "getblocks", write_varint(0xFFFFFFFFFFFFFFFF)
                )
            )
            await writer.drain()

            # The node must still answer for itself a moment later.
            await asyncio.sleep(0.6)
            assert node.chain.height == 0
            assert not node.handshaked_peers()
            writer.close()
        finally:
            await node.stop()
            node.chain.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# The mempool must not be free to fill
# --------------------------------------------------------------------------


def test_the_mempool_stops_growing_at_its_budget(chain):
    """Was a remote memory-exhaustion vector: valid zero-fee transactions
    could be created in bulk and nothing ever forced them out."""
    fundings = fund(chain, count=6)
    pool = Mempool(REGTEST, max_size=600)  # room for roughly three

    # All pay the same rate, so none can outbid another and the pool simply
    # fills up and starts refusing.
    refused = 0
    for funding in fundings:
        tx = spend(funding, REWARD, [(REWARD - 20_000, BOB)])
        try:
            pool.accept(tx, chain)
        except ConsensusError as exc:
            assert "full" in str(exc)
            refused += 1

    assert pool.total_size <= pool.max_size, "the budget was exceeded"
    assert len(pool) < len(fundings), "everything stayed resident"
    assert refused > 0, "nothing was ever refused"


def test_a_better_paying_transaction_evicts_a_worse_one(chain):
    """Past the budget, space goes to whoever pays for it — which is also what
    makes flooding cost money."""
    cheap_funding, rich_funding = fund(chain, count=2)
    pool = Mempool(REGTEST, max_size=260)  # one transaction at a time

    cheap = spend(cheap_funding, REWARD, [(REWARD - 1_000, BOB)])
    pool.accept(cheap, chain)
    assert cheap.txid() in pool

    rich = spend(rich_funding, REWARD, [(REWARD - 5_000_000, BOB)])
    pool.accept(rich, chain)

    assert rich.txid() in pool
    assert cheap.txid() not in pool
    assert pool.total_size <= pool.max_size


def test_a_worse_paying_transaction_cannot_evict_a_better_one(chain):
    cheap_funding, rich_funding = fund(chain, count=2)
    pool = Mempool(REGTEST, max_size=260)

    rich = spend(rich_funding, REWARD, [(REWARD - 5_000_000, BOB)])
    pool.accept(rich, chain)

    cheap = spend(cheap_funding, REWARD, [(REWARD - 1_000, BOB)])
    with pytest.raises(ConsensusError, match="does not outbid"):
        pool.accept(cheap, chain)

    assert rich.txid() in pool


def test_pool_size_accounting_returns_to_zero(chain):
    """Bookkeeping must be exact, or the pool slowly convinces itself it is
    full and starts refusing honest traffic."""
    (funding,) = fund(chain)
    pool = Mempool(REGTEST)

    payment = spend(funding, REWARD, [(REWARD - 1_000, BOB)])
    pool.accept(payment, chain)
    assert pool.total_size == payment.size()

    block = mine(chain, mempool=pool)
    pool.remove_confirmed(block)
    assert len(pool) == 0
    assert pool.total_size == 0


# --------------------------------------------------------------------------
# The wallet must never lose a key it has handed out
# --------------------------------------------------------------------------


def test_concurrent_address_creation_loses_no_keys(tmp_path):
    """Was a high-severity money-loss bug.

    The RPC server is threaded, so two clients could call getnewaddress at the
    same instant. Both wrote the shared temp file and one save overwrote the
    other — discarding a key whose address had already been given out. Anything
    sent there would have been unspendable forever.
    """
    path = tmp_path / "busy.wallet"
    wallet = Wallet.create(path, "pw", REGTEST)

    handed_out: list[str] = []
    errors: list[BaseException] = []

    def request_address():
        try:
            handed_out.append(wallet.new_address())
        except BaseException as exc:  # noqa: BLE001 — recorded and re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=request_address) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"address creation raised: {errors[0]!r}"
    assert len(set(handed_out)) == len(handed_out), "an address was issued twice"

    # Every address handed to a caller must be recoverable from disk.
    reloaded = Wallet.load(path, "pw")
    missing = set(handed_out) - set(reloaded.addresses())
    assert not missing, f"{len(missing)} issued addresses have no key on disk"


def test_no_stray_temp_files_survive_concurrent_saves(tmp_path):
    path = tmp_path / "clean.wallet"
    wallet = Wallet.create(path, "pw", REGTEST)

    threads = [threading.Thread(target=wallet.new_address) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != path.name]
    assert not leftovers, f"scratch files left behind: {leftovers}"


def test_the_wallets_network_cannot_be_edited_by_hand(tmp_path):
    """Was a low-severity but nasty bug: the network name sat outside the
    authentication tag, so anyone with a text editor could flip a testnet
    wallet to mainnet and the node would follow along, deriving addresses on
    the wrong chain."""
    path = tmp_path / "net.wallet"
    Wallet.create(path, "pw", REGTEST)

    envelope = json.loads(path.read_text())
    assert envelope["network"] == "regtest"
    envelope["network"] = "mainnet"
    path.write_text(json.dumps(envelope))

    with pytest.raises(WalletError):
        Wallet.load(path, "pw")


def test_an_untouched_wallet_still_loads(tmp_path):
    """The authentication must not be so eager it rejects honest files."""
    path = tmp_path / "fine.wallet"
    original = Wallet.create(path, "pw", REGTEST)
    original.new_address()

    reloaded = Wallet.load(path, "pw")
    assert reloaded.addresses() == original.addresses()
    assert reloaded.params.name == "regtest"
