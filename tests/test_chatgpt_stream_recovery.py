"""Offline Responses recovery tests: retries must not replay delivered actions."""

from __future__ import annotations

import io
import json
import ssl
import urllib.error
from typing import Any

import pytest

from algo_cli import chatgpt_client


class StreamResponse:
    def __init__(self, events: list[Any], failure: BaseException | None = None):
        self.events = events
        self.failure = failure
        self.closed = False

    def __iter__(self):
        for event in self.events:
            data = event if isinstance(event, str) else json.dumps(event)
            yield f"data: {data}\n\n".encode()
        if self.failure is not None:
            raise self.failure

    def close(self):
        self.closed = True


def text_item(text: str, item_id: str = "msg_1") -> dict[str, Any]:
    return {"type": "message", "id": item_id, "content": [{"type": "output_text", "text": text}]}


def call_item(call_id: str = "call_1", arguments: str = '{"path":"README.md"}') -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": "read_file",
        "arguments": arguments,
    }


def completed(*items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.completed", "response": {"status": "completed", "output": list(items)}}


def content(chunks: list[dict[str, Any]]) -> str:
    return "".join(chunk.get("message", {}).get("content", "") for chunk in chunks)


@pytest.fixture
def posts(monkeypatch):
    requests: list[dict[str, Any]] = []
    responses: list[StreamResponse | BaseException] = []
    sleeps: list[float] = []

    def post(payload, **_kwargs):
        requests.append(json.loads(json.dumps(payload)))
        response = responses[len(requests) - 1]
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(chatgpt_client, "_MODEL_REQUEST_SCOPE_MISSING", False)
    monkeypatch.setattr(chatgpt_client, "_post_codex_responses", post)
    monkeypatch.setattr(chatgpt_client.time, "sleep", sleeps.append)
    return requests, responses, sleeps


@pytest.mark.parametrize("snapshot", ["item", "response"])
@pytest.mark.parametrize("refusal", [False, True])
def test_final_snapshot_only_text_is_not_lost(snapshot, refusal):
    item = text_item("Final answer")
    if refusal:
        item["content"] = [{"type": "refusal", "refusal": "Final answer"}]
    event = completed(item) if snapshot == "response" else {"type": "response.output_item.done", "item": item}
    response = StreamResponse([event, "[DONE]"])
    assert content(list(chatgpt_client._stream_codex_responses_iter(response))) == "Final answer"
    assert response.closed


def test_snapshots_recover_missing_suffix_without_repeating_deltas():
    response = StreamResponse(
        [
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "Hello"},
            {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "Hello world"},
            {"type": "response.output_item.done", "output_index": 0, "item": text_item("Hello world")},
            completed(text_item("Hello world"), text_item("Second message", "msg_2")),
        ]
    )
    assert content(list(chatgpt_client._stream_codex_responses_iter(response))) == "Hello worldSecond message"


@pytest.mark.parametrize("snapshot", ["delta", "item"])
def test_sparse_terminal_snapshot_matches_text_by_item_id(snapshot):
    item = text_item("Verified result")
    event = (
        {"type": "response.output_text.delta", "item_id": item["id"], "delta": "Verified result"}
        if snapshot == "delta"
        else {"type": "response.output_item.done", "item": item}
    )
    response = StreamResponse([{**event, "output_index": 1}, completed(item)])
    assert content(list(chatgpt_client._stream_codex_responses_iter(response))) == "Verified result"
    assert response.closed


def test_sparse_terminal_snapshot_does_not_conflict_with_another_message():
    first, second = text_item("First", "msg_first"), text_item("Second", "msg_second")
    response = StreamResponse(
        [
            {"type": "response.output_item.done", "output_index": 0, "item": first},
            {"type": "response.output_item.done", "output_index": 2, "item": second},
            completed(second),
        ]
    )
    assert content(list(chatgpt_client._stream_codex_responses_iter(response))) == "FirstSecond"


def test_new_message_in_sparse_snapshot_does_not_reuse_another_messages_text():
    response = StreamResponse(
        [
            {"type": "response.output_item.done", "output_index": 0, "item": text_item("First", "msg_first")},
            completed(text_item("Second", "msg_second")),
        ]
    )
    assert content(list(chatgpt_client._stream_codex_responses_iter(response))) == "FirstSecond"


