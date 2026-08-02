"""Hashing, keys, signatures and address encoding.

Everything cryptographic that the rest of Obsidion depends on lives here, behind
a small interface. Two deliberate choices:

* **The proof-of-work hash is a named function, not an inlined call.** Swapping
  SHA-256d for a memory-hard algorithm later is a change to `pow_hash` alone.
* **`verify` never raises.** Signature data arrives from untrusted peers; a
  malformed signature is an invalid signature, not a crash. A node that could be
  killed by a malformed packet is a node that can be knocked off the network.
"""

from __future__ import annotations

import hashlib

from ecdsa import BadSignatureError, SigningKey, VerifyingKey, SECP256k1
from ecdsa.util import sigdecode_der, sigencode_der_canonize

from obsidion import _ripemd160

# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------


def sha256(data: bytes) -> bytes:
    """Single SHA-256."""
    return hashlib.sha256(data).digest()


def sha256d(data: bytes) -> bytes:
    """Double SHA-256, the workhorse hash of Bitcoin and of Obsidion.

    Used for transaction ids, block hashes, merkle nodes and proof-of-work.
    Hashing twice defends against length-extension attacks on the raw
    construction.
    """
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def ripemd160(data: bytes) -> bytes:
    """RIPEMD-160, preferring the platform implementation.

    OpenSSL 3 moved RIPEMD-160 into the legacy provider, so many Python builds
    cannot offer it. Consensus rules cannot depend on a machine's build options,
    so a pure-Python implementation stands behind it.
    """
    try:
        return hashlib.new("ripemd160", data).digest()
    except (ValueError, TypeError):
        return _ripemd160.ripemd160(data)


def hash160(data: bytes) -> bytes:
    """RIPEMD-160 of SHA-256 — how a public key becomes an address.

    The double hashing shortens the key to 20 bytes and means a break in either
    algorithm alone does not expose the key behind an unspent address.
    """
    return ripemd160(sha256(data))


def pow_hash(header_bytes: bytes) -> bytes:
    """The proof-of-work hash.

    Kept separate from `sha256d` even though it currently delegates to it: this
    is the single point that would change if Obsidion ever adopts a memory-hard,
    ASIC-resistant algorithm. Consensus code calls this, never `sha256d`.
    """
    return sha256d(header_bytes)


def hash_to_int(digest: bytes) -> int:
    """Interpret a hash as a little-endian integer, for comparison against a target.

    Little-endian because that is how Bitcoin displays and compares block hashes,
    and matching the convention avoids a whole class of subtle bugs.
    """
    return int.from_bytes(digest, "little")


# --------------------------------------------------------------------------
# Keys and signatures
# --------------------------------------------------------------------------

#: Order of the secp256k1 group. Valid private keys are in [1, ORDER).
ORDER = SECP256k1.order


def generate_private_key() -> bytes:
    """A fresh 32-byte private key from the operating system's secure RNG."""
    return SigningKey.generate(curve=SECP256k1).to_string()


def private_to_public(private_key: bytes) -> bytes:
    """Derive the 33-byte compressed public key from a private key."""
    signing_key = SigningKey.from_string(private_key, curve=SECP256k1)
    return signing_key.get_verifying_key().to_string("compressed")


def sign(private_key: bytes, digest: bytes) -> bytes:
    """Sign a 32-byte digest, returning a DER-encoded signature.

    Uses RFC 6979 deterministic nonces. This is not a stylistic preference: ECDSA
    leaks the private key outright if the same nonce is ever reused across two
    signatures, and deterministic derivation removes any dependence on the
    quality of the random number generator at signing time.

    Signatures are canonicalised to low-S form so that a third party cannot take
    a valid signature, flip S, and produce a different-looking but still-valid
    signature for the same transaction — the malleability bug that forced
    Bitcoin's BIP-62 work.
    """
    signing_key = SigningKey.from_string(private_key, curve=SECP256k1)
    return signing_key.sign_digest_deterministic(
        digest, hashfunc=hashlib.sha256, sigencode=sigencode_der_canonize
    )


