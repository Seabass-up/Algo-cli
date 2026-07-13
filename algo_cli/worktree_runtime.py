"""Safe, persistent Git worktree lifecycle for Algo CLI agent sessions."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config, git_evidence


WORKTREE_STORE_NAME = "worktrees.json"
WORKTREE_SCHEMA_VERSION = 1
MAX_WORKTREE_RECORDS = 100
MAX_NAME_CHARS = 64
MAX_GIT_OUTPUT_CHARS = 8_000
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^+\-]{0,199}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


class WorktreeError(RuntimeError):
    """A safe, user-facing worktree lifecycle failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_path() -> Path:
    return config.CONFIG_DIR / WORKTREE_STORE_NAME


def managed_root(repository_root: str | Path | None = None) -> Path:
    """Return a worktree root that can never sit inside its source repository."""

    configured = (config.CONFIG_DIR / "worktrees").expanduser().resolve()
    if repository_root is None:
        return configured
    repository = Path(repository_root).expanduser().resolve()
    try:
        configured.relative_to(repository)
    except ValueError:
        return configured
    return repository.parent / ".algo-cli-worktrees"


def _run_git(
    args: list[str],
    *,
    cwd: str | Path,
    timeout: int = 30,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=Path(cwd).expanduser().resolve(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("Git is not installed or is not available on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"Git command timed out after {timeout} seconds.") from exc
    except OSError as exc:
        raise WorktreeError(f"Git command could not start: {exc}") from exc
    output = (proc.stdout or proc.stderr or "").strip()
    if len(output) > MAX_GIT_OUTPUT_CHARS:
        output = output[:MAX_GIT_OUTPUT_CHARS] + "\n... [truncated]"
    return proc.returncode, output


def _git_value(args: list[str], *, cwd: str | Path, label: str) -> str:
    rc, output = _run_git(args, cwd=cwd)
    if rc != 0 or not output:
        raise WorktreeError(f"Could not resolve {label}: {output or 'Git returned no value.'}")
    return output.splitlines()[0].strip()


def _normalize_slug(name: str) -> str:
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    slug = slug[:MAX_NAME_CHARS].rstrip("-")
    if not slug:
        raise WorktreeError("Worktree name must contain at least one letter or number.")
    return slug


def _validate_ref(ref: str) -> str:
    value = (ref or "HEAD").strip()
    if not _SAFE_REF_RE.fullmatch(value):
        raise WorktreeError(
            "Base ref contains unsupported characters; use a branch, tag, or commit name."
        )
    return value


def _resolve_git_path(raw: str, *, cwd: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def repository_context(cwd: str | Path) -> dict[str, Any]:
    """Return normalized repository/worktree identity for a directory."""

    workdir = Path(cwd).expanduser().resolve()
    top = Path(
        _git_value(["rev-parse", "--show-toplevel"], cwd=workdir, label="Git worktree root")
    ).resolve()
    common_raw = _git_value(["rev-parse", "--git-common-dir"], cwd=workdir, label="Git common directory")
    git_raw = _git_value(["rev-parse", "--git-dir"], cwd=workdir, label="Git directory")
    common_dir = _resolve_git_path(common_raw, cwd=workdir)
    git_dir = _resolve_git_path(git_raw, cwd=workdir)
    repository_root = common_dir.parent if common_dir.name == ".git" else top

    branch_rc, branch = _run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=workdir)
    if branch_rc != 0:
        branch = "(detached)"
    head = _git_value(["rev-parse", "--verify", "HEAD"], cwd=workdir, label="HEAD")
    return {
        "cwd": str(workdir),
        "workspace_root": str(top),
        "repository_root": str(repository_root.resolve()),
        "git_common_dir": str(common_dir),
        "branch": branch,
        "head": head,
        "is_linked_worktree": git_dir != common_dir,
    }


def capture_workspace(cwd: str | Path) -> dict[str, Any]:
    """Capture bounded workspace identity plus full-state Git digests."""

    try:
        context = repository_context(cwd)
    except WorktreeError as exc:
        return {
            "available": False,
            "cwd": str(Path(cwd).expanduser().resolve()),
            "error": str(exc)[:1_000],
        }
    snapshot = git_evidence.capture_git_snapshot(context["workspace_root"])
    return {
        "available": snapshot.available,
        **context,
        "clean": git_evidence.snapshot_is_clean(snapshot),
        "status": snapshot.status,
        "status_digest": snapshot.status_digest,
        "tracked_diff_digest": snapshot.tracked_diff_digest,
        "untracked_digest": snapshot.untracked_digest,
        "untracked_total": snapshot.untracked_total,
        "captured_at": _now(),
        "error": snapshot.error or "",
    }


def _normalize_record(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    record_id = str(raw.get("id") or "").strip()[:64]
    path = str(raw.get("path") or "").strip()
    if not record_id or not path:
        return None
    status = str(raw.get("status") or "active").strip().lower()
    if status not in {"active", "removed", "missing"}:
        status = "missing"
    return {
        "id": record_id,
        "name": str(raw.get("name") or "worktree").strip()[:MAX_NAME_CHARS],
        "repository_root": str(raw.get("repository_root") or "").strip(),
        "managed_root": str(raw.get("managed_root") or "").strip(),
        "path": path,
        "branch": str(raw.get("branch") or "").strip()[:240],
        "base_ref": str(raw.get("base_ref") or "HEAD").strip()[:240],
        "base_head": str(raw.get("base_head") or "").strip()[:64],
        "status": status,
        "created_at": str(raw.get("created_at") or _now()).strip()[:64],
        "updated_at": str(raw.get("updated_at") or _now()).strip()[:64],
        "removed_at": str(raw.get("removed_at") or "").strip()[:64],
    }


def load_records(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or store_path()
    payload = config._load_json_file(
        target,
        {"version": WORKTREE_SCHEMA_VERSION, "worktrees": []},
    )
    if not isinstance(payload, dict) or payload.get("version") != WORKTREE_SCHEMA_VERSION:
        return []
    raw_records = payload.get("worktrees")
    if not isinstance(raw_records, list):
        return []
    records = [_normalize_record(item) for item in raw_records]
    return [item for item in records if item is not None]


def _mutate_records(
    callback: Callable[[list[dict[str, Any]]], Any],
    *,
    path: Path | None = None,
) -> Any:
    target = path or store_path()
    with config._exclusive_state_lock(target):
        records = load_records(target)
        result = callback(records)
        records.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        active = [item for item in records if item.get("status") == "active"]
        inactive = [item for item in records if item.get("status") != "active"]
        # Never orphan a live filesystem worktree merely to honor a history cap.
        records[:] = active + inactive[: max(0, MAX_WORKTREE_RECORDS - len(active))]
        config._atomic_write_text(
            target,
            json.dumps(
                {"version": WORKTREE_SCHEMA_VERSION, "worktrees": records},
                indent=2,
            ),
        )
        return result


def _record_matches(record: dict[str, Any], ref: str) -> bool:
    needle = ref.strip().lower()
    return record["id"].lower() == needle or record["name"].lower() == needle


def resolve_record(
    ref: str,
    *,
    include_removed: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    needle = (ref or "").strip().lower()
    if not needle:
        raise WorktreeError("Worktree ID or name is required.")
    records = [
        item for item in load_records(path) if include_removed or item.get("status") == "active"
    ]
    exact = [item for item in records if _record_matches(item, needle)]
    if len(exact) == 1:
        return exact[0]
    prefix = [item for item in records if item["id"].lower().startswith(needle)]
    if len(prefix) == 1:
        return prefix[0]
    if len(exact) > 1 or len(prefix) > 1:
        raise WorktreeError(f"Worktree reference '{ref}' is ambiguous.")
    raise WorktreeError(f"Unknown worktree '{ref}'. Use /worktree list.")


def _branch_exists(repository_root: Path, branch: str) -> bool:
    rc, _output = _run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repository_root,
    )
    return rc == 0


def _allocation(repository_root: Path, name: str) -> tuple[str, Path, Path]:
    common = _git_value(
        ["rev-parse", "--git-common-dir"],
        cwd=repository_root,
        label="Git common directory",
    )
    identity = hashlib.blake2s(
        str(_resolve_git_path(common, cwd=repository_root)).encode("utf-8"),
        digest_size=4,
    ).hexdigest()
    repo_slug = _normalize_slug(repository_root.name)
    root_base = managed_root(repository_root)
    root = root_base / f"{repo_slug}-{identity}"
    for index in range(1, 101):
        suffix = "" if index == 1 else f"-{index}"
        branch = f"algo/{name}{suffix}"
        target = root / f"{name}{suffix}"
        if not target.exists() and not _branch_exists(repository_root, branch):
            return branch, target, root_base
    raise WorktreeError("Could not allocate a unique worktree name after 100 attempts.")


def create_worktree(
    cwd: str | Path,
    name: str,
    *,
    base_ref: str = "HEAD",
    path: Path | None = None,
) -> dict[str, Any]:
    """Create a collision-safe linked worktree and persist its identity atomically."""

    context = repository_context(cwd)
    repository_root = Path(context["repository_root"])
    try:
        config.CONFIG_DIR.expanduser().resolve().relative_to(repository_root.resolve())
    except ValueError:
        pass
    else:
        raise WorktreeError(
            "ALGO_CLI_CONFIG_DIR is inside this repository; move it outside the checkout "
            "before creating managed worktrees."
        )
    active_count = sum(1 for item in load_records(path) if item.get("status") == "active")
    if active_count >= MAX_WORKTREE_RECORDS:
        raise WorktreeError(
            f"Managed worktree limit reached ({MAX_WORKTREE_RECORDS}); remove an inactive workspace first."
        )
    slug = _normalize_slug(name)
    ref = _validate_ref(base_ref)
    base_head = _git_value(
        ["rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=repository_root,
        label=f"base ref '{ref}'",
    )
    branch, target, root_base = _allocation(repository_root, slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    rc, output = _run_git(
        ["worktree", "add", "-b", branch, "--", str(target), base_head],
        cwd=repository_root,
        timeout=120,
    )
    if rc != 0:
        raise WorktreeError(f"Could not create worktree: {output or 'Git failed.'}")

    now = _now()
    record = {
        "id": uuid.uuid4().hex[:8],
        "name": slug,
        "repository_root": str(repository_root),
        "managed_root": str(root_base.resolve()),
        "path": str(target.resolve()),
        "branch": branch,
        "base_ref": ref,
        "base_head": base_head,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "removed_at": "",
    }

    try:
        def add(records: list[dict[str, Any]]) -> dict[str, Any]:
            records.append(dict(record))
            return dict(record)

        return _mutate_records(add, path=path)
    except Exception:
        _run_git(["worktree", "remove", "--force", "--", str(target)], cwd=repository_root)
        _run_git(["branch", "-D", "--", branch], cwd=repository_root)
        raise


def activate_worktree(ref: str, cfg: Any, *, path: Path | None = None) -> dict[str, Any]:
    record = resolve_record(ref, path=path)
    target = Path(record["path"]).expanduser().resolve()
    if not target.is_dir():
        raise WorktreeError(f"Worktree path is missing: {target}")
    # Validate that the target is still the registered branch before changing cwd.
    context = repository_context(target)
    if Path(context["repository_root"]).resolve() != Path(record["repository_root"]).resolve():
        raise WorktreeError("Worktree repository identity no longer matches its registry record.")
    if context["branch"] != record["branch"]:
        raise WorktreeError(
            f"Worktree branch mismatch: expected {record['branch']}, found {context['branch']}."
        )
    cfg.cwd = str(target)
    if hasattr(cfg, "save"):
        cfg.save()

    def touch(records: list[dict[str, Any]]) -> None:
        for item in records:
            if item["id"] == record["id"]:
                item["updated_at"] = _now()
                return

    _mutate_records(touch, path=path)
    return record


def remove_worktree(ref: str, cfg: Any | None = None, *, path: Path | None = None) -> dict[str, Any]:
    """Remove only a clean Algo-managed worktree; retain its branch and audit record."""

    record = resolve_record(ref, path=path)
    target = Path(record["path"]).expanduser().resolve()
    # Never trust the editable registry's managed_root value as an authorization
    # boundary. The only removable namespace is the root derived from the live
    # Algo CLI configuration.
    root = managed_root().expanduser().resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorktreeError("Refusing to remove a worktree outside Algo CLI's managed root.") from exc

    repository_root = Path(record["repository_root"]).expanduser().resolve()
    if not repository_root.is_dir():
        raise WorktreeError("Registered source repository is missing; the worktree was not removed.")
    source = repository_context(repository_root)
    if Path(source["repository_root"]).resolve() != repository_root:
        raise WorktreeError("Registered source repository identity no longer matches its record.")

    if target.exists():
        live = repository_context(target)
        if Path(live["workspace_root"]).resolve() != target:
            raise WorktreeError("Registered worktree path is no longer a Git worktree root.")
        if Path(live["repository_root"]).resolve() != repository_root:
            raise WorktreeError("Worktree repository identity no longer matches its registry record.")
        if Path(live["git_common_dir"]).resolve() != Path(source["git_common_dir"]).resolve():
            raise WorktreeError("Worktree Git common directory no longer matches its source repository.")
        if live["branch"] != record["branch"]:
            raise WorktreeError(
                f"Worktree branch mismatch: expected {record['branch']}, found {live['branch']}."
            )
        snapshot = git_evidence.capture_git_snapshot(str(target))
        if not git_evidence.snapshot_is_clean(snapshot):
            raise WorktreeError(
                "Refusing to remove a worktree with uncommitted changes. Commit or preserve them first."
            )
        ignored_rc, ignored = _run_git(
            ["ls-files", "--others", "--ignored", "--exclude-standard"],
            cwd=target,
        )
        if ignored_rc != 0:
            raise WorktreeError(
                f"Could not verify ignored worktree files before removal: {ignored or 'Git failed.'}"
            )
        if ignored.strip():
            ignored_count = len([line for line in ignored.splitlines() if line.strip()])
            raise WorktreeError(
                "Refusing to remove a worktree containing ignored files "
                f"({ignored_count} detected). Preserve or delete them explicitly first."
            )
        rc, output = _run_git(
            ["worktree", "remove", "--", str(target)],
            cwd=repository_root,
            timeout=120,
        )
        if rc != 0:
            raise WorktreeError(f"Could not remove worktree: {output or 'Git failed.'}")
    else:
        prune_rc, prune_output = _run_git(
            ["worktree", "prune", "--expire", "now"],
            cwd=repository_root,
            timeout=120,
        )
        if prune_rc != 0:
            raise WorktreeError(
                f"Could not prune missing worktree metadata: {prune_output or 'Git failed.'}"
            )
        list_rc, porcelain = _run_git(
            ["worktree", "list", "--porcelain"],
            cwd=repository_root,
        )
        if list_rc != 0:
            raise WorktreeError(
                f"Could not verify missing worktree metadata: {porcelain or 'Git failed.'}"
            )
        registered_paths = {
            Path(line.removeprefix("worktree ")).expanduser().resolve()
            for line in porcelain.splitlines()
            if line.startswith("worktree ")
        }
        if target in registered_paths:
            raise WorktreeError(
                "Missing worktree is still registered by Git; unlock or repair it before removal."
            )

    if not _branch_exists(repository_root, record["branch"]):
        raise WorktreeError(
            f"Worktree branch {record['branch']} is missing; the registry record was not marked removed."
        )

    now = _now()

    def mark_removed(records: list[dict[str, Any]]) -> dict[str, Any]:
        for item in records:
            if item["id"] == record["id"]:
                item["status"] = "removed"
                item["removed_at"] = now
                item["updated_at"] = now
                return dict(item)
        raise WorktreeError(f"Worktree record '{record['id']}' disappeared during removal.")

    removed = _mutate_records(mark_removed, path=path)
    if cfg is not None and Path(getattr(cfg, "cwd", ".")).expanduser().resolve() == target:
        fallback = Path(record["repository_root"])
        if fallback.is_dir():
            cfg.cwd = str(fallback.resolve())
            if hasattr(cfg, "save"):
                cfg.save()
    return removed


def activate_thread_workspace(record: dict[str, Any], cfg: Any) -> bool:
    """Restore a persisted agent thread's workspace before resume/fork."""

    workspace = record.get("workspace")
    if not isinstance(workspace, dict) or not workspace.get("available"):
        return False
    target_text = str(workspace.get("workspace_root") or workspace.get("cwd") or "").strip()
    if not target_text:
        return False
    target = Path(target_text).expanduser().resolve()
    if not target.is_dir():
        raise WorktreeError(f"Thread workspace is missing: {target}")
    current = capture_workspace(target)
    if not current.get("available"):
        raise WorktreeError(
            f"Could not capture fresh thread workspace evidence: {current.get('error') or 'Git failed.'}"
        )
    expected_repository = str(workspace.get("repository_root") or "").strip()
    if expected_repository and Path(current["repository_root"]).resolve() != Path(expected_repository).resolve():
        raise WorktreeError("Thread workspace repository identity changed since the recorded run.")
    expected_common = str(workspace.get("git_common_dir") or "").strip()
    if expected_common and Path(current["git_common_dir"]).resolve() != Path(expected_common).resolve():
        raise WorktreeError("Thread workspace Git common directory changed since the recorded run.")
    expected_branch = str(workspace.get("branch") or "")
    if expected_branch and expected_branch != "(detached)" and current["branch"] != expected_branch:
        raise WorktreeError(
            f"Thread workspace branch changed: expected {expected_branch}, found {current['branch']}."
        )
    expected_head = str(workspace.get("head") or "").strip()
    if expected_head and current["head"] != expected_head:
        raise WorktreeError(
            "Thread workspace HEAD changed since the recorded run; inspect it before resuming."
        )
    evidence_labels = (
        ("tracked_diff_digest", "tracked changes"),
        ("untracked_digest", "untracked files"),
        ("status_digest", "full Git status"),
        ("status", "Git status"),
    )
    for key, label in evidence_labels:
        expected = str(workspace.get(key) or "")
        if key in workspace and expected != str(current.get(key) or ""):
            raise WorktreeError(
                f"Thread workspace {label} changed since the recorded run; inspect it before resuming."
            )
    cfg.cwd = str(target)
    if hasattr(cfg, "save"):
        cfg.save()
    return True


def list_worktrees(*, include_removed: bool = False, path: Path | None = None) -> list[dict[str, Any]]:
    records = load_records(path)
    return records if include_removed else [item for item in records if item["status"] == "active"]


def format_worktree_list(records: list[dict[str, Any]], *, active_cwd: str = "") -> str:
    if not records:
        return "No managed worktrees. Create one with /worktree new NAME."
    active = Path(active_cwd).expanduser().resolve() if active_cwd else None
    lines = ["Managed worktrees:"]
    for record in records:
        target = Path(record["path"]).expanduser().resolve()
        marker = "*" if active is not None and target == active else "-"
        health = record["status"] if target.exists() else "missing"
        lines.append(
            f"{marker} {record['id']} [{health}] {record['branch']} · {record['path']}"
        )
    return "\n".join(lines)


def format_status(cwd: str | Path) -> str:
    workspace = capture_workspace(cwd)
    if not workspace.get("available"):
        return f"Workspace unavailable: {workspace.get('error', 'not a Git repository')}"
    cleanliness = "clean" if workspace.get("clean") else "changes present"
    kind = "linked worktree" if workspace.get("is_linked_worktree") else "primary checkout"
    return (
        f"Workspace: {workspace['workspace_root']}\n"
        f"Repository: {workspace['repository_root']}\n"
        f"Branch: {workspace['branch']}\n"
        f"HEAD: {workspace['head'][:12]}\n"
        f"State: {kind} · {cleanliness}"
    )


def parse_new_args(arg: str) -> tuple[str, str]:
    try:
        parts = shlex.split(arg)
    except ValueError as exc:
        raise WorktreeError(f"Invalid worktree arguments: {exc}") from exc
    if not parts:
        raise WorktreeError("Usage: /worktree new NAME [--from REF]")
    name = parts[0]
    base_ref = "HEAD"
    index = 1
    while index < len(parts):
        if parts[index] == "--from" and index + 1 < len(parts):
            base_ref = parts[index + 1]
            index += 2
            continue
        raise WorktreeError("Usage: /worktree new NAME [--from REF]")
    return name, base_ref


def handle_command(arg: str, cfg: Any) -> str:
    """Execute the `/worktree` command family and return display-safe text."""

    text = (arg or "").strip()
    sub, _, remainder = text.partition(" ")
    sub = sub.lower() or "status"
    remainder = remainder.strip()
    if sub in {"help", "?"}:
        return (
            "Usage: /worktree status | list | new NAME [--from REF] | "
            "use ID_OR_NAME | remove ID_OR_NAME"
        )
    if sub in {"status", "show"}:
        return format_status(cfg.cwd)
    if sub == "list":
        return format_worktree_list(list_worktrees(), active_cwd=cfg.cwd)
    if sub == "new":
        name, base_ref = parse_new_args(remainder)
        record = create_worktree(cfg.cwd, name, base_ref=base_ref)
        activate_worktree(record["id"], cfg)
        return (
            f"Created and activated worktree {record['id']}\n"
            f"Branch: {record['branch']}\nPath: {record['path']}"
        )
    if sub == "use":
        record = activate_worktree(remainder, cfg)
        return f"Activated worktree {record['id']} · {record['branch']}\nPath: {record['path']}"
    if sub == "remove":
        record = remove_worktree(remainder, cfg)
        return (
            f"Removed worktree {record['id']}; branch {record['branch']} was retained for recovery."
        )
    raise WorktreeError(
        "Usage: /worktree status | list | new NAME [--from REF] | "
        "use ID_OR_NAME | remove ID_OR_NAME"
    )
