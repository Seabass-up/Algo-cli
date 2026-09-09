"""Completion evidence must cover the actual workspace and possible mutations."""

import os
import shlex
import subprocess
import sys

import pytest

from algo_cli import config as config_module, execution_guardrails as guardrails, nathan_runtime as runtime, tools
from algo_cli.config import Config
from algo_cli.james_dispatch import dispatch_action
from test_execution_guardrails import _init_git_repo
from test_james_dispatch import _dependencies
from test_agent_progress_recovery import call, run_script


POSIX_SHELL = pytest.mark.skipif(os.name == "nt", reason="POSIX shell fixture syntax")


@POSIX_SHELL
def test_failed_shell_write_invalidates_reads_and_completion(tmp_path):
    target = tmp_path / "module.py"
    target.write_text("before\n")
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('module.py').write_text('after\\n'); raise SystemExit(1)",
        ]
    )
    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    deps = _dependencies(tmp_path, lambda _name, args, _cfg: tools.run_shell(**args))
    deps.approve = lambda *_args, **_kwargs: True
    scope = guardrails.begin_execution_scope(tmp_path)
    try:
        guardrails.record_read(target, success=True)
        result = dispatch_action("run_shell", {"command": command}, cfg, dependencies=deps, render=False)
        assert result.outcome.invoked and not result.outcome.worked
        assert target.read_text() == "after\n"
        assert not guardrails.completion_decision().allowed
        assert not guardrails.read_before_edit_decision(target).allowed
    finally:
        guardrails.end_execution_scope(scope)


@pytest.mark.parametrize("wrapper", ["cd {other} && pytest -q", "cd {other} && ruff check . && pytest -q"])
def test_verification_in_another_checkout_does_not_complete_active_changes(tmp_path, wrapper):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    scope = guardrails.begin_execution_scope(workspace)
    try:
        guardrails.record_mutation("module.py", success=True, operation="write_file")
        command = wrapper.format(other=shlex.quote(str(other)))
        assert guardrails.classify_verification_command(command).qualifies
        assert guardrails.record_shell_verification(command, returncode=0) is None
        assert not guardrails.completion_decision().allowed
    finally:
        guardrails.end_execution_scope(scope)


@pytest.mark.parametrize(
    "name,result,args",
    [
        ("run_shell", "passed\n[exit code: 0]", {"command": "pytest -q"}),
        ("git_diff", "diff --git a/module.py b/module.py\n+value = 1", {}),
    ],
)
def test_runtime_verification_uses_the_executed_workspace(tmp_path, name, result, args):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    scope = guardrails.begin_execution_scope(workspace)
    try:
        guardrails.record_mutation("module.py", success=True, operation="write_file")
        runtime.record_tool_attempt(
            Config(cwd=str(other)), name=name, args={**args, "cwd": str(other)}, result=result, status="worked"
        )
        assert not guardrails.completion_decision().allowed
    finally:
        guardrails.end_execution_scope(scope)


def test_untracked_file_is_not_verified_by_an_empty_tracked_diff(tmp_path):
    _init_git_repo(tmp_path)
    target = tmp_path / "new.py"
    target.write_text("value = 1   \n")
    scope = guardrails.begin_execution_scope(tmp_path)
    try:
        guardrails.record_mutation(target, success=True, operation="write_file")
        result = subprocess.run(["git", "diff", "--check", "HEAD"], cwd=tmp_path, capture_output=True)
        assert result.returncode == 0 and not result.stdout and not result.stderr
        assert not guardrails.auto_verify_working_tree().allowed
    finally:
        guardrails.end_execution_scope(scope)


