"""Slash-command table, completion, and REPL dispatch."""

from __future__ import annotations

import json
import os
from difflib import get_close_matches
from pathlib import Path
from typing import Any
from dataclasses import fields

from ollama import Client
from prompt_toolkit.completion import Completer, Completion

from . import display
from .config import (
    CODE_RAG_CONSENT_VERSION,
    Config,
    code_rag_consent_granted,
    safe_conversation_name,
)
from . import code_rag
from . import harness
from . import identity
from . import memory_candidates
from . import perf_telemetry
from . import reflex
from . import skills


SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "Show help"),
    ("/model", "Show or switch model"),
    ("/models", "Browse available local and authenticated provider models"),
    ("/host", "Set local Ollama host"),
    ("/cloud", "Set/toggle direct Ollama Cloud API mode: /cloud [on|off|status]"),
    ("/login", "Run Ollama signin"),
    ("/cloudauto", "Set/toggle cloud auto-connect: /cloudauto [on|off|status]"),
    ("/xai-login", "Optional xAI OAuth; requires your XAI_CLIENT_ID. Flags: --no-browser, --manual"),
    ("/xai-logout", "Revoke local xAI tokens"),
    ("/xai-status", "Show optional xAI OAuth setup and auth state"),
    ("/xai-test", "List xAI models the current token can access"),
    ("/google-login", "Authenticate with Google Workspace (OAuth2 loopback). Flags: --no-browser, --manual"),
    ("/google-callback", "Complete Google Workspace OAuth from callback URL. Flags: --clipboard, --file PATH"),
    ("/google-logout", "Revoke local Google Workspace tokens"),
    ("/google-status", "Show Google Workspace auth state"),
    ("/google", "Run a Google Workspace command: drive-list, drive-search, drive-get, docs-get, sheets-values, calendar-list, gmail-list, gmail-get, help"),
    ("/chatgpt-login", "Authenticate with ChatGPT/Codex OAuth. Flags: --no-browser, --manual, --device-code"),
    ("/chatgpt-logout", "Revoke local ChatGPT tokens"),
    ("/chatgpt-status", "Show ChatGPT auth state"),
    ("/model-check", "Report Algo CLI support for a Grok/xAI model name"),
    ("/x-account", "Manage X account via xurl: status, draft-post, draft-reply, post --confirm, reply --confirm"),
    ("/system", "Show or set system prompt"),
    ("/auto", "Set/toggle auto-approve: /auto [on|off|status]"),
    ("/safe", "Set/toggle safe mode: /safe [on|off|status]"),
    ("/policy", "Toggle or inspect algorithmic tool policy enforcement"),
    ("/reflex", "Toggle reflex self-heal loop (v0.1, default off)"),
    ("/reason", "Reasoning engine: /reason status|guide|react|reflexion|tot|got|mcts|qcr|neuro_symbolic"),
    ("/thinking", "Thinking display/effort: /thinking [on|off|status|efforts|effort [MODEL] LEVEL]"),
    ("/verify", "Set/toggle claim-grounding verify mode: /verify [on|off|status]"),
    ("/keepalive", "Show or set model keep-alive"),
    ("/perf", "Show recent latency metrics"),
    ("/metrics", "Reset recent latency metrics"),
    ("/dashboard", "Open the runtime overview"),
    ("/status", "Show current model, context usage, and active features"),
    ("/identity", "Show identity file status"),
    ("/lesson", "Append a lesson to lessons-learned.md"),
    ("/lessons", "Show lesson index status or 'reindex'"),
    ("/skills", "Review quarantined skills; 'crystallize' / 'approve' / 'reject'"),
    ("/intuition", "Manage embedded memory recall"),
    ("/intelligence", "Inspect repository intelligence: status, query, reindex"),
    ("/intel", "Alias for /intelligence"),
    ("/intelagence", "Alias for /intelligence"),
    ("/kernel", "Kernel registry: /kernel list | show NAME | check [NAME]"),
    ("/kernel list", "List promoted kernel specs"),
    ("/kernel show", "Show a promoted kernel spec"),
    ("/kernel check", "Validate kernel imports, slash routes, and active action contracts"),
    ("/icl", "index-compute-lab graph status, on/off, or ask"),
    ("/code-rag", "Opt in or out of working-directory code retrieval: on, off, status"),
    ("/agent", "Run, delegate, resume, and inspect agent threads"),
    ("/agent team", "Fan out 2-4 read-only specialists, then integrate and verify"),
    ("/agent threads", "List persistent agent runs and child threads"),
    ("/agent switch", "Restore a persistent thread's recorded workspace"),
    ("/worktree", "Inspect or manage isolated Git workspaces"),
    ("/worktree list", "List Algo-managed Git worktrees"),
    ("/worktree new", "Create and activate an isolated Git worktree"),
    ("/worktree use", "Activate a managed Git worktree"),
    ("/worktree remove", "Remove a clean managed worktree while retaining its branch"),
    ("/ship", "Plan or run a guarded commit, push, and pull-request workflow"),
    ("/ship status", "Inspect structured publish readiness without changing state"),
    ("/ship commit", "Scrub, stage, and commit the active feature branch"),
    ("/ship push", "Scrub and push the clean active feature branch"),
    ("/ship pr", "Create or find a pull request; draft by default"),
    ("/ship all", "Commit, push, and open a draft pull request in one guarded flow"),
    ("/goal", "Work a task until complete: /goal [--rounds N] <task> | resume | status | clear"),
    ("/route", "Preview routing, Agent Blocks budget, and tool policy"),
    ("/memory-auto", "Inspect or toggle bounded automatic memory capture: /memory-auto [on|off|status]"),
    ("/remember", "Store a memory"),
    ("/memories", "List memories"),
    ("/forget", "Delete a memory"),
    ("/clear", "Clear conversation"),
    ("/diff", "Show last verified Git diff captured by an agent pipeline"),
    ("/changes", "Summarize the most recent agent pipeline's per-block activity"),
    ("/context", "Show or manage context compression"),
    ("/save", "Save conversation"),
    ("/load", "Load conversation"),
    ("/ctx", "Set context window"),
    ("/temp", "Set temperature"),
    ("/toolmax", "Set max tool iterations"),
    ("/thinkevery", "Set reflection interval"),
    ("/embed", "Generate embeddings"),
    ("/vision", "Describe an image"),
    ("/pdf", "Extract text from a PDF"),
    ("/theme", "Switch visual theme"),
    ("/info", "Show configuration"),
    ("/actions", "Browse commands, tools, and capabilities"),
    ("/doctor", "Show provider, dependency, ICL, and safety readiness"),
    ("/selfcheck", "Audit harness, slash/action wiring, kernels, and retrieval readiness"),
    ("/reload", "Reload configuration, tools, and harness state"),
    ("/harness", "Inspect harness index health"),
    ("/harness status", "Show harness index status and quality"),
    ("/harness refresh", "Refresh harness index"),
    ("/harness embed", "Embed pending harness records"),
    ("/harness score", "Run the ten-gate benchmark and algorithm-effectiveness scorecard"),
    ("/harness compare", "Recompute the external rating and strict leader gates"),
    ("/harness external", "Opt in or out of indexing other local agent stores"),
    ("/harness benchmark-embed", "Measure synthetic embed throughput (--count N --model NAME)"),
    ("/harness build-rust", "Build optional Rust indexer"),
    ("/harness rust", "Build optional Rust indexer"),
    ("/hsearch", "Search harness assets"),
    ("/hs", "Alias for /hsearch"),
    ("/hread", "Read a harness record"),
    ("/hr", "Alias for /hread"),
    ("/cd", "Change working directory"),
    ("/ls", "List directory (cwd-relative)"),
    ("/read", "Read a file (cwd-relative; LLM: session_slash tool)"),
    ("/plugins", "Inspect plugin manifests without executing plugin code"),
    ("/credentials", "List helpers or check a named helper key (value redacted)"),
    ("/url-scheme", "Parse an algo-cli:// deep link: /url-scheme <url> | help"),
    ("/mode", "Session mode: execute | explore | publish"),
    ("/exit", "Exit"),
    ("/quit", "Exit"),
]

