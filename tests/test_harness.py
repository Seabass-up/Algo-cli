"""Harness: pure helpers plus embedding/retrieval against a synthetic index."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from algo_cli import harness
from conftest import make_fake_embed


def _synthetic_index() -> dict:
    return {
        "generated": "2026-05-14T09:00",
        "source_policy": harness._source_policy(),
        "record_count": 3,
        "roots": [],
        "indexer": "python",
        "records": [
            {
                "id": "openclaw:wiki:footer.md",
                "harness": "openclaw",
                "kind": "wiki",
                "title": "Footer toolbar notes",
                "path": "__pytest_harness__/wiki/footer.md",
                "relative_path": "footer.md",
                "description": "footer toolbar chips and color encoding",
                "summary": "The footer toolbar uses chips and a connectivity dot.",
                "search_text": "footer toolbar chips connectivity dot color",
            },
            {
                "id": "codex:skill:rust.md",
                "harness": "codex",
                "kind": "skill",
                "title": "Rust indexer skill",
                "path": "__pytest_harness__/skills/rust.md",
                "relative_path": "rust.md",
                "description": "build the rust harness indexer",
                "summary": "The rust indexer is used for cold-start builds only.",
                "search_text": "rust indexer cold start build cargo",
            },
            {
                "id": "claude:skill:embed.md",
                "harness": "claude",
                "kind": "skill",
                "title": "Embedding skill",
                "path": "__pytest_harness__/skills/embed.md",
                "relative_path": "embed.md",
                "description": "embed records for retrieval",
                "summary": "Records get an embed vector for cosine retrieval.",
                "search_text": "embed model cosine retrieval vector index",
            },
        ],
    }


def _write_synthetic_index():
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps(_synthetic_index()), encoding="utf-8")
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None


def test_cosine():
    assert harness._cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert harness._cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert harness._cosine([1.0], []) == 0.0


def test_numpy_vector_retrieval_matches_cosine_not_raw_dot(monkeypatch):
    index = {
        "records": [
            {
                "id": "a",
                "harness": "h1",
                "kind": "wiki",
                "title": "a",
                "path": "a",
                "relative_path": "a",
                "embedding_model": "m",
                "embedding": [100.0, 0.0],
            },
            {
                "id": "b",
                "harness": "h1",
                "kind": "wiki",
                "title": "b",
                "path": "b",
                "relative_path": "b",
                "embedding_model": "m",
                "embedding": [1.0, 1.0],
            },
        ]
    }
    monkeypatch.setattr(harness, "load_index", lambda refresh=False: index)

    hits = harness.retrieve_for_query("q", lambda _texts: [[1.0, 1.0]], "m", k=2)

    assert [hit["id"] for hit in hits] == ["b", "a"]


def test_parse_frontmatter():
    text = "---\nname: demo\ndescription: a demo\ntags: [a, b]\n---\n# Body\n"
    fm = harness.parse_frontmatter(text)
    assert fm["name"] == "demo"
    assert fm["description"] == "a demo"
    assert fm["tags"] == ["a", "b"]


def test_parse_frontmatter_none():
    assert harness.parse_frontmatter("no frontmatter here") == {}


def test_harness_filter_names():
    assert harness.harness_filter_names(None) is None
    assert harness.harness_filter_names("codex") == {"codex"}
    assert harness.harness_filter_names("openclaude") == {"claude", "openclaw"}
    assert harness.harness_filter_names("all") is None


def test_dedup_records_keeps_same_relative_path_across_harnesses():
    records = [
        {"id": "a", "harness": "codex", "kind": "skill", "relative_path": "foo/SKILL.md"},
        {"id": "b", "harness": "openclaw", "kind": "skill", "relative_path": "foo/SKILL.md"},
    ]

    out = harness._dedup_records(records)

    assert [record["id"] for record in out] == ["a", "b"]


def test_score_record():
    record = {"title": "Rust indexer", "relative_path": "rust.md", "search_text": "rust indexer build"}
    assert harness.score_record(record, ["rust"]) > 0
    assert harness.score_record(record, ["nonexistent"]) == 0


def test_score_record_uses_token_boundaries_for_short_terms():
    record = {"title": "Partial result", "relative_path": "artifact.md", "search_text": "partial artifact"}

    assert harness.score_record(record, ["art"]) == 0


def test_embeddings_summary():
    records = [
        {"embedding": [0.1], "embedding_model": "m"},
        {"embedding": [0.2], "embedding_model": "m"},
        {},  # not embedded
    ]
    summary = harness._embeddings_summary(records, active_model="m")
    assert summary["embedded_count"] == 2
    assert summary["pending_count"] == 1
    assert summary["complete"] is False
    assert summary["embedded_by"] == "python"


def test_embed_index_records_and_retrieve():
    _write_synthetic_index()
    embed = make_fake_embed()
    model = harness.DEFAULT_EMBED_MODEL  # embedded_count filters on this
    result = harness.embed_index_records(embed, model)
    assert result["ready"] is True
    assert result["embedded"] == 3

    matching, total = harness.embedded_count()
    assert matching == total == 3

    hits = harness.retrieve_for_query("rust cold start build", embed, model, k=1)
    assert len(hits) == 1
    assert hits[0]["harness"] == "codex"


def test_embed_index_records_respects_max_records_cap():
    _write_synthetic_index()
    embed = make_fake_embed()
    model = harness.DEFAULT_EMBED_MODEL

    result = harness.embed_index_records(embed, model, max_records=2)

    assert result["ready"] is False
    assert result["embedded"] == 2
    assert result["pending"] == 1
    matching, total = harness.embedded_count()
    assert matching == 2
    assert total == 3


def test_capped_embedding_prioritizes_value_and_repeated_runs_finish_queue():
    records = [
        {
            "id": "codex:plugin:gamma",
            "harness": "codex",
            "kind": "plugin",
            "relative_path": "gamma/plugin.json",
            "path": "__pytest_harness__/gamma/plugin.json",
            "search_text": "codex plugin gamma",
        },
        {
            "id": "codex:agent:alpha",
            "harness": "codex",
            "kind": "agent",
            "relative_path": "alpha/agent.yaml",
            "path": "__pytest_harness__/alpha/agent.yaml",
            "search_text": "codex agent alpha",
        },
        {
            "id": "algo-cli:wiki:zeta",
            "harness": "algo-cli",
            "kind": "wiki",
            "relative_path": "zeta.md",
            "path": "__pytest_harness__/zeta.md",
            "search_text": "algo wiki zeta",
        },
        {
            "id": "codex:install:beta",
            "harness": "codex",
            "kind": "install",
            "relative_path": "beta/install.json",
            "path": "__pytest_harness__/beta/install.json",
            "search_text": "codex install beta",
        },
        {
            "id": "codex:skill:delta",
            "harness": "codex",
            "kind": "skill",
            "relative_path": "delta/SKILL.md",
            "path": "__pytest_harness__/delta/SKILL.md",
            "search_text": "codex skill delta",
        },
        {
            "id": "algo-cli:memory:alpha",
            "harness": "algo-cli",
            "kind": "memory",
            "relative_path": "alpha.md",
            "path": "__pytest_harness__/alpha.md",
            "search_text": "algo memory alpha",
        },
    ]
    harness.INDEX_PATH.write_text(
        json.dumps({"record_count": len(records), "records": records}),
        encoding="utf-8",
    )
    seen: list[str] = []
    events: list[dict] = []

    def embed(texts: list[str]) -> list[list[float]]:
        seen.extend(texts)
        return [[float(index + 1)] for index, _text in enumerate(texts)]

    first = harness.embed_index_records(
        embed,
        "priority-model",
        batch_size=2,
        max_records=3,
        on_perf=events.append,
    )

    assert seen == ["algo memory alpha", "algo wiki zeta", "codex skill delta"]
    assert first["ready"] is False
    assert first["selected_by_priority"] == {
        "project_core": 2,
        "curated_knowledge": 1,
        "runtime_capability": 0,
        "bulk_metadata": 0,
    }
    assert first["pending_by_priority"]["bulk_metadata"] == 3
    assert first["next_priority"] == "bulk_metadata"
    assert events[0]["priority_policy"] == harness.EMBED_PRIORITY_POLICY
    assert events[0]["batch_by_priority"]["project_core"] == 2
    assert events[-1]["pending_by_priority"]["bulk_metadata"] == 3

    progress = harness.embedding_progress("priority-model")
    assert progress["high_value_embedded"] == progress["high_value_total"] == 3
    assert progress["pending_by_priority"]["bulk_metadata"] == 3
    status = harness.stats()["embeddings"]
    assert status["priority_policy"] == harness.EMBED_PRIORITY_POLICY
    assert status["embedded_by_priority"]["project_core"] == 2
    assert status["pending_by_priority"]["bulk_metadata"] == 3

    second = harness.embed_index_records(
        embed,
        "priority-model",
        batch_size=2,
        max_records=3,
    )

    assert seen[3:] == ["codex agent alpha", "codex install beta", "codex plugin gamma"]
    assert second["ready"] is True
    assert second["pending"] == 0
    assert second["next_priority"] is None
    assert harness.embedded_count("priority-model") == (6, 6)


def test_embedding_priority_tiers_keep_capability_metadata_ahead_of_bulk():
    assert harness.embedding_priority({"harness": "algo-cli", "kind": "skill"}) == "project_core"
    assert harness.embedding_priority({"harness": "index-compute-lab", "kind": "memory"}) == "curated_knowledge"
    assert harness.embedding_priority({"harness": "codex", "kind": "connector"}) == "runtime_capability"
    assert harness.embedding_priority({"harness": "codex", "kind": "agent"}) == "bulk_metadata"
    assert harness.embedding_priority({"harness": "algo-cli", "kind": "runtime_capability"}) == "runtime_capability"


def test_runtime_capability_records_are_searchable_and_preserve_embeddings(monkeypatch):
    records = harness._runtime_capability_records()

    assert records
    assert len(records) == len(__import__("algo_cli.action_registry", fromlist=["ACTION_SPECS"]).ACTION_SPECS)
    write_file = next(record for record in records if record["id"].endswith(":write_file"))
    assert write_file["kind"] == "runtime_capability"
    assert "approval required True" in write_file["index_text"]
    assert write_file["capability"]["risk_level"] == "high"

    write_file["embedding"] = [0.25, 0.75]
    write_file["embedding_model"] = "test-model"
    rebuilt = harness._runtime_capability_records({"records": [write_file]})
    rebuilt_write = next(record for record in rebuilt if record["id"] == write_file["id"])
    assert rebuilt_write["embedding"] == [0.25, 0.75]
    assert rebuilt_write["embedding_model"] == "test-model"


def test_runtime_capability_record_reads_bounded_registry_evidence(monkeypatch):
    record = harness._runtime_capability_records()[0]
    monkeypatch.setattr(harness, "load_index", lambda refresh=False: {"records": [record]})
    harness._ID_LOOKUP = None

    text = harness.read_record(record["id"])

    assert "Kind: runtime_capability" in text
    assert record["description"] in text
    assert "class ActionSpec" not in text


def test_extra_root_parser_keeps_valid_entries_and_reports_rejections(tmp_path):
    roots, rejected = harness._parse_extra_source_roots_payload(
        [
            {
                "harness": "demo",
                "kind": "wiki",
                "root": str(tmp_path),
                "patterns": ["*.md"],
                "max_files": 10,
            },
            {"harness": "broken", "root": str(tmp_path)},
            "not-an-object",
        ]
    )

    assert len(roots) == 1
    assert roots[0].harness == "demo"
    assert rejected == 2


def test_extra_root_diagnostics_distinguish_malformed_and_unavailable(tmp_path, monkeypatch):
    roots_path = tmp_path / "harness_roots.json"
    monkeypatch.setattr(harness, "EXTRA_ROOTS_PATH", roots_path)
    roots_path.write_text("{bad json", encoding="utf-8")
    assert harness.extra_source_roots_diagnostics()["status"] == "malformed"

    roots_path.write_text(
        json.dumps(
            [
                {
                    "harness": "demo",
                    "kind": "wiki",
                    "root": str(tmp_path / "missing"),
                    "patterns": ["*.md"],
                }
            ]
        ),
        encoding="utf-8",
    )
    diagnostic = harness.extra_source_roots_diagnostics()
    assert diagnostic["status"] == "degraded"
    assert diagnostic["accepted"] == 1
    assert diagnostic["unavailable"] == 1


def test_source_root_diagnostics_report_adapter_contract_without_paths(tmp_path, monkeypatch):
    core_dir = tmp_path / "core"
    codex_dir = tmp_path / "codex"
    core_dir.mkdir()
    codex_dir.mkdir()
    core = harness.SourceRoot("algo-cli", "wiki", core_dir, ("*.md",), 10)
    codex = harness.SourceRoot("codex", "skill", codex_dir, ("SKILL.md",), 10)
    missing = harness.SourceRoot("claude", "skill", tmp_path / "missing", ("SKILL.md",), 10)
    monkeypatch.setattr(
        harness,
        "built_in_source_roots",
        lambda include_external=False: (core, codex, missing) if include_external else (core,),
    )
    monkeypatch.setattr(
        harness,
        "extra_source_roots_diagnostics",
        lambda: {"status": "absent", "accepted": 0, "rejected": 0, "available": 0},
    )

    result = harness.source_roots_diagnostics(
        [{"id": "codex:skill:one", "harness": "codex", "kind": "skill"}]
    )

    assert result["built_in_adapter_roots"] == 2
    assert result["available_adapter_roots"] == 1
    assert result["unreadable_adapter_roots"] == 0
    assert result["unavailable_adapter_roots"] == 1
    assert result["indexed_records"] == 1
    assert result["indexed_harnesses"] == ["codex"]
    assert str(tmp_path) not in json.dumps(result)


def test_source_root_diagnostics_distinguish_unreadable_adapter(tmp_path, monkeypatch):
    core_dir = tmp_path / "core"
    external_dir = tmp_path / "external"
    core_dir.mkdir()
    external_dir.mkdir()
    core = harness.SourceRoot("algo-cli", "wiki", core_dir, ("*.md",), 10)
    external = harness.SourceRoot("codex", "skill", external_dir, ("SKILL.md",), 10)
    monkeypatch.setattr(
        harness,
        "built_in_source_roots",
        lambda include_external=False: (core, external) if include_external else (core,),
    )
    real_access = os.access
    monkeypatch.setattr(
        harness.os,
        "access",
        lambda path, mode: False if Path(path) == external_dir else real_access(path, mode),
    )
    monkeypatch.setattr(
        harness,
        "extra_source_roots_diagnostics",
        lambda: {"status": "absent", "accepted": 0, "rejected": 0, "available": 0},
    )

    result = harness.source_roots_diagnostics([])

    assert result["available_adapter_roots"] == 0
    assert result["unreadable_adapter_roots"] == 1
    assert result["unavailable_adapter_roots"] == 0


def test_superseded_records_are_excluded_from_automatic_retrieval():
    assert harness.is_excluded_from_retrieval({"status": "superseded"}) is True


def test_harness_stats_reports_truthful_echo_veil_readiness(monkeypatch):
    from algo_cli import memory_echo_veil
    from algo_cli import tools

    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_AVAILABLE", False)
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_IMPORT_ERROR", "ModuleNotFoundError")
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_MODULE_ORIGIN", "")
    stats = harness.stats()

    assert stats["echo_veil"]["installed"] is False
    assert stats["echo_veil"]["enabled"] is False
    assert stats["echo_veil"]["write_wired"] is False
    assert stats["echo_veil"]["retrieval_wired"] is False
    assert stats["echo_veil"]["persistence_wired"] is False
    assert stats["echo_veil"]["import_error"] == "ModuleNotFoundError"
    assert tools.harness is harness
    assert json.loads(tools.harness_stats())["echo_veil"] == stats["echo_veil"]


def test_embed_index_records_emits_perf_events():
    _write_synthetic_index()
    embed = make_fake_embed()
    model = harness.DEFAULT_EMBED_MODEL
    events: list[dict] = []

    result = harness.embed_index_records(embed, model, on_perf=events.append)

    assert result["ready"] is True
    batch_events = [e for e in events if e["event"] == "batch"]
    complete_events = [e for e in events if e["event"] == "complete"]
    assert batch_events, "expected at least one batch perf event"
    assert all("wall_ms" in e and "per_record_ms" in e for e in batch_events)
    assert all(e["model"] == model for e in batch_events)
    assert len(complete_events) == 1
    assert complete_events[0]["embedded"] == 3
    assert complete_events[0]["total_records"] == 3
    assert "total_ms" in complete_events[0]


def test_embed_index_records_skips_perf_when_nothing_pending():
    _write_synthetic_index()
    embed = make_fake_embed()
    model = harness.DEFAULT_EMBED_MODEL
    harness.embed_index_records(embed, model)

    events: list[dict] = []
    result = harness.embed_index_records(embed, model, on_perf=events.append)

    assert result["ready"] is True
    assert result["embedded"] == 0
    assert events == []


def test_retrieve_for_query_empty_without_embeddings():
    _write_synthetic_index()  # records have no embeddings yet
    embed = make_fake_embed()
    assert harness.retrieve_for_query("anything", embed, "fake-model", k=3) == []


def test_format_retrieved_context():
    retrieved = [
        {"id": "x:y:z", "title": "A title", "snippet": "a snippet of context"},
    ]
    block = harness.format_retrieved_context(retrieved)
    assert "### x:y:z" in block
    assert "a snippet of context" in block
    assert harness.format_retrieved_context([]) == ""


def test_format_retrieved_context_handles_missing_id():
    # rec.get("id") falsy must not crash; renders "?" placeholder.
    block = harness.format_retrieved_context([{"title": "no-id record"}])
    assert "### ?" in block


def test_retrieved_context_repairs_common_mojibake():
    block = harness.format_retrieved_context([
        {
            "id": "algo-cli:wiki:test",
            "harness": "algo-cli",
            "kind": "wiki",
            "title": "Donâ€™t repeat",
            "snippet": "periodicâ€¦",
        }
    ])

    assert "Don’t repeat" in block
    assert "periodic…" in block
    assert "Â" not in block
    assert "â" not in block


def test_coerce_tags_string_to_list():
    assert harness._coerce_tags(["a", "b"]) == ["a", "b"]
    assert harness._coerce_tags("foo") == ["foo"]
    assert harness._coerce_tags(None) == []
    assert harness._coerce_tags("") == []


def test_slim_record_canonical_shape():
    raw = {
        "id": "h:k:p", "harness": "h", "kind": "k", "title": "T", "path": "/p",
        "summary": "s" * 10, "embedding": [0.1] * 768, "search_text": "internal",
        "file_size": 100, "file_mtime_ns": 1, "embedding_model": "m",
    }
    slim = harness._slim_record(raw)
    assert "embedding" not in slim
    assert "search_text" not in slim
    assert "file_size" not in slim
    assert slim["snippet"]
    assert slim["id"] == "h:k:p"


def test_hybrid_search_uniform_shape():
    _write_synthetic_index()
    embed = make_fake_embed()
    model = harness.DEFAULT_EMBED_MODEL
    harness.embed_index_records(embed, model)
    results = harness.hybrid_search("rust embed footer", embed, model, k=3)
    assert results
    for r in results:
        assert "embedding" not in r
        assert "search_text" not in r
        assert "score" in r
        assert "id" in r
        assert "rank_sources" in r
        assert "rank_provenance" in r


def test_harness_meta_query_prefers_algo_cli_self_evaluation_over_generic_skills():
    index = {
        "generated": "2026-05-14T09:00",
        "source_policy": harness._source_policy(),
        "record_count": 2,
        "roots": [],
        "records": [
            {
                "id": "codex:skill:hugging-face/rate-your-harness.md",
                "harness": "codex",
                "kind": "skill",
                "title": "Rate your harness extension review skill",
                "path": "__pytest__/skills/rate.md",
                "relative_path": "hugging-face/rate-your-harness.md",
                "description": "A generic extension skill for reviewing external model cards.",
                "summary": "Use this skill when you need to rate an external Hugging Face model or extension.",
                "search_text": (
                    "rate your harness extension review skill hugging face model cards "
                    "external model rating"
                ),
            },
            {
                "id": "algo-cli:skill:harness-search-first.md",
                "harness": "algo-cli",
                "kind": "skill",
                "title": "harness-search-first",
                "path": "__pytest__/skills/harness-search-first.md",
                "relative_path": "harness-search-first.md",
                "description": "Call harness_search before broad scans.",
                "summary": "Before broad filesystem scans, call harness_search to find skills and wiki pages.",
                "search_text": "harness-search-first harness search skills wiki broad filesystem scans",
            },
            {
                "id": "algo-cli:algorithm:ALGO.md",
                "harness": "algo-cli",
                "kind": "algorithm",
                "title": "ALGO reviewed algorithm pattern catalog",
                "path": "__pytest__/docs/ALGO.md",
                "relative_path": "ALGO.md",
                "description": "How to evaluate Algo CLI harness health, action registry, memory, wiki, and tools.",
                "summary": "Use this when asked to rate Algo CLI, its harness, or its available runtime context.",
                "search_text": (
                    "harness self evaluation algo cli capabilities action registry memory wiki "
                    "tools selfcheck doctor runtime context"
                ),
            },
        ],
    }
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None

    hits = harness.search_index("rate your harness", limit=3)

    assert [hit["id"] for hit in hits] == [
        "algo-cli:algorithm:ALGO.md",
        "algo-cli:skill:harness-search-first.md",
        "codex:skill:hugging-face/rate-your-harness.md",
    ]


def test_stats_surfaces_embeddings_block():
    _write_synthetic_index()
    embed = make_fake_embed()
    harness.embed_index_records(embed, "fake-model")
    stats = harness.stats()
    assert stats["record_count"] == 3
    assert stats["embeddings"]["complete"] is True
    assert stats["record_distribution"]["session_count"] >= 1
    assert "top_share" in stats["record_distribution"]
    assert set(stats["retrieval_caches"]) == {
        "bm25_ready",
        "bm25_records",
        "vector_matrix_ready",
        "vector_matrix_rows",
    }


def test_stats_surfaces_harness_quality_block():
    index = {
        "generated": "2026-05-14T09:00",
        "source_policy": harness._source_policy(),
        "record_count": 4,
        "roots": [],
        "records": [
            {
                "id": "algo-cli:wiki:quick-facts.md",
                "harness": "algo-cli",
                "kind": "wiki",
                "title": "Quick facts",
                "path": "__pytest__/quick.md",
                "relative_path": "quick.md",
                "summary": "Algo CLI quick facts.",
                "embedding": [1.0],
                "embedding_model": "fake-model",
            },
            {
                "id": "algo-cli:memory:runtime.md",
                "harness": "algo-cli",
                "kind": "memory",
                "title": "Runtime memory",
                "path": "__pytest__/runtime.md",
                "relative_path": "runtime.md",
                "summary": "Runtime memory.",
                "embedding": [1.0],
                "embedding_model": "fake-model",
            },
            {
                "id": "codex:extension:external.md",
                "harness": "codex",
                "kind": "extension",
                "title": "External extension",
                "path": "__pytest__/external.md",
                "relative_path": "external.md",
                "summary": "External extension.",
                "embedding": [1.0],
                "embedding_model": "fake-model",
            },
            {
                "id": "algo-cli:runtime_capability:read_file",
                "harness": "algo-cli",
                "kind": "runtime_capability",
                "title": "read_file runtime capability",
                "path": "__pytest__/action_registry.py",
                "relative_path": "action-registry/read_file",
                "summary": "Read a file.",
                "embedding": [1.0],
                "embedding_model": "fake-model",
            },
        ],
    }
    index["embeddings"] = harness._embeddings_summary(index["records"], active_model="fake-model")
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None

    quality = harness.stats()["quality"]

    assert quality["status"] == "ready"
    assert quality["project_specific_records"] == 3
    assert quality["extension_records"] == 1
    assert quality["memory_records"] == 1
    assert quality["wiki_records"] == 1
    assert quality["extension_share"] == 0.25
    assert quality["runtime_capability_records"] == 1
    assert quality["embedding_complete"] is True


def test_quality_allows_low_project_share_when_structured_plugin_metadata_is_indexed():
    records = [
        {
            "id": f"algo-cli:wiki:project-{i}.md",
            "harness": "algo-cli",
            "kind": "wiki" if i % 2 == 0 else "memory",
            "title": f"Project {i}",
            "path": f"__pytest__/project-{i}.md",
            "relative_path": f"project-{i}.md",
            "summary": "Project-specific context.",
            "embedding": [1.0],
            "embedding_model": "fake-model",
        }
        for i in range(5)
    ]
    records.extend(
        {
            "id": f"codex:{kind}:plugin-{i}.json",
            "harness": "codex",
            "kind": kind,
            "title": f"Plugin {i}",
            "path": f"__pytest__/plugin-{i}.json",
            "relative_path": f"plugin-{i}.json",
            "summary": "Structured Codex plugin metadata.",
            "embedding": [1.0],
            "embedding_model": "fake-model",
        }
        for i, kind in enumerate(
            [
                "extension",
                "plugin",
                "connector",
                "mcp",
                "command",
                "agent",
                "agent",
                "plugin",
                "connector",
                "command",
                "agent",
                "plugin",
                "mcp",
                "command",
                "agent",
                "connector",
                "plugin",
                "agent",
            ]
        )
    )
    records.append(
        {
            "id": "algo-cli:runtime_capability:read_file",
            "harness": "algo-cli",
            "kind": "runtime_capability",
            "title": "read_file runtime capability",
            "path": "__pytest__/action_registry.py",
            "relative_path": "action-registry/read_file",
            "summary": "Read a file.",
            "embedding": [1.0],
            "embedding_model": "fake-model",
        }
    )
    index = {
        "generated": "2026-05-14T09:00",
        "source_policy": harness._source_policy(),
        "record_count": len(records),
        "roots": [],
        "records": records,
        "embeddings": harness._embeddings_summary(records, active_model="fake-model"),
    }
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None

    quality = harness.stats()["quality"]

    assert quality["project_specific_share"] <= 0.25
    assert quality["extension_share"] < 0.7
    assert quality["status"] == "ready"
    assert not quality["recommendations"]


def test_iter_files_not_blocked_by_ancestor_tmp(tmp_path):
    """iter_files must not skip files whose absolute path contains a 'tmp' ancestor.

    The CONFIG_DIR in tests lives under the OS temp dir (/tmp/... on Linux).
    Previously should_skip checked all absolute path parts, so any root under /tmp
    silently excluded every file. iter_files now checks only relative path components.
    """
    # Write a markdown file directly in a root that lives under tmp_path.
    root_dir = tmp_path / "models"
    root_dir.mkdir()
    (root_dir / "mymodel.md").write_text("---\ntitle: mymodel\n---\n# mymodel\n", encoding="utf-8")

    root = harness.SourceRoot("test-harness", "model", root_dir, ("*.md",), 10)
    found = harness.iter_files(root)
    assert len(found) == 1
    assert found[0].name == "mymodel.md"


def test_iter_files_skips_secret_names(tmp_path):
    """Files with secret/token/key in the name must still be excluded."""
    root_dir = tmp_path / "assets"
    root_dir.mkdir()
    (root_dir / "SKILL.md").write_text("# good\n", encoding="utf-8")
    (root_dir / "secret_key.md").write_text("# bad\n", encoding="utf-8")

    root = harness.SourceRoot("test-harness", "skill", root_dir, ("*.md",), 10)
    found = harness.iter_files(root)
    names = {f.name for f in found}
    assert "SKILL.md" in names
    assert "secret_key.md" not in names


def test_iter_files_skips_secret_directory_components(tmp_path):
    """Files under credential/secret-like directories must be excluded."""
    root_dir = tmp_path / "assets"
    (root_dir / "credentials").mkdir(parents=True)
    (root_dir / "credentials" / "notes.md").write_text("# bad\n", encoding="utf-8")
    (root_dir / "public").mkdir()
    (root_dir / "public" / "notes.md").write_text("# good\n", encoding="utf-8")

    root = harness.SourceRoot("test-harness", "skill", root_dir, ("*.md",), 10)
    found = harness.iter_files(root)

    assert [path.relative_to(root_dir).as_posix() for path in found] == ["public/notes.md"]


def test_iter_files_skips_sessions_subdir(tmp_path):
    """Files inside a skip-dir subdirectory must be excluded."""
    root_dir = tmp_path / "wiki"
    (root_dir / "sessions").mkdir(parents=True)
    (root_dir / "sessions" / "SKILL.md").write_text("# session\n", encoding="utf-8")
    (root_dir / "good.md").write_text("# good\n", encoding="utf-8")

    root = harness.SourceRoot("test-harness", "wiki", root_dir, ("*.md",), 10)
    found = harness.iter_files(root)
    names = {f.name for f in found}
    assert "good.md" in names
    assert "SKILL.md" not in names


def test_is_chatgpt_clipping_detects_description_and_tags():
    assert harness.is_chatgpt_clipping({"description": "ChatGPT conversation with 2 messages"})
    assert harness.is_chatgpt_clipping({"tags": ["clippings"]})
    assert not harness.is_chatgpt_clipping({"description": "Project closeout notes"})


def test_is_excluded_from_retrieval_filters_archive_vendor_and_backlog():
    assert harness.is_excluded_from_retrieval(
        {"id": "openclaw:wiki:archive/2026-06-ollama-cli-stale/foo.md"}
    )
    assert harness.is_excluded_from_retrieval({"kind": "vendor-doc", "id": "pi:tool:pods/docs/glm.md"})
    assert harness.is_excluded_from_retrieval({"status": "backlog", "id": "openclaw:wiki:concepts/roadmap.md"})
    assert not harness.is_excluded_from_retrieval({"id": "openclaw:wiki:concepts/live.md", "kind": "wiki"})


def test_resolve_record_kind_marks_pi_vendor_docs():
    root = harness.SourceRoot("pi", "tool", Path("/pi"), ("*.md",), 10)
    assert harness.resolve_record_kind(root, "pods/docs/gml-4.5.md") == "vendor-doc"
    assert harness.resolve_record_kind(root, "coding-agent/main.ts") == "tool"


def test_search_index_skips_excluded_records():
    index = {
        "generated": "2026-05-14T09:00",
        "record_count": 2,
        "roots": [],
        "records": [
            {
                "id": "openclaw:wiki:live.md",
                "harness": "openclaw",
                "kind": "wiki",
                "title": "Live harness notes",
                "path": "__pytest__/live.md",
                "description": "live operational doc",
                "summary": "live harness operational guidance",
                "search_text": "live harness operational guidance",
            },
            {
                "id": "openclaw:wiki:archive/stale.md",
                "harness": "openclaw",
                "kind": "wiki",
                "title": "Stale archive",
                "path": "__pytest__/archive/stale.md",
                "summary": "stale archive page about harness",
                "search_text": "stale archive page harness",
            },
        ],
    }
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None
    hits = harness.search_index("harness", limit=5)
    assert hits
    assert all("archive/" not in h["id"] for h in hits)


def test_resolve_embed_model_prefers_config():
    class _Cfg:
        harness_embed_model = "embeddinggemma:latest"

    assert harness.resolve_embed_model(_Cfg()) == "embeddinggemma:latest"
    assert harness.resolve_embed_model(None) == harness.DEFAULT_EMBED_MODEL


def test_resolve_embed_model_ignores_deprecated_all_minilm_config():
    class _Cfg:
        harness_embed_model = "all-minilm:latest"

    assert harness.resolve_embed_model(_Cfg()) == harness.DEFAULT_EMBED_MODEL


def test_rust_indexer_candidates_prefers_algo_cli_env(monkeypatch, tmp_path):
    new_binary = tmp_path / "new-indexer"
    legacy_binary = tmp_path / "legacy-indexer"
    monkeypatch.setenv("ALGO_CLI_HARNESS_INDEXER", str(new_binary))
    monkeypatch.setenv("OLLAMA_CLI_HARNESS_INDEXER", str(legacy_binary))

    candidates = harness.rust_indexer_candidates()

    assert candidates[0] == new_binary


def test_rust_indexer_candidates_accepts_legacy_env(monkeypatch, tmp_path):
    legacy_binary = tmp_path / "legacy-indexer"
    monkeypatch.delenv("ALGO_CLI_HARNESS_INDEXER", raising=False)
    monkeypatch.setenv("OLLAMA_CLI_HARNESS_INDEXER", str(legacy_binary))

    candidates = harness.rust_indexer_candidates()

    assert candidates[0] == legacy_binary


def test_rust_indexer_receives_exact_imported_package_boundary(monkeypatch, tmp_path):
    binary = tmp_path / "harness-indexer"
    binary.write_bytes(b"fixture")
    index_path = tmp_path / "private" / "harness_index.json"
    config_dir = index_path.parent
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        index_path.write_text(
            json.dumps(
                {
                    "generated": "1",
                    "record_count": 0,
                    "roots": [],
                    "records": [],
                    "refresh_stats": {
                        "reused_records": 0,
                        "rebuilt_records": 0,
                        "removed_records": 0,
                    },
                    "indexer": "rust",
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(harness, "_EXTERNAL_SOURCES_ENABLED", True)
    monkeypatch.setattr(harness, "find_rust_indexer", lambda: binary)
    monkeypatch.setattr(harness, "INDEX_PATH", index_path)
    monkeypatch.setattr(harness, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(harness.subprocess, "run", fake_run)

    result = harness.build_index_with_rust()

    assert result is not None and result["indexer"] == "rust"
    assert captured["command"] == [str(binary), "--output", str(index_path)]
    assert captured["env"]["ALGO_CLI_REPO_DIR"] == str(harness.ALGO_CLI_REPO_DIR)


def test_retrieve_for_query_skips_excluded_records():
    embed = make_fake_embed()
    live_vec = embed(["live harness operational guidance"])[0]
    stale_vec = embed(["stale archive page harness"])[0]
    index = {
        "generated": "2026-05-14T09:00",
        "record_count": 2,
        "roots": [],
        "records": [
            {
                "id": "openclaw:wiki:live.md",
                "harness": "openclaw",
                "kind": "wiki",
                "title": "Live harness notes",
                "path": "__pytest__/live.md",
                "relative_path": "live.md",
                "description": "live operational doc",
                "summary": "live harness operational guidance",
                "search_text": "live harness operational guidance",
                "embedding": live_vec,
                "embedding_model": "fake-model",
            },
            {
                "id": "openclaw:wiki:archive/stale.md",
                "harness": "openclaw",
                "kind": "wiki",
                "title": "Stale archive",
                "path": "__pytest__/archive/stale.md",
                "relative_path": "archive/stale.md",
                "description": "stale",
                "summary": "stale archive page about harness",
                "search_text": "stale archive page harness",
                "embedding": stale_vec,
                "embedding_model": "fake-model",
            },
        ],
    }
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None
    hits = harness.retrieve_for_query("harness", embed, "fake-model", k=5)
    assert hits
    assert all("archive/" not in h["id"] for h in hits)


def test_iter_files_skips_archive_subdir(tmp_path):
    """Archived/stale wiki pages must not be surfaced as live harness context."""
    root_dir = tmp_path / "wiki"
    (root_dir / "archive" / "2026-06-ollama-cli-stale").mkdir(parents=True)
    (root_dir / "archive" / "2026-06-ollama-cli-stale" / "old.md").write_text("# stale\n", encoding="utf-8")
    (root_dir / "live.md").write_text("# live\n", encoding="utf-8")

    root = harness.SourceRoot("test-harness", "wiki", root_dir, ("*.md",), 10)
    found = harness.iter_files(root)
    relative = {path.relative_to(root_dir).as_posix() for path in found}
    assert relative == {"live.md"}


def test_iter_files_stops_at_max_files(tmp_path):
    root_dir = tmp_path / "wiki"
    root_dir.mkdir()
    for i in range(10):
        (root_dir / f"note-{i}.md").write_text(f"# note {i}\n", encoding="utf-8")

    root = harness.SourceRoot("test-harness", "wiki", root_dir, ("*.md",), 3)
    found = harness.iter_files(root)

    assert len(found) == 3


def test_reviewed_algo_doc_is_builtin_harness_source():
    matching = [
        root for root in harness.built_in_source_roots()
        if root.harness == "algo-cli"
        and root.root == harness.ALGO_CLI_REPO_DIR / "docs"
        and "ALGO.md" in root.patterns
    ]

    assert len(matching) == 1
    assert matching[0].kind == "algorithm"
    assert (matching[0].root / "ALGO.md").exists()


def test_curated_project_docs_are_builtin_harness_wiki_sources():
    matching = [
        root for root in harness.built_in_source_roots()
        if root.harness == "algo-cli"
        and root.kind == "wiki"
        and root.root == harness.ALGO_CLI_REPO_DIR / "docs"
    ]

    assert len(matching) == 1
    patterns = set(matching[0].patterns)
    assert patterns == set(harness.CURATED_PROJECT_WIKI_DOCS)
    assert "quick-facts-algo-cli.md" not in patterns
    assert "memory-facts-algo-cli.md" not in patterns
    assert "audit-memory-and-rebrand-debt.md" not in patterns
    assert "ALGO.md" not in patterns


def test_local_operator_wiki_is_builtin_harness_wiki_source():
    matching = [
        root
        for root in harness.built_in_source_roots()
        if root.harness == "algo-cli"
        and root.kind == "wiki"
        and root.root == harness.CONFIG_DIR / "wiki"
    ]

    assert len(matching) == 1
    assert matching[0].patterns == ("*.md",)
    assert matching[0].max_files == 100



def test_curated_project_memory_docs_are_builtin_harness_memory_sources():
    matching = [
        root
        for root in harness.built_in_source_roots()
        if root.harness == "algo-cli"
        and root.kind == "memory"
        and root.root == harness.ALGO_CLI_REPO_DIR / "docs"
    ]

    assert len(matching) == 1
    patterns = set(matching[0].patterns)
    assert patterns == set(harness.CURATED_PROJECT_MEMORY_DOCS)
    assert "memory-facts-algo-cli.md" not in patterns
    assert "audit-memory-and-rebrand-debt.md" not in patterns
    assert "rebrand-lessons.md" not in patterns
    assert "lessons-rebrand-algo-cli.md" not in patterns
    assert "algo-cli-rebrand-2026-06.md" not in patterns
    assert "graph-rebrand-ollama-cli-concept.md" not in patterns
    assert "quick-facts-algo-cli.md" not in patterns
    assert "ALGO.md" not in patterns


def test_harness_extension_cleanup_doc_describes_structured_plugin_metadata():
    root = next(
        root
        for root in harness.built_in_source_roots()
        if root.harness == "algo-cli"
        and root.kind == "wiki"
        and root.root == harness.ALGO_CLI_REPO_DIR / "docs"
        and "harness-extension-cleanup-recommendation.md" in root.patterns
    )

    record = harness.make_record(root, root.root / "harness-extension-cleanup-recommendation.md")

    assert "Structured Codex plugin metadata" in record["title"]
    assert "codex:plugin" in record["search_text"]
    assert "codex:connector" in record["search_text"]
    assert "codex:mcp" in record["search_text"]
    assert "completed" in record["search_text"]
    assert "would you like me" not in record["search_text"]


def test_iter_files_supports_nested_plugin_cache_globs(tmp_path):
    plugin_cache = tmp_path / "plugins" / "cache"
    manifest = plugin_cache / "openai-curated" / "google-drive" / "3fdeeb49" / ".codex-plugin" / "plugin.json"
    skill = plugin_cache / "openai-curated" / "google-drive" / "3fdeeb49" / "skills" / "google-drive" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    manifest.write_text('{"name": "google-drive"}\n', encoding="utf-8")
    skill.write_text("# Google Drive skill\n", encoding="utf-8")

    root = harness.SourceRoot("codex", "plugin", plugin_cache, ("*/.codex-plugin/plugin.json",), 10)

    assert [path.relative_to(plugin_cache).as_posix() for path in harness.iter_files(root)] == [
        "openai-curated/google-drive/3fdeeb49/.codex-plugin/plugin.json"
    ]


def test_codex_plugin_metadata_sources_are_builtin_harness_sources():
    roots = [
        root
        for root in harness.built_in_source_roots(include_external=True)
        if root.harness == "codex" and root.root == harness.CODEX_DIR / "plugins" / "cache"
    ]

    patterns_by_kind = {root.kind: root.patterns for root in roots}
    assert patterns_by_kind["extension"] == ("SKILL.md",)
    assert patterns_by_kind["plugin"] == ("*/.codex-plugin/plugin.json",)
    assert patterns_by_kind["install"] == ("*/.codex-remote-plugin-install.json",)
    assert patterns_by_kind["connector"] == ("*/.app.json",)
    assert patterns_by_kind["mcp"] == ("*/.mcp.json",)
    assert patterns_by_kind["command"] == ("*/commands/*.md",)
    assert patterns_by_kind["agent"] == ("*/agents/*.yaml", "*/skills/*/agents/*.yaml")


def test_external_agent_sources_are_opt_in():
    default_harnesses = {root.harness for root in harness.built_in_source_roots()}
    opted_in_harnesses = {
        root.harness for root in harness.built_in_source_roots(include_external=True)
    }

    assert not ({"codex", "claude", "openclaw", "mercury"} & default_harnesses)
    assert {"codex", "claude", "openclaw", "mercury"} <= opted_in_harnesses


def test_algo_cli_repo_dir_never_discovers_same_named_home_checkout(monkeypatch, tmp_path):
    installed_package = tmp_path / "site-packages" / "algo_cli"
    installed_resources = installed_package / "resources"
    installed_resources.mkdir(parents=True)
    fake_module = installed_package / "harness.py"
    fake_module.write_text("# installed harness\n", encoding="utf-8")

    unrelated_checkout = tmp_path / "home" / "Code" / "algo-cli"
    unrelated_checkout.mkdir(parents=True)
    (unrelated_checkout / "PRIVATE-SENTINEL.md").write_text(
        "must not be discovered",
        encoding="utf-8",
    )

    monkeypatch.setattr(harness, "__file__", str(fake_module))
    monkeypatch.setattr(harness, "PACKAGE_RESOURCE_DIR", installed_resources)
    monkeypatch.setattr(harness, "_project_dir", lambda _name: unrelated_checkout)

    assert harness._algo_cli_repo_dir() == installed_resources
    assert harness._algo_cli_repo_dir() != unrelated_checkout


def test_record_content_redacts_credentials(tmp_path):
    note = tmp_path / "note.md"
    note.write_text(
        "# Setup\naccess_token = token-value-that-must-not-survive\n"
        "Authorization: Bearer bearer-value-that-must-not-survive\n",
        encoding="utf-8",
    )
    record = harness.make_record(
        harness.SourceRoot("custom", "wiki", tmp_path, ("*.md",), 10),
        note,
    )

    serialized = json.dumps(record)
    assert "token-value-that-must-not-survive" not in serialized
    assert "bearer-value-that-must-not-survive" not in serialized
    assert "<redacted>" in serialized


def test_codex_plugin_manifest_record_uses_json_metadata(tmp_path):
    plugin_cache = tmp_path / "plugins" / "cache"
    manifest = plugin_cache / "openai-curated" / "google-drive" / "3fdeeb49" / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "google-drive",
                "description": "Use Google Drive as the single entrypoint for Drive, Docs, Sheets, and Slides work.",
                "keywords": ["google-drive", "google-docs", "productivity"],
                "apps": "./.app.json",
                "mcpServers": "./.mcp.json",
                "interface": {
                    "displayName": "Google Drive",
                    "shortDescription": "Work across Drive, Docs, Sheets, and Slides",
                    "capabilities": ["Interactive", "Write"],
                },
            }
        ),
        encoding="utf-8",
    )
    root = harness.SourceRoot("codex", "plugin", plugin_cache, ("*/.codex-plugin/plugin.json",), 10)

    record = harness.make_record(root, manifest)

    assert record["id"] == "codex:plugin:openai-curated/google-drive/3fdeeb49/.codex-plugin/plugin.json"
    assert record["title"] == "Google Drive"
    assert record["description"] == "Work across Drive, Docs, Sheets, and Slides"
    assert "google-docs" in record["tags"]
    assert "connector" in record["tags"]
    assert "mcp" in record["tags"]
    assert "interactive" in record["search_text"]
    assert "write" in record["search_text"]


def test_codex_remote_plugin_install_record_uses_json_metadata(tmp_path):
    plugin_cache = tmp_path / "plugins" / "cache"
    receipt = plugin_cache / "openai-curated-remote" / "google-drive" / ".codex-remote-plugin-install.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "remote_plugin_id": "plugin_connector_1p_ab21a553bfbc81919ea8fd1858e3ffa7",
            }
        ),
        encoding="utf-8",
    )
    root = harness.SourceRoot("codex", "install", plugin_cache, ("*/.codex-remote-plugin-install.json",), 10)

    record = harness.make_record(root, receipt)

    assert record["id"] == "codex:install:openai-curated-remote/google-drive/.codex-remote-plugin-install.json"
    assert record["title"] == "Codex plugin install: google-drive"
    assert "remote plugin install receipt" in record["description"]
    assert "remote-plugin" in record["tags"]
    assert "plugin_connector_1p_ab21a553bfbc81919ea8fd1858e3ffa7" in record["search_text"]


def test_codex_connector_and_mcp_records_use_json_metadata(tmp_path):
    plugin_cache = tmp_path / "plugins" / "cache"
    plugin_root = plugin_cache / "openai-curated" / "google-drive" / "3fdeeb49"
    plugin_root.mkdir(parents=True)
    app_path = plugin_root / ".app.json"
    mcp_path = plugin_root / ".mcp.json"
    app_path.write_text(
        json.dumps({"apps": {"google-drive": {"id": "connector_abc123"}}}),
        encoding="utf-8",
    )
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {
                        "type": "http",
                        "url": "https://example.test/mcp/",
                        "access_token": "not-for-indexing-123456789",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    app_record = harness.make_record(
        harness.SourceRoot("codex", "connector", plugin_cache, ("*/.app.json",), 10),
        app_path,
    )
    mcp_record = harness.make_record(
        harness.SourceRoot("codex", "mcp", plugin_cache, ("*/.mcp.json",), 10),
        mcp_path,
    )

    assert app_record["title"] == "Codex app connectors: google-drive"
    assert app_record["description"] == "Codex app connector metadata for google-drive."
    assert "connector" in app_record["tags"]
    assert "google-drive" in app_record["search_text"]
    assert mcp_record["title"] == "Codex MCP servers: github"
    assert mcp_record["description"] == "Codex MCP server metadata for github."
    assert "mcp" in mcp_record["tags"]
    assert "github" in mcp_record["search_text"]
    assert "not-for-indexing" not in json.dumps(mcp_record)


def test_reviewed_algo_doc_record_is_indexable():
    root = next(
        root for root in harness.built_in_source_roots()
        if root.harness == "algo-cli"
        and root.root == harness.ALGO_CLI_REPO_DIR / "docs"
        and "ALGO.md" in root.patterns
    )

    record = harness.make_record(root, root.root / "ALGO.md")

    assert record["id"] == "algo-cli:algorithm:ALGO.md"
    assert record["relative_path"] == "ALGO.md"
    assert "algorithm" in record["search_text"]
    assert "pattern" in record["search_text"]
    assert "catalog" in record["search_text"]
    assert "self-evaluation" in record["search_text"]
    assert "action registry" in record["search_text"]
    assert "selfcheck" in record["search_text"]


def test_reviewed_algo_doc_metadata_rebuilds_legacy_summary_only_record(monkeypatch):
    root = next(
        root for root in harness.built_in_source_roots()
        if root.harness == "algo-cli"
        and root.root == harness.ALGO_CLI_REPO_DIR / "docs"
        and "ALGO.md" in root.patterns
    )
    path = root.root / "ALGO.md"
    stat_result = path.stat()
    stale_record = {
        "id": "algo-cli:algorithm:ALGO.md",
        "harness": "algo-cli",
        "kind": "algorithm",
        "title": "ALGO.md",
        "path": str(path),
        "relative_path": "ALGO.md",
        "description": "stale short description",
        "tags": [],
        "status": "",
        "updated": "2026-07-01T00:00:00",
        "file_size": int(stat_result.st_size),
        "file_mtime_ns": int(stat_result.st_mtime_ns),
        "summary": "Reusable algorithm patterns.",
        "search_text": "algo reviewed",
        "embedding": [0.1, 0.2],
        "embedding_model": harness.DEFAULT_EMBED_MODEL,
    }
    previous = {"records": [stale_record], "indexer": "python"}
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (root,))

    index = harness.build_index(previous)
    record = index["records"][0]

    assert index["refresh_stats"]["reused_records"] == 0
    assert index["refresh_stats"]["rebuilt_records"] >= 1
    assert record["index_text"]
    assert record["title"] == "ALGO reviewed algorithm pattern catalog"
    assert record["description"] == harness.REVIEWED_ALGO_DESCRIPTION
    assert "pattern" in record["search_text"]
    assert "self-evaluation" in record["search_text"]
    assert "action registry" in record["search_text"]
    assert "embedding" not in record


def test_index_is_stale_detects_new_file_in_existing_subdir(tmp_path, monkeypatch):
    root_dir = tmp_path / "wiki"
    subdir = root_dir / "docs"
    subdir.mkdir(parents=True)
    existing = subdir / "existing.md"
    existing.write_text("# existing\n", encoding="utf-8")
    root = harness.SourceRoot("test", "wiki", root_dir, ("*.md",), 10)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (root,))
    monkeypatch.setattr(harness, "load_extra_source_roots", lambda: [])
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(
        json.dumps({"records": [{"path": str(existing), "relative_path": "docs/existing.md"}]}),
        encoding="utf-8",
    )
    harness._INDEX_CACHE = None
    time.sleep(0.02)
    (subdir / "new.md").write_text("# new\n", encoding="utf-8")

    assert harness.index_is_stale() is True


def test_load_index_coalesces_repeated_source_freshness_scans(monkeypatch):
    _write_synthetic_index()
    calls = 0

    def source_watermark(_index=None):
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(harness, "_source_watermark_ns", source_watermark)

    first = harness.load_index()
    second = harness.load_index()

    assert second is first
    assert calls == 1


def test_load_index_notices_external_index_replacement():
    _write_synthetic_index()
    first = harness.load_index()
    replacement = _synthetic_index()
    replacement["records"][0]["title"] = "Externally refreshed title"
    harness.INDEX_PATH.write_text(json.dumps(replacement), encoding="utf-8")
    future = harness.INDEX_PATH.stat().st_mtime + 5
    os.utime(harness.INDEX_PATH, (future, future))

    second = harness.load_index()

    assert second is not first
    assert second["records"][0]["title"] == "Externally refreshed title"


def test_query_vector_cache_clamps_nonpositive_config(monkeypatch):
    _write_synthetic_index()
    embed = make_fake_embed()
    model = "fake-model"
    harness.embed_index_records(embed, model)
    harness._QUERY_VEC_CACHE.clear()
    monkeypatch.setattr(harness, "QUERY_VEC_CACHE_SIZE", 0)

    results = harness.retrieve_for_query("rust indexer", embed, model, k=1)

    assert results
    assert len(harness._QUERY_VEC_CACHE) == 1


def test_agent_dir_prefers_windows_home_when_wsl_home_lacks_agent_dirs(monkeypatch, tmp_path):
    """WSL repo runs should still index the user's Windows-hosted agent harnesses."""
    wsl_home = tmp_path / "wsl-home"
    windows_users = tmp_path / "Users"
    win_home = windows_users / "example"
    (win_home / ".openclaw").mkdir(parents=True)
    wsl_home.mkdir()

    monkeypatch.setenv("ALGO_CLI_ENABLE_WINDOWS_HOME_FALLBACK", "1")
    monkeypatch.setattr(harness, "HOME", wsl_home)
    monkeypatch.setattr(harness, "WINDOWS_USERS_ROOT", windows_users)

    assert harness._agent_dir(".openclaw") == win_home / ".openclaw"


