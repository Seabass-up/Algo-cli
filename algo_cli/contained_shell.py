"""Workspace-only macOS commands while Continuum remains protected.

Default-deny OS enforcement, not command-text filtering, protects memory and
credentials from interpreters, build scripts and subprocesses. No unsafe fallback.
"""
import json
import math
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

import certifi

from .irene_memory_path_policy import ProtectedMemoryPathError, protected_path_rules, require_allowed_path
from .search_execution import run_search_process

MAX_OUTPUT = 65_536
MAX_GROUP_PROCESSES = 32
WORKER = Path(__file__).with_name("contained_shell_worker.py")
# These data names are denied even within the selected workspace/tool runtimes.
def secret_pattern() -> str:
    from .execution_guardrails import _SENSITIVE_COMPONENTS, _SENSITIVE_FILENAMES, _SENSITIVE_SUFFIXES
    # VCS observations have typed tools; Git config may contain embedded tokens.
    components = _SENSITIVE_COMPONENTS
    names = sorted({*_SENSITIVE_FILENAMES, *components, ".npmrc", ".pypirc", ".git-credentials"})
    literal_names = "|".join(re.escape(name) for name in names)
    suffixes = "|".join(re.escape(suffix) for suffix in sorted(_SENSITIVE_SUFFIXES))
    pattern = r"/(" + literal_names + r"|[.]env[.][^/]*|[^/]*(" + suffixes + r"))(/|$)|/[.]git/config[^/]*$"
    # Seatbelt regexes have no portable inline case-fold flag.
    return "".join(f"[{char.lower()}{char.upper()}]" if char.isalpha() else char for char in pattern)


def workspace(cfg: Any) -> Path:
    root = require_allowed_path(getattr(cfg, "cwd", None), cwd=os.getcwd())
    home = Path.home().resolve()
    if not root.is_dir() or root == home or root in home.parents:
        raise ProtectedMemoryPathError("select an explicit project directory, not home or a system root")
    return root


def preflight(cfg: Any) -> str | None:
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file() or not WORKER.is_file():
        return "Error: protected command execution requires the qualified macOS containment runner."
    try:
        workspace(cfg)
    except (OSError, ValueError, ProtectedMemoryPathError):
        return "Error: protected commands require an explicit nonprotected, unaliased project directory."
    return None


def _tool_roots(root: Path) -> tuple[list[Path], list[str]]:
    paths = [str(root / ".venv/bin"), *os.environ.get("PATH", "").split(os.pathsep)]
    roots = [Path(p) for p in ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/share",
             "/System/Library", "/System/Volumes/Preboot/Cryptexes/OS", "/System/Cryptexes/OS",
             "/opt/homebrew/Cellar", "/opt/homebrew/lib/node_modules", "/usr/local/lib/node_modules",
             "/Library/Developer/CommandLineTools",
             "/Applications/Xcode.app/Contents/Developer", "/Library/Frameworks/Python.framework")]
    roots.extend([Path(sys.base_prefix), Path(sys.prefix), Path.home() / ".local/share/uv/python",
                  Path.home() / ".local/share/uv/tools"])
    filtered = []
    standard_bins = {Path(p) for p in ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin",
                     "/usr/local/sbin", "/opt/homebrew/bin", "/opt/homebrew/sbin")}
    standard_bins.add(Path.home() / ".local/bin")
    rules = protected_path_rules()
    for value in dict.fromkeys(paths):
        path = Path(value) if value else Path(".")
        if not path.is_absolute() or not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved in {Path("/"), Path.home(), Path.home().parent} or rules.denies(resolved):
            continue
        # PATH is not a grant to read arbitrary personal directories. Admit
        # conventional tool bins, project bins and already-admitted runtimes.
        if not (path in standard_bins or resolved == root or root in resolved.parents or
                any(resolved == approved or approved in resolved.parents for approved in roots)):
            continue
        roots.extend((path, resolved))
        filtered.append(value)
    return sorted(set(roots)), filtered


