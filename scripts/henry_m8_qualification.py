#!/usr/bin/env python3
"""Run the frozen M8 local qualification matrix and emit content-free evidence."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any

# The script is an operator-facing executable, not only an importable test
# module.  Resolve the checkout from this file before importing the package so
# `python /path/to/scripts/henry_m8_qualification.py` works from any cwd without
# relying on an ambient PYTHONPATH or an editable installation.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algo_cli.evals.tool_context_efficiency import (  # noqa: E402
    run_tool_context_efficiency_benchmark,
)
from algo_cli.henry_hardening_qualification import (  # noqa: E402
    MIN_POSTCONDITION_TRIALS,
    MIN_PRIVACY_CANARIES,
    MIN_PROGRAM_REJECTION_TRIALS,
    MIN_PROTOCOL_FRAMES,
    MIN_RACE_TRIALS,
    MIN_UNKNOWN_OUTCOME_TRIALS,
    build_qualification_report,
)


FUZZER = ROOT / "scripts" / "david_control_kernel_fuzzer.py"
SOURCE_PATHS = (
    ".github/workflows/henry-austin-signing-qualification.yml",
    ".github/workflows/henry-hardening-freeze.yml",
    ".github/workflows/oliver-ci.yml",
    ".github/workflows/oliver-release.yml",
    "hardening/henry-freeze.toml",
    "hardening/ada-signing-lifecycle-authorities.json",
    "algo_cli/david_control_kernel.py",
    "algo_cli/david_control_runtime.py",
    "algo_cli/ada_credential_registry.py",
    "algo_cli/ada_uninstall_recovery.py",
    "algo_cli/boron_browser_isolation.py",
    "algo_cli/neon_browser_simulator.py",
    "algo_cli/austin_desktop_simulator.py",
    "algo_cli/austin_install_finalizer.py",
    "algo_cli/austin_release_packager.py",
    "algo_cli/austin_thomas_binding.py",
    "algo_cli/arthur_control_doctor.py",
    "algo_cli/arthur_control_readiness.py",
    "algo_cli/agent_context.py",
    "algo_cli/agent_pipeline.py",
    "algo_cli/agent_run_journal.py",
    "algo_cli/agent_threads.py",
    "algo_cli/evals/nathan_agent_runtime_hardening.py",
    "algo_cli/main.py",
    "algo_cli/henry_hardening_qualification.py",
    "algo_cli/grace_key_store.py",
    "algo_cli/nathan_program_runtime.py",
    "algo_cli/nathan_provider_protocol.py",
    "algo_cli/nathan_runtime.py",
    "algo_cli/run_contract.py",
    "algo_cli/task_router.py",
    "algo_cli/oliver_control_installation.py",
    "algo_cli/oliver_control_installer.py",
    "algo_cli/oliver_authority_rotation.py",
    "algo_cli/irene_privacy_views.py",
    "algo_cli/marcus_authority.py",
    "algo_cli/samuel_policy_engine.py",
    "scripts/david_control_kernel_fuzzer.py",
    "scripts/arthur_m9_completion_audit.py",
    "scripts/david_hardening_gate.py",
    "scripts/boron_browser_build_images.py",
    "scripts/boron_browser_live_session.py",
    "scripts/henry_boron_hosted_qualification.py",
    "scripts/henry_m8_qualification.py",
    "scripts/henry_austin_ada_crash_qualification.py",
    "scripts/henry_austin_alice_crash_qualification.py",
    "scripts/henry_austin_lifecycle_authority_preflight.py",
    "scripts/henry_austin_prepare_public_key.py",
    "scripts/henry_austin_signing_provisioner.py",
    "scripts/henry_austin_signing_runner.py",
    "scripts/ada_austin_signing_lifecycle_receipt.py",
    "scripts/henry_github_hardening_readiness.py",
    "scripts/nathan_agent_runtime_qualification.py",
    "scripts/oliver_control_uninstall.py",
    "scripts/austin_native_package_audit.py",
    "scripts/austin_release_packager.py",
    "native/austin/Package.swift",
    "native/austin/Sources/AustinCore/AustinAdaPermitStore.swift",
    "native/austin/Sources/AustinCore/AustinAdaCredentialMigration.swift",
    "native/austin/Sources/AustinAdaCrashProbeMain/AustinAdaCrashProbe.swift",
    "native/austin/Sources/AustinApp/AustinApp.swift",
    "native/austin/Sources/AustinReadinessProbe/AustinReadinessProbe.swift",
    "native/austin/Sources/AustinCore/AustinSamuelAuthority.swift",
    "native/austin/Sources/AustinCore/AustinPeerIdentity.swift",
    "native/austin/Sources/AustinCore/AustinSession.swift",
    "native/austin/Sources/AustinCore/AustinWire.swift",
    "native/austin/Sources/AustinCore/AustinXPCProtocol.swift",
    "native/austin/Sources/AustinCredentialMigratorMain/AustinCredentialMigrator.swift",
    "native/austin/Sources/AustinDarwinBridge/AustinDarwinBridge.c",
    "native/austin/Sources/AustinDarwinBridge/include/AustinDarwinBridge.h",
    "native/austin/Sources/AustinRelay/AustinRelay.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinAccessibility.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinAliceCaptureArtifact.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinAppleEvent.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinCGEvent.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinConfirmation.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinDesktopDispatcher.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinIsaacCaptureBoundary.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinIsaacCaptureRedaction.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinReadiness.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinScreenCapture.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinShortcut.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinThomasBindingCoordinator.swift",
    "native/austin/Sources/AustinTCCAdapter/AustinThomasProductionControl.swift",
    "native/austin/Sources/AustinTCCAdapterMain/AustinTCCAdapter.swift",
    "native/austin/Sources/NeonNativeCore/NeonInvocation.swift",
    "native/austin/Sources/NeonNativeHostMain/NeonNativeHost.swift",
    "native/austin/Tests/AustinCoreTests/AustinAdaCredentialMigrationTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinAdaPermitStoreTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinAliceCaptureArtifactTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinAuthorityTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinConfirmationTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinDesktopRulesTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinIsaacCaptureBoundaryTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinIsaacCaptureRedactionTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinReadinessTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinSessionTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinThomasBindingCoordinatorTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinThomasProductionControlTests.swift",
    "native/austin/Tests/AustinCoreTests/AustinWireTests.swift",
    "native/austin/Tests/AustinIntegrationTests/AustinXPCIntegrationTests.swift",
    "native/austin/Tests/NeonNativeCoreTests/NeonInvocationTests.swift",
    "tests/test_henry_hardening_qualification.py",
    "tests/test_arthur_control_readiness.py",
)
FOCUSED_TESTS = (
    "tests/test_agent_context.py",
    "tests/test_agent_pipeline.py",
    "tests/test_agent_run_journal.py",
    "tests/test_agent_threads.py",
    "tests/test_main_helpers.py",
    "tests/test_nathan_agent_runtime_hardening.py",
    "tests/test_run_contract.py",
    "tests/test_task_router.py",
    "tests/test_marcus_authority.py",
    "tests/test_samuel_policy_engine.py",
    "tests/test_irene_privacy_views.py",
    "tests/test_dorothy_perf_telemetry.py",
    "tests/test_julia_memory_runtime.py",
    "tests/test_nathan_program_runtime.py",
    "tests/test_david_control_runtime.py",
    "tests/test_david_control_kernel.py",
    "tests/test_david_control_kernel_fuzzer.py",
    "tests/test_david_hardening_gate.py",
    "tests/test_arthur_m9_completion_audit.py",
    "tests/test_ada_control_journal.py",
    "tests/test_ada_credential_registry.py",
    "tests/test_ada_uninstall_recovery.py",
    "tests/test_grace_key_store.py",
    "tests/test_neon_browser_simulator.py",
    "tests/test_austin_desktop_simulator.py",
    "tests/test_austin_install_finalizer.py",
    "tests/test_austin_release_packager.py",
    "tests/test_henry_austin_ada_crash_qualification.py",
    "tests/test_henry_austin_alice_crash_qualification.py",
    "tests/test_henry_austin_lifecycle_authority_preflight.py",
    "tests/test_henry_austin_prepare_public_key.py",
    "tests/test_henry_austin_signing_provisioner.py",
    "tests/test_henry_austin_signing_runner.py",
    "tests/test_ada_austin_signing_lifecycle_receipt.py",
    "tests/test_henry_github_hardening_readiness.py",
    "tests/test_austin_native_package.py",
    "tests/test_austin_thomas_binding.py",
    "tests/test_austin_thomas_preparation.py",
    "tests/test_oliver_control_installation.py",
    "tests/test_oliver_control_installer.py",
    "tests/test_oliver_authority_rotation.py",
    "tests/test_boron_browser_isolation.py",
    "tests/test_boron_browser_images.py",
    "tests/test_henry_boron_hosted_qualification.py",
    "tests/test_xenon_browser_egress.py",
    "tests/test_carbon_browser_binding.py",
    "tests/test_neon_browser_native_host.py",
)
MAX_SOURCE_BYTES = 2 * 1024 * 1024
POSTWRITE_EVIDENCE_TEST = "test_recorded_local_evidence_is_current_complete_and_honestly_blocked"


class QualificationCommandError(RuntimeError):
    """A content-free qualification command failure."""


def _source_digest() -> str:
    digest = hashlib.sha256()
    for relative in sorted((*SOURCE_PATHS, *FOCUSED_TESTS)):
        path = ROOT / relative
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink():
            raise QualificationCommandError("source_identity")
        if info.st_size > MAX_SOURCE_BYTES:
            raise QualificationCommandError("source_bounds")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _run_protocol_frames(iterations: int) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(FUZZER), "--iterations", str(iterations)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=240,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        raise QualificationCommandError("protocol_fuzzer_failed")
    try:
        report = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        raise QualificationCommandError("protocol_fuzzer_output") from None
    if not isinstance(report, dict):
        raise QualificationCommandError("protocol_fuzzer_output")
    return report


def _run_focused_tests() -> bool:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-k",
            f"not {POSTWRITE_EVIDENCE_TEST}",
            *FOCUSED_TESTS,
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return completed.returncode == 0


def _run_efficiency(repeats: int) -> dict[str, Any]:
    capture = io.StringIO()
    with redirect_stdout(capture), redirect_stderr(capture):
        value = run_tool_context_efficiency_benchmark(repeats=repeats)
    if not isinstance(value, dict):
        raise QualificationCommandError("efficiency_output")
    return value


def _atomic_private_write(path: Path, payload: bytes) -> None:
    parent = path.parent.resolve()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink():
            raise QualificationCommandError("output_identity")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _bounded_output(path: Path) -> Path:
    candidate = (path if path.is_absolute() else ROOT / path).resolve(strict=False)
    evidence_root = (ROOT / "hardening").resolve()
    if (
        not candidate.is_relative_to(evidence_root)
        or candidate.suffix != ".json"
        or not candidate.name.startswith("grace-")
    ):
        raise QualificationCommandError("output_scope")
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--race-trials", type=int, default=MIN_RACE_TRIALS)
    parser.add_argument("--postcondition-trials", type=int, default=MIN_POSTCONDITION_TRIALS)
    parser.add_argument("--unknown-trials", type=int, default=MIN_UNKNOWN_OUTCOME_TRIALS)
    parser.add_argument("--program-trials", type=int, default=MIN_PROGRAM_REJECTION_TRIALS)
    parser.add_argument("--privacy-trials", type=int, default=MIN_PRIVACY_CANARIES)
    parser.add_argument("--protocol-frames", type=int, default=MIN_PROTOCOL_FRAMES)
    parser.add_argument("--efficiency-repeats", type=int, default=5)
    arguments = parser.parse_args(argv)

    protocol = _run_protocol_frames(arguments.protocol_frames)
    efficiency = _run_efficiency(arguments.efficiency_repeats)
    focused_suite_passed = _run_focused_tests()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    report = build_qualification_report(
        protocol_report=protocol,
        efficiency_report=efficiency,
        focused_suite_passed=focused_suite_passed,
        source_digest=_source_digest(),
        race_trials=arguments.race_trials,
        postcondition_trials=arguments.postcondition_trials,
        unknown_trials=arguments.unknown_trials,
        program_trials=arguments.program_trials,
        privacy_trials=arguments.privacy_trials,
        generated_at=generated_at,
    )
    payload = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if arguments.output is not None:
        _atomic_private_write(_bounded_output(arguments.output), payload)
    sys.stdout.buffer.write(payload)
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
