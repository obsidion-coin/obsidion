"""Tests for wallet encryption, balances, and payment construction."""

from __future__ import annotations

import json

import pytest

from obsidion import consensus, crypto
from obsidion.chainstate import ChainState
from obsidion.mempool import Mempool
from obsidion.miner import mine_block
from obsidion.params import COIN, REGTEST
from obsidion.wallet import Balance, DUST, Wallet, WalletError

REWARD = consensus.subsidy(1, REGTEST)


@pytest.fixture
def chain():
    state = ChainState(REGTEST)
    yield state
    state.close()


@pytest.fixture
def wallet(tmp_path):
    return Wallet.create(tmp_path / "test.wallet", "hunter2", REGTEST)


def mine(chain, pubkey_hash, count=1, mempool=None):
    for _ in range(count):
        block = mine_block(chain, mempool, pubkey_hash)
        chain.accept_block(block, now=block.header.timestamp + 600)


def fund(chain, wallet, blocks=1):
    """Mine `blocks` rewards to the wallet and age them to maturity."""
    pubkey_hash = crypto.address_to_pubkey_hash(
        wallet.addresses()[0], REGTEST.bech32_hrp
    )
    mine(chain, pubkey_hash, count=blocks)
    mine(chain, crypto.hash160(b"someone-else"), count=REGTEST.coinbase_maturity)


# --------------------------------------------------------------------------
# Key storage
# --------------------------------------------------------------------------


def test_a_created_wallet_loads_back_with_the_same_addresses(tmp_path, wallet):
    reloaded = Wallet.load(tmp_path / "test.wallet", "hunter2")
    assert reloaded.addresses() == wallet.addresses()
    assert reloaded.params.name == REGTEST.name


def test_the_wrong_password_is_cleanly_refused(tmp_path, wallet):
    with pytest.raises(WalletError, match="wrong password"):
        Wallet.load(tmp_path / "test.wallet", "hunter3")


def test_private_keys_never_touch_the_disk_in_plaintext(tmp_path, wallet):
    """The whole point of the encryption: a stolen wallet file without its
    password is a stolen lump of noise."""
    raw = (tmp_path / "test.wallet").read_bytes()

    for address in wallet.addresses():
        pkh = crypto.address_to_pubkey_hash(address, REGTEST.bech32_hrp)
        private_key, _ = wallet._keys[pkh]
        assert private_key not in raw
        assert private_key.hex().encode() not in raw

    # And the envelope is honest about what it is.
    envelope = json.loads(raw)
    assert envelope["network"] == "regtest"
    assert "tag" in envelope["crypto"]


def test_an_existing_wallet_is_never_overwritten(tmp_path, wallet):
    with pytest.raises(WalletError, match="refusing to overwrite"):
        Wallet.create(tmp_path / "test.wallet", "other", REGTEST)


def test_a_corrupted_wallet_file_is_detected(tmp_path, wallet):
    path = tmp_path / "test.wallet"
    envelope = json.loads(path.read_text())
    ciphertext = bytearray(bytes.fromhex(envelope["crypto"]["ciphertext"]))
    ciphertext[0] ^= 0xFF
    envelope["crypto"]["ciphertext"] = bytes(ciphertext).hex()
    path.write_text(json.dumps(envelope))

    with pytest.raises(WalletError, match="wrong password|corrupted"):
        Wallet.load(path, "hunter2")


def test_new_addresses_persist_across_reload(tmp_path, wallet):
    fresh = wallet.new_address()
    assert fresh.startswith("rtobsd1")

    reloaded = Wallet.load(tmp_path / "test.wallet", "hunter2")
    assert fresh in reloaded.addresses()
    assert len(reloaded.addresses()) == 2


# --------------------------------------------------------------------------
# Balances
# --------------------------------------------------------------------------


def test_a_fresh_wallet_is_empty(chain, wallet):
    assert wallet.balance(chain) == Balance(0, 0)


def test_a_new_reward_is_immature_then_spendable(chain, wallet):
    pubkey_hash = crypto.address_to_pubkey_hash(
        wallet.addresses()[0], REGTEST.bech32_hrp
    )
    mine(chain, pubkey_hash)

    early = wallet.balance(chain)
    assert early.spendable == 0
    assert early.immature == REWARD

    mine(chain, crypto.hash160(b"other"), count=REGTEST.coinbase_maturity)
    later = wallet.balance(chain)
    assert later.spendable == REWARD
    assert later.immature == 0


