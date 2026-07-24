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
    PrivacyView,
    keyed_action_fingerprint,
    project_action_args,
)
from .samuel_policy import RuntimeToolPolicyDecision, evaluate_runtime_tool_policy
from .samuel_policy_engine import (
    resolve_action,
    session_command_requires_approval,  # noqa: F401 - compatibility re-export
)

ATTEMPT_LEDGER_LIMIT = 48
REFLECTION_RECENT_MESSAGES = 8
TOOL_RESULT_CONTENT_LIMIT = 20_000
FAILED_ATTEMPT_SKIP_SECONDS = 120.0
_SHELL_EXIT_CODE_RE = re.compile(r"\[exit code:\s*(-?\d+)\]", re.IGNORECASE)
_BASELINE_CAPABILITIES = CapabilityMask(Capability.READ.value | Capability.MODEL.value)
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
    "agent_batch_quarantined": (
        "Skipped because another tool call in this assistant message violated block policy"
    ),
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
_OPAQUE_JSON_RESULT_TOOLS = frozenset(
    {"harness_read", "read_file", "read_pdf", "render_pdf_pages", "web_fetch"}
)
_STRUCTURED_ERROR_STATUSES = frozenset(
    {"cancelled", "canceled", "denied", "error", "failed", "failure", "timed_out", "timeout"}
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


def _prepared_grant(
    cfg: Config,
    action: ResolvedAction,
    *,
    now: float,
) -> ConsentGrant | None:
    session = authority_session_for(cfg)
    grant = session.matching_grant(action, now)
    if grant is not None:
        return grant
    if session.baseline_allows(action):
        return session.issue(
            action,
            source="runtime-baseline",
            now=now,
            ttl_seconds=_BASELINE_GRANT_SECONDS,
        )
    auto_preapproved = _approval_mode(cfg) == "auto" or bool(cfg.auto_approve_active)
    if action.confirmation_mode is ConfirmationMode.SESSION_PREAPPROVAL and auto_preapproved:
        return session.issue(
            action,
            source="trusted-auto-preapproval",
            now=now,
            maximum_action_count=_SESSION_GRANT_ACTIONS,
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
        reasons = [*self.policy.reasons, *self.guardrail_reasons]
        reason = "; ".join(reasons) or "runtime authority rejected the call"
        return f"Blocked by runtime authority: {reason}."


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
    }:
        call_args["cwd"] = cfg.cwd
    if name == "run_shell":
        # Preserve the session-level /safe guard. A model may opt into stricter
        # safe_mode, but it may not opt out while cfg.safe_mode is enabled.
        call_args["safe_mode"] = bool(getattr(cfg, "safe_mode", True)) or bool(call_args.get("safe_mode", False))
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
            else:
                requires_read = name in {"edit_file", "batch_edit"}
                if name == "write_file" and bool(signature_args.get("overwrite")):
                    requires_read = bool(
                        path_decision.resolved_path is not None
                        and path_decision.resolved_path.exists()
                    )
                if requires_read:
                    read_decision = execution_guardrails.read_before_edit_decision(effective_path)
                    if not read_decision.allowed:
                        guardrail_reasons.append(read_decision.reason)
    preflight = RuntimeToolPreflight(
        signature_args=signature_args,
        runtime_hint=runtime_hint,
        policy=evaluate_runtime_tool_policy(
            name,
            signature_args,
            safe_mode=bool(getattr(cfg, "safe_mode", True)),
            cwd=cfg.cwd,
            grant=grant,
            now=now,
            auto_approve=_approval_mode(cfg) == "auto" or bool(cfg.auto_approve_active),
        ),
        guardrail_allowed=not guardrail_reasons,
        guardrail_reasons=tuple(guardrail_reasons),
        queue_position=queue_position,
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
    confirmation: ConfirmationReceipt | None = None
    mode = _approval_mode(cfg)
    needs_prompt = grant is None or action.confirmation_mode is ConfirmationMode.ACTION_TIME

    if needs_prompt:
        if mode != "interactive":
            return False
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
        session_scope = (
            approval == "a" and action.confirmation_mode is ConfirmationMode.SESSION_PREAPPROVAL
        )
        if approval != "y" and not session_scope:
            return False
        grant = session.issue(
            action,
            source="interactive-session" if session_scope else "interactive-action",
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
    if name in {"write_file", "edit_file"}:
        from . import reconciliation

        violation = reconciliation.structured_write_violation(name, call_args, cfg.messages)
        if violation:
            return f"Error: {violation}"
    if name in ("remember", "append_lesson", "session_command", "action_program"):
        call_args["cfg"] = cfg
    if name == "session_slash":
        from . import session_commands

        return session_commands.execute(str(call_args.get("command") or ""), cfg)
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


def _find_failed_attempt_unlocked(cfg: Config, signature: str) -> dict[str, Any] | None:
    now = time.time()
    for item in reversed(cfg.attempt_ledger):
        if item.get("signature") != signature:
            continue
        status = item.get("status")
        if status == "skipped":
            return None
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
        try:
            age = now - float(item.get("timestamp") or 0)
        except (TypeError, ValueError):
            age = 0.0
        if age <= FAILED_ATTEMPT_SKIP_SECONDS:
            return item
        return None
    return None


def find_failed_attempt(cfg: Config, signature: str) -> dict[str, Any] | None:
    """Read the bounded attempt ledger consistently across parallel observations."""

    with _ATTEMPT_LEDGER_LOCK:
        return _find_failed_attempt_unlocked(cfg, signature)


def summarize_tool_result(result: str, limit: int = 140) -> str:
    text = " ".join(str(result).split())
    return text[:limit] + ("..." if len(text) > limit else "")


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
    return (
        isinstance(status_code, int)
        and not isinstance(status_code, bool)
        and status_code >= 400
    )


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
    lowered = str(result).strip().lower()
    if lowered.startswith(("error:", "tool error", "tool argument error", "unknown tool")):
        return "failed"
    if _structured_result_failed(result, name=name):
        return "failed"
    exit_matches = _SHELL_EXIT_CODE_RE.findall(str(result))
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
) -> None:
    worked = status == "worked"
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
        if returncode == 0 and tools_module.shell_mutates_workspace(command):
            workspace_changed = True
            execution_guardrails.record_workspace_mutation(success=True)
        if returncode is not None:
            execution_guardrails.record_shell_verification(command, returncode=returncode)
    elif name == "git_diff":
        normalized_result = str(result).strip().lower()
        execution_guardrails.record_verification(
            "git_diff",
            success=worked
            and normalized_result not in {"", "(no tracked diff)", "(clean working tree)"},
        )
    try:
        signature = tool_attempt_signature(name, args)
    except (PrivacyProjectionError, TypeError, ValueError):
        signature = f"{name}:privacy-projection-error"
    if workspace_changed:
        # A workspace mutation invalidates cached failures: the exact same
        # test/check command is often the correct next action after a fix.
        cfg.attempt_ledger = [
            item for item in cfg.attempt_ledger if item.get("status") not in {"failed", "skipped"}
        ]
    try:
        audit_args = project_action_args(name, args, PrivacyView.AUDIT)
    except (PrivacyProjectionError, TypeError, ValueError):
        audit_args = {"privacy_error": "arguments unavailable"}
    args_preview = json.dumps(
        audit_args,
        sort_keys=True,
        ensure_ascii=True,
        default=str,
    )[:100]
    entry: dict[str, Any] = {
        "timestamp": time.time(),
        "signature": signature,
        "tool": name,
        "args_preview": args_preview,
        "status": status,
        "summary": summarize_tool_result(result),
    }
    if retry_allowed is not None:
        entry["retry_allowed"] = bool(retry_allowed)
    cfg.attempt_ledger.append(entry)
    cfg.attempt_ledger = cfg.attempt_ledger[-ATTEMPT_LEDGER_LIMIT:]


def record_tool_attempt(
    cfg: Config,
    *,
    name: str,
    args: dict[str, Any],
    result: str,
    status: str,
    retry_allowed: bool | None = None,
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
