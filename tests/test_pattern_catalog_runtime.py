from __future__ import annotations

import json
from pathlib import Path

import pytest

from algo_cli import harness
from algo_cli.pattern_catalog import PatternContext, exclusion_reasons, parse_patterns


ROOT = Path(__file__).resolve().parents[1]


def _index(monkeypatch, tmp_path: Path, text: str, previous=None):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    path = docs / "ALGO.md"
    path.write_text(text, encoding="utf-8")
    root = harness.SourceRoot("algo-cli", "algorithm", docs, ("ALGO.md",), 1)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (root,))
    index = harness.build_index(previous)
    harness._set_index_cache(index)
    monkeypatch.setattr(harness, "load_index", lambda **_kwargs: index)
    return index, path


def _pattern(pattern_id: str, title: str, **rules) -> str:
    return f"### {pattern_id}. {title}\n\n**Status:** proposed\n\n**Applicability:** {json.dumps(rules)}\n\n{title} useful contract.\n\n"


def test_pattern_spans_ignore_fenced_examples_and_unrelated_sections() -> None:
    source = """# ALGO.md
## Track O
### O1. First
**Status:** implemented
```markdown
### X9. Example only
**Applicability:** {"prerequisites": ["not-real"]}
```
## Other Section
Unrelated text
### O2. Last
**Status:** proposed
Tail contract
"""
    patterns = parse_patterns(source)
    assert [row["pattern_id"] for row in patterns] == ["O1", "O2"]
    assert "Unrelated text" not in patterns[0]["body"]
    assert patterns[0]["applicability"]["prerequisites"] == []
    assert patterns[1]["source_start_line"] == 11
    assert patterns[1]["body"].endswith("Tail contract\n")


def test_real_late_catalog_patterns_rank_and_read_as_individual_contracts(monkeypatch, tmp_path: Path) -> None:
    index, _ = _index(monkeypatch, tmp_path, (ROOT / "docs/ALGO.md").read_text())
    assert index["pattern_stats"]["records"] >= 531
    cases = [
        ("capability snapshot model routing", "O1"),
        ("version gated catalog canary", "O2"),
        ("installed artifact identity check", "O3"),
        ("prerequisite partitioned diagnostic probes", "O4"),
        ("multi window error budget burn rate", "L9"),
        ("held out synthetic canary protocol", "B471"),
    ]
    for query, expected in cases:
        results = harness.hybrid_search(query, lambda _: [], k=3, kind="algorithm")
        assert results[0].get("pattern_id") == expected, [(row["id"], row["score"]) for row in results]
        assert results[0]["source_start_line"] > 13_000
        record = harness.get_record(results[0]["id"])
        assert record is not None
        assert expected.lower() in harness._record_text_for_embed(record)
        read = harness.read_record(results[0]["id"])
        assert "Lines:" in read
        assert "## Commands" not in read
        assert "reference only" in harness.format_retrieved_context(results)


def test_changed_pattern_loses_embedding_but_unchanged_pattern_keeps_it(monkeypatch, tmp_path: Path) -> None:
    text = _pattern("O1", "First") + _pattern("O2", "Second")
    index, _ = _index(monkeypatch, tmp_path, text)
    for row in index["records"]:
        if row.get("pattern_id"):
            row.update(embedding=[1.0, 0.0], embedding_model="test")
    refreshed, _ = _index(monkeypatch, tmp_path, text.replace("First", "Changed"), previous=index)
    rows = {row["pattern_id"]: row for row in refreshed["records"] if row.get("pattern_id")}
    assert "embedding" not in rows["O1"]
    assert rows["O2"]["embedding"] == [1.0, 0.0]
    assert refreshed["pattern_stats"]["reused_embeddings"] == 1
    removed, _ = _index(monkeypatch, tmp_path, _pattern("O1", "Changed"), previous=refreshed)
    assert {row["pattern_id"] for row in removed["records"] if row.get("pattern_id")} == {"O1"}


def test_read_rejects_stale_span_instead_of_reading_wrong_pattern(monkeypatch, tmp_path: Path) -> None:
    index, path = _index(monkeypatch, tmp_path, _pattern("O1", "First"))
    record = next(row for row in index["records"] if row.get("pattern_id"))
    path.write_text(_pattern("O2", "Replacement"))
    assert "Error: pattern source changed" in harness.read_record(record["id"])


@pytest.mark.parametrize(
    "malformed",
    [
        '{"prerequisites": null}',
        '{"resource_costs": {"tokens": true}}',
        '{"environments": ["darwin"], "environments": []}',
        '{"shell": "touch unsafe"}',
    ],
)
def test_bad_applicability_never_executes_or_enters_rankers(monkeypatch, tmp_path: Path, malformed: str) -> None:
    text = f"### O1. Exact answer\n**Applicability:** {malformed}\n" + _pattern("O2", "Exact answer")
    index, _ = _index(monkeypatch, tmp_path, text)
    rejected = next(row for row in index["records"] if row.get("pattern_id") == "O1")
    assert exclusion_reasons(rejected) == ["invalid_applicability"]
    assert all(row.get("pattern_id") != "O1" for row in harness.search_index("exact answer"))


