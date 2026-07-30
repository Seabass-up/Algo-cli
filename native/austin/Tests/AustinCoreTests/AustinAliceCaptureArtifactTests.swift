@testable import AustinCore
@testable import AustinDesktopCore
import Darwin
import Foundation
import Testing

private final class AustinAliceFixtureClock: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Int64

    init(_ value: Int64) {
        self.value = value
    }

    func now() -> Int64 {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func set(_ value: Int64) {
        lock.lock()
        self.value = value
        lock.unlock()
    }
}

private final class AustinAliceFixtureKeyStore: AustinAliceKeyMaterialStoring,
    @unchecked Sendable
{
    var value: Data?
    var raceWinner: Data?
    private(set) var insertions = 0

    init(value: Data? = nil, raceWinner: Data? = nil) {
        self.value = value
        self.raceWinner = raceWinner
    }

    func load() throws -> Data? { value }

    func insert(_ keyData: Data) throws -> Bool {
        insertions += 1
        if let raceWinner {
            value = raceWinner
            return false
        }
        guard value == nil else { return false }
        value = keyData
        return true
    }
}

private func austinAliceCapability(_ byte: UInt8) throws -> String {
    let encoded = Data(repeating: byte, count: 32)
        .base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
    return encoded
}

private func austinAlicePrivateDirectory() throws -> URL {
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("austin-alice-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(
        at: directory,
        withIntermediateDirectories: false,
        attributes: [.posixPermissions: 0o700]
    )
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
    return directory
}

private func austinAliceWritePrivateFile(_ data: Data, to url: URL) throws {
    try data.write(to: url, options: .withoutOverwriting)
    try FileManager.default.setAttributes(
        [.posixPermissions: 0o600],
        ofItemAtPath: url.path
    )
}

private func austinAliceWriteExclusiveAtomicMarker(_ data: Data, to url: URL) throws {
    guard !data.isEmpty else {
        throw AustinFailure("capture_artifact_crash_marker_write")
    }
    let directoryURL = url.deletingLastPathComponent().standardizedFileURL
    let destinationName = url.lastPathComponent
    guard directoryURL.path.hasPrefix("/"),
          !destinationName.isEmpty,
          destinationName != ".",
          destinationName != "..",
          !destinationName.contains("/")
    else {
        throw AustinFailure("capture_artifact_crash_marker_write")
    }

    let directoryDescriptor = Darwin.open(
        directoryURL.path,
        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
    )
    guard directoryDescriptor >= 0 else {
        throw AustinFailure("capture_artifact_crash_marker_write")
    }
    defer { Darwin.close(directoryDescriptor) }

    var directoryInformation = stat()
    guard fstat(directoryDescriptor, &directoryInformation) == 0,
          (directoryInformation.st_mode & S_IFMT) == S_IFDIR,
          directoryInformation.st_uid == geteuid(),
          (directoryInformation.st_mode & 0o077) == 0
    else {
        throw AustinFailure("capture_artifact_crash_marker_write")
    }

    let temporaryName = ".\(destinationName).\(UUID().uuidString).tmp"
    var temporaryDescriptor = Darwin.openat(
        directoryDescriptor,
        temporaryName,
        O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC,
        mode_t(0o600)
    )
    guard temporaryDescriptor >= 0 else {
        throw AustinFailure("capture_artifact_crash_marker_write")
    }
    var temporaryPublished = false
    defer {
        if temporaryDescriptor >= 0 {
            Darwin.close(temporaryDescriptor)
        }
        if !temporaryPublished {
            _ = Darwin.unlinkat(directoryDescriptor, temporaryName, 0)
        }
    }

    try data.withUnsafeBytes { buffer in
        guard let baseAddress = buffer.baseAddress else {
            throw AustinFailure("capture_artifact_crash_marker_write")
        }
        var offset = 0
        while offset < buffer.count {
            let written = Darwin.write(
                temporaryDescriptor,
                baseAddress.advanced(by: offset),
                buffer.count - offset
            )
            if written < 0, errno == EINTR {
                continue
            }
            guard written > 0 else {
                throw AustinFailure("capture_artifact_crash_marker_write")
            }
            offset += written
        }
    }
    guard Darwin.fsync(temporaryDescriptor) == 0 else {
        throw AustinFailure("capture_artifact_crash_marker_write")
    }
    guard Darwin.close(temporaryDescriptor) == 0 else {
        temporaryDescriptor = -1
        throw AustinFailure("capture_artifact_crash_marker_write")
    }
    temporaryDescriptor = -1

    guard Darwin.renameatx_np(
        directoryDescriptor,
        temporaryName,
        directoryDescriptor,
        destinationName,
        UInt32(RENAME_EXCL)
    ) == 0 else {
        throw AustinFailure("capture_artifact_crash_marker_write")
    }
    temporaryPublished = true
    guard Darwin.fsync(directoryDescriptor) == 0 else {
        throw AustinFailure("capture_artifact_crash_marker_write")
    }
}

