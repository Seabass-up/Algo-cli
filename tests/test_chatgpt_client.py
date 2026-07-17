"""Offline tests for ChatGPT/OpenAI-compatible chat client adapter."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

from algo_cli import chatgpt_client


class _FakeJsonResponse:
    def __init__(self, body: dict[str, Any]):
        self._buf = io.BytesIO(json.dumps(body).encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()

    def close(self):
        pass


class _FakeStreamResponse:
    def __init__(self, events: list[dict[str, Any] | str]):
        self._lines = []
        for ev in events:
            data = ev if isinstance(ev, str) else json.dumps(ev)
            self._lines.append(f"data: {data}\n".encode("utf-8"))
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_missing_scope_cache(monkeypatch):
    monkeypatch.setattr(chatgpt_client, "_MODEL_REQUEST_SCOPE_MISSING", False)


def test_requires_chatgpt_oauth_token(monkeypatch):
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: None)
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="algo-cli config auth chatgpt login"):
        chatgpt_client._post_chat({"model": "gpt-5.1"}, stream=False)


def test_codex_subscription_model_without_tools_uses_backend(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(payload: dict[str, Any], **kw):
        captured.update(payload)
        return _FakeStreamResponse([{"type": "response.output_text.delta", "delta": "hello"}, "[DONE]"])

    monkeypatch.setattr(chatgpt_client, "_post_codex_responses", fake_post)
    monkeypatch.setattr(
        chatgpt_client,
        "_run_codex_exec",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("backend should be used first")),
    )

    chunk = chatgpt_client.ChatGptClient().chat(
        model="gpt-5.6-sol", messages=[{"role": "user", "content": "hi"}], stream=False
    )

    assert chunk["message"]["content"] == "hello"
    assert captured["model"] == "gpt-5.6-sol"
    assert "tools" not in captured
    assert "tool_choice" not in captured


def test_codex_subscription_model_passes_reasoning_effort_to_exec(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_exec(model: str, messages: list[dict[str, Any]], **kw) -> str:
        captured.update(kw)
        return "ok"

    monkeypatch.setattr(chatgpt_client, "_run_codex_exec", fake_exec)

    monkeypatch.setattr(chatgpt_client, "_MODEL_REQUEST_SCOPE_MISSING", True)
    chatgpt_client.ChatGptClient().chat(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
        options={"reasoning_effort": "high"},
    )

    assert captured["reasoning_effort"] == "high"


def test_codex_exec_preserves_multiline_stdout_when_output_file_is_missing(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "resolve_codex_bin", lambda: "codex")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "CODEX_AUTH_HOME", codex_home)

    result = chatgpt_client._run_codex_exec(
        "gpt-5.5",
        [{"role": "user", "content": "hi"}],
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="first line\nsecond line",
            stderr="",
        ),
    )

    assert result == "first line\nsecond line"


def test_chat_completion_stream_tolerates_invalid_tool_call_index():
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": None,
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    ]

    chunks = list(chatgpt_client._stream_iter(_FakeStreamResponse(events)))

    assert chunks[-1]["message"]["tool_calls"][0]["id"] == "call_1"


def test_gpt_56_aliases_and_reasoning_levels():
    assert chatgpt_client.normalize_codex_model("sol") == "gpt-5.6-sol"
    assert chatgpt_client.normalize_codex_model("lunna") == "gpt-5.6-luna"
    assert chatgpt_client.is_codex_subscription_model("terra") is True
    assert chatgpt_client.parse_reasoning_effort("max", "gpt-5.6-luna") == "max"
    assert chatgpt_client.parse_reasoning_effort("max", "gpt-5.5") == "xhigh"


def test_codex_model_discovery_uses_supported_protocol_and_hides_internal_models(monkeypatch):
    captured: dict[str, Any] = {}

    class _Response(_FakeJsonResponse):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(req: Any, timeout: float):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["timeout"] = timeout
        return _Response(
            {
                "models": [
                    {"slug": "gpt-5.6-sol", "visibility": "list"},
                    {"slug": "codex-auto-review", "visibility": "hide"},
                ]
            }
        )

    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_chatgpt_account_id", lambda: "acct_123")
    monkeypatch.setattr(chatgpt_client.urllib.request, "urlopen", fake_urlopen)

    models = chatgpt_client.get_codex_models(timeout=7)

    assert models == [{"slug": "gpt-5.6-sol", "visibility": "list"}]
    assert "client_version=0.144.2" in captured["url"]
    assert captured["headers"]["Chatgpt-account-id"] == "acct_123"
    assert captured["timeout"] == 7


def test_codex_subscription_model_with_tools_uses_chatgpt_backend(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(payload: dict[str, Any], **kw):
        captured.update(payload)
        return _FakeStreamResponse(
            [
                {"type": "response.output_item.added", "item": {"type": "function_call", "call_id": "c1", "name": "read_file"}},
                {"type": "response.function_call_arguments.delta", "call_id": "c1", "delta": "{\"path\":\"x\"}"},
                "[DONE]",
            ]
        )

    def fake_exec(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("tool-capable subscription calls must stay in Algo CLI's tool loop")

    monkeypatch.setattr(chatgpt_client, "_post_codex_responses", fake_post)
    monkeypatch.setattr(chatgpt_client, "_run_codex_exec", fake_exec)

    chunk = chatgpt_client.ChatGptClient().chat(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "read x"}],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}],
        stream=False,
    )

    assert captured["model"] == "gpt-5.5"
    assert captured["tools"][0]["name"] == "read_file"
    assert captured["tool_choice"] == "auto"
    assert captured["parallel_tool_calls"] is True
    assert chunk["message"]["tool_calls"][0]["function"]["name"] == "read_file"


def test_nonstream_response_is_ollama_shaped(monkeypatch):
    monkeypatch.setattr(
        chatgpt_client,
        "_post_chat",
        lambda payload, **kw: _FakeJsonResponse(
            {"choices": [{"message": {"content": "hello"}}], "usage": {"total_tokens": 3}}
        ),
    )
    chunk = chatgpt_client.ChatGptClient().chat(
        model="gpt-5.1", messages=[{"role": "user", "content": "hi"}], stream=False
    )
    assert chunk["message"]["content"] == "hello"
    assert chunk["usage"] == {"total_tokens": 3}


def test_stream_content_deltas(monkeypatch):
    events = [
        {"choices": [{"delta": {"content": "hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        "[DONE]",
    ]
    monkeypatch.setattr(chatgpt_client, "_post_chat", lambda payload, **kw: _FakeStreamResponse(events))
    chunks = list(chatgpt_client.ChatGptClient().chat(model="gpt-5.1", messages=[], stream=True))
    assert [c["message"].get("content") for c in chunks] == ["hel", "lo"]


def test_payload_preserves_openai_tool_choice_none_for_no_tools(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(payload: dict[str, Any], **kw):
        captured.update(payload)
        return _FakeJsonResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(chatgpt_client, "_post_chat", fake_post)
    chatgpt_client.ChatGptClient().chat(model="gpt-5.1", messages=[], tools=None, stream=False)

    assert captured["model"] == "gpt-5.1"
    assert "tools" not in captured


def test_payload_includes_reasoning_effort_from_options(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(payload: dict[str, Any], **kw):
        captured.update(payload)
        return _FakeStreamResponse([{"type": "response.output_text.delta", "delta": "ok"}, "[DONE]"])

    monkeypatch.setattr(chatgpt_client, "_post_codex_responses", fake_post)
    chatgpt_client.ChatGptClient().chat(
        model="gpt-5.5",
        messages=[],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}],
        stream=False,
        options={"reasoning_effort": "max"},
    )

    assert captured["reasoning"]["effort"] == "xhigh"
    assert captured["reasoning"]["summary"] == "auto"


def test_gpt_56_payload_preserves_max_reasoning_effort(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(payload: dict[str, Any], **kw):
        captured.update(payload)
        return _FakeStreamResponse([{"type": "response.output_text.delta", "delta": "ok"}, "[DONE]"])

    monkeypatch.setattr(chatgpt_client, "_post_codex_responses", fake_post)
    chatgpt_client.ChatGptClient().chat(
        model="gpt-5.6-terra",
        messages=[],
        stream=False,
        options={"reasoning_effort": "max"},
    )

    assert captured["reasoning"]["effort"] == "max"
    assert captured["reasoning"]["context"] == "all_turns"
    assert captured["parallel_tool_calls"] is False


def test_codex_backend_does_not_forward_ollama_num_predict(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(payload: dict[str, Any], **kw):
        captured.update(payload)
        return _FakeStreamResponse([{"type": "response.output_text.delta", "delta": "ok"}, "[DONE]"])

    monkeypatch.setattr(chatgpt_client, "_post_codex_responses", fake_post)
    chatgpt_client.ChatGptClient().chat(
        model="gpt-5.6-luna",
        messages=[],
        stream=False,
        options={"num_predict": 32},
    )

    assert "max_output_tokens" not in captured


def test_codex_reasoning_summary_streams_as_thinking():
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "Checking the plan"},
        {"type": "response.output_text.delta", "delta": "Done"},
        "[DONE]",
    ]

    chunks = list(chatgpt_client._stream_codex_responses_iter(_FakeStreamResponse(events)))

    assert chunks[0]["message"]["thinking"] == "Checking the plan"
    assert chunks[1]["message"]["content"] == "Done"


def test_responses_input_drops_malformed_empty_tool_call_names():
    built = chatgpt_client._build_responses_input(
        [
            {"role": "user", "content": "status"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "", "arguments": "{\"command\":\"/status\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_bad", "name": "", "content": "Unknown tool"},
        ]
    )

    assert not [item for item in built if item.get("type") == "function_call"]
    assert not [item for item in built if item.get("type") == "function_call_output"]


def test_responses_input_drops_orphaned_tool_calls_without_outputs():
    built = chatgpt_client._build_responses_input(
        [
            {"role": "user", "content": "open picker"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_orphan",
                        "type": "function",
                        "function": {"name": "session_command", "arguments": "{\"command\":\"/models\"}"},
                    }
                ],
            },
            {"role": "user", "content": "continue"},
        ]
    )

    assert not [item for item in built if item.get("call_id") == "call_orphan"]
    assert built[-1] == {"role": "user", "content": "continue"}


def test_responses_input_keeps_tool_calls_with_outputs():
    built = chatgpt_client._build_responses_input(
        [
            {"role": "user", "content": "status"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_ok",
                        "type": "function",
                        "function": {"name": "session_command", "arguments": "{\"command\":\"/status\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_ok", "name": "session_command", "content": "Executed: /status"},
        ]
    )

    assert {"type": "function_call", "call_id": "call_ok", "name": "session_command", "arguments": "{\"command\":\"/status\"}"} in built
    assert {"type": "function_call_output", "call_id": "call_ok", "output": "Executed: /status"} in built


def test_chat_messages_pair_missing_tool_result_ids_by_call_sequence_for_duplicate_names():
    built = chatgpt_client._build_openai_messages(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"}},
                    {"id": "call_2", "function": {"name": "read_file", "arguments": "{\"path\":\"b\"}"}},
                ],
            },
            {"role": "tool", "name": "read_file", "content": "first"},
            {"role": "tool", "name": "read_file", "content": "second"},
        ]
    )

    assert built[1] == {"role": "tool", "tool_call_id": "call_1", "content": "first"}
    assert built[2] == {"role": "tool", "tool_call_id": "call_2", "content": "second"}


def test_chat_messages_drop_tool_results_with_untrusted_explicit_call_id():
    built = chatgpt_client._build_openai_messages(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_real", "function": {"name": "read_file", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "name": "read_file", "tool_call_id": "call_injected", "content": "bad"},
            {"role": "tool", "name": "read_file", "content": "good"},
        ]
    )

    assert {"role": "tool", "tool_call_id": "call_injected", "content": "bad"} not in built
    assert built[1] == {"role": "tool", "tool_call_id": "call_real", "content": "good"}


def test_responses_input_drops_tool_outputs_without_explicit_call_id_for_duplicate_names():
    built = chatgpt_client._build_responses_input(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "read_file", "arguments": "{\"path\":\"a\"}"}},
                    {"id": "call_2", "function": {"name": "read_file", "arguments": "{\"path\":\"b\"}"}},
                ],
            },
            {"role": "tool", "name": "read_file", "content": "first"},
            {"role": "tool", "name": "read_file", "content": "second"},
        ]
    )

    assert not [item for item in built if item.get("type") == "function_call"]
    assert not [item for item in built if item.get("type") == "function_call_output"]


def test_codex_responses_stream_drops_nameless_tool_calls():
    events = [
        {"type": "response.function_call_arguments.delta", "call_id": "call_bad", "delta": "{\"command\":\"/status\"}"},
        "[DONE]",
    ]

    chunks = list(chatgpt_client._stream_codex_responses_iter(_FakeStreamResponse(events)))

    assert chunks == []


def test_codex_responses_stream_content_and_tool_calls(monkeypatch):
    events = [
        {"type": "response.output_text.delta", "delta": "I'll read it."},
        {"type": "response.output_item.added", "item": {"type": "function_call", "call_id": "c1", "name": "read_file"}},
        {"type": "response.function_call_arguments.delta", "call_id": "c1", "delta": "{\"path\":\""},
        {"type": "response.function_call_arguments.delta", "call_id": "c1", "delta": "x\"}"},
        "[DONE]",
    ]
    monkeypatch.setattr(chatgpt_client, "_post_codex_responses", lambda payload, **kw: _FakeStreamResponse(events))

    chunks = list(
        chatgpt_client.ChatGptClient().chat(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "read x"}],
            tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {}}}}],
            stream=True,
        )
    )

    assert chunks[0]["message"]["content"] == "I'll read it."
    assert chunks[-1]["message"]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": "{\"path\":\"x\"}"}


def test_post_codex_responses_requires_account_id(monkeypatch):
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_chatgpt_account_id", lambda: None)

    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="account id"):
        chatgpt_client._post_codex_responses({"model": "gpt-5.5"})


def test_post_codex_responses_uses_chatgpt_backend_headers(monkeypatch):
    captured: dict[str, Any] = {}

    class _Response:
        pass

    def fake_urlopen(req: Any, timeout: float):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_chatgpt_account_id", lambda: "acct_123")
    monkeypatch.setattr(chatgpt_client.urllib.request, "urlopen", fake_urlopen)

    chatgpt_client._post_codex_responses({"model": "gpt-5.5"}, timeout=12)

    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["headers"]["Chatgpt-account-id"] == "acct_123"
    assert captured["headers"]["Originator"] == "pi"
    assert captured["headers"]["Openai-beta"] == "responses=experimental"
    assert "User-agent" in captured["headers"]


def test_gpt_56_uses_codex_responses_lite_header(monkeypatch):
    captured: dict[str, Any] = {}

    class _Response:
        pass

    def fake_urlopen(req: Any, timeout: float):
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_chatgpt_account_id", lambda: "acct_123")
    monkeypatch.setattr(chatgpt_client.urllib.request, "urlopen", fake_urlopen)

    chatgpt_client._post_codex_responses({"model": "gpt-5.6-luna"})

    assert captured["headers"]["X-openai-internal-codex-responses-lite"] == "true"
    assert captured["headers"]["Originator"] == "codex_cli_rs"
    assert captured["headers"]["User-agent"] == "codex_cli_rs/0.144.2"


def test_post_codex_responses_refreshes_once_on_token_invalidated(monkeypatch):
    calls: list[str] = []

    class _Response:
        pass

    def fake_urlopen(req: Any, timeout: float):
        calls.append(dict(req.header_items())["Authorization"])
        if len(calls) == 1:
            raise chatgpt_client.urllib.error.HTTPError(
                req.full_url,
                401,
                "Unauthorized",
                hdrs={},
                fp=io.BytesIO(b'{"error":{"code":"token_invalidated","message":"Your authentication token has been invalidated."}}'),
            )
        return _Response()

    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "old-token" if len(calls) == 0 else "new-token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_chatgpt_account_id", lambda: "acct_123")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "force_refresh_token", lambda: "new-token")
    monkeypatch.setattr(chatgpt_client.urllib.request, "urlopen", fake_urlopen)

    assert isinstance(chatgpt_client._post_codex_responses({"model": "gpt-5.5"}), _Response)
    assert calls == ["Bearer old-token", "Bearer new-token"]


def test_post_codex_responses_clears_invalidated_session_when_refresh_fails(monkeypatch):
    cleared: list[bool] = []

    def reject(req: Any, timeout: float):
        raise chatgpt_client.urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs={},
            fp=io.BytesIO(
                b'{"error":{"code":"token_invalidated","message":"Your authentication token has been invalidated."}}'
            ),
        )

    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "old-token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_chatgpt_account_id", lambda: "acct_123")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "force_refresh_token", lambda: None)
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "clear_tokens", lambda: cleared.append(True) or True)
    monkeypatch.setattr(chatgpt_client.urllib.request, "urlopen", reject)

    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError) as exc_info:
        chatgpt_client._post_codex_responses({"model": "gpt-5.6-terra"})

    message = str(exc_info.value)
    assert "sign-in expired or was revoked" in message
    assert "algo-cli config setup chatgpt" in message
    assert "token_invalidated" not in message
    assert cleared == [True]


def test_post_codex_responses_clears_session_when_refreshed_token_is_rejected(monkeypatch):
    calls: list[str] = []
    cleared: list[bool] = []

    def reject(req: Any, timeout: float):
        calls.append(dict(req.header_items())["Authorization"])
        raise chatgpt_client.urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error":{"code":"token_invalidated"}}'),
        )

    monkeypatch.setattr(
        chatgpt_client.chatgpt_auth,
        "get_valid_token",
        lambda: "old-token" if not calls else "new-token",
    )
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_chatgpt_account_id", lambda: "acct_123")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "force_refresh_token", lambda: "new-token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "clear_tokens", lambda: cleared.append(True) or True)
    monkeypatch.setattr(chatgpt_client.urllib.request, "urlopen", reject)

    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="config setup chatgpt"):
        chatgpt_client._post_codex_responses({"model": "gpt-5.6-sol"})

    assert calls == ["Bearer old-token", "Bearer new-token"]
    assert cleared == [True]


def test_post_chat_clears_invalidated_session_after_failed_refresh(monkeypatch):
    cleared: list[bool] = []

    def reject(req: Any, timeout: float):
        raise chatgpt_client.urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error":{"code":"token_invalidated"}}'),
        )

    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "old-token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "force_refresh_token", lambda: None)
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "clear_tokens", lambda: cleared.append(True) or True)
    monkeypatch.setattr(chatgpt_client.urllib.request, "urlopen", reject)

    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="config setup chatgpt"):
        chatgpt_client._post_chat({"model": "gpt-5.4"}, stream=False)

    assert cleared == [True]
