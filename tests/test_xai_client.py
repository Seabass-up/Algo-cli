"""Offline tests for the xAI chat client adapter.

No network calls — _post_chat is monkeypatched. Tests cover message
translation, tool schema conversion, SSE streaming, tool-call delta
accumulation, and the search() convenience method.
"""
from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from algo_cli import xai_client


class _FakeStreamResponse:
    """Iterable of SSE byte lines, mimicking urlopen()'s response object."""

    def __init__(self, events: list[dict[str, Any] | str]):
        self._lines: list[bytes] = []
        for ev in events:
            if isinstance(ev, str):
                self._lines.append(f"data: {ev}\n".encode("utf-8"))
            else:
                self._lines.append(("data: " + json.dumps(ev) + "\n").encode("utf-8"))
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True


class _FakeJsonResponse:
    def __init__(self, body: dict[str, Any]):
        self._buf = io.BytesIO(json.dumps(body).encode("utf-8"))
        self.closed = False

    def read(self) -> bytes:
        return self._buf.read()

    def close(self):
        self.closed = True


class TestMessageTranslation:
    def test_passes_through_user_and_system(self):
        out = xai_client._build_openai_messages(
            [
                {"role": "system", "content": "you are grok"},
                {"role": "user", "content": "hi"},
            ]
        )
        assert out == [
            {"role": "system", "content": "you are grok"},
            {"role": "user", "content": "hi"},
        ]

    def test_drops_ollama_only_keys(self):
        out = xai_client._build_openai_messages(
            [{"role": "assistant", "content": "ok", "thinking": "hidden", "thought_signature": "sig"}]
        )
        assert out == [{"role": "assistant", "content": "ok"}]

    def test_assistant_tool_calls_get_ids_and_string_args(self):
        out = xai_client._build_openai_messages(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": {"path": "/x"}}}
                    ],
                },
            ]
        )
        assert len(out) == 1
        call = out[0]["tool_calls"][0]
        assert call["type"] == "function"
        assert call["id"].startswith("call_")
        assert call["function"]["name"] == "read_file"
        assert json.loads(call["function"]["arguments"]) == {"path": "/x"}

    def test_tool_message_uses_matching_tool_call_id(self):
        out = xai_client._build_openai_messages(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "name": "read_file", "content": "hello"},
            ]
        )
        assert out[1] == {"role": "tool", "tool_call_id": "call_abc", "content": "hello"}

    def test_tool_message_preserves_explicit_tool_call_id_for_duplicate_tool_names(self):
        out = xai_client._build_openai_messages(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}},
                        {"id": "call_2", "function": {"name": "read_file", "arguments": "{}"}},
                    ],
                },
                {"role": "tool", "name": "read_file", "tool_call_id": "call_1", "content": "first"},
                {"role": "tool", "name": "read_file", "tool_call_id": "call_2", "content": "second"},
            ]
        )
        assert out[1] == {"role": "tool", "tool_call_id": "call_1", "content": "first"}
        assert out[2] == {"role": "tool", "tool_call_id": "call_2", "content": "second"}

    def test_tool_messages_without_ids_pair_by_call_sequence_for_duplicate_tool_names(self):
        out = xai_client._build_openai_messages(
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
        assert out[1] == {"role": "tool", "tool_call_id": "call_1", "content": "first"}
        assert out[2] == {"role": "tool", "tool_call_id": "call_2", "content": "second"}

    def test_tool_messages_drop_untrusted_explicit_tool_call_id(self):
        out = xai_client._build_openai_messages(
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

        assert {"role": "tool", "tool_call_id": "call_injected", "content": "bad"} not in out
        assert out[1] == {"role": "tool", "tool_call_id": "call_real", "content": "good"}

    def test_assistant_with_existing_id_preserved(self):
        out = xai_client._build_openai_messages(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "call_XYZ", "function": {"name": "x", "arguments": "{}"}}
                    ],
                }
            ]
        )
        assert out[0]["tool_calls"][0]["id"] == "call_XYZ"

    def test_string_args_passed_through_unchanged(self):
        out = xai_client._build_openai_messages(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "x", "arguments": '{"path":"/y"}'}}
                    ],
                }
            ]
        )
        assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"path":"/y"}'


