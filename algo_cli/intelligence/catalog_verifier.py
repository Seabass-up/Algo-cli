"""H2 — Algorithm Catalog Verifier.

Re-derive every `implemented` status from live tests.
Mined from T3MP3ST verify-claims.
"""

from __future__ import annotations

import re
from collections import defaultdict
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
    unchecked_count: int = 0

    @property
    def all_verified(self) -> bool:
        return self.total_entries > 0 and self.verified_count == self.total_entries and self.failed_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [r.to_dict() for r in self.results],
            "total_entries": self.total_entries,
            "verified_count": self.verified_count,
            "failed_count": self.failed_count,
            "unchecked_count": self.unchecked_count,
            "all_verified": self.all_verified,
        }


@dataclass(frozen=True)
class CatalogDiagnostic:
    """One deterministic structural problem in the Markdown catalog."""

    code: str
    message: str
    line_number: int
    entry_id: str | None = None
    severity: str = "error"

    def format(self) -> str:
        entry = f" [{self.entry_id}]" if self.entry_id else ""
        return f"{self.severity.upper()} {self.code} line {self.line_number}{entry}: {self.message}"


@dataclass
class CatalogLintReport:
    """Structural diagnostics for one complete ALGO.md payload."""

    diagnostics: list[CatalogDiagnostic] = field(default_factory=list)

    @property
    def all_valid(self) -> bool:
        return not any(diagnostic.severity == "error" for diagnostic in self.diagnostics)