def test_applicability_filters_before_lexical_vector_and_fusion_ranking(monkeypatch, tmp_path: Path) -> None:
    source = _pattern(
        "O1", "Quantum routing", environments=["windows"], prerequisites=["docker"], resource_costs={"tokens": 100}
    )
    source += _pattern("O2", "Quantum routing", environments=["darwin"], resource_costs={"tokens": 2})
    index, _ = _index(monkeypatch, tmp_path, source)
    for row in index["records"]:
        if row.get("pattern_id"):
            row.update(embedding=[1.0, 0.0], embedding_model="test")
    context = PatternContext(environment="darwin", resource_budgets={"tokens": 10})
    for results in (
        harness.search_index("quantum", limit=1, pattern_context=context),
        harness.retrieve_for_query("quantum", lambda _: [[1.0, 0.0]], "test", k=1, pattern_context=context),
        harness.hybrid_search("quantum", lambda _: [[1.0, 0.0]], "test", k=1, pattern_context=context),
    ):
        assert results[0]["pattern_id"] == "O2"
    blocked = next(row for row in index["records"] if row.get("pattern_id") == "O1")
    assert exclusion_reasons(blocked, context) == [
        "unsupported_environment",
        "missing_prerequisite:docker",
        "resource_budget:tokens",
    ]


def test_pairwise_conflicts_and_aggregate_costs_apply_to_selection() -> None:
    def row(identity, conflicts=()):
        return {
            "id": identity,
            "pattern_id": identity,
            "applicability": {"conflicts": list(conflicts), "resource_costs": {"tokens": 6}},
        }

    scored = [(4.0, row("O1")), (3.0, row("O2", ["O1"])), (2.0, row("O3")), (1.0, {"id": "ordinary"})]
    selected = harness._select_pattern_compatible(scored, 3, PatternContext(resource_budgets={"tokens": 10}))
    assert [record["id"] for _, record in selected] == ["O1", "ordinary"]


def test_same_size_filtered_slices_do_not_reuse_wrong_corpus_or_matrix() -> None:
    a = {"search_text": "alpha", "embedding": [1.0, 0.0]}
    b = {"search_text": "beta", "embedding": [0.0, 1.0]}
    c = {"search_text": "gamma", "embedding": [1.0, 1.0]}
    d = {"search_text": "delta", "embedding": [0.1, 0.9]}
    kwargs = {"harness_names": None, "kind": None, "excluded_kinds": frozenset()}
    first = harness._candidate_bm25_index([a, b, d], **kwargs)
    second = harness._candidate_bm25_index([a, c, d], **kwargs)
    assert first is not second
    if harness._NUMPY:
        first_rows, _ = harness._normalized_candidate_matrix([a, b, d], model="test", dimensions=2, **kwargs)
        second_rows, _ = harness._normalized_candidate_matrix([a, c, d], model="test", dimensions=2, **kwargs)
        assert first_rows[1] is b
        assert second_rows[1] is c


def test_active_conflicts_work_in_both_directions(monkeypatch, tmp_path):
    source = _pattern("O1", "Active", conflicts=["O2"]) + _pattern("O2", "Unique answer")
    source += _pattern("O3", "Unique answer", conflicts=["O1"]) + _pattern("O4", "Unique answer")
    _index(monkeypatch, tmp_path, source)
    results = harness.search_index("unique answer", pattern_context=PatternContext(active_patterns=frozenset({"O1"})))
    assert {row.get("pattern_id") for row in results}.isdisjoint({"O2", "O3"})
    assert any(row.get("pattern_id") == "O4" for row in results)


def test_pattern_title_and_applicability_are_redacted(monkeypatch, tmp_path):
    secret = "sk-" + "a" * 48
    index, _ = _index(monkeypatch, tmp_path, _pattern("O1", f"Title {secret}", fallback=f"token={secret}"))
    leaked_fields = [
        (row["id"], key) for row in index["records"] for key, value in row.items() if secret in json.dumps(value)
    ]
    assert leaked_fields == []


def test_rust_index_receives_same_pattern_postprocessing(monkeypatch, tmp_path):
    from types import SimpleNamespace

    index, _ = _index(monkeypatch, tmp_path, _pattern("O1", "Contract"))
    parent = next(row for row in index["records"] if row["id"] == "algo-cli:algorithm:ALGO.md")
    output = tmp_path / "native.json"
    monkeypatch.setattr(harness, "_EXTERNAL_SOURCES_ENABLED", True)
    monkeypatch.setattr(harness, "find_rust_indexer", lambda: tmp_path / "native-indexer")
    monkeypatch.setattr(harness, "INDEX_PATH", output)

    def run(*_args, **_kwargs):
        output.write_text(json.dumps({"records": [parent]}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(harness.subprocess, "run", run)
    result = harness.build_index_with_rust()
    assert result["pattern_stats"]["records"] == 1
    assert result["source_policy"]["pattern_records"] is True
    assert any(row.get("pattern_id") == "O1" for row in result["records"])


def test_pre_pattern_index_requires_migration(monkeypatch, tmp_path):
    index, _ = _index(monkeypatch, tmp_path, _pattern("O1", "Contract"))
    path = tmp_path / "index.json"
    monkeypatch.setattr(harness, "INDEX_PATH", path)
    old_policy = {key: value for key, value in harness._source_policy().items() if key != "pattern_records"}
    path.write_text(json.dumps({**index, "source_policy": old_policy}))
    assert harness.index_is_stale() is True