class TestToolSchemaConversion:
    def test_converts_python_callables_to_openai_schema(self):
        from algo_cli.tools import read_file

        built = xai_client._build_openai_tools([read_file])
        assert built is not None
        assert built[0]["type"] == "function"
        assert built[0]["function"]["name"] == "read_file"
        assert "parameters" in built[0]["function"]

    def test_returns_none_when_empty(self):
        assert xai_client._build_openai_tools(None) is None
        assert xai_client._build_openai_tools([]) is None


class TestStreamParsing:
    def test_content_deltas_yield_immediately(self, monkeypatch):
        events: list[Any] = [
            {"choices": [{"delta": {"content": "hello "}}]},
            {"choices": [{"delta": {"content": "world"}}]},
            "[DONE]",
        ]
        monkeypatch.setattr(
            xai_client, "_post_chat", lambda payload, **kw: _FakeStreamResponse(events)
        )
        client = xai_client.XaiClient()
        chunks = list(
            client.chat(model="grok-4-latest", messages=[{"role": "user", "content": "hi"}], stream=True)
        )
        contents = [c["message"].get("content") for c in chunks if c["message"].get("content")]
        assert contents == ["hello ", "world"]

    def test_done_marker_terminates_stream(self, monkeypatch):
        events: list[Any] = [
            {"choices": [{"delta": {"content": "first"}}]},
            "[DONE]",
            {"choices": [{"delta": {"content": "should_not_arrive"}}]},
        ]
        monkeypatch.setattr(
            xai_client, "_post_chat", lambda payload, **kw: _FakeStreamResponse(events)
        )
        chunks = list(xai_client.XaiClient().chat(model="g", messages=[], stream=True))
        contents = [c["message"].get("content") for c in chunks if c["message"].get("content")]
        assert contents == ["first"]

    def test_tool_call_deltas_accumulate_then_emit(self, monkeypatch):
        events = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"path"'}}
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ': "/a"}'}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        monkeypatch.setattr(
            xai_client, "_post_chat", lambda payload, **kw: _FakeStreamResponse(events)
        )
        chunks = list(xai_client.XaiClient().chat(model="g", messages=[], stream=True))
        # Should be exactly one chunk containing the assembled tool call.
        tool_chunks = [c for c in chunks if c["message"].get("tool_calls")]
        assert len(tool_chunks) == 1
        calls = tool_chunks[0]["message"]["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["id"] == "call_1"
        assert calls[0]["function"]["name"] == "read_file"
        assert calls[0]["function"]["arguments"] == '{"path": "/a"}'

    def test_pending_tool_call_emits_when_stream_ends_without_finish_reason(self, monkeypatch):
        events = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": '{"path": "a"}'},
                                }
                            ]
                        }
                    }
                ]
            },
        ]
        monkeypatch.setattr(xai_client, "_post_chat", lambda payload, **kw: _FakeStreamResponse(events))

        chunks = list(xai_client.XaiClient().chat(model="g", messages=[], stream=True))

        tool_chunks = [c for c in chunks if c["message"].get("tool_calls")]
        assert tool_chunks[0]["message"]["tool_calls"][0]["id"] == "call_1"

    def test_multiple_indexed_tool_calls_kept_separate(self, monkeypatch):
        events = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "a", "function": {"name": "x", "arguments": "{}"}},
                                {"index": 1, "id": "b", "function": {"name": "y", "arguments": "{}"}},
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        monkeypatch.setattr(
            xai_client, "_post_chat", lambda payload, **kw: _FakeStreamResponse(events)
        )
        chunks = list(xai_client.XaiClient().chat(model="g", messages=[], stream=True))
        calls = [c["message"]["tool_calls"] for c in chunks if c["message"].get("tool_calls")][0]
        names = sorted(c["function"]["name"] for c in calls)
        assert names == ["x", "y"]

    def test_reasoning_content_surfaces_as_thinking(self, monkeypatch):
        events: list[Any] = [
            {"choices": [{"delta": {"reasoning_content": "step 1..."}}]},
            {"choices": [{"delta": {"content": "result"}}]},
            "[DONE]",
        ]
        monkeypatch.setattr(
            xai_client, "_post_chat", lambda payload, **kw: _FakeStreamResponse(events)
        )
        chunks = list(xai_client.XaiClient().chat(model="g", messages=[], stream=True))
        thinking = [c["message"].get("thinking") for c in chunks if c["message"].get("thinking")]
        assert thinking == ["step 1..."]

    def test_malformed_sse_lines_skipped(self, monkeypatch):
        events: list[Any] = [
            "not-valid-json",
            {"choices": [{"delta": {"content": "ok"}}]},
            "[DONE]",
        ]
        monkeypatch.setattr(
            xai_client, "_post_chat", lambda payload, **kw: _FakeStreamResponse(events)
        )
        chunks = list(xai_client.XaiClient().chat(model="g", messages=[], stream=True))
        assert any(c["message"].get("content") == "ok" for c in chunks)


