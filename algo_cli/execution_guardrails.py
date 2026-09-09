"""Run-scoped execution evidence and fail-closed file-write guardrails.

The ledger deliberately stores only structural metadata. File contents, shell
commands, and command output are never retained here.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal, Sequence


EvidenceKind = Literal["read", "mutation", "verification"]
VerificationKind = Literal["same_path_reread", "git_diff", "test", "lint"]

_PATH_MUTATION_OPERATIONS = frozenset({"write_file", "edit_file", "batch_edit"})
_MUTATION_OPERATIONS = frozenset({*_PATH_MUTATION_OPERATIONS, "run_shell"})
_READ_OPERATIONS = frozenset({"read_file"})
_VERIFICATION_KINDS = frozenset({"git_diff", "test", "lint"})

_SENSITIVE_COMPONENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
    }
)
_SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        ".netrc",
        "_netrc",
        "credentials",
        "credentials.json",
        "secrets.json",
        "service-account.json",
        "service_account.json",
        "token.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx"})
_CONTROL_OPERATOR_RE = re.compile(r"(?:\r|\n|;|&&|\|\||(?<!\|)\|(?!\|)|(?<!&)\&(?!&)|`|\$\(|[<>])")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)
_RUNNER_OPTIONS_WITH_VALUE = frozenset(
    {
        "--directory",
        "--env-file",
        "--extra",
        "--group",
        "--index",
        "--project",
        "--python",
        "--with",
    }
)
_VERIFIER_SCRIPT_RE = re.compile(
    r"^(?:(?:health|smoke)[_-]?check|check(?:[_-].*)?|verify(?:[_-].*)?|"
    r"test(?:[_-].*)?|.*[_-]test)\.(?:py|sh|ps1|js|ts)$",
    re.IGNORECASE,
)


class ExecutionGuardrailError(RuntimeError):
    """Raised when execution evidence cannot be handled safely."""


@dataclass(frozen=True)
class EvidenceEvent:
    """One content-free event; mutation evidence may describe a possible effect."""

    sequence: int
    kind: EvidenceKind
    operation: str
    relative_path: str | None = None
    verification_kind: str | None = None


@dataclass(frozen=True)
class PathDecision:
    """Decision for a prospective model-controlled file write."""

    allowed: bool
    reason: str
    resolved_path: Path | None = None
    relative_path: str | None = None
    sensitive: bool = False


@dataclass(frozen=True)
class ReadBeforeEditDecision:
    """Whether an edit has a fresh successful read in the current run."""

    allowed: bool
    reason: str
    read_sequence: int | None = None


@dataclass(frozen=True)
class VerificationCommand:
    """Content-free classification of a shell verification command."""

    qualifies: bool
    kind: str | None
    reason: str


@dataclass(frozen=True)
class CompletionDecision:
    """Whether execution evidence supports a successful completion claim."""

    allowed: bool
    reason: str
    last_mutation_sequence: int | None = None
    verifier_sequence: int | None = None
    verifier_kind: str | None = None


@dataclass
class _ExecutionLedger:
    workspace: Path
    events: list[EvidenceEvent] = field(default_factory=list)
    closed: bool = False

    def append(
        self,
        kind: EvidenceKind,
        operation: str,
        *,
        relative_path: str | None = None,
        verification_kind: str | None = None,
    ) -> EvidenceEvent:
        if self.closed:
            raise ExecutionGuardrailError("execution scope is closed")
        event = EvidenceEvent(
            sequence=len(self.events) + 1,
            kind=kind,
            operation=operation,
            relative_path=relative_path,
            verification_kind=verification_kind,
        )
        self.events.append(event)
        return event


@dataclass(frozen=True)
class ExecutionScope:
    """Opaque handle returned by :func:`begin_execution_scope`."""

    workspace: Path
    _ledger: _ExecutionLedger
    _token: Token[_ExecutionLedger | None]


_ACTIVE_LEDGER: ContextVar[_ExecutionLedger | None] = ContextVar(
    "algo_cli_execution_evidence_ledger",
    default=None,
)


def begin_execution_scope(workspace: str | Path) -> ExecutionScope:
    """Begin a run-local evidence scope rooted at an existing directory."""

    try:
        resolved_workspace = Path(workspace).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ExecutionGuardrailError("workspace cannot be resolved safely") from exc
    if not resolved_workspace.is_dir():
        raise ExecutionGuardrailError("workspace must be an existing directory")
    if resolved_workspace.parent == resolved_workspace:
        raise ExecutionGuardrailError("filesystem root cannot be used as a workspace")
    ledger = _ExecutionLedger(workspace=resolved_workspace)
    token = _ACTIVE_LEDGER.set(ledger)
    return ExecutionScope(resolved_workspace, ledger, token)


def end_execution_scope(scope: ExecutionScope) -> tuple[EvidenceEvent, ...]:
    """Close *scope*, restore any parent scope, and return immutable evidence."""

    active = _ACTIVE_LEDGER.get()
    if active is not scope._ledger or active.closed:
        raise ExecutionGuardrailError("execution scopes must be ended once and in order")
    snapshot = tuple(active.events)
    try:
        _ACTIVE_LEDGER.reset(scope._token)
    except (RuntimeError, ValueError) as exc:
        raise ExecutionGuardrailError("execution scope belongs to a different context") from exc
    active.closed = True
    return snapshot


def evidence_snapshot() -> tuple[EvidenceEvent, ...]:
    """Return an immutable snapshot of the active scope's evidence."""

    ledger = _ACTIVE_LEDGER.get()
    return tuple(ledger.events) if ledger is not None and not ledger.closed else ()


