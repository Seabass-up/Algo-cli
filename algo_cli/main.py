
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
import importlib
import json
import logging
import os
import shutil
import shlex
import subprocess
import sys
import time
from typing import Any
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ollama import Client
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from rich import box
from rich.table import Table

from .config import (
    CODE_RAG_CONSENT_VERSION,
    CONFIG_DIR,
    Config,
    PROMPT_HISTORY_FILE,
    load_runtime_env,
    has_legacy_data,
    perform_legacy_migration,
    get_legacy_backup_dir,
    migrate_legacy_sidecar_files,
    NEW_ENV_PREFIX,
    OLD_ENV_PREFIX,
    LEGACY_CONFIG_DIR,
    _atomic_write_text,
    code_rag_consent_granted,
)
from . import agent_blocks  # noqa: F401 — tests patch main.agent_blocks
from . import git_evidence  # noqa: F401 — tests patch main.git_evidence
from . import harness
from . import identity
from . import code_rag
from . import execution_guardrails
from . import model_info as _model_info_module
from . import model_profile
from . import memory_runtime
from . import reasoning_bridge
from . import reconciliation
from . import task_ledger
from . import skills
from . import task_router  # noqa: F401 — tests use main.task_router
from . import verify as _verify_module
from . import xai_auth
from . import chatgpt_auth
from . import chatgpt_client
from . import google_workspace_auth
from . import google_workspace
from . import x_account
from .display import (
    compact_path,
    _format_bytes,
    console,
    current_theme_name,
    show_error,
    show_banner,
    show_status_footer,
    show_info,
    start_streaming_response,
    show_stream_text,
    show_thinking_text,
    finish_thinking_block,
    show_tool_call,
    show_tool_result,
    show_recalled_context,
    show_session_overview,  # noqa: F401 — re-exported for slash_dispatch (m.show_session_overview)
    finish_streaming_response,
    theme_colors,
    set_theme,
    json_sink,
    tool_execution_status,
)
from . import tools as tools_module
from .chat_protocol import (
    collapse_tool_history_for_gemini,
    get_attr,
    normalize_tool_call,
    serialize_tool_call,
)
from .model_routing import (
    effective_runtime_host,  # noqa: F401 — re-exported for tests and oneshot callers
    runtime_mode_label,  # noqa: F401 — re-exported for slash/dashboard callers
    is_cloud_model_name,
    is_embedding_model_name,
    is_vision_model_name,
    require_cloud_api_key,  # noqa: F401
    uses_ollama_cloud,  # noqa: F401
)
from .runtime_services import (
    SERVER_READY_CACHE,
    client_for_model,  # noqa: F401 — re-exported for tests
    create_client,
    host_is_local,
    ollama_server_ready,
    scoped_tool_runtime_env,
    start_local_ollama_host,
    start_ollama_server,
    start_supplemental_gateway,
)
from .runtime_qos import order_tool_batch_by_qos
from .perf_telemetry import (
    flush_perf_records,
    log_embed_perf,
    record_chat_metrics,
    record_perf_event,
)
from .tool_runtime import (
    RuntimeToolPreflight,
    ask_approval,
    augment_tool_result_with_reflex,
    classify_tool_status,
    find_failed_attempt,
    preflight_runtime_tool,
    record_tool_attempt,
    reflection_checkpoint,
    run_tool,
    run_args_preview as _run_args_preview,
    tool_attempt_signature,
    tool_result_message,
    tool_runtime_args,
)
from .tool_context import select_tools_for_prompt
from .tool_schema import estimate_tool_schema_tokens
from .slash_dispatch import SLASH_COMMANDS, SlashCommandCompleter, handle_command, unknown_command_message
from .agent_pipeline import (  # noqa: F401
    _session_pipeline_blocks,
    AgentRunResult,
    MAX_RECOVERY_IMPLEMENT_ITERATIONS,
    RECOVERABLE_IMPLEMENT_CODES,
    agent_execution_active,
    agent_usage_text,
    capture_optional_mutation_audit,
    clear_session_pipeline_blocks,
    enforce_required_change_contract,
    execute_agent_command,
    maybe_show_route_suggestion,
    parse_agent_invocation_checked,
    parse_agent_invocation,
    parse_agent_team_invocation,
    recovery_plan_block,
    resolve_agent_workspace,
    resolve_pipeline_for_cli,
    retry_implementation_block,
    run_agent_block,
    run_agent_pipeline,
    run_agent_team,
    session_pipeline_blocks,
    should_recover_implementation,
    show_agent_thread,
    show_agent_threads,
    show_task_route,
)
from .context_budget import (
    CONTEXT_COMPACT_THRESHOLD,
    CONTEXT_KEEP_MESSAGES,
    FOOTER_METRICS_FRESHNESS_SECONDS,
    OptionalContextBlock,
    build_system_prompt,
    context_status,
    estimate_context_usage,  # noqa: F401
    estimate_message_tokens,  # noqa: F401
    estimate_text_tokens,  # noqa: F401
    estimate_usage_with_system_prompt,
    fit_optional_context_blocks,
    invalidate_context_usage_cache,
    maybe_compact_context,
    prune_stale_tool_messages,
    rebuild_context_summary,
    summarize_message_batch,  # noqa: F401
)
from .context_budget import _last_chat_token_usage as _last_chat_token_usage_for
from .context_budget import _tool_call_id  # noqa: F401
from . import small_context


def _last_chat_token_usage() -> int | None:
    return _last_chat_token_usage_for(RUNTIME_STATUS)

ALL_TOOLS = tools_module.ALL_TOOLS
TOOL_MAP = tools_module.TOOL_MAP
logger = logging.getLogger(__name__)

# ---------- Cognitive Stack ----------
# Keep runtime engines package-local. The OpenClaw workspace is an R&D sandbox,
# not a stable import surface for this CLI.
try:
    from .intuition_engine import IntuitionEngine as _IntuitionEngineCls
except ImportError:
    _IntuitionEngineCls = None  # type: ignore[assignment,misc]


def _make_engine(cls: type | None) -> Any:
    if cls is None:
        logger.debug("Cognitive engine class is unavailable.")
        return None
    try:
        instance = cls()
        logger.debug("Cognitive engine %s initialized.", cls.__name__)
        return instance
    except Exception as exc:
        logger.debug("Cognitive engine %s unavailable: %s", cls.__name__, exc)
        return None


_intuition_engine = _make_engine(_IntuitionEngineCls)

CLOUD_MODEL_CHOICES = [
    "glm-4.6:cloud",
    "gpt-oss:20b-cloud",
    "gpt-oss:120b-cloud",
    "qwen3-coder:480b-cloud",
    "qwen3:235b-cloud",
    "qwen3-vl:235b-cloud",
    "deepseek-v3.1:671b-cloud",
]
# xAI Grok models routed via subscription OAuth only. See xai_client.py.
# These are fallback names after OAuth is present but /v1/models is unavailable.
# Never add API-key fallback behavior here.
XAI_MODEL_CHOICES = [
    "grok-4.3",
    "grok-4.20-0309-reasoning",
    "grok-4.20-0309-non-reasoning",
    "grok-4.20-multi-agent-0309",
]
# ChatGPT/Codex models routed via subscription OAuth. These are fallback names
# for the picker because ChatGPT OAuth tokens can be valid for Codex while
# api.openai.com model listing is unavailable or missing model.request scope.
CHATGPT_MODEL_CHOICES = [
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
    "gpt-5.1-codex",
]
MAINTENANCE_CLOUD_MODEL = "glm-5.1:cloud"

ATTEMPT_PROMPT_LIMIT = 24
LOCAL_MODEL_LIST_TTL_SECONDS = 60.0  # increased: avoid re-querying Ollama after every generation

RUNTIME_STATUS: dict[str, Any] = {}
LOCAL_MODEL_CACHE: dict[str, tuple[float, list[str]]] = {}


def sanitize_prompt_text(text: str) -> str:
    """Remove lone surrogate code points before history or tool writes."""
    if not text:
        return text
    return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


PROMPT_HISTORY_MAX_ENTRIES = 500
PROMPT_HISTORY_MAX_BYTES = 2 * 1024 * 1024
PROMPT_HISTORY_MAX_ENTRY_CHARS = 100_000
PROMPT_HISTORY_COMPACT_EVERY = 32


class SafeFileHistory(FileHistory):
    """Wrap prompt_toolkit FileHistory so invalid surrogate text never gets persisted."""

    def __init__(self, path: str) -> None:
        history_path = Path(path)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        if history_path.is_symlink():
            raise OSError("prompt history path must not be a symlink")
        if os.name == "posix":
            os.chmod(history_path.parent, 0o700)
            if history_path.exists():
                os.chmod(history_path, 0o600)
        self._stores_since_compaction = 0
        super().__init__(path)

    def store_string(self, string: str) -> None:
        safe = sanitize_prompt_text(string)[:PROMPT_HISTORY_MAX_ENTRY_CHARS]
        super().store_string(safe)
        path = Path(str(self.filename))
        if os.name == "posix":
            os.chmod(path, 0o600)
        self._stores_since_compaction += 1
        try:
            oversized = path.stat().st_size > PROMPT_HISTORY_MAX_BYTES
        except OSError:
            oversized = False
        if oversized or self._stores_since_compaction >= PROMPT_HISTORY_COMPACT_EVERY:
            self._compact_private_history()

    def append_string(self, string: str) -> None:
        super().append_string(sanitize_prompt_text(string))

    @staticmethod
    def _history_block(value: str) -> str:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = "".join(f"+{line}\n" for line in value.split("\n"))
        return f"\n# {timestamp}\n{lines}"

    def _compact_private_history(self) -> None:
        newest = list(self.load_history_strings())
        kept_newest: list[str] = []
        retained_bytes = 0
        for loaded_item in newest[:PROMPT_HISTORY_MAX_ENTRIES]:
            # prompt_toolkit's history reader can retain carriage returns from
            # files written in Windows text mode.  Do not compound them on each
            # bounded-history rewrite.
            item = loaded_item.replace("\r", "")
            block = self._history_block(item)
            block_bytes = len(block.encode("utf-8"))
            if retained_bytes + block_bytes > PROMPT_HISTORY_MAX_BYTES:
                break
            kept_newest.append(item)
            retained_bytes += block_bytes
        payload = "".join(self._history_block(item) for item in reversed(kept_newest))
        _atomic_write_text(Path(str(self.filename)), payload)
        if os.name == "posix":
            os.chmod(self.filename, 0o600)
        self._loaded_strings = list(kept_newest)
        self._stores_since_compaction = 0


def _chip(label: str, value: str, *, fg: str, bg: str, value_fg: str) -> str:
    del bg
    return (
        f'<style fg="{fg}"><b>{escape(label)}</b></style>'
        f'<style fg="{value_fg}"> {escape(value)}</style>'
    )


_LAST_REFRESH_TIME: float = 0.0
_REFRESH_MIN_INTERVAL_S: float = 2.0  # Skip redundant refresh within this window


def refresh_runtime_status(cfg: Config, client: Any | None = None, *, force: bool = False) -> None:
    """Update the runtime status dict used by the toolbar and status footer.

    Skips redundant refreshes within _REFRESH_MIN_INTERVAL_S unless force=True.
    """
    global _LAST_REFRESH_TIME
    now = time.monotonic()
    if not force and (now - _LAST_REFRESH_TIME) < _REFRESH_MIN_INTERVAL_S:
        return
    _LAST_REFRESH_TIME = now
    model_info = _model_info_module.resolve_model_info(cfg, client)
    used, total, remaining, runtime_cap, native_ctx = context_status(
        cfg, client=client, model_info=model_info
    )
    if total > 0:
        pct_left = int((remaining / total) * 100)
        context = f"{used}/{total} ({pct_left}% left)"
    else:
        pct_left = None
        context = "unknown"
    last_metrics = RUNTIME_STATUS.get("last_metrics")
    RUNTIME_STATUS.clear()
    local_models: list[str] = []
    is_xai = _model_info_module.is_xai_model(cfg.model)
    is_chatgpt = _model_info_module.is_chatgpt_model(cfg.model)
    if not cfg.cloud and not is_xai and not is_chatgpt:
        local_models = local_model_names(cfg)
    if is_xai:
        mode = "xai"
    elif is_chatgpt:
        mode = "chatgpt"
    elif cfg.cloud:
        mode = "cloud"
    else:
        mode = "local"
    RUNTIME_STATUS.update(
        {
            "context": context,
            "context_used": used,
            "context_total": total,
            "context_native": native_ctx,
            "context_runtime_cap": runtime_cap,
            "context_pct_left": pct_left,
            "model_info": model_info,
            "local_models": local_models,
            "cwd": compact_path(cfg.cwd, 32),
            "theme": cfg.theme,
            "model": cfg.model,
            "mode": mode,
            "auto_mode": cfg.auto_approve_active,
            "safe_mode": cfg.safe_mode,
            "tool_think_every": max(1, int(cfg.tool_think_every)),
            "max_tool_iterations": max(1, int(cfg.max_tool_iterations)),
            "memory_count": len(cfg.memories),
        }
    )
    if last_metrics is not None:
        RUNTIME_STATUS["last_metrics"] = last_metrics


def _ftr_chip(text: str, fg: str, *, bold: bool = False) -> str:
    inner = escape(text)
    if bold:
        inner = f"<b>{inner}</b>"
    return f'<style fg="{fg}">{inner}</style>'


def _ftr_sep(palette: dict[str, str]) -> str:
    return f'<style fg="{palette["muted"]}"> · </style>'


