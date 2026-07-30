@testable import AustinCore
@testable import AustinDesktopCore
import Foundation
import Testing

private let thomasNow: Int64 = 1_800_000_000_000

private func thomasPreparation(
    operation: AustinOperation = .activate,
    dataClass: AustinDataClass = .structural,
    route: AustinRoute = .ax,
    selector: String = "focused_element",
    arguments: [String: Any] = [:],
    preparationID: String = "00000000-0000-4000-8000-000000000601",
    requestID: String = "00000000-0000-4000-8000-000000000101"
) -> AustinVerifiedPreparation {
    AustinVerifiedPreparation(
        preparationID: preparationID,
        requestID: requestID,
        subjectID: "runtime.operator",
        operation: operation,
        dataClass: dataClass,
        route: route,
        selector: selector,
        arguments: arguments,
        preparationDigest: "sha256:" + String(repeating: "a", count: 64),
        issuedAtMilliseconds: thomasNow - 100,
        expiresAtMilliseconds: thomasNow + 30_000
    )
}

private final class ThomasConfirmationBackend: AustinConfirmationBackend,
    @unchecked Sendable
{
    var result: AustinConfirmationResult = .confirmed
    var actions: [AustinConfirmationAction] = []

    @MainActor
    func confirm(
        action: AustinConfirmationAction,
        timeoutMilliseconds: Int64
    ) -> AustinConfirmationResult {
        actions.append(action)
        return result
    }
}

private final class ThomasAXBackend: AustinAccessibilityBackend, @unchecked Sendable {
    var currentStateCalls = 0
    var performCalls = 0

    func currentState() throws -> AustinAXElementState {
        currentStateCalls += 1
        return try AustinAXElementState(
            process: AustinDesktopProcess(
                processIdentifier: 4242,
                processStartSeconds: 100,
                processStartMicroseconds: 25,
                bundleIdentifier: "com.example.ThomasFixture"
            ),
            windowFingerprint: 99,
            role: "AXButton",
            enabled: true,
            focusedWindow: true
        )
    }

    func perform(
        operation: AustinOperation,
        arguments: [String: Any]
    ) -> AustinNativeEffect {
        performCalls += 1
        return .performed
    }

    func postcondition(
        operation: AustinOperation,
        before: AustinAXElementState
    ) -> AustinNativePostcondition {
        .verified
    }
}

private func thomasBinding(
    reply: Data
) throws -> (AustinDesktopTargetBinding, [String: Any]) {
    let root = try AustinJSON.decodeCanonicalObject(reply)
    let target = try AustinJSON.exactObject(
        root["target"],
        keys: ["epoch", "fencing_token", "kind", "revision", "target_id"],
        label: "target"
    )
    let snapshot = try AustinJSON.exactObject(
        root["snapshot"],
        keys: [
            "epoch", "fencing_token", "observed_at_ms", "revision", "sequence",
            "snapshot_id", "target_id",
        ],
        label: "snapshot"
    )
    let expires = try AustinJSON.integer(
        root["binding_expires_at_ms"],
        label: "binding_expires_at_ms",
        minimum: 1
    )
    let binding = try AustinDesktopTargetBinding(
        targetID: try AustinJSON.string(target["target_id"], label: "target_id"),
        targetEpoch: try AustinJSON.integer(target["epoch"], label: "target_epoch", minimum: 1),
        targetRevision: try AustinJSON.string(target["revision"], label: "target_revision"),
        fencingToken: try AustinJSON.integer(
            target["fencing_token"], label: "target_fence", minimum: 1
        ),
        snapshotID: try AustinJSON.string(snapshot["snapshot_id"], label: "snapshot_id"),
        snapshotSequence: try AustinJSON.integer(
            snapshot["sequence"], label: "snapshot_sequence", minimum: 1
        ),
        observedAtMilliseconds: try AustinJSON.integer(
            snapshot["observed_at_ms"], label: "snapshot_time"
        ),
        expiresAtMilliseconds: expires
    )
    guard let arguments = root["arguments"] as? [String: Any] else {
        throw AustinFailure("arguments")
    }
    return (binding, arguments)
}

