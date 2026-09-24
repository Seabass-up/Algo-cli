"""Tool execution, scoped authority, attempt ledger, and reflection checkpoints."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from ollama import Client

from .config import Config
from . import execution_guardrails
from . import reflex
from . import tools as tools_module
from .chat_protocol import get_attr
from .display import (
    json_sink,
    redact_tool_args,
    show_info,
    show_tool_call,  # noqa: F401 - dispatcher compatibility surface
    show_tool_result,  # noqa: F401 - dispatcher compatibility surface
)
from .arthur_outcomes import ActionOutcome, OutcomeStatus
from .dorothy_perf_telemetry import record_perf_event
from .theodore_runtime_qos import RuntimeHint, classify_tool_runtime
from .tools import TOOL_MAP
from .marcus_authority import (
    Capability,
    CapabilityMask,
    ConfirmationMode,
    ConfirmationReceipt,
    ConsentGrant,
    EffectClass,
    ResolvedAction,
    TargetScope,
)
from .irene_privacy_views import (
    PrivacyProjectionError,
    keyed_action_fingerprint,
)
from .samuel_policy import RuntimeToolPolicyDecision, evaluate_runtime_tool_policy
from .samuel_policy_engine import (
    PolicyDisposition,
    resolve_action,
    session_command_requires_approval,  # noqa: F401 - compatibility re-export
)

ATTEMPT_LEDGER_LIMIT = 48
REFLECTION_RECENT_MESSAGES = 8
TOOL_RESULT_CONTENT_LIMIT = 20_000
MAX_COMPLETION_RECOVERY_ROUNDS = 4
FAILED_ATTEMPT_SKIP_SECONDS = 120.0
_SHELL_EXIT_CODE_RE = re.compile(r"\[exit code:\s*(-?\d+)\]", re.IGNORECASE)
# Built-in tools report failures as "Error <verb>ing <target>: <reason>" as well as "Error: ...".
# File tools always name the resolved absolute path, which keeps raw read_file content such as
# "Error reading sensor 3: timeout" from looking like a tool failure.
_TOOL_ERROR_PREFIX_RE = re.compile(
    r"error (?:(?:reading|writing|listing) (?:/|[a-z]:[\\/]|\\\\)"
    r"|(?:searching|running|fetching|extracting|rendering|generating|"
    r"executing|pulling|deleting|creating|copying|showing)\b)[^\n]{0,400}?: "
)
# search_files keeps rg matches when rg also reports errors and appends this note as the final
# line (or just before the truncation marker); the search is incomplete, not a success.
_PARTIAL_SEARCH_NOTE_RE = re.compile(
    r"\n\[partial: search reported \d+\+? error\(s\); first: [^\n]*\]"
    r"(?:\n\.\.\.\[truncated: [^\n]*\])?\Z"
)
_READ_FILE_NOT_FOUND_RETRY = "\nRetry read_file with the intended exact path."
_BASELINE_CAPABILITIES = CapabilityMask(Capability.READ.value | Capability.MODEL.value | Capability.MEMORY.value)
_BASELINE_ACTIONS = frozenset(
    {
        "action_search",
        "available_actions",
        "capability_mask_describe",
        "extensions_manifest_build",
        "find_unique_anchor",
        "git_diff",
        "git_status",
        "harness_competitive_rating",
        "harness_read",
        "harness_scorecard",
        "harness_search",
        "harness_stats",
        "jev_kernel_status",
        "list_directory",
        "model_show",
        "plugins_discover",
        "query_knowledge_graph",
        "read_file",
        "read_pdf",
        "render_pdf_pages",
        "runtime_qos_hint",
        "search_files",
        "session_command",
        "session_slash",
        "small_context_ledger_preview",
        "url_scheme_parse",
        "version_manifest_build",
    }
)
_BASELINE_TARGET_SCOPES = frozenset(
    {
        TargetScope.WORKSPACE,
        TargetScope.RUNTIME,
        TargetScope.MEMORY_STORE,
        TargetScope.MODEL_STORE,
        TargetScope.PLUGIN,
    }
)


@dataclass(frozen=True)
class PipelineToolResult:
    """Typed pipeline result with two-item unpacking compatibility."""

    message: dict[str, Any]
    result: str
    outcome: ActionOutcome

    def __iter__(self) -> Iterator[Any]:
        yield self.message
        yield self.result


def show_typed_tool_result(
    name: str,
    result: str,
    *,
    outcome_status: OutcomeStatus,
    duration_ms: float | None = None,
    call_id: str | None = None,
) -> None:
    """Render an outcome without deriving authority state from untrusted text."""

    sink = json_sink()
    if sink is not None:
        sink.tool_result(
            call_id=call_id,
            name=name,
            result=result,
            duration_ms=duration_ms,
            outcome_status=outcome_status.value,
        )
        return
    show_tool_result(
        name,
        result,
        approved=outcome_status is OutcomeStatus.SUCCEEDED,
        duration_ms=duration_ms,
        call_id=call_id,
    )


_BASELINE_GRANT_SECONDS = 30.0
_INTERACTIVE_GRANT_SECONDS = 8 * 60 * 60.0
_SESSION_GRANT_ACTIONS = 256
_ATTEMPT_LEDGER_LOCK = threading.RLock()
_AUTHORITY_SESSION_LOCK = threading.RLock()
_POLICY_CEILING_REASONS = {
    "agent_tool_not_allowed": "Tool not allowed by the active agent block policy",
    "agent_batch_quarantined": ("Skipped because another tool call in this assistant message violated block policy"),
    "batch_unclassified_action": "Batch contains an unclassified action",
    "batch_duplicate_call_id": "Batch contains a duplicate tool-call ID",
    "batch_missing_idempotency_id": "Batch external mutation has no stable idempotency ID",
    "batch_quarantined": "Batch was quarantined because another call failed protocol preflight",
    "required_change_shell_blocked": "Blocked by required-change policy in safe mode",
    "dispatch_cancelled": "Action was cancelled before dispatch",
    "dispatch_deadline_elapsed": "Action deadline elapsed before dispatch",
    "dispatch_invalid_deadline": "Action deadline was invalid",
    "dispatch_clock_error": "Action dispatch clock was invalid",
    "run_contract_tool_budget": "Run contract tool-call budget is exhausted",
    "run_journal_unavailable": "Durable Agent run checkpoint is unavailable",
}
_OPAQUE_JSON_RESULT_TOOLS = frozenset({"harness_read", "read_file", "read_pdf", "render_pdf_pages", "web_fetch"})
_STRUCTURED_ERROR_STATUSES = frozenset(
    {"cancelled", "canceled", "denied", "error", "failed", "failure", "timed_out", "timeout"}
)


DenialKind = Literal["intentional_policy", "setup_gap", "user_declined", "approval_unavailable"]
_APPROVAL_DENIAL_PREFIXES: dict[str, DenialKind] = {
    "This operation was not approved": "user_declined",
    "Approval is unavailable in this noninteractive run": "approval_unavailable",
}


@dataclass(frozen=True)
class DenialExplanation:
    """Typed reason a call was denied and whether this session can clear it."""

    action: str
    target: str
    missing_scope: str
    kind: DenialKind
    resolvable_in_session: bool
    recovery: str

    def text(self) -> str:
        nature = {
            "intentional_policy": "This is intentional containment policy, not a setup error",
            "setup_gap": "This is a runtime configuration gap, not a user decision",
            "user_declined": "The user declined this exact action",
            "approval_unavailable": "This run cannot ask for approval",
        }[self.kind]
        resolvable = (
            "yes, by the user (the model cannot grant it)" if self.resolvable_in_session else "no"
        )
        return (
            f"{self.action} on {self.target} was denied: {self.missing_scope}. {nature}. "
            f"Resolvable in this session: {resolvable}. Recovery: {self.recovery}"
        )


@dataclass(frozen=True)
class RuntimeAuthorization:
    """Consumed authority for exactly one impending execution."""

    allowed: bool
    reason: str
    action: ResolvedAction
    grant_id: str = ""
    confirmation_receipt_id: str = ""


class RuntimeAuthoritySession:
    """Thread-safe, process-local store for revocable scoped grants."""

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self._grants: dict[str, ConsentGrant] = {}
        self._remaining: dict[str, int] = {}
        self._lock = threading.RLock()

    def _workspace_target_allowed(self, target: str) -> bool:
        if not target.startswith("workspace:") or target == "workspace:unresolved":
            return False
        try:
            Path(target.removeprefix("workspace:")).resolve().relative_to(self.workspace_root)
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def baseline_allows(self, action: ResolvedAction) -> bool:
        """Apply a launch-time ceiling independent from the requested policy mask."""

        if action.name not in _BASELINE_ACTIONS:
            return False
        if action.effect_class is not EffectClass.OBSERVE:
            return False
        if action.confirmation_mode is not ConfirmationMode.NONE:
            return False
        if action.target_scope not in _BASELINE_TARGET_SCOPES:
            return False
        if not _BASELINE_CAPABILITIES.contains(CapabilityMask(action.capability_mask)):
            return False
        if action.target_scope is TargetScope.WORKSPACE:
            return self._workspace_target_allowed(action.target)
        return not action.target.endswith(":unresolved")

    def issue(
        self,
        action: ResolvedAction,
        *,
        source: str,
        now: float,
        maximum_action_count: int = 1,
        ttl_seconds: float = _INTERACTIVE_GRANT_SECONDS,
    ) -> ConsentGrant:
        if maximum_action_count <= 0 or ttl_seconds <= 0:
            raise ValueError("runtime grants require a positive count and lifetime")
        grant = ConsentGrant(
            grant_id=f"grant-{uuid.uuid4().hex}",
            capability_mask=action.capability_mask,
            allowed_actions=frozenset({action.name}),
            allowed_targets=frozenset({action.target}),
            expires_at=now + ttl_seconds,
            maximum_action_count=maximum_action_count,
            issued_at=now,
            source=source,
        )
        with self._lock:
            self._grants[grant.grant_id] = grant
            self._remaining[grant.grant_id] = maximum_action_count
        return grant

    def matching_grant(self, action: ResolvedAction, now: float) -> ConsentGrant | None:
        required = CapabilityMask(action.capability_mask)
        with self._lock:
            for grant_id, grant in reversed(tuple(self._grants.items())):
                remaining = self._remaining.get(grant_id, 0)
                if remaining <= 0 or not grant.permits(action.name, required, action.target, now):
                    continue
                return replace(grant, maximum_action_count=remaining)
        return None

    def grant_by_id(self, grant_id: str, now: float) -> ConsentGrant | None:
        with self._lock:
            grant = self._grants.get(grant_id)
            remaining = self._remaining.get(grant_id, 0)
            if grant is None or remaining <= 0 or now >= grant.expires_at:
                return None
            return replace(grant, maximum_action_count=remaining)

    def consume(self, grant_id: str, now: float) -> bool:
        """Atomically consume one use before the external call begins."""

        with self._lock:
            grant = self._grants.get(grant_id)
            remaining = self._remaining.get(grant_id, 0)
            if grant is None or remaining <= 0 or now >= grant.expires_at:
                return False
            self._remaining[grant_id] = remaining - 1
            return True

    def revoke_all(self) -> None:
        with self._lock:
            self._grants.clear()
            self._remaining.clear()


def authority_session_for(cfg: Config) -> RuntimeAuthoritySession:
    """Return a transient authority session, invalidating it when cwd changes."""

    root = str(Path(cfg.cwd).expanduser().resolve())
    with _AUTHORITY_SESSION_LOCK:
        current = getattr(cfg, "_nathan_authority_session", None)
        if not isinstance(current, RuntimeAuthoritySession) or str(current.workspace_root) != root:
            current = RuntimeAuthoritySession(root)
            setattr(cfg, "_nathan_authority_session", current)
        return current


def approval_mode_for_config(
    cfg: Config,
) -> Literal["interactive", "never", "auto"]:
    """Return the closed approval mode without changing legacy semantics."""

    value = str(getattr(cfg, "_nathan_approval_mode", "interactive")).casefold()
    if value == "interactive":
        return "interactive"
    if value == "auto":
        return "auto"
    return "never"


def _approval_mode(cfg: Config) -> str:
    """Compatibility alias for older internal callers."""

    return approval_mode_for_config(cfg)


def explain_missing_grant(cfg: Config, action: ResolvedAction) -> DenialExplanation:
    """Name the launch-time baseline condition a no-confirmation action failed."""

    from .session_mode import active_mode

    no_retry = "Do not retry the call; use a permitted alternative or report the blocker."
    setup_recovery = f"Not resolvable in this session; the runtime baseline must be updated in a release. {no_retry}"

    def setup_gap(missing: str) -> DenialExplanation:
        return DenialExplanation(action.name, action.target, missing, "setup_gap", False, setup_recovery)

    if action.name not in _BASELINE_ACTIONS:
        return setup_gap(
            "this no-confirmation action is not in the runtime baseline allowlist, and actions "
            "without a confirmation step can never be approved by prompt"
        )
    if action.effect_class is not EffectClass.OBSERVE:
        return setup_gap(f"baseline actions must be observations, not {action.effect_class.value}")
    if action.target_scope not in _BASELINE_TARGET_SCOPES:
        return setup_gap(f"target scope {action.target_scope.value} is outside the runtime baseline scopes")
    if not _BASELINE_CAPABILITIES.contains(CapabilityMask(action.capability_mask)):
        return setup_gap("the requested capabilities exceed the runtime baseline read/model/memory ceiling")
    session = authority_session_for(cfg)
    if action.target_scope is TargetScope.WORKSPACE and not session._workspace_target_allowed(action.target):
        yolo = active_mode(cfg) == "yolo"
        return DenialExplanation(
            action.name,
            action.target,
            f"the read-only target is outside the session workspace {session.workspace_root}",
            "intentional_policy",
            not yolo,
            (
                "The user can run /cd to that directory, start Algo CLI from the target workspace, "
                "or enter /mode yolo, which allows ordinary reads outside the working directory. "
                f"{no_retry}"
                if not yolo
                else f"YOLO does not admit this target. {no_retry}"
            ),
        )
    return setup_gap("no launch-time baseline grant was available for this target")


def begin_tool_turn(cfg: Config) -> None:
    """Start a per-turn record of denied calls so identical repeats are not re-asked."""

    with _ATTEMPT_LEDGER_LOCK:
        setattr(cfg, "_nathan_turn_denials", {})


def end_tool_turn(cfg: Config) -> None:
    with _ATTEMPT_LEDGER_LOCK:
        if hasattr(cfg, "_nathan_turn_denials"):
            delattr(cfg, "_nathan_turn_denials")


def _turn_denials(cfg: Config) -> dict[str, tuple[str, DenialExplanation | None]] | None:
    denials = getattr(cfg, "_nathan_turn_denials", None)
    return denials if isinstance(denials, dict) else None


def _register_turn_denial(
    cfg: Config, signature: str, reason: str, explanation: DenialExplanation | None
) -> tuple[str, DenialExplanation | None] | None:
    """Record a denial for this turn and return any earlier identical denial."""

    with _ATTEMPT_LEDGER_LOCK:
        denials = _turn_denials(cfg)
        if denials is None:
            return None
        prior = denials.get(signature)
        if prior is None:
            denials[signature] = (summarize_tool_result(reason, 300), explanation)
        return prior


def turn_denial(cfg: Config, signature: str) -> DenialExplanation | None:
    """Return the typed explanation for a call already denied in this turn."""

    with _ATTEMPT_LEDGER_LOCK:
        denials = _turn_denials(cfg)
        entry = denials.get(signature) if denials is not None else None
    return entry[1] if entry is not None else None


def _repeated_denial_text(reason: str) -> str:
    return (
        f"Skipped repeated denied action (blocked earlier in this turn: {reason}). "
        "It cannot be resolved in this session by retrying. Do not retry; "
        "use a permitted alternative or report the blocker."
    )


def _prepared_grant(
    cfg: Config,
    action: ResolvedAction,
    *,
    now: float,
) -> ConsentGrant | None:
    from .session_mode import active_mode

    yolo = active_mode(cfg) == "yolo"
    session = authority_session_for(cfg)
    if session.baseline_allows(action):
        # Each in-flight observation owns one use; overlapping reads must not share it.
        return session.issue(
            action,
            source="runtime-baseline",
            now=now,
            ttl_seconds=_BASELINE_GRANT_SECONDS,
        )
    if (
        yolo
        and action.name in _BASELINE_ACTIONS
        and action.effect_class is EffectClass.OBSERVE
        and action.confirmation_mode is ConfirmationMode.NONE
        and action.target_scope is TargetScope.WORKSPACE
        and _BASELINE_CAPABILITIES.contains(CapabilityMask(action.capability_mask))
        and action.target.startswith("workspace:")
        and action.target != "workspace:unresolved"
    ):
        # Owner activation permits ordinary observations beyond cwd, not writes.
        return session.issue(
            action,
            source="user-yolo-preapproval",
            now=now,
            maximum_action_count=1,
            ttl_seconds=120.0,
        )
    if yolo and action.confirmation_mode in {
        ConfirmationMode.SESSION_PREAPPROVAL,
        ConfirmationMode.ACTION_TIME,
    }:
        if action.target_scope is not TargetScope.WORKSPACE or session._workspace_target_allowed(action.target):
            # Activation is explicit user consent; each preflight owns one use.
            return session.issue(
                action,
                source="user-yolo-preapproval",
                now=now,
                maximum_action_count=1,
                ttl_seconds=120.0,
            )
    grant = session.matching_grant(action, now)
    if grant is not None and (grant.source != "user-yolo-preapproval" or yolo):
        return grant
    auto_preapproved = _approval_mode(cfg) == "auto" or bool(cfg.auto_approve_active)
    if action.confirmation_mode is ConfirmationMode.SESSION_PREAPPROVAL and auto_preapproved:
        return session.issue(
            action,
            source="user-yolo-preapproval" if yolo else "trusted-auto-preapproval",
            now=now,
            maximum_action_count=1 if yolo else _SESSION_GRANT_ACTIONS,
        )
    return None


@dataclass(frozen=True)
class RuntimeToolPreflight:
    """Shared policy/QoS decision for a model-invoked tool call."""

    signature_args: dict[str, Any]
    runtime_hint: RuntimeHint
    policy: RuntimeToolPolicyDecision
    guardrail_allowed: bool = True
    guardrail_reasons: tuple[str, ...] = ()
    queue_position: int | None = None
    denial: DenialExplanation | None = None
    repeated_denial: bool = False

    @property
    def allowed(self) -> bool:
        """Return whether the action may proceed to the authority step."""

        return self.policy.eligible and self.guardrail_allowed

    @property
    def qos_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "spawn_class": self.runtime_hint.spawn_class.value,
            "estimated_cost": self.runtime_hint.estimated_cost,
            "log_path": self.runtime_hint.log_path,
            "log_suppression": self.runtime_hint.log_suppression,
        }
        if self.queue_position is not None:
            fields["queue_position"] = self.queue_position
        return fields

    @property
    def blocked_result(self) -> str:
        if (
            self.policy.eligible
            and len(self.guardrail_reasons) == 1
            and self.guardrail_reasons[0].startswith("ProgramValidationError:")
        ):
            return f"Invalid action program: {self.guardrail_reasons[0]}."
        if self.policy.eligible:
            # An eligible confirmation would have been resolved at the authority
            # step; only the guardrails actually blocked this call, so the block
            # message must not blame pending confirmation or auto approval.
            reasons = list(self.guardrail_reasons) or list(self.policy.reasons)
        else:
            reasons = [*self.policy.reasons, *self.guardrail_reasons]
        reason = "; ".join(reasons) or "runtime authority rejected the call"
        if self.repeated_denial:
            return _repeated_denial_text(reason)
        if self.denial is not None:
            reason = reason.rstrip(".")
        return f"Blocked by runtime authority: {reason}."

    @property
    def resolvable_in_session(self) -> bool | None:
        """Typed resolvability of a policy denial; None when no explanation applies."""

        return self.denial.resolvable_in_session if self.denial is not None else None


def tool_runtime_args(name: str, args: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """Return tool args after applying runtime defaults used for execution.

    Only JSON-serializable defaults belong here: the result feeds
    tool_attempt_signature and the persisted attempt ledger. The live Config
    handle for cfg-bound tools is injected by run_tool at execution time.
    """
    call_args = dict(args)
    if name in {
        "read_file",
        "read_pdf",
        "render_pdf_pages",
        "write_file",
        "edit_file",
        "list_directory",
        "search_files",
        "find_unique_anchor",
        "batch_edit",
        "run_shell",
        "git_status",
        "git_diff",
        "vision_describe",
    }:
        typed_pdf_artifact = (
            name == "vision_describe"
            and not call_args.get("image_path")
            and call_args.get("artifact_id") is not None
            and call_args.get("artifact_page") is not None
            and call_args.get("artifact_receipt") is not None
        )
        if not typed_pdf_artifact:
            call_args["cwd"] = cfg.cwd
    if name == "run_shell":
        # safe_mode is session authority, not a model-controlled argument.
        # Always replace untrusted/stale tool-call input with the live setting.
        call_args["safe_mode"] = bool(getattr(cfg, "safe_mode", True))
    if name in {"x_account_post", "x_account_reply", "x_account_post_action"}:
        # Confirmation is runtime authority, never a model-controlled argument.
        call_args.pop("confirm", None)
    return call_args


def _effective_tool_path(args: dict[str, Any]) -> Path | None:
    """Return the exact path candidate implied by a tool's path and cwd args."""

    raw_path = args.get("path")
    if not isinstance(raw_path, (str, os.PathLike)) or not str(raw_path).strip():
        return None
    try:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(str(args.get("cwd") or ".")).expanduser() / candidate
        return candidate
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def completion_recovery_prompt(cfg: Config, *, block_output: bool = False) -> str:
    """Request only permitted verification, without granting new authority."""
    from .continuum_memory import selected

    if selected(cfg):
        verifier = (
            "Continuum Memory is authoritative, so run_shell is unavailable. "
            "Do not seek shell approval, retry it, or route around this restriction. "
            "Use git_diff only if admitted and able to verify the tracked workspace changes; "
            "it cannot verify new untracked files. "
        )
    else:
        verifier = (
            "Use one admitted non-mutating test, lint/type check, or git_diff tool. "
            "Run verification from the active execution workspace root; a check redirected "
            "to another directory cannot verify this workspace. Custom verification must fail "
            "on mismatch: use a healthcheck/check/verify script, or Python -c with assertions. "
        )
    from .session_mode import unlimited_work

    final_label = "final ## Block Output" if block_output else "final answer"
    if unlimited_work(cfg):
        budget = (
            "YOLO does not end verification recovery after a round cap. "
            "Keep calling one admitted verifier until it passes, or report the remaining "
            f"blocker without claiming success. Then give a concise {final_label} grounded in actual evidence."
        )
    else:
        budget = (
            f"At most {MAX_COMPLETION_RECOVERY_ROUNDS} recovery model rounds remain, within the "
            f"existing work budget. Then give a concise {final_label} grounded in actual evidence."
        )
    return (
        "[Internal completion gate] Do not claim completion yet. The last workspace mutation "
        "has no successful post-mutation verifier. "
        f"{verifier}"
        "Successful discovery, transforms, read_file, or source substring checks are not "
        "functional verification. If no permitted verifier is available, report the blocker "
        "and unverified work without claiming success. "
        f"{budget}"
    )


