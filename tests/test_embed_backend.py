"""Embedding backend resolver, factory, and telemetry."""

from __future__ import annotations

import json

import pytest

from algo_cli import harness, main
from algo_cli import identity
from algo_cli.config import Config


@pytest.fixture(autouse=True)
def _reset_backend_cache():
    main.reset_embed_backend_cache()
    yield
    main.reset_embed_backend_cache()


def test_resolve_local_setting_returns_local_regardless_of_key(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "show_info", calls.append)
    for setting in ("auto", "local", "cloud"):
        assert main.resolve_embed_backend(Config(embedding_backend=setting))[0] == "retired"
    assert calls == []


def test_resolve_cloud_setting_falls_back_to_local(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "show_info", calls.append)
    for setting in ("auto", "local", "cloud"):
        assert main.resolve_embed_backend(Config(embedding_backend=setting))[0] == "retired"
    assert calls == []


def test_resolve_auto_remains_local_with_or_without_key(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "show_info", calls.append)
    for setting in ("auto", "local", "cloud"):
        assert main.resolve_embed_backend(Config(embedding_backend=setting))[0] == "retired"
    assert calls == []


def test_resolve_caches_announcement_per_setting(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "show_info", calls.append)
    for setting in ("auto", "local", "cloud"):
        assert main.resolve_embed_backend(Config(embedding_backend=setting))[0] == "retired"
    assert calls == []


def test_make_embed_fn_uses_local_model_for_cloud_fallback(monkeypatch):
    monkeypatch.setattr(main, "Client", lambda **kw: pytest.fail("retired factory must not construct a provider"))
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda *a: pytest.fail("no gateway probe"))
    with pytest.raises(RuntimeError, match="retired"):
        main.make_local_embed_fn(Config(), "fixture")
    with pytest.raises(RuntimeError, match="retired"):
        main.make_embed_fn(Config(), "fixture")


def test_log_embed_perf_includes_backend_field(monkeypatch, tmp_path):
    from algo_cli import dorothy_perf_telemetry as perf_telemetry

    perf_path = tmp_path / "perf_history.jsonl"
    monkeypatch.setattr(perf_telemetry, "PERF_HISTORY_FILE", perf_path)

    main.log_embed_perf({"event": "batch", "wall_ms": 12.3}, source="x", backend="local")
    main.log_embed_perf({"event": "batch", "wall_ms": 5.0}, source="x")

    events = perf_telemetry._private_perf_store().read_events()
    records = [event["record"] for event in events if event.get("kind") == "embed"]
    assert records[0]["backend"] == "local"
    assert "backend" not in records[1]


def test_benchmark_arg_parsing_stays_local_model_scoped():
    assert main._parse_benchmark_embed_args("benchmark-embed") == (20, None)
    assert main._parse_benchmark_embed_args("benchmark-embed --count 5 --model qwen3-embedding") == (
        5,
        "qwen3-embedding",
    )


def test_embedded_count_filters_by_passed_model(monkeypatch, tmp_path):
    index_path = tmp_path / "harness_index.json"
    index_path.write_text(
        json.dumps(
            {
                "records": [
                    {"id": "a", "embedding": [0.1], "embedding_model": "model-a"},
                    {"id": "b", "embedding": [0.2], "embedding_model": "model-a"},
                    {"id": "c", "embedding": [0.3], "embedding_model": "model-b"},
                    {"id": "d"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(harness, "INDEX_PATH", index_path)
    harness._set_index_cache(None)

    assert harness.embedded_count("model-a") == (2, 4)
    assert harness.embedded_count("model-b") == (1, 4)
    assert harness.embedded_count("model-c") == (0, 4)


def test_canonical_local_embedding_model_is_qwen3_embedding():
    assert harness.DEFAULT_EMBED_MODEL == "qwen3-embedding:latest"
    assert identity.DEFAULT_EMBED_MODEL == "qwen3-embedding:latest"


@pytest.mark.parametrize(
    ("old_model", "active_model", "old_dimensions", "active_dimensions"),
    [
        ("model-a", "model-b", 2, 2),
        ("model-a", "model-b", 2, 3),
        ("model-a", "model-a", 2, 3),
    ],
    ids=("model-change-same-width", "model-and-width-change", "width-change"),
)
def test_ensure_lessons_index_rebuilds_for_embedding_space_changes(
    monkeypatch,
    old_model,
    active_model,
    old_dimensions,
    active_dimensions,
):
    monkeypatch.setattr(main, "make_embed_fn", lambda *a, **kw: pytest.fail("no provider or dimension migration"))
    monkeypatch.setattr(identity, "rebuild_lessons_index", lambda *a, **kw: pytest.fail("no legacy persistence"))
    assert main.ensure_lessons_index(Config(harness_embed_model=active_model, embed_dimensions=active_dimensions)) is False


def test_local_embed_inputs_are_not_capped_for_default_model(monkeypatch):
    monkeypatch.setattr(main, "Client", lambda **kw: pytest.fail("retired factory must not construct a provider"))
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda *a: pytest.fail("no gateway probe"))
    with pytest.raises(RuntimeError, match="retired"):
        main.make_local_embed_fn(Config(), "fixture")
    with pytest.raises(RuntimeError, match="retired"):
        main.make_embed_fn(Config(), "fixture")


def test_local_embed_passes_configured_dimensions_to_direct_client(monkeypatch):
    monkeypatch.setattr(main, "Client", lambda **kw: pytest.fail("retired factory must not construct a provider"))
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda *a: pytest.fail("no gateway probe"))
    with pytest.raises(RuntimeError, match="retired"):
        main.make_local_embed_fn(Config(), "fixture")
    with pytest.raises(RuntimeError, match="retired"):
        main.make_embed_fn(Config(), "fixture")


def test_make_local_embed_fn_prefers_gateway_when_available(monkeypatch):
    monkeypatch.setattr(main, "Client", lambda **kw: pytest.fail("retired factory must not construct a provider"))
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda *a: pytest.fail("no gateway probe"))
    with pytest.raises(RuntimeError, match="retired"):
        main.make_local_embed_fn(Config(), "fixture")
    with pytest.raises(RuntimeError, match="retired"):
        main.make_embed_fn(Config(), "fixture")


def test_make_local_embed_fn_falls_back_when_gateway_unavailable(monkeypatch):
    monkeypatch.setattr(main, "Client", lambda **kw: pytest.fail("retired factory must not construct a provider"))
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda *a: pytest.fail("no gateway probe"))
    with pytest.raises(RuntimeError, match="retired"):
        main.make_local_embed_fn(Config(), "fixture")
    with pytest.raises(RuntimeError, match="retired"):
        main.make_embed_fn(Config(), "fixture")


def test_make_local_embed_fn_falls_back_when_gateway_returns_none(monkeypatch):
    monkeypatch.setattr(main, "Client", lambda **kw: pytest.fail("retired factory must not construct a provider"))
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda *a: pytest.fail("no gateway probe"))
    with pytest.raises(RuntimeError, match="retired"):
        main.make_local_embed_fn(Config(), "fixture")
    with pytest.raises(RuntimeError, match="retired"):
        main.make_embed_fn(Config(), "fixture")
