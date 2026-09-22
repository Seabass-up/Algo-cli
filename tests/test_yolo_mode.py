from __future__ import annotations

import copy
import json
import os
import shlex
import sys

import pytest

from algo_cli import config, context_budget, execution_guardrails, session_mode, tools
from algo_cli.config import Config
from algo_cli.intelligence.permission_modes import PermissionLevel, PermissionMode
from algo_cli.nathan_runtime import ask_approval, preflight_runtime_tool, run_tool
from algo_cli.james_dispatch import dispatch_action
from algo_cli.oliver_slash_dispatch import SLASH_COMMANDS, handle_command
from algo_cli.samuel_policy_engine import PolicyDisposition
from test_james_dispatch import _dependencies
from test_nathan_program_runtime import _program_store


def activate(cfg, monkeypatch):
    monkeypatch.setattr(Config, "save", lambda self: None)
    handled, _ = handle_command("/mode yolo", cfg, None, user_initiated=True)
    assert handled
    assert session_mode.active_mode(cfg) == "yolo"


def test_mode_registry_contains_yolo():
    assert "yolo" in session_mode.VALID_MODES
    assert "yolo" in dict(SLASH_COMMANDS)["/mode"]


def test_only_user_can_activate_yolo(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    monkeypatch.setattr(Config, "save", lambda self: None)
    handle_command("/mode yolo", cfg, None)
    assert session_mode.active_mode(cfg) == "explore"
    assert "user" in tools.session_command("/mode yolo", cfg).lower()
    assert not preflight_runtime_tool("session_command", {"command": "/mode yolo"}, cfg).allowed
    activate(cfg, monkeypatch)
    assert cfg.auto_approve_active
    assert cfg.safe_mode  # The mode does not disable a separate protection.


def test_yolo_requires_live_owner_not_a_config_string(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), session_mode="yolo")
    assert session_mode.active_mode(cfg) == "explore"
    assert not cfg.auto_approve_active
    activate(cfg, monkeypatch)
    for clone in (copy.copy(cfg), copy.deepcopy(cfg)):
        assert session_mode.active_mode(clone) == "explore"
        assert not clone.auto_approve_active


def test_yolo_is_not_persisted(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), session_mode="execute")
    handle_command("/mode yolo", cfg, None, user_initiated=True)
    data = json.loads(config.CONFIG_FILE.read_text())
    assert data["session_mode"] == "execute"
    assert data["auto_mode"] is False
    assert Config.load().session_mode == "execute"
    data["session_mode"] = "yolo"
    config.CONFIG_FILE.write_text(json.dumps(data))
    assert Config.load().session_mode == "explore"