def _mode_tool_error(name: str, args: dict[str, Any], cfg: Config) -> str | None:
    from .samuel_policy_engine import normalize_session_command
    from .session_mode import active_mode

    if name in {"session_command", "session_slash"}:
        if normalize_session_command(str(args.get("command") or "")) == ("/mode", "yolo"):
            return "Only the user may enter YOLO with /mode yolo in the interactive CLI."
    if active_mode(cfg) == "yolo":
        if name in {"session_command", "session_slash"}:
            command, argument = normalize_session_command(str(args.get("command") or ""))
            if command in {"/safe", "/policy"} and argument not in {"status", "show", "?", "help"}:
                return "Only direct user input may change protection settings in YOLO mode."
        action = resolve_action(name, args, cwd=cfg.cwd)
        if action.capability_mask & Capability.CREDENTIAL.value:
            return "Credential operations are unavailable in YOLO mode."
        if action.target_scope is TargetScope.WORKSPACE and action.target.startswith("workspace:"):
            if name != "run_shell" and execution_guardrails.is_sensitive_path(action.target.removeprefix("workspace:")):
                return "Sensitive paths are unavailable in YOLO mode."
    return None


def _pre_dispatch_tool_error(name: str, args: dict[str, Any], cfg: Config) -> str | None:
    """Reject known unavailable operations before approval or effect dispatch."""
    from .irene_memory_path_policy import UNQUALIFIED_BROWSER_ACTIONS, protected_tool_policy_error
    from .continuum_memory import ContinuumMemoryError, selected

    try:
        protected = selected(cfg)
    except ContinuumMemoryError:
        return "Memory configuration requires repair before tool execution."
    if protected and (
        name in {"update_user_profile", "write_knowledge_graph_note"}
        or name.startswith("intuition_")
    ):
        return "This legacy continuity action is unavailable with Continuum Memory; no alternate authority or plaintext shadow is permitted."

    mode_error = _mode_tool_error(name, args, cfg)
    if mode_error is not None:
        return mode_error

    protected_error = protected_tool_policy_error(name, args, cfg)
    if protected_error is not None:
        return protected_error
    if name == "action_program":
        from .nathan_program_runtime import (
            ActionProgramStep,
            ProgramAuthorization,
            ProgramValidationError,
            coerce_program_plan,
            compile_program,
        )

        authorization = getattr(cfg, "_algo_program_authorization", None)
        if not isinstance(authorization, ProgramAuthorization):
            return "Runtime program authorization was not bound"
        try:
            plan = coerce_program_plan(args.get("plan"), sibling_fields=args)
        except ProgramValidationError as exc:
            return f"ProgramValidationError: {exc}"
        try:
            compiled = compile_program(
                plan,
                authorization=authorization,
                cwd=cfg.cwd,
                safe_mode=bool(cfg.safe_mode),
            )
        except (TypeError, ValueError) as exc:
            return f"{type(exc).__name__}: {exc}"
        for step in compiled.steps:
            if isinstance(step, ActionProgramStep):
                error = _pre_dispatch_tool_error(step.action, tool_runtime_args(step.action, step.args(), cfg), cfg)
                if error is not None:
                    return f"Program step {step.step_id} was not dispatched: {error}"
    if name in UNQUALIFIED_BROWSER_ACTIONS:
        return tools_module._browser_guard()
    return None


