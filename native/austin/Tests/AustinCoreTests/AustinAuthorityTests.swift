import CryptoKit
import Foundation
import Testing
@testable import AustinCore

private let authorityNow: Int64 = 1_800_000_000_000

private struct AustinAuthorityFixture {
    let privateKey: Curve25519.Signing.PrivateKey
    let publicKey: Data
    let payload: Data
    let envelope: [String: Any]
}

private func authorityFixture() throws -> AustinAuthorityFixture {
    let privateKey = try Curve25519.Signing.PrivateKey(
        rawRepresentation: Data((0..<32).map(UInt8.init))
    )
    let publicKey = privateKey.publicKey.rawRepresentation
    let keyDigest = SHA256.hash(data: publicKey).map { String(format: "%02x", $0) }.joined()
    let keyID = "ed25519:" + keyDigest
    let targetID = "hmac-sha256:" + String(repeating: "1", count: 64)
    let elementID = "hmac-sha256:" + String(repeating: "2", count: 64)
    let request: [String: Any] = [
        "arguments": ["element_id": elementID],
        "data_class": "structural",
        "deadline_ms": authorityNow + 5_000,
        "issued_at_ms": authorityNow - 100,
        "max_output_bytes": 4_096,
        "operation": "activate",
        "request_id": "00000000-0000-4000-8000-000000000101",
        "requested_routes": ["ax"],
        "schema_version": 1,
        "sequence": 1,
        "session_id": "00000000-0000-4000-8000-000000000201",
        "snapshot": [
            "epoch": 1,
            "fencing_token": 1,
            "observed_at_ms": authorityNow - 20,
            "revision": "launch_1",
            "sequence": 1,
            "snapshot_id": "00000000-0000-4000-8000-000000000301",
            "target_id": targetID,
        ],
        "subject_id": "runtime.operator",
        "target": [
            "epoch": 1,
            "fencing_token": 1,
            "kind": "desktop_surface",
            "revision": "launch_1",
            "target_id": targetID,
        ],
    ]
    let requestDigest = try AustinJSON.digest(request)
    var grant: [String: Any] = [
        "authority_key_id": keyID,
        "data_classes": ["structural"],
        "effects": ["ui_mutation"],
        "expires_at_ms": authorityNow + 10_000,
        "grant_id": "00000000-0000-4000-8000-000000000401",
        "issued_at_ms": authorityNow - 1_000,
        "max_input_bytes": 8_192,
        "max_output_bytes": 65_536,
        "max_transmit_bytes": 0,
        "maximum_action_count": 1,
        "operations": ["activate"],
        "policy_digest": AustinSamuelAuthority.policyDigest,
        "routes": ["ax"],
        "schema_version": 1,
        "subject_id": "runtime.operator",
        "target_ids": [targetID],
        "target_kinds": ["desktop_surface"],
    ]
    grant["signature"] = try authoritySignature(
        object: grant,
        kind: "control_grant",
        privateKey: privateKey
    )
    var permit: [String: Any] = [
        "authority_key_id": keyID,
        "data_class": "structural",
        "effects": ["ui_mutation"],
        "expires_at_ms": authorityNow + 1_000,
        "fencing_token": 1,
        "grant_id": "00000000-0000-4000-8000-000000000401",
        "input_bytes": try AustinJSON.encodeCanonical(request["arguments"]!).count,
        "issued_at_ms": authorityNow - 10,
        "maximum_action_count": 1,
        "operation": "activate",
        "output_bytes": 4_096,
        "permit_id": "00000000-0000-4000-8000-000000000501",
        "policy_digest": AustinSamuelAuthority.policyDigest,
        "request_digest": requestDigest,
        "request_id": "00000000-0000-4000-8000-000000000101",
        "routes": ["ax"],
        "schema_version": 1,
        "sequence": 1,
        "snapshot_id": "00000000-0000-4000-8000-000000000301",
        "subject_id": "runtime.operator",
        "target_epoch": 1,
        "target_id": targetID,
        "target_kind": "desktop_surface",
        "target_revision": "launch_1",
        "transmit_bytes": 0,
    ]
    permit["signature"] = try authoritySignature(
        object: permit,
        kind: "control_permit",
        privateKey: privateKey
    )
    let envelope: [String: Any] = [
        "grant": grant,
        "message_type": "control.execute",
        "permit": permit,
        "protocol_version": 1,
        "request": request,
    ]
    return AustinAuthorityFixture(
        privateKey: privateKey,
        publicKey: publicKey,
        payload: try AustinJSON.encodeCanonical(envelope),
        envelope: envelope
    )
}

private func authoritySignature(
    object: [String: Any],
    kind: String,
    privateKey: Curve25519.Signing.PrivateKey
) throws -> String {
    var signedData = Data("algo-control-v1\0\(kind)\0".utf8)
    signedData.append(try AustinJSON.encodeCanonical(object))
    return encodeBase64URL(try privateKey.signature(for: signedData))
}

