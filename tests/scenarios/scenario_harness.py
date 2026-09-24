"""End-to-end scenario harness: a scripted model driving whole turns offline.

A scenario scripts the model's rounds (streamed text, thinking, tool calls,
Ctrl+C, provider errors), runs one complete turn through the real agent loop
(one-shot JSON mode or the interactive loop) or a full /agent pipeline, and
returns a ScenarioResult with the interaction-level facts unit tests miss:
completion, final answer, tool calls, denials/skips, wasted calls, history
pairing and cancellation latency. See tests/scenarios/README.md.
"""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from algo_cli import agent_pipeline, agent_threads, display, main, nathan_runtime, oliver_oneshot
from algo_cli import config as config_module

# --- Script vocabulary -------------------------------------------------------


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Thinking:
    text: str


@dataclass(frozen=True)
class Tool:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass(frozen=True)
class Interrupt:
    """Ctrl+C delivered while the model stream is being consumed."""


@dataclass(frozen=True)
class Raise:
    """The provider raises this exception at this point in the stream."""

    exc: BaseException


def text(value: str) -> Text:
    return Text(value)


def thinking(value: str) -> Thinking:
    return Thinking(value)


def tool(name: str, id: str = "", **args: Any) -> Tool:
    return Tool(name, args, id)


def interrupt() -> Interrupt:
    return Interrupt()


def fail(exc: BaseException) -> Raise:
    return Raise(exc)


Round = list[Any]


class ScriptedStream:
    """One model response. Consecutive Tool items become one complete tool_calls chunk."""

    def __init__(self, items: Iterable[Any], model: ScriptedModel):
        self._items = list(items)
        self._model = model
        self.closed = False

    def __iter__(self):
        pending: list[dict[str, Any]] = []

        def flush():
            chunk = {"message": {"tool_calls": list(pending)}}
            pending.clear()
            return chunk

        for item in self._items:
            if isinstance(item, Tool):
                self._model.tool_ordinal += 1
                call_id = item.id or f"call-{self._model.tool_ordinal}"
                pending.append({"id": call_id, "function": {"name": item.name, "arguments": dict(item.args)}})
                continue
            if pending:
                yield flush()
            if isinstance(item, Text):
                yield {"message": {"content": item.text}}
            elif isinstance(item, Thinking):
                yield {"message": {"thinking": item.text}}
            elif isinstance(item, Interrupt):
                self._model.clock.mark_interrupt()
                raise KeyboardInterrupt
            elif isinstance(item, Raise):
                raise item.exc
            else:  # pragma: no cover - script authoring error
                raise TypeError(f"unsupported script item: {item!r}")
        if pending:
            yield flush()

    def close(self):
        self.closed = True


class InterruptClock:
    """Records when Ctrl+C was delivered so the harness can report cancellation latency."""

    def __init__(self) -> None:
        self.interrupted_at: float | None = None

    def mark_interrupt(self) -> None:
        if self.interrupted_at is None:
            self.interrupted_at = time.monotonic()


class ScriptedModel:
    """Ollama-shaped client. `rounds` is a list of rounds, or a callable(round_number) -> round."""

    def __init__(
        self,
        rounds: list[Round] | Callable[[int], Round],
        *,
        clock: InterruptClock,
        events_so_far: Callable[[], int] = lambda: 0,
        final_fallback: str = "Done.",
    ):
        self._rounds = rounds
        self.clock = clock
        self._events_so_far = events_so_far
        self._fallback = final_fallback
        self.calls: list[dict[str, Any]] = []
        self.event_marks: list[int] = []
        self.streams: list[ScriptedStream] = []
        self.tool_ordinal = 0

    def _round(self, number: int) -> Round:
        if callable(self._rounds):
            return self._rounds(number)
        if number <= len(self._rounds):
            return self._rounds[number - 1]
        return [text(self._fallback)]

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        self.event_marks.append(self._events_so_far())
        stream = ScriptedStream(self._round(len(self.calls)), self)
        self.streams.append(stream)
        return stream


# --- Tool fakes --------------------------------------------------------------

ToolFake = str | BaseException | Callable[[dict[str, Any]], str]


