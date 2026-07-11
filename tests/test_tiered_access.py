"""Tests for H31 — Tiered Tool Access."""
from __future__ import annotations

import os

from algo_cli.intelligence.tiered_access import AccessTier, TieredAccessGate


def test_default_tier_always_granted() -> None:
    gate = TieredAccessGate()
    gate.register("safe-tool", AccessTier.DEFAULT)
    result = gate.check("safe-tool")
    assert result.granted is True


def test_opt_in_without_env_var_denied() -> None:
    gate = TieredAccessGate()
    gate.register("opt-tool", AccessTier.OPT_IN, env_var="ENABLE_OPT_TOOL")
    # Ensure env var is not set
    os.environ.pop("ENABLE_OPT_TOOL", None)
    result = gate.check("opt-tool")
    assert result.granted is False
    assert result.missing_env_var == "ENABLE_OPT_TOOL"


def test_opt_in_with_env_var_granted() -> None:
    gate = TieredAccessGate()
    gate.register("opt-tool", AccessTier.OPT_IN, env_var="ENABLE_OPT_TOOL")
    os.environ["ENABLE_OPT_TOOL"] = "1"
    result = gate.check("opt-tool")
    assert result.granted is True
    os.environ.pop("ENABLE_OPT_TOOL", None)


def test_approval_gated_without_approval_denied() -> None:
    gate = TieredAccessGate()
    gate.register("danger-tool", AccessTier.APPROVAL_GATED, env_var="ENABLE_DANGER")
    os.environ["ENABLE_DANGER"] = "1"
    result = gate.check("danger-tool", approved=False)
    assert result.granted is False
    os.environ.pop("ENABLE_DANGER", None)


def test_approval_gated_with_approval_granted() -> None:
    gate = TieredAccessGate()
    gate.register("danger-tool", AccessTier.APPROVAL_GATED, env_var="ENABLE_DANGER")
    os.environ["ENABLE_DANGER"] = "1"
    result = gate.check("danger-tool", approved=True)
    assert result.granted is True
    os.environ.pop("ENABLE_DANGER", None)


def test_approval_gated_without_env_var_denied() -> None:
    gate = TieredAccessGate()
    gate.register("danger-tool", AccessTier.APPROVAL_GATED, env_var="ENABLE_DANGER")
    os.environ.pop("ENABLE_DANGER", None)
    result = gate.check("danger-tool", approved=True)
    assert result.granted is False


def test_unregistered_tool_granted() -> None:
    gate = TieredAccessGate()
    result = gate.check("unknown-tool")
    assert result.granted is True


def test_get_config() -> None:
    gate = TieredAccessGate()
    gate.register("tool", AccessTier.DEFAULT, description="A tool")
    config = gate.get_config("tool")
    assert config is not None
    assert config.description == "A tool"


def test_all_configs() -> None:
    gate = TieredAccessGate()
    gate.register("a", AccessTier.DEFAULT)
    gate.register("b", AccessTier.OPT_IN, env_var="B")
    assert len(gate.all_configs()) == 2


def test_to_dict() -> None:
    gate = TieredAccessGate()
    gate.register("tool", AccessTier.DEFAULT)
    config = gate.get_config("tool")
    d = config.to_dict()
    assert d["tier"] == "default"