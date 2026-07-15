from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

from algo_cli import google_workspace_auth
from algo_cli import main
from algo_cli import session_commands
from algo_cli import slash_dispatch
from algo_cli import tools


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, value="") -> None:
        self.lines.append(str(value))


def _patch_google_runtime(monkeypatch):
    infos: list[str] = []
    errors: list[str] = []
    console = _Console()
    monkeypatch.setattr(main, "show_info", lambda message: infos.append(str(message)))
    monkeypatch.setattr(main, "show_error", lambda message: errors.append(str(message)))
    monkeypatch.setattr(main, "console", console)
    monkeypatch.setattr(main.google_workspace_auth, "get_valid_token", lambda: "token")
    return infos, errors, console


def test_google_help_and_action_discovery_cover_all_readonly_apis(monkeypatch):
    infos, errors, _console = _patch_google_runtime(monkeypatch)

    main.run_google("help")
    actions = tools.available_actions("google")
    slash = dict(slash_dispatch.SLASH_COMMANDS)["/google"]

    assert not errors
    help_text = "\n".join(infos)
    for expected in (
        "/google docs-get DOCUMENT_ID",
        "/google sheets-values SPREADSHEET_ID RANGE",
        "/google calendar-list",
    ):
        assert expected in help_text
        assert expected in actions
    assert "docs-get" in slash
    assert "sheets-values" in slash
    assert "calendar-list" in slash
    assert "algo-cli config setup google" in actions
    assert "algo-cli config auth google login" in actions
    assert "/config" in dict(slash_dispatch.SLASH_COMMANDS)


def test_google_actions_are_visible_from_agent_prompt_catalog():
    catalog = session_commands.catalog_for_prompt()

    assert "available_actions(topic='google')" in catalog


def test_google_login_waits_for_loopback_and_completes_with_redirect_uri(monkeypatch):
    infos, errors, console = _patch_google_runtime(monkeypatch)
    completed: list[tuple[str, str, dict[str, str]]] = []

    monkeypatch.setattr(main.google_workspace_auth, "select_redirect_port", lambda: 56251)
    monkeypatch.setattr(
        main.google_workspace_auth,
        "begin_login",
        lambda *, no_browser, redirect_port: {
            "state": "state-1",
            "code_verifier": "verifier-1",
            "auth_url": "https://accounts.google.test/approve",
            "redirect_uri": "http://127.0.0.1:56251/callback",
            "redirect_port": str(redirect_port),
            "browser_opened": False,
        },
    )
    monkeypatch.setattr(
        main.google_workspace_auth,
        "wait_for_callback",
        lambda *, redirect_port, timeout=300.0: {"code": "code-1", "state": "state-1"},
    )

    def fake_complete(code_verifier: str, state: str, callback: dict[str, str]):
        completed.append((code_verifier, state, callback))
        return {"expires_at": main.time.time() + 3600}

    monkeypatch.setattr(main.google_workspace_auth, "complete_login", fake_complete)

    main.run_google_login("")

    assert not errors
    assert "https://accounts.google.test/approve" in "\n".join(console.lines)
    assert completed == [
        (
            "verifier-1",
            "state-1",
            {
                "code": "code-1",
                "state": "state-1",
                "redirect_uri": "http://127.0.0.1:56251/callback",
            },
        )
    ]
    assert any("authentication successful" in message.lower() for message in infos)


def test_google_authorize_url_does_not_request_previously_granted_scopes(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id.apps.googleusercontent.com")

    url = google_workspace_auth.build_authorize_url(
        state="state-1",
        code_challenge="challenge-1",
        redirect_uri="http://127.0.0.1:56251/callback",
    )
    query = parse_qs(urlparse(url).query)

    assert query["scope"] == [google_workspace_auth.GOOGLE_DEFAULT_SCOPE]
    assert "include_granted_scopes" not in query


def test_google_redirect_port_configuration_fails_safe(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "not-a-port")
    assert google_workspace_auth._configured_redirect_port() == 56251

    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "70000")
    assert google_workspace_auth._configured_redirect_port() == 56251

    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_PORT", "60000")
    assert google_workspace_auth._configured_redirect_port() == 60000