def test_index_is_stale_detects_in_place_file_edits(monkeypatch, tmp_path):
    root_dir = tmp_path / "wiki"
    root_dir.mkdir()
    source = root_dir / "note.md"
    source.write_text("old text\n", encoding="utf-8")
    source_stat = source.stat()
    index_path = tmp_path / "harness_index.json"
    index_path.write_text(
        json.dumps(
                {
                    "source_policy": harness._source_policy(),
                    "records": [
                    {
                        "id": "test:wiki:note.md",
                        "path": str(source),
                        "file_mtime_ns": source_stat.st_mtime_ns,
                        "file_size": source_stat.st_size,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    newer_time = index_path.stat().st_mtime + 5
    os.utime(index_path, (newer_time, newer_time))
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None
    monkeypatch.setattr(harness, "INDEX_PATH", index_path)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (harness.SourceRoot("test", "wiki", root_dir, ("*.md",), 10),))
    monkeypatch.setattr(harness, "load_extra_source_roots", lambda: [])

    assert harness.index_is_stale() is False

    edited_time = newer_time + 5
    source.write_text("new text\n", encoding="utf-8")
    os.utime(source, (edited_time, edited_time))

    assert harness.index_is_stale() is True


def test_embed_index_records_does_not_mask_source_changes(monkeypatch, tmp_path):
    root_dir = tmp_path / "wiki"
    root_dir.mkdir()
    source = root_dir / "note.md"
    source.write_text("old text\n", encoding="utf-8")
    index_path = tmp_path / "harness_index.json"
    monkeypatch.setattr(harness, "INDEX_PATH", index_path)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (harness.SourceRoot("test", "wiki", root_dir, ("*.md",), 10),))
    monkeypatch.setattr(harness, "load_extra_source_roots", lambda: [])
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None
    source_stat = source.stat()
    index_payload = _synthetic_index()
    index_payload["records"].insert(
        0,
        {
            "id": "test:wiki:note.md",
            "harness": "test",
            "kind": "wiki",
            "title": "note",
            "path": str(source),
            "search_text": "old text",
            "file_mtime_ns": source_stat.st_mtime_ns,
            "file_size": source_stat.st_size,
        },
    )
    index_payload["record_count"] = len(index_payload["records"])
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")
    newer_time = index_path.stat().st_mtime + 5
    os.utime(index_path, (newer_time, newer_time))

    def editing_embed(texts: list[str]) -> list[list[float]]:
        edited_time = newer_time + 5
        source.write_text("edited during embedding\n", encoding="utf-8")
        os.utime(source, (edited_time, edited_time))
        return [[1.0] for _ in texts]

    result = harness.embed_index_records(editing_embed, "changing-model", batch_size=10)

    assert result["ready"] is False
    assert result["reason"] == "source_changed_during_embedding"
    assert harness.index_is_stale() is True
