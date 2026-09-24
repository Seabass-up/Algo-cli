"""Denial explanations, same-turn repeat blocking, per-action stalls, and embed-pass observability."""

from __future__ import annotations

import json
from dataclasses import replace

from algo_cli import harness, james_dispatch, main
from algo_cli.config import Config
from algo_cli.marcus_authority import ConfirmationMode, TargetScope
from algo_cli.nathan_runtime import (
    ask_approval,
    begin_tool_turn,
    end_tool_turn,
    explain_missing_grant,
    preflight_runtime_tool,
    tool_attempt_signature,
    turn_denial,
)
from algo_cli.samuel_policy_engine import PolicyDisposition, resolve_action
from test_agent_progress_recovery import call, run_script


# ---------- index 1: jev_kernel_status baseline and actionable denials ----------


def test_jev_kernel_status_is_baseline_allowed_without_a_prompt(tmp_path, monkeypatch) -> None:
    cfg = Config(cwd=str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda *_args: (_ for _ in ()).throw(AssertionError("prompted")))

    action = resolve_action("jev_kernel_status", {}, cwd=cfg.cwd)
    preflight = preflight_runtime_tool("jev_kernel_status", {}, cfg)

    assert action.target_scope is TargetScope.RUNTIME
    assert action.target == "runtime:jev_kernel_status"
    assert action.confirmation_mode is ConfirmationMode.NONE
    assert preflight.policy.disposition is PolicyDisposition.ALLOW
    assert ask_approval("jev_kernel_status", {}, cfg, preflight=preflight) is True

    setattr(cfg, "_nathan_approval_mode", "auto")
    auto = preflight_runtime_tool("jev_kernel_status", {}, cfg)
    assert auto.policy.disposition is PolicyDisposition.ALLOW


def test_jev_question_contract_stays_approval_gated(tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path))
    args = {"contract": {}, "mode": "run"}

    action = resolve_action("jev_question_contract", args, cwd=cfg.cwd)
    preflight = preflight_runtime_tool("jev_question_contract", args, cfg)

    assert action.target == "provider:typesafe:jev"
    assert action.confirmation_mode is ConfirmationMode.SESSION_PREAPPROVAL
    assert preflight.policy.disposition is PolicyDisposition.CONFIRM
    assert not cfg.auto_approve_active
    assert ask_approval("jev_question_contract", args, cfg, preflight=preflight) is False


def test_outside_workspace_read_denial_names_target_scope_and_recovery(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "docs" / "GUIDE.md"
    cfg = Config(cwd=str(workspace))

    preflight = preflight_runtime_tool("read_file", {"path": str(outside)}, cfg)
    text = preflight.blocked_result

    assert preflight.policy.disposition is PolicyDisposition.DENY
    assert preflight.denial is not None
    assert preflight.denial.kind == "intentional_policy"
    assert preflight.resolvable_in_session is True
    assert text.startswith("Blocked by runtime authority: read_file on workspace:")
    assert str(outside.resolve()) in text
    assert f"outside the session workspace {workspace.resolve()}" in text
    assert "intentional containment policy" in text
    assert "Resolvable in this session: yes, by the user" in text
    assert "/mode yolo" in text and "/cd" in text
    assert "Do not retry" in text
    assert not text.endswith("..")
    assert "no scoped capability grant authorizes the action" not in text


def test_uncurated_no_confirmation_action_is_reported_as_setup_gap(tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path))
    # Lint mode is a no-confirmation read outside the runtime baseline.
    preflight = preflight_runtime_tool("jev_question_contract", {"contract": {}}, cfg)

    assert preflight.policy.disposition is PolicyDisposition.DENY
    assert preflight.denial is not None
    assert preflight.denial.kind == "setup_gap"
    assert preflight.resolvable_in_session is False
    assert "not in the runtime baseline allowlist" in preflight.blocked_result
    assert "Resolvable in this session: no" in preflight.blocked_result

    status = resolve_action("jev_kernel_status", {}, cwd=cfg.cwd)
    provider_scoped = explain_missing_grant(cfg, replace(status, target_scope=TargetScope.PROVIDER))
    assert provider_scoped.kind == "setup_gap"
    assert "target scope provider is outside the runtime baseline scopes" in provider_scoped.text()


def test_generic_policy_denial_names_action_and_target(tmp_path) -> None:
    from algo_cli.samuel_policy_engine import evaluate_action

    action = resolve_action("read_file", {"path": "x.txt"}, cwd=str(tmp_path))
    decision = evaluate_action(action, grant=None, confirmation=None, now=0.0)

    assert decision.disposition is PolicyDisposition.DENY
    assert decision.reason == f"no scoped capability grant authorizes read_file on {action.target}"


