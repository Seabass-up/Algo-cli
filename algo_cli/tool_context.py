"""Bounded, task-local tool-context discovery.

The runtime policy remains the enforcement layer.  This module decides which
function schemas are worth showing to the model; it never grants permission to
execute an action.  Explicit ``Allowed tool classes:`` declarations remain a
fail-closed compatibility contract.  Ordinary prompts use a small always-on
core plus deterministic BM25 ranking over the live tool and ActionSpec catalog.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from .retrieval_algorithms import BM25Index, lexical_tokens, stable_top_k


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

# Keep deterministic local inspection available even when the prompt is vague.
# action_search/action_program are supplied by the deferred program runtime and
# are included automatically when that optional runtime is installed.
CORE_TOOL_NAMES = frozenset(
    {
        "read_file",
        "list_directory",
        "search_files",
        "git_status",
        "git_diff",
    }
)
DEFERRED_TOOL_NAMES = frozenset({"action_search", "action_program"})
DEFAULT_TOOL_LIMIT = 12
MAX_QUERY_TERMS = 64

_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "also",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "cli",
        "do",
        "for",
        "from",
        "has",
        "have",
        "help",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "that",
        "the",
        "this",
        "to",
        "tool",
        "tools",
        "use",
        "using",
        "we",
        "with",
        "you",
    }
)

# Small query expansion closes common intent/vocabulary gaps without exposing a
# hand-maintained capability router.  The expanded terms still go through the
# same catalog ranking and never bypass ActionSpec policy.
_QUERY_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "build": ("write", "edit", "shell", "diff"),
    "change": ("write", "edit", "diff"),
    "code": ("file", "edit", "shell", "diff"),
    "create": ("write", "create"),
    "debug": ("read", "shell", "diff"),
    "fix": ("read", "edit", "shell", "diff"),
    "implement": ("write", "edit", "shell", "diff"),
    "internet": ("web", "search", "fetch"),
    "latest": ("web", "search", "fetch"),
    "remember": ("memory", "profile", "lesson"),
    "research": ("web", "search", "fetch"),
    "test": ("run", "shell", "verification"),
    "update": ("write", "edit", "diff"),
    "verify": ("run", "shell", "diff", "status"),
}
_CODE_UPDATE_ANCHORS = frozenset(
    {"code", "config", "configuration", "file", "function", "module", "project", "repo", "repository", "source"}
)


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


def _action_metadata() -> dict[str, str]:
    """Return searchable ActionSpec text without making the registry required."""

    try:
        from .action_registry import effective_action_specs

        specs = effective_action_specs(include_archived=False)
    except Exception:
        # Catalog ranking is an optimization. A diagnostic registry failure must
        # never make the primary tool loop unavailable.
        return {}
    metadata: dict[str, str] = {}
    for spec in specs:
        if getattr(spec, "kind", None) != "tool":
            continue
        name = str(getattr(spec, "name", "") or "")
        if not name:
            continue
        metadata[name] = " ".join(
            str(value)
            for value in (
                getattr(spec, "description", ""),
                getattr(spec, "group", ""),
                " ".join(getattr(spec, "tags", ()) or ()),
            )
            if value
        )
    return metadata


def _tool_search_text(tool: Callable[..., Any], action_metadata: dict[str, str]) -> str:
    name = str(getattr(tool, "__name__", "") or "")
    doc = str(getattr(tool, "__doc__", "") or "").strip().split("\n\n", 1)[0]
    readable_name = re.sub(r"[^a-zA-Z0-9]+", " ", name)
    return " ".join((readable_name, name, doc, action_metadata.get(name, "")))


def _expanded_query_terms(prompt: str) -> tuple[str, ...]:
    source_terms = lexical_tokens(prompt)
    source_term_set = set(source_terms)
    terms: list[str] = []
    seen: set[str] = set()
    for term in source_terms:
        if term not in _QUERY_STOPWORDS and term not in seen:
            terms.append(term)
            seen.add(term)
        expansions = _QUERY_EXPANSIONS.get(term, ())
        if term == "update" and not (_CODE_UPDATE_ANCHORS & source_term_set):
            expansions = ()
        for expansion in expansions:
            if expansion not in seen:
                terms.append(expansion)
                seen.add(expansion)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return tuple(terms[:MAX_QUERY_TERMS])


def rank_tools_for_prompt(
    prompt: str,
    all_tools: Sequence[Callable[..., Any]],
) -> list[Callable[..., Any]]:
    """Return relevant tools in deterministic descending catalog rank."""

    tools = list(all_tools)
    if not tools:
        return []
    query_terms = _expanded_query_terms(prompt)
    if not query_terms:
        return []
    action_metadata = _action_metadata()
    documents = [_tool_search_text(tool, action_metadata) for tool in tools]
    scores = BM25Index(documents).scores(query_terms)
    query_term_set = set(query_terms)

    def score(index: int) -> float:
        value = scores[index]
        name_terms = set(lexical_tokens(re.sub(r"[^a-zA-Z0-9]+", " ", getattr(tools[index], "__name__", ""))))
        if name_terms and name_terms <= query_term_set:
            value += 2.0
        return value

    positive = [index for index in range(len(tools)) if score(index) > 0.0]
    ranked_indices = stable_top_k(positive, len(positive), score=score)
    return [tools[index] for index in ranked_indices]


def select_tools_for_prompt(
    prompt: str,
    all_tools: Sequence[Callable[..., Any]],
    *,
    limit: int = DEFAULT_TOOL_LIMIT,
) -> list[Callable[..., Any]]:
    """Select a bounded model-visible tool catalog for one user turn.

    Explicit declarations select only named known classes. Unknown classes
    contribute no tools, preserving the previous fail-closed behavior. Without
    a declaration, a bounded core and BM25-relevant tools are returned in the
    runtime's original stable order for provider prompt-cache friendliness.
    """

    tools = list(all_tools)
    declared = declared_tool_classes(prompt)
    if declared is not None:
        allowed_names: set[str] = set()
        for tool_class in declared:
            allowed_names.update(_CLASS_TO_TOOLS.get(tool_class, ()))
        return [tool for tool in tools if getattr(tool, "__name__", "") in allowed_names]

    bounded_limit = max(1, int(limit))
    available_names = {str(getattr(tool, "__name__", "") or "") for tool in tools}
    selected_names = set(CORE_TOOL_NAMES & available_names)
    selected_names.update(DEFERRED_TOOL_NAMES & available_names)
    if "action_search" not in available_names and "available_actions" in available_names:
        selected_names.add("available_actions")

    effective_limit = max(bounded_limit, len(selected_names))
    for tool in rank_tools_for_prompt(prompt, tools):
        if len(selected_names) >= effective_limit:
            break
        selected_names.add(str(getattr(tool, "__name__", "") or ""))
    return [tool for tool in tools if getattr(tool, "__name__", "") in selected_names]


__all__ = [
    "CORE_TOOL_NAMES",
    "DEFAULT_TOOL_LIMIT",
    "DEFERRED_TOOL_NAMES",
    "declared_tool_classes",
    "rank_tools_for_prompt",
    "select_tools_for_prompt",
]