class ToolRecorder:
    """Replaces run_tool. Fakes by name; `real` names run the real tool inside the scenario workspace."""

    def __init__(
        self,
        fakes: dict[str, ToolFake],
        real: frozenset[str],
        original: Callable[..., str],
        clock: InterruptClock,
    ):
        self.fakes = fakes
        self.real = real
        self.original = original
        self.clock = clock
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def __call__(self, name: str, args: dict[str, Any], cfg: Any) -> str:
        with self._lock:
            self.invocations.append((name, dict(args)))
        if name in self.real:
            return self.original(name, args, cfg)
        fake = self.fakes.get(name, f"{name} ok")
        if isinstance(fake, BaseException):
            if isinstance(fake, KeyboardInterrupt):
                self.clock.mark_interrupt()
            raise fake
        if callable(fake):
            try:
                return fake(args)
            except KeyboardInterrupt:
                self.clock.mark_interrupt()
                raise
        return fake


# --- Result ------------------------------------------------------------------

NOT_OK_STATUSES = frozenset({"failed", "denied", "skipped", "cancelled", "timed_out", "unknown_outcome"})


@dataclass(frozen=True)
class ToolRequest:
    call_id: str
    name: str
    args: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return self.name, json.dumps(self.args, sort_keys=True, default=str)


@dataclass
class ScenarioResult:
    name: str
    driver: str
    exit_code: int | None
    events: list[dict[str, Any]]
    display_text: str
    messages: list[dict[str, Any]]
    model: ScriptedModel
    invocations: list[tuple[str, dict[str, Any]]]
    raised: BaseException | None
    elapsed: float
    cancel_latency: float | None
    approvals: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    saves: int = 0
    pipeline: Any = None
    thread: dict[str, Any] | None = None

    # Model-side facts ------------------------------------------------------
    @property
    def model_calls(self) -> int:
        return len(self.model.calls)

    @property
    def tool_calls(self) -> list[ToolRequest]:
        requests = []
        for message in self.messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                requests.append(
                    ToolRequest(str(call.get("id") or ""), str(function.get("name")), dict(function.get("arguments") or {}))
                )
        return requests

    @property
    def tool_results(self) -> list[dict[str, Any]]:
        """Per-call outcomes as displayed to the user (one-shot JSON tool_result events)."""
        return [event for event in self.events if event.get("type") == "tool_result"]

    @property
    def status_by_call(self) -> dict[str, str]:
        return {event["call_id"]: event["status"] for event in self.tool_results}

    @property
    def denials(self) -> list[dict[str, Any]]:
        return [event for event in self.tool_results if event["status"] == "denied"]

    @property
    def skips(self) -> list[dict[str, Any]]:
        return [event for event in self.tool_results if event["status"] == "skipped"]

    # Outcome facts ---------------------------------------------------------
    @property
    def done(self) -> dict[str, Any] | None:
        return next((event for event in reversed(self.events) if event.get("type") == "done"), None)

    @property
    def final_answer(self) -> str:
        for message in reversed(self.messages):
            if message.get("role") == "assistant" and not message.get("tool_calls"):
                return str(message.get("content") or "")
        return ""

    @property
    def completed(self) -> bool:
        if self.pipeline is not None:
            return getattr(self.pipeline, "status", "") == "complete"
        if self.driver == "oneshot":
            return bool(self.done) and self.done.get("status") == "complete"
        return self.raised is None and bool(self.final_answer.strip())

    # Waste and recovery ----------------------------------------------------
    def _waste_flags(self) -> list[tuple[ToolRequest, bool, bool]]:
        """Per requested call: (request, identical to an earlier call, retries a refused tool)."""
        statuses = self.status_by_call
        seen: set[tuple[str, str]] = set()
        refused: set[str] = set()
        flags = []
        for request in self.tool_calls:
            flags.append((request, request.key in seen, request.name in refused))
            seen.add(request.key)
            if statuses.get(request.call_id) in {"denied", "skipped"}:
                refused.add(request.name)
        return flags

    @property
    def identical_repeats(self) -> list[ToolRequest]:
        return [request for request, repeat, _retry in self._waste_flags() if repeat]

    @property
    def denied_retries(self) -> list[ToolRequest]:
        """Calls to a tool the runtime had already denied or skipped earlier in the run."""
        return [request for request, _repeat, retry in self._waste_flags() if retry]

    @property
    def wasted_calls(self) -> int:
        return sum(1 for _request, repeat, retry in self._waste_flags() if repeat or retry)

    @property
    def recovery_turns(self) -> int:
        """Model rounds started after the first non-ok tool outcome."""
        first_bad = next(
            (index for index, event in enumerate(self.events)
             if event.get("type") == "tool_result" and event.get("status") in NOT_OK_STATUSES),
            None,
        )
        if first_bad is None:
            return 0
        return sum(1 for mark in self.model.event_marks if mark > first_bad)

    # History ---------------------------------------------------------------
    @property
    def unpaired_tool_calls(self) -> list[str]:
        """Assistant tool_calls without a following tool message (by id, else by position)."""
        missing: list[str] = []
        messages = self.messages
        for index, message in enumerate(messages):
            calls = message.get("tool_calls") if message.get("role") == "assistant" else None
            if not calls:
                continue
            following = []
            for later in messages[index + 1:]:
                if later.get("role") != "tool":
                    break
                following.append(later)
            answered_ids = {str(item.get("tool_call_id") or "") for item in following}
            for position, call in enumerate(calls):
                call_id = str(call.get("id") or "")
                if call_id and call_id in answered_ids:
                    continue
                if not call_id and position < len(following):
                    continue
                missing.append(call_id or f"{index}:{position}")
        return missing

    @property
    def history_well_formed(self) -> bool:
        return not self.unpaired_tool_calls

    def metrics(self) -> dict[str, Any]:
        return {
            "scenario": self.name,
            "completed": self.completed,
            "model_calls": self.model_calls,
            "tool_calls": len(self.tool_calls),
            "denied": len(self.denials),
            "skipped": len(self.skips),
            "wasted": self.wasted_calls,
            "recovery_turns": self.recovery_turns,
            "history_ok": self.history_well_formed,
            "cancel_ms": None if self.cancel_latency is None else round(self.cancel_latency * 1000, 1),
        }


