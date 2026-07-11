"""Agent block execution, required-change contracts, recovery, and pipelines."""

from __future__ import annotations

import copy
import logging
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from rich import box
from rich.table import Table
from rich.text import Text

from . import agent_blocks
from . import agent_threads
from . import spawn_budget
from . import git_evidence
from . import execution_guardrails
from . import harness
from . import inference_harness
from . import memory_runtime
from . import reflex
from . import task_router
from . import tool_policy
from . import model_info as _model_info_module
from . import tools as tools_module
from .chat_protocol import (
    collapse_tool_history_for_gemini,
    get_attr,
    normalize_tool_call,
    serialize_tool_call,
)
from .config import Config
from .display import (
    console,
    finish_thinking_block,
    show_agent_block_complete,
    show_agent_block_start,
    show_agent_pipeline_complete,
    show_agent_recovery_start,
    show_error,
    show_info,
    show_recalled_context,
    show_thinking_text,
    show_tool_result,
)
from .perf_telemetry import flush_perf_records, record_chat_metrics, record_perf_event
from .runtime_services import client_for_model, create_client
from .tool_runtime import (
    execute_tool_call_for_pipeline,
    record_tool_attempt,
    summarize_tool_result,
    tool_result_message,
)

TOOL_MAP = tools_module.TOOL_MAP
logger = logging.getLogger(__name__)


@dataclass
class AgentRunResult:
    """Bounded result returned to the CLI or the parent runtime agent."""

    thread_id: str = ""
    status: str = "failed"
    pipeline: str = "default"
    output: str = ""
    error: str = ""
    children: list[str] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)

    def for_tool(self) -> str:
        lines = [
            f"Agent thread {self.thread_id or '-'}: {self.status}",
            f"Pipeline: {self.pipeline}",
        ]
        if self.children:
            lines.append(f"Child threads: {', '.join(self.children)}")
        if self.error:
            lines.append(f"Error: {self.error}")
        if self.blocks:
            block_text = ", ".join(
                f"{item.get('role', '?')}={item.get('status', '?')}" for item in self.blocks
            )
            lines.append(f"Blocks: {block_text}")
        if self.output:
            lines.append(f"Output:\n{self.output[:12_000]}")
        return "\n".join(lines)


_execution_state = threading.local()


def agent_execution_active() -> bool:
    """Prevent recursive /agent calls while an Agent Blocks run is active."""

    return bool(getattr(_execution_state, "depth", 0))


@contextmanager
def _agent_execution_scope():
    depth = int(getattr(_execution_state, "depth", 0))
    _execution_state.depth = depth + 1
    try:
        yield
    finally:
        _execution_state.depth = depth