def active_workspace() -> Path | None:
    """Return the active canonical workspace, if a scope is open."""

    ledger = _ACTIVE_LEDGER.get()
    return ledger.workspace if ledger is not None and not ledger.closed else None


def is_sensitive_path(path: str | Path) -> bool:
    """Return whether a path names credential, VCS, or private key material."""

    try:
        normalized = str(path).replace("\\", "/")
        parts = tuple(part.casefold() for part in Path(normalized).parts if part not in {"", "."})
    except (OSError, RuntimeError, TypeError, ValueError):
        return True
    if not parts:
        return True
    if any(part in _SENSITIVE_COMPONENTS for part in parts):
        return True
    if any(part in _SENSITIVE_FILENAMES or part.startswith(".env.") for part in parts):
        return True
    name = parts[-1]
    return any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def assess_resolved_write_path(
    resolved_workspace: Path,
    resolved_target: Path,
    *,
    target_is_directory: bool = False,
) -> PathDecision:
    """Pure containment decision for paths already resolved by a caller/probe."""

    if not isinstance(resolved_workspace, Path) or not isinstance(resolved_target, Path):
        return PathDecision(False, "resolved paths are invalid")
    if not resolved_workspace.is_absolute() or not resolved_target.is_absolute():
        return PathDecision(False, "resolved paths must be absolute")
    if resolved_workspace.parent == resolved_workspace:
        return PathDecision(False, "filesystem root cannot be used as a workspace")
    try:
        relative = resolved_target.relative_to(resolved_workspace)
    except (TypeError, ValueError):
        return PathDecision(False, "target escapes the workspace")
    relative_text = relative.as_posix()
    if relative_text in {"", "."}:
        return PathDecision(False, "target must name a file")
    if ".." in relative.parts:
        return PathDecision(False, "target escapes the workspace")
    if target_is_directory:
        return PathDecision(False, "target is a directory")
    if is_sensitive_path(relative):
        return PathDecision(
            False,
            "target is a sensitive path",
            resolved_path=resolved_target,
            relative_path=relative_text,
            sensitive=True,
        )
    return PathDecision(
        True,
        "target is contained in the workspace",
        resolved_path=resolved_target,
        relative_path=relative_text,
    )


