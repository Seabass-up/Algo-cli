"""Prompt-declared tool-context selection.

The runtime policy remains the enforcement layer. This module only reduces the
function schemas shown to the model when a task explicitly declares its allowed
tool classes, improving signal and prompt-cache efficiency.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any


_DECLARATION_RE = re.compile(
    r"(?im)^\s*allowed\s+tool(?:\s+classes)?\s*:\s*([^\r\n]+)"
)
_CLASS_TO_TOOLS: dict[str, frozenset[str]] = {
    "filesystem": frozenset(
        {
            "read_file",
            "edit_file",
            "read_pdf",
            "render_pdf_pages",
            "write_file",
            "list_directory",
            "search_files",
            "find_unique_anchor",
            "batch_edit",
            "git_status",
            "git_diff",
        }
    ),
    "shell": frozenset({"run_shell"}),
    "web": frozenset({"web_search", "web_fetch"}),
    "network": frozenset({"web_search", "web_fetch", "x_search"}),
    "memory": frozenset({"remember", "append_lesson", "update_user_profile"}),
    "model": frozenset(
        {"embed_text", "vision_describe", "model_pull", "model_delete", "model_create", "model_copy", "model_show"}
    ),
    "harness": frozenset(
        {"harness_refresh", "harness_stats", "harness_scorecard", "harness_competitive_rating", "harness_search", "harness_read"}
    ),
    "knowledge_graph": frozenset(
        {"query_knowledge_graph", "reindex_knowledge_graph", "write_knowledge_graph_note"}
    ),
    "session": frozenset({"available_actions", "session_slash", "session_command"}),
    "plugins": frozenset({"plugins_discover", "plugins_load"}),
    "credentials": frozenset({"credential_helpers_get", "credential_helpers_store"}),
    "social": frozenset(
        {"x_search", "x_account_status", "x_account_draft_post", "x_account_draft_reply", "x_account_post", "x_account_reply", "x_account_post_action"}
    ),
}


def _normalize_class(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


def declared_tool_classes(prompt: str) -> tuple[str, ...] | None:
    """Return normalized explicit tool classes, or ``None`` when undeclared."""

    match = _DECLARATION_RE.search(prompt or "")
    if match is None:
        return None
    values = tuple(
        normalized
        for raw in match.group(1).split(",")
        if (normalized := _normalize_class(raw))
    )
    return values


def select_tools_for_prompt(
    prompt: str,
    all_tools: Sequence[Callable[..., Any]],
) -> list[Callable[..., Any]]:
    """Select only explicitly declared known tool classes.

    Unknown classes contribute no tools. This fails closed instead of
    accidentally exposing the full runtime catalog when a declaration has a
    typo or names a class implemented by another harness.
    """

    declared = declared_tool_classes(prompt)
    if declared is None:
        return list(all_tools)
    allowed_names: set[str] = set()
    for tool_class in declared:
        allowed_names.update(_CLASS_TO_TOOLS.get(tool_class, ()))
    return [tool for tool in all_tools if getattr(tool, "__name__", "") in allowed_names]


__all__ = ["declared_tool_classes", "select_tools_for_prompt"]
