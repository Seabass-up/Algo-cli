"""Capability readiness: supported, configured, allowed_in_session, verified."""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import time

import pytest
from rich.console import Console

from algo_cli import capability_readiness as readiness
from algo_cli import display, google_workspace_auth, harness, main, tools, x_account
from algo_cli.config import Config
from algo_cli.marcus_authority import ConfirmationMode, EffectClass, TargetScope
from algo_cli.nathan_program_runtime import authorization_for_actions
from algo_cli.nathan_runtime import (
    ask_approval,
    authority_session_for,
    classify_tool_status,
    preflight_runtime_tool,
    record_tool_attempt,
    session_policy_readiness,
)
from algo_cli.samuel_policy_engine import PolicyDisposition, resolve_action, session_command_requires_approval
from test_agent_progress_recovery import call, run_script


def _chat_cfg(tmp_path) -> Config:
    cfg = Config(cwd=str(tmp_path))
    # Ordinary chat binds the full registered ceiling.
    cfg._algo_program_authorization = authorization_for_actions(tuple(tools.TOOL_MAP))
    return cfg


def _google_status(monkeypatch, **status) -> None:
    base = {"authenticated": False, "client_configured": False, "token_present": False}
    monkeypatch.setattr(google_workspace_auth, "auth_status", lambda: {**base, **status})


