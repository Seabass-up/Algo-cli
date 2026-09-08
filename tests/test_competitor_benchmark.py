from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "benchmarks/competitors/runner.py"
SPEC = importlib.util.spec_from_file_location("competitor_benchmark_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

PUBLISHER_PATH = ROOT / "benchmarks/competitors/publish_website.py"
PUBLISHER_SPEC = importlib.util.spec_from_file_location("competitor_benchmark_publisher", PUBLISHER_PATH)
assert PUBLISHER_SPEC and PUBLISHER_SPEC.loader
publisher = importlib.util.module_from_spec(PUBLISHER_SPEC)
sys.modules[PUBLISHER_SPEC.name] = publisher
PUBLISHER_SPEC.loader.exec_module(publisher)


def fixture_copy(tmp_path: Path, task_id: str) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    shutil.copytree(runner.TASK_ROOT / task_id / "fixtures", workspace)
    artifacts.mkdir()
    return workspace, artifacts


def test_every_measured_product_has_an_adapter() -> None:
    measured = {product_id for product_id, spec in runner.PRODUCTS.items() if spec.adapter}
    assert measured == {
        "algo_cli",
        "codex_cli",
        "claude_code",
        "opencode",
        "pi",
        "copilot_cli",
        "droid",
        "goose",
        "oh_my_pi",
        "hermes_agent",
        "openclaw",
        "grok_build",
    }


def test_algo_final_answer_uses_content_deltas_after_the_last_tool(tmp_path):
    events = [
        {"type": "content", "text": "Preliminary commentary."},
        {"type": "tool_call", "call_id": "read-1", "name": "read_file"},
        {"type": "tool_result", "call_id": "read-1", "status": "ok", "summary": "Untrusted tool text"},
        {"type": "thinking", "text": "Not an answer."},
        {"type": "content", "text": "Verified "},
        {"type": "content", "text": "result."},
        {"type": "done", "status": "complete", "usage": {"total_tokens": 12}},
    ]
    result = runner.event_metrics("algo_cli", events, tmp_path)
    assert result["final_text"] == "Verified result."
    assert result["tokens"] == 12
    assert result["tool_calls"] == 1


def test_rotating_order_is_deterministic_and_complete() -> None:
    harnesses = ["algo_cli", "codex_cli", "pi"]
    tasks = ["code_repair_small_repo", "tool_trap_misleading_state"]

    first = runner.rotating_order(harnesses, tasks, 3)
    second = runner.rotating_order(harnesses, tasks, 3)

    assert first == second
    assert len(first) == 18
    for task in tasks:
        for harness in harnesses:
            assert sum(row[1:] == (task, harness) for row in first) == 3


def test_model_warmup_is_receipted_and_excluded_from_scores(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "resolve_executable", lambda _candidates: "/usr/bin/ollama")
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "WARM\n", ""),
    )

    receipt = runner.warm_model(tmp_path, "test-model", 30, "2h")

    assert receipt["success"] is True
    assert receipt["included_in_scored_duration"] is False
    assert receipt["keepalive"] == "2h"
    assert json.loads((tmp_path / "warmup_receipt.json").read_text())["success"] is True
    assert (tmp_path / "warmup_stdout.txt").read_text() == "WARM\n"


def test_code_repair_checker_fails_then_passes(tmp_path: Path) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "code_repair_small_repo")
    checked = runner.run_task_checker("code_repair_small_repo", workspace, artifacts)
    passed, _receipt = checked
    assert passed is False
    assert checked.completed

    source = workspace / "src/calculator.py"
    source.write_text(source.read_text().replace(" // ", " / "), encoding="utf-8")

    checked = runner.run_task_checker("code_repair_small_repo", workspace, artifacts)
    passed, receipt = checked
    assert passed is True
    assert checked.completed
    assert "PASS code_repair_small_repo" in receipt


