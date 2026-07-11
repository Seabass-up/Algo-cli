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
    monkeypatch.setenv("OLLAMA_API_KEY", "secret")
    cfg = Config()
    cfg.embedding_backend = "local"

    backend, reason = main.resolve_embed_backend(cfg)

    assert backend == "local"
    assert "embedding_backend=local" in reason


def test_resolve_cloud_setting_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "secret")
    cfg = Config()
    cfg.embedding_backend = "cloud"

    backend, reason = main.resolve_embed_backend(cfg)

    assert backend == "local"
    assert "unavailable" in reason


def test_resolve_auto_remains_local_with_or_without_key(monkeypatch):
    cfg = Config()
    cfg.embedding_backend = "auto"
    monkeypatch.setenv("OLLAMA_API_KEY", "secret")
    assert main.resolve_embed_backend(cfg) == ("local", "auto: local embeddings only")

    main.reset_embed_backend_cache()
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert main.resolve_embed_backend(cfg) == ("local", "auto: local embeddings only")


def test_resolve_caches_announcement_per_setting(monkeypatch):
    cfg = Config()
    cfg.embedding_backend = "auto"
    calls: list[str] = []
    monkeypatch.setattr(main, "show_info", lambda message: calls.append(message))

    main.resolve_embed_backend(cfg)
    main.resolve_embed_backend(cfg)
    main.resolve_embed_backend(cfg)

    assert len([message for message in calls if "Embedding backend" in message]) == 1


def test_make_embed_fn_uses_local_model_for_cloud_fallback(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "secret")
    cfg = Config()
    cfg.embedding_backend = "cloud"
    cfg.cloud_embedding_model = "not-served-by-cloud"

    embed_fn, backend, active_model = main.make_embed_fn(cfg, "qwen3-embedding:latest")

    assert backend == "local"
    assert active_model == "qwen3-embedding:latest"
    assert callable(embed_fn)


def test_log_embed_perf_includes_backend_field(monkeypatch, tmp_path):
    from algo_cli import perf_telemetry

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
    assert main._parse_benchmark_embed_args("benchmark-embed --count 5 --model qwen3-embedding") == (5, "qwen3-embedding")


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
    identity.scaffold_if_needed()
    identity.LESSONS_PATH.write_text(
        "# Lessons Learned\n\n## L1\nA sufficiently long embedding space safety lesson.\n",
        encoding="utf-8",
    )
    identity.rebuild_lessons_index(
        lambda texts: [[1.0] + [0.0] * (old_dimensions - 1) for _ in texts],
        old_model,
    )
    cfg = Config(harness_embed_model=active_model, embed_dimensions=active_dimensions)
    rebuild_calls: list[str] = []

    def make_embed_fn(_cfg, model):
        rebuild_calls.append(model)

        def embed(texts):
            return [[1.0] + [0.0] * (active_dimensions - 1) for _ in texts]

        return embed, "local", model

    monkeypatch.setattr(main, "resolve_embed_backend", lambda _cfg: ("local", "test"))
    monkeypatch.setattr(main, "host_is_local", lambda _host: True)
    monkeypatch.setattr(main, "ollama_server_ready", lambda _host: True)
    monkeypatch.setattr(main, "make_embed_fn", make_embed_fn)
    monkeypatch.setattr(main, "show_info", lambda _message: None)

    assert main.ensure_lessons_index(cfg) is True
    assert rebuild_calls == [active_model]
    status = identity.lessons_index_status()
    assert status["model"] == active_model
    assert status["dimensions"] == active_dimensions


def test_local_embed_inputs_are_not_capped_for_default_model(monkeypatch):
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, *, host):
            captured["host"] = host

        def embed(self, *, model, input):
            captured["model"] = model
            captured["input"] = input
            return {"embeddings": [[1.0]]}

    monkeypatch.setattr(main, "Client", _Client)
    # Force the fallback path by stubbing the gateway as unavailable.
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda url=None: False)
    monkeypatch.setattr(
        main.tools_module, "gateway_embed_batch", lambda *a, **k: None
    )
    embed_fn = main.make_local_embed_fn(Config(), "qwen3-embedding:latest")

    embed_fn(["x" * 700])

    assert captured["input"] == ["x" * 700]


def test_local_embed_passes_configured_dimensions_to_direct_client(monkeypatch):
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, *, host):
            captured["host"] = host

        def embed(self, *, model, input, dimensions):
            captured.update(model=model, input=input, dimensions=dimensions)
            return {"embeddings": [[1.0, 0.0]]}

    cfg = Config(embed_dimensions=2)
    monkeypatch.setattr(main, "Client", _Client)
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda url=None: False)

    result = main.make_local_embed_fn(cfg, "model-a")(["lesson"])

    assert result == [[1.0, 0.0]]
    assert captured["dimensions"] == 2

def test_make_local_embed_fn_prefers_gateway_when_available(monkeypatch):
    captured: dict[str, object] = {}

    def _stub_batch(texts, model, truncate, dimensions, url=None):
        captured["via"] = "gateway"
        captured["texts"] = texts
        captured["model"] = model
        return {"embeddings": [[0.5], [0.25]]}

    class _Client:
        def __init__(self, *, host):
            captured["via"] = "client"
            captured["host"] = host

        def embed(self, *, model, input):
            captured["input"] = input
            return {"embeddings": [[0.0]]}

    monkeypatch.setattr(main, "Client", _Client)
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda url=None: True)
    monkeypatch.setattr(main.tools_module, "gateway_embed_batch", _stub_batch)
    embed_fn = main.make_local_embed_fn(Config(), "qwen3-embedding:latest")

    result = embed_fn(["hello", "world"])

    assert captured["via"] == "gateway"
    assert captured["texts"] == ["hello", "world"]
    assert result == [[0.5], [0.25]]

def test_make_local_embed_fn_falls_back_when_gateway_unavailable(monkeypatch):
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, *, host):
            captured["host"] = host

        def embed(self, *, model, input):
            captured["model"] = model
            captured["input"] = input
            return {"embeddings": [[0.1], [0.2]]}

    monkeypatch.setattr(main, "Client", _Client)
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda url=None: False)
    embed_fn = main.make_local_embed_fn(Config(), "qwen3-embedding:latest")

    result = embed_fn(["a", "b"])

    assert captured["input"] == ["a", "b"]
    assert result == [[0.1], [0.2]]

def test_make_local_embed_fn_falls_back_when_gateway_returns_none(monkeypatch):
    captured: dict[str, object] = {}

    class _Client:
        def __init__(self, *, host):
            captured["host"] = host

        def embed(self, *, model, input):
            captured["model"] = model
            captured["input"] = input
            return {"embeddings": [[0.7]]}

    monkeypatch.setattr(main, "Client", _Client)
    monkeypatch.setattr(main.tools_module, "gateway_ready", lambda url=None: True)
    monkeypatch.setattr(
        main.tools_module, "gateway_embed_batch", lambda *a, **k: None
    )
    embed_fn = main.make_local_embed_fn(Config(), "qwen3-embedding:latest")

    result = embed_fn(["only"])

    # Falls back to the Ollama client because the gateway stub returned None.
    assert captured["input"] == ["only"]
    assert result == [[0.7]]
