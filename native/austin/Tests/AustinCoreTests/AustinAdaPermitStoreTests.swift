import Darwin
import Dispatch
import Foundation
import SQLite3
import Testing
@testable import AustinCore

private let permitID = "00000000-0000-4000-8000-000000000501"
private let permitDigest = "sha256:" + String(repeating: "a", count: 64)
private let preparationID = "00000000-0000-4000-8000-000000000601"
private let preparationDigest = "sha256:" + String(repeating: "b", count: 64)

private func privateStoreURL() throws -> URL {
    let root = FileManager.default.temporaryDirectory
        .appendingPathComponent("AustinAda-\(UUID().uuidString)", isDirectory: true)
    return root.appendingPathComponent("AdaPermitClaims.sqlite3")
}

private func failureReason(_ operation: () throws -> Void) -> String? {
    do {
        try operation()
        return nil
    } catch let failure as AustinFailure {
        return failure.reasonCode
    } catch {
        return "unexpected_error"
    }
}

private func testID(_ suffix: Int) -> String {
    String(format: "00000000-0000-4000-8000-%012x", suffix)
}

private func executeSQLite(databaseURL: URL, sql: String) throws {
    var database: OpaquePointer?
    guard sqlite3_open_v2(
        databaseURL.path,
        &database,
        SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE,
        nil
    ) == SQLITE_OK, let database else {
        if let database { sqlite3_close_v2(database) }
        throw AustinFailure("test_sqlite_open")
    }
    defer { sqlite3_close_v2(database) }
    guard sqlite3_exec(database, sql, nil, nil, nil) == SQLITE_OK else {
        throw AustinFailure("test_sqlite_write")
    }
}

@Test func permitClaimIsDurableAndReplayFailsClosed() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    do {
        let store = try AustinAdaPermitStore(databaseURL: url)
        try store.claim(
            permitID: permitID,
            requestDigest: permitDigest,
            claimedAtMilliseconds: 1_000,
            expiresAtMilliseconds: 2_000
        )
        #expect(try store.contains(permitID: permitID))
        #expect(throws: AustinFailure.self) {
            try store.claim(
                permitID: permitID,
                requestDigest: permitDigest,
                claimedAtMilliseconds: 1_001,
                expiresAtMilliseconds: 2_000
            )
        }
    }
    let reopened = try AustinAdaPermitStore(databaseURL: url)
    #expect(try reopened.contains(permitID: permitID))
    #expect(throws: AustinFailure.self) {
        try reopened.claim(
            permitID: permitID,
            requestDigest: permitDigest,
            claimedAtMilliseconds: 1_002,
            expiresAtMilliseconds: 2_000
        )
    }
}

@Test func permitStoreCreatesPrivateRegularFiles() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    _ = try AustinAdaPermitStore(databaseURL: url)
    var directoryInfo = stat()
    var fileInfo = stat()
    #expect(lstat(url.deletingLastPathComponent().path, &directoryInfo) == 0)
    #expect(lstat(url.path, &fileInfo) == 0)
    #expect((directoryInfo.st_mode & 0o077) == 0)
    #expect((fileInfo.st_mode & 0o077) == 0)
    #expect((fileInfo.st_mode & S_IFMT) == S_IFREG)
    #expect(fileInfo.st_nlink == 1)
}

