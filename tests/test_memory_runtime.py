from __future__ import annotations

import json

from algo_cli import config, memory_candidates, memory_runtime
from algo_cli.config import Config


def test_completed_turn_stores_original_user_candidate_and_emits_aggregate_telemetry(
    monkeypatch,
    config_dir,
) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        memory_runtime,
        "record_perf_event",
        lambda event, **fields: events.append((event, fields)),
    )
    cfg = Config()

    result = memory_runtime.capture_completed_user_turn(
        cfg,
        "Remember that our standard shell is zsh.",
        completed=True,
    )

    assert result["status"] == "stored"
    assert cfg.memories == ["our standard shell is zsh."]
    assert events[-1][0] == "memory_candidate"
    assert events[-1][1]["stored"] == 1
    serialized = json.dumps(events)
    assert "standard shell" not in serialized
    assert memory_candidates.memory_fingerprint("our standard shell is zsh.") not in serialized


def test_incomplete_turn_and_explicit_memory_tool_skip_candidate_processing(
    monkeypatch,
    config_dir,
) -> None:
    events: list[dict] = []
    monkeypatch.setattr(
        memory_runtime,
        "record_perf_event",
        lambda _event, **fields: events.append(fields),
    )
    cfg = Config()
    text = "Remember that our standard shell is zsh."

    incomplete = memory_runtime.capture_completed_user_turn(cfg, text, completed=False)
    explicit = memory_runtime.capture_completed_user_turn(
        cfg,
        text,
        completed=True,
        tool_calls=({"name": "remember", "status": "worked"},),
    )

    assert incomplete["reason"] == "incomplete_turn"
    assert explicit["reason"] == "explicit_memory_write"
    assert cfg.memories == []
    assert not config.MEMORY_CANDIDATE_STATE_FILE.exists()
    assert [event["reason"] for event in events] == [
        "incomplete_turn",
        "explicit_memory_write",
    ]


def test_configured_limits_are_forwarded_to_candidate_processor(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_process(*_args, **kwargs):
        captured.update(kwargs)
        result = {
            "status": "rejected",
            "reason": "bounded",
            "counts": {},
            "reason_counts": {},
            "state": {},
        }
        kwargs["telemetry"](result)
        return result

    monkeypatch.setattr(memory_runtime.memory_candidates, "process_memory_candidates", fake_process)
    monkeypatch.setattr(memory_runtime, "record_perf_event", lambda *_args, **_kwargs: None)
    cfg = Config(
        memory_auto_daily_limit=2,
        memory_auto_entry_limit=20,
        memory_auto_char_limit=4_000,
    )

    memory_runtime.capture_completed_user_turn(
        cfg,
        "Remember that our standard shell is zsh.",
        completed=True,
        source="agent",
    )

    assert captured["daily_limit"] == 2
    assert captured["entry_limit"] == 20
    assert captured["char_limit"] == 4_000
