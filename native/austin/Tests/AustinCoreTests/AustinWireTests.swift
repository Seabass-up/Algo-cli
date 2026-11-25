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

@Test func canonicalWireRejectsIntegersThatWrapAsInt64() throws {
    // 2^64 - 1 is UInt64-backed and its int64Value is -1, which a range check on
    // int64Value accepts.
    #expect(throws: AustinFailure.self) {
        try AustinJSON.decodeCanonicalObject(Data("{\"value\":18446744073709551615}".utf8))
    }
    #expect(throws: AustinFailure.self) {
        try AustinJSON.integer(NSNumber(value: UInt64.max), label: "value", minimum: -1, maximum: 10)
    }
    let decoded = try AustinJSON.decodeCanonicalObject(Data("{\"value\":9007199254740991}".utf8))
    #expect(try AustinJSON.integer(decoded["value"], label: "value") == 9_007_199_254_740_991)
}
