"""Adversarial tests for Echo-exclusive model filesystem authority."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from algo_cli import config, irene_memory_path_policy, nathan_runtime, session_commands
from algo_cli.config import Config


def _protected_config(cwd: Path) -> Config:
    return Config(
        cwd=str(cwd),
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )


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


def test_echo_disabled_preserves_shell_and_path_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = tmp_path / ".algo_cli"
    protected.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = Config(
        cwd=str(protected),
        echo_veil_enabled=False,
        echo_veil_protection="optional",
    )
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
