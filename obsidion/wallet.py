"""The wallet: keys, balances, and building signed transactions.

A wallet is nothing but private keys. The coins themselves live in the chain's
UTXO set; the wallet merely knows which of those outputs it can unlock, and can
produce the signatures that unlock them. Lose the keys and the coins still
exist — provably, forever — but no signature can ever move them again.

**Key file encryption.** Keys rest on disk encrypted under a password:

    key material = scrypt(password, salt)          — memory-hard, so brute
                                                     force is expensive
    ciphertext   = keys XOR HMAC-SHA256 keystream  — CTR-mode stream cipher
                                                     built on a standard PRF
    tag          = HMAC-SHA256(mac_key, ciphertext) — encrypt-then-MAC

The stream cipher is built from HMAC rather than AES because Python's standard
library ships no AES, and a wallet should not drag in a C dependency for one
file. HMAC-SHA256 as a PRF in counter mode is a textbook-sound construction;
what it costs is speed, which is irrelevant for a few kilobytes of keys. The
tag is checked before any decryption is trusted, so a wrong password and a
corrupted file produce the same clean error instead of garbage keys.

**Coin selection** is largest-first: it minimises input count, which minimises
fees. Selected outputs are reserved in memory so two payments built in quick
succession cannot promise the same coin twice; reservations clear naturally
when the spend confirms (the output vanishes from the chain) or on restart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from obsidion import crypto
from obsidion.chainstate import ChainState, UTXOEntry
from obsidion.params import NetworkParams, get_network
from obsidion.transaction import OutPoint, Transaction, TxIn, TxOut

#: Change smaller than this is folded into the fee — an output worth less than
#: the cost of ever spending it is litter, not money.
DUST = 1_000

#: Default fee rate in shards per (estimated) byte.
DEFAULT_FEE_RATE = 10

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def _associated_data(network: str) -> bytes:
    """Envelope fields that are stored in the clear but must still be
    authenticated, so they cannot be edited without invalidating the tag."""
    return json.dumps({"coin": "Obsidion", "network": network}, sort_keys=True).encode()


class WalletError(Exception):
    """The wallet cannot do what was asked — wrong password, missing funds,
    malformed address. Always safe to show the message to the user."""


@dataclass(frozen=True)
class Balance:
    spendable: int
    """Confirmed and mature: can go into a transaction right now."""

    immature: int
    """Mining rewards still aging toward coinbase maturity."""

    @property
    def total(self) -> int:
        return self.spendable + self.immature


# --------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------


def _derive_keys(password: str, salt: bytes) -> tuple[bytes, bytes]:
    material = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=64,
    )
    return material[:32], material[32:]  # cipher key, MAC key


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = []
    for counter in range((length + 31) // 32):
        blocks.append(
            hmac.new(key, nonce + counter.to_bytes(8, "little"), hashlib.sha256).digest()
        )
    return b"".join(blocks)[:length]


def _encrypt(password: str, plaintext: bytes, associated: bytes = b"") -> dict:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    cipher_key, mac_key = _derive_keys(password, salt)

    ciphertext = bytes(
        a ^ b for a, b in zip(plaintext, _keystream(cipher_key, nonce, len(plaintext)))
    )
    tag = hmac.new(mac_key, nonce + ciphertext + associated, hashlib.sha256).digest()
    return {
        "kdf": "scrypt",
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
        "tag": tag.hex(),
    }


def _decrypt(password: str, blob: dict, associated: bytes = b"") -> bytes:
    try:
        salt = bytes.fromhex(blob["salt"])
        nonce = bytes.fromhex(blob["nonce"])
        ciphertext = bytes.fromhex(blob["ciphertext"])
        tag = bytes.fromhex(blob["tag"])
    except (KeyError, ValueError) as exc:
        raise WalletError(f"wallet file is malformed: {exc}") from exc

    cipher_key, mac_key = _derive_keys(password, salt)
    expected = hmac.new(
        mac_key, nonce + ciphertext + associated, hashlib.sha256
    ).digest()
    # The tag is verified before a single byte is decrypted, in constant time.
    # `associated` covers the plaintext envelope fields — chiefly the network
    # name, which decides how addresses are derived. Leaving it unauthenticated
    # let anyone flip a wallet from testnet to mainnet with a text editor, and
    # the node would follow along.
    if not hmac.compare_digest(tag, expected):
        raise WalletError("wrong password (or the wallet file is corrupted)")

    return bytes(
        a ^ b
        for a, b in zip(ciphertext, _keystream(cipher_key, nonce, len(ciphertext)))
    )


# --------------------------------------------------------------------------
# The wallet
# --------------------------------------------------------------------------


def _estimate_size(inputs: int, outputs: int) -> int:
    """Rough serialized size: enough accuracy to set a fee, nothing more."""
    return 10 + 150 * inputs + 29 * outputs


class Wallet:
    """A password-protected keyring bound to one network."""

    def __init__(
        self,
        params: NetworkParams,
        private_keys: list[bytes],
        path: Path | None = None,
        password: str | None = None,
    ):
        self.params = params
        self.path = path
        self._password = password
        self._keys: dict[bytes, tuple[bytes, bytes]] = {}  # pkh -> (priv, pub)
        self._order: list[bytes] = []  # pkhs, oldest first
        #: Guards key creation and the save that persists it. The RPC server is
        #: threaded, so two clients can call getnewaddress at the same instant;
        #: without this the second save overwrote the first, discarding a key
        #: whose address had already been handed out.
        self._lock = threading.RLock()
        for private_key in private_keys:
            self._register(private_key)
        #: Outpoints promised to not-yet-confirmed transactions.
        self._reserved: set[OutPoint] = set()

    def _register(self, private_key: bytes) -> bytes:
        public_key = crypto.private_to_public(private_key)
        pubkey_hash = crypto.hash160(public_key)
        if pubkey_hash not in self._keys:
            self._keys[pubkey_hash] = (private_key, public_key)
            self._order.append(pubkey_hash)
        return pubkey_hash

    # -------------------------------------------------------------- lifecycle

    @classmethod
    def create(cls, path: str | Path, password: str, params: NetworkParams) -> Wallet:
        """Make a fresh wallet with one address and write it to disk."""
        path = Path(path)
        if path.exists():
            raise WalletError(f"refusing to overwrite existing wallet at {path}")

        wallet = cls(params, [crypto.generate_private_key()], path, password)
        wallet.save()
        return wallet

    @classmethod
    def load(cls, path: str | Path, password: str) -> Wallet:
        path = Path(path)
        try:
            envelope = json.loads(path.read_text())
        except FileNotFoundError:
            raise WalletError(f"no wallet at {path}") from None
        except json.JSONDecodeError as exc:
            raise WalletError(f"wallet file is not valid JSON: {exc}") from exc

        network = envelope.get("network", "")
        params = get_network(network)
        plaintext = _decrypt(
            password, envelope.get("crypto", {}), _associated_data(network)
        )
        keys = [bytes.fromhex(k) for k in json.loads(plaintext)["keys"]]
        return cls(params, keys, path, password)

    def save(self) -> None:
        """Persist the keys, durably.

        Three properties this has to hold, because the file may be the only
        copy of money that exists:

        * **Atomic.** Written to a temporary file and renamed into place, so a
          crash mid-write leaves the previous wallet intact rather than a
          half-file.
        * **Durable.** fsynced before the rename, and the directory fsynced
          after. Without this the rename can reach disk while the contents
          have not, and a power cut leaves an empty wallet where the keys were.
        * **Concurrency-safe.** The temporary file carries a unique name, so
          two simultaneous saves cannot write over each other's scratch file.
        """
        if self.path is None or self._password is None:
            raise WalletError("wallet has no file path to save to")

        with self._lock:
            plaintext = json.dumps(
                {"keys": [self._keys[pkh][0].hex() for pkh in self._order]}
            ).encode()
            envelope = {
                "coin": "Obsidion",
                "network": self.params.name,
                "crypto": _encrypt(
                    self._password, plaintext, _associated_data(self.params.name)
                ),
            }

            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=f"{self.path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w") as stream:
                    stream.write(json.dumps(envelope, indent=2))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise

            # Persist the rename itself. Not available on Windows, where
            # os.replace is already atomic on NTFS.
            try:
                directory = os.open(str(self.path.parent), os.O_RDONLY)
            except (OSError, AttributeError):
                return
            try:
                os.fsync(directory)
            except OSError:
                pass
            finally:
                os.close(directory)

    # -------------------------------------------------------------- addresses

    def new_address(self) -> str:
        """Generate a key, persist it, and return its address.

        Key creation and the save that records it happen under one lock. An
        address must never be returned to a caller before the key that unlocks
        it is safely on disk — anything sent to it would be unspendable.
        """
        with self._lock:
            pubkey_hash = self._register(crypto.generate_private_key())
            if self.path is not None:
                self.save()
            return crypto.pubkey_hash_to_address(pubkey_hash, self.params.bech32_hrp)

    def addresses(self) -> list[str]:
        return [
            crypto.pubkey_hash_to_address(pkh, self.params.bech32_hrp)
            for pkh in self._order
        ]

    def default_pubkey_hash(self) -> bytes:
        """The wallet's first key — where mining rewards and change land."""
        return self._order[0]

    def owns_address(self, address: str) -> bool:
        try:
            return (
                crypto.address_to_pubkey_hash(address, self.params.bech32_hrp)
                in self._keys
            )
        except ValueError:
            return False

    # ---------------------------------------------------------------- balance

    def _utxos(self, chain: ChainState) -> list[tuple[OutPoint, UTXOEntry]]:
        found: list[tuple[OutPoint, UTXOEntry]] = []
        for pubkey_hash in self._order:
            found.extend(chain.utxos_for_pubkey_hash(pubkey_hash))
        return found

    def _is_mature(self, entry: UTXOEntry, chain: ChainState) -> bool:
        if not entry.is_coinbase:
            return True
        age_at_next_block = chain.height + 1 - entry.height
        return age_at_next_block >= self.params.coinbase_maturity

    def balance(self, chain: ChainState) -> Balance:
        spendable = immature = 0
        for _, entry in self._utxos(chain):
            if self._is_mature(entry, chain):
                spendable += entry.amount
            else:
                immature += entry.amount
        return Balance(spendable, immature)

    def spendable_utxos(self, chain: ChainState) -> list[tuple[OutPoint, UTXOEntry]]:
        """Mature, confirmed, and not already promised to a pending payment."""
        # Reservations for outputs that no longer exist have served their
        # purpose (the spend confirmed); let them go.
        live = {outpoint for outpoint, _ in self._utxos(chain)}
        self._reserved &= live

        return [
            (outpoint, entry)
            for outpoint, entry in self._utxos(chain)
            if self._is_mature(entry, chain) and outpoint not in self._reserved
        ]

    # ----------------------------------------------------------- transactions

    def create_transaction(
        self,
        chain: ChainState,
        to_address: str,
        amount: int,
        fee_rate: int = DEFAULT_FEE_RATE,
    ) -> Transaction:
        """Build and sign a payment of `amount` shards to `to_address`.

        Change returns to the wallet's first address. Raises WalletError if the
        address is malformed or on the wrong network, or if mature funds cannot
        cover amount plus fee.
        """
        if amount <= 0:
            raise WalletError("amount must be positive")
        try:
            to_pubkey_hash = crypto.address_to_pubkey_hash(
                to_address, self.params.bech32_hrp
            )
        except ValueError as exc:
            raise WalletError(str(exc)) from exc

        candidates = sorted(
            self.spendable_utxos(chain), key=lambda pair: pair[1].amount, reverse=True
        )

        selected: list[tuple[OutPoint, UTXOEntry]] = []
        gathered = 0
        for outpoint, entry in candidates:
            if gathered >= amount + fee_rate * _estimate_size(len(selected), 2):
                break
            selected.append((outpoint, entry))
            gathered += entry.amount

        fee = fee_rate * _estimate_size(len(selected), 2)
        if gathered < amount + fee:
            have = sum(entry.amount for _, entry in candidates)
            raise WalletError(
                f"insufficient funds: need {amount + fee} shards "
                f"(amount + fee), have {have} spendable"
            )

        outputs = [TxOut(amount, to_pubkey_hash)]
        change = gathered - amount - fee
        if change >= DUST:
            outputs.append(TxOut(change, self._order[0]))
        # Sub-dust change is abandoned to the miner as extra fee.

        tx = Transaction(
            inputs=[TxIn(outpoint) for outpoint, _ in selected],
            outputs=outputs,
        )
        for index, (_, entry) in enumerate(selected):
            private_key, public_key = self._keys[entry.pubkey_hash]
            tx.sign_input(index, private_key, public_key, entry.amount)

        for outpoint, _ in selected:
            self._reserved.add(outpoint)
        return tx

    def release(self, tx: Transaction) -> None:
        """Un-reserve a transaction's inputs after it was refused somewhere.
        Without this, a rejected payment would strand its coins until restart."""
        for tx_input in tx.inputs:
            self._reserved.discard(tx_input.prevout)
