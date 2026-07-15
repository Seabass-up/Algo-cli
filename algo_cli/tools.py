"""Tools exposed to Ollama tool calling."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import json
import fnmatch
import re
import signal
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
import inspect
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import harness
from . import identity
from . import index_compute_lab as _index_compute_lab
from .config import CONFIG_DIR, Config, load_runtime_env, _atomic_write_text
from ollama import Client

from .chat_protocol import get_attr

logger = logging.getLogger(__name__)


MAX_READ_CHARS = 50_000
MAX_PDF_PAGES = 24
MAX_RENDER_PDF_PAGES = 6
MAX_TOOL_RESULT = 20_000
WEB_FETCH_MAX_INFLIGHT = 2
_WEB_FETCH_SLOTS = threading.BoundedSemaphore(WEB_FETCH_MAX_INFLIGHT)
SESSION_COMMAND_OUTPUT_LIMIT = MAX_TOOL_RESULT
SEARCH_FALLBACK_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build",
    "__pycache__", ".next", "target", ".mypy_cache", ".pytest_cache",
}
SEARCH_FALLBACK_MAX_FILE_BYTES = 2_000_000
SEARCH_FALLBACK_MAX_FILES = 5_000
DEFAULT_GATEWAY_URL = (
    os.environ.get("ALGO_CLI_GATEWAY_URL")
    or os.environ.get("OLLAMA_CLI_GATEWAY_URL")
    or "http://127.0.0.1:8765"
)
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DENY_COMMAND_RE = re.compile(
    r"\b(rm|del|erase|rd|rmdir|format|diskpart|shutdown|restart-computer|stop-computer|"
    r"git\s+reset|git\s+checkout|Remove-Item)\b",
    re.IGNORECASE,
)
REQUIRED_CHANGE_SHELL_MUTATION_RE = re.compile(
    r"\b(?:remove-item|move-item|copy-item|rename-item|new-item|set-content|add-content|clear-content|"
    r"out-file|export-csv|export-clixml|start-transcript)\b|"
    r"(?:^|[\s;&|\"']+)(?:rm|mv|cp|del|erase|rd|rmdir|touch|mkdir|md|ni|ri|mi|cpi)\b|"
    r"\b(?:sed\s+-i|perl\s+-pi|truncate\s+-s)\b|"
    r"\brobocopy\b[^\n]*\s/(?:mir|purge)\b|"
    r"(?:>{1,2}(?!&)|(?:\|\s*)tee\b)|"
    r"\bpython(?:3)?\b[^\n]*\s-c\s+[^\n]*(?:write_text\s*\(|write_bytes\s*\(|\.write\s*\(|"
    r"\.unlink\s*\(|\.rename\s*\(|\.replace\s*\(|\.mkdir\s*\(|os\.(?:remove|unlink|rename|replace|mkdir|makedirs)\s*\(|"
    r"shutil\.(?:copy|copy2|copyfile|move|rmtree)\s*\()",
    re.IGNORECASE,
)
PYTHON_OPEN_WRITE_RE = re.compile(
    r"\bpython(?:3)?\b[^\n]*\s-c\s+[^\n]*\bopen\s*\([^)]*(?:,\s*['\"]"
    r"(?:[wax][^'\"]*|[^'\"]*\+[^'\"]*)['\"]|\bmode\s*=\s*['\"]"
    r"(?:[wax][^'\"]*|[^'\"]*\+[^'\"]*)['\"])",
    re.IGNORECASE,
)
NULL_REDIRECTION_RE = re.compile(r"\b\d?\s*>{1,2}\s*(?:\$null\b|nul\b|/dev/null\b)", re.IGNORECASE)

_GIT_EXECUTABLE_RE = re.compile(r"(?:^|[\\/])git(?:\.exe)?$", re.IGNORECASE)
_GIT_TEXT_RE = re.compile(r"(?<![\w-])git(?:\.exe)?(?:\s|$)", re.IGNORECASE)
_GIT_PARSE_MAX_CHARS = 32_768
_GIT_PARSE_MAX_TOKENS = 512
_GIT_PARSE_MAX_GLOBAL_OPTIONS = 32
_GIT_PARSE_MAX_READ_ONLY_OPTIONS = 128
_GIT_GLOBAL_FLAGS = frozenset(
    {
        "-p",
        "-P",
        "--paginate",
        "--no-pager",
        "--no-replace-objects",
        "--bare",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-optional-locks",
        "--no-lazy-fetch",
        "--no-advice",
    }
)
_GIT_GLOBAL_QUERY_OPTIONS = frozenset(
    {
        "--version",
        "-v",
        "--help",
        "-h",
        "--html-path",
        "--man-path",
        "--info-path",
        "--exec-path",
    }
)
_GIT_GLOBAL_EQUALS_OPTIONS = frozenset(
    {
        "--exec-path",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--config-env",
        "--attr-source",
    }
)
_GIT_MUTATING_SUBCOMMANDS = frozenset(
    {
        "add",
        "apply",
        "commit",
        "checkout",
        "restore",
        "reset",
        "clean",
        "mv",
        "rm",
        "switch",
        "merge",
        "rebase",
        "cherry-pick",
        "push",
    }
)
_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "blame",
        "cat-file",
        "describe",
        "diff",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
    }
)
_GIT_UNSAFE_READ_ONLY_FLAGS = frozenset(
    {
        "--ext-diff",
        "--filters",
        "--open-files-in-pager",
        "--textconv",
    }
)
_GIT_UNSAFE_READ_ONLY_VALUE_OPTIONS = frozenset(
    {
        "--open-files-in-pager",
        "--output",
    }
)
_GIT_MUTATING_WORKTREE_ACTIONS = frozenset(
    {"add", "remove", "move", "lock", "unlock", "prune", "repair"}
)
_GIT_MUTATING_BRANCH_OPTIONS = frozenset(
    {
        "--delete",
        "--move",
        "--copy",
        "--edit-description",
        "--set-upstream-to",
        "--unset-upstream",
        "--create-reflog",
        "--no-create-reflog",
        "--track",
        "--no-track",
        "--recurse-submodules",
    }
)
_GIT_READ_ONLY_BRANCH_FLAGS = frozenset(
    {
        "-a",
        "--all",
        "-r",
        "--remotes",
        "-v",
        "-vv",
        "--verbose",
        "-q",
        "--quiet",
        "--show-current",
        "--ignore-case",
        "--no-color",
    }
)
_GIT_READ_ONLY_BRANCH_VALUE_OPTIONS = frozenset(
    {
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--format",
        "--sort",
        "--abbrev",
    }
)


def _is_shell_control_token(token: str) -> bool:
    return bool(token) and all(char in ";&|()" for char in token)


def _tokenize_shell_for_git(command: str) -> list[str] | None:
    """Return a bounded shell-like token stream, or None when parsing is unsafe."""

    if len(command) > _GIT_PARSE_MAX_CHARS:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens: list[str] = []
        for token in lexer:
            tokens.append(token)
            if len(tokens) > _GIT_PARSE_MAX_TOKENS:
                return None
        return tokens
    except ValueError:
        return None


def _git_global_equals_option(token: str) -> tuple[str, str] | None:
    for option in _GIT_GLOBAL_EQUALS_OPTIONS:
        prefix = f"{option}="
        if token.startswith(prefix):
            return option, token[len(prefix):]
    return None


def _git_branch_mutates(tokens: list[str], start: int) -> bool:
    """Classify branch inspection separately from branch creation/mutation."""

    index = start
    explicit_list = False
    positionals = 0
    while index < len(tokens) and not _is_shell_control_token(tokens[index]):
        token = tokens[index]
        if token in {"--list", "-l"}:
            explicit_list = True
            index += 1
            continue
        if token in _GIT_MUTATING_BRANCH_OPTIONS or any(
            token.startswith(f"{option}=") for option in _GIT_MUTATING_BRANCH_OPTIONS
        ):
            return True
        if token.startswith("-") and not token.startswith("--"):
            if any(flag in "dDmMcC" for flag in token[1:]):
                return True
            if all(flag in "arvql" for flag in token[1:]):
                explicit_list = explicit_list or "l" in token[1:]
                index += 1
                continue
            return True
        if token in _GIT_READ_ONLY_BRANCH_FLAGS:
            index += 1
            continue
        if token in _GIT_READ_ONLY_BRANCH_VALUE_OPTIONS:
            index += 1
            if index >= len(tokens) or _is_shell_control_token(tokens[index]) or not tokens[index]:
                return True
            index += 1
            continue
        if any(token.startswith(f"{option}=") for option in _GIT_READ_ONLY_BRANCH_VALUE_OPTIONS):
            if token.endswith("="):
                return True
            index += 1
            continue
        if token.startswith("-"):
            return True
        positionals += 1
        index += 1
    return bool(positionals and not explicit_list)


def _git_read_only_options_mutate(subcommand: str, tokens: list[str], start: int) -> bool:
    """Fail closed for inspection flags that write output or execute helpers."""

    scanned = 0
    index = start
    while index < len(tokens) and not _is_shell_control_token(tokens[index]):
        scanned += 1
        if scanned > _GIT_PARSE_MAX_READ_ONLY_OPTIONS:
            return True
        token = tokens[index]
        if token in _GIT_UNSAFE_READ_ONLY_FLAGS or any(
            token.startswith(f"{option}=") for option in _GIT_UNSAFE_READ_ONLY_FLAGS
        ):
            return True
        if token in _GIT_UNSAFE_READ_ONLY_VALUE_OPTIONS or any(
            token.startswith(f"{option}=") for option in _GIT_UNSAFE_READ_ONLY_VALUE_OPTIONS
        ):
            return True
        # `git grep -O[pager]` is the short form of
        # --open-files-in-pager and can execute an arbitrary pager command.
        if subcommand == "grep" and token.startswith("-O"):
            return True
        index += 1
    return False


def _git_invocation_mutates(tokens: list[str], git_index: int) -> bool:
    index = git_index + 1
    global_options = 0
    while index < len(tokens) and not _is_shell_control_token(tokens[index]):
        token = tokens[index]
        if token in _GIT_GLOBAL_QUERY_OPTIONS:
            return False
        if token in _GIT_GLOBAL_FLAGS:
            global_options += 1
            index += 1
        elif token in {"-C", "-c"}:
            global_options += 1
            index += 1
            if index >= len(tokens) or _is_shell_control_token(tokens[index]) or not tokens[index]:
                return True
            if token == "-c" and ("=" not in tokens[index] or tokens[index].startswith("=")):
                return True
            index += 1
        else:
            equals_option = _git_global_equals_option(token)
            if equals_option is not None:
                global_options += 1
                option, value = equals_option
                if not value:
                    return True
                if option == "--config-env" and ("=" not in value or value.startswith("=") or value.endswith("=")):
                    return True
                index += 1
            elif token.startswith("-"):
                # Unknown or malformed global options may hide an alias or
                # change parsing. Safe mode must fail closed in this case.
                return True
            else:
                subcommand = token.lower()
                if subcommand in _GIT_MUTATING_SUBCOMMANDS:
                    return True
                if subcommand == "worktree":
                    action_index = index + 1
                    if action_index >= len(tokens) or _is_shell_control_token(tokens[action_index]):
                        return True
                    action = tokens[action_index].lower()
                    if action in _GIT_MUTATING_WORKTREE_ACTIONS:
                        return True
                    return action != "list"
                if subcommand == "branch":
                    return _git_branch_mutates(tokens, index + 1)
                if subcommand in _GIT_READ_ONLY_SUBCOMMANDS:
                    return _git_read_only_options_mutate(subcommand, tokens, index + 1)
                # Git aliases are arbitrary commands and may execute shell
                # snippets. Unknown names therefore cannot be assumed to be
                # inspections, even when they look harmless.
                return True
        if global_options > _GIT_PARSE_MAX_GLOBAL_OPTIONS:
            return True
    # A bare `git` is an inspection/help request. An option-only invocation is
    # incomplete, so fail closed instead of guessing what the caller intended.
    return global_options > 0


def _git_command_mutates_workspace(command: str, *, _depth: int = 0) -> bool:
    if not _GIT_TEXT_RE.search(command):
        return False
    tokens = _tokenize_shell_for_git(command)
    if tokens is None:
        return True
    for index, token in enumerate(tokens):
        if _GIT_EXECUTABLE_RE.search(token) and _git_invocation_mutates(tokens, index):
            return True
    if _depth >= 2:
        return False
    # Shells commonly put a complete command in one quoted `-c` token. Inspect
    # those bounded nested snippets so quoting cannot bypass the Git policy.
    for token in tokens:
        if token != command and _GIT_TEXT_RE.search(token):
            if _git_command_mutates_workspace(token, _depth=_depth + 1):
                return True
    return False


def shell_mutates_workspace(command: str) -> bool:
    """Return whether a shell command appears to alter files or local/remote Git state."""

    without_null_redirection = NULL_REDIRECTION_RE.sub("", command or "")
    return bool(
        _git_command_mutates_workspace(without_null_redirection)
        or REQUIRED_CHANGE_SHELL_MUTATION_RE.search(without_null_redirection)
        or PYTHON_OPEN_WRITE_RE.search(without_null_redirection)
    )


def shell_is_dangerous(command: str) -> bool:
    """Return whether safe mode must block the command.

    The destructive deny list covers host-level actions such as shutdown and
    disk formatting that are not necessarily workspace mutations.
    """

    return bool(DENY_COMMAND_RE.search(command or "")) or shell_mutates_workspace(command)


def _resolve(path: str, cwd: str | None = None) -> Path:
    base = Path(cwd or os.getcwd()).expanduser()
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def _cap(text: str, limit: int = MAX_TOOL_RESULT) -> str:
    return text[:limit] + ("\n...[truncated]" if len(text) > limit else "")


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _missing_file_matches(path: Path, cwd: str | None, *, limit: int = 3) -> list[Path]:
    """Find bounded same-basename recovery candidates inside the active cwd."""

    if not path.name:
        return []
    root = Path(cwd or os.getcwd()).expanduser().resolve()
    matches: list[Path] = []
    scanned = 0
    skipped_dirs = {".git", ".venv", "node_modules", "__pycache__"}
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            scanned += 1
            dirnames[:] = [name for name in dirnames if name not in skipped_dirs]
            if path.name in filenames:
                candidate = (Path(dirpath) / path.name).resolve()
                if candidate != path:
                    matches.append(candidate)
                    if len(matches) >= limit:
                        break
            if scanned >= 2_000:
                break
    except OSError:
        return []
    return matches


def unpack_embed_response(
    response: Any,
    model: str,
    input_text: str,
    *,
    truncate: bool | None = None,
    dimensions: int | None = None,
) -> dict[str, Any]:
    """Normalize Ollama embed API responses (dict or object) into a JSON-serializable payload."""
    embeddings = get_attr(response, "embeddings", []) or []
    first = embeddings[0] if embeddings else []
    payload: dict[str, Any] = {
        "model": get_attr(response, "model", model),
        "input_chars": len(input_text),
        "vector_count": len(embeddings),
        "vector_length": len(first),
        "preview": [round(float(value), 6) for value in first[:8]],
        "total_duration": get_attr(response, "total_duration", None),
        "load_duration": get_attr(response, "load_duration", None),
        "prompt_eval_count": get_attr(response, "prompt_eval_count", None),
    }
    if truncate is not None:
        payload["truncate"] = truncate
    if dimensions is not None:
        payload["dimensions"] = dimensions
    return payload


def active_ollama_client(*, cloud: bool = False) -> Client:
    load_runtime_env(override=True)
    if cloud:
        api_key = os.environ.get("OLLAMA_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        return Client(host="https://ollama.com", headers=headers)
    return Client(host=os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST))


def _ollama_cloud_web_preflight(action: str) -> str | None:
    load_runtime_env(override=True)
    if os.environ.get("OLLAMA_API_KEY", "").strip():
        return None
    return (
        f"Error {action}: OLLAMA_API_KEY is not set. "
        "Set ALGO_CLI_ENV_FILE or ~/.algo_cli/env with OLLAMA_API_KEY, then run /doctor to verify "
        "Ollama Cloud web access."
    )


def read_file(
    path: str,
    cwd: str | None = None,
    max_chars: int = MAX_READ_CHARS,
    start_line: int = 1,
    offset: int | None = None,
) -> str:
    """Read a text file.

    Args:
        path: File path to read.
        cwd: Optional working directory for relative paths.
        max_chars: Maximum characters to return.
        start_line: One-based line number to begin reading from.
        offset: Compatibility alias for start_line.
    """
    p = _resolve(path, cwd)
    if not p.exists():
        matches = _missing_file_matches(p, cwd)
        if not matches:
            return f"Error: file not found: {p}"
        suggestions = "\n".join(f"- {candidate}" for candidate in matches)
        return (
            f"Error: file not found: {p}\n"
            f"Same-name file(s) found inside the working directory:\n{suggestions}\n"
            "Retry read_file with the intended exact path."
        )
    if p.is_dir():
        return f"Error: {p} is a directory. Use list_directory."
    try:
        max_chars = _bounded_int(max_chars, MAX_READ_CHARS, 1, MAX_READ_CHARS)
        text = p.read_text(encoding="utf-8", errors="replace")
        requested_line = offset if offset is not None else start_line
        line_number = max(1, int(requested_line))
        if line_number > 1:
            text = "".join(text.splitlines(keepends=True)[line_number - 1:])
        return text[:max_chars]
    except Exception as exc:
        return f"Error reading {p}: {exc}"


def read_pdf(
    path: str,
    cwd: str | None = None,
    max_chars: int = MAX_READ_CHARS,
    max_pages: int = MAX_PDF_PAGES,
) -> str:
    """Extract text from a PDF using local Python PDF libraries.

    Args:
        path: PDF file path to read.
        cwd: Optional working directory for relative paths.
        max_chars: Maximum characters to return.
        max_pages: Maximum pages to inspect.
    """
    p = _resolve(path, cwd)
    if not p.exists():
        return f"Error: PDF not found: {p}"
    if p.is_dir():
        return f"Error: {p} is a directory, not a PDF."
    if p.suffix.lower() != ".pdf":
        return f"Error: {p} does not look like a PDF."
    max_chars = _bounded_int(max_chars, MAX_READ_CHARS, 1, MAX_READ_CHARS)
    max_pages = _bounded_int(max_pages, MAX_PDF_PAGES, 1, MAX_PDF_PAGES)

    pages: list[str] = []
    engine = ""
    page_count = 0
    try:
        import fitz  # type: ignore[import-not-found]

        engine = "PyMuPDF"
        with fitz.open(p) as doc:
            page_count = len(doc)
            for index, page in enumerate(doc):
                if index >= max_pages:
                    break
                text = page.get_text("text").strip()
                pages.append(f"[page {index + 1}]\n{text}" if text else f"[page {index + 1}]\n")
    except Exception:
        try:
            from PyPDF2 import PdfReader  # type: ignore[import-not-found]

            engine = "PyPDF2"
            reader = PdfReader(str(p))
            page_count = len(reader.pages)
            for index, page in enumerate(reader.pages[:max_pages]):
                text = (page.extract_text() or "").strip()
                pages.append(f"[page {index + 1}]\n{text}" if text else f"[page {index + 1}]\n")
        except Exception as exc:
            return f"Error extracting PDF text from {p}: {exc}"

    combined = "\n\n".join(pages).strip()
    if not combined or all(not chunk.split("\n", 1)[-1].strip() for chunk in pages):
        return (
            f"PDF extraction completed with {engine}, but no text layer was found in {p}. "
            "This PDF may be scanned or image-only. Use render_pdf_pages next, then pass the returned PNG path(s) to vision_describe or another OCR-capable workflow."
        )
    suffix = ""
    if page_count > max_pages:
        suffix = f"\n\n...[limited to first {max_pages} of {page_count} pages]"
    header = f"PDF: {p}\nEngine: {engine}\nPages read: {min(page_count, max_pages)} of {page_count}\n\n"
    return _cap((header + combined + suffix)[:max_chars])


def render_pdf_pages(
    path: str,
    cwd: str | None = None,
    start_page: int = 1,
    max_pages: int = MAX_RENDER_PDF_PAGES,
    scale: float = 1.75,
) -> str:
    """Render PDF pages to PNG images for downstream OCR or visual inspection.

    Args:
        path: PDF file path to render.
        cwd: Optional working directory for relative paths.
        start_page: 1-based page number to start from.
        max_pages: Maximum number of pages to render.
        scale: Render scale multiplier; higher values improve OCR at larger image sizes.
    """
    p = _resolve(path, cwd)
    if not p.exists():
        return f"Error: PDF not found: {p}"
    if p.is_dir():
        return f"Error: {p} is a directory, not a PDF."
    if p.suffix.lower() != ".pdf":
        return f"Error: {p} does not look like a PDF."
    if start_page < 1:
        return "Error: start_page must be 1 or greater."
    if max_pages < 1:
        return "Error: max_pages must be 1 or greater."
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception as exc:
        return f"Error: PDF rendering requires PyMuPDF/fitz, but it could not be imported: {exc}"

    output_dir = Path(tempfile.gettempdir()) / "ollama_cli_pdf_pages"
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[str] = []
    try:
        with fitz.open(p) as doc:
            first_index = start_page - 1
            if first_index >= len(doc):
                return f"Error: start_page {start_page} exceeds PDF page count {len(doc)}."
            last_index = min(len(doc), first_index + max_pages)
            safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", p.stem).strip("_") or "pdf"
            matrix = fitz.Matrix(max(0.5, float(scale)), max(0.5, float(scale)))
            for index in range(first_index, last_index):
                page = doc.load_page(index)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                out = output_dir / f"{safe_stem}_page_{index + 1}.png"
                pix.save(out)
                rendered.append(str(out))
            return json.dumps(
                {
                    "pdf": str(p),
                    "page_count": len(doc),
                    "rendered_pages": len(rendered),
                    "paths": rendered,
                    "next_step": "Pass one returned PNG path to vision_describe or an OCR workflow.",
                },
                indent=2,
            )
    except Exception as exc:
        return f"Error rendering PDF pages from {p}: {exc}"


def write_file(path: str, content: str, cwd: str | None = None, overwrite: bool = False) -> str:
    """Write text to a file. Existing files require overwrite=true.

    Args:
        path: File path to write.
        content: Content to write.
        cwd: Optional working directory for relative paths.
        overwrite: Whether to overwrite an existing file.
    """
    p = _resolve(path, cwd)
    if p.exists() and not overwrite:
        return f"Error: {p} already exists. Re-run with overwrite=true if intended."
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(p, content)
        return f"Wrote {len(content)} characters to {p}"
    except Exception as exc:
        return f"Error writing {p}: {exc}"


def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    cwd: str | None = None,
    replace_all: bool = False,
) -> str:
    """Make a precise, surgical edit to a text file using a find/replace match.

    This is the preferred tool for modifying existing files. It is faster, safer,
    and uses fewer tokens than reading the whole file and rewriting it with
    write_file. The edit is applied atomically (write-to-tmp + os.replace) and
    the tool reports the affected line numbers so callers can verify the change.

    Args:
        path: File path to edit.
        old_string: Exact text to find. Must match the file contents byte-for-byte
            (after decoding as UTF-8). Include enough surrounding context (3-5
            lines) to make the match unique. Whitespace, indentation, and line
            endings matter.
        new_string: The replacement text. Use an empty string to delete the
            matched region. The new_string is inserted verbatim; preserve
            trailing newlines and indentation.
        cwd: Optional working directory for relative paths.
        replace_all: If True, replace every non-overlapping occurrence. If False
            (default), the call FAILS when more than one match is found so you
            do not accidentally rewrite repeated patterns. Set replace_all=True
            only when the match is genuinely meant to apply everywhere.

    Returns a human-readable summary with the affected line range, or an error
    explaining why the edit could not be applied (file missing, no match, or
    ambiguous match). The file is NOT modified when the call returns an error.

    Smart-edit guidelines:
        - First call read_file to confirm the exact text you are about to change.
        - Prefer the smallest, most unique snippet that still locates the right
          place. A full function definition is usually too much; 2-5 lines with
          a distinctive local anchor is ideal.
        - If the match is ambiguous, tighten old_string (add more context) or set
          replace_all=True only when the rewrite is intentionally global.
        - If the file is large or has many similar blocks, call search_files with
          a unique anchor regex to find line numbers first, then construct a
          narrow old_string that includes just enough context to be unique.
    """
    if not old_string:
        return "Error: edit_file requires a non-empty old_string. Use write_file to create a new file."

    p = _resolve(path, cwd)
    if not p.exists():
        return f"Error: file not found: {p}"
    if p.is_dir():
        return f"Error: {p} is a directory. edit_file only works on text files."

    try:
        original = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error reading {p}: {exc}"

    occurrences = original.count(old_string)
    if occurrences == 0:
        # Give the model a helpful pointer: show the closest matching line if any
        first_line = old_string.splitlines()[0] if old_string.splitlines() else old_string
        snippet = first_line[:80] + ("..." if len(first_line) > 80 else "")
        return (
            f"Error: old_string not found in {p}. "
            f"No match for the first line: {snippet!r}. "
            "Re-read the file to confirm exact whitespace and indentation, then retry."
        )
    if occurrences > 1 and not replace_all:
        line_numbers: list[int] = []
        start = 0
        while True:
            idx = original.find(old_string, start)
            if idx < 0:
                break
            line_numbers.append(original.count("\n", 0, idx) + 1)
            start = idx + max(1, len(old_string))
        return (
            f"Error: old_string matched {occurrences} locations in {p} "
            f"(first occurrences near lines {line_numbers[:8]}). "
            "Tighten old_string with more surrounding context, or pass replace_all=True "
            "if the rewrite is intentionally global."
        )

    if replace_all and occurrences > 1:
        new_content = original.replace(old_string, new_string)
        replaced = occurrences
    else:
        new_content = original.replace(old_string, new_string, 1)
        replaced = 1

    # Compute line numbers for the edit anchor (start line, end line)
    try:
        start_index = original.index(old_string)
    except ValueError:
        start_index = 0
    prefix = original[:start_index]
    start_line = prefix.count("\n") + 1
    end_line = start_line + old_string.count("\n")
    span = f"lines {start_line}-{end_line}"

    if new_content == original:
        return (
            f"Error: old_string and new_string are identical at {span} in {p}. "
            "No change would be made. Adjust new_string to actually differ."
        )

    try:
        _atomic_write_text(p, new_content)
    except Exception as exc:
        return f"Error writing {p}: {exc}"

    delta = len(new_string) - len(old_string)
    return (
        f"Edited {p}: replaced {replaced} occurrence(s) at {span} "
        f"({len(old_string)} -> {len(new_string)} chars, delta {delta:+d})."
    )


def find_unique_anchor(
    path: str,
    needle: str,
    cwd: str | None = None,
    *,
    context_before: int = 2,
    context_after: int = 2,
    max_results: int = 5,
) -> str:
    """Locate occurrences of ``needle`` in a file and return enough surrounding
    context that an ``edit_file`` call with that context as ``old_string`` would
    match a unique location.

    Use this when ``edit_file`` reports an ambiguous match (multiple
    locations) and you need to disambiguate by including more context, or
    when you are about to call ``edit_file`` and want a guaranteed-unique
    snippet on the first try.

    The function reports every match with its line number and a few
    surrounding lines. Pass those context lines back as ``old_string`` to
    ``edit_file`` and the match will (with high probability) be unique.

    Args:
        path: File path to search.
        needle: The text to find. Can be multi-line. Whitespace matters.
        cwd: Optional working directory for relative paths.
        context_before: Lines of context to include BEFORE each match.
        context_after: Lines of context to include AFTER each match.
        max_results: Stop after this many matches. Default 5 is enough to
            decide whether a stricter old_string is needed.
    """
    if not needle:
        return "Error: needle is empty."

    p = _resolve(path, cwd)
    if not p.exists():
        return f"Error: file not found: {p}"
    if p.is_dir():
        return f"Error: {p} is a directory."
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error reading {p}: {exc}"

    lines = text.splitlines(keepends=True)
    if not needle.splitlines():
        return "Error: needle is empty."

    matches: list[str] = []
    for i in range(len(lines) - max(1, len(needle.splitlines())) + 1):
        n = len(needle.splitlines())
        block = "".join(lines[i : i + n])
        block_for_compare = block
        if not needle.endswith("\n") and block_for_compare.endswith("\n"):
            block_for_compare = block_for_compare[:-1]
        if block_for_compare == needle:
            start_line = i + 1
            ctx_start = max(0, i - context_before)
            ctx_end = min(len(lines), i + n + context_after)
            ctx_block = "".join(lines[ctx_start:ctx_end])
            matches.append(
                f"--- match at line {start_line} (with {context_before}/{context_after} lines context) ---\n"
                f"{ctx_block.rstrip()}\n"
                f"--- end match ---"
            )
            if len(matches) >= max_results:
                break

    if not matches:
        # Suggest: the first line of needle as a separate search hint
        first = needle.splitlines()[0].rstrip("\n")
        return (
            f"No match for needle in {p}.\n"
            f"First line of needle was: {first!r}\n"
            "Re-read the file and check exact whitespace / line endings, then retry."
        )
    if len(matches) == 1:
        return (
            f"Found 1 unique match for needle in {p}.\n"
            f"{matches[0]}\n"
            "This block should match a unique location when passed to edit_file."
        )
    return (
        f"Found {len(matches)} matches for needle in {p}. "
        f"Include more context (one of the surrounding blocks below) in old_string to disambiguate:\n\n"
        + "\n\n".join(matches)
    )


def batch_edit(
    path: str,
    edits: list[dict[str, str]],
    cwd: str | None = None,
    *,
    replace_all: bool = False,
) -> str:
    """Apply a sequence of find/replace edits to one file in a single tool call.

    Faster and cheaper than calling ``edit_file`` once per edit when the same
    file needs multiple independent changes. The edits are applied in the
    order given; each edit operates on the file as modified by the previous
    edit, so line numbers from the original file are not preserved.

    All edits run in a single atomic write at the end. If any edit fails to
    find its match (or finds ambiguous matches), the entire batch is
    rejected and the file is NOT modified.

    Args:
        path: File path to edit.
        edits: List of edit objects, each ``{"old_string": "...", "new_string": "..."}``.
            Edit order matters: later edits operate on the post-edit content
            of earlier ones.
        cwd: Optional working directory for relative paths.
        replace_all: Apply replace_all=True to every edit. Default False
            requires each old_string to match exactly once.
    """
    if not edits:
        return "Error: edits list is empty."

    p = _resolve(path, cwd)
    if not p.exists():
        return f"Error: file not found: {p}"
    if p.is_dir():
        return f"Error: {p} is a directory."

    try:
        original = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error reading {p}: {exc}"

    working = original
    applied: list[str] = []
    for index, edit in enumerate(edits, 1):
        old = str(edit.get("old_string", ""))
        new = str(edit.get("new_string", ""))
        if not old:
            return f"Error: edit #{index} has empty old_string. Refusing to apply batch."
        count = working.count(old)
        if count == 0:
            return (
                f"Error: edit #{index} old_string not found in working content. "
                f"Refused to apply batch. Earlier edits were: {'; '.join(applied) or '(none)'}."
            )
        if count > 1 and not replace_all:
            return (
                f"Error: edit #{index} old_string matched {count} locations. "
                "Refused to apply batch. Pass replace_all=True or tighten the snippet."
            )
        if replace_all and count > 1:
            working = working.replace(old, new)
            applied.append(f"#{index}: {count} occurrences")
        else:
            working = working.replace(old, new, 1)
            applied.append(f"#{index}: 1 occurrence")

    if working == original:
        return "Error: batch contained only no-op edits. Refused to write the same file."

    try:
        _atomic_write_text(p, working)
    except Exception as exc:
        return f"Error writing {p}: {exc}"

    delta = len(working) - len(original)
    return (
        f"Batch-edited {p}: applied {len(edits)} edits ({'; '.join(applied)}). "
        f"File grew by {delta:+d} chars."
    )


def list_directory(path: str = ".", cwd: str | None = None, limit: int = 200) -> str:
    """List files and directories.

    Args:
        path: Directory path to list.
        cwd: Optional working directory for relative paths.
        limit: Maximum entries to return.
    """
    p = _resolve(path, cwd)
    if not p.exists():
        return f"Error: directory not found: {p}"
    if not p.is_dir():
        return f"Error: {p} is not a directory."
    entries = []
    try:
        for entry in sorted(p.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))[:limit]:
            suffix = "/" if entry.is_dir() else ""
            size = ""
            if entry.is_file():
                try:
                    size = f" ({entry.stat().st_size:,} bytes)"
                except OSError:
                    size = ""
            entries.append(f"{entry.name}{suffix}{size}")
    except Exception as exc:
        return f"Error listing {p}: {exc}"
    more = "" if len(entries) < limit else f"\n...[limited to {limit} entries]"
    return "\n".join(entries) + more if entries else "(empty directory)"


def search_files(pattern: str, path: str = ".", cwd: str | None = None, glob: str | None = None, limit: int = 100) -> str:
    """Search files with ripgrep when available.

    Args:
        pattern: Text or regex pattern to search.
        path: Root path to search.
        cwd: Optional working directory for relative paths.
        glob: Optional rg glob, such as *.py.
        limit: Maximum matching lines.
    """
    root = _resolve(path, cwd)
    if not root.exists():
        return f"Error: path not found: {root}"
    if root.is_file():
        try:
            if glob and not fnmatch.fnmatch(root.name, glob):
                return "No matches."
            if root.stat().st_size > SEARCH_FALLBACK_MAX_FILE_BYTES:
                return "No matches."
            text = root.read_text(encoding="utf-8", errors="ignore")
            file_matches = [
                f"{root}:{lineno}:{line}"
                for lineno, line in enumerate(text.splitlines(), 1)
                if re.search(pattern, line)
            ]
            return "\n".join(file_matches[:limit]) if file_matches else "No matches."
        except Exception as exc:
            return f"Error searching: {exc}"
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--line-number", "--hidden", "--glob", "!{.git,node_modules,.venv,venv,dist,build,__pycache__}"]
        if glob:
            cmd.extend(["--glob", glob])
        cmd.extend([pattern, str(root)])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
            if proc.returncode not in {0, 1}:
                return f"Error searching: {(proc.stderr or proc.stdout or '').strip() or f'rg exited with {proc.returncode}'}"
            lines = (proc.stdout or "").splitlines()
            return "\n".join(lines[:limit]) or "No matches."
        except subprocess.TimeoutExpired:
            return "Error: search timed out after 20 seconds."
    matches: list[str] = []
    scanned = 0
    truncated = False
    try:
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SEARCH_FALLBACK_SKIP_DIRS]
            for filename in files:
                if len(matches) >= limit:
                    break
                if scanned >= SEARCH_FALLBACK_MAX_FILES:
                    truncated = True
                    break
                if glob and not fnmatch.fnmatch(filename, glob):
                    continue
                fpath = Path(current) / filename
                try:
                    if fpath.stat().st_size > SEARCH_FALLBACK_MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                scanned += 1
                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if re.search(pattern, line):
                        matches.append(f"{fpath}:{lineno}:{line}")
                        if len(matches) >= limit:
                            break
            if len(matches) >= limit or truncated:
                break
    except Exception as exc:
        return f"Error searching: {exc}"
    if not matches:
        return "No matches."
    suffix = f"\n...[stopped after scanning {SEARCH_FALLBACK_MAX_FILES} files]" if truncated else ""
    return "\n".join(matches) + suffix


def _isolated_process_group_kwargs(platform_name: str | None = None) -> dict[str, Any]:
    """Return portable subprocess flags that isolate a child process group."""
    if (platform_name or os.name) == "nt":
        # ``subprocess.CREATE_NEW_PROCESS_GROUP`` is only defined on Windows.
        # The documented Win32 value keeps this module importable/type-checkable
        # on POSIX while still preferring the platform constant when available.
        return {
            "creationflags": getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0x00000200,
            )
        }
    return {"start_new_session": True}


def _terminate_process_tree(
    proc: subprocess.Popen[str],
    *,
    platform_name: str | None = None,
) -> None:
    """Best-effort termination for the isolated shell process and descendants."""

    if (platform_name or os.name) == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_shell(command: str, cwd: str | None = None, timeout: float = 30, safe_mode: bool = False) -> str:
    """Run a shell command and return output.

    On Windows this executes under cmd.exe: Unix tools like head, tail, grep,
    sed, and awk are NOT available. Use native equivalents (findstr, more),
    flags on the command itself (e.g. pytest -q, --maxfail=1), or read_file /
    search_files instead of piping.

    Args:
        command: Shell command to execute.
        cwd: Optional working directory.
        timeout: Timeout in seconds (capped at 120). Do not pass milliseconds.
        safe_mode: When True, block shell commands that appear to mutate files or Git state. Default False for LLM autonomy — the approval gate in tool_runtime handles safety instead.
    """
    if safe_mode and shell_is_dangerous(command):
        return (
            "Blocked by safe mode: command appears destructive or may mutate files/Git state. "
            "Toggle /safe only for an explicitly approved, narrower operation."
        )
    workdir = _resolve(cwd or ".", None)
    actual_timeout = max(0.001, min(float(timeout), 120.0))
    # Isolate the child in its own process group. Without this, every child
    # shares the CLI's console group, and any Ctrl+C/Ctrl+Break console event
    # raised inside the child tree (test runners, scripts, taskkill) is also
    # delivered to the CLI as a phantom KeyboardInterrupt mid-generation.
    popen_kwargs = _isolated_process_group_kwargs()
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_kwargs,
        )
        try:
            stdout, stderr = proc.communicate(timeout=actual_timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc)
            try:
                proc.communicate(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            return f"Error: command timed out after {actual_timeout:g} seconds; child processes were terminated."
        except KeyboardInterrupt:
            _terminate_process_tree(proc)
            try:
                proc.communicate(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            return "Error: command interrupted; child processes were terminated."
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {actual_timeout:g} seconds; child processes were terminated."
    except Exception as exc:
        return f"Error running command: {exc}"
    output = ""
    if stdout:
        output += stdout.strip()
    if stderr:
        output += ("\nSTDERR: " if output else "STDERR: ") + stderr.strip()
    if not output:
        output = "(command produced no output)"
    suffix = f"[exit code: {proc.returncode}]"
    body_limit = max(1, MAX_TOOL_RESULT - len(suffix) - 1)
    return f"{_cap(output, body_limit)}\n{suffix}"


def git_status(cwd: str | None = None) -> str:
    """Show concise Git working-tree status for the active project.

    Args:
        cwd: Optional project directory. Relative paths are not accepted by Git itself.
    """
    workdir = _resolve(cwd or ".", None)
    try:
        proc = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "Error: git status timed out after 20 seconds."
    except Exception as exc:
        return f"Error running git status: {exc}"
    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"Error: git status failed ({proc.returncode}): {output or 'no output'}"
    return _cap(output or "(clean working tree)")


def git_diff(path: str | None = None, cwd: str | None = None, names_only: bool = False) -> str:
    """Show the current tracked Git diff against HEAD.

    Args:
        path: Optional file or directory path filter within the project.
        cwd: Optional project directory.
        names_only: Return only changed tracked file names when true.
    """
    workdir = _resolve(cwd or ".", None)
    command = ["git", "diff", "--no-ext-diff"]
    if names_only:
        command.append("--name-only")
    command.append("HEAD")
    if path:
        command.extend(["--", path])
    try:
        proc = subprocess.run(command, cwd=workdir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        return "Error: git diff timed out after 20 seconds."
    except Exception as exc:
        return f"Error running git diff: {exc}"
    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"Error: git diff failed ({proc.returncode}): {output or 'no output'}"
    return _cap(output or "(no tracked diff)")


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web through Ollama Cloud, when configured.

    Args:
        query: Search query.
        max_results: Maximum results.
    """
    preflight = _ollama_cloud_web_preflight("searching web")
    if preflight:
        return preflight
    try:
        response = active_ollama_client(cloud=True).web_search(query, max_results=max_results)
    except Exception as exc:
        return f"Error searching web: {exc}. This usually requires ollama>=0.5 and OLLAMA_API_KEY."
    results = response.get("results", response) if isinstance(response, dict) else getattr(response, "results", response)
    if not results:
        return "No results found."
    rendered = []
    for result in results:
        title = result.get("title", "(untitled)") if isinstance(result, dict) else getattr(result, "title", "(untitled)")
        url = result.get("url", "") if isinstance(result, dict) else getattr(result, "url", "")
        content = result.get("content", "") if isinstance(result, dict) else getattr(result, "content", "")
        rendered.append(f"### {title}\n{url}\n{content}")
    return _cap("\n\n---\n\n".join(rendered))


