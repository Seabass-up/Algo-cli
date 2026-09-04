import CryptoKit
import Foundation

public enum AustinOperation: String, CaseIterable, Sendable {
    case observe
    case activate
    case inputText = "input_text"
    case selectOption = "select_option"
    case scroll
    case upload
    case coordinateActivate = "coordinate_activate"
    case handoff
}

public enum AustinRoute: String, CaseIterable, Sendable {
    case connector
    case shortcut
    case appleEvent = "apple_event"
    case dom
    case ax
    case screenshot
    case coordinate
    case handoff
}

public enum AustinDataClass: String, CaseIterable, Sendable {
    case structural
    case `public`
    case `private`
    case secret
    case file
}

public struct AustinVerifiedEnvelope: @unchecked Sendable {
    public let requestID: String
    public let subjectID: String
    public let permitID: String
    public let requestDigest: String
    public let targetID: String
    public let targetEpoch: Int64
    public let targetRevision: String
    public let fencingToken: Int64
    public let snapshotID: String
    public let snapshotSequence: Int64
    public let operation: AustinOperation
    public let dataClass: AustinDataClass
    public let route: AustinRoute
    public let arguments: [String: Any]
    public let expiresAtMilliseconds: Int64

    public var structuralSummary: [String: Any] {
        [
            "data_class": dataClass.rawValue,
            "operation": operation.rawValue,
            "permit_id": permitID,
            "request_id": requestID,
            "route": route.rawValue,
            "snapshot_id": snapshotID,
            "target_epoch": targetEpoch,
        ]
    }
}

/// A target-free, authority-signed request to discover exactly one native
/// binding. The model and relay cannot mint this object, and the native reply
/// contains only opaque identifiers and structural geometry needed to build a
/// later target-bound `control.execute` envelope.
public struct AustinVerifiedPreparation: @unchecked Sendable {
    public let preparationID: String
    public let requestID: String
    public let subjectID: String
    public let operation: AustinOperation
    public let dataClass: AustinDataClass
    public let route: AustinRoute
    public let selector: String
    public let arguments: [String: Any]
    public let preparationDigest: String
    public let issuedAtMilliseconds: Int64
    public let expiresAtMilliseconds: Int64

    public var structuralSummary: [String: Any] {
        [
            "data_class": dataClass.rawValue,
            "operation": operation.rawValue,
            "preparation_id": preparationID,
            "request_id": requestID,
            "route": route.rawValue,
            "selector": selector,
        ]
    }
}

public final class AustinSamuelAuthority: @unchecked Sendable {
    public static let policyDigest = "sha256:ccc7c0d1ed4613685c2911d716dd618053735a649a99ed3dcb7efb3613e59e21"
    public static let maximumSnapshotAgeMilliseconds: Int64 = 30_000
    public static let clockSkewMilliseconds: Int64 = 2_000

    private let publicKey: Curve25519.Signing.PublicKey
    public let keyID: String

    public init(publicKeyData: Data) throws {
        guard publicKeyData.count == 32 else { throw AustinFailure("authority_key") }
        do {
            publicKey = try Curve25519.Signing.PublicKey(rawRepresentation: publicKeyData)
        } catch {
            throw AustinFailure("authority_key")
        }
        let digest = SHA256.hash(data: publicKeyData)
        keyID = "ed25519:" + digest.map { String(format: "%02x", $0) }.joined()
    }

    public func verify(_ payload: Data, nowMilliseconds: Int64) throws -> AustinVerifiedEnvelope {
        guard nowMilliseconds >= 0, nowMilliseconds <= austinMaximumSafeInteger else {
            throw AustinFailure("now")
        }
        let envelope = try AustinJSON.decodeCanonicalObject(payload)
        let root = try AustinJSON.exactObject(
            envelope,
            keys: ["grant", "message_type", "permit", "protocol_version", "request"],
            label: "envelope"
        )
        guard try integer(root["protocol_version"], "protocol_version", 1, 1) == 1 else {
            throw AustinFailure("protocol_version")
        }
        guard try text(root["message_type"], "message_type") == "control.execute" else {
            throw AustinFailure("message_type")
        }

        let request = try validateRequest(root["request"], nowMilliseconds: nowMilliseconds)
        let grant = try validateGrant(root["grant"], nowMilliseconds: nowMilliseconds)
        let permit = try validatePermit(root["permit"], nowMilliseconds: nowMilliseconds)

        try verifySignature(object: grant.object, kind: "control_grant", label: "grant")
        try verifySignature(object: permit.object, kind: "control_permit", label: "permit")
        try validateAuthorityBinding(request: request, grant: grant, permit: permit)

        let nativeRoutes = nativeRoutes(for: request.operation)
        guard !nativeRoutes.isEmpty else { throw AustinFailure("operation_denied") }
        let route = AustinRoute.allCases.first {
            request.routes.contains($0) && grant.routes.contains($0) && permit.routes.contains($0)
                && nativeRoutes.contains($0)
        }
        guard let route else { throw AustinFailure("no_route") }

        return AustinVerifiedEnvelope(
            requestID: request.requestID,
            subjectID: request.subjectID,
            permitID: permit.permitID,
            requestDigest: request.digest,
            targetID: request.targetID,
            targetEpoch: request.targetEpoch,
            targetRevision: request.targetRevision,
            fencingToken: request.fencingToken,
            snapshotID: request.snapshotID,
            snapshotSequence: request.snapshotSequence,
            operation: request.operation,
            dataClass: request.dataClass,
            route: route,
            arguments: request.arguments,
            expiresAtMilliseconds: permit.expiresAtMilliseconds
        )
    }

