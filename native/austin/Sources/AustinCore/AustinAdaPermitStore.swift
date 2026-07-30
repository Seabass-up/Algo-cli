import Darwin
import Foundation
import SQLite3

private let austinSQLiteTransient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

public struct AustinAdaPermitStoreStats: Equatable, Sendable {
    public let highWaterMilliseconds: Int64
    public let retentionFloorMilliseconds: Int64
    public let permitClaimCount: Int64
    public let preparationClaimCount: Int64
}

public final class AustinAdaPermitStore: @unchecked Sendable {
    // A claim timestamp may be captured just before another connection commits a
    // slightly newer timestamp. A five-second admission window tolerates that
    // benign scheduling inversion, while the transaction still requires the
    // signed object to remain live beyond the durable high-water mark. Claims
    // expired at that mark can therefore be compacted without reopening replay.
    private static let clockReorderingWindowMilliseconds: Int64 = 5_000
    private static let defaultMaximumClaimsPerNamespace: Int64 = 32_768
    private static let maximumDatabaseBytes: Int64 = 32 * 1_024 * 1_024
    private static let maximumJournalBytes: Int64 = 1 * 1_024 * 1_024
    private static let stateSchemaVersion: Int64 = 1

    private enum ClaimNamespace {
        case permit
        case preparation
    }

    private struct ClaimMaintenance {
        let highWaterMilliseconds: Int64
        let permitClaimCount: Int64
        let preparationClaimCount: Int64
    }

    private let lock = NSLock()
    private let maximumClaimsPerNamespace: Int64
    private var database: OpaquePointer?
    public let databaseURL: URL

    public convenience init(databaseURL: URL) throws {
        try self.init(
            databaseURL: databaseURL,
            maximumClaimsPerNamespace: Self.defaultMaximumClaimsPerNamespace
        )
    }