def test_google_loopback_capture_uses_fresh_handler_state(monkeypatch):
    handlers: list[type] = []

    class _Server:
        def __init__(self, _address, handler) -> None:
            self.handler = handler
            handlers.append(handler)

        def handle_request(self) -> None:
            self.handler.query_params = {"code": "code-1", "state": "state-1"}
            self.handler.done = True

        def server_close(self) -> None:
            return

    monkeypatch.setattr(google_workspace_auth.http.server, "HTTPServer", _Server)

    first = google_workspace_auth.run_loopback_capture(redirect_port=56251, timeout=1)
    second = google_workspace_auth.run_loopback_capture(redirect_port=56252, timeout=1)

    assert first == {"code": "code-1", "state": "state-1"}
    assert second == first
    assert len(handlers) == 2
    assert handlers[0] is not handlers[1]


def test_google_token_without_client_configuration_is_not_usable(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    google_workspace_auth.save_tokens(
        {
            "access_token": "old-token",
            "refresh_token": "old-refresh",
            "expires_at": int(time.time()) + 3600,
        }
    )

    status = google_workspace_auth.auth_status()

    assert status["token_present"] is True
    assert status["authenticated"] is False
    assert google_workspace_auth.get_valid_token() is None


def test_google_safe_error_explains_desktop_redirect_mismatch(monkeypatch):
    client_id = "google-client-id-not-for-output"
    secret = "google-secret-not-for-output"
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", client_id)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", secret)

    rendered = google_workspace_auth.safe_error_message(
        f"redirect_uri_mismatch client={client_id} secret={secret}"
    )

    assert "Desktop app" in rendered
    assert client_id not in rendered
    assert secret not in rendered


def test_google_safe_error_redacts_oauth_code_but_preserves_http_status_code():
    rendered = google_workspace_auth.safe_error_message(
        "request failed with status code: 404; callback={'code': 'oauth-secret'}"
    )

    assert "status code: 404" in rendered
    assert "oauth-secret" not in rendered


def test_google_auth_state_handles_malformed_timestamps(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "desktop.apps.googleusercontent.com")
    google_workspace_auth.save_tokens(
        {"access_token": "old-token", "expires_at": "not-a-timestamp"}
    )

    status = google_workspace_auth.auth_status()

    assert status["authenticated"] is False
    assert status["token_valid"] is False
    assert status["expires_at"] == 0


def test_google_concurrent_token_refresh_is_serialized(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "desktop.apps.googleusercontent.com")
    google_workspace_auth.save_tokens(
        {"access_token": "OLD", "refresh_token": "RT", "expires_at": int(time.time()) - 10}
    )
    refreshes: list[str] = []

    def fake_refresh(refresh_token: str) -> dict[str, object]:
        refreshes.append(refresh_token)
        time.sleep(0.05)
        return {
            "access_token": "NEW",
            "refresh_token": "NRT",
            "expires_at": int(time.time()) + 3600,
        }

    monkeypatch.setattr(google_workspace_auth, "refresh_access_token", fake_refresh)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: google_workspace_auth.get_valid_token(), range(2)))

    assert results == ["NEW", "NEW"]
    assert refreshes == ["RT"]


def test_google_pending_login_rejects_malformed_created_at():
    google_workspace_auth.PENDING_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    google_workspace_auth.PENDING_AUTH_FILE.write_text(
        '{"state":"s","code_verifier":"v","redirect_uri":"http://localhost/callback","created_at":"bad"}',
        encoding="utf-8",
    )

    assert google_workspace_auth.load_pending_login() is None


def test_google_missing_access_token_error_does_not_echo_secrets():
    secret = "refresh-secret-not-for-terminal"

    try:
        google_workspace_auth._normalize_token_response({"refresh_token": secret})
    except RuntimeError as exc:
        assert secret not in str(exc)
    else:
        raise AssertionError("missing access token should fail")


