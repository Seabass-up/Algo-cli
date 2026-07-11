"""xAI chat client (OpenAI-compatible API → ollama-shaped responses).

Wraps api.x.ai/v1 with an interface compatible with ollama.Client.chat()
so the agent_loop does not need a separate code path for Grok. Auth is
provided by xai_auth.get_valid_token() (silent refresh).

Streaming: parses OpenAI Server-Sent Events and emits ollama-shaped chunks
of the form {"message": {"content": ..., "tool_calls": [...], "thinking": ...}}.

Tool-call deltas are accumulated by index and emitted as one complete chunk
when finish_reason="tool_calls" arrives, matching agent_loop's expectation
that tool_calls in a chunk are complete (not partial).

Multi-agent models and search use the xAI Responses API. Multi-agent does
not support Chat Completions or client-side custom tools, so that route
preserves text context but deliberately omits the local Python tool schema.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

from . import xai_auth

try:
    from ollama._utils import convert_function_to_tool
except Exception:  # pragma: no cover
    convert_function_to_tool = None  # type: ignore[assignment]


XAI_OAUTH_PROVIDER_LABEL = "optional xAI Grok subscription OAuth"
_BILLING_OR_API_KEY_MARKERS = (
    "api key",
    "api_key",
    "apikey",
    "billing",
    "credits",
    "credit balance",
    "invoice",
    "payment",
    "quota",
    "spend",
)


class XaiOAuthAccessError(RuntimeError):
    """Raised when OAuth access is unavailable without falling back to API spend."""


def _oauth_only_error(status: int | None, endpoint: str, detail: str) -> RuntimeError:
    lower = detail.lower()
    safe_detail = xai_auth.safe_error_message(detail)
    gated = status in {402, 403} or any(marker in lower for marker in _BILLING_OR_API_KEY_MARKERS)
    if gated:
        return XaiOAuthAccessError(
            f"xAI OAuth access was rejected for {endpoint}. "
            "This CLI is configured for subscription OAuth only and will not use "
            "XAI_API_KEY or any pay-per-token API-key fallback. "
            f"Upstream response: {safe_detail or '(no body)'}"
        )
    prefix = f"xAI OAuth request failed for {endpoint}"
    if status is not None:
        prefix += f" ({status})"
    return RuntimeError(f"{prefix} :: {safe_detail or '(no body)'}")


def _build_openai_tools(tools: list[Callable[..., Any]] | None) -> list[dict[str, Any]] | None:
    if not tools or convert_function_to_tool is None:
        return None
    out: list[dict[str, Any]] = []
    for fn in tools:
        try:
            spec = convert_function_to_tool(fn).model_dump(exclude_none=True)
        except Exception:
            continue
        out.append(spec)
    return out


def _build_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ollama-shaped messages to OpenAI chat completion format.

    Ollama tool-result messages may omit `tool_call_id`; OpenAI requires it.
    We consume assistant-emitted call IDs in order so duplicate tool names are
    still associated one-to-one.
    """
    out: list[dict[str, Any]] = []
    pending_call_ids: list[str] = []
    counter = 0
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            calls_out: list[dict[str, Any]] = []
            for call in msg["tool_calls"]:
                if isinstance(call, dict):
                    fn = call.get("function") or {}
                    call_id = call.get("id")
                else:
                    fn = getattr(call, "function", {}) or {}
                    call_id = getattr(call, "id", None)
                if isinstance(fn, dict):
                    name = fn.get("name", "")
                    args = fn.get("arguments", "")
                else:
                    name = getattr(fn, "name", "")
                    args = getattr(fn, "arguments", "")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                if not call_id:
                    counter += 1
                    call_id = f"call_{counter}"
                call_id = str(call_id)
                pending_call_ids.append(call_id)
                calls_out.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": args or "{}"},
                    }
                )
            translated: dict[str, Any] = {"role": "assistant", "tool_calls": calls_out}
            if msg.get("content"):
                translated["content"] = msg["content"]
            out.append(translated)
        elif role == "tool":
            explicit_call_id = msg.get("tool_call_id")
            if explicit_call_id:
                call_id = str(explicit_call_id)
                if call_id not in pending_call_ids:
                    continue
                pending_call_ids.remove(call_id)
            elif pending_call_ids:
                call_id = pending_call_ids.pop(0)
            else:
                continue
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(msg.get("content", "")),
                }
            )
        else:
            keep = {k: v for k, v in msg.items() if k in {"role", "content"}}
            out.append(keep)
    return out


