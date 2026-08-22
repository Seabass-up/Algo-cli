from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time

from algo_cli.config import Config
from algo_cli.marcus_authority import ConfirmationMode
from algo_cli.nathan_runtime import (
    RuntimeAuthoritySession,
    ask_approval,
    authority_session_for,
    mutation_failure_proves_no_effect,
    preflight_runtime_tool,
)
from algo_cli.samuel_policy_engine import PolicyDisposition, resolve_action


def test_baseline_authority_is_read_only_and_workspace_bounded(tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path))
    inside = preflight_runtime_tool("read_file", {"path": "README.md"}, cfg)
    outside = preflight_runtime_tool("read_file", {"path": "../secret.txt"}, cfg)

    assert inside.policy.disposition is PolicyDisposition.ALLOW
    assert ask_approval("read_file", {"path": "README.md"}, cfg, preflight=inside) is True
    assert outside.policy.disposition is PolicyDisposition.DENY
    assert ask_approval("read_file", {"path": "../secret.txt"}, cfg, preflight=outside) is False

    slash_inside = preflight_runtime_tool("session_slash", {"command": "/read README.md"}, cfg)
    slash_outside = preflight_runtime_tool("session_slash", {"command": "/read ../secret.txt"}, cfg)
    assert slash_inside.policy.disposition is PolicyDisposition.ALLOW
    assert slash_outside.policy.disposition is PolicyDisposition.DENY


def test_runtime_grant_is_exact_target_exact_action_and_one_use(tmp_path) -> None:
    now = time.time()
    session = RuntimeAuthoritySession(str(tmp_path))
    action = resolve_action("read_file", {"path": "one.txt"}, cwd=str(tmp_path))
    other_target = resolve_action("read_file", {"path": "two.txt"}, cwd=str(tmp_path))
    other_action = resolve_action("read_pdf", {"path": "one.txt"}, cwd=str(tmp_path))
    grant = session.issue(action, source="test", now=now)

    assert session.matching_grant(action, now) is not None
    assert session.matching_grant(other_target, now) is None
    assert session.matching_grant(other_action, now) is None
    assert session.consume(grant.grant_id, now) is True
    assert session.consume(grant.grant_id, now) is False


def test_grant_consumption_is_atomic_under_race(tmp_path) -> None:
    now = time.time()
    session = RuntimeAuthoritySession(str(tmp_path))
    action = resolve_action("read_file", {"path": "one.txt"}, cwd=str(tmp_path))
    grant = session.issue(action, source="test", now=now)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _index: session.consume(grant.grant_id, now), range(8)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7


def test_parallel_baseline_reads_do_not_spuriously_consume_each_other(tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path))

    def authorize(index: int) -> bool:
        args = {"path": f"file-{index}.txt"}
        preflight = preflight_runtime_tool("read_file", args, cfg)
        return ask_approval("read_file", args, cfg, preflight=preflight)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(authorize, range(32)))

    assert outcomes == [True] * 32


def test_interactive_auto_still_prompts_for_action_time(monkeypatch, tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")

    assert ask_approval("remember", {"fact": "bounded"}, cfg) is False
    assert len(prompts) == 1


def test_noninteractive_auto_only_preapproves_session_mode(monkeypatch, tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    setattr(cfg, "_nathan_approval_mode", "auto")
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("noninteractive mode prompted")),
    )

    assert ask_approval("web_search", {"query": "public docs"}, cfg) is True
    assert ask_approval("remember", {"fact": "must confirm"}, cfg) is False
    assert ask_approval("plugins_load", {"plugin_name": "demo"}, cfg) is False


def test_explicit_workspace_process_authority_is_local_and_curated(monkeypatch, tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path), auto_mode=False)
    setattr(cfg, "_nathan_approval_mode", "workspace")
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("workspace mode prompted")),
    )

    assert ask_approval("run_shell", {"command": "python3 -m pytest"}, cfg) is True
    assert ask_approval("remember", {"fact": "must confirm"}, cfg) is False
    assert ask_approval("web_search", {"query": "public docs"}, cfg) is False
    assert ask_approval("plugins_load", {"plugin_name": "demo"}, cfg) is False
    assert ask_approval("session_command", {"command": "/safe off"}, cfg) is False


def test_only_closed_workspace_adapter_preconditions_prove_no_effect(tmp_path) -> None:
    assert mutation_failure_proves_no_effect(
        "edit_file",
        f"Error: old_string not found in {tmp_path / 'file.txt'}. No match.",
    )
    assert mutation_failure_proves_no_effect(
        "write_file",
        f"Error: {tmp_path / 'file.txt'} already exists. Re-run with overwrite=true if intended.",
    )
    assert not mutation_failure_proves_no_effect("run_shell", "STDERR: failed\n[exit code: 1]")
    assert not mutation_failure_proves_no_effect("batch_edit", "Error: partial batch failure")
    assert not mutation_failure_proves_no_effect("remember", "Tool argument error for remember: bad")


def test_exact_session_subcommand_controls_confirmation(monkeypatch, tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")

    safe = preflight_runtime_tool("session_command", {"command": "/status"}, cfg)
    risky = preflight_runtime_tool("session_command", {"command": "/safe off"}, cfg)
    assert safe.policy.action.confirmation_mode is ConfirmationMode.NONE
    assert ask_approval("session_command", {"command": "/status"}, cfg, preflight=safe) is True
    assert risky.policy.action.confirmation_mode is ConfirmationMode.ACTION_TIME
    assert ask_approval("session_command", {"command": "/safe off"}, cfg, preflight=risky) is False
    assert len(prompts) == 1


def test_force_elevates_a_baseline_read_to_action_time(monkeypatch, tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")
    preflight = preflight_runtime_tool("read_file", {"path": "README.md"}, cfg)

    assert (
        ask_approval(
            "read_file",
            {"path": "README.md"},
            cfg,
            force=True,
            preflight=preflight,
        )
        is False
    )
    assert len(prompts) == 1


def test_preflight_cannot_be_reused_for_different_args_action_or_cwd(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    cfg = Config(cwd=str(first))
    preflight = preflight_runtime_tool("read_file", {"path": "one.txt"}, cfg)

    assert ask_approval("read_file", {"path": "two.txt"}, cfg, preflight=preflight) is False
    assert ask_approval("web_search", {"query": "one"}, cfg, preflight=preflight) is False
    cfg.cwd = str(second)
    assert ask_approval("read_file", {"path": "one.txt"}, cfg, preflight=preflight) is False


def test_authority_session_is_replaced_when_workspace_changes(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    cfg = Config(cwd=str(first))
    original = authority_session_for(cfg)

    cfg.cwd = str(second)
    replacement = authority_session_for(cfg)

    assert replacement is not original
    assert replacement.workspace_root == second.resolve()