def _format_short_count(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "?"
    if n >= 1_000_000:
        formatted = f"{n / 1_000_000:.1f}M"
        return formatted.replace(".0M", "M")
    if n >= 1000:
        formatted = f"{n / 1000:.1f}k"
        return formatted.replace(".0k", "k")
    return str(n)


def _connectivity_dot(cfg: Config, palette: dict[str, str]) -> str:
    if (
        _model_info_module.is_xai_model(cfg.model)
        or _model_info_module.is_chatgpt_model(cfg.model)
        or cfg.cloud
    ):
        color = palette["info"]
    else:
        cached = SERVER_READY_CACHE.get(cfg.host)
        if cached and cached[1]:
            color = palette["success"]
        elif cached:
            color = palette["error"]
        else:
            color = palette["muted"]
    return f'<style fg="{color}">●</style>'


def _context_chip(palette: dict[str, str]) -> str:
    used = RUNTIME_STATUS.get("context_used")
    total = RUNTIME_STATUS.get("context_total")
    native = RUNTIME_STATUS.get("context_native")
    runtime_cap = RUNTIME_STATUS.get("context_runtime_cap")
    pct_left = RUNTIME_STATUS.get("context_pct_left")
    if not total or pct_left is None:
        return _ftr_chip("▣ ctx ?", palette["muted"])
    if pct_left >= 50:
        color = palette["muted"]
        warn = ""
    elif pct_left >= 20:
        color = palette["warning"]
        warn = ""
    else:
        color = palette["error"]
        warn = " ⚠"
    body = f"▣ {_format_short_count(used)}/{_format_short_count(total)} {pct_left}%{warn}"
    if (
        isinstance(native, int)
        and native > 0
        and isinstance(runtime_cap, int)
        and runtime_cap > 0
        and native > runtime_cap
    ):
        body += f" · cap {_format_short_count(runtime_cap)}"
    return _ftr_chip(body, color)


def _token_rate_chip(palette: dict[str, str]) -> str | None:
    metrics = RUNTIME_STATUS.get("last_metrics") or {}
    if not isinstance(metrics, dict):
        return None
    timestamp = metrics.get("timestamp")
    if not timestamp or (time.time() - float(timestamp)) > FOOTER_METRICS_FRESHNESS_SECONDS:
        return None
    eval_count = metrics.get("eval_count")
    eval_duration = metrics.get("eval_duration")
    try:
        count = float(eval_count or 0)
        duration_s = float(eval_duration or 0) / 1_000_000_000.0
    except (TypeError, ValueError):
        return None
    if count <= 0 or duration_s <= 0:
        return None
    rate = count / duration_s
    return _ftr_chip(f"{rate:.0f} tok/s", palette["info"])


def build_status_toolbar(cfg: Config):
    palette = theme_colors(cfg.theme)
    sep = _ftr_sep(palette)
    parts: list[str] = []

    parts.append(" ")
    parts.append(_connectivity_dot(cfg, palette))
    parts.append(" ")
    parts.append(_ftr_chip(RUNTIME_STATUS.get("model", cfg.model), palette["text"], bold=True))
    parts.append(sep)
    mode = RUNTIME_STATUS.get("mode", "local")
    parts.append(_ftr_chip(mode, palette["info"] if mode in {"cloud", "xai", "chatgpt"} else palette["muted"]))
    parts.append(sep)
    parts.append(_context_chip(palette))

    tool_max = RUNTIME_STATUS.get("max_tool_iterations", max(1, int(cfg.max_tool_iterations)))
    reflect = RUNTIME_STATUS.get("tool_think_every", max(1, int(cfg.tool_think_every)))
    parts.append(sep)
    parts.append(_ftr_chip(f"tools {tool_max}", palette["muted"]))
    parts.append(" ")
    parts.append(_ftr_chip(f"reflect {reflect}", palette["muted"]))

    rate_chip = _token_rate_chip(palette)
    if rate_chip:
        parts.append(sep)
        parts.append(rate_chip)

    if not RUNTIME_STATUS.get("safe_mode", cfg.safe_mode):
        parts.append(sep)
        parts.append(_ftr_chip("safe off", palette["error"], bold=True))

    if RUNTIME_STATUS.get("auto_mode", cfg.auto_approve_active):
        parts.append(sep)
        parts.append(_ftr_chip("auto on", palette["warning"], bold=True))

    parts.append(" ")
    return HTML("".join(parts))


def build_prompt_style(palette: dict[str, str]) -> Style:
    return Style.from_dict(
        {
            # noreverse: prompt_toolkit defaults reverse video on toolbars (white bar bug).
            "bottom-toolbar": f"noreverse bg:{palette['surface_alt']} {palette['text']}",
            "bottom-toolbar.off": f"noreverse bg:{palette['surface_alt']} {palette['text']}",
            "bottom-toolbar.on": f"noreverse bg:{palette['surface_alt']} {palette['text']}",
            "rprompt": f"noreverse bg:{palette['surface']} {palette['muted']}",
            "bottom-toolbar.text": f"noreverse {palette['text']}",
            "rprompt.text": f"noreverse {palette['muted']}",
        }
    )


def invalidate_prompt_toolbar(session: Any | None) -> None:
    """Repaint the persistent footer after context/metrics change."""
    if session is None:
        return
    try:
        app = session.app
        if app is not None:
            app.invalidate()
    except Exception:
        pass


def build_status_rprompt(cfg: Config):
    palette = theme_colors(cfg.theme)
    sep = _ftr_sep(palette)
    cwd = RUNTIME_STATUS.get("cwd", compact_path(cfg.cwd, 32))
    theme_name = RUNTIME_STATUS.get("theme", cfg.theme)
    memory_count = RUNTIME_STATUS.get("memory_count", len(cfg.memories))
    from . import session_mode

    mode_label = session_mode.normalize_mode(cfg.session_mode)
    parts = [
        _ftr_chip(cwd, palette["muted"]),
        sep,
        _ftr_chip(mode_label, palette["info"] if mode_label == "publish" else palette["muted"]),
        sep,
        _ftr_chip(f"mem {memory_count}", palette["text"]),
        sep,
        _ftr_chip(theme_name, palette["primary"]),
    ]
    return HTML("".join(parts))


def default_embedding_model(cfg: Config, local_names: list[str] | None = None) -> str:
    if cfg.model and is_embedding_model_name(cfg.model) and cfg.model.lower() not in harness.DEPRECATED_EMBED_MODELS:
        return cfg.model
    preferred = harness.resolve_embed_model(cfg)
    base = preferred.split(":", 1)[0]
    local = local_names if local_names is not None else local_model_names(cfg)
    if any(name.startswith(base) for name in local):
        return preferred
    for candidate in (
        preferred,
        "qwen3-embedding",
        "embeddinggemma",
        "paraphrase-multilingual:latest",
        "nomic-embed-text",
    ):
        cand_base = candidate.split(":", 1)[0]
        if any(name.startswith(cand_base) for name in local):
            return candidate
    return preferred


def default_vision_model(cfg: Config) -> str:
    return cfg.model if is_vision_model_name(cfg.model) else "gemma3"


def resolve_multimodal_model(
    cfg: Config,
    *,
    explicit_model: str | None,
    available: list[str],
    predicate,
    fallback: str,
    install_hint: str,
    missing_hint: str,
) -> str | None:
    if explicit_model:
        if explicit_model in available:
            return explicit_model
        show_error(install_hint)
        return None
    match = next((name for name in available if predicate(name)), "")
    if match:
        return match
    if fallback and fallback in available:
        return fallback
    if available:
        show_error(missing_hint)
    else:
        show_error("No local models are available yet. Use /models or pull a compatible model first.")
    return None


def handle_embed_command(arg: str, cfg: Config, client: Client) -> None:
    parser = argparse.ArgumentParser(prog="/embed", add_help=False, exit_on_error=False)
    parser.add_argument("--model", default=None)
    parser.add_argument("--file", default=None)
    parser.add_argument("--truncate", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dimensions", type=int, default=None)
    parser.add_argument("text", nargs="*")
    try:
        ns = parser.parse_args(shlex.split(arg))
    except Exception:
        show_error("Usage: /embed [--model MODEL] [--file PATH] [--no-truncate] [--dimensions N] TEXT")
        return

    text = " ".join(ns.text).strip()
    if ns.file:
        path = Path(ns.file).expanduser()
        if not path.is_absolute():
            path = Path(cfg.cwd) / path
        if not path.exists():
            show_error(f"File not found: {path}")
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        show_error("Usage: /embed [--model MODEL] [--file PATH] [--no-truncate] [--dimensions N] TEXT")
        return

    if cfg.cloud:
        start_supplemental_gateway(cfg)
    available = [
        name for name in local_model_names(cfg)
        if name.lower() not in harness.DEPRECATED_EMBED_MODELS
    ]
    model = resolve_multimodal_model(
        cfg,
        explicit_model=ns.model,
        available=available,
        predicate=is_embedding_model_name,
        fallback=default_embedding_model(cfg),
        install_hint="That embedding model is not installed locally. Use /models and pick one like qwen3-embedding, embeddinggemma, or nomic-embed-text.",
        missing_hint="No supported embedding model is installed. Use /models and pick one like qwen3-embedding, embeddinggemma, or nomic-embed-text.",
    )
    if not model:
        return
    try:
        response: Any = tools_module.gateway_embed(text, model, ns.truncate, ns.dimensions)
        if response is None:
            response = Client(host=cfg.host).embed(model=model, input=text, truncate=ns.truncate, dimensions=ns.dimensions)
    except Exception as exc:
        show_error(f"Error generating embeddings: {exc}")
        return
    payload = tools_module.unpack_embed_response(
        response, model, text, truncate=ns.truncate, dimensions=ns.dimensions
    )
    console.print(json.dumps(payload, indent=2))


def handle_vision_command(arg: str, cfg: Config, client: Client) -> None:
    parser = argparse.ArgumentParser(prog="/vision", add_help=False, exit_on_error=False)
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("image", nargs="?")
    parser.add_argument("question", nargs="*")
    try:
        ns = parser.parse_args(shlex.split(arg))
    except Exception:
        show_error("Usage: /vision [--model MODEL] [--prompt TEXT] IMAGE [QUESTION]")
        return

    image_path = ns.image
    if not image_path:
        show_error("Usage: /vision [--model MODEL] [--prompt TEXT] IMAGE [QUESTION]")
        return
    prompt = ns.prompt or " ".join(ns.question).strip() or "What is in this image? Be concise."
    if cfg.cloud:
        start_supplemental_gateway(cfg)
    available = local_model_names(cfg)
    model = resolve_multimodal_model(
        cfg,
        explicit_model=ns.model,
        available=available,
        predicate=is_vision_model_name,
        fallback=default_vision_model(cfg),
        install_hint="That vision model is not installed locally. Use /models and pick one like gemma3, qwen3-vl, or llava.",
        missing_hint="No vision model is installed. Use /models and pick one like gemma3, qwen3-vl, or llava.",
    )
    if not model:
        return
    resolved = Path(image_path).expanduser()
    if not resolved.is_absolute():
        resolved = Path(cfg.cwd) / resolved
    if not resolved.exists():
        show_error(f"Image not found: {resolved}")
        return
    try:
        response = Client(host=cfg.host).chat(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                    "images": [str(resolved)],
                }
            ],
            stream=False,
            keep_alive=cfg.keep_alive,
        )
    except Exception as exc:
        show_error(f"Error running vision request: {exc}")
        return
    message = get_attr(response, "message", {}) or {}
    content = get_attr(message, "content", "")
    console.print(content or "(empty response)")


def handle_pdf_command(arg: str, cfg: Config) -> None:
    parser = argparse.ArgumentParser(prog="/pdf", add_help=False, exit_on_error=False)
    parser.add_argument("--pages", type=int, default=24)
    parser.add_argument("--chars", type=int, default=50_000)
    parser.add_argument("path", nargs="?")
    try:
        ns = parser.parse_args(shlex.split(arg))
    except Exception:
        show_error("Usage: /pdf [--pages N] [--chars N] PATH")
        return
    if not ns.path:
        show_error("Usage: /pdf [--pages N] [--chars N] PATH")
        return
    from .tools import read_pdf

    console.print(
        read_pdf(
            ns.path,
            cwd=cfg.cwd,
            max_pages=max(1, int(ns.pages)),
            max_chars=max(1000, int(ns.chars)),
        )
    )


def run_ollama_login() -> None:
    show_info("Starting `ollama signin`. Follow the browser/terminal prompts if they appear.")
    try:
        result = subprocess.run(["ollama", "signin"], check=False)
    except FileNotFoundError:
        show_error("Could not find `ollama` on PATH. Install Ollama before signing in.")
        return
    except KeyboardInterrupt:
        show_info("Ollama sign-in interrupted.")
        return
    if result.returncode == 0:
        show_info("Ollama sign-in finished.")
    else:
        show_error(f"`ollama signin` exited with code {result.returncode}.")


