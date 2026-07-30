@testable import AustinCore
@testable import AustinDesktopCore
import CoreGraphics
import Foundation
import Testing

private let desktopNow: Int64 = 1_800_000_000_000
private let desktopPreparationID = "00000000-0000-4000-8000-000000000601"

private func desktopConfirmation(
    _ action: AustinConfirmationAction,
    preparationID: String = desktopPreparationID,
    at milliseconds: Int64 = desktopNow
) throws -> AustinThomasConfirmationLease {
    try AustinThomasConfirmationLease(
        preparationID: preparationID,
        action: action,
        issuedAtMilliseconds: milliseconds
    )
}

private func desktopProcess(
    pid: Int32 = 4242,
    startSeconds: UInt64 = 100
) throws -> AustinDesktopProcess {
    try AustinDesktopProcess(
        processIdentifier: pid,
        processStartSeconds: startSeconds,
        processStartMicroseconds: 25,
        bundleIdentifier: "com.example.AustinFixture"
    )
}

private func desktopTarget(
    marker: Character = "a",
    lifetimeMilliseconds: Int64 = 5_000
) throws -> AustinDesktopTargetBinding {
    try AustinDesktopTargetBinding(
        targetID: "hmac-sha256:" + String(repeating: marker, count: 64),
        targetEpoch: 1,
        targetRevision: "fixture_1",
        fencingToken: 1,
        snapshotID: UUID().uuidString.lowercased(),
        snapshotSequence: 1,
        observedAtMilliseconds: desktopNow,
        expiresAtMilliseconds: desktopNow + lifetimeMilliseconds
    )
}

private func pickerSelection(
    contentID: UInt32 = 42,
    width: Double = 1_440,
    height: Double = 900
) throws -> AustinCaptureSelectionIdentity {
    try AustinCaptureSelectionIdentity(
        kind: .window,
        contentID: contentID,
        x: 0,
        y: 0,
        width: width,
        height: height,
        pointPixelScale: 2
    )
}

private func desktopEnvelope(
    target: AustinDesktopTargetBinding,
    operation: AustinOperation,
    route: AustinRoute,
    arguments: [String: Any]
) -> AustinVerifiedEnvelope {
    AustinVerifiedEnvelope(
        requestID: "00000000-0000-4000-8000-000000000101",
        subjectID: "runtime.operator",
        permitID: "00000000-0000-4000-8000-000000000501",
        requestDigest: "sha256:" + String(repeating: "d", count: 64),
        targetID: target.targetID,
        targetEpoch: target.targetEpoch,
        targetRevision: target.targetRevision,
        fencingToken: target.fencingToken,
        snapshotID: target.snapshotID,
        snapshotSequence: target.snapshotSequence,
        operation: operation,
        dataClass: operation == .inputText ? .private : .structural,
        route: route,
        arguments: arguments,
        expiresAtMilliseconds: target.expiresAtMilliseconds
    )
}

private func bindAX(
    _ adapter: AustinAccessibility,
    backend: AustinAccessibilityBackend,
    operation: AustinOperation = .activate,
    lifetimeMilliseconds: Int64 = AustinAccessibility.maximumBindingLifetimeMilliseconds
) throws -> AustinAXBinding {
    let action: AustinConfirmationAction
    switch operation {
    case .activate: action = .accessibilityActivate
    case .selectOption: action = .accessibilitySelect
    case .scroll: action = .accessibilityScroll
    default: action = .accessibilityActivate
    }
    return try adapter.bind(
        backend: backend,
        operation: operation,
        confirmation: desktopConfirmation(action),
        preparationID: desktopPreparationID,
        nowMilliseconds: desktopNow,
        lifetimeMilliseconds: lifetimeMilliseconds
    )
}

private func issuePersistentCapture(
    _ adapter: AustinScreenCapture,
    target: AustinDesktopTargetBinding,
    confirmed: Bool,
    redactions: [AustinCaptureRedaction]? = nil
) throws -> AustinCaptureLease {
    let appliedRedactions = try redactions ?? [
        AustinCaptureRedaction(x: 0, y: 0, width: 1, height: 1)
    ]
    let preparation = AustinVerifiedPreparation(
        preparationID: desktopPreparationID,
        requestID: "00000000-0000-4000-8000-000000000101",
        subjectID: "runtime.operator",
        operation: .observe,
        dataClass: .structural,
        route: .screenshot,
        selector: "persistent_programmatic",
        arguments: [:],
        preparationDigest: "sha256:" + String(repeating: "a", count: 64),
        issuedAtMilliseconds: desktopNow - 100,
        expiresAtMilliseconds: desktopNow + 1_000
    )
    return try adapter.issuePersistentLease(
        target: target,
        screenRecordingPermissionGranted: true,
        confirmation: desktopConfirmation(
            confirmed ? .persistentCapture : .shortcutReview
        ),
        preparationID: desktopPreparationID,
        redactionClassifier: FixtureCaptureClassifier(redactions: appliedRedactions),
        redactionContext: AustinCaptureRedactionContext(preparation: preparation),
        nowMilliseconds: desktopNow
    )
}

private func bindAppleEvent(
    _ adapter: AustinAppleEvent,
    target: AustinDesktopTargetBinding,
    confirmed: Bool
) throws -> AustinAppleEventBinding {
    try adapter.bind(
        target: target,
        adapter: .activateFinder,
        confirmation: desktopConfirmation(
            confirmed ? .appleEventActivate : .shortcutReview
        ),
        preparationID: desktopPreparationID,
        nowMilliseconds: desktopNow
    )
}

