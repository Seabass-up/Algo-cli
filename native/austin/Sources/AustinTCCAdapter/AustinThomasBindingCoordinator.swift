import AustinCore
import Foundation

private final class AustinThomasPreparedRecord: @unchecked Sendable {
    let preparationID: String
    let requestID: String
    let subjectID: String
    let operation: AustinOperation
    let dataClass: AustinDataClass
    let route: AustinRoute
    let target: AustinDesktopTargetBinding
    let argumentsDigest: String
    var consumed = false

    init(
        preparation: AustinVerifiedPreparation,
        target: AustinDesktopTargetBinding,
        argumentsDigest: String
    ) {
        preparationID = preparation.preparationID
        requestID = preparation.requestID
        subjectID = preparation.subjectID
        operation = preparation.operation
        dataClass = preparation.dataClass
        route = preparation.route
        self.target = target
        self.argumentsDigest = argumentsDigest
    }

    func matches(_ envelope: AustinVerifiedEnvelope, nowMilliseconds: Int64) -> Bool {
        guard let digest = try? AustinJSON.digest(envelope.arguments) else { return false }
        return !consumed
            && requestID == envelope.requestID
            && subjectID == envelope.subjectID
            && operation == envelope.operation
            && dataClass == envelope.dataClass
            && route == envelope.route
            && target.matches(envelope)
            && argumentsDigest == digest
            && nowMilliseconds >= target.observedAtMilliseconds
            && nowMilliseconds < target.expiresAtMilliseconds
    }
}

/// Samuel-authorized, fixed-confirmation, one-target coordinator. Preparation
/// can discover and bind a native target, but only the existing target-bound
/// execution authority can later consume it.
public final class AustinThomasBindingCoordinator: @unchecked Sendable {
    public static let maximumPreparedBindings = 128

    public typealias AccessibilityDiscovery = () throws -> AustinAccessibilityBackend
    public typealias CapturePermissionPreflight = () -> Bool

    private let confirmation: AustinConfirmationBackend
    private let accessibility: AustinAccessibility?
    private let accessibilityDiscovery: AccessibilityDiscovery?
    private let capture: AustinScreenCapture?
    private let capturePermissionPreflight: CapturePermissionPreflight?
    private let captureRedactionClassifier: AustinCaptureRedactionClassifying?
    private let appleEvent: AustinAppleEvent?
    private let shortcut: AustinShortcut?
    private let cgEvent: AustinCGEvent?
    private let nowMilliseconds: @Sendable () -> Int64
    private let issuer: AustinOpaqueTokenIssuer
    private let lock = NSLock()
    private var generation: Int64 = 0
    private var records: [String: AustinThomasPreparedRecord] = [:]

    public init(
        confirmation: AustinConfirmationBackend,
        accessibility: AustinAccessibility? = nil,
        accessibilityDiscovery: AccessibilityDiscovery? = nil,
        capture: AustinScreenCapture? = nil,
        capturePermissionPreflight: CapturePermissionPreflight? = nil,
        captureRedactionClassifier: AustinCaptureRedactionClassifying? = nil,
        appleEvent: AustinAppleEvent? = nil,
        shortcut: AustinShortcut? = nil,
        cgEvent: AustinCGEvent? = nil,
        nowMilliseconds: @escaping @Sendable () -> Int64 = AustinClock.nowMilliseconds,
        randomBytes: @escaping () throws -> Data = AustinSession.secureRandomBytes
    ) throws {
        self.confirmation = confirmation
        self.accessibility = accessibility
        self.accessibilityDiscovery = accessibilityDiscovery
        self.capture = capture
        self.capturePermissionPreflight = capturePermissionPreflight
        self.captureRedactionClassifier = captureRedactionClassifier
        self.appleEvent = appleEvent
        self.shortcut = shortcut
        self.cgEvent = cgEvent
        self.nowMilliseconds = nowMilliseconds
        issuer = try AustinOpaqueTokenIssuer(randomBytes: randomBytes)
    }

    public func isEnabled(for preparation: AustinVerifiedPreparation) -> Bool {
        switch preparation.route {
        case .ax:
            accessibility != nil && accessibilityDiscovery != nil
        case .screenshot:
            capture != nil
                && capturePermissionPreflight != nil
                && captureRedactionClassifier != nil
        case .appleEvent:
            appleEvent != nil
        case .shortcut:
            shortcut != nil
        case .coordinate:
            cgEvent != nil
        case .connector, .dom, .handoff:
            false
        }
    }