# --- Runner ------------------------------------------------------------------

SCENARIO_METRICS: list[dict[str, Any]] = []
# Captured at import (collection), before any scenario redirects HOME, so guards keep the real location.
REAL_ALGO_DIR = (Path.home() / ".algo_cli").resolve()


def format_metrics_table(rows: list[dict]) -> str:
    columns = ("scenario", "completed", "model_calls", "tool_calls", "denied", "skipped", "wasted",
               "recovery_turns", "history_ok", "cancel_ms")
    cells = [[("-" if row[c] is None else str(row[c])) for c in columns] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(columns)]
    lines = [" | ".join(c.ljust(w) for c, w in zip(columns, widths)), "-+-".join("-" * w for w in widths)]
    lines += [" | ".join(v.ljust(w) for v, w in zip(r, widths)) for r in cells]
    done = sum(1 for row in rows if row["completed"])
    lines.append(
        f"totals: {len(rows)} runs, {done} tasks completed, {sum(r['wasted'] for r in rows)} wasted calls, "
        f"{sum(r['recovery_turns'] for r in rows)} recovery turns"
    )
    return "\n".join(lines)



class ScenarioRunner:
    """Built per test by the `scenario` fixture; owns monkeypatching and isolation."""

    def __init__(self, monkeypatch, workspace, node_name: str):
        self.monkeypatch = monkeypatch
        self.workspace = workspace
        self.node_name = node_name
        # Replaced per run; scenario code may call clock.mark_interrupt() from injected Ctrl+C points.
        self.clock = InterruptClock()

    def _config(self, overrides: dict[str, Any]) -> config_module.Config:
        values: dict[str, Any] = {
            "cwd": str(self.workspace),
            "model": "scripted-model",
            "safe_mode": True,
            "max_tool_iterations": 12,
            "skill_crystallize_enabled": False,
            "code_rag_enabled": False,
        }
        values.update(overrides)
        return config_module.Config(**values)

    def _quiet_runtime(self) -> None:
        from test_main_helpers import _patch_agent_loop_for_tool_policy_test

        _patch_agent_loop_for_tool_policy_test(self.monkeypatch)
        self.monkeypatch.setattr(main, "json_sink", display.json_sink)
        self.monkeypatch.setattr(main, "record_perf_event", lambda *_a, **_k: None)
        self.monkeypatch.setattr(
            main.memory_runtime, "capture_completed_user_turn", lambda *_a, **_k: {"status": "skipped"}
        )

    def run(
        self,
        rounds: list[Round] | Callable[[int], Round],
        *,
        prompt: str = "inspect this project",
        driver: str = "oneshot",
        approval_mode: str = "auto",
        tools: dict[str, ToolFake] | None = None,
        real_tools: Iterable[str] = (),
        approve: Callable[..., bool] | None = None,
        save_error: BaseException | None = None,
        config: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> ScenarioResult:
        """Run one full turn. driver='oneshot' uses run_oneshot; 'interactive' calls agent_loop like the REPL."""

        if driver not in {"oneshot", "interactive"}:
            raise ValueError("driver must be 'oneshot' or 'interactive'")
        self._quiet_runtime()
        cfg = self._config(config or {})
        self.monkeypatch.setattr(config_module.Config, "load", classmethod(lambda cls: cfg))
        saves: list[int] = []

        def save(_self):
            saves.append(1)
            if save_error is not None:
                raise save_error

        self.monkeypatch.setattr(config_module.Config, "save", save)
        clock = self.clock = InterruptClock()
        recorder = ToolRecorder(dict(tools or {}), frozenset(real_tools), main.run_tool, clock)
        self.monkeypatch.setattr(main, "run_tool", recorder)
        approvals: list[tuple[str, dict[str, Any]]] = []
        if approve is None and driver == "interactive":
            approve = _decline_prompt  # never block on a real terminal prompt
        # One-shot keeps the runtime's own non-interactive approval; only observe it.
        decide = approve if approve is not None else main.ask_approval

        def recording_approve(name, args, *a, **k):
            approvals.append((name, dict(args)))
            return decide(name, args, *a, **k)

        self.monkeypatch.setattr(main, "ask_approval", recording_approve)

        stream = io.StringIO()
        display_buffer = io.StringIO()
        typed_events: list[dict[str, Any]] = []

        def event_count() -> int:
            return len(stream.getvalue().splitlines()) if driver == "oneshot" else len(typed_events)

        model = ScriptedModel(rounds, clock=clock, events_so_far=event_count)
        self.monkeypatch.setattr(main, "create_client", lambda _cfg: model)

        exit_code: int | None = None
        raised: BaseException | None = None
        started = time.monotonic()
        if driver == "oneshot":
            self.monkeypatch.setattr(main, "show_stream_text", display.show_stream_text)
            self.monkeypatch.setattr(main, "show_tool_call", display.show_tool_call)
            exit_code = oliver_oneshot.run_oneshot(prompt=prompt, approval_mode=approval_mode, stream=stream)
        else:
            self._record_display(display_buffer, typed_events)
            setattr(cfg, "_nathan_approval_mode", "interactive")
            try:
                main.agent_loop(model, cfg, prompt)
            except BaseException as exc:  # the REPL catches KeyboardInterrupt and provider errors
                raised = exc
                display_buffer.write(f"[repl] {main.generation_interrupted_message(exc)}\n"
                                     if isinstance(exc, KeyboardInterrupt) else f"[repl-error] {exc!r}\n")
        finished = time.monotonic()
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()] + typed_events
        result = ScenarioResult(
            name=name or self.node_name,
            driver=driver,
            exit_code=exit_code,
            events=events,
            display_text=display_buffer.getvalue(),
            messages=list(cfg.messages),
            model=model,
            invocations=list(recorder.invocations),
            raised=raised,
            elapsed=finished - started,
            cancel_latency=None if clock.interrupted_at is None else finished - clock.interrupted_at,
            approvals=approvals,
            saves=len(saves),
        )
        SCENARIO_METRICS.append(result.metrics())
        return result

    def _record_display(self, buffer: io.StringIO, typed_events: list[dict[str, Any]]) -> None:
        """Interactive driver: capture what the user would see as plain lines, plus typed outcomes."""

        def typed_result(name, result, *, outcome_status, duration_ms=None, call_id=None):
            status = "ok" if outcome_status.value == "succeeded" else outcome_status.value
            typed_events.append(
                {"type": "tool_result", "call_id": call_id, "name": name, "status": status, "summary": str(result)}
            )
            buffer.write(f"[tool_result] {name} {status}: {str(result).splitlines()[0] if result else ''}\n")

        self.monkeypatch.setattr(nathan_runtime, "show_typed_tool_result", typed_result)
        self.monkeypatch.setattr(main, "show_typed_tool_result", typed_result)

        def line(kind: str):
            return lambda *args, **kwargs: buffer.write(
                f"[{kind}] " + " ".join(str(arg) for arg in args) + (f" {kwargs}" if kwargs else "") + "\n"
            )

        for name, kind in (
            ("show_stream_text", "text"),
            ("show_error", "error"),
            ("show_info", "info"),
            ("show_tool_call", "tool_call"),
            ("show_tool_result", "tool_result"),
        ):
            self.monkeypatch.setattr(main, name, line(kind))
        self.monkeypatch.setattr(nathan_runtime, "show_tool_call", line("tool_call"))
        self.monkeypatch.setattr(nathan_runtime, "show_tool_result", line("tool_result"))
        self.monkeypatch.setattr(main, "start_streaming_response", lambda: None)
        self.monkeypatch.setattr(main, "finish_streaming_response", lambda: None)
        from algo_cli import sticky_status

        self.monkeypatch.setattr(sticky_status, "start", lambda *_a, **_k: None)
        self.monkeypatch.setattr(sticky_status, "stop", lambda *_a, **_k: None)
        self.monkeypatch.setattr(main.console, "rule", lambda *_a, **_k: None)

    def run_agent(
        self,
        rounds: list[Round] | Callable[[int], Round],
        *,
        task: str = "Review the runtime",
        pipeline_name: str = "review",
        config: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> ScenarioResult:
        """Run a full /agent pipeline; each block consumes model rounds in order."""

        self._quiet_runtime()
        cfg = self._config(config or {})
        self.monkeypatch.setattr(config_module.Config, "save", lambda _self: None)
        errors: list[str] = []
        buffer = io.StringIO()

        def record(kind: str):
            def show(*args, **kwargs):
                if kind == "error":
                    errors.append(str(args[0]))
                buffer.write(f"[{kind}] " + " ".join(str(arg) for arg in args) + "\n")
            return show

        for display_name, kind in (
            ("show_agent_block_start", "block_start"),
            ("show_agent_block_complete", "block_complete"),
            ("show_agent_recovery_start", "recovery"),
            ("show_agent_pipeline_complete", "pipeline_complete"),
            ("show_error", "error"),
            ("show_info", "info"),
            ("finish_thinking_block", "thinking_end"),
            ("show_recalled_context", "recalled"),
            ("record_chat_metrics", "metrics"),
            ("flush_perf_records", "perf"),
        ):
            self.monkeypatch.setattr(agent_pipeline, display_name, record(kind))
        self.monkeypatch.setattr(nathan_runtime, "show_tool_call", record("tool_call"))
        self.monkeypatch.setattr(nathan_runtime, "show_tool_result", record("tool_result"))
        self.monkeypatch.setattr(agent_pipeline, "should_recover_implementation", lambda _block: False)
        clock = self.clock = InterruptClock()
        model = ScriptedModel(rounds, clock=clock, final_fallback="## Block Output\nfallback")
        pipeline_result = None
        raised: BaseException | None = None
        started = time.monotonic()
        try:
            pipeline_result = agent_pipeline.run_agent_pipeline(task, cfg, model, pipeline_name=pipeline_name)
        except BaseException as exc:
            raised = exc
        finished = time.monotonic()
        blocks = list(agent_pipeline.session_pipeline_blocks())
        threads = agent_threads.load_threads()
        result = ScenarioResult(
            name=name or self.node_name,
            driver="agent",
            exit_code=None,
            events=[],
            display_text=buffer.getvalue(),
            messages=[],
            model=model,
            invocations=[],
            raised=raised,
            elapsed=finished - started,
            cancel_latency=None if clock.interrupted_at is None else finished - clock.interrupted_at,
            pipeline=pipeline_result if pipeline_result is not None else _RaisedPipeline(blocks, raised),
            thread=threads[-1] if threads else None,
        )
        SCENARIO_METRICS.append(result.metrics())
        return result


def _decline_prompt(*_args: Any, **_kwargs: Any) -> bool:
    return False


@dataclass
class _RaisedPipeline:
    blocks: list[Any]
    error: BaseException | None
    status: str = "raised"
