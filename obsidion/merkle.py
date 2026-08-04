"""The merkle tree that binds a block header to the transactions inside it.

A block header is only 80 bytes, yet it must commit to every transaction in the
block. It does so by storing a single hash: the root of a binary tree built by
hashing transaction ids in pairs, then hashing those results in pairs, until one
hash remains. Change any transaction and the root changes, so the header — and
therefore the proof-of-work spent on it — becomes invalid.

The structure also allows a lightweight wallet to prove a transaction is in a
block by supplying only the sibling hashes along one path, rather than the whole
block. That is `merkle_proof` and `verify_proof` below.
"""

from __future__ import annotations

from obsidion import crypto

CVE_2012_2459_MESSAGE = "duplicate trailing transaction pair"


def _pair_hash(left: bytes, right: bytes) -> bytes:
    return crypto.sha256d(left + right)


def merkle_root(txids: list[bytes]) -> bytes:
    """Compute the merkle root of a list of transaction ids.

    Rows with an odd number of hashes duplicate their final entry, which keeps
    the tree binary. That rule carries a subtle flaw (CVE-2012-2459): a list whose last
    two hashes are already identical produces the same root as the shorter list
    without them. An attacker can exploit this to build a block that looks
    different but hashes identically, tricking nodes into permanently marking the
    honest block invalid. Obsidion rejects such lists outright.
    """
    if not txids:
        raise ValueError("cannot compute a merkle root of zero transactions")

    row = list(txids)
    while len(row) > 1:
        if len(row) % 2 == 1:
            # About to duplicate the final hash. If the row already ends in a
            # matching pair, this is the malleability case described above.
            if len(row) >= 3 and row[-1] == row[-2]:
                raise ValueError(CVE_2012_2459_MESSAGE)
            row.append(row[-1])

        row = [_pair_hash(row[i], row[i + 1]) for i in range(0, len(row), 2)]

    return row[0]


def merkle_proof(txids: list[bytes], index: int) -> list[tuple[bytes, bool]]:
    """Build the sibling path proving `txids[index]` belongs to the tree.

    Returns a list of `(sibling_hash, sibling_is_left)` pairs, ordered from the
    leaf upward. Verifying needs only this path and the root, not the block.
    """
    if not txids:
        raise ValueError("cannot build a proof against zero transactions")
    if not 0 <= index < len(txids):
        raise IndexError(f"index {index} outside 0..{len(txids) - 1}")

    path: list[tuple[bytes, bool]] = []
    row = list(txids)
    position = index

    while len(row) > 1:
        if len(row) % 2 == 1:
            if len(row) >= 3 and row[-1] == row[-2]:
                raise ValueError(CVE_2012_2459_MESSAGE)
            row.append(row[-1])

        sibling_is_left = position % 2 == 1
        sibling = row[position - 1] if sibling_is_left else row[position + 1]
        path.append((sibling, sibling_is_left))

        row = [_pair_hash(row[i], row[i + 1]) for i in range(0, len(row), 2)]
        position //= 2

    return path


def verify_proof(txid: bytes, path: list[tuple[bytes, bool]], root: bytes) -> bool:
    """Check that `txid` combines with `path` to reproduce `root`."""
    current = txid
    for sibling, sibling_is_left in path:
        current = (
            _pair_hash(sibling, current)
            if sibling_is_left
            else _pair_hash(current, sibling)
        )
    return current == root