def run_xai_login(arg: str = "") -> None:
    load_runtime_env(override=True)
    if not xai_auth.client_id_configured():
        show_error(
            "xAI subscription OAuth is optional and not configured. "
            "Set XAI_CLIENT_ID in ~/.algo_cli/env (or ALGO_CLI_ENV_FILE) to a client id "
            "you are authorized to use, then retry /xai-login. Algo CLI does not bundle one."
        )
        return
    tokens_split = (arg or "").split()
    no_browser = "--no-browser" in tokens_split
    manual_only = "--manual" in tokens_split
    redirect_port = xai_auth.XAI_REDIRECT_PORT if manual_only else xai_auth.select_redirect_port()
    if redirect_port is None:
        show_error(
            "No xAI loopback redirect ports are available on 127.0.0.1. "
            "Close another login listener or retry with /xai-login --manual."
        )
        return
    try:
        prep = xai_auth.begin_login(no_browser=no_browser or manual_only, redirect_port=redirect_port)
    except Exception as exc:
        show_error(f"Could not start xAI login: {xai_auth.safe_error_message(exc)}")
        return
    if no_browser or manual_only:
        show_info("Open this URL on any browser you're signed into xAI with:")
        console.print(prep["auth_url"])
        if no_browser and not manual_only:
            show_info("If you're SSHed in, forward the callback port first:")
            console.print(f"  {prep['ssh_tunnel_cmd']}")
    elif prep.get("browser_opened"):
        # The full authorization URL contains the configured client id. Avoid
        # echoing it into routine terminal transcripts when the browser opened.
        show_info("Opened xAI authorization in your browser.")
    else:
        show_info("The browser did not open. Open this authorization URL manually:")
        console.print(prep["auth_url"])

    callback: dict[str, str] = {}
    if not manual_only:
        show_info(f"Listening on {prep['redirect_uri']} (waiting up to 5 minutes)…")
        show_info("If the browser shows 'Could not establish connection' with a code, paste it here when prompted.")
        try:
            callback = xai_auth.run_loopback_capture(redirect_port=redirect_port)
        except KeyboardInterrupt:
            show_info("Loopback listener cancelled — falling back to manual paste.")
        except Exception as exc:
            show_error(f"Loopback listener failed: {exc} — falling back to manual paste.")

    if not callback:
        if not manual_only:
            show_info("Loopback redirect did not arrive. If xAI showed you a code, paste it now.")
        try:
            pasted = input("xAI callback URL (or blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            show_info("xAI login cancelled.")
            return
        if not pasted:
            show_info("xAI login cancelled.")
            return
        parsed = urlparse(pasted)
        if parsed.query:
            qs = parse_qs(parsed.query)
            callback = {key: values[0] for key, values in qs.items() if values}
        else:
            show_error("Manual xAI login requires the full callback URL so the OAuth state can be verified.")
            return
        callback["redirect_uri"] = prep["redirect_uri"]

    try:
        if callback:
            callback.setdefault("redirect_uri", prep["redirect_uri"])
        tokens = xai_auth.complete_login(prep["code_verifier"], prep["state"], callback)
    except Exception as exc:
        show_error(xai_auth.safe_error_message(exc))
        return
    expires_in = max(0, int(tokens.get("expires_at", 0)) - int(time.time()))
    show_info(f"xAI authentication successful (token valid for {expires_in}s).")


def run_xai_logout() -> None:
    if xai_auth.clear_tokens():
        show_info("xAI tokens cleared.")
    else:
        show_info("No stored xAI tokens to clear.")


def run_xai_status() -> None:
    load_runtime_env(override=True)
    status = xai_auth.auth_status()
    if not status.get("client_configured"):
        if status.get("token_present"):
            show_info(
                "xAI subscription OAuth: a local token exists, but XAI_CLIENT_ID is not configured; "
                "refresh and new login are unavailable. Set your authorized client id in ~/.algo_cli/env."
            )
        else:
            show_info(
                "xAI subscription OAuth: optional, not configured. Algo CLI bundles no client id. "
                "Set XAI_CLIENT_ID in ~/.algo_cli/env only if you want to enable this provider."
            )
        return
    if not status.get("authenticated"):
        show_info("xAI subscription OAuth: client configured, not authenticated. Run /xai-login to continue.")
        return
    show_info(
        f"xAI: authenticated. Token expires in {status['expires_in']}s "
        f"(refresh token: {'yes' if status['has_refresh_token'] else 'no'}, "
        f"scope: {status.get('scope') or '?'})."
    )


def run_google_login(arg: str = "") -> None:
    tokens_split = (arg or "").split()
    no_browser = "--no-browser" in tokens_split
    manual_only = "--manual" in tokens_split
    redirect_port = google_workspace_auth.GOOGLE_REDIRECT_PORT if manual_only else google_workspace_auth.select_redirect_port()
    if redirect_port is None:
        show_error("No Google Workspace loopback port is free. Close anything bound to 56251-56270 or use --manual.")
        return
    try:
        prep = google_workspace_auth.begin_login(no_browser=no_browser or manual_only, redirect_port=redirect_port)
    except Exception as exc:
        show_error(f"Could not start Google login: {exc}")
        return
    if no_browser or manual_only:
        show_info("Open this URL in a browser where you are signed into Google with the target Workspace account:")
        console.print(prep["auth_url"])
        if no_browser and not manual_only:
            show_info("If you're SSHed in, forward the callback port first:")
            console.print(f"  {prep['ssh_tunnel_cmd']}")
    else:
        show_info("Opening Google auth in your browser…")
        show_info("If the browser does not open, copy this URL manually:")
        console.print(prep["auth_url"])

    callback: dict[str, str] = {}
    if not manual_only:
        try:
            callback = google_workspace_auth.wait_for_callback(
                redirect_port=int(prep["redirect_port"]),
                timeout=float(prep.get("timeout", 300.0)),
            )
        except Exception as exc:
            show_info(f"Google loopback did not arrive automatically: {exc}")
        if not callback:
            show_info(
                "Loopback redirect did not arrive. Copy the callback URL and run "
                "/google-callback --clipboard, or paste the full callback URL now."
            )
            try:
                pasted = input("Google callback URL (or blank to cancel): ").strip()
            except (EOFError, KeyboardInterrupt):
                show_info("Google login cancelled.")
                return
            if not pasted:
                show_info("Google login cancelled.")
                return
            callback = google_workspace_auth.parse_callback_value(pasted)
            if not callback:
                show_error("Manual Google login requires the full callback URL so the OAuth state can be verified.")
                return
    else:
        try:
            pasted = input("Google callback URL (or blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            show_info("Google login cancelled.")
            return
        if not pasted:
            show_info("Google login cancelled.")
            return
        callback = google_workspace_auth.parse_callback_value(pasted)
        if not callback:
            show_error("Manual Google login requires the full callback URL so the OAuth state can be verified.")
            return

    try:
        callback.setdefault("redirect_uri", prep["redirect_uri"])
        tokens = google_workspace_auth.complete_login(prep["code_verifier"], prep["state"], callback)
    except Exception as exc:
        show_error(str(exc))
        return
    expires_in = max(0, int(tokens.get("expires_at", 0)) - int(time.time()))
    show_info(f"Google Workspace authentication successful (token valid for {expires_in}s).")


def read_clipboard_text() -> str:
    try:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not read clipboard: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError("Could not read clipboard with pbpaste.")
    return result.stdout.strip()


def run_google_callback(arg: str = "") -> None:
    pending = google_workspace_auth.load_pending_login()
    if not pending:
        show_error("No pending Google login. Run /google-login first, then approve access.")
        return

    tokens_split = shlex.split(arg or "")
    callback_text = ""
    if "--clipboard" in tokens_split:
        try:
            callback_text = read_clipboard_text()
        except Exception as exc:
            show_error(str(exc))
            return
    elif "--file" in tokens_split:
        idx = tokens_split.index("--file")
        if idx + 1 >= len(tokens_split):
            show_error("Usage: /google-callback --file PATH")
            return
        try:
            callback_text = Path(tokens_split[idx + 1]).expanduser().read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            show_error(f"Could not read callback file: {exc}")
            return
    else:
        callback_text = (arg or "").strip()
        if not callback_text:
            show_info("Paste the Google callback URL, or use /google-callback --clipboard.")
            try:
                callback_text = input("Google callback URL (or blank to cancel): ").strip()
            except (EOFError, KeyboardInterrupt):
                show_info("Google login cancelled.")
                return
            if not callback_text:
                show_info("Google login cancelled.")
                return

    callback = google_workspace_auth.parse_callback_value(callback_text)
    if not callback:
        show_error("Could not parse Google callback URL. Copy the full callback URL and retry /google-callback --clipboard.")
        return
    callback.setdefault("redirect_uri", pending["redirect_uri"])
    try:
        tokens = google_workspace_auth.complete_login(pending["code_verifier"], pending["state"], callback)
    except Exception as exc:
        show_error(str(exc))
        return
    expires_in = max(0, int(tokens.get("expires_at", 0)) - int(time.time()))
    show_info(f"Google Workspace authentication successful (token valid for {expires_in}s).")


def run_google_logout() -> None:
    if google_workspace_auth.clear_tokens():
        show_info("Google Workspace tokens cleared.")
    else:
        show_info("No stored Google Workspace tokens to clear.")


def run_google_status() -> None:
    status = google_workspace_auth.auth_status()
    if not status.get("client_configured"):
        show_error("GOOGLE_OAUTH_CLIENT_ID is not set. Export it (and GOOGLE_OAUTH_CLIENT_SECRET) before /google-login.")
        return
    if not status.get("authenticated"):
        show_info("Google Workspace: not authenticated. Run /google-login to start the OAuth flow.")
        return
    show_info(
        f"Google Workspace: authenticated. Token expires in {status['expires_in']}s "
        f"(refresh token: {'yes' if status['has_refresh_token'] else 'no'}, "
        f"scope: {status.get('scope') or '?'})."
    )


_GOOGLE_TEXT_LIMIT = 6000


def _google_usage_text() -> str:
    return (
        "Google Workspace subcommands:\n"
        "  /google drive-list [query] [--max N]\n"
        "  /google drive-search NAME [--max N] [--mime MIME]\n"
        "  /google drive-get FILE_ID [--download | --export MIME]\n"
        "  /google docs-get DOCUMENT_ID\n"
        "  /google sheets-values SPREADSHEET_ID RANGE\n"
        "  /google calendar-list [--max N] [--time-min RFC3339] [--time-max RFC3339]\n"
        "  /google gmail-list [query] [--max N] [--label LABEL]\n"
        "  /google gmail-get MESSAGE_ID\n"
        "  /google gmail-draft --to EMAIL --subject SUBJECT [--html-file PATH | --text-file PATH | BODY...] [--cc EMAIL] [--bcc EMAIL]\n"
        "  /google help"
    )


def _google_pop_option(tokens: list[str], option: str) -> str | None:
    value: str | None = None
    kept: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == option:
            if i + 1 >= len(tokens):
                raise ValueError(f"{option} requires a value")
            value = tokens[i + 1]
            i += 2
        else:
            kept.append(tokens[i])
            i += 1
    tokens[:] = kept
    return value


def _google_pop_int_option(tokens: list[str], option: str, default: int) -> int:
    raw = _google_pop_option(tokens, option)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{option} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{option} must be at least 1")
    return value


def _google_pop_flag(tokens: list[str], flag: str) -> bool:
    found = False
    kept: list[str] = []
    for token in tokens:
        if token == flag:
            found = True
        else:
            kept.append(token)
    tokens[:] = kept
    return found


def _google_print_lines(lines: list[str]) -> None:
    for line in lines:
        console.print(line)


def _google_print_text(text: str, *, limit: int = _GOOGLE_TEXT_LIMIT) -> None:
    console.print(text[:limit] + ("\n...[truncated]" if len(text) > limit else ""))


def run_google(arg: str = "") -> None:
    """Dispatch Google Workspace subcommands (reads plus Gmail draft creation)."""
    try:
        tokens_split = shlex.split(arg or "")
    except ValueError as exc:
        show_error(f"Invalid /google arguments: {exc}")
        return
    if not tokens_split or tokens_split[0] in {"help", "--help", "-h"}:
        show_info(_google_usage_text())
        return
    if not google_workspace_auth.get_valid_token():
        show_error("Not authenticated with Google Workspace. Run /google-login first.")
        return
    sub = tokens_split[0]
    rest = tokens_split[1:]
    client = google_workspace.GoogleWorkspaceClient()
    try:
        if sub == "drive-list":
            args = list(rest)
            max_n = _google_pop_int_option(args, "--max", 20)
            query = " ".join(args).strip() or None
            payload = client.drive_list(query=query, page_size=max_n)
            _google_print_lines(google_workspace.format_drive_files(payload))
        elif sub == "drive-search":
            args = list(rest)
            max_n = _google_pop_int_option(args, "--max", 20)
            mime_type = _google_pop_option(args, "--mime")
            name = " ".join(args).strip()
            if not name:
                show_error("Usage: /google drive-search NAME [--max N] [--mime MIME]")
                return
            payload = client.drive_search(name, mime_type=mime_type, page_size=max_n)
            _google_print_lines(google_workspace.format_drive_files(payload))
        elif sub == "drive-get":
            if not rest:
                show_error("Usage: /google drive-get FILE_ID [--download | --export MIME]")
                return
            args = list(rest[1:])
            file_id = rest[0]
            export_mime = _google_pop_option(args, "--export")
            download = _google_pop_flag(args, "--download")
            if args:
                show_error("Usage: /google drive-get FILE_ID [--download | --export MIME]")
                return
            if export_mime:
                data, _headers = client.drive_export(file_id, mime_type=export_mime)
                _google_print_text(data.decode("utf-8", errors="replace"))
            elif download:
                data, _headers = client.drive_download(file_id)
                _google_print_text(data.decode("utf-8", errors="replace"))
            else:
                console.print(json.dumps(client.drive_get(file_id), indent=2, sort_keys=True))
        elif sub == "docs-get":
            if len(rest) != 1:
                show_error("Usage: /google docs-get DOCUMENT_ID")
                return
            document = client.docs_get(rest[0])
            title = document.get("title")
            if title:
                console.print(f"# {title}")
            _google_print_text(google_workspace.format_docs_plain_text(document, client))
        elif sub == "sheets-values":
            if len(rest) != 2:
                show_error("Usage: /google sheets-values SPREADSHEET_ID RANGE")
                return
            payload = client.sheets_values_get(rest[0], rest[1])
            _google_print_text(google_workspace.format_sheet_values(payload))
        elif sub == "calendar-list":
            args = list(rest)
            max_n = _google_pop_int_option(args, "--max", 20)
            time_min = _google_pop_option(args, "--time-min")
            time_max = _google_pop_option(args, "--time-max")
            if args:
                show_error("Usage: /google calendar-list [--max N] [--time-min RFC3339] [--time-max RFC3339]")
                return
            payload = client.calendar_events_list(time_min=time_min, time_max=time_max, max_results=max_n)
            _google_print_lines(google_workspace.format_calendar_events(payload))
        elif sub == "gmail-list":
            args = list(rest)
            max_n = _google_pop_int_option(args, "--max", 20)
            label = _google_pop_option(args, "--label")
            query_parts = list(args)
            if label:
                query_parts.insert(0, f"label:{label}")
            payload = client.gmail_list(query=" ".join(query_parts).strip() or None, max_results=max_n)
            messages = payload.get("messages", []) or []
            if not messages:
                console.print("  (no messages)")
            for msg in messages:
                console.print(f"  - id={msg.get('id', '?')}  thread={msg.get('threadId', '?')}")
        elif sub == "gmail-get":
            if not rest:
                show_error("Usage: /google gmail-get MESSAGE_ID")
                return
            message = client.gmail_get(rest[0], fmt="metadata")
            _google_print_text(google_workspace.format_gmail_message(message))
        elif sub == "gmail-draft":
            args = list(rest)
            to = _google_pop_option(args, "--to")
            subject = _google_pop_option(args, "--subject")
            cc = _google_pop_option(args, "--cc")
            bcc = _google_pop_option(args, "--bcc")
            html_file = _google_pop_option(args, "--html-file")
            text_file = _google_pop_option(args, "--text-file")
            if not to or not subject:
                show_error("Usage: /google gmail-draft --to EMAIL --subject SUBJECT [--html-file PATH | --text-file PATH | BODY...] [--cc EMAIL] [--bcc EMAIL]")
                return
            if html_file and text_file:
                show_error("Use either --html-file or --text-file, not both.")
                return
            html_body = None
            text_body = None
            if html_file:
                html_body = Path(html_file).expanduser().read_text(encoding="utf-8", errors="replace")
            elif text_file:
                text_body = Path(text_file).expanduser().read_text(encoding="utf-8", errors="replace")
            else:
                text_body = " ".join(args).strip()
            draft = client.gmail_create_draft(to=to, subject=subject, html_body=html_body, text_body=text_body, cc=cc, bcc=bcc)
            message = draft.get("message") or {}
            show_info(f"Gmail draft created: draft_id={draft.get('id', '?')} message_id={message.get('id', '?')}")
        else:
            show_error(f"Unknown /google subcommand: {sub}. Try /google help.")
    except ValueError as exc:
        show_error(str(exc))
    except Exception as exc:
        show_error(f"Google Workspace call failed: {exc}")


def run_chatgpt_login(arg: str = "") -> None:
    tokens_split = (arg or "").split()
    no_browser = "--no-browser" in tokens_split
    manual_only = "--manual" in tokens_split
    device_code = "--device-code" in tokens_split or "--codex-device" in tokens_split
    if device_code:
        show_info("Starting ChatGPT Plus/Pro - Codex device-code login.")
        show_info(f"When Codex prints a one-time code, open {chatgpt_auth.CODEX_DEVICE_VERIFY_URL} and approve it.")
        try:
            tokens = chatgpt_auth.run_codex_device_login()
        except Exception as exc:
            show_error(str(exc))
            return
        expires_in = max(0, int(tokens.get("expires_at", 0)) - int(time.time()))
        show_info(f"ChatGPT authentication successful (token valid for {expires_in}s).")
        _show_chatgpt_models_after_login()
        return
    redirect_port = chatgpt_auth.CHATGPT_REDIRECT_PORT if manual_only else chatgpt_auth.select_redirect_port()
    if redirect_port is None:
        show_error("ChatGPT loopback redirect port 1455 is not available. Retry with /chatgpt-login --manual.")
        return
    try:
        prep = chatgpt_auth.begin_login(no_browser=no_browser or manual_only, redirect_port=redirect_port)
    except Exception as exc:
        show_error(f"Could not start ChatGPT login: {exc}")
        return
    if no_browser or manual_only:
        show_info("Open this URL on any browser you're signed into ChatGPT/OpenAI with:")
        console.print(prep["auth_url"])
        if no_browser and not manual_only:
            show_info("If you're SSHed in, forward the callback port first:")
            console.print(f"  {prep['ssh_tunnel_cmd']}")
    else:
        show_info("Opening ChatGPT/OpenAI auth in your browser…")
        show_info("If the browser does not open, copy this URL manually:")
        console.print(prep["auth_url"])

    callback: dict[str, str] = {}
    if not manual_only:
        show_info(f"Listening on {prep['redirect_uri']} (waiting up to 5 minutes)…")
        try:
            callback = chatgpt_auth.run_loopback_capture(redirect_port=redirect_port)
        except KeyboardInterrupt:
            show_info("Loopback listener cancelled — falling back to manual paste.")
        except Exception as exc:
            show_error(f"Loopback listener failed: {exc} — falling back to manual paste.")

    if not callback:
        if not manual_only:
            show_info("Loopback redirect did not arrive. If ChatGPT showed you a code, paste it now.")
        try:
            pasted = input("ChatGPT callback URL (or blank to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            show_info("ChatGPT login cancelled.")
            return
        if not pasted:
            show_info("ChatGPT login cancelled.")
            return
        parsed = urlparse(pasted)
        if parsed.query:
            qs = parse_qs(parsed.query)
            callback = {key: values[0] for key, values in qs.items() if values}
        else:
            show_error("Manual ChatGPT login requires the full callback URL so the OAuth state can be verified.")
            return
        callback["redirect_uri"] = prep["redirect_uri"]

    try:
        callback.setdefault("redirect_uri", prep["redirect_uri"])
        tokens = chatgpt_auth.complete_login(prep["code_verifier"], prep["state"], callback)
    except Exception as exc:
        show_error(str(exc))
        return
    expires_in = max(0, int(tokens.get("expires_at", 0)) - int(time.time()))
    show_info(f"ChatGPT authentication successful (token valid for {expires_in}s).")
    _show_chatgpt_models_after_login()


def _show_chatgpt_models_after_login() -> None:
    names, _authenticated = chatgpt_model_names()
    if names:
        show_info(f"Codex models enabled: {', '.join(names)}")
        if any(name.startswith("gpt-5.6-") for name in names):
            show_info("GPT-5.6 reasoning is configurable per model with /thinking effort [MODEL] LEVEL.")


def run_chatgpt_logout() -> None:
    if chatgpt_auth.clear_tokens():
        show_info("ChatGPT tokens cleared.")
    else:
        show_info("No stored ChatGPT tokens to clear.")


def run_chatgpt_status() -> None:
    status = chatgpt_auth.auth_status()
    if not status.get("authenticated"):
        show_info("ChatGPT: not authenticated. Run /chatgpt-login to start Codex browser OAuth.")
        show_info(f"Device-code fallback: /chatgpt-login --device-code ({chatgpt_auth.CODEX_DEVICE_VERIFY_URL})")
        return
    show_info(
        f"ChatGPT: authenticated. Token expires in {status['expires_in']}s "
        f"(refresh token: {'yes' if status['has_refresh_token'] else 'no'}, "
        f"scope: {status.get('scope') or '?'})."
    )
    names, _authenticated = chatgpt_model_names()
    if names:
        show_info(f"Codex models: {', '.join(names)}")


def run_model_check(arg: str = "", *, active_model: str = "") -> None:
    """Static compatibility report for Grok/xAI models (no chat API call)."""
    from . import model_info as _mi
    from .xai_client import is_multi_agent_model

    name = (arg or active_model or "").strip()
    if not name:
        show_error("Usage: /model-check MODEL_NAME  (e.g. grok-4.20-multi-agent-0309)")
        return
    bare = name.split(":", 1)[0].strip()
    lines: list[str] = [f"Model: {name}"]
    if not _mi.is_xai_model(name):
        lines.append("Family: not Grok/xAI — routed via Ollama host/cloud per /model and cfg.host.")
        for line in lines:
            console.print(line)
        return
    lines.append("Family: Grok/xAI (optional subscription OAuth)")
    auth = xai_auth.auth_status()
    lines.append(f"OAuth client configured: {'yes' if auth.get('client_configured') else 'no — set XAI_CLIENT_ID'}")
    if auth.get("client_configured"):
        auth_label = "yes" if auth.get("authenticated") else "no — run /xai-login"
    else:
        auth_label = "no — optional provider is not configured"
    lines.append(f"OAuth authenticated: {auth_label}")
    in_fallback = bare in XAI_MODEL_CHOICES
    lines.append(f"In XAI_MODEL_CHOICES fallback list: {'yes' if in_fallback else 'no (may still work if /v1/models lists it)'}")
    if is_multi_agent_model(name):
        lines.append("API route: POST https://api.x.ai/v1/responses (multi-agent)")
        lines.append("Harness: prior tool calls/results folded into text; no client-side tools on this path.")
        lines.append("Timeout: up to 3600s per request in xai_client.chat().")
    else:
        lines.append("API route: POST https://api.x.ai/v1/chat/completions (OpenAI-compatible)")
    lines.append("Billing: optional subscription OAuth only — XAI_API_KEY fallback is disabled.")
    lines.append("Sources: algo_cli/main.py (XAI_MODEL_CHOICES), xai_client.py, tests/test_xai_client.py")
    for line in lines:
        console.print(line)


def run_xai_test() -> None:
    from . import xai_client

    status = xai_auth.auth_status()
    if not status.get("client_configured"):
        show_error("Optional xAI OAuth is not configured. Set your authorized XAI_CLIENT_ID before /xai-login.")
        return
    if not xai_auth.get_valid_token():
        show_error("Not authenticated with xAI. Run /xai-login first.")
        return
    try:
        result = xai_client.get_models()
    except Exception as exc:
        show_error(f"xAI /v1/models failed: {xai_auth.safe_error_message(exc)}")
        show_info(
            "If you see 403/insufficient_scope, the OAuth scope likely does not grant API access "
            "on this account. Some xAI account tiers do not include /v1 access via OAuth."
        )
        return
    items = result.get("data") or result.get("models") or []
    if not items:
        show_info(f"xAI returned no models. Raw payload: {result}")
        return
    show_info(f"xAI returned {len(items)} accessible models:")
    for item in items:
        if isinstance(item, dict):
            name = item.get("id") or item.get("name") or "(unnamed)"
            owned = item.get("owned_by", "")
            console.print(f"  - {name}" + (f"  [muted]({owned})[/]" if owned else ""))
        else:
            console.print(f"  - {item}")


def run_x_account(arg: str = "") -> None:
    try:
        parts = shlex.split(arg or "")
    except ValueError as exc:
        show_error(f"Could not parse /x-account args: {exc}")
        return
    if not parts or parts[0] in {"help", "-h", "--help"}:
        show_info("X account commands use xurl and separate X API OAuth, not xAI Grok OAuth.")
        console.print("  /x-account status")
        console.print('  /x-account draft-post "text"')
        console.print('  /x-account draft-reply POST_ID_OR_URL "text"')
        console.print('  /x-account post --confirm "text"')
        console.print('  /x-account reply --confirm POST_ID_OR_URL "text"')
        console.print("  /x-account like|unlike|repost|unrepost|bookmark|unbookmark|delete --confirm POST_ID_OR_URL")
        return

    sub = parts[0].lower()
    if sub == "status":
        result = x_account.status()
    elif sub == "draft-post":
        result = x_account.draft_post(" ".join(parts[1:]))
    elif sub == "draft-reply":
        if len(parts) < 3:
            show_error('Usage: /x-account draft-reply POST_ID_OR_URL "text"')
            return
        result = x_account.draft_reply(parts[1], " ".join(parts[2:]))
    elif sub == "post":
        confirm = "--confirm" in parts[1:]
        text_parts = [item for item in parts[1:] if item != "--confirm"]
        result = x_account.post(" ".join(text_parts), confirm=confirm)
    elif sub == "reply":
        confirm = "--confirm" in parts[1:]
        text_parts = [item for item in parts[1:] if item != "--confirm"]
        if len(text_parts) < 2:
            show_error('Usage: /x-account reply --confirm POST_ID_OR_URL "text"')
            return
        result = x_account.reply(text_parts[0], " ".join(text_parts[1:]), confirm=confirm)
    elif sub in x_account.CONFIRMED_POST_ACTIONS:
        confirm = "--confirm" in parts[1:]
        text_parts = [item for item in parts[1:] if item != "--confirm"]
        if len(text_parts) != 1:
            show_error(f"Usage: /x-account {sub} --confirm POST_ID_OR_URL")
            return
        result = x_account.post_action(sub, text_parts[0], confirm=confirm)
    else:
        show_error(f"Unknown /x-account subcommand: {sub}")
        return

    if result.ok:
        show_info(result.message)
    else:
        show_error(result.message)
    if result.data:
        console.print(json.dumps(result.data, indent=2))


def auth_hint_for_cloud() -> None:
    load_runtime_env(override=True)
    if os.environ.get("OLLAMA_API_KEY"):
        show_info("OLLAMA_API_KEY detected for Ollama Cloud direct API/web tools.")
    else:
        show_info(
            "For cloud models through local Ollama, run /login (`ollama signin`) and select a :cloud model. "
            "Set OLLAMA_API_KEY only for direct Cloud API/web tools."
        )


def maybe_prompt_cloud_login() -> None:
    load_runtime_env(override=True)
    if os.environ.get("OLLAMA_API_KEY"):
        auth_hint_for_cloud()
        return
    auth_hint_for_cloud()
    answer = input("Run `ollama signin` now? [Y/n] ").strip().lower()
    if answer in {"", "y", "yes"}:
        run_ollama_login()


def local_model_names(cfg: Config) -> list[str]:
    if not start_local_ollama_host(cfg.host):
        return []
    cached = LOCAL_MODEL_CACHE.get(cfg.host)
    now = time.time()
    if cached and now - cached[0] <= LOCAL_MODEL_LIST_TTL_SECONDS:
        return cached[1]
    try:
        models = Client(host=cfg.host).list()
    except Exception as exc:
        show_error(f"Could not list local models: {exc}")
        return []
    items = get_attr(models, "models", []) or []
    names: list[str] = []
    for model in items:
        name = get_attr(model, "name", None) or get_attr(model, "model", None)
        if name:
            names.append(str(name))
    result = sorted(set(names))
    LOCAL_MODEL_CACHE[cfg.host] = (now, result)
    return result


def cloud_model_names() -> list[str]:
    load_runtime_env(override=True)
    api_key = os.environ.get("OLLAMA_API_KEY", "")
    if not api_key:
        return []
    try:
        models = Client(host="https://ollama.com", headers={"Authorization": f"Bearer {api_key}"}).list()
    except Exception:
        return CLOUD_MODEL_CHOICES
    items = get_attr(models, "models", []) or []
    names: list[str] = []
    for model in items:
        name = get_attr(model, "name", None) or get_attr(model, "model", None)
        if name:
            names.append(str(name))
    return sorted(set(names)) or CLOUD_MODEL_CHOICES


def chatgpt_model_names() -> tuple[list[str], bool]:
    """Return (model_names, authenticated) for ChatGPT/Codex subscription OAuth."""
    if not chatgpt_auth.get_valid_token():
        return [], False
    try:
        models = chatgpt_client.get_codex_models()
    except Exception:
        return list(CHATGPT_MODEL_CHOICES), True
    names = [str(item["slug"]) for item in models if item.get("slug")]
    return (names or list(CHATGPT_MODEL_CHOICES)), True


def xai_model_names() -> tuple[list[str], bool]:
    """Return (model_names, authenticated) for subscription OAuth only."""
    try:
        from . import xai_auth
    except Exception:
        return [], False
    if not xai_auth.get_valid_token():
        return [], False
    try:
        from . import xai_client
        response = xai_client.get_models()
    except Exception:
        return list(XAI_MODEL_CHOICES), True
    items = response.get("data") if isinstance(response, dict) else None
    names: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = item.get("id") or item.get("name")
        if not name:
            continue
        # Filter to chat/text models; skip embedding / image / video / audio entries.
        bare = str(name).lower()
        if any(skip in bare for skip in ("embed", "image", "video", "tts", "asr", "imagine")):
            continue
        names.append(str(name))
    return (sorted(set(names)) or list(XAI_MODEL_CHOICES)), True


def collect_dashboard_state(client: Client, cfg: Config) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    installed_models: list[dict[str, str]] = []
    running_models: list[dict[str, str]] = []
    event_lines: list[str] = []

    provider_mode = runtime_mode_label(cfg)
    if provider_mode == "local":
        try:
            list_response = client.list()
            items = get_attr(list_response, "models", []) or []
            for item in items:
                if len(installed_models) >= 4:
                    break
                details = get_attr(item, "details", {}) or {}
                installed_models.append(
                    {
                        "name": str(get_attr(item, "model", None) or get_attr(item, "name", "?")),
                        "size": _format_bytes(get_attr(item, "size", None)),
                        "quant": str(get_attr(details, "quantization_level", None) or "?"),
                    }
                )
        except Exception as exc:
            event_lines.append(f"installed models unavailable: {exc}")

        try:
            process_response = client.ps()
            items = get_attr(process_response, "models", []) or []
            for item in items:
                if len(running_models) >= 3:
                    break
                details = get_attr(item, "details", {}) or {}
                running_models.append(
                    {
                        "name": str(get_attr(item, "name", None) or get_attr(item, "model", None) or "?"),
                        "size_vram": _format_bytes(get_attr(item, "size_vram", None) or get_attr(item, "size", None)),
                        "context": str(get_attr(item, "context_length", None) or get_attr(details, "parameter_size", None) or "?"),
                    }
                )
        except Exception as exc:
            event_lines.append(f"running models unavailable: {exc}")

    event_lines.extend(
        [
            f"connected {effective_runtime_host(cfg)}",
            f"mode {provider_mode}",
            f"context {cfg.num_ctx}",
            f"theme {cfg.theme}",
            f"memories {len(cfg.memories)}",
        ]
    )
    return installed_models, running_models, event_lines


def choose_from_menu(title: str, choices: list[tuple[str, str]], default: int = 1) -> int | None:
    console.print(f"\n[bold]{title}[/]")
    for index, (label, detail) in enumerate(choices, 1):
        suffix = f" [dim]{detail}[/]" if detail else ""
        console.print(f"  [cyan]{index}[/]. {label}{suffix}")
    while True:
        raw = input(f"Select [{default}]: ").strip()
        if not raw:
            return default
        if raw.lower() in {"q", "quit", "exit"}:
            return None
        try:
            choice = int(raw)
        except ValueError:
            console.print("[red]Enter a number, or q to cancel.[/]")
            continue
        if 1 <= choice <= len(choices):
            return choice
        console.print("[red]Choice out of range.[/]")


def model_picker(cfg: Config, *, first_run: bool = False) -> bool:
    local_names = local_model_names(cfg)
    choices: list[tuple[str, str]] = []
    for name in local_names:
        detail = "cloud via local Ollama" if is_cloud_model_name(name) else "local"
        choices.append((name, detail))
    for name in cloud_model_names():
        if name not in local_names:
            choices.append((name, "direct cloud API"))
    chatgpt_names, chatgpt_authed = chatgpt_model_names()
    for name in chatgpt_names:
        effort = chatgpt_client.reasoning_effort_for_model(name, cfg.chatgpt_reasoning_efforts)
        family = {
            "gpt-5.6-sol": "Sol · detail and polish",
            "gpt-5.6-terra": "Terra · everyday workhorse",
            "gpt-5.6-luna": "Luna · fast repeatable work",
        }.get(name, "Codex")
        choices.append((name, f"OpenAI {family} · reasoning {effort} · subscription quota"))
    xai_names, xai_authed = xai_model_names()
    xai_suffix = "xAI Grok OAuth (subscription quota)"
    for name in xai_names:
        choices.append((name, xai_suffix))

    if not choices:
        show_error(
            "No models are selectable yet. Pull a local model with `ollama pull qwen3`, "
            "or run /login and pull/select a :cloud model through local Ollama."
        )
        return False

    prompt = "First-run model picker" if first_run else "Model picker"
    selected = choose_from_menu(prompt, choices)
    if selected is None:
        return False

    model, mode = choices[selected - 1]
    cfg.model = model
    # Cloud flag is only meaningful for Ollama Cloud; xAI uses its own client regardless.
    cfg.cloud = mode == "direct cloud API"
    cfg.save()
    show_info(f"Model set to {cfg.model} ({mode}).")
    if mode.startswith("OpenAI"):
        effort = chatgpt_client.reasoning_effort_for_model(cfg.model, cfg.chatgpt_reasoning_efforts)
        show_info(f"Reasoning effort for {cfg.model}: {effort} (/thinking effort LEVEL).")
    if mode.startswith("OpenAI") and not chatgpt_authed:
        show_info("ChatGPT/Codex OAuth is not authenticated. Run /chatgpt-login.")
    elif mode.startswith("xAI") and not xai_authed:
        if xai_auth.client_id_configured():
            show_info("xAI OAuth is not authenticated. Run /xai-login; API-key fallback is disabled.")
        else:
            show_info(
                "Optional xAI OAuth is not configured. Set your authorized XAI_CLIENT_ID before /xai-login; "
                "API-key fallback is disabled."
            )
    elif cfg.cloud and cfg.auto_cloud_connect:
        maybe_prompt_cloud_login()
    elif cfg.cloud:
        show_info("Direct Cloud API model selected. OLLAMA_API_KEY is used for this route.")
    return True


def reload_runtime() -> Config:
    global tools_module, ALL_TOOLS, TOOL_MAP

    cfg = Config.load()
    for module_name in (
        "algo_cli.tools",
        "algo_cli.harness",
        "algo_cli.session_mode",
        "algo_cli.session_commands",
        "algo_cli.workspace_resolver",
        "algo_cli.task_router",
        "algo_cli.reflex",
        "algo_cli.tool_policy",
    ):
        loaded = sys.modules.get(module_name)
        if loaded is not None:
            importlib.reload(loaded)
    tools_module = sys.modules["algo_cli.tools"]
    ALL_TOOLS = tools_module.ALL_TOOLS
    TOOL_MAP = tools_module.TOOL_MAP
    harness.configure_context_sources(
        external=cfg.external_harness_sources_enabled,
        index_compute_lab=cfg.index_compute_lab_auto_inject,
    )
    try:
        set_theme(cfg.theme)
    except ValueError:
        cfg.theme = current_theme_name()
    return cfg


def handle_status_command(cfg: Config, client: Any | None = None) -> None:
    used, total, remaining, runtime_cap, native_ctx = context_status(cfg, client=client)
    features: list[str] = []
    for enabled, label in (
        (cfg.cloud, "cloud"),
        (cfg.auto_approve_active, "auto-approve"),
        (cfg.safe_mode, "safe-mode"),
        (cfg.show_thinking, "thinking"),
        (cfg.verify_mode, "verify"),
        (cfg.algorithmic_tool_policy_enabled, "policy"),
        (cfg.reflex_enabled, "reflex"),
        (cfg.intuition_recall_enabled, "intuition"),
        (code_rag_consent_granted(cfg), "code-rag"),
        (cfg.skill_crystallize_enabled, "skills"),
        (bool(cfg.session_summary.strip()), "summary"),
    ):
        if enabled:
            features.append(label)
    console.print(f"[bold primary]Model:[/] {cfg.model}")
    ctx_line = f"{used}/{total} tokens ({remaining} remaining)"
    if native_ctx and runtime_cap and native_ctx > runtime_cap:
        ctx_line += f" · runtime cap {runtime_cap:,}"
    console.print(f"[bold primary]Context:[/] {ctx_line}")
    console.print(f"[bold primary]Features:[/] {', '.join(features) if features else 'none'}")


def small_maintenance_client(cfg: Config, fallback_client: Client | None = None) -> tuple[Client, str]:
    local_names = [name for name in local_model_names(cfg) if not is_embedding_model_name(name)]
    preferred = ("qwen3:4b", "qwen3", "gemma3:4b", "gemma3")
    timeout = max(1.0, float(cfg.chat_stream_timeout_seconds))
    for model in preferred:
        if model in local_names:
            return Client(host=cfg.host, timeout=timeout), model
    if local_names:
        return Client(host=cfg.host, timeout=timeout), local_names[0]
    if cfg.cloud:
        return create_client(Config(model=MAINTENANCE_CLOUD_MODEL, cloud=True, host=cfg.host)), MAINTENANCE_CLOUD_MODEL
    return fallback_client or create_client(cfg), cfg.model


def local_maintenance_client(cfg: Config) -> tuple[Client, str] | None:
    """Return a genuinely local, non-embedding maintenance model or None."""

    if not host_is_local(cfg.host) or not ollama_server_ready(cfg.host):
        return None
    local_names = [name for name in local_model_names(cfg) if not is_embedding_model_name(name)]
    if not local_names:
        return None
    preferred = ("qwen3:4b", "qwen3", "gemma3:4b", "gemma3")
    model = next((name for name in preferred if name in local_names), local_names[0])
    timeout = max(1.0, float(cfg.chat_stream_timeout_seconds))
    return Client(host=cfg.host, timeout=timeout), model


def handle_diff_command() -> None:
    """Show the most recent verified Git diff captured by a requires_change block."""
    blocks = session_pipeline_blocks()
    if not blocks:
        show_info("No pipeline activity in this session. Run /agent first.")
        return
    for block in reversed(blocks):
        if block.requires_change and (block.git_evidence or "").strip():
            console.print(
                f"[bold]Diff captured by [{block.role}] block[/] — status: [text]{block.status}[/]"
            )
            if block.status_reason:
                console.print(f"[muted]reason:[/] {block.status_reason}")
            if block.verification_warning:
                console.print(f"[warning]verification:[/] {block.verification_warning}")
            if block.successful_writes:
                console.print(
                    f"[muted]successful_writes:[/] {', '.join(block.successful_writes)}"
                )
            console.print()
            console.print(block.git_evidence.strip())
            return
    show_info(
        "No verified diff captured in this session. requires_change blocks have run "
        "but none recorded Git evidence (e.g., repository unavailable or no changes detected)."
    )


def handle_changes_command() -> None:
    """Summarize per-block activity from the most recent pipeline run."""
    blocks = session_pipeline_blocks()
    if not blocks:
        show_info("No pipeline activity in this session. Run /agent first.")
        return
    console.print(
        f"[bold]Pipeline activity[/] — {len(blocks)} block{'s' if len(blocks) != 1 else ''}"
    )
    for block in blocks:
        duration_s = (block.duration_ms or 0) / 1000
        status_style = "success" if block.status == "complete" else (
            "warning" if block.status == "partial" else "error"
        )
        console.print(
            f"  [bold][{block.role}][/]  [{status_style}]{block.status}[/]"
            f"  {duration_s:.1f}s  {block.tool_calls} tool call"
            f"{'' if block.tool_calls == 1 else 's'}"
        )
        if block.status_reason:
            console.print(f"      [muted]reason:[/] {block.status_reason}")
        if block.verification_warning:
            console.print(f"      [warning]verification:[/] {block.verification_warning}")
        if block.successful_writes:
            console.print(
                f"      [muted]writes:[/] {', '.join(block.successful_writes)}"
            )
        if block.mutation_actions:
            console.print(
                f"      [muted]mutation_actions:[/] {', '.join(block.mutation_actions)}"
            )


def handle_context_command(arg: str, cfg: Config, client: Client) -> None:
    subcommand = (arg or "status").strip().lower()
    if subcommand in {"", "status"}:
        used, total, remaining, runtime_cap, native_ctx = context_status(cfg, client=client)
        pct_left = int((remaining / total) * 100) if total > 0 else 0
        ctx_line = f"{used}/{total} tokens ({pct_left}% left)"
        if native_ctx and runtime_cap and native_ctx > runtime_cap:
            ctx_line += f" · runtime cap {runtime_cap:,}"
        console.print(f"[muted]Context window:[/] {ctx_line}")
        console.print(f"  messages       : [text]{len(cfg.messages)}[/]")
        console.print(f"  summary active : [text]{bool(cfg.session_summary.strip())}[/]")
        console.print(f"  summary chars  : [text]{len(cfg.session_summary)}[/]")
        console.print(f"  keep recent    : [text]{CONTEXT_KEEP_MESSAGES} messages[/]")
        console.print(f"  compact at     : [text]{int(CONTEXT_COMPACT_THRESHOLD * 100)}% used[/]")
    elif subcommand == "clear":
        if not cfg.session_summary:
            show_info("No context summary to clear.")
            return
        cfg.session_summary = ""
        cfg.save()
        show_info("Context summary cleared.")
    elif subcommand == "rebuild":
        ok, message = rebuild_context_summary(client, cfg)
        if ok:
            show_info(message)
        else:
            show_error(message)
    else:
        show_error("Usage: /context [status|rebuild|clear]")


def onboard_if_needed(cfg: Config) -> None:
    load_runtime_env(override=True)
    if cfg.onboarded:
        if cfg.cloud and not os.environ.get("OLLAMA_API_KEY"):
            auth_hint_for_cloud()
        return

    console.print("\n[bold cyan]First run setup[/]")
    console.print("Pick a default model once. The CLI will keep using it until you change it with /model or /models.")
    if model_picker(cfg, first_run=True):
        cfg.onboarded = True
        cfg.save()
    else:
        show_info("Setup is still pending. Install or authenticate a model, then restart Algo CLI to try again.")


LESSONS_TOP_K = 5
HARNESS_TOP_K = 6

# Tracks Gemini models we've already shown the workaround notice for this session.
_GEMINI_WORKAROUND_NOTICE_SHOWN: set[str] = set()

READ_ONLY_TOOLS = frozenset({
    "read_file", "read_pdf", "render_pdf_pages", "list_directory",
    "search_files", "git_status", "git_diff", "harness_search", "harness_read", "harness_stats",
    "available_actions", "action_search",
    "model_show",
})


def _terminal_answer_from_tool_calls(tool_calls: list[Any]) -> str | None:
    """Normalize a declared final-answer control call into assistant content."""

    if not tool_calls:
        return None
    normalized = [normalize_tool_call(call) for call in tool_calls]
    if any(name != "final_answer" for name, _args in normalized):
        return None
    answers = [str(args.get("answer") or "").strip() for _name, args in normalized]
    return "\n\n".join(answer for answer in answers if answer)


def configured_embed_dimensions(cfg: Config) -> int | None:
    """Return a valid configured vector width, or None for model default."""
    value = getattr(cfg, "embed_dimensions", None)
    if value is None:
        return None
    try:
        dimensions = int(value)
    except (TypeError, ValueError):
        return None
    return dimensions if dimensions > 0 else None


def make_local_embed_fn(cfg: Config, model: str) -> identity.EmbedFn:
    """Closure that batches Ollama embed calls.

    Prefers the supplemental gateway when it is reachable so batch embedding
    for the harness index piggybacks on the Go proxy and the in-process RAG
    score is faster. Falls back to the direct Ollama client when the gateway
    is not ready or returns an error.
    """
    host = cfg.host
    dimensions = configured_embed_dimensions(cfg)

    def _embed(texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Prefer the supplemental gateway for batch embedding.
        if tools_module.gateway_ready():
            response = tools_module.gateway_embed_batch(
                texts, model, True, dimensions
            )
            if response is not None:
                embeddings = get_attr(response, "embeddings", []) or []
                if embeddings:
                    return [list(vec) for vec in embeddings]
        # Fallback: direct Ollama client.
        client = Client(host=host)
        if dimensions is None:
            client_response = client.embed(model=model, input=texts)
        else:
            client_response = client.embed(model=model, input=texts, dimensions=dimensions)
        embeddings = get_attr(client_response, "embeddings", []) or []
        return [list(vec) for vec in embeddings]

    return _embed
    return _embed


# Per-session backend resolution cache. Cloud embeddings are not currently
# served by Ollama Cloud, so all supported embedding work remains local.
_EMBED_BACKEND_CACHE: dict[str, tuple[str, str]] = {}
_EMBED_BACKEND_ANNOUNCED: set[str] = set()


def resolve_embed_backend(cfg: Config) -> tuple[str, str]:
    """Decide which embedding backend to use this session.

    Returns (backend, reason). Ollama Cloud currently authenticates chat and
    web-search API requests but does not serve embedding models, so 'auto'
    remains local and an explicit 'cloud' setting falls back visibly to local.
    """
    setting = (cfg.embedding_backend or "auto").strip().lower()
    if setting in _EMBED_BACKEND_CACHE:
        return _EMBED_BACKEND_CACHE[setting]

    if setting == "local":
        result = ("local", "embedding_backend=local")
    elif setting == "cloud":
        result = ("local", "cloud embeddings unavailable; using local")
    else:
        result = ("local", "auto: local embeddings only")

    _EMBED_BACKEND_CACHE[setting] = result
    if setting not in _EMBED_BACKEND_ANNOUNCED:
        _EMBED_BACKEND_ANNOUNCED.add(setting)
        show_info(f"Embedding backend: {result[0]} ({result[1]})")
    return result


def reset_embed_backend_cache() -> None:
    """Clear the resolver cache. For tests and config-change handlers."""
    _EMBED_BACKEND_CACHE.clear()
    _EMBED_BACKEND_ANNOUNCED.clear()


def make_embed_fn(cfg: Config, local_model: str) -> tuple[identity.EmbedFn, str, str]:
    """Backend-aware embed factory. Returns (embed_fn, backend, active_model).

    This preserves one routing boundary for future embedding backends while
    selecting only the currently supported local backend.
    """
    backend, _reason = resolve_embed_backend(cfg)
    return make_local_embed_fn(cfg, local_model), backend, local_model


def make_maintenance_llm_fn(cfg: Config) -> skills.LLMFn:
    """Closure wrapping the small local maintenance model as a (system, user) -> text fn."""

    def _llm(system: str, user: str) -> str:
        client, model = small_maintenance_client(cfg)
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=False,
            think=False,
            keep_alive=cfg.keep_alive,
            options={"temperature": 0.2, "num_ctx": min(cfg.num_ctx, 8192)},
        )
        return get_attr(get_attr(response, "message", {}), "content", "") or ""

    return _llm


def make_local_maintenance_llm_fn(cfg: Config) -> skills.LLMFn | None:
    """Build a maintenance closure that cannot fall back to a cloud provider."""

    resolved = local_maintenance_client(cfg)
    if resolved is None:
        return None
    client, model = resolved

    def _llm(system: str, user: str) -> str:
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=False,
            think=False,
            keep_alive=cfg.keep_alive,
            options={"temperature": 0.2, "num_ctx": min(cfg.num_ctx, 8192)},
        )
        return get_attr(get_attr(response, "message", {}), "content", "") or ""

    return _llm


def intuition_embed_fn(cfg: Config) -> identity.EmbedFn | None:
    backend, _reason = resolve_embed_backend(cfg)
    if not host_is_local(cfg.host) or not ollama_server_ready(cfg.host):
        return None
    embed_fn, _backend, _model = make_embed_fn(cfg, harness.resolve_embed_model(cfg))
    return embed_fn


def capture_intuition_block(
    cfg: Config,
    block_type: str,
    content: str,
    *,
    source: str,
    force: bool = False,
) -> str | None:
    if _intuition_engine is None:
        return None
    if not force and not cfg.intuition_capture_enabled:
        return None
    try:
        return _intuition_engine.capture_block(
            block_type,
            content,
            source=source,
            embed_fn=intuition_embed_fn(cfg),
            embedding_model=harness.resolve_embed_model(cfg),
        )
    except Exception as exc:
        logger.debug("Intuition capture failed: %s", exc)
        return None


def handle_icl_command(arg: str, cfg: Config) -> None:
    """index-compute-lab auto-inject and status (/icl)."""
    from . import index_compute_lab

    parts = (arg or "status").strip().split(maxsplit=1)
    sub = (parts[0].lower() if parts else "status") or "status"
    if sub in {"on", "off"}:
        cfg.index_compute_lab_auto_inject = sub == "on"
        cfg.save()
        harness.configure_context_sources(
            external=cfg.external_harness_sources_enabled,
            index_compute_lab=cfg.index_compute_lab_auto_inject,
        )
        harness.load_index(refresh=True)
        show_info(f"index-compute-lab auto-inject: {'ON' if cfg.index_compute_lab_auto_inject else 'OFF'}")
        return
    if sub == "path":
        show_info(f"Lab root: {index_compute_lab.resolve_lab_root()}")
        show_info(f"Available: {index_compute_lab.lab_available()}")
        return
    if sub == "ask":
        if len(parts) < 2 or not parts[1].strip():
            show_error("Usage: /icl ask <question>")
            return
        console.print(index_compute_lab.run_ask(parts[1].strip(), limit=10))
        return
    console.print(f"[muted]index-compute-lab root:[/] {index_compute_lab.resolve_lab_root()}")
    console.print(f"  assets ready     : [text]{index_compute_lab.lab_available()}[/]")
    console.print(f"  auto-inject      : [text]{'on' if cfg.index_compute_lab_auto_inject else 'off'}[/]")
    console.print("[muted]Use /icl on|off, /icl ask <question>, /icl path.[/]")


def handle_intuition_command(arg: str, cfg: Config) -> None:
    sub, _, rest = (arg or "status").strip().partition(" ")
    sub = sub.lower() or "status"
    if _intuition_engine is None:
        show_error("Intuition engine is unavailable.")
        return

    if sub in {"on", "off"}:
        enabled = sub == "on"
        cfg.intuition_recall_enabled = enabled
        cfg.intuition_capture_enabled = enabled
        cfg.save()
        _intuition_engine.config["recall_enabled"] = enabled
        show_info(f"Intuition recall/capture: {'ON' if enabled else 'OFF'}")
        return

    if sub == "status":
        status = _intuition_engine.status()
        console.print(f"[muted]Intuition index:[/] {status['index_path']}")
        console.print(f"  recall enabled : [text]{cfg.intuition_recall_enabled}[/]")
        console.print(f"  capture enabled: [text]{cfg.intuition_capture_enabled}[/]")
        console.print(f"  blocks         : [text]{status['block_count']}[/]")
        console.print(f"  embedded       : [text]{status['embedded']}[/]")
        console.print(f"  pending        : [text]{status['pending']}[/]")
        console.print(f"  max blocks     : [text]{status['max_blocks']}[/]")
        if status["by_type"]:
            console.print(f"  by type        : [text]{json.dumps(status['by_type'], sort_keys=True)}[/]")
        console.print("[muted]Use /intuition on|off|list|reindex|forget <id>|add <type> <text>.[/]")
        return

    if sub == "list":
        blocks = _intuition_engine.list_blocks()
        if not blocks:
            show_info("No intuition blocks saved.")
            return
        table = Table(title=f"Intuition Blocks ({len(blocks)})", box=box.ROUNDED)
        table.add_column("ID", style="primary", overflow="fold")
        table.add_column("Type", style="secondary")
        table.add_column("Embed", style="text")
        table.add_column("Timestamp", style="muted")
        table.add_column("Snippet", style="text", overflow="fold")
        for block in blocks:
            metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
            embed_state = "ready" if block.get("embedding") else str(metadata.get("embedding_status") or "pending")
            snippet = str(block.get("content", "")).replace("\n", " ").strip()
            if len(snippet) > 90:
                snippet = snippet[:89] + "..."
            table.add_row(
                str(block.get("id", "?")),
                str(block.get("type", "general")),
                embed_state,
                str(block.get("timestamp", ""))[:19],
                snippet,
            )
        console.print(table)
        return

    if sub == "forget":
        block_id = rest.strip()
        if not block_id:
            show_error("Usage: /intuition forget <id>")
            return
        removed = _intuition_engine.forget_block(block_id)
        if removed is None:
            show_error(f"No intuition block found: {block_id}")
        else:
            show_info(f"Forgot intuition block: {block_id}")
        return

    if sub == "reindex":
        embed_fn = intuition_embed_fn(cfg)
        if embed_fn is None:
            show_error("Local Ollama is not reachable; cannot reindex intuition blocks.")
            return
        result = _intuition_engine.reindex(embed_fn, embedding_model=harness.resolve_embed_model(cfg))
        if result.get("ok"):
            show_info(
                f"Reindexed {result.get('updated', 0)}/{result.get('total', 0)} intuition blocks "
                f"with {harness.resolve_embed_model(cfg)}."
            )
        else:
            show_error(
                f"Reindex incomplete: {result.get('updated', 0)} updated, "
                f"{result.get('failed', 0)} failed."
            )
        return

    if sub == "add":
        block_type, _, text = rest.strip().partition(" ")
        if not block_type or not text.strip():
            show_error("Usage: /intuition add <type> <text>")
            return
        captured_id = capture_intuition_block(cfg, block_type, text, source="/intuition add", force=True)
        if captured_id:
            show_info(f"Intuition block saved: {captured_id}")
        else:
            show_error("Could not save intuition block.")
        return

    show_error("Usage: /intuition [on|off|status|list|reindex|forget <id>|add <type> <text>]")


def handle_intelligence_command(arg: str = "", cfg: Config | None = None) -> None:
    raw = (arg or "").strip()
    sub, _, rest = raw.partition(" ")
    sub = (sub or "status").lower()
    if sub in {"help", "?"}:
        show_info("Usage: /intelligence [status|query <term>|reindex] (alias: /intel)")
        return
    try:
        from . import intelligence
    except Exception as exc:
        show_error(f"Intelligence layer unavailable: {exc}")
        return

    root = Path(getattr(cfg, "cwd", "") or os.getcwd()).expanduser().resolve()
    if sub == "query":
        term = rest.strip()
        if not term:
            show_error("Usage: /intelligence query <term> (alias: /intel query <term>)")
            return
        graph = intelligence.build_project_graph(root, persist=False)
        rows = intelligence.query_project_graph(graph, term, limit=10)
        console.print(f"[muted]Intelligence query:[/] {term}")
        if not rows:
            console.print("  [text]no matches[/]")
            return
        for row in rows:
            kind = str(row.get("kind", "?"))
            path = str(row.get("path", row.get("id", "?")))
            line = row.get("line")
            suffix = f":{line}" if line else ""
            label = str(row.get("qualname") or row.get("module") or row.get("id") or "")
            console.print(f"  [primary]{kind}[/] [text]{path}{suffix}[/] {label}")
        return

    if sub == "reindex":
        graph = intelligence.build_project_graph(root, persist=True)
        console.print("[muted]Intelligence graph indexed:[/]")
        console.print(f"  root   : [text]{graph.root}[/]")
        console.print(f"  files  : [text]{len(graph.files)}[/]")
        console.print(f"  symbols: [text]{len(graph.symbols)}[/]")
        console.print(f"  imports: [text]{len(graph.imports)}[/]")
        return

    if sub != "status":
        show_error("Usage: /intelligence [status|query <term>|reindex] (alias: /intel)")
        return

    exports = set(getattr(intelligence, "__all__", ()))
    capability_names = [
        "build_project_graph",
        "query_project_graph",
        "GraphRAGIndex",
        "DeepResearchEngine",
        "LSPManager",
        "TaskClassifier",
        "MemoryEngine",
    ]
    available = [name for name in capability_names if name in exports or hasattr(intelligence, name)]
    console.print("[muted]Intelligence layer:[/] wired")
    console.print("  commands    : [text]status, query <term>, reindex[/]")
    console.print(f"  root        : [text]{root}[/]")
    console.print(f"  module      : [text]{intelligence.__name__}[/]")
    console.print(f"  exports     : [text]{len(exports)}[/]")
    console.print(f"  capabilities: [text]{', '.join(available) if available else 'none'}[/]")


def handle_kernel_command(arg: str = "") -> None:
    from .kernels.manifest import audit_kernels, get_kernel, list_kernels, render_kernel_audit

    raw = (arg or "").strip()
    sub, _, rest = raw.partition(" ")
    sub = (sub or "list").lower()

    if sub in {"help", "?"}:
        show_info("Usage: /kernel list | /kernel show NAME | /kernel check [NAME]")
        return

    if sub == "list":
        console.print("Kernels:")
        for spec in list_kernels():
            console.print(f"  {spec.name} ({spec.status}/{spec.safety_level}) - {spec.description}")
        return

    if sub == "show":
        name = rest.strip().lower()
        if not name:
            show_error("Usage: /kernel show NAME")
            return
        selected_spec = get_kernel(name)
        if selected_spec is None:
            show_error(f"Unknown kernel: {name}")
            return
        console.print(f"Kernel: {selected_spec.name}")
        console.print(f"Description: {selected_spec.description}")
        console.print(f"Status: {selected_spec.status}")
        console.print(f"Safety: {selected_spec.safety_level}")
        console.print("Modules:")
        for module in selected_spec.modules:
            console.print(f"  - {module}")
        console.print("Actions:")
        for action in selected_spec.actions:
            console.print(f"  - {action}")
        console.print("Slash commands:")
        for command in selected_spec.slash_commands:
            console.print(f"  - {command}")
        console.print(f"Readiness: /kernel check {selected_spec.name}")
        return

    if sub == "check":
        check_name = rest.strip().lower() or None
        try:
            audits = audit_kernels(check_name)
        except KeyError as exc:
            show_error(str(exc).strip("'"))
            return
        console.print(render_kernel_audit(audits))
        return

    show_error("Usage: /kernel list | /kernel show NAME | /kernel check [NAME]")


def maybe_crystallize_skills(cfg: Config) -> None:
    """Every N substantive runs, review recent run history and crystallize new skills."""
    if not cfg.skill_crystallize_enabled:
        return
    if cfg.runs_since_crystallize < max(1, int(cfg.skill_crystallize_every)):
        return
    llm_fn = make_local_maintenance_llm_fn(cfg)
    if llm_fn is None:
        return
    cfg.runs_since_crystallize = 0
    cfg.save()
    show_info("Crystallizing skills from recent runs…")
    result = skills.crystallize(llm_fn)
    quarantined = result.get("quarantined", [])
    if quarantined:
        show_info(
            f"Quarantined {len(quarantined)} skill candidate(s): {', '.join(quarantined)}. "
            "Review with /skills status, then use /skills approve NAME to promote one."
        )
    else:
        show_info(f"No skill candidates quarantined ({result.get('reason', 'nothing qualified')}).")


def ensure_harness_index(cfg: Config, local_names: list[str] | None = None, *, max_records: int = 0) -> bool:
    """Embed any harness records missing the embedding for the active model.

    Returns True if at least some records have embeddings for DEFAULT_EMBED_MODEL.
    Idempotent: cheap when everything is already embedded.
    """
    backend, _reason = resolve_embed_backend(cfg)
    # Resolve the active model up-front so embedded_count reflects the right backend.
    embed_fn, _backend2, active_model = make_embed_fn(cfg, harness.resolve_embed_model(cfg))
    matching, total = harness.embedded_count(active_model)
    if total == 0:
        return False
    if matching == total:
        return True
    if not host_is_local(cfg.host) or not ollama_server_ready(cfg.host):
        return matching > 0
    # Auto-pull the embed model if it isn't present locally.
    if local_names is None:
        local_names = local_model_names(cfg)
    base_name = active_model.split(":")[0]
    if local_names and not any(n.startswith(base_name) for n in local_names):
        show_info(f"Pulling embed model {active_model} (first-time setup)…")
        try:
            Client(host=cfg.host).pull(active_model)
        except Exception as exc:
            show_info(f"Could not auto-pull {active_model}: {exc}. RAG disabled until model is available.")
            return matching > 0
    pending = total - matching
    # If switching backends/models leaves the index stale, surface that before
    # we kick off what may be a long re-embed pass.
    if matching == 0 and any(r.get("embedding") for r in (harness.load_index().get("records") or [])):
        show_info(
            f"Embedding backend produced a model change to {active_model}; "
            f"all {total} records will be re-embedded under the new model."
        )
    queue_note = ""
    queue = harness.embedding_progress(active_model)
    if int(queue.get("total", 0)) == total and int(queue.get("high_value_total", 0)) > 0:
        next_priority = str(queue.get("next_priority") or "complete").replace("_", " ")
        queue_note = (
            f" High-value coverage: {queue.get('high_value_embedded', 0)}/"
            f"{queue.get('high_value_total', 0)}; next tier: {next_priority}."
        )
    pass_limit = max_records or harness.EMBED_PER_TURN_CAP
    selected = min(pending, pass_limit) if pass_limit > 0 else pending
    show_info(
        f"Embedding the next {selected} of {pending} pending harness records with "
        f"{active_model} ({backend}) using {harness.EMBED_PRIORITY_POLICY}.{queue_note}"
    )
    last_pct = -10
    def _progress(done: int, target: int) -> None:
        nonlocal last_pct
        pct = int(done * 100 / max(1, target))
        if pct - last_pct >= 10:
            show_info(f"  harness embeddings: {done}/{target} ({pct}%)")
            last_pct = pct
    result = harness.embed_index_records(
        embed_fn,
        active_model,
        max_records=max_records or harness.EMBED_PER_TURN_CAP,
        on_progress=_progress,
        on_perf=lambda rec: log_embed_perf(rec, source="ensure_harness_index", backend=backend),
    )
    if result.get("ready"):
        show_info(f"Harness embeddings ready: {result.get('embedded', 0)} embedded, {result.get('total', 0)} total.")
        return True
    if result.get("reason") == "max_records_reached" and int(result.get("embedded", 0)) > 0:
        next_priority = str(result.get("next_priority") or "complete").replace("_", " ")
        show_info(
            f"Harness embeddings partially ready: {result.get('embedded', 0)} embedded this turn, "
            f"{result.get('pending', 0)} pending. Next tier: {next_priority}; continuing incrementally."
        )
        return True
    show_info(f"Harness embedding unavailable: {result.get('reason', 'unknown')}. RAG disabled this session.")
    return matching > 0


def _parse_benchmark_embed_args(arg: str) -> tuple[int, str | None]:
    count = 20
    model: str | None = None
    tokens = arg.split()[1:] if arg else []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--count" and i + 1 < len(tokens):
            try:
                count = max(1, int(tokens[i + 1]))
            except ValueError:
                pass
            i += 2
        elif token == "--model" and i + 1 < len(tokens):
            model = tokens[i + 1]
            i += 2
        else:
            i += 1
    return count, model


def _synthetic_embed_text(index: int) -> str:
    base = (
        "Title: Synthetic harness record\n"
        "Kind: skill\n"
        "Path: synthetic/record_{i}.md\n"
        "Summary: This is a synthetic benchmark record approximating the length of a "
        "typical harness preamble. It exercises tokenization, batching, and Ollama "
        "embed round-trip overhead under a controlled workload."
    ).format(i=index)
    return base + " " + ("filler " * 30)


def run_harness_benchmark_embed(cfg: Config, arg: str) -> None:
    if not host_is_local(cfg.host) or not ollama_server_ready(cfg.host):
        show_error("Local Ollama is not reachable; cannot run embedding benchmark.")
        return
    count, override_model = _parse_benchmark_embed_args(arg)
    model = override_model or harness.resolve_embed_model(cfg)
    embed_fn, backend, model = make_embed_fn(cfg, model)
    show_info(f"Benchmark: embedding {count} synthetic records with {model} ({backend})…")
    texts = [_synthetic_embed_text(i) for i in range(count)]

    single_start = time.perf_counter()
    try:
        embed_fn([texts[0]])
    except Exception as exc:
        show_error(f"Embedding failed: {exc}")
        return
    single_ms = round((time.perf_counter() - single_start) * 1000, 2)

    batch_size = harness.EMBED_BATCH_SIZE
    per_batch_ms: list[float] = []
    overall_start = time.perf_counter()
    try:
        for start in range(0, count, batch_size):
            chunk = texts[start:start + batch_size]
            chunk_start = time.perf_counter()
            embed_fn(chunk)
            per_batch_ms.append(round((time.perf_counter() - chunk_start) * 1000, 2))
    except Exception as exc:
        show_error(f"Embedding failed mid-run: {exc}")
        return
    total_ms = round((time.perf_counter() - overall_start) * 1000, 2)

    sorted_batches = sorted(per_batch_ms)
    p50 = sorted_batches[len(sorted_batches) // 2]
    p95_index = max(0, int(round(len(sorted_batches) * 0.95)) - 1)
    p95 = sorted_batches[p95_index] if sorted_batches else 0.0
    per_record_mean_ms = round(total_ms / max(1, count), 2)

    show_info(
        f"Benchmark complete: count={count} model={model} batch_size={batch_size}"
    )
    show_info(
        f"  total={total_ms}ms  per-record-mean={per_record_mean_ms}ms  "
        f"single-record-baseline={single_ms}ms"
    )
    show_info(
        f"  batch latency p50={p50}ms  p95={p95}ms  batches={len(per_batch_ms)}"
    )

    log_embed_perf(
        {
            "event": "benchmark",
            "count": count,
            "batch_size": batch_size,
            "model": model,
            "total_ms": total_ms,
            "per_record_mean_ms": per_record_mean_ms,
            "single_record_ms": single_ms,
            "batch_p50_ms": p50,
            "batch_p95_ms": p95,
            "batch_count": len(per_batch_ms),
        },
        source="benchmark_embed",
        backend=backend,
    )


def ensure_lessons_index(cfg: Config) -> bool:
    """Rebuild the lessons embedding index if stale. Returns True if index is ready."""
    if not identity.LESSONS_PATH.exists():
        return False
    requested_model = harness.resolve_embed_model(cfg)
    requested_dimensions = configured_embed_dimensions(cfg)
    if not identity.lessons_index_stale(requested_model, requested_dimensions):
        status = identity.lessons_index_status()
        return bool(status.get("index"))
    backend, _reason = resolve_embed_backend(cfg)
    if not host_is_local(cfg.host) or not ollama_server_ready(cfg.host):
        return False
    embed_fn, _backend, active_model = make_embed_fn(cfg, requested_model)
    show_info(f"Embedding lessons with {active_model} ({backend})…")
    result = identity.rebuild_lessons_index(
        embed_fn,
        active_model,
        expected_dimensions=requested_dimensions,
    )
    if result.get("ready"):
        show_info(f"Lessons indexed: {result.get('chunk_count', 0)} chunks.")
        return True
    show_info(f"Lesson embedding unavailable: {result.get('reason', 'unknown')}. Falling back to inline lessons.")
    return False




def agent_loop(client: Client, cfg: Config, user_message: str) -> None:
    if json_sink() is None:
        console.rule(style="border")
    persisted_user_message = user_message
    context_query_message = user_message
    optional_context_blocks: list[OptionalContextBlock] = []
    reconciliation_guidance = reconciliation.guidance_for_prompt(user_message)
    if reconciliation_guidance:
        optional_context_blocks.append(
            OptionalContextBlock("reconciliation", "Structured Reconciliation", reconciliation_guidance)
        )
    active_tools = select_tools_for_prompt(user_message, ALL_TOOLS)
    active_schema_tokens = estimate_tool_schema_tokens(active_tools)
    full_schema_tokens = estimate_tool_schema_tokens(ALL_TOOLS)
    schema_reduction_pct = round(
        100.0 * (full_schema_tokens - active_schema_tokens) / max(1, full_schema_tokens),
        3,
    )
    cfg.context_state["tool_context"] = {
        "catalog_tools": len(ALL_TOOLS),
        "visible_tools": len(active_tools),
        "schema_tokens": active_schema_tokens,
        "full_schema_tokens": full_schema_tokens,
        "reduction_pct": schema_reduction_pct,
    }
    record_perf_event(
        "tool_context",
        catalog_tools=len(ALL_TOOLS),
        visible_tools=len(active_tools),
        schema_tokens=active_schema_tokens,
        full_schema_tokens=full_schema_tokens,
        reduction_pct=schema_reduction_pct,
    )
    for changed_path in identity.detect_changes():
        show_info(f"↻ identity updated · {changed_path.name}")
    # Single memoized embed function shared by both retrieval calls (same model).
    # Saves one Ollama round-trip when both modules embed the same user message.
    _embed_memo: dict[tuple[str, ...], list[list[float]]] = {}
    _embed_base, _embed_backend, _embed_model = make_embed_fn(cfg, harness.resolve_embed_model(cfg))

    def _shared_embed(texts: list[str]) -> list[list[float]]:
        key = tuple(texts)
        hit = _embed_memo.get(key)
        if hit is not None:
            return hit
        result = _embed_base(texts)
        _embed_memo[key] = result
        return result

    # Fetch model metadata once per turn; used to set context window and gate think mode.
    try:
        _active_model_info = _model_info_module.resolve_model_info(cfg, client)
    except Exception:
        _active_model_info = _model_info_module.resolve_model_info(cfg, None)

    _turn_local_models: list[str] = []
    if not cfg.cloud and not _model_info_module.is_xai_model(cfg.model):
        _turn_local_models = local_model_names(cfg)

    retrieved_lessons: list[str] | None = None
    if ensure_lessons_index(cfg):
        retrieved_lessons = identity.retrieve_lessons(
            context_query_message, _shared_embed, _embed_model, k=LESSONS_TOP_K
        )
    retrieved_context: list[dict[str, Any]] | None = None
    from .session_mode import normalize_mode

    _session_mode = normalize_mode(cfg.session_mode)
    harness_tools_available = any(
        getattr(tool, "__name__", "").startswith("harness_") for tool in active_tools
    )
    if (
        _session_mode != "execute"
        and (json_sink() is None or harness_tools_available)
        and ensure_harness_index(cfg, _turn_local_models)
    ):
        retrieved_context = harness.hybrid_search(
            context_query_message, _shared_embed, _embed_model, k=HARNESS_TOP_K
        )
        context_block = harness.format_retrieved_context(retrieved_context or [])
        if context_block:
            optional_context_blocks.append(
                OptionalContextBlock("harness", "Relevant Context", context_block)
            )
    if _intuition_engine is not None:
        try:
            recalled_blocks = _intuition_engine.recall(
                context_query_message,
                enabled=cfg.intuition_recall_enabled,
                embed_fn=_shared_embed if cfg.intuition_recall_enabled else None,
            )
            if recalled_blocks:
                show_recalled_context(recalled_blocks)
                injection = _intuition_engine.format_for_injection(recalled_blocks)
                if injection:
                    optional_context_blocks.append(OptionalContextBlock("intuition", "", injection))
                    context_query_message = f"{context_query_message}\n\n{injection}"
        except Exception as exc:
            logger.debug("Intuition run failed: %s", exc)
    if cfg.index_compute_lab_auto_inject:
        from . import index_compute_lab

        lab_block = index_compute_lab.context_for_query(context_query_message)
        if lab_block:
            optional_context_blocks.append(
                OptionalContextBlock("index-compute-lab", "Knowledge Graph (index-compute-lab)", lab_block)
            )
    if (
        getattr(cfg, "code_rag_enabled", False)
        and getattr(cfg, "code_rag_consent_version", 0) == CODE_RAG_CONSENT_VERSION
        and code_rag_consent_granted(cfg)
        and host_is_local(cfg.host)
        and ollama_server_ready(cfg.host)
        and code_rag.looks_like_code_project(cfg.cwd)
    ):
        try:
            code_hits = code_rag.retrieve(cfg.cwd, persisted_user_message, _shared_embed, _embed_model, k=4)
            code_block = code_rag.format_code_context(code_hits)
            if code_block:
                optional_context_blocks.append(
                    OptionalContextBlock("code", "Working-Directory Code", code_block)
                )
        except Exception as exc:
            logger.debug("Code RAG failed: %s", exc)
    if getattr(cfg, "reasoning_chat_enabled", False):
        if json_sink() is None:
            show_info(f"↳ reasoning preflight ({cfg.reasoning_mode})…")
        plan_block = reasoning_bridge.maybe_reasoning_plan(cfg, client, persisted_user_message)
        if plan_block:
            optional_context_blocks.append(OptionalContextBlock("reasoning", "", plan_block))
    cfg.messages.append({"role": "user", "content": persisted_user_message})
    max_iterations = max(8, int(cfg.max_tool_iterations))
    # Model-aware params: adapt num_ctx/temperature/reflection cadence to the
    # active model's size + provider, honoring any explicit user overrides.
    if getattr(cfg, "model_adaptive", True):
        _profile_params = model_profile.effective_params(cfg, _active_model_info)
        if _profile_params.adapted_fields and json_sink() is None:
            show_info(
                f"↳ model-adaptive ({', '.join(_profile_params.adapted_fields)}): "
                f"ctx={_profile_params.num_ctx} temp={_profile_params.temperature} "
                f"reflect={_profile_params.tool_think_every}"
            )
    else:
        _profile_params = model_profile.EffectiveParams(
            num_ctx=int(cfg.num_ctx),
            temperature=float(cfg.temperature),
            tool_think_every=max(1, int(cfg.tool_think_every)),
            adapted_fields=(),
        )
    reflection_interval = max(1, _profile_params.tool_think_every)
    tool_calls_since_reflection = 0
    run_started = time.perf_counter()
    run_tool_calls: list[dict[str, Any]] = []
    final_content = ""
    turn_completed_normally = False
    iterations_used = 0
    context_selection_notified = False
    small_context_ledger: small_context.SmallContextLedger | None = None
    small_context_notified = False
    completion_nudged = False

    def _fit_request_user_message(system_prompt: str) -> tuple[str, int, list[str], list[str]]:
        nonlocal small_context_ledger, small_context_notified
        base_used = estimate_usage_with_system_prompt(system_prompt, cfg, tools=active_tools)
        _used, _total, _remaining, runtime_cap, _native = context_status(
            cfg,
            client=client,
            model_info=_active_model_info,
            usage_override=base_used,
        )
        base_message = persisted_user_message
        adjusted_base_used = base_used
        live_optional_blocks = optional_context_blocks
        if optional_context_blocks and small_context.is_small_context(runtime_cap):
            if small_context_ledger is None:
                try:
                    small_context_ledger = small_context.write_ledger(
                        model=cfg.model,
                        runtime_cap=runtime_cap,
                        cwd=str(cfg.cwd),
                        base_message=persisted_user_message,
                        optional_blocks=optional_context_blocks,
                        session_summary=cfg.session_summary,
                        messages=cfg.messages,
                    )
                except OSError as exc:
                    logger.debug("Small-context ledger write failed: %s", exc)
            if small_context_ledger is not None:
                trigger = small_context.refresh_trigger(small_context_ledger)
                base_message = f"{persisted_user_message}\n\n{trigger}"
                adjusted_base_used += estimate_text_tokens("\n\n" + trigger)
                if not small_context_notified and json_sink() is None:
                    show_info(f"↳ small-context ledger: {small_context_ledger.path}")
                    small_context_notified = True
        fitted, included, omitted, optional_used = fit_optional_context_blocks(
            base_message,
            live_optional_blocks,
            base_used_tokens=adjusted_base_used,
            runtime_cap=runtime_cap,
            model_info=_active_model_info,
        )
        return fitted, adjusted_base_used + optional_used, included, omitted

    try:
        execution_scope = execution_guardrails.begin_execution_scope(cfg.cwd)
    except execution_guardrails.ExecutionGuardrailError as exc:
        show_error(f"Cannot start a safe execution scope: {exc}")
        return

    from .program_runtime import authorization_for_actions

    _program_auth_missing = object()
    _previous_program_authorization = getattr(
        cfg, "_algo_program_authorization", _program_auth_missing
    )
    setattr(
        cfg,
        "_algo_program_authorization",
        authorization_for_actions(tuple(TOOL_MAP)),
    )

    try:
        # Keep the configured tool/model iteration ceiling strict, but reserve
        # one tool-free response turn when the final capped iteration leaves a
        # verified state. This prevents a correct run from being reported as
        # partial solely because its verifier consumed the last work turn.
        for _ in range(max_iterations + 1):
            finalization_turn = _ == max_iterations
            if finalization_turn:
                completion = execution_guardrails.completion_decision()
                if not completion.allowed:
                    show_error(
                        f"Max tool iterations reached ({max_iterations}) before successful "
                        "post-mutation verification. Use /toolmax to raise or lower the limit."
                    )
                    break
                optional_context_blocks.clear()
                cfg.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[Internal finalization turn] The configured work-iteration budget is "
                            "exhausted and the current result is verified. Do not call more tools. "
                            "Give a concise final answer describing the verified result or any "
                            "remaining blocker."
                        ),
                    }
                )
            iterations_used += 1
            prune_stale_tool_messages(cfg)
            last_user = persisted_user_message
            for msg in reversed(cfg.messages):
                if msg.get("role") == "user":
                    last_user = str(msg.get("content") or persisted_user_message)
                    break
            system_prompt = build_system_prompt(
                cfg,
                retrieved_lessons=retrieved_lessons,
                active_model_info=_active_model_info,
                user_message=last_user,
            )
            request_user_message, precomputed_used, included_contexts, omitted_contexts = _fit_request_user_message(system_prompt)
            if maybe_compact_context(
                client,
                cfg,
                precomputed_used=precomputed_used,
                model_info=_active_model_info,
            ):
                invalidate_context_usage_cache()
                system_prompt = build_system_prompt(
                    cfg,
                    retrieved_lessons=retrieved_lessons,
                    active_model_info=_active_model_info,
                    user_message=last_user,
                )
                request_user_message, precomputed_used, included_contexts, omitted_contexts = _fit_request_user_message(system_prompt)
            request_messages = [{"role": "system", "content": system_prompt}] + cfg.messages
            if request_user_message != persisted_user_message:
                for i in range(len(request_messages) - 1, -1, -1):
                    if request_messages[i].get("role") == "user":
                        msg = dict(request_messages[i])
                        msg["content"] = request_user_message
                        request_messages[i] = msg
                        break
            if optional_context_blocks and not context_selection_notified and json_sink() is None:
                if included_contexts:
                    show_info(f"↳ auto context attached: {', '.join(included_contexts)}")
                if omitted_contexts:
                    show_info(f"↳ context budget omitted: {', '.join(omitted_contexts)}")
                context_selection_notified = True
            thinking_text = ""
            content_text = ""
            tool_calls: list[Any] = []
            message_signature: str | None = None
            completion_pending_before_response = not execution_guardrails.completion_decision().allowed

            # Use the model's real context window when known; cap at the
            # model-adaptive window (which already honors user overrides and the
            # native ceiling).
            _model_ctx = _model_info_module.get_context_length(_active_model_info)
            _effective_ctx = min(_profile_params.num_ctx, _model_ctx) if _model_ctx else _profile_params.num_ctx
            # Gemini-3 ignores think=False at the SDK layer (model thinks by default
            # at MINIMAL levels and still requires thought_signature on every
            # functionCall). Disable thinking on our side AND collapse prior tool_call
            # history into content turns. Pending Ollama PR #14676 / issue #14567.
            if _model_info_module.is_gemini_model(cfg.model):
                _think = False
                request_messages = collapse_tool_history_for_gemini(request_messages)
                if cfg.model not in _GEMINI_WORKAROUND_NOTICE_SHOWN:
                    _GEMINI_WORKAROUND_NOTICE_SHOWN.add(cfg.model)
                    show_info(
                        f"Note: {cfg.model} is using a tool-call workaround pending "
                        "Ollama issue #14567. Tool history is collapsed into content "
                        "turns instead of native functionCall parts — slight reasoning "
                        "tradeoff but tool use works end-to-end."
                    )
            else:
                _think = cfg.show_thinking and _model_info_module.supports_thinking(_active_model_info)
            chat_options: dict[str, Any] = {
                "num_ctx": _effective_ctx,
                "temperature": _profile_params.temperature,
                # num_keep pins the system prompt in the KV cache slot so it is
                # never evicted by sliding-window truncation during long sessions.
                "num_keep": estimate_text_tokens(system_prompt),
            }
            if _model_info_module.is_chatgpt_model(cfg.model):
                chat_options["reasoning_effort"] = chatgpt_client.reasoning_effort_for_model(
                    cfg.model, cfg.chatgpt_reasoning_efforts
                )
            status = None
            if json_sink() is None:
                status = console.status("[muted]waiting for model...[/]", spinner="dots")
                status.start()
            stream_started = False
            stream_error: Exception | None = None
            try:
                stream = client.chat(
                    model=cfg.model,
                    messages=request_messages,
                    tools=[] if finalization_turn else active_tools,
                    stream=True,
                    think=_think,
                    keep_alive=cfg.keep_alive,
                    options=chat_options,
                )

                for chunk in stream:
                    if status is not None:
                        status.stop()
                        status = None
                    record_chat_metrics(cfg, chunk)
                    event_sink = json_sink()
                    record_usage = getattr(event_sink, "chat_usage", None)
                    if callable(record_usage):
                        record_usage(
                            prompt_tokens=get_attr(chunk, "prompt_eval_count", None),
                            completion_tokens=get_attr(chunk, "eval_count", None),
                        )
                    message = get_attr(chunk, "message", {})
                    thinking = get_attr(message, "thinking", "")
                    content = get_attr(message, "content", "")
                    calls = get_attr(message, "tool_calls", None)
                    if thinking and cfg.show_thinking:
                        show_thinking_text(thinking)
                        thinking_text += thinking
                    if content:
                        finish_thinking_block()
                        if not completion_pending_before_response:
                            if not stream_started:
                                start_streaming_response()
                                stream_started = True
                            show_stream_text(content)
                        content_text += content
                    if calls:
                        tool_calls.extend(calls)
                    # Forward-compat: capture any message-level thought_signature
                    # for round-tripping when the SDK starts exposing it.
                    sig = (
                        get_attr(message, "thought_signature", None)
                        or get_attr(message, "thoughtSignature", None)
                    )
                    if sig:
                        message_signature = sig
            except Exception as exc:
                stream_error = exc
            finally:
                if status is not None:
                    status.stop()
                finish_thinking_block()
                finish_streaming_response()

            if json_sink() is None:
                console.print()
            if stream_error is not None and not (content_text or thinking_text):
                raise stream_error
            terminal_answer = _terminal_answer_from_tool_calls(tool_calls)
            if terminal_answer is not None:
                tool_calls = []
                if terminal_answer:
                    content_text = terminal_answer
                    if not completion_pending_before_response:
                        start_streaming_response()
                        show_stream_text(terminal_answer)
                        finish_streaming_response()
            assistant: dict[str, Any] = {"role": "assistant"}
            if content_text:
                assistant["content"] = content_text
                final_content = content_text
            if thinking_text:
                assistant["thinking"] = thinking_text
            serialized_calls = [serialize_tool_call(call) for call in tool_calls]
            if tool_calls and stream_error is None:
                assistant["tool_calls"] = serialized_calls
            if message_signature:
                assistant["thought_signature"] = message_signature
            cfg.messages.append(assistant)

            if stream_error is not None:
                show_error(
                    "Response stream interrupted after partial output: "
                    f"{stream_error}. Partial response retained; retry the request to continue."
                )
                break

            if finalization_turn and tool_calls:
                final_content = ""
                show_error("Finalization turn attempted an additional tool call; completion withheld.")
                break

            if not tool_calls:
                completion = execution_guardrails.completion_decision()
                if completion.allowed:
                    turn_completed_normally = True
                    break
                if not completion_nudged:
                    if _ + 1 >= max_iterations:
                        final_content = ""
                        show_error(
                            "Completion blocked: the tool-iteration budget ended before successful "
                            "post-mutation verification."
                        )
                        break
                    completion_nudged = True
                    final_content = ""
                    optional_context_blocks.clear()
                    show_info(
                        "Unverified final text was withheld. Completion is deferred until the last "
                        "workspace mutation has a passing "
                        "test, lint/type check, or git diff verification."
                    )
                    cfg.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[Internal completion gate] Do not claim completion yet. The last "
                                "workspace mutation has no successful post-mutation verifier. Run one "
                                "appropriate non-mutating test, lint/type check, or git_diff tool now. "
                                "Custom verification must fail on mismatch: run a healthcheck/check/verify "
                                "script, or Python -c with one or more assertions; then give a concise "
                                "final answer grounded in that result."
                            ),
                        }
                    )
                    continue
                final_content = ""
                show_error(
                    "Completion blocked: the model stopped twice without successful verification "
                    "after its last workspace mutation."
                )
                break

            # Normalize all calls once; reused by both the parallel check and _batch build.
            _nc = [normalize_tool_call(c) for c in tool_calls]

            def _exec_one(item: tuple[tuple[str, dict[str, Any]], str | None]) -> tuple[str, dict[str, Any], str | None, str, float]:
                (n, a), tid = item
                t0 = time.perf_counter()
                res = run_tool(n, a, cfg)
                return n, a, tid, res, round((time.perf_counter() - t0) * 1000, 2)

            # Parallel dispatch when all tool calls in this batch are read-only.
            _parallel = (
                len(tool_calls) > 1
                and all(n[0] in READ_ONLY_TOOLS for n in _nc)
                and all(not find_failed_attempt(cfg, tool_attempt_signature(n[0], tool_runtime_args(n[0], n[1], cfg))) for n in _nc)
            )
            if _parallel:
                # _batch items must match _exec_one signature: tuple[tuple[str, dict], str | None]
                _batch = [(n, (str(sc.get("id") or "") or None)) for n, sc in zip(_nc, serialized_calls)]
                for item in _batch:
                    (name, args), tid = item
                    show_tool_call(name, args, call_id=tid)

                ordered_results: list[
                    tuple[str, dict[str, Any], str | None, str, float] | None
                ] = [None] * len(_batch)
                dispatch_order = order_tool_batch_by_qos([item[0] for item in _batch])
                preflight_by_index: dict[int, RuntimeToolPreflight] = {}
                allowed_dispatch_order: list[int] = []
                for queue_position, batch_index in enumerate(dispatch_order):
                    (queued_name, queued_args), _queued_id = _batch[batch_index]
                    preflight = preflight_runtime_tool(
                        queued_name,
                        queued_args,
                        cfg,
                        queue_position=queue_position,
                    )
                    preflight_by_index[batch_index] = preflight
                    if preflight.allowed:
                        allowed_dispatch_order.append(batch_index)
                if allowed_dispatch_order:
                    with scoped_tool_runtime_env(cfg):
                        with ThreadPoolExecutor(max_workers=min(4, len(allowed_dispatch_order))) as _pool:
                            future_to_index = {
                                _pool.submit(_exec_one, _batch[idx]): idx for idx in allowed_dispatch_order
                            }
                            for future in as_completed(future_to_index):
                                ordered_results[future_to_index[future]] = future.result()

                for idx, ((name, args), _tid) in enumerate(_batch):
                    preflight = preflight_by_index[idx]
                    if not preflight.allowed:
                        result = preflight.blocked_result
                        show_tool_result(name, result, approved=False, call_id=_tid)
                        cfg.messages.append(tool_result_message(name, result, _tid))
                        record_tool_attempt(
                            cfg,
                            name=name,
                            args=preflight.signature_args,
                            result=result,
                            status="denied",
                        )
                        record_perf_event(
                            "tool",
                            tool=name,
                            status="denied",
                            duration_ms=0.0,
                            **preflight.qos_fields,
                        )
                        run_tool_calls.append(
                            {"name": name, "status": "denied", "args": _run_args_preview(args, name=name)}
                        )
                        tool_calls_since_reflection += 1
                        continue
                    row = ordered_results[idx]
                    if row is None:
                        continue
                    name, args, tool_call_id, result, duration_ms = row
                    tool_status = classify_tool_status(result)
                    result = augment_tool_result_with_reflex(
                        cfg, name, preflight.signature_args, result, tool_status
                    )
                    show_tool_result(name, result, duration_ms=duration_ms, call_id=tool_call_id)
                    cfg.messages.append(tool_result_message(name, result, tool_call_id))
                    record_tool_attempt(
                        cfg,
                        name=name,
                        args=preflight.signature_args,
                        result=result,
                        status=tool_status,
                    )
                    record_perf_event(
                        "tool",
                        tool=name,
                        status=tool_status,
                        duration_ms=duration_ms,
                        **preflight.qos_fields,
                    )
                    run_tool_calls.append(
                        {"name": name, "status": tool_status, "args": _run_args_preview(args, name=name)}
                    )
                    tool_calls_since_reflection += 1
            else:
                for call, serialized_call in zip(tool_calls, serialized_calls):
                    tool_call_id = str(serialized_call.get("id") or "") or None
                    name, args = normalize_tool_call(call)
                    show_tool_call(name, args, call_id=tool_call_id)
                    preflight = preflight_runtime_tool(name, args, cfg)
                    signature_args = preflight.signature_args
                    if not preflight.allowed:
                        result = preflight.blocked_result
                        show_tool_result(name, result, approved=False, call_id=tool_call_id)
                        cfg.messages.append(tool_result_message(name, result, tool_call_id))
                        record_tool_attempt(cfg, name=name, args=signature_args, result=result, status="denied")
                        record_perf_event(
                            "tool",
                            tool=name,
                            status="denied",
                            duration_ms=0.0,
                            **preflight.qos_fields,
                        )
                        run_tool_calls.append(
                            {"name": name, "status": "denied", "args": _run_args_preview(args, name=name)}
                        )
                        tool_calls_since_reflection += 1
                        continue
                    signature = tool_attempt_signature(name, signature_args)
                    previous_failure = find_failed_attempt(cfg, signature)
                    if previous_failure:
                        result = (
                            "Skipped repeated failed attempt. "
                            f"Prior outcome: {previous_failure.get('summary', 'same tool path already failed or was denied')}."
                        )
                        show_tool_result(name, result, approved=False, call_id=tool_call_id)
                        cfg.messages.append(tool_result_message(name, result, tool_call_id))
                        record_tool_attempt(cfg, name=name, args=signature_args, result=result, status="skipped")
                        record_perf_event(
                            "tool",
                            tool=name,
                            status="skipped",
                            duration_ms=0.0,
                            **preflight.qos_fields,
                        )
                        run_tool_calls.append({"name": name, "status": "skipped", "args": _run_args_preview(args, name=name)})
                        tool_calls_since_reflection += 1
                        continue
                    if not ask_approval(name, args, cfg):
                        result = "User denied this operation."
                        show_tool_result(name, result, approved=False, call_id=tool_call_id)
                        cfg.messages.append(tool_result_message(name, result, tool_call_id))
                        record_tool_attempt(cfg, name=name, args=signature_args, result=result, status="denied")
                        record_perf_event(
                            "tool",
                            tool=name,
                            status="denied",
                            duration_ms=0.0,
                            **preflight.qos_fields,
                        )
                        run_tool_calls.append({"name": name, "status": "denied", "args": _run_args_preview(args, name=name)})
                        continue

                    started = time.perf_counter()
                    with scoped_tool_runtime_env(cfg):
                        with tool_execution_status(
                            f"[muted]executing {name} · {preflight.runtime_hint.spawn_class.value}...[/]"
                        ):
                            result = run_tool(name, args, cfg)
                    duration_ms = round((time.perf_counter() - started) * 1000, 2)
                    tool_status = classify_tool_status(result)
                    result = augment_tool_result_with_reflex(
                        cfg, name, signature_args, result, tool_status
                    )
                    show_tool_result(name, result, duration_ms=duration_ms, call_id=tool_call_id)
                    cfg.messages.append(tool_result_message(name, result, tool_call_id))
                    record_tool_attempt(
                        cfg, name=name, args=signature_args, result=result, status=tool_status
                    )
                    record_perf_event(
                        "tool",
                        tool=name,
                        status=tool_status,
                        duration_ms=duration_ms,
                        **preflight.qos_fields,
                    )
                    run_tool_calls.append(
                        {"name": name, "status": tool_status, "args": _run_args_preview(args, name=name)}
                    )
                    tool_calls_since_reflection += 1

            if tool_calls_since_reflection >= reflection_interval:
                if json_sink() is None:
                    reflection_checkpoint(client, cfg, persisted_user_message, reflection_interval)
                tool_calls_since_reflection -= reflection_interval
    finally:
        try:
            execution_guardrails.end_execution_scope(execution_scope)
        except execution_guardrails.ExecutionGuardrailError as exc:
            logger.error("Could not close execution guardrail scope: %s", exc)
            turn_completed_normally = False
        if _previous_program_authorization is _program_auth_missing:
            try:
                delattr(cfg, "_algo_program_authorization")
            except AttributeError:
                pass
        else:
            setattr(cfg, "_algo_program_authorization", _previous_program_authorization)
        cfg.save()
        flush_perf_records()

    # Tier-3: claim-grounding verify pass when verify_mode is active.
    if (
        turn_completed_normally
        and cfg.verify_mode
        and final_content
        and host_is_local(cfg.host)
        and ollama_server_ready(cfg.host)
    ):
        try:
            llm_fn = make_maintenance_llm_fn(cfg)
            report = _verify_module.verify_response(final_content, llm_fn)
            summary = _verify_module.format_verification_report(report)
            if summary:
                show_info(summary)
                if report["confidence"] < 0.5:
                    show_info(
                        "⚠ Less than half the model's claims are grounded in the harness. "
                        "Treat specific facts with caution and verify with tool calls."
                    )
        except Exception:
            pass

    memory_result = memory_runtime.capture_completed_user_turn(
        cfg,
        persisted_user_message,
        completed=turn_completed_normally,
        tool_calls=run_tool_calls,
        source="chat",
    )
    flush_perf_records()
    if memory_result.get("status") == "stored":
        show_info("Saved 1 durable memory automatically; review it with /memories.")

    # Post-run: record the completed run, then periodically crystallize skills.
    if cfg.skill_crystallize_enabled and run_tool_calls and turn_completed_normally:
        run_duration_ms = round((time.perf_counter() - run_started) * 1000, 2)
        skills.record_run(
            goal=user_message,
            tool_calls=run_tool_calls,
            outcome=final_content,
            iterations=iterations_used,
            duration_ms=run_duration_ms,
        )
        cfg.runs_since_crystallize += 1
        cfg.save()
        maybe_crystallize_skills(cfg)