def verify(public_key: bytes, signature: bytes, digest: bytes) -> bool:
    """Check a DER signature against a digest and compressed public key.

    Returns False for anything that does not verify, including malformed keys and
    malformed signatures. Never raises — see the module docstring.
    """
    try:
        verifying_key = VerifyingKey.from_string(public_key, curve=SECP256k1)
        return verifying_key.verify_digest(signature, digest, sigdecode=sigdecode_der)
    except (BadSignatureError, ValueError, IndexError, AssertionError):
        return False


# --------------------------------------------------------------------------
# bech32 (BIP-173)
# --------------------------------------------------------------------------

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = (checksum & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            checksum ^= generator[i] if ((top >> i) & 1) else 0
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_verify_checksum(hrp: str, data: list[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _bech32_create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def bech32_encode(hrp: str, data: list[int]) -> str:
    """Encode 5-bit groups into a bech32 string with checksum."""
    combined = data + _bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join(CHARSET[d] for d in combined)


def bech32_decode(bech: str) -> tuple[str | None, list[int] | None]:
    """Decode and validate a bech32 string.

    Returns `(None, None)` for anything invalid rather than raising, so callers
    can distinguish "not a valid address" from a programming error.
    """
    if any(ord(c) < 33 or ord(c) > 126 for c in bech):
        return (None, None)
    if bech.lower() != bech and bech.upper() != bech:
        return (None, None)  # mixed case is forbidden: it breaks the checksum
    bech = bech.lower()

    position = bech.rfind("1")
    if position < 1 or position + 7 > len(bech) or len(bech) > 90:
        return (None, None)
    if not all(c in CHARSET for c in bech[position + 1 :]):
        return (None, None)

    hrp = bech[:position]
    data = [CHARSET.find(c) for c in bech[position + 1 :]]
    if not _bech32_verify_checksum(hrp, data):
        return (None, None)
    return (hrp, data[:-6])


def convertbits(
    data: bytes | list[int], from_bits: int, to_bits: int, pad: bool = True
) -> list[int] | None:
    """Regroup a bit stream, e.g. bytes (8-bit) into bech32 groups (5-bit)."""
    accumulator = 0
    bits = 0
    result: list[int] = []
    max_value = (1 << to_bits) - 1
    max_accumulator = (1 << (from_bits + to_bits - 1)) - 1

    for value in data:
        if value < 0 or (value >> from_bits):
            return None
        accumulator = ((accumulator << from_bits) | value) & max_accumulator
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)

    if pad:
        if bits:
            result.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        return None
    return result


# --------------------------------------------------------------------------
# Obsidion addresses
# --------------------------------------------------------------------------


def pubkey_hash_to_address(pubkey_hash: bytes, hrp: str) -> str:
    """Encode a 20-byte public key hash as an Obsidion address."""
    if len(pubkey_hash) != 20:
        raise ValueError(f"public key hash must be 20 bytes, got {len(pubkey_hash)}")
    converted = convertbits(pubkey_hash, 8, 5)
    if converted is None:  # pragma: no cover — unreachable for 20 valid bytes
        raise ValueError("could not convert public key hash to bech32 groups")
    return bech32_encode(hrp, converted)


def public_key_to_address(public_key: bytes, hrp: str) -> str:
    """Turn a compressed public key into a spendable address."""
    return pubkey_hash_to_address(hash160(public_key), hrp)


def address_to_pubkey_hash(address: str, hrp: str) -> bytes:
    """Decode an address back to its 20-byte public key hash.

    Raises ValueError if the address is malformed, fails its checksum, or belongs
    to a different network. Each of those is a case where proceeding would send
    money somewhere unrecoverable, so none of them may pass silently.
    """
    decoded_hrp, data = bech32_decode(address)
    if decoded_hrp is None or data is None:
        raise ValueError(f"not a valid address: {address!r}")
    if decoded_hrp != hrp:
        raise ValueError(
            f"address belongs to a different network: expected prefix {hrp!r}, "
            f"got {decoded_hrp!r}"
        )

    converted = convertbits(data, 5, 8, pad=False)
    if converted is None or len(converted) != 20:
        raise ValueError(f"address does not contain a 20-byte hash: {address!r}")
    return bytes(converted)


def is_valid_address(address: str, hrp: str) -> bool:
    """True if the address is well-formed and belongs to this network."""
    try:
        address_to_pubkey_hash(address, hrp)
        return True
    except ValueError:
        return False