    /// Verify a one-use, target-free preparation authorization. Preparation
    /// may display fixed native confirmation UI and discover a target, but it
    /// cannot mutate that target. The resulting binding must still be paired
    /// with a separately signed, target-bound execution envelope.
    public func verifyPreparation(
        _ payload: Data,
        nowMilliseconds: Int64
    ) throws -> AustinVerifiedPreparation {
        guard nowMilliseconds >= 0, nowMilliseconds <= austinMaximumSafeInteger else {
            throw AustinFailure("now")
        }
        let envelope = try AustinJSON.decodeCanonicalObject(payload)
        let root = try AustinJSON.exactObject(
            envelope,
            keys: ["message_type", "preparation", "protocol_version"],
            label: "preparation_envelope"
        )
        guard try integer(root["protocol_version"], "protocol_version", 1, 1) == 1 else {
            throw AustinFailure("protocol_version")
        }
        guard try text(root["message_type"], "message_type") == "control.prepare" else {
            throw AustinFailure("message_type")
        }
        let row = try AustinJSON.exactObject(
            root["preparation"],
            keys: [
                "arguments", "authority_key_id", "data_class", "expires_at_ms",
                "issued_at_ms", "operation", "policy_digest", "preparation_id",
                "request_id", "route", "schema_version", "selector", "signature",
                "subject_id",
            ],
            label: "preparation"
        )
        _ = try integer(row["schema_version"], "preparation_version", 1, 1)
        try authorityFields(row, label: "preparation")
        let issued = try integer(
            row["issued_at_ms"], "preparation_issued", 0, austinMaximumSafeInteger
        )
        let expires = try integer(
            row["expires_at_ms"], "preparation_expires", 1, austinMaximumSafeInteger
        )
        guard issued < expires, expires - issued <= 60_000 else {
            throw AustinFailure("preparation_window")
        }
        guard issued <= nowMilliseconds + Self.clockSkewMilliseconds else {
            throw AustinFailure("preparation_not_yet_valid")
        }
        guard nowMilliseconds < expires else {
            throw AustinFailure("preparation_expired")
        }
        let operationText = try text(row["operation"], "preparation_operation")
        guard let operation = AustinOperation(rawValue: operationText), operation != .upload else {
            throw AustinFailure("preparation_operation")
        }
        let dataText = try text(row["data_class"], "preparation_data_class")
        guard let dataClass = AustinDataClass(rawValue: dataText),
              allowedDataClasses(for: operation).contains(dataClass)
        else {
            throw AustinFailure("preparation_data_class")
        }
        let routeText = try text(row["route"], "preparation_route")
        guard let route = AustinRoute(rawValue: routeText),
              nativeRoutes(for: operation).contains(route)
        else {
            throw AustinFailure("preparation_route")
        }
        let selector = try safeID(row["selector"], "preparation_selector")
        let arguments = try validatePreparationArguments(
            row["arguments"],
            operation: operation,
            route: route,
            selector: selector
        )
        try verifySignature(object: row, kind: "control_prepare", label: "preparation")
        return AustinVerifiedPreparation(
            preparationID: try canonicalUUID(row["preparation_id"], "preparation_id"),
            requestID: try canonicalUUID(row["request_id"], "preparation_request_id"),
            subjectID: try safeID(row["subject_id"], "preparation_subject_id"),
            operation: operation,
            dataClass: dataClass,
            route: route,
            selector: selector,
            arguments: arguments,
            preparationDigest: try AustinJSON.digest(row),
            issuedAtMilliseconds: issued,
            expiresAtMilliseconds: expires
        )
    }

