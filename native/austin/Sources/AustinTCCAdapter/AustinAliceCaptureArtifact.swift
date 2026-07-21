import AustinCore
import CryptoKit
import Darwin
import Foundation
import LocalAuthentication
import Security

public struct AustinAliceCaptureArtifactReceipt: Equatable, Sendable {
    public let artifactID: String
    public let ciphertextBytes: Int
    public let expiresAtMilliseconds: Int64

    public var fileName: String { "\(artifactID).alice" }

    public init(
        artifactID: String,
        ciphertextBytes: Int,
        expiresAtMilliseconds: Int64
    ) throws {
        guard artifactID.range(
            of: "^[0-9a-f]{32}$",
            options: .regularExpression
        ) != nil,
        ciphertextBytes > 0,
        ciphertextBytes <= AustinAliceEncryptedCaptureSink.maximumStoredBytes,
        expiresAtMilliseconds > 0,
        expiresAtMilliseconds <= austinMaximumSafeInteger
        else {
            throw AustinFailure("capture_artifact_receipt")
        }
        self.artifactID = artifactID
        self.ciphertextBytes = ciphertextBytes
        self.expiresAtMilliseconds = expiresAtMilliseconds
    }
}

/// One-use, process-memory-only authority to consume one already-redacted
/// capture. Only the ciphertext is persisted; this 256-bit bearer is not.
struct AustinAliceCaptureConsumerGrant: Equatable, Sendable {
    let receipt: AustinAliceCaptureArtifactReceipt
    let capability: String

    init(
        receipt: AustinAliceCaptureArtifactReceipt,
        capability: String
    ) throws {
        guard capability.range(
            of: "^[A-Za-z0-9_-]{43}$",
            options: .regularExpression
        ) != nil else {
            throw AustinFailure("capture_artifact_capability")
        }
        self.receipt = receipt
        self.capability = capability
    }
}

private final class AustinAliceStoredCapture: @unchecked Sendable {
    let receipt: AustinAliceCaptureArtifactReceipt
    let storedBytes: Int
    var consumerCapability: Data
    var grantIssued = false
    var consumed = false

    init(
        receipt: AustinAliceCaptureArtifactReceipt,
        storedBytes: Int,
        consumerCapability: Data
    ) {
        self.receipt = receipt
        self.storedBytes = storedBytes
        self.consumerCapability = consumerCapability
    }

    func clearConsumerCapability() {
        consumerCapability.resetBytes(in: 0..<consumerCapability.count)
        consumerCapability.removeAll(keepingCapacity: false)
        consumed = true
    }
}

private struct AustinAliceOrphanRecord {
    let fileName: String
    let device: dev_t
    let inode: ino_t
    let size: off_t
}

