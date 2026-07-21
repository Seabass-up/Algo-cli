from __future__ import annotations

import importlib.util
from pathlib import Path
import plistlib
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "scripts" / "austin_native_package_audit.py"
AUDIT_SPEC = importlib.util.spec_from_file_location("austin_native_package_audit", AUDIT_PATH)
assert AUDIT_SPEC is not None and AUDIT_SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(AUDIT_SPEC)
sys.modules[AUDIT_SPEC.name] = AUDIT
AUDIT_SPEC.loader.exec_module(AUDIT)

APP_GROUP = AUDIT.APP_GROUP
RESOURCES = AUDIT.RESOURCES
SERVICE_NAME = AUDIT.SERVICE_NAME
AuditError = AUDIT.AuditError
audit_resources = AUDIT.audit_resources
audit_sources = AUDIT.audit_sources
audit_neon_allowed_origin = AUDIT._audit_neon_allowed_origin
audit_capture_xpc_boundary = AUDIT._audit_capture_xpc_boundary


def test_austin_native_resources_and_sources_pass_fail_closed_audit() -> None:
    assert audit_resources() == [
        "entitlements",
        "credential_migrator_entitlements",
        "neon_host_entitlements",
        "launch_agent",
        "info_plist",
    ]
    checks = audit_sources()
    assert "no_network_apis" in checks
    assert "no_subprocess_api" in checks
    assert "no_dynamic_script" in checks
    assert "no_input_monitoring" in checks
    assert "two_event_post_bound" in checks
    assert "single_frame_screencapturekit" in checks
    assert "target_bound_picker_filter" in checks
    assert "bounded_redaction_work" in checks
    assert "post_capture_redaction" in checks
    assert "local_vision_redaction_candidate" in checks
    assert "review_only_shortcut" in checks
    assert "encrypted_capture_artifact" in checks
    assert "crash_recovered_capture_artifact" in checks
    assert "process_killed_capture_recovery" in checks
    assert "sealed_capture_consumer" in checks
    assert "content_free_xpc_capture" in checks
    assert "fresh_native_confirmation" in checks
    assert "signed_native_preparation" in checks
    assert "bounded_replay_retention" in checks
    assert "debug_crash_injection" in checks
    assert "target_bound_python_consumer" in checks
    assert "sealed_control_activation" in checks

    crash_tests = (
        ROOT
        / "native"
        / "austin"
        / "Tests"
        / "AustinCoreTests"
        / "AustinAliceCaptureArtifactTests.swift"
    ).read_text(encoding="utf-8")
    assert "[.atomic, .withoutOverwriting]" not in crash_tests
    for marker in (
        "austinAliceWriteExclusiveAtomicMarker",
        "O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC",
        "Darwin.renameatx_np(",
        "UInt32(RENAME_EXCL)",
        "Darwin.fsync(directoryDescriptor)",
    ):
        assert marker in crash_tests

    info = plistlib.loads((RESOURCES / "AustinApp-Info.plist").read_bytes())
    assert info["NSScreenCaptureUsageDescription"]


def test_relay_is_os_sandboxed_without_network_or_temporary_entitlements() -> None:
    relay = plistlib.loads((RESOURCES / "AustinRelay.entitlements").read_bytes())
    assert relay == {
        "com.apple.security.app-sandbox": True,
        "com.apple.security.application-groups": [APP_GROUP],
    }
    assert not any("network" in key for key in relay)
    assert not any("temporary-exception" in key for key in relay)


def test_tcc_adapter_has_no_network_entitlement_and_launches_as_user_agent() -> None:
    adapter = plistlib.loads((RESOURCES / "AustinTCCAdapter.entitlements").read_bytes())
    agent = plistlib.loads((RESOURCES / "AustinLaunchAgent.plist").read_bytes())
    assert not any("network" in key for key in adapter)
    assert agent["Label"] == SERVICE_NAME
    assert agent["MachServices"] == {SERVICE_NAME: True}
    assert "Sockets" not in agent
    assert "UserName" not in agent
    assert "GroupName" not in agent


def test_adhoc_probe_entitlements_are_empty_and_never_a_production_claim() -> None:
    probe = plistlib.loads((RESOURCES / "AustinRelayProbe.entitlements").read_bytes())
    assert probe == {}


def test_neon_host_has_no_entitlements_and_origin_resource_is_exact(
    tmp_path: Path,
) -> None:
    entitlements = plistlib.loads((RESOURCES / "NeonNativeHost.entitlements").read_bytes())
    assert entitlements == {}
    migration_entitlements = plistlib.loads((RESOURCES / "AustinCredentialMigrator.entitlements").read_bytes())
    assert migration_entitlements == {}
    origin = tmp_path / "NeonAllowedOrigin.txt"
    origin.write_text(
        "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
        encoding="utf-8",
    )
    origin.chmod(0o444)
    assert audit_neon_allowed_origin(origin).endswith("/")

    origin.chmod(0o644)
    with pytest.raises(AuditError, match="neon_origin_file"):
        audit_neon_allowed_origin(origin)

    origin.write_text("https://example.com", encoding="utf-8")
    origin.chmod(0o444)
    with pytest.raises(AuditError, match="neon_origin_value"):
        audit_neon_allowed_origin(origin)

    origin.chmod(0o666)
    with pytest.raises(AuditError, match="neon_origin_file"):
        audit_neon_allowed_origin(origin)