private func thomasEnvelope(
    preparation: AustinVerifiedPreparation,
    reply: Data,
    requestID: String? = nil,
    subjectID: String = "runtime.operator",
    operation: AustinOperation? = nil,
    dataClass: AustinDataClass? = nil,
    route: AustinRoute? = nil,
    arguments: [String: Any]? = nil
) throws -> AustinVerifiedEnvelope {
    let (target, replyArguments) = try thomasBinding(reply: reply)
    return AustinVerifiedEnvelope(
        requestID: requestID ?? preparation.requestID,
        subjectID: subjectID,
        permitID: "00000000-0000-4000-8000-000000000501",
        requestDigest: "sha256:" + String(repeating: "d", count: 64),
        targetID: target.targetID,
        targetEpoch: target.targetEpoch,
        targetRevision: target.targetRevision,
        fencingToken: target.fencingToken,
        snapshotID: target.snapshotID,
        snapshotSequence: target.snapshotSequence,
        operation: operation ?? preparation.operation,
        dataClass: dataClass ?? preparation.dataClass,
        route: route ?? preparation.route,
        arguments: arguments ?? replyArguments,
        expiresAtMilliseconds: target.expiresAtMilliseconds
    )
}

@Test @MainActor func thomasCoordinatorPreparesAXAndDispatcherConsumesExactBindingOnce() throws {
    let confirmation = ThomasConfirmationBackend()
    let backend = ThomasAXBackend()
    let accessibility = try AustinAccessibility(
        randomBytes: { Data(repeating: 7, count: 32) }
    )
    let coordinator = try AustinThomasBindingCoordinator(
        confirmation: confirmation,
        accessibility: accessibility,
        accessibilityDiscovery: { backend },
        nowMilliseconds: { thomasNow },
        randomBytes: { Data(repeating: 8, count: 32) }
    )
    let preparation = thomasPreparation()
    let reply = try coordinator.prepare(preparation)
    let text = String(decoding: reply, as: UTF8.self)
    #expect(!text.contains("ThomasFixture"))
    #expect(!text.contains("AXButton"))
    #expect(!text.contains("bundle"))
    #expect(confirmation.actions == [.accessibilityActivate])
    #expect(backend.currentStateCalls == 1)

    let envelope = try thomasEnvelope(preparation: preparation, reply: reply)
    let dispatcher = AustinDesktopDispatcher(
        accessibility: accessibility,
        preparationCoordinator: coordinator
    )
    #expect(dispatcher.isReady(for: envelope, nowMilliseconds: thomasNow + 1))
    let outcome = try AustinJSON.decodeCanonicalObject(
        dispatcher.execute(envelope, nowMilliseconds: thomasNow + 1)
    )
    #expect(outcome["status"] as? String == "succeeded")
    #expect(outcome["reason_code"] as? String == "ax_postcondition_verified")
    #expect(backend.performCalls == 1)
    #expect(!dispatcher.isReady(for: envelope, nowMilliseconds: thomasNow + 2))
    #expect(backend.performCalls == 1)
}

@Test @MainActor func thomasCoordinatorCrossBindsFutureExecutionBeforePermitConsumption() throws {
    let confirmation = ThomasConfirmationBackend()
    let backend = ThomasAXBackend()
    let accessibility = try AustinAccessibility(
        randomBytes: { Data(repeating: 9, count: 32) }
    )
    let coordinator = try AustinThomasBindingCoordinator(
        confirmation: confirmation,
        accessibility: accessibility,
        accessibilityDiscovery: { backend },
        nowMilliseconds: { thomasNow },
        randomBytes: { Data(repeating: 10, count: 32) }
    )
    let preparation = thomasPreparation()
    let reply = try coordinator.prepare(preparation)
    let (_, arguments) = try thomasBinding(reply: reply)
    let changedElement = [
        "element_id": "hmac-sha256:" + String(repeating: "f", count: 64)
    ]
    let mismatches = try [
        thomasEnvelope(
            preparation: preparation,
            reply: reply,
            requestID: "00000000-0000-4000-8000-000000000102"
        ),
        thomasEnvelope(
            preparation: preparation,
            reply: reply,
            subjectID: "runtime.attacker"
        ),
        thomasEnvelope(
            preparation: preparation,
            reply: reply,
            operation: .selectOption
        ),
        thomasEnvelope(
            preparation: preparation,
            reply: reply,
            dataClass: .private
        ),
        thomasEnvelope(
            preparation: preparation,
            reply: reply,
            route: .shortcut
        ),
        thomasEnvelope(
            preparation: preparation,
            reply: reply,
            arguments: changedElement
        ),
    ]
    for mismatch in mismatches {
        #expect(!coordinator.supports(mismatch, nowMilliseconds: thomasNow + 1))
    }
    #expect(Set(arguments.keys) == ["element_id"])
    let exact = try thomasEnvelope(preparation: preparation, reply: reply)
    #expect(coordinator.supports(exact, nowMilliseconds: thomasNow + 1))
    #expect(backend.performCalls == 0)
}

