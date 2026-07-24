from __future__ import annotations

from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import stat
import sys

import pytest

from algo_cli.evals import nathan_agent_runtime_hardening as benchmark


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    ROOT / "scripts" / "nathan_agent_runtime_qualification.py"
)
SPEC = importlib.util.spec_from_file_location(
    "nathan_agent_runtime_qualification_script",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


@pytest.fixture(scope="module")
def report() -> dict[str, object]:
    return benchmark.run_benchmark(
        contract_repetitions=3,
        context_repetitions=3,
        checkpoint_repetitions=3,
        warmups=0,
        generated_at="2026-07-23T12:00:00Z",
    )


def test_benchmark_discovers_checkout_from_installed_module(
    tmp_path,
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    working_directory = checkout / "tests"
    working_directory.mkdir()
    required_paths = (
        "algo_cli/agent_context.py",
        "tests/test_agent_context.py",
    )
    for relative in required_paths:
        source = checkout / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# source\n", encoding="utf-8")

    installed_module = (
        tmp_path
        / "venv"
        / "site-packages"
        / "algo_cli"
        / "evals"
        / "nathan_agent_runtime_hardening.py"
    )
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("# installed\n", encoding="utf-8")

    discovered = benchmark._discover_source_root(
        required_paths,
        module_file=installed_module,
        cwd=working_directory,
    )

    assert discovered == checkout.resolve()


def test_runtime_benchmark_passes_every_source_bound_probe(
    report,
) -> None:
    benchmark.validate_report(
        report,
        require_current_source=True,
    )

    assert report["status"] == "pass"
    assert report["protocol"]["model_calls"] == 0
    assert report["protocol"]["network_calls"] == 0
    assert report["correctness"]["passed"] == len(
        benchmark.PROBES
    )
    assert report["correctness"]["pass_rate"] == 1.0
    assert all(
        row["p50_ms"] <= row["p95_ms"] <= row["max_ms"]
        for row in report["performance"].values()
    )


def test_runtime_benchmark_recomputes_claimed_gates(report) -> None:
    tampered = deepcopy(report)
    tampered["gates"]["correctness"]["observed"] = 0.0

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="gate is invalid",
    ):
        benchmark.validate_report(
            tampered,
            require_current_source=False,
        )


def test_runtime_benchmark_rejects_stale_source_digest(report) -> None:
    stale = deepcopy(report)
    stale["source_tree_sha256"] = "sha256:" + ("0" * 64)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="source digest is stale",
    ):
        benchmark.validate_report(
            stale,
            require_current_source=True,
        )


def test_qualification_artifact_round_trips_atomically(
    tmp_path,
    report,
) -> None:
    artifact = tmp_path / "nathan-agent-runtime-qualification.json"

    SCRIPT.write_artifact(
        artifact,
        report,
        allowed_root=tmp_path,
    )
    restored = SCRIPT.verify_artifact(
        artifact,
        allowed_root=tmp_path,
    )

    assert restored["report_sha256"] == report["report_sha256"]
    if os.name == "posix":
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def test_qualification_private_mode_falls_back_without_fchmod(
    tmp_path,
    report,
    monkeypatch,
) -> None:
    artifact = tmp_path / "portable-private-artifact.json"
    monkeypatch.delattr(SCRIPT.os, "fchmod", raising=False)

    SCRIPT.write_artifact(
        artifact,
        report,
        allowed_root=tmp_path,
    )

    assert SCRIPT.verify_artifact(
        artifact,
        allowed_root=tmp_path,
    )["report_sha256"] == report["report_sha256"]


def test_qualification_rejects_linked_artifact(
    tmp_path,
    report,
) -> None:
    target = tmp_path / "target.json"
    SCRIPT.write_artifact(
        target,
        report,
        allowed_root=tmp_path,
    )
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)

    with pytest.raises(
        SCRIPT.AgentRuntimeQualificationError,
        match="boundary rejected",
    ):
        SCRIPT.verify_artifact(
            linked,
            allowed_root=tmp_path,
        )
