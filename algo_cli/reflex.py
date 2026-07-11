"""Minimal reflex loop v0.1 — session recovery behind ``reflex_enabled`` (default off).

Implements a subset of docs/reflex-loop-v0.2.md: detect → one safe ACT → inject
result. No background tasks, no index mutation, no verification-loop integration.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from . import tools as tools_module
from .display import redact_tool_args

REFLEX_MAX_CYCLES = 3
REFLEX_LEDGER_CONTEXT_KEY = "reflex_cycles"
REFLEX_CAP_NOTIFIED_KEY = "reflex_cap_notified"

REFLEX_SAFE_TOOLS = frozenset(
    {
        "harness_search",
        "harness_read",
        "harness_stats",
        "available_actions",
        "session_slash",
        "read_file",
        "search_files",
        "list_directory",
        "git_status",
        "git_diff",
    }
)

_EMPTY_RESULT_MARKERS = (
    "no harness matches",
    "error:",
    "not found",
    "no matches",
    "no such file",
)


def tool_signature(name: str, args: dict[str, Any]) -> str:
    safe_args = redact_tool_args(name, args)
    try:
        encoded = json.dumps(safe_args, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":"))
    except TypeError:
        encoded = str(safe_args)
    return f"{name}:{encoded}"


def _reflex_cycles(cfg: Any) -> int:
    state = getattr(cfg, "context_state", None) or {}
    if not isinstance(state, dict):
        return 0
    try:
        return max(0, int(state.get(REFLEX_LEDGER_CONTEXT_KEY, 0)))
    except (TypeError, ValueError):
        return 0


def _increment_reflex_cycles(cfg: Any) -> None:
    state = getattr(cfg, "context_state", None)
    if not isinstance(state, dict):
        cfg.context_state = {}
        state = cfg.context_state
    state[REFLEX_LEDGER_CONTEXT_KEY] = _reflex_cycles(cfg) + 1


def reset_reflex_cycles(cfg: Any) -> None:
    state = getattr(cfg, "context_state", None)
    if isinstance(state, dict):
        state.pop(REFLEX_LEDGER_CONTEXT_KEY, None)
        state.pop(REFLEX_CAP_NOTIFIED_KEY, None)


def begin_agent_pipeline(cfg: Any) -> None:
    """Isolate reflex budget per /agent run (avoids burning cap during exploration)."""
    reset_reflex_cycles(cfg)


def count_signature_in_ledger(cfg: Any, signature: str) -> int:
    ledger = getattr(cfg, "attempt_ledger", None) or []
    return sum(1 for item in ledger if item.get("signature") == signature)


def _result_is_empty_or_failed(result: str, status: str) -> bool:
    if status == "failed":
        return True
    lowered = str(result).strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in _EMPTY_RESULT_MARKERS)


@dataclass(frozen=True)
class ReflexTrigger:
    label: str
    reason: str


def _is_benign_exploration_failure(name: str, args: dict[str, Any], result: str) -> bool:
    """Expected misses while exploring — reflex should not burn cycles here."""
    lowered = str(result).lower()
    if name in {"git_status", "git_diff"}:
        return "not a git repository" in lowered or "fatal: not a git repository" in lowered
    if name == "list_directory":
        return "directory not found" in lowered or "error: directory not found" in lowered
    if name == "read_file":
        if "file not found" not in lowered and "error: file not found" not in lowered:
            return False
        path = str(args.get("path") or "").replace("\\", "/")
        # Wrong relative path from home (e.g. algo_cli/foo vs ollama-cli/algo_cli/foo).
        if path.startswith("algo_cli/") and not path.startswith("ollama-cli/"):
            return True
    return False


def detect_trigger(cfg: Any, name: str, args: dict[str, Any], result: str, status: str) -> ReflexTrigger | None:
    if _is_benign_exploration_failure(name, args, result):
        return None
    signature = tool_signature(name, args)
    prior = count_signature_in_ledger(cfg, signature)
    if prior >= 2:
        return ReflexTrigger("loop_detected", f"same tool path attempted {prior + 1} times")
    if status == "failed":
        return ReflexTrigger("tool_failed", summarize_tool_result(result))
    if (
        name in {"harness_search", "search_files"}
        and prior >= 1
        and _result_is_empty_or_failed(result, status)
    ):
        return ReflexTrigger("search_miss", "search returned no actionable hits")
    if prior >= 1 and _result_is_empty_or_failed(result, status):
        return ReflexTrigger("empty_repeat", "tool returned no useful output on repeat")
    return None


def summarize_tool_result(result: str, limit: int = 140) -> str:
    text = " ".join(str(result).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _broaden_harness_query(args: dict[str, Any]) -> str:
    raw = str(args.get("query") or args.get("q") or "").strip()
    if not raw:
        return ""
    tokens = [t for t in re.findall(r"[\w.-]+", raw) if len(t) > 2]
    return " ".join(tokens[:6]) if tokens else raw


def _recovery_query_for_tool(name: str, args: dict[str, Any]) -> str:
    if name == "harness_search":
        broadened = _broaden_harness_query(args)
        return broadened or "algo-cli harness"
    if name == "read_file":
        path = str(args.get("path") or "")
        stem = path.replace("\\", "/").split("/")[-1]
        return stem or path
    if name == "search_files":
        return str(args.get("pattern") or args.get("query") or "project")
    return _broaden_harness_query(args) or "workspace context"


def _run_safe_act(cfg: Any, trigger: ReflexTrigger, name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Return (supplemental text, resolved)."""
    if trigger.label == "loop_detected":
        return (
            "Reflex: loop detected (same tool + args). Stop retrying this path. "
            "Change strategy, ask the user, or use a different read-only tool.",
            False,
        )

    query = _recovery_query_for_tool(name, args)
    if "harness_search" in REFLEX_SAFE_TOOLS:
        fallback = tools_module.harness_search(query=query, limit=5)
        if fallback and "no harness matches" not in fallback.lower():
            return f"Reflex recovery — harness_search:\n{fallback}", True

    if name == "read_file" and "search_files" in REFLEX_SAFE_TOOLS:
        pattern = str(args.get("path", "")).split("/")[-1].split("\\")[-1]
        if pattern:
            alt = tools_module.search_files(
                pattern=pattern,
                cwd=str(getattr(cfg, "cwd", ".")),
                limit=5,
            )
            if alt and "error" not in alt.lower()[:20]:
                return f"Reflex recovery — search_files for {pattern}:\n{alt}", True

    actions = tools_module.available_actions(topic=query[:40])
    return (
        f"Reflex ({trigger.label}): {trigger.reason}\n"
        f"Suggested next reads:\n{actions[:1200]}",
        False,
    )


