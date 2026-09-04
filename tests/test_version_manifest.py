"""Tests for the version manifest (algo_cli.version_manifest)."""

from __future__ import annotations

import json
from pathlib import Path

from algo_cli import argon_extensions_manifest as extensions_manifest
from algo_cli import tools, version_manifest


class TestVersionManifest:
    def test_build_manifest_has_cli_version(self):
        m = version_manifest.build_manifest()
        assert m.cli_version == "0.18.0"

    def test_build_manifest_has_python_version(self):
        m = version_manifest.build_manifest()
        assert m.python_version != ""
        # Should look like "3.x.y"
        assert m.python_version.startswith("3.")

    def test_build_manifest_has_platform(self):
        m = version_manifest.build_manifest()
        assert m.platform != ""

    def test_build_manifest_has_config_dir(self):
        m = version_manifest.build_manifest()
        assert m.config_dir != ""

    def test_build_manifest_does_not_create_harness_index(self, tmp_path, monkeypatch):
        from algo_cli import harness

        index_path = tmp_path / "harness_index.json"
        monkeypatch.setattr(harness, "INDEX_PATH", index_path)
        monkeypatch.setattr(
            harness,
            "load_index",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not build")),
        )

        version_manifest.build_manifest()

        assert not index_path.exists()

    def test_manifest_to_json(self):
        m = version_manifest.VersionManifest(cli_version="9.9.9")
        j = json.loads(m.to_json())
        assert j["cli_version"] == "9.9.9"

    def test_save_and_load_manifest(self, tmp_path: Path, monkeypatch):
        # Point VERSIONS_FILE to temp
        vfile = tmp_path / "versions.json"
        monkeypatch.setattr(version_manifest, "VERSIONS_FILE", vfile)
        monkeypatch.setattr(version_manifest, "CONFIG_DIR", tmp_path)

        m = version_manifest.VersionManifest(cli_version="1.2.3")
        vfile.write_text(m.to_json(), encoding="utf-8")

        loaded = version_manifest.load_manifest()
        assert loaded is not None
        assert loaded.cli_version == "1.2.3"

    def test_load_manifest_missing_file(self, tmp_path: Path, monkeypatch):
        vfile = tmp_path / "nonexistent.json"
        monkeypatch.setattr(version_manifest, "VERSIONS_FILE", vfile)
        loaded = version_manifest.load_manifest()
        assert loaded is None

    def test_load_manifest_corrupt_file(self, tmp_path: Path, monkeypatch):
        vfile = tmp_path / "corrupt.json"
        vfile.write_text("{invalid", encoding="utf-8")
        monkeypatch.setattr(version_manifest, "VERSIONS_FILE", vfile)
        loaded = version_manifest.load_manifest()
        assert loaded is None

    def test_format_version_string(self):
        m = version_manifest.VersionManifest(
            cli_version="1.0.0",
            python_version="3.12.0",
            platform="Darwin arm64",
            config_dir="/tmp/test",
            harness_record_count=100,
            harness_index_version="2",
            harness_embed_model="qwen3-embedding:latest",
        )
        s = version_manifest.format_version_string(m)
        assert "Algo CLI v1.0.0" in s
        assert "3.12.0" in s
        assert "100 records" in s
        assert "qwen3-embedding" in s

    def test_format_version_string_minimal(self):
        m = version_manifest.VersionManifest(cli_version="0.1.0")
        s = version_manifest.format_version_string(m)
        assert "Algo CLI v0.1.0" in s

    def test_format_version_string_with_plugins(self):
        m = version_manifest.VersionManifest(
            cli_version="1.0.0",
            plugins={"my-plugin": "2.0.0", "other": "0.1.0"},
        )
        s = version_manifest.format_version_string(m)
        assert "my-plugin" in s
        assert "2.0.0" in s


def test_extensions_manifest_has_component_records():
    manifest = extensions_manifest.build_extensions_manifest()

    names = {component.name for component in manifest.components}
    assert {"ollama", "git", "gh", "lms"}.issubset(names)
    assert all(component.kind for component in manifest.components)


