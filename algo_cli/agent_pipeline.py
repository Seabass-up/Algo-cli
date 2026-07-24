"""Agent block execution, required-change contracts, recovery, and pipelines."""

from __future__ import annotations

import copy
import logging
import re
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from rich import box
from rich.table import Table
from rich.text import Text

from . import agent_blocks
from . import agent_context
from . import agent_run_journal
from . import agent_threads
from . import spawn_budget
from . import git_evidence
from . import execution_guardrails
from . import harness
from . import inference_harness
from . import julia_memory_runtime as memory_runtime
from . import reflex
from . import run_contract as run_contracts
from . import task_router
from . import samuel_policy as tool_policy
from . import worktree_runtime
from . import model_info as _model_info_module
from . import nathan_provider_protocol
from . import chatgpt_client
from . import context_budget
from . import tools as tools_module
from .chat_protocol import (
    collapse_tool_history_for_gemini,
    get_attr,
    normalize_tool_call,
)
from .config import Config
from .display import (
    console,
    finish_thinking_block,
    show_agent_block_complete,
    show_agent_block_start,
    show_agent_pipeline_complete,
    show_agent_recovery_start,  # noqa: F401 - compatibility hook for tests/plugins
    show_error,
    show_info,
    show_recalled_context,
    show_thinking_text,
)
from .dorothy_perf_telemetry import flush_perf_records, record_chat_metrics
from .theodore_runtime_services import client_for_model, create_client
from .james_dispatch import batch_policy_ceiling_codes
from .marcus_authority import EffectClass
from .nathan_runtime import (
    approval_mode_for_config,
    classify_tool_status,
    execute_tool_call_for_pipeline,
    summarize_tool_result,
)
from .samuel_policy_engine import resolve_action

TOOL_MAP = tools_module.TOOL_MAP
logger = logging.getLogger(__name__)


def _pipeline_outcome_status(execution: Any, result: str) -> str:
    """Prefer the canonical typed outcome; parse text only for legacy adapters."""

    outcome = getattr(execution, "outcome", None)
    status = getattr(getattr(outcome, "status", None), "value", "")
    if status:
        return str(status)
    lowered = str(result).strip().casefold()
    if lowered.startswith(("blocked by runtime", "user denied")):
        return "denied"
    if lowered.startswith("skipped repeated"):
        return "skipped"
    legacy = classify_tool_status(result)
    return "succeeded" if legacy == "worked" else legacy


def _block_output_is_verified(block: agent_blocks.AgentBlock) -> bool:
    text = block.output.strip()
    if not text.startswith("## Block Output"):
        return False
    remainder = text[len("## Block Output") :].strip()
    return bool(remainder)


_MUTATION_COMPLETION_CLAIM_RE = re.compile(
    r"\b(changed|completed|created|fixed|implemented|updated|wrote)\b",
    re.IGNORECASE,
)
_UNCERTAINTY_DISCLOSURE_RE = re.compile(
    r"\b(blocked|incomplete|partial|uncertain|unverified)\b",
    re.IGNORECASE,
)


def _final_output_claims_are_grounded(
    block: agent_blocks.AgentBlock,
    prior_blocks: list[agent_blocks.AgentBlock],
) -> bool:
    """Ground completion claims in typed prior-block verification evidence."""

    if block.role != "final":
        return True
    if any(
        prior.status != "complete"
        or bool(prior.verification_warning)
        for prior in prior_blocks
    ) and not _UNCERTAINTY_DISCLOSURE_RE.search(block.output):
        return False
    mutation_blocks = [
        prior
        for prior in prior_blocks
        if prior.requires_change
    ]
    if (
        mutation_blocks
        and _MUTATION_COMPLETION_CLAIM_RE.search(block.output)
    ):
        return all(
            prior.status == "complete"
            and not prior.verification_warning
            and bool(prior.successful_writes)
            and (
                "Verified Git state change" in prior.git_evidence
                or "Git state changed during this block" in prior.git_evidence
            )
            for prior in mutation_blocks
        )
    return True


def _enforce_block_output_verification(
    block: agent_blocks.AgentBlock,
    prior_blocks: list[agent_blocks.AgentBlock],
) -> bool:
    output_contract_verified = _block_output_is_verified(block)
    claim_grounded = _final_output_claims_are_grounded(
        block,
        prior_blocks,
    )
    if block.status == "complete" and not output_contract_verified:
        block.status = "partial"
        block.status_code = "output_contract_failed"
        block.status_reason = (
            "Block output did not satisfy the required "
            "'## Block Output' evidence contract."
        )
        block.verification_warning = block.status_reason
    elif block.status == "complete" and not claim_grounded:
        block.status = "partial"
        block.status_code = "claim_grounding_failed"
        block.status_reason = (
            "Final output made a completion claim without matching "
            "verified prior-block evidence or omitted required uncertainty."
        )
        block.verification_warning = block.status_reason
    return output_contract_verified and claim_grounded


def _estimate_agent_request_tokens(
    messages: list[dict[str, Any]],
    tools: list[Any],
) -> int:
    total = sum(
        context_budget.estimate_message_tokens(message)
        for message in messages
    )
    if tools:
        from .tool_schema import estimate_tool_schema_tokens

        total += estimate_tool_schema_tokens(tools)
    return total


def _model_chat_options(model: str, cfg: Config) -> dict[str, Any]:
    options: dict[str, Any] = {"temperature": cfg.temperature, "num_ctx": cfg.num_ctx}
    if _model_info_module.is_chatgpt_model(model):
        options["reasoning_effort"] = chatgpt_client.reasoning_effort_for_model(
            model, cfg.chatgpt_reasoning_efforts
        )
    return options


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
    contract_id: str = ""
    contract_mode: str = ""

    def for_tool(self) -> str:
        lines = [
            f"Agent thread {self.thread_id or '-'}: {self.status}",
            f"Pipeline: {self.pipeline}",
        ]
        if self.children:
            lines.append(f"Child threads: {', '.join(self.children)}")
        if self.contract_id:
            lines.append(
                f"Run contract: {self.contract_id} ({self.contract_mode or 'unknown'})"
            )
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