def run_agent_block(
    block: agent_blocks.AgentBlock,
    *,
    task: str,
    completed: list[agent_blocks.AgentBlock],
    cfg: Config,
    client: Any,
    route: task_router.TaskRoute | None = None,
    completion_check: Callable[[agent_blocks.AgentBlock], None] | None = None,
) -> None:
    block.status = "running"
    block.status_code = ""
    block.status_reason = ""
    policy = tool_policy.compute_policy(
        route or task_router.route_task(task),
        block.role,
        block.allowed_tools,
        cfg.safe_mode,
        cfg.auto_approve_active,
    )
    runtime_tool_names = policy.allowed_tools if cfg.algorithmic_tool_policy_enabled else block.allowed_tools
    allowed_tools = [
        TOOL_MAP[name]
        for name in sorted(runtime_tool_names)
        if name in TOOL_MAP
    ]
    block_model = block.model or cfg.model
    block_client = client_for_model(block_model, cfg, client)
    if block_client is client and block_model != cfg.model:
        block_model = cfg.model
    policy_summary = tool_policy.format_policy_summary(policy)
    if block.requires_change:
        policy_summary += "; file edits: write_file only"
    show_agent_block_start(
        block.role,
        block_model,
        len(allowed_tools),
        policy_summary=policy_summary,
        policy_enforced=cfg.algorithmic_tool_policy_enabled,
        cwd=cfg.cwd,
    )
    started = time.perf_counter()
    from . import session_commands

    mercury = harness.resolve_mercury_stop_conditions(
        user_message=task,
        session_mode=cfg.session_mode,
        include_external=cfg.external_harness_sources_enabled,
    )
    system_parts = [block.prompt]
    if inference_harness.should_inject(task):
        system_parts.append(f"\n\n{inference_harness.context_block()}")
    if block.requires_change:
        system_parts.append(agent_blocks.REQUIRED_CHANGE_PROMPT)
    system_parts.append(
        "\n\n## Session Workspace\n"
        "Relative tool paths resolve from the active session workspace. Use path '.' for its root; "
        "do not guess or disclose the absolute workspace path.\n"
        f"{session_commands.catalog_for_prompt()}"
    )
    if mercury:
        system_parts.append(f"\n\n## Mercury gates\n{mercury}")
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "".join(system_parts),
        },
        {"role": "user", "content": agent_blocks.pipeline_context(task, completed)},
    ]
    block.messages = messages
    completion_nudged = False

    def finish_with_partial_output() -> None:
        messages.append(
            {
                "role": "user",
                "content": (
                    "The block has reached its tool-iteration budget. Do not call any more tools. "
                    "Produce a partial but useful ## Block Output summary from evidence gathered so far. "
                    "State any incomplete checks explicitly."
                ),
            }
        )
        partial_text = ""
        try:
            stream = block_client.chat(
                model=block_model,
                messages=messages,
                tools=[],
                stream=True,
                think=cfg.show_thinking,
                keep_alive=cfg.keep_alive,
                options={"temperature": cfg.temperature, "num_ctx": cfg.num_ctx},
            )
            for chunk in stream:
                record_chat_metrics(cfg, chunk)
                message = get_attr(chunk, "message", {})
                thinking = get_attr(message, "thinking", "")
                content = get_attr(message, "content", "")
                if thinking and cfg.show_thinking:
                    show_thinking_text(thinking)
                if content:
                    finish_thinking_block()
                    partial_text += content
        except Exception as exc:
            logger.debug("Agent block partial wrap-up failed for %s: %s", block.role, exc)
        finally:
            finish_thinking_block()
        if partial_text.strip():
            block.output = partial_text.strip()
        elif not block.output.strip():
            block.output = "## Block Output\n\nPartial review: tool budget reached before a written summary was produced."
        block.status = "partial"
        block.status_code = "max_iterations"
        block.status_reason = (
            f"Iteration budget exhausted after {max(1, int(block.max_iterations))} cycles; "
            "showing a tool-free partial summary."
        )

    try:
        execution_scope = execution_guardrails.begin_execution_scope(cfg.cwd)
    except execution_guardrails.ExecutionGuardrailError as exc:
        block.status = "failed"
        block.status_code = "unsafe_workspace"
        block.status_reason = f"Cannot start a safe execution scope: {exc}"
        block.output = block.status_reason
        block.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        show_agent_block_complete(
            block.role,
            block.output,
            duration_ms=block.duration_ms,
            tool_calls=block.tool_calls,
            status=block.status,
            status_reason=block.status_reason,
            status_code=block.status_code,
            model=block_model if block.model else "",
            policy_summary=policy_summary,
        )
        return

    try:
        for _ in range(max(1, int(block.max_iterations))):
            request_messages = messages
            if _model_info_module.is_gemini_model(block_model):
                request_messages = collapse_tool_history_for_gemini(request_messages)
            stream = block_client.chat(
                model=block_model,
                messages=request_messages,
                tools=allowed_tools,
                stream=True,
                think=cfg.show_thinking,
                keep_alive=cfg.keep_alive,
                options={"temperature": cfg.temperature, "num_ctx": cfg.num_ctx},
            )
            thinking_text = ""
            content_text = ""
            tool_calls: list[Any] = []
            try:
                for chunk in stream:
                    record_chat_metrics(cfg, chunk)
                    message = get_attr(chunk, "message", {})
                    thinking = get_attr(message, "thinking", "")
                    content = get_attr(message, "content", "")
                    calls = get_attr(message, "tool_calls", None)
                    if thinking and cfg.show_thinking:
                        show_thinking_text(thinking)
                        thinking_text += thinking
                    if content:
                        finish_thinking_block()
                        content_text += content
                    if calls:
                        tool_calls.extend(calls)
            finally:
                finish_thinking_block()

            assistant: dict[str, Any] = {"role": "assistant"}
            if content_text:
                assistant["content"] = content_text
                block.output = content_text
            if thinking_text:
                assistant["thinking"] = thinking_text
            serialized_calls = [serialize_tool_call(call) for call in tool_calls]
            if tool_calls:
                assistant["tool_calls"] = serialized_calls
            messages.append(assistant)

            if not tool_calls:
                completion = execution_guardrails.completion_decision()
                if not completion.allowed:
                    if not completion_nudged and _ + 1 < max(1, int(block.max_iterations)):
                        completion_nudged = True
                        block.output = ""
                        show_info(
                            f"{block.role} completion deferred until a post-mutation verifier passes."
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "[Internal completion gate] The last workspace mutation is not "
                                    "verified. Run one appropriate non-mutating test, lint/type check, "
                                    "or git_diff tool now. Then provide a final ## Block Output grounded "
                                    "in that verifier."
                                ),
                            }
                        )
                        continue
                    block.status = "partial"
                    block.status_code = "verification_missing"
                    block.status_reason = (
                        "The block stopped without a successful test, lint/type check, or git diff "
                        "after its last workspace mutation."
                    )
                    block.verification_warning = block.status_reason
                    block.output = f"## Block Output\n\nUNVERIFIED: {block.status_reason}"
                elif block.output.strip():
                    block.status = "complete"
                else:
                    block.status = "failed"
                    block.status_code = "model_error"
                    block.status_reason = "Block returned neither tool calls nor a usable output."
                    block.output = "(no output produced)"
                break

            policy_denied_batch = False
            for call, serialized_call in zip(tool_calls, serialized_calls):
                name, args = normalize_tool_call(call)
                if policy_denied_batch or name not in runtime_tool_names:
                    if name not in runtime_tool_names:
                        result = f"Tool not allowed in {block.role} block: {name}"
                        block.status_reason = f"Tool policy violation: {name} is not allowed in the {block.role} block."
                    else:
                        result = "Skipped because another tool call in this assistant message violated block policy."
                    show_tool_result(name, result, approved=False)
                    messages.append(tool_result_message(name, result, str(serialized_call.get("id") or "") or None))
                    block.status = "failed"
                    block.status_code = "policy_denied"
                    if not block.output:
                        block.output = result
                    policy_denied_batch = True
                    continue
                block.tool_calls += 1
                shell_decision = tool_policy.evaluate_shell_command(
                    str(args.get("command", "")),
                    requires_change=(block.requires_change and name == "run_shell"),
                    safe_mode=cfg.safe_mode,
                )
                if shell_decision.blocked:
                    result = (
                        "Blocked by required-change policy in safe mode: "
                        f"{shell_decision.reason}."
                    )
                    show_tool_result(name, result, approved=False)
                    record_tool_attempt(cfg, name=name, args=args, result=result, status="denied")
                    record_perf_event("tool", tool=name, status="denied", duration_ms=0.0)
                    messages.append(tool_result_message(name, result, str(serialized_call.get("id") or "") or None))
                    block.mutation_denied = True
                    continue
                tool_message, _result = execute_tool_call_for_pipeline(
                    name,
                    args,
                    cfg,
                    tool_call_id=str(serialized_call.get("id") or "") or None,
                    force_approval=tool_policy.requires_explicit_approval(
                        name,
                        block_policy=policy,
                        shell_decision=shell_decision,
                        policy_enforced=cfg.algorithmic_tool_policy_enabled,
                    ),
                )
                mutation_action = tool_policy.describes_mutation_action(name, args)
                mutation_succeeded = (
                    (name == "write_file" and str(_result).lstrip().startswith("Wrote "))
                    or (name == "edit_file" and str(_result).lstrip().startswith("Edited "))
                    or (name == "batch_edit" and str(_result).lstrip().startswith("Batch-edited "))
                )
                if mutation_succeeded:
                    written_path = str(args.get("path", "")).strip()
                    if written_path and written_path not in block.successful_writes:
                        block.successful_writes.append(written_path)
                    if mutation_action and mutation_action not in block.mutation_actions:
                        block.mutation_actions.append(mutation_action)
                elif name in {"write_file", "edit_file", "batch_edit"}:
                    lowered_result = str(_result).strip().lower()
                    if lowered_result.startswith("user denied"):
                        block.mutation_denied = True
                    elif lowered_result.startswith(("error", "tool error", "tool argument error")):
                        block.failed_writes.append(summarize_tool_result(str(_result)))
                elif (
                    name == "run_shell"
                    and mutation_action
                    and not str(_result).startswith(("User denied", "Skipped repeated", "Blocked"))
                    and mutation_action not in block.mutation_actions
                ):
                    block.mutation_actions.append(mutation_action)
                messages.append(tool_message)
            if block.status == "failed":
                break
        else:
            finish_with_partial_output()
    finally:
        completion_error = ""
        try:
            if completion_check is not None:
                completion_check(block)
        except Exception as exc:
            completion_error = f"Completion check failed: {type(exc).__name__}: {exc}"
            block.status = "failed"
            block.status_code = "completion_check_error"
            block.status_reason = completion_error
        try:
            execution_guardrails.end_execution_scope(execution_scope)
        except execution_guardrails.ExecutionGuardrailError as exc:
            block.status = "failed"
            block.status_code = "guardrail_scope_error"
            block.status_reason = f"Execution evidence scope failed to close: {exc}"
        block.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        show_agent_block_complete(
            block.role,
            block.output,
            duration_ms=block.duration_ms,
            tool_calls=block.tool_calls,
            status=block.status,
            status_reason=block.status_reason,
            verification_warning=block.verification_warning,
            status_code=block.status_code,
            model=block_model if block.model else "",
            policy_summary=policy_summary,
            successful_writes=list(block.successful_writes),
        )


