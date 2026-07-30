import CryptoKit
import Foundation
import Security

public let austinCredentialMigrationProtocolVersion = 1
public let austinCredentialMigrationService = "algo-cli-runtime"
public let austinCredentialMigrationIdentifier =
    "com.algo-cli.austin.credential-migrator"

public struct AustinCredentialItem: Sendable {
    public let label: String
    public let value: Data

    public init(label: String, value: Data) {
        self.label = label
        self.value = value
    }
}

public struct AustinNativeCodeIdentity: Sendable {
    public let identifier: String
    public let teamID: String

    public init(identifier: String, teamID: String) {
        self.identifier = identifier
        self.teamID = teamID
    }
}

public enum AustinAdaCredentialMigration {
    private static let registryLabel = "ada-credential-labels-v1"
    private static let fixedLabels: Set<String> = [
        registryLabel,
        "alice-artifact-master-v1",
        "browser-pairing-hmac-v1",
        "control-signing-ed25519-v1",
        "irene-privacy-hmac-v1",
    ]

    public static func designatedRequirement(identity: AustinNativeCodeIdentity) throws -> String {
        guard identity.identifier == austinCredentialMigrationIdentifier,
              identity.teamID.range(of: "^[A-Z0-9]{10}$", options: .regularExpression) != nil
        else {
            throw AustinFailure("credential_identity")
        }
        return "designated => anchor apple generic and "
            + "certificate leaf[field.1.2.840.113635.100.6.1.13] exists and "
            + "certificate leaf[subject.OU] = \"\(identity.teamID)\" and "
            + "identifier \"\(identity.identifier)\""
    }

    public static func evidence(
        items: [AustinCredentialItem],
        nonce: String,
        generatedAtMilliseconds: Int64,
        identity: AustinNativeCodeIdentity
    ) throws -> Data {
        guard nonce.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else {
            throw AustinFailure("credential_nonce")
        }
        guard generatedAtMilliseconds >= 0,
              generatedAtMilliseconds <= austinMaximumSafeInteger,
              items.count <= 256
        else {
            throw AustinFailure("credential_bounds")
        }
        let requirement = try designatedRequirement(identity: identity)
        let requirementHash = SHA256.hash(data: Data(requirement.utf8))
        let requirementDigest = "sha256:"
            + requirementHash.map { String(format: "%02x", $0) }.joined()

        var seen = Set<String>()
        var records: [[String: Any]] = []
        var registryPresent = false
        var unexpectedLabelCount = 0
        for item in items {
            guard item.label.range(
                of: "^[A-Za-z0-9._:-]{1,96}$",
                options: .regularExpression
            ) != nil else {
                throw AustinFailure("credential_label")
            }
            guard seen.insert(item.label).inserted else {
                throw AustinFailure("credential_duplicate")
            }
            if item.label == registryLabel {
                registryPresent = true
                continue
            }
            let allowed = fixedLabels.contains(item.label)
                || item.label.range(
                    of: "^receipt-head-v1-[0-9a-f]{64}$",
                    options: .regularExpression
                ) != nil
            guard allowed else {
                unexpectedLabelCount += 1
                continue
            }
            let digest = SHA256.hash(data: item.value)
            records.append([
                "label": item.label,
                "value_digest": "sha256:"
                    + digest.map { String(format: "%02x", $0) }.joined(),
            ])
        }
        records.sort {
            guard let left = $0["label"] as? String,
                  let right = $1["label"] as? String
            else { return false }
            return left < right
        }
        return try AustinJSON.encodeCanonical([
            "code_identifier": identity.identifier,
            "designated_requirement_digest": requirementDigest,
            "generated_at_ms": generatedAtMilliseconds,
            "kind": "native_credential_enumeration",
            "match_limit": "all",
            "nonce": nonce,
            "query_class": "generic_password",
            "records": records,
            "registry_present": registryPresent,
            "schema_version": austinCredentialMigrationProtocolVersion,
            "service": austinCredentialMigrationService,
            "synchronizable": "any",
            "team_id": identity.teamID,
            "unexpected_label_count": unexpectedLabelCount,
        ])
    }
}

public enum AustinKeychainCredentialReader {
    public static func enumerate() throws -> [AustinCredentialItem] {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: austinCredentialMigrationService,
            kSecAttrSynchronizable as String: kSecAttrSynchronizableAny,
            kSecMatchLimit as String: kSecMatchLimitAll,
            kSecReturnAttributes as String: true,
            kSecReturnData as String: true,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return []
        }
        guard status == errSecSuccess else {
            throw AustinFailure("credential_keychain_status")
        }
        let rows: [[String: Any]]
        if let array = result as? [[String: Any]] {
            rows = array
        } else if let row = result as? [String: Any] {
            rows = [row]
        } else {
            throw AustinFailure("credential_keychain_result")
        }
        return try rows.map { row in
            guard let label = row[kSecAttrAccount as String] as? String,
                  let value = row[kSecValueData as String] as? Data,
                  !value.isEmpty,
                  value.count <= 64 * 1024
            else {
                throw AustinFailure("credential_keychain_item")
            }
            return AustinCredentialItem(label: label, value: value)
        }
    }
}
