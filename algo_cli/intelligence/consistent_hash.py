"""Consistent Hashing — minimal redistribution on topology change.

Distributes keys across N nodes using a ring of virtual nodes (vnodes).
When a node is added or removed, only K/N keys need to move (where K is
total keys and N is node count), vs. K keys with naive mod-N hashing.

Harness use:
  - Route requests across multiple Ollama instances
  - Route embedding batches across multiple model endpoints
  - Distribute harness indexing work across worker processes
  - Session affinity — keep the same user on the same node

Operations:
  - add_node(node): add a node to the ring
  - remove_node(node): remove a node from the ring
  - get_node(key): find the node responsible for a key
  - get_nodes(key, count): get the next N nodes (for replication)

Properties:
  - O(log V) lookup where V = num_nodes * vnodes_per_node
  - Monotonic: only keys mapped to removed/added nodes move
  - Load balanced via virtual nodes
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any


class ConsistentHashRing:
    """Consistent hash ring with virtual nodes.

    Args:
        vnodes_per_node: Number of virtual nodes per physical node.
            Higher = better load distribution, more memory.
    """

    def __init__(self, vnodes_per_node: int = 150) -> None:
        self.vnodes_per_node = vnodes_per_node
        self._ring: dict[int, str] = {}  # hash -> node_name
        self._sorted_hashes: list[int] = []
        self._nodes: set[str] = set()

    def _hash(self, key: str) -> int:
        """Hash a key to a 64-bit integer."""
        return int.from_bytes(
            hashlib.md5(key.encode("utf-8")).digest()[:8],
            "little",
        )

    def add_node(self, node: str) -> None:
        """Add a node to the ring with vnodes_per_node virtual nodes."""
        if node in self._nodes:
            return
        self._nodes.add(node)
        for i in range(self.vnodes_per_node):
            vnode_key = f"{node}#{i}"
            h = self._hash(vnode_key)
            self._ring[h] = node
        self._sorted_hashes = sorted(self._ring.keys())

    def remove_node(self, node: str) -> None:
        """Remove a node and all its virtual nodes."""
        if node not in self._nodes:
            return
        self._nodes.discard(node)
        for i in range(self.vnodes_per_node):
            vnode_key = f"{node}#{i}"
            h = self._hash(vnode_key)
            self._ring.pop(h, None)
        self._sorted_hashes = sorted(self._ring.keys())

    def get_node(self, key: Any) -> str | None:
        """Find the node responsible for a key.

        Returns the first node clockwise from the key's hash position.
        Returns None if the ring is empty.
        """
        if not self._sorted_hashes:
            return None
        h = self._hash(str(key))
        # Binary search for the first hash >= h
        lo, hi = 0, len(self._sorted_hashes)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._sorted_hashes[mid] < h:
                lo = mid + 1
            else:
                hi = mid
        # Wrap around if we passed the end
        if lo == len(self._sorted_hashes):
            lo = 0
        return self._ring[self._sorted_hashes[lo]]

    def get_nodes(self, key: Any, count: int = 3) -> list[str]:
        """Get the next *count* distinct nodes for a key (for replication).

        Walks clockwise around the ring collecting distinct nodes.
        """
        if not self._sorted_hashes or count <= 0:
            return []
        h = self._hash(str(key))
        lo, hi = 0, len(self._sorted_hashes)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._sorted_hashes[mid] < h:
                lo = mid + 1
            else:
                hi = mid
        result: list[str] = []
        seen: set[str] = set()
        n = len(self._sorted_hashes)
        for i in range(n):
            idx = (lo + i) % n
            node = self._ring[self._sorted_hashes[idx]]
            if node not in seen:
                seen.add(node)
                result.append(node)
                if len(result) >= count:
                    break
        return result

    def node_distribution(self, sample_keys: list[Any]) -> dict[str, int]:
        """Simulate key distribution across nodes.

        Useful for verifying load balance.
        """
        dist: dict[str, int] = defaultdict(int)
        for key in sample_keys:
            node = self.get_node(key)
            if node:
                dist[node] += 1
        return dict(dist)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def ring_size(self) -> int:
        return len(self._ring)

    def stats(self) -> dict[str, Any]:
        return {
            "nodes": list(self._nodes),
            "node_count": len(self._nodes),
            "vnodes_per_node": self.vnodes_per_node,
            "ring_size": len(self._ring),
        }