"""Structured, fail-closed commit → push → pull-request workflow."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .intelligence.pre_push_gate import PrePushGate, ScrubEvidence
from . import git_evidence
from .worktree_runtime import WorktreeError, repository_context


MAX_COMMAND_OUTPUT_CHARS = 12_000
MAX_SCAN_BYTES = 4_000_000
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk"})
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
)
_LFS_OID_PATTERN = re.compile(r"(?m)^oid sha256:[0-9a-fA-F]{64}$")


class PublishError(RuntimeError):
    """A user-facing structured publish failure."""


@dataclass(frozen=True)
class RemoteRouting:
    """Credential-free effective routing evidence for origin."""

    origin: bool
    fetch_identities: tuple[str, ...]
    push_identities: tuple[str, ...]
    fetch_digest: str
    push_digest: str
    safe: bool
    reason: str
    bound_push_url: str = field(default="", repr=False, compare=False)


def _sanitize_error_text(value: str) -> str:
    sanitized = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@",
        r"\1[redacted]@",
        value,
    )
    for label, pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(f"[redacted {label}]", sanitized)
    return sanitized


def _run_program(
    program: str,
    args: list[str],
    *,
    cwd: str | Path,
    timeout: int = 120,
    input_text: str | None = None,
) -> tuple[int, str]:
    # Keep line-oriented Git plumbing input byte-exact. Text-mode pipes convert
    # LF to CRLF on Windows, which ``git update-ref --stdin`` rejects.
    encoded_input = None if input_text is None else input_text.encode("utf-8")
    try:
        proc = subprocess.run(
            [program, *args],
            cwd=Path(cwd).expanduser().resolve(),
            capture_output=True,
            timeout=timeout,
            input=encoded_input,
        )
    except FileNotFoundError:
        return 127, f"{program} is not installed or is not available on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"{program} timed out after {timeout} seconds"
    except OSError as exc:
        return 1, f"{program} could not start: {exc}"
    raw_output = proc.stdout or proc.stderr or b""
    output = raw_output.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        output = _sanitize_error_text(output)
    if len(output) > MAX_COMMAND_OUTPUT_CHARS:
        output = output[:MAX_COMMAND_OUTPUT_CHARS] + "\n... [truncated]"
    return proc.returncode, output


def _push_bound_destination(
    *,
    cwd: str | Path,
    push_url: str,
    refspec: str,
    timeout: int = 600,
) -> None:
    """Push one immutable refspec to a captured URL kept out of process argv."""

    remote_name = f"algo-cli-bound-{secrets.token_hex(8)}"
    url_environment = "ALGO_CLI_BOUND_PUSH_URL"
    environment = os.environ.copy()
    environment[url_environment] = push_url
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    args = [
        "git",
        "--no-replace-objects",
        "-c",
        "push.followTags=false",
        "-c",
        "push.recurseSubmodules=no",
        f"--config-env=remote.{remote_name}.url={url_environment}",
        "push",
        "--no-follow-tags",
        "--recurse-submodules=no",
        remote_name,
        refspec,
    ]
    try:
        proc = subprocess.run(
            args,
            cwd=Path(cwd).expanduser().resolve(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=environment,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PublishError("git is not installed or is not available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PublishError(f"Push timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise PublishError(f"Push could not start: {exc}") from exc
    if proc.returncode != 0:
        raise PublishError(
            f"Push to the reviewed destination failed with git exit {proc.returncode}."
        )


def _git(
    args: list[str],
    *,
    cwd: str | Path,
    operation: str,
    timeout: int = 120,
) -> str:
    rc, output = _run_program(
        "git", ["--no-replace-objects", *args], cwd=cwd, timeout=timeout
    )
    if rc != 0:
        raise PublishError(f"{operation} failed: {output or f'git exited {rc}'}")
    return output


def _read_bounded_error(handle: Any) -> str:
    handle.seek(0)
    raw = handle.read(MAX_COMMAND_OUTPUT_CHARS + 1)
    text = raw[:MAX_COMMAND_OUTPUT_CHARS].decode("utf-8", errors="replace").strip()
    if len(raw) > MAX_COMMAND_OUTPUT_CHARS:
        text += "\n... [truncated]"
    return _sanitize_error_text(text)


def _git_security_output(
    args: list[str],
    *,
    cwd: str | Path,
    operation: str,
    timeout: int = 120,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> str:
    """Capture complete bounded Git output for a security decision."""

    with tempfile.TemporaryFile() as output_file, tempfile.TemporaryFile() as error_file:
        try:
            proc = subprocess.run(
                ["git", "--no-replace-objects", *args],
                cwd=Path(cwd).expanduser().resolve(),
                stdout=output_file,
                stderr=error_file,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise PublishError("git is not installed or is not available on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise PublishError(f"{operation} timed out after {timeout} seconds") from exc
        except OSError as exc:
            raise PublishError(f"{operation} could not start: {exc}") from exc

        error = _read_bounded_error(error_file)
        if proc.returncode not in allowed_returncodes:
            raise PublishError(
                f"{operation} failed: {error or f'git exited {proc.returncode}'}"
            )

        size = output_file.seek(0, 2)
        if size > MAX_SCAN_BYTES:
            raise PublishError(
                f"{operation} exceeds the {MAX_SCAN_BYTES}-byte scrub limit; "
                "publishing is blocked. Split the change into smaller reviewed commits."
            )
        output_file.seek(0)
        return output_file.read().decode("utf-8", errors="replace")


def _identity_digest(identities: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(list(identities), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _network_repository_identity(host: str, path: str, *, port: int | None = None) -> str:
    normalized_host = host.strip("[]").rstrip(".").lower()
    decoded_path = unquote(path)
    if (
        not normalized_host
        or not decoded_path
        or any(ord(character) < 32 for character in normalized_host + decoded_path)
    ):
        raise PublishError("Remote destination cannot be normalized safely.")
    path_kind = "absolute" if decoded_path.startswith("/") else "relative"
    normalized_path = posixpath.normpath("/" + decoded_path.lstrip("/")).lstrip("/")
    if normalized_path in {"", "."} or normalized_path.startswith("../"):
        raise PublishError("Remote destination cannot be normalized safely.")
    if normalized_path.lower().endswith(".git"):
        normalized_path = normalized_path[:-4]
    if normalized_host == "github.com":
        normalized_path = normalized_path.lower()
        path_kind = "repository"
    authority = normalized_host if port is None else f"{normalized_host}:{port}"
    return f"network:{authority}/{path_kind}:{normalized_path}"


def _normalized_repository_identity(url: str, cwd: str | Path) -> str:
    """Return a credential-free same-repository identity for a Git URL."""

    if not url or any(ord(character) < 32 for character in url):
        raise PublishError("Remote destination cannot be normalized safely.")

    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url):
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise PublishError("Remote destination cannot be normalized safely.") from exc
        scheme = parsed.scheme.lower()
        if parsed.query or parsed.fragment:
            raise PublishError("Remote destination cannot be normalized safely.")
        if scheme == "file":
            if parsed.hostname not in {None, "", "localhost"}:
                return _network_repository_identity(
                    parsed.hostname or "", parsed.path, port=port
                )
            decoded_path = unquote(parsed.path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:[\\/]", decoded_path):
                decoded_path = decoded_path[1:]
            local_path = Path(decoded_path).expanduser().resolve(strict=False)
            return f"local:{local_path}"
        if scheme not in {"git", "http", "https", "ssh"} or not parsed.hostname:
            raise PublishError("Remote destination cannot be normalized safely.")
        default_ports = {"git": 9418, "http": 80, "https": 443, "ssh": 22}
        normalized_port = None if port in {None, default_ports[scheme]} else port
        return _network_repository_identity(
            parsed.hostname, parsed.path, port=normalized_port
        )

    local_prefix = url.startswith(("/", "./", "../", "~"))
    windows_path = bool(re.match(r"^[A-Za-z]:[\\/]", url))
    if not local_prefix and not windows_path:
        scp_match = re.fullmatch(
            r"(?:(?:[^/@:\s]+)@)?(?P<host>\[[^\]]+\]|[^/:\\\s]+):(?P<path>.+)",
            url,
        )
        if scp_match:
            return _network_repository_identity(
                scp_match.group("host"), scp_match.group("path")
            )

    local_path = Path(url).expanduser()
    if not local_path.is_absolute():
        local_path = Path(cwd).expanduser().resolve() / local_path
    return f"local:{local_path.resolve(strict=False)}"


def _origin_exists(cwd: str | Path) -> bool:
    remotes = _git_security_output(
        ["remote"], cwd=cwd, operation="Resolve configured remotes", timeout=30
    )
    return "origin" in remotes.splitlines()


def _effective_origin_urls(cwd: str | Path, *, push: bool) -> tuple[str, ...]:
    args = ["remote", "get-url"]
    if push:
        args.append("--push")
    args.extend(["--all", "origin"])
    try:
        output = _git_security_output(
            args,
            cwd=cwd,
            operation="Resolve effective origin destinations",
            timeout=30,
        )
    except PublishError:
        raise PublishError("Origin destinations could not be resolved safely.") from None
    return tuple(line for line in output.splitlines() if line)


def _url_rewrite_rules_active(cwd: str | Path) -> bool:
    """Detect effective URL rewrites from every Git configuration scope."""

    for suffix in ("insteadof", "pushinsteadof"):
        output = _git_security_output(
            ["config", "--null", "--get-regexp", rf"^url\..*\.{suffix}$"],
            cwd=cwd,
            operation="Inspect Git URL rewrite configuration",
            timeout=30,
            allowed_returncodes=(0, 1),
        )
        if output:
            return True
    return False


def _remote_routing(cwd: str | Path) -> RemoteRouting:
    if not _origin_exists(cwd):
        empty_digest = _identity_digest(())
        return RemoteRouting(
            origin=False,
            fetch_identities=(),
            push_identities=(),
            fetch_digest=empty_digest,
            push_digest=empty_digest,
            safe=False,
            reason="Origin remote is not configured.",
        )
    rewrite_rules_active = _url_rewrite_rules_active(cwd)
    try:
        fetch_urls = _effective_origin_urls(cwd, push=False)
        push_urls = _effective_origin_urls(cwd, push=True)
        fetch_identities = tuple(
            _normalized_repository_identity(url, cwd)
            for url in fetch_urls
        )
        push_identities = tuple(
            _normalized_repository_identity(url, cwd)
            for url in push_urls
        )
    except PublishError:
        empty_digest = _identity_digest(())
        return RemoteRouting(
            origin=True,
            fetch_identities=(),
            push_identities=(),
            fetch_digest=empty_digest,
            push_digest=empty_digest,
            safe=False,
            reason="Origin contains a destination that cannot be normalized safely.",
        )

    reason = ""
    if rewrite_rules_active:
        reason = (
            "Git URL rewrite configuration is active; remove insteadOf and "
            "pushInsteadOf rules before publishing."
        )
    elif not fetch_identities:
        reason = "Origin has no effective fetch destination."
    elif len(set(fetch_identities)) != 1:
        reason = "Origin resolves to inconsistent fetch repository identities."
    elif len(push_identities) != 1:
        reason = "Origin must resolve to exactly one effective push destination."
    elif push_identities[0] != fetch_identities[0]:
        reason = "Origin push destination does not match its fetch repository identity."
    return RemoteRouting(
        origin=True,
        fetch_identities=fetch_identities,
        push_identities=push_identities,
        fetch_digest=_identity_digest(fetch_identities),
        push_digest=_identity_digest(push_identities),
        safe=not reason,
        reason=reason,
        bound_push_url=push_urls[0] if len(push_urls) == 1 else "",
    )


def _require_safe_remote_routing(cwd: str | Path) -> RemoteRouting:
    routing = _remote_routing(cwd)
    if not routing.safe:
        raise PublishError(routing.reason)
    return routing


def _github_repository_slug(routing: RemoteRouting) -> str:
    if not routing.safe or len(routing.fetch_identities) != 1:
        raise PublishError("A unique reviewed GitHub repository is required.")
    prefix = "network:github.com/repository:"
    identity = routing.fetch_identities[0]
    if not identity.startswith(prefix):
        raise PublishError(
            "Pull-request automation currently requires an origin on github.com."
        )
    path = identity[len(prefix):]
    if len(path.split("/")) != 2:
        raise PublishError("The reviewed GitHub repository identity is invalid.")
    return f"github.com/{path}"


def _require_no_object_overlays(cwd: str | Path) -> None:
    replacements = _git_security_output(
        ["for-each-ref", "--format=%(refname)", "refs/replace"],
        cwd=cwd,
        operation="Inspect Git replacement refs",
        timeout=30,
    )
    if replacements.strip():
        raise PublishError(
            "Git replacement refs are active; remove them before structured publishing."
        )
    grafts_value = _git(
        ["rev-parse", "--git-path", "info/grafts"],
        cwd=cwd,
        operation="Resolve Git grafts path",
        timeout=30,
    ).strip()
    grafts_path = Path(grafts_value)
    if not grafts_path.is_absolute():
        grafts_path = Path(cwd).expanduser().resolve() / grafts_path
    if grafts_path.exists():
        raise PublishError(
            "Git graft metadata is present; remove it before structured publishing."
        )


def _require_no_in_progress_git_operation(cwd: str | Path) -> None:
    markers = (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
        "rebase-apply",
        "rebase-merge",
        "sequencer",
    )
    for marker in markers:
        value = _git(
            ["rev-parse", "--git-path", marker],
            cwd=cwd,
            operation="Inspect Git operation state",
            timeout=30,
        ).strip()
        path = Path(value)
        if not path.is_absolute():
            path = Path(cwd).expanduser().resolve() / path
        if path.exists():
            raise PublishError(
                "A merge, rebase, cherry-pick, revert, or bisect is in progress; "
                "finish or abort it before structured publishing."
            )


def _try_git(args: list[str], *, cwd: str | Path) -> str:
    rc, output = _run_program(
        "git", ["--no-replace-objects", *args], cwd=cwd, timeout=30
    )
    return output if rc == 0 else ""


def _branch(cwd: str | Path) -> str:
    branch = _try_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd)
    if not branch:
        raise PublishError("Publishing from a detached HEAD is not supported.")
    return branch.splitlines()[0].strip()


def _require_feature_branch(cwd: str | Path) -> str:
    branch = _branch(cwd)
    if branch.lower() in PROTECTED_BRANCHES:
        raise PublishError(
            f"Refusing structured publish from protected branch '{branch}'. "
            "Create or activate a feature worktree first."
        )
    return branch


def _status_porcelain(cwd: str | Path) -> str:
    return _git(
        ["status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=cwd,
        operation="Git status",
        timeout=30,
    )


def publish_fingerprint(cwd: str | Path) -> str:
    """Hash all local publish inputs without persisting paths or secret-bearing URLs."""

    _require_no_object_overlays(cwd)
    snapshot = git_evidence.capture_git_snapshot(str(cwd))
    if not snapshot.available:
        raise PublishError(snapshot.error or "Git state is unavailable.")
    staged = _git_security_output(
        [
            "diff",
            "--cached",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--text",
            "--binary",
        ],
        cwd=cwd,
        operation="Read staged fingerprint diff",
        timeout=60,
    )
    remote_refs = _git_security_output(
        ["for-each-ref", "--format=%(refname):%(objectname)", "refs/remotes/origin"],
        cwd=cwd,
        operation="Read remote-ref fingerprint state",
        timeout=60,
    )
    routing = _remote_routing(cwd)
    payload = {
        "branch": _branch(cwd),
        "head": snapshot.head or "",
        "tracked": snapshot.tracked_diff_digest,
        "untracked": snapshot.untracked_digest,
        "staged": hashlib.sha256(staged.encode("utf-8", errors="replace")).hexdigest(),
        "upstream": _upstream(cwd),
        "fetch_destination_digest": routing.fetch_digest,
        "push_destination_digest": routing.push_digest,
        "remote_routing_safe": routing.safe,
        "remote_refs_digest": hashlib.sha256(
            remote_refs.encode("utf-8", errors="replace")
        ).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _require_expected_fingerprint(cwd: str | Path, expected: str) -> None:
    if not expected:
        return
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise PublishError("Expected publish fingerprint must be a 64-character SHA-256 value.")
    actual = publish_fingerprint(cwd)
    if actual.lower() != expected.lower():
        raise PublishError(
            "Publish state changed since the reviewed plan. Run /ship status again before executing."
        )


def _upstream(cwd: str | Path) -> str:
    return _try_git(["rev-parse", "--abbrev-ref", "@{upstream}"], cwd=cwd).strip()


def _ahead_behind(cwd: str | Path, upstream: str) -> tuple[int, int]:
    if not upstream:
        return 0, 0
    raw = _try_git(
        ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"],
        cwd=cwd,
    )
    try:
        behind_text, ahead_text = raw.split()
        return int(ahead_text), int(behind_text)
    except (ValueError, TypeError):
        return 0, 0


def _strict_ahead_behind(
    cwd: str | Path,
    ref: str,
    *,
    head: str = "HEAD",
) -> tuple[int, int]:
    raw = _git(
        ["rev-list", "--left-right", "--count", f"{ref}...{head}"],
        cwd=cwd,
        operation=f"Resolve divergence from {ref}",
        timeout=30,
    )
    try:
        behind_text, ahead_text = raw.split()
        return int(ahead_text), int(behind_text)
    except (TypeError, ValueError) as exc:
        raise PublishError(f"Could not parse divergence from {ref}: {raw or 'no output'}") from exc


def _remote_default(cwd: str | Path) -> tuple[str, bool]:
    """Return the fetched origin default tracking ref and whether origin is empty."""

    rc, output = _run_program(
        "git",
        ["ls-remote", "--symref", "origin", "HEAD"],
        cwd=cwd,
        timeout=60,
    )
    if rc != 0:
        raise PublishError(f"Could not resolve origin's default branch: {output or 'git failed'}")
    target = ""
    for line in output.splitlines():
        match = re.match(r"ref:\s+refs/heads/(\S+)\s+HEAD$", line.strip())
        if match:
            target = match.group(1)
            break
    if target:
        tracking = f"origin/{target}"
        if not _try_git(["rev-parse", "--verify", tracking], cwd=cwd):
            raise PublishError(
                f"Origin reports default branch '{target}', but its fetched ref is unavailable."
            )
        return tracking, False

    heads_rc, heads_output = _run_program(
        "git",
        ["ls-remote", "--heads", "origin"],
        cwd=cwd,
        timeout=60,
    )
    if heads_rc != 0:
        raise PublishError(f"Could not inspect origin branches: {heads_output or 'git failed'}")
    heads = [line for line in heads_output.splitlines() if line.strip()]
    if not heads:
        return "", True
    raise PublishError(
        "Origin has branches but no unambiguous default HEAD. Configure the remote default branch first."
    )


def _empty_tree(cwd: str | Path) -> str:
    rc, output = _run_program("git", ["mktree"], cwd=cwd, timeout=30, input_text="")
    if rc != 0 or not output.strip():
        raise PublishError(f"Could not create an empty-tree scan baseline: {output or 'git failed'}")
    return output.strip().splitlines()[0]


def _added_lines(diff: str) -> str:
    return "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def scan_outgoing_diff(diff: str) -> list[str]:
    size = len(diff.encode("utf-8", errors="replace"))
    if size > MAX_SCAN_BYTES:
        return [f"outgoing diff exceeds the {MAX_SCAN_BYTES}-byte scrub limit"]
    added = _added_lines(diff)
    findings = _secret_findings(added)
    if _LFS_OID_PATTERN.search(added):
        findings.append("Git LFS pointer requires separate payload review")
    return findings


def _secret_findings(text: str) -> list[str]:
    return [label for label, pattern in _SECRET_PATTERNS if pattern.search(text)]


def _require_secret_free_metadata(values: tuple[str, ...], *, label: str) -> None:
    findings = _secret_findings("\0".join(values))
    if findings:
        raise PublishError(
            f"{label} scrub blocked publishing: " + ", ".join(sorted(set(findings)))
        )


def _outgoing_commit_ids(
    cwd: str | Path,
    *,
    base: str,
    head_oid: str,
    remote_empty: bool,
) -> tuple[str, ...]:
    revision = head_oid if remote_empty else f"{base}..{head_oid}"
    output = _git_security_output(
        ["rev-list", "--reverse", "--topo-order", revision],
        cwd=cwd,
        operation="Enumerate immutable outgoing commits",
        timeout=60,
    )
    commits = tuple(line.strip() for line in output.splitlines() if line.strip())
    if any(not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) for commit in commits):
        raise PublishError("Outgoing commit enumeration returned an invalid object ID.")
    return commits


def _scrub_outgoing_history(
    cwd: str | Path,
    *,
    base: str,
    head_oid: str,
    remote_empty: bool,
) -> str:
    """Scrub each outgoing commit delta and its immutable metadata."""

    commits = _outgoing_commit_ids(
        cwd, base=base, head_oid=head_oid, remote_empty=remote_empty
    )
    scanned_bytes = 0
    scrub_reason = "Outgoing history contains no commit delta to scrub."

    def consume(value: str, *, operation: str) -> None:
        nonlocal scanned_bytes
        scanned_bytes += len(value.encode("utf-8", errors="replace"))
        if scanned_bytes > MAX_SCAN_BYTES:
            raise PublishError(
                f"{operation} exceeds the cumulative {MAX_SCAN_BYTES}-byte scrub limit; "
                "publishing is blocked. Split the history into smaller reviewed changes."
            )

    for commit in commits:
        parent_line = _git(
            ["rev-list", "--parents", "-n", "1", commit],
            cwd=cwd,
            operation="Resolve outgoing commit parent",
            timeout=30,
        ).split()
        if not parent_line or parent_line[0] != commit:
            raise PublishError("Outgoing commit parent evidence is invalid.")
        parent = parent_line[1] if len(parent_line) > 1 else _empty_tree(cwd)
        commit_metadata = _git_security_output(
            ["cat-file", "commit", commit],
            cwd=cwd,
            operation="Read outgoing commit metadata",
            timeout=30,
        )
        consume(commit_metadata, operation="Outgoing commit-metadata scrub")
        _require_secret_free_metadata(
            (commit_metadata,), label="Outgoing-commit-metadata"
        )
        commit_paths = _nul_git_paths(
            [
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                "--no-ext-diff",
                "--no-textconv",
                f"{parent}..{commit}",
            ],
            cwd=cwd,
            operation="Read outgoing commit paths",
        )
        consume("\0".join(commit_paths), operation="Outgoing path scrub")
        _require_secret_free_metadata(commit_paths, label="Outgoing-path")
        commit_diff = _git_security_output(
            [
                "diff",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--text",
                "--unified=0",
                f"{parent}..{commit}",
            ],
            cwd=cwd,
            operation="Read outgoing commit delta",
            timeout=60,
        )
        consume(commit_diff, operation="Outgoing history scrub")
        scrub_reason = _require_scrubbed(commit_diff)
    return scrub_reason


def _require_scrubbed(diff: str) -> str:
    findings = scan_outgoing_diff(diff)
    if findings:
        raise PublishError(
            "Outgoing-diff scrub blocked publishing: " + ", ".join(sorted(set(findings)))
        )
    evidence = ScrubEvidence(
        digest=hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest(),
        scanned_chars=len(diff),
        findings=tuple(findings),
    )
    gate = PrePushGate().check(scrub_evidence=evidence)
    if not gate.allowed:  # Defensive: structured path must still fail closed.
        raise PublishError(gate.reason)
    return gate.reason


def _internal_release_scan_notice() -> str:
    """Describe the trust boundary after Algo CLI's internal scrub succeeds."""

    return (
        "Internal outgoing-history scrub passed; repository-provided release scripts "
        "were not executed. The CI public-release scan remains a separate required gate."
    )