def assess_write_path(
    workspace: str | Path,
    path: str | Path,
    *,
    resolve_probe: Callable[[Path, bool], Path] | None = None,
    is_directory_probe: Callable[[Path], bool] | None = None,
) -> PathDecision:
    """Resolve and assess a prospective write without performing the write.

    The optional probes make resolution and directory checks deterministic in
    tests or alternate runtimes. Resolution failures always deny the write.
    """

    def default_resolve(candidate: Path, strict: bool) -> Path:
        return candidate.resolve(strict=strict)

    resolver = resolve_probe or default_resolve
    directory_probe = is_directory_probe or Path.is_dir
    try:
        workspace_input = Path(workspace).expanduser()
        resolved_workspace = resolver(workspace_input, True)
        if not directory_probe(resolved_workspace):
            return PathDecision(False, "workspace is not a directory")
        if resolved_workspace.parent == resolved_workspace:
            return PathDecision(False, "filesystem root cannot be used as a workspace")
        target_input = Path(path).expanduser()
        if not target_input.is_absolute():
            target_input = resolved_workspace / target_input
        resolved_target = resolver(target_input, False)
        target_is_directory = directory_probe(resolved_target)
    except (OSError, RuntimeError, TypeError, ValueError):
        return PathDecision(False, "path cannot be resolved safely")
    return assess_resolved_write_path(
        resolved_workspace,
        resolved_target,
        target_is_directory=target_is_directory,
    )


def assess_active_write_path(path: str | Path) -> PathDecision:
    """Assess a path against the active run workspace, failing closed."""

    workspace = active_workspace()
    if workspace is None:
        return PathDecision(False, "no active execution scope")
    return assess_write_path(workspace, path)


def _valid_evidence(events: Sequence[EvidenceEvent]) -> bool:
    try:
        expected = 1
        for event in events:
            if event.sequence != expected:
                return False
            expected += 1
            if event.kind == "read":
                if event.operation not in _READ_OPERATIONS or not event.relative_path or event.verification_kind:
                    return False
            elif event.kind == "mutation":
                if event.operation not in _MUTATION_OPERATIONS or not event.relative_path or event.verification_kind:
                    return False
            elif event.kind == "verification":
                if (
                    event.operation != "verification"
                    or event.relative_path
                    or event.verification_kind not in _VERIFICATION_KINDS
                ):
                    return False
            else:
                return False
    except (AttributeError, TypeError):
        return False
    return True


def evaluate_read_before_edit(
    relative_path: str,
    events: Sequence[EvidenceEvent],
) -> ReadBeforeEditDecision:
    """Pure decision requiring a read newer than any mutation of the file."""

    if not relative_path or not _valid_evidence(events):
        return ReadBeforeEditDecision(False, "execution evidence is invalid")
    reads = [event for event in events if event.kind == "read" and event.relative_path == relative_path]
    if not reads:
        return ReadBeforeEditDecision(False, "edit requires a successful same-file read")
    last_read = reads[-1]
    mutations = [event for event in events if event.kind == "mutation" and event.relative_path in {relative_path, "."}]
    if mutations and mutations[-1].sequence > last_read.sequence:
        return ReadBeforeEditDecision(False, "edit requires a fresh read after the previous mutation")
    return ReadBeforeEditDecision(True, "same-file read evidence is fresh", last_read.sequence)


def read_before_edit_decision(path: str | Path) -> ReadBeforeEditDecision:
    """Assess active-scope read evidence for a prospective edit."""

    path_decision = assess_active_write_path(path)
    if not path_decision.allowed or not path_decision.relative_path:
        return ReadBeforeEditDecision(False, path_decision.reason)
    return evaluate_read_before_edit(path_decision.relative_path, evidence_snapshot())


def _append_path_event(
    kind: Literal["read", "mutation"],
    path: str | Path,
    *,
    operation: str,
    success: bool,
) -> EvidenceEvent | None:
    if not success:
        return None
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None or ledger.closed:
        return None
    allowed_operations = _READ_OPERATIONS if kind == "read" else _PATH_MUTATION_OPERATIONS
    if operation not in allowed_operations:
        return None
    decision = assess_write_path(ledger.workspace, path)
    if not decision.allowed or not decision.relative_path:
        return None
    return ledger.append(kind, operation, relative_path=decision.relative_path)


def record_read(
    path: str | Path,
    *,
    success: bool,
    operation: str = "read_file",
) -> EvidenceEvent | None:
    """Record only a successful, contained, non-sensitive file read."""

    return _append_path_event("read", path, operation=operation, success=success)


def record_mutation(
    path: str | Path,
    *,
    success: bool,
    operation: str,
) -> EvidenceEvent | None:
    """Record only a successful allowed model file mutation."""

    return _append_path_event("mutation", path, operation=operation, success=success)


