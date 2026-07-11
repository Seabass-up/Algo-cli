"""B79. Shadow Editor: LSP Diff Validation.

Diff LSP diagnostics before and after each edit to catch introduced errors.
Source: Pathfinder pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class LSPDiagnostic:
    severity: str
    message: str
    line: int
    col: int
    source: str = ""


@dataclass
class EditValidation:
    safe: bool
    introduced: list[LSPDiagnostic] = field(default_factory=list)
    resolved: list[LSPDiagnostic] = field(default_factory=list)
    unchanged: list[LSPDiagnostic] = field(default_factory=list)


class ShadowEditor:
    """Validate edits against LSP before writing to disk."""

    def __init__(self, get_diagnostics_fn: Callable[[str, str], list[LSPDiagnostic]] | None = None) -> None:
        self._get_diagnostics = get_diagnostics_fn or self._noop_diagnostics

    @staticmethod
    def _noop_diagnostics(filepath: str, content: str) -> list[LSPDiagnostic]:
        return []

    def validate_edit(self, filepath: str, old_content: str, new_content: str) -> EditValidation:
        """Validate an edit by comparing diagnostics before and after."""
        before = self._get_diagnostics(filepath, old_content)
        after = self._get_diagnostics(filepath, new_content)

        introduced = self._diff_new(before, after)
        resolved = self._diff_new(after, before)
        unchanged = self._intersect(before, after)

        return EditValidation(
            safe=len([d for d in introduced if d.severity == "error"]) == 0,
            introduced=introduced,
            resolved=resolved,
            unchanged=unchanged,
        )

    def _diff_new(self, before: list[LSPDiagnostic], after: list[LSPDiagnostic]) -> list[LSPDiagnostic]:
        """Find diagnostics in 'after' that weren't in 'before'."""
        before_keys = {(d.severity, d.message, d.line) for d in before}
        return [d for d in after if (d.severity, d.message, d.line) not in before_keys]

    def _intersect(self, before: list[LSPDiagnostic], after: list[LSPDiagnostic]) -> list[LSPDiagnostic]:
        """Find diagnostics present in both."""
        after_keys = {(d.severity, d.message, d.line) for d in after}
        return [d for d in before if (d.severity, d.message, d.line) in after_keys]

    def should_block(self, validation: EditValidation) -> bool:
        """Should the edit be blocked?"""
        return any(d.severity == "error" for d in validation.introduced)