def publish_status(cwd: str | Path) -> dict[str, Any]:
    try:
        context = repository_context(cwd)
    except WorktreeError as exc:
        raise PublishError(str(exc)) from exc
    branch = _branch(cwd)
    routing = _remote_routing(cwd)
    upstream = _upstream(cwd)
    ahead, behind = _ahead_behind(cwd, upstream)
    status = _status_porcelain(cwd)
    remote_default = _try_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=cwd,
    ).strip()
    diff_check_rc, diff_check_output = _run_program(
        "git",
        ["diff", "--check"],
        cwd=cwd,
        timeout=30,
    )
    return {
        "workspace": context["workspace_root"],
        "repository": context["repository_root"],
        "branch": branch,
        "head": context["head"],
        "protected_branch": branch.lower() in PROTECTED_BRANCHES,
        "has_changes": bool(status.strip()),
        "status": status,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "origin": routing.origin,
        "remote_routing_safe": routing.safe,
        "remote_routing_error": routing.reason,
        "fetch_destination_digest": routing.fetch_digest,
        "push_destination_digest": routing.push_digest,
        "remote_default": remote_default,
        "remote_default_branch": remote_default.removeprefix("origin/"),
        "gh_available": shutil.which("gh") is not None,
        "diff_check": diff_check_rc == 0,
        "diff_check_error": "" if diff_check_rc == 0 else diff_check_output,
        "fingerprint": publish_fingerprint(cwd),
    }