private func bindShortcut(
    _ adapter: AustinShortcut,
    target: AustinDesktopTargetBinding,
    confirmed: Bool
) throws -> AustinShortcutBinding {
    try adapter.bind(
        target: target,
        adapter: .reviewCurrentTask,
        confirmation: desktopConfirmation(
            confirmed ? .shortcutReview : .appleEventActivate
        ),
        preparationID: desktopPreparationID,
        nowMilliseconds: desktopNow
    )
}

private func bindCoordinate(
    _ adapter: AustinCGEvent,
    target: AustinDesktopTargetBinding,
    x: Int = 100,
    y: Int = 200,
    confirmed: Bool = true
) throws -> AustinCoordinateBinding {
    try adapter.bind(
        target: target,
        x: x,
        y: y,
        confirmation: desktopConfirmation(
            confirmed ? .coordinateActivate : .appleEventActivate
        ),
        preparationID: desktopPreparationID,
        nowMilliseconds: desktopNow
    )
}

private final class FixtureAXBackend: AustinAccessibilityBackend, @unchecked Sendable {
    var state: AustinAXElementState
    var stateError: AustinAXBackendError?
    var effect: AustinNativeEffect = .performed
    var verification: AustinNativePostcondition = .verified
    var performCount = 0

    init(state: AustinAXElementState) {
        self.state = state
    }

    func currentState() throws -> AustinAXElementState {
        if let stateError { throw stateError }
        return state
    }

    func perform(
        operation: AustinOperation,
        arguments: [String: Any]
    ) -> AustinNativeEffect {
        performCount += 1
        return effect
    }

    func postcondition(
        operation: AustinOperation,
        before: AustinAXElementState
    ) -> AustinNativePostcondition {
        verification
    }
}

private func fixtureAXState(
    process: AustinDesktopProcess? = nil,
    role: String = "AXButton",
    focused: Bool = true,
    enabled: Bool = true,
    sensitivity: AustinAXSensitivity = .normal,
    modal: AustinAXModalKind = .none
) throws -> AustinAXElementState {
    try AustinAXElementState(
        process: process ?? desktopProcess(),
        windowFingerprint: 99,
        role: role,
        enabled: enabled,
        focusedWindow: focused,
        modalKind: modal,
        sensitivity: sensitivity
    )
}

@Test func axBindingIsEphemeralOneUseAndTargetBound() throws {
    let backend = FixtureAXBackend(state: try fixtureAXState())
    let adapter = try AustinAccessibility(randomBytes: { Data(repeating: 7, count: 32) })
    let binding = try bindAX(adapter, backend: backend)
    let envelope = desktopEnvelope(
        target: binding.target,
        operation: .activate,
        route: .ax,
        arguments: ["element_id": binding.elementID]
    )
    #expect(adapter.supports(envelope, nowMilliseconds: desktopNow + 1))
    #expect(
        adapter.execute(envelope, nowMilliseconds: desktopNow + 1)
            == AustinDesktopOutcome(.succeeded, "ax_postcondition_verified")
    )
    #expect(!adapter.supports(envelope, nowMilliseconds: desktopNow + 2))
    #expect(
        adapter.execute(envelope, nowMilliseconds: desktopNow + 2)
            == AustinDesktopOutcome(.denied, "ax_element_stale")
    )
    #expect(backend.performCount == 1)
}

@Test func axExpiryFocusTheftAndProcessRelaunchFailBeforeMutation() throws {
    let expiredBackend = FixtureAXBackend(state: try fixtureAXState())
    let expired = try AustinAccessibility(randomBytes: { Data(repeating: 8, count: 32) })
    let expiredBinding = try bindAX(expired, backend: expiredBackend, lifetimeMilliseconds: 5)
    let expiredEnvelope = desktopEnvelope(
        target: expiredBinding.target,
        operation: .activate,
        route: .ax,
        arguments: ["element_id": expiredBinding.elementID]
    )
    #expect(!expired.supports(expiredEnvelope, nowMilliseconds: desktopNow + 5))
    #expect(expiredBackend.performCount == 0)

    let focusBackend = FixtureAXBackend(state: try fixtureAXState())
    let focus = try AustinAccessibility(randomBytes: { Data(repeating: 9, count: 32) })
    let focusBinding = try bindAX(focus, backend: focusBackend)
    focusBackend.state = try fixtureAXState(focused: false)
    let focusResult = focus.execute(
        desktopEnvelope(
            target: focusBinding.target,
            operation: .activate,
            route: .ax,
            arguments: ["element_id": focusBinding.elementID]
        ),
        nowMilliseconds: desktopNow + 1
    )
    #expect(focusResult == AustinDesktopOutcome(.denied, "ax_focus_changed"))
    #expect(focusBackend.performCount == 0)

    let processBackend = FixtureAXBackend(state: try fixtureAXState())
    let process = try AustinAccessibility(randomBytes: { Data(repeating: 10, count: 32) })
    let processBinding = try bindAX(process, backend: processBackend)
    processBackend.state = try fixtureAXState(process: desktopProcess(startSeconds: 101))
    let processResult = process.execute(
        desktopEnvelope(
            target: processBinding.target,
            operation: .activate,
            route: .ax,
            arguments: ["element_id": processBinding.elementID]
        ),
        nowMilliseconds: desktopNow + 1
    )
    #expect(processResult == AustinDesktopOutcome(.denied, "ax_process_changed"))
    #expect(processBackend.performCount == 0)
}