def preflight_runtime_tool(
    name: str,
    args: dict[str, Any],
    cfg: Config,
    *,
    queue_position: int | None = None,
    policy_ceiling_code: str = "",
) -> RuntimeToolPreflight:
    """Evaluate and record the policy/QoS preflight used by every chat path."""

    signature_args = tool_runtime_args(name, args, cfg)
    runtime_hint = classify_tool_runtime(name, signature_args)
    now = time.time()
    action = resolve_action(name, signature_args, cwd=cfg.cwd)
    ceiling_reason = ""
    if policy_ceiling_code:
        ceiling_reason = _POLICY_CEILING_REASONS.get(
            policy_ceiling_code,
            "Unrecognized caller policy ceiling",
        )
    grant = None if ceiling_reason else _prepared_grant(cfg, action, now=now)
    guardrail_reasons: list[str] = []
    if ceiling_reason:
        guardrail_reasons.append(ceiling_reason)
    if not ceiling_reason and action.effect_class is not EffectClass.UNCLASSIFIED:
        admission_error = _pre_dispatch_tool_error(name, signature_args, cfg)
        if admission_error is not None:
            guardrail_reasons.append(admission_error)
    if name == "run_shell" and execution_guardrails.masks_verification_exit_status(
        str(signature_args.get("command") or "")
    ):
        guardrail_reasons.append(
            "verification command must preserve a failing exit status; remove the trailing "
            "`; echo ...$?` because run_shell already reports the exit code"
        )
    if name in {"write_file", "edit_file", "batch_edit"}:
        effective_path = _effective_tool_path(signature_args)
        active_workspace = execution_guardrails.active_workspace()
        if effective_path is None:
            guardrail_reasons.append("file mutation requires a path")
        elif active_workspace is None:
            guardrail_reasons.append("no active execution scope")
        else:
            path_decision = execution_guardrails.assess_write_path(
                active_workspace,
                effective_path,
            )
            if not path_decision.allowed:
                guardrail_reasons.append(path_decision.reason)
            elif (
                name == "write_file"
                and not bool(signature_args.get("overwrite"))
                and path_decision.resolved_path is not None
                and path_decision.resolved_path.exists()
            ):
                # A refused overwrite-less write is a typed pre-effect denial, never
                # an unknown outcome: the destination was provably untouched.
                guardrail_reasons.append(
                    f"{path_decision.resolved_path} already exists. "
                    "Re-run with overwrite=true if intended"
                )
            else:
                requires_read = name in {"edit_file", "batch_edit"}
                if name == "write_file" and bool(signature_args.get("overwrite")):
                    requires_read = bool(
                        path_decision.resolved_path is not None and path_decision.resolved_path.exists()
                    )
                if requires_read:
                    read_decision = execution_guardrails.read_before_edit_decision(effective_path)
                    if not read_decision.allowed:
                        guardrail_reasons.append(read_decision.reason)
    policy = evaluate_runtime_tool_policy(
        name,
        signature_args,
        safe_mode=bool(getattr(cfg, "safe_mode", True)),
        cwd=cfg.cwd,
        grant=grant,
        now=now,
        auto_approve=_approval_mode(cfg) == "auto" or bool(cfg.auto_approve_active),
    )
    denial: DenialExplanation | None = None
    repeated_denial = False
    if (
        not ceiling_reason
        and policy.disposition is PolicyDisposition.DENY
        and policy.fired_rules == ("scoped_authority",)
        and grant is None
        and action.confirmation_mode is ConfirmationMode.NONE
        and not action.target.endswith(":unresolved")
    ):
        denial = explain_missing_grant(cfg, action)
        policy = replace(policy, reasons=(denial.text(),))
    if not ceiling_reason and policy.disposition is PolicyDisposition.DENY and _turn_denials(cfg) is not None:
        try:
            signature = tool_attempt_signature(name, signature_args)
        except Exception:
            signature = ""
        if signature:
            repeated_denial = (
                _register_turn_denial(cfg, signature, "; ".join(policy.reasons), denial) is not None
            )
    preflight = RuntimeToolPreflight(
        signature_args=signature_args,
        runtime_hint=runtime_hint,
        policy=policy,
        guardrail_allowed=not guardrail_reasons,
        guardrail_reasons=tuple(guardrail_reasons),
        queue_position=queue_position,
        denial=denial,
        repeated_denial=repeated_denial,
    )
    record_perf_event(
        "qos",
        tool=name,
        reason=runtime_hint.reason,
        **preflight.qos_fields,
    )
    record_perf_event(
        "policy",
        tool=name,
        status=preflight.policy.disposition.value if preflight.guardrail_allowed else "blocked",
        tier=preflight.policy.tier,
        capability_mask=preflight.policy.capability_mask,
        capabilities=list(preflight.policy.capability_names),
        grant_id=preflight.policy.grant_id,
        fired_rules=list(preflight.policy.fired_rules),
        guardrail_reasons=list(preflight.guardrail_reasons),
    )
    return preflight