def workspace_aliases(root: Path, deadline: float | None = None, cancel_event: Any = None) -> list[Path]:
    """Bounded metadata-only inventory, without interpreting any workspace code."""
    from .execution_guardrails import _SENSITIVE_COMPONENTS
    rules = protected_path_rules()
    command = ["/usr/bin/find", "-P", str(root), "("]
    predicates: list[str] = []
    for name in sorted(_SENSITIVE_COMPONENTS):
        if predicates:
            predicates.append("-o")
        predicates.extend(("-name", name))
    # Configured deny roots may be inside a project. Prune before traversing;
    # find's default nofollow mode never follows an input symlink.
    for protected in rules.roots:
        if protected.startswith(str(root) + "/"):
            if predicates:
                predicates.append("-o")
            # Escape find -path glob metacharacters; this is not shell quoting.
            escaped = protected.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")
            predicates.extend(("-path", escaped))
    command += [*predicates, ")", "-prune", "-o", "("]
    identities = sorted({value[1] for value in rules.identities})
    for index, inode in enumerate(identities):
        if index:
            command.append("-o")
        command.extend(("-inum", str(inode)))
    command += ([")", "-print0", "-prune", "-o", "("] if identities else ["-false", ")", "-o", "("])
    command += ["-type", "f", "-links", "+1",
                "-o", "!", "-type", "d", "!", "-type", "f", "!", "-type", "l", ")", "-print0"]
    remaining = max(.001, deadline - time.monotonic()) if deadline is not None else 30
    result = run_search_process(
        command, limit=100000, max_bytes=2 * 1024 * 1024, timeout=remaining,
        process_kwargs={"env": {"LC_ALL": "C"}, "shell": False, "start_new_session": True,
                        "close_fds": True}, terminate=_kill_group, cancel_event=cancel_event)
    if result.returncode or result.truncated or result.timed_out or result.cancelled or result.stderr_truncated:
        raise OSError("workspace alias inventory unavailable within limits")
    denied = [Path(os.fsdecode(raw)) for raw in result.stdout.split(b"\0") if raw]
    if len(denied) > 4096 or any(not p.is_relative_to(root) for p in denied):
        raise OSError("workspace alias inventory exceeds qualified bound")
    return denied


def profile(root: Path, scratch: Path, reads: list[Path], aliases: list[Path] | None = None) -> str:
    def quoted(path: Any) -> str:
        return json.dumps(str(path), ensure_ascii=True)
    ancestors = sorted({p for path in [root, scratch, *reads] for p in path.parents if str(p) != "/"})
    result = ["(version 1)", "(deny default)", "(deny file-link)", "(allow process-fork)",
              "(allow signal (target same-sandbox))",
              # No process-info-setcontrol grant: children cannot escape the
              # isolated process group using setsid/setpgid, including via exec.
              '(allow sysctl-read (sysctl-name-regex #"^(hw[.].*|kern[.](ostype|osrelease|osversion|osrevision|osproductversion|hostname|version|argmax|osvariant_status))$"))',
              '(allow file-read* (literal "/") (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom") (subpath "/dev/fd"))',
              '(allow file-write* (literal "/dev/null") (subpath "/dev/fd"))',
              "(allow file-read* process-exec " + " ".join(f"(subpath {quoted(p)})" for p in [root, scratch, *reads]) + ")",
              "(allow file-read-metadata " + " ".join(f"(literal {quoted(p)})" for p in ancestors) + ")",
              '(allow file-read-metadata (subpath "/opt/homebrew/opt"))',
              "(allow file-write* " + " ".join(f"(subpath {quoted(p)})" for p in [root, scratch]) + ")",
              f"(deny file-write* (subpath {quoted(Path(sys.prefix))}))",
              f"(deny file-read-data file-write* (require-all (regex {quoted(secret_pattern())}) (require-not (subpath {quoted(scratch)}))))"]
    if aliases:
        result.append("(deny file-read* file-write* process-exec " +
                      " ".join(f"(subpath {quoted(alias)})" for alias in aliases) + ")")
        parents = set()
        for alias in aliases:
            for ancestor in alias.parents:
                parents.add(ancestor)
                if ancestor == root:
                    break
        result.append("(deny file-write* " + " ".join(f"(literal {quoted(p)})" for p in sorted(parents)) + ")")
    for protected in protected_path_rules().roots:
        result.append(f"(deny file-read* file-write* process-exec (subpath {quoted(protected)}))")
    for executable in ("/usr/bin/sudo", "/bin/su", "/usr/bin/security", "/bin/launchctl"):
        result.append(f"(deny process-exec (literal {quoted(executable)}))")
    return "\n".join(result) + "\n"


