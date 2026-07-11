"""HyperLogLog — probabilistic cardinality estimation.

Estimates the number of distinct items in a stream using O(2^p) memory
where p is the precision parameter.  Standard error is ~1.04/sqrt(2^p).

Harness use:
  - Count unique files accessed in a session
  - Count unique error types encountered
  - Count unique tool call paths
  - Count unique URLs fetched
  - Track cardinality of harness index entries

Operations:
  - add(item): O(1) — update registers
  - estimate(): O(2^p) — compute cardinality estimate
  - merge(other): merge two HLLs (for parallel/distributed counting)

Properties:
  - Memory: 2^p bytes (p=14 → 16KB for ~0.8% error)
  - No false negatives or positives — it's an estimate with bounded error
  - Mergeable: HLL(A ∪ B) = merge(HLL(A), HLL(B))
"""
from __future__ import annotations

import hashlib
import math
from typing import Any


class HyperLogLog:
    """HyperLogLog cardinality estimator.

    Args:
        precision: Number of bits used for register index (4-16).
            2^precision registers are used.  Standard error ≈ 1.04/2^(p/2).
    """

    def __init__(self, precision: int = 14) -> None:
        if not 4 <= precision <= 16:
            raise ValueError("precision must be between 4 and 16")
        self.precision = precision
        self.num_registers = 1 << precision
        self._registers = bytearray(self.num_registers)
        self._alpha = self._get_alpha(self.num_registers)

    @staticmethod
    def _get_alpha(m: int) -> float:
        """Bias correction constant."""
        if m == 16:
            return 0.673
        elif m == 32:
            return 0.697
        elif m == 64:
            return 0.709
        else:
            return 0.7213 / (1 + 1.079 / m)

    def _hash(self, item: Any) -> int:
        """Hash item to a 64-bit integer."""
        data = str(item).encode("utf-8")
        return int.from_bytes(hashlib.md5(data).digest()[:8], "little")

    @staticmethod
    def _count_leading_zeros(value: int, bits: int) -> int:
        """Count leading zeros in the lower *bits* bits of value."""
        if value == 0:
            return bits
        count = 0
        for i in range(bits - 1, -1, -1):
            if (value >> i) & 1:
                break
            count += 1
        return count + 1  # +1 for the position of the first 1

    def add(self, item: Any) -> None:
        """Add an item to the HLL."""
        h = self._hash(item)
        # Use the lower p bits as register index
        reg_idx = h & (self.num_registers - 1)
        # Use the remaining bits for the leading-zeros count
        remaining = h >> self.precision
        remaining_bits = 64 - self.precision
        lz = self._count_leading_zeros(remaining, remaining_bits)
        if lz > self._registers[reg_idx]:
            self._registers[reg_idx] = lz

    def estimate(self) -> float:
        """Estimate the cardinality (number of distinct items)."""
        # Raw estimate
        sum_inv = sum(2 ** (-r) for r in self._registers)
        raw = self._alpha * (self.num_registers ** 2) / sum_inv

        # Small range correction
        if raw <= 2.5 * self.num_registers:
            # Count zero registers
            zero_count = sum(1 for r in self._registers if r == 0)
            if zero_count > 0:
                # Linear counting
                return self.num_registers * math.log(
                    self.num_registers / zero_count
                )

        # Large range correction
        if raw > (1 << 32) / 30:
            return -(1 << 32) * math.log(1 - raw / (1 << 32))

        return raw

    def merge(self, other: HyperLogLog) -> None:
        """Merge another HLL into this one (take max of each register)."""
        if self.precision != other.precision:
            raise ValueError("Cannot merge HLLs with different precision")
        for i in range(self.num_registers):
            if other._registers[i] > self._registers[i]:
                self._registers[i] = other._registers[i]

    def reset(self) -> None:
        """Reset all registers to zero."""
        self._registers = bytearray(self.num_registers)

    def stats(self) -> dict[str, Any]:
        return {
            "precision": self.precision,
            "num_registers": self.num_registers,
            "memory_bytes": len(self._registers),
            "estimate": round(self.estimate(), 1),
            "standard_error": round(1.04 / math.sqrt(self.num_registers), 5),
        }