@pytest.mark.parametrize(
    "command",
    [
        "uv --project ../other run pytest -q",
        "uv run --directory=../other pytest -q",
        "npm --prefix=../other test",
        "cargo test --manifest-path ../other/Cargo.toml",
        "pytest ../other/tests",
        pytest.param("PYTHONPATH=../other pytest -q", marks=POSIX_SHELL),
        pytest.param("GIT_DIR=../other/.git git diff --check", marks=POSIX_SHELL),
        "pytest linked",
        "../other/check.py",
        "pytest -c ../other/pytest.ini",
        "cd subproject && pytest -q",
    ],
)
def test_explicit_verifier_scope_cannot_become_workspace_wide(tmp_path, command):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    (workspace / "linked").symlink_to(other, target_is_directory=True)
    (workspace / "subproject").mkdir()
    scope = guardrails.begin_execution_scope(workspace)
    try:
        guardrails.record_mutation("module.py", success=True, operation="write_file")
        assert guardrails.classify_verification_command(command).qualifies
        assert guardrails.record_shell_verification(command, returncode=0) is None
        assert not guardrails.completion_decision().allowed
    finally:
        guardrails.end_execution_scope(scope)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "cd . && pytest tests -q",
        "cd -- . && ruff check . && pytest -q",
        "uv run --directory=. pytest tests -q",
        "cargo test --manifest-path ./Cargo.toml",
        "uv run --python {python} pytest -q",
        "pytest --basetemp ../scratch -q",
        "pytest --junitxml ../reports/result.xml -q",
        pytest.param("PYTHONPATH=src python3 -c 'assert 2 + 2 == 4'", marks=POSIX_SHELL),
        pytest.param("PYTHONPATH=src {python} -m pytest tests -q", marks=POSIX_SHELL),
    ],
)
def test_workspace_verifiers_still_qualify(tmp_path, command):
    scope = guardrails.begin_execution_scope(tmp_path)
    try:
        guardrails.record_mutation("module.py", success=True, operation="write_file")
        assert guardrails.record_shell_verification(command.format(python=shlex.quote(sys.executable)), returncode=0)
        assert guardrails.completion_decision().allowed
    finally:
        guardrails.end_execution_scope(scope)


@pytest.mark.parametrize(
    "status,invoked", [("denied", False), ("skipped", False), ("failed", False), ("worked", False)]
)
def test_noninvocations_cannot_add_mutation_or_verification_evidence(tmp_path, status, invoked):
    scope = guardrails.begin_execution_scope(tmp_path)
    try:
        cfg = Config(cwd=str(tmp_path))
        for command in ("touch module.py", "pytest -q"):
            runtime.record_tool_attempt(
                cfg,
                name="run_shell",
                args={"command": command},
                result="[exit code: 0]",
                status=status,
                invoked=invoked,
            )
        assert guardrails.evidence_snapshot() == ()
    finally:
        guardrails.end_execution_scope(scope)


def test_auto_verification_cannot_be_redirected_by_inherited_git_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    _init_git_repo(workspace)
    _init_git_repo(other)
    target = workspace / "seed.txt"
    target.write_text("changed   \n")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    scope = guardrails.begin_execution_scope(workspace)
    try:
        guardrails.record_mutation(target, success=True, operation="write_file")
        assert not guardrails.auto_verify_working_tree().allowed
    finally:
        guardrails.end_execution_scope(scope)


@POSIX_SHELL
def test_failed_shell_write_finishes_oneshot_partial(monkeypatch, tmp_path):
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('module.py').write_text('changed\\n'); raise SystemExit(1)",
        ]
    )
    code, events, client, invoked, captures = run_script(
        monkeypatch,
        tmp_path,
        lambda turn: call("run_shell", {"command": command}, turn) if turn == 1 else {"content": "Done"},
        max_iterations=4,
        approve=lambda *_args, **_kwargs: True,
        safe_mode=False,
        invoke=lambda _name, args, _cfg: tools.run_shell(**args),
    )
    assert invoked == ["run_shell"], (code, events, invoked)
    assert (tmp_path / "module.py").exists(), (code, events, invoked)
    assert (tmp_path / "module.py").read_text() == "changed\n"
    assert code == 2 and events[-1]["status"] == "partial"
    assert len(client.calls) == 3 and invoked == ["run_shell"]
    assert captures[-1]["completed"] is False


def test_oneshot_fixture_patches_the_current_config_class(monkeypatch, tmp_path):
    class ReloadedConfig(config_module.Config):
        @classmethod
        def load(cls):
            return cls(model="unpatched-config")

    monkeypatch.setattr(config_module, "Config", ReloadedConfig)
    code, events, client, _invoked, _captures = run_script(
        monkeypatch,
        tmp_path,
        lambda _turn: {"content": "Fixture completed"},
    )
    assert code == 0 and len(client.calls) == 1
    assert events[0]["model"] == "test"


def test_auto_verifier_only_requires_coverage_for_unverified_mutations(tmp_path):
    _init_git_repo(tmp_path)
    scope = guardrails.begin_execution_scope(tmp_path)
    try:
        guardrails.record_workspace_mutation(success=False, possible=True)
        guardrails.record_verification("test", success=True)
        (tmp_path / "seed.txt").write_text("changed\n")
        guardrails.record_mutation("seed.txt", success=True, operation="edit_file")
        assert guardrails.auto_verify_working_tree().allowed
    finally:
        guardrails.end_execution_scope(scope)