/// A short-lived native sink for already-redacted frames. The caller must
/// supply an existing owner-private directory and an OS-backed 256-bit key.
/// Files contain only AES-256-GCM ciphertext and are created relative to a
/// pinned directory descriptor with no-follow and exclusive-create semantics.
public final class AustinAliceEncryptedCaptureSink: AustinRedactedCaptureSink,
    @unchecked Sendable
{
    public static let maximumTTLMilliseconds: Int64 = 15 * 60 * 1_000
    public static let maximumArtifacts = 8
    public static let maximumStoredBytes = AustinPixelFrame.maximumBytes + 4_096

    private static let magic = Data("AUSTIN-ALICE-CAPTURE-v1\0".utf8)
    private let directoryDescriptor: Int32
    private let key: SymmetricKey
    private let ttlMilliseconds: Int64
    private let nowMilliseconds: @Sendable () throws -> Int64
    private let randomBytes: @Sendable (Int) throws -> Data
    private let lock = NSLock()
    private var stored: [AustinAliceStoredCapture] = []
    private var lastNowMilliseconds: Int64 = 0
    private var closed = false

    public init(
        directoryURL: URL,
        keyData: Data,
        ttlMilliseconds: Int64 = maximumTTLMilliseconds,
        nowMilliseconds: @escaping @Sendable () throws -> Int64 = {
            AustinClock.nowMilliseconds()
        },
        randomBytes: @escaping @Sendable (Int) throws -> Data = { count in
            let bytes = try AustinSession.secureRandomBytes()
            guard count > 0, count <= bytes.count else {
                throw AustinFailure("capture_artifact_entropy")
            }
            return Data(bytes.prefix(count))
        }
    ) throws {
        guard directoryURL.isFileURL,
              directoryURL.path.hasPrefix("/"),
              keyData.count == 32,
              ttlMilliseconds > 0,
              ttlMilliseconds <= Self.maximumTTLMilliseconds
        else {
            throw AustinFailure("capture_artifact_configuration")
        }
        let descriptor = Darwin.open(
            directoryURL.path,
            O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
        )
        guard descriptor >= 0 else {
            throw AustinFailure("capture_artifact_directory")
        }
        var information = stat()
        guard fstat(descriptor, &information) == 0,
              (information.st_mode & S_IFMT) == S_IFDIR,
              information.st_uid == geteuid(),
              (information.st_mode & 0o777) == 0o700
        else {
            Darwin.close(descriptor)
            throw AustinFailure("capture_artifact_directory")
        }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            Darwin.close(descriptor)
            throw AustinFailure("capture_artifact_directory_busy")
        }
        do {
            try Self.recoverOrphanedArtifacts(in: descriptor)
        } catch {
            Darwin.close(descriptor)
            throw error
        }
        directoryDescriptor = descriptor
        key = SymmetricKey(data: keyData)
        self.ttlMilliseconds = ttlMilliseconds
        self.nowMilliseconds = nowMilliseconds
        self.randomBytes = randomBytes
    }

    deinit {
        lock.lock()
        if !closed {
            for item in stored {
                item.clearConsumerCapability()
                _ = unlinkat(directoryDescriptor, item.receipt.fileName, 0)
            }
            _ = fsync(directoryDescriptor)
        }
        Darwin.close(directoryDescriptor)
        lock.unlock()
    }

    public func acceptRedacted(_ frame: AustinPixelFrame) throws {
        let now = try checkedNow()
        guard now <= austinMaximumSafeInteger - ttlMilliseconds else {
            throw AustinFailure("capture_artifact_time")
        }
        let expires = now + ttlMilliseconds
        let identifierBytes: Data
        do {
            identifierBytes = try randomBytes(16)
        } catch {
            throw AustinFailure("capture_artifact_entropy")
        }
        guard identifierBytes.count == 16 else {
            throw AustinFailure("capture_artifact_entropy")
        }
        let artifactID = identifierBytes.map { String(format: "%02x", $0) }.joined()
        let consumerCapability: Data
        do {
            consumerCapability = try randomBytes(32)
        } catch {
            throw AustinFailure("capture_artifact_entropy")
        }
        guard consumerCapability.count == 32 else {
            throw AustinFailure("capture_artifact_entropy")
        }

        var plaintext = Data()
        plaintext.reserveCapacity(8 + frame.rgbaBytes.count)
        appendUInt32(UInt32(frame.width), to: &plaintext)
        appendUInt32(UInt32(frame.height), to: &plaintext)
        plaintext.append(contentsOf: frame.rgbaBytes)
        defer {
            plaintext.resetBytes(in: 0..<plaintext.count)
            plaintext.removeAll(keepingCapacity: false)
        }
        let associatedData = Data(
            "austin-alice-capture-v1\0\(artifactID)\0\(now)\0\(expires)".utf8
        )
        let sealed: AES.GCM.SealedBox
        do {
            sealed = try AES.GCM.seal(plaintext, using: key, authenticating: associatedData)
        } catch {
            throw AustinFailure("capture_artifact_encrypt")
        }
        guard let combined = sealed.combined else {
            throw AustinFailure("capture_artifact_encrypt")
        }
        var payload = Self.magic
        appendUInt64(UInt64(now), to: &payload)
        appendUInt64(UInt64(expires), to: &payload)
        payload.append(combined)
        defer {
            payload.resetBytes(in: 0..<payload.count)
            payload.removeAll(keepingCapacity: false)
        }
        guard payload.count <= Self.maximumStoredBytes else {
            throw AustinFailure("capture_artifact_size")
        }
        let receipt = try AustinAliceCaptureArtifactReceipt(
            artifactID: artifactID,
            ciphertextBytes: payload.count,
            expiresAtMilliseconds: expires
        )

        lock.lock()
        defer { lock.unlock() }
        guard !closed else { throw AustinFailure("capture_artifact_closed") }
        guard now >= lastNowMilliseconds else {
            throw AustinFailure("capture_artifact_time")
        }
        lastNowMilliseconds = now
        try cleanupExpiredLocked(nowMilliseconds: now)
        let currentBytes = stored.reduce(0) { $0 + $1.storedBytes }
        guard stored.count < Self.maximumArtifacts,
              currentBytes <= Self.maximumStoredBytes - payload.count
        else {
            throw AustinFailure("capture_artifact_quota")
        }
        try publishLocked(fileName: receipt.fileName, payload: payload)
        stored.append(
            AustinAliceStoredCapture(
                receipt: receipt,
                storedBytes: payload.count,
                consumerCapability: consumerCapability
            )
        )
    }

    public func takeReceipts() throws -> [AustinAliceCaptureArtifactReceipt] {
        let nowMilliseconds = try checkedNow()
        lock.lock()
        defer { lock.unlock() }
        guard !closed,
              nowMilliseconds >= lastNowMilliseconds,
              nowMilliseconds <= austinMaximumSafeInteger
        else {
            throw AustinFailure("capture_artifact_time")
        }
        lastNowMilliseconds = nowMilliseconds
        try cleanupExpiredLocked(nowMilliseconds: nowMilliseconds)
        return stored.filter { !$0.consumed }.map(\.receipt)
    }

    func takeConsumerGrants() throws -> [AustinAliceCaptureConsumerGrant] {
        let nowMilliseconds = try checkedNow()
        lock.lock()
        defer { lock.unlock() }
        guard !closed,
              nowMilliseconds >= lastNowMilliseconds,
              nowMilliseconds <= austinMaximumSafeInteger
        else {
            throw AustinFailure("capture_artifact_time")
        }
        lastNowMilliseconds = nowMilliseconds
        try cleanupExpiredLocked(nowMilliseconds: nowMilliseconds)
        var grants: [AustinAliceCaptureConsumerGrant] = []
        for item in stored where !item.grantIssued && !item.consumed {
            let grant = try AustinAliceCaptureConsumerGrant(
                receipt: item.receipt,
                capability: Self.encodeCapability(item.consumerCapability)
            )
            item.grantIssued = true
            grants.append(grant)
        }
        return grants
    }

    /// Atomically validates one ephemeral consumer grant, decrypts the bounded
    /// already-redacted frame, and removes the ciphertext before returning the
    /// pixels. A wrong bearer does not consume the grant; any authenticated
    /// read/decode failure does, preventing ambiguous retry after corruption.
    func consumeRedacted(
        _ grant: AustinAliceCaptureConsumerGrant
    ) throws -> AustinPixelFrame {
        let nowMilliseconds = try checkedNow()
        lock.lock()
        defer { lock.unlock() }
        guard !closed,
              nowMilliseconds >= lastNowMilliseconds,
              nowMilliseconds <= austinMaximumSafeInteger
        else {
            throw AustinFailure("capture_artifact_time")
        }
        lastNowMilliseconds = nowMilliseconds
        try cleanupExpiredLocked(nowMilliseconds: nowMilliseconds)
        guard let index = stored.firstIndex(where: {
            $0.receipt.artifactID == grant.receipt.artifactID
        }) else {
            throw AustinFailure("capture_artifact_missing")
        }
        let item = stored[index]
        guard item.receipt == grant.receipt,
              item.grantIssued,
              !item.consumed,
              let supplied = Self.decodeCapability(grant.capability),
              Self.constantTimeEqual(supplied, item.consumerCapability)
        else {
            throw AustinFailure("capture_artifact_authority")
        }
        item.clearConsumerCapability()
        let frame: AustinPixelFrame
        do {
            frame = try readRedactedLocked(
                item,
                nowMilliseconds: nowMilliseconds
            )
        } catch {
            try discardFailedConsumptionLocked(item, at: index)
            throw error
        }
        stored.remove(at: index)
        return frame
    }

    public func revokeAll() throws {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else { return }
        var retained: [AustinAliceStoredCapture] = []
        var failed = false
        for item in stored {
            item.clearConsumerCapability()
            if unlinkat(directoryDescriptor, item.receipt.fileName, 0) != 0, errno != ENOENT {
                failed = true
                retained.append(item)
            }
        }
        stored = retained
        if fsync(directoryDescriptor) != 0 || failed {
            throw AustinFailure("capture_artifact_revoke")
        }
        closed = true
    }

    private func checkedNow() throws -> Int64 {
        let now: Int64
        do {
            now = try nowMilliseconds()
        } catch {
            throw AustinFailure("capture_artifact_time")
        }
        lock.lock()
        defer { lock.unlock() }
        guard !closed,
              now >= lastNowMilliseconds,
              now >= 0,
              now <= austinMaximumSafeInteger
        else {
            throw AustinFailure("capture_artifact_time")
        }
        lastNowMilliseconds = now
        return now
    }

    private func cleanupExpiredLocked(nowMilliseconds: Int64) throws {
        var retained: [AustinAliceStoredCapture] = []
        var failed = false
        for item in stored {
            if item.receipt.expiresAtMilliseconds <= nowMilliseconds {
                item.clearConsumerCapability()
                if unlinkat(directoryDescriptor, item.receipt.fileName, 0) != 0, errno != ENOENT {
                    failed = true
                    retained.append(item)
                }
            } else {
                retained.append(item)
            }
        }
        stored = retained
        if failed || fsync(directoryDescriptor) != 0 {
            throw AustinFailure("capture_artifact_cleanup")
        }
    }

    private func discardFailedConsumptionLocked(
        _ item: AustinAliceStoredCapture,
        at index: Int
    ) throws {
        guard unlinkat(directoryDescriptor, item.receipt.fileName, 0) == 0 || errno == ENOENT,
              fsync(directoryDescriptor) == 0
        else {
            throw AustinFailure("capture_artifact_consume")
        }
        stored.remove(at: index)
    }

    private func readRedactedLocked(
        _ item: AustinAliceStoredCapture,
        nowMilliseconds: Int64
    ) throws -> AustinPixelFrame {
        let descriptor = openat(
            directoryDescriptor,
            item.receipt.fileName,
            O_RDONLY | O_NOFOLLOW | O_CLOEXEC
        )
        guard descriptor >= 0 else {
            throw AustinFailure("capture_artifact_read")
        }
        defer { Darwin.close(descriptor) }
        var before = stat()
        guard fstat(descriptor, &before) == 0,
              (before.st_mode & S_IFMT) == S_IFREG,
              before.st_nlink == 1,
              before.st_uid == geteuid(),
              (before.st_mode & 0o777) == 0o600,
              before.st_size == item.storedBytes,
              before.st_size == item.receipt.ciphertextBytes
        else {
            throw AustinFailure("capture_artifact_file")
        }

        var payload = Data()
        payload.reserveCapacity(item.storedBytes)
        var remaining = item.storedBytes
        while remaining > 0 {
            var buffer = [UInt8](repeating: 0, count: min(remaining, 64 * 1_024))
            let count = buffer.withUnsafeMutableBytes { bytes in
                Darwin.read(descriptor, bytes.baseAddress, bytes.count)
            }
            if count < 0, errno == EINTR { continue }
            guard count > 0 else {
                throw AustinFailure("capture_artifact_read")
            }
            payload.append(contentsOf: buffer.prefix(count))
            remaining -= count
        }
        var extra: UInt8 = 0
        guard Darwin.read(descriptor, &extra, 1) == 0 else {
            throw AustinFailure("capture_artifact_read")
        }
        var after = stat()
        guard fstat(descriptor, &after) == 0,
              before.st_dev == after.st_dev,
              before.st_ino == after.st_ino,
              before.st_size == after.st_size,
              before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
              before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
              before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec,
              before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec
        else {
            throw AustinFailure("capture_artifact_read")
        }

        let headerBytes = Self.magic.count + 16
        guard payload.count > headerBytes,
              payload.prefix(Self.magic.count) == Self.magic
        else {
            throw AustinFailure("capture_artifact_format")
        }
        let created = try Self.decodeUInt64(payload, offset: Self.magic.count)
        let expires = try Self.decodeUInt64(payload, offset: Self.magic.count + 8)
        guard created <= UInt64(austinMaximumSafeInteger),
              expires <= UInt64(austinMaximumSafeInteger),
              Int64(expires) == item.receipt.expiresAtMilliseconds,
              Int64(created) <= nowMilliseconds,
              nowMilliseconds < Int64(expires)
        else {
            throw AustinFailure("capture_artifact_time")
        }
        let associatedData = Data(
            "austin-alice-capture-v1\0\(item.receipt.artifactID)\0\(created)\0\(expires)".utf8
        )
        let sealed: AES.GCM.SealedBox
        do {
            sealed = try AES.GCM.SealedBox(combined: payload.dropFirst(headerBytes))
        } catch {
            throw AustinFailure("capture_artifact_decrypt")
        }
        var plaintext: Data
        do {
            plaintext = try AES.GCM.open(sealed, using: key, authenticating: associatedData)
        } catch {
            throw AustinFailure("capture_artifact_decrypt")
        }
        defer {
            plaintext.resetBytes(in: 0..<plaintext.count)
            plaintext.removeAll(keepingCapacity: false)
        }
        guard plaintext.count >= 8 else {
            throw AustinFailure("capture_artifact_frame")
        }
        let width = try Self.decodeUInt32(plaintext, offset: 0)
        let height = try Self.decodeUInt32(plaintext, offset: 4)
        guard width > 0,
              height > 0,
              width <= 16_384,
              height <= 16_384,
              width <= Int.max / height,
              width * height <= AustinPixelFrame.maximumBytes / 4,
              plaintext.count == 8 + width * height * 4
        else {
            throw AustinFailure("capture_artifact_frame")
        }
        var frame = try AustinPixelFrame(
            width: width,
            height: height,
            rgbaBytes: Array(plaintext.dropFirst(8))
        )
        guard unlinkat(directoryDescriptor, item.receipt.fileName, 0) == 0,
              fsync(directoryDescriptor) == 0
        else {
            frame.clear()
            throw AustinFailure("capture_artifact_consume")
        }
        return frame
    }

    private func publishLocked(fileName: String, payload: Data) throws {
        let descriptor = openat(
            directoryDescriptor,
            fileName,
            O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC,
            mode_t(0o600)
        )
        guard descriptor >= 0 else {
            throw AustinFailure("capture_artifact_create")
        }
        var complete = false
        defer {
            Darwin.close(descriptor)
            if !complete {
                _ = unlinkat(directoryDescriptor, fileName, 0)
            }
        }
        var information = stat()
        guard fstat(descriptor, &information) == 0,
              (information.st_mode & S_IFMT) == S_IFREG,
              information.st_nlink == 1,
              information.st_uid == geteuid(),
              fchmod(descriptor, mode_t(0o600)) == 0
        else {
            throw AustinFailure("capture_artifact_file")
        }
        try payload.withUnsafeBytes { bytes in
            guard let baseAddress = bytes.baseAddress else {
                throw AustinFailure("capture_artifact_write")
            }
            var written = 0
            while written < bytes.count {
                let count = Darwin.write(
                    descriptor,
                    baseAddress.advanced(by: written),
                    bytes.count - written
                )
                if count < 0, errno == EINTR { continue }
                guard count > 0 else {
                    throw AustinFailure("capture_artifact_write")
                }
                written += count
            }
        }
        guard fsync(descriptor) == 0,
              fsync(directoryDescriptor) == 0
        else {
            throw AustinFailure("capture_artifact_sync")
        }
        complete = true
    }

    /// Consumer capabilities exist only in this process, so an exact Alice
    /// file found at startup can never be consumed safely. Scan through the
    /// pinned directory descriptor, validate the entire directory before any
    /// mutation, then revalidate each orphan immediately before unlinking it.
    /// Unknown or suspicious entries are preserved and block startup.
    private static func recoverOrphanedArtifacts(in directoryDescriptor: Int32) throws {
        let scanDescriptor = openat(
            directoryDescriptor,
            ".",
            O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
        )
        guard scanDescriptor >= 0 else {
            throw AustinFailure("capture_artifact_recovery")
        }
        guard let stream = fdopendir(scanDescriptor) else {
            Darwin.close(scanDescriptor)
            throw AustinFailure("capture_artifact_recovery")
        }
        defer { closedir(stream) }

        var orphans: [AustinAliceOrphanRecord] = []
        while true {
            errno = 0
            guard let entry = readdir(stream) else {
                guard errno == 0 else {
                    throw AustinFailure("capture_artifact_recovery")
                }
                break
            }
            let fileName = withUnsafePointer(to: &entry.pointee.d_name) { pointer in
                pointer.withMemoryRebound(
                    to: CChar.self,
                    capacity: Int(entry.pointee.d_namlen) + 1
                ) {
                    String(validatingCString: $0)
                }
            }
            guard let fileName else {
                throw AustinFailure("capture_artifact_directory_entry")
            }
            if fileName == "." || fileName == ".." { continue }
            guard validOrphanFileName(fileName) else {
                throw AustinFailure("capture_artifact_directory_entry")
            }
            var information = stat()
            guard fstatat(
                directoryDescriptor,
                fileName,
                &information,
                AT_SYMLINK_NOFOLLOW
            ) == 0,
            (information.st_mode & S_IFMT) == S_IFREG,
            information.st_nlink == 1,
            information.st_uid == geteuid(),
            (information.st_mode & 0o777) == 0o600,
            information.st_size >= 0,
            information.st_size <= off_t(maximumStoredBytes)
            else {
                throw AustinFailure("capture_artifact_directory_entry")
            }
            orphans.append(
                AustinAliceOrphanRecord(
                    fileName: fileName,
                    device: information.st_dev,
                    inode: information.st_ino,
                    size: information.st_size
                )
            )
        }

        for orphan in orphans {
            var current = stat()
            guard fstatat(
                directoryDescriptor,
                orphan.fileName,
                &current,
                AT_SYMLINK_NOFOLLOW
            ) == 0,
            (current.st_mode & S_IFMT) == S_IFREG,
            current.st_nlink == 1,
            current.st_uid == geteuid(),
            (current.st_mode & 0o777) == 0o600,
            current.st_dev == orphan.device,
            current.st_ino == orphan.inode,
            current.st_size == orphan.size,
            unlinkat(directoryDescriptor, orphan.fileName, 0) == 0
            else {
                throw AustinFailure("capture_artifact_recovery")
            }
        }
        if !orphans.isEmpty, fsync(directoryDescriptor) != 0 {
            throw AustinFailure("capture_artifact_recovery")
        }
    }

    private static func validOrphanFileName(_ fileName: String) -> Bool {
        guard fileName.utf8.count == 38,
              fileName.hasSuffix(".alice")
        else {
            return false
        }
        return fileName.dropLast(6).utf8.allSatisfy { byte in
            (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte)
                || (UInt8(ascii: "a")...UInt8(ascii: "f")).contains(byte)
        }
    }

    private func appendUInt32(_ value: UInt32, to data: inout Data) {
        var encoded = value.bigEndian
        withUnsafeBytes(of: &encoded) { data.append(contentsOf: $0) }
    }

    private func appendUInt64(_ value: UInt64, to data: inout Data) {
        var encoded = value.bigEndian
        withUnsafeBytes(of: &encoded) { data.append(contentsOf: $0) }
    }

    private static func decodeUInt32(_ data: Data, offset: Int) throws -> Int {
        guard offset >= 0, offset <= data.count - 4 else {
            throw AustinFailure("capture_artifact_format")
        }
        var value: UInt32 = 0
        for byte in data[offset..<(offset + 4)] {
            value = (value << 8) | UInt32(byte)
        }
        return Int(value)
    }

    private static func decodeUInt64(_ data: Data, offset: Int) throws -> UInt64 {
        guard offset >= 0, offset <= data.count - 8 else {
            throw AustinFailure("capture_artifact_format")
        }
        var value: UInt64 = 0
        for byte in data[offset..<(offset + 8)] {
            value = (value << 8) | UInt64(byte)
        }
        return value
    }

    private static func encodeCapability(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private static func decodeCapability(_ text: String) -> Data? {
        guard text.range(
            of: "^[A-Za-z0-9_-]{43}$",
            options: .regularExpression
        ) != nil else {
            return nil
        }
        let normalized = text
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/") + "="
        guard let decoded = Data(base64Encoded: normalized),
              decoded.count == 32,
              encodeCapability(decoded) == text
        else {
            return nil
        }
        return decoded
    }

    private static func constantTimeEqual(_ left: Data, _ right: Data) -> Bool {
        guard left.count == right.count else { return false }
        var difference: UInt8 = 0
        for index in left.indices {
            difference |= left[index] ^ right[index]
        }
        return difference == 0
    }
}