@Test func axSensitiveInputAndSecureModalsAlwaysHandoff() throws {
    for (index, sensitivity) in [
        AustinAXSensitivity.secure,
        .authentication,
        .payment,
        .unknownInput,
    ].enumerated() {
        let backend = FixtureAXBackend(
            state: try fixtureAXState(sensitivity: sensitivity)
        )
        let adapter = try AustinAccessibility(
            randomBytes: { Data(repeating: UInt8(index + 11), count: 32) }
        )
        let binding = try bindAX(adapter, backend: backend)
        let outcome = adapter.execute(
            desktopEnvelope(
                target: binding.target,
                operation: .activate,
                route: .ax,
                arguments: ["element_id": binding.elementID]
            ),
            nowMilliseconds: desktopNow + 1
        )
        #expect(outcome == AustinDesktopOutcome(.handoffRequired, "ax_sensitive_handoff"))
        #expect(backend.performCount == 0)
    }

    let textBackend = FixtureAXBackend(state: try fixtureAXState(role: "AXTextField"))
    let textAdapter = try AustinAccessibility(randomBytes: { Data(repeating: 20, count: 32) })
    #expect(throws: AustinFailure.self) {
        try bindAX(textAdapter, backend: textBackend, operation: .inputText)
    }
    #expect(textBackend.performCount == 0)

    let modalBackend = FixtureAXBackend(
        state: try fixtureAXState(modal: .authentication)
    )
    let modalAdapter = try AustinAccessibility(randomBytes: { Data(repeating: 21, count: 32) })
    let modalBinding = try bindAX(modalAdapter, backend: modalBackend)
    let modalOutcome = modalAdapter.execute(
        desktopEnvelope(
            target: modalBinding.target,
            operation: .activate,
            route: .ax,
            arguments: ["element_id": modalBinding.elementID]
        ),
        nowMilliseconds: desktopNow + 1
    )
    #expect(modalOutcome == AustinDesktopOutcome(.handoffRequired, "ax_modal_handoff"))
}

@Test func axCannotCompleteAndUnverifiedPostconditionNeverRetryAsSuccess() throws {
    let backend = FixtureAXBackend(state: try fixtureAXState())
    let adapter = try AustinAccessibility(randomBytes: { Data(repeating: 22, count: 32) })
    let binding = try bindAX(adapter, backend: backend)
    backend.stateError = .cannotComplete
    let envelope = desktopEnvelope(
        target: binding.target,
        operation: .activate,
        route: .ax,
        arguments: ["element_id": binding.elementID]
    )
    #expect(
        adapter.execute(envelope, nowMilliseconds: desktopNow + 1)
            == AustinDesktopOutcome(.unknownOutcome, "ax_cannot_complete")
    )
    #expect(
        adapter.execute(envelope, nowMilliseconds: desktopNow + 2)
            == AustinDesktopOutcome(.denied, "ax_element_stale")
    )
    #expect(backend.performCount == 0)

    let unverifiedBackend = FixtureAXBackend(state: try fixtureAXState())
    unverifiedBackend.verification = .unavailable
    let unverified = try AustinAccessibility(randomBytes: { Data(repeating: 23, count: 32) })
    let unverifiedBinding = try bindAX(unverified, backend: unverifiedBackend)
    let outcome = unverified.execute(
        desktopEnvelope(
            target: unverifiedBinding.target,
            operation: .activate,
            route: .ax,
            arguments: ["element_id": unverifiedBinding.elementID]
        ),
        nowMilliseconds: desktopNow + 1
    )
    #expect(outcome == AustinDesktopOutcome(.unknownOutcome, "ax_postcondition_unverified"))
    #expect(unverifiedBackend.performCount == 1)
}

@Test func axRejectsUnknownEnvironmentAndBoundsEphemeralRegistry() throws {
    let unknown = FixtureAXBackend(
        state: try AustinAXElementState(
            process: desktopProcess(),
            windowFingerprint: 99,
            role: "AXButton",
            enabled: true,
            focusedWindow: true,
            environmentVerified: false
        )
    )
    let unavailable = try AustinAccessibility(randomBytes: { Data(repeating: 40, count: 32) })
    #expect(throws: AustinFailure.self) {
        try bindAX(unavailable, backend: unknown)
    }

    let bounded = try AustinAccessibility(randomBytes: { Data(repeating: 41, count: 32) })
    let backend = FixtureAXBackend(state: try fixtureAXState())
    for _ in 0..<AustinAccessibility.maximumBindings {
        _ = try bindAX(bounded, backend: backend)
    }
    #expect(throws: AustinFailure.self) {
        try bindAX(bounded, backend: backend)
    }
}

private final class FixtureCaptureBackend: AustinScreenCaptureBackend, @unchecked Sendable {
    var modes: [AustinCaptureMode] = []
    var selections: [AustinCaptureSelectionIdentity?] = []

    func capture(
        mode: AustinCaptureMode,
        expectedSelection: AustinCaptureSelectionIdentity?
    ) throws -> AustinPixelFrame {
        modes.append(mode)
        selections.append(expectedSelection)
        return try AustinPixelFrame(
            width: 2,
            height: 1,
            rgbaBytes: [10, 20, 30, 40, 50, 60, 70, 80]
        )
    }
}

