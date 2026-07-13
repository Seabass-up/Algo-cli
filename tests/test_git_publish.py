from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from algo_cli import git_publish


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
def published_repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")

    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "config", "user.name", "Algo Tests")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "Initial fixture")
    git(root, "branch", "-M", "main")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "--set-upstream", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    git(root, "switch", "-q", "-c", "feature/publish")
    return root, remote


def test_publish_plan_reports_branch_changes_remote_and_readiness(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    (root / "feature.txt").write_text("ready\n", encoding="utf-8")

    plan = git_publish.format_publish_plan(root)

    assert "feature/publish" in plan
    assert "changes: present" in plan
    assert "origin: ready" in plan
    assert "readiness: ready" in plan
    assert "push destination digest:" in plan


def test_repository_identity_accepts_github_transport_variants(tmp_path: Path) -> None:
    https_identity = git_publish._normalized_repository_identity(
        "https://github.com/Example/Project.git", tmp_path
    )
    ssh_identity = git_publish._normalized_repository_identity(
        "git@github.com:example/project.git", tmp_path
    )

    assert https_identity == ssh_identity
    assert "github.com" in https_identity
    assert "https" not in https_identity


def test_repository_identity_preserves_generic_absolute_vs_relative_paths(
    tmp_path: Path,
) -> None:
    absolute = git_publish._normalized_repository_identity(
        "ssh://git@example.test/repository.git", tmp_path
    )
    relative = git_publish._normalized_repository_identity(
        "git@example.test:repository.git", tmp_path
    )

    assert absolute != relative


def test_repository_identity_resolves_equivalent_local_paths(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()

    assert git_publish._normalized_repository_identity("remote.git", tmp_path) == (
        git_publish._normalized_repository_identity(remote.as_uri(), tmp_path)
    )


def test_structured_commit_and_push_publish_feature_branch(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    (root / "feature.txt").write_text("verified work\n", encoding="utf-8")

    committed = git_publish.commit_all(root, "Add verified publish workflow")
    pushed = git_publish.push_branch(root)

    assert committed["status"] == "created"
    assert committed["branch"] == "feature/publish"
    assert len(committed["head"]) == 40
    assert pushed["status"] == "pushed"
    assert pushed["upstream"] == "origin/feature/publish"
    assert git(remote, "show-ref", "--verify", "refs/heads/feature/publish")


def test_commit_scrub_blocks_high_confidence_secret(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    (root / "secret.txt").write_text("AKIA" + "1234567890ABCDEF\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="AWS access key"):
        git_publish.commit_all(root, "Do not publish secret")

    assert git(root, "status", "--porcelain")
    assert git(root, "log", "-1", "--pretty=%s") == "Initial fixture"


def test_commit_scrub_reads_past_generic_output_truncation(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    secret = "AKIA" + "1234567890ABCDEF"
    (root / "late-secret.txt").write_text(
        "x" * (git_publish.MAX_COMMAND_OUTPUT_CHARS + 2_000) + "\n" + secret + "\n",
        encoding="utf-8",
    )

    with pytest.raises(git_publish.PublishError, match="AWS access key"):
        git_publish.commit_all(root, "Do not truncate staged scrub")

    assert git(root, "log", "-1", "--pretty=%s") == "Initial fixture"


def test_commit_scrub_fails_closed_when_diff_exceeds_bound(
    published_repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _remote = published_repository
    monkeypatch.setattr(git_publish, "MAX_SCAN_BYTES", 512)
    (root / "oversized.txt").write_text("x" * 2_000 + "\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="exceeds the 512-byte scrub limit"):
        git_publish.commit_all(root, "Bound the staged scrub")

    assert git(root, "log", "-1", "--pretty=%s") == "Initial fixture"


def test_commit_scrub_forces_binary_and_attribute_hidden_files_to_text(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    (root / ".gitattributes").write_text("opaque.dat -diff\n", encoding="utf-8")
    (root / "opaque.dat").write_bytes(
        b"\x00binary-prefix\nAKIA" + b"1234567890ABCDEF\n"
    )

    with pytest.raises(git_publish.PublishError, match="AWS access key"):
        git_publish.commit_all(root, "Do not hide secrets as binary")

    assert git(root, "log", "-1", "--pretty=%s") == "Initial fixture"


def test_lfs_pointer_update_is_blocked_without_version_context() -> None:
    diff = (
        "@@ -2 +2 @@\n"
        "-oid sha256:" + "0" * 64 + "\n"
        "+oid sha256:" + "1" * 64 + "\n"
    )

    assert "Git LFS pointer requires separate payload review" in (
        git_publish.scan_outgoing_diff(diff)
    )


def test_stale_publish_fingerprint_blocks_before_staging(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    (root / "first.txt").write_text("first\n", encoding="utf-8")
    expected = git_publish.publish_fingerprint(root)
    (root / "second.txt").write_text("second\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="state changed"):
        git_publish.commit_all(
            root,
            "Commit reviewed state",
            expected_fingerprint=expected,
        )

    assert git(root, "diff", "--cached", "--name-only") == ""


def test_publish_fingerprint_reads_remote_refs_past_display_cap(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    head = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    updates = [
        f"update refs/remotes/origin/filler-{index:04d}-{'x' * 40} {head}"
        for index in range(300)
    ]
    updates.append(f"update refs/remotes/origin/zz-late-ref {tree}")
    subprocess.run(
        ["git", "update-ref", "--stdin"],
        cwd=root,
        input=("\n".join(updates) + "\n").encode("utf-8"),
        check=True,
    )
    before = git_publish.publish_fingerprint(root)

    git(root, "update-ref", "refs/remotes/origin/zz-late-ref", head)

    assert git_publish.publish_fingerprint(root) != before


def test_selected_path_commit_leaves_unrelated_changes_uncommitted(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    (root / "selected.txt").write_text("selected\n", encoding="utf-8")
    (root / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")

    result = git_publish.commit_all(
        root,
        "Commit selected path",
        paths=("selected.txt",),
    )

    assert result["paths"] == "selected.txt"
    assert git(root, "show", "--format=", "--name-only", "HEAD").strip() == "selected.txt"
    assert "unrelated.txt" in git(root, "status", "--porcelain")


def test_selected_path_scope_reads_all_nul_delimited_pre_staged_names(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    selected = root / "selected"
    selected.mkdir()
    for index in range(800):
        (selected / f"long-staged-file-{index:04d}.txt").write_text(
            "selected\n", encoding="utf-8"
        )
    outside = root / "zz-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    git(root, "add", "selected", "zz-outside.txt")

    with pytest.raises(git_publish.PublishError, match="zz-outside.txt"):
        git_publish.commit_all(root, "Keep selected scope exact", paths=("selected",))

    assert git(root, "diff", "--cached", "--name-only").splitlines()[-1] == "zz-outside.txt"


def test_selected_path_is_always_a_literal_git_pathspec(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    (root / "ordinary.txt").write_text("ordinary\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="There are no changes"):
        git_publish.commit_all(root, "Do not expand pathspec", paths=(":(glob)**",))

    assert git(root, "diff", "--cached", "--name-only") == ""
    assert "ordinary.txt" in git(root, "status", "--porcelain")


def test_commit_subject_metadata_scrub_blocks_secret_before_staging(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    (root / "safe.txt").write_text("safe\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="Commit-subject.*AWS access key"):
        git_publish.commit_all(root, "AKIA" + "1234567890ABCDEF")

    assert git(root, "diff", "--cached", "--name-only") == ""


def test_staged_filename_metadata_scrub_blocks_secret(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    filename = "AKIA" + "1234567890ABCDEF.txt"
    (root / filename).write_text("safe body\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="Staged-path.*AWS access key"):
        git_publish.commit_all(root, "Safe subject")

    assert filename in git(root, "diff", "--cached", "--name-only")


def test_commit_refuses_in_progress_merge_before_staging(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    merge_head = Path(git(root, "rev-parse", "--git-path", "MERGE_HEAD"))
    if not merge_head.is_absolute():
        merge_head = root / merge_head
    merge_head.write_text(git(root, "rev-parse", "main") + "\n", encoding="utf-8")
    (root / "safe.txt").write_text("safe\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="in progress"):
        git_publish.commit_all(root, "Do not consume merge state")

    assert merge_head.exists()
    assert git(root, "diff", "--cached", "--name-only") == ""


def test_selected_path_cannot_escape_workspace(
    published_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _remote = published_repository
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="escapes"):
        git_publish.commit_all(root, "Escape attempt", paths=("../outside.txt",))


def test_repository_specific_public_scan_is_never_executed(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    scripts = root / "scripts"
    scripts.mkdir()
    scanner = scripts / "check_public_release.py"
    scanner.write_text(
        "from pathlib import Path\n"
        "Path('MALICIOUS_SCANNER_EXECUTED').write_text('executed')\n"
        "raise SystemExit(9)\n",
        encoding="utf-8",
    )
    git(root, "add", "scripts/check_public_release.py")
    git(root, "commit", "-q", "-m", "Add release scanner")
    (root / "feature.txt").write_text("change\n", encoding="utf-8")

    result = git_publish.commit_all(root, "Do not execute repository scanners")

    assert result["status"] == "created"
    assert "were not executed" in result["public_scan"]
    assert not (root / "MALICIOUS_SCANNER_EXECUTED").exists()


def test_pre_commit_hook_cannot_add_unscanned_out_of_scope_secret(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    old_head = git(root, "rev-parse", "HEAD")
    hook = Path(git(root, "rev-parse", "--git-path", "hooks/pre-commit"))
    if not hook.is_absolute():
        hook = root / hook
    hook.write_text(
        "#!/bin/sh\n"
        "printf 'AKIA%s%s\\n' '12345678' '90ABCDEF' > hook-secret.txt\n"
        "git add hook-secret.txt\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    (root / "safe.txt").write_text("safe\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="rolled back.*AWS access key"):
        git_publish.commit_all(root, "Safe selected change", paths=("safe.txt",))

    assert git(root, "rev-parse", "HEAD") == old_head
    assert set(git(root, "diff", "--cached", "--name-only").splitlines()) == {
        "hook-secret.txt",
        "safe.txt",
    }


def test_commit_msg_hook_secret_is_scrubbed_and_rolled_back(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    old_head = git(root, "rev-parse", "HEAD")
    hook = Path(git(root, "rev-parse", "--git-path", "hooks/commit-msg"))
    if not hook.is_absolute():
        hook = root / hook
    hook.write_text(
        "#!/bin/sh\nprintf 'AKIA%s%s\\n' '12345678' '90ABCDEF' > \"$1\"\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    (root / "safe.txt").write_text("safe\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="rolled back.*AWS access key"):
        git_publish.commit_all(root, "Initially safe subject")

    assert git(root, "rev-parse", "HEAD") == old_head
    assert git(root, "diff", "--cached", "--name-only") == "safe.txt"


def test_pre_commit_branch_switch_is_rolled_back_to_reviewed_branch(
    published_repository: tuple[Path, Path]
) -> None:
    root, _remote = published_repository
    old_head = git(root, "rev-parse", "HEAD")
    git(root, "branch", "feature/other")
    hook = Path(git(root, "rev-parse", "--git-path", "hooks/pre-commit"))
    if not hook.is_absolute():
        hook = root / hook
    hook.write_text(
        "#!/bin/sh\ngit symbolic-ref HEAD refs/heads/feature/other\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    (root / "safe.txt").write_text("safe\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="rolled back"):
        git_publish.commit_all(root, "Branch-bound change")

    assert git(root, "branch", "--show-current") == "feature/publish"
    assert git(root, "rev-parse", "feature/publish") == old_head
    assert git(root, "rev-parse", "feature/other") == old_head


def test_protected_branch_is_fail_closed(published_repository: tuple[Path, Path]) -> None:
    root, _remote = published_repository
    git(root, "switch", "-q", "main")
    (root / "main.txt").write_text("change\n", encoding="utf-8")

    with pytest.raises(git_publish.PublishError, match="protected branch"):
        git_publish.commit_all(root, "Direct main change")
    with pytest.raises(git_publish.PublishError, match="protected branch"):
        git_publish.push_branch(root)


def test_root_commit_push_is_scrubbed_when_remote_has_no_default_ref(tmp_path: Path) -> None:
    remote = tmp_path / "empty.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    root = tmp_path / "root-commit"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "config", "user.name", "Algo Tests")
    git(root, "switch", "-q", "-c", "feature/root")
    (root / "secret.txt").write_text("AKIA" + "1234567890ABCDEF\n", encoding="utf-8")
    git(root, "add", "secret.txt")
    git(root, "commit", "-q", "-m", "Root commit")
    git(root, "remote", "add", "origin", str(remote))

    with pytest.raises(git_publish.PublishError, match="AWS access key"):
        git_publish.push_branch(root)

    refs = subprocess.run(["git", "show-ref"], cwd=remote, capture_output=True, text=True)
    assert refs.returncode != 0
    assert refs.stdout == ""


def test_safe_root_commit_pushes_to_empty_remote(tmp_path: Path) -> None:
    remote = tmp_path / "empty.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    root = tmp_path / "safe-root"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "config", "user.name", "Algo Tests")
    git(root, "switch", "-q", "-c", "feature/root")
    (root / "safe.txt").write_text("safe\n", encoding="utf-8")
    git(root, "add", "safe.txt")
    git(root, "commit", "-q", "-m", "Safe root commit")
    git(root, "remote", "add", "origin", str(remote))

    result = git_publish.push_branch(root)

    assert result["status"] == "pushed"
    assert git(remote, "rev-parse", "refs/heads/feature/root") == git(
        root, "rev-parse", "HEAD"
    )


def test_push_scrub_reads_past_generic_output_truncation(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    secret = "AKIA" + "1234567890ABCDEF"
    (root / "late-outgoing-secret.txt").write_text(
        "x" * (git_publish.MAX_COMMAND_OUTPUT_CHARS + 2_000) + "\n" + secret + "\n",
        encoding="utf-8",
    )
    git(root, "add", "late-outgoing-secret.txt")
    git(root, "commit", "-q", "-m", "Commit secret outside structured flow")

    with pytest.raises(git_publish.PublishError, match="AWS access key"):
        git_publish.push_branch(root)

    refs = subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert refs.returncode != 0


def test_push_scrubs_outgoing_commit_messages(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    (root / "safe.txt").write_text("safe\n", encoding="utf-8")
    git(root, "add", "safe.txt")
    git(root, "commit", "-q", "-m", "AKIA" + "1234567890ABCDEF")

    with pytest.raises(
        git_publish.PublishError,
        match="Outgoing-commit-metadata.*AWS access key",
    ):
        git_publish.push_branch(root)

    result = subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_push_scrubs_raw_commit_author_metadata(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    git(root, "config", "user.name", "AKIA" + "1234567890ABCDEF")
    (root / "safe-author-body.txt").write_text("safe\n", encoding="utf-8")
    git(root, "add", "safe-author-body.txt")
    git(root, "commit", "-q", "-m", "Safe subject")

    with pytest.raises(
        git_publish.PublishError,
        match="Outgoing-commit-metadata.*AWS access key",
    ):
        git_publish.push_branch(root)

    result = subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_push_scrubs_published_branch_name(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    branch = "feature/AKIA" + "1234567890ABCDEF"
    git(root, "switch", "-q", "-c", branch)

    with pytest.raises(git_publish.PublishError, match="Branch-name.*AWS access key"):
        git_publish.push_branch(root)

    result = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_push_rejects_upstream_that_targets_remote_default(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    (root / "feature.txt").write_text("feature\n", encoding="utf-8")
    git_publish.commit_all(root, "Feature commit")
    git(root, "branch", "--set-upstream-to=origin/main", "feature/publish")
    remote_main = git(remote, "rev-parse", "refs/heads/main")

    with pytest.raises(
        git_publish.PublishError,
        match="upstream must be exactly origin/feature/publish; found origin/main",
    ):
        git_publish.push_branch(root)

    assert git(remote, "rev-parse", "refs/heads/main") == remote_main
    refs = subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert refs.returncode != 0


def test_push_rejects_distinct_pushurl_without_mutating_either_remote(
    published_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    root, fetch_remote = published_repository
    push_remote = tmp_path / "other.git"
    push_remote.mkdir()
    git(push_remote, "init", "--bare", "-q")
    git(root, "config", "remote.origin.pushurl", str(push_remote))
    (root / "feature.txt").write_text("feature\n", encoding="utf-8")
    git_publish.commit_all(root, "Feature commit")

    with pytest.raises(git_publish.PublishError, match="does not match"):
        git_publish.push_branch(root)

    for remote in (fetch_remote, push_remote):
        result = subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
            cwd=remote,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


def test_push_rejects_multiple_pushurls_without_mutating_destinations(
    published_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    root, first_remote = published_repository
    second_remote = tmp_path / "second.git"
    second_remote.mkdir()
    git(second_remote, "init", "--bare", "-q")
    git(root, "config", "--add", "remote.origin.pushurl", str(first_remote))
    git(root, "config", "--add", "remote.origin.pushurl", str(second_remote))
    (root / "feature.txt").write_text("feature\n", encoding="utf-8")
    git_publish.commit_all(root, "Feature commit")

    with pytest.raises(git_publish.PublishError, match="exactly one"):
        git_publish.push_branch(root)

    for remote in (first_remote, second_remote):
        result = subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
            cwd=remote,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


def test_push_rejects_cascaded_url_rewrites_before_either_destination_mutates(
    published_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _original_remote = published_repository
    reviewed_remote = tmp_path / "reviewed.git"
    attacker_remote = tmp_path / "attacker.git"
    for remote in (reviewed_remote, attacker_remote):
        remote.mkdir()
        git(remote, "init", "--bare", "-q")
    marker = "algo-cli-cascade://repository"
    reviewed_url = reviewed_remote.as_uri()
    attacker_url = attacker_remote.as_uri()
    git(root, "remote", "set-url", "origin", marker)
    git(root, "config", "--add", f"url.{reviewed_url}.insteadOf", marker)
    git(root, "config", "--add", f"url.{attacker_url}.insteadOf", reviewed_url)
    assert git(root, "remote", "get-url", "origin") == reviewed_url
    (root / "feature.txt").write_text("feature\n", encoding="utf-8")
    git_publish.commit_all(root, "Feature commit")

    with pytest.raises(git_publish.PublishError, match="URL rewrite configuration"):
        git_publish.push_branch(root)

    for remote in (reviewed_remote, attacker_remote):
        result = subprocess.run(
            ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
            cwd=remote,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


def test_publish_fingerprint_changes_when_effective_pushurl_changes(
    published_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _remote = published_repository
    before = git_publish.publish_fingerprint(root)
    other = tmp_path / "other.git"
    other.mkdir()
    git(other, "init", "--bare", "-q")
    git(root, "config", "remote.origin.pushurl", str(other))

    after = git_publish.publish_fingerprint(root)

    assert after != before


def test_push_uses_branch_locked_refspec(
    published_repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _remote = published_repository
    (root / "feature.txt").write_text("feature\n", encoding="utf-8")
    git_publish.commit_all(root, "Feature commit")
    original_push = git_publish._push_bound_destination
    calls: list[dict[str, object]] = []

    def recording_push(**kwargs):
        calls.append(dict(kwargs))
        return original_push(**kwargs)

    monkeypatch.setattr(git_publish, "_push_bound_destination", recording_push)

    git_publish.push_branch(root)

    assert len(calls) == 1
    head = git(root, "rev-parse", "HEAD")
    assert calls[0]["refspec"] == f"{head}:refs/heads/feature/publish"
    assert "push_url" in calls[0]
    assert git(root, "rev-parse", "--abbrev-ref", "@{upstream}") == (
        "origin/feature/publish"
    )
    assert git(root, "rev-parse", "refs/remotes/origin/feature/publish") == head


def test_push_fails_if_branch_advances_after_scrub(
    published_repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, remote = published_repository
    (root / "feature.txt").write_text("reviewed\n", encoding="utf-8")
    git(root, "add", "feature.txt")
    git(root, "commit", "-q", "-m", "Reviewed commit")
    original_scrub = git_publish._require_scrubbed
    injected = False

    def advance_after_scrub(diff: str) -> str:
        nonlocal injected
        result = original_scrub(diff)
        if not injected:
            injected = True
            (root / "injected.txt").write_text(
                "AKIA" + "1234567890ABCDEF\n", encoding="utf-8"
            )
            git(root, "add", "injected.txt")
            git(root, "commit", "-q", "-m", "Concurrent commit")
        return result

    monkeypatch.setattr(git_publish, "_require_scrubbed", advance_after_scrub)

    with pytest.raises(git_publish.PublishError, match="advanced after"):
        git_publish.push_branch(root)

    result = subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_push_scrubs_transient_secret_from_every_outgoing_commit(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    transient = root / "transient.txt"
    transient.write_text("AKIA" + "1234567890ABCDEF\n", encoding="utf-8")
    git(root, "add", "transient.txt")
    git(root, "commit", "-q", "-m", "Add transient data")
    transient.unlink()
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Remove transient data")

    with pytest.raises(git_publish.PublishError, match="AWS access key"):
        git_publish.push_branch(root)

    result = subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_push_rejects_git_replacement_refs(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    (root / "secret.txt").write_text(
        "AKIA" + "1234567890ABCDEF\n", encoding="utf-8"
    )
    git(root, "add", "secret.txt")
    git(root, "commit", "-q", "-m", "Secret original commit")
    secret_head = git(root, "rev-parse", "HEAD")
    git(root, "replace", secret_head, "main")

    with pytest.raises(git_publish.PublishError, match="replacement refs"):
        git_publish.push_branch(root)

    result = subprocess.run(
        ["git", "show-ref", "--verify", "refs/heads/feature/publish"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_push_rejects_actual_remote_default_even_when_named_develop(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    git(root, "switch", "-q", "-c", "develop", "main")
    git(root, "push", "-q", "--set-upstream", "origin", "develop")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/develop")
    remote_develop = git(remote, "rev-parse", "refs/heads/develop")
    (root / "develop.txt").write_text("change\n", encoding="utf-8")
    git_publish.commit_all(root, "Develop change")

    with pytest.raises(git_publish.PublishError, match="actual default branch 'develop'"):
        git_publish.push_branch(root)

    assert git(remote, "rev-parse", "refs/heads/develop") == remote_develop


def test_bound_push_does_not_follow_annotated_tags(
    published_repository: tuple[Path, Path]
) -> None:
    root, remote = published_repository
    (root / "feature.txt").write_text("feature\n", encoding="utf-8")
    git_publish.commit_all(root, "Feature commit")
    git(root, "tag", "-a", "private-tag", "-m", "Local only")
    git(root, "config", "push.followTags", "true")

    git_publish.push_branch(root)

    tag = subprocess.run(
        ["git", "show-ref", "--verify", "refs/tags/private-tag"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert tag.returncode != 0


def test_push_blocks_branch_behind_fresh_remote_default(
    published_repository: tuple[Path, Path], tmp_path: Path
) -> None:
    root, remote = published_repository
    (root / "feature.txt").write_text("feature\n", encoding="utf-8")
    git_publish.commit_all(root, "Feature commit")

    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    git(other, "config", "user.email", "other@example.com")
    git(other, "config", "user.name", "Other Tests")
    (other / "main.txt").write_text("remote advance\n", encoding="utf-8")
    git(other, "add", "main.txt")
    git(other, "commit", "-q", "-m", "Advance main")
    git(other, "push", "-q", "origin", "main")

    with pytest.raises(git_publish.PublishError, match="behind origin/main"):
        git_publish.push_branch(root)


def test_pull_request_reuses_existing_request(
    published_repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _remote = published_repository
    original_run = git_publish._run_program

    def fake_run(program, args, **kwargs):
        if program != "gh":
            return original_run(program, args, **kwargs)
        if args[:2] == ["auth", "status"]:
            return 0, "authenticated"
        if args[:2] == ["pr", "view"]:
            return 0, '{"url":"https://github.com/example/repo/pull/7","number":7,"title":"PR","state":"OPEN"}'
        raise AssertionError(args)

    monkeypatch.setattr(git_publish.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        git_publish,
        "_github_repository_slug",
        lambda _routing: "github.com/example/repo",
    )
    monkeypatch.setattr(git_publish, "_run_program", fake_run)

    result = git_publish.create_pull_request(root)

    assert result["status"] == "existing"
    assert result["number"] == 7


def test_pull_request_is_draft_by_default(
    published_repository: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _remote = published_repository
    git(root, "push", "-q", "--set-upstream", "origin", "feature/publish")
    original_run = git_publish._run_program
    calls: list[list[str]] = []

    def fake_run(program, args, **kwargs):
        if program != "gh":
            return original_run(program, args, **kwargs)
        calls.append(args)
        if args[:2] == ["auth", "status"]:
            return 0, "authenticated"
        if args[:2] == ["pr", "view"]:
            return 1, "no pull request"
        if args[:2] == ["pr", "create"]:
            return 0, "https://github.com/example/repo/pull/8"
        raise AssertionError(args)

    monkeypatch.setattr(git_publish.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(
        git_publish,
        "_github_repository_slug",
        lambda _routing: "github.com/example/repo",
    )
    monkeypatch.setenv("GH_REPO", "attacker/wrong-repository")
    monkeypatch.setattr(git_publish, "_run_program", fake_run)

    result = git_publish.create_pull_request(root)

    assert result == {
        "status": "created",
        "branch": "feature/publish",
        "url": "https://github.com/example/repo/pull/8",
        "draft": True,
    }
    assert [
        "pr",
        "create",
        "--repo",
        "github.com/example/repo",
        "--head",
        "feature/publish",
        "--base",
        "main",
        "--fill",
        "--draft",
    ] in calls
