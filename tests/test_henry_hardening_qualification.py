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
from algo_cli.evals import nathan_agent_runtime_hardening as nathan_runtime_benchmark
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


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="M8 qualification evidence is source-bound to POSIX execution",
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
    assert not any("freeze remains in force" in line for line in report["limitations"])
    assert any("outstanding external gates" in line for line in report["limitations"])
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


def test_generation_rejects_source_mutation_during_execution_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("source-bound payload\n", encoding="utf-8")
    output = tmp_path / "hardening" / "grace-m8-local-qualification.json"
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    monkeypatch.setattr(SCRIPT, "FOCUSED_TESTS", ())

    def mutate_source(_iterations: int) -> dict[str, object]:
        information = source.stat()
        source.chmod((information.st_mode & 0o777) ^ 0o100)
        return {}

    monkeypatch.setattr(SCRIPT, "_run_protocol_frames", mutate_source)
    monkeypatch.setattr(SCRIPT, "_run_efficiency", lambda _repeats: {})
    monkeypatch.setattr(SCRIPT, "_run_focused_tests", lambda: True)
    monkeypatch.setattr(SCRIPT, "build_qualification_report", lambda **_arguments: {"status": "blocked"})

    with pytest.raises(SCRIPT.QualificationCommandError, match="source_changed"):
        SCRIPT.main(["--output", str(output)])

    assert not output.exists()


def test_generation_rejects_source_mutation_in_report_builder_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("source-bound payload\n", encoding="utf-8")
    output = tmp_path / "hardening" / "grace-m8-local-qualification.json"
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    monkeypatch.setattr(SCRIPT, "FOCUSED_TESTS", ())
    monkeypatch.setattr(SCRIPT, "_run_protocol_frames", lambda _iterations: {})
    monkeypatch.setattr(SCRIPT, "_run_efficiency", lambda _repeats: {})
    monkeypatch.setattr(SCRIPT, "_run_focused_tests", lambda: True)

    def mutate_source(**_arguments: object) -> dict[str, object]:
        source.write_text("changed during report construction\n", encoding="utf-8")
        return {"status": "blocked"}

    monkeypatch.setattr(SCRIPT, "build_qualification_report", mutate_source)

    with pytest.raises(SCRIPT.QualificationCommandError, match="source_changed"):
        SCRIPT.main(["--output", str(output)])

    assert not output.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_source_fingerprint_rejects_links_and_special_files(
    tmp_path: Path,
    monkeypatch,
    kind: str,
) -> None:
    source = tmp_path / "source.py"
    target = tmp_path / "target.py"
    target.write_text("pass\n", encoding="utf-8")
    if kind == "symlink":
        source.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, source)
    else:
        os.mkfifo(source)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    monkeypatch.setattr(SCRIPT, "FOCUSED_TESTS", ())

    with pytest.raises(SCRIPT.QualificationCommandError, match="source_identity"):
        SCRIPT._source_digest()


def test_source_fingerprint_rejects_oversized_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"12345")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    monkeypatch.setattr(SCRIPT, "FOCUSED_TESTS", ())
    monkeypatch.setattr(SCRIPT, "MAX_SOURCE_BYTES", 4)

    with pytest.raises(SCRIPT.QualificationCommandError, match="source_bounds"):
        SCRIPT._source_digest()


def test_source_fingerprint_open_is_nonblocking_and_rejects_fifo_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    monkeypatch.setattr(SCRIPT, "FOCUSED_TESTS", ())
    real_open = SCRIPT.os.open
    swapped = False

    def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            assert flags & SCRIPT.os.O_NONBLOCK
            source.unlink()
            os.mkfifo(source)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(SCRIPT.os, "open", swap_before_open)

    with pytest.raises(SCRIPT.QualificationCommandError, match="source_changed"):
        SCRIPT._source_digest()
    assert swapped is True


@pytest.mark.parametrize("mutation", ["swap", "timestamp", "mode"])
def test_source_fingerprint_rejects_mutation_during_descriptor_read(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("bounded source\n", encoding="utf-8")
    source.chmod(0o644)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    monkeypatch.setattr(SCRIPT, "FOCUSED_TESTS", ())
    real_read = SCRIPT.os.read
    mutated = False

    def mutate_then_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        if not mutated:
            mutated = True
            if mutation == "swap":
                replacement = tmp_path / "replacement.py"
                replacement.write_bytes(source.read_bytes())
                os.replace(replacement, source)
            elif mutation == "timestamp":
                info = source.stat()
                os.utime(
                    source,
                    ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000),
                )
            else:
                source.chmod(0o600)
        return real_read(descriptor, size)

    monkeypatch.setattr(SCRIPT.os, "read", mutate_then_read)

    with pytest.raises(SCRIPT.QualificationCommandError, match="source_changed"):
        SCRIPT._source_digest()
    assert mutated is True