    init(databaseURL: URL, maximumClaimsPerNamespace: Int64) throws {
        guard databaseURL.isFileURL, databaseURL.path.hasPrefix("/") else {
            throw AustinFailure("permit_store_path")
        }
        guard maximumClaimsPerNamespace > 0,
              maximumClaimsPerNamespace <= Self.defaultMaximumClaimsPerNamespace
        else {
            throw AustinFailure("permit_store_capacity_configuration")
        }
        self.maximumClaimsPerNamespace = maximumClaimsPerNamespace
        self.databaseURL = databaseURL.standardizedFileURL
        try Self.preparePrivatePath(self.databaseURL)

        var opened: OpaquePointer?
        // Apple's SQLite VFS rejects SQLITE_OPEN_NOFOLLOW even on regular files.
        // The path is therefore pre-created with O_NOFOLLOW in a private directory,
        // then revalidated immediately after SQLite opens it.
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX
            | SQLITE_OPEN_PRIVATECACHE
        let result = sqlite3_open_v2(self.databaseURL.path, &opened, flags, nil)
        guard result == SQLITE_OK, let opened else {
            if let opened { sqlite3_close_v2(opened) }
            throw AustinFailure("permit_store_open")
        }
        database = opened
        do {
            try execute("PRAGMA trusted_schema=OFF")
            try execute("PRAGMA foreign_keys=ON")
            try execute("PRAGMA synchronous=FULL")
            guard try scalarText(
                "PRAGMA journal_mode=WAL",
                reason: "permit_store_journal"
            ) == "wal" else {
                throw AustinFailure("permit_store_journal")
            }
            try execute("PRAGMA wal_autocheckpoint=1")
            guard try scalarInt64(
                "PRAGMA journal_size_limit=\(Self.maximumJournalBytes)",
                reason: "permit_store_journal"
            ) == Self.maximumJournalBytes else {
                throw AustinFailure("permit_store_journal")
            }
            try execute("PRAGMA cache_size=-2048")
            guard try scalarInt64(
                "PRAGMA trusted_schema",
                reason: "permit_store_configuration"
            ) == 0,
            try scalarInt64(
                "PRAGMA foreign_keys",
                reason: "permit_store_configuration"
            ) == 1,
            try scalarInt64(
                "PRAGMA synchronous",
                reason: "permit_store_configuration"
            ) == 2,
            try scalarInt64(
                "PRAGMA wal_autocheckpoint",
                reason: "permit_store_configuration"
            ) == 1,
            try scalarInt64(
                "PRAGMA cache_size",
                reason: "permit_store_configuration"
            ) == -2_048
            else {
                throw AustinFailure("permit_store_configuration")
            }
            let pageSize = try scalarInt64("PRAGMA page_size", reason: "permit_store_size")
            guard pageSize > 0, pageSize <= Self.maximumDatabaseBytes else {
                throw AustinFailure("permit_store_size")
            }
            let maximumPageCount = Self.maximumDatabaseBytes / pageSize
            try execute("PRAGMA max_page_count=\(maximumPageCount)")
            guard try scalarInt64(
                "PRAGMA max_page_count",
                reason: "permit_store_size"
            ) <= maximumPageCount else {
                throw AustinFailure("permit_store_size")
            }
            guard sqlite3_busy_timeout(opened, 5_000) == SQLITE_OK else {
                throw AustinFailure("permit_store_busy_timeout")
            }
            try execute(
                """
                CREATE TABLE IF NOT EXISTS permit_claims (
                    permit_id TEXT PRIMARY KEY NOT NULL,
                    request_digest TEXT NOT NULL,
                    claimed_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    CHECK(length(permit_id) = 36),
                    CHECK(length(request_digest) = 71),
                    CHECK(claimed_at_ms >= 0),
                    CHECK(expires_at_ms > claimed_at_ms)
                ) STRICT, WITHOUT ROWID
                """
            )
            try execute(
                "CREATE INDEX IF NOT EXISTS permit_claims_expiry_idx "
                    + "ON permit_claims(expires_at_ms)"
            )
            try execute(
                """
                CREATE TABLE IF NOT EXISTS preparation_claims (
                    preparation_id TEXT PRIMARY KEY NOT NULL,
                    preparation_digest TEXT NOT NULL,
                    claimed_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    CHECK(length(preparation_id) = 36),
                    CHECK(length(preparation_digest) = 71),
                    CHECK(claimed_at_ms >= 0),
                    CHECK(expires_at_ms > claimed_at_ms)
                ) STRICT, WITHOUT ROWID
                """
            )
            try execute(
                "CREATE INDEX IF NOT EXISTS preparation_claims_expiry_idx "
                    + "ON preparation_claims(expires_at_ms)"
            )
            try execute(
                """
                CREATE TABLE IF NOT EXISTS ada_store_state (
                    singleton INTEGER PRIMARY KEY NOT NULL CHECK(singleton = 1),
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    high_water_ms INTEGER NOT NULL CHECK(high_water_ms >= 0),
                    permit_claim_count INTEGER NOT NULL CHECK(permit_claim_count >= 0),
                    preparation_claim_count INTEGER NOT NULL
                        CHECK(preparation_claim_count >= 0)
                ) STRICT, WITHOUT ROWID
                """
            )
            guard try scalarText(
                "PRAGMA quick_check(1)",
                reason: "permit_store_integrity"
            ) == "ok" else {
                throw AustinFailure("permit_store_integrity")
            }
            try execute("BEGIN IMMEDIATE")
            var shouldRollbackState = true
            do {
                try initializeStateIfNeeded()
                try assertStateConsistency()
                try execute("COMMIT")
                shouldRollbackState = false
            } catch {
                if shouldRollbackState {
                    _ = sqlite3_exec(opened, "ROLLBACK", nil, nil, nil)
                }
                throw error
            }
            try Self.assertPrivateDatabaseFiles(self.databaseURL)
        } catch {
            sqlite3_close_v2(opened)
            database = nil
            throw error
        }
    }

    deinit {
        if let database {
            sqlite3_close_v2(database)
        }
    }

