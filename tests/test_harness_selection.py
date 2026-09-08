"""Selection policy must retain distinct capabilities and all applicability gates."""

from copy import deepcopy

import pytest

from algo_cli import harness
from algo_cli.pattern_catalog import PatternContext


def _row(name, path="/public/ALGO.md", **fields):
    return {"id": name, "harness": "algo-cli", "kind": "algorithm", "path": path, **fields}


def _ids(selected):
    return [row["id"] for _, row in selected]


def test_source_selection_penalizes_repetition_without_fabricating_scores():
    scored = [(10.0, _row("first")), (9.0, _row("second")), (8.0, _row("guide", "/public/guide.md"))]
    before = deepcopy(scored)
    selected = harness._select_pattern_compatible(scored, 3, diversify_sources=True)
    assert _ids(selected) == ["first", "guide", "second"]
    assert [score for score, _ in selected] == [10, 8, 9]
    assert scored == before
    assert _ids(harness._select_pattern_compatible(scored, 3)) == ["first", "second", "guide"]


@pytest.mark.parametrize("kind,path", [("runtime_capability", "/public/registry.py"), ("wiki", "")])
def test_independent_capabilities_and_missing_paths_do_not_collapse(kind, path):
    scored = [
        (10.0, _row("first", path, kind=kind)),
        (9.0, _row("second", path, kind=kind)),
        (8.0, _row("guide", "/public/guide.md")),
    ]
    assert _ids(harness._select_pattern_compatible(scored, 2, diversify_sources=True)) == ["first", "second"]


def test_explicit_identifiers_still_obey_conflicts_and_resource_budgets():
    rows = [
        _row("O1", pattern_id="O1", applicability={"resource_costs": {"tokens": 6}}),
        _row("O2", pattern_id="O2", applicability={"conflicts": ["O1"]}),
        _row("O3", pattern_id="O3", applicability={"resource_costs": {"tokens": 5}}),
        _row("ordinary", "/public/guide.md"),
    ]
    selected = harness._select_pattern_compatible(
        list(zip([4.0, 3.0, 2.0, 1.0], rows)),
        4,
        PatternContext(resource_budgets={"tokens": 10}),
        preferred_ids=frozenset({"O1", "O2", "O3"}),
        diversify_sources=True,
    )
    assert _ids(selected) == ["O1", "ordinary"]


def test_explicit_records_do_not_penalize_each_other_and_ties_are_stable():
    scored = [(2.0, _row("noise")), (1.0, _row("O4")), (1.0, _row("O5"))]
    for _ in range(2):
        assert _ids(
            harness._select_pattern_compatible(
                scored,
                2,
                preferred_ids=frozenset({"O4", "O5"}),
                diversify_sources=True,
            )
        ) == ["O4", "O5"]


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_limit_has_no_provider_side_effect(monkeypatch, limit):
    monkeypatch.setattr(harness, "load_index", lambda: pytest.fail("no index read"))
    assert harness.hybrid_search("query", lambda _: pytest.fail("no provider call"), k=limit) == []


@pytest.mark.parametrize("query,expected", [("Compare O4 and O5", True), ("O40", False), ("pO4", False)])
def test_identifier_priority_is_exact_and_case_insensitive(query, expected):
    assert harness._query_identifies_record(_row("O4", pattern_id="O4"), frozenset(query.lower().split())) is expected


def test_slim_capability_retains_identifier_without_large_metadata():
    row = _row(
        "tool",
        kind="runtime_capability",
        relative_path="action-registry/git_diff",
        capability={"name": "git_diff", "approval_required": True},
    )
    slim = harness._slim_record(row)
    assert "capability" not in slim
    assert harness._query_identifies_record(slim, frozenset({"git_diff"}))
    assert not harness._query_identifies_record(slim, frozenset({"git", "diff"}))


def test_named_pattern_enters_candidate_pool_without_bypassing_exclusions(monkeypatch):
    records = [_row(f"noise{i}", search_text="O4 context", title="O4", relative_path="O4.md") for i in range(20)]
    target = _row(
        "O4", pattern_id="O4", search_text="O4", status="proposed", applicability_valid=True, applicability={}
    )
    records.append(target)
    monkeypatch.setattr(harness, "load_index", lambda: {"records": records})
    assert harness._rank_keyword_records("O4", limit=1)[0][1]["id"] == "O4"
    target["status"] = "historical"
    assert all(row["id"] != "O4" for _, row in harness._rank_keyword_records("O4", limit=30))


def test_kind_filter_disables_source_penalty_and_provenance_discloses_policy(monkeypatch):
    records = [
        _row("first", kind="wiki", search_text="query", embedding=[1.0, 0.0], embedding_model="fixture"),
        _row("second", kind="wiki", search_text="query", embedding=[1.0, 0.0], embedding_model="fixture"),
        _row(
            "guide",
            "/public/guide.md",
            kind="wiki",
            search_text="query",
            embedding=[1.0, 0.0],
            embedding_model="fixture",
        ),
    ]
    monkeypatch.setattr(harness, "load_index", lambda: {"records": records})

    def embed(_):
        return [[1.0, 0.0]]

    hits = harness.hybrid_search("query", embed, "fixture", k=2)
    assert [row["id"] for row in hits] == ["first", "guide"]
    assert all(row["rank_provenance"]["source_repeat_penalty"] == 0.5 for row in hits)
    assert hits[1]["score"] == hits[1]["rank_provenance"]["rrf_score"]
    filtered = harness.hybrid_search("query", embed, "fixture", k=2, kind="wiki")
    assert [row["id"] for row in filtered] == ["first", "second"]
    assert all(row["rank_provenance"]["source_repeat_penalty"] == 0.0 for row in filtered)


def test_keyword_only_fallback_preserves_lexical_selection(monkeypatch):
    records = [
        _row("first", search_text="query"),
        _row("second", search_text="query"),
        _row("guide", "/public/guide.md", search_text="query"),
    ]
    monkeypatch.setattr(harness, "load_index", lambda: {"records": records})
    expected = [row["id"] for row in harness.search_index("query", limit=2)]
    hits = harness.hybrid_search("query", lambda _: [], "fixture", k=2)
    assert [row["id"] for row in hits] == expected
    assert all(row["rank_sources"] == ["keyword"] for row in hits)
    assert all(row["rank_provenance"]["source_repeat_penalty"] == 0.0 for row in hits)