def maybe_augment_tool_result(
    cfg: Any,
    name: str,
    args: dict[str, Any],
    result: str,
    status: str,
) -> tuple[str, str | None]:
    """Append a reflex recovery block when enabled and triggered.

    Returns (possibly augmented result, user-visible reflex note or None).
    """
    if not getattr(cfg, "reflex_enabled", False):
        return result, None
    if name not in REFLEX_SAFE_TOOLS and status != "failed":
        # Only auto-intervene on unsafe tools when they hard-failed.
        if status != "failed":
            return result, None
    cycles = _reflex_cycles(cfg)
    if cycles >= REFLEX_MAX_CYCLES:
        state = getattr(cfg, "context_state", None)
        if not isinstance(state, dict):
            cfg.context_state = {}
            state = cfg.context_state
        if not state.get(REFLEX_CAP_NOTIFIED_KEY):
            state[REFLEX_CAP_NOTIFIED_KEY] = True
            return result, (
                f"Reflex: cap ({REFLEX_MAX_CYCLES}) reached for this /agent run — "
                "auto-recovery disabled until /reflex reset or next /agent."
            )
        return result, None

    trigger = detect_trigger(cfg, name, args, result, status)
    if trigger is None:
        return result, None

    _increment_reflex_cycles(cfg)
    supplement, resolved = _run_safe_act(cfg, trigger, name, args)
    verdict = "resolved" if resolved else "escalate"
    note = f"↻ reflex [{trigger.label}] cycle {_reflex_cycles(cfg)}/{REFLEX_MAX_CYCLES} — {verdict}"
    block = f"\n\n---\n{supplement}"
    return f"{result}{block}", note


def status_line(cfg: Any) -> str:
    enabled = getattr(cfg, "reflex_enabled", False)
    cycles = _reflex_cycles(cfg)
    return f"reflex: {'ON' if enabled else 'OFF'} ({cycles}/{REFLEX_MAX_CYCLES} cycles this session)"
# ---------------------------------------------------------------------------
# Reflex Error-Interceptor Loop (v0.2)
# Catches tool errors, generates a diagnostic prompt, retries with modified
# arguments up to REFLEX_ERROR_MAX_RETRIES times. Integrates with the existing
# attempt_ledger and reflex cycle tracker.
# ---------------------------------------------------------------------------

REFLEX_ERROR_MAX_RETRIES = 3
REFLEX_ERROR_LEDGER_KEY = "reflex_error_retries"

# Tools that are safe to auto-retry on failure (read-only or idempotent).
REFLEX_ERROR_SAFE_RETRY_TOOLS = frozenset({
    "read_file",
    "read_pdf",
    "list_directory",
    "search_files",
    "git_status",
    "git_diff",
    "harness_search",
    "harness_read",
    "harness_stats",
    "harness_refresh",
    "available_actions",
    "web_search",
    "web_fetch",
    "x_search",
    "embed_text",
    "vision_describe",
    "model_show",
    "query_knowledge_graph",
})

