from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "benchmarks/competitors/runner.py"
SPEC = importlib.util.spec_from_file_location("competitor_benchmark_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


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


def test_tool_trap_checker_fails_closed_when_live_settings_are_deleted(tmp_path: Path) -> None:
    workspace, artifacts = fixture_copy(tmp_path, "tool_trap_misleading_state")
    (workspace / "app/settings.py").unlink()

    passed, receipt = runner.run_task_checker("tool_trap_misleading_state", workspace, artifacts)

    assert passed is False
    assert "live settings file is unavailable" in receipt
