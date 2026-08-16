"""Watch the Obsidion chain for deposits, and feed them to the ledger.

This is the half of the bridge that reads. It hands out deposit addresses,
notices coins arriving at them, waits until those coins are buried deep enough
to be safe, and marks them ready for the Solana side to mint against.

**It never signs anything.** Deposits are deliberately left where they land
rather than swept into one custody address, which means the entire wrapping
path holds no spending authority at all — this module and the mint that follows
it can be compromised without a coin moving. The cost is that custody is the
sum of many addresses rather than one, which is still publicly verifiable:

    wOBSD in circulation == OBSD across all deposit addresses

Not sweeping buys a second thing, quieter but just as valuable. If deposits
were swept, a UTXO disappearing from a deposit address would be ambiguous —
our own sweep, or a reorg erasing a deposit we already credited? Leaving them
in place makes disappearance mean exactly one thing, and it is always
serious.

The decision logic lives in `reconcile`, which is a pure function of (tip
height, what the chain shows, what we already know). No RPC, no clock, no
randomness — so every rule it enforces can be tested with dictionaries, which
matters more here than anywhere else in the project.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bridge.ledger import BridgeLedger, Deposit, State
from obsidion.rpc import to_shards
from obsidion.rpcclient import rpc

log = logging.getLogger("bridge.watcher")

#: Deposits smaller than this are ignored. A Solana mint costs a fee whoever
#: pays it, so crediting dust would cost the bridge more than the deposit is
#: worth — and that is a cheap way for someone to bleed it.
DEFAULT_MINIMUM_SHARDS = 1_000_000  # 0.01 OBSD


@dataclass
class Actions:
    """What `reconcile` decided, as data the caller applies.

    Keeping the decision separate from the doing is what makes the rules
    testable without a chain — and what stops a subtle branch hiding inside an
    RPC call.
    """

    #: Deposits seen on-chain that the ledger should record.
    record: list[Deposit] = field(default_factory=list)
    #: (key, reason) for deposits that vanished before they were ever minted.
    abandon: list[tuple[str, str]] = field(default_factory=list)
    #: Human-readable disasters. A deposit that vanished *after* it was minted
    #: leaves wrapped tokens with nothing behind them; no code should quietly
    #: paper over that, so it comes back as text for a person to read.
    alerts: list[str] = field(default_factory=list)
    #: Outputs deliberately not credited, with why. Kept for the operator's
    #: sake: "my deposit did not arrive" is otherwise unanswerable.
    ignored: list[tuple[str, str]] = field(default_factory=list)


def reconcile(
    tip_height: int,
    seen: dict[str, list[dict]],
    known: dict[str, State],
    destinations: dict[str, str],
    *,
    minimum_shards: int = DEFAULT_MINIMUM_SHARDS,
) -> Actions:
    """Decide what to do about the current state of the chain.

    `seen` maps a deposit address to the UTXOs currently sitting on it, in the
    shape `getaddressutxos` returns. `known` maps an event key to the state the
    ledger already has for it. `destinations` maps deposit address to Solana
    address — an address with no destination is not ours and is never credited.
    """
    actions = Actions()
    still_present: set[str] = set()

    for address, utxos in seen.items():
        destination = destinations.get(address)
        if destination is None:
            # Not a deposit address we handed out. Somebody else's coins, or a
            # mapping we lost; either way, crediting it would invent a payee.
            actions.ignored.append((address, "no destination assigned"))
            continue

        for utxo in utxos:
            key = f"{utxo['txid']}:{utxo['index']}"

            if utxo.get("coinbase"):
                # A mining reward landing here is not somebody's deposit.
                # Crediting one would mint wOBSD nobody paid for.
                actions.ignored.append((key, "coinbase output, not a deposit"))
                continue

            try:
                amount = to_shards(utxo["amount"])
            except (ValueError, KeyError) as exc:
                actions.ignored.append((key, f"unreadable amount: {exc}"))
                continue

            if amount < minimum_shards:
                actions.ignored.append(
                    (key, f"below the {minimum_shards} shard minimum")
                )
                continue

            still_present.add(key)
            if key in known:
                continue  # already recorded; re-seeing it is the normal case

            actions.record.append(
                Deposit(
                    txid=utxo["txid"],
                    index=utxo["index"],
                    amount=amount,
                    deposit_address=address,
                    solana_address=destination,
                    height=utxo["height"],
                )
            )

    # Anything the ledger is holding that the chain no longer shows.
    for key, state in known.items():
        if key in still_present or state in (State.ABANDONED, State.DONE):
            continue
        actions.abandon.append((key, "no longer in the active chain"))

    for key, state in known.items():
        if state is State.DONE and key not in still_present:
            # The wrapped tokens exist and the deposit backing them is gone.
            # The ledger refuses to abandon a DONE event for exactly this
            # reason; the operator has to look.
            actions.alerts.append(
                f"{key} was minted but is no longer on-chain - wrapped tokens "
                "may be unbacked. Stop minting and reconcile custody."
            )

    return actions


class ObsidionWatcher:
    """The thin I/O shell around `reconcile`. Safe to run on a timer."""

    def __init__(
        self,
        ledger: BridgeLedger,
        rpc_port: int,
        token: str,
        *,
        minimum_shards: int = DEFAULT_MINIMUM_SHARDS,
    ):
        self.ledger = ledger
        self.rpc_port = rpc_port
        self.token = token
        self.minimum_shards = minimum_shards

    def _call(self, method: str, *params):
        return rpc(self.rpc_port, method, *params, token=self.token)

    def allocate_deposit_address(self, solana_address: str) -> str:
        """Give this Solana wallet an Obsidion address of its very own.

        An Obsidion transaction has nowhere to carry a memo, so the address is
        the routing: coins arriving here belong to `solana_address` and nobody
        else. Recorded before it is returned — handing out an address the
        ledger does not know about would make an incoming deposit
        unattributable.
        """
        address = self._call("getnewaddress")
        self.ledger.assign_destination(address, solana_address)
        return address

    def poll(self) -> Actions:
        """One pass: read the chain, decide, apply. Idempotent by construction."""
        tip_height = self._call("getinfo")["height"]

        destinations = dict(
            self.ledger.db.execute(
                "SELECT deposit_address, solana_address FROM destinations"
            ).fetchall()
        )
        seen = {
            address: self._call("getaddressutxos", address)["utxos"]
            for address in destinations
        }
        known = {
            key: State(state)
            for key, state in self.ledger.db.execute(
                "SELECT key, state FROM events WHERE kind = 'deposit'"
            ).fetchall()
        }

        actions = reconcile(
            tip_height,
            seen,
            known,
            destinations,
            minimum_shards=self.minimum_shards,
        )
        self.apply(actions, tip_height)
        return actions

    def apply(self, actions: Actions, tip_height: int) -> None:
        """Write a decision into the ledger, then promote whatever is deep enough."""
        for deposit in actions.record:
            self.ledger.record_deposit(deposit)
            log.info(
                "deposit %s for %s: %d shards at height %d",
                deposit.key,
                deposit.solana_address,
                deposit.amount,
                deposit.height,
            )

        for key, reason in actions.abandon:
            self.ledger.abandon(key, reason)
            log.warning("abandoned %s: %s", key, reason)

        for message in actions.alerts:
            log.error("BRIDGE ALERT: %s", message)

        for key in self.ledger.promote_confirmed(tip_height):
            log.info("deposit %s is confirmed and ready to mint", key)
