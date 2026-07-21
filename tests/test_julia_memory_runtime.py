from __future__ import annotations

import json
from pathlib import Path

import pytest

from algo_cli import config, julia_memory_runtime as memory_runtime, memory_candidates
from algo_cli import tools
from algo_cli.config import Config
from algo_cli.samuel_policy_engine import session_command_requires_approval


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


def _concept_embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        lowered = text.casefold()
        if any(term in lowered for term in ("shell", "zsh", "interpreter", "command language")):
            vectors.append([1.0, 0.0, 0.0])
        elif any(term in lowered for term in ("theme", "dark", "appearance")):
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return vectors


def test_legacy_memory_remains_compatible_and_gains_stable_metadata(
    config_dir: Path,
) -> None:
    cfg = Config()

    assert memory_runtime.remember_fact(cfg, "Our standard shell is zsh.") is True
    assert memory_runtime.remember_fact(cfg, "Our standard shell is zsh.") is False

    assert json.loads(config.MEMORY_FILE.read_text(encoding="utf-8")) == ["Our standard shell is zsh."]
    catalog = memory_runtime.MemoryCatalog()
    records = catalog.records()
    assert len(records) == 1
    assert records[0]["id"].startswith("mem_")
    assert records[0]["tier"] == "pinned"
    assert records[0]["source"] == "user_explicit"
    assert records[0]["scope"] == "global"
    assert records[0]["status"] == "active"
    assert records[0]["pinned_in_legacy"] is True

    first_id = records[0]["id"]
    catalog.sync_legacy_facts(cfg.memories, authoritative=True)
    assert catalog.records()[0]["id"] == first_id


def test_same_slot_conflict_requires_explicit_supersession() -> None:
    catalog = memory_runtime.MemoryCatalog()
    original, _ = catalog.add(
        "Our standard shell is zsh.",
        tier="history",
        slot="environment.shell",
    )

    with pytest.raises(
        memory_runtime.MemoryConflictError,
        match="explicit supersession",
    ):
        catalog.add(
            "Our standard shell is fish.",
            tier="history",
            slot="environment.shell",
        )

    replacement = catalog.supersede(original["id"], "Our standard shell is fish.")
    records = catalog.records()
    old = next(record for record in records if record["id"] == original["id"])
    assert old["status"] == "superseded"
    assert old["superseded_by"] == replacement["id"]
    assert replacement["supersedes"] == original["id"]
    assert [record["content"] for record in catalog.records(include_inactive=False)] == ["Our standard shell is fish."]


def test_inferred_slot_blocks_conflicting_pinned_facts() -> None:
    cfg = Config()
    memory_runtime.remember_fact(cfg, "Our standard shell is zsh.")

    with pytest.raises(memory_runtime.MemoryConflictError):
        memory_runtime.remember_fact(cfg, "Our standard shell is fish.")

    assert cfg.memories == ["Our standard shell is zsh."]


def test_hybrid_recall_finds_a_paraphrase_and_keeps_vectors_separate() -> None:
    catalog = memory_runtime.MemoryCatalog()
    shell, _ = catalog.add("Our standard shell is zsh.", tier="history")
    catalog.add("Use a dark theme for terminal work.", tier="history")

    hits = catalog.search(
        "Which command interpreter should I use?",
        embed_fn=_concept_embed,
        embedding_model="test-concepts-v1",
    )

    assert hits
    assert hits[0]["id"] == shell["id"]
    assert hits[0]["semantic_score"] == 1.0
    assert hits[0]["semantic_status"] == "ready"
    index_payload = json.loads(memory_runtime.index_path().read_text(encoding="utf-8"))
    assert index_payload["model"] == "test-concepts-v1"
    assert "content" not in index_payload["records"][shell["id"]]


def test_lexical_recall_survives_embedding_failure() -> None:
    catalog = memory_runtime.MemoryCatalog()
    shell, _ = catalog.add("Our standard shell is zsh.", tier="history")

    def fail_embed(_texts: list[str]) -> list[list[float]]:
        raise OSError("offline")

    hits = catalog.search("standard shell", embed_fn=fail_embed)

    assert hits[0]["id"] == shell["id"]
    assert hits[0]["lexical_score"] > 0
    assert hits[0]["semantic_status"] == "embedding_failed"


def test_forget_hard_deletes_catalog_and_vector_state() -> None:
    cfg = Config()
    memory_runtime.remember_fact(cfg, "Our standard shell is zsh.")
    catalog = memory_runtime.MemoryCatalog()
    catalog.search(
        "shell",
        embed_fn=_concept_embed,
        embedding_model="test-concepts-v1",
    )

    removed = memory_runtime.forget_memory_index(cfg, 0)

    assert removed == "Our standard shell is zsh."
    assert cfg.memories == []
    assert catalog.records() == []
    index_payload = json.loads(memory_runtime.index_path().read_text(encoding="utf-8"))
    assert index_payload["records"] == {}


