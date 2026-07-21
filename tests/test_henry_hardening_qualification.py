from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from algo_cli.evals.tool_context_efficiency import run_tool_context_efficiency_benchmark
from algo_cli.henry_hardening_qualification import (
    build_qualification_report,
    protocol_metric,
    run_policy_qualification,
    run_postcondition_qualification,
    run_privacy_qualification,
    run_program_rejection_qualification,
    run_race_qualification,
    run_unknown_outcome_qualification,
    wilson_interval,
)


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "hardening" / "grace-m8-local-qualification.json"
SCRIPT_PATH = ROOT / "scripts" / "henry_m8_qualification.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("henry_m8_qualification_script", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = SCRIPT
SCRIPT_SPEC.loader.exec_module(SCRIPT)


def _protocol_report(*, iterations: int = 100_000, accepts: int = 0, crashes: int = 0) -> dict:
    return {
        "iterations": iterations,
        "rejected": iterations - accepts - crashes,
        "unexpected_accepts": accepts,
        "unexpected_crashes": crashes,
        "maximum_case_bytes": 4096,
        "maximum_buffered_bytes": 4092,
        "corpus_digest": "sha256:" + "1" * 64,
        "classification_digest": "sha256:" + "2" * 64,
        "passed": accepts == 0 and crashes == 0,
    }


def test_local_finite_qualifiers_expose_denominators_without_overclaiming() -> None:
    race = run_race_qualification(trials=5)
    postcondition = run_postcondition_qualification(trials=5)
    unknown = run_unknown_outcome_qualification(trials=5)
    programs = run_program_rejection_qualification(trials=25)
    privacy = run_privacy_qualification(trials=5)

    assert race.status == "not_verified"
    assert race.numerator == race.denominator == 5
    assert race.measurements["mutations"] == 0
    assert race.measurements["dispatches"] == 0
    assert postcondition.status == "not_verified"
    assert postcondition.numerator == postcondition.denominator == 5
    assert postcondition.measurements["mutations"] == 5
    assert unknown.status == "not_verified"
    assert unknown.measurements["extra_dispatches"] == 0
    assert unknown.measurements["automatic_reconciliations"] == 0
    assert programs.status == "not_verified"
    assert programs.numerator == programs.denominator == 25
    assert programs.measurements["unexpected_errors"] == 0
    assert privacy.status == "not_verified"
    assert privacy.numerator == privacy.denominator == 5


def test_policy_qualification_covers_current_tools_and_hostile_unknowns() -> None:
    metric = run_policy_qualification()
    assert metric.status == "pass"
    assert metric.numerator == metric.denominator
    assert metric.measurements["generated_privileged_specs"] == 0
    assert metric.measurements["unconfirmed_protected_actions"] == 0
    assert metric.measurements["hostile_unknowns_rejected"] == 1_000


def test_protocol_threshold_requires_full_denominator_and_zero_faults() -> None:
    passed = protocol_metric(_protocol_report())
    too_small = protocol_metric(_protocol_report(iterations=99_999))
    accepted = protocol_metric(_protocol_report(accepts=1))
    crashed = protocol_metric(_protocol_report(crashes=1))

    assert passed.status == "pass"
    assert too_small.status == "not_verified"
    assert accepted.status == "fail"
    assert crashed.status == "fail"


def test_report_stays_blocked_when_live_browser_evidence_is_absent() -> None:
    report = build_qualification_report(
        protocol_report=_protocol_report(),
        efficiency_report=run_tool_context_efficiency_benchmark(repeats=3),
        focused_suite_passed=True,
        source_digest="sha256:" + "a" * 64,
        race_trials=5,
        postcondition_trials=5,
        unknown_trials=5,
        program_trials=25,
        privacy_trials=5,
        generated_at="2026-07-19T20:00:00Z",
    )

    assert report["status"] == "blocked"
    assert report["public_claim_eligible"] is False
    assert report["summary"]["blocked"] == 5
    assert report["summary"]["fail"] == 0
    assert report["fixture_digest"].startswith("sha256:")
    rendered = json.dumps(report, sort_keys=True)
    assert "algo-private-" not in rendered
    assert "zero risk" in rendered.lower()


def test_metric_and_wilson_contracts_fail_closed() -> None:
    assert wilson_interval(100, 100)[1] == 1.0
    with pytest.raises(ValueError, match="qualification_interval"):
        wilson_interval(2, 1)
    metric = run_policy_qualification()
    with pytest.raises(ValueError, match="qualification_metric"):
        replace(metric, status="ready")
    with pytest.raises(ValueError, match="qualification_denominator"):
        replace(metric, numerator=metric.denominator + 1)


