from __future__ import annotations

from algo_cli import harness, session_mode
from algo_cli.config import Config


def test_normalize_mode_defaults_unknown():
    assert session_mode.normalize_mode("bogus") == "explore"
    assert session_mode.normalize_mode(None) == "explore"


def test_execute_mode_forces_compact_mercury():
    text = "Send email and delete all customer files"
    full = harness.load_mercury_stop_conditions()
    resolved = harness.resolve_mercury_stop_conditions(
        user_message=text,
        session_mode="execute",
        include_external=True,
    )
    assert resolved == harness.MERCURY_STOP_CONDITIONS_COMPACT
    if full:
        assert resolved != full


def test_publish_mode_forces_full_mercury():
    text = "Read permit rollout plan read-only"
    full = harness.load_mercury_stop_conditions()
    if not full:
        return
    resolved = harness.resolve_mercury_stop_conditions(
        user_message=text,
        session_mode="publish",
        include_external=True,
    )
    assert resolved == full


def test_publish_mode_does_not_promise_external_mercury_when_disabled():
    cfg = Config(session_mode="publish", external_harness_sources_enabled=False)

    description = session_mode.describe(cfg)
    prompt = session_mode.prompt_section("publish", include_external=False)

    assert "compact built-in" in description
    assert "full stop-conditions loaded" not in description
    assert "Compact built-in stop conditions" in prompt
    assert "Full Mercury stop-conditions apply" not in prompt


def test_execute_mode_turns_reflex_off():
    cfg = Config(reflex_enabled=True)
    notes = session_mode.apply_mode_side_effects(cfg, "execute", previous="explore")
    assert cfg.reflex_enabled is False
    assert notes