def format_publish_plan(cwd: str | Path) -> str:
    status = publish_status(cwd)
    lines = [
        "Structured publish plan:",
        f"- workspace: {status['workspace']}",
        f"- branch: {status['branch']} @ {status['head'][:12]}",
        f"- changes: {'present' if status['has_changes'] else 'none'}",
        f"- upstream: {status['upstream'] or 'not configured'}",
        f"- divergence: ahead {status['ahead']} · behind {status['behind']}",
        f"- origin: {'ready' if status['origin'] else 'missing'}",
        f"- push destination digest: {status['push_destination_digest']}",
        f"- GitHub CLI: {'available' if status['gh_available'] else 'unavailable'}",
        f"- diff check: {'pass' if status['diff_check'] else 'fail'}",
        f"- fingerprint: {status['fingerprint']}",
    ]
    blockers = []
    if status["protected_branch"]:
        blockers.append("protected branch")
    if not status["origin"]:
        blockers.append("origin remote missing")
    elif not status["remote_routing_safe"]:
        blockers.append(status["remote_routing_error"])
    if status["remote_default_branch"] == status["branch"]:
        blockers.append("remote default branch")
    if not status["diff_check"]:
        blockers.append("git diff --check failed")
    lines.append(f"- readiness: {'blocked — ' + ', '.join(blockers) if blockers else 'ready'}")
    lines.append(
        "Next: /ship commit MESSAGE, /ship push, /ship pr [--ready], or /ship all MESSAGE."
    )
    return "\n".join(lines)


