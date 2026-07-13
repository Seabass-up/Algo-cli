from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from algo_cli import config, worktree_runtime


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "config", "user.name", "Algo Tests")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "Initial fixture")
    return root


@pytest.fixture
def worktree_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setattr(config, "CONFIG_DIR", root)
    return root


class FakeConfig:
    def __init__(self, cwd: Path) -> None:
        self.cwd = str(cwd)
        self.save_count = 0

    def save(self) -> None:
        self.save_count += 1


def test_create_allocates_collision_safe_worktrees_and_persists_records(
    repository: Path, worktree_store: Path
) -> None:
    first = worktree_runtime.create_worktree(repository, "Feature Runtime")
    second = worktree_runtime.create_worktree(repository, "Feature Runtime")

    assert first["branch"] == "algo/feature-runtime"
    assert second["branch"] == "algo/feature-runtime-2"
    assert Path(first["path"]).is_dir()
    assert Path(second["path"]).is_dir()
    assert Path(first["path"]).is_relative_to(worktree_store / "worktrees")
    assert git(Path(first["path"]), "branch", "--show-current") == first["branch"]
    assert {item["id"] for item in worktree_runtime.load_records()} == {
        first["id"],
        second["id"],
    }


def test_activate_and_clean_remove_preserve_branch_for_recovery(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "isolated-task")
    cfg = FakeConfig(repository)

    activated = worktree_runtime.activate_worktree(record["id"][:4], cfg)

    assert activated["id"] == record["id"]
    assert Path(cfg.cwd) == Path(record["path"])
    assert cfg.save_count == 1

    removed = worktree_runtime.remove_worktree(record["name"], cfg)

    assert removed["status"] == "removed"
    assert not Path(record["path"]).exists()
    assert Path(cfg.cwd) == repository
    assert git(repository, "show-ref", "--verify", f"refs/heads/{record['branch']}")


def test_remove_refuses_dirty_worktree(repository: Path, worktree_store: Path) -> None:
    record = worktree_runtime.create_worktree(repository, "keep-my-work")
    (Path(record["path"]) / "new.txt").write_text("uncommitted\n", encoding="utf-8")

    with pytest.raises(worktree_runtime.WorktreeError, match="uncommitted changes"):
        worktree_runtime.remove_worktree(record["id"])

    assert Path(record["path"]).is_dir()
    assert worktree_runtime.resolve_record(record["id"])["status"] == "active"


