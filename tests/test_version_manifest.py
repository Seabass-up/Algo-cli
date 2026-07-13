"""Tests for the version manifest (algo_cli.version_manifest)."""

from __future__ import annotations

import json
from pathlib import Path

from algo_cli import extensions_manifest, tools, version_manifest


class TestVersionManifest:
    def test_build_manifest_has_cli_version(self):
        m = version_manifest.build_manifest()
        assert m.cli_version == "0.15.0"

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


def test_extensions_manifest_tool_returns_json():
    payload = json.loads(tools.extensions_manifest_build())

    assert "components" in payload
    assert any(component["name"] == "git" for component in payload["components"])
