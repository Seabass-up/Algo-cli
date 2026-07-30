import AppKit
import ApplicationServices
import AustinCore
import CoreGraphics
import CoreServices
import Foundation
import ScreenCaptureKit

/// Content-free results from public, non-prompting permission preflights.
public enum AustinPermissionObservation: String, CaseIterable, Sendable {
    case granted
    case missing
    case denied
    case notDetermined = "not_determined"
    case targetUnavailable = "target_unavailable"
    case unknown
}

public protocol AustinNativeReadinessBackend: AnyObject {
    func accessibilityPermission() -> AustinPermissionObservation
    func screenRecordingPermission() -> AustinPermissionObservation
    func postEventPermission() -> AustinPermissionObservation
    func appleEventPermission(
        _ adapter: AustinReviewedAppleEvent
    ) -> AustinPermissionObservation
    func systemPickerAvailable() -> Bool
}

public struct AustinNativeReadinessSnapshot: Equatable, Sendable {
    public let accessibilityPermission: AustinPermissionObservation
    public let screenRecordingPermission: AustinPermissionObservation
    public let postEventPermission: AustinPermissionObservation
    public let finderAppleEventsPermission: AustinPermissionObservation
    public let systemSettingsAppleEventsPermission: AustinPermissionObservation
    public let systemPickerAvailable: Bool
    public let controlProtocolEnabled: Bool

    public init(
        accessibilityPermission: AustinPermissionObservation,
        screenRecordingPermission: AustinPermissionObservation,
        postEventPermission: AustinPermissionObservation,
        finderAppleEventsPermission: AustinPermissionObservation,
        systemSettingsAppleEventsPermission: AustinPermissionObservation,
        systemPickerAvailable: Bool,
        controlProtocolEnabled: Bool
    ) {
        self.accessibilityPermission = accessibilityPermission
        self.screenRecordingPermission = screenRecordingPermission
        self.postEventPermission = postEventPermission
        self.finderAppleEventsPermission = finderAppleEventsPermission
        self.systemSettingsAppleEventsPermission = systemSettingsAppleEventsPermission
        self.systemPickerAvailable = systemPickerAvailable
        self.controlProtocolEnabled = controlProtocolEnabled
    }

    public func encoded() -> Data {
        AustinReply.encode(
            status: "succeeded",
            reasonCode: "readiness_observed",
            fields: [
                "readiness": [
                    "accessibility_permission": accessibilityPermission.rawValue,
                    "apple_events_finder_permission": finderAppleEventsPermission.rawValue,
                    "apple_events_system_settings_permission":
                        systemSettingsAppleEventsPermission.rawValue,
                    "control_protocol_enabled": controlProtocolEnabled,
                    "post_event_permission": postEventPermission.rawValue,
                    "screen_recording_permission": screenRecordingPermission.rawValue,
                    "system_picker_available": systemPickerAvailable,
                ],
            ]
        )
    }
}

public final class AustinNativeReadinessProbe: @unchecked Sendable {
    private let backend: AustinNativeReadinessBackend
    private let controlProtocolEnabled: Bool

    public init(
        backend: AustinNativeReadinessBackend,
        controlProtocolEnabled: Bool
    ) {
        self.backend = backend
        self.controlProtocolEnabled = controlProtocolEnabled
    }

    public func snapshot() -> AustinNativeReadinessSnapshot {
        AustinNativeReadinessSnapshot(
            accessibilityPermission: backend.accessibilityPermission(),
            screenRecordingPermission: backend.screenRecordingPermission(),
            postEventPermission: backend.postEventPermission(),
            finderAppleEventsPermission: backend.appleEventPermission(.activateFinder),
            systemSettingsAppleEventsPermission: backend.appleEventPermission(
                .activateSystemSettings
            ),
            systemPickerAvailable: backend.systemPickerAvailable(),
            controlProtocolEnabled: controlProtocolEnabled
        )
    }

    public func encoded() -> Data {
        snapshot().encoded()
    }
}

/// One process-wide lease prevents signed relay fan-out from multiplying
/// potentially slow OS permission preflights.
public final class AustinNativeReadinessGate: @unchecked Sendable {
    private let lock = NSLock()
    private var claimed = false

    public init() {}

    public func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !claimed else { return false }
        claimed = true
        return true
    }

    @discardableResult
    public func release() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard claimed else { return false }
        claimed = false
        return true
    }
}

public final class AustinSystemNativeReadinessBackend:
    AustinNativeReadinessBackend,
    @unchecked Sendable
{
    public init() {}

    public func accessibilityPermission() -> AustinPermissionObservation {
        AXIsProcessTrusted() ? .granted : .missing
    }

    public func screenRecordingPermission() -> AustinPermissionObservation {
        CGPreflightScreenCaptureAccess() ? .granted : .missing
    }

    public func postEventPermission() -> AustinPermissionObservation {
        CGPreflightPostEventAccess() ? .granted : .missing
    }

    public func appleEventPermission(
        _ adapter: AustinReviewedAppleEvent
    ) -> AustinPermissionObservation {
        let descriptor = NSAppleEventDescriptor(
            bundleIdentifier: adapter.bundleIdentifier
        )
        guard let target = descriptor.aeDesc else { return .targetUnavailable }
        // Preflight the exact reviewed Core Suite Activate event. `false`
        // prohibits a TCC prompt and this function sends no Apple Event.
        let status = AEDeterminePermissionToAutomateTarget(
            target,
            AEEventClass(0x6165_7674),
            AEEventID(0x6163_7476),
            false
        )
        switch status {
        case OSStatus(noErr):
            return .granted
        case OSStatus(errAEEventNotPermitted):
            return .denied
        case OSStatus(errAEEventWouldRequireUserConsent):
            return .notDetermined
        case OSStatus(procNotFound):
            return .targetUnavailable
        default:
            return .unknown
        }
    }

    public func systemPickerAvailable() -> Bool {
        #if compiler(>=6.4)
        if #available(macOS 27.0, *) {
            return SCContentSharingPicker.shared.isAvailable
        }
        #endif
        if #available(macOS 15.2, *) {
            _ = SCContentSharingPicker.shared
            return true
        }
        return false
    }
}
