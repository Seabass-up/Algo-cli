"""Algo CLI agent runtime."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

__version__ = "0.18.0"

_RENAMED_INTERNAL_MODULES = {
    "oneshot": "oliver_oneshot",
    "program_runtime": "nathan_program_runtime",
    "tool_policy": "samuel_policy",
    "tool_runtime": "nathan_runtime",
    "runtime_qos": "theodore_runtime_qos",
}


def __getattr__(name: str) -> ModuleType:
    """Keep package-attribute imports working after hardening module renames."""

    target = _RENAMED_INTERNAL_MODULES.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f".{target}", __name__)
    globals()[name] = module
    return module