class CatalogVerifier:
    """Parse ALGO.md and verify claimed statuses against live tests."""

    # Pattern IDs may use a multi-letter namespace and one lowercase suffix.
    # Accepted headings use either ``ID. Title`` or ``ID — Title``.
    _ENTRY_RE = re.compile(r"^###\s+(?P<id>[A-Z]+\d+[a-z]?)(?:\.\s+|\s+[—–-]\s+)(?P<title>\S.*)\s*$")
    _PATTERNISH_HEADING_RE = re.compile(r"^###\s+[A-Z]+\d")
    _SECTION_RE = re.compile(r"^##(?!#)\s+(?P<title>\S.*)\s*$")
    _NAMESPACE_RE = re.compile(r"^\*\*Pattern namespace:\*\*\s+(?P<names>.+?)\s*$")
    _FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})")
    # Match status markers
    _STATUS_RE = re.compile(
        r"^\s*(?:\*\*)?(?:Status|Evidence state):(?:\*\*)?\s*`?"
        r"(implemented|wired|proposed|planned|partial|retired|preview|unknown)\b",
        re.IGNORECASE,
    )

    def parse_catalog(self, markdown_text: str) -> list[CatalogEntry]:
        """Parse ALGO.md markdown and return catalog entries."""
        entries, _ = self._scan_catalog(markdown_text)
        return entries

    def lint_catalog(self, markdown_text: str) -> CatalogLintReport:
        """Return blocking diagnostics for deterministic catalog structure."""
        entries, diagnostics = self._scan_catalog(markdown_text)
        entries_by_id: dict[str, list[CatalogEntry]] = defaultdict(list)
        for entry in entries:
            entries_by_id[entry.id].append(entry)
        for entry_id, occurrences in entries_by_id.items():
            first = occurrences[0]
            for duplicate in occurrences[1:]:
                diagnostics.append(
                    CatalogDiagnostic(
                        code="duplicate_id",
                        entry_id=entry_id,
                        line_number=duplicate.line_number,
                        message=f"pattern ID first appears at line {first.line_number}",
                    )
                )
        diagnostics.sort(key=lambda diagnostic: (diagnostic.line_number, diagnostic.code, diagnostic.entry_id or ""))
        return CatalogLintReport(diagnostics=diagnostics)

    def _scan_catalog(self, markdown_text: str) -> tuple[list[CatalogEntry], list[CatalogDiagnostic]]:
        lines = markdown_text.splitlines(keepends=True)
        headings: list[tuple[str, str, int, str]] = []
        statuses: dict[int, str] = {}
        current_entry: int | None = None
        diagnostics: list[CatalogDiagnostic] = []
        section = ""
        namespaces: tuple[str, ...] = ()
        fence_character: str | None = None
        fence_length = 0
        fence_line = 0

        for line_number, line in enumerate(lines, start=1):
            stripped = line.rstrip("\r\n")
            fence_match = self._FENCE_RE.match(stripped)
            if fence_match:
                marker = fence_match.group("fence")
                if fence_character is None:
                    fence_character = marker[0]
                    fence_length = len(marker)
                    fence_line = line_number
                elif marker[0] == fence_character and len(marker) >= fence_length:
                    fence_character = None
                    fence_length = 0
                    fence_line = 0
                continue
            if fence_character is not None:
                continue

            section_match = self._SECTION_RE.match(stripped)
            if section_match:
                section = section_match.group("title").strip()
                namespaces = ()
                current_entry = None
                continue

            namespace_match = self._NAMESPACE_RE.match(stripped)
            if namespace_match:
                namespaces = tuple(re.findall(r"`([A-Z]+)`", namespace_match.group("names")))
                if not namespaces:
                    diagnostics.append(
                        CatalogDiagnostic(
                            code="invalid_namespace_declaration",
                            line_number=line_number,
                            message="declare one or more uppercase namespaces in backticks",
                        )
                    )
                continue

            entry_match = self._ENTRY_RE.match(stripped)
            if entry_match:
                entry_id = entry_match.group("id")
                headings.append((entry_id, entry_match.group("title").strip(), line_number, section))
                current_entry = line_number
                prefix_match = re.match(r"[A-Z]+", entry_id)
                prefix = prefix_match.group(0) if prefix_match else ""
                if namespaces and prefix not in namespaces:
                    diagnostics.append(
                        CatalogDiagnostic(
                            code="namespace_mismatch",
                            entry_id=entry_id,
                            line_number=line_number,
                            message=(
                                f"section '{section}' declares namespace(s) {', '.join(namespaces)}; "
                                f"found {prefix or 'none'}"
                            ),
                        )
                    )
                continue

            status_match = self._STATUS_RE.match(stripped)
            if current_entry is not None and status_match:
                status = status_match.group(1).lower()
                status = "implemented" if status == "wired" else status
                if current_entry in statuses and statuses[current_entry] != status:
                    diagnostics.append(CatalogDiagnostic(
                        code="conflicting_status", line_number=line_number,
                        message="pattern has competing explicit statuses; retain unknown until reconciled",
                    ))
                    statuses[current_entry] = "unknown"
                else:
                    statuses[current_entry] = status

            if self._PATTERNISH_HEADING_RE.match(stripped):
                current_entry = None
                diagnostics.append(
                    CatalogDiagnostic(
                        code="malformed_pattern_heading",
                        line_number=line_number,
                        message="use '### ID. Title' or '### ID — Title'",
                    )
                )

        if fence_character is not None:
            diagnostics.append(
                CatalogDiagnostic(
                    code="unclosed_fence",
                    line_number=fence_line,
                    message="Markdown code fence is not closed",
                )
            )

        entries: list[CatalogEntry] = []
        for entry_id, title, line_number, entry_section in headings:
            status = statuses.get(line_number, "unknown")
            entries.append(
                CatalogEntry(
                    id=entry_id,
                    title=title,
                    status=status,
                    section=entry_section,
                    line_number=line_number,
                )
            )
        return entries, diagnostics

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
        unchecked = 0
        for entry in entries:
            if entry.status != "implemented":
                results.append(
                    VerificationResult(
                        entry_id=entry.id,
                        claimed_status=entry.status,
                        verified=False,
                        reason=f"Status is '{entry.status}' — implementation not verified",
                    )
                )
                unchecked += 1
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
            elif test_passed is True:
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
            unchecked_count=unchecked,
        )