@Test @MainActor func thomasCoordinatorDenialExpiryAndUnsupportedTextNeverDiscover() throws {
    let confirmation = ThomasConfirmationBackend()
    confirmation.result = .denied
    let backend = ThomasAXBackend()
    let accessibility = try AustinAccessibility(
        randomBytes: { Data(repeating: 11, count: 32) }
    )
    let coordinator = try AustinThomasBindingCoordinator(
        confirmation: confirmation,
        accessibility: accessibility,
        accessibilityDiscovery: { backend },
        nowMilliseconds: { thomasNow },
        randomBytes: { Data(repeating: 12, count: 32) }
    )
    #expect(throws: AustinFailure.self) {
        try coordinator.prepare(thomasPreparation())
    }
    #expect(backend.currentStateCalls == 0)

    #expect(throws: AustinFailure.self) {
        try coordinator.prepare(
            thomasPreparation(
                operation: .inputText,
                dataClass: .private,
                route: .ax,
                arguments: ["replace": true, "text": "never-forwarded"]
            )
        )
    }
    #expect(confirmation.actions == [.accessibilityActivate])
    #expect(backend.currentStateCalls == 0)

    let expiredConfirmation = ThomasConfirmationBackend()
    let expiredCoordinator = try AustinThomasBindingCoordinator(
        confirmation: expiredConfirmation,
        accessibility: accessibility,
        accessibilityDiscovery: { backend },
        nowMilliseconds: { thomasNow + 30_000 },
        randomBytes: { Data(repeating: 20, count: 32) }
    )
    #expect(throws: AustinFailure.self) {
        try expiredCoordinator.prepare(thomasPreparation())
    }
    #expect(expiredConfirmation.actions.isEmpty)
    #expect(backend.currentStateCalls == 0)
}

private final class ThomasCoordinateBackend: AustinCGEventBackend, @unchecked Sendable {
    var postCalls = 0

    func currentContext() throws -> AustinCoordinateContext {
        try AustinCoordinateContext(
            process: AustinDesktopProcess(
                processIdentifier: 5252,
                processStartSeconds: 200,
                processStartMicroseconds: 30,
                bundleIdentifier: "com.example.CoordinateFixture"
            ),
            displayIdentifier: 1,
            logicalWidth: 1440,
            logicalHeight: 900,
            pixelWidth: 2880,
            pixelHeight: 1800,
            scaleMilli: 2_000
        )
    }

    func hasPostEventPermission() -> Bool { true }
    func postClick(x: Int, y: Int) -> AustinNativeEffect {
        postCalls += 1
        return .performed
    }
    func postcondition() -> AustinNativePostcondition { .verified }
}

@Test @MainActor func thomasCoordinatorDerivesCoordinateViewportAndRejectsGeometrySwap() throws {
    let confirmation = ThomasConfirmationBackend()
    let backend = ThomasCoordinateBackend()
    let coordinate = AustinCGEvent(backend: backend)
    let coordinator = try AustinThomasBindingCoordinator(
        confirmation: confirmation,
        cgEvent: coordinate,
        nowMilliseconds: { thomasNow },
        randomBytes: { Data(repeating: 13, count: 32) }
    )
    let preparation = thomasPreparation(
        operation: .coordinateActivate,
        route: .coordinate,
        selector: "frontmost_point",
        arguments: ["x": 100, "y": 200]
    )
    let reply = try coordinator.prepare(preparation)
    let (_, arguments) = try thomasBinding(reply: reply)
    #expect((arguments["viewport_width"] as? NSNumber)?.intValue == 1440)
    #expect((arguments["viewport_height"] as? NSNumber)?.intValue == 900)
    let exact = try thomasEnvelope(preparation: preparation, reply: reply)
    var changed = arguments
    changed["viewport_width"] = 1439
    let mismatch = try thomasEnvelope(
        preparation: preparation,
        reply: reply,
        arguments: changed
    )
    #expect(!coordinator.supports(mismatch, nowMilliseconds: thomasNow + 1))
    let dispatcher = AustinDesktopDispatcher(
        cgEvent: coordinate,
        preparationCoordinator: coordinator
    )
    let outcome = try AustinJSON.decodeCanonicalObject(
        dispatcher.execute(exact, nowMilliseconds: thomasNow + 1)
    )
    #expect(outcome["status"] as? String == "succeeded")
    #expect(backend.postCalls == 1)
}

private final class ThomasCaptureBackend: AustinScreenCaptureBackend, @unchecked Sendable {
    var captures = 0
    func capture(
        mode: AustinCaptureMode,
        expectedSelection: AustinCaptureSelectionIdentity?
    ) throws -> AustinPixelFrame {
        guard mode == .persistentProgrammatic, expectedSelection == nil else {
            throw AustinFailure("capture_fixture_mode")
        }
        captures += 1
        return try AustinPixelFrame(
            width: 2,
            height: 1,
            rgbaBytes: [1, 2, 3, 255, 4, 5, 6, 255]
        )
    }
}