    private struct RequestFields {
        let object: [String: Any]
        let digest: String
        let requestID: String
        let subjectID: String
        let targetID: String
        let targetEpoch: Int64
        let targetRevision: String
        let fencingToken: Int64
        let snapshotID: String
        let snapshotSequence: Int64
        let operation: AustinOperation
        let effects: [String]
        let dataClass: AustinDataClass
        let arguments: [String: Any]
        let argumentBytes: Int64
        let transmitBytes: Int64
        let routes: [AustinRoute]
        let maxOutputBytes: Int64
        let deadlineMilliseconds: Int64
    }

    private struct GrantFields {
        let object: [String: Any]
        let grantID: String
        let subjectID: String
        let targetIDs: [String]
        let targetKinds: [String]
        let operations: [AustinOperation]
        let effects: [String]
        let dataClasses: [AustinDataClass]
        let routes: [AustinRoute]
        let issuedAtMilliseconds: Int64
        let expiresAtMilliseconds: Int64
        let maxInputBytes: Int64
        let maxOutputBytes: Int64
        let maxTransmitBytes: Int64
    }

    private struct PermitFields {
        let object: [String: Any]
        let permitID: String
        let grantID: String
        let subjectID: String
        let requestID: String
        let requestDigest: String
        let targetKind: String
        let targetID: String
        let targetEpoch: Int64
        let targetRevision: String
        let fencingToken: Int64
        let snapshotID: String
        let sequence: Int64
        let operation: AustinOperation
        let effects: [String]
        let dataClass: AustinDataClass
        let routes: [AustinRoute]
        let inputBytes: Int64
        let outputBytes: Int64
        let transmitBytes: Int64
        let issuedAtMilliseconds: Int64
        let expiresAtMilliseconds: Int64
    }