def is_multi_agent_model(model: str) -> bool:
    return isinstance(model, str) and "multi-agent" in model.lower()


def _compact_tool_call(call: Any) -> str:
    if isinstance(call, dict):
        fn = call.get("function") or {}
    else:
        fn = getattr(call, "function", {}) or {}
    if isinstance(fn, dict):
        name = fn.get("name", "?")
        args = fn.get("arguments", "")
    else:
        name = getattr(fn, "name", "?")
        args = getattr(fn, "arguments", "")
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    return f"{name}({args})"


def _build_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert ollama history to Responses input without custom tool roles.

    The multi-agent model does not accept client-side tools. Prior tool calls
    and results are folded into assistant text so the model still sees the
    relevant history without receiving unsupported function-call structures.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = str(msg.get("content") or msg.get("thinking") or "")
        if role == "tool":
            tool_name = msg.get("name") or "tool"
            content = f"[{tool_name} result]\n{content}"
            role = "assistant"
        elif role == "assistant" and msg.get("tool_calls"):
            calls = ", ".join(_compact_tool_call(call) for call in msg.get("tool_calls") or [])
            content = "\n".join(part for part in [content, f"[Called tools: {calls}]"] if part)
        if role not in {"system", "user", "assistant"}:
            role = "user"
        if content:
            out.append({"role": role, "content": content})
    return out


def _post_chat(payload: dict[str, Any], *, stream: bool, timeout: float = 120.0) -> Any:
    token = xai_auth.get_valid_token()
    if not token:
        raise XaiOAuthAccessError("Not authenticated with xAI OAuth. Run /xai-login first.")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{xai_auth.XAI_API_BASE}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        },
        method="POST",
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1500].strip()
        except Exception:
            pass
        raise _oauth_only_error(exc.code, req.full_url, detail) from exc


def _post_responses(payload: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
    """POST to the xAI Responses API (/v1/responses).

    Used for the built-in x_search tool and other Agent Tools.
    Returns the parsed JSON response body.
    """
    token = xai_auth.get_valid_token()
    if not token:
        raise XaiOAuthAccessError("Not authenticated with xAI OAuth. Run /xai-login first.")
    body = json.dumps(payload).encode("utf-8")
    url = f"{xai_auth.XAI_API_BASE}/responses"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1500].strip()
        except Exception:
            pass
        raise _oauth_only_error(exc.code, url, detail) from exc


def get_models() -> dict[str, Any]:
    """GET /v1/models with the current OAuth token. Useful as a token sanity check."""
    token = xai_auth.get_valid_token()
    if not token:
        raise XaiOAuthAccessError("Not authenticated with xAI OAuth. Run /xai-login first.")
    req = urllib.request.Request(
        f"{xai_auth.XAI_API_BASE}/models",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1500].strip()
        except Exception:
            pass
        raise _oauth_only_error(exc.code, f"{xai_auth.XAI_API_BASE}/models", detail) from exc


