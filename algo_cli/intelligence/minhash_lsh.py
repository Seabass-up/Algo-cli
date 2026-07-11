"""MinHash + LSH — approximate near-duplicate detection.

MinHash estimates Jaccard similarity between sets using k random hash
functions.  LSH (Locality-Sensitive Hashing) buckets similar signatures
into the same band-slot for O(1) candidate lookup.

Harness use:
  - Detect near-duplicate files (rebranded code, copied configs)
  - Find similar conversation contexts for dedup
  - Cluster similar error messages
  - Detect duplicate harness entries

Operations:
  - MinHasher.signature(set): compute k-element signature
  - MinHasher.similarity(sig1, sig2): estimated Jaccard similarity
  - LSHIndex.insert(key, signature): add to LSH buckets
  - LSHIndex.query(signature): return candidate near-duplicate keys

Properties:
  - Signature computation: O(k * |set|)
  - Similarity estimation: O(k)
  - LSH query: O(bands) expected, where bands = signature_size / rows_per_band
  - Trade-off: more bands = higher recall, more false positives
"""
from __future__ import annotations

import hashlib
from typing import Any

from dataclasses import dataclass, field


class MinHasher:
    """MinHash signature generator.

    Args:
        num_hashes: Number of hash functions (signature size).
            Higher = more accurate similarity estimation, more memory.
        seed: Random seed for reproducibility.
    """

    def __init__(self, num_hashes: int = 128, seed: int = 42) -> None:
        self.num_hashes = num_hashes
        self._seeds = [(seed + i * 2654435761) & 0xFFFFFFFF for i in range(num_hashes)]

    def _hash(self, item: Any, seed: int) -> int:
        """Hash an item with a given seed to a 32-bit integer."""
        data = f"{seed}:{item}".encode("utf-8")
        return int.from_bytes(hashlib.md5(data).digest()[:4], "big")

    def signature(self, items: set[Any] | list[Any]) -> list[int]:
        """Compute the MinHash signature of a set of items.

        Returns a list of num_hashes integers, where each is the minimum
        hash value for that hash function across all items.
        """
        if not items:
            return [0xFFFFFFFF] * self.num_hashes
        items_set = set(items)
        sig = []
        for seed in self._seeds:
            min_val = min(self._hash(item, seed) for item in items_set)
            sig.append(min_val)
        return sig

    def similarity(self, sig1: list[int], sig2: list[int]) -> float:
        """Estimate Jaccard similarity from two signatures.

        Jaccard(A, B) = |A ∩ B| / |A ∪ B|
        MinHash estimates this as the fraction of matching hash positions.
        """
        if len(sig1) != len(sig2):
            raise ValueError("Signatures must have the same length")
        if not sig1:
            return 0.0
        matches = sum(1 for a, b in zip(sig1, sig2) if a == b)
        return matches / len(sig1)


@dataclass
class LSHIndex:
    """Locality-Sensitive Hashing index for MinHash signatures.

    Splits the signature into bands.  Two signatures are candidates if
    they share at least one band hash.  This provides sublinear query time.

    Args:
        num_bands: Number of bands (more = higher recall, more false positives).
        rows_per_band: Rows per band (more = higher precision, lower recall).
            num_bands * rows_per_band should equal signature size.
    """

    num_bands: int = 32
    rows_per_band: int = 4
    _buckets: dict[tuple[int, int], set[str]] = field(default_factory=dict)
    _signatures: dict[str, list[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._expected_sig_len = self.num_bands * self.rows_per_band

    def _band_hash(self, signature: list[int], band_idx: int) -> int:
        """Hash a band of the signature to an integer."""
        start = band_idx * self.rows_per_band
        end = start + self.rows_per_band
        band = tuple(signature[start:end])
        return hash(band)

    def insert(self, key: str, signature: list[int]) -> None:
        """Insert a key with its MinHash signature into the LSH index."""
        if len(signature) != self._expected_sig_len:
            raise ValueError(
                f"Signature length {len(signature)} != expected "
                f"{self._expected_sig_len} (bands={self.num_bands} * "
                f"rows={self.rows_per_band})"
            )
        self._signatures[key] = signature
        for band_idx in range(self.num_bands):
            bh = self._band_hash(signature, band_idx)
            bucket_key = (band_idx, bh)
            if bucket_key not in self._buckets:
                self._buckets[bucket_key] = set()
            self._buckets[bucket_key].add(key)

    def remove(self, key: str) -> None:
        """Remove a key from the LSH index."""
        sig = self._signatures.pop(key, None)
        if sig is None:
            return
        for band_idx in range(self.num_bands):
            bh = self._band_hash(sig, band_idx)
            bucket_key = (band_idx, bh)
            if bucket_key in self._buckets:
                self._buckets[bucket_key].discard(key)
                if not self._buckets[bucket_key]:
                    del self._buckets[bucket_key]

    def query(self, signature: list[int]) -> list[str]:
        """Find candidate near-duplicate keys for a signature.

        Returns keys that share at least one band hash.  These are
        candidates — verify with MinHasher.similarity() to get exact estimate.
        """
        if len(signature) != self._expected_sig_len:
            raise ValueError(
                f"Signature length {len(signature)} != expected "
                f"{self._expected_sig_len}"
            )
        candidates: set[str] = set()
        for band_idx in range(self.num_bands):
            bh = self._band_hash(signature, band_idx)
            bucket_key = (band_idx, bh)
            if bucket_key in self._buckets:
                candidates.update(self._buckets[bucket_key])
        return list(candidates)

    def query_similar(
        self,
        signature: list[int],
        minhasher: MinHasher,
        threshold: float = 0.5,
    ) -> list[tuple[str, float]]:
        """Find near-duplicates above a similarity threshold.

        Returns list of (key, similarity) sorted by similarity descending.
        """
        candidates = self.query(signature)
        results: list[tuple[str, float]] = []
        for key in candidates:
            sim = minhasher.similarity(signature, self._signatures[key])
            if sim >= threshold:
                results.append((key, sim))
        results.sort(key=lambda x: -x[1])
        return results

    def stats(self) -> dict[str, Any]:
        return {
            "num_bands": self.num_bands,
            "rows_per_band": self.rows_per_band,
            "indexed_items": len(self._signatures),
            "num_buckets": len(self._buckets),
            "expected_sig_len": self._expected_sig_len,
        }