    private func validateRequest(_ value: Any?, nowMilliseconds: Int64) throws -> RequestFields {
        let row = try AustinJSON.exactObject(
            value,
            keys: [
                "arguments", "data_class", "deadline_ms", "issued_at_ms", "max_output_bytes",
                "operation", "request_id", "requested_routes", "schema_version", "sequence",
                "session_id", "snapshot", "subject_id", "target",
            ],
            label: "request"
        )
        _ = try integer(row["schema_version"], "request_version", 1, 1)
        let requestID = try canonicalUUID(row["request_id"], "request_id")
        _ = try canonicalUUID(row["session_id"], "session_id")
        let subjectID = try safeID(row["subject_id"], "subject_id")
        _ = try integer(row["sequence"], "sequence", 1, austinMaximumSafeInteger)
        let issued = try integer(row["issued_at_ms"], "issued_at", 0, austinMaximumSafeInteger)
        let deadline = try integer(row["deadline_ms"], "deadline", 1, austinMaximumSafeInteger)
        guard issued < deadline, deadline - issued <= 120_000 else {
            throw AustinFailure("deadline_window")
        }
        guard issued <= nowMilliseconds + Self.clockSkewMilliseconds else {
            throw AustinFailure("request_not_yet_valid")
        }
        guard deadline > nowMilliseconds else { throw AustinFailure("request_expired") }

        let target = try AustinJSON.exactObject(
            row["target"],
            keys: ["epoch", "fencing_token", "kind", "revision", "target_id"],
            label: "target"
        )
        guard try text(target["kind"], "target_kind") == "desktop_surface" else {
            throw AustinFailure("target_kind_denied")
        }
        let targetID = try opaqueID(target["target_id"], "target_id")
        let targetEpoch = try integer(target["epoch"], "target_epoch", 1, austinMaximumSafeInteger)
        let targetRevision = try revision(target["revision"], "target_revision")
        let fencingToken = try integer(
            target["fencing_token"], "target_fence", 1, austinMaximumSafeInteger
        )

        let snapshot = try AustinJSON.exactObject(
            row["snapshot"],
            keys: [
                "epoch", "fencing_token", "observed_at_ms", "revision", "sequence",
                "snapshot_id", "target_id",
            ],
            label: "snapshot"
        )
        let snapshotID = try canonicalUUID(snapshot["snapshot_id"], "snapshot_id")
        let snapshotObserved = try integer(
            snapshot["observed_at_ms"], "snapshot_time", 0, austinMaximumSafeInteger
        )
        let snapshotSequence = try integer(
            snapshot["sequence"], "snapshot_sequence", 1, austinMaximumSafeInteger
        )
        guard try opaqueID(snapshot["target_id"], "snapshot_target") == targetID,
              try integer(snapshot["epoch"], "snapshot_epoch", 1, austinMaximumSafeInteger)
                == targetEpoch,
              try revision(snapshot["revision"], "snapshot_revision") == targetRevision,
              try integer(snapshot["fencing_token"], "snapshot_fence", 1, austinMaximumSafeInteger)
                == fencingToken
        else {
            throw AustinFailure("snapshot_target")
        }
        guard snapshotObserved <= nowMilliseconds + Self.clockSkewMilliseconds else {
            throw AustinFailure("snapshot_from_future")
        }
        guard nowMilliseconds <= snapshotObserved
                || nowMilliseconds - snapshotObserved <= Self.maximumSnapshotAgeMilliseconds
        else {
            throw AustinFailure("snapshot_stale")
        }

        let operationText = try text(row["operation"], "operation")
        guard let operation = AustinOperation(rawValue: operationText) else {
            throw AustinFailure("operation")
        }
        guard operation != .upload else { throw AustinFailure("operation_denied") }
        let dataText = try text(row["data_class"], "data_class")
        guard let dataClass = AustinDataClass(rawValue: dataText),
              allowedDataClasses(for: operation).contains(dataClass)
        else {
            throw AustinFailure("operation_data_class")
        }
        let arguments = try validateArguments(row["arguments"], operation: operation)
        let argumentBytes = Int64(try AustinJSON.encodeCanonical(arguments).count)
        guard argumentBytes <= 8_192 else { throw AustinFailure("input_limit") }
        let maxOutput = try integer(row["max_output_bytes"], "max_output_bytes", 1, 65_536)
        let routes = try routeList(row["requested_routes"], "requested_routes")
        guard routes.allSatisfy({ pythonRoutes(for: operation).contains($0) }) else {
            throw AustinFailure("route_denied")
        }
        if operation == .coordinateActivate && routes != [.coordinate] {
            throw AustinFailure("coordinate_route_required")
        }
        if operation == .handoff && routes != [.handoff] {
            throw AustinFailure("handoff_route_required")
        }
        let effects = expectedEffects(for: operation)
        return RequestFields(
            object: row,
            digest: try AustinJSON.digest(row),
            requestID: requestID,
            subjectID: subjectID,
            targetID: targetID,
            targetEpoch: targetEpoch,
            targetRevision: targetRevision,
            fencingToken: fencingToken,
            snapshotID: snapshotID,
            snapshotSequence: snapshotSequence,
            operation: operation,
            effects: effects,
            dataClass: dataClass,
            arguments: arguments,
            argumentBytes: argumentBytes,
            transmitBytes: 0,
            routes: routes,
            maxOutputBytes: maxOutput,
            deadlineMilliseconds: deadline
        )
    }

