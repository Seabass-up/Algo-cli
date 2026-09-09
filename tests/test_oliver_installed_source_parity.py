from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "oliver_installed_source_parity.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("oliver_installed_source_parity", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = SCRIPT
SCRIPT_SPEC.loader.exec_module(SCRIPT)


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source" / "algo_cli"
    installed = tmp_path / "installed" / "algo_cli"
    for root in (source, installed):
        (root / "nested").mkdir(parents=True)
        (root / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
        (root / "nested" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        (root / "resource.json").write_text('{"safe":true}\n', encoding="utf-8")
    _manifest(source, {})
    return source, installed


def _manifest(source: Path, entries: dict[str, object]) -> None:
    lines = [
        "[tool.hatch.build.targets.wheel]",
        'packages = ["algo_cli"]',
        "[tool.hatch.build.targets.wheel.force-include]",
    ]
    lines.extend(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in entries.items())
    (source.parent / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("state", ["current", "missing", "stale"])
def test_force_included_document_is_bound_to_its_source(tmp_path, state) -> None:
    source, installed = _trees(tmp_path)
    docs = source.parent / "docs"
    docs.mkdir()
    (docs / "ALGO.md").write_text("current catalog\n", encoding="utf-8")
    _manifest(source, {"docs/ALGO.md": "algo_cli/resources/docs/ALGO.md"})
    target = installed / "resources/docs/ALGO.md"
    target.parent.mkdir(parents=True)
    if state != "missing":
        target.write_text("current catalog\n" if state == "current" else "old catalog\n", encoding="utf-8")

    report = SCRIPT.check_installed_source_parity(source_root=source, installed_root=installed)

    assert report.passed is (state == "current")
    assert report.source_files == 4
    assert report.missing == (("resources/docs/ALGO.md",) if state == "missing" else ())
    assert report.divergent == (("resources/docs/ALGO.md",) if state == "stale" else ())


def test_force_included_skill_directory_changes_the_digest(tmp_path) -> None:
    source, installed = _trees(tmp_path)
    skills = source.parent / "skills/example"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("original\n", encoding="utf-8")
    target = installed / "resources/skills/example/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("original\n", encoding="utf-8")
    _manifest(source, {"skills": "algo_cli/resources/skills"})
    before = SCRIPT.check_installed_source_parity(source_root=source, installed_root=installed)
    assert before.passed

    (skills / "SKILL.md").write_text("changed\n", encoding="utf-8")
    after = SCRIPT.check_installed_source_parity(source_root=source, installed_root=installed)
    assert not after.passed
    assert after.divergent == ("resources/skills/example/SKILL.md",)
    assert before.source_digest != after.source_digest
    assert before.installed_digest == after.installed_digest


@pytest.mark.parametrize(
    "origin,target",
    [
        ("../outside.md", "algo_cli/resources/doc.md"),
        ("/outside.md", "algo_cli/resources/doc.md"),
        ("C:/outside.md", "algo_cli/resources/doc.md"),
        ("docs/doc.md", "algo_cli/../outside.md"),
        ("docs/doc.md", "/algo_cli/resources/doc.md"),
        ("docs/doc.md", "C:/algo_cli/resources/doc.md"),
        ("docs/doc.md", True),
        ("docs/doc.md", "algo_cli/__init__.py"),
    ],
)
def test_invalid_force_include_configuration_is_rejected(tmp_path, origin, target) -> None:
    source, installed = _trees(tmp_path)
    (source.parent / "docs").mkdir()
    (source.parent / "docs/doc.md").write_text("document\n", encoding="utf-8")
    _manifest(source, {origin: target})
    with pytest.raises(SCRIPT.InstalledSourceParityError, match="wheel_"):
        SCRIPT.check_installed_source_parity(source_root=source, installed_root=installed)


@pytest.mark.parametrize(
    "state", ["missing", "linked-file", "linked-directory", "linked-manifest", "malformed-manifest", "missing-manifest"]
)
def test_unavailable_or_linked_force_include_sources_reject(tmp_path, state) -> None:
    source, installed = _trees(tmp_path)
    docs = source.parent / "docs"
    docs.mkdir()
    target = docs / "doc.md"
    _manifest(source, {"docs/doc.md": "algo_cli/resources/doc.md"})
    if state == "linked-file":
        target.symlink_to(source / "__init__.py")
    elif state == "linked-directory":
        docs.rmdir()
        docs.symlink_to(source, target_is_directory=True)
    elif state == "linked-manifest":
        manifest = source.parent / "pyproject.toml"
        content = manifest.read_text(encoding="utf-8")
        manifest.unlink()
        outside = tmp_path / "manifest.toml"
        outside.write_text(content, encoding="utf-8")
        manifest.symlink_to(outside)
    elif state == "malformed-manifest":
        (source.parent / "pyproject.toml").write_text("[[broken", encoding="utf-8")
    elif state == "missing-manifest":
        (source.parent / "pyproject.toml").unlink()
    with pytest.raises(SCRIPT.InstalledSourceParityError, match="wheel_"):
        SCRIPT.check_installed_source_parity(source_root=source, installed_root=installed)


def test_exact_source_subset_and_generated_data_pass(tmp_path) -> None:
    source, installed = _trees(tmp_path)
    (installed / "resources" / "docs").mkdir(parents=True)
    (installed / "resources" / "docs" / "generated.md").write_text("generated\n", encoding="utf-8")

    report = SCRIPT.check_installed_source_parity(
        source_root=source,
        installed_root=installed,
    )

    assert report.passed is True
    assert report.source_digest == report.installed_digest
    assert report.missing == ()
    assert report.divergent == ()
    assert report.unexpected_python == ()


def test_missing_divergent_and_stale_python_files_fail(tmp_path) -> None:
    source, installed = _trees(tmp_path)
    (installed / "nested" / "module.py").write_text("VALUE = 999\n", encoding="utf-8")
    (installed / "resource.json").unlink()
    (installed / "stale.py").write_text("STALE = True\n", encoding="utf-8")

    report = SCRIPT.check_installed_source_parity(
        source_root=source,
        installed_root=installed,
    )

    assert report.passed is False
    assert report.missing == ("resource.json",)
    assert report.divergent == ("nested/module.py",)
    assert report.unexpected_python == ("stale.py",)
    assert report.source_digest != report.installed_digest


def test_source_shadowing_and_symlinked_files_reject(tmp_path) -> None:
    source, installed = _trees(tmp_path)
    with pytest.raises(SCRIPT.InstalledSourceParityError, match="source_shadowed"):
        SCRIPT.check_installed_source_parity(
            source_root=source,
            installed_root=source,
        )

    target = tmp_path / "outside.py"
    target.write_text("outside\n", encoding="utf-8")
    (installed / "nested" / "module.py").unlink()
    (installed / "nested" / "module.py").symlink_to(target)
    with pytest.raises(SCRIPT.InstalledSourceParityError, match="package_file"):
        SCRIPT.check_installed_source_parity(
            source_root=source,
            installed_root=installed,
        )


def test_symlinked_roots_and_directories_reject(tmp_path) -> None:
    source, installed = _trees(tmp_path)
    source_link = tmp_path / "source-link"
    source_link.symlink_to(source, target_is_directory=True)
    with pytest.raises(SCRIPT.InstalledSourceParityError, match="package_root"):
        SCRIPT.check_installed_source_parity(
            source_root=source_link,
            installed_root=installed,
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stale.py").write_text("STALE = True\n", encoding="utf-8")
    (installed / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SCRIPT.InstalledSourceParityError, match="package_file"):
        SCRIPT.check_installed_source_parity(
            source_root=source,
            installed_root=installed,
        )


def test_cli_output_is_content_free_on_shadowing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(SCRIPT, "_installed_package", lambda: SCRIPT.SOURCE_PACKAGE)
    assert SCRIPT.main([]) == 1
    value = json.loads(capsys.readouterr().out)
    assert value == {
        "passed": False,
        "reason_code": "source_shadowed",
        "schema_version": 1,
    }
    assert str(Path.home()) not in json.dumps(value)