def ask_approval(
    name: str,
    args: dict[str, Any],
    cfg: Config,
    *,
    force: bool = False,
    preflight: RuntimeToolPreflight | None = None,
) -> bool:
    """Authorize and atomically consume one exact scoped grant."""

    from .display import console
    from .samuel_policy_engine import PolicyDisposition, evaluate_action

    current = preflight or preflight_runtime_tool(name, args, cfg)
    current_args = tool_runtime_args(name, args, cfg)
    if _mode_tool_error(name, current_args, cfg) is not None:
        return False
    current_action = resolve_action(name, current_args, cwd=cfg.cwd)
    if (
        current.signature_args != current_args
        or current.policy.action.name != name
        or current.policy.action.action_digest != current_action.action_digest
    ):
        return False
    if current.policy.disposition is PolicyDisposition.HANDOFF:
        console.print(f"[yellow]{name} requires a trusted user handoff and was not executed.[/]")
        return False
    if not current.allowed:
        return False
    now = time.time()
    action = current_action
    if force and action.confirmation_mode is not ConfirmationMode.HANDOFF_REQUIRED:
        action = replace(action, confirmation_mode=ConfirmationMode.ACTION_TIME)
    if action.confirmation_mode is ConfirmationMode.HANDOFF_REQUIRED:
        return False

    session = authority_session_for(cfg)
    grant = session.grant_by_id(current.policy.grant_id, now) if current.policy.grant_id else None
    from .session_mode import active_mode

    if grant is not None and grant.source == "user-yolo-preapproval" and active_mode(cfg) != "yolo":
        return False
    confirmation: ConfirmationReceipt | None = None
    mode = _approval_mode(cfg)
    review_authority = (cfg.cwd, cfg.safe_mode, cfg.auto_approve_active, mode)
    yolo_preapproved = (
        not force
        and grant is not None
        and grant.source == "user-yolo-preapproval"
        and active_mode(cfg) == "yolo"
    )
    if yolo_preapproved and action.confirmation_mode is ConfirmationMode.ACTION_TIME:
        confirmation = ConfirmationReceipt(
            receipt_id=f"yolo-confirmation-{uuid.uuid4().hex}",
            action_digest=action.action_digest,
            confirmation_mode=ConfirmationMode.ACTION_TIME,
            confirmed_at=now,
            expires_at=now + 120.0,
        )
    needs_prompt = grant is None or (
        action.confirmation_mode is ConfirmationMode.ACTION_TIME and not yolo_preapproved
    )

    if needs_prompt:
        if mode != "interactive" or action.confirmation_mode is ConfirmationMode.NONE:
            return False
        from .nathan_approval_channel import ApprovalChannel

        channel = getattr(cfg, "_nathan_approval_channel", None)
        if channel is not None:
            if not isinstance(channel, ApprovalChannel) or not channel.confirm(action, current_args):
                return False
            approval = "y"
        else:
            options = "[y/N/a]" if action.confirmation_mode is ConfirmationMode.SESSION_PREAPPROVAL else "[y/N]"
            console.print(f"[yellow]Approve exact {name} action?[/] {options}")
            console.print(
                json.dumps(
                    {
                        "target": action.target,
                        "confirmation": action.confirmation_mode.value,
                        "capabilities": list(current.policy.capability_names),
                        "arguments": redact_tool_args(name, args),
                    },
                    indent=2,
                )
            )
            try:
                approval = input(f"Approve? {options} ").strip().casefold()
            except (EOFError, OSError):
                console.print("[red]No interactive input available; operation denied.[/]")
                return False
        session_scope = approval == "a" and action.confirmation_mode is ConfirmationMode.SESSION_PREAPPROVAL
        if approval != "y" and not session_scope:
            return False
        # Human/supervisor review can outlive the original preflight or change its inputs.
        fresh_args = tool_runtime_args(name, args, cfg)
        if (
            (cfg.cwd, cfg.safe_mode, cfg.auto_approve_active, _approval_mode(cfg)) != review_authority
            or resolve_action(name, fresh_args, cwd=cfg.cwd) != current_action
            or not preflight_runtime_tool(name, args, cfg).allowed
        ):
            return False
        now = time.time()
        grant = session.issue(
            action,
            source="local-supervisor"
            if channel is not None
            else "interactive-session"
            if session_scope
            else "interactive-action",
            now=now,
            maximum_action_count=_SESSION_GRANT_ACTIONS if session_scope else 1,
        )
        if action.confirmation_mode is ConfirmationMode.ACTION_TIME:
            confirmation = ConfirmationReceipt(
                receipt_id=f"confirmation-{uuid.uuid4().hex}",
                action_digest=action.action_digest,
                confirmation_mode=ConfirmationMode.ACTION_TIME,
                confirmed_at=now,
                expires_at=now + 120.0,
            )

    decision = evaluate_action(
        action,
        grant=grant,
        confirmation=confirmation,
        now=now,
        auto_approve=mode == "auto" or bool(cfg.auto_approve_active),
    )
    if decision.disposition is not PolicyDisposition.ALLOW or grant is None:
        return False
    if not session.consume(grant.grant_id, now):
        return False
    record_perf_event(
        "authority",
        tool=name,
        status="allow",
        grant_id=grant.grant_id,
        confirmation=action.confirmation_mode.value,
        confirmation_receipt_id=confirmation.receipt_id if confirmation else "",
    )
    return True