    @MainActor
    public func prepare(_ preparation: AustinVerifiedPreparation) throws -> Data {
        guard isEnabled(for: preparation) else {
            throw AustinFailure("preparation_route_disabled")
        }
        let before = try checkedNow()
        guard before >= preparation.issuedAtMilliseconds,
              before < preparation.expiresAtMilliseconds
        else {
            throw AustinFailure("preparation_expired")
        }

        // Permission and classifier availability checks occur before
        // confirmation so the user is not prompted for a route the adapter
        // cannot complete. Pixel-dependent classification necessarily occurs
        // after the bounded frame is acquired and before any sink sees it.
        if preparation.route == .screenshot {
            guard preparation.selector == AustinCaptureMode.persistentProgrammatic.rawValue,
                  capturePermissionPreflight?() == true,
                  let captureRedactionClassifier
            else {
                throw AustinFailure("capture_permission_denied")
            }
            try captureRedactionClassifier.preflight(for: preparation)
        }

        let action = try confirmationAction(for: preparation)
        let remaining = preparation.expiresAtMilliseconds - before
        let timeout = min(AustinSystemConfirmationBackend.maximumTimeoutMilliseconds, remaining)
        guard timeout > 0 else { throw AustinFailure("preparation_expired") }
        switch confirmation.confirm(action: action, timeoutMilliseconds: timeout) {
        case .confirmed:
            break
        case .denied:
            throw AustinFailure("confirmation_denied")
        case .timedOut:
            throw AustinFailure("confirmation_timeout")
        case .unavailable:
            throw AustinFailure("confirmation_unavailable")
        }

        let confirmedAt = try checkedNow()
        guard confirmedAt >= before,
              confirmedAt < preparation.expiresAtMilliseconds
        else {
            throw AustinFailure("preparation_expired")
        }
        let confirmationLease = try AustinThomasConfirmationLease(
            preparationID: preparation.preparationID,
            action: action,
            issuedAtMilliseconds: confirmedAt
        )

        let target: AustinDesktopTargetBinding
        var arguments = preparation.arguments
        switch preparation.route {
        case .ax:
            guard let accessibility, let accessibilityDiscovery else {
                throw AustinFailure("preparation_route_disabled")
            }
            let backend = try accessibilityDiscovery()
            let binding = try accessibility.bind(
                backend: backend,
                operation: preparation.operation,
                confirmation: confirmationLease,
                preparationID: preparation.preparationID,
                nowMilliseconds: confirmedAt
            )
            target = binding.target
            arguments["element_id"] = binding.elementID
        case .appleEvent:
            guard let appleEvent,
                  let adapter = AustinReviewedAppleEvent(rawValue: preparation.selector)
            else {
                throw AustinFailure("preparation_selector")
            }
            let issued = try issueTarget(
                preparation: preparation,
                nowMilliseconds: confirmedAt,
                lifetimeMilliseconds: AustinAppleEvent.maximumBindingLifetimeMilliseconds
            )
            let binding = try appleEvent.bind(
                target: issued,
                adapter: adapter,
                confirmation: confirmationLease,
                preparationID: preparation.preparationID,
                nowMilliseconds: confirmedAt
            )
            target = binding.target
            arguments["element_id"] = binding.elementID
        case .shortcut:
            guard let shortcut,
                  let adapter = AustinReviewedShortcut(rawValue: preparation.selector)
            else {
                throw AustinFailure("preparation_selector")
            }
            let issued = try issueTarget(
                preparation: preparation,
                nowMilliseconds: confirmedAt,
                lifetimeMilliseconds: AustinShortcut.maximumBindingLifetimeMilliseconds
            )
            let binding = try shortcut.bind(
                target: issued,
                adapter: adapter,
                confirmation: confirmationLease,
                preparationID: preparation.preparationID,
                nowMilliseconds: confirmedAt
            )
            target = binding.target
            arguments["element_id"] = binding.elementID
        case .coordinate:
            guard let cgEvent,
                  let x = (preparation.arguments["x"] as? NSNumber)?.intValue,
                  let y = (preparation.arguments["y"] as? NSNumber)?.intValue
            else {
                throw AustinFailure("preparation_arguments")
            }
            let issued = try issueTarget(
                preparation: preparation,
                nowMilliseconds: confirmedAt,
                lifetimeMilliseconds: AustinCGEvent.maximumBindingLifetimeMilliseconds
            )
            let binding = try cgEvent.bind(
                target: issued,
                x: x,
                y: y,
                confirmation: confirmationLease,
                preparationID: preparation.preparationID,
                nowMilliseconds: confirmedAt
            )
            target = binding.target
            arguments["viewport_height"] = binding.context.logicalHeight
            arguments["viewport_width"] = binding.context.logicalWidth
        case .screenshot:
            guard let capture, let captureRedactionClassifier else {
                throw AustinFailure("preparation_route_disabled")
            }
            let issued = try issueTarget(
                preparation: preparation,
                nowMilliseconds: confirmedAt,
                lifetimeMilliseconds: AustinScreenCapture.maximumLeaseLifetimeMilliseconds
            )
            let lease = try capture.issuePersistentLease(
                target: issued,
                screenRecordingPermissionGranted: true,
                confirmation: confirmationLease,
                preparationID: preparation.preparationID,
                redactionClassifier: captureRedactionClassifier,
                redactionContext: AustinCaptureRedactionContext(preparation: preparation),
                nowMilliseconds: confirmedAt
            )
            target = lease.target
        case .connector, .dom, .handoff:
            throw AustinFailure("preparation_route_disabled")
        }

        try register(preparation: preparation, target: target, arguments: arguments)
        return try encodeReply(preparation: preparation, target: target, arguments: arguments)
    }