def test_final_snapshot_recovers_and_deduplicates_tool_calls():
    item = call_item()
    response = StreamResponse(
        [
            {"type": "response.output_item.added", "item": {**item, "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": item["id"], "delta": '{"path":'},
            completed(item, call_item("call_2")),
        ]
    )
    chunks = list(chatgpt_client._stream_codex_responses_iter(response))
    calls = chunks[-1]["message"]["tool_calls"]
    assert [call["id"] for call in calls] == ["call_1", "call_2"]
    assert all(call["function"]["arguments"] == item["arguments"] for call in calls)


def test_arguments_done_recovers_function_name():
    response = StreamResponse(
        [
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1"},
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_1",
                "name": "read_file",
                "arguments": "{}",
            },
            "[DONE]",
        ]
    )
    calls = list(chatgpt_client._stream_codex_responses_iter(response))[-1]["message"]["tool_calls"]
    assert calls == [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]


@pytest.mark.parametrize("terminal_output", [[], [text_item("Finished")]])
def test_lite_sparse_terminal_envelope_preserves_completed_streamed_calls(posts, terminal_output):
    requests, responses, sleeps = posts
    item = call_item(arguments="{}")
    responses.append(
        StreamResponse(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "item": {**item, "status": "in_progress", "arguments": ""},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 1,
                    "item_id": item["id"],
                    "delta": "{}",
                },
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": 1,
                    "item_id": item["id"],
                    "arguments": "{}",
                },
                {"type": "response.output_item.done", "output_index": 1, "item": {**item, "status": "completed"}},
                completed(*terminal_output),
            ]
        )
    )
    chunks = list(chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True))
    assert chunks[-1]["message"]["tool_calls"] == [
        {"id": item["call_id"], "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
    ]
    assert len(requests) == 1 and sleeps == []


def test_terminal_snapshot_does_not_hide_an_incomplete_call():
    response = StreamResponse(
        [
            {"type": "response.output_item.added", "item": call_item("abandoned", '{"path":')},
            completed(call_item("complete")),
        ]
    )
    chunks = []
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="invalid function call"):
        for chunk in chatgpt_client._stream_codex_responses_iter(response):
            chunks.append(chunk)
    assert not any(chunk.get("message", {}).get("tool_calls") for chunk in chunks)


@pytest.mark.parametrize("arguments", ['{"path":', "null", "[]", '"text"'])
def test_invalid_arguments_never_become_executable_calls(arguments):
    response = StreamResponse([completed(call_item(arguments=arguments))])
    chunks = []
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError):
        for chunk in chatgpt_client._stream_codex_responses_iter(response):
            chunks.append(chunk)
    assert not any(chunk.get("message", {}).get("tool_calls") for chunk in chunks)
    assert response.closed


@pytest.mark.parametrize("item", [text_item("Partial"), call_item()])
def test_eof_without_terminal_event_is_not_success(item):
    response = StreamResponse([{"type": "response.output_item.done", "item": item}])
    chunks = []
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="before completion"):
        for chunk in chatgpt_client._stream_codex_responses_iter(response):
            chunks.append(chunk)
    assert not any(chunk.get("message", {}).get("tool_calls") for chunk in chunks)
    assert response.closed


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("first", ["reasoning", "empty", "timeout", "eof", "server_error"])
def test_retry_before_answer_uses_same_request_and_closes_failed_response(posts, stream, first):
    requests, responses, sleeps = posts
    events = [{"type": "response.reasoning_summary_text.delta", "delta": "Planning segmented tests"}]
    failure = None
    if first == "reasoning":
        events.append(completed({"type": "reasoning", "id": "reason_1"}))
    elif first == "empty":
        events = ["[DONE]"]
    elif first == "timeout":
        failure = TimeoutError("read timed out")
    elif first == "server_error":
        events.append({"type": "response.failed", "response": {"error": {"code": "server_error"}}})
    responses.extend([StreamResponse(events, failure), StreamResponse([completed(text_item("Recovered"))])])
    reply = chatgpt_client.ChatGptClient().chat(
        model="gpt-6-astra", messages=[{"role": "user", "content": "Review"}], stream=stream
    )
    chunks = list(reply) if stream else [reply]
    assert content(chunks) == "Recovered"
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert sleeps == [1.0]
    assert all(response.closed for response in responses)
    if stream:
        retry = [chunk["response_retry"] for chunk in chunks if "response_retry" in chunk]
        assert len(retry) == 1
        assert retry[0]["attempt"] == 1
        assert retry[0]["max_retries"] == 2