def run_tool(name: str, args: dict[str, Any], cfg: Config) -> str:
    call_args = tool_runtime_args(name, args, cfg)
    mode_error = _mode_tool_error(name, call_args, cfg)
    if mode_error is not None:
        return f"Error: {mode_error}"
    from .irene_memory_path_policy import protected_tool_policy_error

    protected_path_error = protected_tool_policy_error(name, call_args, cfg)
    if protected_path_error is not None:
        return protected_path_error
    if name in {"write_file", "edit_file"}:
        from . import reconciliation

        violation = reconciliation.structured_write_violation(name, call_args, cfg.messages)
        if violation:
            return f"Error: {violation}"
    if name.startswith("memory_") or name in (
        "read_file",
        "list_directory",
        "search_files",
        "remember",
        "append_lesson",
        "update_user_profile",
        "query_knowledge_graph",
        "reindex_knowledge_graph",
        "write_knowledge_graph_note",
        "x_search",
        "jev_kernel_status",
        "jev_question_contract",
        "session_command",
        "action_search",
        "action_program",
        "harness_search",
        "harness_stats",
        "harness_refresh",
        "available_actions",
        "harness_read",
        "harness_scorecard",
        "harness_competitive_rating",
    ):
        call_args["cfg"] = cfg
    if name == "session_slash":
        from . import session_commands

        return session_commands.execute(str(call_args.get("command") or ""), cfg)
    if name in {"run_shell", "write_file"}:
        # Team cancellation is runtime authority; never accept a model-supplied value.
        call_args.pop("cancel_event", None)
        team_cancellation = getattr(cfg, "_algo_team_cancellation", None)
        if team_cancellation is not None:
            call_args["cancel_event"] = team_cancellation
    fn = TOOL_MAP.get(name)
    if not fn:
        available = ", ".join(sorted(TOOL_MAP)[:40])
        return f"Unknown tool: {name}. Available tools include: {available}."
    try:
        result = str(fn(**call_args))
    except TypeError as exc:
        # Bad/missing/extra args or unparseable JSON: return a corrective hint
        # (real signature + diagnosis) so a weak model can retry correctly.
        from . import tool_contract

        return tool_contract.correct_tool_error(name, call_args, exc, fn)
    except Exception as exc:
        return f"Tool error for {name}: {exc}"
    # Nudge the model off Unix-in-cmd.exe mistakes when the shell reports them.
    if name == "run_shell":
        from . import tool_contract

        hint = tool_contract.shell_mistake_hint(str(call_args.get("command", "")), result)
        if hint:
            result = f"{result}\n{hint}"
    return result