@pytest.mark.parametrize("failure_mode", ["import_exit", "partial_exit", "forged_stdout"])
def test_code_checker_requires_completed_tests_not_only_exit_zero(tmp_path: Path, failure_mode: str) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "code_repair_small_repo")
    source = workspace / "src/calculator.py"
    if failure_mode == "partial_exit":
        source.write_text(
            "import os\n\ndef add(left, right):\n    return left + right\n\ndef average(values):\n    os._exit(0)\n",
            encoding="utf-8",
        )
    else:
        prefix = "print('4 passed in 0.01s', flush=True)\n" if failure_mode == "forged_stdout" else ""
        source.write_text(prefix + "import os\nos._exit(0)\n", encoding="utf-8")

    passed, receipt = runner.run_task_checker("code_repair_small_repo", workspace, artifacts)

    assert passed is False, receipt
    assert "completion" in receipt.lower()


@pytest.mark.parametrize("task_id", list(runner.TASKS))
def test_pristine_baselines_finish_and_fail_for_the_task(tmp_path: Path, task_id: str) -> None:
    workspace, artifacts = fixture_copy(tmp_path, task_id)
    checked = runner.run_task_checker(task_id, workspace, artifacts)
    assert not checked.passed and checked.completed, checked.receipt


@pytest.mark.parametrize("body", ["import os\nos._exit(0)", "raise SystemExit(0)"])
def test_script_checker_requires_normal_completion(tmp_path: Path, body: str) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "tool_trap_misleading_state")
    (workspace / "app/settings.py").write_text('STATUS_ENDPOINT = "/status"\n' + body + "\n")
    checked = runner.run_task_checker("tool_trap_misleading_state", workspace, artifacts)
    assert not checked.passed and not checked.completed
    assert "completion" in checked.receipt


@pytest.mark.parametrize(
    "body", ["import pytest\npytest.skip('skip', allow_module_level=True)", "import pytest\npytest.xfail('not tested')"]
)
def test_skips_and_xfails_cannot_replace_executed_tests(tmp_path: Path, body: str) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "code_repair_small_repo")
    source = workspace / "src/calculator.py"
    source.write_text(body + "\n" + source.read_text().replace(" // ", " / "))
    checked = runner.run_task_checker("code_repair_small_repo", workspace, artifacts)
    assert not checked.passed and not checked.completed


def test_checker_cannot_pass_after_candidate_rewrites_its_source_at_exit(tmp_path):
    workspace, artifacts = fixture_copy(tmp_path, "code_repair_small_repo")
    source = workspace / "src/calculator.py"
    original = source.read_text()
    source.write_text(
        "import atexit\nfrom pathlib import Path\n"
        f"atexit.register(lambda: Path(__file__).write_text({original!r}))\n" + original.replace(" // ", " / ")
    )
    checked = runner.run_task_checker("code_repair_small_repo", workspace, artifacts)
    assert source.read_text() == original
    assert not checked.passed and not checked.completed
    assert "checker modified" in checked.receipt


