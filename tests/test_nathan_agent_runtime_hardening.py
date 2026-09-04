from __future__ import annotations

from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

from algo_cli.evals import nathan_agent_runtime_hardening as benchmark


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "nathan_agent_runtime_qualification.py"
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
    # Tamper tests exercise the report contract, not the currency of the
    # repository evidence file. Build one small, valid report in memory so a
    # stale stored artifact cannot turn every contract test into a setup error.
    return benchmark.run_benchmark(
        contract_repetitions=benchmark.MIN_LATENCY_SAMPLES,
        context_repetitions=benchmark.MIN_LATENCY_SAMPLES,
        checkpoint_repetitions=benchmark.MIN_LATENCY_SAMPLES,
        workload_repetitions=benchmark.MIN_LATENCY_SAMPLES,
        warmups=0,
        generated_at="2026-08-23T00:00:00Z",
    )


def test_stored_runtime_qualification_artifact_is_current() -> None:
    stored = SCRIPT.verify_artifact()

    assert stored["status"] == "pass"


def _resign_report(report: dict[str, object]) -> None:
    unsigned = dict(report)
    unsigned.pop("report_sha256", None)
    report["report_sha256"] = benchmark._digest(unsigned)


def test_windows_latency_profile_changes_only_checkpoint_durability() -> None:
    expected_baseline = {
        "contract_compile": 250.0,
        "context_broker": 100.0,
        "checkpoint_resume": 1_000.0,
        "agent_workload_ttfa": 1_000.0,
        "agent_workload_total": 2_500.0,
    }
    baseline = benchmark._thresholds_for_operating_system("Linux-6.11-x86_64")
    windows = benchmark._thresholds_for_operating_system("Windows-11-10.0.26100-SP0")

    assert benchmark.LATENCY_THRESHOLDS_MS == expected_baseline
    assert baseline == expected_baseline
    assert {metric for metric in baseline if windows[metric] != baseline[metric]} == {
        "checkpoint_resume",
        "agent_workload_total",
    }
    assert windows["checkpoint_resume"] == 2_500.0
    assert windows["agent_workload_total"] == 3_500.0


def test_report_validation_uses_stored_windows_profile_not_current_host(
    report,
    monkeypatch,
) -> None:
    windows = deepcopy(report)
    windows["environment"]["operating_system"] = "Windows-11-10.0.26100-SP0"
    checkpoint_performance = windows["performance"]["checkpoint_resume"]
    checkpoint_performance.update(
        {
            "p50_ms": 910.0,
            "p95_ms": 1_832.6466,
            "max_ms": 1_975.0,
        }
    )
    checkpoint_gate = windows["gates"]["checkpoint_resume_p95_ms"]
    checkpoint_gate["threshold"] = benchmark.WINDOWS_CHECKPOINT_RESUME_THRESHOLD_MS
    checkpoint_gate["observed"] = checkpoint_performance["p95_ms"]
    checkpoint_gate["passed"] = True
    workload_totals = ([900.0] * (len(windows["effectiveness"]["workloads"]) - 2)) + [
        2_760.6905,
        3_000.0,
    ]
    workload_ttfas = [100.0] * len(workload_totals)
    for workload, ttfa_ms, total_ms in zip(
        windows["effectiveness"]["workloads"],
        workload_ttfas,
        workload_totals,
        strict=True,
    ):
        workload["ttfa_ms"] = ttfa_ms
        workload["total_ms"] = total_ms
    ttfa_performance = benchmark._latency_summary(workload_ttfas)
    windows["performance"]["agent_workload_ttfa"] = ttfa_performance
    ttfa_gate = windows["gates"]["agent_workload_ttfa_p95_ms"]
    ttfa_gate["observed"] = ttfa_performance["p95_ms"]
    ttfa_gate["passed"] = True
    workload_performance = benchmark._latency_summary(workload_totals)
    windows["performance"]["agent_workload_total"] = workload_performance
    workload_gate = windows["gates"]["agent_workload_total_p95_ms"]
    workload_gate["threshold"] = benchmark.WINDOWS_AGENT_WORKLOAD_TOTAL_THRESHOLD_MS
    workload_gate["observed"] = workload_performance["p95_ms"]
    workload_gate["passed"] = True
    windows["status"] = "pass" if all(gate["passed"] is True for gate in windows["gates"].values()) else "fail"
    windows["claim"] = benchmark.BENCHMARK_PASS_CLAIM
    _resign_report(windows)

    def forbid_current_host_access() -> str:
        raise AssertionError("report validation consulted the current host")

    monkeypatch.setattr(benchmark.platform, "platform", forbid_current_host_access)
    monkeypatch.setattr(benchmark.platform, "system", forbid_current_host_access)
    monkeypatch.setattr(benchmark.platform, "machine", forbid_current_host_access)
    monkeypatch.setattr(
        benchmark.platform,
        "python_version",
        forbid_current_host_access,
    )

    benchmark.validate_report(
        windows,
        require_current_source=True,
    )
    assert checkpoint_gate["threshold"] == 2_500.0
    assert checkpoint_gate["observed"] == 1_832.6466
    assert workload_gate["threshold"] == 3_500.0
    assert workload_gate["observed"] == 2_760.6905