def test_private_atomic_evidence_write_rejects_symlink(tmp_path) -> None:
    output = tmp_path / "grace-evidence.json"
    SCRIPT._atomic_private_write(output, b"{}\n")
    assert output.read_bytes() == b"{}\n"
    assert output.stat().st_mode & 0o777 == 0o600

    target = tmp_path / "target.json"
    target.write_text("unchanged", encoding="utf-8")
    output.unlink()
    output.symlink_to(target)
    with pytest.raises(SCRIPT.QualificationCommandError, match="output_identity"):
        SCRIPT._atomic_private_write(output, b"changed")
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_evidence_output_is_confined_to_female_named_hardening_json() -> None:
    expected = (ROOT / "hardening" / "grace-m8-local-qualification.json").resolve()
    assert SCRIPT._bounded_output(Path("hardening/grace-m8-local-qualification.json")) == expected
    with pytest.raises(SCRIPT.QualificationCommandError, match="output_scope"):
        SCRIPT._bounded_output(Path("grace-outside.json"))
    with pytest.raises(SCRIPT.QualificationCommandError, match="output_scope"):
        SCRIPT._bounded_output(Path("hardening/henry-report.json"))
    with pytest.raises(SCRIPT.QualificationCommandError, match="output_scope"):
        SCRIPT._bounded_output(Path("hardening/grace-report.txt"))


def test_source_fingerprint_is_content_only_and_stable() -> None:
    first = SCRIPT._source_digest()
    second = SCRIPT._source_digest()
    assert first == second
    assert first.startswith("sha256:")
    assert str(ROOT) not in first


def test_native_package_is_fully_covered_by_source_fingerprint() -> None:
    covered = set(SCRIPT.SOURCE_PATHS)
    native_files = {
        path.relative_to(ROOT).as_posix()
        for source_root in (ROOT / "native/austin/Sources", ROOT / "native/austin/Tests")
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix in {".c", ".h", ".swift"}
    }
    assert native_files <= covered


def test_operator_script_help_runs_from_outside_checkout(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "frozen M8 local qualification matrix" in completed.stdout


def test_generation_suite_defers_only_the_postwrite_evidence_gate(monkeypatch) -> None:
    captured: list[str] = []

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        captured.extend(command)
        return Completed()

    monkeypatch.setattr(SCRIPT.subprocess, "run", fake_run)
    assert SCRIPT._run_focused_tests() is True
    expression = captured[captured.index("-k") + 1]
    assert expression == f"not {SCRIPT.POSTWRITE_EVIDENCE_TEST}"
    assert SCRIPT.POSTWRITE_EVIDENCE_TEST == ("test_recorded_local_evidence_is_current_complete_and_honestly_blocked")


def test_recorded_local_evidence_is_current_complete_and_honestly_blocked() -> None:
    report = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    metrics = {row["id"]: row for row in report["metrics"]}
    local_ids = {
        "privileged_policy",
        "stale_target_race",
        "unknown_outcome_no_retry",
        "arbitrary_program_rejection",
        "privacy_canaries",
        "malformed_protocol_frames",
        "fresh_postcondition",
        "local_token_efficiency",
        "focused_adversarial_suite",
    }
    blocked_ids = {
        "managed_browser_completion",
        "selected_chrome_completion",
        "semantic_and_screenshot_efficiency",
        "browser_profile_network_boundary",
        "browser_security_freshness",
    }

    assert report["status"] == "blocked"
    assert report["public_claim_eligible"] is False
    assert report["source_digest"] == SCRIPT._source_digest()
    assert set(metrics) == local_ids | blocked_ids
    assert all(metrics[metric_id]["status"] == "pass" for metric_id in local_ids)
    assert all(metrics[metric_id]["status"] == "blocked" for metric_id in blocked_ids)
    assert metrics["stale_target_race"]["denominator"] == 10_000
    assert metrics["stale_target_race"]["measurements"]["mutations"] == 0
    assert metrics["malformed_protocol_frames"]["denominator"] == 100_000
    assert metrics["malformed_protocol_frames"]["measurements"]["unexpected_crashes"] == 0
    assert metrics["fresh_postcondition"]["denominator"] == 1_000
    assert report["summary"] == {"blocked": 5, "fail": 0, "not_verified": 0, "pass": 9}
    rendered = json.dumps(report, sort_keys=True)
    assert str(Path.home()) not in rendered
    assert 'public_claim_eligible": true' not in rendered