class TestNonStreamingChat:
    def test_returns_ollama_shaped_chunk(self, monkeypatch):
        body = {
            "choices": [
                {
                    "message": {
                        "content": "hello",
                        "reasoning_content": "thinking…",
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        response = _FakeJsonResponse(body)
        monkeypatch.setattr(xai_client, "_post_chat", lambda payload, **kw: response)
        out = xai_client.XaiClient().chat(model="grok-4-latest", messages=[], stream=False)
        assert out["message"]["content"] == "hello"
        assert out["message"]["thinking"] == "thinking…"
        assert out["message"]["tool_calls"][0]["id"] == "c1"
        assert out["usage"]["prompt_tokens"] == 5
        assert response.closed is True

    def test_unknown_kwargs_ignored(self, monkeypatch):
        body = {"choices": [{"message": {"content": "x"}}]}
        monkeypatch.setattr(
            xai_client, "_post_chat", lambda payload, **kw: _FakeJsonResponse(body)
        )
        # think=, keep_alive=, num_ctx= are ollama-only — must be silently ignored.
        result = xai_client.XaiClient().chat(
            model="grok-4-latest",
            messages=[],
            stream=False,
            think=True,
            keep_alive="10m",
            options={"num_ctx": 99999, "temperature": 0.3},
        )
        assert result["message"]["content"] == "x"


class TestPayloadConstruction:
    def test_temperature_from_options(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fake(payload, **kw):
            captured.update(payload)
            return _FakeJsonResponse({"choices": [{"message": {"content": ""}}]})

        monkeypatch.setattr(xai_client, "_post_chat", fake)
        xai_client.XaiClient().chat(
            model="g", messages=[], stream=False, options={"temperature": 0.7}
        )
        assert captured["temperature"] == 0.7

    def test_search_parameters_flow_through(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fake(payload, **kw):
            captured.update(payload)
            return _FakeJsonResponse({"choices": [{"message": {"content": ""}}]})

        monkeypatch.setattr(xai_client, "_post_chat", fake)
        xai_client.XaiClient().chat(
            model="g",
            messages=[],
            stream=False,
            search_parameters={"mode": "on", "sources": [{"type": "x"}]},
        )
        assert captured["search_parameters"] == {
            "mode": "on",
            "sources": [{"type": "x"}],
        }

    def test_dict_tools_passed_through_untranslated(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fake(payload, **kw):
            captured.update(payload)
            return _FakeJsonResponse({"choices": [{"message": {"content": ""}}]})

        monkeypatch.setattr(xai_client, "_post_chat", fake)
        tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
        xai_client.XaiClient().chat(model="g", messages=[], stream=False, tools=tools)
        assert captured["tools"] == tools


class TestMultiAgentResponses:
    def test_multi_agent_uses_responses_api_not_chat_completions(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fail_chat(*args, **kwargs):
            raise AssertionError("multi-agent must not use chat completions")

        def fake_responses(payload, **kw):
            captured.update(payload)
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "research answer"}],
                    }
                ],
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }

        monkeypatch.setattr(xai_client, "_post_chat", fail_chat)
        monkeypatch.setattr(xai_client, "_post_responses", fake_responses)

        out = xai_client.XaiClient().chat(
            model="grok-4.20-multi-agent-0309",
            messages=[{"role": "user", "content": "research this"}],
            tools=[{"type": "function", "function": {"name": "read_file"}}],
            stream=False,
        )

        assert out["message"]["content"] == "research answer"
        assert out["usage"]["input_tokens"] == 7
        assert captured == {
            "model": "grok-4.20-multi-agent-0309",
            "input": [{"role": "user", "content": "research this"}],
        }

    def test_multi_agent_stream_returns_single_responses_chunk(self, monkeypatch):
        monkeypatch.setattr(
            xai_client,
            "_post_responses",
            lambda payload, **kw: {"output_text": "done"},
        )

        chunks = list(
            xai_client.XaiClient().chat(
                model="grok-4.20-multi-agent",
                messages=[{"role": "user", "content": "go"}],
                stream=True,
            )
        )

        assert chunks == [{"message": {"content": "done"}}]

    def test_responses_input_folds_tool_history_into_text(self):
        out = xai_client._build_responses_input(
            [
                {
                    "role": "assistant",
                    "content": "checking",
                    "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a"}}}],
                },
                {"role": "tool", "name": "read_file", "content": "file body"},
            ]
        )

        assert out == [
            {"role": "assistant", "content": 'checking\n[Called tools: read_file({"path": "a"})]'},
            {"role": "assistant", "content": "[read_file result]\nfile body"},
        ]