def _commit_subject(message: str) -> str:
    subject = " ".join((message or "").split()).rstrip(".")
    if not subject:
        raise PublishError("Commit message is required.")
    return subject[:72].rstrip()


def _validated_paths(cwd: str | Path, paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        return ()
    try:
        root = Path(repository_context(cwd)["workspace_root"]).resolve()
    except WorktreeError as exc:
        raise PublishError(str(exc)) from exc
    normalized: list[str] = []
    for raw in paths:
        value = (raw or "").strip()
        if not value:
            continue
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            raise PublishError("--files paths must be relative to the active workspace.")
        resolved = (root / candidate).resolve(strict=False)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise PublishError(f"Selected path escapes the active workspace: {value}") from exc
        if relative == ".git" or relative.startswith(".git/"):
            raise PublishError("Git metadata cannot be selected for a commit.")
        if relative not in normalized:
            normalized.append(relative)
    if not normalized:
        raise PublishError("--files requires at least one workspace-relative path.")
    return tuple(normalized)


def _literal_pathspecs(paths: tuple[str, ...]) -> list[str]:
    return [f":(literal){path}" for path in paths]


def _path_within_scopes(path: str, scopes: tuple[str, ...]) -> bool:
    return any(
        scope == "."
        or path == scope
        or path.startswith(f"{scope.rstrip('/')}/")
        for scope in scopes
    )


def _nul_git_paths(
    args: list[str],
    *,
    cwd: str | Path,
    operation: str,
) -> tuple[str, ...]:
    output = _git_security_output(args, cwd=cwd, operation=operation, timeout=60)
    return tuple(path for path in output.split("\0") if path)


def _branch_head_snapshot(cwd: str | Path) -> dict[str, str]:
    output = _git_security_output(
        ["for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads"],
        cwd=cwd,
        operation="Capture branch-ref state",
        timeout=30,
    )
    snapshot: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            continue
        try:
            ref, oid = line.split("\0", 1)
        except ValueError as exc:
            raise PublishError("Branch-ref evidence could not be parsed safely.") from exc
        snapshot[ref] = oid
    return snapshot


def _rollback_unverified_commit(
    cwd: str | Path,
    *,
    branch: str,
    old_head: str,
    new_head: str,
    heads_before: dict[str, str],
) -> None:
    heads_after = _branch_head_snapshot(cwd)
    advanced_refs = sorted(
        ref
        for ref, oid in heads_after.items()
        if oid == new_head and heads_before.get(ref) != new_head
    )
    if len(advanced_refs) != 1:
        raise PublishError(
            "A unique branch ref containing the unverified commit could not be identified."
        )
    commands = ["start"]
    for ref in advanced_refs:
        previous = heads_before.get(ref)
        if previous:
            commands.append(f"update {ref} {previous} {new_head}")
        else:
            commands.append(f"delete {ref} {new_head}")
    commands.extend(["prepare", "commit", ""])
    rc, _output = _run_program(
        "git",
        ["--no-replace-objects", "update-ref", "--stdin"],
        cwd=cwd,
        timeout=30,
        input_text="\n".join(commands),
    )
    if rc != 0:
        raise PublishError("Atomic branch rollback failed.")
    _git(
        ["symbolic-ref", "HEAD", f"refs/heads/{branch}"],
        cwd=cwd,
        operation="Restore reviewed branch",
        timeout=30,
    )
    original_ref = f"refs/heads/{branch}"
    restored = _try_git(["rev-parse", "--verify", original_ref], cwd=cwd).strip()
    if restored != old_head or _branch(cwd) != branch:
        raise PublishError("Branch rollback could not restore the reviewed branch state.")


def commit_all(
    cwd: str | Path,
    message: str,
    *,
    expected_fingerprint: str = "",
    paths: list[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    _require_no_object_overlays(cwd)
    _require_no_in_progress_git_operation(cwd)
    _require_expected_fingerprint(cwd, expected_fingerprint)
    branch = _require_feature_branch(cwd)
    _require_secret_free_metadata((branch,), label="Branch-name")
    subject = _commit_subject(message)
    _require_secret_free_metadata((subject,), label="Commit-subject")
    selected = _validated_paths(cwd, paths)
    literal_selected = _literal_pathspecs(selected)
    status_args = ["status", "--porcelain=v1", "--untracked-files=normal"]
    if selected:
        status_args.extend(["--", *literal_selected])
    selected_status = _git(status_args, cwd=cwd, operation="Git status", timeout=30)
    if not selected_status.strip():
        raise PublishError("There are no changes to commit.")
    if selected:
        pre_staged = _nul_git_paths(
            [
                "diff",
                "--cached",
                "--name-only",
                "--no-renames",
                "-z",
                "--no-ext-diff",
                "--no-textconv",
            ],
            cwd=cwd,
            operation="Read pre-staged paths",
        )
        outside = sorted(
            path
            for path in pre_staged
            if not _path_within_scopes(path, selected)
        )
        if outside:
            raise PublishError(
                "Selected-file commit would include pre-staged paths outside its scope: "
                + ", ".join(outside[:10])
            )
    diff_check_args = ["diff", "--check"] + (
        ["--", *literal_selected] if selected else []
    )
    _git(diff_check_args, cwd=cwd, operation="Working-tree diff check", timeout=30)
    _git(
        ["add", "-A", "--", *literal_selected]
        if selected
        else ["add", "-A", "--"],
        cwd=cwd,
        operation="Stage changes",
        timeout=60,
    )
    staged_paths = _nul_git_paths(
        [
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
            "--no-ext-diff",
            "--no-textconv",
        ],
        cwd=cwd,
        operation="Read staged paths",
    )
    _require_secret_free_metadata(staged_paths, label="Staged-path")
    if selected:
        outside = sorted(
            path for path in staged_paths if not _path_within_scopes(path, selected)
        )
        if outside:
            raise PublishError(
                "Staging escaped the selected-file scope: " + ", ".join(outside[:10])
            )
    staged_args = [
        "diff",
        "--cached",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--text",
        "--unified=0",
    ]
    if selected:
        staged_args.extend(["--", *literal_selected])
    staged = _git_security_output(
        staged_args,
        cwd=cwd,
        operation="Read staged diff",
        timeout=60,
    )
    if not staged.strip():
        raise PublishError("No staged content remains after Git normalization.")
    scrub_reason = _require_scrubbed(staged)
    public_scan = _internal_release_scan_notice()
    _git(["diff", "--cached", "--check"], cwd=cwd, operation="Staged diff check", timeout=30)
    scanned_tree = _git(
        ["write-tree"], cwd=cwd, operation="Capture scanned staged tree", timeout=30
    ).strip()
    old_head = _try_git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=cwd).strip()
    heads_before = _branch_head_snapshot(cwd)
    _git(["commit", "-m", subject], cwd=cwd, operation="Commit", timeout=600)
    head = _git(
        ["rev-parse", "--verify", "HEAD^{commit}"],
        cwd=cwd,
        operation="Resolve committed HEAD",
        timeout=30,
    ).strip()
    rollback_safe = False
    try:
        parent_line = _git(
            ["rev-list", "--parents", "-n", "1", head],
            cwd=cwd,
            operation="Verify committed parent",
            timeout=30,
        ).split()
        expected_parent_line = [head] + ([old_head] if old_head else [])
        if parent_line != expected_parent_line:
            raise PublishError("Created commit does not have the reviewed parent.")
        rollback_safe = True
        committed_tree = _git(
            ["rev-parse", "--verify", f"{head}^{{tree}}"],
            cwd=cwd,
            operation="Verify committed tree",
            timeout=30,
        ).strip()
        base = old_head or _empty_tree(cwd)
        committed_diff = _git_security_output(
            [
                "diff",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--text",
                "--unified=0",
                f"{base}..{head}",
            ],
            cwd=cwd,
            operation="Read immutable committed diff",
            timeout=60,
        )
        scrub_reason = _require_scrubbed(committed_diff)
        committed_metadata = _git_security_output(
            ["cat-file", "commit", head],
            cwd=cwd,
            operation="Read immutable committed metadata",
            timeout=30,
        )
        _require_secret_free_metadata(
            (committed_metadata,), label="Committed-commit-metadata"
        )
        committed_paths = _nul_git_paths(
            [
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                "-z",
                head,
            ],
            cwd=cwd,
            operation="Read immutable committed paths",
        )
        _require_secret_free_metadata(committed_paths, label="Committed-path")
        if selected:
            outside = sorted(
                path
                for path in committed_paths
                if not _path_within_scopes(path, selected)
            )
            if outside:
                raise PublishError(
                    "Created commit escaped the selected-file scope: "
                    + ", ".join(outside[:10])
                )
        if committed_tree != scanned_tree:
            raise PublishError("Created commit tree differs from the scanned staged tree.")
        if _branch(cwd) != branch:
            raise PublishError("Created commit is no longer on the reviewed branch.")
        branch_head = _git(
            ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
            cwd=cwd,
            operation="Verify created branch head",
            timeout=30,
        ).strip()
        if branch_head != head:
            raise PublishError("Reviewed branch no longer points to the created commit.")
    except PublishError as verification_error:
        if not rollback_safe:
            raise PublishError(
                f"SEVERE: commit state changed concurrently around {head[:12]}; "
                "automatic rollback was not safe. Inspect the branch before any push."
            ) from verification_error
        try:
            _rollback_unverified_commit(
                cwd,
                branch=branch,
                old_head=old_head,
                new_head=head,
                heads_before=heads_before,
            )
        except PublishError as rollback_error:
            raise PublishError(
                f"SEVERE: unverified commit {head[:12]} was created and automatic "
                "branch rollback failed. Do not push this branch."
            ) from rollback_error
        raise PublishError(
            f"Commit {head[:12]} failed post-commit verification and was rolled back; "
            f"hook-added content remains staged for inspection. {verification_error}"
        ) from verification_error
    return {
        "status": "created",
        "branch": branch,
        "head": head,
        "subject": subject,
        "scrub": scrub_reason,
        "public_scan": public_scan,
        "paths": ",".join(selected) if selected else "all",
    }


def push_branch(
    cwd: str | Path,
    *,
    expected_fingerprint: str = "",
) -> dict[str, str]:
    _require_no_object_overlays(cwd)
    branch = _require_feature_branch(cwd)
    _require_secret_free_metadata((branch,), label="Branch-name")
    if _status_porcelain(cwd).strip():
        raise PublishError("Refusing to push with uncommitted changes.")
    initial_routing = _require_safe_remote_routing(cwd)
    _git(["fetch", "--prune", "origin"], cwd=cwd, operation="Refresh origin", timeout=300)
    post_fetch_routing = _require_safe_remote_routing(cwd)
    if post_fetch_routing.push_digest != initial_routing.push_digest:
        raise PublishError("Origin push destination changed while refreshing the remote.")
    _require_expected_fingerprint(cwd, expected_fingerprint)
    _git(["diff", "--check"], cwd=cwd, operation="Pre-push diff check", timeout=30)
    if _branch(cwd) != branch:
        raise PublishError("Active branch changed while preparing the publish operation.")
    head_oid = _git(
        ["rev-parse", "--verify", "HEAD^{commit}"],
        cwd=cwd,
        operation="Resolve immutable publish HEAD",
        timeout=30,
    ).strip()
    upstream = _upstream(cwd)
    expected_upstream = f"origin/{branch}"
    if upstream and upstream != expected_upstream:
        raise PublishError(
            f"Feature branch upstream must be exactly {expected_upstream}; found {upstream}. "
            "Correct the branch tracking configuration before pushing."
        )
    if upstream:
        _upstream_ahead, upstream_behind = _strict_ahead_behind(
            cwd, upstream, head=head_oid
        )
        if upstream_behind:
            raise PublishError(
                f"Local branch is behind its upstream by {upstream_behind} commit(s); reconcile before pushing."
            )
    remote_default, remote_empty = _remote_default(cwd)
    if remote_empty:
        base = _empty_tree(cwd)
    else:
        if remote_default == f"origin/{branch}":
            raise PublishError(
                f"Refusing to publish directly to origin's actual default branch '{branch}'."
            )
        _default_ahead, default_behind = _strict_ahead_behind(
            cwd, remote_default, head=head_oid
        )
        if default_behind:
            raise PublishError(
                f"Feature branch is behind {remote_default} by {default_behind} commit(s); update it before pushing."
            )
        base = _git(
            ["merge-base", head_oid, remote_default],
            cwd=cwd,
            operation=f"Resolve merge base with {remote_default}",
            timeout=30,
        ).strip()
    scrub_reason = _scrub_outgoing_history(
        cwd,
        base=base,
        head_oid=head_oid,
        remote_empty=remote_empty,
    )
    public_scan = _internal_release_scan_notice()
    final_routing = _require_safe_remote_routing(cwd)
    if final_routing.push_digest != post_fetch_routing.push_digest:
        raise PublishError("Origin push destination changed during publish preparation.")
    if _branch(cwd) != branch:
        raise PublishError("Active branch changed during publish preparation.")
    current_oid = _git(
        ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
        cwd=cwd,
        operation="Recheck immutable publish HEAD",
        timeout=30,
    ).strip()
    if current_oid != head_oid:
        raise PublishError(
            "Feature branch advanced after outgoing content was scanned; publishing is blocked."
        )
    refspec = f"{head_oid}:refs/heads/{branch}"
    remote_tracking_ref = f"refs/remotes/origin/{branch}"
    old_tracking_oid = _try_git(
        ["rev-parse", "--verify", remote_tracking_ref], cwd=cwd
    ).strip()
    _push_bound_destination(
        cwd=cwd,
        push_url=final_routing.bound_push_url,
        refspec=refspec,
        timeout=600,
    )
    expected_tracking_oid = old_tracking_oid or ("0" * len(head_oid))
    update_tracking_args = [
        "update-ref",
        remote_tracking_ref,
        head_oid,
        expected_tracking_oid,
    ]
    _git(
        update_tracking_args,
        cwd=cwd,
        operation="Record reviewed remote-tracking state",
        timeout=30,
    )
    if not upstream:
        _git(
            ["branch", "--set-upstream-to", expected_upstream, branch],
            cwd=cwd,
            operation="Bind feature-branch upstream",
            timeout=30,
        )
    configured_upstream = _upstream(cwd)
    if configured_upstream != expected_upstream:
        raise PublishError(
            f"Push completed, but the branch upstream is {configured_upstream or 'unset'}; "
            f"expected {expected_upstream}."
        )
    return {
        "status": "pushed",
        "branch": branch,
        "upstream": configured_upstream,
        "scrub": scrub_reason,
        "public_scan": public_scan,
    }


def create_pull_request(cwd: str | Path, *, draft: bool = True) -> dict[str, Any]:
    _require_no_object_overlays(cwd)
    branch = _require_feature_branch(cwd)
    _require_secret_free_metadata((branch,), label="Branch-name")
    initial_routing = _require_safe_remote_routing(cwd)
    github_repo = _github_repository_slug(initial_routing)
    initial_default, initial_remote_empty = _remote_default(cwd)
    if not initial_remote_empty and initial_default == f"origin/{branch}":
        raise PublishError(
            f"Refusing a pull request from origin's actual default branch '{branch}'."
        )
    if shutil.which("gh") is None:
        raise PublishError("GitHub CLI is unavailable; install and authenticate `gh` first.")
    auth_rc, auth_output = _run_program(
        "gh", ["auth", "status", "--hostname", "github.com"], cwd=cwd, timeout=30
    )
    if auth_rc != 0:
        raise PublishError(f"GitHub authentication is unavailable: {auth_output or 'gh auth status failed'}")

    view_rc, view_output = _run_program(
        "gh",
        [
            "pr",
            "view",
            branch,
            "--repo",
            github_repo,
            "--json",
            "url,number,title,state",
        ],
        cwd=cwd,
        timeout=30,
    )
    if view_rc == 0 and view_output:
        try:
            existing = json.loads(view_output)
        except json.JSONDecodeError:
            existing = {"url": view_output.strip()}
        if str(existing.get("state") or "OPEN").upper() == "OPEN":
            return {"status": "existing", "branch": branch, **existing}

    if _status_porcelain(cwd).strip():
        raise PublishError("Refusing to create a pull request with uncommitted changes.")
    _git(["fetch", "--prune", "origin"], cwd=cwd, operation="Refresh origin", timeout=300)
    post_fetch_routing = _require_safe_remote_routing(cwd)
    if post_fetch_routing.push_digest != initial_routing.push_digest:
        raise PublishError("Origin push destination changed while refreshing the remote.")
    upstream = _upstream(cwd)
    if not upstream:
        raise PublishError("Feature branch has no upstream; run /ship push first.")
    expected_upstream = f"origin/{branch}"
    if upstream != expected_upstream:
        raise PublishError(
            f"Feature branch upstream must be exactly {expected_upstream}; found {upstream}."
        )
    ahead, behind = _strict_ahead_behind(cwd, upstream)
    if behind:
        raise PublishError(
            f"Local branch is behind its upstream by {behind} commit(s); reconcile before creating a PR."
        )
    if ahead:
        raise PublishError(
            f"Local branch has {ahead} unpushed commit(s); run /ship push before creating a PR."
        )
    remote_default, remote_empty = _remote_default(cwd)
    if remote_empty:
        raise PublishError("Origin has no default branch; configure it before creating a pull request.")
    if remote_default == f"origin/{branch}":
        raise PublishError(
            f"Refusing a pull request from origin's actual default branch '{branch}'."
        )
    _default_ahead, default_behind = _strict_ahead_behind(cwd, remote_default)
    if default_behind:
        raise PublishError(
            f"Feature branch is behind {remote_default} by {default_behind} commit(s); update it first."
        )

    base_branch = remote_default.removeprefix("origin/")
    args = [
        "pr",
        "create",
        "--repo",
        github_repo,
        "--head",
        branch,
        "--base",
        base_branch,
        "--fill",
    ]
    if draft:
        args.append("--draft")
    create_rc, create_output = _run_program("gh", args, cwd=cwd, timeout=120)
    if create_rc != 0:
        raise PublishError(f"Pull-request creation failed: {create_output or 'gh exited non-zero'}")
    url = next(
        (line.strip() for line in create_output.splitlines() if line.strip().startswith("http")),
        create_output.strip(),
    )
    return {"status": "created", "branch": branch, "url": url, "draft": draft}


def format_result(label: str, result: dict[str, Any]) -> str:
    if label == "commit":
        return f"Committed {result['head'][:12]} on {result['branch']} · {result['subject']}"
    if label == "push":
        return f"Pushed {result['branch']} to {result['upstream']} after outgoing-history scrub."
    if label == "pr":
        state = "Found existing" if result.get("status") == "existing" else "Created"
        kind = "draft pull request" if result.get("draft") else "pull request"
        return f"{state} {kind}: {result.get('url', '(URL unavailable)')}"
    return json.dumps(result, indent=2)


def _parse_options(remainder: str) -> tuple[str, tuple[str, ...], str]:
    try:
        parts = shlex.split(remainder)
    except ValueError as exc:
        raise PublishError(f"Invalid publish arguments: {exc}") from exc
    expected = ""
    if "--expect" in parts:
        index = parts.index("--expect")
        if index + 1 >= len(parts):
            raise PublishError("--expect requires the fingerprint printed by /ship status.")
        expected = parts[index + 1]
        del parts[index:index + 2]
    paths: list[str] = []
    while "--files" in parts:
        index = parts.index("--files")
        if index + 1 >= len(parts):
            raise PublishError("--files requires a comma-separated path list.")
        paths.extend(item for item in parts[index + 1].split(",") if item)
        del parts[index:index + 2]
    return expected, tuple(paths), " ".join(parts)


def handle_command(arg: str, cfg: Any) -> str:
    text = (arg or "").strip()
    sub, _, remainder = text.partition(" ")
    sub = sub.lower() or "status"
    remainder = remainder.strip()
    if sub in {"status", "plan", "show"}:
        return format_publish_plan(cfg.cwd)
    if sub in {"help", "?"}:
        return (
            "Usage: /ship status | commit MESSAGE | push | pr [--ready] | "
            "all [--ready] MESSAGE"
        )
    if sub == "commit":
        expected, paths, message = _parse_options(remainder)
        return format_result(
            "commit",
            commit_all(
                cfg.cwd,
                message,
                expected_fingerprint=expected,
                paths=paths,
            ),
        )
    if sub == "push":
        expected, paths, extra = _parse_options(remainder)
        if extra or paths:
            raise PublishError("Usage: /ship push [--expect FINGERPRINT]")
        return format_result(
            "push",
            push_branch(cfg.cwd, expected_fingerprint=expected),
        )
    if sub == "pr":
        if remainder not in {"", "--ready"}:
            raise PublishError("Usage: /ship pr [--ready]")
        return format_result("pr", create_pull_request(cfg.cwd, draft=remainder != "--ready"))
    if sub == "all":
        draft = True
        expected, paths, message = _parse_options(remainder)
        if message == "--ready" or message.startswith("--ready "):
            draft = False
            message = message[len("--ready"):].strip()
        results: list[str] = []
        if _status_porcelain(cfg.cwd).strip():
            results.append(
                format_result(
                    "commit",
                    commit_all(
                        cfg.cwd,
                        message,
                        expected_fingerprint=expected,
                        paths=paths,
                    ),
                )
            )
        elif message:
            _require_expected_fingerprint(cfg.cwd, expected)
            results.append("No commit created; the worktree is already clean.")
        results.append(format_result("push", push_branch(cfg.cwd)))
        results.append(format_result("pr", create_pull_request(cfg.cwd, draft=draft)))
        return "\n".join(results)
    raise PublishError(
        "Usage: /ship status | commit MESSAGE | push | pr [--ready] | "
        "all [--ready] MESSAGE"
    )