def _record(key: str, cfg) -> dict:
    return readiness.capability_readiness(key, cfg)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("readiness must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


# ---------- Gmail / Google: not configured, configured, blocked by policy ----------


def test_gmail_without_oauth_client_is_not_configured_with_setup_step(monkeypatch, tmp_path):
    _google_status(monkeypatch)
    record = _record("google", _chat_cfg(tmp_path))

    assert record["supported"]["ok"] is True
    assert record["configured"]["ok"] is False
    assert record["status"] == "not_configured"
    assert "algo-cli config setup google" in record["next_step"]


def test_gmail_with_client_but_no_login_names_the_login_step(monkeypatch, tmp_path):
    _google_status(monkeypatch, client_configured=True)
    record = _record("google", _chat_cfg(tmp_path))

    assert record["status"] == "not_configured"
    assert record["reason"] == "Google account is not signed in."
    assert record["next_step"] == "Run `algo-cli config auth google login`."


def test_gmail_configured_and_allowed_is_ready(monkeypatch, tmp_path):
    _google_status(monkeypatch, authenticated=True, client_configured=True, token_present=True)
    record = _record("google", _chat_cfg(tmp_path))

    assert record["configured"] == {"ok": True}
    assert record["allowed_in_session"] == {"ok": True, "state": "allowed"}
    assert record["verified"] == {"ok": False, "at": None}
    assert record["status"] == "ready"


def test_gmail_blocked_by_protected_memory_policy(monkeypatch, tmp_path):
    _google_status(monkeypatch, authenticated=True, client_configured=True, token_present=True)
    cfg = _chat_cfg(tmp_path)
    cfg.continuum_enabled = True

    record = _record("google", cfg)

    assert record["configured"]["ok"] is True
    assert record["allowed_in_session"]["ok"] is False
    assert record["allowed_in_session"]["state"] == "blocked"
    assert record["status"] == "blocked"
    assert "not qualified" in record["reason"]
    assert "do not retry" in record["next_step"]


def test_gmail_blocked_outside_an_agent_block_ceiling(monkeypatch, tmp_path):
    _google_status(monkeypatch, authenticated=True, client_configured=True, token_present=True)
    cfg = Config(cwd=str(tmp_path))
    cfg._algo_program_authorization = authorization_for_actions(("read_file",))

    record = _record("google", cfg)

    assert record["status"] == "blocked"
    assert "tool ceiling" in record["reason"]


def test_gmail_draft_needs_approval_while_read_is_allowed(monkeypatch, tmp_path):
    _google_status(monkeypatch, authenticated=True, client_configured=True, token_present=True)
    cfg = _chat_cfg(tmp_path)

    assert readiness.slash_readiness("/google gmail-list", cfg)["status"] == "ready"
    draft = readiness.slash_readiness("/google gmail-draft", cfg)
    assert draft["status"] == "needs_approval"
    assert draft["allowed_in_session"]["ok"] is True


# ---------- each capability's states with fakes ----------


def test_browser_is_unconfirmed_without_probing_and_blocked_under_protected_memory(monkeypatch, tmp_path):
    from algo_cli import cobalt_browser_service

    monkeypatch.setattr(
        cobalt_browser_service, "is_available", lambda: (_ for _ in ()).throw(AssertionError("probed"))
    )
    cfg = _chat_cfg(tmp_path)
    record = _record("browser", cfg)
    assert record["configured"]["ok"] is None
    assert record["status"] == "unconfirmed"
    assert "Camoufox" in record["next_step"]

    cfg.continuum_enabled = True
    assert _record("browser", cfg)["status"] == "blocked"


def test_jev_states_and_ranker_boundary(tmp_path):
    cfg = _chat_cfg(tmp_path)
    missing = _record("jev", cfg)
    assert missing["status"] == "not_configured"
    assert "config jev enable" in missing["next_step"]
    assert {"browsing", "reading email"} <= set(missing["not_for"])

    companion = tmp_path / "jev-workflows"
    companion.write_text("#!/bin/sh\n", encoding="utf-8")
    companion.chmod(companion.stat().st_mode | stat.S_IXUSR)
    cfg.jev_kernel_cli = str(companion)
    disabled = _record("jev", cfg)
    assert disabled["status"] == "not_configured"
    assert "disabled" in disabled["reason"]

    cfg.jev_kernel_enabled = True
    enabled = _record("jev", cfg)
    assert enabled["configured"] == {"ok": True}
    assert enabled["status"] == "needs_approval"

    cfg.jev_kernel_cli = str(tmp_path / "absent")
    assert "missing" in _record("jev", cfg)["reason"]


def test_xai_depends_on_local_api_key(monkeypatch, tmp_path):
    cfg = _chat_cfg(tmp_path)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    record = _record("xai", cfg)
    assert record["status"] == "not_configured"
    assert record["next_step"] == "Run `algo-cli config setup xai`."

    monkeypatch.setenv("XAI_API_KEY", "fake-test-key")
    record = _record("xai", cfg)
    assert record["configured"] == {"ok": True}
    assert record["status"] == "needs_approval"
    assert "fake-test-key" not in json.dumps(record)


def test_x_account_depends_on_xurl_on_path(monkeypatch, tmp_path):
    cfg = _chat_cfg(tmp_path)
    monkeypatch.setattr(readiness.shutil, "which", lambda _name: None)
    record = _record("x_account", cfg)
    assert record["status"] == "not_configured"
    assert "xurl" in record["next_step"]

    monkeypatch.setattr(readiness.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert _record("x_account", cfg)["configured"] == {"ok": True}


def test_web_tools_need_ollama_api_key_and_sdk_support(monkeypatch, tmp_path):
    import ollama

    cfg = _chat_cfg(tmp_path)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert _record("web", cfg)["status"] == "not_configured"

    monkeypatch.setenv("OLLAMA_API_KEY", "fake-test-key")
    assert _record("web", cfg)["status"] == "needs_approval"

    monkeypatch.delattr(ollama.Client, "web_search", raising=False)
    unsupported = _record("web", cfg)
    assert unsupported["supported"]["ok"] is False
    assert unsupported["status"] == "unsupported"


def test_embeddings_need_a_model(tmp_path):
    cfg = _chat_cfg(tmp_path)
    assert _record("embeddings", cfg)["configured"] == {"ok": True}
    cfg.harness_embed_model = ""
    assert _record("embeddings", cfg)["status"] == "not_configured"


def test_harness_needs_an_index(tmp_path):
    cfg = _chat_cfg(tmp_path)
    harness.INDEX_PATH.unlink(missing_ok=True)
    record = _record("harness", cfg)
    assert record["status"] == "not_configured"
    assert record["next_step"] == "Run /harness refresh."

    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text("{}", encoding="utf-8")
    assert _record("harness", cfg)["status"] == "ready"


def test_unregistered_tool_is_unsupported(tmp_path):
    record = readiness.tool_readiness("no_such_tool", _chat_cfg(tmp_path))
    assert record["status"] == "unsupported"


def test_approval_needing_route_is_blocked_when_the_run_cannot_ask(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-test-key")
    cfg = _chat_cfg(tmp_path)
    setattr(cfg, "_nathan_approval_mode", "never")

    record = _record("web", cfg)

    assert record["status"] == "blocked"
    assert "cannot ask for approval" in record["reason"]


def test_failing_check_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(
        google_workspace_auth, "auth_status", lambda: (_ for _ in ()).throw(OSError("unreadable"))
    )
    record = _record("google", _chat_cfg(tmp_path))
    assert record["configured"]["ok"] is False
    assert "OSError" in record["reason"]


def test_readiness_is_fast_and_leaves_no_grants(tmp_path):
    cfg = _chat_cfg(tmp_path)
    readiness.all_readiness(cfg)
    started = time.perf_counter()
    records = readiness.all_readiness(cfg)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert [record["capability"] for record in records] == [spec.key for spec in readiness.CAPABILITIES]
    assert elapsed_ms < 250  # typical is well under 50 ms; the bound tolerates slow CI hosts
    assert authority_session_for(cfg)._grants == {}
    assert cfg.attempt_ledger == []


def test_session_policy_readiness_does_not_register_turn_denials(tmp_path):
    cfg = _chat_cfg(tmp_path)
    setattr(cfg, "_nathan_turn_denials", {})
    outside = tmp_path.parent / "elsewhere.txt"

    decision = session_policy_readiness("read_file", {"path": str(outside)}, cfg)

    assert decision.state == "blocked"
    assert "outside the session workspace" in decision.reason
    assert cfg._nathan_turn_denials == {}


# ---------- capability_status: baseline-granted, read-only ----------


def test_capability_status_is_baseline_granted_read_only_and_never_prompts(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda *_args: (_ for _ in ()).throw(AssertionError("prompted")))

    action = resolve_action("capability_status", {"topic": "gmail"}, cwd=cfg.cwd)
    preflight = preflight_runtime_tool("capability_status", {"topic": "gmail"}, cfg)

    assert action.effect_class is EffectClass.OBSERVE
    assert action.target_scope is TargetScope.RUNTIME
    assert action.confirmation_mode is ConfirmationMode.NONE
    assert preflight.policy.disposition is PolicyDisposition.ALLOW
    assert ask_approval("capability_status", {"topic": "gmail"}, cfg, preflight=preflight) is True
    assert "capability_status" in main.READ_ONLY_TOOLS
    assert "capability_status" in tools.TOOL_MAP
    assert "cfg" not in tools.TOOL_MAP["capability_status"].__signature__.parameters


def test_capability_status_returns_topic_readiness_without_side_effects(monkeypatch, tmp_path):
    _google_status(monkeypatch)
    cfg = _chat_cfg(tmp_path)
    before = dict(os.environ)

    payload = json.loads(tools.capability_status("check my email", cfg=cfg))

    assert payload["match"] == "matched"
    assert [row["capability"] for row in payload["capabilities"]] == ["google"]
    assert payload["capabilities"][0]["status"] == "not_configured"
    assert "Check readiness before offering a route" in payload["guidance"]
    assert dict(os.environ) == before
    assert cfg.attempt_ledger == []

    tool = json.loads(tools.capability_status("read_file", cfg=cfg))
    assert tool["match"] == "tool"
    assert tool["tools"]["read_file"]["status"] == "ready"

    none = json.loads(tools.capability_status("teleportation", cfg=cfg))
    assert none["match"] == "none"
    assert "google" in none["topics"]


# ---------- discovery surfaces ----------


def test_action_search_rows_carry_readiness_and_guidance(monkeypatch, tmp_path):
    _google_status(monkeypatch)
    cfg = _chat_cfg(tmp_path)

    payload = json.loads(tools.action_search("check my email", cfg=cfg))

    assert "Check readiness before offering a route" in payload["readiness_guidance"]
    gmail = next(row for row in payload["slash_commands"] if row["command"].startswith("/google gmail-list"))
    assert gmail["readiness"]["status"] == "not_configured"
    assert set(gmail["readiness"]) >= {"supported", "configured", "allowed_in_session", "verified"}

    web = json.loads(tools.action_search("search the web", cfg=cfg))
    assert web["actions"]
    assert all({"supported", "configured", "allowed_in_session", "verified"} <= set(row["readiness"]) for row in web["actions"])


def test_available_actions_reports_readiness_for_all_and_for_a_topic(monkeypatch, tmp_path):
    _google_status(monkeypatch)
    # Harness statistics probe the embedding backend; that is unrelated to readiness.
    monkeypatch.setattr(tools, "_harness_stats_for_config", lambda _cfg: {})
    cfg = _chat_cfg(tmp_path)

    full = json.loads(tools.available_actions(cfg=cfg))
    assert set(full["capability_readiness"]) == {spec.key for spec in readiness.CAPABILITIES}
    assert full["capability_readiness"]["google"]["status"] == "not_configured"
    assert "readiness_guidance" in full

    focused = json.loads(tools.available_actions("email", cfg=cfg))["focused"]
    assert focused["capability_readiness"]["google"]["status"] == "not_configured"
    assert "Jev only ranks" in focused["readiness_guidance"]


# ---------- verified: recorded centrally, only after success ----------


def test_verified_is_set_only_by_a_successful_outcome(tmp_path):
    cfg = _chat_cfg(tmp_path)
    record_tool_attempt(cfg, name="x_search", args={"query": "q"}, result="Error: xAI API key", status="failed")
    record_tool_attempt(cfg, name="x_search", args={"query": "q"}, result="", status="denied")
    assert readiness.tool_readiness("x_search", cfg)["verified"] == {"ok": False, "at": None}
    assert _record("xai", cfg)["verified"]["ok"] is False

    record_tool_attempt(cfg, name="x_search", args={"query": "q"}, result="Top posts...", status="worked")
    assert readiness.tool_readiness("x_search", cfg)["verified"]["ok"] is True
    assert _record("xai", cfg)["verified"]["at"]


def test_verified_slash_capability_needs_a_subcommand(monkeypatch, tmp_path):
    _google_status(monkeypatch, authenticated=True, client_configured=True, token_present=True)
    cfg = _chat_cfg(tmp_path)
    record_tool_attempt(cfg, name="session_command", args={"command": "/google"}, result="usage", status="worked")
    assert _record("google", cfg)["verified"]["ok"] is False

    record_tool_attempt(
        cfg, name="session_command", args={"command": "/google gmail-list"}, result="3 messages", status="worked"
    )
    record = _record("google", cfg)
    assert record["verified"]["ok"] is True
    assert record["status"] == "verified"


def _fake_unauthenticated_xurl(monkeypatch, tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    xurl = bin_dir / "xurl"
    xurl.write_text("#!/bin/sh\necho 'not authenticated' >&2\nexit 1\n", encoding="utf-8")
    xurl.chmod(xurl.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell script as the fake xurl")
def test_x_account_local_draft_does_not_verify_an_unauthenticated_account(monkeypatch, tmp_path):
    _fake_unauthenticated_xurl(monkeypatch, tmp_path)
    cfg = _chat_cfg(tmp_path)

    status_result = tools.x_account_status()
    status = classify_tool_status(status_result, name="x_account_status")
    assert status == "failed"
    record_tool_attempt(cfg, name="x_account_status", args={}, result=status_result, status=status)

    for name, args, fn in (
        ("x_account_draft_post", {"text": "hello"}, lambda: tools.x_account_draft_post("hello")),
        (
            "x_account_draft_reply",
            {"post": "123", "text": "hi"},
            lambda: tools.x_account_draft_reply("123", "hi"),
        ),
    ):
        result = fn()
        status = classify_tool_status(result, name=name)
        assert status == "worked"
        record_tool_attempt(cfg, name=name, args=args, result=result, status=status)
        assert readiness.tool_readiness(name, cfg)["verified"]["ok"] is True

    record = _record("x_account", cfg)
    assert record["configured"]["ok"] is True
    assert record["verified"] == {"ok": False, "at": None}
    assert record["status"] != "verified"


@pytest.mark.parametrize(
    "command", ["/x-account draft-post hello", "/x-account draft-reply 123 hi", "/x-account help", "/x-account"]
)
def test_x_account_slash_help_and_drafts_do_not_verify(tmp_path, command):
    cfg = _chat_cfg(tmp_path)
    record_tool_attempt(cfg, name="session_command", args={"command": command}, result="ok", status="worked")
    assert _record("x_account", cfg)["verified"]["ok"] is False


def test_x_account_status_success_still_verifies(tmp_path):
    cfg = _chat_cfg(tmp_path)
    record_tool_attempt(cfg, name="x_account_status", args={}, result='{"ok": true}', status="worked")
    assert _record("x_account", cfg)["verified"]["ok"] is True

    other = _chat_cfg(tmp_path)
    record_tool_attempt(
        other, name="session_command", args={"command": "/x-account status"}, result="ok", status="worked"
    )
    assert _record("x_account", other)["verified"]["ok"] is True


@pytest.mark.parametrize(
    "command", ["/google help", "/google --help", "/google -h", "/google HELP", "/google unknown-sub"]
)
def test_google_help_does_not_verify_when_token_refresh_fails(monkeypatch, tmp_path, command):
    _google_status(monkeypatch, authenticated=True, client_configured=True, token_present=True)
    monkeypatch.setattr(google_workspace_auth, "get_valid_token", lambda: None)
    cfg = _chat_cfg(tmp_path)
    assert _record("google", cfg)["status"] == "ready"

    record_tool_attempt(cfg, name="session_command", args={"command": command}, result="usage", status="worked")
    record = _record("google", cfg)
    assert record["verified"] == {"ok": False, "at": None}
    assert record["status"] == "ready"


def test_google_help_through_session_command_leaves_readiness_ready(monkeypatch, tmp_path):
    _google_status(monkeypatch, authenticated=True, client_configured=True, token_present=True)
    monkeypatch.setattr(google_workspace_auth, "get_valid_token", lambda: None)
    cfg = _chat_cfg(tmp_path)

    help_result = tools.session_command("/google help", cfg)
    help_status = classify_tool_status(help_result, name="session_command")
    record_tool_attempt(
        cfg, name="session_command", args={"command": "/google help"}, result=help_result, status=help_status
    )
    assert _record("google", cfg)["status"] == "ready"

    list_result = tools.session_command("/google gmail-list", cfg)
    list_status = classify_tool_status(list_result, name="session_command")
    assert list_status == "failed"
    record_tool_attempt(
        cfg, name="session_command", args={"command": "/google gmail-list"}, result=list_result, status=list_status
    )
    assert _record("google", cfg)["verified"]["ok"] is False


def test_verified_is_recorded_by_the_agent_runtime_outcome_path(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_API_KEY", "fake-test-key")
    seen = {}

    def responses(turn):
        if turn == 1:
            return call("web_search", {"query": "algo cli"}, turn)
        if turn == 2:
            return call("x_search", {"query": "algo cli"}, turn)
        return {"content": "Done."}

    def invoke(name, _args, _cfg):
        return "Results: one page" if name == "web_search" else "Error: xAI API key is not configured."

    code, _events, _client, invoked, _captures = run_script(
        monkeypatch, tmp_path, responses, invoke=invoke, configure=lambda cfg: seen.setdefault("cfg", cfg)
    )

    cfg = seen["cfg"]
    assert invoked == ["web_search", "x_search"]
    assert readiness.tool_readiness("web_search", cfg)["verified"]["ok"] is True
    assert _record("web", cfg)["verified"]["ok"] is True
    assert readiness.tool_readiness("x_search", cfg)["verified"]["ok"] is False
    assert code == 0


# ---------- /capabilities ----------


def test_capabilities_slash_command_is_read_only_and_renders_a_table(monkeypatch, tmp_path):
    _google_status(monkeypatch)
    cfg = _chat_cfg(tmp_path)
    assert session_command_requires_approval("/capabilities") is False
    assert session_command_requires_approval("/capabilities gmail") is False

    console = Console(
        file=io.StringIO(),
        legacy_windows=False,
        force_terminal=True,
        color_system="truecolor",
        width=160,
        theme=display.THEME_MAP["tokyo-night"],
        record=True,
    )
    monkeypatch.setattr(main, "console", console)
    from algo_cli.oliver_slash_dispatch import handle_command

    handled, _client = handle_command("/capabilities", cfg, None)
    text = console.export_text()

    assert handled is True
    for key in ("google", "browser", "jev", "xai", "x_account", "web", "embeddings", "harness"):
        assert key in text
    assert "not_configured" in text
    assert "config setup google" in text


def test_verifying_subcommands_match_the_real_slash_handlers():
    import inspect

    google_source = inspect.getsource(main.run_google)
    for sub in readiness._BY_KEY["google"].verifying_subcommands:
        assert f'sub == "{sub}"' in google_source
    x_source = inspect.getsource(main.run_x_account)
    for sub in readiness._BY_KEY["x_account"].verifying_subcommands:
        assert f'sub == "{sub}"' in x_source or sub in x_account.CONFIRMED_POST_ACTIONS
    assert set(readiness._BY_KEY["x_account"].offline_tools) <= set(readiness._BY_KEY["x_account"].tools)
