import Foundation
import Dispatch
import Testing
@testable import AustinCore
@testable import AustinDesktopCore

private final class AustinReadinessFixtureBackend: AustinNativeReadinessBackend {
    var accessibility: AustinPermissionObservation = .granted
    var screenRecording: AustinPermissionObservation = .missing
    var postEvent: AustinPermissionObservation = .denied
    var finder: AustinPermissionObservation = .notDetermined
    var systemSettings: AustinPermissionObservation = .targetUnavailable
    var picker = true

    func accessibilityPermission() -> AustinPermissionObservation { accessibility }
    func screenRecordingPermission() -> AustinPermissionObservation { screenRecording }
    func postEventPermission() -> AustinPermissionObservation { postEvent }
    func appleEventPermission(
        _ adapter: AustinReviewedAppleEvent
    ) -> AustinPermissionObservation {
        switch adapter {
        case .activateFinder:
            finder
        case .activateSystemSettings:
            systemSettings
        }
    }
    func systemPickerAvailable() -> Bool { picker }
}

private final class AustinReadinessClaimResults: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var values: [Bool] = []

    func append(_ value: Bool) {
        lock.lock()
        values.append(value)
        lock.unlock()
    }
}

@Test func nativeReadinessSnapshotIsExactContentFreeAndDisabled() throws {
    let backend = AustinReadinessFixtureBackend()
    let probe = AustinNativeReadinessProbe(
        backend: backend,
        controlProtocolEnabled: false
    )

    #expect(
        probe.snapshot() == AustinNativeReadinessSnapshot(
            accessibilityPermission: .granted,
            screenRecordingPermission: .missing,
            postEventPermission: .denied,
            finderAppleEventsPermission: .notDetermined,
            systemSettingsAppleEventsPermission: .targetUnavailable,
            systemPickerAvailable: true,
            controlProtocolEnabled: false
        )
    )
    let encoded = probe.encoded()
    #expect(
        String(decoding: encoded, as: UTF8.self)
            == "{\"protocol_version\":1,\"readiness\":{\"accessibility_permission\":\"granted\",\"apple_events_finder_permission\":\"not_determined\",\"apple_events_system_settings_permission\":\"target_unavailable\",\"control_protocol_enabled\":false,\"post_event_permission\":\"denied\",\"screen_recording_permission\":\"missing\",\"system_picker_available\":true},\"reason_code\":\"readiness_observed\",\"status\":\"succeeded\"}"
    )
    let decoded = try AustinJSON.decodeCanonicalObject(encoded)
    #expect(Set(decoded.keys) == ["protocol_version", "readiness", "reason_code", "status"])
    #expect(decoded["status"] as? String == "succeeded")
    #expect(decoded["reason_code"] as? String == "readiness_observed")
}

@Test func everyPermissionObservationHasAStableClosedVocabulary() {
    #expect(
        AustinPermissionObservation.allCases.map(\.rawValue) == [
            "granted",
            "missing",
            "denied",
            "not_determined",
            "target_unavailable",
            "unknown",
        ]
    )
}

@Test func readinessGateBoundsConcurrentProcessWidePreflights() {
    let gate = AustinNativeReadinessGate()
    let results = AustinReadinessClaimResults()

    #expect(gate.claim())
    DispatchQueue.concurrentPerform(iterations: 128) { _ in
        results.append(gate.claim())
    }
    #expect(results.values == Array(repeating: false, count: 128))
    #expect(gate.release())
    #expect(!gate.release())
    #expect(gate.claim())
    #expect(gate.release())
}
