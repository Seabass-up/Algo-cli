"""Target-bound, fail-closed policy evaluation for Algo CLI actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .marcus_authority import (
    Capability,
    CapabilityMask,
    ConfirmationMode,
    ConfirmationReceipt,
    ConsentGrant,
    DataClass,
    EffectClass,
    IdempotencyClass,
    OutcomeModel,
    ResolvedAction,
    TargetScope,
    VerificationRequirement,
    policy_for_action,
)
from .workspace_resolver import parse_path_arg


class PolicyDisposition(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"
    HANDOFF = "handoff"


@dataclass(frozen=True)
class PolicyDecision:
    disposition: PolicyDisposition
    reason: str
    action: ResolvedAction
    grant_id: str = ""
    confirmation_receipt_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.disposition is PolicyDisposition.ALLOW


_WORKSPACE_PATH_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "batch_edit": ("path",),
    "edit_file": ("path",),
    "find_unique_anchor": ("path",),
    "read_file": ("path",),
    "read_pdf": ("path",),
    "render_pdf_pages": ("path",),
    "search_files": ("path",),
    "write_file": ("path",),
}

_SAFE_SESSION_COMMANDS = frozenset(
    {
        "/actions",
        "/changes",
        "/dashboard",
        "/diff",
        "/doctor",
        "/help",
        "/hread",
        "/hsearch",
        "/identity",
        "/info",
        "/memories",
        "/perf",
        "/selfcheck",
        "/status",
    }
)
_SAFE_SESSION_STATUS_COMMANDS = frozenset(
    {
        "/auto",
        "/cloud",
        "/cloudauto",
        "/code-rag",
        "/context",
        "/harness",
        "/icl",
        "/intel",
        "/intelagence",
        "/intelligence",
        "/intuition",
        "/lessons",
        "/memory-auto",
        "/mode",
        "/policy",
        "/reason",
        "/reflex",
        "/safe",
        "/skills",
        "/thinking",
        "/verify",
        "/x-account",
        "/xai-status",
    }
)
_EMPTY_ARG_TOGGLES = frozenset({"/auto", "/cloud", "/cloudauto", "/safe", "/thinking", "/verify"})


def normalize_session_command(command_line: str) -> tuple[str, str]:
    """Return the normalized slash command and argument without interpreting it."""

    stripped = (command_line or "").strip()
    if not stripped.startswith("/"):
        stripped = f"/{stripped}" if stripped else ""
    if not stripped:
        return "", ""
    parts = stripped.split(maxsplit=1)
    return parts[0].casefold(), parts[1].strip().casefold() if len(parts) > 1 else ""


def session_command_requires_approval(command_line: str) -> bool:
    """Classify the exact model-invoked slash command, defaulting to protected."""

    command, arg = normalize_session_command(command_line)
    if not command:
        return True
    if command in _SAFE_SESSION_COMMANDS or command in {"/read", "/ls", "/cwd"}:
        return False
    if command == "/cd":
        return True
    if command in {"/intelligence", "/intel", "/intelagence"}:
        return not (
            arg in {"", "status", "show", "?", "guide", "help"}
            or arg.startswith("query ")
        )
    if command == "/kernel":
        return not (
            arg in {"", "list", "show", "check", "?", "help"}
            or arg.startswith("show ")
            or arg.startswith("check ")
        )
    if command == "/memory":
        return not (
            arg in {"", "home", "status", "show-home", "doctor", "benchmark", "help", "?"}
            or arg.startswith("search ")
            or arg.startswith("show ")
        )
    if command in _EMPTY_ARG_TOGGLES:
        return arg not in {"status", "show", "?"}
    if command == "/agent":
        return not (
            arg in {"help", "--help", "-h", "?", "threads", "list", "status", "show"}
            or arg.startswith("show ")
        )
    if command == "/worktree":
        return arg not in {"", "status", "show", "list", "help", "?"}
    if command == "/ship":
        return arg not in {"", "status", "plan", "show", "help", "?"}
    if command in _SAFE_SESSION_STATUS_COMMANDS and arg in {
        "",
        "status",
        "show",
        "?",
        "guide",
        "help",
    }:
        return False
    if command == "/x-account" and arg == "status":
        return False
    if command == "/config" and arg in {"", "status", "show", "?", "help"}:
        return False
    return True


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest_action(name: str, args: dict[str, Any], target: str, snapshot_revision: str) -> str:
    envelope = {
        "name": name,
        "args": args,
        "target": target,
        "snapshot_revision": snapshot_revision,
    }
    return hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()


def _canonical_origin(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.casefold()
        hostname = (parsed.hostname or "").encode("idna").decode("ascii").casefold().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError):
        return "provider:unresolved"
    if not scheme or not hostname or scheme not in {"http", "https"}:
        return "provider:unresolved"
    default_port = 443 if scheme == "https" else 80
    effective_port = port if port is not None else default_port
    return f"origin:{scheme}://{hostname}:{effective_port}"


def resolve_action(
    name: str,
    args: dict[str, Any],
    *,
    cwd: str,
    snapshot_revision: str = "unversioned",
) -> ResolvedAction:
    """Resolve the static policy and best available target before approval."""

    policy = policy_for_action(name)
    target = _target_for(name, args, cwd=cwd, target_scope=policy.target_scope)
    effect_class = policy.effect_class
    target_scope = policy.target_scope
    capabilities = policy.capability_mask.value
    data_classes = policy.data_classes
    confirmation_mode = policy.confirmation_mode
    idempotency = policy.idempotency
    outcome_model = policy.outcome_model
    verification = policy.verification
    compensation_action = policy.compensation_action
    if name in {"session_command", "session_slash"}:
        raw_command_line = str(args.get("command") or "").strip()
        command, _arg = normalize_session_command(raw_command_line)
        target = f"runtime:session:{command or 'unresolved'}"
        raw_parts = raw_command_line.split(maxsplit=1)
        raw_arg = raw_parts[1] if len(raw_parts) > 1 else ""
        if command in {"/read", "/ls", "/cd"}:
            parsed_path = parse_path_arg(raw_arg) or ("." if command == "/ls" else "")
            target_scope = TargetScope.WORKSPACE
            data_classes = (DataClass.LOCAL_CONTENT,)
            if parsed_path:
                target = _target_for(
                    "read_file",
                    {"path": parsed_path},
                    cwd=cwd,
                    target_scope=TargetScope.WORKSPACE,
                )
            else:
                target = "workspace:unresolved"
        if session_command_requires_approval(str(args.get("command") or "")):
            effect_class = EffectClass.CONFIGURATION
            capabilities = Capability.READ.value | Capability.WRITE.value
            data_classes = (DataClass.USER_PROFILE,)
            confirmation_mode = ConfirmationMode.ACTION_TIME
            idempotency = IdempotencyClass.NON_IDEMPOTENT
            outcome_model = OutcomeModel.UNKNOWN_POSSIBLE
            verification = VerificationRequirement.FRESH_OBSERVATION
            compensation_action = ""
        else:
            effect_class = EffectClass.OBSERVE
            capabilities = Capability.READ.value
            data_classes = (DataClass.USER_PROFILE,)
            confirmation_mode = ConfirmationMode.NONE
            idempotency = IdempotencyClass.PURE
            outcome_model = OutcomeModel.DETERMINISTIC
            verification = VerificationRequirement.STRUCTURED_RESULT
            compensation_action = ""
        if command in {"/read", "/ls"}:
            data_classes = (DataClass.LOCAL_CONTENT,)
        elif command == "/cd":
            data_classes = (DataClass.LOCAL_CONTENT, DataClass.USER_PROFILE)
    return ResolvedAction(
        name=name,
        target=target,
        target_scope=target_scope,
        effect_class=effect_class,
        capability_mask=capabilities,
        data_classes=data_classes,
        confirmation_mode=confirmation_mode,
        idempotency=idempotency,
        outcome_model=outcome_model,
        verification=verification,
        action_digest=_digest_action(name, args, target, snapshot_revision),
        snapshot_revision=snapshot_revision,
        compensation_action=compensation_action,
    )


def _target_for(name: str, args: dict[str, Any], *, cwd: str, target_scope: TargetScope) -> str:
    path_keys = _WORKSPACE_PATH_ARGUMENTS.get(name, ())
    for key in path_keys:
        raw = str(args.get(key) or "").strip()
        if raw:
            try:
                path = Path(raw)
                resolved = path.resolve() if path.is_absolute() else (Path(cwd) / path).resolve()
            except (OSError, RuntimeError, TypeError, ValueError):
                return "workspace:unresolved"
            return f"workspace:{resolved}"
    if name == "run_shell":
        return f"workspace:{Path(cwd).resolve()}"
    if name in {"web_fetch"}:
        return _canonical_origin(str(args.get("url") or ""))
    if target_scope is TargetScope.MODEL_STORE:
        model = str(args.get("name") or args.get("model") or args.get("source") or "*").strip()
        return f"model:{model or '*'}"
    if target_scope is TargetScope.EXTERNAL_ACCOUNT:
        account = str(args.get("account") or "default").strip()
        return f"external-account:{account or 'default'}"
    if target_scope is TargetScope.PROVIDER:
        return "provider:configured"
    if target_scope is TargetScope.MEMORY_STORE:
        return "memory-store:default"
    if target_scope is TargetScope.PLUGIN:
        plugin = str(args.get("plugin_name") or args.get("name") or "*").strip()
        return f"plugin:{plugin or '*'}"
    if target_scope is TargetScope.WORKSPACE:
        return f"workspace:{Path(cwd).resolve()}"
    return f"runtime:{name}"


def evaluate_action(
    action: ResolvedAction,
    *,
    grant: ConsentGrant | None,
    confirmation: ConfirmationReceipt | None,
    now: float,
    auto_approve: bool = False,
) -> PolicyDecision:
    """Evaluate an exact action without letting auto mode bypass protection."""

    policy = policy_for_action(action.name)
    if not policy.curated or action.effect_class is EffectClass.UNCLASSIFIED:
        return PolicyDecision(PolicyDisposition.DENY, "action has no curated authority policy", action)
    if action.target.endswith(":unresolved"):
        return PolicyDecision(PolicyDisposition.DENY, "action target could not be resolved", action)

    required = CapabilityMask(action.capability_mask)
    if grant is None or not grant.permits(action.name, required, action.target, now):
        if action.confirmation_mode is ConfirmationMode.HANDOFF_REQUIRED:
            return PolicyDecision(PolicyDisposition.HANDOFF, "trusted user handoff is required", action)
        if action.confirmation_mode in {
            ConfirmationMode.SESSION_PREAPPROVAL,
            ConfirmationMode.ACTION_TIME,
        }:
            return PolicyDecision(PolicyDisposition.CONFIRM, "a scoped capability grant is required", action)
        return PolicyDecision(PolicyDisposition.DENY, "no scoped capability grant authorizes the action", action)

    if action.confirmation_mode is ConfirmationMode.HANDOFF_REQUIRED:
        return PolicyDecision(
            PolicyDisposition.HANDOFF,
            "handoff-required actions cannot be approved by a runtime grant",
            action,
            grant_id=grant.grant_id,
        )

    if action.confirmation_mode is ConfirmationMode.ACTION_TIME:
        valid_confirmation = (
            confirmation is not None
            and confirmation.action_digest == action.action_digest
            and confirmation.confirmation_mode is ConfirmationMode.ACTION_TIME
            and confirmation.confirmed_at <= now < confirmation.expires_at
        )
        if not valid_confirmation:
            reason = "action-time confirmation is required"
            if auto_approve:
                reason += "; auto approval is not sufficient"
            return PolicyDecision(
                PolicyDisposition.CONFIRM,
                reason,
                action,
                grant_id=grant.grant_id,
            )

    return PolicyDecision(
        PolicyDisposition.ALLOW,
        "scoped grant and required confirmation are valid",
        action,
        grant_id=grant.grant_id,
        confirmation_receipt_id=confirmation.receipt_id if confirmation is not None else "",
    )


__all__ = [
    "PolicyDecision",
    "PolicyDisposition",
    "evaluate_action",
    "normalize_session_command",
    "resolve_action",
    "session_command_requires_approval",
]