def test_balance_spans_all_the_wallets_addresses(chain, wallet):
    second = wallet.new_address()
    fund(chain, wallet)  # pays the first address
    mine(chain, crypto.address_to_pubkey_hash(second, REGTEST.bech32_hrp))
    mine(chain, crypto.hash160(b"other"), count=REGTEST.coinbase_maturity)

    assert wallet.balance(chain).spendable > REWARD


# --------------------------------------------------------------------------
# Building payments
# --------------------------------------------------------------------------


def test_a_payment_is_valid_and_spendable(chain, wallet):
    """The full circuit: build, admit to a mempool, mine, and confirm the
    recipient ended up owning the coins."""
    fund(chain, wallet)
    recipient = crypto.hash160(b"recipient")
    recipient_address = crypto.pubkey_hash_to_address(recipient, REGTEST.bech32_hrp)

    tx = wallet.create_transaction(chain, recipient_address, 10 * COIN)

    pool = Mempool(REGTEST)
    fee = pool.accept(tx, chain)
    assert fee > 0

    mine(chain, crypto.hash160(b"a-miner"), mempool=pool)
    paid = chain.utxos_for_pubkey_hash(recipient)
    assert sum(entry.amount for _, entry in paid) == 10 * COIN


def test_change_comes_back_to_the_wallet(chain, wallet):
    fund(chain, wallet)
    recipient = crypto.pubkey_hash_to_address(
        crypto.hash160(b"recipient"), REGTEST.bech32_hrp
    )

    tx = wallet.create_transaction(chain, recipient, 10 * COIN)

    change_outputs = [
        output for output in tx.outputs if output.pubkey_hash in wallet._keys
    ]
    assert len(change_outputs) == 1
    fee = REWARD - tx.total_output_value()
    assert change_outputs[0].amount == REWARD - 10 * COIN - fee
    assert 0 < fee < COIN // 10  # sane, not confiscatory


def test_insufficient_funds_is_a_clear_error(chain, wallet):
    fund(chain, wallet)  # one reward
    recipient = crypto.pubkey_hash_to_address(
        crypto.hash160(b"greedy"), REGTEST.bech32_hrp
    )
    with pytest.raises(WalletError, match="insufficient funds"):
        wallet.create_transaction(chain, recipient, REWARD * 2)


def test_immature_rewards_cannot_be_spent(chain, wallet):
    pubkey_hash = crypto.address_to_pubkey_hash(
        wallet.addresses()[0], REGTEST.bech32_hrp
    )
    mine(chain, pubkey_hash)  # reward exists but is not mature

    recipient = crypto.pubkey_hash_to_address(
        crypto.hash160(b"impatient"), REGTEST.bech32_hrp
    )
    with pytest.raises(WalletError, match="insufficient funds"):
        wallet.create_transaction(chain, recipient, COIN)


def test_an_address_from_another_network_is_refused(chain, wallet):
    fund(chain, wallet)
    mainnet_address = crypto.pubkey_hash_to_address(crypto.hash160(b"x"), "obsd")

    with pytest.raises(WalletError, match="different network"):
        wallet.create_transaction(chain, mainnet_address, COIN)


def test_two_payments_never_promise_the_same_coin(chain, wallet):
    """Building two transactions back-to-back, before either confirms, must
    draw on different outputs — reservation in action."""
    fund(chain, wallet, blocks=2)
    recipient = crypto.pubkey_hash_to_address(
        crypto.hash160(b"recipient"), REGTEST.bech32_hrp
    )

    first = wallet.create_transaction(chain, recipient, 10 * COIN)
    second = wallet.create_transaction(chain, recipient, 10 * COIN)

    spent_by_first = {tx_input.prevout for tx_input in first.inputs}
    spent_by_second = {tx_input.prevout for tx_input in second.inputs}
    assert not spent_by_first & spent_by_second

    # Both must be admissible together.
    pool = Mempool(REGTEST)
    pool.accept(first, chain)
    pool.accept(second, chain)


def test_a_released_payment_frees_its_coins(chain, wallet):
    fund(chain, wallet)  # exactly one spendable output
    recipient = crypto.pubkey_hash_to_address(
        crypto.hash160(b"recipient"), REGTEST.bech32_hrp
    )

    first = wallet.create_transaction(chain, recipient, 10 * COIN)
    with pytest.raises(WalletError):  # the only coin is reserved
        wallet.create_transaction(chain, recipient, 10 * COIN)

    wallet.release(first)
    retry = wallet.create_transaction(chain, recipient, 10 * COIN)
    assert retry.inputs[0].prevout == first.inputs[0].prevout