def tool_attempt_signature(name: str, args: dict[str, Any]) -> str:
    return keyed_action_fingerprint(name, args)


def _program_effect_targets(args: dict[str, Any], *, cwd: str) -> tuple[str, ...]:
    """Best-effort workspace effect scope for an action program's mutating steps."""
    plan = args.get("plan")
    if not isinstance(plan, dict):
        return ()
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return ()
    targets: list[str] = []
    for step in steps:
        if not isinstance(step, dict) or step.get("kind") != "action":
            continue
        step_name = str(step.get("action") or "")
        step_args = step.get("args")
        if not step_name or not isinstance(step_args, dict):
            continue
        try:
            action = resolve_action(step_name, step_args, cwd=cwd)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if (
            action.effect_class is not EffectClass.OBSERVE
            and action.target.startswith("workspace:")
            and action.target != "workspace:unresolved"
        ):
            targets.append(action.target)
    return tuple(dict.fromkeys(targets))


def _workspace_target_digest(target: str) -> str | None:
    """Content-free stable digest for one workspace-scoped effect target."""
    if not target.startswith("workspace:") or target == "workspace:unresolved":
        return None
    try:
        return keyed_action_fingerprint("workspace_target", {"target": target})
    except (PrivacyProjectionError, TypeError, ValueError):
        return None


def _observed_target_digest_chain(observed_target: str) -> frozenset[str]:
    """Digests of one observed workspace target and all of its ancestors."""
    if not observed_target.startswith("workspace:") or observed_target == "workspace:unresolved":
        return frozenset()
    try:
        path = Path(observed_target.removeprefix("workspace:"))
        candidates = [path, *path.parents]
    except (OSError, RuntimeError, ValueError):
        return frozenset()
    digests: set[str] = set()
    for candidate in candidates:
        digest = _workspace_target_digest(f"workspace:{candidate}")
        if digest is not None:
            digests.add(digest)
    return frozenset(digests)


def _reconcile_uncertain_attempts(cfg: Config, *, observed_target: str) -> None:
    """Let one fresh in-scope observation mark uncertain workspace effects retryable.

    The runtime cannot prove what an interrupted mutation did; the ledger only
    requires a fresh observation of the affected scope before the same action
    may run again. Targets persist as content-free digests, so reconciliation
    survives restarts without retaining workspace paths. The actor owns the
    judgment of what the observation shows; memory-store and external
    uncertainty stay unreconciled because workspace reads cannot observe them.
    """
    chain = _observed_target_digest_chain(observed_target)
    if not chain:
        return
    reconciled_at = time.time()
    for item in cfg.attempt_ledger:
        if item.get("status") != "unknown_outcome" or item.get("reconciled"):
            continue
        candidate_digests: list[str] = []
        if item.get("target_digest"):
            candidate_digests.append(str(item.get("target_digest")))
        candidate_digests.extend(str(digest) for digest in (item.get("target_digests") or ()))
        if any(digest in chain for digest in candidate_digests):
            item["reconciled"] = True
            item["reconciled_at"] = reconciled_at


def _is_nonretryable_attempt(item: dict[str, Any]) -> bool:
    if item.get("reconciled"):
        # A fresh in-scope observation retired this uncertain attempt.
        return False
    return item.get("status") == "unknown_outcome" or (
        item.get("status") == "failed" and item.get("retry_allowed") is False
    )


def _retry_barrier_indices(cfg: Config) -> set[int]:
    barriers: dict[str, int] = {}
    for index, item in enumerate(cfg.attempt_ledger):
        signature = str(item.get("signature") or "")
        if _is_nonretryable_attempt(item):
            barriers[signature] = index
        elif item.get("status") not in {"skipped", "denied"}:
            barriers.pop(signature, None)
    return set(barriers.values())


@contextmanager
def reserve_retry_capacity(cfg: Config, *, mutating: bool) -> Iterator[bool]:
    """Reserve space for an uncertain effect before approval or invocation."""
    if not mutating:
        yield True
        return
    with _ATTEMPT_LEDGER_LOCK:
        reserved = int(getattr(cfg, "_nathan_retry_slots", 0))
        admitted = len(_retry_barrier_indices(cfg)) + reserved < ATTEMPT_LEDGER_LIMIT
        if admitted:
            setattr(cfg, "_nathan_retry_slots", reserved + 1)
    try:
        yield admitted
    finally:
        if admitted:
            with _ATTEMPT_LEDGER_LOCK:
                remaining = int(getattr(cfg, "_nathan_retry_slots", 0)) - 1
                if remaining:
                    setattr(cfg, "_nathan_retry_slots", remaining)
                else:
                    delattr(cfg, "_nathan_retry_slots")


def _find_failed_attempt_unlocked(cfg: Config, signature: str) -> dict[str, Any] | None:
    now = time.time()
    skipped = False
    for item in reversed(cfg.attempt_ledger):
        if item.get("signature") != signature:
            continue
        status = item.get("status")
        if item.get("reconciled"):
            # A fresh in-scope observation retired this uncertain attempt.
            continue
        if status in {"skipped", "denied"}:
            # Neither outcome reconciles an earlier uncertain/nonretryable effect.
            skipped = skipped or status == "skipped"
            continue
        if status == "unknown_outcome":
            # Uncertain mutations remain non-retryable until a fresh observer
            # reconciles them; elapsed time cannot prove that no effect occurred.
            return item
        if status != "failed":
            return None
        if item.get("retry_allowed") is False:
            # A typed non-idempotent/at-most-once failure remains blocked even
            # when it is known not to have succeeded. A fresh explicit action
            # or reconciliation workflow must decide whether to try again.
            return item
        if skipped:
            return None
        try:
            age = now - float(item.get("timestamp") or 0)
        except (TypeError, ValueError):
            age = 0.0
        if age <= FAILED_ATTEMPT_SKIP_SECONDS:
            return item
        return None
    return None


def find_failed_attempt(cfg: Config, signature: str) -> dict[str, Any] | None:
    """Read the bounded attempt ledger consistently across parallel observations.

    A call denied earlier in the current turn is returned as a synthetic,
    never-persisted entry so the dispatcher skips it without a second prompt.
    """

    with _ATTEMPT_LEDGER_LOCK:
        found = _find_failed_attempt_unlocked(cfg, signature)
        if found is not None:
            return found
        denials = _turn_denials(cfg)
        prior = denials.get(signature) if denials is not None else None
    if prior is None:
        return None
    return {"signature": signature, "status": "denied", "summary": _repeated_denial_text(prior[0])}


def summarize_tool_result(result: str, limit: int = 140) -> str:
    text = " ".join(str(result).split())
    return text[:limit] + ("..." if len(text) > limit else "")