AGENT_USAGE = "Usage: /agent [--pipeline NAME] <task>"
AGENT_TEAM_USAGE = "Usage: /agent team [--roles ROLE,ROLE[,ROLE,ROLE]] <task>"
AGENT_THREAD_USAGE = "Usage: /agent show THREAD | resume THREAD [task] | fork THREAD <task>"
MIN_TEAM_ROLES = 2
MAX_TEAM_ROLES = 4


def agent_usage_text() -> str:
    return (
        f"{AGENT_USAGE}\n"
        f"{AGENT_TEAM_USAGE}\n"
        f"{AGENT_THREAD_USAGE}\n"
        "Thread commands: /agent threads | show THREAD | resume THREAD [task] | fork THREAD <task>\n"
        f"Available pipelines: {', '.join(agent_blocks.pipeline_names())}\n"
        "Examples:\n"
        "  /agent --pipeline code-change Fix the failing tests\n"
        "  /agent team --roles scout,critic,verifier Review the current worktree\n"
        "  /agent resume 7d12a9 Finish the remaining verification"
    )


def _normalize_team_role(role: str) -> str:
    cleaned = "-".join(role.strip().lower().split())
    if not cleaned or len(cleaned) > 32:
        return ""
    if not all(char.isalnum() or char in {"-", "_"} for char in cleaned):
        return ""
    return cleaned


def default_team_roles(route: task_router.TaskRoute) -> list[str]:
    if route.task_type == "coding":
        return ["code-scout", "solution-designer", "risk-verifier"]
    if route.task_type == "review":
        return ["correctness-reviewer", "security-reviewer", "test-reviewer"]
    if route.task_type == "research":
        return ["source-scout", "counterpoint", "fact-checker"]
    return ["planner", "analyst", "critic"]


def parse_agent_team_invocation(arg: str) -> tuple[list[str], str, str]:
    """Parse the portion after `/agent team`."""

    try:
        parts = shlex.split((arg or "").strip())
    except ValueError as exc:
        return [], "", f"{AGENT_TEAM_USAGE} ({exc})"
    roles: list[str] = []
    task_parts: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if part == "--roles":
            if index + 1 >= len(parts):
                return [], "", AGENT_TEAM_USAGE
            raw_roles = parts[index + 1]
            index += 2
        elif part.startswith("--roles="):
            raw_roles = part.split("=", 1)[1]
            index += 1
        elif part.startswith("--"):
            return [], "", f"Unknown team option '{part}'. {AGENT_TEAM_USAGE}"
        else:
            task_parts.append(part)
            index += 1
            continue
        roles = [_normalize_team_role(item) for item in raw_roles.split(",")]
        if not all(roles):
            return [], "", "Team roles must be short names using letters, numbers, '-' or '_'."
    task = " ".join(task_parts).strip()
    if not task:
        return [], "", AGENT_TEAM_USAGE
    if roles:
        if not MIN_TEAM_ROLES <= len(roles) <= MAX_TEAM_ROLES:
            return [], "", f"Team runs require {MIN_TEAM_ROLES}-{MAX_TEAM_ROLES} roles."
        if len(set(roles)) != len(roles):
            return [], "", "Team roles must be unique."
    return roles, task, ""


def parse_agent_invocation_checked(arg: str) -> tuple[str, str, str]:
    """Return (pipeline_name, task, error) from /agent arguments."""
    text = (arg or "").strip()
    if not text:
        return "default", "", ""
    try:
        parts = shlex.split(text)
    except ValueError:
        return "default", text, ""
    if len(parts) >= 3 and parts[0] == "--pipeline":
        pipeline = parts[1]
        task = " ".join(parts[2:]).strip()
        return pipeline, task, ""
    if len(parts) >= 2 and parts[0].startswith("--pipeline="):
        pipeline = parts[0].split("=", 1)[1]
        task = " ".join(parts[1:]).strip()
        if not pipeline.strip() or not task:
            return "default", "", AGENT_USAGE
        return pipeline, task, ""
    if parts and parts[0].startswith("--pipeline"):
        return "default", "", AGENT_USAGE
    return "default", text, ""


def parse_agent_invocation(arg: str) -> tuple[str, str]:
    """Return (pipeline_name, task) from /agent arguments."""
    pipeline, task, _error = parse_agent_invocation_checked(arg)
    return pipeline, task


