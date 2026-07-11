"""Context window estimates, pruning, compaction, and system prompt assembly."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

from ollama import Client

from .config import Config, load_runtime_env
from . import harness
from . import identity
from . import model_info as _model_info_module
from . import reflex
from .chat_protocol import get_attr
from .display import json_sink

CONTEXT_COMPACT_THRESHOLD = 0.85
CONTEXT_KEEP_MESSAGES = 12
SMALL_CONTEXT_COMPACT_THRESHOLD = 0.70
SMALL_CONTEXT_KEEP_MESSAGES = 8
MEDIUM_CONTEXT_COMPACT_THRESHOLD = 0.78
MEDIUM_CONTEXT_KEEP_MESSAGES = 10
FOOTER_METRICS_FRESHNESS_SECONDS = 30.0
ATTEMPT_PROMPT_LIMIT = 24
OPTIONAL_CONTEXT_MIN_TOKENS = 96
OPTIONAL_CONTEXT_TRUNCATION_SUFFIX = "\n...[truncated by context budget]"

_SMALL_MODEL_THRESHOLD_B = 70.0

_CALIBRATION_BLOCK = (
    "\n\n## Accuracy Constraints (small-model mode)\n"
    "You are running as a compact model. Apply these rules strictly:\n"
    "- Never invent file paths, function names, version numbers, URLs, or command flags.\n"
    "- If you lack clear evidence for a specific fact, say 'I'm not certain — let me check' "
    "and use search_files, read_file, or harness_search to verify before stating it.\n"
    "- Prefer 'I don't know' over a confident wrong answer.\n"
    "- For code claims, verify with a tool call rather than relying on memory."
)

CONTEXT_USAGE_CACHE: tuple[tuple[Any, ...], int] | None = None


def invalidate_context_usage_cache() -> None:
    global CONTEXT_USAGE_CACHE
    CONTEXT_USAGE_CACHE = None


def _context_usage_cache_key(
    cfg: Config,
    *,
    lessons_fingerprint: int = 0,
    model_info_fingerprint: int = 0,
    user_message_fingerprint: int = 0,
) -> tuple[Any, ...]:
    last_message = cfg.messages[-1] if cfg.messages else {}
    return (
        len(cfg.messages),
        len(str(last_message.get("content", ""))),
        len(str(last_message.get("thinking", ""))),
        len(json.dumps(last_message.get("tool_calls", []), ensure_ascii=False, default=str)),
        len(cfg.session_summary),
        len(cfg.attempt_ledger),
        len(cfg.memories),
        cfg.num_ctx,
        cfg.system,
        identity.identity_mtime_key(),
        lessons_fingerprint,
        model_info_fingerprint,
        user_message_fingerprint,
        cfg.session_mode,
    )


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    total = 12
    total += estimate_text_tokens(str(message.get("role", "")))
    total += estimate_text_tokens(str(message.get("content", "")))
    total += estimate_text_tokens(str(message.get("thinking", "")))
    total += estimate_text_tokens(json.dumps(message.get("tool_calls", []), ensure_ascii=False, default=str))
    total += estimate_text_tokens(str(message.get("tool_name", "")))
    return total


@dataclass(frozen=True)
class OptionalContextBlock:
    name: str
    title: str
    body: str


def context_compaction_policy(model_info: dict[str, Any] | None = None) -> tuple[float, int]:
    """Return (threshold, keep_messages) for the active model/window."""
    info = model_info or {}
    size_b = _model_info_module.parameter_size_billions(info)
    native_ctx = _model_info_module.get_context_length(info)
    if (size_b is not None and size_b <= 9.0) or (native_ctx is not None and native_ctx <= 8192):
        return SMALL_CONTEXT_COMPACT_THRESHOLD, SMALL_CONTEXT_KEEP_MESSAGES
    if size_b is not None and size_b <= 32.0:
        return MEDIUM_CONTEXT_COMPACT_THRESHOLD, MEDIUM_CONTEXT_KEEP_MESSAGES
    return CONTEXT_COMPACT_THRESHOLD, CONTEXT_KEEP_MESSAGES


def context_response_reserve(runtime_cap: int, model_info: dict[str, Any] | None = None) -> int:
    """Reserve room for the assistant reply and tool-call metadata."""
    cap = max(1, int(runtime_cap or 1))
    size_b = _model_info_module.parameter_size_billions(model_info or {})
    if cap <= 4096 or (size_b is not None and size_b <= 9.0):
        reserve = max(384, cap // 6)
    elif size_b is not None and size_b <= 32.0:
        reserve = max(768, cap // 8)
    else:
        reserve = max(1024, cap // 10)
    return min(reserve, max(1, cap // 3))


def render_optional_context_block(block: OptionalContextBlock) -> str:
    body = (block.body or "").strip()
    if not body:
        return ""
    title = (block.title or "").strip()
    return f"## {title}\n{body}" if title else body


def _truncate_to_token_budget(text: str, token_budget: int) -> str:
    if token_budget < OPTIONAL_CONTEXT_MIN_TOKENS:
        return ""
    if estimate_text_tokens(text) <= token_budget:
        return text
    suffix_tokens = estimate_text_tokens(OPTIONAL_CONTEXT_TRUNCATION_SUFFIX)
    char_limit = max(0, (token_budget - suffix_tokens) * 4)
    if char_limit < 120:
        return ""
    candidate = text[:char_limit].rstrip() + OPTIONAL_CONTEXT_TRUNCATION_SUFFIX
    while estimate_text_tokens(candidate) > token_budget and char_limit > 120:
        char_limit -= 80
        candidate = text[:char_limit].rstrip() + OPTIONAL_CONTEXT_TRUNCATION_SUFFIX
    if estimate_text_tokens(candidate) > token_budget:
        return ""
    return candidate


def fit_optional_context_blocks(
    base_message: str,
    blocks: list[OptionalContextBlock],
    *,
    base_used_tokens: int,
    runtime_cap: int,
    model_info: dict[str, Any] | None = None,
) -> tuple[str, list[str], list[str], int]:
    """Append optional context blocks only while the request stays within budget."""
    budget = int(runtime_cap) - int(base_used_tokens) - context_response_reserve(runtime_cap, model_info)
    budget = max(0, budget)
    included: list[str] = []
    omitted: list[str] = []
    rendered_parts: list[str] = []
    used_tokens = 0
    for block in blocks:
        rendered = render_optional_context_block(block)
        if not rendered:
            continue
        cost = estimate_text_tokens("\n\n" + rendered)
        if cost <= budget:
            rendered_parts.append(rendered)
            included.append(block.name)
            budget -= cost
            used_tokens += cost
            continue
        truncated = _truncate_to_token_budget(rendered, budget)
        if truncated:
            truncated_cost = estimate_text_tokens("\n\n" + truncated)
            rendered_parts.append(truncated)
            included.append(block.name)
            budget = max(0, budget - truncated_cost)
            used_tokens += truncated_cost
        else:
            omitted.append(block.name)
    if not rendered_parts:
        return base_message, included, omitted, used_tokens
    return f"{base_message}\n\n" + "\n\n".join(rendered_parts), included, omitted, used_tokens


def build_system_prompt(
    cfg: Config,
    *,
    retrieved_lessons: list[str] | None = None,
    active_model_info: dict[str, Any] | None = None,
    user_message: str | None = None,
) -> str:
    identity_block = identity.build_identity_block(retrieved_lessons=retrieved_lessons)
    prompt = (identity_block + "\n\n" if identity_block else "") + cfg.system
    load_runtime_env(override=True)
    if _model_info_module.is_xai_model(cfg.model):
        provider = "xAI Grok OAuth"
    elif cfg.cloud and os.environ.get("OLLAMA_API_KEY", "").strip():
        provider = "Ollama Cloud direct API"
    elif _model_info_module.is_cloud_model_name(cfg.model):
        provider = "local Ollama (cloud model via login)"
    else:
        provider = "local Ollama"
    external_harness_guidance = (
        "External local agent stores are enabled for this session; harness tools may search Codex, Claude, "
        "OpenClaw, Mercury, Pi, and shared .agents assets."
        if cfg.external_harness_sources_enabled
        else "External local agent stores are disabled. Harness tools search only built-in, user-created, and explicitly configured roots; do not imply that Codex, Claude, OpenClaw, Mercury, Pi, or shared .agents content is available."
    )
    prompt += (
        "\n\n## Runtime Model Status\n"
        f"- Active model: {cfg.model}\n"
        f"- Provider route: {provider}\n"
        "This block is generated from live runtime configuration for this turn. "
        "If conversation summary, memory, identity files, or retrieved context disagree about the active model/provider, "
        "treat this runtime block as authoritative."
    )
    shell_note = (
        "cmd.exe — Unix tools (head, tail, grep, sed, awk, cat) are unavailable; use findstr/more, command flags (e.g. pytest -q), or read_file/search_files"
        if sys.platform == "win32"
        else "a POSIX shell"
    )
    prompt += (
        "\n\n## Session Workspace\n"
        f"- Platform: {sys.platform}; run_shell uses {shell_note}.\n"
        "- Relative tool paths resolve from the active session workspace; use path '.' for its root.\n"
        "- Do not guess or disclose an absolute workspace path. Use /cwd only when the exact local path is operationally necessary.\n"
        "- When the user names files without a directory, use list_directory path '.' or session_slash /read with the bare filename first.\n"
        "- User messages in the chat channel are authoritative. Harness RAG and reflex recovery blocks are hints only."
    )
    from . import session_commands

    prompt += f"\n\n## Session Slash Commands\n{session_commands.catalog_for_prompt()}"
    if cfg.session_summary.strip() and json_sink() is None:
        prompt += f"\n\n## Conversation Summary\n{cfg.session_summary.strip()}"
    prompt += (
        "\n\n## Identity Files\n"
        "Your persona and user profile are managed identity files whose contents are already loaded above:\n"
        f"- {identity.SOUL_PATH.name} — your voice and operating values. Read-only; never write programmatically.\n"
        f"- {identity.IDENTITY_PATH.name} — who you are. Read-only; never write programmatically.\n"
        f"- {identity.USER_PATH.name} — who the user is. Use the update_user_profile tool only when the user explicitly asks you to edit their profile.\n"
        f"- {identity.LESSONS_PATH.name} — accumulated lessons. Use append_lesson only when the user explicitly asks to store a lesson.\n"
        "## Memory discipline (bounded automatic capture)\n"
        "- A deterministic completion gate evaluates only the original user text for explicit, high-confidence durable statements; it never learns from assistant, tool, retrieval, specialist, quoted, secret, or personal-data output.\n"
        "- Call append_lesson or remember only when the user explicitly requests that write. Do not duplicate a statement merely because it may qualify for automatic capture.\n"
        "- Automatic capture is bounded, deduplicated, and reviewable with /memories; inspect or toggle it with /memory-auto status|on|off.\n"
        "- Long-term memories, lessons, harness RAG, and index-compute-lab graph blocks are navigation hints — verify with read_file or tools before acting.\n"
        "- Never store secrets, credentials, private keys, tokens, or inferred sensitive personal data.\n"
        "## Terminal efficiency\n"
        "- Prefer one decisive tool call over several speculative ones when the target is already known.\n"
        "- Do not narrate every tool call; summarize outcomes in plain language after work completes.\n"
        "- index-compute-lab canonical for this product is concept:algo-cli (legacy ollama-cli / ollama-cli-concept names in graph output are retired).\n"
        "The contents of all four files are already loaded into this system prompt above; do not read_file them just to see what they say. "
        "When the user references 'my wiki', 'my notes', or asks you to learn from their knowledge base, note that the harness RAG layer already injected the most relevant entries into the Relevant Context section (if present); use harness_search/harness_read only for explicit deep dives the retrieval missed."
        "\n\n## Local Harness Bridge\n"
        f"{external_harness_guidance} Use available_actions, harness_search, harness_read, harness_stats, and harness_refresh. "
        "These tools are read-only and should be preferred before broad filesystem scans when the user asks about skills, tools, prompts, memory, or wiki context."
        "\n\n## index-compute-lab (knowledge graph)\n"
        "When enabled, each user turn may include a ## Knowledge Graph (index-compute-lab) block: "
        "ranked associations from the user's configured index-compute-lab sources. "
        "Treat it like harness RAG — navigation and relationship hints, not proof that files exist. "
        "Use query_knowledge_graph for ranked co-occurrence (not prose biographies), and use harness_search for supporting documents. "
        "To persist a correction or contact before the next full reindex: write_knowledge_graph_note then harness_refresh. "
        "Use reindex_knowledge_graph only when the user explicitly asks to rebuild configured graph sources."
        "\n\n## Grok / xAI model compatibility\n"
        "When the user asks whether a grok-* model works in this harness, do not scan the repo blindly: "
        "tell them to run /model-check NAME (or run it yourself via session slash if available), "
        "and search the currently enabled harness for Algo CLI xAI/X-account guidance; do not assume external record IDs exist. "
        "Multi-agent grok models use xAI /v1/responses; other Grok models use /v1/chat/completions. "
        "Auth is optional subscription OAuth only: the user must provide XAI_CLIENT_ID, and tokens live in "
        "~/.algo_cli/xai_auth.json. Algo CLI bundles no xAI client identity."
        "\n\n## Capability Awareness\n"
        "When the user asks what you can do, what actions are available, what internal stats exist, or what tools/skills/memory/wiki you can access, call available_actions first."
        "\n\n## PDF Handling\n"
        "For PDF files, call read_pdf first. Do not claim Python is unavailable and do not improvise shell-based PDF extraction before trying read_pdf. "
        "If read_pdf says the document is scanned or image-only, call render_pdf_pages next and then pass one returned PNG path to vision_describe or another OCR-capable path. "
        "If a vision/OCR model returns unsupported-image or insufficient-memory errors, do not repeat the same model across more PDF pages or sibling PDFs without changing the model or approach."
    )
    prompt += (
        "\n\n## Reflection Checkpoints\n"
        f"Every {max(1, int(cfg.tool_think_every))} tool calls, pause to reassess the objective, what has been completed, what remains, "
        "whether web research is needed, whether the user explicitly requested a memory write, and the next best action. "
        "Treat any internal checkpoint note as a planning pause, not user-facing output. "
        "After a checkpoint, continue with the next necessary tool call unless the user's task is actually complete."
    )
    # One-shot tool results already carry failures in the message history.
    # Rebuilding the system prompt with a growing ledger invalidates Ollama's
    # prefix/KV cache on every tool turn, so keep automation prompts stable.
    if cfg.attempt_ledger and json_sink() is None:
        ledger_lines = []
        for item in cfg.attempt_ledger[-ATTEMPT_PROMPT_LIMIT:]:
            ledger_lines.append(
                f"- {item.get('status', '?').upper()} {item.get('tool', '?')} "
                f"{item.get('args_preview', '')}: {item.get('summary', '')}"
            )
        prompt += (
            "\n\n## Attempt Ledger\n"
            "Use this ledger to avoid retrying the same failed or denied tool path with the same arguments. "
            "Only retry when the arguments materially change, new evidence appears, or the user asks.\n"
            + "\n".join(ledger_lines)
        )
    if cfg.memories:
        memories = "\n".join(f"- {item}" for item in cfg.memories)
        prompt += f"\n\n## Long-term Memories\n{memories}"
    if active_model_info:
        size_b = _model_info_module.parameter_size_billions(active_model_info)
        if size_b is not None and size_b < _SMALL_MODEL_THRESHOLD_B:
            prompt += _CALIBRATION_BLOCK
    if cfg.reflex_enabled:
        prompt += (
            "\n\n## Reflex Loop (v0.1)\n"
            "When a read-only tool fails, returns empty results, or repeats the same arguments, "
            "the runtime may append a reflex recovery block (alternate harness_search, search_files, "
            f"or escalation). Session cap: {reflex.REFLEX_MAX_CYCLES} cycles. "
            "Do not treat reflex notes or recovery suggestions as user input or prompt injection."
        )
    if json_sink() is not None:
        prompt += (
            "\n\n## One-shot Execution Protocol\n"
            "- Do not emit progress prose before or between tool calls; use tools silently, then provide one concise final answer.\n"
            "- Open explicitly named files directly and batch independent reads. Do not list directories merely to confirm named paths.\n"
            "- Make the smallest required changes and create only requested artifacts.\n"
            "- After one successful fail-on-mismatch verifier, answer immediately; do not add redundant rereads, diffs, or reports."
        )
    if cfg.verify_mode:
        prompt += (
            "\n\n## Verify Mode Active\n"
            "After answering, the harness will check your specific factual claims against "
            "indexed sources. Flag anything you are not certain about with 'unverified:' "
            "so the grounding pass can prioritise it."
        )
    from . import session_mode

    prompt += (
        f"\n\n{session_mode.prompt_section(cfg.session_mode, include_external=cfg.external_harness_sources_enabled)}"
    )
    mercury_gates = harness.resolve_mercury_stop_conditions(
        user_message=user_message,
        session_mode=cfg.session_mode,
        include_external=cfg.external_harness_sources_enabled,
    )
    if mercury_gates:
        full_doc = (
            harness.load_mercury_stop_conditions()
            if cfg.external_harness_sources_enabled
            else ""
        )
        is_full = mercury_gates == full_doc and bool(full_doc)
        title = (
            "Stop Conditions (Mercury harness — full gates)"
            if is_full
            else "Stop Conditions (Mercury harness — compact)"
        )
        prompt += (
            f"\n\n## {title}\n"
            "Apply full gates only for external send/post, financial commitments, destructive actions, "
            "or unsourced consequential facts. For read-only file work, read live files before refusing.\n\n"
            f"{mercury_gates}"
        )
    return prompt


def estimate_context_usage(
    cfg: Config,
    *,
    prebuilt_system: str | None = None,
    lessons_fingerprint: int = 0,
    model_info_fingerprint: int = 0,
    user_message_fingerprint: int = 0,
) -> int:
    global CONTEXT_USAGE_CACHE
    cache_key = _context_usage_cache_key(
        cfg,
        lessons_fingerprint=lessons_fingerprint,
        model_info_fingerprint=model_info_fingerprint,
        user_message_fingerprint=user_message_fingerprint,
    )
    if CONTEXT_USAGE_CACHE and CONTEXT_USAGE_CACHE[0] == cache_key:
        return CONTEXT_USAGE_CACHE[1]
    system_text = prebuilt_system if prebuilt_system is not None else build_system_prompt(cfg)
    total = estimate_text_tokens(system_text)
    for message in cfg.messages:
        total += estimate_message_tokens(message)
    CONTEXT_USAGE_CACHE = (cache_key, total)
    return total


def estimate_usage_with_system_prompt(system_prompt: str, cfg: Config) -> int:
    """Token estimate for a fully-built system prompt plus current messages."""
    total = estimate_text_tokens(system_prompt)
    for message in cfg.messages:
        total += estimate_message_tokens(message)
    return total


def _last_chat_token_usage(runtime_status: dict[str, Any]) -> int | None:
    metrics = runtime_status.get("last_metrics") or {}
    if not isinstance(metrics, dict):
        return None
    timestamp = metrics.get("timestamp")
    if not timestamp or (time.time() - float(timestamp)) > FOOTER_METRICS_FRESHNESS_SECONDS:
        return None
    try:
        prompt = int(metrics.get("prompt_eval_count") or 0)
        completion = int(metrics.get("eval_count") or 0)
    except (TypeError, ValueError):
        return None
    total = prompt + completion
    return total if total > 0 else None


def context_status(
    cfg: Config,
    *,
    client: Any | None = None,
    model_info: dict[str, Any] | None = None,
    runtime_status: dict[str, Any] | None = None,
    usage_override: int | None = None,
) -> tuple[int, int, int, int, int | None]:
    """Return (used, display_total, remaining, runtime_cap, native_ctx)."""
    if runtime_status is None:
        from . import main as _main

        runtime_status = _main.RUNTIME_STATUS
    if model_info is None:
        model_info = runtime_status.get("model_info")
    if not isinstance(model_info, dict) or not model_info:
        model_info = _model_info_module.resolve_model_info(cfg, client)

    runtime_cap, native_ctx = _model_info_module.effective_context_limits(cfg, model_info)
    display_total = runtime_cap
    display_total = max(int(display_total), 1)

    estimated = usage_override if usage_override is not None else estimate_context_usage(cfg)
    api_used = _last_chat_token_usage(runtime_status)
    used = api_used if api_used is not None else estimated
    if api_used is not None:
        used = max(api_used, estimated)
    used = min(max(int(used), 0), display_total)
    remaining = max(display_total - used, 0)
    return used, display_total, remaining, runtime_cap, native_ctx


def summarize_message_batch(
    cfg: Config,
    batch: list[dict[str, Any]],
    fallback_client: Client | None = None,
    *,
    maintenance_client_fn: Callable[[Config, Client | None], tuple[Client, str]] | None = None,
) -> str:
    if not batch:
        return cfg.session_summary
    source_lines = []
    if cfg.session_summary.strip():
        source_lines.append("CURRENT SUMMARY:")
        source_lines.append(cfg.session_summary.strip())
        source_lines.append("")
    source_lines.append("MESSAGES TO COMPRESS:")
    for item in batch:
        role = item.get("role", "message")
        content = item.get("content") or item.get("thinking") or ""
        tool_name = item.get("tool_name")
        if tool_name:
            source_lines.append(f"- {role} [{tool_name}]: {content}")
        else:
            source_lines.append(f"- {role}: {content}")
    prompt = "\n".join(source_lines)
    system = (
        "You compress conversation state for a terminal coding assistant.\n"
        'Return JSON with a single key "summary" whose value is a concise continuation '
        "with only stable facts, decisions, file paths, tool results, and unresolved tasks. "
        "Omit filler and repeated content. Keep it under 500 words."
    )
    try:
        if maintenance_client_fn is None:
            from . import main as _main

            maintenance_client_fn = _main.small_maintenance_client
        summary_client, summary_model = maintenance_client_fn(cfg, fallback_client)
        response = summary_client.chat(
            model=summary_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            stream=False,
            think=False,
            format="json",
            keep_alive=cfg.keep_alive,
            options={"temperature": 0.1, "num_ctx": min(cfg.num_ctx, 4096), "num_predict": 600},
        )
        content = get_attr(get_attr(response, "message", {}), "content", "")
        if content:
            try:
                return str(json.loads(content).get("summary", content)).strip()
            except (json.JSONDecodeError, AttributeError):
                return str(content).strip()
    except Exception:
        pass
    fallback = []
    if cfg.session_summary.strip():
        fallback.append(cfg.session_summary.strip())
    for item in batch[-4:]:
        role = item.get("role", "message")
        content = (item.get("content") or item.get("thinking") or "")[:240]
        fallback.append(f"{role}: {content}")
    return "\n".join(fallback).strip()


def _tool_call_id(call: Any) -> str | None:
    if isinstance(call, dict):
        return call.get("id") or (call.get("function") or {}).get("id")
    return getattr(call, "id", None)


def _strip_tool_call_at(
    messages: list[dict[str, Any]],
    assistant_index: int,
    *,
    call_id: str | None = None,
) -> bool:
    if assistant_index < 0 or assistant_index >= len(messages):
        return False
    prev = messages[assistant_index]
    calls = list(prev.get("tool_calls") or [])
    if not calls:
        return False
    if call_id:
        filtered = [c for c in calls if _tool_call_id(c) != call_id]
        if len(filtered) == len(calls):
            return False
    else:
        filtered = calls[1:]
    new_prev = dict(prev)
    if filtered:
        new_prev["tool_calls"] = filtered
    else:
        new_prev.pop("tool_calls", None)
    messages[assistant_index] = new_prev
    return True


def prune_stale_tool_messages(cfg: Config) -> int:
    total = len(cfg.messages)
    if total <= cfg.prune_after_messages:
        return 0
    keep_from = max(0, total - cfg.prune_keep_recent)
    if keep_from == 0:
        return 0

    removed = 0
    new_messages: list[dict[str, Any]] = []
    call_id_to_assistant: dict[str, int] = {}
    pending_assistants: list[int] = []

    for index, message in enumerate(cfg.messages):
        if index >= keep_from:
            new_messages.append(message)
            continue
        role = message.get("role")
        if role == "assistant":
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                pending_assistants.append(len(new_messages))
            for call in tool_calls:
                cid = _tool_call_id(call)
                if cid:
                    call_id_to_assistant[cid] = len(new_messages)
            new_messages.append(message)
            continue
        if role != "tool":
            new_messages.append(message)
            continue
        call_id = message.get("tool_call_id")
        if call_id and call_id in call_id_to_assistant:
            idx = call_id_to_assistant.pop(call_id)
            _strip_tool_call_at(new_messages, idx, call_id=call_id)
        elif not call_id:
            while pending_assistants:
                idx = pending_assistants[0]
                if idx >= len(new_messages) or not (new_messages[idx].get("tool_calls") or []):
                    pending_assistants.pop(0)
                    continue
                _strip_tool_call_at(new_messages, idx)
                break
        removed += 1

    if removed:
        cfg.messages = new_messages
        from . import perf_telemetry

        perf_telemetry.record_perf_event(
            "prune",
            removed=removed,
            kept=len(new_messages),
            threshold=cfg.prune_after_messages,
        )
    return removed


def _split_for_compaction(messages: list[dict[str, Any]], keep_count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keep_from = max(0, len(messages) - keep_count)
    while keep_from > 0 and messages[keep_from].get("role") == "tool":
        keep_from -= 1
        while keep_from > 0 and messages[keep_from].get("role") == "tool":
            keep_from -= 1
    return messages[:keep_from], messages[keep_from:]


def maybe_compact_context(
    client: Client,
    cfg: Config,
    *,
    precomputed_used: int | None = None,
    model_info: dict[str, Any] | None = None,
) -> bool:
    used, total, _remaining, _runtime_cap, _native = context_status(
        cfg,
        client=client,
        model_info=model_info,
        usage_override=precomputed_used,
    )
    if total <= 0:
        return False
    compact_threshold, keep_messages = context_compaction_policy(model_info)
    if used < total * compact_threshold:
        return False
    if len(cfg.messages) <= keep_messages:
        return False
    batch, kept = _split_for_compaction(cfg.messages, keep_messages)
    if not batch:
        return False
    started = time.perf_counter()
    from . import main as _main
    from . import perf_telemetry

    summary = summarize_message_batch(
        cfg, batch, client, maintenance_client_fn=_main.small_maintenance_client
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    cfg.session_summary = summary
    cfg.messages = kept
    cfg.save()
    perf_telemetry.record_perf_event(
        "compaction",
        duration_ms=duration_ms,
        messages_compacted=len(batch),
        keep_messages=keep_messages,
        threshold=compact_threshold,
    )
    return True


def rebuild_context_summary(client: Client, cfg: Config) -> tuple[bool, str]:
    if len(cfg.messages) <= CONTEXT_KEEP_MESSAGES:
        return False, f"Need more than {CONTEXT_KEEP_MESSAGES} messages to compact."
    batch, kept = _split_for_compaction(cfg.messages, CONTEXT_KEEP_MESSAGES)
    if not batch:
        return False, "No safe message boundary found for compaction."
    started = time.perf_counter()
    from . import main as _main
    from . import perf_telemetry

    summary = summarize_message_batch(
        cfg, batch, client, maintenance_client_fn=_main.small_maintenance_client
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    cfg.session_summary = summary
    cfg.messages = kept
    cfg.save()
    perf_telemetry.record_perf_event("compaction", duration_ms=duration_ms, messages_compacted=len(batch), manual=True)
    return True, f"Context summary rebuilt from {len(batch)} messages; kept {len(kept)} recent messages."