    public func claim(
        permitID: String,
        requestDigest: String,
        claimedAtMilliseconds: Int64,
        expiresAtMilliseconds: Int64
    ) throws {
        guard Self.isCanonicalUUID(permitID) else { throw AustinFailure("permit_id") }
        guard requestDigest.range(
            of: "^sha256:[0-9a-f]{64}$",
            options: .regularExpression
        ) != nil else {
            throw AustinFailure("request_digest")
        }
        guard claimedAtMilliseconds >= 0,
              expiresAtMilliseconds > claimedAtMilliseconds,
              expiresAtMilliseconds - claimedAtMilliseconds <= 300_000,
              expiresAtMilliseconds <= austinMaximumSafeInteger
        else {
            throw AustinFailure("permit_claim_time")
        }

        lock.lock()
        defer { lock.unlock() }
        guard let database else { throw AustinFailure("permit_store_closed") }
        try execute("BEGIN IMMEDIATE")
        Self.hardeningCrashIfRequested("after_begin")
        var shouldRollback = true
        defer {
            if shouldRollback {
                _ = sqlite3_exec(database, "ROLLBACK", nil, nil, nil)
            }
        }
        let maintenance = try maintainClaims(
            claimedAtMilliseconds: claimedAtMilliseconds,
            expiresAtMilliseconds: expiresAtMilliseconds,
            namespace: .permit
        )
        Self.hardeningCrashIfRequested("after_maintenance")
        var statement: OpaquePointer?
        let sql = """
            INSERT INTO permit_claims (
                permit_id, request_digest, claimed_at_ms, expires_at_ms
            ) VALUES (?1, ?2, ?3, ?4)
            """
        guard sqlite3_prepare_v3(database, sql, -1, UInt32(SQLITE_PREPARE_PERSISTENT), &statement, nil)
                == SQLITE_OK,
              let statement
        else {
            throw AustinFailure("permit_store_prepare")
        }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_bind_text(statement, 1, permitID, -1, austinSQLiteTransient) == SQLITE_OK,
              sqlite3_bind_text(statement, 2, requestDigest, -1, austinSQLiteTransient) == SQLITE_OK,
              sqlite3_bind_int64(statement, 3, claimedAtMilliseconds) == SQLITE_OK,
              sqlite3_bind_int64(statement, 4, expiresAtMilliseconds) == SQLITE_OK
        else {
            throw AustinFailure("permit_store_bind")
        }
        let step = sqlite3_step(statement)
        if step == SQLITE_CONSTRAINT {
            throw AustinFailure("permit_replay")
        }
        guard step == SQLITE_DONE else { throw AustinFailure("permit_store_write") }
        Self.hardeningCrashIfRequested("after_insert")
        try commitState(maintenance, adding: .permit)
        Self.hardeningCrashIfRequested("after_state")
        try execute("COMMIT")
        shouldRollback = false
        Self.hardeningCrashIfRequested("after_commit")
        try Self.assertPrivateDatabaseFiles(databaseURL)
    }

    public func contains(permitID: String) throws -> Bool {
        guard Self.isCanonicalUUID(permitID) else { throw AustinFailure("permit_id") }
        lock.lock()
        defer { lock.unlock() }
        guard let database else { throw AustinFailure("permit_store_closed") }
        var statement: OpaquePointer?
        guard sqlite3_prepare_v3(
            database,
            "SELECT 1 FROM permit_claims WHERE permit_id = ?1 LIMIT 1",
            -1,
            UInt32(SQLITE_PREPARE_PERSISTENT),
            &statement,
            nil
        ) == SQLITE_OK, let statement else {
            throw AustinFailure("permit_store_prepare")
        }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_bind_text(statement, 1, permitID, -1, austinSQLiteTransient) == SQLITE_OK else {
            throw AustinFailure("permit_store_bind")
        }
        let step = sqlite3_step(statement)
        if step == SQLITE_ROW { return true }
        if step == SQLITE_DONE { return false }
        throw AustinFailure("permit_store_read")
    }

