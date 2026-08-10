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
    return benchmark.run_benchmark(
        contract_repetitions=3,
        context_repetitions=3,
        checkpoint_repetitions=3,
        workload_repetitions=3,
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
            contract_repetitions=3,
            context_repetitions=3,
            checkpoint_repetitions=3,
            workload_repetitions=3,
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