@pytest.mark.parametrize(
    ("metric", "forged_threshold"),
    [
        ("checkpoint_resume", 3_000.0),
        ("agent_workload_total", 4_000.0),
    ],
)
def test_runtime_benchmark_rejects_resigned_windows_threshold_tampering(
    report,
    metric: str,
    forged_threshold: float,
) -> None:
    tampered = deepcopy(report)
    tampered["environment"]["operating_system"] = "Windows-11-10.0.26100-SP0"
    windows_thresholds = benchmark._thresholds_for_environment(tampered["environment"])
    for gate_metric, threshold in windows_thresholds.items():
        gate = tampered["gates"][f"{gate_metric}_p95_ms"]
        gate["threshold"] = threshold
        gate["passed"] = gate["observed"] <= threshold
    tampered["gates"][f"{metric}_p95_ms"]["threshold"] = forged_threshold
    _resign_report(tampered)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match=rf"runtime benchmark gate is invalid: {metric}_p95_ms",
    ):
        benchmark.validate_report(
            tampered,
            require_current_source=False,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("operating_system", "Plan9-1.0"),
        ("operating_system", "Windows-11\nforged"),
        ("machine", ""),
        ("machine", "arm64\u2603"),
        ("python", 312),
    ],
)
def test_runtime_benchmark_rejects_malformed_environment(
    report,
    field: str,
    replacement: object,
) -> None:
    tampered = deepcopy(report)
    tampered["environment"][field] = replacement

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark environment is invalid",
    ):
        benchmark.validate_report(
            tampered,
            require_current_source=False,
        )


def test_runtime_benchmark_rejects_environment_extension(report) -> None:
    tampered = deepcopy(report)
    tampered["environment"]["latency_profile"] = "forged"

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark environment is invalid",
    ):
        benchmark.validate_report(
            tampered,
            require_current_source=False,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("latency_profile", "forged"),
        ("clock", "forged"),
        ("warmups", "forged"),
        ("warmups", -1),
        ("warmups", 101),
        ("model_calls", False),
        ("model_calls", 0.0),
        ("network_calls", False),
        ("network_calls", 0.0),
    ],
)
def test_runtime_benchmark_rejects_resigned_protocol_tampering(
    report,
    field: str,
    replacement: object,
) -> None:
    tampered = deepcopy(report)
    tampered["protocol"][field] = replacement
    _resign_report(tampered)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark protocol is invalid",
    ):
        benchmark.validate_report(
            tampered,
            require_current_source=False,
        )


