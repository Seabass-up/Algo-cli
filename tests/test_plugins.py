"""Tests for the plugin system (algo_cli.plugins)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from algo_cli import plugins


@pytest.fixture
def plugin_dir(tmp_path: Path, monkeypatch) -> Path:
    """Create a temporary plugins directory."""
    pdir = tmp_path / "plugins"
    pdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(plugins, "PLUGINS_DIR", pdir)
    return pdir


def _make_plugin(pdir: Path, name: str, *, version: str = "1.0.0",
                 description: str = "Test plugin", enabled: bool = True,
                 code: str = "") -> Path:
    """Create a minimal plugin directory with manifest and __init__.py."""
    ppath = pdir / name
    ppath.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": version,
        "description": description,
        "enabled": enabled,
        "author": "test",
    }
    (ppath / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (ppath / "__init__.py").write_text(code, encoding="utf-8")
    return ppath


class TestPluginDiscovery:
    def test_discover_empty_dir(self, plugin_dir):
        manifests = plugins.discover_plugins()
        assert manifests == []

    def test_discover_single_plugin(self, plugin_dir):
        _make_plugin(plugin_dir, "test-plugin")
        manifests = plugins.discover_plugins()
        assert len(manifests) == 1
        assert manifests[0].name == "test-plugin"
        assert manifests[0].version == "1.0.0"

    def test_discover_multiple_plugins_sorted(self, plugin_dir):
        _make_plugin(plugin_dir, "zebra")
        _make_plugin(plugin_dir, "alpha")
        manifests = plugins.discover_plugins()
        assert len(manifests) == 2
        assert manifests[0].name == "alpha"
        assert manifests[1].name == "zebra"

    def test_discover_skips_dirs_without_manifest(self, plugin_dir):
        (plugin_dir / "no-manifest").mkdir()
        manifests = plugins.discover_plugins()
        assert manifests == []

    def test_discover_skips_files(self, plugin_dir):
        (plugin_dir / "not-a-dir.json").write_text("{}")
        manifests = plugins.discover_plugins()
        assert manifests == []

    def test_discover_handles_corrupt_manifest(self, plugin_dir):
        ppath = plugin_dir / "broken"
        ppath.mkdir()
        (ppath / "plugin.json").write_text("{invalid json", encoding="utf-8")
        manifests = plugins.discover_plugins()
        assert manifests == []


class TestPluginLoading:
    def test_load_plugin_success(self, plugin_dir):
        _make_plugin(plugin_dir, "good-plugin", code="VALUE = 42\n")
        manifests = plugins.discover_plugins()
        loaded = plugins.load_plugin(manifests[0])
        assert loaded.loaded
        assert loaded.load_error == ""
        assert loaded.module is not None
        assert loaded.module.VALUE == 42

    def test_load_plugin_no_init(self, plugin_dir):
        ppath = plugin_dir / "no-init"
        ppath.mkdir()
        (ppath / "plugin.json").write_text(
            json.dumps({"name": "no-init", "version": "1.0.0"}), encoding="utf-8"
        )
        manifests = plugins.discover_plugins()
        loaded = plugins.load_plugin(manifests[0])
        assert not loaded.loaded
        assert "No __init__.py" in loaded.load_error

    def test_load_plugin_broken_code(self, plugin_dir):
        _make_plugin(plugin_dir, "broken", code="raise RuntimeError('boom')\n")
        manifests = plugins.discover_plugins()
        loaded = plugins.load_plugin(manifests[0])
        assert not loaded.loaded
        assert "boom" in loaded.load_error

    def test_load_disabled_plugin(self, plugin_dir):
        _make_plugin(plugin_dir, "disabled", enabled=False, code="VALUE = 1\n")
        manifests = plugins.discover_plugins()
        loaded = plugins.load_plugin(manifests[0])
        assert not loaded.loaded
        assert "disabled" in loaded.load_error.lower()


class TestPluginEntryPoints:
    def test_collect_actions(self, plugin_dir):
        code = (
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True)\n"
            "class FakeAction:\n"
            "    name: str\n"
            "def register_actions():\n"
            "    return [FakeAction('test-action')]\n"
        )
        _make_plugin(plugin_dir, "action-plugin", code=code)
        loaded = plugins.load_all_plugins()
        actions = plugins.collect_plugin_actions(loaded)
        assert len(actions) == 1
        assert actions[0].name == "test-action"

    def test_collect_slash_commands(self, plugin_dir):
        code = (
            "def register_slash_commands():\n"
            "    return [('/test', 'Test command')]\n"
        )
        _make_plugin(plugin_dir, "slash-plugin", code=code)
        loaded = plugins.load_all_plugins()
        cmds = plugins.collect_plugin_slash_commands(loaded)
        assert cmds == [("/test", "Test command")]

    def test_collect_tools(self, plugin_dir):
        code = (
            "def my_tool():\n"
            "    return 'hello'\n"
            "def register_tools():\n"
            "    return {'my_tool': my_tool}\n"
        )
        _make_plugin(plugin_dir, "tool-plugin", code=code)
        loaded = plugins.load_all_plugins()
        tools = plugins.collect_plugin_tools(loaded)
        assert "my_tool" in tools
        assert tools["my_tool"]() == "hello"

    def test_collect_from_broken_plugin_does_not_crash(self, plugin_dir):
        _make_plugin(plugin_dir, "ok", code="def register_actions(): return []\n")
        _make_plugin(plugin_dir, "bad", code="raise RuntimeError('x')\n")
        loaded = plugins.load_all_plugins()
        actions = plugins.collect_plugin_actions(loaded)
        # Only the OK plugin's actions are collected
        assert actions == []


class TestPluginStatus:
    def test_plugin_status_empty(self, plugin_dir):
        status = plugins.plugin_status()
        assert status == []

    def test_plugin_status_with_plugins(self, plugin_dir):
        _make_plugin(plugin_dir, "p1", code="x = 1\n")
        _make_plugin(plugin_dir, "p2", enabled=False, code="x = 2\n")
        status = plugins.plugin_status()
        assert len(status) == 2
        p1 = [s for s in status if s["name"] == "p1"][0]
        p2 = [s for s in status if s["name"] == "p2"][0]
        assert p1["loaded"] is False
        assert p1["state"] == "discovered"
        assert p1["path"] == "plugins/p1"
        assert p2["loaded"] is False
        assert p2["state"] == "disabled"