def test_remove_refuses_ignored_files_that_git_would_otherwise_delete(
    repository: Path, worktree_store: Path
) -> None:
    (repository / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    git(repository, "add", ".gitignore")
    git(repository, "commit", "-q", "-m", "Ignore fixture")
    record = worktree_runtime.create_worktree(repository, "ignored-data")
    ignored = Path(record["path"]) / "ignored.txt"
    ignored.write_text("must survive\n", encoding="utf-8")

    with pytest.raises(worktree_runtime.WorktreeError, match="ignored files"):
        worktree_runtime.remove_worktree(record["id"])

    assert ignored.read_text(encoding="utf-8") == "must survive\n"


def test_remove_refuses_intent_to_add_empty_file(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "intent-to-add")
    target = Path(record["path"])
    empty = target / "empty.txt"
    empty.touch()
    git(target, "add", "--intent-to-add", "--", empty.name)

    assert worktree_runtime.capture_workspace(target)["clean"] is False
    with pytest.raises(worktree_runtime.WorktreeError, match="uncommitted changes"):
        worktree_runtime.remove_worktree(record["id"])

    assert target.is_dir()


def test_remove_does_not_trust_registry_managed_root(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "registry-boundary")

    def tamper(records: list[dict[str, object]]) -> None:
        for item in records:
            if item["id"] == record["id"]:
                item["managed_root"] = "/"
                item["path"] = str(repository)

    worktree_runtime._mutate_records(tamper)

    with pytest.raises(worktree_runtime.WorktreeError, match="outside Algo CLI's managed root"):
        worktree_runtime.remove_worktree(record["id"])

    assert repository.is_dir()


def test_remove_validates_live_branch_before_mutating(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "branch-identity")
    target = Path(record["path"])
    git(target, "switch", "--detach", "-q")

    with pytest.raises(worktree_runtime.WorktreeError, match="branch mismatch"):
        worktree_runtime.remove_worktree(record["id"])

    assert target.is_dir()
    assert worktree_runtime.resolve_record(record["id"])["status"] == "active"


def test_remove_missing_worktree_prunes_and_verifies_git_metadata(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "missing-cleanly")
    target = Path(record["path"])
    shutil.rmtree(target)

    removed = worktree_runtime.remove_worktree(record["id"])

    assert removed["status"] == "removed"
    assert str(target) not in git(repository, "worktree", "list", "--porcelain")
    assert git(repository, "show-ref", "--verify", f"refs/heads/{record['branch']}")


def test_remove_missing_locked_worktree_refuses_unpruned_metadata(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "missing-locked")
    target = Path(record["path"])
    git(repository, "worktree", "lock", "--", str(target))
    shutil.rmtree(target)

    with pytest.raises(worktree_runtime.WorktreeError, match="still registered by Git"):
        worktree_runtime.remove_worktree(record["id"])

    assert worktree_runtime.resolve_record(record["id"])["status"] == "active"


def test_capture_workspace_contains_branch_and_full_state_digests(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "evidence")
    workspace = Path(record["path"])
    (workspace / "README.md").write_text("# changed\n", encoding="utf-8")

    captured = worktree_runtime.capture_workspace(workspace)

    assert captured["available"] is True
    assert captured["is_linked_worktree"] is True
    assert captured["branch"] == record["branch"]
    assert captured["clean"] is False
    assert len(captured["tracked_diff_digest"]) == 64
    assert len(captured["untracked_digest"]) == 64


def test_create_rejects_option_like_or_unsafe_base_ref(
    repository: Path, worktree_store: Path
) -> None:
    with pytest.raises(worktree_runtime.WorktreeError, match="unsupported characters"):
        worktree_runtime.create_worktree(repository, "unsafe", base_ref="--detach")


def test_handle_command_creates_activates_lists_and_reports_status(
    repository: Path, worktree_store: Path
) -> None:
    cfg = FakeConfig(repository)

    created = worktree_runtime.handle_command("new demo --from HEAD", cfg)
    listed = worktree_runtime.handle_command("list", cfg)
    status = worktree_runtime.handle_command("status", cfg)

    assert "Created and activated worktree" in created
    assert "algo/demo" in listed
    assert "Branch: algo/demo" in status
    assert "linked worktree" in status


def test_repo_local_config_is_rejected_before_it_can_nest_worktrees(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_state = repository / ".algo_cli"
    monkeypatch.setattr(config, "CONFIG_DIR", local_state)

    with pytest.raises(worktree_runtime.WorktreeError, match="inside this repository"):
        worktree_runtime.create_worktree(repository, "no-recursion")

    assert not local_state.exists()
    assert git(repository, "status", "--porcelain") == ""


def test_thread_activation_rejects_head_drift(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "head-guard")
    target = Path(record["path"])
    captured = worktree_runtime.capture_workspace(target)
    (target / "next.txt").write_text("next\n", encoding="utf-8")
    git(target, "add", "next.txt")
    git(target, "commit", "-q", "-m", "Advance branch")
    cfg = FakeConfig(repository)

    with pytest.raises(worktree_runtime.WorktreeError, match="HEAD changed"):
        worktree_runtime.activate_thread_workspace({"workspace": captured}, cfg)

    assert Path(cfg.cwd) == repository


def test_thread_activation_rejects_tracked_digest_drift_without_head_change(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "tracked-guard")
    target = Path(record["path"])
    captured = worktree_runtime.capture_workspace(target)
    (target / "README.md").write_text("# changed without commit\n", encoding="utf-8")
    cfg = FakeConfig(repository)

    with pytest.raises(worktree_runtime.WorktreeError, match="tracked changes changed"):
        worktree_runtime.activate_thread_workspace({"workspace": captured}, cfg)

    assert Path(cfg.cwd) == repository


def test_thread_activation_rejects_untracked_content_drift_without_head_change(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "untracked-guard")
    target = Path(record["path"])
    note = target / "notes.txt"
    note.write_text("first\n", encoding="utf-8")
    captured = worktree_runtime.capture_workspace(target)
    note.write_text("second\n", encoding="utf-8")
    cfg = FakeConfig(repository)

    with pytest.raises(worktree_runtime.WorktreeError, match="untracked files changed"):
        worktree_runtime.activate_thread_workspace({"workspace": captured}, cfg)

    assert Path(cfg.cwd) == repository


def test_thread_activation_rejects_status_drift_when_diff_is_unchanged(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "status-guard")
    target = Path(record["path"])
    (target / "README.md").write_text("# same diff\n", encoding="utf-8")
    captured = worktree_runtime.capture_workspace(target)
    git(target, "add", "README.md")
    cfg = FakeConfig(repository)

    with pytest.raises(worktree_runtime.WorktreeError, match="Git status changed"):
        worktree_runtime.activate_thread_workspace({"workspace": captured}, cfg)

    assert Path(cfg.cwd) == repository


def test_thread_activation_uses_uncapped_status_digest_for_late_staging_change(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "status-digest-guard")
    target = Path(record["path"])
    filenames = [f"tracked-{index:02d}.txt" for index in range(45)]
    for name in filenames:
        (target / name).write_text("initial\n", encoding="utf-8")
    git(target, "add", "--", *filenames)
    git(target, "commit", "-q", "-m", "Add status digest fixtures")
    for name in filenames:
        (target / name).write_text("changed\n", encoding="utf-8")

    captured = worktree_runtime.capture_workspace(target)
    git(target, "add", "--", filenames[-1])
    current = worktree_runtime.capture_workspace(target)
    cfg = FakeConfig(repository)

    assert captured["head"] == current["head"]
    assert captured["status"] == current["status"]
    assert captured["tracked_diff_digest"] == current["tracked_diff_digest"]
    assert captured["untracked_digest"] == current["untracked_digest"]
    assert captured["status_digest"] != current["status_digest"]
    with pytest.raises(worktree_runtime.WorktreeError, match="full Git status changed"):
        worktree_runtime.activate_thread_workspace({"workspace": captured}, cfg)

    assert Path(cfg.cwd) == repository


def test_thread_activation_rejects_discarded_dirty_state_plus_unrelated_descendant(
    repository: Path, worktree_store: Path
) -> None:
    record = worktree_runtime.create_worktree(repository, "discarded-parent-state")
    target = Path(record["path"])
    (target / "README.md").write_text("# dirty thread state\n", encoding="utf-8")
    captured = worktree_runtime.capture_workspace(target)
    assert captured["clean"] is False
    git(target, "restore", "README.md")
    (target / "unrelated.txt").write_text("unrelated descendant\n", encoding="utf-8")
    git(target, "add", "unrelated.txt")
    git(target, "commit", "-q", "-m", "Unrelated descendant")
    assert worktree_runtime.capture_workspace(target)["clean"] is True
    cfg = FakeConfig(repository)

    with pytest.raises(worktree_runtime.WorktreeError, match="HEAD changed"):
        worktree_runtime.activate_thread_workspace({"workspace": captured}, cfg)

    assert Path(cfg.cwd) == repository


def test_active_records_are_never_evicted_by_history_cap(tmp_path: Path) -> None:
    path = tmp_path / "worktrees.json"
    records = [
        {
            "id": f"active-{index}",
            "name": f"active-{index}",
            "repository_root": "/repo",
            "path": f"/worktree/{index}",
            "branch": f"algo/{index}",
            "status": "active",
        }
        for index in range(worktree_runtime.MAX_WORKTREE_RECORDS + 1)
    ]
    path.write_text(
        json.dumps({"version": 1, "worktrees": records}),
        encoding="utf-8",
    )

    worktree_runtime._mutate_records(lambda _items: None, path=path)

    assert len(worktree_runtime.load_records(path)) == worktree_runtime.MAX_WORKTREE_RECORDS + 1