def test_repeated_empty_stream_exhausts_bounded_retry(posts):
    requests, responses, sleeps = posts
    responses.extend(StreamResponse([completed()]) for _ in range(3))
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="2 retries"):
        list(chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True))
    assert len(requests) == 3
    assert sleeps == [1.0, 2.0]
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "reset", "url_timeout", "url_reset", "tls_eof", "http_502"])
def test_initial_transport_failure_recovers_without_changing_request(monkeypatch, stream, failure):
    requests, sleeps = [], []
    response = StreamResponse([completed(text_item("Recovered"))])
    http_body = io.BytesIO(b"upstream unavailable")
    error = {
        "timeout": TimeoutError("opening timed out"),
        "reset": ConnectionResetError("connection reset"),
        "url_timeout": urllib.error.URLError(TimeoutError("opening timed out")),
        "url_reset": urllib.error.URLError(ConnectionResetError("connection reset")),
        "tls_eof": ssl.SSLEOFError(8, "unexpected EOF"),
        "http_502": urllib.error.HTTPError("https://example.invalid", 502, "Bad Gateway", {}, http_body),
    }[failure]

    def open_request(request, **_kwargs):
        requests.append(request.data)
        if len(requests) == 1:
            raise error
        return response

    monkeypatch.setattr(chatgpt_client, "_MODEL_REQUEST_SCOPE_MISSING", False)
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "test-token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_chatgpt_account_id", lambda: "test-account")
    monkeypatch.setattr(chatgpt_client.urllib.request, "urlopen", open_request)
    monkeypatch.setattr(chatgpt_client.time, "sleep", sleeps.append)
    reply = chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=stream)
    assert content(list(reply) if stream else [reply]) == "Recovered"
    assert len(requests) == 2 and requests[0] == requests[1]
    assert sleeps == [1.0]
    assert response.closed
    if failure == "http_502":
        assert http_body.closed


@pytest.mark.parametrize("phase", ["open", "read", "reopen"])
@pytest.mark.parametrize("failure", ["url_timeout", "tls_eof"])
def test_transport_failures_share_the_current_request_retry_budget(posts, phase, failure):
    requests, responses, sleeps = posts
    error = (
        urllib.error.URLError(TimeoutError("timed out"))
        if failure == "url_timeout"
        else ssl.SSLEOFError(8, "unexpected EOF")
    )
    if phase == "reopen":
        responses.append(StreamResponse([completed()]))
    responses.append(StreamResponse([], error) if phase == "read" else error)
    responses.append(StreamResponse([completed(text_item("Recovered"))]))
    reply = chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True)
    assert content(list(reply)) == "Recovered"
    assert len(requests) == (3 if phase == "reopen" else 2)
    assert all(request == requests[0] for request in requests)
    assert sleeps == ([1.0, 2.0] if phase == "reopen" else [1.0])
    assert all(response.closed for response in responses if isinstance(response, StreamResponse))


def test_initial_and_stream_failures_share_one_bounded_retry_budget(posts):
    requests, responses, sleeps = posts
    responses.extend(
        [TimeoutError("opening timed out"), StreamResponse([completed()]), StreamResponse([], ConnectionResetError())]
    )
    with pytest.raises(chatgpt_client.CodexResponseStreamError, match="2 retries"):
        list(chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True))
    assert len(requests) == 3 and sleeps == [1.0, 2.0]
    assert all(response.closed for response in responses if isinstance(response, StreamResponse))


@pytest.mark.parametrize("phase", ["open", "read", "reopen"])
@pytest.mark.parametrize("failure", ["certificate", "wrapped_certificate", "permanent_url"])
def test_nontransient_transport_errors_are_not_retried(posts, phase, failure):
    requests, responses, sleeps = posts
    error = {
        "certificate": ssl.SSLCertVerificationError(1, "certificate verify failed"),
        "wrapped_certificate": urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
        "permanent_url": urllib.error.URLError("unknown URL type"),
    }[failure]
    if phase == "reopen":
        responses.append(StreamResponse([completed()]))
    responses.append(StreamResponse([], error) if phase == "read" else error)
    with pytest.raises(type(error)):
        list(chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True))
    assert len(requests) == (2 if phase == "reopen" else 1)
    assert sleeps == ([1.0] if phase == "reopen" else [])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "url_timeout", "tls_eof"])
def test_partial_answer_is_retained_and_never_retried(posts, stream, failure):
    requests, responses, sleeps = posts
    error = {
        "timeout": TimeoutError("read timed out"),
        "url_timeout": urllib.error.URLError(TimeoutError("read timed out")),
        "tls_eof": ssl.SSLEOFError(8, "unexpected EOF"),
    }[failure]
    responses.append(StreamResponse([{"type": "response.output_text.delta", "delta": "Partial answer"}], error))
    with pytest.raises(type(error)):
        reply = chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=stream)
        if stream:
            assert next(reply)["message"]["content"] == "Partial answer"
            list(reply)
    assert len(requests) == 1
    assert sleeps == []
    assert responses[0].closed