    public func claimPreparation(
        preparationID: String,
        preparationDigest: String,
        claimedAtMilliseconds: Int64,
        expiresAtMilliseconds: Int64
    ) throws {
        guard Self.isCanonicalUUID(preparationID) else {
            throw AustinFailure("preparation_id")
        }
        guard preparationDigest.range(
            of: "^sha256:[0-9a-f]{64}$",
            options: .regularExpression
        ) != nil else {
            throw AustinFailure("preparation_digest")
        }
        guard claimedAtMilliseconds >= 0,
              expiresAtMilliseconds > claimedAtMilliseconds,
              expiresAtMilliseconds - claimedAtMilliseconds <= 62_000,
              expiresAtMilliseconds <= austinMaximumSafeInteger
        else {
            throw AustinFailure("preparation_claim_time")
        }

        lock.lock()
        defer { lock.unlock() }
        guard let database else { throw AustinFailure("permit_store_closed") }
        try execute("BEGIN IMMEDIATE")
        Self.hardeningCrashIfRequested("after_begin")
        var shouldRollback = true
        defer {
            if shouldRollback {
                _ = sqlite3_exec(database, "ROLLBACK", nil, nil, nil)
            }
        }
        let maintenance = try maintainClaims(
            claimedAtMilliseconds: claimedAtMilliseconds,
            expiresAtMilliseconds: expiresAtMilliseconds,
            namespace: .preparation
        )
        Self.hardeningCrashIfRequested("after_maintenance")
        var statement: OpaquePointer?
        let sql = """
            INSERT INTO preparation_claims (
                preparation_id, preparation_digest, claimed_at_ms, expires_at_ms
            ) VALUES (?1, ?2, ?3, ?4)
            """
        guard sqlite3_prepare_v3(
            database,
            sql,
            -1,
            UInt32(SQLITE_PREPARE_PERSISTENT),
            &statement,
            nil
        ) == SQLITE_OK, let statement else {
            throw AustinFailure("permit_store_prepare")
        }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_bind_text(
            statement, 1, preparationID, -1, austinSQLiteTransient
        ) == SQLITE_OK,
        sqlite3_bind_text(
            statement, 2, preparationDigest, -1, austinSQLiteTransient
        ) == SQLITE_OK,
        sqlite3_bind_int64(statement, 3, claimedAtMilliseconds) == SQLITE_OK,
        sqlite3_bind_int64(statement, 4, expiresAtMilliseconds) == SQLITE_OK
        else {
            throw AustinFailure("permit_store_bind")
        }
        let step = sqlite3_step(statement)
        if step == SQLITE_CONSTRAINT {
            throw AustinFailure("preparation_replay")
        }
        guard step == SQLITE_DONE else { throw AustinFailure("permit_store_write") }
        Self.hardeningCrashIfRequested("after_insert")
        try commitState(maintenance, adding: .preparation)
        Self.hardeningCrashIfRequested("after_state")
        try execute("COMMIT")
        shouldRollback = false
        Self.hardeningCrashIfRequested("after_commit")
        try Self.assertPrivateDatabaseFiles(databaseURL)
    }

    public func containsPreparation(preparationID: String) throws -> Bool {
        guard Self.isCanonicalUUID(preparationID) else {
            throw AustinFailure("preparation_id")
        }
        lock.lock()
        defer { lock.unlock() }
        guard let database else { throw AustinFailure("permit_store_closed") }
        var statement: OpaquePointer?
        guard sqlite3_prepare_v3(
            database,
            "SELECT 1 FROM preparation_claims WHERE preparation_id = ?1 LIMIT 1",
            -1,
            UInt32(SQLITE_PREPARE_PERSISTENT),
            &statement,
            nil
        ) == SQLITE_OK, let statement else {
            throw AustinFailure("permit_store_prepare")
        }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_bind_text(
            statement, 1, preparationID, -1, austinSQLiteTransient
        ) == SQLITE_OK else {
            throw AustinFailure("permit_store_bind")
        }
        let step = sqlite3_step(statement)
        if step == SQLITE_ROW { return true }
        if step == SQLITE_DONE { return false }
        throw AustinFailure("permit_store_read")
    }

    public func stats() throws -> AustinAdaPermitStoreStats {
        lock.lock()
        defer { lock.unlock() }
        guard database != nil else { throw AustinFailure("permit_store_closed") }
        return try loadState()
    }

    private func initializeStateIfNeeded() throws {
        let permitCount = try scalarInt64(
            "SELECT COUNT(*) FROM permit_claims",
            reason: "permit_store_state"
        )
        let preparationCount = try scalarInt64(
            "SELECT COUNT(*) FROM preparation_claims",
            reason: "permit_store_state"
        )
        let permitHighWater = try scalarInt64(
            "SELECT COALESCE(MAX(claimed_at_ms), 0) FROM permit_claims",
            reason: "permit_store_state"
        )
        let preparationHighWater = try scalarInt64(
            "SELECT COALESCE(MAX(claimed_at_ms), 0) FROM preparation_claims",
            reason: "permit_store_state"
        )
        _ = try executeInt64(
            """
            INSERT OR IGNORE INTO ada_store_state (
                singleton, schema_version, high_water_ms,
                permit_claim_count, preparation_claim_count
            ) VALUES (1, ?1, ?2, ?3, ?4)
            """,
            bindings: [
                Self.stateSchemaVersion,
                max(permitHighWater, preparationHighWater),
                permitCount,
                preparationCount,
            ],
            reason: "permit_store_state"
        )
    }