def test_yolo_posture_and_exit(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    activate(cfg, monkeypatch)
    assert "Session Mode: yolo" in session_mode.prompt_section(session_mode.active_mode(cfg))
    assert "Do not ask the user" in session_mode.prompt_section("yolo")
    assert "yolo" in session_mode.status_line(cfg)
    assert "unlimited tool calls and turns" in session_mode.status_line(cfg)
    handle_command("/mode explore", cfg, None, user_initiated=True)
    assert not cfg.auto_approve_active
    assert session_mode.active_mode(cfg) == "explore"


@pytest.mark.parametrize("automation", [False, True])
def test_actual_system_prompt_uses_live_activation(monkeypatch, tmp_path, automation):
    cfg = Config(cwd=str(tmp_path), continuum_enabled=True)
    monkeypatch.setattr(context_budget, "_memory_prompt_section", lambda *args, **kwargs: "")
    monkeypatch.setattr(context_budget, "json_sink", lambda: object() if automation else None)
    activate(cfg, monkeypatch)
    prompt = context_budget.build_system_prompt(cfg, user_message="Read the source")
    assert "## Session Mode: yolo" in prompt
    assert "action-bound authority and confirmation receipts" in prompt
    assert "memory backend's protections" in prompt
    with session_mode.delegated_scope():
        child_prompt = context_budget.build_system_prompt(cfg, user_message="Read the source")
        assert "## Session Mode: yolo" not in child_prompt
        assert "## Session Mode: explore" in child_prompt


def test_yolo_workspace_change_revokes_activation(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    activate(cfg, monkeypatch)
    cfg.cwd = str(tmp_path.parent)
    assert session_mode.active_mode(cfg) == "explore"
    assert not cfg.auto_approve_active
    cfg.cwd = str(tmp_path)
    assert not cfg.auto_approve_active


@pytest.mark.parametrize("transition", ["child", "copy", "exit", "workspace"])
def test_yolo_preapproval_cannot_survive_transition(monkeypatch, tmp_path, transition):
    cfg = Config(cwd=str(tmp_path))
    activate(cfg, monkeypatch)
    args = {"query": "public docs"}
    prepared = preflight_runtime_tool("web_search", args, cfg)
    assert prepared.policy.disposition is PolicyDisposition.ALLOW
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    if transition == "child":
        with session_mode.delegated_scope():
            assert not ask_approval("web_search", args, cfg, preflight=prepared)
            assert preflight_runtime_tool("web_search", args, cfg).policy.disposition is PolicyDisposition.CONFIRM
    else:
        if transition == "copy":
            cfg = copy.copy(cfg)
        elif transition == "exit":
            handle_command("/mode explore", cfg, None, user_initiated=True)
        else:
            cfg.cwd = str(tmp_path.parent)
        assert not ask_approval("web_search", args, cfg, preflight=prepared)


def test_yolo_spawn_safety(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    activate(cfg, monkeypatch)
    assert session_mode.MODE_POLICIES["yolo"].tools == ("*",)
    for policy in session_mode.MODE_POLICIES.values():
        assert "yolo" not in policy.spawnable_modes
    assert "explore" in session_mode.MODE_POLICIES["yolo"].spawnable_modes
    with session_mode.delegated_scope():
        assert session_mode.active_mode(cfg) == "explore"
        assert not cfg.auto_approve_active
        with pytest.raises(ValueError, match="user"):
            session_mode.select_mode(cfg, "yolo", user_initiated=True)
    assert session_mode.active_mode(cfg) == "yolo"


def test_yolo_preapproves_workspace_shell_and_file_actions(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")
    assert ask_approval("web_search", {"query": "public docs"}, cfg)
    assert prompts == []
    assert ask_approval("run_shell", {"command": "pytest -q"}, cfg)
    assert prompts == []
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        assert ask_approval("write_file", {"path": "new.py", "content": "x = 1"}, cfg)
        assert prompts == []
        assert not preflight_runtime_tool("write_file", {"path": "../outside.py"}, cfg).allowed
    finally:
        execution_guardrails.end_execution_scope(scope)


def test_yolo_explicit_forced_review_is_not_preapproved(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")
    assert not ask_approval("run_shell", {"command": "pytest -q"}, cfg, force=True)
    assert len(prompts) == 1
    assert ask_approval("model_delete", {"name": "unused"}, cfg)
    assert len(prompts) == 1


def test_yolo_concurrent_shell_preflights_do_not_share_one_use(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected approval prompt"))
    actions = [{"command": f"pytest -q --maxfail={index}"} for index in range(1, 4)]
    prepared = [preflight_runtime_tool("run_shell", args, cfg) for args in actions]
    assert len({item.policy.grant_id for item in prepared}) == 3
    for args, item in zip(actions, prepared):
        assert ask_approval("run_shell", args, cfg, preflight=item)


@pytest.mark.parametrize("transition", ["child", "copy", "exit", "workspace"])
def test_yolo_shell_preapproval_is_revoked_on_transition(monkeypatch, tmp_path, transition):
    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    args = {"command": "pytest -q"}
    prepared = preflight_runtime_tool("run_shell", args, cfg)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    if transition == "child":
        with session_mode.delegated_scope():
            assert not ask_approval("run_shell", args, cfg, preflight=prepared)
    else:
        if transition == "copy":
            cfg = copy.copy(cfg)
        elif transition == "exit":
            handle_command("/mode explore", cfg, None, user_initiated=True)
        else:
            cfg.cwd = str(tmp_path.parent)
        assert not ask_approval("run_shell", args, cfg, preflight=prepared)


def test_yolo_safe_mode_still_denies_shell_mutation(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), safe_mode=True)
    activate(cfg, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("denial must not prompt"))
    assert not ask_approval("run_shell", {"command": "rm -rf example"}, cfg)
    for command in ("/safe off", "/policy off"):
        assert not preflight_runtime_tool("session_command", {"command": command}, cfg).allowed


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting for real shell fixture")
def test_yolo_real_dispatch_writes_and_verifies_without_approval(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected approval prompt"))
    deps = _dependencies(tmp_path, run_tool)
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        wrote = dispatch_action("write_file", {"path": "made.txt", "content": "verified"},
                                cfg, dependencies=deps, render=False)
        assert wrote.outcome.invoked and wrote.outcome.worked
        assert not execution_guardrails.completion_decision().allowed
        command = shlex.join([sys.executable, "-c",
                              "from pathlib import Path; assert Path('made.txt').read_text() == 'verified'"])
        checked = dispatch_action("run_shell", {"command": command}, cfg,
                                  dependencies=deps, render=False)
        assert checked.outcome.invoked and checked.outcome.worked
        assert "[exit code: 0]" in checked.result
        assert execution_guardrails.completion_decision().allowed
    finally:
        execution_guardrails.end_execution_scope(scope)


def test_yolo_action_program_accepts_version_and_steps_as_sibling_args(monkeypatch, tmp_path):
    from algo_cli import nathan_program_runtime as program_runtime

    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    cfg._algo_program_authorization = program_runtime.authorization_for_actions(("run_shell",))
    args = {
        "version": 1,
        "steps": [
            {
                "id": "v",
                "kind": "action",
                "action": "run_shell",
                "args": {"command": f"{sys.executable} -c \"assert True\""},
            }
        ],
        "outputs": ["v"],
    }
    prepared = preflight_runtime_tool("action_program", args, cfg)
    assert prepared.allowed, prepared.blocked_result


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting for real shell fixture")
@pytest.mark.parametrize("outputs", ["omitted", "null", "empty"])
def test_yolo_real_action_program_shell_defaults_outputs_without_approval(monkeypatch, tmp_path, outputs):
    from algo_cli import nathan_program_runtime as program_runtime

    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    cfg._algo_program_authorization = program_runtime.authorization_for_actions(("run_shell",))
    store = _program_store(tmp_path)
    monkeypatch.setattr(program_runtime, "ProgramArtifactStore", lambda: store)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected approval prompt"))
    command = shlex.join([sys.executable, "-c", "assert 2 + 2 == 4; print('yolo-program-ok')"])
    plan = {"version": 1, "steps": [
        {"id": "s10", "kind": "action", "action": "run_shell", "args": {"command": command}},
    ]}
    if outputs != "omitted":
        plan["outputs"] = None if outputs == "null" else []
    result = dispatch_action("action_program", {"plan": plan}, cfg,
                             dependencies=_dependencies(tmp_path, run_tool), render=False)
    assert result.outcome.invoked and result.outcome.worked, result.result
    payload = json.loads(result.result)
    assert payload["status"] == "worked"
    assert payload["outputs"][0]["reference"] == {"$ref": "s10"}
    assert "yolo-program-ok" in payload["outputs"][0]["preview"]
    assert payload["receipt_count"] == 1


def test_yolo_invalid_program_is_not_mislabeled_as_missing_authority(monkeypatch, tmp_path):
    from algo_cli.nathan_program_runtime import authorization_for_actions

    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    cfg._algo_program_authorization = authorization_for_actions(("read_file",))
    args = {"plan": {"version": 1, "steps": [
        {"id": "read", "kind": "action", "action": "read_file", "args": {"path": "REPORT.md"}},
    ], "outputs": "wrong type"}}
    prepared = preflight_runtime_tool("action_program", args, cfg)
    assert not prepared.allowed
    assert prepared.blocked_result.startswith("Invalid action program:")
    assert "ProgramValidationError" in prepared.blocked_result
    ceiling = preflight_runtime_tool("action_program", args, cfg, policy_ceiling_code="agent_tool_not_allowed")
    assert not ceiling.allowed
    assert ceiling.blocked_result.startswith("Blocked by runtime authority:")


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting for real shell fixture")
def test_yolo_real_program_write_and_verify_with_redundant_cwd(monkeypatch, tmp_path):
    from algo_cli import nathan_program_runtime as program_runtime

    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    cfg._algo_program_authorization = program_runtime.authorization_for_actions(("write_file", "run_shell"))
    store = _program_store(tmp_path)
    monkeypatch.setattr(program_runtime, "ProgramArtifactStore", lambda: store)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected approval prompt"))
    deps = _dependencies(tmp_path, run_tool)
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        write = {"version": 1, "steps": [{"id": "write_s10", "kind": "action", "action": "write_file",
                 "args": {"path": "experiment.py", "content": "fixture = 10\n", "cwd": str(tmp_path)}}], "outputs": []}
        wrote = dispatch_action("action_program", {"plan": write}, cfg, dependencies=deps, render=False)
        assert wrote.outcome.invoked and wrote.outcome.worked, wrote.result
        assert not execution_guardrails.completion_decision().allowed
        command = shlex.join([sys.executable, "-c",
                              "from pathlib import Path; assert Path('experiment.py').read_text() == 'fixture = 10\\n'"])
        verify = {"version": 1, "steps": [{"id": "verify_s10", "kind": "action", "action": "run_shell",
                  "args": {"command": command, "cwd": str(tmp_path)}}], "outputs": []}
        checked = dispatch_action("action_program", {"plan": verify}, cfg, dependencies=deps, render=False)
        assert checked.outcome.invoked and checked.outcome.worked, checked.result
        assert execution_guardrails.completion_decision().allowed
    finally:
        execution_guardrails.end_execution_scope(scope)


@pytest.mark.parametrize("path_style", ["absolute", "relative", "home"])
def test_yolo_real_dispatch_observes_sibling_workspace_without_approval(monkeypatch, tmp_path, capsys, path_style):
    workspace = tmp_path / "current"
    sibling = tmp_path / "sibling" / "runs"
    workspace.mkdir()
    sibling.mkdir(parents=True)
    report = sibling / "REPORT.md"
    report.write_text("qualified sibling report\n")
    cfg = Config(cwd=str(workspace))
    activate(cfg, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected approval prompt"))
    if path_style == "absolute":
        path = str(sibling)
    elif path_style == "relative":
        path = "../sibling/runs"
    else:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        path = "~/sibling/runs"
    deps = _dependencies(tmp_path, run_tool)
    for name, args, expected in (
        ("read_file", {"path": f"{path}/REPORT.md"}, "qualified sibling report"),
        ("list_directory", {"path": path}, "REPORT.md"),
        ("search_files", {"path": path, "pattern": "qualified sibling"}, "qualified sibling report"),
        ("session_slash", {"command": f"/read {path}/REPORT.md"}, "qualified sibling report"),
        ("session_command", {"command": f"/ls {path}"}, "REPORT.md"),
    ):
        result = dispatch_action(name, args, cfg, dependencies=deps, render=False)
        assert result.outcome.invoked and result.outcome.worked, result.result
        observed = capsys.readouterr().out if name == "session_command" else result.result
        assert expected in observed
    assert report.read_text() == "qualified sibling report\n"
    assert cfg.cwd == str(workspace)


@pytest.mark.parametrize("mode", ["explore", "execute", "publish", "yolo"])
def test_external_observation_requires_live_yolo_owner(tmp_path, mode):
    cfg = Config(cwd=str(tmp_path / "current"), session_mode=mode)
    assert not preflight_runtime_tool("read_file", {"path": "../REPORT.md"}, cfg).allowed
    assert not preflight_runtime_tool("list_directory", {"path": ".."}, cfg).allowed


@pytest.mark.parametrize("transition", ["child", "copy", "exit", "workspace"])
def test_yolo_external_observation_grant_is_revoked(monkeypatch, tmp_path, transition):
    cfg = Config(cwd=str(tmp_path / "current"))
    activate(cfg, monkeypatch)
    args = {"path": str(tmp_path / "sibling" / "REPORT.md")}
    prepared = preflight_runtime_tool("read_file", args, cfg)
    assert prepared.allowed
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("denial must not prompt"))
    if transition == "child":
        with session_mode.delegated_scope():
            assert not ask_approval("read_file", args, cfg, preflight=prepared)
    else:
        if transition == "copy":
            cfg = copy.copy(cfg)
        elif transition == "exit":
            handle_command("/mode explore", cfg, None, user_initiated=True)
        else:
            cfg.cwd = str(tmp_path)
        assert not ask_approval("read_file", args, cfg, preflight=prepared)


def test_yolo_external_observations_have_independent_one_use_grants(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path / "current"))
    activate(cfg, monkeypatch)
    args = {"path": str(tmp_path / "sibling" / "REPORT.md")}
    first = preflight_runtime_tool("read_file", args, cfg)
    second = preflight_runtime_tool("read_file", args, cfg)
    assert first.allowed and second.allowed
    assert first.policy.grant_id != second.policy.grant_id
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected approval prompt"))
    assert ask_approval("read_file", args, cfg, preflight=first)
    assert not ask_approval("read_file", args, cfg, preflight=first)
    assert ask_approval("read_file", args, cfg, preflight=second)


def test_yolo_external_observation_keeps_sensitive_and_caller_ceilings(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path / "current"))
    activate(cfg, monkeypatch)
    for path in ("../.ssh/id_ed25519", "../.env", "../credentials.json"):
        assert not preflight_runtime_tool("read_file", {"path": path}, cfg).allowed
    assert not preflight_runtime_tool("list_directory", {"path": "../.ssh"}, cfg).allowed
    assert not preflight_runtime_tool("search_files", {"path": "../.ssh", "pattern": "key"}, cfg).allowed
    args = {"path": "../REPORT.md"}
    assert not preflight_runtime_tool("read_file", args, cfg, policy_ceiling_code="agent_tool_not_allowed").allowed
    assert not ask_approval("read_file", {"path": "../OTHER.md"}, cfg,
                            preflight=preflight_runtime_tool("read_file", args, cfg))
    assert not preflight_runtime_tool("write_file", {"path": "../outside.py", "content": "x = 1"}, cfg).allowed


@pytest.mark.skipif(os.name == "nt", reason="symlink privileges vary on Windows")
def test_yolo_external_symlink_to_sensitive_path_stays_denied(monkeypatch, tmp_path):
    private = tmp_path / ".ssh"
    private.mkdir()
    (private / "id_ed25519").write_text("fixture, not a credential")
    (tmp_path / "alias").symlink_to(private, target_is_directory=True)
    cfg = Config(cwd=str(tmp_path / "current"))
    activate(cfg, monkeypatch)
    assert not preflight_runtime_tool("read_file", {"path": "../alias/id_ed25519"}, cfg).allowed
    assert not preflight_runtime_tool("list_directory", {"path": "../alias"}, cfg).allowed


def test_yolo_keeps_echo_shell_and_credential_denials(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), continuum_enabled=True)
    activate(cfg, monkeypatch)
    shell = preflight_runtime_tool("run_shell", {"command": "pytest -q"}, cfg)
    assert not shell.allowed
    assert "Continuum Memory" in shell.blocked_result
    for name in ("credential_helpers_get", "credential_helpers_store", "send_email"):
        assert not preflight_runtime_tool(name, {}, cfg).allowed
    assert not preflight_runtime_tool("read_file", {"path": str(config.CONFIG_DIR / "memory.json")}, cfg).allowed


def test_yolo_typed_tools_cannot_read_or_write_sensitive_paths(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    activate(cfg, monkeypatch)
    invoked = []
    monkeypatch.setitem(tools.TOOL_MAP, "read_file", lambda **kwargs: invoked.append(kwargs) or "private")
    for name, args in (
        ("read_file", {"path": ".env"}),
        ("read_file", {"path": ".git/config"}),
        ("session_slash", {"command": "/read .env"}),
        ("session_command", {"command": "/read .env"}),
        ("write_file", {"path": "credentials.json", "content": "private"}),
    ):
        assert not preflight_runtime_tool(name, args, cfg).allowed
        assert run_tool(name, args, cfg).startswith("Error:")
    assert invoked == []


def test_completion_gate_still_requires_verifier_in_yolo(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    activate(cfg, monkeypatch)
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        execution_guardrails.record_mutation("new.py", success=True, operation="write_file")
        assert not execution_guardrails.completion_decision().allowed
        execution_guardrails.record_shell_verification("pytest -q", returncode=0)
        assert execution_guardrails.completion_decision().allowed
    finally:
        execution_guardrails.end_execution_scope(scope)


def test_b51_explicit_empty_permissions_are_not_replaced_by_defaults():
    mode = PermissionMode(PermissionLevel.ELEVATED, allowed_tools=set(), spawnable_modes=set())
    assert not mode.can_use_tool("write_file")
    assert not mode.can_spawn(PermissionLevel.READ_ONLY)


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting for real shell fixture")
def test_yolo_refused_overwrite_less_write_is_denied_not_unknown(monkeypatch, tmp_path):
    """A clean 'already exists' refusal must stay a typed denial, never unknown."""
    from algo_cli.arthur_outcomes import OutcomeStatus

    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected approval prompt"))
    deps = _dependencies(tmp_path, run_tool)
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        (tmp_path / "s10_metadata.py").write_text("original", encoding="utf-8")
        refused = dispatch_action(
            "write_file",
            {"path": "s10_metadata.py", "content": "corrected"},
            cfg,
            dependencies=deps,
            render=False,
        )
        assert refused.outcome.status is OutcomeStatus.DENIED
        assert not refused.outcome.invoked
        assert "already exists" in refused.result
        assert "overwrite=true" in refused.result
        assert "Unknown outcome" not in refused.result
        assert "action-time confirmation" not in refused.result
        assert all(item["status"] != "unknown_outcome" for item in cfg.attempt_ledger)
        # The same refused call again is still a clean denial, not a skip barrier.
        again = dispatch_action(
            "write_file",
            {"path": "s10_metadata.py", "content": "corrected"},
            cfg,
            dependencies=deps,
            render=False,
        )
        assert again.outcome.status is OutcomeStatus.DENIED
        # Overwrite after a successful same-file read completes the workflow.
        read = dispatch_action("read_file", {"path": "s10_metadata.py"}, cfg,
                               dependencies=deps, render=False)
        assert read.outcome.worked
        overwrote = dispatch_action(
            "write_file",
            {"path": "s10_metadata.py", "content": "corrected", "overwrite": True},
            cfg,
            dependencies=deps,
            render=False,
        )
        assert overwrote.outcome.invoked and overwrote.outcome.worked, overwrote.result
        assert (tmp_path / "s10_metadata.py").read_text(encoding="utf-8") == "corrected"
    finally:
        execution_guardrails.end_execution_scope(scope)


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting for real shell fixture")
def test_yolo_denied_program_is_not_relabeled_unknown(monkeypatch, tmp_path):
    """A program that reports 'denied' must keep that status at the outer dispatch."""
    from algo_cli import nathan_program_runtime as program_runtime
    from algo_cli.arthur_outcomes import OutcomeStatus

    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    cfg._algo_program_authorization = program_runtime.authorization_for_actions(("write_file",))
    store = _program_store(tmp_path)
    monkeypatch.setattr(program_runtime, "ProgramArtifactStore", lambda: store)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected approval prompt"))
    deps = _dependencies(tmp_path, run_tool)
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        (tmp_path / "s10_metadata.py").write_text("original", encoding="utf-8")
        plan = {"version": 1, "steps": [
            {"id": "fix_script", "kind": "action", "action": "write_file",
             "args": {"path": "s10_metadata.py", "content": "corrected"}},
        ], "outputs": []}
        denied = dispatch_action("action_program", {"plan": plan}, cfg,
                                 dependencies=deps, render=False)
        payload = json.loads(denied.result)
        assert payload["status"] == "denied"
        assert denied.outcome.status is OutcomeStatus.DENIED
        assert "Unknown outcome" not in denied.result
        # Retrying the same plan stays a denial (no unknown/skip barrier).
        retried = dispatch_action("action_program", {"plan": plan}, cfg,
                                  dependencies=deps, render=False)
        assert retried.outcome.status is OutcomeStatus.DENIED
        assert "Unknown outcome" not in retried.result
    finally:
        execution_guardrails.end_execution_scope(scope)


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting for real shell fixture")
def test_yolo_interrupted_workflow_recovers_after_fresh_observation(monkeypatch, tmp_path):
    """The interrupted -> continue -> yolo workflow must complete end to end.

    Mirrors the 2026-09-16 codec-research-fork session: an interrupted program
    left uncertain run_shell/program attempts; the recovery session must be able
    to reconcile them with a fresh observation and finish the experiment.
    """
    from algo_cli import nathan_program_runtime as program_runtime
    from algo_cli.arthur_outcomes import OutcomeStatus
    from algo_cli.nathan_runtime import (
        find_failed_attempt,
        record_tool_attempt,
        tool_attempt_signature,
        tool_runtime_args,
    )

    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    cfg._algo_program_authorization = program_runtime.authorization_for_actions(("write_file", "run_shell"))
    store = _program_store(tmp_path)
    monkeypatch.setattr(program_runtime, "ProgramArtifactStore", lambda: store)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected approval prompt"))
    deps = _dependencies(tmp_path, run_tool)
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        script = (tmp_path / "s10_metadata.py")
        script.write_text("print('chain ok')", encoding="utf-8")
        shell_args = tool_runtime_args(
            "run_shell", {"command": f"{sys.executable} s10_metadata.py", "cwd": str(tmp_path)}, cfg
        )
        shell_signature = tool_attempt_signature("run_shell", shell_args)
        # 1. The interrupted prior generation left genuinely uncertain attempts.
        record_tool_attempt(cfg, name="run_shell", args=shell_args,
                             result="status=unknown_outcome", status="unknown_outcome",
                             retry_allowed=False, invoked=True)
        assert find_failed_attempt(cfg, shell_signature) is not None
        # 2. The same shell action is still skipped until reconciled.
        skipped = dispatch_action("run_shell", {"command": f"{sys.executable} s10_metadata.py",
                                                 "cwd": str(tmp_path)}, cfg,
                                  dependencies=deps, render=False)
        assert skipped.outcome.status is OutcomeStatus.SKIPPED
        assert "unresolved outcome" in skipped.result
        # 3. A fresh observation of the affected workspace reconciles it.
        read = dispatch_action("read_file", {"path": "s10_metadata.py"}, cfg,
                               dependencies=deps, render=False)
        assert read.outcome.worked
        assert find_failed_attempt(cfg, shell_signature) is None
        ran = dispatch_action("run_shell", {"command": f"{sys.executable} s10_metadata.py",
                                             "cwd": str(tmp_path)}, cfg,
                              dependencies=deps, render=False)
        assert ran.outcome.invoked and ran.outcome.worked, ran.result
        # 4. An interrupted program entry reconciles through its step targets.
        plan = {"version": 1, "steps": [
            {"id": "run_s10", "kind": "action", "action": "run_shell",
             "args": {"command": f"{sys.executable} s10_metadata.py"}},
        ], "outputs": []}
        record_tool_attempt(cfg, name="action_program", args={"plan": plan},
                             result="status=unknown_outcome", status="unknown_outcome",
                             retry_allowed=False, invoked=True)
        program_signature = tool_attempt_signature("action_program", {"plan": plan})
        assert find_failed_attempt(cfg, program_signature) is not None
        listed = dispatch_action("list_directory", {"path": "."}, cfg,
                                 dependencies=deps, render=False)
        assert listed.outcome.worked
        assert find_failed_attempt(cfg, program_signature) is None
        finished = dispatch_action("action_program", {"plan": plan}, cfg,
                                   dependencies=deps, render=False)
        assert finished.outcome.invoked and finished.outcome.worked, finished.result
        assert json.loads(finished.result)["status"] == "worked"
    finally:
        execution_guardrails.end_execution_scope(scope)


@pytest.mark.skipif(os.name == "nt", reason="POSIX quoting for real shell fixture")
def test_yolo_write_overwrite_block_message_names_only_the_real_guardrail(monkeypatch, tmp_path):
    """A guardrail block must not blame pending confirmation in yolo mode."""
    cfg = Config(cwd=str(tmp_path), safe_mode=False)
    activate(cfg, monkeypatch)
    (tmp_path / "draft.txt").write_text("original", encoding="utf-8")
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        prepared = preflight_runtime_tool(
            "write_file",
            {"path": "draft.txt", "content": "new", "overwrite": True},
            cfg,
        )
        assert not prepared.allowed
        assert prepared.blocked_result == (
            "Blocked by runtime authority: edit requires a successful same-file read."
        )
    finally:
        execution_guardrails.end_execution_scope(scope)


def test_yolo_work_ceiling_is_unbounded_and_not_inherited(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), max_tool_iterations=2)
    assert session_mode.work_iteration_limit(cfg) == 2
    activate(cfg, monkeypatch)
    assert session_mode.work_iteration_limit(cfg) is None
    assert session_mode.work_iteration_label(cfg) == "unlimited"
    assert session_mode.completion_recovery_limit(cfg, bounded_default=4) is None
    assert cfg.max_tool_iterations == 2
    with session_mode.delegated_scope():
        assert session_mode.active_mode(cfg) == "explore"
        assert session_mode.work_iteration_limit(cfg) == 2
        assert session_mode.completion_recovery_limit(cfg, bounded_default=4) == 4
    handle_command("/mode explore", cfg, None, user_initiated=True)
    assert session_mode.work_iteration_limit(cfg) == 2
    assert session_mode.completion_recovery_limit(cfg, bounded_default=4) == 4


def test_yolo_chat_continues_past_saved_toolmax(monkeypatch, tmp_path):
    from test_agent_progress_recovery import call, run_script

    def responses(turn):
        if turn <= 6:
            return call("read_file", {"path": f"note-{turn}.txt"}, turn)
        return {"content": "Finished after extra YOLO rounds."}

    def configure(cfg):
        cfg.max_tool_iterations = 2
        activate(cfg, monkeypatch)

    code, events, client, invoked, captures = run_script(
        monkeypatch,
        tmp_path,
        responses,
        max_iterations=2,
        configure=configure,
        invoke=lambda name, _args, _cfg: "ok",
        approve=lambda *_args, **_kwargs: True,
        safe_mode=False,
    )
    assert code == 0
    assert len(client.calls) == 7
    assert invoked == ["read_file"] * 6
    assert captures[-1]["completed"] is True
    assert not any("[Internal finalization turn]" in str(message.get("content") or "")
                   for request in client.calls for message in request["messages"])


def test_yolo_verification_recovery_continues_past_the_ordinary_round_cap(monkeypatch, tmp_path):
    from test_agent_progress_recovery import call, run_script
    from test_nathan_verification_recovery import _invoke

    def responses(turn):
        if turn == 1:
            return call("write_file", {"path": "made.py", "content": "x = 1\n"}, turn)
        if turn == 10:
            return call("run_shell", {"command": "pytest -q"}, turn)
        if turn == 11:
            return {"content": "The test passed."}
        return {"content": "Premature completion claim."}

    def configure(cfg):
        activate(cfg, monkeypatch)

    code, events, client, invoked, captures = run_script(
        monkeypatch,
        tmp_path,
        responses,
        configure=configure,
        invoke=_invoke,
        approve=lambda *_args, **_kwargs: True,
        safe_mode=False,
    )
    assert code == 0
    assert len(client.calls) == 11
    assert invoked[-1] == "run_shell"
    assert captures[-1]["completed"] is True
    assert not any("verification recovery budget ended" in str(event).lower() for event in events)


def test_yolo_survives_session_mode_module_reload(monkeypatch, tmp_path):
    import importlib
    import sys

    cfg = Config(cwd=str(tmp_path))
    activate(cfg, monkeypatch)
    importlib.reload(sys.modules["algo_cli.session_mode"])
    live = sys.modules["algo_cli.session_mode"]
    assert live.active_mode(cfg) == "yolo"
    assert live.unlimited_work(cfg)
    assert cfg.auto_approve_active


def test_reload_keeps_live_yolo_after_session_mode_reload(monkeypatch, tmp_path):
    import importlib
    import sys

    from algo_cli import main

    cfg = Config(cwd=str(tmp_path), session_mode="execute", model="old-model")
    activate(cfg, monkeypatch)
    loaded = Config(cwd=str(tmp_path), session_mode="execute", model="new-model", cloud=True)

    def fake_reload():
        importlib.reload(sys.modules["algo_cli.session_mode"])
        return loaded

    monkeypatch.setattr(main, "reload_runtime", fake_reload)
    monkeypatch.setattr(main, "create_client", lambda _cfg: object())
    monkeypatch.setattr(main, "show_info", lambda _msg: None)
    monkeypatch.setattr(main, "show_error", lambda _msg: None)

    handled, _client = handle_command("/reload", cfg, object(), user_initiated=True)
    live = sys.modules["algo_cli.session_mode"]
    assert handled is True
    assert cfg.model == "new-model"
    assert live.active_mode(cfg) == "yolo"
    assert live.unlimited_work(cfg)
    assert live.work_iteration_label(cfg) == "unlimited"
    assert live.persisted_mode(cfg) == "execute"
    assert cfg.auto_approve_active


def test_reload_without_yolo_still_copies_session_mode(monkeypatch, tmp_path):
    from algo_cli import main

    cfg = Config(cwd=str(tmp_path), session_mode="explore")
    loaded = Config(cwd=str(tmp_path), session_mode="execute")
    monkeypatch.setattr(Config, "save", lambda self: None)
    monkeypatch.setattr(main, "reload_runtime", lambda: loaded)
    monkeypatch.setattr(main, "create_client", lambda _cfg: object())
    monkeypatch.setattr(main, "show_info", lambda _msg: None)

    handled, _client = handle_command("/reload", cfg, object(), user_initiated=True)
    assert handled is True
    assert session_mode.active_mode(cfg) == "execute"


def test_yolo_run_contract_does_not_inherit_the_ordinary_iteration_cap(monkeypatch, tmp_path):
    from algo_cli import agent_blocks, run_contract, task_router

    cfg = Config(cwd=str(tmp_path), max_tool_iterations=2, algorithmic_tool_policy_enabled=False)
    activate(cfg, monkeypatch)
    contract = run_contract.compile_agent_run_contract(
        task="inspect the workspace",
        route=task_router.route_task("inspect the workspace"),
        pipeline_name="implement",
        blocks=[
            agent_blocks.AgentBlock(
                role="implement",
                prompt="do the work",
                allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
                max_iterations=8,
                requires_change=True,
            )
        ],
        cfg=cfg,
        approval_mode="auto",
    )
    assert contract.budget.max_iterations_per_block == run_contract._YOLO_MAX_ITERATIONS_PER_BLOCK
    assert contract.budget.max_tool_calls == run_contract._YOLO_MAX_TOOL_CALLS
    assert contract.blocks[0].max_iterations == run_contract._YOLO_MAX_ITERATIONS_PER_BLOCK
