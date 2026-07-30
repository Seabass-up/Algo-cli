import AustinCore
import CryptoKit
import Foundation

public enum AustinDesktopDisposition: String, Sendable {
    case succeeded
    case denied
    case handoffRequired = "handoff_required"
    case unknownOutcome = "unknown_outcome"
    case failed
}

public struct AustinDesktopOutcome: Equatable, Sendable {
    public let disposition: AustinDesktopDisposition
    public let reasonCode: String

    public init(_ disposition: AustinDesktopDisposition, _ reasonCode: String) {
        self.disposition = disposition
        self.reasonCode = AustinFailure(reasonCode).reasonCode
    }

    public func encoded(operation: AustinOperation, route: AustinRoute) -> Data {
        AustinReply.encode(
            status: disposition.rawValue,
            reasonCode: reasonCode,
            fields: [
                "operation": operation.rawValue,
                "route": route.rawValue,
            ]
        )
    }
}

public enum AustinClock {
    public static func nowMilliseconds() -> Int64 {
        Int64(Date().timeIntervalSince1970 * 1_000)
    }
}

public enum AustinNativeEffect: Equatable, Sendable {
    case performed
    case rejected(String)
    case uncertain(String)
}

public enum AustinNativePostcondition: Equatable, Sendable {
    case verified
    case notVerified
    case unavailable
}

public struct AustinDesktopProcess: Equatable, Sendable {
    public let processIdentifier: Int32
    public let processStartSeconds: UInt64
    public let processStartMicroseconds: UInt64
    public let bundleIdentifier: String

    public init(
        processIdentifier: Int32,
        processStartSeconds: UInt64,
        processStartMicroseconds: UInt64,
        bundleIdentifier: String
    ) throws {
        guard processIdentifier > 0,
              processStartSeconds > 0,
              processStartMicroseconds < 1_000_000,
              bundleIdentifier.range(
                  of: "^[A-Za-z0-9][A-Za-z0-9.-]{2,255}$",
                  options: .regularExpression
              ) != nil
        else {
            throw AustinFailure("desktop_process")
        }
        self.processIdentifier = processIdentifier
        self.processStartSeconds = processStartSeconds
        self.processStartMicroseconds = processStartMicroseconds
        self.bundleIdentifier = bundleIdentifier
    }
}

public struct AustinDesktopTargetBinding: Equatable, Sendable {
    public let targetID: String
    public let targetEpoch: Int64
    public let targetRevision: String
    public let fencingToken: Int64
    public let snapshotID: String
    public let snapshotSequence: Int64
    public let observedAtMilliseconds: Int64
    public let expiresAtMilliseconds: Int64

    public init(
        targetID: String,
        targetEpoch: Int64,
        targetRevision: String,
        fencingToken: Int64,
        snapshotID: String,
        snapshotSequence: Int64,
        observedAtMilliseconds: Int64,
        expiresAtMilliseconds: Int64
    ) throws {
        guard targetID.range(
            of: "^hmac-sha256:[0-9a-f]{64}$",
            options: .regularExpression
        ) != nil,
        targetEpoch > 0,
        targetEpoch <= austinMaximumSafeInteger,
        targetRevision.range(
            of: "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
            options: .regularExpression
        ) != nil,
        fencingToken > 0,
        fencingToken <= austinMaximumSafeInteger,
        snapshotID.range(
            of: "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            options: .regularExpression
        ) != nil,
        let parsedSnapshot = UUID(uuidString: snapshotID),
        parsedSnapshot.uuidString.lowercased() == snapshotID,
        snapshotID != "00000000-0000-0000-0000-000000000000",
        snapshotSequence > 0,
        observedAtMilliseconds >= 0,
        observedAtMilliseconds < expiresAtMilliseconds,
        expiresAtMilliseconds <= austinMaximumSafeInteger
        else {
            throw AustinFailure("desktop_binding")
        }
        self.targetID = targetID
        self.targetEpoch = targetEpoch
        self.targetRevision = targetRevision
        self.fencingToken = fencingToken
        self.snapshotID = snapshotID
        self.snapshotSequence = snapshotSequence
        self.observedAtMilliseconds = observedAtMilliseconds
        self.expiresAtMilliseconds = expiresAtMilliseconds
    }

    public func matches(_ envelope: AustinVerifiedEnvelope) -> Bool {
        targetID == envelope.targetID
            && targetEpoch == envelope.targetEpoch
            && targetRevision == envelope.targetRevision
            && fencingToken == envelope.fencingToken
            && snapshotID == envelope.snapshotID
            && snapshotSequence == envelope.snapshotSequence
    }
}

final class AustinOpaqueTokenIssuer: @unchecked Sendable {
    private let key: SymmetricKey
    private let lock = NSLock()
    private var sequence: UInt64 = 0

    init(randomBytes: () throws -> Data = AustinSession.secureRandomBytes) throws {
        let bytes = try randomBytes()
        guard bytes.count == 32 else { throw AustinFailure("binding_entropy") }
        key = SymmetricKey(data: bytes)
    }

