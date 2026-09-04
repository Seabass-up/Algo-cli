import Foundation
import Testing
@testable import AustinCore

@Test func sessionBindsCapabilitySequenceAndExpiry() throws {
    let peer = try AustinPeer(
        processIdentifier: 42,
        effectiveUserIdentifier: 501,
        auditSessionIdentifier: 100
    )
    let capability = Data(repeating: 0xA5, count: 32)
    let session = try AustinSession(
        peer: peer,
        nowMilliseconds: 1_000,
        randomBytes: { capability }
    )
    try session.consume(
        suppliedCapability: capability,
        sequence: 1,
        nowMilliseconds: 1_001
    )
    #expect(throws: AustinFailure.self) {
        try session.consume(
            suppliedCapability: capability,
            sequence: 1,
            nowMilliseconds: 1_002
        )
    }
}

@Test func invalidCapabilityFailsClosedAndInvalidatesSession() throws {
    let peer = try AustinPeer(
        processIdentifier: 42,
        effectiveUserIdentifier: 501,
        auditSessionIdentifier: 100
    )
    let capability = Data(repeating: 0xA5, count: 32)
    let session = try AustinSession(
        peer: peer,
        nowMilliseconds: 1_000,
        randomBytes: { capability }
    )
    #expect(throws: AustinFailure.self) {
        try session.consume(
            suppliedCapability: Data(repeating: 0x5A, count: 32),
            sequence: 1,
            nowMilliseconds: 1_001
        )
    }
    #expect(throws: AustinFailure.self) {
        try session.consume(
            suppliedCapability: capability,
            sequence: 1,
            nowMilliseconds: 1_002
        )
    }
}

@Test func developerIDRequirementPinsTeamBundleAndHardenedCertificateChain() throws {
    let requirement = try AustinPeerIdentity.developerIDRequirement(
        teamIdentifier: "ABCDE12345",
        peerIdentifier: "com.algo-cli.austin.relay"
    )
    #expect(requirement.contains("identifier \"com.algo-cli.austin.relay\""))
    #expect(requirement.contains("certificate leaf[subject.OU] = \"ABCDE12345\""))
    #expect(requirement.contains("1.2.840.113635.100.6.1.13"))
    #expect(requirement.contains("1.2.840.113635.100.6.2.6"))
    #expect(throws: AustinFailure.self) {
        try AustinPeerIdentity.developerIDRequirement(
            teamIdentifier: "wrong",
            peerIdentifier: "com.algo-cli.austin.relay"
        )
    }
    #expect(throws: AustinFailure.self) {
        try AustinPeerIdentity.developerIDRequirement(
            teamIdentifier: "ABCDE12345",
            peerIdentifier: "bad identifier"
        )
    }
}