    private func validateGrant(_ value: Any?, nowMilliseconds: Int64) throws -> GrantFields {
        let row = try AustinJSON.exactObject(
            value,
            keys: [
                "authority_key_id", "data_classes", "effects", "expires_at_ms", "grant_id",
                "issued_at_ms", "max_input_bytes", "max_output_bytes", "max_transmit_bytes",
                "maximum_action_count", "operations", "policy_digest", "routes", "schema_version",
                "signature", "subject_id", "target_ids", "target_kinds",
            ],
            label: "grant"
        )
        _ = try integer(row["schema_version"], "grant_version", 1, 1)
        try authorityFields(row, label: "grant")
        let issued = try integer(row["issued_at_ms"], "grant_issued", 0, austinMaximumSafeInteger)
        let expires = try integer(row["expires_at_ms"], "grant_expires", 1, austinMaximumSafeInteger)
        guard issued < expires, expires - issued <= 86_400_000 else {
            throw AustinFailure("grant_window")
        }
        guard nowMilliseconds >= issued else { throw AustinFailure("grant_not_yet_valid") }
        guard nowMilliseconds < expires else { throw AustinFailure("grant_expired") }

        let operationStrings = try AustinJSON.strings(row["operations"], label: "operations")
        guard operationStrings == operationStrings.sorted(),
              operationStrings.allSatisfy({ AustinOperation(rawValue: $0) != nil })
        else {
            throw AustinFailure("operations")
        }
        let operations = operationStrings.compactMap(AustinOperation.init(rawValue:))
        let expectedGrantEffects = Array(
            Set(operations.flatMap(expectedEffects(for:)))
        ).sorted()
        let effects = try sortedStrings(row["effects"], "effects", allowed: effectVocabulary)
        guard effects == expectedGrantEffects else { throw AustinFailure("grant_effects") }
        let dataStrings = try sortedStrings(
            row["data_classes"], "data_classes", allowed: Set(AustinDataClass.allCases.map(\.rawValue))
        )
        let dataClasses = dataStrings.compactMap(AustinDataClass.init(rawValue:))
        let targetKinds = try sortedStrings(
            row["target_kinds"], "target_kinds",
            allowed: ["browser_document", "desktop_surface", "external_resource"]
        )
        let targetIDs = try AustinJSON.strings(row["target_ids"], label: "target_ids")
        guard targetIDs == targetIDs.sorted(),
              targetIDs.allSatisfy({ (try? opaqueID($0, "target_id")) != nil })
        else {
            throw AustinFailure("target_ids")
        }
        let routes = try routeList(row["routes"], "routes")
        let allowedGrantRoutes = Set(operations.flatMap(pythonRoutes(for:)))
        guard routes.allSatisfy({ allowedGrantRoutes.contains($0) }) else {
            throw AustinFailure("grant_routes")
        }
        _ = try integer(row["maximum_action_count"], "grant_action_count", 1, 64)
        let maxInput = try integer(row["max_input_bytes"], "grant_input_bytes", 1, 8_192)
        let maxOutput = try integer(row["max_output_bytes"], "grant_output_bytes", 1, 1_048_576)
        let maxTransmit = try integer(
            row["max_transmit_bytes"], "grant_transmit_bytes", 0, 67_108_864
        )
        return GrantFields(
            object: row,
            grantID: try canonicalUUID(row["grant_id"], "grant_id"),
            subjectID: try safeID(row["subject_id"], "subject_id"),
            targetIDs: targetIDs,
            targetKinds: targetKinds,
            operations: operations,
            effects: effects,
            dataClasses: dataClasses,
            routes: routes,
            issuedAtMilliseconds: issued,
            expiresAtMilliseconds: expires,
            maxInputBytes: maxInput,
            maxOutputBytes: maxOutput,
            maxTransmitBytes: maxTransmit
        )
    }

    private func validatePermit(_ value: Any?, nowMilliseconds: Int64) throws -> PermitFields {
        let row = try AustinJSON.exactObject(
            value,
            keys: [
                "authority_key_id", "data_class", "effects", "expires_at_ms", "fencing_token",
                "grant_id", "input_bytes", "issued_at_ms", "maximum_action_count", "operation",
                "output_bytes", "permit_id", "policy_digest", "request_digest", "request_id",
                "routes", "schema_version", "sequence", "signature", "snapshot_id", "subject_id",
                "target_epoch", "target_id", "target_kind", "target_revision", "transmit_bytes",
            ],
            label: "permit"
        )
        _ = try integer(row["schema_version"], "permit_version", 1, 1)
        try authorityFields(row, label: "permit")
        let issued = try integer(row["issued_at_ms"], "permit_issued", 0, austinMaximumSafeInteger)
        let expires = try integer(row["expires_at_ms"], "permit_expires", 1, austinMaximumSafeInteger)
        guard issued < expires, expires - issued <= 300_000 else {
            throw AustinFailure("permit_window")
        }
        guard nowMilliseconds >= issued else { throw AustinFailure("permit_not_yet_valid") }
        guard nowMilliseconds < expires else { throw AustinFailure("permit_expired") }
        let operationText = try text(row["operation"], "permit_operation")
        guard let operation = AustinOperation(rawValue: operationText) else {
            throw AustinFailure("permit_operation")
        }
        let dataText = try text(row["data_class"], "permit_data_class")
        guard let dataClass = AustinDataClass(rawValue: dataText) else {
            throw AustinFailure("permit_data_class")
        }
        let effects = try sortedStrings(row["effects"], "permit_effects", allowed: effectVocabulary)
        let routes = try routeList(row["routes"], "permit_routes")
        _ = try integer(row["maximum_action_count"], "permit_action_count", 1, 1)
        return PermitFields(
            object: row,
            permitID: try canonicalUUID(row["permit_id"], "permit_id"),
            grantID: try canonicalUUID(row["grant_id"], "grant_id"),
            subjectID: try safeID(row["subject_id"], "subject_id"),
            requestID: try canonicalUUID(row["request_id"], "request_id"),
            requestDigest: try digest(row["request_digest"], "request_digest"),
            targetKind: try text(row["target_kind"], "permit_target_kind"),
            targetID: try opaqueID(row["target_id"], "permit_target"),
            targetEpoch: try integer(row["target_epoch"], "permit_epoch", 1, austinMaximumSafeInteger),
            targetRevision: try revision(row["target_revision"], "permit_revision"),
            fencingToken: try integer(row["fencing_token"], "permit_fence", 1, austinMaximumSafeInteger),
            snapshotID: try canonicalUUID(row["snapshot_id"], "permit_snapshot"),
            sequence: try integer(row["sequence"], "permit_sequence", 1, austinMaximumSafeInteger),
            operation: operation,
            effects: effects,
            dataClass: dataClass,
            routes: routes,
            inputBytes: try integer(row["input_bytes"], "permit_input", 1, 8_192),
            outputBytes: try integer(row["output_bytes"], "permit_output", 1, 1_048_576),
            transmitBytes: try integer(row["transmit_bytes"], "permit_transmit", 0, 67_108_864),
            issuedAtMilliseconds: issued,
            expiresAtMilliseconds: expires
        )
    }

