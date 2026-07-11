"""H14 — Symmetric Encode/Decode Verification.

Every creator pattern needs a verifier companion.
Mined from ST3GG dual-use design.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SymmetricPair:
    """A creator/verifier pair of algorithm entries."""

    creator_id: str
    verifier_id: str
    creator_title: str = ""
    verifier_title: str = ""
    metadata: dict[str, field] = field(default_factory=dict)

    def to_dict(self) -> dict[str, str]:
        return {
            "creator_id": self.creator_id,
            "verifier_id": self.verifier_id,
            "creator_title": self.creator_title,
            "verifier_title": self.verifier_title,
        }


class SymmetricVerifier:
    """Check that every creator entry has a matching verifier entry."""

    def __init__(self) -> None:
        self._pairs: list[SymmetricPair] = []

    def register(
        self,
        creator_id: str,
        verifier_id: str,
        creator_title: str = "",
        verifier_title: str = "",
    ) -> SymmetricPair:
        pair = SymmetricPair(
            creator_id=creator_id,
            verifier_id=verifier_id,
            creator_title=creator_title,
            verifier_title=verifier_title,
        )
        self._pairs.append(pair)
        return pair

    def check_symmetry(self, entry_ids: set[str]) -> list[str]:
        """Return list of unpaired creator IDs."""
        missing = []
        for pair in self._pairs:
            if pair.creator_id in entry_ids and pair.verifier_id not in entry_ids:
                missing.append(pair.creator_id)
        return missing

    def get_pairs(self) -> list[SymmetricPair]:
        return list(self._pairs)

    def count(self) -> int:
        return len(self._pairs)

    def find_pair(self, creator_id: str) -> SymmetricPair | None:
        for pair in self._pairs:
            if pair.creator_id == creator_id:
                return pair
        return None