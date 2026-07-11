"""Tests for H32 — Pre-Push Scrubbing Gate."""
from __future__ import annotations

from algo_cli.intelligence.pre_push_gate import PrePushGate, GateResult


class TestPrePushGate:
    def test_blocks_by_default(self) -> None:
        gate = PrePushGate()
        result = gate.check()
        assert isinstance(result, GateResult)
        assert not result.allowed
        assert not result.override_used
        assert "blocked" in result.reason.lower()

    def test_allows_with_env_override(self) -> None:
        gate = PrePushGate(override_env_var="ALGO_ALLOW_RAW_PUSH")
        result = gate.check(env_getter=lambda k: "1" if k == "ALGO_ALLOW_RAW_PUSH" else None)
        assert result.allowed
        assert result.override_used

    def test_allows_with_true_env_override(self) -> None:
        gate = PrePushGate()
        result = gate.check(env_getter=lambda k: "true" if k == "ALGO_ALLOW_RAW_PUSH" else None)
        assert result.allowed

    def test_allows_with_yes_env_override(self) -> None:
        gate = PrePushGate()
        result = gate.check(env_getter=lambda k: "yes" if k == "ALGO_ALLOW_RAW_PUSH" else None)
        assert result.allowed

    def test_blocks_with_empty_env(self) -> None:
        gate = PrePushGate()
        result = gate.check(env_getter=lambda k: "")
        assert not result.allowed

    def test_allows_with_explicit_override_flag(self) -> None:
        gate = PrePushGate()
        result = gate.check(allow_override=True)
        assert result.allowed
        assert result.override_used

    def test_require_override_returns_true_when_blocked(self) -> None:
        gate = PrePushGate()
        assert gate.require_override() is True

    def test_require_override_returns_false_when_allowed(self) -> None:
        gate = PrePushGate()
        assert not gate.require_override(
            env_getter=lambda k: "1" if k == "ALGO_ALLOW_RAW_PUSH" else None
        )

    def test_custom_env_var_name(self) -> None:
        gate = PrePushGate(override_env_var="MY_CUSTOM_OVERRIDE")
        result = gate.check(env_getter=lambda k: "1" if k == "MY_CUSTOM_OVERRIDE" else None)
        assert result.allowed