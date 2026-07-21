import AppKit
import AustinCore
import Foundation

public enum AustinReviewedAppleEvent: String, CaseIterable, Sendable {
    case activateFinder = "activate_finder"
    case activateSystemSettings = "activate_system_settings"

    public var bundleIdentifier: String {
        switch self {
        case .activateFinder:
            "com.apple.finder"
        case .activateSystemSettings:
            "com.apple.systempreferences"
        }
    }
}

public enum AustinAppleEventBackendResult: Equatable, Sendable {
    case delivered
    case denied
    case targetUnavailable
    case timedOut
    case uncertain
}

public protocol AustinAppleEventBackend: AnyObject {
    func send(
        _ adapter: AustinReviewedAppleEvent,
        timeoutMilliseconds: Int64
    ) -> AustinAppleEventBackendResult
    func postcondition(_ adapter: AustinReviewedAppleEvent) -> AustinNativePostcondition
}

public struct AustinAppleEventBinding: Equatable, Sendable {
    public let target: AustinDesktopTargetBinding
    public let elementID: String
    public let adapter: AustinReviewedAppleEvent

    public init(
        target: AustinDesktopTargetBinding,
        elementID: String,
        adapter: AustinReviewedAppleEvent
    ) throws {
        guard elementID.range(
            of: "^hmac-sha256:[0-9a-f]{64}$",
            options: .regularExpression
        ) != nil else {
            throw AustinFailure("apple_event_element")
        }
        self.target = target
        self.elementID = elementID
        self.adapter = adapter
    }
}

private final class AustinAppleEventRecord: @unchecked Sendable {
    let binding: AustinAppleEventBinding
    var consumed = false

    init(binding: AustinAppleEventBinding) {
        self.binding = binding
    }
}

public final class AustinAppleEvent: @unchecked Sendable {
    public static let maximumBindingLifetimeMilliseconds: Int64 = 5_000
    public static let eventTimeoutMilliseconds: Int64 = 2_000
    public static let maximumBindings = 64

    private let backend: AustinAppleEventBackend
    private let issuer: AustinOpaqueTokenIssuer
    private let lock = NSLock()
    private var records: [String: AustinAppleEventRecord] = [:]

    public init(
        backend: AustinAppleEventBackend,
        randomBytes: @escaping () throws -> Data = AustinSession.secureRandomBytes
    ) throws {
        self.backend = backend
        issuer = try AustinOpaqueTokenIssuer(randomBytes: randomBytes)
    }

    func bind(
        target: AustinDesktopTargetBinding,
        adapter: AustinReviewedAppleEvent,
        confirmation: AustinThomasConfirmationLease,
        preparationID: String,
        nowMilliseconds: Int64
    ) throws -> AustinAppleEventBinding {
        try confirmation.claim(
            action: .appleEventActivate,
            preparationID: preparationID,
            nowMilliseconds: nowMilliseconds
        )
        guard nowMilliseconds >= target.observedAtMilliseconds,
              nowMilliseconds < target.expiresAtMilliseconds,
              target.expiresAtMilliseconds - nowMilliseconds
                <= Self.maximumBindingLifetimeMilliseconds
        else {
            throw AustinFailure("apple_event_binding_time")
        }
        let elementID = try issuer.issue(
            domain: "apple_event",
            fields: [target.targetID, adapter.rawValue, adapter.bundleIdentifier]
        )
        let binding = try AustinAppleEventBinding(
            target: target,
            elementID: elementID,
            adapter: adapter
        )
        lock.lock()
        records = records.filter {
            !$0.value.consumed && $0.value.binding.target.expiresAtMilliseconds > nowMilliseconds
        }
        guard records.count < Self.maximumBindings else {
            lock.unlock()
            throw AustinFailure("apple_event_capacity")
        }
        records[elementID] = AustinAppleEventRecord(binding: binding)
        lock.unlock()
        return binding
    }