def record_workspace_mutation(
    *,
    success: bool,
    possible: bool = False,
    operation: str = "run_shell",
) -> EvidenceEvent | None:
    """Invalidate workspace evidence after a successful or possibly partial mutation."""

    if not (success or possible) or operation != "run_shell":
        return None
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None or ledger.closed:
        return None
    return ledger.append("mutation", operation, relative_path=".")


def _shell_split(command: str, *, platform_name: str | None = None) -> list[str]:
    """Split one shell command using the quoting rules of the target platform."""

    platform = os.name if platform_name is None else platform_name
    tokens = shlex.split(command, posix=platform != "nt")
    if platform == "nt":
        # ``cmd.exe`` uses double quotes for grouping and removes the outer
        # pair before invoking Python. Single quotes are literal characters.
        tokens = [
            token[1:-1] if len(token) >= 2 and token.startswith('"') and token.endswith('"') else token
            for token in tokens
        ]
    return tokens


def _command_tokens(command: str) -> list[str] | None:
    if not command.strip() or _CONTROL_OPERATOR_RE.search(command):
        return None
    try:
        tokens = _shell_split(command)
    except ValueError:
        return None
    while os.name != "nt" and tokens and _ENV_ASSIGNMENT_RE.match(tokens[0]):
        tokens.pop(0)
    return tokens or None


def _unwrap_verification_shell(command: str) -> str:
    """Remove narrowly safe shell framing around a verifier command.

    Models commonly express a working directory in-band even though
    ``run_shell`` also has a structured ``cwd`` argument. Accept one leading
    ``cd PATH &&`` and one trailing ``2>&1`` while leaving every other shell
    operator for the fail-closed parser to reject.
    """

    candidate = command.strip()
    candidate = re.sub(r"\s+2\s*>\s*&\s*1\s*$", "", candidate).strip()
    if "&&" not in candidate:
        return candidate
    parts = candidate.split("&&")
    if len(parts) != 2:
        return candidate
    try:
        prefix = _shell_split(parts[0].strip())
    except ValueError:
        return candidate
    if len(prefix) == 2 and _executable_name(prefix[0]) == "cd" and prefix[1]:
        return parts[1].strip()
    if len(prefix) == 3 and _executable_name(prefix[0]) == "cd" and prefix[1] == "--" and prefix[2]:
        return parts[1].strip()
    return candidate


def _executable_name(token: str) -> str:
    name = token.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _strip_runner(tokens: list[str]) -> list[str]:
    first = _executable_name(tokens[0])
    if first == "env":
        tokens = tokens[1:]
        while tokens and (_ENV_ASSIGNMENT_RE.match(tokens[0]) or tokens[0].startswith("-")):
            tokens = tokens[1:]
        return _strip_runner(tokens) if tokens else []
    if first in {"uv", "poetry", "pipenv"}:
        try:
            run_index = tokens.index("run")
        except ValueError:
            return []
        nested = tokens[run_index + 1 :]
        while nested and nested[0].startswith("-"):
            raw_option = nested.pop(0)
            option = raw_option.split("=", 1)[0]
            if "=" not in raw_option and option in _RUNNER_OPTIONS_WITH_VALUE:
                if not nested:
                    return []
                nested.pop(0)
            elif option not in _RUNNER_OPTIONS_WITH_VALUE and option not in {
                "--all-extras",
                "--exact",
                "--frozen",
                "--isolated",
                "--locked",
                "--no-project",
                "--no-sync",
                "--offline",
            }:
                return []
        return _strip_runner(nested) if nested else []
    return tokens


