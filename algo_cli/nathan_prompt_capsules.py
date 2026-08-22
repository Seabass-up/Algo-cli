"""Deterministic, budgeted prompt-capability capsules.

The registry in this module is the sole authority for optional system-prompt
guidance.  It does not grant tool authority: runtime policy, approval, and
provider adapters continue to enforce execution.  Activation uses observable
signals only and every decision has a content-free receipt.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal


PromptPhase = Literal["interactive", "agent", "oneshot"]
AmbiguityBehavior = Literal["include", "exclude", "legacy"]

VALID_PHASES: frozenset[PromptPhase] = frozenset({"interactive", "agent", "oneshot"})
VALID_TRUST_CLASSES = frozenset({"policy", "runtime_guidance", "retrieval_guidance"})
VALID_AMBIGUITY_BEHAVIORS = frozenset({"include", "exclude", "legacy"})
CAPSULE_ID_RE = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")
TOOL_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,95}\Z")
COMMAND_RE = re.compile(r"/[a-z][a-z0-9_-]{1,63}\Z")
DEFAULT_TOTAL_STATIC_BUDGET = 2_400
DEFAULT_CORE_BUDGET = 1_600


@dataclass(frozen=True)
class PromptCapsuleContext:
    """Content and observable activation signals for one prompt assembly."""

    user_message: str
    phase: PromptPhase
    model: str
    provider: str
    session_mode: str
    external_harness_enabled: bool
    verify_mode: bool
    reflex_enabled: bool
    echo_authority: bool
    visible_tools: frozenset[str] = frozenset()
    unresolved_attempt_count: int = 0
    sections: Mapping[str, str] | None = None

    def section(self, capsule_id: str) -> str:
        sections = self.sections or {}
        return str(sections.get(capsule_id, "") or "").strip()


@dataclass(frozen=True)
class Activation:
    active: bool
    reason: str
    ambiguous: bool = False


Activator = Callable[[PromptCapsuleContext], Activation]
Renderer = Callable[[PromptCapsuleContext], str]


@dataclass(frozen=True)
class PromptCapsule:
    capsule_id: str
    version: int
    phases: frozenset[PromptPhase]
    priority: int
    max_tokens: int
    trust_class: str
    ambiguity_behavior: AmbiguityBehavior
    dependencies: tuple[str, ...]
    conflicts: tuple[str, ...]
    related_tools: tuple[str, ...]
    related_commands: tuple[str, ...]
    activate: Activator
    render: Renderer


@dataclass(frozen=True)
class CapsuleDecision:
    capsule_id: str
    active: bool
    reason: str
    ambiguous: bool


@dataclass(frozen=True)
class PromptCapsuleAssembly:
    prompt: str
    receipt: dict[str, Any]
    fallback_reason: str


class PromptCapsuleRegistryError(RuntimeError):
    """Raised when the registry or a required capsule cannot be used safely."""


def _terms(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", (text or "").casefold()))


def _contains_phrase(text: str, phrases: Sequence[str]) -> bool:
    normalized = " ".join((text or "").casefold().split())
    return any(phrase in normalized for phrase in phrases)


def _tool_prefix(context: PromptCapsuleContext, *prefixes: str) -> bool:
    return any(name.startswith(prefixes) for name in context.visible_tools)


def _section_renderer(capsule_id: str) -> Renderer:
    return lambda context: context.section(capsule_id)


def _slash_activation(context: PromptCapsuleContext) -> Activation:
    message = context.user_message.casefold()
    terms = _terms(message)
    active = (
        bool(re.search(r"(?:^|\s)/(?:context|agent|help|actions|mode|reason)\b", message))
        or bool({"slash", "commands"} <= terms)
        or _contains_phrase(message, ("how do i use /", "command help", "available commands"))
    )
    return Activation(active, "slash_intent" if active else "no_slash_intent")


def _pdf_activation(context: PromptCapsuleContext) -> Activation:
    message = context.user_message.casefold()
    active = (
        ".pdf" in message
        or bool({"pdf", "ocr"} & _terms(message))
        or _contains_phrase(message, ("scanned document", "render document pages"))
        or bool({"read_pdf", "render_pdf_pages"} & context.visible_tools)
    )
    return Activation(active, "pdf_artifact_or_tool" if active else "no_pdf_signal")


def _grok_activation(context: PromptCapsuleContext) -> Activation:
    terms = _terms(context.user_message)
    active = "xai" in terms or "grok" in terms or context.provider == "xai" or _tool_prefix(context, "x_account_")
    return Activation(active, "xai_provider_or_intent" if active else "no_xai_signal")


def _harness_activation(context: PromptCapsuleContext) -> Activation:
    message = context.user_message
    terms = _terms(message)
    active = (
        "wiki" in terms
        or (
            "harness" in terms
            and bool(
                {
                    "search",
                    "index",
                    "retrieval",
                    "external",
                    "compare",
                    "comparison",
                    "openclaw",
                    "hermes",
                    "codex",
                    "claude",
                }
                & terms
            )
        )
        or _contains_phrase(
            message,
            ("external agent store", "harness search", "harness index"),
        )
        or _tool_prefix(context, "harness_")
    )
    return Activation(active, "harness_intent_or_tool" if active else "no_harness_signal")


def _graph_activation(context: PromptCapsuleContext) -> Activation:
    message = context.user_message
    active = _contains_phrase(
        message,
        ("knowledge graph", "graph rag", "index compute lab", "index-compute-lab"),
    ) or bool(
        {"query_knowledge_graph", "reindex_knowledge_graph", "write_knowledge_graph_note"} & context.visible_tools
    )
    return Activation(active, "graph_intent_or_tool" if active else "no_graph_signal")


def _capability_activation(context: PromptCapsuleContext) -> Activation:
    message = context.user_message
    active = (
        _contains_phrase(
            message,
            (
                "what can you do",
                "available actions",
                "available tools",
                "what tools",
                "capability awareness",
            ),
        )
        or "available_actions" in context.visible_tools
        or "/actions" in message.casefold()
    )
    return Activation(active, "capability_intent_or_tool" if active else "no_capability_signal")


def _memory_activation(context: PromptCapsuleContext) -> Activation:
    terms = _terms(context.user_message)
    active = (
        bool({"memory", "memories", "remember", "forget", "lesson", "echo"} & terms)
        or _tool_prefix(
            context,
            "echo_veil_",
        )
        or bool(
            {"remember", "append_lesson", "write_knowledge_graph_note", "update_user_profile"} & context.visible_tools
        )
    )
    return Activation(active, "memory_intent_or_tool" if active else "no_memory_signal")


def _agent_activation(context: PromptCapsuleContext) -> Activation:
    message = context.user_message
    terms = _terms(message)
    active = (
        context.phase == "agent"
        or "/agent" in message.casefold()
        or bool({"pipeline", "delegate", "delegation", "subagent"} & terms)
        or ("agent" in terms and bool({"runtime", "pipeline", "delegate", "parallel", "resume", "fork"} & terms))
        or _contains_phrase(message, ("resume thread", "fork thread", "agent runtime", "run an agent"))
    )
    return Activation(active, "agent_phase_or_intent" if active else "no_agent_signal")


def _verification_activation(context: PromptCapsuleContext) -> Activation:
    terms = _terms(context.user_message)
    active = context.verify_mode or bool({"verify", "verification", "audit", "evidence"} & terms)
    return Activation(active, "verification_mode_or_intent" if active else "no_verification_signal")


def _publishing_activation(context: PromptCapsuleContext) -> Activation:
    terms = _terms(context.user_message)
    active = context.session_mode == "publish" or bool(
        {"publish", "release", "deploy", "tag", "invoice", "payment", "send", "post"} & terms
    )
    return Activation(active, "publish_mode_or_intent" if active else "no_publish_signal")


def _reflection_activation(context: PromptCapsuleContext) -> Activation:
    terms = _terms(context.user_message)
    active = (
        context.reflex_enabled
        or context.unresolved_attempt_count > 0
        or bool({"retry", "recover", "recovery", "debug"} & terms)
    )
    return Activation(active, "reflex_or_unresolved_attempt" if active else "no_recovery_signal")


CAPSULES: tuple[PromptCapsule, ...] = (
    PromptCapsule(
        "slash_commands",
        1,
        frozenset({"interactive", "agent"}),
        10,
        900,
        "runtime_guidance",
        "exclude",
        (),
        (),
        ("session_command", "session_slash"),
        ("/help", "/context", "/agent"),
        _slash_activation,
        _section_renderer("slash_commands"),
    ),
    PromptCapsule(
        "pdf_handling",
        1,
        frozenset(VALID_PHASES),
        20,
        260,
        "runtime_guidance",
        "include",
        (),
        (),
        ("read_pdf", "render_pdf_pages", "vision_describe"),
        ("/pdf",),
        _pdf_activation,
        _section_renderer("pdf_handling"),
    ),
    PromptCapsule(
        "grok_xai",
        1,
        frozenset(VALID_PHASES),
        30,
        360,
        "runtime_guidance",
        "include",
        (),
        (),
        ("x_search", "x_account_status"),
        ("/model-check", "/x-account"),
        _grok_activation,
        _section_renderer("grok_xai"),
    ),
    PromptCapsule(
        "harness_search",
        1,
        frozenset(VALID_PHASES),
        40,
        280,
        "retrieval_guidance",
        "exclude",
        (),
        (),
        ("harness_search", "harness_read", "harness_stats"),
        ("/harness", "/hsearch", "/hread"),
        _harness_activation,
        _section_renderer("harness_search"),
    ),
    PromptCapsule(
        "knowledge_graph",
        1,
        frozenset(VALID_PHASES),
        50,
        300,
        "retrieval_guidance",
        "exclude",
        ("harness_search",),
        (),
        ("query_knowledge_graph", "reindex_knowledge_graph", "write_knowledge_graph_note"),
        ("/icl", "/intel"),
        _graph_activation,
        _section_renderer("knowledge_graph"),
    ),
    PromptCapsule(
        "capability_discovery",
        1,
        frozenset(VALID_PHASES),
        60,
        160,
        "runtime_guidance",
        "exclude",
        (),
        (),
        ("available_actions", "action_search"),
        ("/actions",),
        _capability_activation,
        _section_renderer("capability_discovery"),
    ),
    PromptCapsule(
        "memory_administration",
        1,
        frozenset(VALID_PHASES),
        70,
        360,
        "policy",
        "include",
        (),
        (),
        ("remember", "echo_veil_remember", "echo_veil_recall", "echo_veil_context"),
        ("/memory", "/remember", "/forget"),
        _memory_activation,
        _section_renderer("memory_administration"),
    ),
    PromptCapsule(
        "agent_runtime",
        1,
        frozenset({"interactive", "agent"}),
        80,
        320,
        "runtime_guidance",
        "include",
        (),
        (),
        ("session_command", "git_status", "git_diff"),
        ("/agent", "/route"),
        _agent_activation,
        _section_renderer("agent_runtime"),
    ),
    PromptCapsule(
        "verification",
        1,
        frozenset(VALID_PHASES),
        90,
        220,
        "policy",
        "include",
        (),
        (),
        ("git_diff", "run_shell"),
        ("/verify",),
        _verification_activation,
        _section_renderer("verification"),
    ),
    PromptCapsule(
        "publishing_release",
        1,
        frozenset(VALID_PHASES),
        100,
        300,
        "policy",
        "include",
        ("verification",),
        (),
        ("git_status", "git_diff"),
        ("/ship",),
        _publishing_activation,
        _section_renderer("publishing_release"),
    ),
    PromptCapsule(
        "reflection_recovery",
        1,
        frozenset(VALID_PHASES),
        110,
        240,
        "runtime_guidance",
        "include",
        (),
        (),
        ("action_search",),
        ("/reflex",),
        _reflection_activation,
        _section_renderer("reflection_recovery"),
    ),
)


def validate_registry(registry: Sequence[PromptCapsule] = CAPSULES) -> None:
    ids = [capsule.capsule_id for capsule in registry]
    if len(ids) != len(set(ids)) or any(CAPSULE_ID_RE.fullmatch(value) is None for value in ids):
        raise PromptCapsuleRegistryError("prompt_capsule_registry_identity")
    known = frozenset(ids)
    graph: dict[str, tuple[str, ...]] = {}
    for capsule in registry:
        if (
            type(capsule.version) is not int
            or capsule.version < 1
            or not capsule.phases
            or not capsule.phases <= VALID_PHASES
            or type(capsule.priority) is not int
            or not 0 <= capsule.priority <= 10_000
            or type(capsule.max_tokens) is not int
            or not 32 <= capsule.max_tokens <= 2_000
            or capsule.trust_class not in VALID_TRUST_CLASSES
            or capsule.ambiguity_behavior not in VALID_AMBIGUITY_BEHAVIORS
            or not set(capsule.dependencies) <= known
            or not set(capsule.conflicts) <= known
            or capsule.capsule_id in capsule.dependencies
            or capsule.capsule_id in capsule.conflicts
            or set(capsule.dependencies) & set(capsule.conflicts)
            or any(TOOL_NAME_RE.fullmatch(value) is None for value in capsule.related_tools)
            or any(COMMAND_RE.fullmatch(value) is None for value in capsule.related_commands)
            or len(capsule.related_tools) != len(set(capsule.related_tools))
            or len(capsule.related_commands) != len(set(capsule.related_commands))
            or not callable(capsule.activate)
            or not callable(capsule.render)
        ):
            raise PromptCapsuleRegistryError(f"prompt_capsule_registry_schema:{capsule.capsule_id}")
        graph[capsule.capsule_id] = capsule.dependencies

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(capsule_id: str) -> None:
        if capsule_id in visiting:
            raise PromptCapsuleRegistryError("prompt_capsule_registry_cycle")
        if capsule_id in visited:
            return
        visiting.add(capsule_id)
        for dependency in graph[capsule_id]:
            visit(dependency)
        visiting.remove(capsule_id)
        visited.add(capsule_id)

    for capsule_id in ids:
        visit(capsule_id)


def registry_digest(registry: Sequence[PromptCapsule] = CAPSULES) -> str:
    validate_registry(registry)
    projection = [
        {
            "id": capsule.capsule_id,
            "version": capsule.version,
            "phases": sorted(capsule.phases),
            "priority": capsule.priority,
            "max_tokens": capsule.max_tokens,
            "trust_class": capsule.trust_class,
            "ambiguity_behavior": capsule.ambiguity_behavior,
            "dependencies": list(capsule.dependencies),
            "conflicts": list(capsule.conflicts),
            "related_tools": list(capsule.related_tools),
            "related_commands": list(capsule.related_commands),
        }
        for capsule in sorted(registry, key=lambda item: (item.priority, item.capsule_id))
    ]
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def resolve_capsules(
    context: PromptCapsuleContext,
    registry: Sequence[PromptCapsule] = CAPSULES,
) -> tuple[CapsuleDecision, ...]:
    validate_registry(registry)
    by_id = {capsule.capsule_id: capsule for capsule in registry}
    decisions: dict[str, CapsuleDecision] = {}
    active_ids: set[str] = set()
    for capsule in sorted(registry, key=lambda item: (item.priority, item.capsule_id)):
        if context.phase not in capsule.phases:
            decisions[capsule.capsule_id] = CapsuleDecision(
                capsule.capsule_id,
                False,
                "phase_ineligible",
                False,
            )
            continue
        activation = capsule.activate(context)
        if activation.ambiguous:
            if capsule.ambiguity_behavior == "legacy":
                raise PromptCapsuleRegistryError(f"prompt_capsule_ambiguous:{capsule.capsule_id}")
            active = capsule.ambiguity_behavior == "include"
        else:
            active = activation.active
        decisions[capsule.capsule_id] = CapsuleDecision(
            capsule.capsule_id,
            active,
            activation.reason,
            activation.ambiguous,
        )
        if active:
            active_ids.add(capsule.capsule_id)

    pending = list(active_ids)
    while pending:
        capsule_id = pending.pop()
        for dependency in by_id[capsule_id].dependencies:
            if context.phase not in by_id[dependency].phases:
                raise PromptCapsuleRegistryError(f"prompt_capsule_dependency_phase:{capsule_id}")
            if dependency not in active_ids:
                active_ids.add(dependency)
                decisions[dependency] = CapsuleDecision(
                    dependency,
                    True,
                    f"dependency_of:{capsule_id}",
                    False,
                )
                pending.append(dependency)

    for capsule_id in active_ids:
        conflict = active_ids & set(by_id[capsule_id].conflicts)
        if conflict:
            raise PromptCapsuleRegistryError(f"prompt_capsule_conflict:{capsule_id}:{sorted(conflict)[0]}")
    return tuple(
        decisions[capsule.capsule_id] for capsule in sorted(registry, key=lambda item: (item.priority, item.capsule_id))
    )


def related_tools_for_capsules(
    context: PromptCapsuleContext,
    registry: Sequence[PromptCapsule] = CAPSULES,
) -> tuple[str, ...]:
    decisions = resolve_capsules(context, registry)
    active = {decision.capsule_id for decision in decisions if decision.active}
    tools: list[str] = []
    for capsule in sorted(registry, key=lambda item: (item.priority, item.capsule_id)):
        if capsule.capsule_id not in active:
            continue
        tools.extend(capsule.related_tools)
    return tuple(dict.fromkeys(tools))


def assemble_capsules(
    core_prompt: str,
    context: PromptCapsuleContext,
    *,
    estimate_tokens: Callable[[str], int],
    registry: Sequence[PromptCapsule] = CAPSULES,
    core_budget: int = DEFAULT_CORE_BUDGET,
    total_static_budget: int = DEFAULT_TOTAL_STATIC_BUDGET,
) -> PromptCapsuleAssembly:
    """Append active capsules without truncating any capsule contract."""

    try:
        decisions = resolve_capsules(context, registry)
        digest = registry_digest(registry)
    except PromptCapsuleRegistryError as exc:
        return PromptCapsuleAssembly("", {}, str(exc))

    core_tokens = estimate_tokens(core_prompt)
    if core_tokens > core_budget:
        return PromptCapsuleAssembly("", {}, "prompt_capsule_core_budget")
    by_id = {capsule.capsule_id: capsule for capsule in registry}
    rendered_parts = [core_prompt.rstrip()]
    included: list[dict[str, Any]] = []
    omitted: list[dict[str, str]] = []
    used_tokens = core_tokens
    for decision in decisions:
        if not decision.active:
            continue
        capsule = by_id[decision.capsule_id]
        body = capsule.render(context).strip()
        if not body:
            if capsule.ambiguity_behavior in {"include", "legacy"}:
                return PromptCapsuleAssembly(
                    "",
                    {},
                    f"prompt_capsule_required_empty:{capsule.capsule_id}",
                )
            omitted.append({"id": capsule.capsule_id, "reason": "empty_optional_section"})
            continue
        tokens = estimate_tokens("\n\n" + body)
        if tokens > capsule.max_tokens:
            if capsule.ambiguity_behavior in {"include", "legacy"}:
                return PromptCapsuleAssembly(
                    "",
                    {},
                    f"prompt_capsule_section_budget:{capsule.capsule_id}",
                )
            omitted.append({"id": capsule.capsule_id, "reason": "section_budget"})
            continue
        if used_tokens + tokens > total_static_budget:
            if capsule.ambiguity_behavior in {"include", "legacy"}:
                return PromptCapsuleAssembly(
                    "",
                    {},
                    f"prompt_capsule_total_budget:{capsule.capsule_id}",
                )
            omitted.append({"id": capsule.capsule_id, "reason": "total_static_budget"})
            continue
        rendered_parts.append(body)
        used_tokens += tokens
        included.append(
            {
                "id": capsule.capsule_id,
                "reason": decision.reason,
                "tokens": tokens,
                "version": capsule.version,
            }
        )
    receipt = {
        "schema": "nathan-prompt-capsule-receipt-v1",
        "registry_digest": digest,
        "phase": context.phase,
        "core_tokens": core_tokens,
        "static_tokens": used_tokens,
        "included": included,
        "omitted": omitted,
        "inactive": [
            {"id": decision.capsule_id, "reason": decision.reason} for decision in decisions if not decision.active
        ],
    }
    return PromptCapsuleAssembly("\n\n".join(rendered_parts), receipt, "")


validate_registry()


__all__ = [
    "CAPSULES",
    "DEFAULT_CORE_BUDGET",
    "DEFAULT_TOTAL_STATIC_BUDGET",
    "Activation",
    "CapsuleDecision",
    "PromptCapsule",
    "PromptCapsuleAssembly",
    "PromptCapsuleContext",
    "PromptCapsuleRegistryError",
    "assemble_capsules",
    "registry_digest",
    "related_tools_for_capsules",
    "resolve_capsules",
    "validate_registry",
]
