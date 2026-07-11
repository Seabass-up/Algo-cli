"""H31 — Tiered Tool Access.

Default / opt-in / approval-gated tool tiers.
Mined from T3MP3ST FEATURES.md §6.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any


class AccessTier(str, Enum):
    DEFAULT = "default"
    OPT_IN = "opt_in"
    APPROVAL_GATED = "approval_gated"


@dataclass
class TierConfig:
    """Configuration for a tool's access tier."""

    tool_name: str
    tier: AccessTier
    env_var: str = ""
    requires_approval: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tier": self.tier.value,
            "env_var": self.env_var,
            "requires_approval": self.requires_approval,
            "description": self.description,
        }


@dataclass
class AccessResult:
    """Result of an access check."""

    granted: bool
    reason: str = ""
    required_tier: AccessTier = AccessTier.DEFAULT
    missing_env_var: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "reason": self.reason,
            "required_tier": self.required_tier.value,
            "missing_env_var": self.missing_env_var,
        }


class TieredAccessGate:
    """Check tool access against tiered configuration."""

    def __init__(self) -> None:
        self._configs: dict[str, TierConfig] = {}

    def register(
        self,
        tool_name: str,
        tier: AccessTier,
        env_var: str = "",
        requires_approval: bool = False,
        description: str = "",
    ) -> TierConfig:
        config = TierConfig(
            tool_name=tool_name,
            tier=tier,
            env_var=env_var,
            requires_approval=requires_approval,
            description=description,
        )
        self._configs[tool_name] = config
        return config

    def check(self, tool_name: str, approved: bool = False) -> AccessResult:
        config = self._configs.get(tool_name)
        if config is None:
            return AccessResult(granted=True, reason="No tier config — default allow")
        if config.tier == AccessTier.DEFAULT:
            return AccessResult(granted=True, reason="Default tier — always allowed")
        if config.tier == AccessTier.OPT_IN:
            if config.env_var and not os.environ.get(config.env_var):
                return AccessResult(
                    granted=False,
                    reason=f"Opt-in tool requires env var {config.env_var}",
                    required_tier=AccessTier.OPT_IN,
                    missing_env_var=config.env_var,
                )
            return AccessResult(granted=True, reason="Opt-in env var set")
        if config.tier == AccessTier.APPROVAL_GATED:
            if config.env_var and not os.environ.get(config.env_var):
                return AccessResult(
                    granted=False,
                    reason=f"Approval-gated tool requires env var {config.env_var}",
                    required_tier=AccessTier.APPROVAL_GATED,
                    missing_env_var=config.env_var,
                )
            if not approved:
                return AccessResult(
                    granted=False,
                    reason="Approval-gated tool requires explicit approval",
                    required_tier=AccessTier.APPROVAL_GATED,
                )
            return AccessResult(granted=True, reason="Approved and env var set")
        return AccessResult(granted=False, reason="Unknown tier")

    def get_config(self, tool_name: str) -> TierConfig | None:
        return self._configs.get(tool_name)

    def all_configs(self) -> list[TierConfig]:
        return list(self._configs.values())

    def count(self) -> int:
        return len(self._configs)