private final class FixtureCaptureSink: AustinRedactedCaptureSink, @unchecked Sendable {
    var frames: [AustinPixelFrame] = []

    func acceptRedacted(_ frame: AustinPixelFrame) throws {
        frames.append(frame)
    }
}

private final class FixtureCaptureClassifier: AustinCaptureRedactionClassifying,
    @unchecked Sendable
{
    private let plannedRedactions: [AustinCaptureRedaction]

    init(redactions: [AustinCaptureRedaction]) {
        plannedRedactions = redactions
    }

    func preflight(for preparation: AustinVerifiedPreparation) throws {}

    func redactions(
        for frame: AustinPixelFrame,
        context: AustinCaptureRedactionContext
    ) throws -> [AustinCaptureRedaction] {
        plannedRedactions
    }
}

private final class FixtureCaptureModeLog: @unchecked Sendable {
    private let lock = NSLock()
    private var modes: [AustinCaptureMode] = []

    func append(_ mode: AustinCaptureMode) {
        lock.lock()
        modes.append(mode)
        lock.unlock()
    }

    func snapshot() -> [AustinCaptureMode] {
        lock.lock()
        defer { lock.unlock() }
        return modes
    }
}

private final class FixtureIntegerCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0

    func increment() {
        lock.lock()
        value += 1
        lock.unlock()
    }

    func snapshot() -> Int {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

private func fixtureCaptureImage(_ rgbaBytes: [UInt8], width: Int, height: Int) throws -> CGImage {
    guard rgbaBytes.count == width * height * 4,
          let provider = CGDataProvider(data: Data(rgbaBytes) as CFData),
          let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
          let image = CGImage(
              width: width,
              height: height,
              bitsPerComponent: 8,
              bitsPerPixel: 32,
              bytesPerRow: width * 4,
              space: colorSpace,
              bitmapInfo: CGBitmapInfo(
                  rawValue: CGBitmapInfo.byteOrder32Big.rawValue
                      | CGImageAlphaInfo.premultipliedLast.rawValue
              ),
              provider: provider,
              decode: nil,
              shouldInterpolate: false,
              intent: .defaultIntent
          )
    else {
        throw AustinFailure("capture_fixture")
    }
    return image
}

@Test func captureModesAreDistinctConfirmedRedactedAndOneShot() throws {
    let pickerBackend = FixtureCaptureBackend()
    let pickerSink = FixtureCaptureSink()
    let picker = try AustinScreenCapture(
        backend: pickerBackend,
        sink: pickerSink,
        randomBytes: { Data(repeating: 24, count: 32) }
    )
    let target = try desktopTarget(lifetimeMilliseconds: 4_000)
    #expect(throws: AustinFailure.self) {
        try picker.issuePickerLease(
            target: target,
            userGestureConfirmed: false,
            selection: try pickerSelection(),
            redactions: [],
            nowMilliseconds: desktopNow
        )
    }
    let redaction = try AustinCaptureRedaction(x: 0, y: 0, width: 1, height: 1)
    let pickerLease = try picker.issuePickerLease(
        target: target,
        userGestureConfirmed: true,
        selection: try pickerSelection(),
        redactions: [redaction],
        nowMilliseconds: desktopNow
    )
    #expect(throws: AustinFailure.self) {
        try issuePersistentCapture(
            picker,
            target: target,
            confirmed: true
        )
    }
    let pickerEnvelope = desktopEnvelope(
        target: pickerLease.target,
        operation: .observe,
        route: .screenshot,
        arguments: [:]
    )
    #expect(
        picker.execute(pickerEnvelope, nowMilliseconds: desktopNow + 1)
            == AustinDesktopOutcome(.succeeded, "capture_picker_redacted")
    )
    #expect(pickerBackend.modes == [.pickerScoped])
    #expect(pickerBackend.selections == [try pickerSelection()])
    #expect(pickerSink.frames.first?.rgbaBytes == [0, 0, 0, 255, 50, 60, 70, 80])
    #expect(
        picker.execute(pickerEnvelope, nowMilliseconds: desktopNow + 2)
            == AustinDesktopOutcome(.denied, "capture_lease_stale")
    )

    let persistentBackend = FixtureCaptureBackend()
    let persistentSink = FixtureCaptureSink()
    let persistent = try AustinScreenCapture(
        backend: persistentBackend,
        sink: persistentSink,
        randomBytes: { Data(repeating: 25, count: 32) }
    )
    #expect(throws: AustinFailure.self) {
        try issuePersistentCapture(
            persistent,
            target: try desktopTarget(marker: "b", lifetimeMilliseconds: 3_000),
            confirmed: false
        )
    }
    let persistentLease = try issuePersistentCapture(
        persistent,
        target: desktopTarget(marker: "c", lifetimeMilliseconds: 3_000),
        confirmed: true
    )
    let persistentEnvelope = desktopEnvelope(
        target: persistentLease.target,
        operation: .observe,
        route: .screenshot,
        arguments: [:]
    )
    #expect(
        persistent.execute(persistentEnvelope, nowMilliseconds: desktopNow + 1)
            == AustinDesktopOutcome(.succeeded, "capture_persistent_redacted")
    )
    #expect(persistentBackend.modes == [.persistentProgrammatic])
}

