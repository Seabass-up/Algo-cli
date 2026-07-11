"""H9 — Ground-Truth Artifact Binding.

Every claim binds to a raw artifact with a content hash.
Mined from GLOSSOPETRAE 78 raw JSONs + T3MP3ST bench/.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


def _compute_hash(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


@dataclass
class ArtifactBinding:
    """A binding between a claim and a raw artifact."""

    claim_id: str
    artifact_path: str
    content_hash: str
    artifact_type: str = "json"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "artifact_path": self.artifact_path,
            "content_hash": self.content_hash,
            "artifact_type": self.artifact_type,
            "metadata": dict(self.metadata),
        }


class ArtifactBinder:
    """Bind claims to raw artifacts with content hashes."""

    def __init__(self) -> None:
        self._bindings: dict[str, ArtifactBinding] = {}

    def bind(
        self,
        claim_id: str,
        artifact_path: str,
        content: bytes | str,
        artifact_type: str = "json",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactBinding:
        content_hash = _compute_hash(content)
        binding = ArtifactBinding(
            claim_id=claim_id,
            artifact_path=artifact_path,
            content_hash=content_hash,
            artifact_type=artifact_type,
            metadata=metadata or {},
        )
        self._bindings[claim_id] = binding
        return binding

    def verify(self, claim_id: str, content: bytes | str) -> bool:
        binding = self._bindings.get(claim_id)
        if binding is None:
            return False
        return _compute_hash(content) == binding.content_hash

    def get(self, claim_id: str) -> ArtifactBinding | None:
        return self._bindings.get(claim_id)

    def all(self) -> list[ArtifactBinding]:
        return list(self._bindings.values())

    def count(self) -> int:
        return len(self._bindings)

    def remove(self, claim_id: str) -> bool:
        return self._bindings.pop(claim_id, None) is not None