    func issue(domain: String, fields: [String]) throws -> String {
        guard domain.range(
            of: "^[a-z][a-z0-9_]{0,31}$",
            options: .regularExpression
        ) != nil,
        fields.count <= 16,
        fields.allSatisfy({ !$0.isEmpty && $0.utf8.count <= 256 })
        else {
            throw AustinFailure("binding_token_input")
        }
        lock.lock()
        guard sequence < UInt64(austinMaximumSafeInteger) else {
            lock.unlock()
            throw AustinFailure("binding_token_exhausted")
        }
        sequence += 1
        let current = sequence
        lock.unlock()
        var material = Data("austin-binding-v1\0\(domain)\0\(current)\0".utf8)
        for field in fields {
            material.append(Data(field.utf8))
            material.append(0)
        }
        let code = HMAC<SHA256>.authenticationCode(for: material, using: key)
        return "hmac-sha256:" + code.map { String(format: "%02x", $0) }.joined()
    }
}

public final class AustinDesktopDispatcher: @unchecked Sendable {
    private let accessibility: AustinAccessibility?
    private let capture: AustinScreenCapture?
    private let appleEvent: AustinAppleEvent?
    private let shortcut: AustinShortcut?
    private let cgEvent: AustinCGEvent?
    private let preparationCoordinator: AustinThomasBindingCoordinator?

    public init(
        accessibility: AustinAccessibility? = nil,
        capture: AustinScreenCapture? = nil,
        appleEvent: AustinAppleEvent? = nil,
        shortcut: AustinShortcut? = nil,
        cgEvent: AustinCGEvent? = nil,
        preparationCoordinator: AustinThomasBindingCoordinator? = nil
    ) {
        self.accessibility = accessibility
        self.capture = capture
        self.appleEvent = appleEvent
        self.shortcut = shortcut
        self.cgEvent = cgEvent
        self.preparationCoordinator = preparationCoordinator
    }

    public static func disabledFoundation() -> AustinDesktopDispatcher {
        AustinDesktopDispatcher()
    }

    public func isReady(
        for envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> Bool {
        guard preparationCoordinator?.supports(
            envelope,
            nowMilliseconds: nowMilliseconds
        ) == true else {
            return false
        }
        return switch envelope.route {
        case .ax:
            accessibility?.supports(envelope, nowMilliseconds: nowMilliseconds) == true
        case .screenshot:
            capture?.supports(envelope, nowMilliseconds: nowMilliseconds) == true
        case .appleEvent:
            appleEvent?.supports(envelope, nowMilliseconds: nowMilliseconds) == true
        case .shortcut:
            shortcut?.supports(envelope, nowMilliseconds: nowMilliseconds) == true
        case .coordinate:
            cgEvent?.supports(envelope, nowMilliseconds: nowMilliseconds) == true
        case .connector, .dom, .handoff:
            false
        }
    }

    public func execute(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> Data {
        guard isReady(for: envelope, nowMilliseconds: nowMilliseconds) else {
            return AustinDesktopOutcome(
                .denied,
                "prepared_binding_mismatch"
            ).encoded(operation: envelope.operation, route: envelope.route)
        }
        do {
            try preparationCoordinator?.claimExecution(
                envelope,
                nowMilliseconds: nowMilliseconds
            )
        } catch let failure as AustinFailure {
            return AustinDesktopOutcome(
                .denied,
                failure.reasonCode
            ).encoded(operation: envelope.operation, route: envelope.route)
        } catch {
            return AustinDesktopOutcome(
                .failed,
                "prepared_binding_internal"
            ).encoded(operation: envelope.operation, route: envelope.route)
        }
        let outcome: AustinDesktopOutcome
        switch envelope.route {
        case .ax:
            outcome = accessibility?.execute(envelope, nowMilliseconds: nowMilliseconds)
                ?? AustinDesktopOutcome(.denied, "adapter_disabled")
        case .screenshot:
            outcome = capture?.execute(envelope, nowMilliseconds: nowMilliseconds)
                ?? AustinDesktopOutcome(.denied, "adapter_disabled")
        case .appleEvent:
            outcome = appleEvent?.execute(envelope, nowMilliseconds: nowMilliseconds)
                ?? AustinDesktopOutcome(.denied, "adapter_disabled")
        case .shortcut:
            outcome = shortcut?.execute(envelope, nowMilliseconds: nowMilliseconds)
                ?? AustinDesktopOutcome(.denied, "adapter_disabled")
        case .coordinate:
            outcome = cgEvent?.execute(envelope, nowMilliseconds: nowMilliseconds)
                ?? AustinDesktopOutcome(.denied, "adapter_disabled")
        case .connector, .dom, .handoff:
            outcome = AustinDesktopOutcome(.denied, "native_route_denied")
        }
        return outcome.encoded(operation: envelope.operation, route: envelope.route)
    }
}