    private func validateAuthorityBinding(
        request: RequestFields,
        grant: GrantFields,
        permit: PermitFields
    ) throws {
        let checks: [(Bool, String)] = [
            (permit.grantID == grant.grantID, "permit_grant"),
            (permit.subjectID == request.subjectID, "permit_subject"),
            (permit.requestID == request.requestID, "permit_request"),
            (permit.requestDigest == request.digest, "permit_request_digest"),
            (permit.targetKind == "desktop_surface", "permit_target_kind"),
            (permit.targetID == request.targetID, "permit_target"),
            (permit.targetEpoch == request.targetEpoch, "permit_epoch"),
            (permit.targetRevision == request.targetRevision, "permit_revision"),
            (permit.fencingToken == request.fencingToken, "permit_fence"),
            (permit.snapshotID == request.snapshotID, "permit_snapshot"),
            (permit.operation == request.operation, "permit_operation"),
            (permit.effects == request.effects, "permit_effects"),
            (permit.dataClass == request.dataClass, "permit_data_class"),
            (permit.inputBytes == request.argumentBytes, "permit_input"),
            (permit.outputBytes == request.maxOutputBytes, "permit_output"),
            (permit.transmitBytes == request.transmitBytes, "permit_transmit"),
            (permit.issuedAtMilliseconds >= grant.issuedAtMilliseconds, "permit_issued_scope"),
            (
                permit.expiresAtMilliseconds
                    <= min(grant.expiresAtMilliseconds, request.deadlineMilliseconds),
                "permit_expiry_scope"
            ),
            (grant.subjectID == request.subjectID, "grant_subject"),
            (grant.targetIDs.contains(request.targetID), "grant_target"),
            (grant.targetKinds.contains("desktop_surface"), "grant_target_kind"),
            (grant.operations.contains(request.operation), "grant_operation"),
            (request.effects.allSatisfy(grant.effects.contains), "grant_effects"),
            (grant.dataClasses.contains(request.dataClass), "grant_data_class"),
            (request.argumentBytes <= grant.maxInputBytes, "grant_input_limit"),
            (request.maxOutputBytes <= grant.maxOutputBytes, "grant_output_limit"),
            (request.transmitBytes <= grant.maxTransmitBytes, "grant_transmit_limit"),
            (
                permit.routes.allSatisfy {
                    request.routes.contains($0) && grant.routes.contains($0)
                },
                "permit_routes"
            ),
        ]
        for (accepted, reason) in checks where !accepted {
            throw AustinFailure(reason)
        }
    }

