import Foundation
import Testing
@testable import AustinCore

@Test func canonicalWireRejectsDuplicateAndNoncanonicalObjects() throws {
    let valid = Data("{\"message_type\":\"austin.begin\",\"protocol_version\":1}".utf8)
    let decoded = try AustinJSON.decodeCanonicalObject(valid)
    #expect(decoded["message_type"] as? String == "austin.begin")

    #expect(throws: AustinFailure.self) {
        try AustinJSON.decodeCanonicalObject(
            Data("{\"protocol_version\":1,\"message_type\":\"austin.begin\"}".utf8)
        )
    }
    #expect(throws: AustinFailure.self) {
        try AustinJSON.decodeCanonicalObject(Data("{\"a\":1,\"a\":1}".utf8))
    }
}

@Test func canonicalWireRejectsFloatsNullAndOversizedPayloads() {
    #expect(throws: AustinFailure.self) {
        try AustinJSON.decodeCanonicalObject(Data("{\"value\":1.0}".utf8))
    }
    #expect(throws: AustinFailure.self) {
        try AustinJSON.decodeCanonicalObject(Data("{\"value\":null}".utf8))
    }
    #expect(throws: AustinFailure.self) {
        try AustinJSON.decodeCanonicalObject(Data(repeating: 0x61, count: austinMaximumFrameBytes + 1))
    }
}