private final class ThomasCaptureSink: AustinRedactedCaptureSink, @unchecked Sendable {
    var frames: [AustinPixelFrame] = []
    func acceptRedacted(_ frame: AustinPixelFrame) throws { frames.append(frame) }
}

private final class ThomasRedactionClassifier: AustinCaptureRedactionClassifying,
    @unchecked Sendable
{
    var preflightCalls = 0
    var classificationCalls = 0

    func preflight(for preparation: AustinVerifiedPreparation) throws {
        preflightCalls += 1
    }

    func redactions(
        for frame: AustinPixelFrame,
        context: AustinCaptureRedactionContext
    ) throws -> [AustinCaptureRedaction] {
        classificationCalls += 1
        return [try AustinCaptureRedaction(x: 0, y: 0, width: 1, height: 1)]
    }
}

private final class ThomasEmptyRedactionClassifier: AustinCaptureRedactionClassifying,
    @unchecked Sendable
{
    var preflightCalls = 0
    var classificationCalls = 0

    func preflight(for preparation: AustinVerifiedPreparation) throws {
        preflightCalls += 1
    }

    func redactions(
        for frame: AustinPixelFrame,
        context: AustinCaptureRedactionContext
    ) throws -> [AustinCaptureRedaction] {
        classificationCalls += 1
        return []
    }
}

private final class ThomasUnavailableRedactionClassifier: AustinCaptureRedactionClassifying,
    @unchecked Sendable
{
    var preflightCalls = 0

    func preflight(for preparation: AustinVerifiedPreparation) throws {
        preflightCalls += 1
        throw AustinFailure("capture_classifier_unavailable")
    }

    func redactions(
        for frame: AustinPixelFrame,
        context: AustinCaptureRedactionContext
    ) throws -> [AustinCaptureRedaction] {
        throw AustinFailure("capture_classifier_unavailable")
    }
}

