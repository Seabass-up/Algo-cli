"""Corrective feedback for malformed tool calls.

Weak local models routinely fumble tool invocations: bad JSON arguments,
wrong/missing parameter names, a JSON string where a dict was expected, or a
Unix command piped into a Windows shell. The default behavior — returning a
bare ``TypeError`` string — gives the model nothing to correct against, so it
loops or gives up.

These helpers turn a failed call into an actionable correction: the tool's real
signature (required vs optional params, runtime-injected params hidden) plus a
diagnosis of what the model sent. The agent loop feeds this back as the tool
result so the model can retry with the right shape.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

# Params the runtime injects; the model must never supply them.
_RUNTIME_PARAMS = frozenset({"cwd", "cfg", "safe_mode"})

# Tokens that mean "you used a Unix tool in the Windows cmd.exe shell".
_WINDOWS_SHELL_MISS = (
    "is not recognized as an internal or external command",
    "'head'", "'tail'", "'grep'", "'sed'", "'awk'", "'cat'", "'less'",
)
_UNIX_TO_WINDOWS = {
    "head": "use `more` or a command flag (e.g. pytest -q), not | head",
    "tail": "use PowerShell `Get-Content -Tail N` or read_file with offset",
    "grep": "use `findstr` or the search_files tool",
    "cat": "use the read_file tool or `type`",
    "sed": "use the edit_file tool",
    "awk": "use read_file + parse, or findstr",
}


def signature_hint(fn: Callable[..., Any]) -> str:
    """Render a model-facing signature: required params first, then optionals."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return ""
    required: list[str] = []
    optional: list[str] = []
    for name, param in sig.parameters.items():
        if name in _RUNTIME_PARAMS:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        ann = ""
        if param.annotation is not inspect.Parameter.empty:
            ann = getattr(param.annotation, "__name__", None) or str(param.annotation).replace("typing.", "")
        if param.default is inspect.Parameter.empty:
            required.append(f"{name}: {ann}" if ann else name)
        else:
            optional.append(f"{name}={param.default!r}")
    parts = []
    if required:
        parts.append("required: " + ", ".join(required))
    if optional:
        parts.append("optional: " + ", ".join(optional))
    return "; ".join(parts)


def correct_tool_error(name: str, args: dict[str, Any], exc: Exception, fn: Callable[..., Any] | None) -> str:
    """Build a corrective tool-result message for a failed call."""
    sent = sorted(k for k in (args or {}).keys() if k not in _RUNTIME_PARAMS)
    lines = [f"Tool argument error for {name}: {exc}"]

    # Malformed-JSON signal: chat_protocol wraps unparseable arguments as {"raw": ...}.
    if list(args.keys()) == ["raw"] or "raw" in sent and len(sent) == 1:
        lines.append(
            "Your arguments were not valid JSON, so they could not be parsed into "
            "fields. Re-send the call with a proper JSON object for `arguments`."
        )

    hint = signature_hint(fn) if fn is not None else ""
    if hint:
        lines.append(f"Correct signature — {name}({hint})")
    if sent:
        lines.append(f"You sent: {', '.join(sent)}")
    lines.append("Re-issue the call with exactly the required parameters and valid JSON values.")
    return "\n".join(lines)


def shell_mistake_hint(command: str, output: str) -> str:
    """Return a one-line correction when shell output shows a cmd.exe miss, else ""."""
    lowered = (output or "").lower()
    if not any(marker.lower() in lowered for marker in _WINDOWS_SHELL_MISS):
        return ""
    cmd_lower = (command or "").lower()
    for unix_tool, advice in _UNIX_TO_WINDOWS.items():
        if unix_tool in cmd_lower:
            return f"[hint] This is Windows cmd.exe: {advice}."
    return (
        "[hint] This is Windows cmd.exe — Unix tools are unavailable. "
        "Use findstr/more, command flags, or read_file/search_files."
    )