def web_fetch(url: str, timeout: float = 30) -> str:
    """Fetch web page text through Ollama Cloud, when configured.

    Args:
        url: URL to fetch.
        timeout: Timeout seconds for the fetch operation.
    """
    preflight = _ollama_cloud_web_preflight("fetching URL")
    if preflight:
        return preflight
    import queue

    actual_timeout = max(0.001, min(float(timeout), 120.0))
    if not _WEB_FETCH_SLOTS.acquire(blocking=False):
        return (
            "Error fetching URL: too many previous fetches are still running after timeout; "
            "wait for them to finish before retrying."
        )

    def _fetch():
        try:
            result = active_ollama_client(cloud=True).web_fetch(url)
            content = result.get("content", "") if isinstance(result, dict) else getattr(result, "content", str(result))
            results.put(("ok", _cap(content)))
        except Exception as exc:
            results.put(("error", exc))
        finally:
            _WEB_FETCH_SLOTS.release()

    results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    thread = threading.Thread(target=_fetch, name="algo-cli-web-fetch", daemon=True)
    try:
        thread.start()
    except Exception as exc:
        _WEB_FETCH_SLOTS.release()
        return f"Error fetching URL: could not start worker: {exc}"
    try:
        status, payload = results.get(timeout=actual_timeout)
    except queue.Empty:
        return f"Error fetching URL: timed out after {actual_timeout:g} seconds."
    if status == "ok":
        return str(payload)
    fetch_error = payload
    if isinstance(fetch_error, Exception):
        return f"Error fetching URL: {fetch_error}. This usually requires ollama>=0.5 and OLLAMA_API_KEY."
    return f"Error fetching URL: {fetch_error}. This usually requires ollama>=0.5 and OLLAMA_API_KEY."


