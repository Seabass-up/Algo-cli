from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from algo_cli import chatgpt_client
from algo_cli import google_workspace
from algo_cli import google_workspace_auth
from algo_cli import xai_client


class _FakeSseResponse:
    def __init__(self, events: list[dict[str, Any] | str]) -> None:
        self._lines = []
        for ev in events:
            data = ev if isinstance(ev, str) else json.dumps(ev)
            self._lines.append(f"data: {data}\n\n".encode("utf-8"))
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# D1: Google Docs walker reads the real structural-element shape
# ---------------------------------------------------------------------------


def _text_run(text: str) -> dict[str, Any]:
    return {"textRun": {"content": text, "textStyle": {}}}


def _docs_fixture() -> dict[str, Any]:
    return {
        "title": "Fixture doc",
        "body": {
            "content": [
                {"sectionBreak": {"sectionStyle": {}}},
                {
                    "paragraph": {
                        "elements": [_text_run("Hello "), _text_run("world\n")],
                        "paragraphStyle": {},
                    }
                },
                {"paragraph": {"elements": [_text_run("Second\n")]}},
                {
                    "table": {
                        "rows": 1,
                        "columns": 2,
                        "tableRows": [
                            {
                                "tableCells": [
                                    {"content": [{"paragraph": {"elements": [_text_run("A\n")]}}]},
                                    {"content": [{"paragraph": {"elements": [_text_run("B\n")]}}]},
                                ]
                            }
                        ],
                    }
                },
            ]
        },
    }


def test_docs_plain_text_collects_paragraphs_and_table_cells_in_order():
    client = google_workspace.GoogleWorkspaceClient()

    text = client.docs_to_plain_text(_docs_fixture())

    assert text == "Hello world\nSecond\nA\tB\n"


def test_docs_plain_text_walks_table_of_contents():
    client = google_workspace.GoogleWorkspaceClient()
    document = {
        "body": {
            "content": [
                {
                    "tableOfContents": {
                        "content": [{"paragraph": {"elements": [_text_run("Intro\n")]}}],
                    }
                },
                {"paragraph": {"elements": [_text_run("Body\n")]}},
            ]
        }
    }

    assert client.docs_to_plain_text(document) == "Intro\nBody\n"


# ---------------------------------------------------------------------------
# D2 and D3: Google token file handling and best-effort revoke
# ---------------------------------------------------------------------------


@pytest.fixture
def google_auth_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "google_workspace_auth.json"
    monkeypatch.setattr(google_workspace_auth, "AUTH_FILE", path)
    return path


def test_load_tokens_treats_non_utf8_file_as_missing(google_auth_file: Path):
    google_auth_file.write_bytes(b"\xff\xfe\x00{not utf-8")

    assert google_workspace_auth.load_tokens() is None


def test_clear_tokens_removes_non_utf8_token_file(google_auth_file: Path):
    google_auth_file.write_bytes(b"\xff\xfe\x00 garbage")

    assert google_workspace_auth.clear_tokens() is True
    assert not google_auth_file.exists()


def _raise_timeout(*_args: Any, **_kwargs: Any) -> None:
    raise TimeoutError("timed out")


def test_revoke_token_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(urllib.request, "urlopen", _raise_timeout)

    assert google_workspace_auth.revoke_token("rt-value") is False


def test_clear_tokens_deletes_local_file_when_remote_revoke_times_out(
    google_auth_file: Path, monkeypatch: pytest.MonkeyPatch
):
    google_auth_file.write_text(
        json.dumps({"access_token": "at-value", "refresh_token": "rt-value"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(urllib.request, "urlopen", _raise_timeout)

    assert google_workspace_auth.clear_tokens() is True
    assert not google_auth_file.exists()


# ---------------------------------------------------------------------------
# D4: a streamed error event must not end the stream as a successful reply
# ---------------------------------------------------------------------------


_ERROR_EVENT = {"error": {"message": "upstream exploded", "type": "server_error"}}


def test_chatgpt_stream_raises_on_error_event_without_choices():
    response = _FakeSseResponse(
        [
            {"choices": [{"delta": {"content": "partial "}}]},
            _ERROR_EVENT,
        ]
    )

    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="upstream exploded"):
        list(chatgpt_client._stream_iter(response))


def test_chatgpt_stream_still_skips_empty_choices_without_error():
    response = _FakeSseResponse(
        [
            {"choices": []},
            {"choices": [{"delta": {"content": "ok"}}]},
        ]
    )

    chunks = list(chatgpt_client._stream_iter(response))

    assert [c["message"]["content"] for c in chunks] == ["ok"]


def test_xai_stream_raises_on_error_event_without_choices():
    response = _FakeSseResponse(
        [
            {"choices": [{"delta": {"content": "partial "}}]},
            _ERROR_EVENT,
        ]
    )

    with pytest.raises(RuntimeError, match="upstream exploded"):
        list(xai_client._stream_iter(response))


def test_xai_stream_error_about_credits_uses_access_error_type():
    response = _FakeSseResponse(
        [{"error": {"message": "You have insufficient credits on this account", "type": "billing"}}]
    )

    with pytest.raises(xai_client.XaiApiAccessError):
        list(xai_client._stream_iter(response))


def test_xai_stream_still_skips_empty_choices_without_error():
    response = _FakeSseResponse(
        [
            {"choices": []},
            {"choices": [{"delta": {"content": "ok"}}]},
        ]
    )

    chunks = list(xai_client._stream_iter(response))

    assert [c["message"]["content"] for c in chunks] == ["ok"]