def test_multiple_outputs_are_gathered_when_one_is_not_enough(chain, wallet):
    fund(chain, wallet, blocks=3)
    recipient = crypto.pubkey_hash_to_address(
        crypto.hash160(b"big-purchase"), REGTEST.bech32_hrp
    )

    tx = wallet.create_transaction(chain, recipient, int(REWARD * 2.5))
    assert len(tx.inputs) == 3

    pool = Mempool(REGTEST)
    pool.accept(tx, chain)  # and every input is properly signed


def test_dust_change_is_left_as_fee_rather_than_created(chain, wallet):
    fund(chain, wallet)
    recipient = crypto.pubkey_hash_to_address(
        crypto.hash160(b"recipient"), REGTEST.bech32_hrp
    )

    # Ask for almost everything: what remains after the fee is under the dust
    # line (fee at default rate for 1-in/2-out is 2,180 shards, so 3,000 total
    # slack leaves 820 shards of would-be change).
    tx = wallet.create_transaction(chain, recipient, REWARD - 3_000)

    assert len(tx.outputs) == 1  # no dust output was created
    pool = Mempool(REGTEST)
    fee = pool.accept(tx, chain)
    assert fee == 3_000  # estimated fee plus the abandoned dust


# --------------------------------------------------------------------------
# Deleting a key is the only irreversible thing a wallet can do to itself, so
# the guards get more tests than the operation.
# --------------------------------------------------------------------------


def test_an_unfunded_address_can_be_forgotten(chain, wallet):
    spare = wallet.new_address()
    assert spare in wallet.addresses()

    wallet.forget_address(spare, chain)

    assert spare not in wallet.addresses()
    assert not wallet.owns_address(spare)
    # And the deletion is durable, not just in memory.
    assert spare not in Wallet.load(wallet.path, "hunter2").addresses()


def test_a_funded_address_is_never_forgotten(chain, wallet):
    """The whole reason this operation needs guarding: coins would vanish."""
    funded = wallet.new_address()
    pkh = crypto.address_to_pubkey_hash(funded, REGTEST.bech32_hrp)
    mine(chain, pkh)
    mine(chain, crypto.hash160(b"someone-else"), count=REGTEST.coinbase_maturity)

    with pytest.raises(WalletError, match="holds"):
        wallet.forget_address(funded, chain)

    assert funded in wallet.addresses()
    assert wallet.balance(chain).total > 0


def test_the_default_address_is_never_forgotten(chain, wallet):
    """_order[0] receives mining rewards and every payment's change.

    Deleting it silently redirects both, which must never be a side effect of
    tidying an address list.
    """
    default = wallet.addresses()[0]
    wallet.new_address()  # so it is not merely the last-address guard firing

    with pytest.raises(WalletError, match="default"):
        wallet.forget_address(default, chain)

    assert wallet.addresses()[0] == default


def test_the_last_address_is_never_forgotten(chain, wallet):
    """A wallet with no keys cannot receive, spend, or mine — and
    default_pubkey_hash would raise on the node's next block."""
    only = wallet.addresses()[0]
    assert len(wallet.addresses()) == 1

    with pytest.raises(WalletError, match="only address"):
        wallet.forget_address(only, chain)

    assert wallet.addresses() == [only]
    assert wallet.default_pubkey_hash()  # still answers


def test_forgetting_an_address_the_wallet_does_not_hold_is_refused(chain, wallet):
    stranger = Wallet(REGTEST, [b"\x33" * 32]).addresses()[0]
    with pytest.raises(WalletError, match="does not hold"):
        wallet.forget_address(stranger, chain)


def test_forgetting_a_malformed_address_is_refused(chain, wallet):
    with pytest.raises(WalletError):
        wallet.forget_address("rtobsd1nonsense", chain)


def test_forgetting_does_not_disturb_the_remaining_keys(chain, wallet):
    """Deletion must remove one key and leave every other one intact."""
    a = wallet.new_address()
    b = wallet.new_address()
    before = wallet.addresses()

    wallet.forget_address(a, chain)

    assert wallet.addresses() == [x for x in before if x != a]
    assert b in wallet.addresses()
    # The surviving keys must still sign — a corrupted key store would not.
    reloaded = Wallet.load(wallet.path, "hunter2")
    assert reloaded.addresses() == wallet.addresses()
