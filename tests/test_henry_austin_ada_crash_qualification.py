from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import sys

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Ada crash qualification is a macOS process and filesystem boundary",
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "henry_austin_ada_crash_qualification.py"
SPEC = importlib.util.spec_from_file_location("henry_austin_ada_crash_qualification", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


def test_ada_crash_evidence_output_is_bounded_and_female_named() -> None:
    expected = (ROOT / "hardening" / "ada-native-crash-qualification.json").resolve()
    assert SCRIPT._bounded_output(Path("hardening/ada-native-crash-qualification.json")) == expected
    with pytest.raises(SCRIPT.CrashQualificationError, match="crash_probe_output_scope"):
        SCRIPT._bounded_output(Path("ada-native-crash-qualification.json"))
    with pytest.raises(SCRIPT.CrashQualificationError, match="crash_probe_output_scope"):
        SCRIPT._bounded_output(Path("hardening/henry-native-crash-qualification.json"))
    with pytest.raises(SCRIPT.CrashQualificationError, match="crash_probe_output_scope"):
        SCRIPT._bounded_output(Path("hardening/ada-native-crash-qualification.txt"))


def test_ada_crash_hook_is_debug_only_and_process_fatal() -> None:
    source = (
        ROOT / "native" / "austin" / "Sources" / "AustinCore" / "AustinAdaPermitStore.swift"
    ).read_text(encoding="utf-8")
    marker = 'ProcessInfo.processInfo.environment["ALGO_AUSTIN_ADA_CRASH_CHECKPOINT"]'
    assert source.count(marker) == 1
    marker_offset = source.index(marker)
    assert source.rfind("#if DEBUG", 0, marker_offset) >= 0
    assert source.index("SIGKILL", marker_offset) > marker_offset
    assert source.index("#endif", marker_offset) > source.index("SIGKILL", marker_offset)


def test_ada_crash_evidence_write_is_private_atomic_and_rejects_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir(mode=0o700)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "HARDENING", hardening)
    output = hardening / "ada-crash-evidence.json"
    SCRIPT._atomic_private_write(output, b"{}\n")
    assert output.read_bytes() == b"{}\n"
    assert output.stat().st_mode & 0o777 == 0o600

    target = hardening / "ada-target.json"
    target.write_bytes(b"unchanged\n")
    target.chmod(0o600)
    output.unlink()
    os.symlink(target, output)
    with pytest.raises(SCRIPT.CrashQualificationError, match="crash_probe_output_identity"):
        SCRIPT._atomic_private_write(output, b"changed\n")
    assert target.read_bytes() == b"unchanged\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="Austin is a macOS native package")
def test_ada_replay_store_survives_every_process_kill_checkpoint() -> None:
    report = SCRIPT.qualify(trials=1)
    assert report["status"] == "passed"
    assert report["schema_version"] == 1
    assert report["checkpoints"] == list(SCRIPT.CHECKPOINTS)
    assert report["namespaces"] == ["permit", "preparation"]
    assert report["trials_per_checkpoint"] == 1
    assert report["process_kills"] == 10
    assert report["precommit_rollbacks"] == 8
    assert report["postcommit_durable"] == 2
    assert report["replay_rejections"] == 10
    assert report["release_hook_absent"] is True
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", report["debug_binary_digest"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", report["release_binary_digest"])
