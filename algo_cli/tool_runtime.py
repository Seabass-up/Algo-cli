"""Tool execution, approval, attempt ledger, and reflection checkpoints."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ollama import Client

from .config import Config
from . import execution_guardrails
from . import reflex
from . import tools as tools_module
from .chat_protocol import get_attr
from .display import redact_tool_args, show_info, show_tool_call, show_tool_result, tool_execution_status
from .perf_telemetry import record_perf_event
from .runtime_qos import RuntimeHint, classify_tool_runtime
from .runtime_services import scoped_tool_runtime_env
from .tools import TOOL_MAP
from .tool_policy import RuntimeToolPolicyDecision, evaluate_runtime_tool_policy

ATTEMPT_LEDGER_LIMIT = 48
REFLECTION_RECENT_MESSAGES = 8
TOOL_RESULT_CONTENT_LIMIT = 20_000
FAILED_ATTEMPT_SKIP_SECONDS = 120.0
_SHELL_EXIT_CODE_RE = re.compile(r"\[exit code:\s*(-?\d+)\]", re.IGNORECASE)


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
        return self.policy.allowed and self.guardrail_allowed

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
        reason = "; ".join(reasons) or "runtime policy chain rejected the call"
        return f"Blocked by runtime policy chain: {reason}."


_SAFE_SESSION_COMMANDS = {
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
_SAFE_SESSION_STATUS_COMMANDS = {
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
_EMPTY_ARG_TOGGLES = {
    "/auto",
    "/cloud",
    "/cloudauto",
    "/safe",
    "/thinking",
    "/verify",
}


def session_command_requires_approval(command_line: str) -> bool:
    """Return whether a model-invoked slash command should prompt first."""
    stripped = (command_line or "").strip()
    if not stripped.startswith("/"):
        stripped = f"/{stripped}" if stripped else ""
    if not stripped:
        return True
    parts = stripped.split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if command in _SAFE_SESSION_COMMANDS:
        return False
    if command in {"/read", "/ls", "/cwd"}:
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
    if command in _SAFE_SESSION_STATUS_COMMANDS and arg in {"", "status", "show", "?", "guide", "help"}:
        return False
    if command == "/x-account" and arg == "status":
        return False
    return True


def ask_approval(name: str, args: dict[str, Any], cfg: Config, *, force: bool = False) -> bool:
    from .display import console

    command_line = str(args.get("command") or "")
    model_cd = name in {"session_command", "session_slash"} and (
        command_line.strip().lower() == "/cd"
        or command_line.strip().lower().startswith("/cd ")
    )
    if cfg.auto_approve_active and not force and not model_cd:
        return True
    from .action_registry import action_requires_approval

    dangerous = model_cd or action_requires_approval(name) or (
        name == "session_command"
        and session_command_requires_approval(str(args.get("command") or ""))
    )
    if not dangerous:
        return True
    console.print(f"[yellow]Approve {name}?[/] Use y, n, or a to approve all this session.")
    console.print(json.dumps(redact_tool_args(name, args), indent=2))
    try:
        approval = input("Approve? [y/N/a] ").strip().lower()
    except EOFError:
        # No stdin available (e.g., in tests or non-interactive mode) - deny by default
        console.print("[red]No input available, denying operation.[/]")
        return False
    if approval == "a":
        # Session-only: cfg.save() never persists this flag, unlike /auto.
        cfg.session_auto_approve = True
        return True
    return approval == "y"


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
) -> RuntimeToolPreflight:
    """Evaluate and record the policy/QoS preflight used by every chat path."""

    signature_args = tool_runtime_args(name, args, cfg)
    runtime_hint = classify_tool_runtime(name, signature_args)
    guardrail_reasons: list[str] = []
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
        status="pass" if preflight.allowed else "blocked",
        tier=preflight.policy.tier,
        capability_mask=preflight.policy.capability_mask,
        capabilities=list(preflight.policy.capability_names),
        fired_rules=list(preflight.policy.fired_rules),
        guardrail_reasons=list(preflight.guardrail_reasons),
    )
    return preflight


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
    safe_args = redact_tool_args(name, args)
    try:
        encoded = json.dumps(safe_args, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":"))
    except TypeError:
        encoded = str(safe_args)
    return f"{name}:{encoded}"


def find_failed_attempt(cfg: Config, signature: str) -> dict[str, Any] | None:
    now = time.time()
    for item in reversed(cfg.attempt_ledger):
        if item.get("signature") != signature:
            continue
        status = item.get("status")
        if status == "skipped":
            return None
        if status != "failed":
            return None
        try:
            age = now - float(item.get("timestamp") or 0)
        except (TypeError, ValueError):
            age = 0.0
        if age <= FAILED_ATTEMPT_SKIP_SECONDS:
            return item
        return None
    return None


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


def classify_tool_status(result: str, *, approved: bool = True, skipped: bool = False) -> str:
    if skipped:
        return "skipped"
    if not approved:
        return "denied"
    lowered = str(result).strip().lower()
    if lowered.startswith(("error:", "tool error", "tool argument error", "unknown tool")):
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


def record_tool_attempt(
    cfg: Config,
    *,
    name: str,
    args: dict[str, Any],
    result: str,
    status: str,
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
    signature = tool_attempt_signature(name, args)
    if workspace_changed:
        # A workspace mutation invalidates cached failures: the exact same
        # test/check command is often the correct next action after a fix.
        cfg.attempt_ledger = [
            item for item in cfg.attempt_ledger if item.get("status") not in {"failed", "skipped"}
        ]
    args_preview = json.dumps(
        redact_tool_args(name, args),
        sort_keys=True,
        ensure_ascii=True,
        default=str,
    )[:100]
    cfg.attempt_ledger.append(
        {
            "timestamp": time.time(),
            "signature": signature,
            "tool": name,
            "args_preview": args_preview,
            "status": status,
            "summary": summarize_tool_result(result),
        }
    )
    cfg.attempt_ledger = cfg.attempt_ledger[-ATTEMPT_LEDGER_LIMIT:]


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
) -> tuple[dict[str, Any], str]:
    show_tool_call(name, args)
    preflight = preflight_runtime_tool(name, args, cfg)
    signature_args = preflight.signature_args
    qos_fields = preflight.qos_fields
    if not preflight.allowed:
        result = preflight.blocked_result
        show_tool_result(name, result, approved=False)
        record_tool_attempt(cfg, name=name, args=signature_args, result=result, status="denied")
        record_perf_event("tool", tool=name, status="denied", duration_ms=0.0, **qos_fields)
        return tool_result_message(name, result, tool_call_id), result
    signature = tool_attempt_signature(name, signature_args)
    previous_failure = find_failed_attempt(cfg, signature)
    if previous_failure:
        result = (
            "Skipped repeated failed attempt. "
            f"Prior outcome: {previous_failure.get('summary', 'same tool path already failed or was denied')}."
        )
        show_tool_result(name, result, approved=False)
        record_tool_attempt(cfg, name=name, args=signature_args, result=result, status="skipped")
        record_perf_event("tool", tool=name, status="skipped", duration_ms=0.0, **qos_fields)
        return tool_result_message(name, result, tool_call_id), result
    if not ask_approval(name, args, cfg, force=force_approval):
        result = "User denied this operation."
        show_tool_result(name, result, approved=False)
        record_tool_attempt(cfg, name=name, args=signature_args, result=result, status="denied")
        record_perf_event("tool", tool=name, status="denied", duration_ms=0.0, **qos_fields)
        return tool_result_message(name, result, tool_call_id), result

    started = time.perf_counter()
    with scoped_tool_runtime_env(cfg):
        with tool_execution_status(
            f"[muted]executing {name} · {preflight.runtime_hint.spawn_class.value}...[/]"
        ):
            result = run_tool(name, args, cfg)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    status = classify_tool_status(result)
    result = augment_tool_result_with_reflex(cfg, name, signature_args, result, status)
    show_tool_result(name, result, duration_ms=duration_ms)
    record_tool_attempt(cfg, name=name, args=signature_args, result=result, status=status)
    record_perf_event("tool", tool=name, status=status, duration_ms=duration_ms, **qos_fields)
    return tool_result_message(name, result, tool_call_id), result