public protocol AustinAliceKeyMaterialStoring: AnyObject {
    func load() throws -> Data?
    /// Returns false only when another process won the exact create race.
    func insert(_ keyData: Data) throws -> Bool
}

/// Production Keychain storage for the native Alice capture key. The item is
/// local-device, when-unlocked, non-synchronizing, non-interactive, and left in
/// the signed adapter's default Keychain access group. No secret access group
/// is shared with the Python runtime or another helper.
public final class AustinAliceKeychainMaterialStore: AustinAliceKeyMaterialStoring,
    @unchecked Sendable
{
    public static let service = "algo-cli-runtime"
    public static let account = "alice-artifact-master-v1"

    public init() {}

    public func load() throws -> Data? {
        let context = LAContext()
        context.interactionNotAllowed = true
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: Self.account,
            kSecAttrSynchronizable as String: false,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecReturnData as String: true,
            kSecUseAuthenticationContext as String: context,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess,
              let data = result as? Data,
              data.count == 32
        else {
            throw AustinFailure("capture_keychain_load")
        }
        return data
    }

    public func insert(_ keyData: Data) throws -> Bool {
        guard keyData.count == 32 else {
            throw AustinFailure("capture_key_material")
        }
        let context = LAContext()
        context.interactionNotAllowed = true
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: Self.service,
            kSecAttrAccount as String: Self.account,
            kSecAttrSynchronizable as String: false,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            kSecValueData as String: keyData,
            kSecUseAuthenticationContext as String: context,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        if status == errSecDuplicateItem { return false }
        guard status == errSecSuccess else {
            throw AustinFailure("capture_keychain_insert")
        }
        return true
    }
}

