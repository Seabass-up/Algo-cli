"""Adversarial tests for manifest-only William plugin discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from algo_cli import william_plugins as plugins


@pytest.fixture
def plugin_dir(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "plugins"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(plugins, "PLUGINS_DIR", root)
    return root


def _manifest(name: str, **overrides) -> dict:
    value = {
        "schema_version": 1,
        "name": name,
        "version": "1.0.0",
        "description": "Test plugin",
        "author": "test",
        "enabled": True,
        "entry_points": [],
    }
    value.update(overrides)
    return value


def _make_plugin(
    root: Path,
    name: str,
    *,
    manifest: dict | None = None,
    code: str = "",
) -> Path:
    plugin_path = root / name
    plugin_path.mkdir(mode=0o700)
    (plugin_path / "plugin.json").write_text(
        json.dumps(manifest if manifest is not None else _manifest(name)),
        encoding="utf-8",
    )
    if code:
        (plugin_path / "__init__.py").write_text(code, encoding="utf-8")
    return plugin_path


def _rejection_codes(root: Path) -> set[str]:
    return {item.error_code for item in plugins.scan_plugins(root).rejections}


class TestPluginDiscovery:
    def test_windows_reparse_tags_are_treated_as_links(self, tmp_path) -> None:
        info = SimpleNamespace(st_mode=0o040700, st_reparse_tag=1)

        assert plugins._is_link_or_reparse(tmp_path, info) is True

    def test_missing_and_empty_roots_are_non_executable(self, tmp_path) -> None:
        missing = plugins.scan_plugins(tmp_path / "missing")
        empty_root = tmp_path / "empty"
        empty_root.mkdir(mode=0o700)
        empty = plugins.scan_plugins(empty_root)

        assert missing.manifests == ()
        assert missing.rejections == ()
        assert missing.root_ready is False
        assert empty.manifests == ()
        assert empty.root_ready is True

    def test_discovers_strict_manifests_in_canonical_order(self, plugin_dir) -> None:
        _make_plugin(plugin_dir, "zebra", manifest=_manifest("zebra", enabled=False))
        _make_plugin(plugin_dir, "alpha")

        manifests = plugins.discover_plugins()

        assert [item.name for item in manifests] == ["alpha", "zebra"]
        assert manifests[0].schema_version == 1
        assert manifests[1].enabled is False

    def test_non_plugin_directories_and_regular_files_are_ignored(self, plugin_dir) -> None:
        (plugin_dir / "no-manifest").mkdir(mode=0o700)
        (plugin_dir / "note.txt").write_text("not a plugin", encoding="utf-8")

        scan = plugins.scan_plugins()

        assert scan.manifests == ()
        assert scan.rejections == ()

    @pytest.mark.parametrize(
        ("manifest", "code"),
        [
            ({"name": "demo", "version": "1.0.0"}, "manifest_version"),
            (_manifest("demo", schema_version=2), "manifest_version"),
            (_manifest("demo", version="latest"), "manifest_version"),
            (_manifest("demo", enabled="yes"), "manifest_schema"),
            (_manifest("demo", unknown=True), "manifest_schema"),
            (_manifest("other"), "name_mismatch"),
            (_manifest("Demo"), "manifest_name"),
            (_manifest("demo", description="bad\x1b[31m"), "manifest_schema"),
            (_manifest("demo", entry_points=["register_tools"]), "privileged_contribution"),
            (_manifest("demo", tools={"read_file": "override"}), "privileged_contribution"),
            (_manifest("demo", capabilities=["desktop_input"]), "privileged_contribution"),
        ],
    )
    def test_strict_schema_rejects_ambiguous_or_privileged_manifests(self, plugin_dir, manifest, code) -> None:
        _make_plugin(plugin_dir, "demo", manifest=manifest)

        scan = plugins.scan_plugins()

        assert scan.manifests == ()
        assert [item.error_code for item in scan.rejections] == [code]

    def test_invalid_json_top_level_and_duplicate_keys_reject(self, plugin_dir) -> None:
        plugin_path = plugin_dir / "demo"
        plugin_path.mkdir(mode=0o700)
        manifest_path = plugin_path / "plugin.json"

        manifest_path.write_text("{invalid", encoding="utf-8")
        assert _rejection_codes(plugin_dir) == {"manifest_json"}

        manifest_path.write_text("[]", encoding="utf-8")
        assert _rejection_codes(plugin_dir) == {"manifest_schema"}

        manifest_path.write_text(
            '{"schema_version":1,"name":"demo","name":"other","version":"1.0.0"}',
            encoding="utf-8",
        )
        assert _rejection_codes(plugin_dir) == {"manifest_duplicate_key"}

        manifest_path.write_text(
            '{"schema_version":NaN,"name":"demo","version":"1.0.0"}',
            encoding="utf-8",
        )
        assert _rejection_codes(plugin_dir) == {"manifest_json"}

        manifest_path.write_text(
            '{"schema_version":1,"name":"demo","version":"1.0.0","description":"\\ud800"}',
            encoding="utf-8",
        )
        assert _rejection_codes(plugin_dir) == {"manifest_encoding"}

    def test_non_utf8_and_oversized_manifests_reject(self, plugin_dir) -> None:
        plugin_path = plugin_dir / "demo"
        plugin_path.mkdir(mode=0o700)
        manifest_path = plugin_path / "plugin.json"

        manifest_path.write_bytes(b"\xff\xfe")
        assert _rejection_codes(plugin_dir) == {"manifest_encoding"}

        manifest_path.write_bytes(b"x" * (plugins.MAX_MANIFEST_BYTES + 1))
        assert _rejection_codes(plugin_dir) == {"manifest_oversize"}

    def test_directory_name_and_manifest_name_cannot_traverse(self, plugin_dir) -> None:
        _make_plugin(plugin_dir, "demo", manifest=_manifest("../outside"))
        invalid = plugin_dir / "bad_name"
        invalid.mkdir(mode=0o700)
        (invalid / "plugin.json").write_text(json.dumps(_manifest("bad_name")), encoding="utf-8")

        scan = plugins.scan_plugins()

        assert scan.manifests == ()
        assert {item.error_code for item in scan.rejections} == {
            "manifest_name",
            "directory_name",
        }
        assert all("outside" not in item.logical_name for item in scan.rejections)

    def test_plugin_directory_symlink_escape_rejects(self, plugin_dir, tmp_path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        (outside / "plugin.json").write_text(json.dumps(_manifest("escape")), encoding="utf-8")
        try:
            (plugin_dir / "escape").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")

        scan = plugins.scan_plugins()

        assert scan.manifests == ()
        assert _rejection_codes(plugin_dir) == {"symlink_entry"}

    def test_manifest_symlink_escape_rejects(self, plugin_dir, tmp_path) -> None:
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps(_manifest("demo")), encoding="utf-8")
        plugin_path = plugin_dir / "demo"
        plugin_path.mkdir(mode=0o700)
        try:
            (plugin_path / "plugin.json").symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")

        assert _rejection_codes(plugin_dir) == {"manifest_symlink"}

    def test_symlink_root_rejects_without_following(self, tmp_path) -> None:
        real_root = tmp_path / "real"
        real_root.mkdir(mode=0o700)
        _make_plugin(real_root, "demo")
        linked_root = tmp_path / "linked"
        try:
            linked_root.symlink_to(real_root, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")

        scan = plugins.scan_plugins(linked_root)

        assert scan.manifests == ()
        assert scan.root_ready is False
        assert [item.error_code for item in scan.rejections] == ["root_unsafe"]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
    def test_group_writable_root_directory_and_manifest_reject(self, tmp_path) -> None:
        root = tmp_path / "plugins"
        root.mkdir(mode=0o700)
        root.chmod(0o770)
        assert [item.error_code for item in plugins.scan_plugins(root).rejections] == ["root_permissions"]

        root.chmod(0o700)
        plugin_path = _make_plugin(root, "demo")
        (plugin_path / "plugin.json").chmod(0o660)
        assert _rejection_codes(root) == {"manifest_permissions"}

    @pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO only")
    def test_special_manifest_file_rejects_without_opening(self, plugin_dir) -> None:
        plugin_path = plugin_dir / "demo"
        plugin_path.mkdir(mode=0o700)
        os.mkfifo(plugin_path / "plugin.json", mode=0o600)

        assert _rejection_codes(plugin_dir) == {"manifest_special"}

    def test_hardlinked_manifest_rejects(self, plugin_dir, tmp_path) -> None:
        source = tmp_path / "source.json"
        source.write_text(json.dumps(_manifest("demo")), encoding="utf-8")
        plugin_path = plugin_dir / "demo"
        plugin_path.mkdir(mode=0o700)
        try:
            os.link(source, plugin_path / "plugin.json")
        except OSError as exc:
            pytest.skip(f"hard links unavailable: {exc}")

        assert _rejection_codes(plugin_dir) == {"manifest_hardlink"}

    def test_duplicate_canonical_names_remove_every_candidate(self, plugin_dir, monkeypatch) -> None:
        _make_plugin(plugin_dir, "first")
        _make_plugin(plugin_dir, "second")

        def same_manifest(_path, *, expected_name):
            del expected_name
            return plugins.PluginManifest("collision", "1.0.0", "collision")

        monkeypatch.setattr(plugins, "_parse_manifest", same_manifest)
        scan = plugins.scan_plugins()

        assert scan.manifests == ()
        assert "duplicate_plugin" in {item.error_code for item in scan.rejections}


class TestPluginLoading:
    def test_load_never_executes_python_module(self, plugin_dir, tmp_path) -> None:
        marker = tmp_path / "executed.txt"
        _make_plugin(
            plugin_dir,
            "demo",
            code=f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        )
        manifest = plugins.discover_plugins()[0]

        loaded = plugins.load_plugin(manifest)

        assert loaded.loaded is False
        assert loaded.module is None
        assert loaded.error_code == "code_loading_disabled"
        assert "no local plugin execution route" in loaded.load_error
        assert not marker.exists()

    def test_disabled_manifest_is_still_revalidated(self, plugin_dir) -> None:
        forged = plugins.PluginManifest("disabled", "1.0.0", "missing", enabled=False)

        loaded = plugins.load_plugin(forged)

        assert loaded.error_code == "manifest_revalidation_failed"

    def test_load_all_returns_explicit_blocked_results(self, plugin_dir) -> None:
        _make_plugin(plugin_dir, "alpha")
        _make_plugin(plugin_dir, "disabled", manifest=_manifest("disabled", enabled=False))

        loaded = plugins.load_all_plugins()

        assert [item.name for item in loaded] == ["alpha", "disabled"]
        assert all(item.loaded is False and item.module is None for item in loaded)
        assert loaded[0].error_code == "code_loading_disabled"
        assert loaded[1].error_code == "plugin_disabled"

    def test_manifest_is_revalidated_at_load_time(self, plugin_dir) -> None:
        plugin_path = _make_plugin(plugin_dir, "demo")
        manifest = plugins.discover_plugins()[0]
        (plugin_path / "plugin.json").write_text(json.dumps(_manifest("demo", version="2.0.0")), encoding="utf-8")

        loaded = plugins.load_plugin(manifest)

        assert loaded.error_code == "manifest_revalidation_failed"
        assert loaded.loaded is False

    def test_forged_traversal_manifest_never_builds_a_traversal_status_path(self, plugin_dir) -> None:
        with pytest.raises(plugins.PluginValidationError, match="object is invalid"):
            plugins.PluginManifest("../outside", "1.0.0", "forged")
        forged = object.__new__(plugins.PluginManifest)
        object.__setattr__(forged, "name", "../outside")
        object.__setattr__(forged, "version", "1.0.0")
        object.__setattr__(forged, "description", "forged")
        object.__setattr__(forged, "author", "")
        object.__setattr__(forged, "entry_points", ())
        object.__setattr__(forged, "enabled", True)
        object.__setattr__(forged, "schema_version", 1)

        loaded = plugins.load_plugin(forged)
        status = loaded.as_dict()

        assert loaded.error_code == "manifest_revalidation_failed"
        assert status["path"] == "plugins/invalid-plugin"
        assert ".." not in json.dumps(status)


class TestPluginContributions:
    def test_callable_contributions_are_never_invoked_or_registered(self) -> None:
        calls: list[str] = []

        def invoked(name):
            def register():
                calls.append(name)
                raise AssertionError("plugin callable must not execute")

            return register

        forged_module = SimpleNamespace(
            register_actions=invoked("actions"),
            register_slash_commands=invoked("commands"),
            register_tools=invoked("tools"),
        )
        forged = plugins.LoadedPlugin(
            plugins.PluginManifest("forged", "1.0.0", "forged"),
            module=forged_module,
            loaded=True,
        )

        assert plugins.collect_plugin_actions([forged]) == []
        assert plugins.collect_plugin_slash_commands([forged]) == []
        assert plugins.collect_plugin_tools([forged]) == {}
        assert calls == []


class TestPluginStatus:
    def test_status_distinguishes_discovered_disabled_and_rejected(self, plugin_dir) -> None:
        _make_plugin(plugin_dir, "ready")
        _make_plugin(plugin_dir, "disabled", manifest=_manifest("disabled", enabled=False))
        _make_plugin(plugin_dir, "rejected", manifest=_manifest("rejected", tools={}))

        statuses = plugins.plugin_status()
        by_name = {item["name"]: item for item in statuses}

        assert by_name["ready"]["state"] == "discovered"
        assert by_name["ready"]["code_loading"] is False
        assert by_name["ready"]["security_boundary"] is False
        assert by_name["disabled"]["state"] == "disabled"
        assert by_name["rejected"]["state"] == "rejected"
        assert by_name["rejected"]["error_code"] == "privileged_contribution"
        assert all(item["loaded"] is False for item in statuses)
        assert str(plugin_dir.parent) not in json.dumps(statuses)

    def test_ensure_plugins_dir_is_private_and_rejects_symlink(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "new-plugins"
        monkeypatch.setattr(plugins, "PLUGINS_DIR", root)

        created = plugins.ensure_plugins_dir()

        assert created == root
        assert created.is_dir()
        if os.name == "posix":
            assert stat_mode(created) == 0o700

        created.rmdir()
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        try:
            root.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
        with pytest.raises(plugins.PluginValidationError, match="non-symlink"):
            plugins.ensure_plugins_dir()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
