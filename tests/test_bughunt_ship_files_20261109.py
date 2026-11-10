"""Regression tests: selected-file commits started from a repository subdirectory.

``--files`` paths are relative to the workspace root. Git resolves a plain
pathspec against the process working directory, so from a subdirectory the
validated path and the path Git acted on used to differ.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from algo_cli import git_publish


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True)
    return proc.stdout.strip()


@pytest.fixture
def repository_with_subdirectory(tmp_path: Path) -> tuple[Path, Path]:
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
    git(root, "switch", "-q", "-c", "feature/subdirectory")

    (root / "src").mkdir()
    (root / "src" / "x.py").write_text("value = 1\n", encoding="utf-8")
    subdirectory = root / "sub"
    subdirectory.mkdir()
    (subdirectory / "y.py").write_text("other = 2\n", encoding="utf-8")
    return root, subdirectory


def test_workspace_relative_file_commits_from_a_subdirectory(
    repository_with_subdirectory: tuple[Path, Path],
) -> None:
    root, subdirectory = repository_with_subdirectory

    git_publish.commit_all(subdirectory, "Add the value module", paths=("src/x.py",))

    assert git(root, "show", "--name-only", "--format=", "HEAD").splitlines() == ["src/x.py"]
    assert git(root, "diff", "--cached", "--name-only") == ""
    assert "sub/" in git(root, "status", "--porcelain")


def test_subdirectory_relative_path_is_refused_with_a_hint_and_stages_nothing(
    repository_with_subdirectory: tuple[Path, Path],
) -> None:
    root, subdirectory = repository_with_subdirectory

    with pytest.raises(git_publish.PublishError, match=r"relative to the workspace root.*sub/y\.py"):
        git_publish.commit_all(subdirectory, "Add the other module", paths=("y.py",))

    assert git(root, "diff", "--cached", "--name-only") == ""
    assert git(root, "log", "-1", "--format=%s") == "Initial fixture"


def test_selected_path_stays_literal_from_a_subdirectory(
    repository_with_subdirectory: tuple[Path, Path],
) -> None:
    root, subdirectory = repository_with_subdirectory

    with pytest.raises(git_publish.PublishError, match="There are no changes"):
        git_publish.commit_all(subdirectory, "Do not expand pathspec", paths=(":(glob)**",))

    assert git(root, "diff", "--cached", "--name-only") == ""


def test_selected_file_commit_from_the_workspace_root_is_unchanged(
    repository_with_subdirectory: tuple[Path, Path],
) -> None:
    root, _subdirectory = repository_with_subdirectory

    git_publish.commit_all(root, "Add the other module", paths=("sub/y.py",))

    assert git(root, "show", "--name-only", "--format=", "HEAD").splitlines() == ["sub/y.py"]
    assert "src/" in git(root, "status", "--porcelain")


def test_ship_command_commits_a_workspace_relative_file_from_a_subdirectory(
    repository_with_subdirectory: tuple[Path, Path],
) -> None:
    root, subdirectory = repository_with_subdirectory

    result = git_publish.handle_command(
        "commit --files src/x.py Add the value module",
        SimpleNamespace(cwd=str(subdirectory)),
    )

    assert "src/x.py" in git(root, "show", "--name-only", "--format=", "HEAD")
    assert git(root, "log", "-1", "--format=%s") == "Add the value module"
    assert isinstance(result, str) and result


def test_selected_directory_commits_from_a_subdirectory(
    repository_with_subdirectory: tuple[Path, Path],
) -> None:
    root, subdirectory = repository_with_subdirectory
    (root / "src" / "nested").mkdir()
    (root / "src" / "nested" / "z.py").write_text("deep = 3\n", encoding="utf-8")

    git_publish.commit_all(subdirectory, "Add the source directory", paths=("src",))

    assert sorted(git(root, "show", "--name-only", "--format=", "HEAD").splitlines()) == [
        "src/nested/z.py",
        "src/x.py",
    ]
    assert git(root, "diff", "--cached", "--name-only") == ""


def test_dot_from_a_subdirectory_is_refused_instead_of_widening_or_narrowing_the_commit(
    repository_with_subdirectory: tuple[Path, Path],
) -> None:
    root, subdirectory = repository_with_subdirectory

    with pytest.raises(git_publish.PublishError, match=r"whole workspace.*--files sub\b"):
        git_publish.commit_all(subdirectory, "Add everything here", paths=(".",))

    assert git(root, "diff", "--cached", "--name-only") == ""
    assert git(root, "log", "-1", "--format=%s") == "Initial fixture"


def test_dot_from_the_workspace_root_still_selects_every_change(
    repository_with_subdirectory: tuple[Path, Path],
) -> None:
    root, _subdirectory = repository_with_subdirectory

    git_publish.commit_all(root, "Add both modules", paths=(".",))

    assert sorted(git(root, "show", "--name-only", "--format=", "HEAD").splitlines()) == [
        "src/x.py",
        "sub/y.py",
    ]
    assert git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("start", ["root", "subdirectory"])
def test_selected_path_under_a_missing_directory_reports_no_changes(
    repository_with_subdirectory: tuple[Path, Path],
    start: str,
) -> None:
    root, subdirectory = repository_with_subdirectory
    cwd = root if start == "root" else subdirectory

    # Git warns that it cannot open sub/absent/; that warning is not a change.
    with pytest.raises(git_publish.PublishError) as refused:
        git_publish.commit_all(cwd, "Add a missing module", paths=("sub/absent/z.py",))

    assert str(refused.value) == "There are no changes to commit."
    assert git(root, "diff", "--cached", "--name-only") == ""


def test_hint_does_not_name_a_path_that_has_no_changes_from_the_subdirectory(
    repository_with_subdirectory: tuple[Path, Path],
) -> None:
    root, subdirectory = repository_with_subdirectory

    with pytest.raises(git_publish.PublishError) as refused:
        git_publish.commit_all(subdirectory, "Add a missing module", paths=("absent/z.py", "z.py"))

    assert str(refused.value) == "There are no changes to commit."
    assert git(root, "diff", "--cached", "--name-only") == ""