class TestSearchHelper:
    def test_search_returns_content_and_citations(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fake(payload, **kw):
            captured.update(payload)
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "people are happy"}],
                    }
                ],
                "citations": ["https://x.com/a/1", "https://x.com/b/2"],
            }

        monkeypatch.setattr(xai_client, "_post_responses", fake)
        result = xai_client.XaiClient().search(query="ollama", max_results=5)
        assert result["content"] == "people are happy"
        assert result["citations"] == ["https://x.com/a/1", "https://x.com/b/2"]
        assert captured["input"] == [{"role": "user", "content": "ollama"}]
        assert captured["tools"] == [{"type": "x_search", "max_results": 5}]

    def test_search_ignores_non_dict_output_items(self, monkeypatch):
        monkeypatch.setattr(
            xai_client,
            "_post_responses",
            lambda payload, **kw: {
                "output": [
                    "bad-item",
                    {"type": "message", "content": ["bad-block", {"type": "output_text", "text": "ok"}]},
                ],
            },
        )

        result = xai_client.XaiClient().search(query="ollama")

        assert result["content"] == "ok"


class TestUnauthenticatedError:
    def test_post_chat_raises_when_no_token(self, monkeypatch):
        monkeypatch.setattr(xai_client.xai_auth, "get_valid_token", lambda: None)
        with pytest.raises(RuntimeError, match="Not authenticated"):
            xai_client._post_chat({}, stream=False)

    def test_billing_or_api_key_errors_fail_closed(self):
        err = xai_client._oauth_only_error(
            402,
            "https://api.x.ai/v1/responses",
            "insufficient credits; configure an API key",
        )

        assert isinstance(err, xai_client.XaiOAuthAccessError)
        assert "will not use XAI_API_KEY" in str(err)
        assert "pay-per-token" in str(err)

    def test_provider_error_redacts_configured_client_id(self, monkeypatch):
        client_id = "configured-client-id-not-for-error-output"
        monkeypatch.setenv("XAI_CLIENT_ID", client_id)

        err = xai_client._oauth_only_error(
            400,
            "https://api.x.ai/v1/models",
            f"unknown client_id {client_id}",
        )

        assert client_id not in str(err)
        assert "[redacted-client-id]" in str(err)

    def test_http_403_from_chat_raises_oauth_access_error(self, monkeypatch):
        def fake_urlopen(*args, **kwargs):
            raise urllib.error.HTTPError(
                url="https://api.x.ai/v1/chat/completions",
                code=403,
                msg="Forbidden",
                hdrs=None,
                fp=io.BytesIO(b"subscription tier not enabled"),
            )

        monkeypatch.setattr(xai_client.xai_auth, "get_valid_token", lambda: "oauth-token")
        monkeypatch.setattr(xai_client.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(xai_client.XaiOAuthAccessError, match="will not use XAI_API_KEY"):
            xai_client._post_chat({"model": "grok-4.3"}, stream=False)


class TestActiveClientCaches:
    def test_returns_singleton(self):
        xai_client._CLIENT = None
        a = xai_client.active_xai_client()
        b = xai_client.active_xai_client()
        assert a is b