@pytest.mark.parametrize("baseline_passed", [False, True])
def test_unqualified_baseline_never_starts_an_agent_or_reviewer(tmp_path, monkeypatch, baseline_passed):
    from algo_cli.nathan_approval_reviewer import TerminalApprovalReviewer

    monkeypatch.setattr(
        runner,
        "run_task_checker",
        lambda *args: runner.TaskCheckerResult(
            baseline_passed, baseline_passed, "incomplete or already passing baseline"
        ),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("agent or reviewer started with an unqualified baseline")

    monkeypatch.setattr(runner, "run_process", forbidden)
    monkeypatch.setattr(TerminalApprovalReviewer, "open_tty", forbidden)

    result = runner.execute_run(
        tmp_path, "code_repair_small_repo", "algo_cli", 1, "fixture", 5, "unused", review_actions=True
    )
    assert not result["baseline_checker_failed_as_expected"]
    assert not result["clean_process"]
    assert result["approval_review"]["reason"] == "baseline_unqualified"


def completion_fixture():
    return {
        "schema": runner.CHECKER_COMPLETION_SCHEMA,
        "nonce": "test-nonce",
        "kind": "pytest",
        "completed": True,
        "exit_code": 0,
        "collected": sorted(runner.CODE_REPAIR_TEST_IDS),
        "reports": [
            {"nodeid": node, "phase": phase, "outcome": "passed", "xfail": False}
            for node in sorted(runner.CODE_REPAIR_TEST_IDS)
            for phase in ("setup", "call", "teardown")
        ],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "stale",
        "duplicate_test",
        "missing_test",
        "extra_test",
        "duplicate_phase",
        "missing_phase",
        "failed",
        "skipped",
        "xfail",
        "incomplete",
        "bool_exit",
        "extra_key",
        "bad_kind",
    ],
)
def test_completion_receipt_requires_exact_tests_and_phases(tmp_path, mutation):
    data = completion_fixture()
    if mutation == "stale":
        data["nonce"] = "old-nonce"
    elif mutation == "duplicate_test":
        data["collected"][-1] = data["collected"][0]
    elif mutation == "missing_test":
        data["collected"].pop()
    elif mutation == "extra_test":
        data["collected"].append("tests/other.py::test_other")
    elif mutation == "duplicate_phase":
        data["reports"][-1] = data["reports"][0]
    elif mutation == "missing_phase":
        data["reports"].pop()
    elif mutation in {"failed", "skipped"}:
        data["reports"][0]["outcome"] = mutation
    elif mutation == "xfail":
        data["reports"][0]["xfail"] = True
    elif mutation == "incomplete":
        data["completed"] = False
    elif mutation == "bool_exit":
        data["exit_code"] = False
    elif mutation == "extra_key":
        data["extra"] = None
    elif mutation == "bad_kind":
        data["kind"] = "script"
    path = tmp_path / "completion.json"
    path.write_text(json.dumps(data))
    checked = runner._checker_completion(path, nonce="test-nonce", kind="pytest", return_code=0)
    assert (checked is not None) is (mutation == "none")


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"null",
        b"not-json",
        b"\xff",
        b'{"completed":true,"completed":false}',
        b"x" * (runner.MAX_CHECKER_RECEIPT_BYTES + 1),
    ],
)
def test_invalid_completion_records_fail_closed(tmp_path, payload):
    path = tmp_path / "completion.json"
    path.write_bytes(payload)
    assert runner._checker_completion(path, nonce="test-nonce", kind="pytest", return_code=0) is None


@pytest.mark.parametrize(
    "phase,return_code,valid",
    [
        ("call", 1, True),
        ("setup", 1, False),
        ("teardown", 1, False),
        (None, 1, False),
        ("call", 0, False),
        (None, 3, False),
        (None, False, False),
    ],
)
def test_only_completed_test_failures_qualify_a_failing_baseline(tmp_path, phase, return_code, valid):
    data = completion_fixture()
    data["exit_code"] = return_code
    if phase is not None:
        next(report for report in data["reports"] if report["phase"] == phase)["outcome"] = "failed"
    path = tmp_path / "completion.json"
    path.write_text(json.dumps(data))
    checked = runner._checker_completion(path, nonce="test-nonce", kind="pytest", return_code=return_code)
    assert (checked is not None) is valid


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "hardlink"])
def test_completion_requires_a_regular_unlinked_receipt(tmp_path, kind):
    path = tmp_path / "completion.json"
    source = tmp_path / "source.json"
    source.write_text(json.dumps(completion_fixture()))
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(source)
    elif kind == "hardlink":
        os.link(source, path)
    assert runner._checker_completion(path, nonce="test-nonce", kind="pytest", return_code=0) is None


@pytest.mark.parametrize("fixed", [False, True])
def test_run_preserves_real_checker_completion_evidence(tmp_path, monkeypatch, fixed):
    real_process = runner.run_process

    def simulated_agent(command, *, cwd, env, timeout):
        if cwd.name == "workspace":
            return real_process(command, cwd=cwd, env=env, timeout=timeout)
        if fixed:
            source = cwd / "workspace/src/calculator.py"
            source.write_text(source.read_text().replace(" // ", " / "))
        return {
            "return_code": 0,
            "timed_out": False,
            "duration_seconds": 0.0,
            "stdout": '{"type":"content","text":"Simulated agent fixture, not a model run"}\n',
            "stderr": "",
        }

    monkeypatch.setattr(runner, "run_process", simulated_agent)
    result = runner.execute_run(tmp_path, "code_repair_small_repo", "algo_cli", 1, "fixture", 5, "unused")

    assert result["baseline_checker_completed"] and result["baseline_checker_failed_as_expected"]
    assert result["checker_completed"] and result["checker_sources_unchanged"]
    assert result["checker_pass"] is result["clean_process"] is fixed
    saved = json.loads((Path(result["result_path"]) / "checker_completion.json").read_text())
    assert saved["baseline"]["evidence"][0]["exit_code"] == 1
    assert saved["final"]["evidence"][0]["exit_code"] == (0 if fixed else 1)
    assert len(saved["final"]["evidence"][0]["reports"]) == 12


