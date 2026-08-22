from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from algo_cli.evals import nathan_prompt_capsule_qualification as benchmark


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "nathan_prompt_capsule_qualification.py"
SPEC = importlib.util.spec_from_file_location(
    "nathan_prompt_capsule_qualification_script",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


@pytest.fixture(scope="module")
def report() -> dict[str, object]:
    return benchmark.run_qualification(repetitions=benchmark.MIN_LATENCY_SAMPLES)


def _resign(report: dict[str, object]) -> None:
    unsigned = dict(report)
    unsigned.pop("report_sha256", None)
    report["report_sha256"] = benchmark._digest(unsigned)


def test_source_manifest_is_complete_and_content_bound() -> None:
    assert len(benchmark.SOURCE_PATHS) == len(set(benchmark.SOURCE_PATHS))
    assert all((ROOT / relative).is_file() for relative in benchmark.SOURCE_PATHS)
    assert benchmark.source_tree_digest().startswith("sha256:")


def test_controlled_ablation_passes_every_gate(report) -> None:
    assert report["status"] == "pass"
    assert all(report["gates"].values())
    assert report["public_claim_eligible"] is False
    benchmark.validate_report(report, require_current_source=True)


def test_report_rejects_digest_and_baseline_tampering(report) -> None:
    digest_tampered = deepcopy(report)
    digest_tampered["cases"]["simple"]["capsule"]["system_tokens"] += 1
    with pytest.raises(benchmark.PromptCapsuleQualificationError, match="reduction|digest"):
        benchmark.validate_report(digest_tampered, require_current_source=False)

    baseline_tampered = deepcopy(report)
    baseline_tampered["baseline"]["cases"]["simple"]["system_tokens"] += 1
    _resign(baseline_tampered)
    with pytest.raises(benchmark.PromptCapsuleQualificationError, match="baseline"):
        benchmark.validate_report(baseline_tampered, require_current_source=False)


def test_report_rejects_resigned_status_tampering(report) -> None:
    tampered = deepcopy(report)
    tampered["status"] = "fail"
    _resign(tampered)
    with pytest.raises(benchmark.PromptCapsuleQualificationError, match="status"):
        benchmark.validate_report(tampered, require_current_source=False)


def test_report_rejects_stale_source_binding(report, monkeypatch) -> None:
    monkeypatch.setattr(benchmark, "source_tree_digest", lambda: "sha256:" + "0" * 64)
    with pytest.raises(benchmark.PromptCapsuleQualificationError, match="source tree"):
        benchmark.validate_report(report, require_current_source=True)


def test_too_few_latency_samples_fail_closed() -> None:
    with pytest.raises(benchmark.PromptCapsuleQualificationError, match="below the minimum"):
        benchmark.run_qualification(repetitions=benchmark.MIN_LATENCY_SAMPLES - 1)


def test_artifact_round_trip_is_private_and_current(report, tmp_path, monkeypatch) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    output = hardening / "nathan-prompt-capsule-qualification.json"
    monkeypatch.setattr(SCRIPT, "HARDENING_ROOT", hardening)
    monkeypatch.setattr(SCRIPT, "DEFAULT_ARTIFACT", output)

    SCRIPT.write_artifact(output, report)
    assert SCRIPT.verify_artifact(output)["report_sha256"] == report["report_sha256"]
    if os.name == "posix":
        assert output.stat().st_mode & 0o777 == 0o600


def test_artifact_boundary_rejects_escape_symlink_and_failed_report(
    report,
    tmp_path,
    monkeypatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    monkeypatch.setattr(SCRIPT, "HARDENING_ROOT", hardening)

    with pytest.raises(SCRIPT.PromptCapsuleArtifactError, match="outside hardening"):
        SCRIPT.write_artifact(tmp_path / "outside.json", report)

    target = hardening / "target.json"
    target.write_text("unchanged", encoding="ascii")
    link = hardening / "report.json"
    link.symlink_to(target)
    with pytest.raises(SCRIPT.PromptCapsuleArtifactError, match="cannot replace"):
        SCRIPT.write_artifact(link, report)
    assert target.read_text(encoding="ascii") == "unchanged"

    failed = deepcopy(report)
    failed["cases"]["simple"]["capsule"]["assembly"]["p95_ms"] = 60.0
    failed["cases"]["simple"]["capsule"]["assembly"]["max_ms"] = 60.0
    failed["gates"]["assembly_latency"] = False
    failed["status"] = "fail"
    failed["claim"] = benchmark.FAIL_CLAIM
    _resign(failed)
    failed_path = hardening / "failed.json"
    failed_path.write_text(json.dumps(failed), encoding="ascii")
    with pytest.raises(SCRIPT.PromptCapsuleArtifactError, match="does not pass"):
        SCRIPT.verify_artifact(failed_path)