private let austinAliceCrashModeKey = "ALGO_AUSTIN_ALICE_CRASH_MODE"
private let austinAliceCrashDirectoryKey = "ALGO_AUSTIN_ALICE_CRASH_DIRECTORY"
private let austinAliceCrashMarkerKey = "ALGO_AUSTIN_ALICE_CRASH_MARKER"
private let austinAliceCrashTokenKey = "ALGO_AUSTIN_ALICE_CRASH_TOKEN"

private func austinAliceCrashFixture(
    mode: String
) throws -> (directory: URL, marker: URL, token: String)? {
    let environment = ProcessInfo.processInfo.environment
    guard environment[austinAliceCrashModeKey] == mode else { return nil }
    guard let directoryPath = environment[austinAliceCrashDirectoryKey],
          let markerPath = environment[austinAliceCrashMarkerKey],
          let token = environment[austinAliceCrashTokenKey],
          directoryPath.hasPrefix("/"),
          markerPath.hasPrefix("/"),
          token.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil
    else {
        throw AustinFailure("capture_artifact_crash_fixture")
    }
    let directory = URL(fileURLWithPath: directoryPath, isDirectory: true)
        .standardizedFileURL
    let marker = URL(fileURLWithPath: markerPath, isDirectory: false)
        .standardizedFileURL
    guard marker.deletingLastPathComponent() != directory,
          !FileManager.default.fileExists(atPath: marker.path)
    else {
        throw AustinFailure("capture_artifact_crash_fixture")
    }
    return (directory, marker, token)
}

@Test func aliceCaptureSinkPersistsCiphertextOnlyAndRevokesExactly() throws {
    let directory = try austinAlicePrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let clock = AustinAliceFixtureClock(10_000)
    let marker = Array("RAW-CAPTURE-MARKER".utf8)
    let frame = try AustinPixelFrame(width: marker.count, height: 1, rgbaBytes: marker.flatMap {
        [$0, $0, $0, UInt8.max]
    })
    let sink = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0xA1, count: 32),
        ttlMilliseconds: 1_000,
        nowMilliseconds: { clock.now() },
        randomBytes: { count in Data(repeating: 0xB2, count: count) }
    )

    try sink.acceptRedacted(frame)
    let receipts = try sink.takeReceipts()
    let receipt = try #require(receipts.first)
    #expect(receipts.count == 1)
    #expect(receipt.artifactID == String(repeating: "b2", count: 16))
    let path = directory.appendingPathComponent(receipt.fileName, isDirectory: false)
    let payload = try Data(contentsOf: path)
    #expect(payload.starts(with: Data("AUSTIN-ALICE-CAPTURE-v1\0".utf8)))
    #expect(!payload.contains(Data(marker)))
    #expect(payload.count == receipt.ciphertextBytes)
    var information = stat()
    #expect(lstat(path.path, &information) == 0)
    #expect((information.st_mode & S_IFMT) == S_IFREG)
    #expect((information.st_mode & 0o777) == 0o600)
    #expect(information.st_nlink == 1)

    try sink.revokeAll()
    #expect(!FileManager.default.fileExists(atPath: path.path))
    #expect(throws: AustinFailure.self) { try sink.takeReceipts() }
}

