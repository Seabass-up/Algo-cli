"""H32 — Pre-Push Scrubbing Gate.

Hard-blocks raw pushes of private working tree; requires explicit env var
override.  Mirrors T3MP3ST ``.githooks/pre-push`` pattern.

Source: T3MP3ST ``.githooks/pre-push``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class GateResult:
    """Result of a pre-push gate check."""

    allowed: bool
    reason: str
    override_used: bool


@dataclass(frozen=True)
class ScrubEvidence:
    """Bound evidence produced by a structured outgoing-diff scanner."""

    digest: str
    scanned_chars: int
    findings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return (
            len(self.digest) == 64
            and all(char in "0123456789abcdef" for char in self.digest.lower())
            and self.scanned_chars >= 0
            and not self.findings
        )


class PrePushGate:
    """Gate that blocks pushes unless an override env var is set."""

    def __init__(self, override_env_var: str = "ALGO_ALLOW_RAW_PUSH") -> None:
        self.override_env_var = override_env_var

    def check(
        self,
        env_getter: Callable[[str], Optional[str]] = lambda _k: None,
        allow_override: bool = False,
        scrub_evidence: ScrubEvidence | None = None,
    ) -> GateResult:
        """Check whether a push should be allowed.

        Args:
            env_getter: Function that returns env var values (defaults to None).
            allow_override: If True, skip the env var check entirely.
            scrub_evidence: Evidence from the structured outgoing-diff scanner.

        Returns:
            GateResult with allowed=True if push should proceed.
        """
        if allow_override:
            return GateResult(
                allowed=True,
                reason="Override explicitly granted by caller.",
                override_used=True,
            )
        if scrub_evidence is not None and scrub_evidence.valid:
            return GateResult(
                allowed=True,
                reason=(
                    "Structured publish path completed its outgoing-diff scrub "
                    f"({scrub_evidence.digest[:12]})."
                ),
                override_used=False,
            )
        env_value = env_getter(self.override_env_var)
        if env_value and env_value.lower() in ("1", "true", "yes"):
            return GateResult(
                allowed=True,
                reason=f"Override env var {self.override_env_var} is set.",
                override_used=True,
            )
        return GateResult(
            allowed=False,
            reason=(
                f"Raw push blocked. Set {self.override_env_var}=1 to override, "
                "or use a scrubbed export path."
            ),
            override_used=False,
        )

    def require_override(self, env_getter: Callable[[str], Optional[str]] = lambda _k: None) -> bool:
        """Return True if an override is required to push."""
        result = self.check(env_getter=env_getter)
        return not result.allowed