def content_free_tool_result_summary(name: str, result: str, status: str) -> str:
    """Return bounded receipt metadata without retaining a tool payload prefix."""

    text = str(result)
    encoded = text.encode("utf-8", errors="replace")
    normalized_status = (
        str(status).strip().casefold()
        if str(status).strip().casefold()
        in {
            "worked",
            "failed",
            "denied",
            "skipped",
            "timed_out",
            "cancelled",
            "unknown_outcome",
        }
        else "failed"
    )
    try:
        digest = keyed_action_fingerprint(
            f"{str(name or 'unknown')}:result",
            {"result": text},
        )
    except (PrivacyProjectionError, TypeError, ValueError):
        digest = "unavailable"
    return f"status={normalized_status}; chars={len(text)}; bytes={len(encoded)}; digest={digest}"


def run_args_preview(args: dict[str, Any], limit: int = 60, *, name: str = "") -> str:
    safe_args = redact_tool_args(name, args)
    try:
        text = json.dumps(safe_args, ensure_ascii=True, default=str, separators=(",", ":"))
    except TypeError:
        text = str(safe_args)
    return text[:limit]


def _structured_result_failed(result: str, *, name: str = "") -> bool:
    text = str(result).strip()
    if name in _OPAQUE_JSON_RESULT_TOOLS or len(text) > 64 * 1024 or not text.startswith("{"):
        return False
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(value, dict):
        return False
    if value.get("ok") is False or value.get("success") is False:
        return True
    status = str(value.get("status") or "").strip().casefold()
    if status in _STRUCTURED_ERROR_STATUSES:
        return True
    error = value.get("error")
    if error is not None and error is not False and error != "":
        return True
    status_code = value.get("status_code")
    return isinstance(status_code, int) and not isinstance(status_code, bool) and status_code >= 400


def _structured_result_status(result: str, *, name: str = "") -> str | None:
    """Preserve a typed structured status instead of flattening it to failed.

    Program and bridge results carry their own outcome status. Collapsing a
    reported ``denied`` or ``unknown_outcome`` to ``failed`` would mislabel the
    outer dispatch of an action that never applied its effect.
    """
    text = str(result).strip()
    if name in _OPAQUE_JSON_RESULT_TOOLS or len(text) > 64 * 1024 or not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    status = str(value.get("status") or "").strip().casefold()
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"timed_out", "timeout"}:
        return "timed_out"
    if status in {"denied", "skipped", "unknown_outcome"}:
        return status
    return None


def _is_read_file_error(result: str) -> bool:
    """Recognize only the error shapes read_file and the runtime emit, never file bodies.

    read_file returns file text verbatim, so its first line proves nothing. Its own errors
    and the runtime's refusals are single lines with no trailing newline; the two multi-line
    shapes are the not-found suggestion list and the argument-correction hint.
    """

    lowered = result.lower()
    if lowered.startswith("error: file not found: ") and result.endswith(_READ_FILE_NOT_FOUND_RETRY):
        return True
    if lowered.startswith("tool argument error for read_file:"):
        return True
    if "\n" in result or "\r" in result:
        return False
    lowered = lowered.strip()
    return lowered.startswith(("error:", "tool error", "unknown tool")) or bool(_TOOL_ERROR_PREFIX_RE.match(lowered))


def classify_tool_status(
    result: str,
    *,
    name: str = "",
    approved: bool = True,
    skipped: bool = False,
) -> str:
    if skipped:
        return "skipped"
    if not approved:
        return "denied"
    if name == "read_file":
        return "failed" if _is_read_file_error(str(result)) else "worked"
    lowered = str(result).strip().lower()
    if lowered.startswith(("error:", "tool error", "tool argument error", "unknown tool")):
        return "failed"
    if name in {"", "search_files"} and _PARTIAL_SEARCH_NOTE_RE.search(str(result).rstrip()):
        # The matches stay in the result; only the status stops claiming a complete search.
        return "failed"
    exit_matches = _SHELL_EXIT_CODE_RE.findall(str(result))
    # Shell output carries its own exit code; its first line is the command's output, not a tool error.
    if not exit_matches and _TOOL_ERROR_PREFIX_RE.match(lowered):
        return "failed"
    preserved_status = _structured_result_status(result, name=name)
    if preserved_status is not None:
        return preserved_status
    if _structured_result_failed(result, name=name):
        return "failed"
    if exit_matches and int(exit_matches[-1]) != 0:
        return "failed"
    return "worked"


def augment_tool_result_with_reflex(
    cfg: Config,
    name: str,
    args: dict[str, Any],
    result: str,
    status: str,
) -> str:
    augmented, note = reflex.maybe_augment_tool_result(cfg, name, args, result, status)
    if status == "worked":
        from . import reconciliation

        augmented = reconciliation.augment_read_result(name, augmented, messages=cfg.messages)
    if note:
        show_info(note)
    return augmented


def _record_tool_attempt_unlocked(
    cfg: Config,
    *,
    name: str,
    args: dict[str, Any],
    result: str,
    status: str,
    retry_allowed: bool | None = None,
    invoked: bool = True,
) -> None:
    invoked = invoked and status not in {"denied", "skipped"}
    worked = invoked and status == "worked"
    workspace_changed = False
    effective_path = _effective_tool_path(args)
    if name == "read_file" and effective_path is not None:
        execution_guardrails.record_read(effective_path, success=worked)
    elif name in {"write_file", "edit_file", "batch_edit"} and effective_path is not None:
        success_prefixes = {
            "write_file": "Wrote ",
            "edit_file": "Edited ",
            "batch_edit": "Batch-edited ",
        }
        mutation_succeeded = worked and str(result).lstrip().startswith(success_prefixes[name])
        workspace_changed = mutation_succeeded
        execution_guardrails.record_mutation(
            effective_path,
            success=mutation_succeeded,
            operation=name,
        )
    elif name == "run_shell":
        command = str(args.get("command") or "")
        exit_matches = _SHELL_EXIT_CODE_RE.findall(str(result))
        returncode = int(exit_matches[-1]) if exit_matches else None
        if invoked and tools_module.shell_mutates_workspace(command):
            workspace_changed = True
            execution_guardrails.record_workspace_mutation(success=worked, possible=not worked)
        if worked and returncode is not None:
            execution_guardrails.record_shell_verification(
                command, returncode=returncode, cwd=args.get("cwd") or cfg.cwd
            )
    elif name == "git_diff":
        normalized_result = str(result).strip().lower()
        execution_guardrails.record_verification(
            "git_diff",
            success=worked and normalized_result not in {"", "(no tracked diff)", "(clean working tree)"},
            cwd=args.get("cwd") or cfg.cwd,
        )
    try:
        signature = tool_attempt_signature(name, args)
        args_receipt = keyed_action_fingerprint(f"{name}:args", args)
    except (PrivacyProjectionError, TypeError, ValueError):
        safe_name = str(name or "unknown")
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", safe_name) is None:
            safe_name = "unknown"
        signature = keyed_action_fingerprint(
            "privacy_projection_error",
            {"tool": safe_name},
        )
        args_receipt = keyed_action_fingerprint(
            "privacy_projection_error:args",
            {"tool": safe_name},
        )
    if workspace_changed:
        # A workspace mutation invalidates cached failures: the exact same
        # test/check command is often the correct next action after a fix.
        cfg.attempt_ledger = [
            item
            for item in cfg.attempt_ledger
            if item.get("status") not in {"failed", "skipped"} or _is_nonretryable_attempt(item)
        ]
    if status == "denied" and _turn_denials(cfg) is not None:
        for prefix, kind in _APPROVAL_DENIAL_PREFIXES.items():
            if str(result).startswith(prefix):
                try:
                    denied_target = resolve_action(name, args, cwd=cfg.cwd).target
                except (OSError, RuntimeError, TypeError, ValueError):
                    denied_target = "unresolved"
                _register_turn_denial(
                    cfg,
                    signature,
                    summarize_tool_result(result, 300),
                    DenialExplanation(
                        name,
                        denied_target,
                        "the required approval was not granted",
                        kind,
                        False,
                        "Not resolvable by retrying in this turn. Use a permitted alternative or report the blocker.",
                    ),
                )
                break
    if status in {"skipped", "denied"}:
        prior = _find_failed_attempt_unlocked(cfg, signature)
        if prior is not None and _is_nonretryable_attempt(prior):
            # Coalesce nonexecutions so repeated requests cannot evict their barrier.
            # Each request still has a typed dispatch/performance receipt.
            return
    try:
        recorded_action = resolve_action(name, args, cwd=cfg.cwd)
    except (OSError, RuntimeError, TypeError, ValueError):
        recorded_action = None
    if recorded_action is not None and status == "worked" and invoked:
        if (
            recorded_action.effect_class is EffectClass.OBSERVE
            and recorded_action.target.startswith("workspace:")
            and recorded_action.target != "workspace:unresolved"
        ):
            # A fresh observation of the affected scope is the promised
            # reconciliation path for uncertain workspace effects.
            _reconcile_uncertain_attempts(cfg, observed_target=recorded_action.target)
    entry: dict[str, Any] = {
        "timestamp": time.time(),
        "signature": signature,
        "tool": name,
        "args_receipt": args_receipt,
        "status": status,
        "summary": content_free_tool_result_summary(name, result, status),
    }
    if retry_allowed is not None:
        entry["retry_allowed"] = bool(retry_allowed)
    if recorded_action is not None:
        target_digest = _workspace_target_digest(recorded_action.target)
        if target_digest is not None:
            entry["target_digest"] = target_digest
        if name == "action_program":
            program_digests = tuple(
                digest
                for target in _program_effect_targets(args, cwd=cfg.cwd)
                if (digest := _workspace_target_digest(target)) is not None
            )
            if program_digests:
                entry["target_digests"] = list(program_digests)
    cfg.attempt_ledger.append(entry)
    barriers = _retry_barrier_indices(cfg)
    recent = [index for index in range(len(cfg.attempt_ledger)) if index not in barriers]
    available = max(0, ATTEMPT_LEDGER_LIMIT - len(barriers))
    retained = barriers | set(recent[-available:] if available else [])
    cfg.attempt_ledger = [item for index, item in enumerate(cfg.attempt_ledger) if index in retained]