    private func assertStateConsistency() throws {
        let state = try loadState()
        let actualPermitCount = try scalarInt64(
            "SELECT COUNT(*) FROM permit_claims",
            reason: "permit_store_state"
        )
        let actualPreparationCount = try scalarInt64(
            "SELECT COUNT(*) FROM preparation_claims",
            reason: "permit_store_state"
        )
        let permitHighWater = try scalarInt64(
            "SELECT COALESCE(MAX(claimed_at_ms), 0) FROM permit_claims",
            reason: "permit_store_state"
        )
        let preparationHighWater = try scalarInt64(
            "SELECT COALESCE(MAX(claimed_at_ms), 0) FROM preparation_claims",
            reason: "permit_store_state"
        )
        guard state.permitClaimCount == actualPermitCount,
              state.preparationClaimCount == actualPreparationCount,
              state.permitClaimCount <= maximumClaimsPerNamespace,
              state.preparationClaimCount <= maximumClaimsPerNamespace,
              state.highWaterMilliseconds >= permitHighWater,
              state.highWaterMilliseconds >= preparationHighWater
        else {
            throw AustinFailure("permit_store_state")
        }
    }

    private func maintainClaims(
        claimedAtMilliseconds: Int64,
        expiresAtMilliseconds: Int64,
        namespace: ClaimNamespace
    ) throws -> ClaimMaintenance {
        let state = try loadState()
        guard claimedAtMilliseconds >= state.retentionFloorMilliseconds else {
            throw AustinFailure("permit_store_clock_rollback")
        }

        let nextHighWater = max(state.highWaterMilliseconds, claimedAtMilliseconds)
        guard expiresAtMilliseconds > nextHighWater else {
            throw AustinFailure("permit_store_stale_claim")
        }
        let removedPermits = try deleteExpiredClaims(
            table: "permit_claims",
            throughMilliseconds: nextHighWater
        )
        let removedPreparations = try deleteExpiredClaims(
            table: "preparation_claims",
            throughMilliseconds: nextHighWater
        )
        guard removedPermits <= state.permitClaimCount,
              removedPreparations <= state.preparationClaimCount
        else {
            throw AustinFailure("permit_store_state")
        }
        let nextPermitCount = state.permitClaimCount - removedPermits
        let nextPreparationCount = state.preparationClaimCount - removedPreparations

        let namespaceCount: Int64
        switch namespace {
        case .permit:
            namespaceCount = nextPermitCount
        case .preparation:
            namespaceCount = nextPreparationCount
        }
        guard namespaceCount < maximumClaimsPerNamespace else {
            throw AustinFailure("permit_store_capacity")
        }
        return ClaimMaintenance(
            highWaterMilliseconds: nextHighWater,
            permitClaimCount: nextPermitCount,
            preparationClaimCount: nextPreparationCount
        )
    }

    private func deleteExpiredClaims(
        table: String,
        throughMilliseconds: Int64
    ) throws -> Int64 {
        let sql: String
        switch table {
        case "permit_claims":
            sql = "DELETE FROM permit_claims WHERE expires_at_ms <= ?1"
        case "preparation_claims":
            sql = "DELETE FROM preparation_claims WHERE expires_at_ms <= ?1"
        default:
            throw AustinFailure("permit_store_state")
        }
        return try executeInt64(
            sql,
            bindings: [throughMilliseconds],
            reason: "permit_store_write"
        )
    }

    private func commitState(
        _ maintenance: ClaimMaintenance,
        adding namespace: ClaimNamespace
    ) throws {
        var permitCount = maintenance.permitClaimCount
        var preparationCount = maintenance.preparationClaimCount
        switch namespace {
        case .permit:
            permitCount += 1
        case .preparation:
            preparationCount += 1
        }
        guard permitCount <= maximumClaimsPerNamespace,
              preparationCount <= maximumClaimsPerNamespace
        else {
            throw AustinFailure("permit_store_capacity")
        }
        let changed = try executeInt64(
            """
            UPDATE ada_store_state
            SET high_water_ms = ?1,
                permit_claim_count = ?2,
                preparation_claim_count = ?3
            WHERE singleton = 1
            """,
            bindings: [
                maintenance.highWaterMilliseconds,
                permitCount,
                preparationCount,
            ],
            reason: "permit_store_state"
        )
        guard changed == 1 else { throw AustinFailure("permit_store_state") }
    }