@Test func captureExpiredLeaseIsNotReadyAndNeverReadsPixels() throws {
    let backend = FixtureCaptureBackend()
    let sink = FixtureCaptureSink()
    let adapter = try AustinScreenCapture(
        backend: backend,
        sink: sink,
        randomBytes: { Data(repeating: 42, count: 32) }
    )
    let lease = try adapter.issuePickerLease(
        target: desktopTarget(marker: "6", lifetimeMilliseconds: 10),
        userGestureConfirmed: true,
        selection: try pickerSelection(contentID: 43),
        redactions: [try AustinCaptureRedaction(x: 0, y: 0, width: 1, height: 1)],
        nowMilliseconds: desktopNow,
        lifetimeMilliseconds: 10
    )
    let envelope = desktopEnvelope(
        target: lease.target,
        operation: .observe,
        route: .screenshot,
        arguments: [:]
    )
    #expect(!adapter.supports(envelope, nowMilliseconds: desktopNow + 10))
    #expect(backend.modes.isEmpty)
    #expect(sink.frames.isEmpty)
}

@Test func persistentCaptureClassifierContextIsCrossBoundBeforePixelAcquisition() throws {
    let backend = FixtureCaptureBackend()
    let sink = FixtureCaptureSink()
    let adapter = try AustinScreenCapture(
        backend: backend,
        sink: sink,
        randomBytes: { Data(repeating: 44, count: 32) }
    )
    let lease = try issuePersistentCapture(
        adapter,
        target: desktopTarget(marker: "4", lifetimeMilliseconds: 1_000),
        confirmed: true
    )
    let exact = desktopEnvelope(
        target: lease.target,
        operation: .observe,
        route: .screenshot,
        arguments: [:]
    )
    let changedSubject = AustinVerifiedEnvelope(
        requestID: exact.requestID,
        subjectID: "runtime.attacker",
        permitID: exact.permitID,
        requestDigest: exact.requestDigest,
        targetID: exact.targetID,
        targetEpoch: exact.targetEpoch,
        targetRevision: exact.targetRevision,
        fencingToken: exact.fencingToken,
        snapshotID: exact.snapshotID,
        snapshotSequence: exact.snapshotSequence,
        operation: exact.operation,
        dataClass: exact.dataClass,
        route: exact.route,
        arguments: exact.arguments,
        expiresAtMilliseconds: exact.expiresAtMilliseconds
    )

    #expect(!adapter.supports(changedSubject, nowMilliseconds: desktopNow + 1))
    #expect(
        adapter.execute(changedSubject, nowMilliseconds: desktopNow + 1)
            == AustinDesktopOutcome(.denied, "capture_redaction_authority")
    )
    #expect(backend.modes.isEmpty)
    #expect(sink.frames.isEmpty)
}

@Test func captureRejectsEmptyRedactionPlanBeforeReadingPixels() throws {
    let backend = FixtureCaptureBackend()
    let sink = FixtureCaptureSink()
    let adapter = try AustinScreenCapture(
        backend: backend,
        sink: sink,
        randomBytes: { Data(repeating: 43, count: 32) }
    )
    #expect(throws: AustinFailure.self) {
        try adapter.issuePickerLease(
            target: desktopTarget(marker: "5", lifetimeMilliseconds: 1_000),
            userGestureConfirmed: true,
            selection: pickerSelection(contentID: 46),
            redactions: [],
            nowMilliseconds: desktopNow
        )
    }
    #expect(backend.modes.isEmpty)
    #expect(sink.frames.isEmpty)

    var frame = try AustinPixelFrame(width: 1, height: 1, rgbaBytes: [1, 2, 3, 255])
    #expect(throws: AustinFailure.self) { try frame.redact([]) }
    #expect(frame.rgbaBytes == [1, 2, 3, 255])
}

@Test func captureRejectsAmplifiedRedactionWorkBeforeMutatingPixels() throws {
    let original: [UInt8] = [1, 2, 3, 255, 4, 5, 6, 255]
    var frame = try AustinPixelFrame(width: 2, height: 1, rgbaBytes: original)
    let entireFrame = try AustinCaptureRedaction(x: 0, y: 0, width: 2, height: 1)

    #expect(throws: AustinFailure.self) {
        try frame.redact([entireFrame, entireFrame])
    }
    #expect(frame.rgbaBytes == original)
}

@Test func systemCaptureBackendSelectsExactModeAndConvertsOneFrameToRGBA() throws {
    let pickerImage = try fixtureCaptureImage(
        [255, 0, 0, 255, 0, 255, 0, 255],
        width: 2,
        height: 1
    )
    let persistentImage = try fixtureCaptureImage(
        [0, 0, 255, 255],
        width: 1,
        height: 1
    )
    let observedModes = FixtureCaptureModeLog()
    let backend = try AustinSystemScreenCaptureBackend(
        timeoutMilliseconds: 50,
        captureOperation: { mode, _, completion in
            observedModes.append(mode)
            completion(mode == .pickerScoped ? pickerImage : persistentImage)
            completion(nil)
        }
    )

    let pickerFrame = try backend.capture(
        mode: .pickerScoped,
        expectedSelection: pickerSelection()
    )
    #expect(pickerFrame.width == 2)
    #expect(pickerFrame.height == 1)
    #expect(pickerFrame.rgbaBytes == [255, 0, 0, 255, 0, 255, 0, 255])

    let persistentFrame = try backend.capture(
        mode: .persistentProgrammatic,
        expectedSelection: nil
    )
    #expect(persistentFrame.width == 1)
    #expect(persistentFrame.height == 1)
    #expect(persistentFrame.rgbaBytes == [0, 0, 255, 255])
    #expect(observedModes.snapshot() == [.pickerScoped, .persistentProgrammatic])
}

