"""The evaluator must exercise production retrieval and preserve failed evidence."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from algo_cli import harness
from algo_cli.evals import grounded_retrieval as evaluation


@pytest.fixture
def grounded(monkeypatch, tmp_path):
    docs = tmp_path / "public"
    docs.mkdir()
    source = docs / "runtime-capability-catalog.md"
    source.write_text("# Tools\n\nExact query recovery. Discovery evidence, not authority.\n")
    monkeypatch.setattr(harness, "_algo_cli_docs_dir", lambda: docs)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (harness.SourceRoot("algo-cli", "wiki", docs, (source.name,), 1),))
    case = evaluation.Case("fixture", "exact", "exact query recovery", (evaluation.CAPABILITY,))
    monkeypatch.setattr(evaluation, "CASES", (case,))
    harness.load_index(refresh=True)
    harness.embed_index_records(lambda texts: [[1.0, 0.5] for _ in texts], "fixture")
    return source, case


def test_grounded_evaluation_uses_real_search_and_read(grounded):
    calls = []

    def embed(texts):
        calls.append(texts)
        return [[1.0, 0.5]]

    report = evaluation.run_grounded_retrieval(embed, "fixture")
    assert report["status"] == "pass"
    assert report["source_stable"] is True
    assert report["metrics"]["passed"] == 2
    assert report["metrics"]["mrr"] == 1
    assert calls == [["exact query recovery"]]
    assert all(sample["vector_hits"] for sample in report["samples"])
    json.dumps(report, allow_nan=False)


def test_lexical_fallback_is_not_semantic_qualification(grounded):
    report = evaluation.run_grounded_retrieval(lambda _: [], "fixture")
    assert report["status"] == "fail"
    assert report["metrics"]["mean_labeled_recall_at_k"] == 1
    assert all(sample["vector_hits"] == 0 for sample in report["samples"])


def test_label_missing_or_read_body_missing_fails(grounded, monkeypatch):
    monkeypatch.setattr(
        harness, "read_record", lambda *_args, **_kwargs: "# Discovery evidence, not authority\n\nSource: fixture\n\n"
    )
    report = evaluation.run_grounded_retrieval(lambda _: [[1.0, 0.5]], "fixture")
    assert report["status"] == "fail"
    assert report["label_failures"] == [evaluation.CAPABILITY.record_id]
    assert report["metrics"]["passed"] == 0


def test_source_change_during_query_invalidates_entire_run(grounded):
    source, _ = grounded

    def embed(_):
        source.write_text(source.read_text() + "Changed source.\n")
        return [[1.0, 0.5]]

    report = evaluation.run_grounded_retrieval(embed, "fixture")
    assert report["status"] == "fail"
    assert report["source_stable"] is False


@pytest.mark.parametrize("failure", ["exception", "duplicates", "filter"])
def test_search_failures_are_not_rewritten_as_passes(grounded, monkeypatch, failure):
    def search(*_args, **_kwargs):
        if failure == "exception":
            raise RuntimeError("private content must not be reported")
        hit = {"id": evaluation.CAPABILITY.record_id, "harness": "external" if failure == "filter" else "algo-cli"}
        return [hit, hit] if failure == "duplicates" else [hit]

    monkeypatch.setattr(harness, "hybrid_search", search)
    report = evaluation.run_grounded_retrieval(lambda _: [[1.0, 0.5]], "fixture")
    assert report["status"] == "fail"
    assert report["metrics"]["passed"] == 0
    assert all(sample["error"] for sample in report["samples"])
    assert "private content" not in json.dumps(report)


def test_all_multi_source_labels_are_required(grounded, monkeypatch):
    source, case = grounded
    other = source.parent / "privacy-and-context.md"
    other.write_text("# Recovery\n\nAlways never restart the agent or tool batch.\n")
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (harness.SourceRoot("algo-cli", "wiki", source.parent, ("*.md",), 2),))
    harness.load_index(refresh=True)
    harness.embed_index_records(lambda texts: [[1.0, 0.5] for _ in texts], "fixture")
    second = evaluation.Evidence("algo-cli:wiki:privacy-and-context.md", evaluation.AUTH.anchor)
    monkeypatch.setattr(evaluation, "CASES", (replace(case, evidence=(evaluation.CAPABILITY, second)),))
    monkeypatch.setattr(
        harness,
        "hybrid_search",
        lambda *_args, **_kwargs: [
            {"id": evaluation.CAPABILITY.record_id, "harness": "algo-cli", "kind": "wiki", "rank_sources": ["vector"]}
        ],
    )
    report = evaluation.run_grounded_retrieval(lambda _: [[1.0, 0.5]], "fixture")
    assert report["status"] == "fail"
    assert not report["label_failures"], report
    assert report["metrics"]["mean_labeled_recall_at_k"] == 0.5


@pytest.mark.parametrize("status", ["historical", "backlog", "superseded"])
def test_ineligible_positive_label_stops_before_provider_call(grounded, status):
    record = harness.get_record(evaluation.CAPABILITY.record_id)
    record["status"] = status
    report = evaluation.run_grounded_retrieval(lambda _: pytest.fail("no provider request"), "fixture")
    assert report["status"] == "fail"
    assert report["stage"] == "label_validation"
    assert report["label_failures"] == [evaluation.CAPABILITY.record_id]
    assert report["metrics"]["samples"] == 0


@pytest.mark.parametrize("behavior", ["absent", "present", "exception"])
def test_exclusion_checks_are_separate_and_fail_closed(grounded, monkeypatch, behavior):
    source, _ = grounded
    old = source.parent / "reflex-loop-v0.2.md"
    old.write_text("---\nstatus: historical\n---\n# Historical reference\n\nOld design contract.\n")
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (harness.SourceRoot("algo-cli", "wiki", source.parent, ("*.md",), 2),))
    harness.load_index(refresh=True)
    harness.embed_index_records(lambda texts: [[1.0, 0.5] for _ in texts], "fixture")
    evidence = evaluation.Evidence("algo-cli:wiki:reflex-loop-v0.2.md", "Old design contract.")
    negative = evaluation.Case("historical", "exclusion", "historical reference", (evidence,))
    original = harness.hybrid_search

    def search(query, *args, **kwargs):
        if query == negative.query:
            if behavior == "exception":
                raise RuntimeError("private details")
            if behavior == "present":
                return [{"id": evidence.record_id, "kind": "wiki", "harness": "algo-cli"}]
            return []
        return original(query, *args, **kwargs)

    monkeypatch.setattr(harness, "hybrid_search", search)
    report = evaluation.run_grounded_retrieval(lambda _: [[1.0, 0.5]], "fixture", exclusion_cases=(negative,))
    assert not report["label_failures"], report
    assert report["status"] == ("pass" if behavior == "absent" else "fail")
    assert report["metrics"]["samples"] == report["metrics"]["passed"] == 2
    assert report["policy_metrics"] == {"samples": 2, "passed": 2 if behavior == "absent" else 0}
    assert "private details" not in json.dumps(report)


def test_eligible_source_cannot_be_an_exclusion_label(grounded):
    _, case = grounded
    report = evaluation.run_grounded_retrieval(
        lambda _: pytest.fail("no provider request"),
        "fixture",
        exclusion_cases=(replace(case, name="negative"),),
    )
    assert report["label_failures"] == [evaluation.CAPABILITY.record_id]


def test_original_inventory_unchanged_and_historical_label_correction_explicit():
    from dataclasses import asdict
    from algo_cli.evals import grounded_retrieval_validation as validation

    assert (
        evaluation.digest([asdict(case) for case in evaluation.CASES])
        == "sha256:2330b374808a129b7cb76bffabc97d1b05129b74bb95cb28b1d03b29e0e301a2"
    )
    assert len(validation.CASES) == 19
    assert [case.name for case in validation.EXCLUSION_CASES] == ["reflex_spec"]
    assert len(validation.CHALLENGE_CASES) == 8


def test_operator_memory_corpus_rejected_before_provider_call(grounded, monkeypatch):
    monkeypatch.setattr(harness, "_protected_memory_record_allowed", lambda _: False)
    with pytest.raises(ValueError, match="public-only"):
        evaluation.run_grounded_retrieval(lambda _: pytest.fail("no provider request"), "fixture")


def test_cancellation_propagates(grounded):
    def embed(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        evaluation.run_grounded_retrieval(embed, "fixture")


@pytest.mark.parametrize("repetitions", [True, 0, 1, 11, 2.0])
def test_repetitions_bound(grounded, repetitions):
    with pytest.raises(ValueError):
        evaluation.run_grounded_retrieval(lambda _: [], "fixture", repetitions=repetitions)


@pytest.mark.parametrize("option", [["--host", "https://example.com"], ["--model", "anything:cloud"]])
def test_runner_rejects_remote_or_cloud_before_touching_state(tmp_path, option):
    script = Path(__file__).resolve().parents[1] / "scripts/grounded_retrieval_qualification.py"
    result = subprocess.run(
        [sys.executable, str(script), "--output", str(tmp_path / "report.json"), *option],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert not (tmp_path / "report.json").exists()
