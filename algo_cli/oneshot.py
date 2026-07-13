"""One-shot non-interactive JSON event mode.

Emits one JSON object per line to stdout, suitable for subprocess consumption
by an external bridge (Telegram bot, CI, scripts). The agent loop, tool
execution, policy, and contract code are unchanged; this module only swaps
the output sink and gates the approval flow.

Event schema (one JSON object per line, no embedded raw newlines):
    {"type":"session_start","model":...,"host":...,"cwd":...,"approval_mode":...,"version":...}
    {"type":"thinking","text":...}
    {"type":"content","text":...}
    {"type":"tool_call","call_id":...,"name":...,"args":{...}}
    {"type":"tool_result","call_id":...,"name":...,"status":"ok|failed|denied|skipped",
                         "duration_ms":...,"summary":...,"truncated":...}
    {"type":"tool_denied","call_id":...,"name":...,"reason":...}
    {"type":"error","class":"timeout|policy|tool|model|internal","message":...}
    {"type":"done","status":"complete|partial|failed","status_reason":...,
                   "tool_calls":...,"duration_ms":...}

Invariants:
    - session_start is the first event; done is the last event.
    - tool_result always follows the matching tool_call by call_id.
    - No ANSI escape codes in stdout.
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from collections.abc import Iterator, Set
from typing import Any


class _RegistryApprovalSet(Set[str]):
    """Compatibility view that cannot drift from the central approval registry."""

    @staticmethod
    def _names() -> tuple[str, ...]:
        from .action_registry import action_requires_approval
        from .tools import TOOL_MAP

        return tuple(name for name in TOOL_MAP if action_requires_approval(name))

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        from .action_registry import action_requires_approval

        return action_requires_approval(value)

    def __iter__(self) -> Iterator[str]:
        return iter(self._names())

    def __len__(self) -> int:
        return len(self._names())


# Deprecated compatibility surface. Never use this as an independent policy
# source; membership resolves lazily through Action Registry.
DANGEROUS_TOOLS: Set[str] = _RegistryApprovalSet()


SUMMARY_LIMIT = 600


def _summarize(text: str, limit: int = SUMMARY_LIMIT) -> tuple[str, bool]:
    s = str(text).strip()
    if len(s) <= limit:
        return s, False
    return s[:limit].rstrip() + "...", True


def _tool_status_from_result(result: str) -> str:
    lowered = str(result).strip().lower()
    if lowered.startswith("user denied"):
        return "denied"
    if lowered.startswith("skipped repeated"):
        return "skipped"
    # Keep bridge event status aligned with the interactive runtime, including
    # non-zero ``run_shell`` exit-code suffixes.
    from .tool_runtime import classify_tool_status

    return "failed" if classify_tool_status(result) == "failed" else "ok"


class JsonEventSink:
    """Stdout writer for one-shot JSON events. Thread-safe enough for serial dispatch."""

    def __init__(self, *, stream=None, approval_mode: str = "never") -> None:
        self._stream = stream if stream is not None else sys.stdout
        self.approval_mode = approval_mode
        self.deny_dangerous = approval_mode == "never"
        self._call_count = 0
        self._pending_call_ids: dict[str, deque[str]] = {}
        self._tool_calls_done = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._errors: list[dict[str, str]] = []
        self._started_at = time.perf_counter()

    def _write(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, default=str)
        # JSON encoding already escapes embedded newlines; one event per line is invariant.
        self._stream.write(line + "\n")
        self._stream.flush()

    # --- framing ---

    def session_start(self, *, model: str, host: str, cwd: str, version: str) -> None:
        self._write({
            "type": "session_start",
            "model": model,
            "host": host,
            "cwd": cwd,
            "approval_mode": self.approval_mode,
            "version": version,
        })

    def done(self, *, status: str, status_reason: str, duration_ms: float) -> None:
        total_tokens = self._prompt_tokens + self._completion_tokens
        self._write({
            "type": "done",
            "status": status,
            "status_reason": status_reason,
            "tool_calls": self._tool_calls_done,
            "duration_ms": round(duration_ms, 2),
            "usage": {
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": total_tokens,
            },
        })

    # --- model output ---

    def thinking(self, text: str) -> None:
        if not text:
            return
        self._write({"type": "thinking", "text": text})

    def content(self, text: str) -> None:
        if not text:
            return
        self._write({"type": "content", "text": text})

    def chat_usage(self, *, prompt_tokens: Any, completion_tokens: Any) -> None:
        """Accumulate provider-reported usage once per completed chat turn."""

        try:
            prompt = int(prompt_tokens or 0)
            completion = int(completion_tokens or 0)
        except (TypeError, ValueError):
            return
        if prompt > 0 or completion > 0:
            self._prompt_tokens += max(0, prompt)
            self._completion_tokens += max(0, completion)

    # --- tool dispatch ---

    def next_call_id(self) -> str:
        self._call_count += 1
        return f"oneshot-{self._call_count}"

    def tool_call(self, *, call_id: str, name: str, args: dict[str, Any]) -> None:
        self._pending_call_ids.setdefault(name, deque()).append(call_id)
        self._write({
            "type": "tool_call",
            "call_id": call_id,
            "name": name,
            "args": args,
        })

    def tool_result(
        self,
        *,
        call_id: str | None,
        name: str,
        result: str,
        duration_ms: float | None,
    ) -> None:
        call_id = self._matching_call_id(name, call_id)
        status = _tool_status_from_result(result)
        summary, truncated = _summarize(result)
        self._tool_calls_done += 1
        self._write({
            "type": "tool_result",
            "call_id": call_id,
            "name": name,
            "status": status,
            "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
            "summary": summary,
            "truncated": truncated,
        })

    def tool_denied(self, *, call_id: str | None, name: str, reason: str) -> None:
        call_id = self._matching_call_id(name, call_id)
        self._tool_calls_done += 1
        self._write({
            "type": "tool_denied",
            "call_id": call_id,
            "name": name,
            "reason": reason,
        })

    def _matching_call_id(self, name: str, call_id: str | None) -> str:
        """Resolve a result to the oldest unmatched call of the same tool."""

        pending = self._pending_call_ids.get(name)
        if call_id:
            if pending:
                try:
                    pending.remove(call_id)
                except ValueError:
                    pass
                if not pending:
                    self._pending_call_ids.pop(name, None)
            return call_id
        if pending:
            matched = pending.popleft()
            if not pending:
                self._pending_call_ids.pop(name, None)
            return matched
        return self.next_call_id()

    # --- errors ---

    def error(self, *, error_class: str, message: str) -> None:
        self._errors.append({"class": str(error_class), "message": str(message)})
        self._write({
            "type": "error",
            "class": error_class,
            "message": message,
        })

    @property
    def errors(self) -> tuple[dict[str, str], ...]:
        return tuple(self._errors)


def run_oneshot(
    *,
    prompt: str,
    approval_mode: str = "never",
    cfg_overrides: dict[str, Any] | None = None,
    stream=None,
) -> int:
    """Run a single agent turn and emit JSON events to stdout. Returns exit code.

    - approval_mode="never" (default): dangerous tools are denied; emits tool_denied.
    - approval_mode="auto": equivalent to cfg.auto_mode=True for this run only.
    - cfg_overrides: applied to the loaded Config before the run (e.g., {"model": "qwen3"}).
    """
    # Imports deferred to avoid cycle with display + to keep import cost off the
    # interactive path when --oneshot is not used.
    from . import deliberation, display, harness, main, skills, tool_runtime
    from .action_registry import action_requires_approval
    from .config import Config
    from .model_routing import effective_runtime_host
    from .tool_runtime import session_command_requires_approval

    cfg = Config.load()
    persistent_values: dict[str, Any] = {
        "auto_mode": cfg.auto_mode,
        "skill_crystallize_enabled": cfg.skill_crystallize_enabled,
        "session_summary": cfg.session_summary,
        "show_thinking": cfg.show_thinking,
        "temperature": cfg.temperature,
    }
    if cfg_overrides:
        for key, value in cfg_overrides.items():
            if value is not None and hasattr(cfg, key):
                persistent_values.setdefault(key, getattr(cfg, key))
                setattr(cfg, key, value)
    harness.configure_context_sources(
        external=cfg.external_harness_sources_enabled,
        index_compute_lab=cfg.index_compute_lab_auto_inject,
    )
    if approval_mode not in {"never", "auto"}:
        raise ValueError("approval_mode must be 'never' or 'auto'")
    cfg.auto_mode = approval_mode == "auto"
    cfg.skill_crystallize_enabled = False  # subprocess invocation must not mutate skill store
    if not cfg_overrides or "show_thinking" not in cfg_overrides:
        cfg.show_thinking = cfg.show_thinking and deliberation.needs_deliberation(prompt)
    if not cfg_overrides or "temperature" not in cfg_overrides:
        cfg.temperature = min(cfg.temperature, 0.2)

    # Bridge runs (Telegram, CI) must not inherit interactive session_summary into prompts.
    cfg.session_summary = ""

    sink = JsonEventSink(stream=stream, approval_mode=approval_mode)
    sink.session_start(
        model=cfg.model,
        host=effective_runtime_host(cfg),
        cwd=cfg.cwd,
        version=_resolve_version(),
    )

    display.install_json_sink(sink)
    original_ask_approval = main.ask_approval
    original_runtime_ask_approval = tool_runtime.ask_approval

    def _oneshot_ask_approval(name: str, args: dict[str, Any], cfg: Config, *, force: bool = False) -> bool:
        del cfg
        requires_approval = action_requires_approval(name) or (
            name == "session_command"
            and session_command_requires_approval(str(args.get("command") or ""))
        )
        if approval_mode == "never" and requires_approval:
            # Sink's tool_denied is emitted in the agent_loop's denial path via show_tool_result.
            # We just refuse here; the existing "User denied this operation." message flows
            # through show_tool_result → sink.tool_denied conversion.
            return False
        if approval_mode == "auto" and not force:
            return True
        return True

    main.ask_approval = _oneshot_ask_approval
    tool_runtime.ask_approval = _oneshot_ask_approval

    started = time.perf_counter()
    status = "complete"
    status_reason = ""
    try:
        client = main.create_client(cfg)
        main.agent_loop(client, cfg, prompt)
    except KeyboardInterrupt:
        status = "failed"
        status_reason = "interrupted"
        sink.error(error_class="internal", message="KeyboardInterrupt")
    except Exception as exc:
        status = "failed"
        status_reason = f"{type(exc).__name__}: {exc}"
        sink.error(error_class="internal", message=status_reason)
    finally:
        main.ask_approval = original_ask_approval
        tool_runtime.ask_approval = original_runtime_ask_approval
        display.uninstall_json_sink()
        skills.ensure_dirs()  # restore any deferred dir state
        # agent_loop saves config; restore bridge-only mutations and CLI override fields.
        for key, value in persistent_values.items():
            setattr(cfg, key, value)
        cfg.save()

    if status == "complete" and sink.errors:
        status = "partial"
        status_reason = sink.errors[-1].get("message", "one-shot run emitted an error event")

    sink.done(
        status=status,
        status_reason=status_reason,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return 0 if status == "complete" else 2


def _resolve_version() -> str:
    try:
        from importlib.metadata import version
        return version("algo-cli-runtime")
    except Exception:
        return "unknown"