# ---------- index 2: same-turn repeat blocking and per-action progress ----------


def _dispatch_with_approver(cfg, approvals: list[str], answer: bool):
    deps = james_dispatch.default_dispatch_dependencies()

    def approve(name, _args, _cfg, **_kwargs):
        approvals.append(name)
        return answer

    def invoke(_name, _args, _cfg):
        raise AssertionError("a denied action must not be invoked")

    deps.approve = approve
    deps.invoke = invoke
    return lambda: james_dispatch.dispatch_action(
        "run_shell", {"command": "python -V"}, cfg, dependencies=deps, render=False
    )


def test_identical_approval_denied_call_is_skipped_without_a_second_prompt(tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path))
    approvals: list[str] = []
    dispatch = _dispatch_with_approver(cfg, approvals, answer=False)

    begin_tool_turn(cfg)
    first = dispatch()
    second = dispatch()

    assert first.status == "denied"
    assert first.result == "This operation was not approved and was not executed."
    assert second.status == "skipped"
    assert "cannot be resolved in this session" in second.result
    assert "Do not retry" in second.result
    assert approvals == ["run_shell"]
    signature = tool_attempt_signature("run_shell", first.preflight.signature_args)
    explanation = turn_denial(cfg, signature)
    assert explanation is not None and explanation.kind == "user_declined"
    assert explanation.resolvable_in_session is False

    # A later turn may ask again; the block is scoped to one turn only.
    end_tool_turn(cfg)
    begin_tool_turn(cfg)
    third = dispatch()
    assert third.status == "denied"
    assert approvals == ["run_shell", "run_shell"]


def test_repeated_policy_denial_says_do_not_retry_only_within_a_turn(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = Config(cwd=str(workspace))
    args = {"path": str(tmp_path / "outside.md")}

    outside_turn = preflight_runtime_tool("read_file", args, cfg)
    assert outside_turn.blocked_result.startswith("Blocked by runtime authority:")

    begin_tool_turn(cfg)
    first = preflight_runtime_tool("read_file", args, cfg)
    repeat = preflight_runtime_tool("read_file", args, cfg)

    assert first.repeated_denial is False
    assert first.blocked_result.startswith("Blocked by runtime authority:")
    assert repeat.repeated_denial is True
    assert repeat.blocked_result.startswith("Skipped repeated denied action")
    assert "cannot be resolved in this session by retrying. Do not retry" in repeat.blocked_result
    end_tool_turn(cfg)


def test_repeated_denied_call_beside_approved_reads_trips_the_stall_guard(monkeypatch, tmp_path) -> None:
    approvals: list[str] = []

    def approve(name, args, cfg, **kwargs):
        approvals.append(name)
        return name == "read_file"

    def responses(turn):
        return {
            "tool_calls": [
                {"id": f"read-{turn}", "function": {"name": "read_file", "arguments": {"path": "README.md"}}},
                {"id": f"shell-{turn}", "function": {"name": "run_shell", "arguments": {"command": "python -V"}}},
            ]
        }

    code, events, client, invoked, captures = run_script(
        monkeypatch, tmp_path, responses, max_iterations=8, approve=approve
    )

    assert code == 2
    assert len(client.calls) == 3
    assert invoked == ["read_file", "read_file", "read_file"]
    assert approvals.count("run_shell") == 1
    assert captures[-1]["completed"] is False
    errors = [event for event in events if event["type"] == "error"]
    assert any("run_shell was blocked 3 times in this turn" in json.dumps(event) for event in errors)
    second_request = json.dumps(client.calls[1]["messages"])
    assert "Some requested actions were blocked before execution: run_shell" in second_request
    assert json.dumps(client.calls[2]["messages"]).count("Some requested actions were blocked") == 1


def test_distinct_single_denials_do_not_stop_a_productive_turn(monkeypatch, tmp_path) -> None:
    def approve(name, args, cfg, **kwargs):
        return name == "read_file"

    def responses(turn):
        if turn == 4:
            return {"content": "Done reading; shell needs approval."}
        return {
            "tool_calls": [
                {"id": f"read-{turn}", "function": {"name": "read_file", "arguments": {"path": "README.md"}}},
                {
                    "id": f"shell-{turn}",
                    "function": {"name": "run_shell", "arguments": {"command": f"python -V # {turn}"}},
                },
            ]
        }

    code, _events, client, invoked, captures = run_script(monkeypatch, tmp_path, responses, approve=approve)

    assert code == 0
    assert len(client.calls) == 4
    assert invoked == ["read_file"] * 3
    assert captures[-1]["completed"] is True


def test_denied_only_rounds_still_inject_the_original_boundary(monkeypatch, tmp_path) -> None:
    code, _events, client, _invoked, _captures = run_script(
        monkeypatch,
        tmp_path,
        lambda turn: call("run_shell", {"command": f"python -m pytest -q --maxfail={turn}"}, turn),
    )

    assert code == 2
    assert len(client.calls) == 3
    second_request = json.dumps(client.calls[1]["messages"])
    assert "No requested action executed in the last tool batch" in second_request
    assert "Some requested actions were blocked" not in json.dumps(client.calls[2]["messages"])


# ---------- index 5: embed-pass observability ----------


def _write_pending_index() -> None:
    records = [
        {
            "id": f"algo-cli:wiki:page-{i}",
            "harness": "algo-cli",
            "kind": "wiki",
            "relative_path": f"page-{i}.md",
            "path": f"__pytest_harness__/page-{i}.md",
            "search_text": f"algo wiki page {i}",
        }
        for i in range(3)
    ]
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps({"record_count": len(records), "records": records}), encoding="utf-8")
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None