@Test func authorityVerifiesSignedPythonCompatibleEnvelope() throws {
    let fixture = try authorityFixture()
    let authority = try AustinSamuelAuthority(publicKeyData: fixture.publicKey)
    let verified = try authority.verify(fixture.payload, nowMilliseconds: authorityNow)

    #expect(verified.operation == .activate)
    #expect(verified.route == .ax)
    #expect(verified.dataClass == .structural)
    #expect(verified.structuralSummary["target_id"] == nil)
    #expect(verified.structuralSummary["arguments"] == nil)
}

@Test func authorityRejectsTamperingWrongKeyAndReplayRelevantMixups() throws {
    let fixture = try authorityFixture()
    let authority = try AustinSamuelAuthority(publicKeyData: fixture.publicKey)

    var changed = fixture.envelope
    var request = changed["request"] as! [String: Any]
    request["max_output_bytes"] = 4_095
    changed["request"] = request
    #expect(throws: AustinFailure.self) {
        try authority.verify(
            AustinJSON.encodeCanonical(changed),
            nowMilliseconds: authorityNow
        )
    }

    let wrongKey = Curve25519.Signing.PrivateKey().publicKey.rawRepresentation
    let wrongAuthority = try AustinSamuelAuthority(publicKeyData: wrongKey)
    #expect(throws: AustinFailure.self) {
        try wrongAuthority.verify(fixture.payload, nowMilliseconds: authorityNow)
    }

    #expect(throws: AustinFailure.self) {
        try authority.verify(fixture.payload, nowMilliseconds: authorityNow + 2_000)
    }
}

@Test func authorityRejectsUnsupportedNativeRouteEvenWhenOuterPermitIsValid() throws {
    let fixture = try authorityFixture()
    var envelope = fixture.envelope
    var request = envelope["request"] as! [String: Any]
    request["requested_routes"] = ["connector"]
    envelope["request"] = request
    let requestDigest = try AustinJSON.digest(request)

    var grant = envelope["grant"] as! [String: Any]
    grant["routes"] = ["connector"]
    grant.removeValue(forKey: "signature")
    grant["signature"] = try authoritySignature(
        object: grant,
        kind: "control_grant",
        privateKey: fixture.privateKey
    )
    envelope["grant"] = grant

    var permit = envelope["permit"] as! [String: Any]
    permit["request_digest"] = requestDigest
    permit["routes"] = ["connector"]
    permit.removeValue(forKey: "signature")
    permit["signature"] = try authoritySignature(
        object: permit,
        kind: "control_permit",
        privateKey: fixture.privateKey
    )
    envelope["permit"] = permit

    let authority = try AustinSamuelAuthority(publicKeyData: fixture.publicKey)
    #expect(throws: AustinFailure.self) {
        try authority.verify(
            AustinJSON.encodeCanonical(envelope),
            nowMilliseconds: authorityNow
        )
    }
}

@Test func authorityRejectsGenericActivateOverCoordinateFallback() throws {
    let fixture = try authorityFixture()
    var envelope = fixture.envelope
    var request = envelope["request"] as! [String: Any]
    request["requested_routes"] = ["coordinate"]
    envelope["request"] = request
    let requestDigest = try AustinJSON.digest(request)

    var grant = envelope["grant"] as! [String: Any]
    grant["routes"] = ["coordinate"]
    grant.removeValue(forKey: "signature")
    grant["signature"] = try authoritySignature(
        object: grant,
        kind: "control_grant",
        privateKey: fixture.privateKey
    )
    envelope["grant"] = grant

    var permit = envelope["permit"] as! [String: Any]
    permit["request_digest"] = requestDigest
    permit["routes"] = ["coordinate"]
    permit.removeValue(forKey: "signature")
    permit["signature"] = try authoritySignature(
        object: permit,
        kind: "control_permit",
        privateKey: fixture.privateKey
    )
    envelope["permit"] = permit

    let authority = try AustinSamuelAuthority(publicKeyData: fixture.publicKey)
    #expect(throws: AustinFailure.self) {
        try authority.verify(
            AustinJSON.encodeCanonical(envelope),
            nowMilliseconds: authorityNow
        )
    }
}

private struct AustinPreparationFixture {
    let privateKey: Curve25519.Signing.PrivateKey
    let publicKey: Data
    let object: [String: Any]
    let payload: Data
}

