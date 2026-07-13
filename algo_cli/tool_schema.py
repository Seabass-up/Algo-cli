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
        if convert_function_to_tool is None:
            continue
        try:
            payload.append(convert_function_to_tool(item).model_dump(exclude_none=True))
        except Exception:
            # Provider adapters skip unconvertible callables too; accounting
            # must describe the request that can actually be emitted.
            continue
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimate_tool_schema_tokens(
    tools: Sequence[Callable[..., Any] | dict[str, Any]],
) -> int:
    """Return Algo CLI's conservative deterministic chars/4 estimate."""

    encoded = serialized_tool_schemas(tools)
    return 0 if encoded == "[]" else max(1, (len(encoded) + 3) // 4)


__all__ = ["estimate_tool_schema_tokens", "serialized_tool_schemas"]