@pytest.mark.parametrize("status", [400, 401, 403, 408, 429, 500, 501, 502, 503, 504])
def test_http_failure_classification_closes_response_without_clearing_auth(monkeypatch, status):
    body = io.BytesIO(b"provider request failure")

    def fail_request(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://example.invalid", status, "Failure", {}, body)

    def forbidden(*_args, **_kwargs):
        pytest.fail("non-invalidated HTTP failures must not clear or refresh credentials")

    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_valid_token", lambda: "test-token")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "get_chatgpt_account_id", lambda: "test-account")
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "force_refresh_token", forbidden)
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "clear_tokens", forbidden)
    monkeypatch.setattr(chatgpt_client.urllib.request, "urlopen", fail_request)
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError) as result:
        chatgpt_client._post_codex_responses({"model": "gpt-6-astra"})
    assert chatgpt_client._is_retryable_codex_failure(result.value) == (status in {408, 500, 502, 503, 504})
    assert body.closed


def test_cancel_during_initial_retry_does_not_open_another_request(posts):
    requests, responses, sleeps = posts
    responses.append(TimeoutError("opening timed out"))
    reply = chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True)
    assert next(reply)["response_retry"]["attempt"] == 1
    reply.close()
    assert len(requests) == 1 and sleeps == []


@pytest.mark.parametrize("code", ["invalid_api_key", "insufficient_quota", "invalid_request_error"])
def test_permanent_provider_failure_is_not_retried(posts, code):
    requests, responses, sleeps = posts
    responses.append(StreamResponse([{"type": "response.failed", "response": {"error": {"code": code}}}]))
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match=code):
        list(chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True))
    assert len(requests) == 1
    assert sleeps == []


def test_partial_tool_call_is_discarded_before_retry(posts):
    requests, responses, sleeps = posts
    responses.extend(
        [
            StreamResponse([{"type": "response.output_item.added", "item": call_item("incomplete", '{"path":')}]),
            StreamResponse([completed(call_item("recovered"))]),
        ]
    )
    chunks = list(chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True))
    calls = [call for chunk in chunks for call in chunk.get("message", {}).get("tool_calls", [])]
    assert [call["id"] for call in calls] == ["recovered"]
    assert len(requests) == 2
    assert sleeps == [1.0]


def test_cancel_during_reasoning_does_not_retry(posts):
    requests, responses, sleeps = posts
    responses.append(StreamResponse([{"type": "response.reasoning_summary_text.delta", "delta": "Plan"}]))
    reply = chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True)
    assert next(reply)["message"]["thinking"] == "Plan"
    reply.close()
    assert responses[0].closed
    assert len(requests) == 1
    assert sleeps == []


def test_refusal_done_uses_refusal_field():
    response = StreamResponse([{"type": "response.refusal.done", "refusal": "Cannot do that"}, "[DONE]"])
    assert content(list(chatgpt_client._stream_codex_responses_iter(response))) == "Cannot do that"


@pytest.mark.parametrize("stream", [False, True])
def test_usage_includes_completed_failed_attempts(posts, stream):
    _, responses, _ = posts
    empty = completed()
    empty["response"]["usage"] = {
        "input_tokens": 40,
        "output_tokens": 5,
        "total_tokens": 45,
        "input_tokens_details": {"cached_tokens": 3},
    }
    answer = completed(text_item("Done"))
    answer["response"]["usage"] = {
        "input_tokens": 40,
        "output_tokens": 8,
        "total_tokens": 48,
        "input_tokens_details": {"cached_tokens": 30},
    }
    responses.extend([StreamResponse([empty]), StreamResponse([answer])])
    reply = chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=stream)
    chunks = list(reply) if stream else [reply]
    assert sum(chunk.get("prompt_eval_count", 0) for chunk in chunks) == 80
    assert sum(chunk.get("eval_count", 0) for chunk in chunks) == 13
    if not stream:
        assert reply["usage"] == {
            "input_tokens": 80,
            "output_tokens": 13,
            "total_tokens": 93,
            "input_tokens_details": {"cached_tokens": 33},
        }


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress"])
def test_unfinished_terminal_item_is_not_a_completed_answer(status):
    response = StreamResponse([completed({**text_item("Partial"), "status": status})])
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="not completed"):
        list(chatgpt_client._stream_codex_responses_iter(response))
    assert response.closed


def test_failed_tool_batch_does_not_emit_even_its_complete_calls(posts):
    requests, responses, sleeps = posts
    responses.append(
        StreamResponse(
            [
                {"type": "response.output_item.done", "item": call_item()},
                {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
            ]
        )
    )
    chunks = []
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="max_output_tokens"):
        for chunk in chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True):
            chunks.append(chunk)
    assert not any(chunk.get("message", {}).get("tool_calls") for chunk in chunks)
    assert len(requests) == 1
    assert sleeps == []