@pytest.mark.parametrize("field", ["claim", "limitations"])
@pytest.mark.parametrize("replacement", [None, "", "forged"])
def test_runtime_benchmark_rejects_resigned_narrative_tampering(
    report,
    field: str,
    replacement: object,
) -> None:
    tampered = deepcopy(report)
    tampered[field] = replacement
    _resign_report(tampered)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark narrative is invalid",
    ):
        benchmark.validate_report(
            tampered,
            require_current_source=False,
        )


def test_runtime_benchmark_rejects_affirmative_claim_on_failed_report(report) -> None:
    failed = deepcopy(report)
    failed_probe = failed["correctness"]["probes"][0]
    failed_probe.update(
        {
            "passed": False,
            "failure_code": "AgentRuntimeBenchmarkError",
        }
    )
    failed["correctness"]["passed"] -= 1
    failed["correctness"]["pass_rate"] = failed["correctness"]["passed"] / failed["correctness"]["total"]
    correctness_gate = failed["gates"]["correctness"]
    correctness_gate["observed"] = failed["correctness"]["pass_rate"]
    correctness_gate["passed"] = False
    failed["status"] = "fail"
    failed["claim"] = benchmark.BENCHMARK_PASS_CLAIM
    _resign_report(failed)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark narrative is invalid",
    ):
        benchmark.validate_report(failed, require_current_source=False)

    failed["claim"] = benchmark.BENCHMARK_FAIL_CLAIM
    _resign_report(failed)
    benchmark.validate_report(failed, require_current_source=False)


@pytest.mark.parametrize(
    ("passed", "failure_code"),
    [
        (True, "ValueError"),
        (True, "bad\ncode"),
        (False, ""),
        (False, "bad\ncode"),
    ],
)
def test_runtime_benchmark_rejects_resigned_probe_failure_code_tampering(
    report,
    passed: bool,
    failure_code: str,
) -> None:
    tampered = deepcopy(report)
    tampered["correctness"]["probes"][0].update(
        {
            "passed": passed,
            "failure_code": failure_code,
        }
    )
    _resign_report(tampered)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark probes are invalid",
    ):
        benchmark.validate_report(
            tampered,
            require_current_source=False,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", 3.0),
        ("source_revision", 1234567),
    ],
)
def test_runtime_benchmark_rejects_resigned_identity_type_tampering(
    report,
    field: str,
    replacement: object,
) -> None:
    tampered = deepcopy(report)
    tampered[field] = replacement
    _resign_report(tampered)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark report identity is invalid",
    ):
        benchmark.validate_report(tampered, require_current_source=False)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("extension", "forged"),
        ("passed", 17.0),
        ("total", 17.0),
        ("pass_rate", True),
    ],
)
def test_runtime_benchmark_rejects_resigned_correctness_type_tampering(
    report,
    field: str,
    replacement: object,
) -> None:
    tampered = deepcopy(report)
    tampered["correctness"][field] = replacement
    _resign_report(tampered)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark correctness is invalid",
    ):
        benchmark.validate_report(tampered, require_current_source=False)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("runs", 31.0),
        ("task_pass_rate", True),
        ("policy_escapes", False),
    ],
)
def test_runtime_benchmark_rejects_resigned_effectiveness_type_tampering(
    report,
    field: str,
    replacement: object,
) -> None:
    tampered = deepcopy(report)
    tampered["effectiveness"][field] = replacement
    _resign_report(tampered)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark effectiveness fields are invalid",
    ):
        benchmark.validate_report(tampered, require_current_source=False)


@pytest.mark.parametrize(
    ("gate", "field", "replacement"),
    [
        ("correctness", "threshold", True),
        ("policy_escapes", "threshold", False),
        ("policy_escapes", "observed", False),
    ],
)
def test_runtime_benchmark_rejects_resigned_gate_type_tampering(
    report,
    gate: str,
    field: str,
    replacement: object,
) -> None:
    tampered = deepcopy(report)
    tampered["gates"][gate][field] = replacement
    _resign_report(tampered)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match=rf"runtime benchmark gate is invalid: {gate}",
    ):
        benchmark.validate_report(tampered, require_current_source=False)


