from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import sys

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Alice crash qualification is a macOS process and filesystem boundary",
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "henry_austin_alice_crash_qualification.py"
SPEC = importlib.util.spec_from_file_location("henry_austin_alice_crash_qualification", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


def test_alice_crash_evidence_output_is_bounded_and_female_named() -> None:
    expected = (ROOT / "hardening" / "alice-native-capture-crash-qualification.json").resolve()
    assert SCRIPT._bounded_output(Path("hardening/alice-native-capture-crash-qualification.json")) == expected
    for invalid in (
        Path("alice-native-capture-crash-qualification.json"),
        Path("hardening/henry-native-capture-crash-qualification.json"),
        Path("hardening/alice-native-capture-crash-qualification.txt"),
    ):
        with pytest.raises(SCRIPT.AliceCrashQualificationError, match="alice_crash_output_scope"):
            SCRIPT._bounded_output(invalid)


def test_alice_crash_evidence_write_is_private_atomic_and_rejects_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir(mode=0o700)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "HARDENING", hardening)
    output = hardening / "alice-crash-evidence.json"
    SCRIPT._atomic_private_write(output, b"{}\n")
    assert output.read_bytes() == b"{}\n"
    assert output.stat().st_mode & 0o777 == 0o600

    output.chmod(0o644)
    SCRIPT._atomic_private_write(output, b'{"refreshed":true}\n')
    assert output.read_bytes() == b'{"refreshed":true}\n'
    assert output.stat().st_mode & 0o777 == 0o600

    output.chmod(0o664)
    with pytest.raises(SCRIPT.AliceCrashQualificationError, match="alice_crash_output_identity"):
        SCRIPT._atomic_private_write(output, b"changed\n")
    assert output.read_bytes() == b'{"refreshed":true}\n'

    target = hardening / "alice-target.json"
    target.write_bytes(b"unchanged\n")
    target.chmod(0o600)
    output.unlink()
    os.symlink(target, output)
    with pytest.raises(SCRIPT.AliceCrashQualificationError, match="alice_crash_output_identity"):
        SCRIPT._atomic_private_write(output, b"changed\n")
    assert target.read_bytes() == b"unchanged\n"


def test_alice_crash_report_rejects_counter_overclaim() -> None:
    report = {
        "fixture_digest": "sha256:" + "1" * 64,
        "generated_at": "2026-07-20T00:00:00Z",
        "limitations": SCRIPT.LIMITATIONS,
        "orphans_recovered_after_restart": 1,
        "process_kills": 1,
        "published_before_kill": 1,
        "schema_version": 1,
        "source_digest": "sha256:" + "2" * 64,
        "status": "passed",
        "trials": 1,
    }
    SCRIPT._validate_report(report)
    report["process_kills"] = 2
    with pytest.raises(SCRIPT.AliceCrashQualificationError, match="alice_crash_report"):
        SCRIPT._validate_report(report)


def test_alice_crash_marker_reader_is_descriptor_bound_and_rejects_unsafe_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "alice.ready"
    marker.write_text("ready", encoding="ascii")
    marker.chmod(0o600)
    assert SCRIPT._read_marker(marker) == "ready"

    real_read = os.read

    def short_read(descriptor: int, size: int) -> bytes:
        return real_read(descriptor, min(size, 1))

    monkeypatch.setattr(SCRIPT.os, "read", short_read)
    assert SCRIPT._read_marker(marker) == "ready"
    monkeypatch.setattr(SCRIPT.os, "read", real_read)

    marker.write_bytes(b"")
    with pytest.raises(SCRIPT.AliceCrashQualificationError, match="alice_crash_marker"):
        SCRIPT._read_marker(marker)

    target = tmp_path / "alice-target.ready"
    target.write_text("unsafe", encoding="ascii")
    marker.unlink()
    os.symlink(target, marker)
    with pytest.raises(SCRIPT.AliceCrashQualificationError, match="alice_crash_marker"):
        SCRIPT._read_marker(marker)


def test_checked_in_alice_crash_evidence_is_current_and_source_bound() -> None:
    artifact = ROOT / "hardening" / "alice-native-capture-crash-qualification.json"
    report = json.loads(artifact.read_text(encoding="utf-8"))
    SCRIPT._validate_report(report)
    assert report["trials"] == 10
    assert report["source_digest"] == SCRIPT._digest_paths(SCRIPT.SOURCE_PATHS)


@pytest.mark.skipif(sys.platform != "darwin", reason="Austin is a macOS native package")
def test_alice_capture_artifact_recovers_after_actual_process_kill() -> None:
    report = SCRIPT.qualify(trials=1)
    assert report["status"] == "passed"
    assert report["trials"] == 1
    assert report["process_kills"] == 1
    assert report["published_before_kill"] == 1
    assert report["orphans_recovered_after_restart"] == 1
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", report["fixture_digest"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", report["source_digest"])