def test_source_audit_rejects_network_and_subprocess_primitives(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "Sources" / "AustinCore"
    source.mkdir(parents=True)
    monkeypatch.setattr(AUDIT, "AUSTIN", tmp_path)
    (source / "AustinBad.swift").write_text("import Network\nlet c = NWConnection()\n", encoding="utf-8")
    with pytest.raises(AuditError, match="source_network_framework"):
        audit_sources()
    (source / "AustinBad.swift").write_text("import Foundation\nlet task = Process()\n", encoding="utf-8")
    with pytest.raises(AuditError, match="source_subprocess"):
        audit_sources()


def _capture_boundary_sources() -> dict[str, str]:
    source = ROOT / "native" / "austin" / "Sources"
    return {
        "screen_capture": (source / "AustinTCCAdapter" / "AustinScreenCapture.swift").read_text(),
        "capture_artifact": (source / "AustinTCCAdapter" / "AustinAliceCaptureArtifact.swift").read_text(),
        "capture_boundary": (source / "AustinTCCAdapter" / "AustinIsaacCaptureBoundary.swift").read_text(),
        "capture_redaction": (source / "AustinTCCAdapter" / "AustinIsaacCaptureRedaction.swift").read_text(),
        "coordinator": (source / "AustinTCCAdapter" / "AustinThomasBindingCoordinator.swift").read_text(),
        "xpc_protocol": (source / "AustinCore" / "AustinXPCProtocol.swift").read_text(),
        "relay": (source / "AustinRelay" / "AustinRelay.swift").read_text(),
        "adapter_main": (source / "AustinTCCAdapterMain" / "AustinTCCAdapter.swift").read_text(),
    }


@pytest.mark.parametrize(
    ("field", "injection", "reason"),
    [
        (
            "xpc_protocol",
            "\nfunc captureArtifact(_ grant: AustinAliceCaptureConsumerGrant) {}\n",
            "xpc_protocol_surface",
        ),
        (
            "relay",
            "\nlet leaked = frame.rgbaBytes\n",
            "capture_xpc_exposure:relay",
        ),
        (
            "adapter_main",
            "\nlet grants = sink.takeConsumerGrants()\n",
            "capture_xpc_exposure:adapter",
        ),
        (
            "capture_artifact",
            "\npublic func consumeRedacted(_ value: Data) {}\n",
            "capture_capability_visibility",
        ),
        (
            "capture_redaction",
            "\nlet ocr = VNRecognizeTextRequest()\n",
            "capture_redaction_candidate",
        ),
    ],
)
def test_capture_boundary_rejects_capability_or_pixel_xpc_exposure(
    field: str,
    injection: str,
    reason: str,
) -> None:
    values = _capture_boundary_sources()
    values[field] += injection
    with pytest.raises(AuditError, match=re.escape(reason)):
        audit_capture_xpc_boundary(**values)


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("let script = NSAppleScript(source: input)", "source_dynamic_script"),
        ("CGRequestScreenCaptureAccess()", "source_silent_capture_prompt"),
        ("CGRequestPostEventAccess()", "source_silent_event_prompt"),
        ("CGEvent.tapCreate(tap: value)", "source_input_monitoring"),
        ("event.keyboardSetUnicodeString(stringLength: 1, unicodeString: p)", "source_keyboard_injection"),
        ("let pasteboard = NSPasteboard.general", "source_clipboard"),
        ("let locked = CGSSessionScreenIsLocked()", "source_private_session_lock"),
        ("let token = OPENAI_API_KEY", "source_model_credential"),
    ],
)
def test_source_audit_rejects_privilege_expansion_primitives(
    tmp_path: Path,
    monkeypatch,
    source: str,
    reason: str,
) -> None:
    source_root = tmp_path / "Sources" / "AustinCore"
    source_root.mkdir(parents=True)
    adapter_root = tmp_path / "Sources" / "AustinTCCAdapter"
    adapter_root.mkdir(parents=True)
    (adapter_root / "AustinCGEvent.swift").write_text(
        "down.post(tap: .cghidEventTap)\nup.post(tap: .cghidEventTap)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(AUDIT, "AUSTIN", tmp_path)
    (source_root / "AustinBad.swift").write_text(source, encoding="utf-8")
    with pytest.raises(AuditError, match=reason):
        audit_sources()


def test_austin_audit_cli_emits_structural_json() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/austin_native_package_audit.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert '"status": "passed"' in completed.stdout


def test_debug_staging_uses_canonical_32_byte_authority_key_decoder() -> None:
    script = (ROOT / "script" / "austin_build_and_run.sh").read_text(encoding="utf-8")
    assert '"${TEST_PUBLIC_KEY}="' in script
    assert "tr '_-' '/+' | base64 -D" in script
    assert "staged_authority_key_size" in script
    assert 'install -m 0755 "$bin_path/neon-native-host"' in script
    assert 'install -m 0755 "$bin_path/austin-credential-migrator"' in script
    assert "NeonAllowedOrigin.txt" in script
    assert '"com.algo-cli.neon.host"' in script
    assert "neon-probe)" in script
    assert "migration-probe)" in script
    assert "local-test)" in script
    assert "run_local_test" in script
    assert "persistent_runtime_writes" in script
    assert (
        "for probe_command in probe neon-probe migration-probe readiness-probe audit"
        in script
    )
    assert "readiness-probe)" in script
    assert '"com.algo-cli.austin.readiness-probe"' in script
    assert 'SCRIPT="$ROOT/script/austin_build_and_run.sh"' in script
    assert 'AUSTIN_BUILD_LOCK_HELD=1 "$SCRIPT" "$probe_command"' in script
    assert "developer_id_application_required" in script
    assert "release_version_required" in script
    assert "release_build_required" in script
    assert "plutil -replace CFBundleShortVersionString" in script
    assert '/usr/bin/lockf -k -t 120 "$BUILD_LOCK"' in script
    assert "AUSTIN_BUILD_LOCK_HELD=1" in script