def resolve_pipeline_for_cli(name: str) -> tuple[list[agent_blocks.AgentBlock], str] | None:
    try:
        return agent_blocks.resolve_pipeline(name)
    except agent_blocks.BlocksConfigError as exc:
        show_error(str(exc))
        try:
            pipeline = agent_blocks.builtin_pipeline_by_name(name)
        except ValueError as builtin_exc:
            show_error(str(builtin_exc))
            return None
        show_info(f"Using built-in '{name}' pipeline instead.")
        return pipeline, "built-in fallback"
    except ValueError as exc:
        show_error(str(exc))
        return None


def show_task_route(route: task_router.TaskRoute, cfg: Config, prompt: str = "") -> None:
    budget = spawn_budget.compute_budget(route, prompt)
    table = Table(title="Task Route", box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("Field", style="muted")
    table.add_column("Value", style="text")
    table.add_row("type", route.task_type)
    table.add_row("complexity", route.complexity)
    table.add_row("mode", route.recommended_mode)
    table.add_row("pipeline", route.suggested_pipeline)
    table.add_row("tool groups", ", ".join(route.allowed_tool_groups) or "-")
    table.add_row("risk", route.risk)
    table.add_row("reason", route.reason)
    table.add_row(
        "budget",
        (
            f"{budget.max_blocks} blocks, {budget.max_iterations_per_block} iterations/block, "
            f"parallelism: {spawn_budget.parallelism_label(budget.parallelism)}"
            if budget.max_blocks
            else "chat/user-directed only"
        ),
    )
    resolved = resolve_pipeline_for_cli(route.suggested_pipeline)
    if resolved is None:
        console.print(table)
        return
    pipeline, pipeline_source = resolved
    table.add_row("pipeline source", pipeline_source)
    console.print(table)
    policy_table = Table(
        title=f"Advisory Tool Policy - {route.suggested_pipeline}",
        box=box.SIMPLE,
        padding=(0, 1),
    )
    policy_table.add_column("Block", style="primary")
    policy_table.add_column("Effective tools", style="text", overflow="fold")
    policy_table.add_column("Approval", style="warning", overflow="fold")
    policy_table.add_column("Denied", style="muted", overflow="fold")
    for block in pipeline:
        decision = tool_policy.compute_policy(
            route,
            block.role,
            block.allowed_tools,
            cfg.safe_mode,
            cfg.auto_approve_active,
        )
        policy_table.add_row(
            block.role,
            ", ".join(sorted(decision.allowed_tools)) or "-",
            ", ".join(sorted(decision.approval_required)) or "-",
            ", ".join(sorted(decision.denied_tools)) or "-",
        )
    console.print(policy_table)
    pipeline_blocks = len(pipeline)
    if budget.max_blocks and pipeline_blocks > budget.max_blocks:
        show_info(
            f"Budget recommends at most {budget.max_blocks} blocks; "
            f"pipeline {route.suggested_pipeline} defines {pipeline_blocks}. Execution remains unchanged."
        )
    over_iteration_roles = [
        block.role
        for block in pipeline
        if budget.max_iterations_per_block and block.max_iterations > budget.max_iterations_per_block
    ]
    if over_iteration_roles:
        show_info(
            f"Budget recommends at most {budget.max_iterations_per_block} iterations/block; "
            f"blocks exceeding it: {', '.join(over_iteration_roles)}. Execution remains unchanged."
        )
    for reason in budget.reasons:
        show_info(f"Budget: {reason}")
    if cfg.algorithmic_tool_policy_enabled:
        show_info("Policy enforcement is ON; Agent Block tool sets use this policy.")
    else:
        show_info("Policy preview is advisory; Agent Block execution tools are unchanged.")
    if route.recommended_mode == "agent":
        suffix = f" {prompt.strip()}" if prompt.strip() else " <task>"
        show_info(f"Suggested command: /agent --pipeline {route.suggested_pipeline}{suffix}")
    elif route.risk == "high":
        show_info("High-risk task: review the action before running tools or Agent Blocks.")


def maybe_show_route_suggestion(user_message: str) -> None:
    route = task_router.route_task(user_message)
    if not task_router.should_suggest(route):
        return
    if route.recommended_mode == "agent":
        show_info(
            "Route suggestion: "
            f"{route.task_type} task -> /agent --pipeline {route.suggested_pipeline} "
            f"(reason: {route.reason})"
        )
    elif route.risk == "high":
        show_info(f"Route warning: high-risk task detected ({route.reason})")


def enforce_required_change_contract(
    block: agent_blocks.AgentBlock,
    before: git_evidence.GitSnapshot,
    after: git_evidence.GitSnapshot,
) -> None:
    """Prevent change-producing blocks from completing without final evidence."""

    block.git_evidence = git_evidence.format_git_evidence(before, after)
    if not block.requires_change or block.status != "complete":
        return

    # Git evidence is the strict path; when it is unavailable but recorded
    # write_file evidence exists, the contract is satisfied with a manual-
    # verification notice rather than a partial downgrade. Returning here
    # preserves block.status == "complete" and skips the produced_change gate.
    if (not before.available or not after.available) and block.successful_writes:
        block.verification_warning = (
            "Git verification was unavailable. Successful write_file operations were recorded, "
            "but review must manually confirm the written files."
        )
        return

    produced_change = git_evidence.has_verified_delta(before, after) or (
        bool(block.successful_writes) and git_evidence.has_observed_delta(before, after)
    )
    if produced_change:
        return

    reported_output = block.output.strip()
    block.status = "partial"
    if block.mutation_denied:
        block.status_code = "policy_denied"
        block.status_reason = "Required change not verified: a requested mutation was denied or blocked by policy."
    elif block.failed_writes:
        block.status_code = "write_blocked"
        block.status_reason = "Required change not verified: write_file was attempted but failed before producing a verified change."
    elif not before.available or not after.available:
        block.status_code = "no_write_evidence"
        block.status_reason = "Required change not verified: Git evidence is unavailable and no successful write_file action was recorded."
    elif before.head != after.head:
        block.status_code = "attribution_unsafe"
        block.status_reason = "Required change not verified: repository HEAD changed during execution, so attribution is unsafe."
    elif block.successful_writes:
        block.status_code = "no_verified_delta"
        block.status_reason = "Required change not verified: recorded writes left no attributable final-state Git delta."
    else:
        block.status_code = "no_write_evidence"
        block.status_reason = "Required change not verified: no successful write_file action or attributable Git delta was detected."
    block.output = (
        "## Block Output\n\n"
        "No verified code change was produced. "
        "No successful write_file operation with a remaining final-state delta or attributable Git delta was detected."
    )
    if reported_output:
        block.output += f"\n\nUnverified reported output:\n{reported_output}"


def capture_optional_mutation_audit(
    block: agent_blocks.AgentBlock,
    before: git_evidence.GitSnapshot,
    after: git_evidence.GitSnapshot,
) -> None:
    """Report unexpected mutation actions by blocks without a change contract."""

    if block.requires_change or not block.mutation_actions:
        return
    actions = "\n".join(f"- {action}" for action in block.mutation_actions)
    block.audit_evidence = (
        "Audit notice: a block without requires_change executed mutation-capable actions.\n"
        f"Actions:\n{actions}\n\n"
        f"{git_evidence.format_git_evidence(before, after)}"
    )
    show_info(f"Mutation audit: {block.role} executed mutation-capable actions; evidence was captured for review.")


RECOVERABLE_IMPLEMENT_CODES = frozenset({"max_iterations", "no_write_evidence", "write_blocked", "no_verified_delta"})
MAX_RECOVERY_IMPLEMENT_ITERATIONS = 8


def should_recover_implementation(block: agent_blocks.AgentBlock) -> bool:
    """Return whether a partial implementation should receive one focused retry."""

    return (
        block.requires_change
        and block.status == "partial"
        and block.status_code in RECOVERABLE_IMPLEMENT_CODES
        and not block.mutation_denied
        and not (block.status_code == "max_iterations" and block.successful_writes)
    )


def recovery_plan_block(failed_block: agent_blocks.AgentBlock, cfg: Config) -> agent_blocks.AgentBlock:
    recent_attempts = cfg.attempt_ledger[-6:]
    attempt_lines = [
        f"- {item.get('status', '?').upper()} {item.get('tool', '?')}: {item.get('summary', '')}"
        for item in recent_attempts
    ]
    attempt_context = "\n".join(attempt_lines) if attempt_lines else "- (no recent tool attempts recorded)"
    return agent_blocks.AgentBlock(
        role="recovery-plan",
        model=failed_block.model,
        max_iterations=1,
        prompt=(
            "You are a focused recovery planner. The previous required-change implementation did not complete. "
            "Use only the provided failure output, status, write evidence, Git evidence, and attempt ledger context. "
            "Do not call tools. Produce a concise corrected execution plan for a single retry, prioritizing the "
            "specific write_file call(s) and minimal verification needed. Return Markdown beginning with "
            "## Block Output.\n\nRecent tool-attempt summary:\n"
            f"{attempt_context}"
        ),
    )


def retry_implementation_block(failed_block: agent_blocks.AgentBlock) -> agent_blocks.AgentBlock:
    return agent_blocks.AgentBlock(
        role="implement-retry",
        prompt=(
            f"{failed_block.prompt}\n\n"
            "This is the only recovery retry. Follow the recovery-plan output already in context. "
            "Avoid repeating broad exploration; execute the targeted write_file edits and focused verification."
        ),
        allowed_tools=failed_block.allowed_tools,
        model=failed_block.model,
        max_iterations=min(MAX_RECOVERY_IMPLEMENT_ITERATIONS, max(1, failed_block.max_iterations)),
        requires_change=True,
    )


# Session-scoped buffer holding the most recent pipeline's completed blocks.
# Surfaced by `/diff` and `/changes`. Cleared on `/clear`, overwritten on each
# new pipeline run. Not persisted to disk — purely in-process state.
_session_pipeline_blocks: list[agent_blocks.AgentBlock] = []


def session_pipeline_blocks() -> list[agent_blocks.AgentBlock]:
    return _session_pipeline_blocks


def clear_session_pipeline_blocks() -> None:
    _session_pipeline_blocks.clear()


def resolve_agent_workspace(task: str, cfg: Config) -> bool:
    """Point cfg.cwd at a recognized project root inferred from the task."""
    from . import workspace_resolver

    if workspace_resolver.resolve_agent_workspace(task, cfg):
        show_info(f"Agent workspace set to {cfg.cwd}")
        return True
    return False


def _block_record(block: agent_blocks.AgentBlock) -> dict[str, Any]:
    return {
        "role": block.role,
        "status": block.status,
        "status_code": block.status_code,
        "status_reason": block.status_reason,
        "tool_calls": block.tool_calls,
        "duration_ms": block.duration_ms,
        "successful_writes": list(block.successful_writes),
        "verification_warning": block.verification_warning,
    }


def _start_thread_record(
    task: str,
    cfg: Config,
    pipeline_name: str,
    *,
    thread_id: str | None,
    parent_id: str,
) -> str:
    try:
        if thread_id:
            agent_threads.begin_turn(thread_id, task, pipeline=pipeline_name, model=cfg.model)
            return thread_id
        record = agent_threads.create_thread(
            task,
            pipeline=pipeline_name,
            model=cfg.model,
            parent_id=parent_id,
            status="queued",
            start_turn=True,
        )
        return str(record["id"])
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Agent thread persistence unavailable: %s", exc)
        show_info(f"Agent thread history unavailable for this run: {exc}")
        return ""


def _finish_thread_record(
    thread_id: str,
    *,
    status: str,
    output: str,
    error: str,
    blocks: list[dict[str, Any]],
    pipeline: str,
) -> None:
    if not thread_id:
        return
    try:
        agent_threads.finish_turn(
            thread_id,
            status=status,
            output=output,
            error=error,
            blocks=blocks,
            pipeline=pipeline,
        )
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Could not finish agent thread record %s: %s", thread_id, exc)


def run_agent_pipeline(
    task: str,
    cfg: Config,
    client: Any,
    pipeline_name: str = "default",
    *,
    thread_id: str | None = None,
    parent_id: str = "",
    prior_context: str = "",
    thread_pipeline_label: str | None = None,
) -> AgentRunResult:
    if not task.strip():
        show_error(AGENT_USAGE)
        return AgentRunResult(status="failed", pipeline=pipeline_name, error=AGENT_USAGE)
    reflex.begin_agent_pipeline(cfg)
    resolve_agent_workspace(task, cfg)
    started = time.perf_counter()
    completed: list[agent_blocks.AgentBlock] = []
    resolved = resolve_pipeline_for_cli(pipeline_name)
    if resolved is None:
        return AgentRunResult(status="failed", pipeline=pipeline_name, error=f"Pipeline '{pipeline_name}' is unavailable.")
    pipeline, _pipeline_source = resolved
    record_pipeline = thread_pipeline_label or pipeline_name
    active_thread_id = _start_thread_record(
        task,
        cfg,
        record_pipeline,
        thread_id=thread_id,
        parent_id=parent_id,
    )
    if active_thread_id:
        show_info(f"Agent thread {active_thread_id} · {record_pipeline}")
    route = task_router.route_task(task)
    pipeline_task = task
    if prior_context.strip():
        pipeline_task = (
            f"{task}\n\n## Parent Thread Handoff\n"
            "Treat this as bounded evidence from independent specialist threads. "
            "Verify consequential claims before acting.\n\n"
            f"{prior_context.strip()[:24_000]}"
        )
    from . import main as _main

    engine = _main._intuition_engine
    if engine is not None and cfg.intuition_recall_enabled:
        try:
            recalled_blocks = engine.recall(
                task,
                enabled=True,
                embed_fn=_main.intuition_embed_fn(cfg),
            )
            if recalled_blocks:
                show_recalled_context(recalled_blocks)
                injection = engine.format_for_injection(recalled_blocks)
                if injection:
                    pipeline_task = f"{pipeline_task}\n\n{injection}"
        except Exception as exc:
            logger.debug("Agent pipeline intuition recall failed: %s", exc)

    def run_pipeline_block(block: agent_blocks.AgentBlock) -> None:
        before_git = (
            git_evidence.capture_git_snapshot(cfg.cwd)
            if block.requires_change or tool_policy.supports_mutation_audit(block.allowed_tools)
            else None
        )
        completion_check = None
        if before_git is not None:
            def completion_check(completed_block: agent_blocks.AgentBlock, baseline=before_git) -> None:
                after_git = git_evidence.capture_git_snapshot(cfg.cwd)
                if completed_block.requires_change:
                    enforce_required_change_contract(completed_block, baseline, after_git)
                else:
                    capture_optional_mutation_audit(completed_block, baseline, after_git)
        run_agent_block(
            block,
            task=pipeline_task,
            completed=completed,
            cfg=cfg,
            client=client,
            route=route,
            completion_check=completion_check,
        )

    def append_pipeline_block(block: agent_blocks.AgentBlock) -> None:
        block.context_output = agent_blocks.compact_block_output(block.output)
        completed.append(block)

    terminal_block: agent_blocks.AgentBlock | None = None
    cancelled = False
    run_error = ""
    try:
        with _agent_execution_scope():
            for block in pipeline:
                terminal_block = block
                run_pipeline_block(block)
                if block.status not in {"complete", "partial"}:
                    detail = f" ({block.status_reason})" if block.status_reason else ""
                    show_error(f"Agent pipeline stopped at {block.role}: {block.status}{detail}")
                    break
                append_pipeline_block(block)
                if block.status_code == "verification_missing":
                    show_error(
                        f"Agent pipeline stopped at {block.role}: post-mutation verification is missing."
                    )
                    break
                if should_recover_implementation(block):
                    retry_iterations = min(MAX_RECOVERY_IMPLEMENT_ITERATIONS, max(1, block.max_iterations))
                    show_agent_recovery_start(block.role, block.status_reason, retry_iterations)
                    replan = recovery_plan_block(block, cfg)
                    terminal_block = replan
                    run_pipeline_block(replan)
                    if replan.status in {"complete", "partial"}:
                        append_pipeline_block(replan)
                    if replan.status == "complete":
                        retry = retry_implementation_block(block)
                        terminal_block = retry
                        run_pipeline_block(retry)
                        append_pipeline_block(retry)
                    else:
                        show_info("Recovery replan did not complete; continuing with the original partial evidence.")
            if (
                completed
                and completed[-1].role == "final"
                and all(block.status == "complete" for block in completed)
            ):
                show_agent_pipeline_complete(
                    completed[-1].output,
                    block_count=len(completed),
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
    except KeyboardInterrupt:
        cancelled = True
        finish_thinking_block()
        show_error("Agent pipeline cancelled.")
    except Exception as exc:
        run_error = str(exc)
        raise
    finally:
        # Overwrite the session buffer with whichever blocks made it into
        # `completed`. Partial / stopped runs still expose what they did.
        _session_pipeline_blocks[:] = completed
        persisted_blocks = list(completed)
        if terminal_block is not None and terminal_block not in persisted_blocks:
            persisted_blocks.append(terminal_block)
        output = (
            completed[-1].output
            if completed
            else terminal_block.output if terminal_block is not None else ""
        )
        if cancelled:
            status = "cancelled"
            error = "Agent pipeline cancelled."
        elif run_error:
            status = "failed"
            error = run_error
        elif terminal_block is not None and terminal_block.status == "failed":
            status = "failed"
            error = terminal_block.status_reason or terminal_block.output
        elif any(block.status == "partial" for block in persisted_blocks):
            status = "partial"
            error = next(
                (block.status_reason for block in persisted_blocks if block.status == "partial" and block.status_reason),
                "",
            )
        elif completed and completed[-1].role == "final":
            status = "complete"
            error = ""
        else:
            status = "partial"
            error = "Pipeline ended before the final block."
        block_records = [_block_record(block) for block in persisted_blocks]
        _finish_thread_record(
            active_thread_id,
            status=status,
            output=output,
            error=error,
            blocks=block_records,
            pipeline=record_pipeline,
        )
        cfg.save()
        flush_perf_records()
    return AgentRunResult(
        thread_id=active_thread_id,
        status=status,
        pipeline=record_pipeline,
        output=output,
        error=error,
        blocks=block_records,
    )


def _specialist_prompt(role: str) -> str:
    return (
        f"You are the {role} specialist in an Algo CLI multi-agent team. You have a fresh, "
        "isolated context and a read-only tool set. Work independently; do not assume another "
        "specialist will cover your angle.\n\n"
        "Use the Algo loop:\n"
        "1. Define the question, invariant, or failure mode assigned to your role.\n"
        "2. Gather the smallest useful set of direct evidence.\n"
        "3. Compare alternatives or challenge the leading assumption.\n"
        "4. Separate verified facts from hypotheses and unresolved risks.\n"
        "5. Produce a concise handoff that an integration agent can verify and act on.\n\n"
        "Do not modify files, memory, configuration, or external systems. Cite concrete paths, "
        "commands, or sources when available. Return Markdown starting with exactly:\n"
        "## Block Output"
    )


def _finish_specialist_thread(
    thread_id: str,
    block: agent_blocks.AgentBlock,
    *,
    error: str = "",
) -> None:
    if not thread_id:
        return
    status = block.status if block.status in {"complete", "partial", "failed", "cancelled"} else "failed"
    try:
        agent_threads.finish_turn(
            thread_id,
            status=status,
            output=block.output,
            error=error or block.status_reason,
            blocks=[_block_record(block)],
        )
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Could not finish specialist thread %s: %s", thread_id, exc)


def run_agent_team(
    task: str,
    cfg: Config,
    client: Any,
    *,
    roles: list[str] | None = None,
) -> AgentRunResult:
    """Fan out independent read-only specialists, then integrate in one pipeline."""

    if not task.strip():
        show_error(AGENT_TEAM_USAGE)
        return AgentRunResult(status="failed", pipeline="team", error=AGENT_TEAM_USAGE)
    route = task_router.route_task(task)
    requested_roles = roles or default_team_roles(route)
    selected_roles = [_normalize_team_role(role) for role in requested_roles]
    if not all(selected_roles):
        error = "Team roles must be short names using letters, numbers, '-' or '_'."
        show_error(error)
        return AgentRunResult(status="failed", pipeline="team", error=error)
    if not MIN_TEAM_ROLES <= len(selected_roles) <= MAX_TEAM_ROLES:
        error = f"Team runs require {MIN_TEAM_ROLES}-{MAX_TEAM_ROLES} roles."
        show_error(error)
        return AgentRunResult(status="failed", pipeline="team", error=error)
    if len(set(selected_roles)) != len(selected_roles):
        error = "Team roles must be unique."
        show_error(error)
        return AgentRunResult(status="failed", pipeline="team", error=error)

    parent_id = ""
    child_ids: list[str] = []
    child_by_role: dict[str, str] = {}
    try:
        parent = agent_threads.create_thread(
            task,
            role="orchestrator",
            pipeline="team",
            model=cfg.model,
            status="running",
            title=f"Team: {' '.join(task.split())[:72]}",
        )
        parent_id = str(parent["id"])
        for role in selected_roles:
            child = agent_threads.create_thread(
                task,
                role=role,
                pipeline="specialist",
                model=cfg.model,
                parent_id=parent_id,
                status="queued",
                start_turn=True,
                title=f"{role}: {' '.join(task.split())[:64]}",
            )
            child_id = str(child["id"])
            child_ids.append(child_id)
            child_by_role[role] = child_id
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Could not initialize complete team thread tree: %s", exc)
        show_info(f"Some team thread history may be unavailable: {exc}")

    show_info(
        f"Agent team {parent_id or '(unrecorded)'}: launching {len(selected_roles)} read-only specialists "
        f"({', '.join(selected_roles)})."
    )

    def run_specialist(role: str) -> agent_blocks.AgentBlock:
        member_cfg = copy.deepcopy(cfg)
        member_cfg.messages = []
        member_cfg.session_summary = ""
        member_cfg.attempt_ledger = []
        block = agent_blocks.AgentBlock(
            role=role,
            prompt=_specialist_prompt(role),
            allowed_tools=agent_blocks.READ_TOOLS,
            max_iterations=min(8, max(2, int(cfg.max_tool_iterations))),
        )
        try:
            member_client = create_client(member_cfg)
            with _agent_execution_scope():
                run_agent_block(
                    block,
                    task=task,
                    completed=[],
                    cfg=member_cfg,
                    client=member_client,
                    route=route,
                )
        except Exception as exc:
            block.status = "failed"
            block.status_code = "specialist_error"
            block.status_reason = str(exc)
            block.output = block.output or f"## Block Output\n\nSpecialist failed: {exc}"
        block.context_output = agent_blocks.compact_block_output(block.output)
        return block

    specialists: dict[str, agent_blocks.AgentBlock] = {}
    with ThreadPoolExecutor(max_workers=len(selected_roles), thread_name_prefix="algo-agent") as pool:
        futures = {pool.submit(run_specialist, role): role for role in selected_roles}
        try:
            for future in as_completed(futures):
                role = futures[future]
                block = future.result()
                specialists[role] = block
                _finish_specialist_thread(child_by_role.get(role, ""), block)
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            for role in selected_roles:
                if role in specialists:
                    continue
                block = agent_blocks.AgentBlock(
                    role=role,
                    prompt=_specialist_prompt(role),
                    status="cancelled",
                    status_reason="Team run cancelled.",
                )
                _finish_specialist_thread(child_by_role.get(role, ""), block)
            if parent_id:
                try:
                    agent_threads.update_thread(parent_id, status="cancelled", error="Team run cancelled.")
                except (OSError, ValueError, KeyError):
                    pass
            show_error("Agent team cancelled.")
            return AgentRunResult(
                thread_id=parent_id,
                status="cancelled",
                pipeline="team",
                error="Team run cancelled.",
                children=child_ids,
            )

    ordered = [specialists[role] for role in selected_roles if role in specialists]
    useful = [block for block in ordered if block.status in {"complete", "partial"} and block.output.strip()]
    if not useful:
        error = "All specialist threads failed; integration was not started."
        if parent_id:
            try:
                agent_threads.update_thread(parent_id, status="failed", error=error)
            except (OSError, ValueError, KeyError):
                pass
        show_error(error)
        return AgentRunResult(
            thread_id=parent_id,
            status="failed",
            pipeline="team",
            error=error,
            children=child_ids,
            blocks=[_block_record(block) for block in ordered],
        )

    handoff_parts = []
    for block in ordered:
        handoff_parts.append(
            f"### Thread {child_by_role.get(block.role, '-')} · {block.role} · {block.status}\n"
            f"{block.context_output or block.output or '(no output)'}"
        )
    handoff = "\n\n".join(handoff_parts)
    integration_pipeline = (
        route.suggested_pipeline
        if route.task_type in {"coding", "research", "review"}
        else "research"
    )
    show_info(
        f"Agent team {parent_id or '(unrecorded)'}: specialists joined; "
        f"integrating through '{integration_pipeline}' with verification gates."
    )
    result = run_agent_pipeline(
        task,
        cfg,
        client,
        pipeline_name=integration_pipeline,
        thread_id=parent_id or None,
        prior_context=handoff,
        thread_pipeline_label=f"team:{integration_pipeline}",
    )
    result.children = child_ids
    return result


def _thread_list_text(records: list[dict[str, Any]]) -> str:
    if not records:
        return "No agent threads recorded."
    lines = ["Agent threads:"]
    for record in records:
        parent = f" <- {record['parent_id']}" if record.get("parent_id") else ""
        lines.append(
            f"- {record['id']}{parent} [{record['status']}] {record['role']} · "
            f"{record['pipeline']} · {record['title']}"
        )
    return "\n".join(lines)


def show_agent_threads() -> str:
    records = agent_threads.list_threads(limit=20)
    if not records:
        message = "No agent threads recorded. Run /agent TASK or /agent team TASK."
        show_info(message)
        return message
    table = Table(title="Agent Threads", box=box.SIMPLE, padding=(0, 1))
    table.add_column("ID", style="primary", no_wrap=True)
    table.add_column("Status", style="text")
    table.add_column("Role", style="muted")
    table.add_column("Pipeline", style="text")
    table.add_column("Task", style="text", overflow="fold")
    for record in records:
        table.add_row(
            record["id"],
            record["status"],
            record["role"],
            record["pipeline"],
            record["title"],
        )
    console.print(table)
    return _thread_list_text(records)


def show_agent_thread(thread_ref: str) -> str:
    record = agent_threads.resolve_thread(thread_ref)
    table = Table(title=f"Agent Thread {record['id']}", box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("Field", style="muted")
    table.add_column("Value", style="text", overflow="fold")
    for label, value in (
        ("status", record["status"]),
        ("role", record["role"]),
        ("pipeline", record["pipeline"]),
        ("model", record["model"] or "-"),
        ("parent", record["parent_id"] or "-"),
        ("children", ", ".join(record["children"]) or "-"),
        ("updated", record["updated_at"]),
        ("task", record["task"]),
        ("error", record["error"] or "-"),
    ):
        table.add_row(label, str(value))
    console.print(table)
    if record["output"]:
        console.print(Text(record["output"]))
    return agent_threads.context_handoff(record)


def _pipeline_for_thread(record: dict[str, Any]) -> str:
    pipeline = str(record.get("pipeline") or "default")
    if pipeline.startswith("team:"):
        pipeline = pipeline.split(":", 1)[1]
    if pipeline in agent_blocks.pipeline_names():
        return pipeline
    route = task_router.route_task(str(record.get("task") or ""))
    return route.suggested_pipeline if route.recommended_mode == "agent" else "research"


def _completed_agent_result_for_tool(
    result: AgentRunResult,
    *,
    task: str,
    cfg: Config,
) -> str:
    memory_result = memory_runtime.capture_completed_user_turn(
        cfg,
        task,
        completed=result.status == "complete",
        source="agent",
    )
    flush_perf_records()
    if memory_result.get("status") == "stored":
        show_info("Saved 1 durable memory automatically; review it with /memories.")
    return result.for_tool()


def execute_agent_command(arg: str, cfg: Config, client: Any) -> str:
    """Execute `/agent` for either the TUI or a parent runtime model."""

    text = (arg or "").strip()
    lowered = text.lower()
    if lowered in {"help", "--help", "-h", "?"}:
        message = agent_usage_text()
        show_info(message)
        return message
    if lowered == "init":
        try:
            path = agent_blocks.write_starter_config()
        except FileExistsError as exc:
            show_error(str(exc))
            return f"Error: {exc}"
        message = f"Wrote Agent Blocks starter config: {path}"
        show_info(message)
        return message
    if lowered in {"threads", "list", "status"}:
        return show_agent_threads()
    if lowered.startswith("show "):
        try:
            return show_agent_thread(text.split(maxsplit=1)[1])
        except KeyError as exc:
            message = str(exc).strip("'")
            show_error(message)
            return f"Error: {message}"
    if lowered == "show":
        show_error(AGENT_THREAD_USAGE)
        return f"Error: {AGENT_THREAD_USAGE}"
    if lowered.startswith("team") and (len(text) == 4 or text[4].isspace()):
        roles, task, error = parse_agent_team_invocation(text[4:].strip())
        if error:
            show_error(error)
            return f"Error: {error}"
        return _completed_agent_result_for_tool(
            run_agent_team(task, cfg, client, roles=roles or None),
            task=task,
            cfg=cfg,
        )
    for action in ("resume", "fork"):
        if lowered == action or lowered.startswith(f"{action} "):
            try:
                parts = shlex.split(text)
            except ValueError as exc:
                message = f"{AGENT_THREAD_USAGE} ({exc})"
                show_error(message)
                return f"Error: {message}"
            if len(parts) < 2 or (action == "fork" and len(parts) < 3):
                show_error(AGENT_THREAD_USAGE)
                return f"Error: {AGENT_THREAD_USAGE}"
            try:
                record = agent_threads.resolve_thread(parts[1])
            except KeyError as exc:
                message = str(exc).strip("'")
                show_error(message)
                return f"Error: {message}"
            task = " ".join(parts[2:]).strip() or "Continue from the latest verified state and finish remaining work."
            handoff = agent_threads.context_handoff(record)
            result = run_agent_pipeline(
                task,
                cfg,
                client,
                pipeline_name=_pipeline_for_thread(record),
                thread_id=record["id"] if action == "resume" else None,
                parent_id=record["id"] if action == "fork" else "",
                prior_context=handoff,
            )
            return _completed_agent_result_for_tool(result, task=task, cfg=cfg)
    pipeline_name, task, error = parse_agent_invocation_checked(text)
    if error:
        show_error(error)
        return f"Error: {error}"
    return _completed_agent_result_for_tool(
        run_agent_pipeline(task, cfg, client, pipeline_name=pipeline_name),
        task=task,
        cfg=cfg,
    )
