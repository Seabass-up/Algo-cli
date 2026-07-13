from __future__ import annotations

from algo_cli import git_evidence


def _snapshot(
    *,
    available: bool = True,
    head: str | None = "abc",
    diff: str = "",
    diff_digest: str = "empty",
    untracked: tuple[str, ...] = (),
    untracked_digest: str = "empty",
    error: str | None = None,
) -> git_evidence.GitSnapshot:
    return git_evidence.GitSnapshot(
        available=available,
        error=error,
        head=head,
        status="## main",
        tracked_diff=diff,
        untracked_files=untracked,
        tracked_diff_digest=diff_digest,
        untracked_digest=untracked_digest,
        untracked_total=len(untracked),
    )


def test_format_git_evidence_reports_unavailable_repository():
    evidence = git_evidence.format_git_evidence(
        _snapshot(available=False, error="not a Git repository"),
        _snapshot(available=False, error="not a Git repository"),
    )

    assert "Git evidence unavailable" in evidence
    assert "not a Git repository" in evidence


def test_format_git_evidence_rejects_changed_head():
    evidence = git_evidence.format_git_evidence(_snapshot(head="abc"), _snapshot(head="def"))

    assert "ATTRIBUTION UNSAFE" in evidence
    assert not git_evidence.has_verified_delta(_snapshot(head="abc"), _snapshot(head="def"))


def test_clean_baseline_tracks_untracked_file_as_verified_delta():
    empty = git_evidence._digest("")
    before = _snapshot(diff_digest=empty, untracked_digest=empty)
    after = _snapshot(
        diff_digest=empty,
        untracked=("created.py",),
        untracked_digest=git_evidence._digest("created.py"),
    )

    evidence = git_evidence.format_git_evidence(before, after)

    assert "Verified Git state change" in evidence
    assert "created.py" in evidence
    assert git_evidence.has_verified_delta(before, after)


def test_dirty_baseline_change_is_not_automatically_verified():
    before = _snapshot(diff="+old", diff_digest="old")
    after = _snapshot(diff="+old\n+new", diff_digest="new")

    evidence = git_evidence.format_git_evidence(before, after)

    assert "previously dirty working tree" in evidence
    assert not git_evidence.has_verified_delta(before, after)
    assert git_evidence.has_observed_delta(before, after)


def test_comparison_uses_uncapped_diff_digest():
    shown = "x" * git_evidence.MAX_TRACKED_DIFF_CHARS
    before = _snapshot(diff=shown, diff_digest="before")
    after = _snapshot(diff=shown, diff_digest="after")

    assert "previously dirty working tree" in git_evidence.format_git_evidence(before, after)


def test_capture_git_snapshot_caps_display_but_hashes_full_state(monkeypatch, tmp_path):
    long_diff = "+" + ("x" * (git_evidence.MAX_TRACKED_DIFF_CHARS + 50))
    untracked = "\n".join(f"new-{index}.py" for index in range(git_evidence.MAX_UNTRACKED_FILES + 2))
    outputs = iter(
        [
            (0, "true"),
            (0, "abc"),
            (0, "## main"),
            (0, long_diff),
            (0, untracked),
        ]
    )
    monkeypatch.setattr(git_evidence, "_run_git", lambda *_args, **_kwargs: next(outputs))

    snapshot = git_evidence.capture_git_snapshot(str(tmp_path))

    assert snapshot.available
    assert "characters omitted" in snapshot.tracked_diff
    assert snapshot.status_digest == git_evidence._digest("## main")
    assert snapshot.tracked_diff_digest == git_evidence._digest(long_diff)
    assert len(snapshot.untracked_files) == git_evidence.MAX_UNTRACKED_FILES
    assert snapshot.untracked_total == git_evidence.MAX_UNTRACKED_FILES + 2


def test_capture_intent_to_add_empty_file_is_not_clean(monkeypatch, tmp_path):
    empty = git_evidence._digest("")
    outputs = iter(
        [
            (0, "true"),
            (0, "abc"),
            (0, "## main\n A empty.txt"),
            (0, ""),
            (0, ""),
        ]
    )
    monkeypatch.setattr(git_evidence, "_run_git", lambda *_args, **_kwargs: next(outputs))

    snapshot = git_evidence.capture_git_snapshot(str(tmp_path))

    assert snapshot.tracked_diff_digest == empty
    assert snapshot.untracked_digest == empty
    assert snapshot.status_digest == git_evidence._digest("## main\n A empty.txt")
    assert git_evidence.snapshot_is_clean(snapshot) is False


def test_untracked_digest_detects_content_change_without_filename_change(tmp_path):
    path = tmp_path / "existing-untracked.txt"
    path.write_text("before", encoding="utf-8")
    before = git_evidence._untracked_state_digest(str(tmp_path), (path.name,))

    path.write_text("after", encoding="utf-8")
    after = git_evidence._untracked_state_digest(str(tmp_path), (path.name,))

    assert before != after