SLASH_COMMAND_ALIASES: dict[str, str] = {
    "/hs": "/hsearch",
    "/hr": "/hread",
}


class SlashCommandCompleter(Completer):
    def __init__(self, commands: list[tuple[str, str]]):
        self.commands = commands

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        normalized = text.lower()
        for command, description in self.commands:
            if command.lower().startswith(normalized):
                yield Completion(
                    command,
                    start_position=-len(text),
                    display=command,
                    display_meta=description,
                )


_PATH_SLASH_COMMANDS = frozenset({"/cd", "/read", "/ls"})
_HARNESS_SUBCOMMANDS = (
    "status",
    "refresh",
    "embed",
    "score",
    "compare",
    "external",
    "build-rust",
    "benchmark-embed",
)
_HARNESS_USAGE = (
    "Usage: /harness [status|refresh|embed|score|compare|build-rust] or "
    "/harness external [on|off|status] or "
    "/harness benchmark-embed [--count N] [--model NAME]. "
    "Search with /hsearch (alias /hs); read with /hread (alias /hr)."
)


def slash_command_suggestions(command: str, *, limit: int = 3) -> list[str]:
    """Return close slash-command matches for a mistyped command token."""
    token = (command or "").strip().split(maxsplit=1)[0].lower()
    if not token.startswith("/"):
        return []
    commands = [name for name, _description in SLASH_COMMANDS if " " not in name]
    return get_close_matches(token, commands, n=max(1, limit), cutoff=0.6)


def unknown_command_message(raw: str) -> str:
    command = (raw or "").strip().split(maxsplit=1)[0] or "(empty)"
    suggestions = slash_command_suggestions(command, limit=1)
    suggestion = f" Did you mean {suggestions[0]}?" if suggestions else ""
    return (
        f"Unknown command: {command}.{suggestion} "
        "Use /help. If this command was added in a code update, exit and restart algo-cli."
    )


def harness_subcommand_error(arg: str) -> str:
    """Return an actionable error for an unknown ``/harness`` subcommand."""
    parts = (arg or "").strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else "(empty)"
    if len(parts) > 1 and subcommand in {
        "status",
        "stats",
        "quality",
        "refresh",
        "embed",
        "score",
        "scorecard",
        "compare",
        "competitive",
        "grade",
        "rating",
        "build-rust",
        "rust",
        "help",
        "?",
    }:
        return f"Unexpected arguments for /harness {subcommand}: {parts[1]}. {_HARNESS_USAGE}"
    suggestions = get_close_matches(subcommand, _HARNESS_SUBCOMMANDS, n=1, cutoff=0.6)
    suggestion = f" Did you mean {suggestions[0]}?" if suggestions else ""
    return f"Unknown /harness subcommand: {subcommand}.{suggestion} {_HARNESS_USAGE}"


_TOGGLE_ON = {"on", "true", "1", "yes", "enable", "enabled"}
_TOGGLE_OFF = {"off", "false", "0", "no", "disable", "disabled"}
_TOGGLE_STATUS = {"status", "show", "?"}
_REASON_MODE_ALIASES = {
    "neuro-symbolic": "neuro_symbolic",
    "neurosymbolic": "neuro_symbolic",
}
_REASON_MODE_GUIDE = [
    "react: default for normal tool-use loops, file inspection, small coding edits, and straightforward tasks.",
    "reflexion: after a failed, partial, or contradicted attempt when self-critique/retry discipline is needed.",
    "tot: Tree-of-Thought for ambiguous planning or architecture decisions with several independent paths.",
    "got: Graph-of-Thought for interconnected evidence/ideas that should merge or revise each other.",
    "mcts: Monte Carlo tree search for deeper exploration/exploitation across many possible action sequences.",
    "qcr: quantum-inspired aggregation for comparing/ranking multiple candidate solutions or reasoning fragments.",
    "neuro_symbolic: verification-heavy logic, math, code invariants, contracts, or claim checking.",
    "depth N / branches N: raise search cost only when deeper exploration is worth the latency.",
    "auto-reflexion / auto-verify: enable only when the user wants automated retry/verification behavior.",
    "Do not change /reason for routine reads or simple edits; still verify facts with tools.",
]


def _parse_toggle_arg(arg: str, current: bool) -> tuple[bool, bool] | None:
    """Return (new_value, should_change). Empty arg means toggle; status means report only."""
    value = (arg or "").strip().lower()
    if not value:
        return (not current, True)
    if value in _TOGGLE_ON:
        return (True, True)
    if value in _TOGGLE_OFF:
        return (False, True)
    if value in _TOGGLE_STATUS:
        return (current, False)
    return None