def test_minimum_latency_corpus_excludes_one_outlier_from_p95() -> None:
    values = ([1.0] * (benchmark.MIN_LATENCY_SAMPLES - 2)) + [5.0, 10.0]

    assert benchmark._latency_summary(values) == {
        "samples": benchmark.MIN_LATENCY_SAMPLES,
        "p50_ms": 1.0,
        "p95_ms": 5.0,
        "max_ms": 10.0,
    }


def test_checkpoint_and_workload_paths_complete_without_latency_gate(tmp_path) -> None:
    benchmark._checkpoint_cycle(tmp_path, index=1)
    workload = benchmark._frozen_agent_workload(tmp_path, index=2)

    assert {
        field: workload[field]
        for field in (
            "task_passed",
            "verifier_passed",
            "verifier_total",
            "policy_escapes",
            "unverified_completions",
            "duplicate_mutations",
            "crash_resume_passed",
            "protocol_correct",
            "context_useful",
        )
    } == {
        "task_passed": True,
        "verifier_passed": 2,
        "verifier_total": 2,
        "policy_escapes": 0,
        "unverified_completions": 0,
        "duplicate_mutations": 0,
        "crash_resume_passed": True,
        "protocol_correct": True,
        "context_useful": True,
    }
    assert type(workload["ttfa_ms"]) is float and workload["ttfa_ms"] >= 0.0
    assert type(workload["total_ms"]) is float and workload["total_ms"] >= workload["ttfa_ms"]


def test_every_correctness_probe_passes_live_without_latency_gate(tmp_path) -> None:
    probes = benchmark._run_probes(tmp_path)

    assert [row["id"] for row in probes] == [probe_id for probe_id, _operation in benchmark.PROBES]
    assert [row for row in probes if row["passed"] is not True] == []


@pytest.mark.parametrize(
    "field",
    [
        "contract_repetitions",
        "context_repetitions",
        "checkpoint_repetitions",
        "workload_repetitions",
    ],
)
@pytest.mark.parametrize(
    "replacement",
    [benchmark.MIN_LATENCY_SAMPLES - 1, 10_001],
)
def test_runtime_benchmark_rejects_invalid_latency_corpus(
    field: str,
    replacement: int,
) -> None:
    repetitions = {
        "contract_repetitions": benchmark.MIN_LATENCY_SAMPLES,
        "context_repetitions": benchmark.MIN_LATENCY_SAMPLES,
        "checkpoint_repetitions": benchmark.MIN_LATENCY_SAMPLES,
        "workload_repetitions": benchmark.MIN_LATENCY_SAMPLES,
    }
    repetitions[field] = replacement

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match=rf"{field} must be an integer from {benchmark.MIN_LATENCY_SAMPLES} to 10000",
    ):
        benchmark.run_benchmark(**repetitions)


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

    installed_module = tmp_path / "venv" / "site-packages" / "algo_cli" / "evals" / "nathan_agent_runtime_hardening.py"
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("# installed\n", encoding="utf-8")

    discovered = benchmark._discover_source_root(
        required_paths,
        module_file=installed_module,
        cwd=working_directory,
    )

    assert discovered == checkout.resolve()


