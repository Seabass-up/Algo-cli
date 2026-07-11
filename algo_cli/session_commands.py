"""Model-callable session slash commands (same behavior as TUI /read, /ls, /cd)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import tools as tools_module
from .workspace_resolver import parse_path_arg

MODEL_SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/read", "Read a file relative to session cwd (deterministic). Args: /read PATH"),
    ("/ls", "List directory under cwd. Args: /ls or /ls SUBPATH"),
    ("/cd", "Change session cwd (saved). Args: /cd PATH"),
    ("/cwd", "Show current session cwd. No args."),
)

_ALLOWED = frozenset(cmd for cmd, _ in MODEL_SLASH_COMMANDS)


def catalog_for_prompt() -> str:
    lines = [
        "Slash commands are session controls, not normal prose. If you merely write '/command' in a reply, nothing runs.",
        "Choose the right path:",
        "  1. User typed a slash command: the CLI handles it before you see it; do not re-run it unless asked.",
        "  2. Need deterministic cwd file navigation/read: call session_slash with the full line:",
        *[f"     - {cmd} — {desc}" for cmd, desc in MODEL_SLASH_COMMANDS],
        "  3. Need to change/check Algo CLI session state (/status, /mode, /context, /harness refresh, /agent, etc.): call session_command with the full line. Read-only/status commands run without approval; state-changing commands may require approval.",
        "  4. Need actual work: prefer model-callable tools (read_file/write_file/search_files/run_shell/web_search/etc.) over slash commands.",
        "When to use common slash commands:",
        "  - /status or /info: check active model, cwd, context, and toggles before giving configuration advice.",
        "  - /mode execute|explore|publish: switch operating posture only when the user's task clearly needs it or they ask.",
        "  - /context status|rebuild|clear: inspect or repair compressed context when context quality/length is the issue.",
        "  - /reason status|guide: inspect the active reasoning posture or show mode guidance before changing it.",
        "  - /reason react|reflexion|tot|got|mcts|qcr|neuro_symbolic: change reasoning posture for complex work only; do not switch modes for routine reads/edits.",
        "    Use react for normal tool loops, reflexion after failed/partial attempts, tot/got/mcts for ambiguous multi-path planning, qcr for comparing candidate solutions, and neuro_symbolic for verification-heavy logic/code claims.",
        "  - /harness refresh: refresh indexed skills/wiki/memory after local harness files changed.",
        "  - /intel query TERM: inspect the local repository intelligence graph before code/navigation decisions.",
        "    Use /intel status to check availability; /intel reindex requires approval because it writes the graph index.",
        "  - Google Workspace commands are read-only and auth-gated; call available_actions(topic='google') before using /google-login, /google-callback, /google-status, or /google subcommands.",
        "  - /route TASK: preview the recommended Agent Blocks route before a complex task.",
        "  - /agent [--pipeline NAME] TASK: run a traceable multi-block pipeline for larger implementation/research/review work.",
        "  - /agent team [--roles ROLE,ROLE[,ROLE,ROLE]] TASK: delegate independent analysis to 2-4 read-only child threads, then integrate once with normal write and verification gates.",
        "  - /agent threads or /agent show THREAD: inspect run history; /agent resume THREAD [TASK] continues it and /agent fork THREAD TASK branches it.",
        "    Delegate only when work has genuinely independent angles. Keep mutations in the integration pipeline; never ask child threads to edit the same workspace.",
        "  - /kernel check [NAME]: verify kernel imports, slash routes, and active action contracts without executing workloads.",
        "Do not use slash commands for file edits; use write_file. Do not use slash commands for build/test; use run_shell.",
        "For the full command/tool catalog and focused examples, call available_actions(topic='slash').",
    ]
    return "\n".join(lines)


def execute(command_line: str, cfg: Any, *, max_read_chars: int | None = None) -> str:
    """Run one whitelisted slash command against cfg.cwd."""
    stripped = (command_line or "").strip()
    if not stripped.startswith("/"):
        stripped = f"/{stripped}"
    if not stripped:
        return "Error: empty command. Allowed: /read, /ls, /cd, /cwd"

    parts = stripped.split(None, 1)
    command = parts[0].lower()
    remainder = stripped[len(parts[0]):].strip() if len(parts) > 1 else ""

    if command not in _ALLOWED:
        allowed = ", ".join(sorted(_ALLOWED))
        return f"Error: {command} is not model-invokable. Allowed: {allowed}"

    if command == "/cwd" or (command == "/cd" and not remainder):
        return f"cwd: {getattr(cfg, 'cwd', '.')}"

    if command == "/cd":
        target = parse_path_arg(remainder)
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = Path(getattr(cfg, "cwd", ".")) / path
        if path.exists() and path.is_dir():
            cfg.cwd = str(path.resolve())
            if hasattr(cfg, "save"):
                cfg.save()
            return f"cwd: {cfg.cwd}"
        return f"Error: not a directory: {path}"

    if command == "/ls":
        rel = parse_path_arg(remainder) or "."
        return tools_module.list_directory(rel, cwd=getattr(cfg, "cwd", None), limit=40)

    if command == "/read":
        rel = parse_path_arg(remainder)
        if not rel:
            return "Error: usage: /read PATH"
        limit = max_read_chars if max_read_chars is not None else tools_module.MAX_READ_CHARS
        return tools_module.read_file(rel, cwd=getattr(cfg, "cwd", None), max_chars=limit)

    return f"Error: unhandled command {command}"
