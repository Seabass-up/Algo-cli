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
from .tool_schema import estimate_tool_schema_tokens


_DECLARATION_RE = re.compile(r"(?im)^\s*allowed\s+tool(?:\s+classes)?\s*:\s*([^\r\n]+)")
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
    "jev": frozenset({"jev_kernel_status", "jev_question_contract"}),
    "web": frozenset({"web_search", "web_fetch"}),
    "network": frozenset({"web_search", "web_fetch", "x_search"}),
    "memory": frozenset(
        {
            "remember",
            "append_lesson",
            "write_knowledge_graph_note",
            "update_user_profile",
            "memory_init",
            "memory_verify",
            "memory_status",
            "memory_capture",
            "memory_remember",
            "memory_get",
            "memory_read",
            "memory_search",
            "memory_context",
            "memory_validate_context",
            "memory_revoke",
            "memory_resolve",
            "memory_explain",
            "memory_history",
            "memory_handoff",
        }
    ),
    "model": frozenset(
        {"embed_text", "vision_describe", "model_pull", "model_delete", "model_create", "model_copy", "model_show"}
    ),
    "harness": frozenset(
        {
            "harness_refresh",
            "harness_stats",
            "harness_scorecard",
            "harness_competitive_rating",
            "harness_search",
            "harness_read",
        }
    ),
    "knowledge_graph": frozenset({"query_knowledge_graph", "reindex_knowledge_graph", "write_knowledge_graph_note"}),
    "session": frozenset({"available_actions", "session_slash", "session_command"}),
    "plugins": frozenset({"plugins_discover", "plugins_load"}),
    "credentials": frozenset({"credential_helpers_get", "credential_helpers_store"}),
    "social": frozenset(
        {
            "x_search",
            "x_account_status",
            "x_account_draft_post",
            "x_account_draft_reply",
            "x_account_post",
            "x_account_reply",
            "x_account_post_action",
        }
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
DEFAULT_SCHEMA_TOKEN_BUDGET = 2_150
MAX_QUERY_TERMS = 64

# A single incidental domain word must not pull an entire specialist surface
# into the prompt.  These gates apply before BM25 ranking and still allow
# action_search to discover a deferred capability later.
_SPECIALIZED_INTENT_GATES: tuple[tuple[str, frozenset[str], frozenset[str]], ...] = (
    ("jev_", frozenset(), frozenset({"jev", "classify", "classification", "triage", "rank", "review"})),
    (
        "memory_",
        frozenset({"memory"}),
        frozenset(
            {
                "build",
                "capture",
                "context",
                "continuum",
                "exact",
                "handoff",
                "history",
                "integrity",
                "private",
                "read",
                "remember",
                "revoke",
                "search",
                "shared",
                "status",
                "validate",
                "verify",
            }
        ),
    ),
    (
        "harness_",
        frozenset({"harness"}),
        frozenset(
            {"compare", "index", "memory", "rating", "record", "refresh", "score", "search", "skill", "stats", "wiki"}
        ),
    ),
    (
        "screenshot_",
        frozenset(),
        frozenset({"image", "pixel", "screen", "screenshot", "visual"}),
    ),
    (
        "url_scheme_",
        frozenset(),
        frozenset({"link", "scheme", "uri", "url"}),
    ),
    (
        "small_context_",
        frozenset({"context"}),
        frozenset({"ledger", "preview", "small"}),
    ),
    ("plugins_", frozenset(), frozenset({"extension", "plugin"})),
    ("x_account_", frozenset({"account"}), frozenset({"draft", "post", "reply", "social", "tweet", "twitter", "xcom"})),
    ("x_search", frozenset(), frozenset({"social", "tweet", "twitter", "xcom"})),
    ("query_knowledge_graph", frozenset(), frozenset({"graph", "knowledge"})),
    ("reindex_knowledge_graph", frozenset(), frozenset({"graph", "knowledge"})),
    ("write_knowledge_graph", frozenset(), frozenset({"graph", "knowledge"})),
    ("web_", frozenset(), frozenset({"internet", "latest", "online", "research", "web"})),
    ("version_manifest_", frozenset(), frozenset({"manifest", "version"})),
    ("extensions_manifest_", frozenset(), frozenset({"extension", "manifest"})),
    ("credential_", frozenset(), frozenset({"credential", "keychain", "secret"})),
    ("google_", frozenset(), frozenset({"calendar", "docs", "drive", "gmail", "google", "sheets"})),
)

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
    "email": ("gmail", "google", "mail"),
    "emails": ("email", "gmail", "google", "mail"),
    "fix": ("read", "edit", "shell", "diff"),
    "implement": ("write", "edit", "shell", "diff"),
    "inbox": ("email", "gmail", "google", "mail"),
    "internet": ("web", "search", "fetch"),
    "latest": ("web", "search", "fetch"),
    "mail": ("email", "gmail", "google"),
    "remember": ("memory", "profile", "lesson"),
    "continuum": ("memory", "context", "integrity", "history"),
    "handoff": ("memory", "context", "resume"),
    "history": ("memory", "exact", "revision"),
    "revoke": ("memory", "destructive", "withdraw"),
    "research": ("web", "search", "fetch"),
    "test": ("run", "shell", "verification"),
    "update": ("write", "edit", "diff"),
    "validate": ("memory", "context", "verify"),
    "verify": ("run", "shell", "diff", "status"),
}
# Generic verbs name an operation, not a capability domain. A candidate that
# matches only these terms is not relevant: "read my email" must not select
# read_file merely because both contain "read".
_GENERIC_QUERY_TERMS = frozenset({"check", "get", "list", "look", "read", "see", "show", "view"})
_CODE_UPDATE_ANCHORS = frozenset(
    {"code", "config", "configuration", "file", "function", "module", "project", "repo", "repository", "source"}
)
# A filename token such as ``report.pdf`` names a file object; it expands to
# ``file`` plus its extension so "read report.pdf" still reaches read_pdf once
# generic verbs alone no longer establish relevance. Web domains are not files.
_FILENAME_TOKEN_RE = re.compile(r"^\w[\w.-]*\.([a-z][a-z0-9]{0,5})$")
_NON_FILE_EXTENSIONS = frozenset({"ai", "com", "dev", "edu", "gov", "io", "net", "org"})