def test_runtime_benchmark_source_manifest_covers_protected_execution_boundary() -> None:
    required = {
        "algo_cli/ada_memory_echo_veil.py",
        "algo_cli/agent_blocks.py",
        "algo_cli/agent_pipeline.py",
        "algo_cli/agent_run_journal.py",
        "algo_cli/agent_threads.py",
        "algo_cli/chat_protocol.py",
        "algo_cli/config.py",
        "algo_cli/context_budget.py",
        "algo_cli/elsie_echo_preflight.py",
        "algo_cli/git_evidence.py",
        "algo_cli/grace_key_store.py",
        "algo_cli/grace_memory_receipts.py",
        "algo_cli/irene_privacy_views.py",
        "algo_cli/ada_private_event_store.py",
        "algo_cli/run_contract.py",
        "tests/test_ada_memory_echo_veil.py",
        "tests/test_agent_run_journal.py",
        "tests/test_agent_threads.py",
        "tests/test_elsie_echo_preflight.py",
        "tests/test_grace_key_store.py",
        "tests/test_grace_memory_receipts.py",
        "tests/test_run_contract.py",
    }

    assert required <= set(benchmark.SOURCE_PATHS)
    assert len(benchmark.SOURCE_PATHS) == len(set(benchmark.SOURCE_PATHS))
    assert all((ROOT / relative).is_file() for relative in benchmark.SOURCE_PATHS)


def test_source_reader_rejects_leaf_swap_before_descriptor_open(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("original\n", encoding="utf-8")
    replacement = tmp_path / "replacement.py"
    replacement.write_text("forged!!\n", encoding="utf-8")
    original_open = benchmark.os.open
    swapped = False

    def swap_then_open(path, flags, *args):
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            source.rename(tmp_path / "original.py")
            replacement.rename(source)
        return original_open(path, flags, *args)

    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark.os, "open", swap_then_open)

    with pytest.raises(benchmark.AgentRuntimeBenchmarkError, match="changed while opening"):
        benchmark._read_source_payload("source.py")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support is unavailable")
def test_source_reader_rejects_fifo_swap_without_blocking(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("original\n", encoding="utf-8")
    original_open = benchmark.os.open
    observed_flags: list[int] = []
    swapped = False

    def swap_to_fifo_then_open(path, flags, *args):
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            source.unlink()
            os.mkfifo(source)
            observed_flags.append(flags)
        return original_open(path, flags, *args)

    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark.os, "open", swap_to_fifo_then_open)

    with pytest.raises(benchmark.AgentRuntimeBenchmarkError, match="changed while opening"):
        benchmark._read_source_payload("source.py")

    assert swapped is True
    assert len(observed_flags) == 1
    assert observed_flags[0] & getattr(os, "O_NONBLOCK", 0)


def test_windows_source_identity_normalizes_path_and_crt_metadata(monkeypatch) -> None:
    common = {
        "st_dev": 7,
        "st_ino": 11,
        "st_nlink": 1,
        "st_size": 17,
        "st_mtime_ns": 23,
        "st_file_attributes": 0x20,
    }
    path_view = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_ctime_ns=29, **common)
    handle_view = SimpleNamespace(st_mode=stat.S_IFREG | 0o666, st_ctime_ns=31, **common)
    changed = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o666,
        st_ctime_ns=31,
        **(common | {"st_file_attributes": 1}),
    )
    reparse = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o666,
        st_ctime_ns=31,
        **(common | {"st_file_attributes": 0x400}),
    )
    monkeypatch.setattr(benchmark.os, "name", "nt")

    assert benchmark._source_identity(path_view) == benchmark._source_identity(handle_view)
    assert benchmark._source_identity(path_view) != benchmark._source_identity(changed)
    assert benchmark._source_is_reparse(path_view) is False
    assert benchmark._source_is_reparse(reparse) is True


def test_source_reader_requests_binary_descriptor_mode(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"source-bound payload\n")
    original_open = benchmark.os.open
    binary_flag = 1 << 28
    observed: list[int] = []

    def capture_open(path, flags, *args):
        observed.append(flags)
        return original_open(path, flags & ~binary_flag, *args)

    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark.os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(benchmark.os, "open", capture_open)

    assert benchmark._read_source_payload("source.py") == b"source-bound payload\n"
    assert len(observed) == 1
    assert observed[0] & binary_flag


