import CryptoKit
import Foundation
import Security

public struct AustinPeer: Equatable, Sendable {
    public let processIdentifier: Int32
    public let effectiveUserIdentifier: UInt32
    public let auditSessionIdentifier: Int32
    public let processStartSeconds: UInt64
    public let processStartMicroseconds: UInt64

    public init(
        processIdentifier: Int32,
        effectiveUserIdentifier: UInt32,
        auditSessionIdentifier: Int32,
        processStartSeconds: UInt64 = 1,
        processStartMicroseconds: UInt64 = 0
    ) throws {
        guard processIdentifier > 0,
              processStartSeconds > 0,
              processStartMicroseconds < 1_000_000
        else {
            throw AustinFailure("peer_pid")
        }
        self.processIdentifier = processIdentifier
        self.effectiveUserIdentifier = effectiveUserIdentifier
        self.auditSessionIdentifier = auditSessionIdentifier
        self.processStartSeconds = processStartSeconds
        self.processStartMicroseconds = processStartMicroseconds
    }
}

public final class AustinSession: @unchecked Sendable {
    public static let maximumCalls: UInt64 = 64
    public static let maximumLifetimeMilliseconds: Int64 = 300_000

    public let peer: AustinPeer
    public let createdAtMilliseconds: Int64
    public let expiresAtMilliseconds: Int64
    private let lock = NSLock()
    private let capability: Data
    private var nextSequence: UInt64 = 1
    private var invalidated = false

    public init(
        peer: AustinPeer,
        nowMilliseconds: Int64,
        randomBytes: () throws -> Data = AustinSession.secureRandomBytes
    ) throws {
        guard nowMilliseconds >= 0,
              nowMilliseconds <= austinMaximumSafeInteger - Self.maximumLifetimeMilliseconds
        else {
            throw AustinFailure("session_time")
        }
        let generated = try randomBytes()
        guard generated.count == 32 else { throw AustinFailure("session_entropy") }
        self.peer = peer
        self.createdAtMilliseconds = nowMilliseconds
        self.expiresAtMilliseconds = nowMilliseconds + Self.maximumLifetimeMilliseconds
        self.capability = generated
    }

    public func capabilityData() -> Data {
        capability
    }

    public func consume(
        suppliedCapability: Data,
        sequence: UInt64,
        nowMilliseconds: Int64
    ) throws {
        lock.lock()
        defer { lock.unlock() }
        guard !invalidated else { throw AustinFailure("session_invalidated") }
        guard nowMilliseconds >= createdAtMilliseconds,
              nowMilliseconds < expiresAtMilliseconds
        else {
            invalidated = true
            throw AustinFailure("session_expired")
        }
        guard constantTimeEqual(capability, suppliedCapability) else {
            invalidated = true
            throw AustinFailure("session_capability")
        }
        guard sequence == nextSequence, sequence <= Self.maximumCalls else {
            invalidated = true
            throw AustinFailure("session_sequence")
        }
        nextSequence += 1
    }

    public func invalidate() {
        lock.lock()
        invalidated = true
        lock.unlock()
    }

    public static func decodeHello(_ data: Data) throws -> String {
        let object = try AustinJSON.decodeCanonicalObject(data)
        let row = try AustinJSON.exactObject(
            object,
            keys: ["client_nonce", "message_type", "protocol_version"],
            label: "hello"
        )
        guard try AustinJSON.integer(
            row["protocol_version"],
            label: "protocol_version",
            minimum: 1,
            maximum: 1
        ) == 1 else {
            throw AustinFailure("protocol_version")
        }
        let type = try AustinJSON.string(row["message_type"], label: "message_type")
        guard type == "austin.begin" else { throw AustinFailure("message_type") }
        let nonce = try AustinJSON.string(
            row["client_nonce"],
            label: "client_nonce",
            pattern: "^[A-Za-z0-9_-]{43}$",
            maximumBytes: 43
        )
        guard decodeBase64URL(nonce)?.count == 32 else {
            throw AustinFailure("client_nonce")
        }
        return nonce
    }

    public static func encodeBeginReply(_ session: AustinSession, clientNonce: String) -> Data {
        AustinReply.encode(
            status: "succeeded",
            reasonCode: "session_open",
            fields: [
                "capability": encodeBase64URL(session.capabilityData()),
                "client_nonce": clientNonce,
                "expires_at_ms": session.expiresAtMilliseconds,
                "maximum_calls": maximumCalls,
            ]
        )
    }

    public static func secureRandomBytes() throws -> Data {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw AustinFailure("session_entropy")
        }
        return Data(bytes)
    }
}

public func encodeBase64URL(_ data: Data) -> String {
    data.base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

public func decodeBase64URL(_ text: String) -> Data? {
    guard text.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil else {
        return nil
    }
    let padding = String(repeating: "=", count: (4 - text.count % 4) % 4)
    let standard = text
        .replacingOccurrences(of: "-", with: "+")
        .replacingOccurrences(of: "_", with: "/") + padding
    guard let decoded = Data(base64Encoded: standard), encodeBase64URL(decoded) == text else {
        return nil
    }
    return decoded
}

private func constantTimeEqual(_ left: Data, _ right: Data) -> Bool {
    var difference = UInt8(left.count == right.count ? 0 : 1)
    let count = max(left.count, right.count)
    for index in 0..<count {
        let lhs = index < left.count ? left[index] : 0
        let rhs = index < right.count ? right[index] : 0
        difference |= lhs ^ rhs
    }
    return difference == 0
}
