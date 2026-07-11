from __future__ import annotations

import subprocess

import pytest

from algo_cli import x_account


def test_normalize_post_id_accepts_id_and_url():
    assert x_account.normalize_post_id("1234567890") == "1234567890"
    assert x_account.normalize_post_id("https://x.com/user/status/1234567890") == "1234567890"


def test_normalize_post_id_rejects_invalid():
    with pytest.raises(ValueError):
        x_account.normalize_post_id("https://x.com/user")


def test_draft_post_returns_compose_url():
    result = x_account.draft_post("hello world")
    assert result.ok is True
    assert result.data is not None
    assert result.data["url"] == "https://x.com/compose/post?text=hello%20world"


def test_draft_reply_returns_intent_url():
    result = x_account.draft_reply("https://x.com/u/status/123", "agreed")
    assert result.ok is True
    assert result.data is not None
    assert result.data["url"] == "https://x.com/intent/tweet?in_reply_to=123"
    assert result.data["post_id"] == "123"


def test_post_without_confirm_blocks_and_does_not_run(monkeypatch):
    monkeypatch.setattr(x_account, "_run_xurl", lambda *args, **kwargs: pytest.fail("should not run xurl"))
    result = x_account.post("hello", confirm=False)
    assert result.ok is False
    assert "Blocked write" in result.message
    assert result.data is not None
    assert "compose/post" in result.data["url"]


def test_reply_without_confirm_blocks_and_does_not_run(monkeypatch):
    monkeypatch.setattr(x_account, "_run_xurl", lambda *args, **kwargs: pytest.fail("should not run xurl"))
    result = x_account.reply("123", "hello", confirm=False)
    assert result.ok is False
    assert "Blocked write" in result.message
    assert result.data is not None
    assert result.data["post_id"] == "123"


def test_confirmed_post_runs_xurl(monkeypatch):
    captured: dict = {}

    def fake_run(args, *, timeout):
        captured["args"] = args
        captured["timeout"] = timeout
        return x_account.XAccountResult(True, "xurl", "posted", {"exit_code": 0})

    monkeypatch.setattr(x_account, "_run_xurl", fake_run)
    result = x_account.post("hello", confirm=True)
    assert result.ok is True
    assert captured["args"] == ["post", "hello"]
    assert captured["timeout"] == 45


def test_post_action_blocks_without_confirm(monkeypatch):
    monkeypatch.setattr(x_account, "_run_xurl", lambda *args, **kwargs: pytest.fail("should not run xurl"))
    result = x_account.post_action("like", "123", confirm=False)
    assert result.ok is False
    assert "Blocked write" in result.message
    assert result.data == {"post_id": "123"}


def test_confirmed_post_action_runs_xurl(monkeypatch):
    captured: dict = {}

    def fake_run(args, *, timeout):
        captured["args"] = args
        captured["timeout"] = timeout
        return x_account.XAccountResult(True, "xurl", "liked", {"exit_code": 0})

    monkeypatch.setattr(x_account, "_run_xurl", fake_run)
    result = x_account.post_action("like", "https://x.com/u/status/123", confirm=True)
    assert result.ok is True
    assert captured["args"] == ["like", "123"]


def test_run_xurl_rejects_inline_secret_flags():
    with pytest.raises(ValueError):
        x_account._validate_xurl_args(["auth", "--client-secret=secret"])


def test_run_xurl_uses_list_args_and_captures_output(monkeypatch):
    monkeypatch.setattr(x_account, "xurl_path", lambda: "xurl")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(x_account.subprocess, "run", fake_run)
    result = x_account.status()
    assert result.ok is True
    assert result.message == "ok"
    assert captured["cmd"] == ["xurl", "auth", "status"]
    assert "shell" not in captured["kwargs"]