def x_search(query: str, max_results: int = 10) -> str:
    """Search X.com (Twitter) in real time via Grok's native Live Search.

    Requires a configured xAI API key (run ``algo-cli config setup xai``).
    Results are summarized by Grok and include citation URLs, then cached as a
    harness record so future turns can retrieve them via RAG. xAI requests may
    consume paid API usage.

    Args:
        query: What to search X.com for.
        max_results: Maximum number of source posts Grok considers (1-30).
    """
    from . import xai_auth, xai_client
    from .config import _resolve_config_dir

    if not xai_auth.get_valid_token():
        return "Error: xAI API key is not configured. Run `algo-cli config setup xai` first."
    if not query or not query.strip():
        return "Error: query is empty."

    try:
        result = xai_client.active_xai_client().search(
            query=query.strip(),
            sources=[{"type": "x"}],
            max_results=max(1, min(int(max_results), 30)),
        )
    except Exception as exc:
        return f"Error running x_search: {exc}"

    content = str(result.get("content", "") or "(Grok returned no summary.)")
    raw_citations = result.get("citations") or []
    citations = (
        [str(item).replace("\r", " ").replace("\n", " ")[:2048] for item in raw_citations]
        if isinstance(raw_citations, (list, tuple))
        else []
    )

    cache_dir = _resolve_config_dir() / "x_search_cache"
    # Use hash of full query to avoid collisions from truncation
    clean_query = " ".join(query.strip().split())[:500]
    query_hash = hashlib.sha256(query.strip().encode("utf-8")).hexdigest()[:16]
    ts = time.strftime("%Y%m%dT%H%M%S") + f".{int(time.time() * 1000) % 1000:03d}"
    path = cache_dir / f"x_search_{query_hash}_{ts}.md"

    body_lines = [
        "---",
        f"id: algo-cli:x_search:{query_hash}_{ts}",
        "harness: algo-cli",
        "kind: x_search",
        f"title: {json.dumps(f'X.com search: {clean_query}', ensure_ascii=False)}",
        "tags: [x_search, xai, realtime]",
        f"fetched_at: {ts}",
        "---",
        f"# X.com search: {clean_query}",
        "",
        content,
    ]
    if citations:
        body_lines.append("")
        body_lines.append("## Citations")
        for url in citations:
            body_lines.append(f"- {url}")
    cached = False
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, "\n".join(body_lines))
        cached = True
    except OSError as exc:
        logger.debug("x_search cache write failed for %s: %s", path, exc)

    out_parts = [content]
    if citations:
        out_parts.append("")
        out_parts.append("Sources:")
        out_parts.extend(f"- {url}" for url in citations[:max_results])
    if cached:
        out_parts.append("")
        out_parts.append(f"(cached to {path.name})")
    return _cap("\n".join(out_parts))


