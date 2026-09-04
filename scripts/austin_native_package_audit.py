#!/usr/bin/env python3
"""Fail-closed static and staged-binary audit for the Austin native boundary."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUSTIN = ROOT / "native" / "austin"
RESOURCES = AUSTIN / "Resources"
SERVICE_NAME = "group.com.algo-cli.control.austin.tcc-adapter"
APP_GROUP = "group.com.algo-cli.control"
NEON_EXTENSION_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")

NETWORK_SOURCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("network_framework", re.compile(r"^\s*import\s+Network\b", re.MULTILINE)),
    ("url_session", re.compile(r"\bURLSession\b")),
    ("network_connection", re.compile(r"\bNW(Connection|Listener|Browser|PathMonitor)\b")),
    ("bsd_socket", re.compile(r"\b(socket|connect|listen|accept|getaddrinfo)\s*\(")),
    ("socket_stream", re.compile(r"\bCFStreamCreatePairWithSocket\b")),
)

CONTROL_SOURCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dynamic_script", re.compile(r"\b(NSAppleScript|NSUserAppleScriptTask|OSAScript)\b")),
    ("silent_ax_prompt", re.compile(r"\bAXIsProcessTrustedWithOptions\b")),
    ("silent_capture_prompt", re.compile(r"\bCGRequestScreenCaptureAccess\b")),
    ("silent_event_prompt", re.compile(r"\bCGRequestPostEventAccess\b")),
    ("input_monitoring", re.compile(r"\b(CGEventTapCreate|tapCreate)\s*\(")),
    (
        "keyboard_injection",
        re.compile(r"\b(keyboardEventSource|keyboardSetUnicodeString)\b|\.key(?:Down|Up)\b"),
    ),
    ("clipboard", re.compile(r"\bNSPasteboard\b")),
    (
        "private_session_lock",
        re.compile(
            r"CGSSessionScreenIsLocked|kCGSession(?:OnConsoleKey|LoginDoneKey)|"
            r"com\.apple\.screenIsLocked"
        ),
    ),
    (
        "model_credential",
        re.compile(r"\b(?:OPENAI|ANTHROPIC|XAI|GOOGLE|OLLAMA)_[A-Z0-9_]*(?:KEY|TOKEN|SECRET)\b"),
    ),
)

FORBIDDEN_UNDEFINED_SYMBOLS = (
    "_accept",
    "_connect",
    "_getaddrinfo",
    "_listen",
    "_socket",
)


class AuditError(RuntimeError):
    """A content-free native package audit failure."""


def _plist(path: Path) -> dict[str, Any]:
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise AuditError(f"invalid_plist:{path.name}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"invalid_plist_root:{path.name}")
    return value


def audit_resources() -> list[str]:
    relay = _plist(RESOURCES / "AustinRelay.entitlements")
    relay_probe = _plist(RESOURCES / "AustinRelayProbe.entitlements")
    adapter = _plist(RESOURCES / "AustinTCCAdapter.entitlements")
    credential_migrator = _plist(RESOURCES / "AustinCredentialMigrator.entitlements")
    neon_host = _plist(RESOURCES / "NeonNativeHost.entitlements")
    app = _plist(RESOURCES / "AustinApp.entitlements")
    launch_agent = _plist(RESOURCES / "AustinLaunchAgent.plist")
    info = _plist(RESOURCES / "AustinApp-Info.plist")

    expected_sandboxed = {
        "com.apple.security.app-sandbox": True,
        "com.apple.security.application-groups": [APP_GROUP],
    }
    if relay != expected_sandboxed:
        raise AuditError("relay_entitlements")
    if app != expected_sandboxed:
        raise AuditError("app_entitlements")
    if adapter != {"com.apple.security.automation.apple-events": True}:
        raise AuditError("adapter_entitlements")
    if credential_migrator != {}:
        raise AuditError("credential_migrator_entitlements")
    if relay_probe != {}:
        raise AuditError("relay_probe_entitlements")
    if neon_host != {}:
        raise AuditError("neon_host_entitlements")
    for entitlements in (relay, adapter, credential_migrator, neon_host, app):
        if any("network" in key.casefold() for key in entitlements):
            raise AuditError("network_entitlement")
        if entitlements.get("com.apple.security.get-task-allow") is not None:
            raise AuditError("debug_entitlement")
        if any("temporary-exception" in key for key in entitlements):
            raise AuditError("temporary_exception")

    if launch_agent.get("Label") != SERVICE_NAME:
        raise AuditError("launch_agent_label")
    if launch_agent.get("MachServices") != {SERVICE_NAME: True}:
        raise AuditError("launch_agent_mach_service")
    if launch_agent.get("ProgramArguments") != ["__AUSTIN_ADAPTER_PATH__"]:
        raise AuditError("launch_agent_program")
    if launch_agent.get("RunAtLoad") is not False:
        raise AuditError("launch_agent_run_at_load")
    if "Sockets" in launch_agent or "KeepAlive" in launch_agent:
        raise AuditError("launch_agent_activation")
    if launch_agent.get("UserName") is not None or launch_agent.get("GroupName") is not None:
        raise AuditError("launch_agent_privilege")
    if launch_agent.get("StandardOutPath") != "/dev/null":
        raise AuditError("launch_agent_stdout")
    if launch_agent.get("StandardErrorPath") != "/dev/null":
        raise AuditError("launch_agent_stderr")

    if info.get("CFBundleIdentifier") != "com.algo-cli.austin.control":
        raise AuditError("app_bundle_identifier")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(info.get("CFBundleShortVersionString", ""))) is None:
        raise AuditError("app_bundle_version")
    if re.fullmatch(r"[1-9][0-9]{0,8}", str(info.get("CFBundleVersion", ""))) is None:
        raise AuditError("app_build_version")
    if info.get("LSUIElement") is not True:
        raise AuditError("app_ui_element")
    if not info.get("NSAppleEventsUsageDescription"):
        raise AuditError("apple_events_usage")
    if not info.get("NSScreenCaptureUsageDescription"):
        raise AuditError("screen_capture_usage")
    return [
        "entitlements",
        "credential_migrator_entitlements",
        "neon_host_entitlements",
        "launch_agent",
        "info_plist",
    ]


def _audit_capture_xpc_boundary(
    *,
    screen_capture: str,
    capture_artifact: str,
    capture_boundary: str,
    capture_redaction: str,
    coordinator: str,
    xpc_protocol: str,
    relay: str,
    adapter_main: str,
) -> None:
    required_boundary = (
        "protocol AustinIsaacRedactedFrameConsuming",
        "final class AustinIsaacSealedCapturePipeline",
        "try artifactSink.acceptRedacted(frame)",
        "let grants = try artifactSink.takeConsumerGrants()",
        "var recovered = try artifactSink.consumeRedacted(grant)",
        "defer { recovered.clear() }",
        "try consumer.consumeRedacted(&recovered)",
        "terminalFailure = true",
        "try? artifactSink.revokeAll()",
    )
    if any(marker not in capture_boundary for marker in required_boundary):
        raise AuditError("sealed_capture_consumer_boundary")
    if any(
        marker in capture_boundary
        for marker in (
            "public protocol AustinIsaacRedactedFrameConsuming",
            "public final class AustinIsaacSealedCapturePipeline",
        )
    ):
        raise AuditError("capture_consumer_visibility")
    if any(
        marker in capture_artifact
        for marker in (
            "public struct AustinAliceCaptureConsumerGrant",
            "public func takeConsumerGrants(",
            "public func consumeRedacted(",
        )
    ):
        raise AuditError("capture_capability_visibility")

    required_redaction = (
        "final class AustinIsaacVisionRedactionCandidate",
        "VNDetectTextRectanglesRequest()",
        "VNDetectFaceRectanglesRequest()",
        "text.reportCharacterBoxes = false",
        "CGDataProvider(",
        "mutable.initializeMemory(as: UInt8.self, repeating: 0, count: count)",
        "context.dataClass == .private",
        "detected.count <= Self.maximumDetectedRegions",
        "regions.count <= Self.maximumOutputRegions",
        "return [try fullFrame(frame)]",
    )
    if any(marker not in capture_redaction for marker in required_redaction) or any(
        marker in capture_redaction
        for marker in (
            "VNRecognizeTextRequest",
            "topCandidates(",
            ".string",
        )
    ):
        raise AuditError("capture_redaction_candidate")

    execute_start = screen_capture.find("    public func execute(")
    execute_end = screen_capture.find("\n    private func issue(", execute_start)
    execute = screen_capture[execute_start:execute_end]
    capture_index = execute.find("backend.capture(")
    classify_index = execute.find("classifier.redactions(for: frame")
    redact_index = execute.find("frame.redact(redactions)")
    sink_index = execute.find("sink.acceptRedacted(frame)")
    if (
        execute_start < 0
        or execute_end < 0
        or min(capture_index, classify_index, redact_index, sink_index) < 0
        or not capture_index < classify_index < redact_index < sink_index
        or "captureRedactionClassifier.preflight(for: preparation)" not in coordinator
    ):
        raise AuditError("post_capture_redaction_order")

    methods = re.findall(r"^\s*func\s+([A-Za-z][A-Za-z0-9]*)\(", xpc_protocol, re.MULTILINE)
    if methods != ["beginSession", "readiness", "prepare", "execute"]:
        raise AuditError("xpc_protocol_surface")
    forbidden_xpc_capture = (
        "AustinAliceCaptureConsumerGrant",
        "AustinAliceCaptureArtifactReceipt",
        "AustinIsaacSealedCapturePipeline",
        "AustinPixelFrame",
        "takeConsumerGrants",
        "consumeRedacted",
        "rgbaBytes",
    )
    for label, source in (
        ("protocol", xpc_protocol),
        ("relay", relay),
        ("adapter", adapter_main),
    ):
        if any(marker in source for marker in forbidden_xpc_capture):
            raise AuditError(f"capture_xpc_exposure:{label}")


def audit_sources() -> list[str]:
    source_root = AUSTIN / "Sources"
    files = sorted((*source_root.rglob("*.swift"), *source_root.rglob("*.c")))
    if not files:
        raise AuditError("source_missing")
    for path in files:
        text = path.read_text(encoding="utf-8")
        for label, pattern in NETWORK_SOURCE_PATTERNS:
            if pattern.search(text):
                raise AuditError(f"source_{label}:{path.name}")
        for label, pattern in CONTROL_SOURCE_PATTERNS:
            if pattern.search(text):
                raise AuditError(f"source_{label}:{path.name}")
        if re.search(r"\b(Process|NSTask)\s*\(", text):
            raise AuditError(f"source_subprocess:{path.name}")
    package_manifest = (AUSTIN / "Package.swift").read_text(encoding="utf-8")
    if '.linkedFramework("Vision")' not in package_manifest:
        raise AuditError("capture_redaction_framework")
    cg_event = (source_root / "AustinTCCAdapter" / "AustinCGEvent.swift").read_text(encoding="utf-8")
    if cg_event.count(".post(tap:") != 2:
        raise AuditError("cg_event_post_bound")
    screen_capture = (source_root / "AustinTCCAdapter" / "AustinScreenCapture.swift").read_text(encoding="utf-8")
    required_capture_contract = (
        "SCScreenshotManager.captureImage(",
        "configuration.showsCursor = false",
        "configuration.capturesAudio = false",
        "configuration.queueDepth = 1",
        "AustinPixelFrame.maximumBytes",
        "capture_timeout",
        "SCContentSharingPickerObserver",
        "configuration.allowedPickerModes = [.singleWindow, .singleDisplay]",
        "configuration.allowsChangingSelectedContent = false",
        "AustinOneShotCaptureSelection<AustinBoundCaptureFilter>",
        "@available(macOS 15.2, *)",
        "filter.includedWindows.count == 1",
        "filter.includedDisplays.count == 1",
        "validatedFilter(",
        "(mode == .pickerScoped) == (expectedSelection != nil)",
        "expectedSelection: record.pickerSelection",
        "capture_picker_identity_unavailable",
        "!redactions.isEmpty",
        "capture_redaction_work",
        "picker === self.picker",
        "selection.revoke()",
    )
    if any(marker not in screen_capture for marker in required_capture_contract):
        raise AuditError("screen_capturekit_contract")
    shortcut = (source_root / "AustinTCCAdapter" / "AustinShortcut.swift").read_text(encoding="utf-8")
    required_shortcut_contract = (
        'components.scheme = "shortcuts"',
        'components.host = "open-shortcut"',
        'URLQueryItem(name: "name", value: shortcutName)',
        "shortcut_review_handoff",
        "shortcut_open_unknown",
    )
    forbidden_shortcut_contract = (
        'components.host = "run-shortcut"',
        'URLQueryItem(name: "input"',
        'URLQueryItem(name: "text"',
        'URLQueryItem(name: "x-success"',
        'URLQueryItem(name: "x-error"',
        'URLQueryItem(name: "x-cancel"',
        'URL(string: "shortcuts://run-shortcut',
        '"/usr/bin/shortcuts"',
    )
    if any(marker not in shortcut for marker in required_shortcut_contract) or any(
        marker in shortcut for marker in forbidden_shortcut_contract
    ):
        raise AuditError("shortcut_review_only_contract")
    capture_artifact = (source_root / "AustinTCCAdapter" / "AustinAliceCaptureArtifact.swift").read_text(
        encoding="utf-8"
    )
    capture_boundary = (source_root / "AustinTCCAdapter" / "AustinIsaacCaptureBoundary.swift").read_text(
        encoding="utf-8"
    )
    capture_redaction = (source_root / "AustinTCCAdapter" / "AustinIsaacCaptureRedaction.swift").read_text(
        encoding="utf-8"
    )
    required_capture_artifact_contract = (
        "AES.GCM.seal(",
        "AES.GCM.open(",
        "O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC",
        "O_RDONLY | O_NOFOLLOW | O_CLOEXEC",
        "fchmod(descriptor, mode_t(0o600))",
        "fsync(descriptor)",
        "plaintext.resetBytes",
        "maximumTTLMilliseconds",
        "maximumStoredBytes",
        "capture_artifact_time",
        "AustinAliceCaptureConsumerGrant",
        "takeConsumerGrants()",
        "consumeRedacted(",
        "constantTimeEqual(",
        "AustinAliceOSKeyFactory",
        "AustinAliceKeychainMaterialStore",
        "SecItemCopyMatching(",
        "SecItemAdd(",
        "kSecAttrAccessibleWhenUnlockedThisDeviceOnly",
        "kSecAttrSynchronizable as String: false",
        "kSecUseAuthenticationContext",
        "context.interactionNotAllowed = true",
        "recoverOrphanedArtifacts(in:",
        "AT_SYMLINK_NOFOLLOW",
        "LOCK_EX | LOCK_NB",
        "capture_artifact_directory_entry",
        "clearConsumerCapability()",
        "discardFailedConsumptionLocked(",
    )
    if any(marker not in capture_artifact for marker in required_capture_artifact_contract) or any(
        marker in capture_artifact
        for marker in (
            "kSecUseAuthenticationUI",
            "SecTrustedApplicationCreateFromPath",
            "SecAccessCreate",
        )
    ):
        raise AuditError("encrypted_capture_artifact_contract")
    capture_artifact_tests = (AUSTIN / "Tests" / "AustinCoreTests" / "AustinAliceCaptureArtifactTests.swift").read_text(
        encoding="utf-8"
    )
    required_capture_crash_contract = (
        "ALGO_AUSTIN_ALICE_CRASH_MODE",
        "austinAliceWriteExclusiveAtomicMarker",
        "aliceProcessCrashProbePublishesThenWaitsForKill",
        "aliceProcessCrashProbeRecoversKilledPublisher",
        "O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC",
        "Darwin.renameatx_np(",
        "UInt32(RENAME_EXCL)",
        "Darwin.fsync(directoryDescriptor)",
        "alarm(15)",
        "withExtendedLifetime(sink)",
    )
    if (
        any(marker not in capture_artifact_tests for marker in required_capture_crash_contract)
        or "[.atomic, .withoutOverwriting]" in capture_artifact_tests
    ):
        raise AuditError("capture_artifact_process_kill_contract")
    confirmation = (source_root / "AustinTCCAdapter" / "AustinConfirmation.swift").read_text(encoding="utf-8")
    required_confirmation_contract = (
        "NSWorkspace.sessionDidResignActiveNotification",
        "NSWorkspace.sessionDidBecomeActiveNotification",
        "NSWorkspace.screensDidSleepNotification",
        "NSWorkspace.screensDidWakeNotification",
        "maximumPresenceLifetimeMilliseconds",
        "@MainActor",
        "Allow Once",
        "!presenting",
        "RunLoop.main.add(timer, forMode: .modalPanel)",
        "confirmation_clock_rollback",
    )
    if any(marker not in confirmation for marker in required_confirmation_contract):
        raise AuditError("native_confirmation_contract")
    authority = (source_root / "AustinCore" / "AustinSamuelAuthority.swift").read_text(encoding="utf-8")
    xpc_protocol = (source_root / "AustinCore" / "AustinXPCProtocol.swift").read_text(encoding="utf-8")
    permit_store = (source_root / "AustinCore" / "AustinAdaPermitStore.swift").read_text(encoding="utf-8")
    coordinator = (source_root / "AustinTCCAdapter" / "AustinThomasBindingCoordinator.swift").read_text(
        encoding="utf-8"
    )
    production_control = (source_root / "AustinTCCAdapter" / "AustinThomasProductionControl.swift").read_text(
        encoding="utf-8"
    )
    dispatcher = (source_root / "AustinTCCAdapter" / "AustinDesktopDispatcher.swift").read_text(encoding="utf-8")
    relay = (source_root / "AustinRelay" / "AustinRelay.swift").read_text(encoding="utf-8")
    adapter_main = (source_root / "AustinTCCAdapterMain" / "AustinTCCAdapter.swift").read_text(encoding="utf-8")
    readiness = (source_root / "AustinTCCAdapter" / "AustinReadiness.swift").read_text(encoding="utf-8")
    required_preparation_contract = (
        (authority, '"control.prepare"'),
        (authority, 'kind: "control_prepare"'),
        (authority, "validatePreparationArguments("),
        (xpc_protocol, "func prepare("),
        (permit_store, "CREATE TABLE IF NOT EXISTS preparation_claims"),
        (permit_store, "preparation_replay"),
        (coordinator, "confirmation.confirm(action:"),
        (coordinator, "AustinThomasConfirmationLease("),
        (coordinator, "captureRedactionClassifier.preflight(for: preparation)"),
        (coordinator, "argumentsDigest"),
        (coordinator, "claimExecution("),
        (dispatcher, "preparationCoordinator?.supports("),
        (dispatcher, "claimExecution("),
        (relay, 'case "control.prepare"'),
    )
    if any(marker not in text for text, marker in required_preparation_contract):
        raise AuditError("native_preparation_contract")
    _audit_capture_xpc_boundary(
        screen_capture=screen_capture,
        capture_artifact=capture_artifact,
        capture_boundary=capture_boundary,
        capture_redaction=capture_redaction,
        coordinator=coordinator,
        xpc_protocol=xpc_protocol,
        relay=relay,
        adapter_main=adapter_main,
    )
    required_readiness_contract = (
        (xpc_protocol, "func readiness("),
        (relay, '"--readiness-probe"'),
        (relay, "DispatchSemaphore(value: 0)"),
        (relay, "DispatchTime.now()"),
        (adapter_main, "AustinSystemNativeReadinessBackend()"),
        (adapter_main, "controlProtocolEnabled: control.controlProtocolEnabled"),
        (adapter_main, "AustinNativeReadinessGate"),
        (adapter_main, 'AustinFailure("readiness_busy")'),
        (readiness, "AXIsProcessTrusted()"),
        (readiness, "CGPreflightScreenCaptureAccess()"),
        (readiness, "CGPreflightPostEventAccess()"),
        (readiness, "AEDeterminePermissionToAutomateTarget("),
        (readiness, "AEEventClass(0x6165_7674)"),
        (readiness, "AEEventID(0x6163_7476)"),
        (readiness, "SCContentSharingPicker.shared.isAvailable"),
        (readiness, "readiness_observed"),
    )
    if any(marker not in text for text, marker in required_readiness_contract):
        raise AuditError("native_readiness_contract")
    required_production_control_contract = (
        (production_control, "public struct AustinThomasControlActivation"),
        (production_control, "AustinJSON.decodeCanonicalObject(payload)"),
        (production_control, 'case "disabled":'),
        (production_control, 'case "enabled":'),
        (production_control, "routeNames == routeNames.sorted()"),
        (production_control, "AustinSystemAccessibilityBackend.focusedElement("),
        (production_control, "AustinSystemAppleEventBackend()"),
        (production_control, "AustinSystemShortcutBackend()"),
        (production_control, "AustinSystemCGEventBackend(sessionSafety:"),
        (production_control, "dispatcher: .disabledFoundation()"),
        (adapter_main, "let control = try AustinThomasProductionControl.system("),
        (adapter_main, "activationPayload: loadControlActivation()"),
        (adapter_main, "dispatcher: control.dispatcher"),
        (adapter_main, "bindingCoordinator: control.coordinator"),
        (adapter_main, "AustinNativeControlActivation.json"),
        (adapter_main, "O_RDONLY | O_NOFOLLOW | O_CLOEXEC"),
        (adapter_main, "try validateSealedApplicationBundle(bundle)"),
    )
    if any(marker not in text for text, marker in required_production_control_contract):
        raise AuditError("native_production_control_contract")
    route_start = production_control.find("public static let productionRoutes")
    route_end = production_control.find("\n    ]", route_start)
    if (
        route_start < 0
        or route_end < 0
        or ".screenshot" in production_control[route_start:route_end]
        or "dispatcher: AustinDesktopDispatcher.disabledFoundation()" in adapter_main
        or "bindingCoordinator: nil" in adapter_main
    ):
        raise AuditError("native_production_control_scope")
    readiness_start = adapter_main.find("    func readiness(")
    readiness_end = adapter_main.find("\n    func prepare(", readiness_start)
    readiness_handler = adapter_main[readiness_start:readiness_end]
    unlock_index = readiness_handler.find("lock.unlock()")
    probe_index = readiness_handler.find("let response = readinessProbe.encoded()")
    if (
        readiness_start < 0
        or readiness_end < 0
        or unlock_index < 0
        or probe_index < 0
        or unlock_index > probe_index
        or "defer { lock.unlock() }" in readiness_handler
        or "Date().addingTimeInterval" in relay
    ):
        raise AuditError("native_readiness_lock_scope")
    required_replay_retention_contract = (
        "CREATE TABLE IF NOT EXISTS ada_store_state",
        "high_water_ms",
        "permit_store_clock_rollback",
        "permit_store_stale_claim",
        "defaultMaximumClaimsPerNamespace",
        "PRAGMA max_page_count=",
        "PRAGMA journal_size_limit=",
        "PRAGMA quick_check(1)",
        "DELETE FROM permit_claims WHERE expires_at_ms <= ?1",
        "DELETE FROM preparation_claims WHERE expires_at_ms <= ?1",
        "BEGIN IMMEDIATE",
    )
    if any(marker not in permit_store for marker in required_replay_retention_contract):
        raise AuditError("native_replay_retention_contract")
    crash_probe = (source_root / "AustinAdaCrashProbeMain" / "AustinAdaCrashProbe.swift").read_text(encoding="utf-8")
    required_crash_contract = (
        "#if DEBUG",
        "ALGO_AUSTIN_ADA_CRASH_CHECKPOINT",
        "SIGKILL",
        "Darwin._exit(86)",
    )
    if any(marker not in permit_store for marker in required_crash_contract):
        raise AuditError("native_crash_injection_contract")
    required_crash_probe_contract = (
        'case "claim"',
        'case "claim-preparation"',
        'case "inspect"',
        'case "inspect-preparation"',
    )
    if any(marker not in crash_probe for marker in required_crash_probe_contract):
        raise AuditError("native_crash_probe_contract")
    adapter_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (source_root / "AustinTCCAdapter").glob("*.swift")
    )
    if "nativeConfirmationGranted" in adapter_sources or "actionConfirmationGranted" in adapter_sources:
        raise AuditError("native_confirmation_boolean")
    adapter_main = (source_root / "AustinTCCAdapterMain" / "AustinTCCAdapter.swift").read_text(encoding="utf-8")
    preparation_guard = "guard bindingCoordinator?.isEnabled(for: verified) == true"
    preparation_claim = "try permitStore.claimPreparation("
    if (
        adapter_main.count(preparation_guard) != 1
        or adapter_main.count(preparation_claim) != 1
        or adapter_main.index(preparation_guard) > adapter_main.index(preparation_claim)
    ):
        raise AuditError("native_preparation_admission_order")
    binding_consumer = (ROOT / "algo_cli" / "austin_thomas_binding.py").read_text(encoding="utf-8")
    required_binding_consumer_contract = (
        "canonical_json_bytes(decoded) != payload",
        "_validate_prepared_arguments(preparation, arguments)",
        "preparation.digest != self.preparation_digest",
        "MappingProxyType(validated)",
        "TargetKind.DESKTOP_SURFACE",
        '"requested_routes": [self.route.value]',
        "preparation.issued_at_ms",
        "preparation.expires_at_ms",
    )
    if any(marker not in binding_consumer for marker in required_binding_consumer_contract):
        raise AuditError("python_binding_consumer_contract")
    return [
        f"source_files:{len(files)}",
        "no_network_apis",
        "no_subprocess_api",
        "no_dynamic_script",
        "no_input_monitoring",
        "two_event_post_bound",
        "single_frame_screencapturekit",
        "target_bound_picker_filter",
        "bounded_redaction_work",
        "post_capture_redaction",
        "local_vision_redaction_candidate",
        "review_only_shortcut",
        "encrypted_capture_artifact",
        "crash_recovered_capture_artifact",
        "process_killed_capture_recovery",
        "authorized_capture_consumer",
        "sealed_capture_consumer",
        "content_free_xpc_capture",
        "os_backed_capture_key",
        "fresh_native_confirmation",
        "signed_native_preparation",
        "bounded_replay_retention",
        "debug_crash_injection",
        "target_bound_python_consumer",
        "sealed_control_activation",
    ]


def _run(*command: str) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AuditError(f"command_failed:{Path(command[0]).name}")
    return completed.stdout


def _audit_neon_allowed_origin(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    chunks: list[bytes] = []
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuditError("neon_origin_missing") from exc
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.getuid()}
            or mode & 0o022
            or (before.st_uid == os.getuid() and mode & 0o200)
            or not 1 <= before.st_size <= 64
        ):
            raise AuditError("neon_origin_file")
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise AuditError("neon_origin_read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AuditError("neon_origin_read")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise AuditError("neon_origin_race")
    except AuditError:
        raise
    except OSError as exc:
        raise AuditError("neon_origin_read") from exc
    finally:
        os.close(descriptor)
    try:
        origin = b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AuditError("neon_origin_encoding") from exc
    if NEON_EXTENSION_ORIGIN_RE.fullmatch(origin) is None:
        raise AuditError("neon_origin_value")
    return origin


def audit_bundle(bundle: Path) -> list[str]:
    bundle = bundle.resolve()
    expected = {
        "app": bundle / "Contents" / "MacOS" / "austin-control",
        "relay": bundle / "Contents" / "Helpers" / "austin-relay",
        "adapter": bundle / "Contents" / "Helpers" / "austin-tcc-adapter",
        "credential_migrator": bundle / "Contents" / "Helpers" / "austin-credential-migrator",
        "neon_host": bundle / "Contents" / "Helpers" / "neon-native-host",
    }
    if not bundle.is_dir() or any(not path.is_file() for path in expected.values()):
        raise AuditError("bundle_layout")
    authority_key = bundle / "Contents" / "Resources" / "AustinAuthorityPublicKey.bin"
    try:
        key_stat = authority_key.lstat()
        key_bytes = authority_key.read_bytes()
    except OSError as exc:
        raise AuditError("authority_key_missing") from exc
    if not authority_key.is_file() or authority_key.is_symlink():
        raise AuditError("authority_key_type")
    if key_stat.st_nlink != 1 or key_stat.st_mode & 0o022:
        raise AuditError("authority_key_permissions")
    if len(key_bytes) != 32:
        raise AuditError("authority_key_size")
    _audit_neon_allowed_origin(bundle / "Contents" / "Resources" / "NeonAllowedOrigin.txt")
    _run("codesign", "--verify", "--deep", "--strict", str(bundle))
    for label, binary in expected.items():
        libraries = _run("otool", "-L", str(binary))
        if "Network.framework" in libraries:
            raise AuditError(f"binary_network_framework:{label}")
        symbols = _run("nm", "-u", str(binary))
        for symbol in FORBIDDEN_UNDEFINED_SYMBOLS:
            if re.search(rf"(?:^|\s){re.escape(symbol)}(?:$|\s)", symbols, re.MULTILINE):
                raise AuditError(f"binary_network_symbol:{label}:{symbol[1:]}")
    return [
        "authority_key",
        "neon_allowed_origin",
        "bundle_signature",
        "binary_linkage",
        "binary_symbols",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        checks = [*audit_resources(), *audit_sources()]
        if args.bundle is not None:
            checks.extend(audit_bundle(args.bundle))
    except AuditError as exc:
        print(json.dumps({"status": "blocked", "reason_code": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"checks": checks, "status": "passed"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
