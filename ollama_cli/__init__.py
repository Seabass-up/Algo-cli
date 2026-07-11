"""Legacy import compatibility shim for the renamed :mod:`algo_cli` package.

The project was renamed from ``ollama_cli`` to ``algo_cli``.  Keep the old
Python package importable for one compatibility window so existing tests,
plugins, and user scripts do not fail at import time.  Submodules are aliased
to the real ``algo_cli`` modules so monkeypatching either import path affects
one shared module object.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

from algo_cli import __version__


# Submodules that are known public/legacy import targets.  Keeping this list
# explicit prevents accidental exposure of arbitrary module names while covering
# the existing package surface used by tests and old integrations.
_LEGACY_SUBMODULES = {
    "agent_blocks",
    "agent_pipeline",
    "chat_protocol",
    "config",
    "context_budget",
    "display",
    "git_evidence",
    "harness",
    "identity",
    "intuition_engine",
    "main",
    "model_info",
    "model_routing",
    "oneshot",
    "perf_telemetry",
    "slash_dispatch",
    "tool_runtime",
    "task_router",
    "tool_policy",
    "tools",
    "x_account",
    "xai_auth",
    "xai_client",
}


def _alias_submodule(name: str) -> ModuleType:
    if name not in _LEGACY_SUBMODULES:
        raise AttributeError(f"module 'ollama_cli' has no attribute {name!r}")
    module = importlib.import_module(f"algo_cli.{name}")
    sys.modules[f"ollama_cli.{name}"] = module
    globals()[name] = module
    return module


def __getattr__(name: str) -> Any:
    return _alias_submodule(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LEGACY_SUBMODULES)


__all__ = ["__version__", *_LEGACY_SUBMODULES]