def test_skipped_embed_pass_records_reason_and_quality_recommendation(monkeypatch, tmp_path) -> None:
    _write_pending_index()
    cfg = Config(cwd=str(tmp_path), host="http://203.0.113.9:11434")
    monkeypatch.setattr(main, "make_embed_fn", lambda *_a, **_k: ((lambda texts: [[1.0] for _ in texts]), "t", "m"))
    monkeypatch.setattr(main, "host_is_local", lambda _host: False)

    assert main.ensure_harness_index(cfg, []) is False

    stats = harness.stats(model="m")
    last_pass = stats["embeddings"]["last_pass"]
    assert last_pass["outcome"] == "skipped"
    assert last_pass["reason"] == "non_local_host"
    assert last_pass["pending"] == 3
    quality = stats["quality"]
    assert quality["last_embed_pass"] == last_pass
    recommendation = next(item for item in quality["recommendations"] if "/harness embed" in item)
    assert recommendation.startswith("High-value tiers pending (0/3 embedded); last embed pass skipped: host not local")
    assert recommendation.endswith("Run /harness embed or wait for the next chat turn to complete embeddings.")


def test_unreachable_local_host_records_its_own_skip_reason(monkeypatch, tmp_path) -> None:
    _write_pending_index()
    cfg = Config(cwd=str(tmp_path))
    monkeypatch.setattr(main, "make_embed_fn", lambda *_a, **_k: ((lambda texts: [[1.0] for _ in texts]), "t", "m"))
    monkeypatch.setattr(main, "host_is_local", lambda _host: True)
    monkeypatch.setattr(main, "ollama_server_ready", lambda _host: False)

    main.ensure_harness_index(cfg, [])

    assert harness.last_embed_pass()["reason"] == "ollama_unreachable"


def test_embed_pass_outcomes_are_persisted_by_the_embedder() -> None:
    _write_pending_index()

    partial = harness.embed_index_records(lambda texts: [[1.0] for _ in texts], "m", max_records=2)
    assert partial["ready"] is False
    last_pass = harness.last_embed_pass()
    assert last_pass["outcome"] == "partial"
    assert last_pass["reason"] == "max_records_reached"
    assert last_pass["embedded"] == 2
    assert last_pass["selected_by_priority"]["project_core"] == 2
    assert "embedded 2, per-turn cap reached" in harness._describe_embed_pass(last_pass)

    def broken(_texts):
        raise RuntimeError("backend offline")

    failed = harness.embed_index_records(broken, "m")
    assert failed["ready"] is False
    assert harness.last_embed_pass()["outcome"] == "failed"
    assert "backend offline" in harness.last_embed_pass()["reason"]

    done = harness.embed_index_records(lambda texts: [[1.0] for _ in texts], "m")
    assert done["ready"] is True
    assert harness.last_embed_pass()["outcome"] == "ready"
    assert all("High-value" not in item for item in harness.stats(model="m")["quality"]["recommendations"])


def test_malformed_embed_pass_sidecar_is_ignored() -> None:
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness._embed_pass_path().write_text("[1, 2]", encoding="utf-8")
    assert harness.last_embed_pass() is None