# Known error patterns and suggested argument fixes.
_REFLEX_ERROR_FIXES: dict[str, dict[str, Any]] = {
    "file not found": {"action": "broaden_path", "description": "Try broader search or alternative paths"},
    "not a directory": {"action": "fix_path_type", "description": "Target is a file, not directory"},
    "is a directory": {"action": "fix_path_type", "description": "Target is a directory, not file"},
    "permission denied": {"action": "skip_retry", "description": "Permission issue; retry unlikely to help"},
    "timeout": {"action": "increase_timeout", "description": "Try again with longer timeout"},
    "no harness matches": {"action": "broaden_query", "description": "Broaden or rephrase harness search query"},
    "no matches": {"action": "broaden_query", "description": "Broaden or rephrase search"},
    "tool error": {"action": "diagnose", "description": "Generic tool error; run diagnostic"},
}


def _reflex_error_retries(cfg: Any, tool_name: str, args: dict[str, Any]) -> int:
    """Return how many error-retries have been used for this specific call signature."""
    state = getattr(cfg, "context_state", None)
    if not isinstance(state, dict):
        return 0
    key = (REFLEX_ERROR_LEDGER_KEY, tool_name, tool_signature(tool_name, args))
    retries = state.get(str(key), 0)
    return int(retries) if isinstance(retries, (int, float)) else 0


def _increment_reflex_error_retry(cfg: Any, tool_name: str, args: dict[str, Any]) -> None:
    """Increment the retry counter for this specific tool call signature."""
    state = getattr(cfg, "context_state", None)
    if not isinstance(state, dict):
        cfg.context_state = {}
        state = cfg.context_state
    key = str((REFLEX_ERROR_LEDGER_KEY, tool_name, tool_signature(tool_name, args)))
    state[key] = _reflex_error_retries(cfg, tool_name, args) + 1


def _suggest_fix(name: str, args: dict[str, Any], result: str) -> dict[str, Any] | None:
    """Based on the error pattern, suggest argument modifications for retry."""
    lowered = str(result).strip().lower()
    for pattern, fix in _REFLEX_ERROR_FIXES.items():
        if pattern in lowered:
            if fix["action"] == "skip_retry":
                return None
            if fix["action"] == "broaden_path" and name == "read_file":
                # Suggest using search_files instead
                return {"action": "search_files", "pattern": str(args.get("path", "")), "description": fix["description"]}
            if fix["action"] == "broaden_query" and name in {"harness_search", "search_files"}:
                return {"action": "broaden_query", "description": fix["description"]}
            if fix["action"] == "increase_timeout" and name == "run_shell":
                return {"action": "increase_timeout", "description": fix["description"]}
            return {"action": fix["action"], "description": fix["description"]}
    return None


def intercept_tool_error(
    cfg: Any,
    name: str,
    args: dict[str, Any],
    result: str,
) -> tuple[str, bool]:
    """Error-interceptor: check if a failed tool call should be retried.

    Returns (diagnostic_message, should_retry).
    The diagnostic_message explains why the error occurred and what to try next.
    should_retry is True only when the tool is retry-safe and retries remain.
    """
    if not getattr(cfg, "reflex_enabled", False):
        return "", False

    # Only intercept actual errors (status "failed" is handled by the caller)
    lowered = str(result).strip().lower()
    is_error = lowered.startswith("error") or "tool error" in lowered
    if not is_error:
        return "", False

    # Skip tools that shouldn't be auto-retried
    if name not in REFLEX_ERROR_SAFE_RETRY_TOOLS:
        return "", False

    retries_so_far = _reflex_error_retries(cfg, name, args)
    if retries_so_far >= REFLEX_ERROR_MAX_RETRIES:
        return (
            f"Reflex: max error-retries ({REFLEX_ERROR_MAX_RETRIES}) reached for {name}. "
            "Escalate to user or change strategy.",
            False,
        )

    # Generate diagnostic
    fix = _suggest_fix(name, args, result)
    if fix is None:
        return "", False

    _increment_reflex_error_retry(cfg, name, args)
    suggestion = fix.get("description", "Try again with modified approach")
    return (
        f"Reflex error-interceptor: {name} failed with: {summarize_tool_result(result)}\n"
        f"Diagnostic: {suggestion}\n"
        f"Retry {_reflex_error_retries(cfg, name, args)}/{REFLEX_ERROR_MAX_RETRIES}",
        True,
    )


def reset_error_retries(cfg: Any) -> None:
    """Clear all error-retry counters for a fresh session or /agent run."""
    state = getattr(cfg, "context_state", None)
    if isinstance(state, dict):
        keys_to_remove = [k for k in state if k.startswith(str((REFLEX_ERROR_LEDGER_KEY,)))]
        for key in keys_to_remove:
            state.pop(key, None)
