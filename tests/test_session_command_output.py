from __future__ import annotations

import json

import pytest

from algo_cli import main
from algo_cli import display
from algo_cli import runtime_services
from algo_cli import slash_dispatch
from algo_cli import tools
from algo_cli.config import Config


@pytest.fixture
def session_cfg(monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg = Config()
    cfg.model = "test-model"
    cfg.num_ctx = 8_192
    monkeypatch.setattr(runtime_services, "create_client", lambda _cfg: object())
    return cfg


def test_session_command_returns_status_payload_without_direct_console_output(
    monkeypatch: pytest.MonkeyPatch,
    session_cfg: Config,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        main,
        "context_status",
        lambda _cfg, *, client=None: (320, 8_192, 7_872, 8_192, 8_192),
    )

    result = tools.session_command("/status", cfg=session_cfg)

    assert "Model: test-model" in result
    assert "Context: 320/8192 tokens (7872 remaining)" in result
    assert "Features:" in result
    assert "Executed: /status" not in result
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("command", ["/harness status", "/harness score", "/harness compare"])
def test_session_command_returns_harness_payload(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    session_cfg: Config,
) -> None:
    monkeypatch.setattr(tools, "harness_stats", lambda: "HARNESS STATUS\nrecords=624")
    monkeypatch.setattr(tools, "harness_scorecard", lambda: "HARNESS SCORE\nscore=9")
    monkeypatch.setattr(tools, "harness_competitive_rating", lambda: "HARNESS COMPARE\nrank=2")

    result = tools.session_command(command, cfg=session_cfg)

    expected = (
        "HARNESS STATUS"
        if command.endswith("status")
        else "HARNESS COMPARE"
        if command.endswith("compare")
        else "HARNESS SCORE"
    )
    assert expected in result
    assert "records=624" in result or "score=9" in result or "rank=2" in result
    assert not result.startswith("Executed:")


def test_session_command_preserves_machine_parseable_harness_json(
    monkeypatch: pytest.MonkeyPatch,
    session_cfg: Config,
) -> None:
    payload = {
        "score": 10,
        "overall_status": "ready",
        "checks": [{"name": "long evidence", "evidence": "word " * 40}],
    }
    monkeypatch.setattr(tools, "harness_scorecard", lambda: json.dumps(payload, indent=2))

    result = tools.session_command("/harness score", cfg=session_cfg)

    assert json.loads(result) == payload


def test_session_command_returns_read_only_google_calendar_payload(
    monkeypatch: pytest.MonkeyPatch,
    session_cfg: Config,
) -> None:
    class _GoogleClient:
        def calendar_events_list(self, **_kwargs: object) -> dict[str, object]:
            return {"items": [{"summary": "Dispatch review"}]}

    monkeypatch.setattr(main.google_workspace_auth, "get_valid_token", lambda: "live-token")
    monkeypatch.setattr(main.google_workspace, "GoogleWorkspaceClient", _GoogleClient)
    monkeypatch.setattr(
        main.google_workspace,
        "format_calendar_events",
        lambda _payload: ["2026-07-10 09:00  Dispatch review"],
    )

    result = tools.session_command(
        "/google calendar-list --max 3",
        cfg=session_cfg,
    )

    assert "2026-07-10 09:00  Dispatch review" in result
    assert "live-token" not in result
    assert not result.startswith("Executed:")


def test_session_command_returns_read_only_google_gmail_list_payload(
    monkeypatch: pytest.MonkeyPatch,
    session_cfg: Config,
) -> None:
    class _GoogleClient:
        def gmail_list(self, **_kwargs: object) -> dict[str, object]:
            return {"messages": [{"id": "message-1", "threadId": "thread-7"}]}

    monkeypatch.setattr(main.google_workspace_auth, "get_valid_token", lambda: "live-token")
    monkeypatch.setattr(main.google_workspace, "GoogleWorkspaceClient", _GoogleClient)

    result = tools.session_command("/google gmail-list --max 3", cfg=session_cfg)

    assert "id=message-1" in result
    assert "thread=thread-7" in result
    assert "live-token" not in result
    assert not result.startswith("Executed:")


def test_session_command_returns_redacted_google_auth_status(
    monkeypatch: pytest.MonkeyPatch,
    session_cfg: Config,
) -> None:
    monkeypatch.setattr(
        main.google_workspace_auth,
        "auth_status",
        lambda: {
            "client_configured": True,
            "authenticated": True,
            "expires_in": 1_800,
            "has_refresh_token": True,
            "scope": "calendar.readonly gmail.readonly",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
        },
    )

    result = tools.session_command("/google-status", cfg=session_cfg)

    assert "Google Workspace: authenticated" in result
    assert "Token expires in 1800s" in result
    assert "calendar.readonly gmail.readonly" in result
    assert "access-secret" not in result
    assert "refresh-secret" not in result


def test_session_command_captures_google_status_while_json_sink_is_active(
    monkeypatch: pytest.MonkeyPatch,
    session_cfg: Config,
) -> None:
    class _Sink:
        def __init__(self) -> None:
            self.errors: list[str] = []

        def error(self, *, error_class: str, message: str) -> None:
            self.errors.append(f"{error_class}:{message}")

    sink = _Sink()
    monkeypatch.setattr(
        main.google_workspace_auth,
        "auth_status",
        lambda: {
            "client_configured": True,
            "authenticated": True,
            "expires_in": 900,
            "has_refresh_token": False,
            "scope": "calendar.readonly",
        },
    )

    display.install_json_sink(sink)
    try:
        result = tools.session_command("/google-status", cfg=session_cfg)
    finally:
        display.uninstall_json_sink()

    assert "Google Workspace: authenticated" in result
    assert "Token expires in 900s" in result
    assert sink.errors == []


def test_session_command_redacts_credentials_from_captured_errors(
    monkeypatch: pytest.MonkeyPatch,
    session_cfg: Config,
) -> None:
    class _GoogleClient:
        def calendar_events_list(self, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError(
                "Authorization: Bearer bearer-secret access_token=access-secret"
            )

    monkeypatch.setattr(main.google_workspace_auth, "get_valid_token", lambda: "live-token")
    monkeypatch.setattr(main.google_workspace, "GoogleWorkspaceClient", _GoogleClient)

    result = tools.session_command("/google calendar-list", cfg=session_cfg)

    assert result.startswith("Error:")
    assert "Bearer <redacted>" in result
    assert "access_token=<redacted>" in result
    assert "bearer-secret" not in result
    assert "access-secret" not in result
    assert "live-token" not in result


def test_session_command_output_redacts_local_path_prefixes() -> None:
    rendered = tools._redact_session_command_output(
        f"config={tools.CONFIG_DIR}/skills home={tools.Path.home()}/private-project"
    )

    assert str(tools.CONFIG_DIR) not in rendered
    assert str(tools.Path.home()) not in rendered
    assert "$ALGO_CLI_CONFIG_DIR" in rendered


@pytest.mark.parametrize(
    ("command", "captures"),
    [
        ("/google-status", True),
        ("/google gmail-list --max 5", True),
        ("/google docs-get document-id", True),
        ("/harness status", True),
        ("/harness score", True),
        ("/safe status", True),
        ("/memory-auto", True),
        ("/memory-auto status", True),
        ("/code-rag", True),
        ("/code-rag status", True),
        ("/mode status", True),
        ("/kernel check", True),
        ("/kernel show harness-fusion-ranker", True),
        ("/intelligence query algo-cli", True),
        ("/icl ask algo-cli", True),
        ("/x-account status", True),
        ("/plugins list", True),
        ("/selfcheck", True),
        ("/memories", True),
        ("/xai-status", True),
        ("/safe", False),
        ("/auto", False),
        ("/cloud", False),
        ("/cloudauto", False),
        ("/thinking", False),
        ("/verify", False),
        ("/google-login --no-browser", False),
        ("/google-callback https://localhost/?code=secret", False),
        ("/google gmail-draft --to a@example.test --subject test body", False),
        ("/harness refresh", False),
        ("/harness embed", False),
        ("/safe off", False),
        ("/memory-auto off", False),
        ("/code-rag on", False),
        ("/code-rag off", False),
        ("/mode publish", False),
    ],
)
def test_session_output_capture_policy_is_read_only(command: str, captures: bool) -> None:
    assert tools._session_command_captures_output(command) is captures


def test_noncaptured_session_command_keeps_console_rendering(
    monkeypatch: pytest.MonkeyPatch,
    session_cfg: Config,
) -> None:
    def fake_handle(
        _raw: str,
        _cfg: Config,
        client: object,
    ) -> tuple[bool, object]:
        main.console.print("access_token=interactive-secret")
        return True, client

    monkeypatch.setattr(slash_dispatch, "handle_command", fake_handle)

    with main.console.capture() as captured:
        result = tools.session_command(
            "/google gmail-draft --to a@example.test --subject test body",
            cfg=session_cfg,
        )

    assert result == (
        "Executed: /google gmail-draft --to a@example.test --subject test body"
    )
    assert "interactive-secret" not in result
    assert captured.get().strip() == "access_token=interactive-secret"


def test_session_output_is_bounded() -> None:
    oversized = "x" * (tools.SESSION_COMMAND_OUTPUT_LIMIT + 10)

    result = tools._captured_session_result(oversized, "/status")

    assert len(result) < len(oversized)
    assert result.endswith("...[truncated]")
