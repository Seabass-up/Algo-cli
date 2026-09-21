"""Adversarial tests for protected-memory model filesystem authority."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys

import pytest

from algo_cli import config, irene_memory_path_policy, nathan_runtime, session_commands
from algo_cli.config import Config


def _protected_config(cwd: Path) -> Config:
    return Config(cwd=str(cwd), continuum_enabled=True)


@pytest.mark.parametrize("name,field", [
    ("read_file", "path"), ("read_pdf", "path"), ("render_pdf_pages", "path"),
    ("vision_describe", "image_path"), ("write_file", "path"),
    ("edit_file", "path"), ("batch_edit", "path"),
])
def test_symlink_parent_traversal_refuses_before_dispatch(tmp_path, monkeypatch, name, field):
    root = tmp_path.resolve()
    workspace, protected = root / "workspace", root / "memory"
    workspace.mkdir()
    (protected / "child").mkdir(parents=True)
    (protected / "report.pdf").write_text("PRIVATE_CANARY")
    (workspace / "report.pdf").write_text("PUBLIC_CONTROL")
    try:
        (workspace / "link").symlink_to(protected / "child", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr(irene_memory_path_policy, "_known_protected_roots", lambda: (protected,))
    monkeypatch.setitem(nathan_runtime.TOOL_MAP, name, lambda **_: pytest.fail("aliased dispatch"))
    result = nathan_runtime.run_tool(name, {field: "link/../report.pdf"}, _protected_config(workspace))
    assert "protected memory paths" in result
    assert irene_memory_path_policy.require_allowed_path("../workspace/report.pdf", cwd=workspace) == workspace / "report.pdf"


@pytest.mark.parametrize("alias_kind", ["file", "ancestor"])
def test_protected_read_binds_validation_to_open(tmp_path, monkeypatch, alias_kind):
    root = tmp_path.resolve()
    workspace, protected = root / "workspace", root / "memory"
    workspace.mkdir()
    protected.mkdir()
    safe = workspace / "ordinary.txt"
    safe.write_text("PUBLIC_CONTROL")
    secret = protected / "ordinary.txt"
    secret.write_text("PRIVATE_CANARY")
    monkeypatch.setattr(irene_memory_path_policy, "_known_protected_roots", lambda: (protected,))
    original_open = os.open
    descriptor_support = config._directory_descriptor_io_supported()
    monkeypatch.setattr(config, "_directory_descriptor_io_supported", lambda: descriptor_support)
    switched = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal switched
        if not switched and (os.fspath(path) == str(safe) or os.fspath(path) == safe.name):
            switched = True
            if alias_kind == "file":
                safe.unlink()
                safe.symlink_to(secret)
            else:
                workspace.rename(root / "old-workspace")
                workspace.symlink_to(protected, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    # Ordinary Path.open reaches io.open rather than os.open; retain a baseline
    # injection there so this test demonstrates the original implementation too.
    original_path_open = Path.open

    def racing_path_open(path, *args, **kwargs):
        nonlocal switched
        if path == safe and not switched:
            switched = True
            if alias_kind == "file":
                safe.unlink()
                safe.symlink_to(secret)
            else:
                workspace.rename(root / "old-workspace")
                workspace.symlink_to(protected, target_is_directory=True)
        return original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    monkeypatch.setattr(Path, "open", racing_path_open)
    result = nathan_runtime.run_tool("read_file", {"path": "ordinary.txt"}, _protected_config(workspace))
    assert switched
    assert "PRIVATE_CANARY" not in result
    assert result.startswith("Error")


@pytest.mark.parametrize("tracked", [False, True])
@pytest.mark.parametrize("filename", [
    "ordinary.txt",
    " space.txt",
    pytest.param("\nnewline.txt", marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids newlines in names")),
])
def test_real_git_refuses_hardlink_aliases(tmp_path, monkeypatch, tracked, filename):
    from algo_cli import git_evidence

    root = tmp_path.resolve()
    workspace, protected = root / "workspace", root / "memory"
    workspace.mkdir()
    protected.mkdir()
    monkeypatch.setattr(irene_memory_path_policy, "_known_protected_roots", lambda: (protected,))

    def git(*args):
        return subprocess.run(["git", "-c", "core.hooksPath=", *args], cwd=workspace, check=True, capture_output=True)

    git("init", "-q")
    safe = workspace / filename
    safe.write_text("PUBLIC_CONTROL")
    git("add", "--", filename)
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
    cfg = _protected_config(workspace)
    assert "protected memory paths" not in nathan_runtime.run_tool("git_diff", {}, cfg)
    assert git_evidence.capture_git_snapshot(str(workspace), protected_memory=True).available
    secret = protected / "private.txt"
    secret.write_text("PRIVATE_CANARY")
    alias = safe if tracked else workspace / ("subdir" + filename)
    if tracked:
        alias.unlink()
    os.link(secret, alias)
    result = nathan_runtime.run_tool("git_diff", {}, cfg)
    assert "PRIVATE_CANARY" not in result and "protected memory paths" in result
    snapshot = git_evidence.capture_git_snapshot(str(workspace), protected_memory=True)
    assert not snapshot.available and not snapshot.tracked_diff


def test_git_rejects_configured_worktree_redirection(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    workspace, protected = root / "workspace", root / "memory"
    workspace.mkdir()
    protected.mkdir()
    monkeypatch.setattr(irene_memory_path_policy, "_known_protected_roots", lambda: (protected,))
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    subprocess.run(["git", "config", "core.worktree", str(protected)], cwd=workspace, check=True, capture_output=True)
    result = nathan_runtime.run_tool("git_status", {}, _protected_config(workspace))
    assert "protected memory paths" in result


@pytest.mark.parametrize("command", [
    "/google gmail-draft --text-file /protected/memory.db",
    "/google gmail-draft --html-file /protected/memory.db",
    "/google-callback --file /protected/memory.db",
    "/google-login", "/chatgpt-login", "/config setup google", "/login",
    "/intel reindex", "/code-rag on", "/reload", "/future-unqualified-command",
])
def test_broad_session_routes_refuse_before_dispatch(tmp_path, monkeypatch, command):
    cfg = _protected_config(tmp_path)
    monkeypatch.setitem(nathan_runtime.TOOL_MAP, "session_command", lambda **_: pytest.fail("nested dispatch"))
    result = nathan_runtime.run_tool("session_command", {"command": command}, cfg)
    assert result.startswith("Error:") and "not qualified" in result
    assert "/protected" not in result


@pytest.mark.parametrize("name,field", [
    ("read_file", "path"), ("write_file", "path"), ("edit_file", "path"),
    ("find_unique_anchor", "path"), ("batch_edit", "path"), ("list_directory", "path"),
    ("read_pdf", "path"), ("render_pdf_pages", "path"), ("vision_describe", "image_path"),
    ("git_diff", "path"),
])
def test_same_identity_root_alias_is_denied_across_typed_routes(tmp_path, monkeypatch, name, field):
    root = tmp_path.resolve()
    protected, alias = root / "native-memory", root / "alternate-mount"
    protected.mkdir()
    alias.mkdir()
    monkeypatch.setattr(irene_memory_path_policy, "_known_protected_roots", lambda: (protected,))
    original = Path.lstat
    identity = protected.lstat()
    monkeypatch.setattr(Path, "lstat", lambda path, *a, **kw: identity if path == alias else original(path, *a, **kw))
    result = irene_memory_path_policy.protected_tool_policy_error(
        name, {field: str(alias / "missing-target")}, _protected_config(root)
    )
    assert result is not None and "protected memory paths" in result


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Data-volume alias")
def test_real_data_volume_alias_is_denied_without_reading_store(tmp_path, monkeypatch):
    protected = tmp_path.resolve() / "native-memory"
    protected.mkdir()
    alias = Path("/System/Volumes/Data") / str(protected).lstrip("/")
    if not alias.exists() or not alias.samefile(protected):
        pytest.skip("no distinct Data-volume spelling for this temporary volume")
    monkeypatch.setattr(irene_memory_path_policy, "_known_protected_roots", lambda: (protected,))
    with pytest.raises(irene_memory_path_policy.ProtectedMemoryPathError):
        irene_memory_path_policy.require_allowed_path(alias / "new-file", cwd=tmp_path)


def test_native_continuum_root_is_protected_without_retired_configuration(tmp_path, monkeypatch):
    from algo_cli import continuum_memory

    store = tmp_path / "native-continuum"
    store.mkdir()
    monkeypatch.setattr(continuum_memory, "storage_root", lambda: store)
    assert store in irene_memory_path_policy._known_protected_roots()
    with pytest.raises(irene_memory_path_policy.ProtectedMemoryPathError):
        irene_memory_path_policy.require_allowed_path(store / "data", cwd=tmp_path)


def test_git_and_internal_evidence_reject_enclosed_memory(tmp_path, monkeypatch):
    from algo_cli import git_evidence

    root = tmp_path.resolve()
    (root / ".git").mkdir()
    protected = root / "native-memory"
    protected.mkdir()
    child = root / "ordinary"
    child.mkdir()
    monkeypatch.setattr(irene_memory_path_policy, "_known_protected_roots", lambda: (protected,))
    monkeypatch.setattr(git_evidence, "_run_git", lambda *a, **kw: pytest.fail("aggregate Git read"))
    for cwd in (root, child):
        for name in ("git_status", "git_diff"):
            error = irene_memory_path_policy.protected_tool_policy_error(name, {}, _protected_config(cwd))
            assert error is not None and "protected memory paths" in error
        snapshot = git_evidence.capture_git_snapshot(str(cwd), protected_memory=True)
        assert not snapshot.available and not snapshot.tracked_diff and not snapshot.untracked_files


def test_parent_listing_and_missing_file_recovery_do_not_reveal_protected_names(tmp_path, monkeypatch):
    from algo_cli import tools

    root = tmp_path.resolve()
    protected = root / "PRIVATE_DIRECTORY_CANARY"
    protected.mkdir()
    (protected / "lost.txt").write_text("PRIVATE_FILE_CANARY")
    (root / "ordinary.txt").write_text("public")
    monkeypatch.setattr(irene_memory_path_policy, "_known_protected_roots", lambda: (protected,))
    cfg = _protected_config(root)
    for name, args in (("list_directory", {"path": "."}), ("session_slash", {"command": "/ls"})):
        result = nathan_runtime.run_tool(name, args, cfg)
        assert "ordinary.txt" in result and "PRIVATE_DIRECTORY_CANARY" not in result
    monkeypatch.setattr(tools, "_missing_file_matches", lambda *a, **kw: pytest.fail("recursive recovery read"))
    result = nathan_runtime.run_tool("read_file", {"path": "lost.txt"}, cfg)
    assert "file not found" in result and "PRIVATE_DIRECTORY_CANARY" not in result


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("read_file", {"path": "memory.json"}),
        ("read_pdf", {"path": "memory.pdf"}),
        ("render_pdf_pages", {"path": "memory.pdf"}),
        ("write_file", {"path": "memory.json", "content": "x"}),
        (
            "edit_file",
            {"path": "memory.json", "old_string": "a", "new_string": "b"},
        ),
        ("find_unique_anchor", {"path": "memory.json", "needle": "x"}),
        (
            "batch_edit",
            {
                "path": "memory.json",
                "edits": [{"old_string": "a", "new_string": "b"}],
            },
        ),
        ("list_directory", {"path": "."}),
        ("search_files", {"path": ".", "pattern": "canary"}),
        ("vision_describe", {"image_path": "memory.png"}),
        ("git_status", {}),
        ("git_diff", {"path": "memory.json"}),
    ],
)
def test_protected_model_path_actions_refuse_memory_root_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, object],
) -> None:
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(protected)
    invoked: list[bool] = []
    monkeypatch.setitem(
        nathan_runtime.TOOL_MAP,
        name,
        lambda **_kwargs: invoked.append(True) or "unsafe",
    )

    result = nathan_runtime.run_tool(name, args, cfg)

    assert "protected memory paths are unavailable" in result
    assert invoked == []


def test_typed_pdf_artifact_consumer_does_not_inherit_workspace_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(workspace)
    captured: list[dict[str, object]] = []
    monkeypatch.setitem(
        nathan_runtime.TOOL_MAP,
        "vision_describe",
        lambda **kwargs: captured.append(kwargs) or "described",
    )

    result = nathan_runtime.run_tool(
        "vision_describe",
        {
            "image_path": "",
            "artifact_id": "0123456789abcdef0123456789abcdef",
            "artifact_page": 1,
            "artifact_receipt": f"hmac-sha256:{'a' * 64}",
        },
        cfg,
    )

    assert result == "described"
    assert captured == [
        {
            "image_path": "",
            "artifact_id": "0123456789abcdef0123456789abcdef",
            "artifact_page": 1,
            "artifact_receipt": f"hmac-sha256:{'a' * 64}",
        }
    ]


def test_protected_profile_mutation_has_dedicated_content_free_refusal(
    tmp_path: Path,
) -> None:
    canary = "PROFILE_PATH_POLICY_CANARY"
    cfg = _protected_config(tmp_path)

    error = irene_memory_path_policy.protected_tool_policy_error(
        "update_user_profile",
        {"content": canary},
        cfg,
    )

    assert error is not None
    assert "update_user_profile is unavailable" in error
    assert canary not in error


@pytest.mark.parametrize(
    "missing_field",
    ["artifact_id", "artifact_page", "artifact_receipt"],
)
def test_incomplete_typed_pdf_artifact_does_not_bypass_path_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
) -> None:
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(protected)
    args: dict[str, object] = {
        "image_path": "",
        "artifact_id": "0123456789abcdef0123456789abcdef",
        "artifact_page": 1,
        "artifact_receipt": f"hmac-sha256:{'a' * 64}",
    }
    args.pop(missing_field)

    result = nathan_runtime.run_tool("vision_describe", args, cfg)

    assert "protected memory paths are unavailable" in result


@pytest.mark.parametrize(
    "path_value",
    [
        "../.algo_cli/memory.json",
        "../.ALGO_CLI/memory.json",
        "~/.algo_cli/memory.json",
    ],
)
def test_protected_path_policy_normalizes_relative_case_and_tilde_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_value: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    # pathlib delegates to USERPROFILE on Windows even when HOME is set.
    # Bind both platform spellings so the tilde alias exercises this test's
    # isolated protected root instead of the hosted runner profile.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(workspace)

    result = nathan_runtime.run_tool("read_file", {"path": path_value}, cfg)

    assert "protected memory paths are unavailable" in result


def test_protected_path_policy_rejects_symlinked_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    (protected / "memory.json").write_text("SECRET", encoding="utf-8")
    (workspace / "alias").symlink_to(protected, target_is_directory=True)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(workspace)

    result = nathan_runtime.run_tool(
        "read_file",
        {"path": "alias/memory.json"},
        cfg,
    )

    assert "protected memory paths are unavailable" in result
    assert "SECRET" not in result


def test_protected_path_policy_rejects_hardlink_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    source = protected / "memory.json"
    source.write_text("HARDLINK_SECRET", encoding="utf-8")
    alias = workspace / "apparently-safe.txt"
    try:
        alias.hardlink_to(source)
    except OSError:
        pytest.skip("hardlinks are unavailable on this filesystem")
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(workspace)

    result = nathan_runtime.run_tool("read_file", {"path": alias.name}, cfg)

    assert "protected memory paths are unavailable" in result
    assert "HARDLINK_SECRET" not in result


def test_protected_path_policy_denies_extra_root_and_malformed_root_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    extra = tmp_path / "operator-memory"
    extra.mkdir()
    roots_file = protected / "harness_roots.json"
    roots_file.write_text(
        '[{"harness":"custom","kind":"wiki","root":"' + str(extra) + '","patterns":["*.md"],"max_files":5}]',
        encoding="utf-8",
    )
    roots_file.chmod(0o600)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(workspace)

    denied = nathan_runtime.run_tool(
        "read_file",
        {"path": str(extra / "fact.md")},
        cfg,
    )
    roots_file.write_text("{malformed", encoding="utf-8")
    roots_file.chmod(0o600)
    fail_closed = nathan_runtime.run_tool(
        "read_file",
        {"path": str(workspace / "safe.txt")},
        cfg,
    )

    assert "protected memory paths are unavailable" in denied
    assert "protected memory paths are unavailable" in fail_closed


def test_protected_path_policy_denies_symlinked_extra_root_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    target = tmp_path / "operator-memory"
    target.mkdir()
    alias = tmp_path / "operator-memory-alias"
    alias.symlink_to(target, target_is_directory=True)
    roots_file = protected / "harness_roots.json"
    roots_file.write_text(
        '[{"harness":"custom","kind":"wiki","root":"' + str(alias) + '","patterns":["*.md"],"max_files":5}]',
        encoding="utf-8",
    )
    roots_file.chmod(0o600)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(workspace)

    result = nathan_runtime.run_tool(
        "read_file",
        {"path": str(target / "fact.md")},
        cfg,
    )

    assert "protected memory paths are unavailable" in result


@pytest.mark.parametrize(
    "relative",
    [
        ".ollama_cli.backup/memory.json",
        ".ollama_cli.backup.migration-deadbeef/memory.json",
        ".algo_cli.migration-deadbeef/config.json",
    ],
)
def test_protected_path_policy_denies_legacy_backup_and_migration_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", tmp_path / ".ollama_cli")
    monkeypatch.setattr(
        config,
        "get_legacy_backup_dir",
        lambda: tmp_path / ".ollama_cli.backup",
    )
    cfg = _protected_config(workspace)

    result = nathan_runtime.run_tool(
        "read_file",
        {"path": str(tmp_path / relative)},
        cfg,
    )

    assert "protected memory paths are unavailable" in result


def test_protected_shell_is_disabled_but_non_memory_typed_read_still_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    safe = workspace / "safe.txt"
    safe.write_text("SAFE_CONTENT", encoding="utf-8")
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(workspace)

    shell = nathan_runtime.run_tool("run_shell", {"command": "printf unsafe"}, cfg)
    read = nathan_runtime.run_tool("read_file", {"path": "safe.txt"}, cfg)

    assert "run_shell is disabled" in shell
    assert read == "SAFE_CONTENT"


@pytest.mark.parametrize("command", ["/read memory.json", "/ls .", "/cd ."])
def test_model_session_paths_refuse_protected_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(protected)

    result = session_commands.execute(command, cfg)

    assert "protected memory paths are unavailable" in result


@pytest.mark.parametrize(
    "command",
    [
        "/embed --file memory.txt",
        "/identity",
        "/pdf --pages 1 memory.pdf",
        "/vision inspect memory.png",
    ],
)
def test_protected_session_command_denies_other_path_bearing_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(workspace)
    invoked: list[bool] = []
    monkeypatch.setitem(
        nathan_runtime.TOOL_MAP,
        "session_command",
        lambda **_kwargs: invoked.append(True) or "unsafe",
    )

    result = nathan_runtime.run_tool(
        "session_command",
        {"command": command},
        cfg,
    )

    assert "protected memory paths are unavailable" in result
    assert invoked == []


def test_protected_session_command_rejects_implicit_protected_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(protected)
    invoked: list[bool] = []
    monkeypatch.setitem(
        nathan_runtime.TOOL_MAP,
        "session_command",
        lambda **_kwargs: invoked.append(True) or "unsafe",
    )

    result = nathan_runtime.run_tool(
        "session_command",
        {"command": "/intelligence query secret"},
        cfg,
    )

    assert "protected memory paths are unavailable" in result
    assert invoked == []


def test_protected_memory_disabled_preserves_shell_and_path_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = Config(cwd=str(protected), continuum_enabled=False)
    monkeypatch.setitem(
        nathan_runtime.TOOL_MAP,
        "run_shell",
        lambda **_kwargs: "LEGACY_SHELL_ALLOWED",
    )

    result = nathan_runtime.run_tool("run_shell", {"command": "true"}, cfg)

    assert result == "LEGACY_SHELL_ALLOWED"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS path alias contract")
def test_macos_standard_symlink_alias_requires_canonical_private_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    safe_file = workspace / "safe.txt"
    safe_file.write_text("safe", encoding="utf-8")
    canonical = str(safe_file)
    if not canonical.startswith("/private/var/"):
        pytest.skip("temporary directory is not below the macOS /var alias")
    alias = canonical.replace("/private/var/", "/var/", 1)
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = _protected_config(workspace)
    monkeypatch.setitem(
        nathan_runtime.TOOL_MAP,
        "read_file",
        lambda **_kwargs: "CANONICAL_SAFE_PATH_ALLOWED",
    )

    refused = nathan_runtime.run_tool("read_file", {"path": alias}, cfg)
    allowed = nathan_runtime.run_tool("read_file", {"path": canonical}, cfg)

    assert "protected memory paths are unavailable" in refused
    assert allowed == "CANONICAL_SAFE_PATH_ALLOWED"