def _assertion_script_qualifies(source: str) -> bool:
    """Return whether inline Python contains a fail-on-error check."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sys"
            and node.func.attr == "exit"
            and node.args
            and not (isinstance(node.args[0], ast.Constant) and node.args[0].value in {0, None})
        ):
            return True
    return False


def _looks_like_verifier_script(token: str) -> bool:
    return bool(_VERIFIER_SCRIPT_RE.fullmatch(_executable_name(token)))


def _inline_python_verification(
    command: str,
    *,
    platform_name: str | None = None,
) -> VerificationCommand | None:
    """Classify a standalone Python ``-c`` check before newline rejection.

    ``shlex`` preserves newlines inside the quoted source as one argument, so
    multiline assertion scripts remain unambiguous while extra shell segments
    produce additional tokens and are rejected.
    """

    platform = os.name if platform_name is None else platform_name
    try:
        tokens = _shell_split(command, platform_name=platform)
    except ValueError:
        return None
    while platform != "nt" and tokens and _ENV_ASSIGNMENT_RE.match(tokens[0]):
        tokens.pop(0)
    if (
        len(tokens) != 3
        or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|py", _executable_name(tokens[0]))
        or tokens[1].casefold() != "-c"
    ):
        return None
    if _assertion_script_qualifies(tokens[2]):
        return VerificationCommand(True, "test", "inline assertions execute a fail-on-error check")
    return VerificationCommand(False, None, "inline Python lacks fail-on-error assertions")


def _verifier_chain(command: str) -> VerificationCommand | None:
    """Classify a chain of verifiers joined by ``&&``.

    ``&&`` short-circuits, so the compound's exit status is the AND of every
    segment: it fails if any segment fails. A chain therefore preserves the
    fail-on-error guarantee as long as every segment is itself a recognized
    verifier. This lets models express ``go vet ./... && go test ./...`` as a
    single verification step instead of being rejected as compound.
    """

    candidate = command.strip()
    if "&&" not in candidate:
        return None
    segments = [segment.strip() for segment in candidate.split("&&")]
    if len(segments) < 2 or any(not segment for segment in segments):
        return None
    # A leading ``cd PATH`` segment is a working-directory wrapper, not a
    # verifier; drop it so the remaining segments are all verifiers.
    if segments:
        try:
            prefix = _shell_split(segments[0])
        except ValueError:
            prefix = []
        if (len(prefix) == 2 and _executable_name(prefix[0]) == "cd" and prefix[1]) or (
            len(prefix) == 3 and _executable_name(prefix[0]) == "cd" and prefix[1] == "--" and prefix[2]
        ):
            segments = segments[1:]
    if len(segments) < 2:
        return None
    kinds: list[str] = []
    for segment in segments:
        decision = classify_verification_command(segment)
        if not decision.qualifies or not decision.kind:
            return None
        kinds.append(decision.kind)
    # A chain of verifiers is itself a test; preserve the most specific kind
    # when all segments agree, otherwise report the compound as a test.
    kind = kinds[0] if len(set(kinds)) == 1 else "test"
    return VerificationCommand(True, kind, "command executes a chain of verifiers")


def classify_verification_command(command: str) -> VerificationCommand:
    """Classify one shell command without retaining its text.

    Compound/piped commands and discovery-only flags are rejected because a
    zero shell status would not reliably prove the verifier itself passed. A
    single working-directory wrapper and stderr-to-stdout redirect are allowed
    because they preserve the verifier's exit status. A chain of verifiers
    joined by ``&&`` is accepted when every segment is itself a verifier.
    """

    chain = _verifier_chain(command)
    if chain is not None:
        return chain
    unwrapped = _unwrap_verification_shell(command)
    inline_python = _inline_python_verification(unwrapped)
    if inline_python is not None:
        return inline_python
    tokens = _command_tokens(unwrapped)
    if tokens is None:
        return VerificationCommand(False, None, "command is empty, compound, or ambiguous")
    tokens = _strip_runner(tokens)
    if not tokens:
        return VerificationCommand(False, None, "command runner does not invoke a verifier")
    lowered = [token.casefold() for token in tokens]
    if any(
        flag in lowered
        for flag in {
            "--help",
            "-h",
            "--version",
            "--collect-only",
            "--co",
            "--list-tests",
            "--list-sessions",
            "--notest",
            "--showconfig",
            "--dry-run",
            "--print-config",
        }
    ):
        return VerificationCommand(False, None, "command does not execute verification")
    if any(
        flag in lowered
        for flag in {
            "--exit-zero",
            "--no-error-on-unmatched-pattern",
            "--pass-with-no-tests",
        }
    ):
        return VerificationCommand(False, None, "command disables verifier failure status")

    executable = _executable_name(tokens[0])
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|py", executable):
        if len(tokens) >= 2 and _looks_like_verifier_script(tokens[1]):
            return VerificationCommand(True, "test", "command executes a verifier script")
        if len(lowered) < 3 or lowered[1] != "-m":
            return VerificationCommand(False, None, "python command does not invoke a verifier module")
        tokens = tokens[2:]
        lowered = lowered[2:]
        executable = _executable_name(tokens[0])
    elif executable == "git":
        if len(lowered) >= 3 and lowered[1] == "diff" and "--check" in lowered[2:]:
            return VerificationCommand(True, "git_diff", "git diff --check is structural verification")
        return VerificationCommand(False, None, "git command is not a fail-on-error git diff check")

    if _looks_like_verifier_script(tokens[0]):
        return VerificationCommand(True, "test", "command executes a verifier script")

    if executable in {"pytest", "py.test", "unittest", "tox", "nox"}:
        return VerificationCommand(True, "test", "command executes a test runner")
    if executable in {"go", "cargo", "dotnet"}:
        if executable == "go" and len(lowered) >= 2 and lowered[1] == "vet":
            return VerificationCommand(True, "lint", "go vet is a static-analysis check")
        if executable == "go" and (len(lowered) < 2 or lowered[1] != "test"):
            return VerificationCommand(False, None, "go command is not a test")
        if executable == "cargo" and len(lowered) >= 2 and lowered[1] == "clippy":
            return VerificationCommand(True, "lint", "command executes a lint check")
        if executable == "cargo" and (len(lowered) < 2 or lowered[1] != "test"):
            return VerificationCommand(False, None, "cargo command is not a test or lint check")
        if executable == "dotnet" and (len(lowered) < 2 or lowered[1] != "test"):
            return VerificationCommand(False, None, "dotnet command is not a test")
        return VerificationCommand(True, "test", "command executes a test runner")
    if executable in {"mvn", "mvnw"}:
        goals = {token for token in lowered[1:] if not token.startswith("-")}
        if goals & {"test", "verify"}:
            return VerificationCommand(True, "test", "build goal executes tests")
        return VerificationCommand(False, None, "build command lacks a test goal")
    if executable in {"gradle", "gradlew"}:
        tasks = {token for token in lowered[1:] if not token.startswith("-")}
        if tasks & {"test", "check"}:
            return VerificationCommand(True, "test", "build task executes tests")
        return VerificationCommand(False, None, "build command lacks a test task")

    if executable in {"npm", "pnpm", "yarn", "bun"}:
        scripts = {"test", "lint", "check", "typecheck", "type-check"}
        requested = next((token for token in lowered[1:] if not token.startswith("-")), "")
        if requested == "run" and len(lowered) >= 3:
            requested = lowered[2]
        if requested in scripts:
            kind = "test" if requested == "test" else "lint"
            return VerificationCommand(True, kind, "package script executes verification")
        return VerificationCommand(False, None, "package command is not a test or lint script")

    if executable in {"make", "just"}:
        targets = {token for token in lowered[1:] if not token.startswith("-")}
        if "test" in targets:
            return VerificationCommand(True, "test", "build target executes tests")
        if targets & {"lint", "check", "typecheck", "type-check"}:
            return VerificationCommand(True, "lint", "build target executes lint checks")
        return VerificationCommand(False, None, "build command lacks a verification target")

    if executable in {
        "ruff",
        "mypy",
        "pyright",
        "pylint",
        "flake8",
        "eslint",
        "biome",
        "shellcheck",
        "golangci-lint",
    }:
        if executable in {"ruff", "biome"} and (len(lowered) < 2 or lowered[1] != "check"):
            return VerificationCommand(False, None, "command does not execute a lint check")
        if any(flag in lowered for flag in {"--fix", "--write"}):
            return VerificationCommand(False, None, "mutating lint commands are not verification")
        return VerificationCommand(True, "lint", "command executes a lint or type check")
    if executable == "tsc" and "--noemit" in lowered:
        return VerificationCommand(True, "lint", "command executes a no-emit type check")
    return VerificationCommand(False, None, "command is not a recognized verifier")


_STATUS_MASKING_ECHO_RE = re.compile(r"(?is)^(?P<base>.+?);\s*echo\s+(?:[\"'])?[^\n;]*\$\?[^\n;]*(?:[\"'])?\s*$")


def masks_verification_exit_status(command: str) -> bool:
    """Return whether a verifier is followed by an echo that masks its status."""

    match = _STATUS_MASKING_ECHO_RE.match(str(command or "").strip())
    if match is None:
        return False
    return classify_verification_command(match.group("base").strip()).qualifies


def record_verification(
    verification_kind: str,
    *,
    success: bool,
    cwd: str | Path | None = None,
) -> EvidenceEvent | None:
    """Record a successful recognized verifier without command/output data."""

    if not success or verification_kind not in _VERIFICATION_KINDS:
        return None
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None or ledger.closed:
        return None
    if not _verification_workspace_matches(cwd) or (verification_kind == "git_diff" and _git_scope_overridden()):
        return None
    return ledger.append("verification", "verification", verification_kind=verification_kind)


def _verification_workspace_matches(cwd: str | Path | None) -> bool:
    workspace = active_workspace()
    if workspace is None:
        return False
    try:
        candidate = workspace if cwd is None else Path(cwd).expanduser().resolve(strict=True)
        return candidate == workspace
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _git_scope_overridden() -> bool:
    return any(
        value
        and (
            key
            in {
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_INDEX_FILE",
                "GIT_COMMON_DIR",
                "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            }
            or key.startswith("GIT_CONFIG")
        )
        for key, value in os.environ.items()
    )


def _shell_verification_scope_matches(command: str, cwd: str | Path | None) -> bool:
    """Require workspace-wide evidence, not a verifier redirected to another root.

    Classification recognizes command syntax, not test relevance or arbitrary
    script internals. Reject explicit directory changes and out-of-root targets
    that would otherwise lose their scope when shell/runner wrappers are stripped.
    """

    workspace = active_workspace()
    if workspace is None or not _verification_workspace_matches(cwd):
        return False

    def contained(value: str, *, same_root: bool = False) -> bool:
        if not value or any(char in value for char in ("$", "`", "%")):
            return False
        try:
            path = Path(value).expanduser()
            resolved = (workspace / path).resolve(strict=same_root)
            return resolved == workspace if same_root else resolved.is_relative_to(workspace)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    directory_options = {"--directory", "--project", "--cwd", "--dir", "--prefix", "--working-directory", "-C"}
    path_options = {"--manifest-path", "--rootdir", "--test-dir", "--config-file"}
    # Use the same bounded shell framing accepted by the classifier. Quoted
    # ambiguous chains may be refused; they must never gain broader evidence.
    for segment in command.strip().split("&&"):
        segment = re.sub(r"\s+2\s*>\s*&\s*1\s*$", "", segment).strip()
        try:
            tokens = _shell_split(segment)
        except ValueError:
            return False
        if not tokens:
            return False
        if _executable_name(tokens[0]) == "cd":
            path = tokens[2] if len(tokens) == 3 and tokens[1] == "--" else tokens[1] if len(tokens) == 2 else ""
            if not contained(path, same_root=True):
                return False
            continue
        nested = list(tokens)
        while nested and _ENV_ASSIGNMENT_RE.match(nested[0]):
            nested.pop(0)
        nested = _strip_runner(nested) if nested else []
        if not nested:
            return False
        executable_index = len(tokens) - len(nested)
        if _looks_like_verifier_script(nested[0]) and not contained(nested[0]):
            return False
        verifier = _executable_name(nested[0])
        if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|py", verifier) and len(nested) > 2 and nested[1] == "-m":
            verifier = nested[2]
        inline_source_index = (
            executable_index + 2
            if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|py", _executable_name(nested[0]))
            and len(nested) == 3
            and nested[1] == "-c"
            else None
        )
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if _ENV_ASSIGNMENT_RE.match(token):
                key, value = token.split("=", 1)
                if key.startswith("GIT_"):
                    return False
                if key == "PYTHONPATH" and not all(contained(path) for path in value.split(os.pathsep)):
                    return False
            option, separator, value = token.partition("=")
            non_target_option = (index < executable_index and option == "--python") or (
                verifier in {"pytest", "py.test"} and option in {"--basetemp", "--junitxml", "--junit-xml"}
            )
            if non_target_option:
                if not separator:
                    index += 1
                    if index >= len(tokens):
                        return False
            elif option in directory_options | path_options:
                if not separator:
                    index += 1
                    if index >= len(tokens):
                        return False
                    value = tokens[index]
                if not contained(value, same_root=option in directory_options):
                    return False
            elif token.startswith("-C") and token != "-C":
                if not contained(token[2:], same_root=True):
                    return False
            elif index != executable_index and not token.startswith("-") and not _ENV_ASSIGNMENT_RE.match(token):
                # Executable paths and inline Python are not target operands.
                if index != inline_source_index and not contained(token):
                    return False
            index += 1
    return True


def record_shell_verification(
    command: str,
    *,
    returncode: int,
    cwd: str | Path | None = None,
) -> EvidenceEvent | None:
    """Record a passed verifier only when its explicit scope matches this run."""

    if returncode != 0:
        return None
    classification = classify_verification_command(command)
    if not classification.qualifies or classification.kind is None:
        return None
    if not _shell_verification_scope_matches(command, cwd):
        return None
    return record_verification(classification.kind, success=True, cwd=cwd)


def evaluate_completion(events: Sequence[EvidenceEvent]) -> CompletionDecision:
    """Pure completion decision over an immutable or probe-supplied ledger."""

    if not _valid_evidence(events):
        return CompletionDecision(False, "execution evidence is invalid")
    mutations = [event for event in events if event.kind == "mutation"]
    if not mutations:
        return CompletionDecision(True, "no recorded workspace mutation requires verification")
    last_mutation = mutations[-1]
    for event in events:
        if event.sequence <= last_mutation.sequence:
            continue
        if event.kind == "verification" and event.verification_kind in _VERIFICATION_KINDS:
            return CompletionDecision(
                True,
                "last mutation was followed by successful verification",
                last_mutation.sequence,
                event.sequence,
                event.verification_kind,
            )
    return CompletionDecision(
        False,
        "successful verification is required after the last mutation",
        last_mutation.sequence,
    )


def completion_decision(events: Iterable[EvidenceEvent] | None = None) -> CompletionDecision:
    """Evaluate completion for supplied evidence or the active run scope."""

    if events is not None:
        return evaluate_completion(tuple(events))
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None or ledger.closed:
        return CompletionDecision(False, "no active execution scope")
    return evaluate_completion(tuple(ledger.events))


def auto_verify_working_tree(root: str | Path | None = None) -> CompletionDecision:
    """Second-stop fallback: run git diff --check HEAD as a structural verifier.

    A pass qualifies only known file mutations covered by the tracked diff.
    Missing Git, an unknown shell write set, and untracked/ignored paths require
    explicit verification instead of a success claim with a manual warning.
    """
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None or ledger.closed:
        return CompletionDecision(False, "no active execution scope")
    try:
        workspace = ledger.workspace if root is None else Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return CompletionDecision(False, "verification workspace cannot be resolved")
    if workspace != ledger.workspace:
        return CompletionDecision(False, "verification workspace does not match the active execution scope")
    if _git_scope_overridden():
        return CompletionDecision(False, "Git environment overrides prevent workspace-bound automatic verification")
    pending = completion_decision()
    if pending.allowed:
        return pending
    last_verifier = max((event.sequence for event in ledger.events if event.kind == "verification"), default=0)
    mutations = [event for event in ledger.events if event.kind == "mutation" and event.sequence > last_verifier]
    if any(event.relative_path == "." for event in mutations):
        return CompletionDecision(False, "shell mutation scope requires an explicit workspace verifier")
    try:
        completed = subprocess.run(
            ["git", "diff", "--check", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return CompletionDecision(
            False,
            "git is unavailable; verification is incomplete",
        )
    except subprocess.TimeoutExpired:
        return CompletionDecision(False, "git diff --check timed out; verification is incomplete")
    except (OSError, subprocess.SubprocessError):
        return CompletionDecision(False, "git diff --check could not complete")
    if completed.returncode != 0:
        if "not a git repository" in (completed.stderr or "").lower():
            return CompletionDecision(
                False,
                "workspace is not a git repository; verification is incomplete",
            )
        return CompletionDecision(
            False,
            "git diff --check reported structural problems",
        )
    paths = sorted({str(event.relative_path) for event in mutations})
    try:
        tracked = subprocess.run(
            ["git", "--literal-pathspecs", "ls-files", "--error-unmatch", "--", *paths],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return CompletionDecision(False, "tracked mutation coverage could not be verified")
    if tracked.returncode != 0:
        return CompletionDecision(False, "tracked diff does not cover every mutated path; run an explicit verifier")
    record_verification("git_diff", success=True, cwd=workspace)
    return completion_decision()
