import AustinCore
import Foundation

/// Adapter-local consumer for an already-redacted frame. This protocol is
/// intentionally internal to AustinDesktopCore: neither the relay, XPC
/// protocol, Python runtime, nor model process can implement it or receive the
/// ephemeral Alice consumer capability.
protocol AustinIsaacRedactedFrameConsuming: AnyObject {
    func consumeRedacted(_ frame: inout AustinPixelFrame) throws
}

/// Sealed bridge between the redacted capture sink and its native consumer.
/// The frame is encrypted before the consumer grant is issued, the grant never
/// leaves this module, ciphertext is deleted before the consumer runs, and the
/// recovered buffer is cleared on every return path. Any invariant or consumer
/// failure terminally revokes the pipeline until process restart.
final class AustinIsaacSealedCapturePipeline: AustinRedactedCaptureSink,
    @unchecked Sendable
{
    private let artifactSink: AustinAliceEncryptedCaptureSink
    private let consumer: AustinIsaacRedactedFrameConsuming
    private let lock = NSLock()
    private var terminalFailure = false

    init(
        artifactSink: AustinAliceEncryptedCaptureSink,
        consumer: AustinIsaacRedactedFrameConsuming
    ) {
        self.artifactSink = artifactSink
        self.consumer = consumer
    }

    func acceptRedacted(_ frame: AustinPixelFrame) throws {
        lock.lock()
        defer { lock.unlock() }
        guard !terminalFailure else {
            throw AustinFailure("capture_consumer_terminal")
        }

        do {
            guard try artifactSink.takeReceipts().isEmpty else {
                throw AustinFailure("capture_consumer_pending")
            }
            try artifactSink.acceptRedacted(frame)
            let grants = try artifactSink.takeConsumerGrants()
            guard grants.count == 1, let grant = grants.first else {
                throw AustinFailure("capture_consumer_grant")
            }
            var recovered = try artifactSink.consumeRedacted(grant)
            defer { recovered.clear() }
            try consumer.consumeRedacted(&recovered)
            guard try artifactSink.takeReceipts().isEmpty else {
                throw AustinFailure("capture_consumer_residue")
            }
        } catch {
            terminalFailure = true
            try? artifactSink.revokeAll()
            throw AustinFailure("capture_consumer_failed")
        }
    }
}