@Test func aliceConsumerGrantIsOneUseAuthenticatedAndDeletesBeforeReturn() throws {
    let directory = try austinAlicePrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let frame = try AustinPixelFrame(
        width: 2,
        height: 1,
        rgbaBytes: [1, 2, 3, 255, 4, 5, 6, 255]
    )
    let sink = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0x91, count: 32),
        ttlMilliseconds: 1_000,
        nowMilliseconds: { 30_000 },
        randomBytes: { count in
            Data(repeating: count == 16 ? 0xA2 : 0xB3, count: count)
        }
    )
    try sink.acceptRedacted(frame)
    let grant = try #require(sink.takeConsumerGrants().first)
    #expect(try sink.takeConsumerGrants().isEmpty)
    let path = directory.appendingPathComponent(grant.receipt.fileName)
    #expect(FileManager.default.fileExists(atPath: path.path))

    let wrong = try AustinAliceCaptureConsumerGrant(
        receipt: grant.receipt,
        capability: austinAliceCapability(0xC4)
    )
    #expect(throws: AustinFailure.self) {
        try sink.consumeRedacted(wrong)
    }
    #expect(FileManager.default.fileExists(atPath: path.path))

    let restored = try sink.consumeRedacted(grant)
    #expect(restored == frame)
    #expect(!FileManager.default.fileExists(atPath: path.path))
    #expect(try sink.takeReceipts().isEmpty)
    #expect(throws: AustinFailure.self) {
        try sink.consumeRedacted(grant)
    }
}

@Test func aliceAuthenticatedCorruptionIsConsumedAndDeletedBeforeFailureReturns() throws {
    let directory = try austinAlicePrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let sink = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0x92, count: 32),
        ttlMilliseconds: 1_000,
        nowMilliseconds: { 31_000 },
        randomBytes: { count in
            Data(repeating: count == 16 ? 0xA3 : 0xB4, count: count)
        }
    )
    try sink.acceptRedacted(
        AustinPixelFrame(width: 1, height: 1, rgbaBytes: [1, 2, 3, 255])
    )
    let grant = try #require(sink.takeConsumerGrants().first)
    let path = directory.appendingPathComponent(grant.receipt.fileName)
    let payload = try Data(contentsOf: path)
    var corruptedByte = try #require(payload.last)
    corruptedByte ^= 0xFF
    let descriptor = Darwin.open(path.path, O_WRONLY | O_NOFOLLOW | O_CLOEXEC)
    try #require(descriptor >= 0)
    defer { Darwin.close(descriptor) }
    #expect(lseek(descriptor, off_t(payload.count - 1), SEEK_SET) >= 0)
    let written = Darwin.write(descriptor, &corruptedByte, 1)
    #expect(written == 1)
    #expect(fsync(descriptor) == 0)

    #expect(throws: AustinFailure.self) {
        try sink.consumeRedacted(grant)
    }
    #expect(!FileManager.default.fileExists(atPath: path.path))
    #expect(try sink.takeReceipts().isEmpty)
    #expect(throws: AustinFailure.self) {
        try sink.consumeRedacted(grant)
    }
}

@Test func aliceOSKeyFactoryLoadsCreatesAndResolvesExactCreateRace() throws {
    let existing = Data(repeating: 0x11, count: 32)
    let existingStore = AustinAliceFixtureKeyStore(value: existing)
    let existingFactory = AustinAliceOSKeyFactory(
        store: existingStore,
        randomBytes: { _ in throw AustinFailure("entropy_must_not_run") }
    )
    #expect(try existingFactory.loadOrCreate() == existing)
    #expect(existingStore.insertions == 0)

    let createdStore = AustinAliceFixtureKeyStore()
    let createdFactory = AustinAliceOSKeyFactory(
        store: createdStore,
        randomBytes: { count in Data(repeating: 0x22, count: count) }
    )
    let created = try createdFactory.loadOrCreate()
    #expect(created == Data(repeating: 0x22, count: 32))
    #expect(createdStore.value == created)
    #expect(createdStore.insertions == 1)

    let winner = Data(repeating: 0x33, count: 32)
    let raceStore = AustinAliceFixtureKeyStore(raceWinner: winner)
    let raceFactory = AustinAliceOSKeyFactory(
        store: raceStore,
        randomBytes: { count in Data(repeating: 0x44, count: count) }
    )
    #expect(try raceFactory.loadOrCreate() == winner)
    #expect(raceStore.insertions == 1)
}

