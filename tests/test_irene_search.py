"""Protected searches must authorize descendants, not just a starting path."""

from __future__ import annotations

import os
from dataclasses import asdict
import inspect
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess

import pytest

from algo_cli import config, irene_memory_path_policy as policy, irene_search, nathan_runtime, tools
from algo_cli.search_execution import SearchProcessResult


@pytest.fixture
def protected_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path.resolve() / "workspace"
    workspace.mkdir()
    private = workspace / ".algo_cli"
    private.mkdir(mode=0o700)
    (private / "memory.txt").write_text("needle PRIVATE_CANARY\n", encoding="utf-8")
    (workspace / "ordinary.txt").write_text("needle PUBLIC_CONTROL\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_DIR", private)
    monkeypatch.setattr(policy, "_known_protected_roots", lambda: (private,))
    return (
        workspace,
        private,
        config.Config(cwd=str(workspace), echo_veil_enabled=True, echo_veil_protection="required"),
    )


@pytest.mark.parametrize("backend", ["rg", "fallback"])
def test_protected_ancestor_search_never_returns_private_descendants(
    protected_tree,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    workspace, private, cfg = protected_tree
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)

    result = nathan_runtime.run_tool("search_files", {"pattern": "needle", "path": str(workspace)}, cfg)

    assert "PRIVATE_CANARY" not in result
    assert "PUBLIC_CONTROL" in result
    direct = nathan_runtime.run_tool("search_files", {"pattern": "needle", "path": str(private)}, cfg)
    assert "protected memory paths are unavailable" in direct