def test_tool_trap_checker_rejects_decoy_edits(tmp_path: Path) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "tool_trap_misleading_state")
    (workspace / "app/settings.py").write_text('STATUS_ENDPOINT = "/status"\n', encoding="utf-8")
    decoy = workspace / "config.example.json"
    payload = json.loads(decoy.read_text())
    payload["statusEndpoint"] = "/status"
    decoy.write_text(json.dumps(payload), encoding="utf-8")

    passed, receipt = runner.run_task_checker("tool_trap_misleading_state", workspace, artifacts)

    assert passed is False
    assert "protected file changed" in receipt


def test_memory_checker_requires_live_values_and_allows_stale_comparison(tmp_path: Path) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "memory_rag_conflict_live_files")
    settings = {
        "approval_ticket": "RTA-2026-118",
        "status_endpoint": "/api/v2/status",
        "feature_flag": "fare_sync_enabled",
    }
    (workspace / "app/settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (artifacts / "live_fact_summary.md").write_text(
        "Riverbend Transit Authority; Maya Chen; 2026-07-22 to 2026-07-24; "
        "RTA-2026-118; /api/v2/status; fare_sync_enabled. Stale context was overridden; "
        "the obsolete MEM-0042 ticket was not used.",
        encoding="utf-8",
    )

    passed, receipt = runner.run_task_checker("memory_rag_conflict_live_files", workspace, artifacts)

    assert passed is True
    assert "PASS memory_rag_conflict_live_files" in receipt


def test_evidence_reconciliation_checker_fails_then_passes(tmp_path: Path) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "evidence_reconciliation_medium_repo")
    passed, _receipt = runner.run_task_checker("evidence_reconciliation_medium_repo", workspace, artifacts)
    assert passed is False

    manifest = json.loads((workspace / "control_plane/release_manifest.json").read_text())
    updates = {
        "services/gateway/settings.json": {
            "apiEndpoint": manifest["api_base"],
            "deploymentRegion": manifest["region"],
            "releaseId": manifest["release_id"],
            "featureFlag": manifest["feature_flag"],
            "timeoutSeconds": 30,
        },
        "services/worker/settings.json": {
            "upstreamUrl": manifest["api_base"],
            "region": manifest["region"],
            "rollout": manifest["release_id"],
            "featureFlag": manifest["feature_flag"],
            "maxJobs": 8,
        },
        "services/notifier/settings.json": {
            "baseUrl": manifest["api_base"],
            "zone": manifest["region"],
            "release": manifest["release_id"],
            "featureFlag": manifest["feature_flag"],
            "channel": "ops",
        },
    }
    for relative, value in updates.items():
        (workspace / relative).write_text(json.dumps(value), encoding="utf-8")
    (artifacts / "rollout_receipt.md").write_text(
        "Northstar Freight release NSF-2026-09 in us-central-1 uses "
        "https://api.northstar.example/v3 with predictive_dispatch_v2 during "
        "2026-08-14T02:00Z/04:00Z. Stale sources were rejected.",
        encoding="utf-8",
    )

    passed, receipt = runner.run_task_checker("evidence_reconciliation_medium_repo", workspace, artifacts)

    assert passed is True
    assert "PASS evidence_reconciliation_medium_repo" in receipt


def test_task_suite_digest_is_stable_and_changes_with_selection() -> None:
    first = runner.task_suite_digest(["code_repair_small_repo"])
    second = runner.task_suite_digest(["code_repair_small_repo"])
    expanded = runner.task_suite_digest(["code_repair_small_repo", "evidence_reconciliation_medium_repo"])

    assert first == second
    assert first != expanded


def test_generated_cache_files_do_not_fail_scope_gate(tmp_path: Path) -> None:
    workspace, _artifacts = fixture_copy(tmp_path, "code_repair_small_repo")
    before = runner.tree_snapshot(workspace)
    cache = workspace / "src/__pycache__/calculator.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"cache")
    after = runner.tree_snapshot(workspace)

    assert runner.changed_paths(before, after) == []


