import AustinCore
import Darwin
import Foundation

@main
struct AustinAdaCrashProbeMain {
    private static let requestDigest = "sha256:" + String(repeating: "a", count: 64)

    static func main() {
        do {
            let arguments = CommandLine.arguments
            guard arguments.count >= 2 else { throw AustinFailure("probe_arguments") }
            switch arguments[1] {
            case "claim":
                try claim(arguments)
            case "claim-preparation":
                try claimPreparation(arguments)
            case "inspect":
                try inspect(arguments)
            case "inspect-preparation":
                try inspectPreparation(arguments)
            default:
                throw AustinFailure("probe_command")
            }
        } catch let failure as AustinFailure {
            writeError(failure.reasonCode)
            Darwin.exit(78)
        } catch {
            writeError("probe_internal")
            Darwin.exit(78)
        }
    }

    private static func claim(_ arguments: [String]) throws {
        guard arguments.count == 6,
              let claimedAt = Int64(arguments[4]),
              let expiresAt = Int64(arguments[5])
        else {
            throw AustinFailure("probe_arguments")
        }
        let store = try AustinAdaPermitStore(
            databaseURL: URL(fileURLWithPath: arguments[2])
        )
        try store.claim(
            permitID: arguments[3],
            requestDigest: requestDigest,
            claimedAtMilliseconds: claimedAt,
            expiresAtMilliseconds: expiresAt
        )
        try writeClaimStats(store)
    }

    private static func claimPreparation(_ arguments: [String]) throws {
        guard arguments.count == 6,
              let claimedAt = Int64(arguments[4]),
              let expiresAt = Int64(arguments[5])
        else {
            throw AustinFailure("probe_arguments")
        }
        let store = try AustinAdaPermitStore(
            databaseURL: URL(fileURLWithPath: arguments[2])
        )
        try store.claimPreparation(
            preparationID: arguments[3],
            preparationDigest: "sha256:" + String(repeating: "b", count: 64),
            claimedAtMilliseconds: claimedAt,
            expiresAtMilliseconds: expiresAt
        )
        try writeClaimStats(store)
    }

    private static func inspect(_ arguments: [String]) throws {
        guard arguments.count == 4 else { throw AustinFailure("probe_arguments") }
        let store = try AustinAdaPermitStore(
            databaseURL: URL(fileURLWithPath: arguments[2])
        )
        let stats = try store.stats()
        try writeOutput([
            "contains": try store.contains(permitID: arguments[3]),
            "high_water_ms": stats.highWaterMilliseconds,
            "permit_claim_count": stats.permitClaimCount,
            "preparation_claim_count": stats.preparationClaimCount,
            "retention_floor_ms": stats.retentionFloorMilliseconds,
            "status": "inspected",
        ])
    }

    private static func inspectPreparation(_ arguments: [String]) throws {
        guard arguments.count == 4 else { throw AustinFailure("probe_arguments") }
        let store = try AustinAdaPermitStore(
            databaseURL: URL(fileURLWithPath: arguments[2])
        )
        let stats = try store.stats()
        try writeOutput([
            "contains": try store.containsPreparation(preparationID: arguments[3]),
            "high_water_ms": stats.highWaterMilliseconds,
            "permit_claim_count": stats.permitClaimCount,
            "preparation_claim_count": stats.preparationClaimCount,
            "retention_floor_ms": stats.retentionFloorMilliseconds,
            "status": "inspected",
        ])
    }

    private static func writeClaimStats(_ store: AustinAdaPermitStore) throws {
        let stats = try store.stats()
        try writeOutput([
            "permit_claim_count": stats.permitClaimCount,
            "preparation_claim_count": stats.preparationClaimCount,
            "status": "committed",
        ])
    }

    private static func writeOutput(_ object: [String: Any]) throws {
        var payload = try AustinJSON.encodeCanonical(object)
        payload.append(0x0A)
        try FileHandle.standardOutput.write(contentsOf: payload)
    }

    private static func writeError(_ reasonCode: String) {
        FileHandle.standardError.write(Data("austin ada probe: \(reasonCode)\n".utf8))
    }
}