@Test func durableHighWaterCompactsExpiredClaimsWithoutReopeningRollback() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    do {
        let store = try AustinAdaPermitStore(databaseURL: url)
        try store.claim(
            permitID: testID(701),
            requestDigest: permitDigest,
            claimedAtMilliseconds: 10_000,
            expiresAtMilliseconds: 11_000
        )
        try store.claimPreparation(
            preparationID: testID(702),
            preparationDigest: preparationDigest,
            claimedAtMilliseconds: 17_000,
            expiresAtMilliseconds: 18_000
        )
        let stats = try store.stats()
        #expect(stats.highWaterMilliseconds == 17_000)
        #expect(stats.retentionFloorMilliseconds == 12_000)
        #expect(stats.permitClaimCount == 0)
        #expect(stats.preparationClaimCount == 1)
        #expect(try store.contains(permitID: testID(701)) == false)
    }

    let reopened = try AustinAdaPermitStore(databaseURL: url)
    #expect(try reopened.stats().highWaterMilliseconds == 17_000)
    #expect(failureReason {
        try reopened.claim(
            permitID: testID(703),
            requestDigest: permitDigest,
            claimedAtMilliseconds: 11_999,
            expiresAtMilliseconds: 18_001
        )
    } == "permit_store_clock_rollback")

    // A delayed caller inside the scheduling window remains admissible only
    // when its signed object is still live beyond the newest observed clock.
    try reopened.claim(
        permitID: testID(704),
        requestDigest: permitDigest,
        claimedAtMilliseconds: 12_000,
        expiresAtMilliseconds: 18_001
    )
    #expect(failureReason {
        try reopened.claim(
            permitID: testID(705),
            requestDigest: permitDigest,
            claimedAtMilliseconds: 13_000,
            expiresAtMilliseconds: 17_000
        )
    } == "permit_store_stale_claim")
}

@Test func claimCapacityFailsClosedThenReclaimsExpiredRows() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    let store = try AustinAdaPermitStore(
        databaseURL: url,
        maximumClaimsPerNamespace: 2
    )
    for index in 0..<2 {
        try store.claim(
            permitID: testID(800 + index),
            requestDigest: permitDigest,
            claimedAtMilliseconds: 10_000 + Int64(index),
            expiresAtMilliseconds: 20_000
        )
    }
    #expect(failureReason {
        try store.claim(
            permitID: testID(802),
            requestDigest: permitDigest,
            claimedAtMilliseconds: 10_002,
            expiresAtMilliseconds: 20_000
        )
    } == "permit_store_capacity")
    #expect(try store.stats().permitClaimCount == 2)

    // Capacity in one namespace does not consume the other namespace.
    try store.claimPreparation(
        preparationID: testID(803),
        preparationDigest: preparationDigest,
        claimedAtMilliseconds: 10_003,
        expiresAtMilliseconds: 20_001
    )
    try store.claim(
        permitID: testID(804),
        requestDigest: permitDigest,
        claimedAtMilliseconds: 20_001,
        expiresAtMilliseconds: 21_000
    )
    let stats = try store.stats()
    #expect(stats.permitClaimCount == 1)
    #expect(stats.preparationClaimCount == 0)
    #expect(try store.contains(permitID: testID(800)) == false)
    #expect(try store.containsPreparation(preparationID: testID(803)) == false)
}

@Test func repeatedExpiredClaimChurnReusesBoundedStorage() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    let store = try AustinAdaPermitStore(
        databaseURL: url,
        maximumClaimsPerNamespace: 8
    )
    for index in 0..<256 {
        let now = 10_000 + Int64(index * 1_000)
        try store.claim(
            permitID: testID(1_000 + index),
            requestDigest: permitDigest,
            claimedAtMilliseconds: now,
            expiresAtMilliseconds: now + 1
        )
    }
    let stats = try store.stats()
    #expect(stats.permitClaimCount == 1)
    #expect(stats.preparationClaimCount == 0)
    let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
    let databaseBytes = try #require(attributes[.size] as? NSNumber).int64Value
    #expect(databaseBytes <= 32 * 1_024 * 1_024)
}