GOAL_COMPLETE_MARKER = "GOAL COMPLETE"
GOAL_BLOCKED_MARKER = "GOAL BLOCKED:"
GOAL_DEFAULT_MAX_ROUNDS = 10

_GOAL_INSTRUCTIONS = (
    "\n\n[Goal mode] Work toward the goal above until it is 100% complete.\n"
    "- Verify progress with tools (run tests, read files) before claiming completion.\n"
    f"- When and ONLY when the goal is fully complete and verified, end your reply with a line containing exactly: {GOAL_COMPLETE_MARKER}\n"
    f"- If you cannot proceed without input only the user can give, end with: {GOAL_BLOCKED_MARKER} <one-line reason>\n"
    "- Otherwise just keep working; you will be asked to continue."
)

_GOAL_CONTINUE_PROMPT = (
    "[Goal mode] The goal is not yet marked complete. Re-check what remains, "
    "continue working, and verify with tools. End with the completion or "
    "blocked marker per the goal-mode rules."
)


def _last_assistant_content(cfg: Config) -> str:
    for message in reversed(cfg.messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""


def parse_goal_args(arg: str) -> tuple[str, int]:
    """Parse '/goal [--rounds N] TASK' into (task, max_rounds)."""
    tokens = (arg or "").split()
    max_rounds = GOAL_DEFAULT_MAX_ROUNDS
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "--rounds" and i + 1 < len(tokens):
            try:
                max_rounds = max(1, int(tokens[i + 1]))
            except ValueError:
                pass
            i += 2
            continue
        rest.append(tokens[i])
        i += 1
    return " ".join(rest).strip(), max_rounds


def _drive_goal(client: Client, cfg: Config, record: task_ledger.GoalRecord, *, first_prompt: str) -> None:
    """Run rounds for an (already-persisted) goal record until done/blocked/cap."""
    prompt = first_prompt
    remaining = record.max_rounds - record.rounds_done
    if remaining <= 0:
        show_error(
            f"Goal already used its {record.max_rounds}-round budget. "
            "Raise it with /goal resume --rounds N, or /goal clear to drop it."
        )
        return
    show_info(f"Goal mode: {remaining} round(s) remaining of {record.max_rounds}. Ctrl+C to stop.")
    for _ in range(remaining):
        round_number = record.rounds_done + 1
        show_info(f"Goal round {round_number}/{record.max_rounds}")
        try:
            agent_loop(client, cfg, prompt)
        except KeyboardInterrupt:
            record.status = task_ledger.STATUS_STOPPED
            record.reason = "interrupted by user"
            record.add_round("(interrupted)")
            task_ledger.save_goal(record)
            show_info(f"Goal stopped after round {round_number}. Resume with /goal resume.")
            return
        final = _last_assistant_content(cfg)
        record.add_round(final[:500])
        if GOAL_COMPLETE_MARKER in final:
            record.status = task_ledger.STATUS_COMPLETE
            task_ledger.save_goal(record)
            show_info(f"Goal marked complete after {round_number} round(s).")
            return
        blocked_at = final.find(GOAL_BLOCKED_MARKER)
        if blocked_at != -1:
            reason_lines = final[blocked_at + len(GOAL_BLOCKED_MARKER):].strip().splitlines()
            reason = reason_lines[0] if reason_lines else "(no reason given)"
            record.status = task_ledger.STATUS_BLOCKED
            record.reason = reason
            task_ledger.save_goal(record)
            show_error(f"Goal blocked: {reason}")
            return
        record.status = task_ledger.STATUS_RUNNING
        task_ledger.save_goal(record)
        prompt = _GOAL_CONTINUE_PROMPT
    record.status = task_ledger.STATUS_STOPPED
    record.reason = "round cap reached"
    task_ledger.save_goal(record)
    show_error(
        f"Goal not marked complete after {record.max_rounds} rounds. "
        "Continue with /goal resume [--rounds N], or /goal clear to drop it."
    )


def show_goal_status() -> None:
    record = task_ledger.load_goal()
    if record is None:
        show_info("No active goal. Start one with /goal <task>.")
        return
    console.print(f"[bold primary]Goal:[/] {record.goal}")
    console.print(f"  status : [text]{record.status}[/]")
    console.print(f"  rounds : [text]{record.rounds_done}/{record.max_rounds}[/]")
    if record.cwd:
        console.print(f"  cwd    : [text]{record.cwd}[/]")
    if record.reason:
        console.print(f"  reason : [text]{record.reason}[/]")
    if record.history:
        last = record.history[-1]
        console.print(f"  last   : [muted]{str(last.get('summary', ''))[:160]}[/]")
    if record.is_open:
        console.print("[muted]Resume with /goal resume; drop with /goal clear.[/]")


def run_goal_loop(client: Client, cfg: Config, arg: str) -> None:
    """Drive agent_loop rounds until the model marks the goal complete/blocked.

    Subcommands: /goal status, /goal resume [--rounds N], /goal clear.
    """
    stripped = (arg or "").strip()
    sub = stripped.split(maxsplit=1)[0].lower() if stripped else ""

    if sub == "status":
        show_goal_status()
        return
    if sub == "clear":
        show_info("Cleared active goal." if task_ledger.clear_goal() else "No active goal to clear.")
        return
    if sub == "resume":
        record = task_ledger.load_goal()
        if record is None:
            show_error("No saved goal to resume. Start one with /goal <task>.")
            return
        if not record.is_open:
            show_error(f"Saved goal is '{record.status}', not resumable. Use /goal <task> to start fresh.")
            return
        _, extra_rounds = parse_goal_args(stripped[len(sub):])
        if "--rounds" in stripped:
            record.max_rounds = record.rounds_done + extra_rounds
        if record.cwd:
            cfg.cwd = record.cwd
        show_info(f"Resuming goal: {record.goal}")
        _drive_goal(client, cfg, record, first_prompt=_GOAL_CONTINUE_PROMPT)
        return

    goal, max_rounds = parse_goal_args(stripped)
    if not goal:
        show_error("Usage: /goal [--rounds N] <task>  |  /goal resume|status|clear  "
                   f"(default rounds: {GOAL_DEFAULT_MAX_ROUNDS}; Ctrl+C stops)")
        return
    record = task_ledger.GoalRecord(goal=goal, max_rounds=max_rounds, cwd=cfg.cwd)
    task_ledger.save_goal(record)
    _drive_goal(client, cfg, record, first_prompt=f"GOAL: {goal}{_GOAL_INSTRUCTIONS}")


def print_models(client: Client) -> None:
    models = client.list()
    items = get_attr(models, "models", []) or []
    if not items:
        show_info("No local models returned.")
        return
    for model in items:
        name = get_attr(model, "name", None) or get_attr(model, "model", "?")
        size = get_attr(model, "size", 0) or 0
        size_text = f"{size / 1e9:.1f} GB" if size else "?"
        console.print(f"  {name} ({size_text})")


def print_harness_results(
    query: str,
    cfg: Config | None = None,
    harness_name: str | None = None,
    kind: str | None = None,
) -> None:
    if cfg is not None:
        ensure_harness_index(cfg)  # embed any pending records before searching
    if cfg is not None:
        embed_fn, _backend, active_model = make_embed_fn(cfg, harness.resolve_embed_model(cfg))
        matching, _total = harness.embedded_count(active_model)
    else:
        embed_fn = None
        active_model = harness.DEFAULT_EMBED_MODEL
        matching, _total = harness.embedded_count(active_model)
    if cfg is not None and embed_fn is not None and matching > 0:
        results = harness.hybrid_search(query, embed_fn, active_model, k=12, harness=harness_name, kind=kind)
        if results:
            lines = [f"[dim]hybrid (RRF) results for:[/] {query}", ""]
            for rec in results:
                harness_kind = " · ".join(filter(None, [rec.get("harness"), rec.get("kind")]))
                score = rec.get("score", 0.0)
                lines.append(f"  [primary]{rec.get('id', '')}[/]  {rec.get('title', '')}  [muted]{harness_kind}  rrf={score:.4f}[/]")
                if rec.get("snippet"):
                    lines.append(f"    [muted]{rec['snippet'][:160]}[/]")
            console.print("\n".join(lines))
            return
    from .tools import harness_search
    console.print(harness_search(query=query, harness_name=harness_name, kind=kind, limit=12))


def build_rust_indexer() -> None:
    source_dir = Path(__file__).resolve().parents[1] / "harness-indexer"
    cargo = shutil.which("cargo") or (
        str(Path.home() / ".cargo" / "bin" / "cargo.exe")
        if (Path.home() / ".cargo" / "bin" / "cargo.exe").exists()
        else ""
    )
    if not source_dir.exists():
        show_error(f"Rust indexer source not found: {source_dir}")
        return
    if not cargo:
        show_error("Cargo was not found. Install Rust or add cargo to PATH.")
        return
    show_info("Building optional Rust harness indexer with `cargo build --release`.")
    try:
        proc = subprocess.run(
            [cargo, "build", "--release"],
            cwd=source_dir,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=180,
        )
    except Exception as exc:
        show_error(f"Could not build Rust indexer: {exc}")
        return
    if proc.returncode != 0:
        show_error((proc.stderr or proc.stdout or "Rust indexer build failed.").strip())
        return
    binary = harness.find_rust_indexer()
    if binary:
        show_info(f"Rust harness indexer built: {binary}")
        try:
            harness.INDEX_PATH.unlink(missing_ok=True)
            harness._INDEX_CACHE = None
            index = harness.load_index(refresh=True)
        except Exception as exc:
            show_error(f"Rust indexer built, but immediate refresh failed: {exc}")
            return
        show_info(f"Rust harness indexer exercised. Records: {index.get('record_count', 0)}.")
    else:
        show_error("Cargo build finished, but the Rust indexer binary was not found.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agent runtime for tools, durable context, and verified work."
    )
    parser.add_argument("--model", help="Model to use for this session.")
    parser.add_argument("--host", help="Local Ollama host.")
    parser.add_argument("--cloud", action="store_true", help="Use Ollama Cloud client defaults.")
    parser.add_argument("--cwd", help="Working directory for tools.")
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show full version manifest (CLI, Python, platform, harness, plugins) and exit.",
    )
    parser.add_argument(
        "--oneshot",
        action="store_true",
        help="Run a single non-interactive turn and exit. Requires --json. Prompt comes from positional arg or stdin.",
    )
    parser.add_argument(
        "--json",
        dest="json_events",
        action="store_true",
        help="Emit one JSON event per line to stdout (NDJSON). Implies non-Rich output; only valid with --oneshot.",
    )
    parser.add_argument(
        "--approval-mode",
        choices=["never", "auto"],
        default="never",
        help="In --oneshot, control how approval-required tools are handled: never (default, deny + tool_denied event) or auto (auto-approve, equivalent to /auto).",
    )
    parser.add_argument(
        "--thinking",
        choices=["auto", "on", "off"],
        default="auto",
        help="In --oneshot, use adaptive deliberation (default), force model thinking on, or force it off.",
    )
    parser.add_argument("prompt", nargs="*", help="Prompt for --oneshot mode. If omitted, read from stdin. Use `doctor` for readiness diagnostics, `plugin list` for plugins, `credential list` for credential helpers, `url-scheme <url>` for URL scheme parsing.")
    ns = parser.parse_args(argv)
    # Normalize nargs="*" list into a single string for downstream code
    if ns.prompt:
        ns.prompt = " ".join(ns.prompt)
    else:
        ns.prompt = None
    return ns


