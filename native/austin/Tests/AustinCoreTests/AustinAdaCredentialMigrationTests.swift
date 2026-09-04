import CryptoKit
import Foundation
import Testing
@testable import AustinCore

private let migrationIdentity = AustinNativeCodeIdentity(
    identifier: austinCredentialMigrationIdentifier,
    teamID: "ABCDE12345"
)

@Test func credentialMigrationEvidenceIsCanonicalSortedAndContentFree() throws {
    let evidence = try AustinAdaCredentialMigration.evidence(
        items: [
            AustinCredentialItem(
                label: "receipt-head-v1-" + String(repeating: "f", count: 64),
                value: Data("anchor-secret".utf8)
            ),
            AustinCredentialItem(
                label: "control-signing-ed25519-v1",
                value: Data("control-secret".utf8)
            ),
        ],
        nonce: String(repeating: "a", count: 64),
        generatedAtMilliseconds: 1_800_000_000_000,
        identity: migrationIdentity
    )
    let row = try AustinJSON.decodeCanonicalObject(evidence)
    let records = try #require(row["records"] as? [[String: Any]])

    #expect(row["kind"] as? String == "native_credential_enumeration")
    #expect(row["registry_present"] as? Bool == false)
    #expect(row["unexpected_label_count"] as? Int == 0)
    #expect(records.compactMap { $0["label"] as? String } == [
        "control-signing-ed25519-v1",
        "receipt-head-v1-" + String(repeating: "f", count: 64),
    ])
    #expect(!evidence.contains(Data("control-secret".utf8)))
    #expect(!evidence.contains(Data("anchor-secret".utf8)))
}

@Test func credentialMigrationEvidenceReportsRegistryAndUnexpectedScope() throws {
    let evidence = try AustinAdaCredentialMigration.evidence(
        items: [
            AustinCredentialItem(
                label: "ada-credential-labels-v1",
                value: Data("existing-registry".utf8)
            ),
            AustinCredentialItem(label: "foreign-item", value: Data("foreign".utf8)),
        ],
        nonce: String(repeating: "b", count: 64),
        generatedAtMilliseconds: 1_800_000_000_000,
        identity: migrationIdentity
    )
    let row = try AustinJSON.decodeCanonicalObject(evidence)

    #expect(row["registry_present"] as? Bool == true)
    #expect(row["unexpected_label_count"] as? Int == 1)
    #expect((row["records"] as? [Any])?.isEmpty == true)
    #expect(!evidence.contains(Data("foreign".utf8)))
}

@Test func credentialMigrationRejectsDuplicateInvalidAndWrongIdentityInputs() {
    let duplicate = [
        AustinCredentialItem(label: "browser-pairing-hmac-v1", value: Data([1])),
        AustinCredentialItem(label: "browser-pairing-hmac-v1", value: Data([2])),
    ]
    #expect(throws: AustinFailure.self) {
        try AustinAdaCredentialMigration.evidence(
            items: duplicate,
            nonce: String(repeating: "c", count: 64),
            generatedAtMilliseconds: 1_800_000_000_000,
            identity: migrationIdentity
        )
    }
    #expect(throws: AustinFailure.self) {
        try AustinAdaCredentialMigration.evidence(
            items: [],
            nonce: "not-a-nonce",
            generatedAtMilliseconds: 1_800_000_000_000,
            identity: migrationIdentity
        )
    }
    #expect(throws: AustinFailure.self) {
        try AustinAdaCredentialMigration.evidence(
            items: [],
            nonce: String(repeating: "d", count: 64),
            generatedAtMilliseconds: 1_800_000_000_000,
            identity: AustinNativeCodeIdentity(
                identifier: "com.example.foreign",
                teamID: "ABCDE12345"
            )
        )
    }
}

@Test func credentialRequirementDigestMatchesTheCanonicalRequirement() throws {
    let requirement = try AustinAdaCredentialMigration.designatedRequirement(
        identity: migrationIdentity
    )
    let digest = SHA256.hash(data: Data(requirement.utf8))
    let expected = "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
    let evidence = try AustinAdaCredentialMigration.evidence(
        items: [],
        nonce: String(repeating: "e", count: 64),
        generatedAtMilliseconds: 1_800_000_000_000,
        identity: migrationIdentity
    )
    let row = try AustinJSON.decodeCanonicalObject(evidence)

    #expect(row["designated_requirement_digest"] as? String == expected)
}
