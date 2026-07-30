import AustinCore
import Darwin
import Foundation
import Security

private let maximumRequestBytes = 1_024

private func readBoundedRequest() throws -> Data {
    var result = Data()
    while true {
        let remaining = maximumRequestBytes + 1 - result.count
        guard remaining > 0 else { throw AustinFailure("credential_request_size") }
        guard let chunk = try FileHandle.standardInput.read(upToCount: min(remaining, 512)) else {
            break
        }
        if chunk.isEmpty { break }
        result.append(chunk)
    }
    guard !result.isEmpty, result.count <= maximumRequestBytes else {
        throw AustinFailure("credential_request_size")
    }
    return result
}

private func currentCodeIdentity() throws -> AustinNativeCodeIdentity {
    var code: SecCode?
    guard SecCodeCopySelf([], &code) == errSecSuccess, let code else {
        throw AustinFailure("credential_identity")
    }
    var staticCode: SecStaticCode?
    guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess,
          let staticCode
    else {
        throw AustinFailure("credential_identity")
    }
    var rawInformation: CFDictionary?
    guard SecCodeCopySigningInformation(
        staticCode,
        SecCSFlags(rawValue: kSecCSSigningInformation),
        &rawInformation
    ) == errSecSuccess,
        let information = rawInformation as? [String: Any],
        let identifier = information[kSecCodeInfoIdentifier as String] as? String,
        let teamID = information[kSecCodeInfoTeamIdentifier as String] as? String
    else {
        throw AustinFailure("credential_identity")
    }
    return AustinNativeCodeIdentity(identifier: identifier, teamID: teamID)
}

private func blocked(_ reasonCode: String) -> Data {
    AustinReply.encode(status: "blocked", reasonCode: reasonCode)
}

@main
enum AustinCredentialMigratorMain {
    static func main() {
        do {
            let request = try AustinJSON.decodeCanonicalObject(readBoundedRequest())
            let row = try AustinJSON.exactObject(
                request,
                keys: ["nonce", "protocol_version"],
                label: "credential_request"
            )
            guard try AustinJSON.integer(
                row["protocol_version"],
                label: "credential_protocol",
                minimum: 1,
                maximum: 1
            ) == 1 else {
                throw AustinFailure("credential_protocol")
            }
            let nonce = try AustinJSON.string(
                row["nonce"],
                label: "credential_nonce",
                pattern: "^[0-9a-f]{64}$",
                maximumBytes: 64
            )
            let identity = try currentCodeIdentity()
            _ = try AustinAdaCredentialMigration.designatedRequirement(identity: identity)
            let now = Int64(Date().timeIntervalSince1970 * 1_000)
            let items = try AustinKeychainCredentialReader.enumerate()
            let evidence = try AustinAdaCredentialMigration.evidence(
                items: items,
                nonce: nonce,
                generatedAtMilliseconds: now,
                identity: identity
            )
            try FileHandle.standardOutput.write(contentsOf: evidence)
            exit(EXIT_SUCCESS)
        } catch let failure as AustinFailure {
            try? FileHandle.standardOutput.write(contentsOf: blocked(failure.reasonCode))
            exit(78)
        } catch {
            try? FileHandle.standardOutput.write(
                contentsOf: blocked("credential_internal_error")
            )
            exit(78)
        }
    }
}