    private func loadState() throws -> AustinAdaPermitStoreStats {
        guard let database else { throw AustinFailure("permit_store_closed") }
        var statement: OpaquePointer?
        let sql = """
            SELECT schema_version, high_water_ms,
                   permit_claim_count, preparation_claim_count
            FROM ada_store_state
            WHERE singleton = 1
            """
        guard sqlite3_prepare_v3(
            database,
            sql,
            -1,
            UInt32(SQLITE_PREPARE_PERSISTENT),
            &statement,
            nil
        ) == SQLITE_OK, let statement else {
            throw AustinFailure("permit_store_state")
        }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_step(statement) == SQLITE_ROW,
              sqlite3_column_type(statement, 0) == SQLITE_INTEGER,
              sqlite3_column_type(statement, 1) == SQLITE_INTEGER,
              sqlite3_column_type(statement, 2) == SQLITE_INTEGER,
              sqlite3_column_type(statement, 3) == SQLITE_INTEGER
        else {
            throw AustinFailure("permit_store_state")
        }
        let schemaVersion = sqlite3_column_int64(statement, 0)
        let highWater = sqlite3_column_int64(statement, 1)
        let permitCount = sqlite3_column_int64(statement, 2)
        let preparationCount = sqlite3_column_int64(statement, 3)
        guard sqlite3_step(statement) == SQLITE_DONE,
              schemaVersion == Self.stateSchemaVersion,
              highWater >= 0,
              highWater <= austinMaximumSafeInteger,
              permitCount >= 0,
              preparationCount >= 0
        else {
            throw AustinFailure("permit_store_state")
        }
        return AustinAdaPermitStoreStats(
            highWaterMilliseconds: highWater,
            retentionFloorMilliseconds: Self.retentionFloor(for: highWater),
            permitClaimCount: permitCount,
            preparationClaimCount: preparationCount
        )
    }

    private static func retentionFloor(for highWaterMilliseconds: Int64) -> Int64 {
        guard highWaterMilliseconds > clockReorderingWindowMilliseconds else { return 0 }
        return highWaterMilliseconds - clockReorderingWindowMilliseconds
    }

    private static func hardeningCrashIfRequested(_ checkpoint: String) {
        #if DEBUG
        guard ProcessInfo.processInfo.environment["ALGO_AUSTIN_ADA_CRASH_CHECKPOINT"]
                == checkpoint
        else {
            return
        }
        _ = Darwin.kill(Darwin.getpid(), SIGKILL)
        Darwin._exit(86)
        #endif
    }

    private func scalarText(_ sql: String, reason: String) throws -> String {
        guard let database else { throw AustinFailure("permit_store_closed") }
        var statement: OpaquePointer?
        guard sqlite3_prepare_v3(
            database,
            sql,
            -1,
            UInt32(SQLITE_PREPARE_PERSISTENT),
            &statement,
            nil
        ) == SQLITE_OK, let statement else {
            throw AustinFailure(reason)
        }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_step(statement) == SQLITE_ROW,
              sqlite3_column_type(statement, 0) == SQLITE_TEXT,
              let bytes = sqlite3_column_text(statement, 0)
        else {
            throw AustinFailure(reason)
        }
        let value = String(cString: bytes)
        guard sqlite3_step(statement) == SQLITE_DONE else {
            throw AustinFailure(reason)
        }
        return value
    }

    private func scalarInt64(_ sql: String, reason: String) throws -> Int64 {
        guard let database else { throw AustinFailure("permit_store_closed") }
        var statement: OpaquePointer?
        guard sqlite3_prepare_v3(
            database,
            sql,
            -1,
            UInt32(SQLITE_PREPARE_PERSISTENT),
            &statement,
            nil
        ) == SQLITE_OK, let statement else {
            throw AustinFailure(reason)
        }
        defer { sqlite3_finalize(statement) }
        guard sqlite3_step(statement) == SQLITE_ROW,
              sqlite3_column_type(statement, 0) == SQLITE_INTEGER
        else {
            throw AustinFailure(reason)
        }
        let value = sqlite3_column_int64(statement, 0)
        guard sqlite3_step(statement) == SQLITE_DONE else {
            throw AustinFailure(reason)
        }
        return value
    }

