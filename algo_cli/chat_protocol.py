"""Message and tool-call normalization for chat providers (Ollama / xAI shapes)."""

from __future__ import annotations

import json
from typing import Any


def get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def normalize_tool_call(call: Any) -> tuple[str, dict[str, Any]]:
    function = get_attr(call, "function", {})
    name = get_attr(function, "name", "")
    args = get_attr(function, "arguments", {}) or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"raw": args}
    if not isinstance(args, dict):
        args = {"raw": args}
    return name, dict(args)


def collapse_tool_history_for_gemini(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rewrite assistant tool_calls + tool result messages into plain content turns.

    Workaround for Ollama issue #14567 / PR #14676. Remove when the Ollama Python
    SDK exposes thought_signature on tool calls.
    """
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            summary_lines: list[str] = []
            if msg.get("content"):
                summary_lines.append(str(msg["content"]))
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                fn_name = fn.get("name", "?")
                fn_args = fn.get("arguments", {})
                try:
                    args_preview = json.dumps(fn_args, ensure_ascii=True, default=str)[:120]
                except Exception:
                    args_preview = str(fn_args)[:120]
                summary_lines.append(f"[Previously called {fn_name}({args_preview})]")
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tr = messages[j]
                tool_name = tr.get("name", "?")
                tool_content = str(tr.get("content", "")).strip()
                preview = tool_content[:600] + ("…" if len(tool_content) > 600 else "")
                summary_lines.append(f"[{tool_name} result] {preview}")
                j += 1
            out.append({"role": "assistant", "content": "\n".join(summary_lines)})
            i = j
        else:
            out.append(msg)
            i += 1
    return out


def _coerce_arguments_dict(args: Any) -> dict[str, Any]:
    """Tool-call arguments must serialize as a dict: the Ollama SDK's Message
    model rejects string arguments, but OpenAI-style streams (xAI) accumulate
    arguments as a JSON string."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {"raw": args}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    return {}


def serialize_tool_call(call: Any) -> dict[str, Any]:
    if isinstance(call, dict):
        out_dict: dict[str, Any] = json.loads(json.dumps(call, ensure_ascii=False, default=str))
        fn = out_dict.get("function")
        if isinstance(fn, dict):
            fn["arguments"] = _coerce_arguments_dict(fn.get("arguments"))
        return out_dict
    function = get_attr(call, "function", {})
    name = get_attr(function, "name", "")
    args_payload = _coerce_arguments_dict(get_attr(function, "arguments", {}) or {})
    out: dict[str, Any] = {"function": {"name": name, "arguments": args_payload}}
    call_id = get_attr(call, "id", None)
    if call_id:
        out["id"] = call_id
    call_type = get_attr(call, "type", None)
    if call_type:
        out["type"] = call_type
    for sig_key in ("thought_signature", "thoughtSignature"):
        sig = get_attr(call, sig_key, None) or get_attr(function, sig_key, None)
        if sig:
            out[sig_key] = sig
            break
    return out


def normalize_message(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    out: dict[str, Any] = {}
    for key in ("role", "content", "thinking", "tool_calls"):
        value = getattr(message, key, None)
        if value:
            out[key] = value
    return out