@pytest.mark.parametrize("mutation", ["mode", "timestamp"])
def test_source_reader_rejects_metadata_change_during_descriptor_read(
    tmp_path,
    monkeypatch,
    mutation,
) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"source-bound payload\n")
    original_read = benchmark.os.read
    mutated = False
    original_mode = source.stat().st_mode

    def mutate_after_read(descriptor, size):
        nonlocal mutated
        chunk = original_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            information = source.stat()
            if mutation == "mode":
                if os.name == "nt":
                    source.chmod(stat.S_IREAD)
                else:
                    source.chmod((information.st_mode & 0o777) ^ 0o100)
            else:
                benchmark.os.utime(
                    source,
                    ns=(information.st_atime_ns, information.st_mtime_ns + 1_000_000_000),
                )
        return chunk

    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark.os, "read", mutate_after_read)

    try:
        with pytest.raises(benchmark.AgentRuntimeBenchmarkError, match="changed while reading"):
            benchmark._read_source_payload("source.py")
    finally:
        if os.name == "nt":
            source.chmod(original_mode)


def test_source_tree_digest_rechecks_earlier_paths_after_full_read(
    tmp_path,
    monkeypatch,
) -> None:
    first = tmp_path / "first.py"
    first.write_text("first\n", encoding="utf-8")
    second = tmp_path / "second.py"
    second.write_text("second\n", encoding="utf-8")
    original_read = benchmark._read_source_payload_with_identity

    def mutate_first_after_reading_second(relative):
        payload, identity = original_read(relative)
        if relative == "second.py":
            information = first.stat()
            os.utime(
                first,
                ns=(information.st_atime_ns, information.st_mtime_ns + 1_000_000_000),
            )
        return payload, identity

    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "SOURCE_PATHS", ("first.py", "second.py"))
    monkeypatch.setattr(
        benchmark,
        "_read_source_payload_with_identity",
        mutate_first_after_reading_second,
    )

    with pytest.raises(benchmark.AgentRuntimeBenchmarkError, match="changed after reading: first.py"):
        benchmark.source_tree_digest()


def test_source_snapshot_rejects_mutation_across_qualification(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("source-bound payload\n", encoding="utf-8")
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "SOURCE_PATHS", ("source.py",))
    _digest, snapshot = benchmark._capture_source_tree()
    information = source.stat()
    os.utime(
        source,
        ns=(information.st_atime_ns, information.st_mtime_ns + 1_000_000_000),
    )

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="changed after reading",
    ):
        benchmark._verify_source_tree_snapshot(snapshot)


def test_benchmark_rejects_source_mutation_during_execution(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("source-bound payload\n", encoding="utf-8")
    original_run_probes = benchmark._run_probes

    def run_then_mutate(root):
        rows = original_run_probes(root)
        information = source.stat()
        os.utime(
            source,
            ns=(information.st_atime_ns, information.st_mtime_ns + 1_000_000_000),
        )
        return rows

    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "SOURCE_PATHS", ("source.py",))
    monkeypatch.setattr(benchmark, "_run_probes", run_then_mutate)

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="changed after reading",
    ):
        benchmark.run_benchmark(
            contract_repetitions=benchmark.MIN_LATENCY_SAMPLES,
            context_repetitions=benchmark.MIN_LATENCY_SAMPLES,
            checkpoint_repetitions=benchmark.MIN_LATENCY_SAMPLES,
            workload_repetitions=benchmark.MIN_LATENCY_SAMPLES,
            warmups=0,
        )


def test_protected_journal_probe_rejects_crash_window_plaintext_leak(
    tmp_path,
    monkeypatch,
) -> None:
    original = benchmark.agent_run_journal.AgentRunJournal.tool_intent

    def leaking_tool_intent(self, **kwargs):
        try:
            return original(self, **kwargs)
        finally:
            with self.store.path.open("ab") as handle:
                handle.write(b"NATHAN_PROTECTED_INTENT_CANARY")

    monkeypatch.setattr(
        benchmark.agent_run_journal.AgentRunJournal,
        "tool_intent",
        leaking_tool_intent,
    )

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="persisted plaintext or an unkeyed hash",
    ):
        benchmark._probe_protected_contract_journal_receipts(tmp_path)