@Test func aliceOSKeyFactoryRejectsMalformedStoredAndGeneratedMaterial() {
    let malformedStore = AustinAliceFixtureKeyStore(value: Data(repeating: 1, count: 31))
    let malformedFactory = AustinAliceOSKeyFactory(store: malformedStore)
    #expect(throws: AustinFailure.self) {
        try malformedFactory.loadOrCreate()
    }

    let shortFactory = AustinAliceOSKeyFactory(
        store: AustinAliceFixtureKeyStore(),
        randomBytes: { _ in Data(repeating: 2, count: 31) }
    )
    #expect(throws: AustinFailure.self) {
        try shortFactory.loadOrCreate()
    }
}

@Test func aliceCaptureSinkExpiresAndRejectsClockRollback() throws {
    let directory = try austinAlicePrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let clock = AustinAliceFixtureClock(20_000)
    let sink = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0xC3, count: 32),
        ttlMilliseconds: 10,
        nowMilliseconds: { clock.now() },
        randomBytes: { count in Data(repeating: 0xD4, count: count) }
    )
    try sink.acceptRedacted(
        AustinPixelFrame(width: 1, height: 1, rgbaBytes: [1, 2, 3, 255])
    )
    let receipt = try #require(sink.takeReceipts().first)
    clock.set(20_010)
    #expect(try sink.takeReceipts().isEmpty)
    #expect(!FileManager.default.fileExists(
        atPath: directory.appendingPathComponent(receipt.fileName).path
    ))
    clock.set(20_009)
    #expect(throws: AustinFailure.self) {
        try sink.takeReceipts()
    }
}

@Test func aliceCaptureSinkRecoversExactProcessCrashOrphanOnStartup() throws {
    let directory = try austinAlicePrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let orphan = directory.appendingPathComponent(
        "\(String(repeating: "a", count: 32)).alice",
        isDirectory: false
    )
    try austinAliceWritePrivateFile(Data([1, 2, 3]), to: orphan)

    let sink = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0xD5, count: 32),
        nowMilliseconds: { 40_000 }
    )

    #expect(!FileManager.default.fileExists(atPath: orphan.path))
    #expect(try sink.takeReceipts().isEmpty)
    try sink.revokeAll()
}

@Test func aliceCaptureSinkHoldsExclusiveDirectoryLeaseAcrossItsLifetime() throws {
    let directory = try austinAlicePrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    var first: AustinAliceEncryptedCaptureSink? = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0xD6, count: 32),
        nowMilliseconds: { 41_000 }
    )

    #expect(throws: AustinFailure.self) {
        try AustinAliceEncryptedCaptureSink(
            directoryURL: directory,
            keyData: Data(repeating: 0xD7, count: 32)
        )
    }

    try first?.revokeAll()
    first = nil
    let replacement = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0xD8, count: 32),
        nowMilliseconds: { 41_001 }
    )
    try replacement.revokeAll()
}

@Test func aliceCaptureSinkPreservesEverythingWhenDirectoryContainsUnknownEntry() throws {
    let directory = try austinAlicePrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let orphan = directory.appendingPathComponent(
        "\(String(repeating: "b", count: 32)).alice",
        isDirectory: false
    )
    let unknown = directory.appendingPathComponent("operator-note", isDirectory: false)
    try austinAliceWritePrivateFile(Data([4, 5, 6]), to: orphan)
    try austinAliceWritePrivateFile(Data([7, 8, 9]), to: unknown)

    #expect(throws: AustinFailure.self) {
        try AustinAliceEncryptedCaptureSink(
            directoryURL: directory,
            keyData: Data(repeating: 0xE6, count: 32)
        )
    }
    #expect(FileManager.default.fileExists(atPath: orphan.path))
    #expect(FileManager.default.fileExists(atPath: unknown.path))
}

