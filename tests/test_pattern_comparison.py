from __future__ import annotations

from pathlib import Path

import pytest

from algo_cli import harness, main, pattern_runtime
from algo_cli.config import Config
from algo_cli.evals import pattern_comparison as comparison
from algo_cli.evals import pattern_evidence as evidence


ROOT = Path(__file__).resolve().parents[1]


def test_comparison_runs_real_ranker_without_replacing_live_index(monkeypatch, tmp_path):
    sentinel = {"records": [{"id": "live"}]}
    monkeypatch.setattr(harness, "load_index", lambda **_kwargs: sentinel)
    report = comparison.run_comparison(ROOT, tmp_path, repetitions=2)
    assert harness.load_index() is sentinel
    assert report["status"] == "complete"
    assert len(report["samples"]) == len(comparison.TASKS) * 4 * 2
    assert report["cells"]["records_1_rules_1"]["accuracy"] == 1
    assert report["cells"]["records_0_rules_0"]["accuracy"] == 0
    assert report["cells"]["records_1_rules_1"]["policy_violations"] == 0
    assert report["cells"]["records_1_rules_0"]["policy_violations"] > 0
    assert report["accuracy_interaction"] > 0
    assert all(row["provider_tokens"] == 0 for row in report["samples"])
    assert evidence.evidence_status(ROOT, "O5", tmp_path)["measured"] is True
    assert evidence.evidence_status(ROOT, "O4", tmp_path)["measured"] is False
    assert comparison.complete_report(report) is True
    report["samples"].pop()
    evidence.save_report(
        tmp_path / "comparison.json", {key: value for key, value in report.items() if key != "artifact_digest"}
    )
    assert comparison.complete_report(report) is False
    assert evidence.evidence_status(ROOT, "O5", tmp_path)["measured"] is False


@pytest.mark.parametrize("repetitions", [True, 1, 21, 2.0])
def test_comparison_rejects_unbounded_or_unpaired_runs(tmp_path, repetitions):
    with pytest.raises(ValueError):
        comparison.run_comparison(ROOT, tmp_path, repetitions=repetitions)


def test_operator_dispatch_and_invalid_commands(monkeypatch):
    calls = []

    class Console:
        def print(self, value, **kwargs):
            calls.append(value)

    monkeypatch.setattr(main, "console", Console())
    monkeypatch.setattr(pattern_runtime, "run_command", lambda arg: {"requested": arg})
    handled, _ = main.handle_command("/harness patterns status O5", Config(), None)
    assert handled is True
    assert '"requested": "status O5"' in calls[0]


def test_unknown_id_and_markdown_commands_cannot_execute(tmp_path):
    with pytest.raises(ValueError, match="not_registered"):
        pattern_runtime.run_command("verify shell", root=ROOT, directory=tmp_path)
    with pytest.raises(ValueError, match="Usage"):
        pattern_runtime.run_command("verify O4 --extra", root=ROOT, directory=tmp_path)


def test_explain_does_not_run_fallback(monkeypatch):
    monkeypatch.setattr(
        harness,
        "load_index",
        lambda: {
            "records": [
                {
                    "pattern_id": "O5",
                    "status": "proposed",
                    "applicability_valid": True,
                    "applicability_declared": True,
                    "source_digest": "snapshot",
                    "applicability": {"prerequisites": ["missing"], "fallback": "do not run this"},
                }
            ]
        },
    )
    result = pattern_runtime.run_command("explain O5")
    assert result["eligible"] is False
    assert result["excluded_by"] == ["missing_prerequisite:missing"]