def _normalize_class(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")


def declared_tool_classes(prompt: str) -> tuple[str, ...] | None:
    """Return normalized explicit tool classes, or ``None`` when undeclared."""

    match = _DECLARATION_RE.search(prompt or "")
    if match is None:
        return None
    values = tuple(normalized for raw in match.group(1).split(",") if (normalized := _normalize_class(raw)))
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


def _specialized_intent_allowed(name: str, query_terms: set[str]) -> bool:
    if name and name.casefold() in query_terms:
        return True
    for prefix, required, supporting in _SPECIALIZED_INTENT_GATES:
        if not name.startswith(prefix):
            continue
        return required <= query_terms and bool(supporting & query_terms)
    return True


def _expanded_query_terms(prompt: str) -> tuple[str, ...]:
    # Retrieval tokenizers may preserve trailing punctuation. Normalize here so
    # intent gates and exact-name boosts see ``account`` rather than ``account.``.
    raw_terms = [term for term in lexical_tokens(prompt) if _normalize_class(term)]
    source_terms = tuple(_normalize_class(term) for term in raw_terms)
    source_term_set = set(source_terms)
    has_generic_verb = bool(_GENERIC_QUERY_TERMS & source_term_set)
    terms: list[str] = []
    seen: set[str] = set()
    for raw, term in zip(raw_terms, source_terms):
        if term not in _QUERY_STOPWORDS and term not in seen:
            terms.append(term)
            seen.add(term)
        expansions = _QUERY_EXPANSIONS.get(term, ())
        if term == "update" and not (_CODE_UPDATE_ANCHORS & source_term_set):
            expansions = ()
        filename = _FILENAME_TOKEN_RE.match(raw.rstrip("."))
        if filename and filename.group(1) not in _NON_FILE_EXTENSIONS:
            expansions = (*expansions, "file", filename.group(1))
        elif has_generic_verb and term in _CODE_UPDATE_ANCHORS:
            # "read the config" names a file object, not a capability domain.
            expansions = (*expansions, "file")
        for expansion in expansions:
            if expansion not in seen:
                terms.append(expansion)
                seen.add(expansion)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return tuple(terms[:MAX_QUERY_TERMS])


def _document_term_sequence(text: str) -> list[str]:
    terms: list[str] = []
    for term in lexical_tokens(text):
        normalized = _normalize_class(term)
        if normalized:
            # ``gmail-list`` and ``read_file`` also answer to their parts.
            terms.append(normalized)
            parts = [part for part in normalized.split("_") if len(part) > 1]
            if len(parts) > 1 or (parts and parts[0] != normalized):
                terms.extend(parts)
    return terms


def _bm25_index(documents: Sequence[str]) -> BM25Index:
    # Query terms are punctuation-normalized (``x.com`` -> ``x_com``), so BM25
    # must score documents in the same vocabulary or such terms never match.
    return BM25Index([" ".join(_document_term_sequence(document)) for document in documents])


def document_terms(text: str) -> frozenset[str]:
    """Return the normalized lexical terms of one candidate document."""

    return frozenset(_document_term_sequence(text))


def specific_query_terms(prompt: str) -> frozenset[str]:
    """Return expanded query terms that can establish relevance on their own."""

    return frozenset(term for term in _expanded_query_terms(prompt) if term not in _GENERIC_QUERY_TERMS)


def rank_texts_for_prompt(prompt: str, documents: Sequence[str]) -> list[int]:
    """Return indices of relevant documents in deterministic descending BM25 rank.

    A document is relevant only when it shares a non-generic query term.
    """

    if not documents:
        return []
    query_terms = _expanded_query_terms(prompt)
    specific = {term for term in query_terms if term not in _GENERIC_QUERY_TERMS}
    if not specific:
        return []
    scores = _bm25_index(documents).scores(query_terms)
    relevant = [
        index for index, document in enumerate(documents) if scores[index] > 0.0 and specific & document_terms(document)
    ]
    return stable_top_k(relevant, len(relevant), score=lambda index: scores[index])


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
    scores = _bm25_index(documents).scores(query_terms)
    query_term_set = set(query_terms)
    specific_terms = query_term_set - _GENERIC_QUERY_TERMS

    def name_match(index: int) -> bool:
        name = str(getattr(tools[index], "__name__", "") or "")
        name_terms = set(lexical_tokens(re.sub(r"[^a-zA-Z0-9]+", " ", name)))
        return name.casefold() in query_term_set or bool(name_terms and name_terms <= query_term_set)

    def score(index: int) -> float:
        return scores[index] + (2.0 if name_match(index) else 0.0)

    positive = [
        index
        for index in range(len(tools))
        if score(index) > 0.0
        and (name_match(index) or bool(specific_terms & document_terms(documents[index])))
        and _specialized_intent_allowed(
            str(getattr(tools[index], "__name__", "") or ""),
            query_term_set,
        )
    ]
    ranked_indices = stable_top_k(positive, len(positive), score=score)
    return [tools[index] for index in ranked_indices]


def select_tools_for_prompt(
    prompt: str,
    all_tools: Sequence[Callable[..., Any]],
    *,
    limit: int = DEFAULT_TOOL_LIMIT,
    schema_token_budget: int = DEFAULT_SCHEMA_TOKEN_BUDGET,
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
    effective_schema_budget = max(
        int(schema_token_budget),
        estimate_tool_schema_tokens([tool for tool in tools if getattr(tool, "__name__", "") in selected_names]),
    )
    for tool in rank_tools_for_prompt(prompt, tools):
        if len(selected_names) >= effective_limit:
            break
        name = str(getattr(tool, "__name__", "") or "")
        if name in selected_names:
            continue
        candidate_names = {*selected_names, name}
        candidate_tools = [item for item in tools if getattr(item, "__name__", "") in candidate_names]
        if estimate_tool_schema_tokens(candidate_tools) > effective_schema_budget:
            continue
        selected_names.add(name)
    return [tool for tool in tools if getattr(tool, "__name__", "") in selected_names]


__all__ = [
    "CORE_TOOL_NAMES",
    "DEFAULT_TOOL_LIMIT",
    "DEFAULT_SCHEMA_TOKEN_BUDGET",
    "DEFERRED_TOOL_NAMES",
    "declared_tool_classes",
    "document_terms",
    "rank_texts_for_prompt",
    "rank_tools_for_prompt",
    "select_tools_for_prompt",
    "specific_query_terms",
]