@Test func systemCaptureBackendFailsClosedOnTimeoutAndMissingImage() throws {
    let timedOut = try AustinSystemScreenCaptureBackend(
        timeoutMilliseconds: 5,
        captureOperation: { _, _, _ in }
    )
    do {
        _ = try timedOut.capture(
            mode: .pickerScoped,
            expectedSelection: pickerSelection()
        )
        Issue.record("capture unexpectedly completed")
    } catch let failure as AustinFailure {
        #expect(failure.reasonCode == "capture_timeout")
    }

    let missing = try AustinSystemScreenCaptureBackend(
        timeoutMilliseconds: 50,
        captureOperation: { _, _, completion in completion(nil) }
    )
    do {
        _ = try missing.capture(
            mode: .persistentProgrammatic,
            expectedSelection: nil
        )
        Issue.record("missing image unexpectedly completed")
    } catch let failure as AustinFailure {
        #expect(failure.reasonCode == "capture_image")
    }
}

@Test func captureSelectionIdentityRejectsInvalidAndDistinguishesTargetChanges() throws {
    let first = try pickerSelection(contentID: 44)
    let same = try pickerSelection(contentID: 44)
    let changedID = try pickerSelection(contentID: 45)
    let changedGeometry = try pickerSelection(contentID: 44, width: 1_439)
    #expect(first == same)
    #expect(first != changedID)
    #expect(first != changedGeometry)
    #expect(throws: AustinFailure.self) {
        try AustinCaptureSelectionIdentity(
            kind: .window,
            contentID: 0,
            x: 0,
            y: 0,
            width: 1,
            height: 1,
            pointPixelScale: 1
        )
    }
    #expect(throws: AustinFailure.self) {
        try AustinCaptureSelectionIdentity(
            kind: .display,
            contentID: 1,
            x: 0,
            y: 0,
            width: .infinity,
            height: 1,
            pointPixelScale: 1
        )
    }
}

@Test func systemCaptureBackendRejectsCrossModeSelectionBeforeCapture() throws {
    let calls = FixtureIntegerCounter()
    let backend = try AustinSystemScreenCaptureBackend(
        timeoutMilliseconds: 50,
        captureOperation: { _, _, completion in
            calls.increment()
            completion(nil)
        }
    )
    #expect(throws: AustinFailure.self) {
        try backend.capture(mode: .pickerScoped, expectedSelection: nil)
    }
    #expect(throws: AustinFailure.self) {
        try backend.capture(
            mode: .persistentProgrammatic,
            expectedSelection: pickerSelection()
        )
    }
    #expect(calls.snapshot() == 0)
}

@Test func pickerSelectionStateIsOneShotResettableAndExactlyOneWinner() throws {
    let selection = AustinOneShotCaptureSelection<String>()
    #expect(selection.currentState() == .idle)
    #expect(throws: AustinFailure.self) { try selection.consume() }

    try selection.begin()
    #expect(selection.currentState() == .presenting)
    #expect(throws: AustinFailure.self) { try selection.begin() }
    let winners = FixtureIntegerCounter()
    DispatchQueue.concurrentPerform(iterations: 32) { index in
        do {
            try selection.select("selection-\(index)")
            winners.increment()
        } catch {
            return
        }
    }
    #expect(winners.snapshot() == 1)
    #expect(try selection.consume().hasPrefix("selection-"))
    #expect(selection.currentState() == .consumed)
    #expect(throws: AustinFailure.self) { try selection.consume() }

    try selection.begin()
    try selection.select("must-not-survive-stop")
    selection.revoke()
    #expect(selection.currentState() == .cancelled)
    #expect(throws: AustinFailure.self) { try selection.consume() }

    try selection.begin()
    selection.cancel()
    #expect(selection.currentState() == .cancelled)
    #expect(throws: AustinFailure.self) { try selection.consume() }
    try selection.begin()
    selection.fail()
    #expect(selection.currentState() == .failed)
}

private final class FixtureAppleEventBackend: AustinAppleEventBackend, @unchecked Sendable {
    var result: AustinAppleEventBackendResult = .delivered
    var verification: AustinNativePostcondition = .verified
    var calls = 0

    func send(
        _ adapter: AustinReviewedAppleEvent,
        timeoutMilliseconds: Int64
    ) -> AustinAppleEventBackendResult {
        calls += 1
        return result
    }

    func postcondition(_ adapter: AustinReviewedAppleEvent) -> AustinNativePostcondition {
        verification
    }
}

