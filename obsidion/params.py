"""Network parameters for Obsidion.

Every magic number that defines what Obsidion *is* lives in this file. Renaming the
coin, retuning the economics, or adding a new network is a change here and nowhere
else.

Three networks ship by default:

  mainnet  the real chain: 60-second blocks, halving every 210,000 blocks
  testnet  same shape, faster halvings, throwaway coins
  regtest  for development and tests: trivial proof-of-work, 10-block halvings,
           so an entire halving schedule can be exercised in under a second

Nodes on different networks can never talk to each other: the 4-byte `magic`
prefixes every P2P message, and a mismatch drops the connection immediately.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

COIN_NAME = "Obsidion"
TICKER = "OBSD"

#: Name of the smallest indivisible unit. Obsidion has no fractions of a shard.
SUBUNIT_NAME = "shard"

#: Shards in one OBSD — eight decimal places of precision.
#: All amounts everywhere in this codebase are integers in shards — never floats.
#: Money and floating point must never meet.
COIN = 100_000_000


@dataclass(frozen=True)
class NetworkParams:
    """The complete consensus and network configuration for one chain."""

    # Identity
    name: str
    bech32_hrp: str
    """Human-readable part of addresses, e.g. 'obsd' gives 'obsd1q...'."""

    magic: bytes
    """4-byte prefix on every P2P message. Isolates this network from all others."""

    default_port: int
    default_rpc_port: int

    # Economics
    initial_subsidy: int
    """Block reward at height 0, in shards."""

    halving_interval: int
    """Blocks between reward halvings."""

    # Proof of work
    target_block_time: int
    """Desired seconds between blocks. Difficulty retargets toward this."""

    retarget_interval: int
    """Blocks per difficulty adjustment period."""

    max_retarget_factor: int
    """Difficulty may not change by more than this factor in one period.
    Clamps both directions, so an attacker cannot whipsaw difficulty."""

    pow_limit_bits: int
    """Easiest permitted target, in compact 'bits' form. Difficulty may never
    fall below this, and the genesis block is mined at exactly this value."""

    pow_algorithm: str
    """Name of the mining hash, from `crypto.POW_ALGORITHMS`.

    'scrypt-2mb' is the real one: memory-hard, so a hashing core needs two
    megabytes of its own RAM and custom silicon gains roughly ten times over a
    CPU instead of a hundred million. 'sha256d' is kept for regtest, where
    thousands of blocks are mined in the test suite and the memory-hard cost
    would turn seconds into hours."""

    # Safety rules
    coinbase_maturity: int
    """Blocks a mining reward must age before it can be spent. Protects against
    spending rewards that a chain reorganisation would erase."""

    max_block_weight: int
    """Maximum serialized block size in bytes."""

    median_time_blocks: int
    """A block's timestamp must exceed the median of this many previous blocks.
    Stops a miner lying about time to force an easier difficulty."""

    max_future_block_time: int
    """Seconds a block timestamp may run ahead of local clock before rejection.

    Must stay small relative to `retarget_interval * target_block_time`. The
    retarget measures how long the last period actually took using raw
    timestamps, so a miner allowed to post-date a boundary block by a large
    fraction of that window can inflate the measured span and pull the next
    difficulty down. Keeping the allowance to a few block times bounds the
    distortion to noise."""

    # Genesis
    genesis_timestamp: int
    genesis_message: bytes
    """Embedded in the genesis coinbase. Carrying a dated, externally
    verifiable phrase proves the chain was not started before that date —
    nobody can produce a genesis block predating the words inside it."""

    seed_nodes: tuple[tuple[str, int], ...] = ()
    """Addresses a fresh node dials to find the network.

    This is the bootstrap problem: a node with no peers and no way to learn any
    is simply alone. One reachable seed is enough — the node asks it for other
    addresses, and from there discovery is self-sustaining. Seeds hold no
    special authority and validate nothing on anyone's behalf; they are only a
    first phone number. Anyone can run one, and a node that already knows peers
    never needs them again.

    Fill these in before publishing binaries, or every user must pass
    --connect by hand."""

    seed_nodes: tuple[str, ...] = ()
    """Addresses a fresh node dials when it knows nobody yet, as "host:port".

    This is the bootstrap problem: peer discovery works by asking peers, which
    is useless before you have one. Every node that joins learns the wider
    network from these, then gossips addresses on its own — so seeds matter
    enormously on day one and barely at all once the network has depth.

    Keep at least two, on different providers. A network whose only seed is
    offline is a network nobody new can join, however many nodes are running."""

    genesis_nonce: int | None = None
    """The winning nonce for this network's genesis block, if already found.

    Left as None for a brand-new network, in which case genesis is mined on
    first use. Filled in afterwards so start-up verifies with a single hash
    instead of repeating the search — which under a memory-hard algorithm
    costs tens of thousands of expensive hashes every time a node boots.
    `tests/test_genesis.py` re-derives each value from scratch, so a wrong
    entry here fails the suite rather than shipping."""

    @property
    def max_supply(self) -> int:
        """Theoretical maximum supply in shards.

        Note this is an upper bound, not the exact figure. Because each halving
        uses integer division, the true total is fractionally lower — for the
        default parameters it lands at 20,999,999.9769 rather than a round
        21 million. `consensus.total_supply()` computes the exact value.
        """
        return self.initial_subsidy * self.halving_interval * 2


MAINNET = NetworkParams(
    name="mainnet",
    bech32_hrp="obsd",
    magic=b"\x0b\x5d\x51\xc3",
    default_port=9444,
    default_rpc_port=9445,
    initial_subsidy=50 * COIN,
    halving_interval=210_000,          # ~1 year at 150s blocks
    target_block_time=150,             # 2.5 minutes, as Litecoin
    retarget_interval=120,             # ~5 hours
    max_retarget_factor=4,
    pow_limit_bits=0x1F0FFFFF,         # ~4,096 scrypt hashes: ~25s for one CPU
    pow_algorithm="scrypt-2mb",
    coinbase_maturity=100,
    max_block_weight=1_000_000,
    median_time_blocks=11,
    max_future_block_time=10 * 60,     # well under the 5h retarget window
    genesis_timestamp=1_785_628_800,   # 2026-08-02T00:00:00Z
    genesis_message=b"Obsidion genesis - forged under pressure, 02 Aug 2026",
    # Fill these in with your own always-on nodes before announcing the
    # network. Until then a new node can only join via --connect.
    seed_nodes=(),
    genesis_nonce=1451,
)

TESTNET = NetworkParams(
    name="testnet",
    bech32_hrp="tobsd",
    magic=b"\x1a\x3f\x77\x9e",
    default_port=19444,
    default_rpc_port=19445,
    initial_subsidy=50 * COIN,
    halving_interval=1_000,            # halvings arrive in under a day
    target_block_time=30,
    retarget_interval=60,
    max_retarget_factor=4,
    pow_limit_bits=0x1F0FFFFF,
    pow_algorithm="scrypt-2mb",
    coinbase_maturity=10,
    max_block_weight=1_000_000,
    median_time_blocks=11,
    max_future_block_time=5 * 60,      # under the 30m retarget window
    genesis_timestamp=1_785_628_800,
    genesis_message=b"Obsidion testnet",
    genesis_nonce=1424,
)

REGTEST = NetworkParams(
    name="regtest",
    bech32_hrp="rtobsd",
    magic=b"\xda\xb5\xbf\xfa",
    default_port=19555,
    default_rpc_port=19556,
    initial_subsidy=50 * COIN,
    halving_interval=10,               # watch four halvings in a single test
    target_block_time=1,
    retarget_interval=10_000,          # effectively never retargets
    max_retarget_factor=4,
    pow_limit_bits=0x207FFFFF,         # any hash passes; mining is instant
    pow_algorithm="sha256d",           # keeps the test suite in seconds
    coinbase_maturity=2,
    max_block_weight=1_000_000,
    median_time_blocks=11,
    max_future_block_time=600,
    genesis_timestamp=1_785_628_800,
    genesis_message=b"Obsidion regtest",
    genesis_nonce=6,
)

NETWORKS: dict[str, NetworkParams] = {
    MAINNET.name: MAINNET,
    TESTNET.name: TESTNET,
    REGTEST.name: REGTEST,
}


def get_network(name: str) -> NetworkParams:
    """Look up a network by name, raising a helpful error for typos."""
    try:
        return NETWORKS[name]
    except KeyError:
        known = ", ".join(sorted(NETWORKS))
        raise ValueError(f"unknown network {name!r}; expected one of: {known}") from None
