"""Self-evaluation source selection must survive a fully embedded corpus."""

import pytest

from algo_cli import harness


@pytest.mark.parametrize("query, pinned", [("rate your harness", True), ("find harness examples", False)])
def test_hybrid_self_evaluation_retains_canonical_reference(monkeypatch, query, pinned):
    records = [
        {
            "id": "algo-cli:algorithm:ALGO.md",
            "harness": "algo-cli",
            "kind": "algorithm",
            "relative_path": "ALGO.md",
            "search_text": "rate your harness examples",
            "embedding": [0.0, 1.0],
            "embedding_model": "canonical-test",
        },
        *[
            {
                "id": f"algo-cli:skill:{i}.md",
                "harness": "algo-cli",
                "kind": "skill",
                "relative_path": f"{i}.md",
                "search_text": "rate your harness examples",
                "embedding": [1.0, 0.0],
                "embedding_model": "canonical-test",
            }
            for i in range(12)
        ],
    ]
    monkeypatch.setattr(harness, "load_index", lambda: {"records": records})
    hits = harness.hybrid_search(query, lambda _: [[1.0, 0.0]], "canonical-test", k=3)
    assert (hits[0]["id"] == "algo-cli:algorithm:ALGO.md") is pinned
    assert (hits[0]["rank_provenance"].get("selection_reason") == "canonical-harness-reference") is pinned
    if pinned:
        assert hits[0]["rank_provenance"]["keyword_rank"] == 1
        assert "vector_rank" not in hits[0]["rank_provenance"]
        assert hits[0]["score"] < hits[1]["score"]  # selection is explicit, not a fabricated RRF score
    filtered = harness.hybrid_search(query, lambda _: [[1.0, 0.0]], "canonical-test", k=3, kind="skill")
    assert all(hit["kind"] == "skill" for hit in filtered)