@Test @MainActor func thomasCoordinatorPreflightsAndLocallyRedactsPersistentCapture() throws {
    let deniedConfirmation = ThomasConfirmationBackend()
    let deniedClassifier = ThomasRedactionClassifier()
    let deniedCapture = try AustinScreenCapture(
        backend: ThomasCaptureBackend(),
        sink: ThomasCaptureSink(),
        randomBytes: { Data(repeating: 14, count: 32) }
    )
    let deniedCoordinator = try AustinThomasBindingCoordinator(
        confirmation: deniedConfirmation,
        capture: deniedCapture,
        capturePermissionPreflight: { false },
        captureRedactionClassifier: deniedClassifier,
        nowMilliseconds: { thomasNow },
        randomBytes: { Data(repeating: 15, count: 32) }
    )
    let preparation = thomasPreparation(
        operation: .observe,
        route: .screenshot,
        selector: "persistent_programmatic"
    )
    #expect(throws: AustinFailure.self) {
        try deniedCoordinator.prepare(preparation)
    }
    #expect(deniedConfirmation.actions.isEmpty)
    #expect(deniedClassifier.preflightCalls == 0)
    #expect(deniedClassifier.classificationCalls == 0)

    let unavailableConfirmation = ThomasConfirmationBackend()
    let unavailableClassifier = ThomasUnavailableRedactionClassifier()
    let unavailableCapture = try AustinScreenCapture(
        backend: ThomasCaptureBackend(),
        sink: ThomasCaptureSink(),
        randomBytes: { Data(repeating: 21, count: 32) }
    )
    let unavailableCoordinator = try AustinThomasBindingCoordinator(
        confirmation: unavailableConfirmation,
        capture: unavailableCapture,
        capturePermissionPreflight: { true },
        captureRedactionClassifier: unavailableClassifier,
        nowMilliseconds: { thomasNow },
        randomBytes: { Data(repeating: 22, count: 32) }
    )
    #expect(throws: AustinFailure.self) {
        try unavailableCoordinator.prepare(preparation)
    }
    #expect(unavailableClassifier.preflightCalls == 1)
    #expect(unavailableConfirmation.actions.isEmpty)

    let emptyConfirmation = ThomasConfirmationBackend()
    let emptyClassifier = ThomasEmptyRedactionClassifier()
    let emptyBackend = ThomasCaptureBackend()
    let emptySink = ThomasCaptureSink()
    let emptyCapture = try AustinScreenCapture(
        backend: emptyBackend,
        sink: emptySink,
        randomBytes: { Data(repeating: 23, count: 32) }
    )
    let emptyCoordinator = try AustinThomasBindingCoordinator(
        confirmation: emptyConfirmation,
        capture: emptyCapture,
        capturePermissionPreflight: { true },
        captureRedactionClassifier: emptyClassifier,
        nowMilliseconds: { thomasNow },
        randomBytes: { Data(repeating: 24, count: 32) }
    )
    let emptyReply = try emptyCoordinator.prepare(preparation)
    let emptyEnvelope = try thomasEnvelope(preparation: preparation, reply: emptyReply)
    let emptyDispatcher = AustinDesktopDispatcher(
        capture: emptyCapture,
        preparationCoordinator: emptyCoordinator
    )
    let emptyOutcome = try AustinJSON.decodeCanonicalObject(
        emptyDispatcher.execute(emptyEnvelope, nowMilliseconds: thomasNow + 1)
    )
    #expect(emptyOutcome["status"] as? String == "failed")
    #expect(emptyOutcome["reason_code"] as? String == "capture_redaction_failed")
    #expect(emptyClassifier.preflightCalls == 1)
    #expect(emptyClassifier.classificationCalls == 1)
    #expect(emptyBackend.captures == 1)
    #expect(emptySink.frames.isEmpty)

    let confirmation = ThomasConfirmationBackend()
    let classifier = ThomasRedactionClassifier()
    let backend = ThomasCaptureBackend()
    let sink = ThomasCaptureSink()
    let capture = try AustinScreenCapture(
        backend: backend,
        sink: sink,
        randomBytes: { Data(repeating: 16, count: 32) }
    )
    let coordinator = try AustinThomasBindingCoordinator(
        confirmation: confirmation,
        capture: capture,
        capturePermissionPreflight: { true },
        captureRedactionClassifier: classifier,
        nowMilliseconds: { thomasNow },
        randomBytes: { Data(repeating: 17, count: 32) }
    )
    let reply = try coordinator.prepare(preparation)
    let envelope = try thomasEnvelope(preparation: preparation, reply: reply)
    let dispatcher = AustinDesktopDispatcher(
        capture: capture,
        preparationCoordinator: coordinator
    )
    let outcome = try AustinJSON.decodeCanonicalObject(
        dispatcher.execute(envelope, nowMilliseconds: thomasNow + 1)
    )
    #expect(outcome["reason_code"] as? String == "capture_persistent_redacted")
    #expect(classifier.preflightCalls == 1)
    #expect(classifier.classificationCalls == 1)
    #expect(backend.captures == 1)
    #expect(sink.frames.first?.rgbaBytes == [0, 0, 0, 255, 4, 5, 6, 255])
}

private final class ThomasAppleBackend: AustinAppleEventBackend, @unchecked Sendable {
    var calls = 0
    func send(
        _ adapter: AustinReviewedAppleEvent,
        timeoutMilliseconds: Int64
    ) -> AustinAppleEventBackendResult {
        calls += 1
        return .delivered
    }
    func postcondition(_ adapter: AustinReviewedAppleEvent) -> AustinNativePostcondition {
        .verified
    }
}

@Test @MainActor func thomasCoordinatorKeepsReviewedAppleSelectorOutOfExecutionArguments() throws {
    let confirmation = ThomasConfirmationBackend()
    let backend = ThomasAppleBackend()
    let appleEvent = try AustinAppleEvent(
        backend: backend,
        randomBytes: { Data(repeating: 18, count: 32) }
    )
    let coordinator = try AustinThomasBindingCoordinator(
        confirmation: confirmation,
        appleEvent: appleEvent,
        nowMilliseconds: { thomasNow },
        randomBytes: { Data(repeating: 19, count: 32) }
    )
    let preparation = thomasPreparation(
        route: .appleEvent,
        selector: "activate_finder"
    )
    let reply = try coordinator.prepare(preparation)
    let (_, arguments) = try thomasBinding(reply: reply)
    #expect(Set(arguments.keys) == ["element_id"])
    #expect(!String(decoding: reply, as: UTF8.self).contains("com.apple.finder"))
    let envelope = try thomasEnvelope(preparation: preparation, reply: reply)
    let dispatcher = AustinDesktopDispatcher(
        appleEvent: appleEvent,
        preparationCoordinator: coordinator
    )
    let outcome = try AustinJSON.decodeCanonicalObject(
        dispatcher.execute(envelope, nowMilliseconds: thomasNow + 1)
    )
    #expect(outcome["status"] as? String == "succeeded")
    #expect(backend.calls == 1)
}