def x_account_status() -> str:
    """Check X account CLI auth status through xurl without reading token files.

    This uses the separate X account OAuth lane (api.x.com), not the xAI Grok API key.
    It never reads or prints ~/.xurl directly.
    """
    from . import x_account

    return x_account.status().to_json()


def x_account_draft_post(text: str) -> str:
    """Create a browser draft URL for an X post without publishing it.

    Args:
        text: Exact post text to draft.
    """
    from . import x_account

    return x_account.draft_post(text).to_json()


def x_account_draft_reply(post: str, text: str) -> str:
    """Create a browser draft URL for an X reply without publishing it.

    Args:
        post: X post id or x.com status URL to reply to.
        text: Exact reply text to draft.
    """
    from . import x_account

    return x_account.draft_reply(post, text).to_json()


def x_account_post(text: str, confirm: bool = False) -> str:
    """Publish an X post through xurl only after explicit user confirmation.

    Args:
        text: Exact post text to publish.
        confirm: Must be true only when the user explicitly approved this exact text.
    """
    from . import x_account

    return x_account.post(text, confirm=confirm).to_json()


def x_account_reply(post: str, text: str, confirm: bool = False) -> str:
    """Publish an X reply through xurl only after explicit user confirmation.

    Args:
        post: X post id or x.com status URL to reply to.
        text: Exact reply text to publish.
        confirm: Must be true only when the user explicitly approved this exact reply.
    """
    from . import x_account

    return x_account.reply(post, text, confirm=confirm).to_json()


def x_account_post_action(action: str, post: str, confirm: bool = False) -> str:
    """Run a confirmed X post action through xurl.

    Supported actions: delete, like, unlike, repost, unrepost, bookmark, unbookmark.

    Args:
        action: The action to run.
        post: X post id or x.com status URL.
        confirm: Must be true only when the user explicitly approved this exact action.
    """
    from . import x_account

    return x_account.post_action(action, post, confirm=confirm).to_json()


def remember(fact: str, cfg: Config | None = None) -> str:
    """Store a fact in long-term memory.

    Call only when the user explicitly asks to remember something. The runtime's
    bounded completion gate handles other high-confidence durable markers, so do
    not duplicate them with a speculative tool call. One concise sentence per
    explicit request.

    Args:
        fact: Fact to remember.
        cfg: Optional Config instance for persistence (required for actual storage).
    """
    if cfg is not None:
        added = cfg.remember_fact(fact)
        if added:
            from .main import capture_intuition_block
            capture_intuition_block(cfg, "memory", fact, source="tool:remember")
            return f"Remembered: {fact}"
        return f"Fact already in memory: {fact}"
    return f"Remembered: {fact} (no config provided - not persisted)"


def append_lesson(text: str, cfg: Config | None = None) -> str:
    """Append a lesson to lessons-learned.md so it is available in future turns.

    Call only when the user explicitly asks to retain a preference, correction,
    or pattern as a lesson. The lesson is timestamped and embedded for retrieval
    on the next turn. Do NOT use this for session notes, speculative capture, or
    to paraphrase the last message.

    Args:
        text: The lesson, written as a short paragraph. Be specific about the
            preference and, when useful, the reason.
        cfg: Optional Config instance for intuition capture (required for embedding).
    """
    if not text or not text.strip():
        return "Error: lesson text was empty."
    path = identity.append_lesson(text)
    if cfg is not None:
        from .main import capture_intuition_block
        capture_intuition_block(cfg, "lesson", text.strip(), source="tool:append_lesson")
    return f"Appended lesson to {path}"


def update_user_profile(content: str) -> str:
    """Overwrite USER.md (the 'About the User' identity file).

    Use this ONLY when the user explicitly asks you to update or rewrite their
    profile. Preserve existing structure unless they ask for a rewrite. Do NOT
    update USER.md to record session-specific facts; use append_lesson for those.

    Never modify SOUL.md or IDENTITY.md programmatically; only the user edits
    those by hand.

    Args:
        content: The full new contents of USER.md as Markdown. Include the
            existing sections (Who I am, How I work, etc.) unless the user
            asked for a different structure.
    """
    if not content or not content.strip():
        return "Error: refusing to overwrite USER.md with empty content."
    path = identity.write_user_profile(content)
    return f"Wrote {len(content)} chars to {path}"


def current_gateway_url(url: str | None = None) -> str:
    return (
        url
        or os.environ.get("ALGO_CLI_GATEWAY_URL")
        or os.environ.get("OLLAMA_CLI_GATEWAY_URL")
        or DEFAULT_GATEWAY_URL
    ).rstrip("/")


def gateway_ready(url: str | None = None) -> bool:
    url = current_gateway_url(url)
    try:
        request = Request(url + "/healthz", method="GET")
        with urlopen(request, timeout=1.0) as response:
            return 200 <= response.status < 500
    except (OSError, URLError, ValueError):
        return False


def gateway_embed(
    text: str,
    model: str,
    truncate: bool,
    dimensions: int | None,
    url: str | None = None,
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "model": model,
        "input": text,
        "truncate": truncate,
    }
    if dimensions is not None:
        payload["dimensions"] = dimensions
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        current_gateway_url(url) + "/supplemental/embed",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (OSError, URLError, ValueError):
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def gateway_embed_batch(
    texts: list[str],
    model: str,
    truncate: bool,
    dimensions: int | None,
    url: str | None = None,
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {
        "model": model,
        "input": list(texts),
        "truncate": truncate,
    }
    if dimensions is not None:
        payload["dimensions"] = dimensions
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        current_gateway_url(url) + "/supplemental/embed",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except (OSError, URLError, ValueError):
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None

def embed_text(
    text: str,
    model: str = "embeddinggemma",
    truncate: bool = True,
    dimensions: int | None = None,
) -> str:
    """Generate embeddings for text through Ollama."""
    try:
        response: Any = gateway_embed(text, model, truncate, dimensions)
        if response is None:
            response = active_ollama_client().embed(model=model, input=text, truncate=truncate, dimensions=dimensions)
    except Exception as exc:
        return f"Error generating embeddings: {exc}"
    payload = unpack_embed_response(
        response, model, text, truncate=truncate, dimensions=dimensions
    )
    return json.dumps(payload, indent=2)


def vision_describe(
    image_path: str,
    prompt: str = "What is in this image? Be concise.",
    model: str = "gemma3",
) -> str:
    """Describe an image through Ollama vision."""
    load_runtime_env(override=True)
    resolved = Path(image_path).expanduser()
    if not resolved.exists():
        return f"Error: image not found: {resolved}"
    if resolved.suffix.lower() == ".pdf":
        return "Error: vision_describe expects an image file, not a PDF. Use render_pdf_pages first, then pass a returned PNG path."
    try:
        response = active_ollama_client().chat(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                    "images": [str(resolved)],
                }
            ],
            stream=False,
        )
    except Exception as exc:
        return f"Error running vision request: {exc}"
    message = get_attr(response, "message", {}) or {}
    content = get_attr(message, "content", "")
    return content or "(empty response)"


def available_actions(topic: str | None = None) -> str:
    """Show the CLI's available commands, model-callable tools, and internal harness stats.

    Use this before answering questions like "what can you do?", "what actions are available?",
    "what tools do you have?", or "what internal knowledge can you search?"

    Args:
        topic: Optional focus area such as files, shell, web, memory, harness, verification, or models.
    """
    focus = (topic or "").strip().lower()
    commands = {
        "model": ["/model [NAME]", "/models", "/cloud [on|off|status]", "/cloudauto [on|off|status]", "/login", "/host URL"],
        "session": [
            "/help",
            "/status",
            "/info",
            "/clear",
            "/context [status|rebuild|clear]",
            "/save NAME",
            "/load NAME",
            "/theme NAME",
            "/mode [execute|explore|publish|status]",
            "/exit",
        ],
        "tools": ["/auto [on|off|status]", "/safe [on|off|status]", "/policy [on|off|status]", "/thinking [on|off|status|efforts|effort [MODEL] LEVEL]", "/verify [on|off|status]", "/ctx NUM", "/temp NUM", "/toolmax NUM", "/thinkevery NUM", "/cd PATH", "/route TASK"],
        "agent": [
            "/agent help",
            "/agent init",
            "/agent [--pipeline NAME] TASK",
            "/agent team [--roles ROLE,ROLE[,ROLE,ROLE]] TASK",
            "/agent threads",
            "/agent show THREAD",
            "/agent switch THREAD",
            "/agent resume THREAD [TASK]",
            "/agent fork THREAD [--same-worktree] TASK",
            "/route TASK",
            "/goal [--rounds N] TASK",
        ],
        "workspace": [
            "/worktree status",
            "/worktree list",
            "/worktree new NAME [--from REF]",
            "/worktree use ID_OR_NAME",
            "/worktree remove ID_OR_NAME",
            "/cd PATH",
            "/ship status",
            "/ship commit MESSAGE",
            "/ship push",
            "/ship pr [--ready]",
            "/ship all [--ready] MESSAGE",
        ],
        "reasoning": ["/reason status", "/reason guide", "/reason react", "/reason reflexion", "/reason tot", "/reason got", "/reason mcts", "/reason qcr", "/reason neuro_symbolic", "/reason depth N", "/reason branches N", "/reason auto-reflexion on|off", "/reason auto-verify on|off"],
        "xai": ["algo-cli config setup xai", "algo-cli config auth xai verify", "/model-check MODEL", "/x-account status"],
        "google": [
            "algo-cli config setup google",
            "algo-cli config auth google login",
            "/google drive-list [query] [--max N]",
            "/google drive-search NAME [--max N] [--mime MIME]",
            "/google drive-get FILE_ID [--download | --export MIME]",
            "/google docs-get DOCUMENT_ID",
            "/google sheets-values SPREADSHEET_ID RANGE",
            "/google calendar-list [--max N] [--time-min RFC3339] [--time-max RFC3339]",
            "/google gmail-list [query] [--max N] [--label LABEL]",
            "/google gmail-get MESSAGE_ID",
        ],
        "chatgpt": ["algo-cli config setup chatgpt", "algo-cli config auth chatgpt status", "algo-cli config auth chatgpt logout"],
        "multimodal": [
            "/embed [--model MODEL] [--file PATH] TEXT",
            "/vision [--model MODEL] [--prompt TEXT] IMAGE [QUESTION]",
        ],
        "documents": ["/pdf [--pages N] [--chars N] PATH"],
        "memory": [
            "/memory-auto [on|off|status]",
            "/remember FACT",
            "/memories",
            "/forget ID",
            "/intuition [on|off|status|add]",
        ],
        "intelligence": [
            "/intel status",
            "/intel query TERM",
            "/intel reindex",
            "/intel status|query TERM|reindex",
            "/intelligence status|query TERM|reindex",
            "/intelagence status|query TERM|reindex",
        ],
        "kernel": [
            "/kernel list",
            "/kernel show NAME",
            "/kernel check [NAME]",
        ],
        "harness": [
            "/code-rag [on|off|status]",
            "/harness status",
            "/harness refresh",
            "/harness embed",
            "/harness score",
            "/harness compare",
            "/hsearch QUERY",
            "/hread RECORD_ID",
            "/actions",
            "/selfcheck",
            "/reload",
        ],
    }
    tool_groups = {
        "files": [
            "read_file",
            "edit_file",
            "read_pdf",
            "render_pdf_pages",
            "write_file",
            "list_directory",
            "search_files",
            "find_unique_anchor",
            "batch_edit",
            "git_status",
            "git_diff",
            "session_slash",
        ],
        "session": [
            "session_slash: /read PATH, /ls [PATH], /cd PATH, /cwd (safe cwd file navigation)",
            "session_command: any registered slash command such as /status, /mode execute, /context status, /code-rag status, /harness status, /harness refresh, /harness embed, /harness score, /harness compare, /route TASK (session control; may require approval)",
        ],
        "shell": ["run_shell"],
        "web": ["web_search", "web_fetch"],
        "xai": ["x_search"],
        "x_account": [
            "x_account_status",
            "x_account_draft_post",
            "x_account_draft_reply",
            "x_account_post",
            "x_account_reply",
            "x_account_post_action",
        ],
        "memory": ["remember"],
        "multimodal": ["embed_text", "vision_describe"],
        "harness": [
            "available_actions",
            "harness_stats",
            "harness_scorecard",
            "harness_competitive_rating",
            "harness_search",
            "harness_read",
            "harness_refresh",
            "query_knowledge_graph",
            "reindex_knowledge_graph",
            "write_knowledge_graph_note",
        ],
        "models": ["model_show", "model_pull", "model_copy", "model_create", "model_delete"],
    }
    slash_guidance = [
        "Slash commands are session controls. Writing '/command' in a final answer does not execute it.",
        "Use session_slash for /read, /ls, /cd, and /cwd when you need deterministic cwd-relative file navigation.",
        "Use session_command for non-file slash commands only when the user asks for that action or session state must change/check before continuing; read-only/status commands such as /kernel list, /code-rag status, /harness status, /agent threads, /harness score, or /harness compare run without approval; state-changing commands such as /code-rag on, /code-rag off, /harness refresh, or /harness embed and agent execution require approval.",
        "Prefer direct tools for actual work: write_file for edits, run_shell for tests/builds, read_pdf/render_pdf_pages for PDFs, web_search/web_fetch for web.",
        "Prefer explicit on/off/status forms for toggles (/auto on, /safe off, /memory-auto status, /code-rag status, /thinking status, /verify on, /cloud off) so you do not accidentally flip state.",
        "For /reason, check /reason status or /reason guide first; only change reasoning mode for genuinely complex, failed, ambiguous, or verification-heavy work.",
        "For independent complex work, the parent runtime may invoke /agent team through session_command. Use 2-4 clear roles; specialists are read-only and one integration pipeline owns mutations and verification.",
        "Use /agent resume THREAD to continue the same context or /agent fork THREAD [--same-worktree] TASK to explore an isolated child follow-up. Do not delegate routine one-step work.",
        "Call available_actions('agent'), available_actions('kernel'), available_actions('intel'), available_actions('google'), available_actions('chatgpt'), available_actions('slash'), or available_actions('reason') when unsure which command/tool should be used.",
    ]
    reasoning_guidance = [
        "/reason status: inspect current reasoning mode, depth, branches, and auto flags; safe/read-only.",
        "/reason guide: show this mode-selection guide; safe/read-only.",
        "/reason react: default for normal ReAct tool-use loops, file inspection, small coding edits, and straightforward tasks.",
        "/reason reflexion: use after a failed, partial, or contradicted attempt when self-critique/retry discipline is needed.",
        "/reason tot: Tree-of-Thought; use for ambiguous planning/architecture decisions where several independent paths should be explored.",
        "/reason got: Graph-of-Thought; use when ideas/evidence interconnect, merge, or revise each other across a research/design problem.",
        "/reason mcts: Monte Carlo tree search; use for deeper exploration/exploitation when there are many possible action sequences.",
        "/reason qcr: quantum-inspired candidate aggregation; use to compare/rank multiple proposed solutions or reasoning fragments.",
        "/reason neuro_symbolic (or neuro-symbolic): use for verification-heavy logic, math, code invariants, contracts, or claim checking.",
        "/reason depth N and /reason branches N: adjust search cost; keep small unless the user asks for deeper exploration.",
        "/reason auto-reflexion on|off and /reason auto-verify on|off: enable automation for failed blocks or implementation verification only when the user wants that behavior.",
        "Do not switch /reason for routine reads, simple edits, or before every answer; mode changes are session state changes and may require approval.",
        "Changing /reason does not replace evidence gathering: still use read_file, search_files, run_shell, git_diff, or web/harness tools to verify facts.",
    ]
    verification_layer = [
        "Use harness_search before broad filesystem scans for skills, prompts, memory, wiki, and workflows.",
        "Use read_pdf for PDF text extraction. If it reports a scanned/image-only PDF, use render_pdf_pages next and then vision_describe on the returned PNG paths.",
        "Prefer edit_file (find/replace) over write_file for modifying existing files — it is faster, uses fewer tokens, and is less error-prone. Use write_file only for new files or full rewrites.",
        "When using edit_file, first call read_file to confirm the exact text, then include 2-5 lines of surrounding context so the match is unique. If the tool reports an ambiguous match, tighten old_string or pass replace_all=True when the rewrite is genuinely global.",
        "If edit_file fails with 'old_string not found' or 'matched N locations', call find_unique_anchor with the same needle to get a unique snippet with line numbers and context — then retry edit_file with that context.",
        "When the same file needs 2+ independent edits, call batch_edit with all of them at once instead of looping edit_file. One atomic write, faster, and you can see the whole change set in the result.",
        "Use run_shell for build/test/typecheck/lint/git verification after code changes.",
        "In requires_change Agent Blocks, use write_file or edit_file for file edits; mutating shell or Git commands require explicit approval.",
        "For Algo algorithm/pattern catalog guidance, read and update docs/ALGO.md.",
        "For harness maintenance, use harness_stats or /harness status to inspect quality, harness_scorecard or /harness score to grade readiness, harness_refresh or /harness refresh after source edits, and /harness embed to fill pending embeddings.",
        "Use web_search/web_fetch only when OLLAMA_API_KEY enables Ollama Cloud web access.",
        "Use x_search for real-time X.com content only after the user configured XAI_API_KEY with `algo-cli config setup xai`; xAI API calls may consume paid usage.",
        "Use x_account_* for X account actions through xurl; writes require explicit confirmation and separate X API OAuth.",
        "Treat memory/wiki as navigation; verify consequential facts against live files or endpoints.",
    ]
    stats = harness.stats()
    payload: dict[str, Any] = {
        "topic": focus or "all",
        "commands": commands,
        "model_callable_tools": tool_groups,
        "slash_command_guidance": slash_guidance,
        "reasoning_mode_guidance": reasoning_guidance,
        "verification_layer": verification_layer,
        "harness_index": {
            "record_count": stats.get("record_count"),
            "generated": stats.get("generated"),
            "counts": stats.get("counts", {}),
        },
    }
    if focus:
        slash_focus = focus in {"slash", "slashes", "command", "commands", "session-command", "session_command"}
        reason_focus = focus in {"reason", "reasoning", "reason-engine", "reasoning-engine"}
        matching: dict[str, Any] = {
            "commands": commands if slash_focus else {key: value for key, value in commands.items() if reason_focus and key == "reasoning" or focus in key or any(focus in item.lower() for item in value)},
            "model_callable_tools": {key: value for key, value in tool_groups.items() if slash_focus and key == "session" or focus in key or any(focus in item.lower() for item in value)},
        }
        if slash_focus:
            matching["when_to_use"] = slash_guidance
        if reason_focus:
            matching["reasoning_mode_guidance"] = reasoning_guidance
        payload["focused"] = matching
    return json.dumps(payload, indent=2)


