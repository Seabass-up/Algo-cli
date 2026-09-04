import AustinDarwinBridge
import Foundation
import Security

public struct AustinSigningIdentity: Equatable, Sendable {
    public let signingIdentifier: String
    public let teamIdentifier: String?
    public let hardenedRuntime: Bool

    public var isDeveloperIDReady: Bool {
        teamIdentifier != nil && hardenedRuntime
    }
}

public enum AustinPeerIdentity {
    public static func processStartTime(
        processIdentifier: Int32
    ) throws -> (seconds: UInt64, microseconds: UInt64) {
        guard processIdentifier > 0 else { throw AustinFailure("process_identifier") }
        var seconds: UInt64 = 0
        var microseconds: UInt64 = 0
        guard AustinProcessStartTime(processIdentifier, &seconds, &microseconds),
              seconds > 0,
              microseconds < 1_000_000
        else {
            throw AustinFailure("process_start")
        }
        return (seconds, microseconds)
    }

    public static func currentSigningIdentity() throws -> AustinSigningIdentity {
        var code: SecCode?
        guard SecCodeCopySelf(SecCSFlags(), &code) == errSecSuccess, let code else {
            throw AustinFailure("self_code_identity")
        }
        var staticCode: SecStaticCode?
        guard SecCodeCopyStaticCode(code, SecCSFlags(), &staticCode) == errSecSuccess,
              let staticCode
        else {
            throw AustinFailure("self_static_code")
        }
        var rawInformation: CFDictionary?
        let flags = SecCSFlags(rawValue: kSecCSSigningInformation)
        guard SecCodeCopySigningInformation(staticCode, flags, &rawInformation) == errSecSuccess,
              let information = rawInformation as? [CFString: Any],
              let identifier = information[kSecCodeInfoIdentifier] as? String,
              !identifier.isEmpty
        else {
            throw AustinFailure("self_signing_information")
        }
        let team = information[kSecCodeInfoTeamIdentifier] as? String
        let signatureFlags = (information[kSecCodeInfoFlags] as? NSNumber)?.uint32Value ?? 0
        // CS_RUNTIME is 0x10000 in Security/CSCommon.h but is not imported by Swift.
        let runtimeFlag: UInt32 = 0x0001_0000
        return AustinSigningIdentity(
            signingIdentifier: identifier,
            teamIdentifier: team,
            hardenedRuntime: signatureFlags & runtimeFlag == runtimeFlag
        )
    }

    public static func developerIDRequirement(
        teamIdentifier: String,
        peerIdentifier: String
    ) throws -> String {
        guard teamIdentifier.range(
            of: "^[A-Z0-9]{10}$",
            options: .regularExpression
        ) != nil else {
            throw AustinFailure("team_identifier")
        }
        guard peerIdentifier.range(
            of: "^[A-Za-z0-9][A-Za-z0-9.-]{2,127}$",
            options: .regularExpression
        ) != nil else {
            throw AustinFailure("peer_identifier")
        }
        return [
            "anchor apple generic",
            "identifier \"\(peerIdentifier)\"",
            "certificate 1[field.1.2.840.113635.100.6.2.6] exists",
            "certificate leaf[field.1.2.840.113635.100.6.1.13] exists",
            "certificate leaf[subject.OU] = \"\(teamIdentifier)\"",
        ].joined(separator: " and ")
    }

    public static func productionPeerRequirement(peerIdentifier: String) throws -> String {
        let identity = try currentSigningIdentity()
        guard identity.hardenedRuntime, let team = identity.teamIdentifier else {
            throw AustinFailure("developer_id_identity_required")
        }
        return try developerIDRequirement(teamIdentifier: team, peerIdentifier: peerIdentifier)
    }

    public static func peerRequirement(peerIdentifier: String) throws -> String {
        #if DEBUG
        if ProcessInfo.processInfo.environment["ALGO_AUSTIN_ADHOC_TEST"] == "1" {
            guard peerIdentifier.range(
                of: "^[A-Za-z0-9][A-Za-z0-9.-]{2,127}$",
                options: .regularExpression
            ) != nil else {
                throw AustinFailure("peer_identifier")
            }
            return "identifier \"\(peerIdentifier)\""
        }
        #endif
        return try productionPeerRequirement(peerIdentifier: peerIdentifier)
    }

    public static func validateConnectionPeer(
        _ connection: NSXPCConnection,
        expectedUserIdentifier: UInt32 = geteuid(),
        requiredAuditSessionIdentifier: Int32? = nil
    ) throws -> AustinPeer {
        let pid = connection.processIdentifier
        let user = connection.effectiveUserIdentifier
        let session = connection.auditSessionIdentifier
        guard pid > 0 else { throw AustinFailure("peer_pid") }
        guard user == expectedUserIdentifier else { throw AustinFailure("peer_user") }
        if let requiredAuditSessionIdentifier,
           session != requiredAuditSessionIdentifier {
            throw AustinFailure("peer_session")
        }
        guard try isLocalGraphicSession(session) else { throw AustinFailure("peer_session") }
        let start: (seconds: UInt64, microseconds: UInt64)
        do {
            start = try processStartTime(processIdentifier: pid)
        } catch {
            throw AustinFailure("peer_process_start")
        }
        return try AustinPeer(
            processIdentifier: pid,
            effectiveUserIdentifier: user,
            auditSessionIdentifier: session,
            processStartSeconds: start.seconds,
            processStartMicroseconds: start.microseconds
        )
    }

    public static func isLocalGraphicSession(_ auditSessionIdentifier: Int32) throws -> Bool {
        guard auditSessionIdentifier > 0 else { return false }
        let requested = SecuritySessionId(bitPattern: auditSessionIdentifier)
        var resolved: SecuritySessionId = 0
        var attributes: SessionAttributeBits = []
        guard SessionGetInfo(requested, &resolved, &attributes) == errSecSuccess,
              resolved == requested
        else {
            throw AustinFailure("peer_session_lookup")
        }
        // AuthSession.h constants are public but not imported into Swift.
        let isRoot: UInt32 = 0x0001
        let hasGraphicAccess: UInt32 = 0x0010
        let isRemote: UInt32 = 0x1000
        let value = attributes.rawValue
        return value & hasGraphicAccess == hasGraphicAccess
            && value & isRoot == 0
            && value & isRemote == 0
    }
}
