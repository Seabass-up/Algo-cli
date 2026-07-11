from __future__ import annotations

from contextvars import Context, copy_context
from dataclasses import asdict
import os
from pathlib import Path

import pytest

from algo_cli import execution_guardrails as guardrails
from algo_cli import tool_runtime
from algo_cli.config import Config


def test_scope_records_only_successful_content_free_ordered_evidence(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("original secret-like file contents", encoding="utf-8")

    scope = guardrails.begin_execution_scope(tmp_path)
    assert guardrails.record_read(target, success=False) is None
    read = guardrails.record_read(target, success=True)
    mutation = guardrails.record_mutation(target, success=True, operation="edit_file")
    assert guardrails.record_shell_verification("pytest -q", returncode=1) is None
    verification = guardrails.record_shell_verification("pytest -q", returncode=0)
    snapshot = guardrails.end_execution_scope(scope)

    assert read is not None and mutation is not None and verification is not None
    assert [event.sequence for event in snapshot] == [1, 2, 3]
    assert [event.kind for event in snapshot] == ["read", "mutation", "verification"]
    assert snapshot[-1].verification_kind == "test"
    assert set(asdict(snapshot[0])) == {
        "sequence",
        "kind",
        "operation",
        "relative_path",
        "verification_kind",
    }
    rendered = repr(snapshot)
    assert "original secret-like file contents" not in rendered
    assert "pytest -q" not in rendered


def test_contextvar_scope_is_local_and_nested_scopes_restore_parent(tmp_path: Path) -> None:
    outer = guardrails.begin_execution_scope(tmp_path)
    assert guardrails.record_read("outer.py", success=True) is not None

    assert Context().run(guardrails.active_workspace) is None
    assert Context().run(guardrails.evidence_snapshot) == ()

    inner_dir = tmp_path / "inner"
    inner_dir.mkdir()
    inner = guardrails.begin_execution_scope(inner_dir)
    assert guardrails.active_workspace() == inner_dir.resolve()
    assert guardrails.record_read("inner.py", success=True) is not None
    assert [event.relative_path for event in guardrails.end_execution_scope(inner)] == ["inner.py"]

    assert guardrails.active_workspace() == tmp_path.resolve()
    assert [event.relative_path for event in guardrails.evidence_snapshot()] == ["outer.py"]
    guardrails.end_execution_scope(outer)
    assert guardrails.active_workspace() is None


def test_scope_end_is_fail_closed_for_wrong_order_or_reuse(tmp_path: Path) -> None:
    outer = guardrails.begin_execution_scope(tmp_path)
    inner = guardrails.begin_execution_scope(tmp_path)
    with pytest.raises(guardrails.ExecutionGuardrailError):
        guardrails.end_execution_scope(outer)
    guardrails.end_execution_scope(inner)
    guardrails.end_execution_scope(outer)
    with pytest.raises(guardrails.ExecutionGuardrailError):
        guardrails.end_execution_scope(outer)


def test_scope_cannot_be_ended_from_a_copied_context(tmp_path: Path) -> None:
    scope = guardrails.begin_execution_scope(tmp_path)
    copied = copy_context()
    with pytest.raises(guardrails.ExecutionGuardrailError):
        copied.run(guardrails.end_execution_scope, scope)
    assert guardrails.active_workspace() == tmp_path.resolve()
    guardrails.end_execution_scope(scope)


def test_begin_scope_rejects_missing_or_non_directory_workspace(tmp_path: Path) -> None:
    with pytest.raises(guardrails.ExecutionGuardrailError):
        guardrails.begin_execution_scope(tmp_path / "missing")
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(guardrails.ExecutionGuardrailError):
        guardrails.begin_execution_scope(file_path)
    with pytest.raises(guardrails.ExecutionGuardrailError):
        guardrails.begin_execution_scope(Path(tmp_path.anchor))


def test_write_path_allows_normalized_contained_target(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    decision = guardrails.assess_write_path(tmp_path, "src/../module.py")
    assert decision.allowed is True
    assert decision.resolved_path == (tmp_path / "module.py").resolve()
    assert decision.relative_path == "module.py"


@pytest.mark.parametrize("candidate", ["../escape.py", "../../escape.py"])
def test_write_path_denies_parent_escape(tmp_path: Path, candidate: str) -> None:
    decision = guardrails.assess_write_path(tmp_path, candidate)
    assert decision.allowed is False
    assert "escape" in decision.reason


def test_write_path_denies_absolute_outside_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    decision = guardrails.assess_write_path(tmp_path, outside)
    assert decision.allowed is False
    assert decision.resolved_path is None


def test_write_path_denies_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    decision = guardrails.assess_write_path(workspace, "linked/escape.py")
    assert decision.allowed is False
    assert "escape" in decision.reason


@pytest.mark.parametrize(
    "candidate",
    [
        ".env",
        ".env.production",
        ".git/config",
        ".ssh/config",
        "credentials.json",
        "private.pem",
        "nested/service-account.json",
        r"nested\.git\config",
    ],
)
def test_sensitive_paths_are_detected_and_denied(tmp_path: Path, candidate: str) -> None:
    assert guardrails.is_sensitive_path(candidate) is True
    decision = guardrails.assess_write_path(tmp_path, candidate)
    assert decision.allowed is False
    assert decision.sensitive is True


def test_similar_non_sensitive_source_names_remain_writable(tmp_path: Path) -> None:
    for candidate in (".gitignore", "tests/test_credentials.py", "src/secrets.py"):
        assert guardrails.is_sensitive_path(candidate) is False
        assert guardrails.assess_write_path(tmp_path, candidate).allowed is True


def test_write_path_probe_failure_denies_without_raising(tmp_path: Path) -> None:
    def broken_resolver(_path: Path, _strict: bool) -> Path:
        raise OSError("probe details must not leak")

    decision = guardrails.assess_write_path(tmp_path, "module.py", resolve_probe=broken_resolver)
    assert decision.allowed is False
    assert decision.reason == "path cannot be resolved safely"
    assert "probe details" not in decision.reason


def test_pure_resolved_path_probe_rejects_unresolved_parent_traversal(tmp_path: Path) -> None:
    workspace = tmp_path.resolve()
    decision = guardrails.assess_resolved_write_path(workspace, workspace / ".." / "outside.py")
    assert decision.allowed is False


def test_read_before_edit_requires_fresh_same_path_read(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("x = 1\n", encoding="utf-8")
    scope = guardrails.begin_execution_scope(tmp_path)

    assert guardrails.read_before_edit_decision(target).allowed is False
    guardrails.record_read(target, success=True)
    first = guardrails.read_before_edit_decision(target)
    assert first.allowed is True
    assert first.read_sequence == 1

    guardrails.record_mutation(target, success=True, operation="edit_file")
    assert guardrails.read_before_edit_decision(target).allowed is False
    guardrails.record_read("other.py", success=True)
    assert guardrails.read_before_edit_decision(target).allowed is False
    guardrails.record_read(target, success=True)
    assert guardrails.read_before_edit_decision(target).allowed is True
    guardrails.end_execution_scope(scope)


def test_workspace_shell_mutation_invalidates_earlier_file_read(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("x = 1\n", encoding="utf-8")
    scope = guardrails.begin_execution_scope(tmp_path)
    guardrails.record_read(target, success=True)
    assert guardrails.read_before_edit_decision(target).allowed is True
    guardrails.record_workspace_mutation(success=True)
    assert guardrails.read_before_edit_decision(target).allowed is False
    guardrails.end_execution_scope(scope)


def test_read_and_mutation_evidence_rejects_outside_sensitive_failed_and_unknown_events(
    tmp_path: Path,
) -> None:
    scope = guardrails.begin_execution_scope(tmp_path)
    assert guardrails.record_read("../outside.py", success=True) is None
    assert guardrails.record_read(".env", success=True) is None
    assert guardrails.record_mutation("file.py", success=False, operation="write_file") is None
    assert guardrails.record_mutation("file.py", success=True, operation="run_shell") is None
    assert guardrails.evidence_snapshot() == ()
    guardrails.end_execution_scope(scope)


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("pytest -q", "test"),
        ("pytest -v tests/test_one.py", "test"),
        ("python -m unittest", "test"),
        ("uv run pytest tests", "test"),
        ("uv run --project . pytest tests", "test"),
        ("uv run --project=. pytest tests", "test"),
        ("cargo test", "test"),
        ("cargo clippy", "lint"),
        ("go test ./...", "test"),
        ("npm run lint", "lint"),
        ("ruff check algo_cli", "lint"),
        ("python -m ruff check algo_cli", "lint"),
        ("mypy algo_cli", "lint"),
        ("git diff --check", "git_diff"),
        ("cd /tmp/workspace && python3 -m pytest -q --tb=short 2>&1", "test"),
        ("cd -- '/tmp/a workspace' && git diff --check 2>&1", "git_diff"),
        ("python3 healthcheck.py", "test"),
        ("python3 verify_settings.py", "test"),
        ('python3 -c "assert 2 + 2 == 4"', "test"),
        ('python3 -c "value = 4\nassert value == 4\nprint(value)"', "test"),
        ('python3 -c "import sys\nerrors = [1]\nif errors: sys.exit(1)"', "test"),
        ('python3 -c "import sys\nok = True\nsys.exit(0 if ok else 1)"', "test"),
    ],
)
def test_verification_command_classification_accepts_real_verifiers(command: str, kind: str) -> None:
    decision = guardrails.classify_verification_command(command)
    assert decision.qualifies is True
    assert decision.kind == kind
    assert "command" not in asdict(decision)


@pytest.mark.skipif(os.name == "nt", reason="POSIX environment-prefix syntax")
def test_posix_environment_prefix_preserves_inline_verification() -> None:
    decision = guardrails.classify_verification_command(
        'PYTHONPATH=src python3 -c "value = 4\nassert value == 4" 2>&1'
    )

    assert decision.qualifies is True
    assert decision.kind == "test"


def test_windows_inline_python_requires_cmd_compatible_double_quotes() -> None:
    accepted = guardrails._inline_python_verification(
        'python3 -c "assert 2 + 2 == 4"',
        platform_name="nt",
    )
    single_quoted = guardrails._inline_python_verification(
        "python3 -c 'assert 2 + 2 == 4'",
        platform_name="nt",
    )
    posix_environment = guardrails._inline_python_verification(
        'PYTHONPATH=src python3 -c "assert True"',
        platform_name="nt",
    )

    assert accepted is not None and accepted.qualifies is True
    assert single_quoted is not None and single_quoted.qualifies is False
    assert posix_environment is None


def test_status_masking_verifier_suffix_is_detected() -> None:
    assert guardrails.masks_verification_exit_status(
        'python3 healthcheck.py 2>&1; echo "EXIT_CODE=$?"'
    ) is True
    assert guardrails.masks_verification_exit_status("python3 healthcheck.py") is False
    assert guardrails.masks_verification_exit_status('echo "EXIT_CODE=$?"') is False


@pytest.mark.parametrize(
    "command",
    [
        "",
        "echo pytest",
        "pytest --collect-only",
        "pytest || true",
        "pytest; true",
        "pytest | tee results.txt",
        "pytest > results.txt",
        "pytest>results.txt",
        "pytest $(touch changed.txt)",
        "cd /tmp && pytest -q && echo passed",
        "cd /tmp && pytest -q | tee results.txt",
        "cd /tmp && pytest -q > results.txt 2>&1",
        "ruff format algo_cli",
        "ruff check --fix algo_cli",
        "ruff check --exit-zero algo_cli",
        "pytest --pass-with-no-tests",
        "git diff",
        "python -c 'print(1)'",
        "python -c 'value = 1'",
        "cargo build",
        "npm install",
    ],
)
def test_verification_command_classification_rejects_ambiguous_or_non_verifiers(command: str) -> None:
    decision = guardrails.classify_verification_command(command)
    assert decision.qualifies is False
    assert decision.kind is None


def test_completion_without_active_scope_fails_closed() -> None:
    decision = guardrails.completion_decision()
    assert decision.allowed is False
    assert "no active" in decision.reason


def test_read_only_scope_can_complete_without_mutation(tmp_path: Path) -> None:
    scope = guardrails.begin_execution_scope(tmp_path)
    guardrails.record_read("module.py", success=True)
    assert guardrails.completion_decision().allowed is True
    guardrails.end_execution_scope(scope)


def test_completion_requires_evidence_after_last_mutation(tmp_path: Path) -> None:
    scope = guardrails.begin_execution_scope(tmp_path)
    guardrails.record_read("first.py", success=True)
    guardrails.record_mutation("first.py", success=True, operation="edit_file")
    guardrails.record_shell_verification("pytest -q", returncode=0)
    guardrails.record_mutation("second.py", success=True, operation="write_file")

    stale = guardrails.completion_decision()
    assert stale.allowed is False
    assert stale.last_mutation_sequence == 4
    guardrails.record_read("first.py", success=True)
    assert guardrails.completion_decision().allowed is False
    reread = guardrails.record_read("second.py", success=True)
    assert reread is not None
    assert guardrails.completion_decision().allowed is False
    verifier = guardrails.record_verification("git_diff", success=True)
    decision = guardrails.completion_decision()
    assert verifier is not None
    assert decision.allowed is True
    assert decision.verifier_sequence == verifier.sequence
    assert decision.verifier_kind == "git_diff"
    guardrails.end_execution_scope(scope)


def test_workspace_shell_mutation_requires_later_verification(tmp_path: Path) -> None:
    scope = guardrails.begin_execution_scope(tmp_path)
    mutation = guardrails.record_workspace_mutation(success=True)
    assert mutation is not None
    assert mutation.relative_path == "."
    assert guardrails.completion_decision().allowed is False
    guardrails.record_shell_verification("pytest -q", returncode=0)
    assert guardrails.completion_decision().allowed is True
    guardrails.end_execution_scope(scope)


@pytest.mark.parametrize(
    ("command", "expected_kind"),
    [
        ("pytest -q", "test"),
        ("ruff check algo_cli", "lint"),
        ("git diff --check", "git_diff"),
    ],
)
def test_passing_recognized_verifier_after_mutation_allows_completion(
    tmp_path: Path,
    command: str,
    expected_kind: str,
) -> None:
    scope = guardrails.begin_execution_scope(tmp_path)
    guardrails.record_mutation("module.py", success=True, operation="write_file")
    guardrails.record_shell_verification(command, returncode=0)
    decision = guardrails.completion_decision()
    assert decision.allowed is True
    assert decision.verifier_kind == expected_kind
    guardrails.end_execution_scope(scope)


def test_failed_or_unrecognized_shell_does_not_satisfy_completion(tmp_path: Path) -> None:
    scope = guardrails.begin_execution_scope(tmp_path)
    guardrails.record_mutation("module.py", success=True, operation="write_file")
    assert guardrails.record_shell_verification("pytest -q", returncode=1) is None
    assert guardrails.record_shell_verification("echo done", returncode=0) is None
    assert guardrails.completion_decision().allowed is False
    guardrails.end_execution_scope(scope)


def test_pure_completion_probe_rejects_malformed_or_forged_evidence() -> None:
    malformed = (
        guardrails.EvidenceEvent(
            sequence=2,
            kind="mutation",
            operation="write_file",
            relative_path="module.py",
        ),
    )
    forged = (
        guardrails.EvidenceEvent(
            sequence=1,
            kind="verification",
            operation="verification",
            verification_kind="arbitrary",
        ),
    )
    assert guardrails.evaluate_completion(malformed).allowed is False
    assert guardrails.evaluate_completion(forged).allowed is False


def test_failed_mutation_is_not_success_evidence_or_a_completion_obligation(tmp_path: Path) -> None:
    scope = guardrails.begin_execution_scope(tmp_path)
    guardrails.record_mutation("module.py", success=False, operation="write_file")
    assert guardrails.evidence_snapshot() == ()
    assert guardrails.completion_decision().allowed is True
    guardrails.end_execution_scope(scope)


def test_preflight_uses_immutable_scope_not_changed_config_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    cfg = Config(cwd=str(workspace))
    scope = guardrails.begin_execution_scope(workspace)

    cfg.cwd = str(outside)
    preflight = tool_runtime.preflight_runtime_tool(
        "write_file",
        {"path": "escape.py", "content": "x"},
        cfg,
    )

    assert preflight.allowed is False
    assert "escapes the workspace" in preflight.blocked_result
    guardrails.end_execution_scope(scope)


def test_shell_evidence_requires_explicit_exit_marker_and_nonempty_diff(tmp_path: Path) -> None:
    cfg = Config(cwd=str(tmp_path))
    scope = guardrails.begin_execution_scope(tmp_path)
    guardrails.record_mutation("new.py", success=True, operation="write_file")

    tool_runtime.record_tool_attempt(
        cfg,
        name="run_shell",
        args={"command": "pytest -q", "cwd": str(tmp_path)},
        result="Blocked by safe mode",
        status="worked",
    )
    tool_runtime.record_tool_attempt(
        cfg,
        name="git_diff",
        args={"cwd": str(tmp_path)},
        result="(no tracked diff)",
        status="worked",
    )
    assert guardrails.completion_decision().allowed is False

    tool_runtime.record_tool_attempt(
        cfg,
        name="run_shell",
        args={"command": "pytest -q", "cwd": str(tmp_path)},
        result="tests passed\n[exit code: 0]\nreflex note",
        status="worked",
    )
    assert guardrails.completion_decision().allowed is True
    guardrails.end_execution_scope(scope)


def test_model_cd_requires_approval_even_for_auto_approve_session(monkeypatch, tmp_path: Path) -> None:
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert tool_runtime.session_command_requires_approval(f"/cd {tmp_path}") is True
    assert (
        tool_runtime.ask_approval(
            "session_slash",
            {"command": f"/cd {tmp_path}"},
            cfg,
        )
        is False
    )
