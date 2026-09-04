"""Provider-neutral state machine for model and tool-call transcripts.

This module owns protocol structure only. It deliberately has no access to
approval configuration, policy decisions, or tool implementations.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Sequence

from .chat_protocol import serialize_tool_call


MODEL_EVENT_KINDS = frozenset({"content", "reasoning", "tool"})
TERMINAL_PHASES = frozenset({"interrupted", "timed_out", "cancelled"})


class ProviderToolProtocolError(RuntimeError):
    """Raised when a provider/tool transcript cannot remain balanced."""


def _new_loop_id() -> str:
    return secrets.token_hex(8)


def _clean_call_id(value: Any) -> str:
    if value is None:
        return ""
    call_id = str(value).strip()
    if (
        not call_id
        or len(call_id) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in call_id)
    ):
        return ""
    return call_id


@dataclass
class ProviderToolLoopState:
    """Validate one ordinary-chat or Agent-Block provider/tool loop.

    Missing provider call IDs are filled with runtime-owned IDs. Duplicate or
    reused provider IDs are replaced as well, while the whole batch is marked
    for fail-closed quarantine by the caller.
    """

    loop_id: str = field(default_factory=_new_loop_id)
    phase: str = "ready"
    round_number: int = -1
    expected_tool_results: int = 0
    observed_tool_results: int = 0
    expected_call_ids: tuple[str, ...] = ()
    model_event_counts: dict[str, int] = field(
        default_factory=lambda: {
            "content": 0,
            "reasoning": 0,
            "tool": 0,
        }
    )
    protocol_violations: tuple[str, ...] = ()
    interruption_reason: str = ""
    _seen_call_ids: set[str] = field(default_factory=set, repr=False)
    _dispatched_call_ids: set[str] = field(default_factory=set, repr=False)
    _completed_call_ids: set[str] = field(default_factory=set, repr=False)
    _mutation_call_ids: set[str] = field(default_factory=set, repr=False)
    _uncertain_mutation_call_ids: set[str] = field(
        default_factory=set,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.loop_id = _clean_call_id(self.loop_id)
        if not self.loop_id:
            raise ProviderToolProtocolError(
                "provider loop ID must be bounded non-empty text"
            )

    @property
    def uncertain_mutation_call_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._uncertain_mutation_call_ids))

    def begin_model_round(self, round_number: int) -> None:
        if (
            self.phase != "ready"
            or isinstance(round_number, bool)
            or not isinstance(round_number, int)
            or round_number < 0
            or round_number <= self.round_number
        ):
            raise ProviderToolProtocolError(
                "model round cannot start from the current loop state"
            )
        self.phase = "model_inflight"
        self.round_number = round_number
        self.expected_tool_results = 0
        self.observed_tool_results = 0
        self.expected_call_ids = ()
        self.protocol_violations = ()
        self.interruption_reason = ""
        self._dispatched_call_ids.clear()
        self._completed_call_ids.clear()
        self._mutation_call_ids.clear()
        self.model_event_counts = {
            "content": 0,
            "reasoning": 0,
            "tool": 0,
        }

    def record_model_event(self, kind: str) -> None:
        if self.phase != "model_inflight" or kind not in MODEL_EVENT_KINDS:
            raise ProviderToolProtocolError(
                "model event is outside an active model round"
            )
        self.model_event_counts[kind] += 1

    def _synthetic_call_id(self, index: int) -> str:
        return (
            f"algo-{self.loop_id}-r{self.round_number:04d}"
            f"-c{index:04d}"
        )

    def complete_model_round(
        self,
        tool_calls: int | Sequence[Any],
    ) -> list[dict[str, Any]]:
        if self.phase != "model_inflight":
            raise ProviderToolProtocolError(
                "model round completion is structurally invalid"
            )
        if isinstance(tool_calls, bool):
            raise ProviderToolProtocolError(
                "model round completion is structurally invalid"
            )
        if isinstance(tool_calls, int):
            if tool_calls < 0:
                raise ProviderToolProtocolError(
                    "model round completion is structurally invalid"
                )
            serialized = [
                {
                    "id": self._synthetic_call_id(index),
                    "function": {"name": "", "arguments": {}},
                }
                for index in range(tool_calls)
            ]
        else:
            serialized = [
                serialize_tool_call(call)
                for call in tool_calls
            ]

        effective_ids: list[str] = []
        violations: list[str] = []
        batch_seen: set[str] = set()
        for index, call in enumerate(serialized):
            provider_id = _clean_call_id(call.get("id"))
            if provider_id and (
                provider_id in batch_seen
                or provider_id in self._seen_call_ids
            ):
                violations.append("duplicate_or_reused_call_id")
                provider_id = ""
            effective_id = provider_id or self._synthetic_call_id(index)
            while (
                effective_id in batch_seen
                or effective_id in self._seen_call_ids
            ):
                effective_id += "-x"
            call["id"] = effective_id
            effective_ids.append(effective_id)
            batch_seen.add(effective_id)

        self._seen_call_ids.update(effective_ids)
        self.expected_call_ids = tuple(effective_ids)
        self.expected_tool_results = len(effective_ids)
        self.protocol_violations = tuple(dict.fromkeys(violations))
        self.phase = "model_complete"
        return serialized

    def finish_without_tools(self) -> None:
        if (
            self.phase != "model_complete"
            or self.expected_tool_results != 0
        ):
            raise ProviderToolProtocolError(
                "tool-free completion has pending tool calls"
            )
        self.phase = "ready"

    def begin_tool_batch(self) -> None:
        if (
            self.phase != "model_complete"
            or self.expected_tool_results < 1
        ):
            raise ProviderToolProtocolError(
                "tool dispatch has no completed model tool batch"
            )
        self.phase = "tools_inflight"

    def _resolve_call_id(self, call_id: str | None, *, result: bool) -> str:
        cleaned = _clean_call_id(call_id)
        if cleaned:
            return cleaned
        consumed = (
            self._completed_call_ids
            if result
            else self._dispatched_call_ids
        )
        for expected in self.expected_call_ids:
            if expected not in consumed:
                return expected
        return ""

    def record_tool_dispatch(
        self,
        call_id: str | None = None,
        *,
        mutating: bool,
    ) -> str:
        if self.phase != "tools_inflight":
            raise ProviderToolProtocolError(
                "tool dispatch is outside a tool batch"
            )
        effective_id = self._resolve_call_id(call_id, result=False)
        if (
            effective_id not in self.expected_call_ids
            or effective_id in self._dispatched_call_ids
        ):
            raise ProviderToolProtocolError(
                "tool dispatch is duplicated or orphaned"
            )
        self._dispatched_call_ids.add(effective_id)
        if mutating:
            self._mutation_call_ids.add(effective_id)
        return effective_id

    def record_tool_result(
        self,
        call_id: str | None = None,
        *,
        mutation_outcome_uncertain: bool = False,
    ) -> str:
        if self.phase != "tools_inflight":
            raise ProviderToolProtocolError(
                "tool result is duplicated or outside a tool batch"
            )
        effective_id = self._resolve_call_id(call_id, result=True)
        if effective_id not in self._dispatched_call_ids:
            raise ProviderToolProtocolError(
                "tool result is orphaned from its dispatch"
            )
        if effective_id in self._completed_call_ids:
            raise ProviderToolProtocolError(
                "tool result is duplicated or outside a tool batch"
            )
        self._completed_call_ids.add(effective_id)
        self.observed_tool_results = len(self._completed_call_ids)
        if (
            mutation_outcome_uncertain
            and effective_id in self._mutation_call_ids
        ):
            self._uncertain_mutation_call_ids.add(effective_id)
        return effective_id

    def finish_tool_batch(self) -> None:
        if (
            self.phase != "tools_inflight"
            or self._dispatched_call_ids != set(self.expected_call_ids)
            or self._completed_call_ids != set(self.expected_call_ids)
        ):
            raise ProviderToolProtocolError(
                "provider tool batch is missing balanced tool results"
            )
        self.phase = "ready"

    def tool_batch_ceiling_codes(self) -> tuple[str, ...]:
        if not self.expected_call_ids:
            return ()
        code = (
            "provider_tool_protocol"
            if self.protocol_violations
            else ""
        )
        return tuple(code for _call_id in self.expected_call_ids)

    def interrupt(
        self,
        reason: str,
        *,
        timed_out: bool = False,
    ) -> None:
        if self.phase in TERMINAL_PHASES:
            return
        if self.phase == "tools_inflight":
            unresolved = (
                self._dispatched_call_ids - self._completed_call_ids
            )
            self._uncertain_mutation_call_ids.update(
                unresolved & self._mutation_call_ids
            )
        self.interruption_reason = str(reason).strip()[:500]
        self.phase = "timed_out" if timed_out else "interrupted"

    def cancel(self, reason: str = "cancelled") -> None:
        self.interrupt(reason)
        self.phase = "cancelled"

    def assert_provider_fallback_safe(self) -> None:
        unresolved = self._dispatched_call_ids - self._completed_call_ids
        if self._uncertain_mutation_call_ids:
            raise ProviderToolProtocolError(
                "provider fallback could replay an uncertain mutation"
            )
        if unresolved:
            raise ProviderToolProtocolError(
                "provider fallback has unresolved tool dispatches"
            )


# Compatibility names used by existing Agent Block callers and plugins.
AgentLoopState = ProviderToolLoopState
AgentLoopProtocolError = ProviderToolProtocolError