    public func supports(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        prune(nowMilliseconds: nowMilliseconds)
        return records[envelope.targetID]?.matches(
            envelope,
            nowMilliseconds: nowMilliseconds
        ) == true
    }

    func claimExecution(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) throws {
        lock.lock()
        defer { lock.unlock() }
        prune(nowMilliseconds: nowMilliseconds)
        guard let record = records[envelope.targetID],
              record.matches(envelope, nowMilliseconds: nowMilliseconds)
        else {
            throw AustinFailure("prepared_binding_mismatch")
        }
        record.consumed = true
    }

    private func confirmationAction(
        for preparation: AustinVerifiedPreparation
    ) throws -> AustinConfirmationAction {
        switch (preparation.operation, preparation.route) {
        case (.activate, .ax): .accessibilityActivate
        case (.selectOption, .ax): .accessibilitySelect
        case (.scroll, .ax): .accessibilityScroll
        case (.coordinateActivate, .coordinate): .coordinateActivate
        case (.observe, .screenshot): .persistentCapture
        case (.activate, .appleEvent): .appleEventActivate
        case (.activate, .shortcut): .shortcutReview
        default: throw AustinFailure("preparation_route_operation")
        }
    }

    private func issueTarget(
        preparation: AustinVerifiedPreparation,
        nowMilliseconds: Int64,
        lifetimeMilliseconds: Int64
    ) throws -> AustinDesktopTargetBinding {
        guard lifetimeMilliseconds > 0,
              nowMilliseconds >= 0,
              nowMilliseconds < preparation.expiresAtMilliseconds,
              nowMilliseconds <= austinMaximumSafeInteger - lifetimeMilliseconds
        else {
            throw AustinFailure("prepared_binding_time")
        }
        lock.lock()
        guard generation < austinMaximumSafeInteger else {
            lock.unlock()
            throw AustinFailure("prepared_binding_exhausted")
        }
        generation += 1
        let current = generation
        lock.unlock()
        let expires = min(
            preparation.expiresAtMilliseconds,
            nowMilliseconds + lifetimeMilliseconds
        )
        let targetID = try issuer.issue(
            domain: "prepared_target",
            fields: [
                preparation.preparationID,
                preparation.requestID,
                preparation.route.rawValue,
                preparation.selector,
                String(current),
            ]
        )
        return try AustinDesktopTargetBinding(
            targetID: targetID,
            targetEpoch: current,
            targetRevision: "prepared_\(current)",
            fencingToken: current,
            snapshotID: UUID().uuidString.lowercased(),
            snapshotSequence: 1,
            observedAtMilliseconds: nowMilliseconds,
            expiresAtMilliseconds: expires
        )
    }

    private func register(
        preparation: AustinVerifiedPreparation,
        target: AustinDesktopTargetBinding,
        arguments: [String: Any]
    ) throws {
        let digest = try AustinJSON.digest(arguments)
        let record = AustinThomasPreparedRecord(
            preparation: preparation,
            target: target,
            argumentsDigest: digest
        )
        lock.lock()
        defer { lock.unlock() }
        prune(nowMilliseconds: target.observedAtMilliseconds)
        guard records[target.targetID] == nil else {
            throw AustinFailure("prepared_binding_conflict")
        }
        guard records.count < Self.maximumPreparedBindings else {
            throw AustinFailure("prepared_binding_capacity")
        }
        records[target.targetID] = record
    }

    private func prune(nowMilliseconds: Int64) {
        records = records.filter {
            !$0.value.consumed
                && $0.value.target.expiresAtMilliseconds > nowMilliseconds
        }
    }

    private func encodeReply(
        preparation: AustinVerifiedPreparation,
        target: AustinDesktopTargetBinding,
        arguments: [String: Any]
    ) throws -> Data {
        AustinReply.encode(
            status: "succeeded",
            reasonCode: "binding_prepared",
            fields: [
                "arguments": arguments,
                "binding_expires_at_ms": target.expiresAtMilliseconds,
                "data_class": preparation.dataClass.rawValue,
                "operation": preparation.operation.rawValue,
                "preparation_id": preparation.preparationID,
                "request_id": preparation.requestID,
                "route": preparation.route.rawValue,
                "snapshot": [
                    "epoch": target.targetEpoch,
                    "fencing_token": target.fencingToken,
                    "observed_at_ms": target.observedAtMilliseconds,
                    "revision": target.targetRevision,
                    "sequence": target.snapshotSequence,
                    "snapshot_id": target.snapshotID,
                    "target_id": target.targetID,
                ],
                "target": [
                    "epoch": target.targetEpoch,
                    "fencing_token": target.fencingToken,
                    "kind": "desktop_surface",
                    "revision": target.targetRevision,
                    "target_id": target.targetID,
                ],
            ]
        )
    }

    private func checkedNow() throws -> Int64 {
        let value = nowMilliseconds()
        guard value >= 0, value <= austinMaximumSafeInteger else {
            throw AustinFailure("preparation_clock")
        }
        return value
    }
}
