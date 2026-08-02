"""Pure-Python RIPEMD-160, used only when the platform cannot provide it.

OpenSSL 3 moved RIPEMD-160 into its legacy provider, so `hashlib.new("ripemd160")`
fails on a good number of otherwise healthy Python builds. Address derivation is a
consensus rule: two nodes that disagree about `hash160` disagree about who owns
what. Obsidion therefore carries its own implementation rather than letting a
build flag decide whether a node can join the network.

This is a direct transcription of the RIPEMD-160 specification (Dobbertin,
Bosselaers and Preneel, 1996). It is not fast, and it does not need to be — it is
a fallback, and `crypto.ripemd160` prefers the platform version when one exists.
"""

from __future__ import annotations

import struct

# Message word order, left line and right line.
_R_LEFT = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
    3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
    1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
    4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13,
]
_R_RIGHT = [
    5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
    6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
    15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
    8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
    12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11,
]

# Rotation amounts, left line and right line.
_S_LEFT = [
    11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
    7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
    11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
    11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
    9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6,
]
_S_RIGHT = [
    8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
    9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
    9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
    15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
    8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11,
]

# Per-round additive constants.
_K_LEFT = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
_K_RIGHT = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]

_MASK = 0xFFFFFFFF
_INITIAL_STATE = (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0)


def _rotl(value: int, count: int) -> int:
    """Rotate a 32-bit word left."""
    value &= _MASK
    return ((value << count) | (value >> (32 - count))) & _MASK


def _f(round_index: int, x: int, y: int, z: int) -> int:
    """The nonlinear function, which changes every 16 rounds."""
    if round_index < 16:
        return x ^ y ^ z
    if round_index < 32:
        return (x & y) | (~x & _MASK & z)
    if round_index < 48:
        return (x | (~y & _MASK)) ^ z
    if round_index < 64:
        return (x & z) | (y & (~z & _MASK))
    return x ^ (y | (~z & _MASK))


def _compress(state: tuple[int, ...], block: bytes) -> tuple[int, ...]:
    """Mix one 64-byte block into the state via two parallel 80-round lines."""
    words = struct.unpack("<16I", block)
    h0, h1, h2, h3, h4 = state

    # Left line.
    a, b, c, d, e = h0, h1, h2, h3, h4
    # Right line, running independently over the same block.
    a2, b2, c2, d2, e2 = h0, h1, h2, h3, h4

    for j in range(80):
        block_index = j // 16

        temp = _rotl(
            (a + _f(j, b, c, d) + words[_R_LEFT[j]] + _K_LEFT[block_index]) & _MASK,
            _S_LEFT[j],
        )
        temp = (temp + e) & _MASK
        a, e, d, c, b = e, d, _rotl(c, 10), b, temp

        temp = _rotl(
            (a2 + _f(79 - j, b2, c2, d2) + words[_R_RIGHT[j]] + _K_RIGHT[block_index])
            & _MASK,
            _S_RIGHT[j],
        )
        temp = (temp + e2) & _MASK
        a2, e2, d2, c2, b2 = e2, d2, _rotl(c2, 10), b2, temp

    # Combine the two lines, rotating the state by one position as specified.
    return (
        (h1 + c + d2) & _MASK,
        (h2 + d + e2) & _MASK,
        (h3 + e + a2) & _MASK,
        (h4 + a + b2) & _MASK,
        (h0 + b + c2) & _MASK,
    )


def ripemd160(data: bytes) -> bytes:
    """Return the 20-byte RIPEMD-160 digest of `data`."""
    # Merkle-Damgard padding: a 0x80 byte, zeros, then the bit length as a
    # little-endian 64-bit integer.
    bit_length = (len(data) * 8) & 0xFFFFFFFFFFFFFFFF
    padded = data + b"\x80"
    padded += b"\x00" * ((56 - len(padded) % 64) % 64)
    padded += struct.pack("<Q", bit_length)

    state = _INITIAL_STATE
    for offset in range(0, len(padded), 64):
        state = _compress(state, padded[offset : offset + 64])

    return struct.pack("<5I", *state)