@Test func appleEventsUseClosedReviewedAdaptersAndTimeoutIsOneShotUnknown() throws {
    #expect(
        Set(AustinReviewedAppleEvent.allCases.map(\.bundleIdentifier))
            == ["com.apple.finder", "com.apple.systempreferences"]
    )
    let backend = FixtureAppleEventBackend()
    backend.result = .timedOut
    let adapter = try AustinAppleEvent(
        backend: backend,
        randomBytes: { Data(repeating: 26, count: 32) }
    )
    let target = try desktopTarget(marker: "d", lifetimeMilliseconds: 4_000)
    #expect(throws: AustinFailure.self) {
        try bindAppleEvent(adapter, target: target, confirmed: false)
    }
    let binding = try bindAppleEvent(adapter, target: target, confirmed: true)
    let envelope = desktopEnvelope(
        target: binding.target,
        operation: .activate,
        route: .appleEvent,
        arguments: ["element_id": binding.elementID]
    )
    #expect(
        adapter.execute(envelope, nowMilliseconds: desktopNow + 1)
            == AustinDesktopOutcome(.unknownOutcome, "apple_event_timeout")
    )
    #expect(
        adapter.execute(envelope, nowMilliseconds: desktopNow + 2)
            == AustinDesktopOutcome(.denied, "apple_event_stale")
    )
    #expect(backend.calls == 1)
}

private final class FixtureShortcutBackend: AustinShortcutBackend, @unchecked Sendable {
    var result: AustinShortcutBackendResult = .openedForReview
    var adapters: [AustinReviewedShortcut] = []

    func openForReview(_ adapter: AustinReviewedShortcut) -> AustinShortcutBackendResult {
        adapters.append(adapter)
        return result
    }
}

@Test func shortcutsAreFixedReviewOnlyConfirmedAndOneShot() throws {
    #expect(AustinReviewedShortcut.allCases == [.reviewCurrentTask])
    let reviewURL = try #require(AustinReviewedShortcut.reviewCurrentTask.reviewURL)
    let components = try #require(URLComponents(url: reviewURL, resolvingAgainstBaseURL: false))
    #expect(components.scheme == "shortcuts")
    #expect(components.host == "open-shortcut")
    #expect(components.queryItems == [
        URLQueryItem(name: "name", value: "Algo CLI Review Current Task")
    ])
    #expect(!reviewURL.absoluteString.contains("run-shortcut"))
    #expect(!reviewURL.absoluteString.contains("input="))
    #expect(!reviewURL.absoluteString.contains("clipboard"))

    let backend = FixtureShortcutBackend()
    let shortcut = try AustinShortcut(
        backend: backend,
        randomBytes: { Data(repeating: 52, count: 32) }
    )
    let target = try desktopTarget(marker: "8", lifetimeMilliseconds: 4_000)
    #expect(throws: AustinFailure.self) {
        try bindShortcut(shortcut, target: target, confirmed: false)
    }
    let binding = try bindShortcut(shortcut, target: target, confirmed: true)
    let envelope = desktopEnvelope(
        target: binding.target,
        operation: .activate,
        route: .shortcut,
        arguments: ["element_id": binding.elementID]
    )
    let dispatcher = AustinDesktopDispatcher(shortcut: shortcut)
    #expect(!dispatcher.isReady(for: envelope, nowMilliseconds: desktopNow + 1))
    let reply = dispatcher.execute(envelope, nowMilliseconds: desktopNow + 1)
    #expect(String(decoding: reply, as: UTF8.self).contains("prepared_binding_mismatch"))
    #expect(backend.adapters.isEmpty)
    #expect(
        shortcut.execute(envelope, nowMilliseconds: desktopNow + 1)
            == AustinDesktopOutcome(.handoffRequired, "shortcut_review_handoff")
    )
    #expect(backend.adapters == [.reviewCurrentTask])
    #expect(!dispatcher.isReady(for: envelope, nowMilliseconds: desktopNow + 2))
    #expect(backend.adapters.count == 1)
}

@Test func shortcutUncertainOpenIsUnknownAndNeverRetried() throws {
    let backend = FixtureShortcutBackend()
    backend.result = .uncertain
    let shortcut = try AustinShortcut(
        backend: backend,
        randomBytes: { Data(repeating: 53, count: 32) }
    )
    let binding = try bindShortcut(
        shortcut,
        target: desktopTarget(marker: "9", lifetimeMilliseconds: 4_000),
        confirmed: true
    )
    let envelope = desktopEnvelope(
        target: binding.target,
        operation: .activate,
        route: .shortcut,
        arguments: ["element_id": binding.elementID]
    )
    #expect(
        shortcut.execute(envelope, nowMilliseconds: desktopNow + 1)
            == AustinDesktopOutcome(.unknownOutcome, "shortcut_open_unknown")
    )
    #expect(
        shortcut.execute(envelope, nowMilliseconds: desktopNow + 2)
            == AustinDesktopOutcome(.denied, "shortcut_stale")
    )
    #expect(backend.adapters.count == 1)
}

private final class FixtureCGEventBackend: AustinCGEventBackend, @unchecked Sendable {
    var context: AustinCoordinateContext
    var permission = true
    var effect: AustinNativeEffect = .performed
    var verification: AustinNativePostcondition = .verified
    var postCalls = 0
    var postedEventCount = 0

    init(context: AustinCoordinateContext) {
        self.context = context
    }

    func currentContext() throws -> AustinCoordinateContext { context }
    func hasPostEventPermission() -> Bool { permission }

    func postClick(x: Int, y: Int) -> AustinNativeEffect {
        postCalls += 1
        postedEventCount += 2
        return effect
    }

    func postcondition() -> AustinNativePostcondition { verification }
}

private func coordinateContext(
    process: AustinDesktopProcess? = nil,
    singleDisplay: Bool = true
) throws -> AustinCoordinateContext {
    try AustinCoordinateContext(
        process: process ?? desktopProcess(),
        displayIdentifier: 1,
        logicalWidth: 1440,
        logicalHeight: 900,
        pixelWidth: 2880,
        pixelHeight: 1800,
        scaleMilli: 2_000,
        singleDisplay: singleDisplay
    )
}