private func preparationFixture(
    operation: String = "activate",
    dataClass: String = "structural",
    route: String = "ax",
    selector: String = "focused_element",
    arguments: [String: Any] = [:],
    issuedAt: Int64 = authorityNow - 100,
    expiresAt: Int64 = authorityNow + 5_000
) throws -> AustinPreparationFixture {
    let privateKey = try Curve25519.Signing.PrivateKey(
        rawRepresentation: Data((0..<32).map(UInt8.init))
    )
    let publicKey = privateKey.publicKey.rawRepresentation
    let keyDigest = SHA256.hash(data: publicKey).map { String(format: "%02x", $0) }.joined()
    var preparation: [String: Any] = [
        "arguments": arguments,
        "authority_key_id": "ed25519:" + keyDigest,
        "data_class": dataClass,
        "expires_at_ms": expiresAt,
        "issued_at_ms": issuedAt,
        "operation": operation,
        "policy_digest": AustinSamuelAuthority.policyDigest,
        "preparation_id": "00000000-0000-4000-8000-000000000601",
        "request_id": "00000000-0000-4000-8000-000000000101",
        "route": route,
        "schema_version": 1,
        "selector": selector,
        "subject_id": "runtime.operator",
    ]
    preparation["signature"] = try authoritySignature(
        object: preparation,
        kind: "control_prepare",
        privateKey: privateKey
    )
    let envelope: [String: Any] = [
        "message_type": "control.prepare",
        "preparation": preparation,
        "protocol_version": 1,
    ]
    return AustinPreparationFixture(
        privateKey: privateKey,
        publicKey: publicKey,
        object: envelope,
        payload: try AustinJSON.encodeCanonical(envelope)
    )
}

@Test func authorityVerifiesTargetFreePreparationWithoutLeakingArguments() throws {
    let fixture = try preparationFixture()
    var pythonObject = fixture.object
    var pythonRow = pythonObject["preparation"] as! [String: Any]
    pythonRow["signature"] =
        "dRCGQbDBtg3yJDXaQb1swCb3IC_53yLYHHtXwZrKqfrkL8mHCxD9gDtfNJE_BRhASs-MNFPvIWSPDsxGPjfbAA"
    pythonObject["preparation"] = pythonRow
    let pythonPayload = try AustinJSON.encodeCanonical(pythonObject)
    let authority = try AustinSamuelAuthority(publicKeyData: fixture.publicKey)
    let verified = try authority.verifyPreparation(
        pythonPayload,
        nowMilliseconds: authorityNow
    )
    #expect(verified.preparationID == "00000000-0000-4000-8000-000000000601")
    #expect(verified.requestID == "00000000-0000-4000-8000-000000000101")
    #expect(verified.subjectID == "runtime.operator")
    #expect(verified.operation == .activate)
    #expect(verified.route == .ax)
    #expect(verified.selector == "focused_element")
    #expect(
        verified.preparationDigest
            == "sha256:2bb14fc1187d0d7681c32352b25972443f9de83a6c4ce2de19e860d884c8f5f3"
    )
    let row = pythonObject["preparation"] as! [String: Any]
    #expect(
        row["signature"] as? String
            == "dRCGQbDBtg3yJDXaQb1swCb3IC_53yLYHHtXwZrKqfrkL8mHCxD9gDtfNJE_BRhASs-MNFPvIWSPDsxGPjfbAA"
    )
    #expect(verified.structuralSummary["arguments"] == nil)
    #expect(verified.structuralSummary["subject_id"] == nil)
}

@Test func authorityRejectsTamperedExpiredAndWrongKeyPreparations() throws {
    let fixture = try preparationFixture()
    let authority = try AustinSamuelAuthority(publicKeyData: fixture.publicKey)
    var changed = fixture.object
    var preparation = changed["preparation"] as! [String: Any]
    preparation["selector"] = "activate_finder"
    changed["preparation"] = preparation
    #expect(throws: AustinFailure.self) {
        try authority.verifyPreparation(
            AustinJSON.encodeCanonical(changed),
            nowMilliseconds: authorityNow
        )
    }
    #expect(throws: AustinFailure.self) {
        try authority.verifyPreparation(
            fixture.payload,
            nowMilliseconds: authorityNow + 5_000
        )
    }
    let attacker = try AustinSamuelAuthority(
        publicKeyData: Curve25519.Signing.PrivateKey().publicKey.rawRepresentation
    )
    #expect(throws: AustinFailure.self) {
        try attacker.verifyPreparation(fixture.payload, nowMilliseconds: authorityNow)
    }
}

@Test func authorityRejectsSignedPreparationRouteAndArgumentConfusion() throws {
    let invalid: [AustinPreparationFixture] = try [
        preparationFixture(route: "coordinate", selector: "frontmost_point"),
        preparationFixture(
            operation: "coordinate_activate",
            route: "coordinate",
            selector: "frontmost_point",
            arguments: ["x": 10, "y": 20, "viewport_width": 100]
        ),
        preparationFixture(
            operation: "scroll",
            route: "ax",
            selector: "focused_element",
            arguments: ["delta_x": 0, "delta_y": 0]
        ),
        preparationFixture(
            operation: "observe",
            route: "screenshot",
            selector: "picker_scoped"
        ),
        preparationFixture(expiresAt: authorityNow + 60_001),
    ]
    for fixture in invalid {
        let authority = try AustinSamuelAuthority(publicKeyData: fixture.publicKey)
        #expect(throws: AustinFailure.self) {
            try authority.verifyPreparation(
                fixture.payload,
                nowMilliseconds: authorityNow
            )
        }
    }
}
