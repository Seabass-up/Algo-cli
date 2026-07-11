"""B159, B161, B173: Acrobat-derived security patterns.

- B159: Search/Redaction Pattern Packs
- B161: Pre-Model Disqualification Rules
- B173: Native Messaging Host Allowlist
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import re


# ── B159: Search/Redaction Pattern Packs ───────────────────────────────


@dataclass
class RedactionPattern:
    """A single redaction pattern (B159)."""
    display_name: str
    regex: str
    examples: str = ""
    category: str = "general"
    severity: str = "medium"  # "low", "medium", "high"


@dataclass
class RedactionCode:
    """A legal/business reason code for redaction (B159)."""
    name: str
    description: str = ""


@dataclass
class RedactionMatch:
    """A matched span for redaction."""
    pattern_name: str
    span: tuple[int, int]
    text: str
    code: str = ""
    locale: str = "ENU"


@dataclass
class RedactionPatternPack:
    """A locale-specific pack of redaction patterns (B159)."""
    locale: str = "ENU"
    patterns: list[RedactionPattern] = field(default_factory=list)
    codes: list[RedactionCode] = field(default_factory=list)

    def scan(self, text: str, enabled_categories: set[str] | None = None) -> list[RedactionMatch]:
        """Scan text for matches against all patterns."""
        matches: list[RedactionMatch] = []
        for pattern in self.patterns:
            if enabled_categories and pattern.category not in enabled_categories:
                continue
            try:
                for m in re.finditer(pattern.regex, text):
                    matches.append(RedactionMatch(
                        pattern_name=pattern.display_name,
                        span=(m.start(), m.end()),
                        text=m.group(),
                        locale=self.locale,
                    ))
            except re.error:
                continue
        return matches


class RedactionPackRegistry:
    """Registry of locale-specific redaction packs (B159)."""

    def __init__(self) -> None:
        self._packs: dict[str, RedactionPatternPack] = {}

    def register(self, pack: RedactionPatternPack) -> None:
        self._packs[pack.locale] = pack

    def get(self, locale: str) -> RedactionPatternPack | None:
        # Try exact locale
        if locale in self._packs:
            return self._packs[locale]
        # Try language prefix
        lang = locale.split("_")[0].upper()
        if lang in self._packs:
            return self._packs[lang]
        # Fall back to ENU
        return self._packs.get("ENU")

    def scan(self, text: str, locale: str = "ENU", enabled_categories: set[str] | None = None) -> list[RedactionMatch]:
        """Scan text using the best available locale pack."""
        pack = self.get(locale)
        if not pack:
            return []
        return pack.scan(text, enabled_categories)

    def available_locales(self) -> list[str]:
        return list(self._packs.keys())


# Default patterns (ENU)
def default_enu_pack() -> RedactionPatternPack:
    """Create a default ENU redaction pack with common PII patterns."""
    return RedactionPatternPack(
        locale="ENU",
        patterns=[
            RedactionPattern(
                display_name="Phone Numbers",
                regex=r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b|\b\d{3}[-.\s]\d{4}\b",
                examples="555-123-4567, 555-1212",
                category="phone",
                severity="medium",
            ),
            RedactionPattern(
                display_name="Credit Cards",
                regex=r"(\b)(((\d{4}(-|\s|\.|_)?){3}(\d{3,4})))",
                examples="1234-5678-9012-3456",
                category="credit_card",
                severity="high",
            ),
            RedactionPattern(
                display_name="Social Security Numbers",
                regex=r"(\b)((\d{3}(-|\s|\.|_)\d{2}(-|\s|\.|_)\d{4})|(\d{9}))(\b)",
                examples="123-45-6789",
                category="ssn",
                severity="high",
            ),
            RedactionPattern(
                display_name="Email Addresses",
                regex=r"([a-zA-Z0-9_])([a-zA-Z0-9_\-\.])*@([a-zA-Z0-9\-])+\.([a-zA-Z\.]+)",
                examples="user@example.com",
                category="email",
                severity="medium",
            ),
        ],
        codes=[
            RedactionCode(name="(b) (1) (A)", description="FOIA exemption"),
            RedactionCode(name="(b) (6)", description="Personal privacy"),
        ],
    )


# ── B161: Pre-Model Disqualification Rules ─────────────────────────────


@dataclass
class DisqualificationRule:
    """A deterministic disqualification rule (B161)."""
    rule_id: str
    tag_type: str  # "table", "artifact", "toc", "any", etc.
    tag_subtype: str = ""
    nested_in_tag: str = ""
    action: str = "disqualify"  # "disqualify" or "allow"
    conditions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DisqualificationResult:
    """Result of checking disqualification rules."""
    disqualified: bool
    rule_id: str = ""
    reason: str = ""


class DisqualificationEngine:
    """Checks candidates against deterministic rules before model calls (B161)."""

    def __init__(self) -> None:
        self._rules: list[DisqualificationRule] = []

    def add_rule(self, rule: DisqualificationRule) -> None:
        self._rules.append(rule)

    def check(self, candidate: dict[str, Any]) -> DisqualificationResult:
        """Check a candidate against all rules.

        Candidate should have: tag_type, tag_subtype, nested_in (dict with tag_type).
        """
        for rule in self._rules:
            if rule.tag_type != "any" and rule.tag_type != candidate.get("tag_type"):
                continue
            if rule.tag_subtype and rule.tag_subtype != candidate.get("tag_subtype"):
                continue
            if rule.nested_in_tag:
                nested = candidate.get("nested_in", {})
                if nested.get("tag_type") != rule.nested_in_tag:
                    continue
            if rule.action == "disqualify":
                return DisqualificationResult(
                    disqualified=True,
                    rule_id=rule.rule_id,
                    reason=f"disqualified by rule: {rule.rule_id}",
                )
        return DisqualificationResult(disqualified=False)

    @property
    def rule_count(self) -> int:
        return len(self._rules)


def default_disqual_controls() -> list[DisqualificationRule]:
    """Default disqualification rules from Acrobat's disqual-controls.json."""
    return [
        DisqualificationRule(
            rule_id="table_inside_list",
            tag_type="table",
            nested_in_tag="list",
        ),
        DisqualificationRule(
            rule_id="rule_watermark",
            tag_type="artifact",
            tag_subtype="watermark",
        ),
        DisqualificationRule(
            rule_id="toc",
            tag_type="toc",
        ),
        DisqualificationRule(
            rule_id="rule_True_MC",
            tag_type="any",
        ),
    ]


# ── B173: Native Messaging Host Allowlist ─────────────────────────────


@dataclass
class NativeMessagingHostManifest:
    """A native messaging host manifest (B173)."""
    name: str
    description: str = ""
    path: str = ""
    host_type: str = "stdio"
    allowed_origins: list[str] = field(default_factory=list)


class NativeMessagingAllowlist:
    """Validates incoming messages against an allowlist of origins (B173)."""

    def __init__(self) -> None:
        self._hosts: dict[str, NativeMessagingHostManifest] = {}

    def register(self, manifest: NativeMessagingHostManifest) -> None:
        self._hosts[manifest.name] = manifest

    def is_allowed(self, host_name: str, origin: str) -> bool:
        """Check if an origin is allowed for a host."""
        host = self._hosts.get(host_name)
        if not host:
            return False
        if not host.allowed_origins:
            return False  # empty allowlist rejects all
        return origin in host.allowed_origins

    def get_host(self, name: str) -> NativeMessagingHostManifest | None:
        return self._hosts.get(name)

    def all_hosts(self) -> list[str]:
        return list(self._hosts.keys())