@pytest.mark.parametrize("backend", ["rg", "fallback"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_protected_search_rejects_descendant_file_aliases(
    protected_tree,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    alias: str,
) -> None:
    workspace, private, cfg = protected_tree
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    selected = workspace / "alias.txt"
    try:
        if alias == "symlink":
            selected.symlink_to(private / "memory.txt")
        else:
            os.link(private / "memory.txt", selected)
    except OSError as exc:
        pytest.skip(f"platform cannot create {alias}: {exc}")

    result = nathan_runtime.run_tool("search_files", {"pattern": "needle", "glob": "*.txt"}, cfg)

    assert "PRIVATE_CANARY" not in result
    assert "PUBLIC_CONTROL" in result


def test_migration_residue_is_excluded_from_ancestor_search(protected_tree) -> None:
    workspace, private, cfg = protected_tree
    residue = workspace / f"{private.name}.migration-interrupted"
    residue.mkdir()
    (residue / "old.txt").write_text("needle RESIDUE_CANARY\n", encoding="utf-8")

    result = nathan_runtime.run_tool("search_files", {"pattern": "needle"}, cfg)

    assert "RESIDUE_CANARY" not in result
    assert "PRIVATE_CANARY" not in result
    assert "PUBLIC_CONTROL" in result


def _request(workspace: Path, *, glob: str | None = None) -> dict:
    return {
        "path": str(workspace),
        "root_identity": irene_search._identity(workspace.lstat()),
        "rules": asdict(policy.protected_path_rules()),
        "pattern": "needle",
        "glob": glob,
        "limit": 100,
        "max_files": 5000,
        "max_file_bytes": 2_000_000,
        "max_output_bytes": 19_488,
        "skip_dirs": sorted(tools.SEARCH_FALLBACK_SKIP_DIRS),
        "rg": None,
        "timeout": 20,
    }


@pytest.mark.parametrize("backend", ["rg", "fallback"])
@pytest.mark.parametrize(
    "glob,expected",
    [
        ("*.py", {"main.py", "child.py"}),
        ("{*.py,*.txt}", {"main.py", "child.py", "ordinary.txt"}),
        ("nested/*.py", {"child.py"}),
        ("!*.py", {"ordinary.txt", "notes.md"}),
        ("**/child.py", {"child.py"}),
    ],
)
def test_protected_globs_preserve_ordinary_search(protected_tree, monkeypatch, backend, glob, expected):
    workspace, _private, cfg = protected_tree
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    (workspace / "nested").mkdir()
    for name in ("main.py", "nested/child.py", "notes.md"):
        (workspace / name).write_text("needle PUBLIC_FILE\n", encoding="utf-8")
    result = nathan_runtime.run_tool("search_files", {"pattern": "needle", "glob": glob}, cfg)
    assert not result.startswith("Error:"), result
    assert "PRIVATE_CANARY" not in result
    for name in ("main.py", "child.py", "notes.md", "ordinary.txt"):
        assert (name in result) == (name in expected), result


@pytest.mark.parametrize("backend", ["rg", "fallback"])
def test_protected_local_ignore_and_negation_rules(protected_tree, monkeypatch, backend):
    workspace, _private, cfg = protected_tree
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    (workspace / ".gitignore").write_text("*.txt\n!ordinary.txt\nvendor/\n", encoding="utf-8")
    (workspace / ".ignore").write_text("ignored.py\n", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested/.gitignore").write_text("!keep.txt\n", encoding="utf-8")
    (workspace / "vendor").mkdir()
    for name in ("ignored.txt", "ignored.py", "nested/keep.txt", "nested/skip.txt", "vendor/lib.py"):
        (workspace / name).write_text(f"needle {name}\n", encoding="utf-8")
    result = nathan_runtime.run_tool("search_files", {"pattern": "needle"}, cfg)
    assert "PUBLIC_CONTROL" in result and "keep.txt" in result
    for marker in ("PRIVATE_CANARY", "ignored.txt", "ignored.py", "skip.txt", "lib.py"):
        assert marker not in result
    override = nathan_runtime.run_tool("search_files", {"pattern": "needle", "glob": "*.py"}, cfg)
    assert "ignored.py" in override and "lib.py" in override
    assert "PRIVATE_CANARY" not in override


def test_protected_search_does_not_consume_parent_or_global_ignores(protected_tree, monkeypatch):
    workspace, _private, cfg = protected_tree
    (workspace.parent / ".gitignore").write_text("*.txt\n", encoding="utf-8")
    rc = workspace.parent / "global-ignore"
    rc.write_text("--files\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(rc))
    result = nathan_runtime.run_tool("search_files", {"pattern": "needle"}, cfg)
    assert "PUBLIC_CONTROL" in result and "PRIVATE_CANARY" not in result


def test_protected_rg_retains_native_regex_language(protected_tree):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    _workspace, _private, cfg = protected_tree
    result = nathan_runtime.run_tool("search_files", {"pattern": r"needle \p{L}+"}, cfg)
    assert "PUBLIC_CONTROL" in result and "PRIVATE_CANARY" not in result


@pytest.mark.parametrize("pattern", [r"\Aneedle", r"CONTROL\z", r"(?-m:^needle)"])
@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""], ids=["lf", "crlf", "no-final-newline"])
def test_native_anchor_semantics_remain_line_based(protected_tree, pattern, ending):
    executable = shutil.which("rg")
    if executable is None:
        pytest.skip("ripgrep is not installed")
    workspace, _private, cfg = protected_tree
    ordinary = workspace / "ordinary.txt"
    ordinary.write_bytes(b"needle PUBLIC_CONTROL" + ending)
    second = workspace / "second.txt"
    second.write_bytes(b"needle SECOND_CONTROL")
    direct = subprocess.run(
        [
            executable, "--no-config", "--line-number", "--with-filename", "--no-heading",
            "--color=never", "--text", "--encoding=none", "--", pattern, str(ordinary), str(second),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert direct.returncode == 0, direct.stderr.decode("utf-8", errors="replace")
    result = nathan_runtime.run_tool("search_files", {"pattern": pattern}, cfg)
    assert set(result.splitlines()) == set(direct.stdout.decode("utf-8").splitlines())
    assert ("PUBLIC_CONTROL" in result) is not (ending == b"\r\n" and pattern == r"CONTROL\z")
    assert "SECOND_CONTROL" in result and "PRIVATE_CANARY" not in result


@pytest.mark.parametrize("backend", ["rg", "fallback"])
def test_protected_invalid_regex_returns_pattern_error(protected_tree, monkeypatch, backend):
    _workspace, _private, cfg = protected_tree
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    result = nathan_runtime.run_tool("search_files", {"pattern": "("}, cfg)
    assert result.startswith("Error:") and "search pattern" in result
    assert "PRIVATE_CANARY" not in result


@pytest.mark.parametrize("backend", ["rg", "fallback"])
def test_protected_search_preserves_per_file_newlines_and_line_numbers(protected_tree, monkeypatch, backend):
    workspace, _private, cfg = protected_tree
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    (workspace / "first.txt").write_bytes(b"no match\r\nneedle first")
    (workspace / "second.txt").write_bytes(b"needle second\n")
    result = nathan_runtime.run_tool("search_files", {"pattern": "needle"}, cfg)
    assert "first.txt:2:needle first" in result
    assert "second.txt:1:needle second" in result
    assert "firstneedle" not in result


@pytest.mark.parametrize("backend", ["rg", "fallback", "file"])
def test_protected_search_caps_large_unicode_results(protected_tree, monkeypatch, backend):
    workspace, _private, cfg = protected_tree
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    path = workspace / "large.txt"
    path.write_text("needle " + "\u2603" * 100_000 + "\n", encoding="utf-8")
    result = nathan_runtime.run_tool(
        "search_files",
        {"pattern": "needle", "path": str(path if backend == "file" else workspace)},
        cfg,
    )
    assert "truncated" in result and "needle" in result
    assert len(result.encode("utf-8")) <= tools.MAX_TOOL_RESULT
    assert "PRIVATE_CANARY" not in result


def test_search_runtime_hides_and_overrides_model_supplied_config(protected_tree):
    _workspace, _private, cfg = protected_tree
    assert "cfg" not in inspect.signature(tools.TOOL_MAP["search_files"]).parameters
    result = nathan_runtime.run_tool("search_files", {"pattern": "needle", "cfg": None}, cfg)
    assert "PUBLIC_CONTROL" in result and "PRIVATE_CANARY" not in result


@pytest.mark.parametrize("failure", ["changed", "unavailable"])
def test_policy_change_discards_all_buffered_results(protected_tree, monkeypatch, failure):
    _workspace, _private, cfg = protected_tree
    original_rules = policy.protected_path_rules()
    calls = 0

    def rules():
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_rules
        if failure == "unavailable":
            raise policy.ProtectedMemoryPathError("unavailable")
        return policy.ProtectedPathRules((cfg.cwd,), original_rules.residue_parent, original_rules.residue_prefixes)

    monkeypatch.setattr(irene_search, "protected_path_rules", rules)
    monkeypatch.setattr(
        irene_search,
        "run_search_process",
        lambda *a, **kw: SearchProcessResult(
            b"needle MUST_NOT_RELEASE\n",
            b"",
            0,
            False,
            False,
            False,
        ),
    )
    result = nathan_runtime.run_tool("search_files", {"pattern": "needle"}, cfg)
    assert result.startswith("Error:") and "no search results were released" in result
    assert "MUST_NOT_RELEASE" not in result


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor race; Windows pins names with native handles")
def test_file_symlink_swap_is_rejected_before_read(protected_tree, monkeypatch):
    workspace, private, _cfg = protected_tree
    request = _request(workspace)
    original_open = os.open

    def swapped_open(path, flags, *args, **kwargs):
        if path == "ordinary.txt":
            (workspace / path).unlink()
            (workspace / path).symlink_to(private / "memory.txt")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(irene_search.os, "open", swapped_open)
    monkeypatch.setattr(config, "_directory_descriptor_io_supported", lambda: True)
    monkeypatch.setattr(irene_search.os, "read", lambda *a: pytest.fail("replacement reached content read"))
    with pytest.raises(OSError):
        irene_search._search_snapshot(request)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor race; Windows pins names with native handles")
def test_ancestor_swap_is_rejected_before_read(protected_tree, monkeypatch):
    workspace, private, _cfg = protected_tree
    folder = workspace / "folder"
    folder.mkdir()
    (folder / "memory.txt").write_text("needle ordinary", encoding="utf-8")
    request = _request(folder)
    original_read = irene_search._read_payload

    def swapped_read(directory, name, rules, budget):
        if name == "memory.txt":
            folder.rename(workspace / "parked")
            folder.symlink_to(private, target_is_directory=True)
        return original_read(directory, name, rules, budget)

    monkeypatch.setattr(irene_search, "_read_payload", swapped_read)
    monkeypatch.setattr(irene_search.os, "read", lambda *a: pytest.fail("replacement reached content read"))
    with pytest.raises(OSError):
        irene_search._search_snapshot(request)


def test_file_changed_during_read_discards_snapshot(protected_tree, monkeypatch):
    workspace, _private, _cfg = protected_tree
    request = _request(workspace)
    original_read = os.read
    changed = False

    def changed_read(fd, size):
        nonlocal changed
        payload = original_read(fd, size)
        if payload and not changed:
            changed = True
            (workspace / "ordinary.txt").write_text("needle CHANGED", encoding="utf-8")
        return payload

    monkeypatch.setattr(irene_search.os, "read", changed_read)
    with pytest.raises(OSError):
        irene_search._search_snapshot(request)


def test_root_replacement_rejects_parent_to_worker_race(protected_tree):
    workspace, _private, _cfg = protected_tree
    request = _request(workspace)
    workspace.rename(workspace.with_name("parked"))
    workspace.mkdir()
    (workspace / "replacement.txt").write_text("needle REPLACEMENT", encoding="utf-8")
    with pytest.raises(OSError):
        irene_search._search_snapshot(request)


@pytest.mark.skipif(os.name == "nt", reason="POSIX rename race; Windows pins names with native handles")
def test_renamed_protected_subtree_is_rejected_before_read(protected_tree, monkeypatch):
    workspace, private, _cfg = protected_tree
    folder = workspace / "folder"
    folder.mkdir()
    request = _request(workspace)
    private_id = (private / "memory.txt").stat().st_ino
    original_bound = irene_search._bound_directory
    original_read = os.read

    @contextmanager
    def swapped_bound(path, rules, parent=None):
        if path == folder:
            folder.rmdir()
            private.rename(folder)
        with original_bound(path, rules, parent) as directory:
            yield directory

    def checked_read(fd, size):
        if os.fstat(fd).st_ino == private_id:
            pytest.fail("renamed protected subtree reached the content reader")
        return original_read(fd, size)

    monkeypatch.setattr(irene_search, "_bound_directory", swapped_bound)
    monkeypatch.setattr(irene_search.os, "read", checked_read)
    with pytest.raises(OSError):
        irene_search._search_snapshot(request)


def test_protected_files_are_not_opened_even_when_an_alias_exists(protected_tree, monkeypatch):
    workspace, private, _cfg = protected_tree
    os.link(private / "memory.txt", workspace / "alias.txt")
    private_id = (private / "memory.txt").stat().st_ino
    original_read = os.read

    def checked_read(fd, size):
        assert os.fstat(fd).st_ino != private_id, "protected bytes reached the reader"
        return original_read(fd, size)

    monkeypatch.setattr(irene_search.os, "read", checked_read)
    result = irene_search._search_snapshot(_request(workspace)).decode("utf-8")
    assert "PUBLIC_CONTROL" in result and "PRIVATE_CANARY" not in result


@pytest.mark.parametrize("budget", ["files", "entries", "bytes", "depth", "file_size"])
def test_protected_scan_budgets_report_incomplete_results(protected_tree, monkeypatch, budget):
    workspace, _private, _cfg = protected_tree
    request = _request(workspace)
    if budget == "files":
        request["max_files"] = 0
    elif budget == "entries":
        monkeypatch.setattr(irene_search, "MAX_ENTRIES", 0)
    elif budget == "bytes":
        monkeypatch.setattr(irene_search, "MAX_SCAN_BYTES", 0)
    elif budget == "file_size":
        request["max_file_bytes"] = 1
    else:
        monkeypatch.setattr(irene_search, "MAX_DEPTH", 0)
        (workspace / "nested").mkdir()
    result = irene_search._search_snapshot(request).decode("utf-8")
    assert "incomplete" in result and "PRIVATE_CANARY" not in result


def test_protected_search_timeout_has_no_partial_payload(protected_tree, monkeypatch):
    workspace, _private, cfg = protected_tree
    (workspace / "ordinary.txt").write_text("a" * 200 + "!\n", encoding="utf-8")
    monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    monkeypatch.setattr(tools, "SEARCH_TIMEOUT_SECONDS", 0.5)
    result = nathan_runtime.run_tool("search_files", {"pattern": "(a+)+$"}, cfg)
    assert result.startswith("Error:") and "timed out" in result and "no results released" in result


@pytest.mark.parametrize(
    "glob", ["[", "{" + ",".join(f"x{i}" for i in range(300)) + "}"], ids=["open-class", "expansion-limit"]
)
def test_protected_invalid_or_expansive_glob_does_not_expose_memory(protected_tree, glob):
    _workspace, _private, cfg = protected_tree
    result = nathan_runtime.run_tool("search_files", {"pattern": "needle", "glob": glob}, cfg)
    assert result.startswith("Error:") or result == "No matches."
    assert "PRIVATE_CANARY" not in result


def test_protected_rg_caps_display_lines_after_unicode_separator(protected_tree):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    workspace, _private, cfg = protected_tree
    (workspace / "ordinary.txt").write_text("needle first\u2028extra line\n", encoding="utf-8")
    result = nathan_runtime.run_tool("search_files", {"pattern": "needle", "limit": 1}, cfg)
    assert "needle first" in result and "extra line" not in result and "truncated" in result


@pytest.mark.skipif(os.name != "nt", reason="native Windows DOS alias boundary")
def test_windows_short_path_alias_cannot_bypass_denied_root(protected_tree):
    import ctypes

    workspace, private, _cfg = protected_tree
    target = private / "Long Protected Filename.txt"
    target.write_text("needle SHORT_ALIAS_CANARY", encoding="utf-8")
    buffer = ctypes.create_unicode_buffer(32_768)
    get_short_path = ctypes.windll.kernel32.GetShortPathNameW
    if not get_short_path(str(target), buffer, len(buffer)) or buffer.value.casefold() == str(target).casefold():
        pytest.skip("the volume does not provide a distinct DOS short path")
    alias = Path(buffer.value)
    request = _request(workspace)
    request.update(path=str(alias), root_identity=irene_search._identity(alias.lstat()))
    with pytest.raises(OSError):
        irene_search._search_snapshot(request)
