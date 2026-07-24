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
    return source, installed


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