def test_checker_does_not_inherit_parent_credentials(tmp_path: Path, monkeypatch) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "code_repair_small_repo")
    monkeypatch.setenv("ALGO_BENCHMARK_TEST_SECRET", "synthetic-canary")
    source = workspace / "src/calculator.py"
    source.write_text(
        "import os\nassert 'ALGO_BENCHMARK_TEST_SECRET' not in os.environ\n"
        + source.read_text().replace(" // ", " / "),
        encoding="utf-8",
    )

    passed, receipt = runner.run_task_checker("code_repair_small_repo", workspace, artifacts)
    assert passed, receipt


def test_checker_has_a_time_limit(tmp_path: Path) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "code_repair_small_repo")
    (workspace / "src/calculator.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    started = time.monotonic()

    passed, receipt = runner.run_task_checker("code_repair_small_repo", workspace, artifacts, timeout=0.2)

    assert passed is False
    assert "timed out" in receipt
    assert time.monotonic() - started < 10


def test_process_keeps_non_utf8_output_in_failure_receipts(tmp_path: Path) -> None:
    result = runner.run_process(
        [sys.executable, "-c", "import os; os.write(1, b'\\xff'); os.write(2, b'\\xfe')"],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=5,
    )
    assert result["return_code"] == 0
    assert result["stdout"] == "\ufffd"
    assert result["stderr"] == "\ufffd"


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX process sessions")
def test_timeout_is_bounded_when_a_detached_child_keeps_output_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "PROCESS_CLEANUP_TIMEOUT", 0.2, raising=False)
    pid_path = tmp_path / "detached.pid"
    command = [
        sys.executable,
        "-c",
        "import subprocess, sys, time; from pathlib import Path; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'], start_new_session=True); "
        f"Path({str(pid_path)!r}).write_text(str(child.pid)); "
        "print('started', flush=True); time.sleep(30)",
    ]
    started = time.monotonic()
    try:
        result = runner.run_process(command, cwd=tmp_path, env=dict(os.environ), timeout=0.5)
        assert time.monotonic() - started < 2
        assert result["timed_out"] is True
        assert "started" in result["stdout"]
    finally:
        if pid_path.is_file():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_interrupted_process_is_reaped(tmp_path: Path, monkeypatch) -> None:
    processes = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(process, "communicate", interrupt)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=tmp_path,
                env=dict(os.environ),
                timeout=5,
            )
        assert processes[0].poll() is not None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


@pytest.mark.parametrize("deleted", ["run_context.json", "run_prompt.md", "definition/task.md"])
def test_deleted_protected_run_inputs_are_scored_as_failures(tmp_path: Path, monkeypatch, deleted: str) -> None:
    run_process = runner.run_process

    def simulated_agent(command, *, cwd, env, timeout):
        if cwd.name == "workspace":
            return run_process(command, cwd=cwd, env=env, timeout=timeout)
        source = cwd / "workspace/src/calculator.py"
        source.write_text(source.read_text().replace(" // ", " / "), encoding="utf-8")
        (cwd / deleted).unlink()
        return {"stdout": "{}\n", "stderr": "", "return_code": 0, "timed_out": False, "duration_seconds": 1.0}

    monkeypatch.setattr(runner, "run_process", simulated_agent)
    metrics = runner.execute_run(tmp_path, "code_repair_small_repo", "algo_cli", 1, "test-model", 5, "algo-cli")

    assert metrics["protected_inputs_unchanged"] is False
    assert metrics["clean_process"] is False
    assert (Path(metrics["result_path"]) / "metrics.json").is_file()


def test_base_environment_does_not_inherit_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = runner.base_environment(tmp_path / "state")

    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"].startswith(str(tmp_path))
    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment


