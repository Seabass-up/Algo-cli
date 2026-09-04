import CoreFoundation
import CryptoKit
import Foundation

public let austinProtocolVersion = 1
public let austinMaximumFrameBytes = 65_536
public let austinMaximumJSONDepth = 12
public let austinMaximumJSONItems = 512
public let austinMaximumStringBytes = 8_192
public let austinMaximumSafeInteger: Int64 = 9_007_199_254_740_991

public struct AustinFailure: Error, Equatable, Sendable, CustomStringConvertible {
    public let reasonCode: String

    public init(_ reasonCode: String) {
        let allowed = reasonCode.unicodeScalars.allSatisfy {
            CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789._:-").contains($0)
        }
        self.reasonCode = allowed && !reasonCode.isEmpty && reasonCode.utf8.count <= 128
            ? reasonCode
            : "invalid_failure"
    }

    public var description: String { reasonCode }
}

public enum AustinJSON {
    public static func decodeCanonicalObject(_ data: Data) throws -> [String: Any] {
        guard !data.isEmpty, data.count <= austinMaximumFrameBytes else {
            throw AustinFailure("frame_size")
        }
        guard !data.starts(with: [0xEF, 0xBB, 0xBF]) else {
            throw AustinFailure("json_bom")
        }
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: data, options: [])
        } catch {
            throw AustinFailure("json_syntax")
        }
        var count = 0
        try validate(object, depth: 0, count: &count)
        guard let dictionary = object as? [String: Any] else {
            throw AustinFailure("json_root")
        }
        let canonical = try encodeCanonical(dictionary)
        guard canonical == data else {
            throw AustinFailure("json_noncanonical")
        }
        return dictionary
    }

    public static func encodeCanonical(_ object: Any) throws -> Data {
        var count = 0
        try validate(object, depth: 0, count: &count)
        guard JSONSerialization.isValidJSONObject(object) else {
            throw AustinFailure("json_type")
        }
        let encoded: Data
        do {
            encoded = try JSONSerialization.data(
                withJSONObject: object,
                options: [.sortedKeys, .withoutEscapingSlashes]
            )
        } catch {
            throw AustinFailure("json_encoding")
        }
        guard !encoded.isEmpty, encoded.count <= austinMaximumFrameBytes else {
            throw AustinFailure("json_size")
        }
        return encoded
    }

    public static func digest(_ object: Any) throws -> String {
        let digest = SHA256.hash(data: try encodeCanonical(object))
        return "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
    }

    public static func exactObject(
        _ value: Any?,
        keys: Set<String>,
        label: String
    ) throws -> [String: Any] {
        guard let object = value as? [String: Any], Set(object.keys) == keys else {
            throw AustinFailure("\(label)_schema")
        }
        return object
    }

    public static func string(
        _ value: Any?,
        label: String,
        pattern: String? = nil,
        maximumBytes: Int = austinMaximumStringBytes
    ) throws -> String {
        guard let text = value as? String,
              !text.isEmpty,
              text.utf8.count <= maximumBytes
        else {
            throw AustinFailure(label)
        }
        if let pattern,
           text.range(of: pattern, options: .regularExpression) == nil {
            throw AustinFailure(label)
        }
        return text
    }

    public static func integer(
        _ value: Any?,
        label: String,
        minimum: Int64 = 0,
        maximum: Int64 = austinMaximumSafeInteger
    ) throws -> Int64 {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              !CFNumberIsFloatType(number),
              number.int64Value >= minimum,
              number.int64Value <= maximum
        else {
            throw AustinFailure(label)
        }
        return number.int64Value
    }

    public static func boolean(_ value: Any?, label: String) throws -> Bool {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID()
        else {
            throw AustinFailure(label)
        }
        return number.boolValue
    }

    public static func strings(
        _ value: Any?,
        label: String,
        maximumCount: Int = 16
    ) throws -> [String] {
        guard let values = value as? [Any],
              !values.isEmpty,
              values.count <= maximumCount
        else {
            throw AustinFailure(label)
        }
        let parsed = try values.map { try string($0, label: label, maximumBytes: 128) }
        guard Set(parsed).count == parsed.count else {
            throw AustinFailure(label)
        }
        return parsed
    }

    private static func validate(_ value: Any, depth: Int, count: inout Int) throws {
        count += 1
        guard depth <= austinMaximumJSONDepth, count <= austinMaximumJSONItems else {
            throw AustinFailure("json_bounds")
        }
        if value is NSNull {
            throw AustinFailure("json_type")
        }
        if let text = value as? String {
            guard text.utf8.count <= austinMaximumStringBytes else {
                throw AustinFailure("json_string")
            }
            return
        }
        if let number = value as? NSNumber {
            if CFGetTypeID(number) == CFBooleanGetTypeID() {
                return
            }
            guard !CFNumberIsFloatType(number),
                  number.int64Value >= -austinMaximumSafeInteger,
                  number.int64Value <= austinMaximumSafeInteger
            else {
                throw AustinFailure("json_number")
            }
            return
        }
        if let array = value as? [Any] {
            for child in array {
                try validate(child, depth: depth + 1, count: &count)
            }
            return
        }
        if let dictionary = value as? [String: Any] {
            for (key, child) in dictionary {
                guard key.range(
                    of: "^[a-z][a-z0-9_]{0,63}$",
                    options: .regularExpression
                ) != nil else {
                    throw AustinFailure("json_key")
                }
                try validate(key, depth: depth + 1, count: &count)
                try validate(child, depth: depth + 1, count: &count)
            }
            return
        }
        throw AustinFailure("json_type")
    }
}

public enum AustinReply {
    public static func encode(
        status: String,
        reasonCode: String,
        fields: [String: Any] = [:]
    ) -> Data {
        var object = fields
        object["protocol_version"] = austinProtocolVersion
        object["reason_code"] = reasonCode
        object["status"] = status
        return (try? AustinJSON.encodeCanonical(object)) ?? Data(
            "{\"protocol_version\":1,\"reason_code\":\"reply_encoding\",\"status\":\"failed\"}"
                .utf8
        )
    }
}