def record_tool_attempt(
    cfg: Config,
    *,
    name: str,
    args: dict[str, Any],
    result: str,
    status: str,
    retry_allowed: bool | None = None,
    invoked: bool = True,
) -> None:
    """Record one outcome atomically with its execution-evidence side effects."""

    with _ATTEMPT_LEDGER_LOCK:
        _record_tool_attempt_unlocked(
            cfg,
            name=name,
            args=args,
            result=result,
            status=status,
            retry_allowed=retry_allowed,
            invoked=invoked,
        )


def tool_result_message(name: str, content: str, tool_call_id: str | None = None) -> dict[str, Any]:
    message = {
        "role": "tool",
        "name": name,
        "tool_name": name,
        "content": content[:TOOL_RESULT_CONTENT_LIMIT],
    }
    if tool_call_id:
        message["tool_call_id"] = tool_call_id
    return message


def recent_messages_for_reflection(cfg: Config, user_message: str) -> str:
    snippets: list[str] = []
    if cfg.session_summary.strip():
        snippets.append(f"SESSION SUMMARY:\n{cfg.session_summary.strip()}")
    snippets.append(f"CURRENT USER GOAL:\n{user_message.strip()}")
    if cfg.messages:
        snippets.append("RECENT MESSAGES:")
        for message in cfg.messages[-REFLECTION_RECENT_MESSAGES:]:
            role = str(message.get("role", "message"))
            name = message.get("name")
            label = f"{role}[{name}]" if name else role
            content = (message.get("content") or message.get("thinking") or "").strip()
            if not content:
                continue
            if len(content) > 700:
                content = content[:700] + "..."
            snippets.append(f"- {label}: {content}")
    return "\n".join(snippets)


def reflection_checkpoint(client: Client, cfg: Config, user_message: str, tool_calls_seen: int) -> None:
    checkpoint_prompt = recent_messages_for_reflection(cfg, user_message)
    system = (
        "You are pausing an agentic terminal session for a progress checkpoint.\n"
        "Return compact JSON with keys: objective, completed, evidence, remaining, "
        "alignment_check, web_research_needed, web_research_reason, next_action, "
        "confidence (float 0.0-1.0: how certain you are the completed work is correct), "
        "and unverified_claims (list of specific facts stated but not confirmed by tool results).\n"
        "The alignment_check must state whether completed work and next_action still match the user's objective.\n"
        "Keep each value short. Do not include chain-of-thought or hidden reasoning. "
        "The result will be fed back into the conversation as an internal continuation note."
    )
    try:
        from . import main as _main

        reflection_client, reflection_model = _main.small_maintenance_client(cfg, client)
        response = reflection_client.chat(
            model=reflection_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": checkpoint_prompt},
            ],
            stream=False,
            think=False,
            format="json",
            keep_alive=cfg.keep_alive,
            options={"temperature": 0.1, "num_ctx": min(cfg.num_ctx, 4096), "num_predict": 512},
        )
        content = get_attr(get_attr(response, "message", {}), "content", "").strip()
    except Exception as exc:
        content = json.dumps(
            {
                "objective": "Checkpoint unavailable",
                "completed": "Reflection failed before summary generation.",
                "evidence": "No checkpoint response was produced.",
                "remaining": "Continue from the last verified tool result.",
                "alignment_check": "Use the current user goal and latest tool results as the source of truth.",
                "web_research_needed": False,
                "web_research_reason": f"Reflection error: {exc}",
                "next_action": "Continue without a checkpoint summary.",
                "confidence": 0.5,
                "unverified_claims": [],
            },
            ensure_ascii=False,
        )
    if not content:
        return
    low_confidence_note = ""
    try:
        parsed = json.loads(content)
        confidence = float(parsed.get("confidence", 1.0))
        unverified = parsed.get("unverified_claims", [])
        if confidence < 0.6:
            low_confidence_note = (
                "\n⚠ Low confidence detected. Verify uncertain claims with "
                "read_file, search_files, or harness_search before providing the final answer."
            )
        elif unverified:
            low_confidence_note = (
                f"\n⚠ {len(unverified)} unverified claim(s) flagged. "
                "Consider using tool calls to confirm before stating as fact."
            )
    except Exception:
        pass
    note = (
        f"[Internal checkpoint after {tool_calls_seen} tool calls]\n"
        f"{content}{low_confidence_note}\n\n"
        "Use this only to align the next step with the user's goal. Do not answer this checkpoint directly. "
        "Continue the active task with the next necessary tool call, or provide the final answer only if the task is complete."
    )
    cfg.messages.append({"role": "user", "content": note})
    show_info(f"Checkpoint after {tool_calls_seen} tool calls: progress reviewed.")


def execute_tool_call_for_pipeline(
    name: str,
    args: dict[str, Any],
    cfg: Config,
    *,
    tool_call_id: str | None = None,
    force_approval: bool = False,
    policy_ceiling_code: str = "",
    deadline_monotonic: float | None = None,
    cancellation: Any = None,
) -> PipelineToolResult:
    from .james_dispatch import dispatch_action

    dispatched = dispatch_action(
        name,
        args,
        cfg,
        tool_call_id=tool_call_id,
        force_approval=force_approval,
        policy_ceiling_code=policy_ceiling_code,
        deadline_monotonic=deadline_monotonic,
        cancellation=cancellation,
    )
    return PipelineToolResult(dispatched.message, dispatched.result, dispatched.outcome)