/// Load-or-create coordinator for exactly one 256-bit OS-backed Alice key.
/// Existing malformed material is never replaced, and a concurrent create
/// race must resolve by reading the winner rather than overwriting it.
public final class AustinAliceOSKeyFactory: @unchecked Sendable {
    private let store: AustinAliceKeyMaterialStoring
    private let randomBytes: @Sendable (Int) throws -> Data

    public init(
        store: AustinAliceKeyMaterialStoring = AustinAliceKeychainMaterialStore(),
        randomBytes: @escaping @Sendable (Int) throws -> Data = { count in
            guard count == 32 else { throw AustinFailure("capture_key_entropy") }
            var bytes = [UInt8](repeating: 0, count: count)
            guard SecRandomCopyBytes(kSecRandomDefault, count, &bytes) == errSecSuccess else {
                throw AustinFailure("capture_key_entropy")
            }
            return Data(bytes)
        }
    ) {
        self.store = store
        self.randomBytes = randomBytes
    }

    public func loadOrCreate() throws -> Data {
        if let existing = try store.load() {
            guard existing.count == 32 else {
                throw AustinFailure("capture_key_material")
            }
            return existing
        }
        var generated = try randomBytes(32)
        guard generated.count == 32 else {
            generated.resetBytes(in: 0..<generated.count)
            throw AustinFailure("capture_key_entropy")
        }
        if try store.insert(generated) {
            return generated
        }
        generated.resetBytes(in: 0..<generated.count)
        guard let winner = try store.load(), winner.count == 32 else {
            throw AustinFailure("capture_key_race")
        }
        return winner
    }
}
