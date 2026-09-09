from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runtime_profile", ROOT / "scripts/nathan_agent_runtime_profile.py")
assert SPEC is not None and SPEC.loader is not None
PROFILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROFILE)


@pytest.fixture
def synthetic_workload(monkeypatch):
    calls = []

    def workload(root, *, index):
        assert root.is_dir()
        calls.append(index)
        return {"total_ms": 12.5, "ttfa_ms": 1.5, "task_passed": True,
                "crash_resume_passed": True, "protocol_correct": True}

    monkeypatch.setattr(PROFILE.benchmark, "_frozen_agent_workload", workload)
    return calls


def test_profile_is_bounded_diagnostic_only(synthetic_workload) -> None:
    report = PROFILE.profile_runtime()

    assert synthetic_workload == list(range(700_000, 700_005))
    assert report["status"] == "diagnostic_only"
    assert report["public_claim_eligible"] is False
    assert report["samples"] == 5
    assert report["total_p50_ms"] == 12.5
    assert report["ttfa_p50_ms"] == 1.5
    assert all(report["correctness"].values())
    assert 1 <= len(report["functions"]) <= PROFILE.MAX_FUNCTIONS
    assert all(not Path(row["source"]).is_absolute() for row in report["functions"])
    assert report["limitations"] == PROFILE.LIMITATIONS
    assert "gates" not in report
    assert all("arguments" not in row and "callers" not in row for row in report["functions"])


def test_profile_rejects_source_drift(monkeypatch, synthetic_workload) -> None:
    monkeypatch.setattr(PROFILE.benchmark, "source_tree_digest", lambda: "sha256:" + "0" * 64)
    with pytest.raises(PROFILE.benchmark.AgentRuntimeBenchmarkError, match="source changed"):
        PROFILE.profile_runtime()


def test_source_labels_do_not_export_host_paths(tmp_path: Path) -> None:
    assert PROFILE._source_label("~") == "built-in"
    assert PROFILE._source_label(str(tmp_path / "outside.py")) == "outside.py"
    assert PROFILE._source_label(str(ROOT / "algo_cli/config.py")) == "algo_cli/config.py"


def test_profile_json_is_exclusive_and_keeps_previous_receipts(tmp_path: Path) -> None:
    first = PROFILE.write_profile(tmp_path, {"status": "diagnostic_only"})
    before = first.read_bytes()
    second = PROFILE.write_profile(tmp_path, {"status": "diagnostic_only", "new": True})
    assert first != second
    assert first.read_bytes() == before
    assert json.loads(second.read_bytes())["new"] is True
    assert first.suffix == second.suffix == ".json"


@pytest.mark.parametrize("report", [{"value": "x" * PROFILE.MAX_REPORT_BYTES}, {"value": float("nan")}],
                         ids=["oversized", "nonfinite"])
def test_profile_output_rejects_oversized_or_nonfinite_values(tmp_path: Path, report) -> None:
    with pytest.raises(ValueError):
        PROFILE.write_profile(tmp_path, report)
    assert not list(tmp_path.iterdir())
