"""Personal catalog and kernels stay in the user's config directory, never in the package."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from rich.console import Console

from algo_cli import harness, main
from algo_cli.kernels import manifest
from algo_cli.pattern_catalog import parse_patterns

ROOT = Path(__file__).resolve().parents[1]


def _render(monkeypatch, fn, *args) -> str:
    output = io.StringIO()
    monkeypatch.setattr(main, "console", Console(file=output, width=400, color_system=None))
    monkeypatch.setattr(main, "show_info", lambda text: output.write(f"{text}\n"))
    monkeypatch.setattr(main, "show_error", lambda text: output.write(f"error: {text}\n"))
    fn(*args)
    return output.getvalue()


def _write_kernels(config_dir: Path, rows) -> Path:
    path = config_dir / "kernels" / "kernels.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"kernels": rows}), encoding="utf-8")
    return path


def test_public_catalog_is_an_empty_template() -> None:
    assert parse_patterns((ROOT / "docs" / "ALGO.md").read_text(encoding="utf-8")) == []


def test_no_personal_kernels_ship_built_in() -> None:
    assert manifest.load_user_kernels().kernels == ()
    assert {spec.source for spec in manifest.list_kernels()} == {"built-in"}
    assert manifest.get_kernel("acrobat") is None
    assert not (ROOT / "algo_cli" / "intelligence" / "finance").exists()
    assert not (ROOT / "algo_cli" / "intelligence" / "construction").exists()


def test_user_kernels_load_from_config_dir(config_dir: Path, monkeypatch) -> None:
    package = config_dir / "kernels" / "my_kernels_fixture"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_kernels(
        config_dir,
        [{"name": "Mine", "description": "fixture", "modules": ["my_kernels_fixture"], "slash_commands": ["/kernel show mine"]}],
    )
    monkeypatch.setattr(sys, "path", list(sys.path))

    spec = manifest.get_kernel("mine")
    assert spec is not None and spec.source == "user" and spec.status == "preview"
    assert "mine" in manifest.kernel_names()
    [audit] = manifest.audit_kernels("mine")
    assert audit.issues == ()
    assert sys.path[-1] == str(config_dir / "kernels")
    snapshot = manifest.kernel_runtime_snapshot()
    assert {"name": "mine", "source": "user"}.items() <= next(k for k in snapshot["kernels"] if k["name"] == "mine").items()


def test_user_kernels_cannot_replace_built_ins_or_carry_bad_fields(config_dir: Path) -> None:
    _write_kernels(
        config_dir,
        [
            {"name": "review", "description": "shadow"},
            {"name": "extra", "description": "x", "run": "rm -rf /"},
            {"name": "badlist", "modules": "not-a-list"},
            "not-an-object",
            {"description": "nameless"},
            {"name": "ok"},
            {"name": "OK"},
        ],
    )
    catalog = manifest.load_user_kernels()
    assert [spec.name for spec in catalog.kernels] == ["ok"]
    assert len(catalog.issues) == 6
    assert manifest.get_kernel("review").source == "built-in"


def test_malformed_kernel_file_reports_issue_without_kernels(config_dir: Path) -> None:
    path = config_dir / "kernels" / "kernels.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    catalog = manifest.load_user_kernels()
    assert catalog.kernels == () and catalog.issues


def test_harness_prefers_user_catalog(config_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(harness, "CONFIG_DIR", config_dir)
    assert harness._algo_catalog_dir() == harness._algo_cli_docs_dir()
    (config_dir / "ALGO.md").write_text("# ALGO.md\n", encoding="utf-8")
    assert harness._algo_catalog_dir() == config_dir
    roots = [root for root in harness.built_in_source_roots() if root.kind == "algorithm"]
    assert [root.root for root in roots] == [config_dir]


def test_intelligence_init_creates_library_without_overwriting(config_dir: Path, monkeypatch) -> None:
    import algo_cli.config as config_module

    monkeypatch.setattr(config_module, "CONFIG_DIR", config_dir)
    before = _render(monkeypatch, main.handle_intelligence_command, "status", None)
    assert "catalog     : empty" in before and "/intelligence init" in before

    _render(monkeypatch, main.handle_intelligence_command, "init", None)
    catalog = config_dir / "ALGO.md"
    kernels = config_dir / "kernels" / "kernels.json"
    assert catalog.read_text(encoding="utf-8") == (ROOT / "docs" / "ALGO.md").read_text(encoding="utf-8")
    assert json.loads(kernels.read_text(encoding="utf-8")) == {"kernels": []}

    catalog.write_text("# mine\n", encoding="utf-8")
    again = _render(monkeypatch, main.handle_intelligence_command, "init", None)
    assert "already exists" in again
    assert catalog.read_text(encoding="utf-8") == "# mine\n"


def test_kernel_list_separates_user_section(config_dir: Path, monkeypatch) -> None:
    empty = _render(monkeypatch, main.handle_kernel_command, "list")
    assert "Built-in kernels:" in empty and "Your kernels:\n  none yet" in empty
    _write_kernels(config_dir, [{"name": "mine", "description": "fixture kernel"}])
    listed = _render(monkeypatch, main.handle_kernel_command, "list")
    assert "Your kernels:\n  mine (preview/medium) - fixture kernel" in listed
    assert "kernels.json" in _render(monkeypatch, main.handle_kernel_command, "help")


def test_user_kernel_file_is_bounded_before_and_after_parsing(config_dir: Path) -> None:
    path = _write_kernels(config_dir, [{"name": f"k{index}"} for index in range(manifest.MAX_USER_KERNELS + 1)])
    catalog = manifest.load_user_kernels()
    assert catalog.kernels == () and "more than" in catalog.issues[0]

    path.write_bytes(b" " * (manifest.MAX_USER_KERNELS_BYTES + 1))
    catalog = manifest.load_user_kernels()
    assert catalog.kernels == () and "larger than" in catalog.issues[0]

    _write_kernels(
        config_dir,
        [
            {"name": "n" * 65},
            {"name": "longtext", "description": "d" * (manifest.MAX_USER_KERNEL_TEXT + 1)},
            {"name": "manymods", "modules": ["m"] * 65},
            {"name": "fine"},
        ],
    )
    catalog = manifest.load_user_kernels()
    assert [spec.name for spec in catalog.kernels] == ["fine"] and len(catalog.issues) == 3