def test_google_callback_clipboard_completes_pending_login(monkeypatch):
    infos, errors, _console = _patch_google_runtime(monkeypatch)
    completed: list[tuple[str, str, dict[str, str]]] = []
    long_callback = (
        "27.0.0.1:56251/callback?state=state-1&iss=https://accounts.google.com"
        "&code=code-1&scope="
        + "x" * 5000
    )

    monkeypatch.setattr(
        main.google_workspace_auth,
        "load_pending_login",
        lambda: {
            "state": "state-1",
            "code_verifier": "verifier-1",
            "redirect_uri": "http://127.0.0.1:56251/callback",
        },
    )
    monkeypatch.setattr(main, "read_clipboard_text", lambda: long_callback)

    def fake_complete(code_verifier: str, state: str, callback: dict[str, str]):
        completed.append((code_verifier, state, callback))
        return {"expires_at": main.time.time() + 3600}

    monkeypatch.setattr(main.google_workspace_auth, "complete_login", fake_complete)

    main.run_google_callback("--clipboard")

    assert not errors
    assert completed == [
        (
            "verifier-1",
            "state-1",
            {
                "state": "state-1",
                "iss": "https://accounts.google.com",
                "code": "code-1",
                "scope": "x" * 5000,
                "redirect_uri": "http://127.0.0.1:56251/callback",
            },
        )
    ]
    assert any("authentication successful" in message.lower() for message in infos)


def test_google_drive_list_uses_client_payload_and_formatter(monkeypatch):
    _infos, errors, console = _patch_google_runtime(monkeypatch)
    calls: list[tuple[str, str | None, int]] = []

    class _FakeGoogleClient:
        def drive_list(self, *, query=None, page_size=20):
            calls.append(("drive_list", query, page_size))
            return {
                "files": [
                    {"id": "file-1", "name": "Algo Spec", "mimeType": "application/pdf", "size": "42"}
                ]
            }

    monkeypatch.setattr(main.google_workspace, "GoogleWorkspaceClient", _FakeGoogleClient)

    main.run_google("drive-list name contains algo --max 2")

    assert not errors
    assert calls == [("drive_list", "name contains algo", 2)]
    assert "Algo Spec" in "\n".join(console.lines)
    assert "id=file-1" in "\n".join(console.lines)


def test_google_docs_sheets_calendar_subcommands_use_existing_client_methods(monkeypatch):
    _infos, errors, console = _patch_google_runtime(monkeypatch)
    calls: list[tuple] = []

    class _FakeGoogleClient:
        def docs_get(self, document_id: str):
            calls.append(("docs_get", document_id))
            return {"title": "Project Notes", "body": {}}

        def docs_to_plain_text(self, _document):
            return "Hello from docs\n"

        def sheets_values_get(self, spreadsheet_id: str, range_a1: str):
            calls.append(("sheets_values_get", spreadsheet_id, range_a1))
            return {"values": [["Name", "Status"], ["Algo", "Ready"]]}

        def calendar_events_list(self, *, time_min=None, time_max=None, max_results=20):
            calls.append(("calendar_events_list", time_min, time_max, max_results))
            return {
                "items": [
                    {
                        "id": "event-1",
                        "summary": "Planning",
                        "start": {"dateTime": "2026-07-01T10:00:00-05:00"},
                        "end": {"dateTime": "2026-07-01T10:30:00-05:00"},
                    }
                ]
            }

    monkeypatch.setattr(main.google_workspace, "GoogleWorkspaceClient", _FakeGoogleClient)

    main.run_google("docs-get doc-1")
    main.run_google("sheets-values sheet-1 'Sheet 1!A1:B2'")
    main.run_google("calendar-list --max 3 --time-min 2026-07-01T00:00:00Z")

    assert not errors
    assert calls == [
        ("docs_get", "doc-1"),
        ("sheets_values_get", "sheet-1", "Sheet 1!A1:B2"),
        ("calendar_events_list", "2026-07-01T00:00:00Z", None, 3),
    ]
    joined = "\n".join(console.lines)
    assert "Project Notes" in joined
    assert "Hello from docs" in joined
    assert "Name" in joined
    assert "Status" in joined
    assert "Planning" in joined