def test_extensions_manifest_includes_discovered_plugin_path(tmp_path, monkeypatch):
    from algo_cli import william_plugins as plugins

    plugin_dir = tmp_path / "example-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "example-plugin",
                "version": "1.2.3",
                "description": "test plugin",
                "entry_points": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(plugins, "PLUGINS_DIR", tmp_path)

    manifest = extensions_manifest.build_extensions_manifest()
    component = next(
        item for item in manifest.components if item.name == "example-plugin"
    )

    assert component.version == "1.2.3"
    assert component.path == "plugins/example-plugin"


def test_extensions_manifest_tool_returns_json():
    payload = json.loads(tools.extensions_manifest_build())

    assert "components" in payload
    assert any(component["name"] == "git" for component in payload["components"])


def test_homebrew_helper_catalog_has_all_unique_requested_packages():
    expected = {
        "awww", "b4n", "bluetuith", "cliphist", "cuttlefish", "evnx",
        "fluxcd", "fusesoc", "inshellisense", "kata", "lld@22", "llvm@22",
        "nats", "rammap", "svlang", "systemd-lsp", "tracy-genomics",
        "vapoursynth-vszip", "vi-sql", "xmedcon", "bb", "ds4-control",
        "font-jetendard", "font-nexon-football-gothic",
        "font-nexon-kart-gothic", "glean", "grok-bot", "mongrel",
        "muse-code", "owlocr", "petdex", "sentry-cli", "sina-finance",
        "subtitle-edit", "warp-agent-cli",
    }

    records = extensions_manifest.HOMEBREW_HELPERS

    assert len(records) == len(expected) == 35
    assert {name for name, _kind, _probe in records} == expected
    assert len({name for name, _kind, _probe in records}) == len(records)
    assert all(kind in {"homebrew-formula", "homebrew-cask", "homebrew-font"}
               for _name, kind, _probe in records)


def test_homebrew_helpers_are_missing_without_brew(monkeypatch):
    monkeypatch.setattr(extensions_manifest.shutil, "which", lambda _name: None)

    components = extensions_manifest._homebrew_components()

    assert len(components) == 35
    assert all(component.status == "missing" for component in components)
    assert all(component.path == "" for component in components)


def test_homebrew_formula_detection_uses_receipt_without_execution(tmp_path, monkeypatch):
    prefix = tmp_path / "homebrew"
    receipt = prefix / "Cellar" / "fluxcd" / "2.7.0"
    receipt.mkdir(parents=True)
    command = prefix / "opt" / "fluxcd" / "bin" / "flux"
    command.parent.mkdir(parents=True)
    command.write_text("not executed", encoding="utf-8")
    calls: list[str] = []

    def fake_which(name: str) -> str | None:
        calls.append(name)
        return str(prefix / "bin" / "brew") if name == "brew" else None

    monkeypatch.setattr(extensions_manifest.shutil, "which", fake_which)

    component = extensions_manifest._homebrew_component(
        "fluxcd", "homebrew-formula", "flux", prefix=prefix
    )

    assert component.status == "ready"
    assert component.version == "2.7.0"
    assert component.path == str(command)
    assert calls == []


def test_homebrew_formula_alias_does_not_satisfy_exact_receipt(tmp_path):
    prefix = tmp_path / "homebrew"
    aliased_command = prefix / "opt" / "llvm@22" / "bin" / "clang"
    aliased_command.parent.mkdir(parents=True)
    aliased_command.write_text("alias", encoding="utf-8")
    (prefix / "Cellar" / "llvm" / "22.1.8").mkdir(parents=True)

    component = extensions_manifest._homebrew_component(
        "llvm@22", "homebrew-formula", "clang", prefix=prefix
    )

    assert component.status == "missing"
    assert component.path == ""


def test_homebrew_plugin_formula_is_installed_not_ready(tmp_path):
    prefix = tmp_path / "homebrew"
    receipt = prefix / "Cellar" / "vapoursynth-vszip"
    receipt.mkdir(parents=True)

    component = extensions_manifest._homebrew_component(
        "vapoursynth-vszip", "homebrew-formula", "", prefix=prefix
    )

    assert component.status == "installed"
    assert component.path == str(receipt)


def test_homebrew_cask_requires_receipt_even_if_colliding_command_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(
        extensions_manifest.shutil,
        "which",
        lambda name: "/usr/local/bin/muse" if name == "muse" else None,
    )

    component = extensions_manifest._homebrew_component(
        "muse-code", "homebrew-cask", "muse", prefix=tmp_path / "homebrew"
    )

    assert component.status == "missing"
    assert component.path == ""