def session_slash(command: str) -> str:
    """Execute a session slash command (same as TUI /read, /ls, /cd).

    Prefer this for deterministic reads when the user names files under session cwd.
    Allowed commands: /read PATH, /ls [PATH], /cd PATH, /cwd.

    Args:
        command: Full slash line, e.g. "/read PERMIT_ROLLOUT_PLAN.md" or "/ls".
    """
    return "Error: session_slash must be invoked by the algo CLI runtime (not called directly)."


_SESSION_OUTPUT_COMMANDS = frozenset({
    "/actions",
    "/changes",
    "/chatgpt-status",
    "/credentials",
    "/dashboard",
    "/diff",
    "/doctor",
    "/google-status",
    "/help",
    "/hread",
    "/hsearch",
    "/identity",
    "/info",
    "/memories",
    "/model-check",
    "/perf",
    "/route",
    "/selfcheck",
    "/status",
    "/ship",
    "/url-scheme",
    "/worktree",
    "/xai-status",
})
_SESSION_STATUS_COMMANDS = frozenset({
    "/auto",
    "/cloud",
    "/cloudauto",
    "/code-rag",
    "/context",
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
})
_SESSION_STATUS_ARGS = frozenset({"", "?", "guide", "help", "show", "status"})
_SESSION_EMPTY_ARG_TOGGLES = frozenset({
    "/auto",
    "/cloud",
    "/cloudauto",
    "/safe",
    "/thinking",
    "/verify",
})
_READ_ONLY_GOOGLE_SUBCOMMANDS = frozenset({
    "calendar-list",
    "docs-get",
    "drive-get",
    "drive-list",
    "drive-search",
    "gmail-get",
    "gmail-list",
    "help",
    "sheets-values",
})
_READ_ONLY_HARNESS_SUBCOMMANDS = frozenset({
    "",
    "?",
    "grade",
    "help",
    "quality",
    "rating",
    "score",
    "scorecard",
    "compare",
    "competitive",
    "stats",
    "status",
})
_SESSION_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|"
    r"client[_-]?secret|password)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
)
_SESSION_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;&}\]]+")


def _session_command_captures_output(command_line: str) -> bool:
    """Return whether a slash result is safe and useful to return to the model.

    Capture is intentionally narrower than the set of commands that the runtime
    can execute without approval. In particular, authentication and mutation
    routes must keep rendering only to the interactive console so OAuth URLs,
    callback values, and message bodies do not become model-visible tool output.
    """

    stripped = (command_line or "").strip()
    if not stripped:
        return False
    parts = stripped.split(maxsplit=1)
    root = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    normalized_arg = arg.lower()

    if root in _SESSION_OUTPUT_COMMANDS:
        return True
    if root in _SESSION_STATUS_COMMANDS:
        if root in _SESSION_EMPTY_ARG_TOGGLES and not normalized_arg:
            return False
        if normalized_arg in _SESSION_STATUS_ARGS:
            return True
    if root == "/harness":
        subcommand = normalized_arg.split(maxsplit=1)[0] if normalized_arg else ""
        return subcommand in _READ_ONLY_HARNESS_SUBCOMMANDS
    if root == "/google":
        subcommand = normalized_arg.split(maxsplit=1)[0] if normalized_arg else "help"
        return subcommand in _READ_ONLY_GOOGLE_SUBCOMMANDS
    if root == "/kernel":
        subcommand = normalized_arg.split(maxsplit=1)[0] if normalized_arg else "list"
        return subcommand in {"?", "check", "help", "list", "show"}
    if root in {"/intel", "/intelagence", "/intelligence"}:
        return normalized_arg in {"", "?", "guide", "help", "show", "status"} or normalized_arg.startswith("query ")
    if root == "/icl":
        return normalized_arg.startswith("ask ")
    if root == "/x-account":
        return normalized_arg == "status"
    if root == "/config":
        return normalized_arg in {"", "?", "help", "show", "status"}
    if root == "/plugins":
        subcommand = normalized_arg.split(maxsplit=1)[0] if normalized_arg else "list"
        return subcommand in {"?", "help", "list", "status"}
    return False