def _run_oneshot_entry(args: argparse.Namespace) -> int:
    if not args.json_events:
        sys.stderr.write("--oneshot requires --json (NDJSON output). Exiting.\n")
        return 64
    # args.prompt is normalized to a string by parse_args()/main()
    prompt = args.prompt or sys.stdin.read()
    prompt = (prompt or "").strip()
    if not prompt:
        sys.stderr.write("No prompt provided (positional arg empty and stdin empty). Exiting.\n")
        return 64
    overrides: dict[str, Any] = {}
    if args.model:
        overrides["model"] = chatgpt_client.normalize_codex_model(args.model)
    if args.host:
        overrides["host"] = args.host
        overrides["cloud"] = False
    if args.cloud:
        overrides["cloud"] = True
    if args.cwd:
        overrides["cwd"] = str(Path(args.cwd).expanduser().resolve())
    if args.thinking != "auto":
        overrides["show_thinking"] = args.thinking == "on"
    from . import oneshot as _oneshot_module
    return _oneshot_module.run_oneshot(
        prompt=prompt,
        approval_mode=args.approval_mode,
        cfg_overrides=overrides or None,
    )


def _force_utf8_console() -> None:
    """Reconfigure Windows console + Python streams to use UTF-8.

    Source files are now clean UTF-8, but Windows consoles default to cp1252
    and downgrade glyphs (●, ·, →, ⚡, ✓, ⏵, etc.) at render time. Without
    this helper the status bar would re-mojibake even after the source-level
    repair. On non-Windows or when stdout/stderr are not real TTYs this is a
    no-op.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # CP_UTF8 = 65001
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
            # Best-effort: enable VT processing so Rich can paint colors/unicode
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(kernel32.GetStdHandle(-11), ctypes.byref(mode)):
                ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                if not (mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING):
                    kernel32.SetConsoleMode(
                        kernel32.GetStdHandle(-11),
                        mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING,
                    )
    except Exception:
        # Never let codec setup crash startup; log and fall through.
        try:
            logging.getLogger(__name__).debug("utf8 console setup failed", exc_info=True)
        except Exception:
            pass
    # Python stream encoding (works on every platform).
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


def main() -> None:
    _force_utf8_console()
    if Path(sys.argv[0]).name.lower().startswith("ollama-cli"):
        console.print("[warning]`ollama-cli` is deprecated; use `algo-cli` instead.[/]")
    load_runtime_env(override=True)
    args = parse_args()
    if args.version:
        from .version_manifest import build_manifest, format_version_string
        console.print(format_version_string(build_manifest()))
        return
    if args.oneshot:
        _exit = _run_oneshot_entry(args)
        sys.exit(_exit)
    # Migration to new default location (~/.algo_cli) must happen before any
    # first-run scaffolding writes into CONFIG_DIR; otherwise the migration
    # helper will correctly refuse to overwrite the newly-created directory and
    # legacy memories/config are stranded in ~/.ollama_cli.
    already_migrated = (CONFIG_DIR / ".migrated_from_legacy").exists()
    migrated = False
    if has_legacy_data() and not already_migrated:
        migrated = perform_legacy_migration()

    sidecar = migrate_legacy_sidecar_files()
    if sidecar:
        show_info(
            f"Imported legacy config file(s) into {CONFIG_DIR}: {', '.join(sidecar)}"
        )

    cfg = Config.load()
    harness.configure_context_sources(
        external=cfg.external_harness_sources_enabled,
        index_compute_lab=cfg.index_compute_lab_auto_inject,
    )
    if args.model:
        cfg.model = chatgpt_client.normalize_codex_model(args.model)
    if args.host:
        cfg.host = args.host
        cfg.cloud = False
    if args.cloud:
        cfg.cloud = True
    if args.cwd:
        cfg.cwd = str(Path(args.cwd).expanduser().resolve())
    if (args.prompt or "").strip().lower() == "doctor" and not args.oneshot:
        from .action_registry import build_doctor_report, render_doctor

        report = build_doctor_report(cfg)
        console.print(render_doctor(report))
        if report.overall_status == "blocked":
            raise SystemExit(1)
        return

    created = identity.scaffold_if_needed()
    if created:
        show_info(f"Scaffolded identity in {identity.IDENTITY_DIR} ({len(created)} files). Edit USER.md to teach the CLI about yourself.")
    skills.ensure_dirs()
    from . import index_compute_lab

    if index_compute_lab.ensure_harness_roots_file():
        show_info(
            "Removed legacy index-compute-lab entry from harness_roots.json "
            f"(lab is indexed dynamically from {index_compute_lab.resolve_lab_root()}). "
            "Run /harness refresh to drop duplicate atom records."
        )

    # --- Subcommand: plugin list ---
    _prompt_lower = (args.prompt or "").strip().lower()
    if _prompt_lower.startswith("plugin ") and not args.oneshot:
        from .plugins import discover_plugins, plugin_status
        sub = _prompt_lower.split(maxsplit=1)[1].strip() if " " in _prompt_lower else ""
        if sub in ("", "list"):
            discovered = discover_plugins()
            if not discovered:
                console.print("[dim]No plugins discovered in ~/.algo_cli/plugins/[/dim]")
            else:
                table = Table(title="Discovered Plugins", box=box.ROUNDED)
                table.add_column("Name", style="cyan")
                table.add_column("Version", style="green")
                table.add_column("Description")
                table.add_column("Enabled", style="yellow")
                for manifest in sorted(discovered, key=lambda item: item.name.lower()):
                    table.add_row(
                        manifest.name,
                        manifest.version,
                        manifest.description,
                        "yes" if manifest.enabled else "no",
                    )
                console.print(table)
            return
        elif sub == "status":
            statuses = plugin_status()
            if not statuses:
                console.print("[dim]No plugins loaded.[/dim]")
            else:
                table = Table(title="Plugin Status", box=box.ROUNDED)
                table.add_column("Name", style="cyan")
                table.add_column("Loaded", style="green")
                table.add_column("Error", style="red")
                for s in statuses:
                    table.add_row(
                        s.get("name", "?"),
                        "yes" if s.get("loaded") else "no",
                        s.get("error", "") or "",
                    )
                console.print(table)
            return
        else:
            console.print("[yellow]Usage: algo-cli plugin [list|status][/yellow]")
            return

    # --- Subcommand: credential list ---
    if _prompt_lower.startswith("credential ") and not args.oneshot:
        from .credential_helpers import list_helpers, get_helper
        sub = _prompt_lower.split(maxsplit=1)[1].strip() if " " in _prompt_lower else ""
        if sub in ("", "list"):
            helpers = sorted(list_helpers())
            if not helpers:
                console.print("[dim]No credential helpers registered.[/dim]")
            else:
                table = Table(title="Credential Helpers", box=box.ROUNDED)
                table.add_column("Name", style="cyan")
                table.add_column("Description")
                for name in helpers:
                    h = get_helper(name)
                    table.add_row(name, h.description if h else "?")
                console.print(table)
            return
        elif sub.startswith("get "):
            raw_prompt = args.prompt.strip()
            parts = raw_prompt.split(maxsplit=3)
            if len(parts) != 4:
                console.print("[yellow]Usage: algo-cli credential get <helper> <key>[/yellow]")
                return
            _command, _verb, helper, key = parts
            from .credential_helpers import get_credential
            val = get_credential(helper, key)
            if val is None:
                console.print(f"[dim]No credential found for '{key}' in helper '{helper}'[/dim]")
            else:
                console.print(f"[green]{helper}/{key}[/green]: configured (value redacted)")
            return
        else:
            console.print("[yellow]Usage: algo-cli credential [list|get <helper> <key>][/yellow]")
            return

    # --- Subcommand: url-scheme <url> ---
    if _prompt_lower.startswith("url-scheme ") and not args.oneshot:
        from .url_scheme import handle_deep_link, format_help
        url = args.prompt.strip().split(maxsplit=1)[1] if " " in args.prompt.strip() else ""
        if not url or url == "help":
            console.print(format_help())
            return
        result = handle_deep_link(url)
        if not result.get("valid"):
            console.print(f"[red]Invalid URL: {result.get('error', 'unknown error')}[/red]")
            return
        console.print(f"[green]Action:[/green] {result.get('action', '?')}")
        if result.get('target'):
            console.print(f"[green]Target:[/green] {result['target']}")
        if result.get('query'):
            console.print(f"[green]Query:[/green] {result['query']}")
        return
    try:
        cfg.theme = set_theme(cfg.theme)
    except ValueError:
        cfg.theme = current_theme_name()
    show_banner()
    show_info("Ask naturally. Type / for commands or /status for runtime details.")

    # Report migration after banner/theme initialization so the message is visible.
    if migrated:
        show_info(
            f"Data migrated from legacy location {LEGACY_CONFIG_DIR} → new default {CONFIG_DIR}."
        )
        show_info(
            f"Full backup preserved at {get_legacy_backup_dir()} (originals untouched)."
        )
        show_info(
            "You are now using the new default config directory. "
            "Legacy OLLAMA_CLI_* environment variables and the `ollama-cli` command "
            "remain available as compatibility aliases."
        )

    # Deprecation notice when old OLLAMA_CLI_* vars are still in use
    used_old = [k for k in os.environ if k.startswith(OLD_ENV_PREFIX) and not k.startswith(NEW_ENV_PREFIX)]
    if used_old:
        show_info(
            "Compatibility notice: legacy OLLAMA_CLI_* environment variables are active. "
            "Use ALGO_CLI_* for new configuration."
        )

    onboard_if_needed(cfg)
    cfg.save()

    if cfg.cloud:
        start_supplemental_gateway(cfg)
    else:
        start_ollama_server(cfg)
    client = create_client(cfg)

    try:
        from prompt_toolkit import PromptSession
        slash_completer = SlashCommandCompleter(SLASH_COMMANDS)
        palette = theme_colors(cfg.theme)
        session: PromptSession[str] | None = PromptSession(
            history=SafeFileHistory(str(PROMPT_HISTORY_FILE)),
            completer=slash_completer,
            complete_while_typing=True,
            complete_style=CompleteStyle.MULTI_COLUMN,
            style=build_prompt_style(palette),
            bottom_toolbar=lambda: build_status_toolbar(cfg),
            rprompt=lambda: build_status_rprompt(cfg),
            reserve_space_for_menu=8,
        )
    except Exception:
        session = None

    while True:
        try:
            refresh_runtime_status(cfg, client)
            user_input = (
                session.prompt(" ❯ ", complete_style=CompleteStyle.MULTI_COLUMN)
                if session
                else input(" ❯ ")
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye.[/]")
            break
        if not user_input:
            continue
        user_input = sanitize_prompt_text(user_input)
        if user_input.startswith("/"):
            try:
                handled, client = handle_command(user_input, cfg, client, session)
            except EOFError:
                console.print("\n[dim]Bye.[/]")
                break
            except Exception as exc:
                show_error(str(exc))
                refresh_runtime_status(cfg, client)
                invalidate_prompt_toolbar(session)
                continue
            if handled:
                continue
            show_error(unknown_command_message(user_input))
            continue
        try:
            if cfg.cloud:
                start_supplemental_gateway(cfg)
            elif not start_ollama_server(cfg):
                continue
            maybe_show_route_suggestion(user_input)
            agent_loop(client, cfg, user_input)
            refresh_runtime_status(cfg, client)
            invalidate_prompt_toolbar(session)
            if session is None:
                used, total, _remaining, _runtime_cap, _native = context_status(cfg, client=client)
                show_status_footer(
                    cfg.model,
                    used,
                    total,
                    summary_active=bool(cfg.session_summary.strip()),
                )
        except KeyboardInterrupt:
            console.print("\n[yellow]Generation interrupted.[/]")
            refresh_runtime_status(cfg, client)
            invalidate_prompt_toolbar(session)
        except Exception as exc:
            show_error(str(exc))
            refresh_runtime_status(cfg, client)
            invalidate_prompt_toolbar(session)


if __name__ == "__main__":
    main()
