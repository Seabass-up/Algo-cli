"""Provider-neutral tool-schema size accounting.

The model request budget must include callable schemas as well as messages.
This module deliberately uses the same Ollama SDK conversion used by the
provider adapters, while keeping token estimation deterministic and offline.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

try:
    from ollama._utils import convert_function_to_tool
except ImportError:  # pragma: no cover - exercised only without the runtime SDK
    convert_function_to_tool = None  # type: ignore[assignment]


def serialized_tool_schemas(
    tools: Sequence[Callable[..., Any] | dict[str, Any]],
) -> str:
    """Serialize the callable catalog in the provider-adapter wire shape."""

    payload: list[dict[str, Any]] = []
    for item in tools:
        if isinstance(item, dict):
            payload.append(item)
            continue
        native = _native_tool_schema(item)
        if native is not None:
            payload.append(native)
            continue
        if convert_function_to_tool is None:
            continue
        try:
            payload.append(convert_function_to_tool(item).model_dump(exclude_none=True))
        except Exception:
            # Provider adapters skip unconvertible callables too; accounting
            # must describe the request that can actually be emitted.
            continue
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _native_tool_schema(tool: Callable[..., Any]) -> dict[str, Any] | None:
    from .continuum_tools import memory_set_applicability
    if tool is not memory_set_applicability:
        return _decision_schema(tool)
    return {"type": "function", "function": {
        "name": "memory_set_applicability", "description": memory_set_applicability.__doc__,
        "parameters": {"type": "object", "additionalProperties": False,
            "required": ["memory_id", "memory_revision", "projects", "reason", "scope"],
            "properties": {
                "memory_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "memory_revision": {"type": "integer", "minimum": 1},
                "projects": {"type": ["array", "null"], "minItems": 1, "maxItems": 16,
                    "uniqueItems": True, "items": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"}},
                "reason": {"type": "string", "minLength": 1, "maxLength": 2048},
                "scope": {"type": "string", "enum": ["shared", "private"]},
                "expected_revision": {"type": "integer", "minimum": 0},
                "request_id": {"type": "string", "minLength": 1, "maxLength": 256},
            }},
    }}


def _decision_schema(tool: Callable[..., Any]) -> dict[str, Any] | None:
    from .decision_layer import decision_decide

    if tool is not decision_decide:
        return None
    # SDK callable conversion joins union types into an invalid comma-separated string.
    return {"type": "function", "function": {
        "name": "decision_decide", "description": (
            "Request a bounded advisory decision. Supply state and named questions with type/instructions/criteria: "
            "choice uses option IDs mapped to descriptions; noul returns probability of a condition; score uses "
            "ordered descriptions and may return a fractional value. Providers: jev, clef, clef-flash, clm, decider, "
            "tev1 or configured adapters. Omit provider for Jev coordination; explicit provider is a user override. "
            "The main AI frames bounded questions and retains planning and final judgment. "
            "Use at reasoning checkpoints; pause dependent steps and resume after validated answers. "
            "Routing includes advisory kernel/algorithm recommendations, never automatic execution. "
            "Clef-flash/Tev1 replace the coordinator only after confirmed internet loss. "
            "local_only defaults true and forbids remote routing/fallbacks; online Jev requires false "
            "and runtime authority. Optional explicit policy supplies noul_thresholds or minimum_choice_probability. "
            "The main model uses validated answers to plan; normal tools authorize actions and verify outcomes."
        ),
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["state", "questions"], "properties": {
                           "state": {"type": ["string", "object", "array"]},
                           "questions": {"type": "object", "description": (
                               "Map IDs to question objects. Only type, instructions, criteria are allowed. "
                               "Choice example: {\"step\":{\"type\":\"choice\",\"instructions\":\"Which step fits the evidence?\","
                               "\"criteria\":{\"proceed\":\"Evidence supports proceeding\",\"defer\":\"Evidence is missing\"}}}. "
                               "Noul example: {\"ready\":{\"type\":\"noul\",\"instructions\":\"Is evidence present?\","
                               "\"criteria\":{\"true\":\"Evidence exists\",\"false\":\"No evidence\"}}}. "
                               "Score example: {\"quality\":{\"type\":\"score\",\"instructions\":\"How complete?\","
                               "\"criteria\":[\"Missing\",\"Partial\",\"Complete\"]}}. Criteria is never prose."
                           )},
                           "provider": {"type": "string"}, "task": {"type": "string"},
                           "local_only": {"type": "boolean", "default": True},
                           "policy": {"type": "object"},
                       }},
    }}


def provider_tools(tools: Sequence[Callable[..., Any] | dict[str, Any]]) -> list[Any]:
    """Send native schemas through every conversational provider adapter unchanged."""
    if any(callable(tool) and _native_tool_schema(tool) is not None for tool in tools):
        return json.loads(serialized_tool_schemas(tools))
    return list(tools)


def estimate_tool_schema_tokens(
    tools: Sequence[Callable[..., Any] | dict[str, Any]],
) -> int:
    """Return Algo CLI's conservative deterministic chars/4 estimate."""

    encoded = serialized_tool_schemas(tools)
    return 0 if encoded == "[]" else max(1, (len(encoded) + 3) // 4)


__all__ = ["estimate_tool_schema_tokens", "serialized_tool_schemas", "provider_tools"]