def test_completed_event_finishes_without_reading_trailing_socket_failure(posts):
    requests, responses, sleeps = posts
    responses.append(StreamResponse([completed(call_item())], TimeoutError()))
    chunks = list(chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True))
    assert len(chunks[-1]["message"]["tool_calls"]) == 1
    assert responses[0].closed
    assert len(requests) == 1
    assert sleeps == []


def test_retry_does_not_fall_back_or_clear_auth_on_permanent_failure(posts, monkeypatch):
    requests, responses, sleeps = posts
    responses.extend([StreamResponse([completed()]), chatgpt_client.ChatGptOAuthAccessError("HTTP 401")])

    def forbidden(*_args, **_kwargs):
        pytest.fail("stream recovery cannot fall back or clear credentials")

    monkeypatch.setattr(chatgpt_client, "_codex_exec_chunk", forbidden)
    monkeypatch.setattr(chatgpt_client.chatgpt_auth, "clear_tokens", forbidden)
    with pytest.raises(chatgpt_client.ChatGptOAuthAccessError, match="HTTP 401"):
        list(chatgpt_client.ChatGptClient().chat(model="gpt-6-astra", messages=[], stream=True))
    assert len(requests) == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize("show_thinking", [False, True])
@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("prior_tool", ["read_file", "git_diff"])
def test_agent_loop_retries_current_round_without_replaying_previous_tool(
    posts, monkeypatch, show_thinking, exhausted, prior_tool
):
    from algo_cli import main
    from algo_cli.config import Config
    from algo_cli.nathan_runtime import TOOL_RESULT_CONTENT_LIMIT
    from test_main_helpers import _patch_agent_loop_for_tool_policy_test

    requests, responses, sleeps = posts
    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    errors: list[str] = []
    infos: list[str] = []
    dispatched: list[str] = []
    captures: list[bool] = []
    tool_result = (
        "README contents" if prior_tool == "read_file" else "diff --git a/example.py b/example.py\n" + "+line\n" * 3500
    )
    monkeypatch.setattr(main, "show_error", errors.append)
    monkeypatch.setattr(main, "show_info", infos.append)
    monkeypatch.setattr(main, "show_thinking_text", lambda _text: None)
    monkeypatch.setattr(main, "show_stream_text", lambda _text: None)
    monkeypatch.setattr(main, "start_streaming_response", lambda: None)
    monkeypatch.setattr(main, "record_perf_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "run_tool", lambda name, *_args, **_kwargs: dispatched.append(name) or tool_result)
    monkeypatch.setattr(
        main.memory_runtime,
        "capture_completed_user_turn",
        lambda _cfg, _text, **kwargs: captures.append(kwargs["completed"]) or {"status": "skipped"},
    )
    prior_call = call_item(arguments='{"path":"README.md"}' if prior_tool == "read_file" else '{"names_only":false}')
    prior_call["name"] = prior_tool
    responses.append(StreamResponse([completed(prior_call)]))
    for _ in range(3 if exhausted else 1):
        responses.append(
            StreamResponse(
                [
                    {"type": "response.reasoning_summary_text.delta", "delta": "Failed attempt reasoning"},
                    completed(),
                ]
            )
        )
    if not exhausted:
        responses.append(
            StreamResponse(
                [
                    {"type": "response.reasoning_summary_text.delta", "delta": "Successful reasoning"},
                    completed(text_item("Verified result")),
                ]
            )
        )
    cfg = Config(
        model="gpt-6-astra", max_tool_iterations=2, skill_crystallize_enabled=False, show_thinking=show_thinking
    )

    main.agent_loop(chatgpt_client.ChatGptClient(), cfg, "Read README.md and summarize it")

    assert dispatched == [prior_tool]
    assert len(requests) == (4 if exhausted else 3)
    assert all(request == requests[1] for request in requests[2:])
    assert any(
        item.get("type") == "function_call_output" and item.get("output") == tool_result[:TOOL_RESULT_CONTENT_LIMIT]
        for item in requests[-1]["input"]
    )
    assert captures == [not exhausted]
    assert any("retrying (1/2)" in info for info in infos)
    if exhausted:
        assert errors and "2 retries" in errors[-1]
    else:
        assert errors == []
        assert cfg.messages[-1]["content"] == "Verified result"
        assert "Failed attempt reasoning" not in str(cfg.messages)
        assert cfg.messages[-1].get("thinking") == ("Successful reasoning" if show_thinking else None)