@Test func legacyClaimsSeedDurableStateBeforeCompaction() throws {
    let url = try privateStoreURL()
    let directory = url.deletingLastPathComponent()
    defer { try? FileManager.default.removeItem(at: directory) }
    try FileManager.default.createDirectory(
        at: directory,
        withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]
    )
    try executeSQLite(
        databaseURL: url,
        sql: """
        CREATE TABLE permit_claims (
            permit_id TEXT PRIMARY KEY NOT NULL,
            request_digest TEXT NOT NULL,
            claimed_at_ms INTEGER NOT NULL,
            expires_at_ms INTEGER NOT NULL,
            CHECK(length(permit_id) = 36),
            CHECK(length(request_digest) = 71),
            CHECK(claimed_at_ms >= 0),
            CHECK(expires_at_ms > claimed_at_ms)
        ) STRICT, WITHOUT ROWID;
        CREATE TABLE preparation_claims (
            preparation_id TEXT PRIMARY KEY NOT NULL,
            preparation_digest TEXT NOT NULL,
            claimed_at_ms INTEGER NOT NULL,
            expires_at_ms INTEGER NOT NULL,
            CHECK(length(preparation_id) = 36),
            CHECK(length(preparation_digest) = 71),
            CHECK(claimed_at_ms >= 0),
            CHECK(expires_at_ms > claimed_at_ms)
        ) STRICT, WITHOUT ROWID;
        INSERT INTO permit_claims VALUES (
            '00000000-0000-4000-8000-000000000901',
            'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            1234,
            2000
        );
        """
    )
    #expect(chmod(url.path, 0o600) == 0)

    let migrated = try AustinAdaPermitStore(databaseURL: url)
    let stats = try migrated.stats()
    #expect(stats.highWaterMilliseconds == 1_234)
    #expect(stats.permitClaimCount == 1)
    #expect(stats.preparationClaimCount == 0)
}

@Test func inconsistentDurableStateFailsClosedOnReopen() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    do {
        let store = try AustinAdaPermitStore(databaseURL: url)
        try store.claim(
            permitID: testID(950),
            requestDigest: permitDigest,
            claimedAtMilliseconds: 1_000,
            expiresAtMilliseconds: 2_000
        )
    }
    try executeSQLite(
        databaseURL: url,
        sql: "UPDATE ada_store_state SET permit_claim_count = 0 WHERE singleton = 1"
    )
    #expect(failureReason {
        _ = try AustinAdaPermitStore(databaseURL: url)
    } == "permit_store_state")
}

@Test func preparationClaimIsDurableAndIndependentFromPermitNamespace() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    do {
        let store = try AustinAdaPermitStore(databaseURL: url)
        try store.claimPreparation(
            preparationID: preparationID,
            preparationDigest: preparationDigest,
            claimedAtMilliseconds: 1_000,
            expiresAtMilliseconds: 2_000
        )
        #expect(try store.containsPreparation(preparationID: preparationID))
        #expect(try store.contains(permitID: preparationID) == false)
        #expect(throws: AustinFailure.self) {
            try store.claimPreparation(
                preparationID: preparationID,
                preparationDigest: preparationDigest,
                claimedAtMilliseconds: 1_001,
                expiresAtMilliseconds: 2_000
            )
        }
    }
    let reopened = try AustinAdaPermitStore(databaseURL: url)
    #expect(try reopened.containsPreparation(preparationID: preparationID))
}

@Test func permitStoreRejectsSymlinksAndCorruption() throws {
    let url = try privateStoreURL()
    let directory = url.deletingLastPathComponent()
    defer { try? FileManager.default.removeItem(at: directory) }
    try FileManager.default.createDirectory(
        at: directory,
        withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]
    )
    let target = directory.appendingPathComponent("target")
    #expect(FileManager.default.createFile(atPath: target.path, contents: Data(), attributes: [.posixPermissions: 0o600]))
    try FileManager.default.createSymbolicLink(at: url, withDestinationURL: target)
    #expect(throws: AustinFailure.self) {
        try AustinAdaPermitStore(databaseURL: url)
    }

    try FileManager.default.removeItem(at: url)
    try Data("not sqlite".utf8).write(to: url)
    #expect(chmod(url.path, 0o600) == 0)
    #expect(throws: AustinFailure.self) {
        try AustinAdaPermitStore(databaseURL: url)
    }
}