def _redact_session_command_output(output: str, *, workspace: str = "") -> str:
    """Remove common credential forms from captured slash-command output."""

    redacted = _SESSION_BEARER_RE.sub("Bearer <redacted>", str(output))
    redacted = _SESSION_SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", redacted)
    # Read-only slash output can be returned to a cloud model. Preserve useful
    # logical locations without disclosing the user's home or an overridden
    # config directory. Interactive console output remains unchanged.
    prefixes = [
        (str(CONFIG_DIR), "$ALGO_CLI_CONFIG_DIR"),
        (str(Path.home()), "~"),
    ]
    if workspace:
        prefixes.append((str(Path(workspace).expanduser().resolve()), "$WORKSPACE"))
    for prefix, replacement in sorted(
        prefixes,
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if prefix:
            redacted = re.sub(re.escape(prefix), replacement, redacted, flags=re.IGNORECASE)
    return redacted


def _captured_session_result(output: str, normalized: str, *, workspace: str = "") -> str:
    rendered = _redact_session_command_output(output, workspace=workspace).strip()
    # Rich error output begins with the product glyph. Normalize it so the
    # runtime's existing error classifier still recognizes failed tool calls.
    first_line, separator, remainder = rendered.partition("\n")
    marker = " Error: "
    if marker in first_line and first_line.index(marker) <= 3:
        first_line = f"Error: {first_line.split(marker, 1)[1]}"
        rendered = first_line + (separator + remainder if separator else "")
    if not rendered:
        return f"Executed: {normalized} (command produced no output)."
    truncation_marker = "\n...[truncated]"
    if len(rendered) > SESSION_COMMAND_OUTPUT_LIMIT:
        return rendered[: SESSION_COMMAND_OUTPUT_LIMIT - len(truncation_marker)] + truncation_marker
    return rendered


def _direct_read_only_session_result(command_line: str) -> str | None:
    """Return source payloads for read-only routes that already expose strings.

    Sending JSON through ``Rich Console.print`` can insert display-width line
    breaks inside quoted values. Returning these tool payloads directly keeps
    `/harness status`, `/harness score`, and `/actions` machine-parseable while
    the capture path remains available for slash handlers with no return value.
    """
    parts = (command_line or "").strip().split(maxsplit=1)
    if not parts:
        return None
    root = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if root == "/actions":
        return available_actions(arg or None)
    if root == "/hread":
        return harness_read(arg) if arg else "Error: Usage: /hread <record-id>"
    if root != "/harness":
        return None
    harness_parts = arg.split(maxsplit=1)
    subcommand = harness_parts[0].lower() if harness_parts else ""
    if len(harness_parts) > 1:
        return None
    if subcommand in {"", "status", "stats", "quality"}:
        return harness_stats()
    if subcommand in {"score", "scorecard", "grade", "rating"}:
        return harness_scorecard()
    if subcommand in {"compare", "competitive"}:
        return harness_competitive_rating()
    return None


def session_command(command: str, cfg: Any = None) -> str:
    """Execute an Algo CLI slash command as a tool call.

    Use this for session control when the user asks for it or the next step depends
    on CLI state. Do not call it just to perform normal work: use write_file for
    edits, run_shell for tests/builds, and session_slash for /read, /ls, /cd, /cwd.
    If you merely type a slash command in a final answer, it will not execute.

    Prefer idempotent commands with explicit state when available:
    - /status, /info — inspect model, cwd, context, and active toggles
    - /cloud on|off|status, /auto on|off|status, /safe on|off|status
    - /thinking on|off|status|efforts|effort [MODEL] LEVEL, /verify on|off|status, /policy on|off|status
    - /mode execute|explore|publish|status — switch/check session mode
    - /reason status|guide — inspect reasoning posture and mode-selection guidance
    - /reason react|reflexion|tot|got|mcts|qcr|neuro_symbolic|hybrid — set reasoning posture only for complex/failed/ambiguous/verification-heavy work
    - /reason depth N, /reason branches N — reasoning search-cost parameters
    - /agent [--pipeline NAME] TASK — run a traceable agent pipeline for larger tasks
    - /agent team [--roles ROLE,ROLE[,ROLE,ROLE]] TASK — run 2-4 independent read-only specialists, then one verified integration pipeline
    - /agent threads, /agent show THREAD — inspect persistent parent/child run records
    - /agent resume THREAD [TASK], /agent fork THREAD [--same-worktree] TASK — continue or create an isolated child from prior verified context
    - /route TASK — preview route/budget/tool policy without running a pipeline
    - /kernel list, /kernel show NAME — inspect promoted kernel specs without executing workloads
    - /kernel check [NAME] — validate imports, slash routes, metadata, and active ActionSpecs
    - /code-rag on|off|status — opt in/out of cwd source indexing and prompt retrieval
    - /harness status, /harness refresh, /harness embed, /harness score, /harness compare, /hsearch QUERY, /hread ID — harness index
    - /intelligence status|query TERM|reindex or /intel ... — repository intelligence project graph
    - /remember FACT, /memories, /forget ID, /lesson TEXT, /lessons reindex
    - /context status|rebuild|clear — context management
    - /save NAME, /load NAME — conversation persistence
    - /temp N, /ctx N, /toolmax N, /thinkevery N — model parameters
    - /diff, /changes — git/agent activity
    - /skills, /intuition, /icl, /reflex on|off|status — knowledge/loop controls
    - /theme NAME, /embed, /vision, /pdf, /reload

    Args:
        command: Full slash command line, e.g. "/status" or "/mode execute".
        cfg: Runtime Config (injected automatically, do not set).
    """
    if cfg is None:
        return "Error: session_command must be invoked by the algo CLI runtime (not called directly)."
    normalized = command.strip()
    from .slash_dispatch import handle_command, unknown_command_message
    from .runtime_services import create_client
    if normalized.lower() == "/agent" or normalized.lower().startswith("/agent "):
        from .agent_pipeline import agent_execution_active, execute_agent_command

        if agent_execution_active():
            return "Error: recursive /agent delegation is blocked while an Agent Blocks run is active."
        client = create_client(cfg)
        arg = normalized[len("/agent"):].strip()
        return _captured_session_result(
            execute_agent_command(arg, cfg, client),
            normalized,
            workspace=cfg.cwd,
        )
    direct_result = _direct_read_only_session_result(normalized)
    if direct_result is not None:
        return _captured_session_result(direct_result, normalized, workspace=cfg.cwd)
    client = create_client(cfg)
    try:
        if _session_command_captures_output(normalized):
            # Capture is context-local rather than a process stdout redirect, so
            # direct interactive slash commands and unrelated threads keep their
            # existing console behavior.
            from . import display as display_module

            with display_module.capture_console_output() as capture:
                handled, _client = handle_command(normalized, cfg, client)
            if not handled:
                return unknown_command_message(normalized)
            return _captured_session_result(capture.get(), normalized, workspace=cfg.cwd)
        handled, _client = handle_command(normalized, cfg, client)
        if not handled:
            return unknown_command_message(normalized)
        return f"Executed: {normalized}"
    except EOFError:
        return "Executed: exit command (session ended)."
    except Exception as exc:
        return f"Error executing {normalized}: {exc}"


def harness_refresh() -> str:
    """Refresh the local harness index for skills, tools, prompts, memories, and wiki pages."""
    index = harness.load_index(refresh=True)
    indexer = str(index.get("indexer") or "python")
    refresh_stats = index.get("refresh_stats", {})
    if indexer == "rust":
        refresh_detail = "Rust full rebuild."
    else:
        refresh_detail = (
            f"Reused: {refresh_stats.get('reused_records', 0)}, "
            f"rebuilt: {refresh_stats.get('rebuilt_records', 0)}, "
            f"removed: {refresh_stats.get('removed_records', 0)}."
        )
    embeddings = index.get("embeddings") or harness._embeddings_summary(index.get("records", []) or [])
    embedded_count = int(embeddings.get("embedded_count", 0))
    pending_count = int(embeddings.get("pending_count", 0))
    total = embedded_count + pending_count
    active_model = embeddings.get("active_model") or harness.DEFAULT_EMBED_MODEL
    if total == 0:
        embed_detail = "Embeddings: no records to embed."
    elif embeddings.get("complete"):
        embed_detail = f"Embeddings: {active_model} ({embedded_count}/{total} ready, embedded by {embeddings.get('embedded_by', 'python')})."
    else:
        embed_detail = (
            f"Embeddings: {active_model} ({embedded_count}/{total} ready, {pending_count} pending). "
            f"Run /harness embed or wait for the next chat turn to fill them in."
        )
    return (
        f"Refreshed harness index at {harness.INDEX_PATH}. "
        f"Indexer: {indexer}. Records: {index.get('record_count', 0)}. {refresh_detail} "
        f"{embed_detail}"
    )


def harness_stats() -> str:
    """Show counts for indexed Codex, Claude, OpenClaw, Mercury, Pi, and shared harness assets."""
    return json.dumps(harness.stats(), indent=2)


def _scorecard_check(
    name: str,
    status: str,
    evidence: str,
    recommendation: str = "",
    *,
    critical: bool = False,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check: dict[str, Any] = {
        "name": name,
        "status": status,
        "evidence": evidence,
        "recommendation": recommendation,
        "critical": critical,
    }
    if metrics is not None:
        check["metrics"] = metrics
    return check


def _object_status(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("status") or value.get("overall_status") or "")
    return str(getattr(value, "status", getattr(value, "overall_status", "")) or "")


def _collect_harness_index_integrity() -> dict[str, Any]:
    """Validate the persisted index contract from raw records, not status labels."""
    try:
        index = harness.load_index()
        raw_records = index.get("records", [])
        records = [record for record in raw_records if isinstance(record, dict)]
        ids = [str(record.get("id") or "") for record in records]
        declared_count = int(index.get("record_count", -1))
        embedding_dimensions: dict[str, set[int]] = {}
        malformed_embeddings = 0
        for record in records:
            raw_embedding = record.get("embedding")
            if raw_embedding is None:
                continue
            embedding_model = str(record.get("embedding_model") or "").strip()
            if (
                not isinstance(raw_embedding, list)
                or not raw_embedding
                or not embedding_model
            ):
                malformed_embeddings += 1
                continue
            try:
                finite = all(math.isfinite(float(value)) for value in raw_embedding)
            except (TypeError, ValueError):
                finite = False
            if not finite:
                malformed_embeddings += 1
                continue
            embedding_dimensions.setdefault(embedding_model, set()).add(len(raw_embedding))
        required_fields_present = all(
            record.get("id") and record.get("harness") and record.get("kind") and record.get("path")
            for record in records
        )
        checks = {
            "nonempty": bool(records),
            "record_count_matches": declared_count == len(records) == len(raw_records),
            "unique_ids": bool(ids) and len(ids) == len(set(ids)) and all(ids),
            "required_fields_present": required_fields_present,
            "embedding_vectors_well_formed": malformed_embeddings == 0,
            "embedding_dimensions_consistent": all(
                len(dimensions) == 1 for dimensions in embedding_dimensions.values()
            ),
            "generated_present": bool(str(index.get("generated") or "").strip()),
            "roots_present": bool(index.get("roots")),
            "source_current": not harness.index_is_stale(allow_cached=True),
        }
        fingerprint_rows = [
            (
                record.get("id"),
                record.get("file_size"),
                record.get("file_mtime_ns"),
                record.get("embedding_model"),
                len(record.get("embedding") or []),
            )
            for record in records
        ]
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_rows, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        failed = sorted(name for name, passed in checks.items() if not passed)
        return {
            "status": "pass" if not failed else "fail",
            "record_count": len(records),
            "declared_count": declared_count,
            "fingerprint": fingerprint,
            "embedding_dimensions": {
                model: sorted(dimensions)
                for model, dimensions in sorted(embedding_dimensions.items())
            },
            "malformed_embeddings": malformed_embeddings,
            "checks": checks,
            "failed": failed,
        }
    except Exception as exc:
        return {
            "status": "error",
            "record_count": 0,
            "fingerprint": "",
            "checks": {},
            "failed": [type(exc).__name__],
        }


def harness_scorecard() -> str:
    """Run the evidence-backed v2 harness scorecard and return structured JSON.

    Ten scored gates are worth one point each. A 10/10 therefore requires
    current index/embedding evidence, retrieval correctness, a repeatable
    benchmark, and proof that every required production algorithm actually ran.
    Optional cloud/Web and Google availability remain visible but unscored so a
    healthy local-first runtime is not penalized for intentionally absent creds.
    """
    from . import action_registry
    from .evals.algorithm_effectiveness import (
        PROBE_NAME,
        PROBE_SCHEMA_VERSION,
        REQUIRED_CHECKS,
        run_algorithm_effectiveness_probe,
    )
    from .evals.harness_retrieval_benchmark import (
        BENCHMARK_VERSION,
        MAX_WARM_MAD_RATIO,
        MIN_REUSABLE_SPEEDUP,
        run_harness_retrieval_benchmark,
    )
    from .evals.scorecard_grading import finalize_scorecard

    checks: list[dict[str, Any]] = []
    capabilities: list[dict[str, Any]] = []

    integrity = _collect_harness_index_integrity()
    integrity_status = str(integrity.get("status") or "error")
    checks.append(
        _scorecard_check(
            "index integrity",
            integrity_status,
            (
                f"records={integrity.get('record_count', 0)} "
                f"fingerprint={integrity.get('fingerprint') or '-'} "
                f"failed={integrity.get('failed', [])}"
            ),
            "Refresh the index and repair duplicate/malformed/stale records." if integrity_status != "pass" else "",
            critical=True,
            metrics=integrity,
        )
    )

    stats_error = ""
    try:
        stats = harness.stats()
    except Exception as exc:
        stats = {}
        stats_error = type(exc).__name__
    quality_value = stats.get("quality")
    embeddings_value = stats.get("embeddings")
    echo_value = stats.get("echo_veil")
    runtime_store_value = stats.get("runtime_event_store")
    quality = quality_value if isinstance(quality_value, dict) else {}
    embeddings = embeddings_value if isinstance(embeddings_value, dict) else {}
    echo_readiness = echo_value if isinstance(echo_value, dict) else {}
    runtime_store = runtime_store_value if isinstance(runtime_store_value, dict) else {}

    embedding_fields = (
        stats.get("record_count"),
        embeddings.get("embedded_count"),
        embeddings.get("pending_count"),
        embeddings.get("high_value_pending"),
    )
    if stats_error or any(value is None for value in embedding_fields):
        status = "error" if stats_error else "unavailable"
        total = embedded_count = pending_count = high_value_pending = 0
    else:
        total = int(embedding_fields[0] or 0)
        embedded_count = int(embedding_fields[1] or 0)
        pending_count = int(embedding_fields[2] or 0)
        high_value_pending = int(embedding_fields[3] or 0)
        counts_consistent = embedded_count + pending_count == total
        if (
            total > 0
            and counts_consistent
            and embedded_count == total
            and pending_count == 0
            and high_value_pending == 0
            and embeddings.get("complete") is True
        ):
            status = "pass"
        elif embedded_count > 0 and counts_consistent and high_value_pending == 0:
            status = "warn"
        else:
            status = "fail"
    checks.append(
        _scorecard_check(
            "embedding readiness",
            status,
            (
                f"model={embeddings.get('active_model') or '-'} "
                f"embedded={embedded_count}/{total} pending={pending_count} "
                f"high_value_pending={high_value_pending}"
            ),
            "Run /harness embed until active-model and high-value coverage are complete." if status != "pass" else "",
            metrics={
                "total": total,
                "embedded": embedded_count,
                "pending": pending_count,
                "high_value_pending": high_value_pending,
                "complete": embeddings.get("complete"),
            },
        )
    )

    memory_value = quality.get("memory_records")
    curated_memory_value = quality.get("curated_product_memory_records")
    wiki_value = quality.get("wiki_records")
    required_value = quality.get("required_product_memory_categories")
    covered_value = quality.get("covered_product_memory_categories")
    missing_value = quality.get("missing_product_memory_categories")
    echo_fields_ready = all(
        key in echo_readiness
        for key in (
            "installed",
            "enabled",
            "write_wired",
            "retrieval_wired",
            "persistence_wired",
            "readiness_source",
            "runtime",
        )
    )
    echo_enabled = bool(echo_readiness.get("enabled"))
    echo_stages = {
        "write": bool(echo_readiness.get("write_wired")),
        "retrieval": bool(echo_readiness.get("retrieval_wired")),
        "persistence": bool(echo_readiness.get("persistence_wired")),
    }
    echo_safe = (
        not echo_enabled
        or (
            bool(echo_readiness.get("installed"))
            and all(echo_stages.values())
        )
    )
    runtime_store_fields_ready = all(
        key in runtime_store
        for key in (
            "status",
            "initialized",
            "directory_private",
            "file_private",
            "lock_private",
            "compaction_needed",
        )
    )
    runtime_store_safe = bool(
        runtime_store_fields_ready
        and runtime_store.get("status") in {"ready", "empty"}
        and runtime_store.get("directory_private") is True
        and runtime_store.get("lock_private") is True
        and (
            runtime_store.get("initialized") is not True
            or runtime_store.get("file_private") is True
        )
        and runtime_store.get("compaction_needed") is False
    )
    if (
        memory_value is None
        or curated_memory_value is None
        or wiki_value is None
        or not isinstance(required_value, list)
        or not isinstance(covered_value, list)
        or not isinstance(missing_value, list)
        or not echo_fields_ready
        or not runtime_store_fields_ready
    ):
        status = "unavailable"
        memory_records = curated_memory_records = wiki_records = 0
        required_categories: list[str] = []
        covered_categories: list[str] = []
        missing_categories: list[str] = []
    else:
        memory_records, wiki_records = int(memory_value), int(wiki_value)
        curated_memory_records = int(curated_memory_value)
        required_categories = [str(item) for item in required_value]
        covered_categories = [str(item) for item in covered_value]
        missing_categories = [str(item) for item in missing_value]
        required_set = set(required_categories)
        covered_set = set(covered_categories)
        category_coverage = len(required_set & covered_set)
        if not echo_safe or not runtime_store_safe:
            status = "fail"
        elif (
            required_set
            and required_set <= covered_set
            and not missing_categories
            and curated_memory_records >= len(required_set)
            and wiki_records >= 5
        ):
            status = "pass"
        elif category_coverage >= max(1, len(required_set) - 1) and wiki_records >= 2:
            status = "warn"
        else:
            status = "fail"
    checks.append(
        _scorecard_check(
            "project memory/wiki coverage",
            status,
            (
                f"product_memory={memory_records} curated={curated_memory_records} "
                f"categories={len(covered_categories)}/{len(required_categories)} "
                f"missing={missing_categories} wiki={wiki_records} "
                f"echo_enabled={echo_enabled} echo_stages={echo_stages} "
                f"private_store={runtime_store.get('status') or 'unknown'}"
            ),
            (
                "Cover every required product-memory category with a curated contract, "
                "retain at least five curated wiki records, and disable Echo Veil until "
                "its write/retrieval/persistence stages are all operational. Keep the runtime "
                "event store private, bounded, and compact; then refresh."
                if status != "pass"
                else ""
            ),
            critical=True,
            metrics={
                "product_memory_records": memory_records,
                "curated_product_memory_records": curated_memory_records,
                "required_categories": required_categories,
                "covered_categories": covered_categories,
                "missing_categories": missing_categories,
                "wiki_records": wiki_records,
                "echo_installed": bool(echo_readiness.get("installed")),
                "echo_enabled": echo_enabled,
                "echo_stages": echo_stages,
                "echo_readiness_source": echo_readiness.get("readiness_source"),
                "echo_runtime": echo_readiness.get("runtime"),
                "runtime_event_store": runtime_store,
            },
        )
    )

    extension_value = quality.get("extension_share")
    project_value = quality.get("project_specific_share")
    if extension_value is None or project_value is None:
        status = "unavailable"
        extension_share = project_share = 0.0
    else:
        extension_share, project_share = float(extension_value), float(project_value)
        if extension_share <= 0.5 and project_share >= 0.2:
            status = "pass"
        elif extension_share <= 0.7 and project_share >= 0.1:
            status = "warn"
        else:
            status = "fail"
    checks.append(
        _scorecard_check(
            "corpus signal balance",
            status,
            f"project_share={project_share:.3f} extension_share={extension_share:.3f}",
            "Increase curated project signal or reduce generic extension dominance." if status != "pass" else "",
        )
    )

    try:
        meta_results = harness.search_index("rate your harness", limit=5)
        meta_ids = [str(record.get("id", "")) for record in meta_results if isinstance(record, dict)]
        if meta_ids and meta_ids[0] == "algo-cli:algorithm:ALGO.md":
            status = "pass"
        elif "algo-cli:algorithm:ALGO.md" in meta_ids:
            status = "warn"
        else:
            status = "fail"
    except Exception as exc:
        meta_ids = []
        status = "error"
        meta_ids.append(type(exc).__name__)
    checks.append(
        _scorecard_check(
            "meta-query retrieval",
            status,
            f"top_ids={meta_ids[:5]}",
            "Ensure the reviewed ALGO record is the stable top self-evaluation result." if status != "pass" else "",
            critical=True,
        )
    )

    try:
        kg_text = str(query_knowledge_graph("rate your harness"))
        canonical_match = re.search(r"(?<![\w-])project:algo-cli(?![\w-])", kg_text) is not None
        if canonical_match:
            status = "pass"
        elif "No matching canonicals" in kg_text:
            status = "fail"
        else:
            status = "warn"
    except Exception as exc:
        kg_text = type(exc).__name__
        status = "error"
    checks.append(
        _scorecard_check(
            "knowledge graph",
            status,
            kg_text[:240],
            "Reindex the graph and verify the exact project:algo-cli canonical." if status != "pass" else "",
        )
    )

    try:
        audit = action_registry.audit_action_registry_runtime()
        audit_status = str(getattr(audit, "overall_status", "") or "")
        status = "pass" if audit_status == "ready" else "fail"
    except Exception as exc:
        audit_status = type(exc).__name__
        status = "error"
    checks.append(
        _scorecard_check(
            "action registry runtime audit",
            status,
            f"overall_status={audit_status or 'unknown'}",
            "Run /selfcheck and repair ActionSpec/tool/slash coverage." if status != "pass" else "",
            critical=True,
        )
    )

    required_harness_commands = {
        "/harness status",
        "/harness refresh",
        "/harness embed",
        "/harness score",
        "/harness compare",
    }
    try:
        harness_actions = json.loads(available_actions("harness"))
        harness_commands = set(harness_actions.get("commands", {}).get("harness", []))
        missing_harness_commands = sorted(required_harness_commands - harness_commands)
        direct_status = _direct_read_only_session_result("/harness status")
        status_payload = json.loads(direct_status or "")
        payload_contract = isinstance(status_payload, dict) and isinstance(status_payload.get("embeddings"), dict)
        capture_contract = all(
            _session_command_captures_output(command)
            for command in ("/harness score", "/harness compare")
        )
        status = "pass" if not missing_harness_commands and payload_contract and capture_contract else "fail"
    except Exception as exc:
        harness_commands = set()
        missing_harness_commands = sorted(required_harness_commands)
        payload_contract = capture_contract = False
        status = "error"
        direct_status = type(exc).__name__
    checks.append(
        _scorecard_check(
            "harness maintenance loop",
            status,
            (
                f"commands={sorted(required_harness_commands & harness_commands)} "
                f"payload_json={payload_contract} capture={capture_contract}"
            ),
            "Restore maintenance commands and machine-parseable read-only payloads." if status != "pass" else "",
        )
    )

    try:
        benchmark = run_harness_retrieval_benchmark()
        status = str(benchmark.get("status") or "error")
        correctness_value = benchmark.get("correctness")
        performance_value = benchmark.get("performance")
        evidence_value = benchmark.get("evidence")
        correctness = correctness_value if isinstance(correctness_value, dict) else {}
        performance = performance_value if isinstance(performance_value, dict) else {}
        benchmark_evidence = evidence_value if isinstance(evidence_value, dict) else {}
        try:
            measured_speedup = float(str(performance.get("speedup")))
            measured_mad_ratio = float(str(performance.get("warm_mad_ratio")))
        except (TypeError, ValueError):
            measured_speedup = measured_mad_ratio = math.nan
        benchmark_contract = (
            benchmark.get("benchmark_version") == BENCHMARK_VERSION
            and correctness.get("passed") is True
            and correctness.get("stable_rankings") is True
            and math.isfinite(measured_speedup)
            and measured_speedup >= MIN_REUSABLE_SPEEDUP
            and math.isfinite(measured_mad_ratio)
            and measured_mad_ratio <= MAX_WARM_MAD_RATIO
            and bool(benchmark_evidence.get("index_digest"))
        )
        if status == "pass" and not benchmark_contract:
            status = "fail"
            benchmark["scorecard_contract_error"] = "pass payload lacked required correctness/performance evidence"
    except Exception as exc:
        benchmark = {"status": "error", "reason": type(exc).__name__}
        status = "error"
    checks.append(
        _scorecard_check(
            "retrieval benchmark",
            status,
            json.dumps(benchmark, sort_keys=True, default=str)[:1000],
            "Repair retrieval correctness or investigate the measured reusable-index regression." if status != "pass" else "",
            critical=True,
            metrics=benchmark,
        )
    )

    try:
        algorithm_report = run_algorithm_effectiveness_probe()
        status = str(algorithm_report.get("status") or "error")
        probe_checks_value = algorithm_report.get("checks")
        probe_summary_value = algorithm_report.get("summary")
        probe_checks = probe_checks_value if isinstance(probe_checks_value, dict) else {}
        probe_summary = probe_summary_value if isinstance(probe_summary_value, dict) else {}
        required_checks = set(REQUIRED_CHECKS)
        algorithm_contract = (
            algorithm_report.get("schema_version") == PROBE_SCHEMA_VERSION
            and algorithm_report.get("probe") == PROBE_NAME
            and set(algorithm_report.get("required_checks") or ()) == required_checks
            and required_checks <= set(probe_checks)
            and all(
                isinstance(probe_checks.get(name), dict)
                and probe_checks[name].get("status") == "pass"
                and probe_checks[name].get("required") is True
                for name in required_checks
            )
            and int(probe_summary.get("required") or 0) == len(required_checks)
            and int(probe_summary.get("passed") or 0) == len(required_checks)
            and int(probe_summary.get("failed") or 0) == 0
        )
        if status == "pass" and not algorithm_contract:
            status = "fail"
            algorithm_report["scorecard_contract_error"] = "pass payload lacked every required production-path check"
    except Exception as exc:
        algorithm_report = {"status": "error", "reason": type(exc).__name__}
        status = "error"
    checks.append(
        _scorecard_check(
            "algorithm effectiveness",
            status,
            json.dumps(algorithm_report, sort_keys=True, default=str)[:1000],
            "Fix the failing production-path algorithm probe before claiming readiness." if status != "pass" else "",
            critical=True,
            metrics=algorithm_report,
        )
    )

    try:
        doctor = action_registry.build_doctor_report(Config())
        doctor_findings = getattr(doctor, "findings", ()) or ()
        web_status = ""
        web_message = "web-tools finding missing"
        for finding in doctor_findings:
            area = finding.get("area") if isinstance(finding, dict) else getattr(finding, "area", "")
            if area == "web-tools":
                web_status = _object_status(finding)
                value = finding.get("message") if isinstance(finding, dict) else getattr(finding, "message", "")
                web_message = str(value or "")
                break
        capability_status = "pass" if web_status == "ready" else ("warn" if web_status else "unavailable")
    except Exception as exc:
        capability_status = "error"
        web_message = type(exc).__name__
    capabilities.append(_scorecard_check("web tools", capability_status, web_message))

    try:
        google_actions = json.loads(available_actions("google"))
        google_commands = set(google_actions.get("commands", {}).get("google", []))
        required_google_fragments = (
            "algo-cli config setup google", "algo-cli config auth google login", "/google drive-list",
            "/google gmail-list", "/google docs-get", "/google sheets-values", "/google calendar-list",
        )
        missing_google = [
            fragment for fragment in required_google_fragments
            if not any(command.startswith(fragment) for command in google_commands)
        ]
        capability_status = "pass" if not missing_google else "warn"
    except Exception as exc:
        google_commands = set()
        missing_google = [type(exc).__name__]
        capability_status = "error"
    capabilities.append(
        _scorecard_check(
            "google workspace wiring",
            capability_status,
            f"commands={len(google_commands)} missing={missing_google}",
        )
    )

    return json.dumps(finalize_scorecard(checks, capabilities=capabilities), indent=2)


def harness_competitive_rating() -> str:
    """Run local evidence probes and grade the attached cross-harness comparison.

    The report deliberately cannot declare Algo CLI the leader without
    revision-pinned competitor artifacts and a same-workload protocol. Local
    benchmark and algorithm receipts are still included so the missing proof is
    distinguishable from a local regression.
    """
    from . import git_evidence
    from .evals.algorithm_effectiveness import run_algorithm_effectiveness_probe
    from .evals.competitive_harness_rating import (
        SUBJECT_PROJECT,
        build_competitive_harness_report,
        recompute_comparative_rating,
    )
    from .evals.harness_retrieval_benchmark import run_harness_retrieval_benchmark

    rating = recompute_comparative_rating()
    projects = [str(row["project"]) for row in rating.get("ranking", [])]

    try:
        benchmark = run_harness_retrieval_benchmark()
    except Exception as exc:
        benchmark = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    benchmark_json = json.dumps(benchmark, sort_keys=True, default=str)
    performance = benchmark.get("performance") if isinstance(benchmark, dict) else {}
    if not isinstance(performance, dict):
        performance = {}
    local_run_count = min(
        int(performance.get("cold_sample_count") or 0),
        int(performance.get("warm_sample_count") or 0),
    )

    try:
        algorithms = run_algorithm_effectiveness_probe()
    except Exception as exc:
        algorithms = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    algorithm_json = json.dumps(algorithms, sort_keys=True, default=str)
    required = [str(item) for item in algorithms.get("required_checks", [])]
    raw_checks = algorithms.get("checks")
    checks = raw_checks if isinstance(raw_checks, dict) else {}
    receipts = {
        name: "sha256:"
        + hashlib.sha256(
            json.dumps(checks.get(name, {}), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        for name in required
    }
    summary = algorithms.get("summary") if isinstance(algorithms, dict) else {}
    if not isinstance(summary, dict):
        summary = {}

    snapshot = git_evidence.capture_git_snapshot()
    clean = git_evidence.snapshot_is_clean(snapshot) if snapshot.available else None
    local_verification_digest = "sha256:" + hashlib.sha256(
        f"{benchmark_json}\n{algorithm_json}".encode("utf-8")
    ).hexdigest()
    local_evidence: dict[str, Any] = {
        "benchmark": {
            "status": str(benchmark.get("status") or "error"),
            "protocol": str(benchmark.get("benchmark_version") or "local-retrieval-benchmark"),
            "artifact_digest": "sha256:" + hashlib.sha256(benchmark_json.encode("utf-8")).hexdigest(),
            "projects": [SUBJECT_PROJECT],
            "runs_per_project": local_run_count,
        },
        "algorithms": {
            "status": str(algorithms.get("status") or "error"),
            "probe": str(algorithms.get("probe") or "algorithm-effectiveness"),
            "artifact_digest": "sha256:" + hashlib.sha256(algorithm_json.encode("utf-8")).hexdigest(),
            "required_checks": len(required),
            "passed_checks": int(summary.get("passed") or 0),
            "algorithm_ids": required,
            "production_receipts": receipts,
        },
        "release": {
            "status": (
                "pass"
                if clean is True
                and benchmark.get("status") == "pass"
                and algorithms.get("status") == "pass"
                else "fail"
            ),
            "commit": snapshot.head or "",
            "verification_artifact": local_verification_digest,
        },
    }
    report = build_competitive_harness_report(
        evidence=local_evidence,
        worktree_clean=clean,
        competitor_evidence_complete=False,
    )
    report["local_probe_artifacts"] = {
        "retrieval_benchmark": benchmark,
        "algorithm_effectiveness": algorithms,
        "warning": (
            "Local evidence is not cross-harness evidence. Leadership remains blocked until every "
            f"project ({', '.join(projects)}) is revision-pinned and run under one protocol."
        ),
    }
    return json.dumps(report, indent=2, sort_keys=True)


def harness_search(query: str, harness_name: str | None = None, kind: str | None = None, limit: int = 10) -> str:
    """Search local harness assets.

    Args:
        query: Search terms.
        harness_name: Optional harness filter: codex, claude, openclaw, openclaude, mercury, pi, agents.
        kind: Optional kind filter: skill, tool, prompt, memory, wiki, workflow, extension.
        limit: Maximum records.
    """
    results = harness.search_index(query, harness_name, kind, limit)
    if not results:
        return "No harness matches."
    lines = []
    for record in results:
        lines.append(
            f"- {record['id']}\n"
            f"  title: {record['title']}\n"
            f"  path: {record['path']}\n"
            f"  summary: {record.get('description') or record.get('summary', '')[:220]}"
        )
    return "\n".join(lines)


def harness_read(record_id: str, max_chars: int = 20_000) -> str:
    """Read one indexed harness asset by id from harness_search.

    Args:
        record_id: Exact record id returned by harness_search.
        max_chars: Maximum characters to return.
    """
    return harness.read_record(record_id, _bounded_int(max_chars, 20_000, 1, 50_000))


_KG_HARNESS_META_TERMS = {
    "assess",
    "audit",
    "capabilities",
    "capability",
    "evaluate",
    "evaluation",
    "grade",
    "rate",
    "rating",
    "score",
    "selfcheck",
}


def _knowledge_graph_query_expansion(question: str) -> str | None:
    terms = {
        term.lower()
        for term in re.findall(r"[\w.-]+", question or "")
        if len(term) > 1
    }
    if "harness" in terms and terms & _KG_HARNESS_META_TERMS:
        return "Algo CLI harness self-evaluation capability audit"
    return None


def query_knowledge_graph(question: str, limit: int = 10) -> str:
    """Query the local index-compute-lab ranked association graph.

    Returns co-occurring entities and relationship counts (not prose bios).
    For biography-style questions, use harness_search on user-configured sources
    and verify important details against live files or authoritative sources.

    Args:
        question: Natural-language question about entities, projects, or relationships.
        limit: Maximum ranked neighbors or results to return, between 1 and 20.
    """
    text = (question or "").strip()
    if not text:
        return "Error: knowledge graph question was empty."
    output = _index_compute_lab.run_ask(text, limit=limit, timeout=20.0)
    if output and "no matching canonicals" in output.lower():
        expanded = _knowledge_graph_query_expansion(text)
        if expanded and expanded != text:
            fallback = _index_compute_lab.run_ask(expanded, limit=limit, timeout=20.0)
            if fallback and "no matching canonicals" not in fallback.lower() and not fallback.startswith("Error:"):
                output = fallback
    return _cap(output or "No knowledge graph results.")


def reindex_knowledge_graph(
    include_removable_seed: bool = False,
    removable_scope: str | None = None,
) -> str:
    """Rebuild index-compute-lab ranked graph (association → normalize → rank).

    Use only when the user explicitly asks to rebuild configured graph sources.
    include_removable_seed runs removable_drive_atoms.py first; pass removable_scope
    to limit the scan to one user-provided path.
    After completion, run harness_refresh in the same session.

    Args:
        include_removable_seed: When true, re-seed removable-drive atoms before the pipeline.
        removable_scope: Optional single --scope path for removable_drive_atoms.py.
    """
    scopes = [removable_scope.strip()] if removable_scope and removable_scope.strip() else None
    output = _index_compute_lab.run_pipeline(
        include_removable_seed=include_removable_seed,
        removable_scopes=scopes,
    )
    return _cap(output)


def write_knowledge_graph_note(title: str, body: str) -> str:
    """Write a markdown note under index-compute-lab/atoms/agent-notes/ for harness RAG.

    Use when the user states a fact that should be retrievable before the next full reindex
    (contacts, project aliases, corrections). Follow with harness_refresh.

    Args:
        title: Short note title (used as filename slug).
        body: Markdown body (one or more paragraphs).
    """
    return _index_compute_lab.write_graph_note(title, body)


def model_pull(name: str) -> str:
    """Pull a model from the Ollama registry.

    Args:
        name: Model name to pull, e.g. 'llama3.2' or 'nomic-embed-text'.
    """
    client = active_ollama_client()
    try:
        last_status = ""
        for progress in client.pull(name, stream=True):
            status = getattr(progress, "status", None) or (progress.get("status") if isinstance(progress, dict) else None)
            if status:
                last_status = str(status)
        return f"Pulled {name}: {last_status or 'complete'}"
    except Exception as exc:
        return f"Error pulling {name}: {exc}"


def model_delete(name: str) -> str:
    """Delete a local Ollama model. This is irreversible and requires approval.

    Args:
        name: Exact model name to delete, e.g. 'llama3.2:latest'.
    """
    client = active_ollama_client()
    try:
        client.delete(name)
        return f"Deleted model: {name}"
    except Exception as exc:
        return f"Error deleting {name}: {exc}"


def _parse_modelfile(modelfile: str) -> dict[str, Any]:
    """Translate common Modelfile directives to the current Ollama SDK API."""
    directives: list[tuple[str, str, int]] = []
    lines = modelfile.splitlines()
    index = 0
    while index < len(lines):
        line_number = index + 1
        stripped = lines[index].strip()
        index += 1
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z]+)(?:\s+(.*))?$", stripped)
        if match is None or not (match.group(2) or "").strip():
            raise ValueError(f"invalid Modelfile instruction on line {line_number}")
        instruction = match.group(1).upper()
        value = (match.group(2) or "").strip()
        if value.startswith('"""'):
            first_segment = value[3:]
            segments = [first_segment]
            while True:
                closing = segments[-1].find('"""')
                if closing >= 0:
                    trailing = segments[-1][closing + 3 :].strip()
                    segments[-1] = segments[-1][:closing]
                    if trailing and not trailing.startswith("#"):
                        raise ValueError(
                            f"unexpected text after multiline value on line {index}"
                        )
                    break
                if index >= len(lines):
                    raise ValueError(
                        f"unterminated multiline {instruction} starting on line {line_number}"
                    )
                segments.append(lines[index])
                index += 1
            value = "\n".join(segments)
            if not first_segment and value.startswith("\n"):
                value = value[1:]
            value = value.rstrip("\n")
        directives.append((instruction, value, line_number))

    create_args: dict[str, Any] = {}
    parameters: dict[str, Any] = {}
    messages: list[dict[str, str]] = []
    licenses: list[str] = []
    for instruction, value, line_number in directives:
        if instruction == "FROM":
            if "from_" in create_args:
                raise ValueError(f"duplicate FROM instruction on line {line_number}")
            create_args["from_"] = value
        elif instruction in {"SYSTEM", "TEMPLATE"}:
            key = instruction.lower()
            if key in create_args:
                raise ValueError(
                    f"duplicate {instruction} instruction on line {line_number}"
                )
            create_args[key] = value
        elif instruction == "PARAMETER":
            parts = value.split(maxsplit=1)
            if len(parts) != 2:
                raise ValueError(f"invalid PARAMETER instruction on line {line_number}")
            parameter_name, raw_value = parts
            try:
                parameter_value: Any = json.loads(raw_value)
            except json.JSONDecodeError:
                parameter_value = raw_value
            existing = parameters.get(parameter_name)
            if parameter_name not in parameters:
                parameters[parameter_name] = parameter_value
            elif isinstance(existing, list):
                existing.append(parameter_value)
            else:
                parameters[parameter_name] = [existing, parameter_value]
        elif instruction == "MESSAGE":
            parts = value.split(maxsplit=1)
            if len(parts) != 2:
                raise ValueError(f"invalid MESSAGE instruction on line {line_number}")
            role, content = parts
            messages.append({"role": role.lower(), "content": content})
        elif instruction == "LICENSE":
            licenses.append(value)
        else:
            raise ValueError(
                f"unsupported Modelfile instruction {instruction!r} on line {line_number}"
            )

    if not create_args.get("from_"):
        raise ValueError("Modelfile requires a FROM instruction")
    if parameters:
        create_args["parameters"] = parameters
    if messages:
        create_args["messages"] = messages
    if licenses:
        create_args["license"] = licenses[0] if len(licenses) == 1 else licenses
    return create_args


def model_create(name: str, modelfile: str) -> str:
    """Create a custom Ollama model from a Modelfile string.

    Use this to bake a persona, system prompt, or parameter set into a reusable
    local model. Requires approval.

    Args:
        name: Name for the new model, e.g. 'my-assistant:latest'.
        modelfile: Full Modelfile content (FROM, SYSTEM, PARAMETER lines).
    """
    try:
        create_args = _parse_modelfile(modelfile)
        client = active_ollama_client()
        last_status = ""
        for progress in client.create(
            model=name,
            from_=create_args.get("from_"),
            template=create_args.get("template"),
            license=create_args.get("license"),
            system=create_args.get("system"),
            parameters=create_args.get("parameters"),
            messages=create_args.get("messages"),
            stream=True,
        ):
            status = get_attr(progress, "status", "")
            if status:
                last_status = str(status)
        return f"Created model {name}: {last_status or 'complete'}"
    except Exception as exc:
        return f"Error creating {name}: {exc}"


def model_copy(source: str, destination: str) -> str:
    """Copy a local Ollama model to a new name.

    Args:
        source: Source model name.
        destination: Destination model name.
    """
    client = active_ollama_client()
    try:
        client.copy(source, destination)
        return f"Copied {source} → {destination}"
    except Exception as exc:
        return f"Error copying {source} to {destination}: {exc}"


def model_show(name: str) -> str:
    """Show detailed metadata for a local Ollama model.

    Returns context length, architecture, parameter count, quantization, and capability flags.

    Args:
        name: Model name to inspect.
    """
    client = active_ollama_client()
    try:
        response = client.show(name)
    except Exception as exc:
        return f"Error showing {name}: {exc}"
    details = getattr(response, "details", None) or {}
    raw_info = getattr(response, "model_info", None) or {}
    if not isinstance(raw_info, dict):
        try:
            raw_info = dict(raw_info)
        except Exception:
            raw_info = {}
    ctx_length = None
    for key, val in raw_info.items():
        if key.endswith(".context_length") and isinstance(val, int):
            ctx_length = val
            break
    family = getattr(details, "family", None) or (details.get("family") if isinstance(details, dict) else None)
    param_size = getattr(details, "parameter_size", None) or (details.get("parameter_size") if isinstance(details, dict) else None)
    quant = getattr(details, "quantization_level", None) or (details.get("quantization_level") if isinstance(details, dict) else None)
    fmt = getattr(details, "format", None) or (details.get("format") if isinstance(details, dict) else None)
    payload = {
        "name": name,
        "family": str(family or ""),
        "parameter_size": str(param_size or ""),
        "quantization": str(quant or ""),
        "context_length": ctx_length,
        "format": str(fmt or ""),
    }
    return json.dumps(payload, indent=2)



def _hide_cfg_param(fn):
    """Remove the ``cfg`` parameter from the Ollama tool-call schema.

    ``remember`` and ``append_lesson`` accept a runtime-injected ``cfg``
    (Config instance) that the model should never pass.  Without this
    wrapper the Ollama SDK tries to build a Pydantic model from
    ``Config | None`` and crashes with "not fully defined".
    """
    original_sig = inspect.signature(fn)
    params = [p for name, p in original_sig.parameters.items() if name != "cfg"]
    fn.__signature__ = original_sig.replace(parameters=params)
    return fn


# ---------------------------------------------------------------------------
# Plugin / version / credential / URL-scheme tool wrappers
# ---------------------------------------------------------------------------
# Thin wrappers that expose the four new subsystems as model-callable tools.
# The heavy logic lives in the dedicated modules; these just bridge the
# tool-calling interface.


def plugins_discover() -> str:
    """Discover plugins from ~/.algo_cli/plugins/ and return a JSON summary."""
    from .plugins import discover_plugins
    return json.dumps([manifest.as_dict() for manifest in discover_plugins()], indent=2, sort_keys=True)


def plugins_load(plugin_name: str) -> str:
    """Explicitly import a discovered plugin and return its module load status.

    Dynamic action/tool registration is not yet part of the stable runtime API.
    """
    from .plugins import discover_plugins, load_plugin

    manifest = next(
        (item for item in discover_plugins() if item.name.lower() == plugin_name.strip().lower()),
        None,
    )
    if manifest is None:
        return json.dumps({"loaded": False, "error": f"Plugin not found: {plugin_name}"})
    result = load_plugin(manifest)
    return json.dumps(result.as_dict(), indent=2, sort_keys=True)


def version_manifest_build() -> str:
    """Build a version manifest with CLI, Python, platform, harness, and plugin versions."""
    from .version_manifest import build_manifest
    m = build_manifest()
    return json.dumps(m.as_dict(), indent=2, sort_keys=True)


def extensions_manifest_build() -> str:
    """Build an extension manifest with plugin/helper binary versions and status."""
    from .extensions_manifest import build_extensions_manifest
    m = build_extensions_manifest()
    return m.to_json()


def runtime_qos_hint(tool_name: str, args_json: str = "{}") -> str:
    """Classify a tool call's runtime QoS and named log destination."""
    from .runtime_qos import classify_tool_runtime
    try:
        args = json.loads(args_json or "{}")
        if not isinstance(args, dict):
            args = {}
    except Exception:
        args = {}
    return json.dumps(classify_tool_runtime(tool_name, args).to_dict(), indent=2, sort_keys=True)


def screenshot_description_verify(description: str, expected_terms: str = "", forbidden_terms: str = "") -> str:
    """Verify a screenshot description against expected and forbidden comma-separated terms."""
    from .vision_screenshot_verify import verify_screenshot_description
    expected = [term.strip() for term in (expected_terms or "").split(",") if term.strip()]
    forbidden = [term.strip() for term in (forbidden_terms or "").split(",") if term.strip()]
    return json.dumps(verify_screenshot_description(description, expected, forbidden).to_dict(), indent=2, sort_keys=True)


def capability_mask_describe(tier: str = "", capabilities: str = "") -> str:
    """Describe a stable capability bit mask from a tier and/or comma-separated capability names."""
    from .capability_mask import CapabilityMask, mask_from_names, tier_mask
    names = [name.strip() for name in (capabilities or "").split(",") if name.strip()]
    mask = CapabilityMask(tier_mask(tier) | mask_from_names(names).value)
    return json.dumps(mask.to_dict(), indent=2, sort_keys=True)


def small_context_ledger_preview(model: str, runtime_cap: int, blocks_json: str = "[]") -> str:
    """Preview whether the small-context ledger path would activate for a model/window."""
    from .small_context import preview_small_context_ledger
    return preview_small_context_ledger(model, runtime_cap, blocks_json)


def credential_helpers_get(helper: str, key: str) -> str:
    """Check a named helper for a credential without returning its plaintext value."""
    from .credential_helpers import get_credential
    val = get_credential(helper, key)
    return json.dumps(
        {
            "helper": helper,
            "key": key,
            "found": val is not None,
            "value": "<redacted>" if val is not None else None,
        }
    )


def credential_helpers_store(helper: str, key: str, value: str) -> str:
    """Store a credential value by key via a named credential helper."""
    from .credential_helpers import store_credential
    stored = store_credential(helper, key, value)
    return json.dumps({"helper": helper, "key": key, "stored": stored})


def url_scheme_parse(url: str) -> str:
    """Parse an algo-cli:// deep link into an action descriptor."""
    from .url_scheme import handle_deep_link

    result = handle_deep_link(url)
    return json.dumps(result, indent=2, sort_keys=True)


def action_search(query: str, limit: int = 6) -> str:
    """Discover relevant deferred actions and return their exact schemas.

    Use this when the small visible tool set does not contain a needed action.
    Discovery does not bypass runtime policy or approval. Execution still goes
    through action_program, ActionSpec policy, runtime guardrails, and per-action
    approvals within the active runtime-owned capability ceiling.

    Args:
        query: Capability or operation to find, such as "render a PDF" or "store a credential".
        limit: Maximum matching action schemas to return (1-12).
    """

    from .action_registry import get_action_spec
    from .tool_context import rank_tools_for_prompt
    from .tool_schema import serialized_tool_schemas

    normalized_query = str(query or "").strip()
    if not normalized_query:
        return json.dumps({"status": "error", "error": "query must not be empty"})
    bounded_limit = max(1, min(12, int(limit)))
    excluded = {"action_search", "action_program", "session_command", "session_slash"}
    candidates = [fn for name, fn in TOOL_MAP.items() if name not in excluded]
    ranked = rank_tools_for_prompt(normalized_query, candidates)[:bounded_limit]
    actions: list[dict[str, Any]] = []
    for fn in ranked:
        name = str(getattr(fn, "__name__", "") or "")
        try:
            wire_schema = json.loads(serialized_tool_schemas([fn]))[0]
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            wire_schema = {"type": "function", "function": {"name": name}}
        try:
            spec = get_action_spec(name)
            policy: dict[str, Any] = {
                "risk": spec.risk_level,
                "mutates_state": spec.mutates_state,
                "requires_approval": spec.requires_approval,
                "safe_retry": spec.safe_retry,
            }
        except KeyError:
            policy = {
                "risk": "unknown",
                "mutates_state": None,
                "requires_approval": None,
                "safe_retry": None,
            }
        actions.append({"name": name, "schema": wire_schema, "policy": policy})
    return json.dumps(
        {
            "status": "ok",
            "query": normalized_query,
            "count": len(actions),
            "actions": actions,
            "next": "Call action_program with a bounded typed plan; discovery does not bypass runtime policy or approval.",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def action_program(plan: dict, cfg: Any = None) -> str:
    """Compile and execute a bounded typed action plan without evaluating code.

    Plans use version 1 and ordered steps. An action step has ``id``,
    ``kind: action``, ``action``, and ``args``. A transform step has ``id``,
    ``kind: transform``, ``op``, ``input``, and optional ``args``. References
    use ``{"$ref": "earlier_step", "path": ["optional", "keys"]}``.
    Supported deterministic transforms are returned by validation errors.
    Large intermediates become content-addressed artifacts; every nested action
    retains its normal policy, approval, guardrail, attempt-ledger, and telemetry
    path. Session/meta calls and recursive programs are forbidden.

    Args:
        plan: Typed version-1 plan object with bounded ordered steps and outputs.
    """

    if cfg is None:
        return json.dumps({"status": "error", "error": "runtime config was not injected"})
    from .program_runtime import ProgramAuthorization, execute_program

    authorization = getattr(cfg, "_algo_program_authorization", None)
    if not isinstance(authorization, ProgramAuthorization):
        return json.dumps(
            {"status": "error", "error": "runtime program authorization was not bound"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    try:
        result = execute_program(plan, cfg, authorization=authorization)
    except Exception as exc:
        return json.dumps(
            {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return json.dumps(result.to_dict(compact=True), ensure_ascii=False, separators=(",", ":"))


ALL_TOOLS = [
    read_file,
    edit_file,
    read_pdf,
    render_pdf_pages,
    write_file,
    list_directory,
    search_files,
    find_unique_anchor,
    batch_edit,
    run_shell,
    git_status,
    git_diff,
    web_search,
    web_fetch,
    x_search,
    x_account_status,
    x_account_draft_post,
    x_account_draft_reply,
    x_account_post,
    x_account_reply,
    x_account_post_action,
    _hide_cfg_param(remember),
    _hide_cfg_param(append_lesson),
    update_user_profile,
    embed_text,
    vision_describe,
    action_search,
    _hide_cfg_param(action_program),
    available_actions,
    session_slash,
    _hide_cfg_param(session_command),
    harness_refresh,
    harness_stats,
    harness_scorecard,
    harness_competitive_rating,
    harness_search,
    harness_read,
    query_knowledge_graph,
    reindex_knowledge_graph,
    write_knowledge_graph_note,
    model_pull,
    model_delete,
    model_create,
    model_copy,
    model_show,
    plugins_discover,
    plugins_load,
    version_manifest_build,
    extensions_manifest_build,
    runtime_qos_hint,
    screenshot_description_verify,
    capability_mask_describe,
    small_context_ledger_preview,
    credential_helpers_get,
    credential_helpers_store,
    url_scheme_parse,
]
TOOL_MAP = {fn.__name__: fn for fn in ALL_TOOLS}