def test_runtime_benchmark_passes_every_source_bound_probe(
    report,
) -> None:
    benchmark.validate_report(
        report,
        require_current_source=True,
    )

    failures = {
        "probes": [row["id"] for row in report["correctness"]["probes"] if row["passed"] is not True],
        "gates": {
            name: {
                "observed": gate["observed"],
                "threshold": gate["threshold"],
            }
            for name, gate in report["gates"].items()
            if gate["passed"] is not True
        },
        "performance": report["performance"],
    }
    assert report["status"] == "pass", failures
    assert report["schema_version"] == 3
    assert report["benchmark"] == "nathan-agent-runtime-hardening-v3"
    assert report["public_claim_eligible"] is False
    assert report["protocol"]["model_calls"] == 0
    assert report["protocol"]["network_calls"] == 0
    assert report["correctness"]["passed"] == len(benchmark.PROBES)
    assert len(benchmark.PROBES) == 17
    assert {
        "protected_contract_journal_recovery",
        "protected_thread_projection_recovery",
        "echo_agent_preflight_refusal",
    } <= {row["id"] for row in report["correctness"]["probes"]}
    assert report["correctness"]["pass_rate"] == 1.0
    assert report["effectiveness"]["task_pass_rate"] == 1.0
    assert report["effectiveness"]["verifier_pass_rate"] == 1.0
    assert report["effectiveness"]["policy_escapes"] == 0
    assert report["effectiveness"]["unverified_completions"] == 0
    assert report["effectiveness"]["duplicate_mutations"] == 0
    assert report["effectiveness"]["crash_resume_rate"] == 1.0
    assert report["effectiveness"]["protocol_correctness_rate"] == 1.0
    assert report["effectiveness"]["context_usefulness_rate"] == 1.0
    assert all(row["p50_ms"] <= row["p95_ms"] <= row["max_ms"] for row in report["performance"].values())


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


def test_runtime_benchmark_rejects_latency_sample_count_mismatch(report) -> None:
    tampered = deepcopy(report)
    tampered["performance"]["contract_compile"]["samples"] = benchmark.MIN_LATENCY_SAMPLES - 1

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark latency is invalid: contract_compile",
    ):
        benchmark.validate_report(
            tampered,
            require_current_source=False,
        )


def test_runtime_benchmark_rejects_underpowered_stored_protocol(report) -> None:
    tampered = deepcopy(report)
    tampered["protocol"]["contract_repetitions"] = benchmark.MIN_LATENCY_SAMPLES - 1
    tampered["performance"]["contract_compile"]["samples"] = benchmark.MIN_LATENCY_SAMPLES - 1

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="runtime benchmark protocol is invalid",
    ):
        benchmark.validate_report(
            tampered,
            require_current_source=False,
        )


@pytest.mark.parametrize("replacement", [True, None])
def test_runtime_benchmark_rejects_public_claim_eligibility_tampering(
    report,
    replacement,
) -> None:
    tampered = deepcopy(report)
    if replacement is None:
        del tampered["public_claim_eligible"]
    else:
        tampered["public_claim_eligible"] = replacement

    with pytest.raises(
        benchmark.AgentRuntimeBenchmarkError,
        match="(?:fields do not match schema|identity is invalid)",
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
    assert restored["public_claim_eligible"] is False
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

    assert (
        SCRIPT.verify_artifact(
            artifact,
            allowed_root=tmp_path,
        )["report_sha256"]
        == report["report_sha256"]
    )


def test_qualification_receipt_retains_public_claim_limit(tmp_path, report) -> None:
    artifact = tmp_path / "nathan-agent-runtime-qualification.json"

    receipt = SCRIPT._receipt(report, artifact=artifact)

    assert receipt["status"] == "pass"
    assert receipt["public_claim_eligible"] is False


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