def handle_command(raw: str, cfg: Config, client: Client, session: Any = None) -> tuple[bool, Client]:
    from . import main as m

    def refresh_after_model_change(updated_client: Client) -> None:
        m.refresh_runtime_status(cfg, updated_client, force=True)
        m.invalidate_prompt_toolbar(session)

    stripped = raw.strip()
    parts = stripped.split(maxsplit=1)
    entered_command = parts[0].lower()
    command = SLASH_COMMAND_ALIASES.get(entered_command, entered_command)
    arg = parts[1] if len(parts) > 1 else ""
    # Use the original command (parts[0]) to compute arg offset
    # so that /CD, /Cd, /cD all work the same as /cd
    if command in _PATH_SLASH_COMMANDS:
        arg = stripped[len(parts[0]):].strip()

    if command in {"/exit", "/quit"}:
        raise EOFError
    if command == "/help":
        display.show_help()
    elif command == "/model":
        if arg:
            if m.chatgpt_client.is_codex_subscription_model(arg):
                arg = m.chatgpt_client.normalize_codex_model(arg)
            cfg.model = arg
            # Direct model selection must not inherit a stale provider route.
            # A cloud-suffixed Ollama model selects direct cloud; xAI and
            # ChatGPT models route through their dedicated clients.
            cfg.cloud = m.is_cloud_model_name(arg)
            cfg.save()
            m.show_info(f"Model set to {cfg.model}")
            client = m.create_client(cfg)
            refresh_after_model_change(client)
        else:
            if m.model_picker(cfg):
                client = m.create_client(cfg)
                refresh_after_model_change(client)
    elif command == "/models":
        if m.model_picker(cfg):
            client = m.create_client(cfg)
            refresh_after_model_change(client)
    elif command == "/host":
        if arg:
            cfg.host = arg
            cfg.cloud = False
            cfg.save()
            m.start_ollama_server(cfg)
            client = m.create_client(cfg)
            m.show_info(f"Local host set to {cfg.host}")
        else:
            m.show_info(f"Host: {cfg.host}")
    elif command == "/cloud":
        parsed = _parse_toggle_arg(arg, bool(cfg.cloud))
        if parsed is None:
            m.show_error("Usage: /cloud [on|off|status]")
            return True, client
        new_value, should_change = parsed
        m.load_runtime_env(override=True)
        has_api_key = bool(os.environ.get("OLLAMA_API_KEY", "").strip())
        if should_change and new_value and not has_api_key:
            if cfg.cloud:
                cfg.cloud = False
                cfg.save()
            m.start_ollama_server(cfg)
            client = m.create_client(cfg)
            m.show_info(
                "Ollama Cloud via local Ollama login: using local Ollama at "
                f"{cfg.host}. Direct Cloud API mode requires OLLAMA_API_KEY."
            )
            if m.is_cloud_model_name(cfg.model):
                m.show_info(f"Current cloud model {cfg.model} will route through local Ollama login.")
            else:
                m.show_info("Run /login if needed, then select a :cloud model with /model or /models.")
            return True, client
        changed = should_change and (bool(cfg.cloud) != new_value)
        if should_change:
            cfg.cloud = new_value
            cfg.save()
            client = m.create_client(cfg)
        if cfg.cloud:
            m.show_info("Ollama Cloud direct API mode: ON")
        elif m.is_cloud_model_name(cfg.model):
            m.show_info(f"Ollama Cloud via local Ollama login: ON for {cfg.model}")
        else:
            m.show_info("Ollama Cloud direct API mode: OFF. Local Ollama login can still run :cloud models.")
        if should_change:
            if cfg.cloud:
                m.start_supplemental_gateway(cfg)
                if cfg.auto_cloud_connect:
                    m.maybe_prompt_cloud_login()
                else:
                    m.show_info("Direct Cloud API mode is on. OLLAMA_API_KEY is used for chat/web access.")
            elif changed:
                m.start_ollama_server(cfg)
    elif command == "/cloudauto":
        parsed = _parse_toggle_arg(arg, bool(cfg.auto_cloud_connect))
        if parsed is None:
            m.show_error("Usage: /cloudauto [on|off|status]")
            return True, client
        new_value, should_change = parsed
        if should_change:
            cfg.auto_cloud_connect = new_value
            cfg.save()
        m.show_info(f"Cloud auto-connect prompt: {'ON' if cfg.auto_cloud_connect else 'OFF'}")
    elif command == "/login":
        m.run_ollama_login()
        m.load_runtime_env(override=True)
        client = m.create_client(cfg)
    elif command == "/xai-login":
        m.run_xai_login(arg)
    elif command == "/xai-logout":
        m.run_xai_logout()
    elif command == "/xai-status":
        m.run_xai_status()
    elif command == "/xai-test":
        m.run_xai_test()
    elif command == "/google-login":
        m.run_google_login(arg)
    elif command == "/google-callback":
        m.run_google_callback(arg)
    elif command == "/google-logout":
        m.run_google_logout()
    elif command == "/google-status":
        m.run_google_status()
    elif command == "/google":
        m.run_google(arg)
    elif command == "/chatgpt-login":
        m.run_chatgpt_login(arg)
    elif command == "/chatgpt-logout":
        m.run_chatgpt_logout()
    elif command == "/chatgpt-status":
        m.run_chatgpt_status()
    elif command == "/model-check":
        m.run_model_check(arg, active_model=cfg.model)
    elif command == "/x-account":
        m.run_x_account(arg)
    elif command == "/system":
        if arg:
            cfg.system = arg
            cfg.save()
            m.show_info("System prompt updated.")
        else:
            m.console.print(cfg.system)
    elif command == "/auto":
        parsed = _parse_toggle_arg(arg, bool(cfg.auto_mode))
        if parsed is None:
            m.show_error("Usage: /auto [on|off|status]")
            return True, client
        new_value, should_change = parsed
        if should_change:
            cfg.auto_mode = new_value
            cfg.save()
        m.show_info(f"Auto-approve: {'ON' if cfg.auto_mode else 'OFF'}")
    elif command == "/safe":
        parsed = _parse_toggle_arg(arg, bool(cfg.safe_mode))
        if parsed is None:
            m.show_error("Usage: /safe [on|off|status]")
            return True, client
        new_value, should_change = parsed
        if should_change:
            cfg.safe_mode = new_value
            cfg.save()
        m.show_info(f"Safe mode: {'ON' if cfg.safe_mode else 'OFF'}")
    elif command == "/policy":
        sub = arg.strip().lower() or "status"
        if sub in {"on", "off"}:
            cfg.algorithmic_tool_policy_enabled = sub == "on"
            cfg.save()
        elif sub != "status":
            m.show_error("Usage: /policy [on|off|status]")
            return True, client
        state = "ON" if cfg.algorithmic_tool_policy_enabled else "OFF"
        detail = "enforced for Agent Blocks" if cfg.algorithmic_tool_policy_enabled else "preview only"
        m.show_info(f"Algorithmic tool policy: {state} ({detail}).")
    elif command == "/thinking":
        thinking_args = arg.strip().split()
        sub = thinking_args[0].lower() if thinking_args else "status"
        if sub in {"efforts", "models"}:
            for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
                effort = m.chatgpt_client.reasoning_effort_for_model(model, cfg.chatgpt_reasoning_efforts)
                supported = ", ".join(m.chatgpt_client.supported_reasoning_efforts(model))
                m.show_info(f"{model}: {effort} (supports {supported})")
            m.show_info("Ultra is multi-agent orchestration, not an effort level; use /agent team for that workflow.")
        elif sub == "effort":
            if len(thinking_args) == 1:
                if not m.chatgpt_client.is_codex_subscription_model(cfg.model):
                    m.show_error("The active model is not a ChatGPT/Codex reasoning model.")
                    return True, client
                effort = m.chatgpt_client.reasoning_effort_for_model(
                    cfg.model, cfg.chatgpt_reasoning_efforts
                )
                m.show_info(f"Reasoning effort for {cfg.model}: {effort}")
                return True, client
            if len(thinking_args) == 2:
                target_model, raw_effort = cfg.model, thinking_args[1]
            elif len(thinking_args) == 3:
                target_model, raw_effort = thinking_args[1], thinking_args[2]
            else:
                m.show_error("Usage: /thinking effort [MODEL] LEVEL")
                return True, client
            target_model = m.chatgpt_client.normalize_codex_model(target_model)
            if not m.chatgpt_client.is_codex_subscription_model(target_model):
                m.show_error(f"{target_model} is not a ChatGPT/Codex reasoning model.")
                return True, client
            try:
                effort = m.chatgpt_client.parse_reasoning_effort(raw_effort, target_model)
            except ValueError as exc:
                m.show_error(str(exc))
                return True, client
            cfg.chatgpt_reasoning_efforts[target_model] = effort
            cfg.save()
            m.show_info(f"Reasoning effort for {target_model}: {effort}")
        else:
            parsed = _parse_toggle_arg(arg, bool(cfg.show_thinking))
            if parsed is None:
                m.show_error(
                    "Usage: /thinking [on|off|status|efforts|effort [MODEL] LEVEL]"
                )
                return True, client
            new_value, should_change = parsed
            if should_change:
                cfg.show_thinking = new_value
                cfg.save()
            m.show_info(f"Thinking display: {'ON' if cfg.show_thinking else 'OFF'}")
            if m.chatgpt_client.is_codex_subscription_model(cfg.model):
                effort = m.chatgpt_client.reasoning_effort_for_model(
                    cfg.model, cfg.chatgpt_reasoning_efforts
                )
                m.show_info(f"Reasoning effort for {cfg.model}: {effort}")
    elif command == "/verify":
        parsed = _parse_toggle_arg(arg, bool(cfg.verify_mode))
        if parsed is None:
            m.show_error("Usage: /verify [on|off|status]")
            return True, client
        new_value, should_change = parsed
        if should_change:
            cfg.verify_mode = new_value
            cfg.save()
        m.show_info(
            f"Verify mode: {'ON' if cfg.verify_mode else 'OFF'}  "
            f"({'claim-grounding pipeline active after each response' if cfg.verify_mode else 'disabled'})"
        )
    elif command == "/keepalive":
        if arg:
            cfg.keep_alive = arg
            cfg.save()
            m.show_info(f"Keep-alive set to {cfg.keep_alive}")
        else:
            m.show_info(f"Keep-alive: {cfg.keep_alive}")
    elif command == "/perf":
        perf_telemetry.show_perf_summary()
    elif command == "/metrics":
        if arg.strip().lower() != "reset":
            m.show_error("Usage: /metrics reset")
        else:
            for key in [item for item in m.RUNTIME_STATUS if item == "last_metrics" or item.startswith("last_") and item.endswith("_metrics")]:
                m.RUNTIME_STATUS.pop(key, None)
            try:
                perf_telemetry.PERF_HISTORY_FILE.unlink(missing_ok=True)
            except OSError as exc:
                m.show_error(f"Could not reset performance history: {exc}")
            else:
                m.show_info("Performance metrics reset.")
    elif command == "/dashboard":
        installed_models, running_models, event_lines = m.collect_dashboard_state(client, cfg)
        used, total, _remaining, _runtime_cap, _native = m.context_status(cfg, client=client)
        m.show_session_overview(
            model=cfg.model,
            host=m.effective_runtime_host(cfg),
            cwd=cfg.cwd,
            theme_name=cfg.theme,
            cloud=cfg.cloud,
            provider_mode=m.runtime_mode_label(cfg),
            auto_mode=cfg.auto_approve_active,
            safe_mode=cfg.safe_mode,
            temperature=cfg.temperature,
            used_tokens=used,
            total_tokens=total,
            summary_active=bool(cfg.session_summary.strip()),
            tool_think_every=max(1, int(cfg.tool_think_every)),
            max_tool_iterations=max(1, int(cfg.max_tool_iterations)),
            memory_count=len(cfg.memories),
            system_prompt=cfg.system,
            messages=cfg.messages,
            installed_models=installed_models,
            running_models=running_models,
            event_lines=event_lines,
        )
    elif command == "/status":
        m.handle_status_command(cfg, client)
    elif command == "/identity":
        rows = identity.status()
        m.console.print(f"[muted]Identity directory:[/] {identity.IDENTITY_DIR}")
        for row in rows:
            if row["exists"]:
                m.console.print(
                    f"  [primary]{row['name']:<20}[/] "
                    f"[muted]{row['size']:>6} B[/]  "
                    f"[muted]{row['modified']}[/]"
                )
            else:
                m.console.print(f"  [muted]{row['name']:<20}  missing[/]")
    elif command == "/lesson":
        if not arg:
            m.show_error("Usage: /lesson <text>")
        else:
            path = identity.append_lesson(arg)
            m.capture_intuition_block(cfg, "lesson", arg, source="/lesson")
            m.show_info(f"Lesson saved to {path}")
    elif command == "/lessons":
        lessons_sub = arg.strip().lower()
        if lessons_sub == "reindex":
            backend, _reason = m.resolve_embed_backend(cfg)
            if not m.host_is_local(cfg.host) or not m.ollama_server_ready(cfg.host):
                m.show_error("Local Ollama is not reachable; cannot rebuild lesson embeddings.")
            else:
                embed_fn, _backend, active_model = m.make_embed_fn(cfg, harness.resolve_embed_model(cfg))
                m.show_info(f"Rebuilding lesson embeddings with {active_model} ({backend})…")
                result = identity.rebuild_lessons_index(
                    embed_fn,
                    active_model,
                    expected_dimensions=m.configured_embed_dimensions(cfg),
                )
                if result.get("ready"):
                    m.show_info(f"Indexed {result.get('chunk_count', 0)} lesson chunks.")
                else:
                    m.show_error(f"Reindex failed: {result.get('reason', 'unknown')}")
        elif lessons_sub in {"", "status", "show", "?"}:
            status = identity.lessons_index_status()
            m.console.print(f"[muted]Lessons file:[/] {identity.LESSONS_PATH}")
            m.console.print(f"  file present : [text]{status['file']}[/]")
            m.console.print(f"  index built  : [text]{status['index']}[/]")
            m.console.print(f"  chunks       : [text]{status['chunk_count']}[/]")
            m.console.print(f"  model        : [text]{status['model'] or '-'}[/]")
            m.console.print(f"  stale        : [{'warning' if status['stale'] else 'text'}]{status['stale']}[/]")
            m.console.print("[muted]Use /lessons reindex to rebuild.[/]")
        else:
            m.show_error("Usage: /lessons [status|reindex]")
    elif command == "/skills":
        skill_parts = arg.strip().split(maxsplit=1)
        sub = skill_parts[0].lower() if skill_parts else ""
        skill_name = skill_parts[1].strip() if len(skill_parts) > 1 else ""
        if sub == "crystallize":
            llm_fn = m.make_local_maintenance_llm_fn(cfg)
            if llm_fn is None:
                m.show_error(
                    "Skill crystallization requires a reachable local Ollama host with a "
                    "non-embedding model; cloud fallback is disabled."
                )
            else:
                m.show_info("Reviewing recent runs for skill discoveries…")
                result = skills.crystallize(llm_fn)
                quarantined = result.get("quarantined", [])
                if quarantined:
                    m.show_info(
                        f"Quarantined {len(quarantined)} skill candidate(s): "
                        f"{', '.join(quarantined)}. Review and promote with /skills approve NAME."
                    )
                else:
                    m.show_info(f"No skill candidates quarantined ({result.get('reason', 'nothing qualified')}).")
        elif sub in {"approve", "reject"}:
            if not skill_name:
                m.show_error(f"Usage: /skills {sub} NAME")
            else:
                try:
                    if sub == "approve":
                        promoted = skills.promote_quarantined_skill(skill_name)
                        m.show_info(
                            f"Promoted skill {promoted.stem}. Run /harness refresh to index it."
                        )
                    else:
                        rejected = skills.reject_quarantined_skill(skill_name)
                        m.show_info(f"Rejected skill candidate {rejected.stem}.")
                except (FileNotFoundError, FileExistsError, ValueError, OSError) as exc:
                    m.show_error(str(exc))
        elif sub in {"on", "off"}:
            cfg.skill_crystallize_enabled = sub == "on"
            cfg.save()
            m.show_info(
                "Skill run-history capture and automatic crystallization: "
                f"{'ON' if cfg.skill_crystallize_enabled else 'OFF'}"
            )
            if cfg.skill_crystallize_enabled:
                m.show_info(
                    "Bounded run summaries are stored under ~/.algo_cli/private and are "
                    "processed only by a local non-embedding Ollama model."
                )
        elif sub in {"", "status", "show", "?"}:
            status = skills.skills_status()
            m.console.print(f"[muted]Skills directory:[/] {status['skills_dir']}")
            m.console.print(f"  crystallized   : [text]{status['skill_count']}[/]")
            m.console.print(f"  quarantined    : [text]{len(status['quarantined'])}[/]")
            m.console.print(f"  recorded runs  : [text]{status['run_count']}[/]")
            m.console.print(f"  auto-crystallize: [text]{'on' if cfg.skill_crystallize_enabled else 'off'}[/] (every {max(1, int(cfg.skill_crystallize_every))} runs, {cfg.runs_since_crystallize} since last)")
            if status["skills"]:
                for name in status["skills"][:20]:
                    m.console.print(f"  [primary]{name}[/]")
            if status["quarantined"]:
                m.console.print("[muted]Pending explicit review:[/]")
                for name in status["quarantined"][:20]:
                    m.console.print(f"  [warning]{name}[/]")
            m.console.print(
                "[muted]Use /skills crystallize to propose, /skills approve|reject NAME to review, "
                "or /skills on|off to toggle.[/]"
            )
        else:
            m.show_error("Usage: /skills [status|crystallize|approve NAME|reject NAME|on|off]")
    elif command == "/icl":
        m.handle_icl_command(arg, cfg)
    elif command == "/code-rag":
        sub = (arg or "status").strip().lower() or "status"
        if sub == "on":
            cfg.code_rag_enabled = True
            cfg.code_rag_consent_version = CODE_RAG_CONSENT_VERSION
            cfg.save()
            m.show_info("Working-directory code retrieval: ON")
            m.show_info(
                "Source snippets from the active cwd may be included in model prompts, "
                "including requests sent to cloud providers."
            )
        elif sub == "off":
            cfg.code_rag_enabled = False
            cfg.code_rag_consent_version = 0
            cfg.save()
            try:
                removed = code_rag.purge_persisted_indexes()
            except OSError as exc:
                m.show_error(f"Code retrieval disabled, but persisted index purge failed: {exc}")
            else:
                m.show_info(
                    "Working-directory code retrieval: OFF · "
                    f"purged {removed} persisted index file(s)"
                )
        elif sub in {"status", "show", "?"}:
            enabled = code_rag_consent_granted(cfg)
            m.console.print(
                "Working-directory code retrieval: "
                f"{'on' if enabled else 'off'} · "
                f"persisted indexes {code_rag.persisted_index_count()}"
            )
            m.console.print(
                "[muted]When on, retrieved cwd snippets join the active model request and "
                "may leave this machine with a cloud provider.[/]"
            )
        else:
            m.show_error("Usage: /code-rag [on|off|status]")
    elif command == "/intuition":
        m.handle_intuition_command(arg, cfg)
    elif command in {"/intelligence", "/intel", "/intelagence"}:
        m.handle_intelligence_command(arg, cfg)
    elif command == "/kernel":
        m.handle_kernel_command(arg)
    elif command == "/agent":
        m.execute_agent_command(arg, cfg, client)
    elif command == "/worktree":
        from . import worktree_runtime

        try:
            worktree_text = worktree_runtime.handle_command(arg, cfg)
        except worktree_runtime.WorktreeError as exc:
            m.show_error(str(exc))
        else:
            m.console.print(worktree_text)
    elif command == "/ship":
        from . import git_publish

        try:
            ship_text = git_publish.handle_command(arg, cfg)
        except git_publish.PublishError as exc:
            m.show_error(str(exc))
        else:
            m.console.print(ship_text)
    elif command == "/goal":
        m.run_goal_loop(client, cfg, arg)
    elif command == "/route":
        if not arg.strip():
            m.show_error("Usage: /route <task>")
        else:
            m.show_task_route(m.task_router.route_task(arg), cfg, prompt=arg)
    elif command == "/memory-auto":
        sub = arg.strip().lower()
        if sub in {"on", "off"}:
            cfg.memory_auto_capture_enabled = sub == "on"
            cfg.save()
            m.show_info(
                f"Automatic memory capture: {'ON' if cfg.memory_auto_capture_enabled else 'OFF'}"
            )
        elif sub in {"", "status", "show", "?"}:
            daily_limit = min(
                memory_candidates.MAX_DAILY_WRITES,
                max(0, int(cfg.memory_auto_daily_limit)),
            )
            entry_limit = min(
                memory_candidates.MAX_AUTO_FINGERPRINTS,
                max(0, int(cfg.memory_auto_entry_limit)),
            )
            char_limit = min(
                memory_candidates.MAX_MEMORY_CHARS,
                max(0, int(cfg.memory_auto_char_limit)),
            )
            m.show_info(
                "Automatic memory capture: "
                f"{'ON' if cfg.memory_auto_capture_enabled else 'OFF'} · "
                f"daily limit {daily_limit} · "
                f"fingerprint cap {entry_limit} · "
                f"memory budget {char_limit} chars"
            )
        else:
            m.show_error("Usage: /memory-auto [on|off|status]")
    elif command == "/remember":
        if not arg:
            m.show_error("Usage: /remember <fact>")
        else:
            cfg.remember_fact(arg)
            m.capture_intuition_block(cfg, "memory", arg, source="/remember")
            m.show_info("Memory saved.")
    elif command == "/memories":
        display.show_memory(cfg.memories)
    elif command == "/forget":
        try:
            display_index = int(arg)
            if display_index < 1:
                raise ValueError
            removed = cfg.forget_memory_index(display_index - 1)
            m.show_info(f"Forgot: {removed}")
        except (ValueError, IndexError):
            m.show_error("Usage: /forget <number>")
    elif command == "/clear":
        cfg.messages.clear()
        cfg.session_summary = ""
        cfg.attempt_ledger.clear()
        reflex.reset_reflex_cycles(cfg)
        m.clear_session_pipeline_blocks()
        cfg.save()
        m.show_info("Conversation cleared.")
    elif command == "/mode":
        from . import session_mode

        sub = arg.strip().lower() or "status"
        if sub in session_mode.VALID_MODES:
            previous = cfg.session_mode
            cfg.session_mode = sub
            for note in session_mode.apply_mode_side_effects(cfg, sub, previous=previous):
                m.show_info(note)
            cfg.save()
            m.show_info(session_mode.status_line(cfg))
        elif sub == "status":
            m.console.print(session_mode.describe(cfg))
        else:
            m.show_error("Usage: /mode [execute|explore|publish|status]")
    elif command == "/plugins":
        from .plugins import discover_plugins, plugin_status
        sub = arg.strip().lower() or "list"
        if sub in ("list", ""):
            discovered = discover_plugins()
            if not discovered:
                m.show_info("No plugins discovered in ~/.algo_cli/plugins/")
            else:
                from rich.table import Table as _Table
                table = _Table(title="Discovered Plugins")
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
                m.console.print(table)
        elif sub == "status":
            statuses = plugin_status()
            if not statuses:
                m.show_info("No plugins loaded.")
            else:
                from rich.table import Table as _Table
                table = _Table(title="Plugin Status")
                table.add_column("Name", style="cyan")
                table.add_column("Loaded", style="green")
                table.add_column("Error", style="red")
                for s in statuses:
                    table.add_row(
                        s.get("name", "?"),
                        "yes" if s.get("loaded") else "no",
                        s.get("error", "") or "",
                    )
                m.console.print(table)
        else:
            m.show_error("Usage: /plugins [list|status]")
    elif command == "/credentials":
        from .credential_helpers import list_helpers, get_helper, get_credential
        raw_sub = arg.strip()
        sub = raw_sub.lower() or "list"
        if sub in ("list", ""):
            helpers = sorted(list_helpers())
            if not helpers:
                m.show_info("No credential helpers registered.")
            else:
                from rich.table import Table as _Table
                table = _Table(title="Credential Helpers")
                table.add_column("Name", style="cyan")
                table.add_column("Description")
                for name in helpers:
                    h = get_helper(name)
                    table.add_row(name, h.description if h else "?")
                m.console.print(table)
        elif sub.startswith("get "):
            parts = raw_sub.split(maxsplit=2)
            if len(parts) != 3:
                m.show_error("Usage: /credentials get <helper> <key>")
                return True, client
            _verb, helper, key = parts
            val = get_credential(helper, key)
            if val is None:
                m.show_info(f"No credential found for '{key}' in helper '{helper}'")
            else:
                m.show_info(f"{helper}/{key}: configured (value redacted)")
        else:
            m.show_error("Usage: /credentials [list|get <helper> <key>]")
    elif command == "/url-scheme":
        from .url_scheme import handle_deep_link, format_help
        url = arg.strip()
        if not url or url.lower() == "help":
            m.console.print(format_help())
            return True, client
        deep_link_result = handle_deep_link(url)
        if not deep_link_result.get("valid"):
            m.show_error(f"Invalid URL: {deep_link_result.get('error', 'unknown error')}")
            return True, client
        m.show_info(f"Action: {deep_link_result.get('action', '?')}")
        if deep_link_result.get("target"):
            m.show_info(f"Target: {deep_link_result['target']}")
        if deep_link_result.get("query"):
            m.show_info(f"Query: {deep_link_result['query']}")
    elif command == "/reflex":
        sub = arg.strip().lower() or "status"
        if sub in {"on", "off"}:
            cfg.reflex_enabled = sub == "on"
            cfg.save()
            m.show_info(f"Reflex loop v0.1: {'ON' if cfg.reflex_enabled else 'OFF'}")
        elif sub == "reset":
            reflex.reset_reflex_cycles(cfg)
            cfg.save()
            m.show_info("Reflex cycle counter reset for this session.")
        elif sub in {"status", "show", "?"}:
            m.show_info(reflex.status_line(cfg))
        else:
            m.show_error("Usage: /reflex [on|off|status|reset]")
    elif command == "/reason":
        sub, _, rest = (arg or "status").strip().partition(" ")
        sub = _REASON_MODE_ALIASES.get(sub.lower(), sub.lower()) or "status"
        if sub == "status":
            mode = getattr(cfg, "reasoning_mode", "react")
            depth = getattr(cfg, "reasoning_depth", 4)
            branches = getattr(cfg, "reasoning_branches", 3)
            auto_ref = getattr(cfg, "reasoning_auto_reflexion", False)
            auto_ver = getattr(cfg, "reasoning_auto_verify", False)
            chat_on = getattr(cfg, "reasoning_chat_enabled", False)
            m.show_info(
                f"Reasoning engine: mode={mode} depth={depth} branches={branches}"
                f"  chat-preflight={'on' if chat_on else 'off'}"
                f"  auto-reflexion={auto_ref} auto-verify={auto_ver}. "
                "Use /reason guide for when-to-use guidance."
            )
        elif sub in {"guide", "help"}:
            m.show_info("Reasoning mode guide:")
            for line in _REASON_MODE_GUIDE:
                m.show_info(f"- {line}")
        elif sub in {"react", "reflexion", "tot", "got", "mcts", "qcr", "neuro_symbolic", "hybrid"}:
            cfg.reasoning_mode = sub
            cfg.save()
            m.show_info(f"Reasoning mode set to: {sub}")
        elif sub == "depth":
            try:
                requested_depth = int(rest.strip())
                if not 1 <= requested_depth <= 16:
                    raise ValueError
                cfg.reasoning_depth = requested_depth
                cfg.save()
                m.show_info(f"Reasoning depth set to: {cfg.reasoning_depth}")
            except (ValueError, TypeError):
                m.show_error("Usage: /reason depth <1-16>")
        elif sub == "branches":
            try:
                requested_branches = int(rest.strip())
                if not 1 <= requested_branches <= 8:
                    raise ValueError
                cfg.reasoning_branches = requested_branches
                cfg.save()
                m.show_info(f"Reasoning branches set to: {cfg.reasoning_branches}")
            except (ValueError, TypeError):
                m.show_error("Usage: /reason branches <1-8>")
        elif sub == "auto-reflexion":
            value = rest.strip().lower()
            if value not in _TOGGLE_ON | _TOGGLE_OFF | _TOGGLE_STATUS:
                m.show_error("Usage: /reason auto-reflexion [on|off|status]")
                return True, client
            toggle = cfg.reasoning_auto_reflexion if value in _TOGGLE_STATUS else value in _TOGGLE_ON
            if value not in _TOGGLE_STATUS:
                cfg.reasoning_auto_reflexion = toggle
                cfg.save()
            m.show_info(f'Auto-reflexion on failed blocks: {"ON" if toggle else "OFF"}')
        elif sub == "auto-verify":
            value = rest.strip().lower()
            if value not in _TOGGLE_ON | _TOGGLE_OFF | _TOGGLE_STATUS:
                m.show_error("Usage: /reason auto-verify [on|off|status]")
                return True, client
            toggle = cfg.reasoning_auto_verify if value in _TOGGLE_STATUS else value in _TOGGLE_ON
            if value not in _TOGGLE_STATUS:
                cfg.reasoning_auto_verify = toggle
                cfg.save()
            m.show_info(f'Auto-verify implement blocks: {"ON" if toggle else "OFF"}')
        elif sub == "chat":
            value = rest.strip().lower()
            if value not in _TOGGLE_ON | _TOGGLE_OFF | _TOGGLE_STATUS:
                m.show_error("Usage: /reason chat [on|off|status]")
                return True, client
            toggle = cfg.reasoning_chat_enabled if value in _TOGGLE_STATUS else value in _TOGGLE_ON
            if value not in _TOGGLE_STATUS:
                cfg.reasoning_chat_enabled = toggle
                cfg.save()
            m.show_info(
                f'Reasoning chat preflight: {"ON" if toggle else "OFF"}'
                + (f" (mode={cfg.reasoning_mode}; multi-call, runs each turn)" if toggle else "")
            )
        else:
            m.show_info(
                "Usage: /reason [status|guide|react|reflexion|tot|got|mcts|qcr|neuro_symbolic|neuro-symbolic|hybrid|"
                "depth N|branches N|chat on|off|auto-reflexion on|off|auto-verify on|off]"
            )
    elif command == "/diff":
        m.handle_diff_command()
    elif command == "/changes":
        m.handle_changes_command()
    elif command == "/context":
        m.handle_context_command(arg, cfg, client)
    elif command == "/save":
        if not arg:
            m.show_error("Usage: /save <name>")
        else:
            try:
                safe_name = safe_conversation_name(arg)
                if safe_name != arg.strip():
                    m.show_info(f"Using canonical save name: {safe_name}")
                path = cfg.save_conversation(safe_name)
            except ValueError as exc:
                m.show_error(str(exc))
            else:
                m.show_info(f"Saved conversation to {path}")
    elif command == "/load":
        if not arg:
            m.show_error("Usage: /load <name>")
        else:
            try:
                safe_name = safe_conversation_name(arg)
                if safe_name != arg.strip():
                    m.show_info(f"Using canonical load name: {safe_name}")
                count = cfg.load_conversation(safe_name)
                m.show_info(f"Loaded {count} messages.")
            except Exception as exc:
                m.show_error(str(exc))
    elif command == "/ctx":
        if arg:
            try:
                requested_context = int(arg)
                if not 256 <= requested_context <= 2_000_000:
                    raise ValueError
                cfg.num_ctx = requested_context
                cfg.save()
            except ValueError:
                m.show_error("Usage: /ctx <number from 256 to 2000000>")
        m.show_info(f"Context: {cfg.num_ctx}")
    elif command == "/temp":
        if arg:
            try:
                import math

                requested_temperature = float(arg)
                if not math.isfinite(requested_temperature) or not 0.0 <= requested_temperature <= 2.0:
                    raise ValueError
                cfg.temperature = requested_temperature
                cfg.save()
            except ValueError:
                m.show_error("Usage: /temp <number from 0.0 to 2.0>")
        m.show_info(f"Temperature: {cfg.temperature}")
    elif command == "/toolmax":
        if arg:
            try:
                requested_tool_limit = int(arg)
                if not 1 <= requested_tool_limit <= 128:
                    raise ValueError
                cfg.max_tool_iterations = requested_tool_limit
                cfg.save()
            except ValueError:
                m.show_error("Usage: /toolmax <1-128>")
        m.show_info(f"Max tool iterations: {cfg.max_tool_iterations}")
    elif command == "/thinkevery":
        if arg:
            try:
                requested_reflection_interval = int(arg)
                if not 1 <= requested_reflection_interval <= 128:
                    raise ValueError
                cfg.tool_think_every = requested_reflection_interval
                cfg.save()
            except ValueError:
                m.show_error("Usage: /thinkevery <1-128>")
        m.show_info(f"Tool-call reflection interval: {cfg.tool_think_every}")
    elif command == "/embed":
        m.handle_embed_command(arg, cfg, client)
    elif command == "/vision":
        m.handle_vision_command(arg, cfg, client)
    elif command == "/pdf":
        m.handle_pdf_command(arg, cfg)
    elif command == "/theme":
        if not arg:
            m.show_info(f"Current theme: {cfg.theme}")
            m.console.print("Available themes: " + ", ".join(display.available_themes()))
        else:
            try:
                cfg.theme = m.set_theme(arg)
                cfg.save()
                if session is not None:
                    try:
                        session.style = m.build_prompt_style(m.theme_colors(cfg.theme))
                    except Exception:
                        pass
                    m.invalidate_prompt_toolbar(session)
                m.show_info(f"Theme set to {cfg.theme}")
            except ValueError as exc:
                m.show_error(f"{exc}. Available themes: {', '.join(display.available_themes())}")
    elif command == "/cd":
        from .workspace_resolver import parse_path_arg

        if not arg:
            m.show_info(f"cwd: {cfg.cwd}")
        else:
            from .workspace_resolver import parse_path_arg

            target = parse_path_arg(arg)
            path = Path(target).expanduser()
            if not path.is_absolute():
                path = Path(cfg.cwd) / path
            if path.exists() and path.is_dir():
                cfg.cwd = str(path.resolve())
                cfg.save()
                m.show_info(f"cwd: {cfg.cwd}")
            else:
                m.show_error(f"Not a directory: {path}")
    elif command == "/ls":
        from . import tools as tools_module
        from .workspace_resolver import parse_path_arg

        rel = parse_path_arg(arg) or "."
        directory_listing = tools_module.list_directory(rel, cwd=cfg.cwd, limit=40)
        m.console.print(directory_listing)
    elif command == "/read":
        from . import tools as tools_module
        from .workspace_resolver import parse_path_arg

        rel = parse_path_arg(arg)
        if not rel:
            m.show_error("Usage: /read <path>")
        else:
            file_contents = tools_module.read_file(rel, cwd=cfg.cwd)
            m.console.print(file_contents)
    elif command == "/info":
        if arg.strip().lower() == "json":
            m.console.print(
                json.dumps(
                    {
                        "model": cfg.model,
                        "host": cfg.host,
                        "cloud": cfg.cloud,
                        "cwd": cfg.cwd,
                        "context": cfg.num_ctx,
                        "temperature": cfg.temperature,
                        "theme": cfg.theme,
                        "auto_mode": cfg.auto_mode,
                        "auto_cloud_connect": cfg.auto_cloud_connect,
                        "safe_mode": cfg.safe_mode,
                        "algorithmic_tool_policy_enabled": cfg.algorithmic_tool_policy_enabled,
                        "thinking": cfg.show_thinking,
                        "keep_alive": cfg.keep_alive,
                        "tool_think_every": cfg.tool_think_every,
                        "messages": len(cfg.messages),
                        "memories": len(cfg.memories),
                        "attempts": len(cfg.attempt_ledger),
                        "last_metrics": m.RUNTIME_STATUS.get("last_metrics", {}),
                    },
                    indent=2,
                )
            )
        else:
            info_used, info_total, *_rest = m.context_status(cfg, client=client)
            m.show_session_overview(
                model=cfg.model,
                host=m.effective_runtime_host(cfg),
                cwd=cfg.cwd,
                theme_name=cfg.theme,
                cloud=cfg.cloud,
                provider_mode=m.runtime_mode_label(cfg),
                auto_mode=cfg.auto_approve_active,
                safe_mode=cfg.safe_mode,
                temperature=cfg.temperature,
                used_tokens=info_used,
                total_tokens=info_total,
                summary_active=bool(cfg.session_summary.strip()),
                tool_think_every=max(1, int(cfg.tool_think_every)),
                max_tool_iterations=max(1, int(cfg.max_tool_iterations)),
                memory_count=len(cfg.memories),
                system_prompt=cfg.system,
                messages=cfg.messages,
            )
    elif command == "/harness":
        from .tools import (
            harness_competitive_rating,
            harness_refresh,
            harness_scorecard,
            harness_stats,
        )

        harness_arg = arg.strip()
        harness_parts = harness_arg.split(maxsplit=1)
        subcommand = harness_parts[0].lower() if harness_parts else ""
        subargs = harness_parts[1] if len(harness_parts) > 1 else ""
        if subargs and subcommand not in {"benchmark-embed", "external"}:
            m.show_error(harness_subcommand_error(harness_arg))
        elif subcommand == "refresh":
            m.console.print(harness_refresh())
        elif subcommand in {"", "status", "stats", "quality"}:
            m.console.print(harness_stats())
        elif subcommand in {"score", "scorecard", "grade", "rating"}:
            m.console.print(harness_scorecard())
        elif subcommand in {"compare", "competitive"}:
            m.console.print(harness_competitive_rating())
        elif subcommand == "external":
            external_arg = (subargs or "status").strip().lower()
            if external_arg in {"on", "off"}:
                cfg.external_harness_sources_enabled = external_arg == "on"
                cfg.save()
                harness.configure_context_sources(
                    external=cfg.external_harness_sources_enabled,
                    index_compute_lab=cfg.index_compute_lab_auto_inject,
                )
                harness.load_index(refresh=True)
                m.show_info(
                    "External harness sources: "
                    f"{'ON' if cfg.external_harness_sources_enabled else 'OFF'}"
                )
                if cfg.external_harness_sources_enabled:
                    m.show_info(
                        "Content from other local agent stores may be included in model prompts, "
                        "including requests sent to cloud providers."
                    )
            elif external_arg in {"", "status", "show", "?"}:
                m.console.print(
                    "External harness sources: "
                    f"{'on' if cfg.external_harness_sources_enabled else 'off'}"
                )
            else:
                m.show_error("Usage: /harness external [on|off|status]")
        elif subcommand in {"build-rust", "rust"}:
            m.build_rust_indexer()
        elif subcommand == "benchmark-embed":
            m.run_harness_benchmark_embed(cfg, harness_arg)
        elif subcommand == "embed":
            backend, _reason = m.resolve_embed_backend(cfg)
            if not m.host_is_local(cfg.host) or not m.ollama_server_ready(cfg.host):
                m.show_error("Local Ollama is not reachable; cannot embed harness records.")
            else:
                embed_fn, _backend, active_model = m.make_embed_fn(cfg, harness.resolve_embed_model(cfg))
                matching, total = harness.embedded_count(active_model)
                pending = total - matching
                if matching == 0 and any(r.get("embedding") for r in (harness.load_index().get("records") or [])):
                    m.show_info(
                        f"Backend/model change detected: all {total} records "
                        f"will be re-embedded under {active_model}."
                    )
                queue = harness.embedding_progress(active_model)
                high_value = (
                    f" High-value coverage: {queue.get('high_value_embedded', 0)}/"
                    f"{queue.get('high_value_total', 0)}; next tier: "
                    f"{str(queue.get('next_priority') or 'complete').replace('_', ' ')}."
                    if int(queue.get("total", 0)) == total and int(queue.get("high_value_total", 0)) > 0
                    else ""
                )
                m.show_info(
                    f"Embedding {pending} of {total} harness records with {active_model} ({backend}) "
                    f"using {harness.EMBED_PRIORITY_POLICY}.{high_value}"
                )
                def _prog(done: int, target: int) -> None:
                    if done == target or done % (max(1, target // 5)) == 0:
                        m.show_info(f"  embeddings: {done}/{target}")
                result = harness.embed_index_records(
                    embed_fn,
                    active_model,
                    on_progress=_prog,
                    on_perf=lambda rec: m.log_embed_perf(rec, source="harness_embed_command", backend=backend),
                )
                if result.get("ready"):
                    m.show_info(f"Embedded {result.get('embedded', 0)} records. Total now embedded: {result.get('total', 0)}.")
                elif result.get("reason") == "max_records_reached":
                    next_priority = str(result.get("next_priority") or "complete").replace("_", " ")
                    m.show_info(
                        f"Embedded {result.get('embedded', 0)} records; {result.get('pending', 0)} pending. "
                        f"Next tier: {next_priority}."
                    )
                else:
                    m.show_error(f"Embedding failed: {result.get('reason', 'unknown')}")
        elif subcommand in {"help", "?"}:
            m.show_info(_HARNESS_USAGE)
        else:
            m.show_error(harness_subcommand_error(harness_arg))
    elif command == "/actions":
        from .tools import available_actions

        m.console.print(available_actions(arg or None))
    elif command == "/doctor":
        from .action_registry import build_doctor_report, render_doctor

        m.console.print(render_doctor(build_doctor_report(cfg)))
    elif command == "/selfcheck":
        from .tools import available_actions, harness_search, harness_stats
        from .action_registry import audit_action_registry_runtime, render_action_registry_runtime_audit
        from .kernels.manifest import audit_kernels, render_kernel_audit
        from .perf_telemetry import render_runtime_quality_snapshot

        m.console.print(harness_stats())
        m.console.print()
        m.console.print(render_action_registry_runtime_audit(audit_action_registry_runtime()))
        m.console.print()
        m.console.print(render_kernel_audit(audit_kernels()))
        m.console.print()
        m.console.print(render_runtime_quality_snapshot(cfg))
        m.console.print()
        m.console.print(available_actions("harness"))
        m.console.print()
        for query in (
            "index-compute-lab",
            "reflex loop",
            "harness context",
            "memory recall",
            "verification before completion",
        ):
            m.console.print(f"[bold]Search:[/] {query}")
            m.console.print(harness_search(query=query, limit=5))
            m.console.print()
    elif command == "/reload":
        reloaded_cfg = m.reload_runtime()
        for field in fields(Config):
            if field.name in {"messages", "session_summary", "context_state", "attempt_ledger"}:
                continue
            setattr(cfg, field.name, getattr(reloaded_cfg, field.name))
        client = m.create_client(cfg)
        m.show_info("Reloaded config, tools, and harness index.")
        m.show_info(
            "Restart algo-cli after Python code or plugin changes; /reload refreshes "
            "configuration, core tool modules, routing, and the harness index."
        )
    elif command == "/hsearch":
        if not arg:
            m.show_error("Usage: /hsearch <query>")
        else:
            if harness.index_is_stale():
                m.show_info("Harness index may be stale (source dirs changed). Run /harness refresh to update.")
            m.print_harness_results(arg, cfg=cfg)
    elif command == "/hread":
        if not arg:
            m.show_error("Usage: /hread <record-id>")
        else:
            from .tools import harness_read

            m.console.print(harness_read(arg))
    else:
        return False, client
    return True, client