AgentLoopProtocolError = nathan_provider_protocol.AgentLoopProtocolError
AgentLoopState = nathan_provider_protocol.AgentLoopState


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
    run_contract: run_contracts.RunContract | None = None,
    block_contract: run_contracts.BlockRunContract | None = None,
    contract_tracker: run_contracts.RunContractTracker | None = None,
    run_journal: agent_run_journal.AgentRunJournal | None = None,
    block_ordinal: int | None = None,
    recovery_phase: str = "",
    recovery_attempt: int = 0,
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
    policy_enforced = cfg.algorithmic_tool_policy_enabled
    iteration_limit = max(1, int(block.max_iterations))
    runtime_tool_names: frozenset[str]
    if run_contract is not None:
        try:
            if block_contract is None:
                raise run_contracts.RunContractViolation(
                    "run contract execution requires a block contract"
                )
            run_contract.assert_live_authority(
                approval_mode=approval_mode_for_config(cfg),
                safe_mode=bool(cfg.safe_mode),
                session_preapproval=(
                    False
                    if approval_mode_for_config(cfg) == "never"
                    else approval_mode_for_config(cfg) == "auto"
                    or bool(cfg.auto_approve_active)
                ),
            )
            if recovery_phase:
                if (
                    recovery_attempt < 1
                    or recovery_attempt
                    > block_contract.max_recovery_attempts
                ):
                    raise run_contracts.RunContractViolation(
                        "recovery attempt exceeds the immutable run contract"
                    )
                if recovery_phase == "plan":
                    if (
                        block.role != "recovery-plan"
                        or block.allowed_tools
                        or block.requires_change
                    ):
                        raise run_contracts.RunContractViolation(
                            "live recovery plan differs from the immutable run contract"
                        )
                    runtime_tool_names = frozenset()
                    iteration_limit = 1
                elif recovery_phase == "retry":
                    if (
                        block.role
                        != f"{block_contract.role}-retry"
                        or frozenset(block_contract.configured_tools)
                        != block.allowed_tools
                        or block_contract.requires_change
                        != block.requires_change
                    ):
                        raise run_contracts.RunContractViolation(
                            "live recovery retry differs from the immutable run contract"
                        )
                    runtime_tool_names = frozenset(
                        block_contract.effective_tools(
                            run_contract.mode
                        )
                    )
                    iteration_limit = (
                        block_contract.recovery_max_iterations
                    )
                else:
                    raise run_contracts.RunContractViolation(
                        "recovery phase is invalid"
                    )
            else:
                if block_contract.role != block.role:
                    raise run_contracts.RunContractViolation(
                        "live block role differs from the immutable run contract"
                    )
                if (
                    agent_run_journal.digest_text(block.prompt)
                    != block_contract.prompt_digest
                ):
                    raise run_contracts.RunContractViolation(
                        "live block prompt differs from the immutable run contract"
                    )
                if (
                    frozenset(block_contract.configured_tools)
                    != block.allowed_tools
                ):
                    raise run_contracts.RunContractViolation(
                        "live block tools differ from the immutable run contract"
                    )
                if block_contract.requires_change != block.requires_change:
                    raise run_contracts.RunContractViolation(
                        "live block mutation contract differs from the immutable run contract"
                    )
                runtime_tool_names = frozenset(
                    block_contract.effective_tools(run_contract.mode)
                )
                iteration_limit = block_contract.max_iterations
            policy_enforced = run_contract.mode == "enforced"
            policy = tool_policy.ToolPolicyDecision(
                allowed_tools=(
                    frozenset()
                    if recovery_phase == "plan"
                    else frozenset(block_contract.admitted_tools)
                ),
                denied_tools=(
                    frozenset()
                    if recovery_phase == "plan"
                    else frozenset(block_contract.denied_tools)
                ),
                approval_required=(
                    frozenset()
                    if recovery_phase == "plan"
                    else frozenset(
                        block_contract.approval_required_tools
                    )
                ),
                reasons=(
                    ("contract-bound recovery plan",)
                    if recovery_phase == "plan"
                    else block_contract.policy_reasons
                ),
            )
        except run_contracts.RunContractViolation as exc:
            block.status = "failed"
            block.status_code = "run_contract_violation"
            block.status_reason = str(exc)
            block.output = f"## Block Output\n\nRun contract rejected execution: {exc}"
            block.duration_ms = 0.0
            show_agent_block_complete(
                block.role,
                block.output,
                duration_ms=block.duration_ms,
                tool_calls=block.tool_calls,
                status=block.status,
                status_reason=block.status_reason,
                status_code=block.status_code,
                policy_summary="run contract rejected",
            )
            return
    else:
        runtime_tool_names = (
            policy.allowed_tools if policy_enforced else block.allowed_tools
        )
    allowed_tools = [
        TOOL_MAP[name]
        for name in sorted(runtime_tool_names)
        if name in TOOL_MAP
    ]
    block_model = (
        block_contract.model
        if block_contract is not None
        else block.model or cfg.model
    )
    block_client = client_for_model(block_model, cfg, client)
    if block_client is client and block_model != cfg.model:
        if run_contract is not None:
            block.status = "failed"
            block.status_code = "run_contract_model_unavailable"
            block.status_reason = (
                f"Contract model {block_model} is unavailable; runtime "
                f"fallback to {cfg.model} was withheld."
            )
            block.output = (
                "## Block Output\n\n"
                f"{block.status_reason}"
            )
            block.duration_ms = 0.0
            show_agent_block_complete(
                block.role,
                block.output,
                duration_ms=block.duration_ms,
                tool_calls=block.tool_calls,
                status=block.status,
                status_reason=block.status_reason,
                status_code=block.status_code,
                policy_summary="run contract rejected model fallback",
            )
            return
        block_model = cfg.model
    policy_summary = tool_policy.format_policy_summary(policy)
    if block.requires_change:
        policy_summary += "; file edits: write_file only"
    show_agent_block_start(
        block.role,
        block_model,
        len(allowed_tools),
        policy_summary=policy_summary,
        policy_enforced=policy_enforced,
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
    loop_state = AgentLoopState()

    def record_protocol_dispatch(
        name: str,
        args: dict[str, Any],
        tool_call_id: str | None,
    ) -> None:
        try:
            action = resolve_action(name, args, cwd=cfg.cwd)
            mutating = action.effect_class is not EffectClass.OBSERVE
        except Exception:
            # Unknown actions are treated as mutation-capable until the
            # dispatcher produces their fail-closed result.
            mutating = True
        loop_state.record_tool_dispatch(
            tool_call_id,
            mutating=mutating,
        )

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
        partial_tool_calls: list[Any] = []
        wrap_round = loop_state.round_number + 1
        loop_state.begin_model_round(wrap_round)
        try:
            stream = block_client.chat(
                model=block_model,
                messages=messages,
                tools=[],
                stream=True,
                think=cfg.show_thinking,
                keep_alive=cfg.keep_alive,
                options=_model_chat_options(block_model, cfg),
            )
            for chunk in stream:
                record_chat_metrics(cfg, chunk)
                message = get_attr(chunk, "message", {})
                thinking = get_attr(message, "thinking", "")
                content = get_attr(message, "content", "")
                calls = get_attr(message, "tool_calls", None)
                if thinking:
                    loop_state.record_model_event("reasoning")
                    if cfg.show_thinking:
                        show_thinking_text(thinking)
                if content:
                    loop_state.record_model_event("content")
                    finish_thinking_block()
                    partial_text += content
                if calls:
                    loop_state.record_model_event("tool")
                    partial_tool_calls.extend(calls)
            loop_state.complete_model_round(partial_tool_calls)
            if partial_tool_calls:
                loop_state.cancel(
                    "tool-free partial wrap-up attempted a tool call"
                )
                partial_text = ""
            else:
                loop_state.finish_without_tools()
        except KeyboardInterrupt:
            loop_state.cancel(
                "Agent Block partial wrap-up cancelled"
            )
            raise
        except Exception as exc:
            loop_state.interrupt(
                str(exc),
                timed_out=isinstance(exc, TimeoutError),
            )
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
            f"Iteration budget exhausted after {iteration_limit} cycles; "
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


    from .nathan_program_runtime import authorization_for_actions

    _program_auth_missing = object()
    _previous_program_authorization = getattr(
        cfg, "_algo_program_authorization", _program_auth_missing
    )
    setattr(
        cfg,
        "_algo_program_authorization",
        authorization_for_actions(tuple(runtime_tool_names)),
    )

    try:
        for _ in range(iteration_limit):
            if run_contract is not None:
                try:
                    run_contract.assert_live_authority(
                        approval_mode=approval_mode_for_config(cfg),
                        safe_mode=bool(cfg.safe_mode),
                        session_preapproval=(
                            False
                            if approval_mode_for_config(cfg) == "never"
                            else approval_mode_for_config(cfg) == "auto"
                            or bool(cfg.auto_approve_active)
                        ),
                    )
                except run_contracts.RunContractViolation as exc:
                    block.status = "partial"
                    block.status_code = "run_contract_violation"
                    block.status_reason = str(exc)
                    block.output = (
                        "## Block Output\n\n"
                        f"Run contract stopped execution: {exc}"
                    )
                    break
            request_messages = messages
            if _model_info_module.is_gemini_model(block_model):
                request_messages = collapse_tool_history_for_gemini(request_messages)
            if run_contract is not None:
                try:
                    prompt_tokens = _estimate_agent_request_tokens(
                        request_messages,
                        allowed_tools,
                    )
                    if contract_tracker is not None:
                        contract_tracker.start_model_round(prompt_tokens)
                    if run_journal is not None:
                        if block_ordinal is None:
                            raise run_contracts.RunContractViolation(
                                "journaled block is missing its contract ordinal"
                            )
                        run_journal.model_round_started(
                            block_ordinal,
                            _,
                            attempt=recovery_attempt,
                            prompt_tokens=prompt_tokens,
                        )
                except run_contracts.RunContractViolation as exc:
                    block.status = "partial"
                    block.status_code = "run_contract_prompt_budget"
                    block.status_reason = str(exc)
                    block.output = (
                        "## Block Output\n\n"
                        f"Run contract stopped model dispatch: {exc}"
                    )
                    break
                except agent_run_journal.AgentRunJournalError as exc:
                    block.status = "failed"
                    block.status_code = "run_journal_unavailable"
                    block.status_reason = str(exc)
                    block.output = (
                        "## Block Output\n\n"
                        f"Durable run checkpoint failed before model dispatch: {exc}"
                    )
                    break
            loop_state.begin_model_round(_)
            thinking_text = ""
            content_text = ""
            tool_calls: list[Any] = []
            try:
                stream = block_client.chat(
                    model=block_model,
                    messages=request_messages,
                    tools=allowed_tools,
                    stream=True,
                    think=cfg.show_thinking,
                    keep_alive=cfg.keep_alive,
                    options=_model_chat_options(block_model, cfg),
                )
                for chunk in stream:
                    record_chat_metrics(cfg, chunk)
                    message = get_attr(chunk, "message", {})
                    thinking = get_attr(message, "thinking", "")
                    content = get_attr(message, "content", "")
                    calls = get_attr(message, "tool_calls", None)
                    if thinking:
                        loop_state.record_model_event("reasoning")
                        if cfg.show_thinking:
                            show_thinking_text(thinking)
                            thinking_text += thinking
                    if content:
                        loop_state.record_model_event("content")
                        finish_thinking_block()
                        content_text += content
                    if calls:
                        loop_state.record_model_event("tool")
                        tool_calls.extend(calls)
            except KeyboardInterrupt:
                loop_state.cancel("Agent Block model stream cancelled")
                raise
            except Exception as exc:
                loop_state.interrupt(
                    str(exc),
                    timed_out=isinstance(exc, TimeoutError),
                )
                raise
            finally:
                finish_thinking_block()
            serialized_calls = loop_state.complete_model_round(tool_calls)

            assistant: dict[str, Any] = {"role": "assistant"}
            if content_text:
                assistant["content"] = content_text
                block.output = content_text
            if thinking_text:
                assistant["thinking"] = thinking_text
            if tool_calls:
                assistant["tool_calls"] = serialized_calls
            messages.append(assistant)
            if run_journal is not None and block_ordinal is not None:
                try:
                    run_journal.model_round_completed(
                        block_ordinal,
                        _,
                        status="completed",
                        tool_call_count=len(tool_calls),
                        response_digest=agent_run_journal.digest_json(
                            {
                                "content": content_text,
                                "thinking": thinking_text,
                                "tool_calls": serialized_calls,
                            }
                        ),
                        attempt=recovery_attempt,
                    )
                except agent_run_journal.AgentRunJournalError as exc:
                    block.status = "failed"
                    block.status_code = "run_journal_unavailable"
                    block.status_reason = str(exc)
                    block.output = (
                        "## Block Output\n\n"
                        f"Durable run checkpoint failed after model dispatch: {exc}"
                    )
                    break

            if not tool_calls:
                loop_state.finish_without_tools()
                completion = execution_guardrails.completion_decision()
                if not completion.allowed:
                    if not completion_nudged and _ + 1 < iteration_limit:
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

            normalized_batch = [normalize_tool_call(call) for call in tool_calls]
            batch = [
                (normalized, str(serialized.get("id") or "") or None)
                for normalized, serialized in zip(normalized_batch, serialized_calls)
            ]
            loop_state.begin_tool_batch()
            if contract_tracker is not None:
                try:
                    contract_tracker.reserve_tool_calls(len(batch))
                except run_contracts.RunContractViolation as exc:
                    for (name, args), tool_call_id in batch:
                        record_protocol_dispatch(
                            name,
                            args,
                            tool_call_id,
                        )
                        execution = execute_tool_call_for_pipeline(
                            name,
                            args,
                            cfg,
                            tool_call_id=tool_call_id,
                            policy_ceiling_code="run_contract_tool_budget",
                        )
                        messages.append(execution.message)
                        loop_state.record_tool_result(tool_call_id)
                    loop_state.finish_tool_batch()
                    block.status = "partial"
                    block.status_code = "run_contract_tool_budget"
                    block.status_reason = str(exc)
                    block.output = (
                        "## Block Output\n\n"
                        f"Run contract stopped tool dispatch: {exc}"
                    )
                    break
            journal_steps: list[str] = []
            if run_journal is not None and block_ordinal is not None:
                try:
                    for tool_index, ((name, args), tool_call_id) in enumerate(
                        batch
                    ):
                        action = resolve_action(name, args, cwd=cfg.cwd)
                        event = run_journal.tool_intent(
                            ordinal=block_ordinal,
                            round_number=_,
                            tool_index=tool_index,
                            action=name,
                            args=args,
                            call_id=tool_call_id or "",
                            mutating=action.effect_class
                            is not EffectClass.OBSERVE,
                            idempotency=action.idempotency.value,
                            target=action.target,
                            attempt=recovery_attempt,
                        )
                        journal_steps.append(
                            str(event.payload.get("step_id") or "")
                        )
                except Exception as exc:
                    for step_id in journal_steps:
                        try:
                            run_journal.tool_result(
                                step_id=step_id,
                                status="failed",
                                invoked=False,
                                verification="failed",
                                error_code="journal_intent_failed",
                            )
                        except agent_run_journal.AgentRunJournalError:
                            break
                    for (name, args), tool_call_id in batch:
                        record_protocol_dispatch(
                            name,
                            args,
                            tool_call_id,
                        )
                        execution = execute_tool_call_for_pipeline(
                            name,
                            args,
                            cfg,
                            tool_call_id=tool_call_id,
                            policy_ceiling_code="run_journal_unavailable",
                        )
                        messages.append(execution.message)
                        loop_state.record_tool_result(tool_call_id)
                    loop_state.finish_tool_batch()
                    block.status = "failed"
                    block.status_code = "run_journal_unavailable"
                    block.status_reason = (
                        f"Durable tool intent checkpoint failed: {exc}"
                    )
                    block.output = (
                        "## Block Output\n\n"
                        f"Tool dispatch was withheld because its durable intent "
                        f"could not be recorded: {exc}"
                    )
                    break
            policy_ceilings = batch_policy_ceiling_codes(batch, cfg)
            state_ceilings = loop_state.tool_batch_ceiling_codes()
            protocol_ceilings = tuple(
                state_code or policy_code
                for state_code, policy_code in zip(
                    state_ceilings,
                    policy_ceilings,
                )
            )
            shell_decisions = [
                tool_policy.evaluate_shell_command(
                    str(args.get("command", "")),
                    requires_change=(block.requires_change and name == "run_shell"),
                    safe_mode=cfg.safe_mode,
                )
                for name, args in normalized_batch
            ]
            has_disallowed_tool = any(
                name not in runtime_tool_names for name, _args in normalized_batch
            )
            has_blocked_shell = any(decision.blocked for decision in shell_decisions)
            batch_must_quarantine = (
                any(protocol_ceilings) or has_disallowed_tool or has_blocked_shell
            )
            journal_result_failed = False

            def append_journal_result(
                index: int,
                execution: Any,
                result: str,
            ) -> None:
                nonlocal journal_result_failed
                if (
                    run_journal is None
                    or block_ordinal is None
                    or index >= len(journal_steps)
                ):
                    return
                outcome = getattr(execution, "outcome", None)
                status = _pipeline_outcome_status(execution, result)
                verification_value = getattr(
                    getattr(outcome, "verification", None),
                    "value",
                    "",
                )
                try:
                    run_journal.tool_result(
                        step_id=journal_steps[index],
                        status=status,
                        invoked=bool(
                            getattr(
                                outcome,
                                "invoked",
                                status == "succeeded",
                            )
                        ),
                        verification=str(
                            verification_value
                            or (
                                "passed"
                                if status == "succeeded"
                                else "failed"
                            )
                        ),
                        effect_id=str(getattr(outcome, "effect_id", "") or ""),
                        idempotency_key=str(
                            getattr(outcome, "idempotency_key", "") or ""
                        ),
                        error_code=str(
                            getattr(outcome, "error_code", "") or ""
                        ),
                        deduplicated=bool(
                            getattr(outcome, "deduplicated", False)
                        ),
                    )
                except agent_run_journal.AgentRunJournalError as exc:
                    journal_result_failed = True
                    block.status = "failed"
                    block.status_code = "run_journal_unavailable"
                    block.status_reason = (
                        f"Tool outcome may require reconciliation because its "
                        f"durable checkpoint failed: {exc}"
                    )
                    block.output = (
                        "## Block Output\n\n"
                        f"Tool outcome checkpoint failed after dispatch: {exc}"
                    )

            for index, ((name, args), tool_call_id) in enumerate(batch):
                if journal_result_failed:
                    record_protocol_dispatch(
                        name,
                        args,
                        tool_call_id,
                    )
                    execution = execute_tool_call_for_pipeline(
                        name,
                        args,
                        cfg,
                        tool_call_id=tool_call_id,
                        policy_ceiling_code="run_journal_unavailable",
                    )
                    tool_message, result = execution
                    messages.append(tool_message)
                    loop_state.record_tool_result(tool_call_id)
                    append_journal_result(
                        index,
                        execution,
                        result,
                    )
                    continue
                shell_decision = shell_decisions[index]
                if batch_must_quarantine:
                    if name not in runtime_tool_names:
                        block.status_reason = f"Tool policy violation: {name} is not allowed in the {block.role} block."
                        ceiling_code = "agent_tool_not_allowed"
                    elif shell_decision.blocked:
                        ceiling_code = "required_change_shell_blocked"
                        block.mutation_denied = True
                    elif protocol_ceilings[index]:
                        ceiling_code = protocol_ceilings[index]
                    else:
                        ceiling_code = "agent_batch_quarantined"
                    record_protocol_dispatch(
                        name,
                        args,
                        tool_call_id,
                    )
                    execution = execute_tool_call_for_pipeline(
                        name,
                        args,
                        cfg,
                        tool_call_id=tool_call_id,
                        policy_ceiling_code=ceiling_code,
                    )
                    tool_message, result = execution
                    messages.append(tool_message)
                    loop_state.record_tool_result(tool_call_id)
                    append_journal_result(index, execution, result)
                    if has_disallowed_tool or any(protocol_ceilings):
                        block.status = "failed"
                        block.status_code = "policy_denied"
                    if name not in runtime_tool_names or (
                        protocol_ceilings[index]
                        and protocol_ceilings[index] != "batch_quarantined"
                    ):
                        block.output = result
                    continue
                block.tool_calls += 1
                record_protocol_dispatch(
                    name,
                    args,
                    tool_call_id,
                )
                execution = execute_tool_call_for_pipeline(
                    name,
                    args,
                    cfg,
                    tool_call_id=tool_call_id,
                    force_approval=tool_policy.requires_explicit_approval(
                        name,
                        block_policy=policy,
                        shell_decision=shell_decision,
                        policy_enforced=policy_enforced,
                    ),
                )
                tool_message, _result = execution
                append_journal_result(index, execution, _result)
                outcome_status = _pipeline_outcome_status(execution, _result)
                mutation_action = tool_policy.describes_mutation_action(name, args)
                mutation_succeeded = (
                    outcome_status == "succeeded"
                    and (
                        (name == "write_file" and str(_result).lstrip().startswith("Wrote "))
                        or (name == "edit_file" and str(_result).lstrip().startswith("Edited "))
                        or (name == "batch_edit" and str(_result).lstrip().startswith("Batch-edited "))
                    )
                )
                if mutation_succeeded:
                    written_path = str(args.get("path", "")).strip()
                    if written_path and written_path not in block.successful_writes:
                        block.successful_writes.append(written_path)
                    if mutation_action and mutation_action not in block.mutation_actions:
                        block.mutation_actions.append(mutation_action)
                elif name in {"write_file", "edit_file", "batch_edit"}:
                    if outcome_status == "denied":
                        block.mutation_denied = True
                    elif outcome_status != "succeeded":
                        block.failed_writes.append(summarize_tool_result(str(_result)))
                elif (
                    name == "run_shell"
                    and mutation_action
                    and outcome_status == "succeeded"
                    and mutation_action not in block.mutation_actions
                ):
                    block.mutation_actions.append(mutation_action)
                messages.append(tool_message)
                loop_state.record_tool_result(tool_call_id)
            loop_state.finish_tool_batch()
            if block.status == "failed" or journal_result_failed:
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
        if _previous_program_authorization is _program_auth_missing:
            try:
                delattr(cfg, "_algo_program_authorization")
            except AttributeError:
                pass
        else:
            setattr(cfg, "_algo_program_authorization", _previous_program_authorization)
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
AGENT_THREAD_USAGE = "Usage: /agent show THREAD | switch THREAD | resume THREAD [task] | fork THREAD [--same-worktree] <task>"
MIN_TEAM_ROLES = 2
MAX_TEAM_ROLES = 4


def agent_usage_text() -> str:
    return (
        f"{AGENT_USAGE}\n"
        f"{AGENT_TEAM_USAGE}\n"
        f"{AGENT_THREAD_USAGE}\n"
        "Thread commands: /agent threads | show THREAD | switch THREAD | resume THREAD [task] | fork THREAD [--same-worktree] <task>\n"
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

    # A recorded write is action evidence, not final-state verification. Never
    # convert it into a warning-only completion when Git attribution is
    # unavailable.
    if (not before.available or not after.available) and block.successful_writes:
        reported_output = block.output.strip()
        block.status = "partial"
        block.status_code = "verification_unavailable"
        block.status_reason = (
            "Required change not verified: Git final-state evidence is "
            "unavailable, so recorded writes cannot establish completion."
        )
        block.verification_warning = block.status_reason
        block.output = (
            "## Block Output\n\n"
            "UNVERIFIED: a write action was recorded, but no attributable "
            "final-state verifier was available."
        )
        if reported_output:
            block.output += (
                "\n\nUnverified reported output:\n"
                f"{reported_output}"
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
        "context_output": block.context_output[
            : agent_threads.MAX_BLOCK_CONTEXT_CHARS
        ],
        "tool_calls": block.tool_calls,
        "duration_ms": block.duration_ms,
        "successful_writes": list(block.successful_writes),
        "verification_warning": block.verification_warning,
        "git_head": block.git_head,
        "git_status": block.git_status,
        "status_digest": block.status_digest,
        "tracked_diff_digest": block.tracked_diff_digest,
        "untracked_digest": block.untracked_digest,
        "git_clean": block.git_clean,
    }


def _contract_thread_link(
    contract: run_contracts.RunContract,
) -> dict[str, Any]:
    return {
        "contract_id": contract.contract_id,
        "digest": contract.digest,
        "run_nonce": contract.run_nonce,
        "mode": contract.mode,
        "approval_mode": contract.approval_mode,
        "journal_file": agent_run_journal.journal_path(
            contract.run_nonce
        ).name,
    }


def _checkpoint_payload(
    journal: agent_run_journal.AgentRunJournal,
) -> dict[str, Any]:
    return journal.checkpoint_payload()


def _hydrate_verified_blocks(
    pipeline: list[agent_blocks.AgentBlock],
    block_records: list[dict[str, Any]],
    journal: agent_run_journal.AgentRunJournal,
) -> list[agent_blocks.AgentBlock]:
    """Restore only context whose digest is accepted by the journal chain."""

    checkpoints = journal.verified_blocks()
    if len(block_records) < len(checkpoints):
        raise agent_run_journal.AgentRunJournalCorrupt(
            "thread context is missing a verified block checkpoint"
        )
    hydrated: list[agent_blocks.AgentBlock] = []
    for checkpoint in checkpoints:
        if checkpoint.ordinal >= len(pipeline):
            raise agent_run_journal.AgentRunJournalCorrupt(
                "verified block is outside the current pipeline"
            )
        raw = block_records[checkpoint.ordinal]
        if not isinstance(raw, dict):
            raise agent_run_journal.AgentRunJournalCorrupt(
                "verified thread block context is invalid"
            )
        template = copy.deepcopy(pipeline[checkpoint.ordinal])
        context_output = str(raw.get("context_output") or "")
        if (
            str(raw.get("role") or "") != checkpoint.role
            or template.role != checkpoint.role
            or str(raw.get("status") or "") != "complete"
            or agent_run_journal.digest_text(context_output)
            != checkpoint.context_digest
        ):
            raise agent_run_journal.AgentRunJournalCorrupt(
                "thread block context does not match its verified journal digest"
            )
        template.status = "complete"
        template.status_code = str(raw.get("status_code") or "")
        template.status_reason = str(raw.get("status_reason") or "")
        template.context_output = context_output
        template.output = context_output
        template.tool_calls = int(raw.get("tool_calls") or 0)
        template.duration_ms = float(raw.get("duration_ms") or 0.0)
        template.successful_writes = [
            str(item)
            for item in raw.get("successful_writes", [])
            if str(item).strip()
        ]
        template.verification_warning = str(
            raw.get("verification_warning") or ""
        )
        template.git_head = str(raw.get("git_head") or "")
        template.git_status = str(raw.get("git_status") or "")
        template.status_digest = str(raw.get("status_digest") or "")
        template.tracked_diff_digest = str(
            raw.get("tracked_diff_digest") or ""
        )
        template.untracked_digest = str(
            raw.get("untracked_digest") or ""
        )
        template.git_clean = bool(raw.get("git_clean", False))
        hydrated.append(template)
    return hydrated


def _validate_resume_contract(
    *,
    task: str,
    cfg: Config,
    pipeline: list[agent_blocks.AgentBlock],
    pipeline_name: str,
    journal: agent_run_journal.AgentRunJournal,
    snapshot: git_evidence.GitSnapshot,
) -> agent_run_journal.AgentResumeState:
    contract = journal.contract
    state = journal.resume_state()
    if state.terminal:
        raise agent_run_journal.AgentRunJournalError(
            "terminal Agent runs cannot be resumed in place"
        )
    if state.uncertain_mutation_steps:
        raise agent_run_journal.AgentRunJournalError(
            "a prior mutation outcome is uncertain and requires reconciliation"
        )
    contract.assert_live_authority(
        approval_mode=approval_mode_for_config(cfg),
        safe_mode=bool(cfg.safe_mode),
        session_preapproval=(
            False
            if approval_mode_for_config(cfg) == "never"
            else approval_mode_for_config(cfg) == "auto"
            or bool(cfg.auto_approve_active)
        ),
    )
    if contract.task_digest != agent_run_journal.digest_text(task):
        raise run_contracts.RunContractViolation(
            "resume task differs from the immutable run contract"
        )
    if contract.pipeline != pipeline_name:
        raise run_contracts.RunContractViolation(
            "resume pipeline differs from the immutable run contract"
        )
    current_root = str(Path(cfg.cwd).expanduser().resolve(strict=False))
    if current_root != contract.workspace.root:
        raise run_contracts.RunContractViolation(
            "resume workspace differs from the immutable run contract"
        )
    if len(pipeline) != len(contract.blocks):
        raise run_contracts.RunContractViolation(
            "resume pipeline shape differs from the immutable run contract"
        )
    for ordinal, (block, block_contract) in enumerate(
        zip(pipeline, contract.blocks)
    ):
        if (
            block_contract.ordinal != ordinal
            or block.role != block_contract.role
            or agent_run_journal.digest_text(block.prompt)
            != block_contract.prompt_digest
            or block.allowed_tools
            != frozenset(block_contract.configured_tools)
            or block.requires_change != block_contract.requires_change
        ):
            raise run_contracts.RunContractViolation(
                "resume block definition differs from the immutable run contract"
            )
    if not state.workspace_matches(snapshot):
        raise run_contracts.RunContractViolation(
            "workspace state differs from the last verified Agent checkpoint"
        )
    return state


def _capture_thread_workspace(
    cfg: Config,
    block: agent_blocks.AgentBlock | None = None,
) -> dict[str, Any]:
    """Keep optional thread metadata from interrupting the agent pipeline."""

    try:
        workspace = worktree_runtime.capture_workspace(cfg.cwd)
        if not workspace.get("available") and block is not None and block.git_head:
            workspace.update(
                {
                    "head": block.git_head,
                    "clean": block.git_clean,
                    "status": block.git_status,
                    "status_digest": block.status_digest,
                    "tracked_diff_digest": block.tracked_diff_digest,
                    "untracked_digest": block.untracked_digest,
                }
            )
        return workspace
    except Exception as exc:
        logger.debug("Could not capture thread workspace metadata: %s", exc)
        return {"available": False, "cwd": str(cfg.cwd), "error": str(exc)[:1_000]}


def _start_thread_record(
    task: str,
    cfg: Config,
    pipeline_name: str,
    *,
    thread_id: str | None,
    parent_id: str,
    contract: run_contracts.RunContract,
    checkpoint: dict[str, Any],
) -> str:
    workspace = _capture_thread_workspace(cfg)
    contract_link = _contract_thread_link(contract)
    try:
        if thread_id:
            agent_threads.begin_turn(
                thread_id,
                task,
                pipeline=pipeline_name,
                model=cfg.model,
                workspace=workspace,
                run_contract=contract_link,
                checkpoint=checkpoint,
            )
            return thread_id
        record = agent_threads.create_thread(
            task,
            pipeline=pipeline_name,
            model=cfg.model,
            parent_id=parent_id,
            status="queued",
            start_turn=True,
            workspace=workspace,
            run_contract=contract_link,
            checkpoint=checkpoint,
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
    workspace: dict[str, Any] | None = None,
    contract: run_contracts.RunContract | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> bool:
    if not thread_id:
        return False
    try:
        agent_threads.finish_turn(
            thread_id,
            status=status,
            output=output,
            error=error,
            blocks=blocks,
            pipeline=pipeline,
            workspace=workspace,
            run_contract=(
                _contract_thread_link(contract)
                if contract is not None
                else None
            ),
            checkpoint=checkpoint,
        )
        return True
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Could not finish agent thread record %s: %s", thread_id, exc)
        return False


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
    resume_journal: agent_run_journal.AgentRunJournal | None = None,
    resume_block_records: list[dict[str, Any]] | None = None,
    resume_direction: str = "",
) -> AgentRunResult:
    if not task.strip():
        show_error(AGENT_USAGE)
        return AgentRunResult(status="failed", pipeline=pipeline_name, error=AGENT_USAGE)
    reflex.begin_agent_pipeline(cfg)
    if thread_id is None and not parent_id and not prior_context.strip():
        resolve_agent_workspace(task, cfg)
    started = time.perf_counter()
    completed: list[agent_blocks.AgentBlock] = []
    resolved = resolve_pipeline_for_cli(pipeline_name)
    if resolved is None:
        return AgentRunResult(status="failed", pipeline=pipeline_name, error=f"Pipeline '{pipeline_name}' is unavailable.")
    pipeline, _pipeline_source = resolved
    record_pipeline = thread_pipeline_label or pipeline_name
    route = task_router.route_task(task)
    journal_lease = ExitStack()
    try:
        initial_contract_snapshot = git_evidence.capture_git_snapshot(cfg.cwd)
        if resume_journal is None:
            contract = run_contracts.compile_agent_run_contract(
                task=task,
                route=route,
                pipeline_name=record_pipeline,
                blocks=pipeline,
                cfg=cfg,
                approval_mode=approval_mode_for_config(cfg),
                snapshot=initial_contract_snapshot,
            )
            run_journal = agent_run_journal.AgentRunJournal.create(contract)
            journal_lease.enter_context(run_journal.execution_lease())
            contract_tracker = run_contracts.RunContractTracker(contract)
        else:
            run_journal = resume_journal
            journal_lease.enter_context(run_journal.execution_lease())
            resume_state = _validate_resume_contract(
                task=task,
                cfg=cfg,
                pipeline=pipeline,
                pipeline_name=record_pipeline,
                journal=run_journal,
                snapshot=initial_contract_snapshot,
            )
            contract = run_journal.contract
            completed = _hydrate_verified_blocks(
                pipeline,
                resume_block_records or [],
                run_journal,
            )
            contract_tracker = run_contracts.RunContractTracker.restore(
                contract,
                completed_blocks=resume_state.next_block_ordinal,
                model_rounds=resume_state.model_rounds,
                tool_calls=resume_state.tool_calls,
                prompt_tokens=resume_state.prompt_tokens,
            )
            run_journal.run_resumed(
                next_block_ordinal=resume_state.next_block_ordinal,
                last_verified_sequence=(
                    resume_state.last_verified_sequence
                ),
            )
    except (
        run_contracts.RunContractError,
        run_contracts.RunContractViolation,
        agent_run_journal.AgentRunJournalError,
        OSError,
    ) as exc:
        journal_lease.close()
        error = f"Agent run contract could not be compiled: {exc}"
        show_error(error)
        return AgentRunResult(
            status="failed",
            pipeline=record_pipeline,
            error=error,
        )
    active_thread_id = _start_thread_record(
        task,
        cfg,
        record_pipeline,
        thread_id=thread_id,
        parent_id=parent_id,
        contract=contract,
        checkpoint=_checkpoint_payload(run_journal),
    )
    if not active_thread_id:
        error = (
            "Agent run stopped because durable thread context could not be "
            "created."
        )
        try:
            run_journal.run_finished(
                status="failed",
                last_verified_sequence=-1,
            )
        except agent_run_journal.AgentRunJournalError:
            pass
        journal_lease.close()
        show_error(error)
        return AgentRunResult(
            status="failed",
            pipeline=record_pipeline,
            error=error,
            contract_id=contract.contract_id,
            contract_mode=contract.mode,
        )
    if active_thread_id:
        show_info(f"Agent thread {active_thread_id} · {record_pipeline}")
    show_info(
        f"Run contract {contract.digest[:12]} · {contract.mode} · "
        f"approval {contract.approval_mode}"
    )
    context_sources: list[agent_context.AgentContextSource] = []
    if resume_direction.strip():
        context_sources.append(
            agent_context.AgentContextSource(
                name="resume_direction",
                title="Resume Direction",
                body=resume_direction.strip(),
                priority=120,
                trust="user_resume_direction",
                scope="session",
                freshness_rank=1_000,
                provenance="user-resume-direction",
            )
        )
    if prior_context.strip():
        context_sources.append(
            agent_context.AgentContextSource(
                name="parent_handoff",
                title="Parent Thread Handoff",
                body=prior_context.strip(),
                priority=100,
                trust="verified_handoff",
                scope="session",
                freshness_rank=900,
                provenance="verified-parent-thread",
            )
        )
    from . import main as _main

    try:
        memory_catalog = memory_runtime.MemoryCatalog()
        memory_catalog.sync_legacy_facts(cfg.memories, authoritative=False)
        memory_hits = memory_catalog.search(
            task,
            embed_fn=_main.intuition_embed_fn(cfg),
            embedding_model=_main.harness.resolve_embed_model(cfg),
            tiers={"curated", "history"},
            scopes={memory_runtime.scope_for_workspace(cfg.cwd)},
        )
        memory_injection = memory_runtime.format_prompt_hits(memory_hits)
        if memory_injection:
            context_sources.append(
                agent_context.AgentContextSource(
                    name="governed_memory",
                    title="Relevant System Memory",
                    body=memory_injection,
                    priority=80,
                    trust="governed_memory",
                    scope="workspace",
                    freshness_rank=700,
                    provenance="julia-memory-runtime",
                )
            )
    except memory_runtime.MemorySystemError as exc:
        logger.debug("Agent pipeline governed memory recall failed: %s", exc)

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
                    context_sources.append(
                        agent_context.AgentContextSource(
                            name="intuition_memory",
                            title="Heuristic Intuition Memory",
                            body=injection,
                            priority=50,
                            trust="heuristic_memory",
                            scope="workspace",
                            freshness_rank=500,
                            provenance="intuition-runtime",
                        )
                    )
        except Exception as exc:
            logger.debug("Agent pipeline intuition recall failed: %s", exc)
    try:
        context_bundle = agent_context.build_agent_context(
            task,
            context_sources,
            max_tokens=max(
                256,
                int(
                    contract.budget.max_prompt_tokens_per_round
                    * 0.45
                ),
            ),
        )
        pipeline_task = context_bundle.text
        run_journal.context_bound(context_bundle.receipt.payload())
        if context_bundle.receipt.truncated_sources:
            show_info(
                "Agent context truncated by budget: "
                + ", ".join(
                    context_bundle.receipt.truncated_sources
                )
            )
        if context_bundle.receipt.omitted_sources:
            show_info(
                "Agent context omitted by budget: "
                + ", ".join(
                    context_bundle.receipt.omitted_sources
                )
            )
    except (
        agent_context.AgentContextError,
        agent_run_journal.AgentRunJournalError,
    ) as exc:
        error = f"Agent context could not be durably bound: {exc}"
        show_error(error)
        block_records = [_block_record(block) for block in completed]
        _finish_thread_record(
            active_thread_id,
            status="failed",
            output=completed[-1].output if completed else "",
            error=error,
            blocks=block_records,
            pipeline=record_pipeline,
            workspace=_capture_thread_workspace(cfg),
            contract=contract,
            checkpoint=_checkpoint_payload(run_journal),
        )
        try:
            state = run_journal.resume_state()
            if not state.uncertain_mutation_steps:
                run_journal.run_finished(
                    status="failed",
                    last_verified_sequence=(
                        state.last_verified_sequence
                    ),
                )
        except agent_run_journal.AgentRunJournalError:
            pass
        journal_lease.close()
        return AgentRunResult(
            thread_id=active_thread_id,
            status="failed",
            pipeline=record_pipeline,
            error=error,
            blocks=block_records,
            contract_id=contract.contract_id,
            contract_mode=contract.mode,
        )

    block_final_snapshots: dict[int, git_evidence.GitSnapshot] = {}

    def run_pipeline_block(
        block: agent_blocks.AgentBlock,
        *,
        ordinal: int | None = None,
        start_contract_block: bool = True,
        recovery_phase: str = "",
        recovery_attempt: int = 0,
    ) -> None:
        selected_block_contract = None
        if ordinal is not None:
            try:
                selected_block_contract = contract.block(ordinal)
                if start_contract_block:
                    run_journal.block_started(ordinal, block.role)
                    contract_tracker.start_block(ordinal)
            except (
                run_contracts.RunContractViolation,
                agent_run_journal.AgentRunJournalError,
            ) as exc:
                block.status = "failed"
                block.status_code = (
                    "run_contract_violation"
                    if isinstance(
                        exc,
                        run_contracts.RunContractViolation,
                    )
                    else "run_journal_unavailable"
                )
                block.status_reason = str(exc)
                block.output = (
                    "## Block Output\n\n"
                    f"Run contract rejected block execution: {exc}"
                )
                return
        before_git = (
            (
                initial_contract_snapshot
                if ordinal == 0
                else git_evidence.capture_git_snapshot(cfg.cwd)
            )
            if block.requires_change or tool_policy.supports_mutation_audit(block.allowed_tools)
            else None
        )
        def completion_check(
            completed_block: agent_blocks.AgentBlock,
            baseline=before_git,
        ) -> None:
            if baseline is not None:
                after_git = git_evidence.capture_git_snapshot(cfg.cwd)
                block_final_snapshots[id(completed_block)] = after_git
                completed_block.git_head = after_git.head or ""
                completed_block.git_status = after_git.status
                completed_block.status_digest = after_git.status_digest
                completed_block.tracked_diff_digest = after_git.tracked_diff_digest
                completed_block.untracked_digest = after_git.untracked_digest
                completed_block.git_clean = git_evidence.snapshot_is_clean(after_git)
                if completed_block.requires_change:
                    enforce_required_change_contract(completed_block, baseline, after_git)
                else:
                    capture_optional_mutation_audit(completed_block, baseline, after_git)
                if ordinal is not None:
                    run_journal.verifier_result(
                        ordinal=ordinal,
                        verifier=(
                            "post_mutation"
                            if completed_block.requires_change
                            else "mutation_audit"
                        ),
                        status=(
                            "passed"
                            if completed_block.status == "complete"
                            else "failed"
                        ),
                        snapshot=after_git,
                    )
            _enforce_block_output_verification(
                completed_block,
                completed,
            )
        run_agent_block(
            block,
            task=pipeline_task,
            completed=completed,
            cfg=cfg,
            client=client,
            route=route,
            completion_check=completion_check,
            run_contract=contract if selected_block_contract is not None else None,
            block_contract=selected_block_contract,
            contract_tracker=(
                contract_tracker if selected_block_contract is not None else None
            ),
            run_journal=(
                run_journal if selected_block_contract is not None else None
            ),
            block_ordinal=ordinal,
            recovery_phase=recovery_phase,
            recovery_attempt=recovery_attempt,
        )

    def run_typed_recovery(
        block: agent_blocks.AgentBlock,
        *,
        ordinal: int,
    ) -> None:
        block_contract = contract.block(ordinal)
        if (
            block.status_code not in block_contract.recovery_codes
            or block_contract.max_recovery_attempts < 1
        ):
            return
        retry_iterations = block_contract.recovery_max_iterations
        show_agent_recovery_start(
            block.role,
            block.status_reason,
            retry_iterations,
        )
        original_output = block.output
        replan = recovery_plan_block(block, cfg)
        retry: agent_blocks.AgentBlock | None = None
        try:
            run_journal.recovery_started(
                ordinal=ordinal,
                attempt=1,
                recovery_code=block.status_code,
            )
            run_pipeline_block(
                replan,
                ordinal=ordinal,
                start_contract_block=False,
                recovery_phase="plan",
                recovery_attempt=1,
            )
            if replan.status == "complete":
                retry = retry_implementation_block(block)
                retry.max_iterations = retry_iterations
                run_pipeline_block(
                    retry,
                    ordinal=ordinal,
                    start_contract_block=False,
                    recovery_phase="retry",
                    recovery_attempt=1,
                )
                block.status = retry.status
                block.status_code = retry.status_code
                block.status_reason = retry.status_reason
                block.tool_calls += retry.tool_calls
                block.duration_ms += retry.duration_ms
                block.successful_writes = list(
                    dict.fromkeys(
                        [
                            *block.successful_writes,
                            *retry.successful_writes,
                        ]
                    )
                )
                block.verification_warning = retry.verification_warning
                block.git_evidence = retry.git_evidence
                block.git_head = retry.git_head
                block.git_status = retry.git_status
                block.status_digest = retry.status_digest
                block.tracked_diff_digest = retry.tracked_diff_digest
                block.untracked_digest = retry.untracked_digest
                block.git_clean = retry.git_clean
                retry_snapshot = block_final_snapshots.pop(
                    id(retry),
                    None,
                )
                if retry_snapshot is not None:
                    block_final_snapshots[id(block)] = retry_snapshot
            recovery_output = (
                retry.output
                if retry is not None
                else "Recovery retry was not started."
            )
            block.output = (
                "## Block Output\n\n"
                "### Original attempt\n"
                f"{original_output}\n\n"
                "### Contract-bound recovery plan\n"
                f"{replan.output}\n\n"
                "### Recovery retry\n"
                f"{recovery_output}"
            )
            run_journal.recovery_finished(
                ordinal=ordinal,
                attempt=1,
                status=block.status,
                context_digest=agent_run_journal.digest_text(
                    agent_blocks.compact_block_output(block.output)[
                        : agent_threads.MAX_BLOCK_CONTEXT_CHARS
                    ]
                ),
            )
        except (
            run_contracts.RunContractViolation,
            agent_run_journal.AgentRunJournalError,
        ) as exc:
            block.status = "failed"
            block.status_code = "run_journal_unavailable"
            block.status_reason = f"Contract-bound recovery failed: {exc}"
            block.output = (
                "## Block Output\n\n"
                f"{block.status_reason}"
            )

    def append_pipeline_block(
        block: agent_blocks.AgentBlock,
        *,
        ordinal: int | None = None,
    ) -> bool:
        block.context_output = agent_blocks.compact_block_output(
            block.output
        )[: agent_threads.MAX_BLOCK_CONTEXT_CHARS]
        if ordinal is not None:
            try:
                snapshot = block_final_snapshots.pop(
                    id(block),
                    None,
                ) or git_evidence.capture_git_snapshot(cfg.cwd)
                output_verified = (
                    _enforce_block_output_verification(
                        block,
                        completed,
                    )
                )
                agent_threads.update_thread(
                    active_thread_id,
                    status="running",
                    blocks=[
                        _block_record(item)
                        for item in [*completed, block]
                    ],
                    workspace=_capture_thread_workspace(cfg, block),
                    run_contract=_contract_thread_link(contract),
                    checkpoint=_checkpoint_payload(run_journal),
                )
                run_journal.verifier_result(
                    ordinal=ordinal,
                    verifier=(
                        "final_output"
                        if block.role == "final"
                        else "block_output"
                    ),
                    status=(
                        "passed"
                        if output_verified
                        and block.status == "complete"
                        else "failed"
                    ),
                    snapshot=snapshot,
                )
                run_journal.block_finished(
                    ordinal=ordinal,
                    role=block.role,
                    status=block.status,
                    verified=(
                        block.status == "complete"
                        and not block.verification_warning
                        and output_verified
                    ),
                    context_digest=agent_run_journal.digest_text(
                        block.context_output
                    ),
                    snapshot=snapshot,
                )
            except (
                OSError,
                ValueError,
                KeyError,
                agent_run_journal.AgentRunJournalError,
            ) as exc:
                block.status = "failed"
                block.status_code = "run_journal_unavailable"
                block.status_reason = (
                    f"Durable block checkpoint failed: {exc}"
                )
                block.output = (
                    "## Block Output\n\n"
                    f"Block completion was withheld because its durable "
                    f"checkpoint failed: {exc}"
                )
                return False
        completed.append(block)
        if ordinal is not None:
            try:
                agent_threads.update_thread(
                    active_thread_id,
                    status="running",
                    blocks=[
                        _block_record(item)
                        for item in completed
                    ],
                    workspace=_capture_thread_workspace(cfg, block),
                    run_contract=_contract_thread_link(contract),
                    checkpoint=_checkpoint_payload(run_journal),
                )
            except (OSError, ValueError, KeyError) as exc:
                # The staged context was persisted before the authoritative
                # journal boundary, so resume can reconstruct this checkpoint
                # even if the redundant metadata refresh is unavailable.
                logger.debug(
                    "Could not refresh Agent thread checkpoint %s: %s",
                    active_thread_id,
                    exc,
                )
        return True

    terminal_block: agent_blocks.AgentBlock | None = (
        completed[-1] if completed else None
    )
    cancelled = False
    run_error = ""
    try:
        with _agent_execution_scope():
            for ordinal, block in enumerate(pipeline):
                if ordinal < len(completed):
                    continue
                terminal_block = block
                run_pipeline_block(block, ordinal=ordinal)
                if block.status not in {"complete", "partial"}:
                    detail = f" ({block.status_reason})" if block.status_reason else ""
                    show_error(f"Agent pipeline stopped at {block.role}: {block.status}{detail}")
                    break
                if should_recover_implementation(block):
                    run_typed_recovery(block, ordinal=ordinal)
                    if block.status not in {"complete", "partial"}:
                        detail = (
                            f" ({block.status_reason})"
                            if block.status_reason
                            else ""
                        )
                        show_error(
                            f"Agent pipeline stopped at {block.role}: "
                            f"{block.status}{detail}"
                        )
                        break
                if not append_pipeline_block(block, ordinal=ordinal):
                    show_error(
                        f"Agent pipeline stopped at {block.role}: "
                        f"{block.status_reason}"
                    )
                    break
                if block.status_code == "verification_missing":
                    show_error(
                        f"Agent pipeline stopped at {block.role}: post-mutation verification is missing."
                    )
                    break
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
        latest_git_block = next(
            (block for block in reversed(persisted_blocks) if block.git_head),
            None,
        )
        workspace = _capture_thread_workspace(cfg, latest_git_block)
        try:
            resume_state = run_journal.resume_state()
            if resume_state.uncertain_mutation_steps:
                status = "failed"
                error = (
                    "A mutation outcome lacks a durable checkpoint and must "
                    "be reconciled before resume."
                )
            elif (
                status == "complete"
                and resume_state.next_block_ordinal
                != len(contract.blocks)
            ):
                status = "failed"
                error = (
                    "Agent completion was withheld because not every "
                    "contract block reached a verified checkpoint."
                )
            checkpoint = _checkpoint_payload(run_journal)
            thread_persisted = _finish_thread_record(
                active_thread_id,
                status=status,
                output=output,
                error=error,
                blocks=block_records,
                pipeline=record_pipeline,
                workspace=workspace,
                contract=contract,
                checkpoint=checkpoint,
            )
            if not thread_persisted:
                status = "failed"
                error = (
                    "Agent terminal state could not be durably persisted; "
                    "the nonterminal journal was retained for recovery."
                )
            elif not resume_state.uncertain_mutation_steps:
                try:
                    run_journal.run_finished(
                        status=status,
                        last_verified_sequence=(
                            resume_state.last_verified_sequence
                        ),
                    )
                    agent_threads.update_thread(
                        active_thread_id,
                        checkpoint=_checkpoint_payload(run_journal),
                    )
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    agent_run_journal.AgentRunJournalError,
                ) as exc:
                    status = "failed"
                    error = (
                        "Agent run journal could not be finalized: "
                        f"{exc}"
                    )
                    _finish_thread_record(
                        active_thread_id,
                        status=status,
                        output=output,
                        error=error,
                        blocks=block_records,
                        pipeline=record_pipeline,
                        workspace=workspace,
                        contract=contract,
                        checkpoint=_checkpoint_payload(run_journal),
                    )
        except (
            OSError,
            agent_run_journal.AgentRunJournalError,
        ) as exc:
            status = "failed"
            error = f"Agent run journal could not be finalized: {exc}"
            _finish_thread_record(
                active_thread_id,
                status=status,
                output=output,
                error=error,
                blocks=block_records,
                pipeline=record_pipeline,
                workspace=workspace,
                contract=contract,
                checkpoint={},
            )
        finally:
            journal_lease.close()
        cfg.save()
        flush_perf_records()
    return AgentRunResult(
        thread_id=active_thread_id,
        status=status,
        pipeline=record_pipeline,
        output=output,
        error=error,
        blocks=block_records,
        contract_id=contract.contract_id,
        contract_mode=contract.mode,
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


def _run_contract_bound_specialist(
    *,
    task: str,
    route: task_router.TaskRoute,
    cfg: Config,
    client: Any,
    block: agent_blocks.AgentBlock,
    thread_id: str,
) -> agent_blocks.AgentBlock:
    """Run one team specialist with the same contract/journal loop as Agent."""

    lease = ExitStack()
    contract: run_contracts.RunContract | None = None
    journal: agent_run_journal.AgentRunJournal | None = None
    initial_snapshot = git_evidence.capture_git_snapshot(cfg.cwd)
    try:
        contract = run_contracts.compile_agent_run_contract(
            task=task,
            route=route,
            pipeline_name="specialist",
            blocks=[block],
            cfg=cfg,
            approval_mode=approval_mode_for_config(cfg),
            snapshot=initial_snapshot,
        )
        journal = agent_run_journal.AgentRunJournal.create(contract)
        lease.enter_context(journal.execution_lease())
        tracker = run_contracts.RunContractTracker(contract)
        agent_threads.begin_turn(
            thread_id,
            task,
            pipeline="specialist",
            model=contract.blocks[0].model,
            workspace=_capture_thread_workspace(cfg),
            run_contract=_contract_thread_link(contract),
            checkpoint=_checkpoint_payload(journal),
        )
        context_bundle = agent_context.build_agent_context(
            task,
            [],
            max_tokens=max(
                256,
                int(
                    contract.budget.max_prompt_tokens_per_round
                    * 0.45
                ),
            ),
        )
        journal.context_bound(context_bundle.receipt.payload())
        journal.block_started(0, block.role)
        tracker.start_block(0)
        run_agent_block(
            block,
            task=context_bundle.text,
            completed=[],
            cfg=cfg,
            client=client,
            route=route,
            run_contract=contract,
            block_contract=contract.block(0),
            contract_tracker=tracker,
            run_journal=journal,
            block_ordinal=0,
        )
        block.context_output = agent_blocks.compact_block_output(
            block.output
        )[: agent_threads.MAX_BLOCK_CONTEXT_CHARS]
        final_snapshot = git_evidence.capture_git_snapshot(cfg.cwd)
        output_verified = _block_output_is_verified(block)
        if (
            block.status == "complete"
            and agent_run_journal.workspace_view(initial_snapshot)
            != agent_run_journal.workspace_view(final_snapshot)
        ):
            block.status = "partial"
            block.status_code = "specialist_workspace_drift"
            block.status_reason = (
                "Workspace changed during the read-only specialist run."
            )
            block.verification_warning = block.status_reason
        if block.status == "complete" and not output_verified:
            block.status = "partial"
            block.status_code = "output_contract_failed"
            block.status_reason = (
                "Specialist output did not satisfy the required "
                "'## Block Output' evidence contract."
            )
            block.verification_warning = block.status_reason
        agent_threads.update_thread(
            thread_id,
            status="running",
            blocks=[_block_record(block)],
            workspace=_capture_thread_workspace(cfg, block),
            run_contract=_contract_thread_link(contract),
            checkpoint=_checkpoint_payload(journal),
        )
        journal.verifier_result(
            ordinal=0,
            verifier="block_output",
            status=(
                "passed"
                if block.status == "complete" and output_verified
                else "failed"
            ),
            snapshot=final_snapshot,
        )
        journal.block_finished(
            ordinal=0,
            role=block.role,
            status=block.status,
            verified=(
                block.status == "complete"
                and output_verified
                and not block.verification_warning
            ),
            context_digest=agent_run_journal.digest_text(
                block.context_output
            ),
            snapshot=final_snapshot,
        )
        state = journal.resume_state()
        terminal_status = (
            block.status
            if block.status in {
                "complete",
                "partial",
                "failed",
                "cancelled",
            }
            else "failed"
        )
        persisted = _finish_thread_record(
            thread_id,
            status=terminal_status,
            output=block.output,
            error=block.status_reason,
            blocks=[_block_record(block)],
            pipeline="specialist",
            workspace=_capture_thread_workspace(cfg, block),
            contract=contract,
            checkpoint=_checkpoint_payload(journal),
        )
        if not persisted:
            raise agent_run_journal.AgentRunJournalError(
                "specialist terminal state was not persisted"
            )
        if state.uncertain_mutation_steps:
            raise agent_run_journal.AgentRunJournalError(
                "read-only specialist recorded an uncertain mutation"
            )
        journal.run_finished(
            status=terminal_status,
            last_verified_sequence=state.last_verified_sequence,
        )
        agent_threads.update_thread(
            thread_id,
            checkpoint=_checkpoint_payload(journal),
        )
    except Exception as exc:
        block.status = "failed"
        block.status_code = (
            block.status_code or "specialist_contract_error"
        )
        block.status_reason = str(exc)
        block.output = (
            block.output
            or f"## Block Output\n\nSpecialist failed: {exc}"
        )
        block.context_output = agent_blocks.compact_block_output(
            block.output
        )[: agent_threads.MAX_BLOCK_CONTEXT_CHARS]
        if contract is not None and journal is not None:
            _finish_thread_record(
                thread_id,
                status="failed",
                output=block.output,
                error=block.status_reason,
                blocks=[_block_record(block)],
                pipeline="specialist",
                workspace=_capture_thread_workspace(cfg, block),
                contract=contract,
                checkpoint=_checkpoint_payload(journal),
            )
        else:
            _finish_specialist_thread(
                thread_id,
                block,
                error=block.status_reason,
            )
    finally:
        lease.close()
    return block


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
        workspace = _capture_thread_workspace(cfg)
        parent = agent_threads.create_thread(
            task,
            role="orchestrator",
            pipeline="team",
            model=cfg.model,
            status="running",
            title=f"Team: {' '.join(task.split())[:72]}",
            workspace=workspace,
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
                start_turn=False,
                title=f"{role}: {' '.join(task.split())[:64]}",
                workspace=workspace,
            )
            child_id = str(child["id"])
            child_ids.append(child_id)
            child_by_role[role] = child_id
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Could not initialize complete team thread tree: %s", exc)
        error = (
            "Agent team stopped because its durable thread tree could not "
            f"be created: {exc}"
        )
        show_error(error)
        return AgentRunResult(
            thread_id=parent_id,
            status="failed",
            pipeline="team",
            error=error,
            children=child_ids,
        )

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
                return _run_contract_bound_specialist(
                    task=task,
                    route=route,
                    cfg=member_cfg,
                    client=member_client,
                    block=block,
                    thread_id=child_by_role[role],
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
        ("workspace", record.get("workspace", {}).get("workspace_root") or "-"),
        ("branch", record.get("workspace", {}).get("branch") or "-"),
        ("HEAD", record.get("workspace", {}).get("head") or "-"),
        (
            "run contract",
            record.get("run_contract", {}).get("contract_id") or "-",
        ),
        (
            "contract mode",
            record.get("run_contract", {}).get("mode") or "-",
        ),
        (
            "approval mode",
            record.get("run_contract", {}).get("approval_mode") or "-",
        ),
        (
            "next block",
            record.get("checkpoint", {}).get(
                "next_block_ordinal",
                "-",
            ),
        ),
        (
            "checkpoint",
            record.get("checkpoint", {}).get(
                "last_verified_sequence",
                "-",
            ),
        ),
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


def _journal_for_thread(
    record: dict[str, Any],
) -> agent_run_journal.AgentRunJournal | None:
    link = record.get("run_contract")
    if not isinstance(link, dict) or not link:
        return None
    run_nonce = str(link.get("run_nonce") or "").strip()
    digest = str(link.get("digest") or "").strip()
    contract_id = str(link.get("contract_id") or "").strip()
    if not run_nonce or not digest or not contract_id:
        raise agent_run_journal.AgentRunJournalCorrupt(
            "thread run-contract link is incomplete"
        )
    journal = agent_run_journal.AgentRunJournal.load(run_nonce)
    if (
        journal.contract.digest != digest
        or journal.contract.contract_id != contract_id
    ):
        raise agent_run_journal.AgentRunJournalCorrupt(
            "thread run-contract link does not match the private journal"
        )
    return journal


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
    if lowered.startswith("switch "):
        try:
            record = agent_threads.resolve_thread(text.split(maxsplit=1)[1])
            restored = worktree_runtime.activate_thread_workspace(record, cfg)
        except (KeyError, worktree_runtime.WorktreeError) as exc:
            message = str(exc).strip("'")
            show_error(message)
            return f"Error: {message}"
        if not restored:
            message = "That thread has no recorded Git workspace."
            show_error(message)
            return f"Error: {message}"
        message = (
            f"Switched to thread {record['id']} workspace · "
            f"{record.get('workspace', {}).get('branch') or 'unknown branch'} · {cfg.cwd}"
        )
        show_info(message)
        return message
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
            same_worktree = False
            if action == "fork" and "--same-worktree" in parts[2:]:
                parts.remove("--same-worktree")
                same_worktree = True
            if len(parts) < 2 or (action == "fork" and len(parts) < 3):
                show_error(AGENT_THREAD_USAGE)
                return f"Error: {AGENT_THREAD_USAGE}"
            try:
                record = agent_threads.resolve_thread(parts[1])
            except KeyError as exc:
                message = str(exc).strip("'")
                show_error(message)
                return f"Error: {message}"
            structured_journal = None
            structured_state = None
            if action == "resume":
                try:
                    structured_journal = _journal_for_thread(record)
                    if structured_journal is not None:
                        structured_state = structured_journal.resume_state()
                        if structured_state.terminal:
                            structured_journal = None
                            structured_state = None
                except agent_run_journal.AgentRunJournalError as exc:
                    message = (
                        "Could not verify the durable Agent checkpoint: "
                        f"{exc}"
                    )
                    show_error(message)
                    return f"Error: {message}"
            if structured_state is not None:
                if structured_state.uncertain_mutation_steps:
                    message = (
                        "This Agent run has an uncertain mutation outcome. "
                        "Inspect the workspace and start a new run only after "
                        "reconciling the recorded step IDs: "
                        + ", ".join(
                            structured_state.uncertain_mutation_steps
                        )
                    )
                    show_error(message)
                    return f"Error: {message}"
                target = Path(
                    structured_state.contract.workspace.root
                ).expanduser().resolve(strict=False)
                if not target.is_dir():
                    message = f"Thread workspace is missing: {target}"
                    show_error(message)
                    return f"Error: {message}"
                cfg.cwd = str(target)
                cfg.save()
                restored = True
            else:
                try:
                    restored = worktree_runtime.activate_thread_workspace(
                        record,
                        cfg,
                    )
                except worktree_runtime.WorktreeError as exc:
                    message = str(exc)
                    show_error(message)
                    return f"Error: {message}"
            if restored:
                show_info(
                    f"Restored thread {record['id']} workspace: "
                    f"{record.get('workspace', {}).get('branch') or cfg.cwd}"
                )
            task = " ".join(parts[2:]).strip() or (
                "Continue from the latest verified state and finish "
                "remaining work."
            )
            if action == "fork" and restored and not same_worktree:
                source_state = worktree_runtime.capture_workspace(cfg.cwd)
                if not source_state.get("available"):
                    message = (
                        "Could not isolate forked thread because its parent Git state could not be verified. "
                        "Inspect the workspace; use --same-worktree only if it still matches the recorded "
                        "state, or start a new /worktree and agent thread."
                    )
                    show_error(message)
                    return f"Error: {message}"
                if source_state.get("clean") is not True:
                    message = (
                        "Could not isolate forked thread without dropping the parent workspace's "
                        "uncommitted tracked or untracked changes. Use --same-worktree to preserve "
                        "the current recorded state, or commit the changes and start a new /worktree "
                        "and agent thread."
                    )
                    show_error(message)
                    return f"Error: {message}"
                source_head = str(source_state.get("head") or "").strip()
                if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", source_head):
                    message = (
                        "Could not isolate forked thread because the verified parent HEAD is "
                        "missing or invalid. Inspect the workspace and start a new /worktree "
                        "and agent thread."
                    )
                    show_error(message)
                    return f"Error: {message}"
                try:
                    fork_workspace = worktree_runtime.create_worktree(
                        cfg.cwd,
                        f"{record['id']}-{task}",
                        base_ref=source_head,
                    )
                    worktree_runtime.activate_worktree(fork_workspace["id"], cfg)
                except worktree_runtime.WorktreeError as exc:
                    message = f"Could not isolate forked thread: {exc}"
                    show_error(message)
                    return f"Error: {message}"
                show_info(
                    f"Fork workspace {fork_workspace['id']} · {fork_workspace['branch']}"
                )
            elif action == "fork" and not restored and not same_worktree:
                show_info(
                    "Legacy thread has no recorded Git workspace; fork is using the current cwd. "
                    "Use /worktree new first for isolation."
                )
            handoff = agent_threads.context_handoff(record)
            run_task = task
            resume_kwargs: dict[str, Any] = {}
            if structured_journal is not None:
                run_task = str(record.get("task") or "").strip()
                if not run_task:
                    message = (
                        "Durable Agent checkpoint has no original task to "
                        "bind during resume."
                    )
                    show_error(message)
                    return f"Error: {message}"
                resume_kwargs = {
                    "resume_journal": structured_journal,
                    "resume_block_records": record.get("blocks", []),
                    "resume_direction": task,
                }
            result = run_agent_pipeline(
                run_task,
                cfg,
                client,
                pipeline_name=_pipeline_for_thread(record),
                thread_id=record["id"] if action == "resume" else None,
                parent_id=record["id"] if action == "fork" else "",
                prior_context=handoff,
                **resume_kwargs,
            )
            return _completed_agent_result_for_tool(
                result,
                task=task,
                cfg=cfg,
            )
    pipeline_name, task, error = parse_agent_invocation_checked(text)
    if error:
        show_error(error)
        return f"Error: {error}"
    return _completed_agent_result_for_tool(
        run_agent_pipeline(task, cfg, client, pipeline_name=pipeline_name),
        task=task,
        cfg=cfg,
    )
