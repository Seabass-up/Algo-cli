from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from algo_cli.ada_supply_chain_manifest import (
    SupplyChainManifestError,
    normalize_sbom,
    write_checksums,
)


ROOT = Path(__file__).resolve().parents[1]


def _sbom() -> dict:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:00000000-0000-4000-8000-000000000001",
        "version": 1,
        "metadata": {
            "timestamp": "2026-07-19T00:00:00Z",
            "tools": [{"name": "uv", "version": "0.11.26"}],
            "component": {
                "type": "library",
                "bom-ref": "algo-cli-runtime-1",
                "name": "algo-cli-runtime",
            },
        },
        "components": [
            {
                "type": "library",
                "bom-ref": "cryptography@49.0.0",
                "name": "cryptography",
                "version": "49.0.0",
                "purl": "pkg:pypi/cryptography@49.0.0",
            }
        ],
        "dependencies": [
            {"ref": "algo-cli-runtime-1", "dependsOn": ["cryptography@49.0.0"]},
            {"ref": "cryptography@49.0.0"},
        ],
    }


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_sbom_normalization_is_deterministic_and_removes_wall_clock_identity(tmp_path: Path) -> None:
    source = tmp_path / "raw.json"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    value = _sbom()
    _write(source, value)

    first_result = normalize_sbom(source, first)
    value["metadata"]["timestamp"] = "2099-01-01T00:00:00Z"
    value["serialNumber"] = "urn:uuid:00000000-0000-4000-8000-000000000099"
    _write(source, value)
    second_result = normalize_sbom(source, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_result == second_result
    normalized = json.loads(first.read_text(encoding="utf-8"))
    assert "timestamp" not in normalized["metadata"]
    assert normalized["serialNumber"].startswith("urn:uuid:")
    assert normalized["metadata"]["component"]["type"] == "application"
    assert normalized["metadata"]["component"]["version"] == "0.18.0"
    assert {
        (item["name"], item["value"])
        for item in normalized["metadata"]["properties"]
    } == {
        ("algo-cli:runtime-dependencies-embedded", "false"),
        ("algo-cli:sbom-scope", "locked-runtime-resolution"),
    }
    assert first_result == {
        "component_count": 1,
        "digest": "sha256:" + hashlib.sha256(first.read_bytes()).hexdigest(),
        "spec_version": "1.5",
        "status": "passed",
    }


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value.update({"bomFormat": "SPDX"}), "sbom_format"),
        (lambda value: value["metadata"].update({"component": {"name": "other"}}), "sbom_root"),
        (
            lambda value: value["dependencies"][0]["dependsOn"].append("missing@1"),
            "sbom_dependency",
        ),
        (
            lambda value: value["components"][0].update(
                {"path": "/" + "/".join(("Users", "private", "project"))}
            ),
            "private_path",
        ),
    ],
)
def test_sbom_schema_identity_and_privacy_fail_closed(tmp_path: Path, mutation, reason: str) -> None:
    source = tmp_path / "raw.json"
    value = _sbom()
    mutation(value)
    _write(source, value)
    with pytest.raises(SupplyChainManifestError, match=reason):
        normalize_sbom(source, tmp_path / "normalized.json")


def test_duplicate_json_keys_and_source_symlinks_reject(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"bomFormat":"CycloneDX","bomFormat":"CycloneDX"}', encoding="utf-8")
    with pytest.raises(SupplyChainManifestError, match="json_duplicate_key"):
        normalize_sbom(duplicate, tmp_path / "output.json")

    source = tmp_path / "raw.json"
    _write(source, _sbom())
    linked = tmp_path / "linked.json"
    linked.symlink_to(source)
    with pytest.raises(SupplyChainManifestError, match="file_type"):
        normalize_sbom(linked, tmp_path / "output.json")


def test_checksum_manifest_is_sorted_content_bound_and_excludes_itself(tmp_path: Path) -> None:
    wheel = tmp_path / "package.whl"
    source = tmp_path / "package.tar.gz"
    sbom = tmp_path / "package.cdx.json"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")
    sbom.write_bytes(b"sbom")
    output = tmp_path / "SHA256SUMS"

    result = write_checksums((source, wheel, sbom), output)

    names = [line.split("  ", 1)[1] for line in output.read_text(encoding="ascii").splitlines()]
    assert names == sorted(names)
    assert "SHA256SUMS" not in names
    assert result["artifact_count"] == 3
    for line in output.read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ", 1)
        assert digest == hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()


def test_checksum_manifest_rejects_cross_directory_symlink_and_hardlink(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"artifact")
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SupplyChainManifestError, match="artifact_path"):
        write_checksums((artifact,), other / "SHA256SUMS")

    linked = tmp_path / "linked.whl"
    linked.symlink_to(artifact)
    with pytest.raises(SupplyChainManifestError, match="file_type"):
        write_checksums((linked,), tmp_path / "SHA256SUMS")

    hard = tmp_path / "hard.whl"
    os.link(artifact, hard)
    with pytest.raises(SupplyChainManifestError, match="file_hardlink"):
        write_checksums((hard,), tmp_path / "SHA256SUMS")


def test_manifest_cli_emits_only_structural_evidence(tmp_path: Path) -> None:
    source = tmp_path / "raw.json"
    output = tmp_path / "normalized.json"
    _write(source, _sbom())
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/ada_supply_chain_manifest.py",
            "sbom",
            "--source",
            str(source),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["status"] == "passed"
    assert str(tmp_path) not in completed.stdout