def _parse_sse_events(resp: Any) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON events from a text/event-stream response.

    Supports both fully framed SSE (blank line separates events) and the common
    test/HTTP-client shape where each ``data:`` line is yielded as its own
    complete event. Malformed partial data is buffered until a blank line/end of
    stream, then skipped if it still is not valid JSON.
    """
    data_lines: list[str] = []

    def parse_event(data: str) -> dict[str, Any] | None:
        data = data.strip()
        if not data:
            return None
        if data == "[DONE]":
            raise StopIteration
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    def flush_buffer() -> dict[str, Any] | None:
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        data_lines.clear()
        return parse_event(data)

    try:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                event = flush_buffer()
                if event is not None:
                    yield event
                continue
            if line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].lstrip()
            # Fast path for OpenAI-style one-JSON-object-per-data-line streams
            # and for tests/fakes that omit the blank SSE separator.
            event = parse_event(data)
            if event is not None:
                if data_lines:
                    buffered = flush_buffer()
                    if buffered is not None:
                        yield buffered
                yield event
            else:
                data_lines.append(data)
        event = flush_buffer()
        if event is not None:
            yield event
    except StopIteration:
        return


def _stream_iter(resp: Any) -> Iterator[dict[str, Any]]:
    """Translate OpenAI-style SSE events into ollama-shaped chunks."""
    pending_calls: dict[int, dict[str, Any]] = {}

    try:
        for event in _parse_sse_events(resp):
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            finish_reason = choice.get("finish_reason")

            if delta.get("content"):
                yield {"message": {"content": delta["content"]}}

            if delta.get("reasoning_content"):
                yield {"message": {"thinking": delta["reasoning_content"]}}

            for tc_delta in delta.get("tool_calls") or []:
                idx = int(tc_delta.get("index", 0))
                slot = pending_calls.setdefault(
                    idx, {"function": {"name": "", "arguments": ""}}
                )
                if tc_delta.get("id"):
                    slot["id"] = tc_delta["id"]
                if tc_delta.get("type"):
                    slot["type"] = tc_delta["type"]
                fn_delta = tc_delta.get("function") or {}
                if fn_delta.get("name"):
                    slot["function"]["name"] = fn_delta["name"]
                if fn_delta.get("arguments"):
                    slot["function"]["arguments"] += fn_delta["arguments"]

            if finish_reason in {"tool_calls", "stop"} and pending_calls:
                completed = [pending_calls[i] for i in sorted(pending_calls)]
                pending_calls.clear()
                yield {"message": {"tool_calls": completed}}
        if pending_calls:
            completed = [pending_calls[i] for i in sorted(pending_calls)]
            pending_calls.clear()
            yield {"message": {"tool_calls": completed}}
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _nonstream_to_chunk(body: dict[str, Any]) -> dict[str, Any]:
    """Convert a non-streaming xAI response into one ollama-shaped chunk."""
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    out_msg: dict[str, Any] = {}
    if msg.get("content"):
        out_msg["content"] = msg["content"]
    if msg.get("reasoning_content"):
        out_msg["thinking"] = msg["reasoning_content"]
    if msg.get("tool_calls"):
        out_msg["tool_calls"] = msg["tool_calls"]
    chunk: dict[str, Any] = {"message": out_msg}
    if body.get("citations"):
        chunk["citations"] = body["citations"]
    if body.get("usage"):
        chunk["usage"] = body["usage"]
    return chunk


def _responses_to_chunk(body: dict[str, Any]) -> dict[str, Any]:
    """Convert a non-streaming Responses API body into one ollama-shaped chunk."""
    out_msg: dict[str, Any] = {}
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[Any] = []

    if body.get("output_text"):
        content_parts.append(str(body["output_text"]))

    for item in body.get("output", []):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "message":
            for block in item.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "output_text" and block.get("text"):
                    content_parts.append(str(block["text"]))
        elif item_type in {"reasoning", "summary"}:
            for block in item.get("summary", []) or item.get("content", []):
                if isinstance(block, dict) and block.get("text"):
                    thinking_parts.append(str(block["text"]))
                elif isinstance(block, str):
                    thinking_parts.append(block)
        elif item_type in {"function_call", "tool_call"}:
            tool_calls.append(item)

    if content_parts:
        out_msg["content"] = "\n\n".join(content_parts)
    if thinking_parts:
        out_msg["thinking"] = "\n".join(thinking_parts)
    if tool_calls:
        out_msg["tool_calls"] = tool_calls

    chunk: dict[str, Any] = {"message": out_msg}
    if body.get("citations"):
        chunk["citations"] = body["citations"]
    if body.get("usage"):
        chunk["usage"] = body["usage"]
    return chunk


class XaiClient:
    """Ollama-shaped chat client routed to api.x.ai/v1."""

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[Callable[..., Any]] | list[dict[str, Any]] | None = None,
        stream: bool = False,
        options: dict[str, Any] | None = None,
        search_parameters: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> Any:
        if is_multi_agent_model(model):
            payload: dict[str, Any] = {
                "model": model,
                "input": _build_responses_input(messages),
            }
            if options and "temperature" in options:
                payload["temperature"] = options["temperature"]
            body = _post_responses(payload, timeout=3600.0)
            chunk = _responses_to_chunk(body)
            if stream:
                return iter([chunk])
            return chunk

        payload: dict[str, Any] = {
            "model": model,
            "messages": _build_openai_messages(messages),
            "stream": bool(stream),
        }
        if tools:
            built: list[dict[str, Any]] | None
            if tools and isinstance(tools[0], dict):
                built = list(tools)  # already in OpenAI format
            else:
                built = _build_openai_tools(tools)  # type: ignore[arg-type]
            if built:
                payload["tools"] = built
        if options:
            if "temperature" in options:
                payload["temperature"] = options["temperature"]
            if "top_p" in options:
                payload["top_p"] = options["top_p"]
        if search_parameters:
            payload["search_parameters"] = search_parameters

        if stream:
            resp = _post_chat(payload, stream=True)
            return _stream_iter(resp)
        resp = _post_chat(payload, stream=False)
        try:
            body = json.loads(resp.read().decode("utf-8"))
            return _nonstream_to_chunk(body)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def search(
        self,
        *,
        query: str,
        model: str = "grok-4-latest",
        sources: list[dict[str, Any]] | None = None,
        max_results: int = 10,
        from_date: str | None = None,
        to_date: str | None = None,
        allowed_x_handles: list[str] | None = None,
        excluded_x_handles: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search X.com via the xAI Responses API with the built-in x_search tool.

        Replaces the deprecated Live Search (/v1/chat/completions + search_parameters).
        Uses the Agent Tools API: POST /v1/responses with tools=[{type: "x_search"}].

        Returns {"content": str, "citations": list[str]}.
        """
        tool_config: dict[str, Any] = {
            "type": "x_search",
            "max_results": max(1, min(int(max_results), 30)),
        }
        if from_date:
            tool_config["from_date"] = from_date
        if to_date:
            tool_config["to_date"] = to_date
        if allowed_x_handles:
            tool_config["allowed_x_handles"] = allowed_x_handles
        if excluded_x_handles:
            tool_config["excluded_x_handles"] = excluded_x_handles

        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "user", "content": query},
            ],
            "tools": [tool_config],
        }

        body = _post_responses(payload)

        # Extract content and citations from the Responses API output.
        # The response shape is: {"output": [...items...], "usage": {...}}
        # Content items have type "message", search results have type "x_search_call".
        content_parts: list[str] = []
        citations: list[str] = []

        for item in body.get("output", []):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            if item_type == "message":
                # Message item: {"type": "message", "content": [{"type": "output_text", "text": "..."}]}
                for content_block in item.get("content", []):
                    if not isinstance(content_block, dict):
                        continue
                    if content_block.get("type") == "output_text" and content_block.get("text"):
                        content_parts.append(content_block["text"])
            elif item_type == "x_search_call":
                # Search call result may contain citations
                result = item.get("result", {})
                if isinstance(result, dict):
                    for url in result.get("cited_urls", []):
                        if isinstance(url, str):
                            citations.append(url)
                        elif isinstance(url, dict) and url.get("url"):
                            citations.append(url["url"])

        # Also check top-level citations if present (some models return them there)
        for url in body.get("citations", []):
            if isinstance(url, str) and url not in citations:
                citations.append(url)
            elif isinstance(url, dict) and url.get("url") and url["url"] not in citations:
                citations.append(url["url"])

        return {
            "content": "\n\n".join(content_parts) if content_parts else "(Grok returned no summary.)",
            "citations": citations,
            "usage": body.get("usage") or {},
        }


_CLIENT: XaiClient | None = None


def active_xai_client() -> XaiClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = XaiClient()
    return _CLIENT
