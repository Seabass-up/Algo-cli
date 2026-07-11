"""ChatGPT/OpenAI-compatible chat client adapter.

Uses ChatGPT/OpenAI OAuth tokens from chatgpt_auth and exposes an
ollama.Client.chat()-shaped interface so agent_loop can reuse one provider path.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator

from . import chatgpt_auth

try:
    from ollama._utils import convert_function_to_tool
except Exception:  # pragma: no cover
    convert_function_to_tool = None  # type: ignore[assignment]


class ChatGptOAuthAccessError(RuntimeError):
    """Raised when ChatGPT OAuth access is unavailable."""


CODEX_SUBSCRIPTION_MODELS = {
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
    "gpt-5.1-codex",
}
_MODEL_REQUEST_SCOPE_MISSING = False
CODEX_RESPONSES_BASE_URL = os.environ.get("OPENAI_CODEX_RESPONSES_BASE", "https://chatgpt.com/backend-api").rstrip("/")
CODEX_RESPONSES_ORIGINATOR = os.environ.get("OPENAI_CODEX_ORIGINATOR", "pi").strip() or "pi"
_REASONING_EFFORT_ALIASES = {
    "": "medium",
    "default": "medium",
    "normal": "medium",
    "med": "medium",
    "maximum": "xhigh",
    "max": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "very-high": "xhigh",
    "very_high": "xhigh",
}
_REASONING_EFFORT_LEVELS = {"minimal", "low", "medium", "high", "xhigh"}


def is_codex_subscription_model(model: str) -> bool:
    return str(model or "").split(":", 1)[0] in CODEX_SUBSCRIPTION_MODELS


def _normalize_reasoning_effort(value: Any) -> str:
    text = str(value or "").strip().lower()
    normalized = _REASONING_EFFORT_ALIASES.get(text, text)
    if normalized in _REASONING_EFFORT_LEVELS:
        return normalized
    return "medium"


def _messages_to_codex_prompt(messages: list[dict[str, Any]]) -> str:
    lines = [
        "You are being invoked by algo-cli through the OpenAI Codex CLI subscription runtime.",
        "Answer the latest user request directly. Do not mention this transport unless asked.",
        "",
        "Conversation:",
    ]
    for msg in messages[-20:]:
        role = str(msg.get("role") or "unknown")
        content = msg.get("content", "")
        if not content and msg.get("tool_calls"):
            content = f"[tool calls: {json.dumps(msg.get('tool_calls'), ensure_ascii=False)}]"
        lines.append(f"[{role}] {content}")
    return "\n".join(lines).strip()


def _run_codex_exec(
    model: str,
    messages: list[dict[str, Any]],
    *,
    reasoning_effort: str = "medium",
    timeout: float = 300.0,
    runner: Any = subprocess.run,
) -> str:
    codex_bin = chatgpt_auth.resolve_codex_bin()
    if not codex_bin:
        raise ChatGptOAuthAccessError("Codex CLI is not installed or not discoverable. Run /chatgpt-login again after installing Codex.")
    if not chatgpt_auth.get_valid_token():
        raise ChatGptOAuthAccessError("Not authenticated with ChatGPT/Codex OAuth. Run /chatgpt-login first.")
    prompt = _messages_to_codex_prompt(messages)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(chatgpt_auth.CODEX_AUTH_HOME)
    with tempfile.TemporaryDirectory(prefix="algo-codex-") as tmpdir:
        output_path = Path(tmpdir) / "last-message.txt"
        cmd = [
            codex_bin,
            "exec",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{_normalize_reasoning_effort(reasoning_effort)}"',
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-o",
            str(output_path),
            "-",
        ]
        try:
            result = runner(
                cmd,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ChatGptOAuthAccessError(f"Codex CLI timed out after {timeout:.0f}s for model {model}.") from exc
        except FileNotFoundError as exc:
            raise ChatGptOAuthAccessError("Codex CLI is not installed or not discoverable. Run /chatgpt-login again.") from exc
        if getattr(result, "returncode", 0) != 0:
            stderr = str(getattr(result, "stderr", "") or "").strip()
            stdout = str(getattr(result, "stdout", "") or "").strip()
            detail = (stderr or stdout or "no output")[-1500:]
            raise ChatGptOAuthAccessError(f"Codex CLI request failed for {model}: {detail}")
        if output_path.exists():
            text = output_path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
        stdout = str(getattr(result, "stdout", "") or "").strip()
        if stdout:
            return stdout.splitlines()[-1].strip()
    raise ChatGptOAuthAccessError(f"Codex CLI completed for {model} but produced no response.")


def _reasoning_effort_from_options(options: dict[str, Any] | None) -> str:
    if not options:
        return "medium"
    effort = options.get("reasoning_effort") or options.get("chatgpt_reasoning_effort") or "medium"
    return _normalize_reasoning_effort(effort)


def _codex_exec_chunk(model: str, messages: list[dict[str, Any]], options: dict[str, Any] | None) -> dict[str, Any]:
    content = _run_codex_exec(
        model,
        messages,
        reasoning_effort=_reasoning_effort_from_options(options),
        timeout=300.0,
    )
    return {"message": {"content": content}}


def _is_missing_model_request_scope(exc: Exception) -> bool:
    text = str(exc).lower()
    return "missing scopes" in text and "model.request" in text


def _build_openai_tools(tools: list[Callable[..., Any]] | list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for item in tools:
        if isinstance(item, dict):
            out.append(item)
            continue
        if convert_function_to_tool is None:
            continue
        try:
            out.append(convert_function_to_tool(item).model_dump(exclude_none=True))
        except Exception:
            continue
    return out or None


def _build_responses_tools(tools: list[Callable[..., Any]] | list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    built = _build_openai_tools(tools)
    if not built:
        return None
    out: list[dict[str, Any]] = []
    for tool in built:
        if tool.get("type") != "function":
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):
            out.append(
                {
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                    "strict": fn.get("strict"),
                }
            )
        else:
            out.append(tool)
    return out or None


def _build_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending_call_ids: list[str] = []
    counter = 0
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            calls_out: list[dict[str, Any]] = []
            for call in msg.get("tool_calls") or []:
                fn = call.get("function", {}) if isinstance(call, dict) else getattr(call, "function", {})
                call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
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
                calls_out.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": args or "{}"}})
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
            if keep:
                out.append(keep)
    return out


def _build_responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    valid_call_ids: set[str] = set()
    output_call_ids = {
        str(msg.get("tool_call_id"))
        for msg in messages
        if msg.get("role") == "tool" and msg.get("tool_call_id")
    }
    counter = 0
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "assistant" and msg.get("tool_calls"):
            if content:
                out.append({"role": "assistant", "content": str(content)})
            for call in msg.get("tool_calls") or []:
                fn = call.get("function", {}) if isinstance(call, dict) else getattr(call, "function", {})
                call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                if isinstance(fn, dict):
                    name = str(fn.get("name", ""))
                    args = fn.get("arguments", "{}")
                else:
                    name = str(getattr(fn, "name", ""))
                    args = getattr(fn, "arguments", "{}")
                if not name.strip():
                    continue
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                if not call_id:
                    counter += 1
                    call_id = f"call_{counter}"
                call_id = str(call_id)
                if call_id not in output_call_ids:
                    continue
                valid_call_ids.add(call_id)
                out.append({"type": "function_call", "call_id": call_id, "name": name, "arguments": args or "{}"})
        elif role == "tool":
            raw_call_id = msg.get("tool_call_id")
            if raw_call_id:
                call_id = str(raw_call_id)
                if call_id not in valid_call_ids:
                    continue
            else:
                continue
            out.append({"type": "function_call_output", "call_id": call_id, "output": str(content)})
        elif role in {"system", "developer", "user", "assistant"}:
            mapped_role = "developer" if role == "system" else role
            out.append({"role": mapped_role, "content": str(content)})
    return out


def _post_chat(payload: dict[str, Any], *, stream: bool, timeout: float = 120.0) -> Any:
    token = chatgpt_auth.get_valid_token()
    if not token:
        raise ChatGptOAuthAccessError("Not authenticated with ChatGPT OAuth. Run /chatgpt-login first.")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{chatgpt_auth.CHATGPT_API_BASE}/chat/completions",
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
        raise ChatGptOAuthAccessError(f"ChatGPT OAuth request failed ({exc.code}): {detail or '(no body)'}") from exc


def _codex_responses_url() -> str:
    base = CODEX_RESPONSES_BASE_URL
    if base.endswith("/codex/responses"):
        return base
    return f"{base}/codex/responses"


def _is_token_invalidated_error(detail: str) -> bool:
    text = str(detail or "").lower()
    return "token_invalidated" in text or "authentication token has been invalidated" in text


def _build_codex_responses_request(payload: dict[str, Any], *, token: str, account_id: str) -> urllib.request.Request:
    body = json.dumps(payload).encode("utf-8")
    request_id = str(uuid.uuid4())
    return urllib.request.Request(
        _codex_responses_url(),
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "chatgpt-account-id": account_id,
            "originator": CODEX_RESPONSES_ORIGINATOR,
            "User-Agent": "pi (Windows; x64)",
            "OpenAI-Beta": "responses=experimental",
            "x-client-request-id": request_id,
        },
        method="POST",
    )


def _post_codex_responses(payload: dict[str, Any], *, timeout: float = 120.0, _retried: bool = False) -> Any:
    token = chatgpt_auth.get_valid_token()
    if not token:
        raise ChatGptOAuthAccessError("Not authenticated with ChatGPT/Codex OAuth. Run /chatgpt-login first.")
    account_id = chatgpt_auth.get_chatgpt_account_id()
    if not account_id:
        raise ChatGptOAuthAccessError(
            "ChatGPT/Codex OAuth token does not include a ChatGPT account id. Run /chatgpt-login again."
        )
    req = _build_codex_responses_request(payload, token=token, account_id=account_id)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1500].strip()
        except Exception:
            pass
        if exc.code == 401 and not _retried and _is_token_invalidated_error(detail):
            refreshed = chatgpt_auth.force_refresh_token()
            if refreshed:
                return _post_codex_responses(payload, timeout=timeout, _retried=True)
        raise ChatGptOAuthAccessError(f"ChatGPT Codex Responses request failed ({exc.code}): {detail or '(no body)'}") from exc


def _parse_sse_events(resp: Any) -> Iterator[dict[str, Any]]:
    try:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _stream_iter(resp: Any) -> Iterator[dict[str, Any]]:
    pending_calls: dict[int, dict[str, Any]] = {}
    for event in _parse_sse_events(resp):
        choices = event.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")
        if delta.get("content"):
            yield {"message": {"content": delta["content"]}}
        for tc_delta in delta.get("tool_calls") or []:
            idx = int(tc_delta.get("index", 0))
            slot = pending_calls.setdefault(idx, {"function": {"name": "", "arguments": ""}})
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
        yield {"message": {"tool_calls": [pending_calls[i] for i in sorted(pending_calls)]}}


def _extract_responses_event_text(event: dict[str, Any]) -> str:
    if isinstance(event.get("delta"), str):
        return event["delta"]
    if isinstance(event.get("text"), str):
        return event["text"]
    if isinstance(event.get("content"), str):
        return event["content"]
    return ""


def _stream_codex_responses_iter(resp: Any) -> Iterator[dict[str, Any]]:
    pending_calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def completed_calls() -> list[dict[str, Any]]:
        return [
            pending_calls[call_id]
            for call_id in order
            if pending_calls.get(call_id)
            and str(pending_calls[call_id].get("function", {}).get("name") or "").strip()
        ]

    for event in _parse_sse_events(resp):
        event_type = str(event.get("type") or "")
        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
            text = _extract_responses_event_text(event)
            if text:
                yield {"message": {"content": text}}
            continue
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                continue
            call_id = str(item.get("call_id") or item.get("id") or f"call_{len(order) + 1}")
            if call_id not in pending_calls:
                order.append(call_id)
            pending_calls[call_id] = {
                "id": call_id,
                "type": "function",
                "function": {"name": str(item.get("name", "")), "arguments": str(item.get("arguments") or "")},
            }
            continue
        if event_type == "response.function_call_arguments.delta":
            call_id = str(event.get("call_id") or event.get("item_id") or "")
            if not call_id:
                continue
            if call_id not in pending_calls:
                order.append(call_id)
                pending_calls[call_id] = {"id": call_id, "type": "function", "function": {"name": "", "arguments": ""}}
            pending_calls[call_id]["function"]["arguments"] += str(event.get("delta") or "")
            continue
        if event_type in {"response.function_call_arguments.done", "response.output_item.done"}:
            item = event.get("item") or {}
            call_id = str(event.get("call_id") or item.get("call_id") or item.get("id") or "")
            if not call_id:
                continue
            if call_id not in pending_calls:
                order.append(call_id)
                pending_calls[call_id] = {"id": call_id, "type": "function", "function": {"name": "", "arguments": ""}}
            if item.get("name"):
                pending_calls[call_id]["function"]["name"] = str(item["name"])
            if item.get("arguments") is not None:
                pending_calls[call_id]["function"]["arguments"] = str(item.get("arguments") or "")
    completed = completed_calls()
    if completed:
        yield {"message": {"tool_calls": completed}}


def _nonstream_to_chunk(body: dict[str, Any]) -> dict[str, Any]:
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    out_msg: dict[str, Any] = {}
    if msg.get("content"):
        out_msg["content"] = msg["content"]
    if msg.get("tool_calls"):
        out_msg["tool_calls"] = msg["tool_calls"]
    chunk: dict[str, Any] = {"message": out_msg}
    if body.get("usage"):
        chunk["usage"] = body["usage"]
    return chunk


def _codex_responses_to_chunk(resp: Any) -> dict[str, Any]:
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for chunk in _stream_codex_responses_iter(resp):
        message = chunk.get("message") or {}
        if message.get("content"):
            content_parts.append(str(message["content"]))
        if message.get("tool_calls"):
            tool_calls.extend(message["tool_calls"])
    out_msg: dict[str, Any] = {}
    if content_parts:
        out_msg["content"] = "".join(content_parts)
    if tool_calls:
        out_msg["tool_calls"] = tool_calls
    return {"message": out_msg}


class ChatGptClient:
    """Ollama-shaped chat client routed to the OpenAI-compatible Chat Completions API."""

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[Callable[..., Any]] | list[dict[str, Any]] | None = None,
        stream: bool = False,
        options: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> Any:
        global _MODEL_REQUEST_SCOPE_MISSING
        if is_codex_subscription_model(model) and not tools:
            chunk = _codex_exec_chunk(model, messages, options)
            if stream:
                return iter([chunk])
            return chunk

        if is_codex_subscription_model(model) and _MODEL_REQUEST_SCOPE_MISSING:
            chunk = _codex_exec_chunk(model, messages, options)
            note = (
                "Note: ChatGPT OAuth is Codex-CLI-only in this session, so this response used "
                "Codex CLI fallback without Algo CLI tool calls. For full file edits, shell, "
                "approval gates, and tool ledger, switch to a local/Ollama/xAI tool-capable model "
                "or authenticate with an OpenAI token that has model.request.\n\n"
            )
            chunk["message"]["content"] = note + str(chunk["message"].get("content", ""))
            if stream:
                return iter([chunk])
            return chunk

        if is_codex_subscription_model(model):
            payload: dict[str, Any] = {
                "model": model,
                "store": False,
                "stream": True,
                "input": _build_responses_input(messages),
                "include": ["reasoning.encrypted_content"],
                "tool_choice": "auto",
                "parallel_tool_calls": True,
            }
            built_tools = _build_responses_tools(tools)
            if built_tools:
                payload["tools"] = built_tools
            if options:
                effort = options.get("reasoning_effort") or options.get("chatgpt_reasoning_effort")
                if effort:
                    payload["reasoning"] = {
                        "effort": _normalize_reasoning_effort(effort),
                        "summary": "auto",
                    }
                if "num_predict" in options:
                    payload["max_output_tokens"] = options["num_predict"]
            resp = _post_codex_responses(payload, timeout=120.0)
            if stream:
                return _stream_codex_responses_iter(resp)
            return _codex_responses_to_chunk(resp)

        payload: dict[str, Any] = {
            "model": model,
            "messages": _build_openai_messages(messages),
            "stream": bool(stream),
        }
        built_tools = _build_openai_tools(tools)
        if built_tools:
            payload["tools"] = built_tools
        if options:
            if "temperature" in options:
                payload["temperature"] = options["temperature"]
            if "num_predict" in options:
                payload["max_tokens"] = options["num_predict"]
        try:
            resp = _post_chat(payload, stream=stream, timeout=120.0)
        except ChatGptOAuthAccessError as exc:
            if not is_codex_subscription_model(model) or not _is_missing_model_request_scope(exc):
                raise
            _MODEL_REQUEST_SCOPE_MISSING = True
            chunk = _codex_exec_chunk(model, messages, options)
            note = (
                "Note: ChatGPT OAuth lacks the model.request scope, so this response used "
                "Codex CLI fallback without Algo CLI tool calls. For full file edits, shell, "
                "approval gates, and tool ledger, switch to a local/Ollama/xAI tool-capable model "
                "or authenticate with an OpenAI token that has model.request.\n\n"
            )
            chunk["message"]["content"] = note + str(chunk["message"].get("content", ""))
            if stream:
                return iter([chunk])
            return chunk
        if stream:
            return _stream_iter(resp)
        try:
            body = json.loads(resp.read().decode("utf-8"))
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return _nonstream_to_chunk(body)


_CLIENT = ChatGptClient()


def active_chatgpt_client() -> ChatGptClient:
    return _CLIENT