def test_privacy_gate_applies_to_explicit_memory_writes() -> None:
    cfg = Config()
    unsafe_candidate = "The API key is " + "sk-" + "this-is-not-safe-1234567890."

    with pytest.raises(memory_runtime.MemorySafetyError, match="privacy gate"):
        memory_runtime.remember_fact(
            cfg,
            unsafe_candidate,
        )

    assert cfg.memories == []
    assert not config.MEMORY_FILE.exists()


def test_memory_home_commands_promote_demote_and_report_readiness() -> None:
    cfg = Config()
    catalog = memory_runtime.MemoryCatalog()
    record, _ = catalog.add("Use dark terminal appearance.", tier="history")

    promoted = memory_runtime.command_text(f"promote {record['id']}", cfg)
    assert "Promoted" in promoted
    assert cfg.memories == ["Use dark terminal appearance."]
    assert catalog.get(record["id"])["tier"] == "pinned"

    demoted = memory_runtime.command_text(f"demote {record['id']}", cfg)
    assert "Demoted" in demoted
    assert cfg.memories == []
    assert catalog.get(record["id"])["tier"] == "history"

    home = memory_runtime.command_text("home", cfg)
    assert "Algo Memory Home" in home
    assert "history 1" in home


def test_prompt_format_preserves_provenance_and_authority_warning() -> None:
    catalog = memory_runtime.MemoryCatalog()
    catalog.add("Use dark terminal appearance.", tier="curated", source="verified")
    hits = catalog.search("dark terminal")

    rendered = memory_runtime.format_prompt_hits(hits)

    assert "not live proof" in rendered
    assert "[curated/verified]" in rendered
    assert "mem_" in rendered


def test_model_invoked_memory_reads_are_safe_but_mutations_require_approval() -> None:
    for command in (
        "/memory",
        "/memory home",
        "/memory doctor",
        "/memory benchmark",
        "/memory search shell preference",
        "/memory show mem_abc",
    ):
        assert session_command_requires_approval(command) is False
        assert tools._session_command_captures_output(command) is True

    for command in (
        "/memory add --tier history a durable preference",
        "/memory supersede mem_abc replacement text",
        "/memory promote mem_abc",
        "/memory demote mem_abc",
        "/memory archive mem_abc",
        "/memory reindex",
    ):
        assert session_command_requires_approval(command) is True
        assert tools._session_command_captures_output(command) is False


def test_memory_home_slash_route_reaches_the_governed_runtime(monkeypatch) -> None:
    from algo_cli import main

    rendered: list[str] = []
    monkeypatch.setattr(main.console, "print", lambda value: rendered.append(str(value)))

    handled, _client = main.handle_command("/memory home", Config(), None)

    assert handled is True
    assert rendered
    assert "Algo Memory Home" in rendered[-1]


def _benchmark_embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        lowered = text.casefold()
        if any(
            term in lowered
            for term in (
                "shell",
                "fish",
                "zsh",
                "interpreter",
                "intérprete",
                "comandos",
            )
        ):
            vectors.append([1.0, 0.0, 0.0, 0.0])
        elif any(
            term in lowered
            for term in (
                "payment",
                "invoice",
                "settling",
                "charge",
                "billing artifact",
                "pago",
                "factura",
            )
        ):
            vectors.append([0.0, 1.0, 0.0, 0.0])
        elif any(term in lowered for term in ("terminal", "theme")):
            vectors.append([0.0, 0.0, 1.0, 0.0])
        elif any(term in lowered for term in ("release", "signed package")):
            vectors.append([0.0, 0.0, 0.0, 1.0])
        else:
            vectors.append([-1.0, -1.0, -1.0, -1.0])
    return vectors


def test_controlled_memory_benchmark_passes_all_strength_and_safety_gates() -> None:
    result = memory_runtime.run_benchmark(
        embed_fn=_benchmark_embed,
        embedding_model="fixture-concepts-v1",
    )

    assert result["passed"] is True
    assert result["metrics"]["exact_recall_at_3"] == 1.0
    assert result["metrics"]["paraphrase_recall_at_3"] == 1.0
    assert result["metrics"]["semantic_paraphrase_recall"] == 1.0
    assert result["metrics"]["multilingual_recall_at_3"] == 1.0
    assert result["metrics"]["semantic_multilingual_recall"] == 1.0
    assert result["metrics"]["authority_precision_at_1"] == 1.0
    assert result["metrics"]["mrr"] == 1.0
    assert result["metrics"]["stale_hit_rate"] == 0.0
    assert result["metrics"]["unrelated_rejection"] is True
    assert result["metrics"]["lexical_fallback"] is True


def test_benchmark_truthfully_fails_semantic_gate_without_embeddings() -> None:
    result = memory_runtime.run_benchmark(embed_fn=None)

    assert result["passed"] is False
    assert result["metrics"]["exact_recall_at_3"] == 1.0
    assert result["metrics"]["semantic_paraphrase_recall"] == 0.0
    assert result["metrics"]["lexical_fallback"] is True