    private func validateArguments(_ value: Any?, operation: AustinOperation) throws -> [String: Any] {
        let keys: Set<String>
        switch operation {
        case .observe: keys = []
        case .activate: keys = ["element_id"]
        case .inputText: keys = ["element_id", "replace", "text"]
        case .selectOption: keys = ["element_id", "option_id"]
        case .scroll: keys = ["delta_x", "delta_y", "element_id"]
        case .upload: keys = ["artifact_id", "byte_count", "element_id"]
        case .coordinateActivate: keys = ["viewport_height", "viewport_width", "x", "y"]
        case .handoff: keys = ["reason_code"]
        }
        let row = try AustinJSON.exactObject(value, keys: keys, label: "arguments")
        switch operation {
        case .observe:
            return row
        case .activate:
            _ = try opaqueID(row["element_id"], "element_id")
        case .inputText:
            _ = try opaqueID(row["element_id"], "element_id")
            _ = try AustinJSON.boolean(row["replace"], label: "replace")
            let input = try text(row["text"], "input_text", maximumBytes: 4_096)
            guard input.unicodeScalars.allSatisfy({ scalar in
                scalar.value == 9 || scalar.value == 10 || scalar.value == 13
                    || scalar.properties.generalCategory != .control
            }) else {
                throw AustinFailure("input_text")
            }
        case .selectOption:
            _ = try opaqueID(row["element_id"], "element_id")
            _ = try opaqueID(row["option_id"], "option_id")
        case .scroll:
            _ = try opaqueID(row["element_id"], "element_id")
            let x = try integer(row["delta_x"], "delta_x", -10_000, 10_000)
            let y = try integer(row["delta_y"], "delta_y", -10_000, 10_000)
            guard x != 0 || y != 0 else { throw AustinFailure("scroll_zero") }
        case .upload:
            throw AustinFailure("operation_denied")
        case .coordinateActivate:
            let width = try integer(row["viewport_width"], "viewport_width", 1, 16_384)
            let height = try integer(row["viewport_height"], "viewport_height", 1, 16_384)
            _ = try integer(row["x"], "coordinate_x", 0, width - 1)
            _ = try integer(row["y"], "coordinate_y", 0, height - 1)
        case .handoff:
            _ = try safeID(row["reason_code"], "reason_code")
        }
        return row
    }

    private func validatePreparationArguments(
        _ value: Any?,
        operation: AustinOperation,
        route: AustinRoute,
        selector: String
    ) throws -> [String: Any] {
        let expectedSelector: Set<String>
        let keys: Set<String>
        switch (operation, route) {
        case (.activate, .ax):
            expectedSelector = ["focused_element"]
            keys = []
        case (.selectOption, .ax):
            expectedSelector = ["focused_element"]
            keys = ["option_id"]
        case (.scroll, .ax):
            expectedSelector = ["focused_element"]
            keys = ["delta_x", "delta_y"]
        case (.activate, .appleEvent):
            expectedSelector = ["activate_finder", "activate_system_settings"]
            keys = []
        case (.activate, .shortcut):
            expectedSelector = ["review_current_task"]
            keys = []
        case (.coordinateActivate, .coordinate):
            expectedSelector = ["frontmost_point"]
            keys = ["x", "y"]
        case (.observe, .screenshot):
            // Picker-scoped capture must originate from a direct gesture in the
            // native app. The CLI preparation protocol authorizes only the
            // separately confirmed persistent one-frame path.
            expectedSelector = ["persistent_programmatic"]
            keys = []
        default:
            throw AustinFailure("preparation_route_operation")
        }
        guard expectedSelector.contains(selector) else {
            throw AustinFailure("preparation_selector")
        }
        let row = try AustinJSON.exactObject(value, keys: keys, label: "preparation_arguments")
        switch (operation, route) {
        case (.selectOption, .ax):
            _ = try opaqueID(row["option_id"], "option_id")
        case (.scroll, .ax):
            let x = try integer(row["delta_x"], "delta_x", -10_000, 10_000)
            let y = try integer(row["delta_y"], "delta_y", -10_000, 10_000)
            guard x != 0 || y != 0 else { throw AustinFailure("scroll_zero") }
        case (.coordinateActivate, .coordinate):
            _ = try integer(row["x"], "coordinate_x", 0, 16_383)
            _ = try integer(row["y"], "coordinate_y", 0, 16_383)
        default:
            break
        }
        return row
    }

    private func verifySignature(object: [String: Any], kind: String, label: String) throws {
        guard let signatureText = object["signature"] as? String,
              signatureText.count == 86,
              let signature = decodeBase64URL(signatureText),
              signature.count == 64
        else {
            throw AustinFailure("\(label)_signature")
        }
        var unsigned = object
        unsigned.removeValue(forKey: "signature")
        var signedData = Data("algo-control-v1\0\(kind)\0".utf8)
        signedData.append(try AustinJSON.encodeCanonical(unsigned))
        guard publicKey.isValidSignature(signature, for: signedData) else {
            throw AustinFailure("\(label)_signature_invalid")
        }
    }

    private func authorityFields(_ row: [String: Any], label: String) throws {
        guard try text(row["authority_key_id"], "\(label)_key") == keyID else {
            throw AustinFailure("\(label)_key")
        }
        guard try digest(row["policy_digest"], "\(label)_policy") == Self.policyDigest else {
            throw AustinFailure("\(label)_policy")
        }
        _ = try text(
            row["signature"], "\(label)_signature",
            pattern: "^[A-Za-z0-9_-]{86}$", maximumBytes: 86
        )
    }