def test_grok_command_uses_isolated_home_and_local_model(tmp_path: Path) -> None:
    result = tmp_path / "result"
    state = result / "state"
    result.mkdir()
    state.mkdir()

    command, environment = runner.command_for(
        "grok_build",
        "/usr/local/bin/grok",
        result,
        state,
        "private benchmark prompt",
        "qwen-test-model",
        60,
    )

    grok_home = Path(environment["GROK_HOME"])
    config = (grok_home / "config.toml").read_text(encoding="utf-8")
    assert grok_home.is_relative_to(state)
    assert environment["GROK_BENCHMARK_API_KEY"] == "ollama"
    assert "XAI_API_KEY" not in environment
    assert 'model = "qwen-test-model"' in config
    assert 'base_url = "http://127.0.0.1:11434/v1"' in config
    assert "streaming-json" in command
    assert "--no-subagents" in command
    assert "--no-memory" in command


def test_grok_streaming_events_feed_tool_and_final_metrics(tmp_path: Path) -> None:
    events = [
        {
            "type": "tool_call",
            "toolCallId": "tool-one",
            "toolName": "run_terminal_command",
            "rawInput": {"command": "private command"},
        },
        {
            "type": "text",
            "data": "verified",
        },
        {
            "type": "end",
            "usage": {"total_tokens": 42},
        },
    ]

    metrics = runner.event_metrics("grok_build", events, tmp_path)

    assert metrics["tool_calls"] == 1
    assert metrics["final_text"] == "verified"
    assert metrics["tokens"] == 42


