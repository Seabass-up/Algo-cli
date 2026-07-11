"""Resolve an agent session cwd from generic project hints."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any


_WIN_DRIVE_ABS = re.compile(r"^(?:\\\\\?\\)?[A-Za-z]:[\\/]")


def parse_path_arg(arg: str) -> str:
    """Parse a single path from a slash-command argument (handles quoted Windows paths)."""
    text = (arg or "").strip()
    if not text:
        return ""
    # shlex treats backslash as escape (POSIX rules), so a Windows absolute path loses separators.
    if _WIN_DRIVE_ABS.match(text) or (len(text) >= 2 and text[1] == ":" and text[0].isalpha()):
        return text.strip('"').strip("'")
    if text[0] in "\"'":
        try:
            parts = shlex.split(text)
            return parts[0] if parts else ""
        except ValueError:
            return text.strip('"').strip("'")
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = [text.strip('"').strip("'")]
    return parts[0] if parts else ""

_ALGO_CLI_PATH = re.compile(r"\balgo_cli/", re.IGNORECASE)
_ALGO_CLI_NAME = re.compile(r"\b(?:ollama-cli|algo-cli)\b", re.IGNORECASE)


def candidate_workspaces(task: str) -> list[Path]:
    text = task or ""
    candidates: list[Path] = []
    if _ALGO_CLI_PATH.search(text) or _ALGO_CLI_NAME.search(text):
        candidates.append(Path.home() / "algo-cli")
        legacy = Path.home() / "ollama-cli"
        if legacy.is_dir() and legacy != candidates[-1]:
            candidates.append(legacy)
    return candidates


def _is_valid_workspace(root: Path) -> bool:
    if not root.is_dir():
        return False
    return (root / "algo_cli").is_dir()


def resolve_agent_workspace(task: str, cfg: Any) -> bool:
    """Point cfg.cwd at a known project root inferred from the task. Returns True if updated."""
    seen: set[str] = set()
    for root in candidate_workspaces(task):
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if _is_valid_workspace(root):
            cfg.cwd = str(root.resolve())
            if hasattr(cfg, "save"):
                cfg.save()
            return True
    return False