    private let effectVocabulary: Set<String> = ["handoff", "input", "read", "transmit", "ui_mutation"]

    private func expectedEffects(for operation: AustinOperation) -> [String] {
        switch operation {
        case .observe: return ["read"]
        case .activate, .selectOption, .scroll, .coordinateActivate: return ["ui_mutation"]
        case .inputText: return ["input", "ui_mutation"]
        case .upload: return ["transmit", "ui_mutation"]
        case .handoff: return ["handoff"]
        }
    }

    private func allowedDataClasses(for operation: AustinOperation) -> Set<AustinDataClass> {
        switch operation {
        case .observe: return [.structural, .public, .private]
        case .activate, .scroll, .coordinateActivate: return [.structural]
        case .inputText: return [.private]
        case .selectOption: return [.structural, .private]
        case .upload: return [.file, .private]
        case .handoff: return Set(AustinDataClass.allCases)
        }
    }

    private func pythonRoutes(for operation: AustinOperation) -> Set<AustinRoute> {
        switch operation {
        case .observe: return [.connector, .dom, .ax, .screenshot]
        case .activate: return [.connector, .shortcut, .appleEvent, .dom, .ax, .screenshot, .coordinate]
        case .inputText: return [.connector, .shortcut, .appleEvent, .dom, .ax]
        case .selectOption: return [.connector, .shortcut, .appleEvent, .dom, .ax]
        case .scroll: return [.connector, .dom, .ax, .coordinate]
        case .upload: return [.connector, .dom, .ax]
        case .coordinateActivate: return [.coordinate]
        case .handoff: return [.handoff]
        }
    }

    private func nativeRoutes(for operation: AustinOperation) -> Set<AustinRoute> {
        switch operation {
        case .observe: return [.ax, .screenshot]
        case .activate: return [.shortcut, .appleEvent, .ax]
        case .inputText, .selectOption: return [.ax]
        case .scroll: return [.ax, .coordinate]
        case .coordinateActivate: return [.coordinate]
        case .handoff: return [.handoff]
        case .upload: return []
        }
    }

    private func routeList(_ value: Any?, _ label: String) throws -> [AustinRoute] {
        let values = try AustinJSON.strings(value, label: label, maximumCount: AustinRoute.allCases.count)
        let order = Dictionary(uniqueKeysWithValues: AustinRoute.allCases.enumerated().map { ($1.rawValue, $0) })
        guard values.allSatisfy({ order[$0] != nil }),
              values == values.sorted(by: { order[$0, default: 99] < order[$1, default: 99] })
        else {
            throw AustinFailure(label)
        }
        return values.compactMap(AustinRoute.init(rawValue:))
    }

    private func sortedStrings(_ value: Any?, _ label: String, allowed: Set<String>) throws -> [String] {
        let values = try AustinJSON.strings(value, label: label)
        guard values == values.sorted(), values.allSatisfy(allowed.contains) else {
            throw AustinFailure(label)
        }
        return values
    }

    private func text(
        _ value: Any?, _ label: String,
        pattern: String? = nil, maximumBytes: Int = 8_192
    ) throws -> String {
        try AustinJSON.string(value, label: label, pattern: pattern, maximumBytes: maximumBytes)
    }

    private func integer(
        _ value: Any?, _ label: String, _ minimum: Int64, _ maximum: Int64
    ) throws -> Int64 {
        try AustinJSON.integer(value, label: label, minimum: minimum, maximum: maximum)
    }

    private func safeID(_ value: Any?, _ label: String) throws -> String {
        try text(value, label, pattern: "^[a-z][a-z0-9._:-]{0,127}$", maximumBytes: 128)
    }

    private func opaqueID(_ value: Any?, _ label: String) throws -> String {
        try text(value, label, pattern: "^hmac-sha256:[0-9a-f]{64}$", maximumBytes: 76)
    }

    private func digest(_ value: Any?, _ label: String) throws -> String {
        try text(value, label, pattern: "^sha256:[0-9a-f]{64}$", maximumBytes: 71)
    }

    private func revision(_ value: Any?, _ label: String) throws -> String {
        try text(value, label, pattern: "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", maximumBytes: 128)
    }

    private func canonicalUUID(_ value: Any?, _ label: String) throws -> String {
        let candidate = try text(
            value, label,
            pattern: "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            maximumBytes: 36
        )
        guard let parsed = UUID(uuidString: candidate),
              parsed.uuidString.lowercased() == candidate,
              candidate != "00000000-0000-0000-0000-000000000000"
        else {
            throw AustinFailure(label)
        }
        return candidate
    }
}
