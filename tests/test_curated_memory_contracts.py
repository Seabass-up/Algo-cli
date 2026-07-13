"""Contracts for the curated, category-driven Algo CLI product memory set."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from algo_cli import harness


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
EXPECTED_MEMORY_DOCS = (
    "algo-cli-memory-lifecycle-contract.md",
    "algo-cli-execution-verification-contract.md",
    "algo-cli-algorithm-evidence-contract.md",
)
EXPECTED_WIKI_DOCS = (
    "harness-extension-cleanup-recommendation.md",
    "index-compute-lab-integration.md",
    "inference-harness-loop-blueprint-2026-06.md",
    "main-split-map.md",
    "reflex-loop-v0.2.md",
    "privacy-and-context.md",
)
EXPECTED_CATEGORIES = (
    "memory-lifecycle",
    "execution-verification",
    "algorithm-evidence",
)
RETIRED_MEMORY_DOCS = {
    "memory-facts-algo-cli.md",
    "audit-memory-and-rebrand-debt.md",
    "rebrand-lessons.md",
    "lessons-rebrand-algo-cli.md",
    "algo-cli-rebrand-2026-06.md",
    "graph-rebrand-ollama-cli-concept.md",
}


def _curated_records() -> list[dict[str, Any]]:
    root = harness.SourceRoot(
        "algo-cli",
        "memory",
        DOCS_ROOT,
        EXPECTED_MEMORY_DOCS,
        len(EXPECTED_MEMORY_DOCS),
    )
    return [harness.make_record(root, DOCS_ROOT / filename) for filename in EXPECTED_MEMORY_DOCS]


def test_curated_project_source_tuples_are_contract_focused() -> None:
    assert harness.CURATED_PROJECT_MEMORY_DOCS == EXPECTED_MEMORY_DOCS
    assert harness.CURATED_PROJECT_WIKI_DOCS == EXPECTED_WIKI_DOCS
    assert harness.REQUIRED_PRODUCT_MEMORY_CATEGORIES == EXPECTED_CATEGORIES
    assert RETIRED_MEMORY_DOCS.isdisjoint(harness.CURATED_PROJECT_MEMORY_DOCS)
    assert "quick-facts-algo-cli.md" not in harness.CURATED_PROJECT_WIKI_DOCS
    assert "wiki-openclaw-codex-algo-cli-rebrand.md" not in harness.CURATED_PROJECT_WIKI_DOCS

    roots = harness.built_in_source_roots()
    docs_memory_roots = [
        root
        for root in roots
        if root.harness == "algo-cli" and root.kind == "memory" and root.root == DOCS_ROOT
    ]
    docs_wiki_roots = [
        root
        for root in roots
        if root.harness == "algo-cli" and root.kind == "wiki" and root.root == DOCS_ROOT
    ]
    assert [root.patterns for root in docs_memory_roots] == [EXPECTED_MEMORY_DOCS]
    assert [root.patterns for root in docs_wiki_roots] == [EXPECTED_WIKI_DOCS]


@pytest.mark.parametrize(
    ("filename", "category"),
    tuple(zip(EXPECTED_MEMORY_DOCS, EXPECTED_CATEGORIES)),
)
def test_curated_memory_contract_frontmatter_and_scope(filename: str, category: str) -> None:
    text = (DOCS_ROOT / filename).read_text(encoding="utf-8")
    frontmatter = harness.parse_frontmatter(text)
    tags = {str(tag).lower() for tag in frontmatter.get("tags", [])}

    assert frontmatter["status"] == "active"
    assert frontmatter["title"]
    assert frontmatter["description"]
    assert "product-memory" in tags
    assert category in tags
    assert tags.intersection(EXPECTED_CATEGORIES) == {category}

    # These identity seeds already live in always-on memory.json. The contract
    # records should explain runtime behavior without creating another copy.
    lowered = text.lower()
    assert "ollama-cli" not in lowered
    assert "rebrand" not in lowered
    assert "concept:algo-cli" not in lowered
    assert "primary command is algo-cli" not in lowered
    assert "primary python package is algo_cli" not in lowered


@pytest.mark.parametrize(
    ("query", "expected_id"),
    (
        (
            "memory placement retention Echo Veil readiness",
            "algo-cli:memory:algo-cli-memory-lifecycle-contract.md",
        ),
        (
            "single mutation owner policy preflight verification agent team",
            "algo-cli:memory:algo-cli-execution-verification-contract.md",
        ),
        (
            "kernel readiness production algorithm effectiveness evidence probe",
            "algo-cli:memory:algo-cli-algorithm-evidence-contract.md",
        ),
    ),
)
def test_curated_memory_probe_queries_surface_expected_contract(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    expected_id: str,
) -> None:
    records = _curated_records()
    monkeypatch.setattr(harness, "load_index", lambda refresh=False: {"records": records})
    harness._BM25_INDEX_CACHE = None

    results = harness.search_index(query, harness="algo-cli", kind="memory", limit=3)

    assert results
    assert results[0]["id"] == expected_id


def test_product_memory_quality_uses_categories_and_excludes_personal_records() -> None:
    curated = _curated_records()
    personal = {
        "id": "algo-cli:memory:user-profile.md",
        "harness": "algo-cli",
        "kind": "memory",
        "title": "Private user profile",
        "path": str(REPO_ROOT / "personal" / "user-profile.md"),
        "relative_path": "user-profile.md",
        "tags": ["personal"],
    }
    graph_memory = {
        "id": "index-compute-lab:memory:atom.md",
        "harness": "index-compute-lab",
        "kind": "memory",
        "title": "Graph atom",
        "path": "__pytest__/atoms/atom.md",
        "relative_path": "atom.md",
        "tags": ["graph"],
    }

    quality = harness._index_quality_summary(
        [*curated, personal, graph_memory],
        {"complete": True},
    )

    assert quality["memory_records"] == 3
    assert quality["all_memory_records"] == 5
    assert quality["personal_memory_records"] == 1
    assert quality["curated_product_memory_records"] == 3
    assert quality["required_product_memory_categories"] == list(EXPECTED_CATEGORIES)
    assert quality["covered_product_memory_categories"] == list(EXPECTED_CATEGORIES)
    assert quality["missing_product_memory_categories"] == []

    missing_quality = harness._index_quality_summary(
        [*curated[:-1], personal, graph_memory],
        {"complete": True},
    )
    assert missing_quality["memory_records"] == 2
    assert missing_quality["personal_memory_records"] == 1
    assert missing_quality["covered_product_memory_categories"] == list(EXPECTED_CATEGORIES[:2])
    assert missing_quality["missing_product_memory_categories"] == ["algorithm-evidence"]
