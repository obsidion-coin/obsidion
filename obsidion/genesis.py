"""The genesis block — the first block of the chain, which has no parent.

Every other block is accepted because it builds on a block already accepted.
Genesis has nothing beneath it, so it is accepted by definition: it is hardcoded
into the software, and agreeing on it is what makes two nodes part of the same
network.

Obsidion derives genesis rather than storing it. Given a fixed timestamp,
message and difficulty, mining is a deterministic search from nonce zero, so
every node independently computes a bit-identical genesis block. There is no
constant to copy incorrectly and no opportunity for a distributed binary to
carry a subtly different one.

**The genesis reward is unspendable.** Its output is locked to a hash of twenty
zero bytes, and no public key hashes to that value. Those 50 OBSD can never
move, and the supply figures reported by the explorer count them even though
nobody can ever touch them. This is deliberate: the chain begins with nobody
holding anything, and the first spendable coin in existence is one somebody
mined.
"""

from __future__ import annotations

from functools import lru_cache

from obsidion.block import Block, BlockHeader
from obsidion.consensus import subsidy
from obsidion.merkle import merkle_root
from obsidion.params import NetworkParams
from obsidion.transaction import Transaction

#: Provably unspendable: no public key is known to hash to twenty zero bytes.
UNSPENDABLE = b"\x00" * 20


def genesis_coinbase(params: NetworkParams) -> Transaction:
    """The single transaction inside the genesis block."""
    return Transaction.coinbase(
        height=0,
        reward=subsidy(0, params),
        pubkey_hash=UNSPENDABLE,
        message=params.genesis_message,
    )


@lru_cache(maxsize=None)
def create_genesis_block(params: NetworkParams) -> Block:
    """Produce the genesis block for `params`.

    Deterministic either way: with `genesis_nonce` set the stored answer is
    verified, and without one the nonce is searched from zero, which lands on
    the same value every time. Cached because it never changes for a network.

    The stored nonce exists for cost, not trust. Under a memory-hard mining
    algorithm the search runs to tens of thousands of expensive hashes, and
    paying that on every node start-up would put twenty-five seconds in front
    of a user opening their wallet. Verifying costs a single hash. A test
    re-derives every network's nonce from scratch, so a wrong value in
    params.py cannot go unnoticed.
    """
    coinbase = genesis_coinbase(params)
    header = BlockHeader(
        prev_hash=b"\x00" * 32,
        merkle_root=merkle_root([coinbase.txid()]),
        timestamp=params.genesis_timestamp,
        bits=params.pow_limit_bits,
        nonce=params.genesis_nonce or 0,
    )

    if params.genesis_nonce is not None:
        if not header.satisfies_pow(params.pow_algorithm):
            raise RuntimeError(
                f"the stored genesis nonce for {params.name} does not satisfy "
                "its proof of work; params.py has been edited inconsistently"
            )
        return Block(header, [coinbase])

    while not header.satisfies_pow(params.pow_algorithm):
        header.nonce += 1
        if header.nonce > 0xFFFFFFFF:  # pragma: no cover — impossible at pow_limit
            raise RuntimeError(
                f"exhausted the nonce space mining genesis for {params.name}; "
                "pow_limit_bits is set too hard"
            )

    return Block(header, [coinbase])


def genesis_hash(params: NetworkParams) -> bytes:
    """The hash that identifies this network's chain from its very first block."""
    return create_genesis_block(params).hash()