    public func supports(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> Bool {
        guard envelope.route == .appleEvent,
              envelope.operation == .activate,
              let elementID = envelope.arguments["element_id"] as? String
        else {
            return false
        }
        lock.lock()
        defer { lock.unlock() }
        guard let record = records[elementID] else { return false }
        return !record.consumed
            && record.binding.target.matches(envelope)
            && nowMilliseconds >= record.binding.target.observedAtMilliseconds
            && nowMilliseconds < record.binding.target.expiresAtMilliseconds
    }

    public func execute(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> AustinDesktopOutcome {
        guard envelope.route == .appleEvent,
              envelope.operation == .activate,
              let elementID = envelope.arguments["element_id"] as? String
        else {
            return AustinDesktopOutcome(.denied, "apple_event_request")
        }
        let record: AustinAppleEventRecord
        lock.lock()
        if let existing = records[elementID], !existing.consumed {
            existing.consumed = true
            record = existing
        } else {
            lock.unlock()
            return AustinDesktopOutcome(.denied, "apple_event_stale")
        }
        lock.unlock()
        guard record.binding.target.matches(envelope) else {
            return AustinDesktopOutcome(.denied, "apple_event_target_changed")
        }
        guard nowMilliseconds >= record.binding.target.observedAtMilliseconds,
              nowMilliseconds < record.binding.target.expiresAtMilliseconds
        else {
            return AustinDesktopOutcome(.denied, "apple_event_expired")
        }

        switch backend.send(
            record.binding.adapter,
            timeoutMilliseconds: Self.eventTimeoutMilliseconds
        ) {
        case .denied:
            return AustinDesktopOutcome(.denied, "apple_event_permission_denied")
        case .targetUnavailable:
            return AustinDesktopOutcome(.denied, "apple_event_target_unavailable")
        case .timedOut:
            return AustinDesktopOutcome(.unknownOutcome, "apple_event_timeout")
        case .uncertain:
            return AustinDesktopOutcome(.unknownOutcome, "apple_event_unknown")
        case .delivered:
            switch backend.postcondition(record.binding.adapter) {
            case .verified:
                return AustinDesktopOutcome(.succeeded, "apple_event_postcondition_verified")
            case .notVerified, .unavailable:
                return AustinDesktopOutcome(.unknownOutcome, "apple_event_postcondition_unverified")
            }
        }
    }
}

public final class AustinSystemAppleEventBackend: AustinAppleEventBackend, @unchecked Sendable {
    public init() {}

    public func send(
        _ adapter: AustinReviewedAppleEvent,
        timeoutMilliseconds: Int64
    ) -> AustinAppleEventBackendResult {
        guard timeoutMilliseconds > 0, timeoutMilliseconds <= 2_000 else { return .denied }
        let target = NSAppleEventDescriptor(bundleIdentifier: adapter.bundleIdentifier)
        // 'aevt' / 'actv': the reviewed Core Suite Activate event only.
        let event = NSAppleEventDescriptor(
            eventClass: 0x6165_7674,
            eventID: 0x6163_7476,
            targetDescriptor: target,
            returnID: AEReturnID(kAutoGenerateReturnID),
            transactionID: AETransactionID(kAnyTransactionID)
        )
        do {
            _ = try event.sendEvent(
                options: [.waitForReply, .neverInteract],
                timeout: TimeInterval(timeoutMilliseconds) / 1_000
            )
            return .delivered
        } catch {
            switch (error as NSError).code {
            case -1712:
                return .timedOut
            case -1743:
                return .denied
            case -600:
                return .targetUnavailable
            default:
                return .uncertain
            }
        }
    }

    public func postcondition(_ adapter: AustinReviewedAppleEvent) -> AustinNativePostcondition {
        NSWorkspace.shared.frontmostApplication?.bundleIdentifier == adapter.bundleIdentifier
            ? .verified
            : .notVerified
    }
}

// Intentionally no arbitrary Shortcut runner exists. A future Shortcut route
// must be a separate reviewed enum and bounded native adapter; Process, shell,
// AppleScript, and caller-supplied names are not accepted by this foundation.