def test_source_fingerprint_closes_descriptor_when_read_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    monkeypatch.setattr(SCRIPT, "FOCUSED_TESTS", ())
    real_open = SCRIPT.os.open
    real_close = SCRIPT.os.close
    opened: list[int] = []
    closed: list[int] = []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode) if dir_fd is None else real_open(path, flags, mode, dir_fd=dir_fd)
        if Path(path) == source:
            opened.append(descriptor)
        return descriptor

    def fail_read(descriptor: int, _size: int) -> bytes:
        if descriptor in opened:
            raise OSError("simulated read failure")
        raise AssertionError("unexpected descriptor")

    def tracked_close(descriptor: int) -> None:
        if descriptor in opened:
            closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(SCRIPT.os, "open", tracked_open)
    monkeypatch.setattr(SCRIPT.os, "read", fail_read)
    monkeypatch.setattr(SCRIPT.os, "close", tracked_close)

    with pytest.raises(SCRIPT.QualificationCommandError, match="source_changed"):
        SCRIPT._source_digest()
    assert opened == closed


def test_source_fingerprint_rechecks_earlier_paths_after_complete_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("first.py", "second.py"))
    monkeypatch.setattr(SCRIPT, "FOCUSED_TESTS", ())
    real_read_source = SCRIPT._read_source_file

    def mutate_earlier_after_read(path: Path):
        result = real_read_source(path)
        if path == second:
            first.write_text("changed\n", encoding="utf-8")
        return result

    monkeypatch.setattr(SCRIPT, "_read_source_file", mutate_earlier_after_read)

    with pytest.raises(SCRIPT.QualificationCommandError, match="source_changed"):
        SCRIPT._source_digest()