    private func executeInt64(
        _ sql: String,
        bindings: [Int64],
        reason: String
    ) throws -> Int64 {
        guard let database else { throw AustinFailure("permit_store_closed") }
        var statement: OpaquePointer?
        guard sqlite3_prepare_v3(
            database,
            sql,
            -1,
            UInt32(SQLITE_PREPARE_PERSISTENT),
            &statement,
            nil
        ) == SQLITE_OK, let statement else {
            throw AustinFailure(reason)
        }
        defer { sqlite3_finalize(statement) }
        for (index, value) in bindings.enumerated() {
            guard sqlite3_bind_int64(statement, Int32(index + 1), value) == SQLITE_OK else {
                throw AustinFailure(reason)
            }
        }
        guard sqlite3_step(statement) == SQLITE_DONE else {
            throw AustinFailure(reason)
        }
        return Int64(sqlite3_changes(database))
    }

    private func execute(_ sql: String) throws {
        guard let database else { throw AustinFailure("permit_store_closed") }
        var errorPointer: UnsafeMutablePointer<CChar>?
        let result = sqlite3_exec(database, sql, nil, nil, &errorPointer)
        if let errorPointer { sqlite3_free(errorPointer) }
        guard result == SQLITE_OK else { throw AustinFailure("permit_store_sql") }
    }

    private static func preparePrivatePath(_ databaseURL: URL) throws {
        let directory = databaseURL.deletingLastPathComponent()
        var directoryInfo = stat()
        if lstat(directory.path, &directoryInfo) != 0 {
            guard errno == ENOENT else { throw AustinFailure("permit_store_directory") }
            do {
                try FileManager.default.createDirectory(
                    at: directory,
                    withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700]
                )
            } catch {
                throw AustinFailure("permit_store_directory")
            }
            guard chmod(directory.path, 0o700) == 0 else {
                throw AustinFailure("permit_store_permissions")
            }
        }
        try assertPrivateDirectory(directory)

        var fileInfo = stat()
        if lstat(databaseURL.path, &fileInfo) != 0 {
            guard errno == ENOENT else { throw AustinFailure("permit_store_file") }
            let descriptor = Darwin.open(
                databaseURL.path,
                O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW,
                S_IRUSR | S_IWUSR
            )
            guard descriptor >= 0 else { throw AustinFailure("permit_store_create") }
            guard Darwin.close(descriptor) == 0 else { throw AustinFailure("permit_store_close") }
        }
        try assertPrivateFile(databaseURL)
        try assertPrivateSidecars(databaseURL)
    }

    private static func assertPrivateDirectory(_ url: URL) throws {
        var info = stat()
        guard lstat(url.path, &info) == 0,
              (info.st_mode & S_IFMT) == S_IFDIR,
              info.st_uid == geteuid(),
              (info.st_mode & 0o077) == 0
        else {
            throw AustinFailure("permit_store_directory_permissions")
        }
    }

    private static func assertPrivateFile(_ url: URL) throws {
        var info = stat()
        guard lstat(url.path, &info) == 0,
              (info.st_mode & S_IFMT) == S_IFREG,
              info.st_nlink == 1,
              info.st_uid == geteuid(),
              (info.st_mode & 0o077) == 0
        else {
            throw AustinFailure("permit_store_file_permissions")
        }
    }

    private static func assertPrivateDatabaseFiles(_ databaseURL: URL) throws {
        try assertPrivateDirectory(databaseURL.deletingLastPathComponent())
        try assertPrivateFile(databaseURL)
        try assertPrivateSidecars(databaseURL)
    }

    private static func assertPrivateSidecars(_ databaseURL: URL) throws {
        for suffix in ["-journal", "-wal", "-shm"] {
            let sidecar = URL(fileURLWithPath: databaseURL.path + suffix)
            var info = stat()
            if lstat(sidecar.path, &info) == 0 {
                try assertPrivateFile(sidecar)
            } else if errno != ENOENT {
                throw AustinFailure("permit_store_file")
            }
        }
    }

    private static func isCanonicalUUID(_ text: String) -> Bool {
        guard text.range(
            of: "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            options: .regularExpression
        ) != nil,
        let parsed = UUID(uuidString: text),
        parsed.uuidString.lowercased() == text
        else {
            return false
        }
        return true
    }
}