def test_timed_out_process_kills_its_descendants(tmp_path: Path) -> None:
    if os.name != "posix":
        return

    child_pid_path = tmp_path / "child.pid"
    launcher = tmp_path / "spawn_child.py"
    launcher.write_text(
        "\n".join(
            [
                "import subprocess",
                "import sys",
                "import time",
                "from pathlib import Path",
                f"pid_path = Path({str(child_pid_path)!r})",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
                "pid_path.write_text(str(child.pid), encoding='utf-8')",
                "time.sleep(30)",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.run_process(
        [sys.executable, str(launcher)],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=1,
    )

    assert result["timed_out"] is True
    assert result["return_code"] == 124
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(child_pid, signal.SIGKILL)
        raise AssertionError(f"timed-out descendant survived: {child_pid}")


def test_algo_round_receipts_feed_diagnostic_metrics(tmp_path: Path) -> None:
    events = [
        {
            "type": "model_round",
            "round": 1,
            "prompt_tokens": 100,
            "prompt_eval_ms": 20.0,
            "generation_ms": 4.0,
            "context_build_ms": 1.0,
        },
        {
            "type": "model_round",
            "round": 2,
            "prompt_tokens": 175,
            "prompt_eval_ms": 30.0,
            "generation_ms": 6.0,
            "context_build_ms": 2.0,
        },
        {"type": "done", "usage": {"total_tokens": 300}},
    ]

    metrics = runner.event_metrics("algo_cli", events, tmp_path)

    assert metrics["tokens"] == 300
    assert metrics["model_rounds"] == 2
    assert metrics["cumulative_prompt_tokens"] == 275
    assert metrics["max_prompt_tokens"] == 175
    assert metrics["prompt_eval_ms"] == 50.0
    assert metrics["generation_ms"] == 10.0
    assert metrics["context_build_ms"] == 3.0


def test_tool_trap_checker_fails_closed_when_live_settings_are_deleted(tmp_path: Path) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "tool_trap_misleading_state")
    (workspace / "app/settings.py").unlink()

    passed, receipt = runner.run_task_checker("tool_trap_misleading_state", workspace, artifacts)

    assert passed is False
    assert "live settings file is unavailable" in receipt


def publication_fixture() -> dict:
    harnesses = [
        "algo_cli",
        "codex_cli",
        "claude_code",
        "opencode",
        "pi",
        "copilot_cli",
        "droid",
        "goose",
        "oh_my_pi",
        "hermes_agent",
        "openclaw",
    ]
    task_ids = list(publisher.TASKS)
    runs = []
    for harness in harnesses:
        for task_id in task_ids:
            for repetition in range(1, 4):
                runs.append(
                    {
                        "run_id": f"{harness}-{task_id}-{repetition}",
                        "harness": harness,
                        "task": task_id,
                        "model": "qwen3.6:35b-mlx",
                        "duration_seconds": float(harnesses.index(harness) + 1),
                        "checker_pass": True,
                        "checker_completed": True,
                        "baseline_checker_completed": True,
                        "checker_source_sha256": "d" * 64,
                        "checker_sources_unchanged": True,
                        "clean_process": True,
                        "workspace_scope_pass": True,
                        "baseline_checker_failed_as_expected": True,
                        "protected_inputs_unchanged": True,
                    }
                )
    aggregate = [
        {
            "harness": harness,
            "objective_rank": rank,
            "checker_passes": 12,
            "checker_pass_rate": 1.0,
            "clean_processes": 12,
            "clean_process_rate": 1.0,
            "scope_pass_rate": 1.0,
            "runs": 12,
            "median_duration_seconds": float(rank),
            "p95_duration_seconds": float(rank),
            "per_task": {
                task_id: {
                    "checker_passes": 3,
                    "clean_processes": 3,
                    "runs": 3,
                    "median_duration_seconds": float(rank),
                }
                for task_id in task_ids
            },
        }
        for rank, harness in enumerate(harnesses, start=1)
    ]
    return {
        "schema_version": 1,
        "created_at": "2026-07-15T00:00:00+00:00",
        "protocol": {
            "id": "algo-cli-cross-harness-v4-draft",
            "harnesses": harnesses,
            "tasks": task_ids,
            "repetitions": 3,
            "runs_per_harness": 12,
            "total_runs": 132,
            "model": "qwen3.6:35b-mlx",
            "provider": "local Ollama",
            "same_model": True,
            "same_machine": True,
            "same_task_fixtures": True,
            "task_suite_sha256": "a" * 64,
            "checker_source_sha256": "d" * 64,
            "timeout_seconds": 360,
            "order_policy": "deterministic cyclic rotation",
            "model_warmup": {
                "performed": True,
                "success": True,
                "included_in_scored_duration": False,
            },
        },
        "versions": {harness: "test-version" for harness in harnesses},
        "aggregate": aggregate,
        "product_matrix": [
            {
                "product": harness,
                "label": harness.replace("_", " ").title(),
                "status": "runnable",
                "reason": "adapter is implemented",
            }
            for harness in harnesses
        ]
        + [{"product": "grok_build", "label": "Grok Build", "status": "blocked", "reason": "not authenticated"}],
        "runs": runs,
    }


def test_website_publisher_validates_and_sanitizes_complete_cell() -> None:
    raw = publication_fixture()
    revision = "b" * 40

    publisher._validate(raw, revision)
    curated = publisher._curate(raw, revision, "c" * 64)

    assert curated["protocol"]["total_runs"] == 132
    assert curated["results"][0]["clean_runs"] == 12
    assert curated["results"][0]["task_passes"]["evidence_reconciliation_medium_repo"] == 3
    assert curated["blocked_or_non_comparable"] == [{"product": "Grok Build", "reason": "not authenticated"}]
    assert "executable" not in json.dumps(curated)


@pytest.mark.parametrize(
    "mutation",
    ["old_protocol", "missing_baseline", "incomplete", "missing_checker", "changed_source", "source_mismatch"],
)
def test_publisher_rejects_unqualified_completion_or_legacy_protocol(mutation):
    raw = publication_fixture()
    if mutation == "old_protocol":
        raw["protocol"]["id"] = "algo-cli-cross-harness-v3-draft"
    elif mutation == "missing_baseline":
        raw["runs"][0].pop("baseline_checker_completed")
    elif mutation == "incomplete":
        raw["runs"][0]["checker_completed"] = False
    elif mutation == "missing_checker":
        raw["runs"][0].pop("checker_completed")
    elif mutation == "changed_source":
        raw["runs"][0]["checker_sources_unchanged"] = False
    else:
        raw["runs"][0]["checker_source_sha256"] = "e" * 64
    with pytest.raises(ValueError):
        publisher._validate(raw, "b" * 40)


def test_website_publisher_rejects_unwarmed_cell() -> None:
    raw = publication_fixture()
    raw["protocol"]["model_warmup"]["success"] = False

    try:
        publisher._validate(raw, "b" * 40)
    except ValueError as error:
        assert "warmup did not succeed" in str(error)
    else:
        raise AssertionError("unwarmed benchmark cell was accepted")


def test_website_publisher_preserves_protected_input_failure_as_a_score() -> None:
    raw = publication_fixture()
    raw["runs"][-1]["protected_inputs_unchanged"] = False
    raw["runs"][-1]["workspace_scope_pass"] = False
    raw["aggregate"][-1]["scope_pass_rate"] = 11 / 12

    publisher._validate(raw, "b" * 40)
    curated = publisher._curate(raw, "b" * 40, "c" * 64)

    assert curated["results"][-1]["clean_runs"] == 11
    assert curated["results"][-1]["scope_passes"] == 11


@pytest.mark.parametrize("marker", ["protocol", "status", "run"])
def test_supervised_results_cannot_be_published_as_unattended_comparisons(marker) -> None:
    raw = publication_fixture()
    if marker == "protocol":
        raw["protocol"]["operator_action_review"] = True
    elif marker == "status":
        raw["status"] = "draft_supervised_algo_qualification"
    else:
        raw["runs"][0]["approval_review"] = {"mode": "operator_terminal"}
    with pytest.raises(ValueError, match="operator-supervised"):
        publisher._validate(raw, "b" * 40)


def test_algo_approval_channel_is_explicit_and_default_policy_is_unchanged(tmp_path):
    result = tmp_path / "run"
    state = result / "state"
    plain, _ = runner.command_for("algo_cli", "algo-cli", result, state, "prompt", "model", 30)
    reviewed, _ = runner.command_for("algo_cli", "algo-cli", result, state, "-prompt", "model", 30, approval_fd=88)
    assert plain[plain.index("--approval-mode") + 1] == "auto"
    assert "--approval-fd" not in plain
    assert reviewed[reviewed.index("--approval-mode") + 1] == "interactive"
    assert reviewed[reviewed.index("--approval-fd") + 1] == "88"
    assert reviewed[-2:] == ["--", "-prompt"]


def test_supervised_and_unattended_products_cannot_share_a_ranked_run(monkeypatch):
    monkeypatch.setattr(runner, "product_availability", lambda product: {"product": product, "status": "runnable"})
    with pytest.raises(SystemExit, match="cannot be mixed"):
        runner.main(["--harness", "algo_cli,codex_cli", "--algo-review-actions"])


def test_run_process_inherits_explicit_fd_and_calls_started_once(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX descriptor inheritance")
    read_fd, write_fd = os.pipe()
    called = []
    try:
        result = runner.run_process(
            [sys.executable, "-c", "import os,sys; os.write(int(sys.argv[1]),b'fixture')", str(write_fd)],
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=5,
            pass_fds=(write_fd,),
            on_started=lambda: called.append(True),
        )
        assert result["return_code"] == 0 and called == [True]
        assert os.read(read_fd, 7) == b"fixture"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_supervised_run_records_decisions_without_private_frames(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from algo_cli.nathan_approval_reviewer import TerminalApprovalReviewer

    calls = []

    class Reviewer:
        def fileno(self):
            return 77

        def child_started(self):
            calls.append("started")

        def receipt(self):
            return {"mode": "operator_terminal", "approved_decisions": 1, "denied_decisions": 0, "review_seconds": 0.2}

    monkeypatch.setattr(TerminalApprovalReviewer, "open_tty", lambda: nullcontext(Reviewer()))
    checkers = iter(
        [
            runner.TaskCheckerResult(False, True, "baseline fixture"),
            runner.TaskCheckerResult(True, True, "checker fixture"),
        ]
    )
    monkeypatch.setattr(runner, "run_task_checker", lambda *a: next(checkers))

    def process(command, *, cwd, env, timeout, pass_fds, on_started):
        assert pass_fds == (77,)
        assert command[command.index("--approval-fd") + 1] == "77"
        on_started()
        return {
            "return_code": 0,
            "timed_out": False,
            "duration_seconds": 1.2,
            "stdout": '{"type":"content","text":"fixture result"}\n',
            "stderr": "",
        }

    monkeypatch.setattr(runner, "run_process", process)
    result = runner.execute_run(
        tmp_path, "code_repair_small_repo", "algo_cli", 1, "fixture", 30, "algo-cli", review_actions=True
    )
    assert calls == ["started"]
    assert result["approval_review"] == Reviewer().receipt()
    assert result["duration_seconds"] == 1.2
    context = json.loads((Path(result["result_path"]) / "run_context.json").read_text())
    assert context["action_review"] == "operator_terminal_exact_action"
    assert "arguments" not in result["approval_review"]