private func coordinateEnvelope(
    target: AustinDesktopTargetBinding,
    x: Int = 100,
    y: Int = 200,
    width: Int = 1440,
    height: Int = 900
) -> AustinVerifiedEnvelope {
    desktopEnvelope(
        target: target,
        operation: .coordinateActivate,
        route: .coordinate,
        arguments: [
            "viewport_height": NSNumber(value: height),
            "viewport_width": NSNumber(value: width),
            "x": NSNumber(value: x),
            "y": NSNumber(value: y),
        ]
    )
}

@Test func coordinateFallbackRevalidatesPermissionFocusGeometryAndPostsExactlyTwoEvents() throws {
    let backend = FixtureCGEventBackend(context: try coordinateContext())
    let adapter = AustinCGEvent(backend: backend)
    let target = try desktopTarget(marker: "e", lifetimeMilliseconds: 1_500)
    #expect(throws: AustinFailure.self) {
        try bindCoordinate(adapter, target: target, confirmed: false)
    }
    let binding = try bindCoordinate(adapter, target: target)
    let envelope = coordinateEnvelope(target: binding.target)
    #expect(
        adapter.execute(envelope, nowMilliseconds: desktopNow + 1)
            == AustinDesktopOutcome(.succeeded, "coordinate_postcondition_verified")
    )
    #expect(backend.postCalls == 1)
    #expect(backend.postedEventCount == 2)
    #expect(
        adapter.execute(envelope, nowMilliseconds: desktopNow + 2)
            == AustinDesktopOutcome(.denied, "coordinate_binding_stale")
    )

    let deniedBackend = FixtureCGEventBackend(context: try coordinateContext())
    deniedBackend.permission = false
    let denied = AustinCGEvent(backend: deniedBackend)
    let deniedTarget = try desktopTarget(marker: "f", lifetimeMilliseconds: 1_500)
    let deniedBinding = try bindCoordinate(denied, target: deniedTarget)
    #expect(
        denied.execute(
            coordinateEnvelope(target: deniedBinding.target),
            nowMilliseconds: desktopNow + 1
        ) == AustinDesktopOutcome(.denied, "coordinate_permission_denied")
    )
    #expect(deniedBackend.postCalls == 0)
}

@Test func coordinateFallbackRejectsFocusDisplayAndGeometryRaces() throws {
    let focusBackend = FixtureCGEventBackend(context: try coordinateContext())
    let focus = AustinCGEvent(backend: focusBackend)
    let focusTarget = try desktopTarget(marker: "1", lifetimeMilliseconds: 1_500)
    let focusBinding = try bindCoordinate(focus, target: focusTarget)
    focusBackend.context = try coordinateContext(process: desktopProcess(startSeconds: 200))
    #expect(
        focus.execute(
            coordinateEnvelope(target: focusBinding.target),
            nowMilliseconds: desktopNow + 1
        ) == AustinDesktopOutcome(.denied, "coordinate_context_changed")
    )
    #expect(focusBackend.postCalls == 0)

    let geometryBackend = FixtureCGEventBackend(context: try coordinateContext())
    let geometry = AustinCGEvent(backend: geometryBackend)
    let geometryTarget = try desktopTarget(marker: "2", lifetimeMilliseconds: 1_500)
    let geometryBinding = try bindCoordinate(geometry, target: geometryTarget)
    #expect(
        !geometry.supports(
            coordinateEnvelope(target: geometryBinding.target, width: 1920),
            nowMilliseconds: desktopNow + 1
        )
    )
    #expect(
        geometry.execute(
            coordinateEnvelope(target: geometryBinding.target, width: 1920),
            nowMilliseconds: desktopNow + 1
        ) == AustinDesktopOutcome(.denied, "coordinate_geometry_changed")
    )
    #expect(geometryBackend.postCalls == 0)

    let multiple = FixtureCGEventBackend(context: try coordinateContext(singleDisplay: false))
    let multipleAdapter = AustinCGEvent(backend: multiple)
    #expect(throws: AustinFailure.self) {
        try bindCoordinate(
            multipleAdapter,
            target: desktopTarget(marker: "3", lifetimeMilliseconds: 1_500),
            confirmed: true
        )
    }


    let conflictBackend = FixtureCGEventBackend(context: try coordinateContext())
    let conflict = AustinCGEvent(backend: conflictBackend)
    let conflictTarget = try desktopTarget(marker: "7", lifetimeMilliseconds: 1_500)
    _ = try bindCoordinate(conflict, target: conflictTarget)
    #expect(throws: AustinFailure.self) {
        try bindCoordinate(
            conflict,
            target: conflictTarget,
            x: 101,
            y: 200
        )
    }
}

@Test func disabledDispatcherAndShortcutRouteRemainFailClosed() throws {
    let target = try desktopTarget(marker: "4", lifetimeMilliseconds: 1_000)
    let shortcut = desktopEnvelope(
        target: target,
        operation: .activate,
        route: .shortcut,
        arguments: ["element_id": "hmac-sha256:" + String(repeating: "5", count: 64)]
    )
    let dispatcher = AustinDesktopDispatcher.disabledFoundation()
    #expect(!dispatcher.isReady(for: shortcut, nowMilliseconds: desktopNow + 1))
}