@Test func aliceCaptureSinkRejectsSuspiciousOrphanWithoutFollowingIt() throws {
    let directory = try austinAlicePrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let target = FileManager.default.temporaryDirectory
        .appendingPathComponent("austin-alice-target-\(UUID().uuidString)", isDirectory: false)
    defer { try? FileManager.default.removeItem(at: target) }
    try austinAliceWritePrivateFile(Data([10, 11, 12]), to: target)
    let link = directory.appendingPathComponent(
        "\(String(repeating: "c", count: 32)).alice",
        isDirectory: false
    )
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)

    #expect(throws: AustinFailure.self) {
        try AustinAliceEncryptedCaptureSink(
            directoryURL: directory,
            keyData: Data(repeating: 0xF7, count: 32)
        )
    }
    #expect(try Data(contentsOf: target) == Data([10, 11, 12]))
    var information = stat()
    #expect(lstat(link.path, &information) == 0)
    #expect((information.st_mode & S_IFMT) == S_IFLNK)
}

@Test func aliceProcessCrashProbePublishesThenWaitsForKill() throws {
    guard let fixture = try austinAliceCrashFixture(mode: "publish-and-wait") else {
        return
    }
    let sink = try AustinAliceEncryptedCaptureSink(
        directoryURL: fixture.directory,
        keyData: Data(repeating: 0x81, count: 32),
        ttlMilliseconds: 60_000,
        nowMilliseconds: { 50_000 },
        randomBytes: { count in
            Data(repeating: count == 16 ? 0x82 : 0x83, count: count)
        }
    )
    try sink.acceptRedacted(
        AustinPixelFrame(width: 1, height: 1, rgbaBytes: [1, 2, 3, 255])
    )
    let receipt = try #require(sink.takeReceipts().first)
    let marker = "\(fixture.token):\(getpid()):\(receipt.fileName)"
    try austinAliceWriteExclusiveAtomicMarker(Data(marker.utf8), to: fixture.marker)

    signal(SIGALRM, SIG_DFL)
    alarm(15)
    withExtendedLifetime(sink) {
        while true { pause() }
    }
}

@Test func aliceProcessCrashProbeRecoversKilledPublisher() throws {
    guard let fixture = try austinAliceCrashFixture(mode: "recover") else {
        return
    }
    let sink = try AustinAliceEncryptedCaptureSink(
        directoryURL: fixture.directory,
        keyData: Data(repeating: 0x84, count: 32),
        nowMilliseconds: { 50_001 }
    )
    #expect(try FileManager.default.contentsOfDirectory(atPath: fixture.directory.path).isEmpty)
    try sink.revokeAll()
    try austinAliceWriteExclusiveAtomicMarker(Data(fixture.token.utf8), to: fixture.marker)
}

@Test func aliceCaptureSinkRejectsBroadAndLinkedDirectories() throws {
    let broad = FileManager.default.temporaryDirectory
        .appendingPathComponent("austin-alice-broad-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(
        at: broad,
        withIntermediateDirectories: false,
        attributes: [.posixPermissions: 0o755]
    )
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: broad.path)
    defer { try? FileManager.default.removeItem(at: broad) }
    #expect(throws: AustinFailure.self) {
        try AustinAliceEncryptedCaptureSink(
            directoryURL: broad,
            keyData: Data(repeating: 0xE5, count: 32)
        )
    }

    let privateDirectory = try austinAlicePrivateDirectory()
    defer { try? FileManager.default.removeItem(at: privateDirectory) }
    let link = FileManager.default.temporaryDirectory
        .appendingPathComponent("austin-alice-link-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: privateDirectory)
    defer { try? FileManager.default.removeItem(at: link) }
    #expect(throws: AustinFailure.self) {
        try AustinAliceEncryptedCaptureSink(
            directoryURL: link,
            keyData: Data(repeating: 0xF6, count: 32)
        )
    }
}