def _kill_group(proc: Any) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _processes(*, uid: bool, group: int = 0) -> int:
    command = ["/bin/ps", "-u", str(os.getuid()), "-o", "pid="] if uid else [
        "/bin/ps", "-g", str(group), "-o", "pid="]
    result = subprocess.run(command, capture_output=True, timeout=3, check=False)
    if result.returncode not in {0, 1}:
        raise OSError("process inventory unavailable")
    return len(result.stdout.splitlines())


def run(command: str, cfg: Any, timeout: float, cancel_event: Any = None) -> str:
    reason = preflight(cfg)
    if reason:
        return reason
    if not isinstance(command, str) or not command.strip() or "\x00" in command or len(command) > 32768:
        return "Error: invalid contained command."
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 120:
        return "Error: timeout must be finite seconds in (0, 120]."
    from .tools import DENY_COMMAND_RE, shell_is_dangerous
    if DENY_COMMAND_RE.search(command) or (bool(getattr(cfg, "safe_mode", True)) and shell_is_dangerous(command)):
        return "Blocked by safe mode/destructive-action policy; containment does not grant a policy waiver."
    if cancel_event is not None and cancel_event.is_set():
        return "Error: command was not run because the agent team was cancelled."
    try:
        root = workspace(cfg)
        parent = Path(tempfile.gettempdir()).resolve()
        require_allowed_path(parent, cwd=parent)
        deadline = time.monotonic() + timeout
        reads, path = _tool_roots(root)
        aliases = workspace_aliases(root, deadline, cancel_event)
        with tempfile.TemporaryDirectory(prefix="algo-command-", dir=parent) as name:
            scratch = Path(name)
            for directory in ("home", "tmp", "cache", "config"):
                (scratch / directory).mkdir(mode=0o700)
            from .ssea_sandbox import regular_reader
            # Public, installed trust anchors are not user credentials. Give
            # clients an isolated bundle without admitting arbitrary .pem files.
            ca_source = require_allowed_path(certifi.where(), cwd=root)
            with regular_reader(ca_source) as stream:
                public_ca = stream.read(2 * 1024 * 1024 + 1)
            if len(public_ca) > 2 * 1024 * 1024:
                return "Error: installed public CA bundle exceeds the qualified bound."
            ca_bundle = scratch / "public-ca.pem"
            ca_bundle.write_bytes(public_ca)
            (scratch / "config/npm-user").write_text("")
            (scratch / "config/npm-global").write_text("")
            policy = scratch / "command.sb"
            policy.write_text(profile(root, scratch, reads, aliases), encoding="utf-8")
            env = {"HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"),
                   "PATH": os.pathsep.join(path), "LANG": "en_US.UTF-8", "TERM": "dumb",
                   "XDG_CACHE_HOME": str(scratch / "cache"), "XDG_CONFIG_HOME": str(scratch / "config"),
                   "PYTHONPYCACHEPREFIX": str(scratch / "pycache"), "PYTHONDONTWRITEBYTECODE": "1",
                   "UV_CACHE_DIR": str(scratch / "uv-cache"), "UV_OFFLINE": "1", "PIP_NO_INDEX": "1",
                   "PIP_CONFIG_FILE": "/dev/null", "NPM_CONFIG_OFFLINE": "true", "OPENSSL_CONF": "/dev/null",
                   "SSL_CERT_FILE": str(ca_bundle), "REQUESTS_CA_BUNDLE": str(ca_bundle),
                   "NPM_CONFIG_CACHE": str(scratch / "npm-cache"),
                   "NPM_CONFIG_USERCONFIG": str(scratch / "config/npm-user"),
                   "NPM_CONFIG_GLOBALCONFIG": str(scratch / "config/npm-global"), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "NO_COLOR": "1"}
            developer = Path("/var/select/developer_dir").resolve()
            qualified_developers = {Path("/Library/Developer/CommandLineTools"),
                                    Path("/Applications/Xcode.app/Contents/Developer")}
            if developer in qualified_developers and developer.is_dir():
                env["DEVELOPER_DIR"] = str(developer)
            elif Path("/Library/Developer/CommandLineTools").is_dir():
                env["DEVELOPER_DIR"] = "/Library/Developer/CommandLineTools"
            env["CLANG_MODULE_CACHE_PATH"] = str(scratch / "clang-cache")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "Error: command preparation exhausted the deadline; no command executed."
            payload = {"profile": str(policy), "command": command, "timeout": remaining,
                       "uid_process_limit": min(65536, _processes(uid=True) + MAX_GROUP_PROCESSES)}
            launched: list[Any] = []
            finished = threading.Event()
            exhausted = threading.Event()
            monitor_error = threading.Event()
            def monitor() -> None:
                while not finished.wait(.1):
                    if not launched:
                        continue
                    try:
                        if _processes(uid=False, group=launched[0].pid) > MAX_GROUP_PROCESSES:
                            exhausted.set()
                            _kill_group(launched[0])
                            return
                    except (OSError, subprocess.SubprocessError):
                        monitor_error.set()
                        _kill_group(launched[0])
                        return
            def launched_process(proc: Any) -> None:
                launched.append(proc)
            watcher = threading.Thread(target=monitor, daemon=True)
            watcher.start()
            try:
                result = run_search_process(
                    [sys.executable, "-I", "-S", str(WORKER), json.dumps(payload)],
                    limit=100_000, max_bytes=MAX_OUTPUT, timeout=remaining,
                    process_kwargs={"cwd": str(root), "env": env, "start_new_session": True,
                                    "close_fds": True, "shell": False},
                    terminate=_kill_group, cancel_event=cancel_event, on_started=launched_process,
                )
            finally:
                finished.set()
                if launched:
                    _kill_group(launched[0])
                watcher.join(timeout=5)
            if watcher.is_alive() or monitor_error.is_set():
                return "Error: containment process accounting or cleanup could not be established; outcome unknown."
            if exhausted.is_set():
                return "Error: contained process-group limit exceeded; children terminated."
            if result.cancelled:
                return "Error: agent team was cancelled; contained children terminated."
            if result.timed_out:
                return "Error: contained command timed out; children terminated."
            if result.truncated or result.stderr_truncated:
                return "Error: contained command output limit exceeded; outcome unknown. Use quiet flags."
            output = result.stdout.decode("utf-8", errors="replace").lstrip("\r\n").rstrip()
            if result.stderr:
                output += "\nSTDERR: " + result.stderr.decode("utf-8", errors="replace").rstrip()
            text = output or "(command produced no output)"
            return f"[contained workspace command; network denied; clean environment]\n{text}\n[exit code: {result.returncode}]"
    except (OSError, ValueError, ProtectedMemoryPathError, subprocess.SubprocessError):
        return "Error: protected command containment unavailable or cleanup failed; no uncontained fallback."