def test_source_fingerprint_rejects_symlinked_directory_ancestor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "source.py").write_text("pass\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(SCRIPT, "ROOT", linked)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    monkeypatch.setattr(SCRIPT, "FOCUSED_TESTS", ())

    with pytest.raises(SCRIPT.QualificationCommandError, match="source_identity"):
        SCRIPT._source_digest()


def test_native_package_is_fully_covered_by_source_fingerprint() -> None:
    covered = set(SCRIPT.SOURCE_PATHS)
    native_files = {
        path.relative_to(ROOT).as_posix()
        for source_root in (ROOT / "native/austin/Sources", ROOT / "native/austin/Tests")
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix in {".c", ".h", ".swift"}
    }
    assert native_files <= covered


def test_hosted_browser_qualification_is_fully_source_bound() -> None:
    hosted_script_path = ROOT / "scripts" / "henry_boron_hosted_qualification.py"
    scripts_path = str(hosted_script_path.parent)
    module_name = "henry_boron_hosted_source_manifest_test"
    sys.path.insert(0, scripts_path)
    try:
        spec = importlib.util.spec_from_file_location(module_name, hosted_script_path)
        assert spec is not None and spec.loader is not None
        hosted = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = hosted
        spec.loader.exec_module(hosted)
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(scripts_path)

    covered = set(SCRIPT.SOURCE_PATHS) | set(SCRIPT.FOCUSED_TESTS)
    assert len(set(SCRIPT.SOURCE_PATHS)) == len(SCRIPT.SOURCE_PATHS)
    assert len(set(SCRIPT.FOCUSED_TESTS)) == len(SCRIPT.FOCUSED_TESTS)
    assert len(covered) == len(SCRIPT.SOURCE_PATHS) + len(SCRIPT.FOCUSED_TESTS)
    assert set(hosted.SOURCE_PATHS) <= covered
    assert "docs/boron-browser-isolation-contract.md" in covered
    assert {
        "algo_cli/resources/boron_browser/carbon_native_browser.Dockerfile",
        "algo_cli/resources/boron_browser/carbon_native_browser.Dockerfile.dockerignore",
    } <= set(SCRIPT.SOURCE_PATHS)


def test_echo_alice_pdf_privacy_controls_are_fully_qualified() -> None:
    covered = set(SCRIPT.SOURCE_PATHS) | set(SCRIPT.FOCUSED_TESTS)
    required_sources = {
        "pyproject.toml",
        "uv.lock",
        "algo_cli/action_registry.py",
        "algo_cli/ada_echo_veil_identity.py",
        "algo_cli/ada_memory_echo_veil.py",
        "algo_cli/ada_task_ledger.py",
        "algo_cli/agent_blocks.py",
        "algo_cli/agent_pipeline.py",
        "algo_cli/agent_run_journal.py",
        "algo_cli/agent_threads.py",
        "algo_cli/alice_artifact_store.py",
        "algo_cli/arthur_outcomes.py",
        "algo_cli/chatgpt_auth.py",
        "algo_cli/code_rag.py",
        "algo_cli/config.py",
        "algo_cli/context_budget.py",
        "algo_cli/deliberation.py",
        "algo_cli/display.py",
        "algo_cli/elsie_echo_preflight.py",
        "algo_cli/execution_guardrails.py",
        "algo_cli/google_workspace_auth.py",
        "algo_cli/grace_memory_receipts.py",
        "algo_cli/harness.py",
        "algo_cli/identity.py",
        "algo_cli/irene_memory_path_policy.py",
        "algo_cli/james_dispatch.py",
        "algo_cli/julia_memory_runtime.py",
        "algo_cli/julia_memory_candidates.py",
        "algo_cli/main.py",
        "algo_cli/reasoning/react.py",
        "algo_cli/run_contract.py",
        "algo_cli/session_commands.py",
        "algo_cli/skills.py",
        "algo_cli/small_context.py",
        "algo_cli/tool_context.py",
        "algo_cli/tools.py",
        "docs/ada-algo-cli-memory-lifecycle-contract.md",
        "docs/echo-veil-security-status.md",
        "docs/external-agent-store-operations.md",
        "docs/privacy-and-context.md",
        "scripts/henry_echo_veil_dependency_audit.py",
        "tests/conftest.py",
    }
    required_tests = {
        "tests/test_ada_echo_veil_identity.py",
        "tests/test_ada_memory_echo_veil.py",
        "tests/test_ada_task_ledger_echo.py",
        "tests/test_agent_pipeline.py",
        "tests/test_agent_run_journal.py",
        "tests/test_agent_threads.py",
        "tests/test_alice_artifact_store.py",
        "tests/test_chatgpt_auth.py",
        "tests/test_code_rag.py",
        "tests/test_config.py",
        "tests/test_context_accounting.py",
        "tests/test_display.py",
        "tests/test_elsie_echo_preflight.py",
        "tests/test_execution_guardrails.py",
        "tests/test_goal_mode.py",
        "tests/test_google_workspace_wiring.py",
        "tests/test_grace_memory_receipts.py",
        "tests/test_harness.py",
        "tests/test_henry_echo_veil_dependency_audit.py",
        "tests/test_identity.py",
        "tests/test_irene_memory_path_policy.py",
        "tests/test_james_dispatch.py",
        "tests/test_julia_curated_memory_contracts.py",
        "tests/test_julia_memory_candidates.py",
        "tests/test_julia_memory_runtime.py",
        "tests/test_main_helpers.py",
        "tests/test_pdf_render_artifacts.py",
        "tests/test_reasoning_bridge.py",
        "tests/test_run_contract.py",
        "tests/test_session_command_output.py",
        "tests/test_skills.py",
        "tests/test_small_context.py",
        "tests/test_tool_context.py",
        "tests/test_tools.py",
    }

    assert required_sources <= set(SCRIPT.SOURCE_PATHS)
    assert required_tests <= set(SCRIPT.FOCUSED_TESTS)
    assert required_sources | required_tests <= covered


def test_nathan_runtime_qualification_is_fully_source_bound() -> None:
    source_paths = set(SCRIPT.SOURCE_PATHS)
    focused_tests = set(SCRIPT.FOCUSED_TESTS)
    covered = source_paths | focused_tests

    assert set(nathan_runtime_benchmark.SOURCE_PATHS) <= covered
    assert {
        "algo_cli/chat_protocol.py",
        "algo_cli/git_evidence.py",
        "algo_cli/ada_private_event_store.py",
        "algo_cli/samuel_policy.py",
        "algo_cli/spawn_budget.py",
    } <= source_paths
    assert {
        "tests/test_git_evidence.py",
        "tests/test_ada_private_event_store.py",
        "tests/test_samuel_policy.py",
        "tests/test_spawn_budget.py",
    } <= focused_tests


def test_m8_execution_dependencies_are_source_bound_and_exercised() -> None:
    source_paths = set(SCRIPT.SOURCE_PATHS)
    focused_tests = set(SCRIPT.FOCUSED_TESTS)

    assert {
        "algo_cli/ada_control_journal.py",
        "algo_cli/chatgpt_client.py",
        "algo_cli/evals/tool_context_efficiency.py",
        "algo_cli/evelyn_context_supersession.py",
        "algo_cli/model_aliases.py",
        "algo_cli/model_info.py",
    } <= source_paths
    assert {
        "tests/test_ada_control_journal.py",
        "tests/test_chatgpt_client.py",
        "tests/test_evelyn_context_supersession.py",
        "tests/test_model_info.py",
        "tests/test_tool_context_efficiency.py",
    } <= focused_tests


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


def test_generation_suite_defers_only_postwrite_evidence_gates(monkeypatch) -> None:
    captured: list[str] = []

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        captured.extend(command)
        return Completed()

    monkeypatch.setattr(SCRIPT.subprocess, "run", fake_run)
    assert SCRIPT._run_focused_tests() is True
    expression = captured[captured.index("-k") + 1]
    assert expression == " and ".join(f"not {name}" for name in SCRIPT.POSTWRITE_EVIDENCE_TESTS)
    assert SCRIPT.POSTWRITE_EVIDENCE_TESTS == (
        "test_recorded_local_evidence_is_current_complete_and_honestly_blocked",
        "test_repository_report_is_exact_and_freeze_workflow_enforces_it",
    )


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
