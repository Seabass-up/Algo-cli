"""H2 — Algorithm Catalog Verifier.

Re-derive every `implemented` status from live tests.
Mined from T3MP3ST verify-claims.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CatalogEntry:
    """A single catalog entry parsed from ALGO.md."""

    id: str
    title: str
    status: str = "unknown"  # "implemented", "proposed", "partial", "retired"
    section: str = ""
    line_number: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "section": self.section,
            "line_number": self.line_number,
            "metadata": dict(self.metadata),
        }


@dataclass
class VerificationResult:
    """Result of verifying a single catalog entry."""

    entry_id: str
    claimed_status: str
    verified: bool
    reason: str = ""
    test_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "claimed_status": self.claimed_status,
            "verified": self.verified,
            "reason": self.reason,
            "test_names": list(self.test_names),
        }


@dataclass
class VerificationReport:
    """Full report of catalog verification."""

    results: list[VerificationResult] = field(default_factory=list)
    total_entries: int = 0
    verified_count: int = 0
    failed_count: int = 0

    @property
    def all_verified(self) -> bool:
        return self.failed_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [r.to_dict() for r in self.results],
            "total_entries": self.total_entries,
            "verified_count": self.verified_count,
            "failed_count": self.failed_count,
            "all_verified": self.all_verified,
        }


class CatalogVerifier:
    """Parse ALGO.md and verify claimed statuses against live tests."""

    # Match entries like "### H1. Title", "### H1 — Title", or "### H1 Title"
    _ENTRY_RE = re.compile(r"^###\s+([A-Z]\d+)\.?\s*[—–\-\s]\s*(.+)$", re.MULTILINE)
    # Match status markers
    _STATUS_RE = re.compile(r"\b(implemented|proposed|partial|retired)\b", re.IGNORECASE)

    def parse_catalog(self, markdown_text: str) -> list[CatalogEntry]:
        """Parse ALGO.md markdown and return catalog entries."""
        entries: list[CatalogEntry] = []
        for match in self._ENTRY_RE.finditer(markdown_text):
            entry_id = match.group(1)
            title = match.group(2).strip()
            line_num = markdown_text[: match.start()].count("\n") + 1
            # Look for status in the next ~500 chars
            after_text = markdown_text[match.end() : match.end() + 500]
            status_match = self._STATUS_RE.search(after_text)
            status = status_match.group(1).lower() if status_match else "unknown"
            entries.append(
                CatalogEntry(
                    id=entry_id,
                    title=title,
                    status=status,
                    line_number=line_num,
                )
            )
        return entries

    def verify(
        self,
        entries: list[CatalogEntry],
        test_results: dict[str, bool] | None = None,
    ) -> VerificationReport:
        """Verify entries against test results.

        Args:
            entries: Parsed catalog entries.
            test_results: Map of entry_id → test_passed. If None, all claimed
                          "implemented" entries without tests are flagged.
        """
        test_results = test_results or {}
        results: list[VerificationResult] = []
        verified = 0
        failed = 0
        for entry in entries:
            if entry.status != "implemented":
                results.append(
                    VerificationResult(
                        entry_id=entry.id,
                        claimed_status=entry.status,
                        verified=True,
                        reason=f"Status is '{entry.status}' — no verification needed",
                    )
                )
                verified += 1
                continue
            # Check if we have test results
            test_passed = test_results.get(entry.id)
            if test_passed is None:
                results.append(
                    VerificationResult(
                        entry_id=entry.id,
                        claimed_status=entry.status,
                        verified=False,
                        reason="Claimed 'implemented' but no test result provided",
                    )
                )
                failed += 1
            elif test_passed:
                results.append(
                    VerificationResult(
                        entry_id=entry.id,
                        claimed_status=entry.status,
                        verified=True,
                        reason="Test passed",
                    )
                )
                verified += 1
            else:
                results.append(
                    VerificationResult(
                        entry_id=entry.id,
                        claimed_status=entry.status,
                        verified=False,
                        reason="Test failed",
                    )
                )
                failed += 1
        return VerificationReport(
            results=results,
            total_entries=len(entries),
            verified_count=verified,
            failed_count=failed,
        )