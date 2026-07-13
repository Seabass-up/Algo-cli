"""Regression tests for weighted CodeRank and structural code retrieval."""

from __future__ import annotations

import pytest

from algo_cli import code_rag
from algo_cli.intelligence.coderank import CodeRank
from algo_cli.intelligence.project_graph import build_project_graph
from algo_cli.intelligence.repo_map import (
    rank_repo_map,
    render_repo_map,
    snapshot_project_graph,
)


def _write(root, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _constant_embed(texts: list[str]) -> list[list[float]]:
    return [[1.0, 1.0] for _text in texts]


def test_coderank_conserves_mass_and_redistributes_dangling_rank() -> None:
    results = CodeRank().compute(
        ["caller_a", "caller_b", "shared"],
        [("caller_a", "shared"), ("caller_b", "shared")],
    )

    assert sum(result.rank for result in results) == pytest.approx(1.0)
    assert results[0].symbol == "shared"


def test_coderank_honors_edge_weights_and_personalization() -> None:
    weighted = CodeRank().compute(
        ["source", "light", "heavy"],
        [("source", "light"), ("source", "heavy")],
        {("source", "light"): 1.0, ("source", "heavy"): 8.0},
    )
    personalized = CodeRank().compute(
        ["first", "second"],
        [],
        personalization={"first": 9.0, "second": 1.0},
    )

    ranks = {result.symbol: result.rank for result in weighted}
    preferences = {result.symbol: result.rank for result in personalized}
    assert ranks["heavy"] > ranks["light"]
    assert preferences == pytest.approx({"first": 0.9, "second": 0.1})


def test_repo_map_ranks_imported_core_and_respects_budget(tmp_path) -> None:
    _write(tmp_path, "z_core.py", "def validate_request(value):\n    return bool(value)\n")
    _write(tmp_path, "a_api.py", "import z_core\n\ndef endpoint(value):\n    return z_core.validate_request(value)\n")
    _write(tmp_path, "b_cli.py", "import z_core\n\ndef main(value):\n    return z_core.validate_request(value)\n")
    graph = build_project_graph(tmp_path, persist=False, include_git_recency=False)
    snapshot = snapshot_project_graph(graph)

    entries = rank_repo_map(snapshot, "validate request")
    rendered = render_repo_map(snapshot, "validate request", token_budget=45)

    assert entries[0].path == "z_core.py"
    assert "validate_request" in rendered
    assert len(rendered) <= 45 * 4


def test_project_graph_reuses_consent_filtered_inventory(tmp_path) -> None:
    _write(tmp_path, "allowed.py", "def visible():\n    return True\n")
    _write(tmp_path, "secrets/blocked.py", "def credential_value():\n    return 'private'\n")

    graph = build_project_graph(
        tmp_path,
        persist=False,
        source_files=["allowed.py"],
        include_git_recency=False,
    )

    assert set(graph.files) == {"allowed.py"}
    assert {symbol.name for symbol in graph.symbols.values()} == {"visible"}


def test_structural_snapshot_bounds_symbols_per_file(tmp_path) -> None:
    _write(
        tmp_path,
        "many.py",
        "\n".join(f"def symbol_{index}():\n    return {index}" for index in range(100)),
    )
    graph = build_project_graph(tmp_path, persist=False, include_git_recency=False)

    snapshot = snapshot_project_graph(graph)

    assert len(snapshot["files"]["many.py"]["symbols"]) == 80


def test_code_rag_fuses_structure_and_emits_score_provenance(tmp_path) -> None:
    _write(tmp_path, "a_api.py", "import z_core\n\ndef endpoint():\n    return z_core.run()\n")
    _write(tmp_path, "b_worker.py", "import z_core\n\ndef work():\n    return z_core.run()\n")
    _write(tmp_path, "z_core.py", "def run():\n    return 'shared implementation'\n")

    baseline = code_rag.retrieve(
        str(tmp_path),
        "repair shared behavior",
        _constant_embed,
        "constant",
        k=3,
        structural_weight=0.0,
    )
    enhanced = code_rag.retrieve(
        str(tmp_path),
        "repair shared behavior",
        _constant_embed,
        "constant",
        k=3,
    )

    assert baseline[0]["relative_path"] == "a_api.py"
    assert enhanced[0]["relative_path"] == "z_core.py"
    assert enhanced[0]["structural_score"] > enhanced[1]["structural_score"]
    assert enhanced[0]["retrieval_strategy"] == "semantic+structural"
    assert "Structural repository map" in enhanced[0]["repo_map"]
    assert "Structural repository map" in code_rag.format_code_context(enhanced)
