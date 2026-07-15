from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "benchmarks/competitors/runner.py"
SPEC = importlib.util.spec_from_file_location("competitor_benchmark_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

PUBLISHER_PATH = ROOT / "benchmarks/competitors/publish_website.py"
PUBLISHER_SPEC = importlib.util.spec_from_file_location(
    "competitor_benchmark_publisher", PUBLISHER_PATH
)
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
    }


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


def test_model_warmup_is_receipted_and_excluded_from_scores(
    tmp_path: Path, monkeypatch
) -> None:
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
    passed, _receipt = runner.run_task_checker("code_repair_small_repo", workspace, artifacts)
    assert passed is False

    source = workspace / "src/calculator.py"
    source.write_text(source.read_text().replace(" // ", " / "), encoding="utf-8")

    passed, receipt = runner.run_task_checker("code_repair_small_repo", workspace, artifacts)
    assert passed is True
    assert "PASS code_repair_small_repo" in receipt


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
    passed, _receipt = runner.run_task_checker(
        "evidence_reconciliation_medium_repo", workspace, artifacts
    )
    assert passed is False

    manifest = json.loads(
        (workspace / "control_plane/release_manifest.json").read_text()
    )
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

    passed, receipt = runner.run_task_checker(
        "evidence_reconciliation_medium_repo", workspace, artifacts
    )

    assert passed is True
    assert "PASS evidence_reconciliation_medium_repo" in receipt


def test_task_suite_digest_is_stable_and_changes_with_selection() -> None:
    first = runner.task_suite_digest(["code_repair_small_repo"])
    second = runner.task_suite_digest(["code_repair_small_repo"])
    expanded = runner.task_suite_digest(
        ["code_repair_small_repo", "evidence_reconciliation_medium_repo"]
    )

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


def test_base_environment_does_not_inherit_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = runner.base_environment(tmp_path / "state")

    assert environment["PATH"] == "/usr/bin"
    assert environment["HOME"].startswith(str(tmp_path))
    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment


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
        "algo_cli", "codex_cli", "claude_code", "opencode", "pi", "copilot_cli",
        "droid", "goose", "oh_my_pi", "hermes_agent", "openclaw",
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
            "id": "algo-cli-cross-harness-v3-draft",
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
        ] + [
            {"product": "grok_build", "label": "Grok Build", "status": "blocked", "reason": "not authenticated"}
        ],
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
    assert curated["blocked_or_non_comparable"] == [
        {"product": "Grok Build", "reason": "not authenticated"}
    ]
    assert "executable" not in json.dumps(curated)


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