@Test func permitStoreRejectsPreexistingSidecarSymlink() throws {
    let url = try privateStoreURL()
    let directory = url.deletingLastPathComponent()
    defer { try? FileManager.default.removeItem(at: directory) }
    try FileManager.default.createDirectory(
        at: directory,
        withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700]
    )
    let target = directory.appendingPathComponent("sidecar-target")
    #expect(FileManager.default.createFile(
        atPath: target.path,
        contents: Data(),
        attributes: [.posixPermissions: 0o600]
    ))
    try FileManager.default.createSymbolicLink(
        atPath: url.path + "-wal",
        withDestinationPath: target.path
    )
    #expect(failureReason {
        _ = try AustinAdaPermitStore(databaseURL: url)
    } == "permit_store_file_permissions")
}

@Test func permitStoreRejectsInvalidClaimsWithoutCreatingRows() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    let store = try AustinAdaPermitStore(databaseURL: url)
    #expect(throws: AustinFailure.self) {
        try store.claim(
            permitID: "not-a-permit",
            requestDigest: permitDigest,
            claimedAtMilliseconds: 1_000,
            expiresAtMilliseconds: 2_000
        )
    }
    #expect(failureReason {
        try store.claim(
            permitID: permitID,
            requestDigest: permitDigest,
            claimedAtMilliseconds: 1_000,
            expiresAtMilliseconds: 301_001
        )
    } == "permit_claim_time")
    #expect(failureReason {
        try store.claimPreparation(
            preparationID: preparationID,
            preparationDigest: preparationDigest,
            claimedAtMilliseconds: 1_000,
            expiresAtMilliseconds: 63_001
        )
    } == "preparation_claim_time")
    #expect(failureReason {
        _ = try AustinAdaPermitStore(
            databaseURL: try privateStoreURL(),
            maximumClaimsPerNamespace: 32_769
        )
    } == "permit_store_capacity_configuration")
    #expect(throws: AustinFailure.self) {
        try store.claim(
            permitID: permitID,
            requestDigest: "sha256:bad",
            claimedAtMilliseconds: 1_000,
            expiresAtMilliseconds: 2_000
        )
    }
}

private final class PermitClaimCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0

    func increment() {
        lock.lock()
        value += 1
        lock.unlock()
    }

    func read() -> Int {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

@Test func concurrentPermitClaimsHaveExactlyOneWinner() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    let store = try AustinAdaPermitStore(databaseURL: url)
    let winners = PermitClaimCounter()
    DispatchQueue.concurrentPerform(iterations: 32) { index in
        if (try? store.claim(
            permitID: permitID,
            requestDigest: permitDigest,
            claimedAtMilliseconds: 1_000 + Int64(index),
            expiresAtMilliseconds: 3_000
        )) != nil {
            winners.increment()
        }
    }
    #expect(winners.read() == 1)
    #expect(try store.contains(permitID: permitID))
}

@Test func concurrentPreparationClaimsHaveExactlyOneWinner() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    let store = try AustinAdaPermitStore(databaseURL: url)
    let winners = PermitClaimCounter()
    DispatchQueue.concurrentPerform(iterations: 32) { index in
        if (try? store.claimPreparation(
            preparationID: preparationID,
            preparationDigest: preparationDigest,
            claimedAtMilliseconds: 1_000 + Int64(index),
            expiresAtMilliseconds: 3_000
        )) != nil {
            winners.increment()
        }
    }
    #expect(winners.read() == 1)
    #expect(try store.containsPreparation(preparationID: preparationID))
}

@Test func independentStoreConnectionsSerializeReplayClaims() throws {
    let url = try privateStoreURL()
    defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
    let first = try AustinAdaPermitStore(databaseURL: url)
    let second = try AustinAdaPermitStore(databaseURL: url)
    let winners = PermitClaimCounter()
    DispatchQueue.concurrentPerform(iterations: 2) { index in
        let store = index == 0 ? first : second
        if (try? store.claim(
            permitID: testID(990),
            requestDigest: permitDigest,
            claimedAtMilliseconds: 1_000 + Int64(index),
            expiresAtMilliseconds: 3_000
        )) != nil {
            winners.increment()
        }
    }
    #expect(winners.read() == 1)
    #expect(try first.stats().permitClaimCount == 1)
    #expect(try second.stats().permitClaimCount == 1)
}
