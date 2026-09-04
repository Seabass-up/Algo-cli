@testable import AustinCore
@testable import AustinDesktopCore
import Foundation
import Testing

private final class AustinIsaacFixtureConsumer: AustinIsaacRedactedFrameConsuming,
    @unchecked Sendable
{
    var frames: [AustinPixelFrame] = []
    var shouldFail = false

    func consumeRedacted(_ frame: inout AustinPixelFrame) throws {
        frames.append(frame)
        if shouldFail {
            throw AustinFailure("fixture_consumer_failure")
        }
    }
}

private final class AustinIsaacBoundaryCaptureBackend: AustinScreenCaptureBackend,
    @unchecked Sendable
{
    var calls = 0

    func capture(
        mode: AustinCaptureMode,
        expectedSelection: AustinCaptureSelectionIdentity?
    ) throws -> AustinPixelFrame {
        guard mode == .persistentProgrammatic, expectedSelection == nil else {
            throw AustinFailure("fixture_capture_mode")
        }
        calls += 1
        return try AustinPixelFrame(
            width: 2,
            height: 1,
            rgbaBytes: [1, 2, 3, 255, 4, 5, 6, 255]
        )
    }
}

private final class AustinIsaacBoundaryClassifier: AustinCaptureRedactionClassifying,
    @unchecked Sendable
{
    func preflight(for preparation: AustinVerifiedPreparation) throws {}

    func redactions(
        for frame: AustinPixelFrame,
        context: AustinCaptureRedactionContext
    ) throws -> [AustinCaptureRedaction] {
        [try AustinCaptureRedaction(x: 0, y: 0, width: 1, height: 1)]
    }
}

private func austinIsaacPrivateDirectory() throws -> URL {
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("austin-isaac-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(
        at: directory,
        withIntermediateDirectories: false,
        attributes: [.posixPermissions: 0o700]
    )
    try FileManager.default.setAttributes(
        [.posixPermissions: 0o700],
        ofItemAtPath: directory.path
    )
    return directory
}

@Test func isaacPipelineConsumesAliceArtifactLocallyAndLeavesNoCiphertext() throws {
    let directory = try austinIsaacPrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let alice = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0x61, count: 32),
        ttlMilliseconds: 1_000,
        nowMilliseconds: { 60_000 },
        randomBytes: { count in
            Data(repeating: count == 16 ? 0x62 : 0x63, count: count)
        }
    )
    let consumer = AustinIsaacFixtureConsumer()
    let pipeline = AustinIsaacSealedCapturePipeline(
        artifactSink: alice,
        consumer: consumer
    )
    let frame = try AustinPixelFrame(
        width: 2,
        height: 1,
        rgbaBytes: [0, 0, 0, 255, 4, 5, 6, 255]
    )

    try pipeline.acceptRedacted(frame)

    #expect(consumer.frames == [frame])
    #expect(try FileManager.default.contentsOfDirectory(atPath: directory.path).isEmpty)
    #expect(try alice.takeReceipts().isEmpty)
}

@Test func isaacPipelineFailureDeletesArtifactAndBecomesTerminal() throws {
    let directory = try austinIsaacPrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let alice = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0x71, count: 32),
        ttlMilliseconds: 1_000,
        nowMilliseconds: { 70_000 },
        randomBytes: { count in
            Data(repeating: count == 16 ? 0x72 : 0x73, count: count)
        }
    )
    let consumer = AustinIsaacFixtureConsumer()
    consumer.shouldFail = true
    let pipeline = AustinIsaacSealedCapturePipeline(
        artifactSink: alice,
        consumer: consumer
    )
    let frame = try AustinPixelFrame(
        width: 1,
        height: 1,
        rgbaBytes: [0, 0, 0, 255]
    )

    #expect(throws: AustinFailure.self) {
        try pipeline.acceptRedacted(frame)
    }
    #expect(consumer.frames == [frame])
    #expect(try FileManager.default.contentsOfDirectory(atPath: directory.path).isEmpty)
    #expect(throws: AustinFailure.self) {
        try pipeline.acceptRedacted(frame)
    }
    #expect(consumer.frames.count == 1)
}

@Test func isaacCaptureExecutionReturnsOnlyContentFreeOutcomeAfterLocalConsumption() throws {
    let directory = try austinIsaacPrivateDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }
    let alice = try AustinAliceEncryptedCaptureSink(
        directoryURL: directory,
        keyData: Data(repeating: 0x81, count: 32),
        ttlMilliseconds: 1_000,
        nowMilliseconds: { 80_000 },
        randomBytes: { count in
            Data(repeating: count == 16 ? 0x82 : 0x83, count: count)
        }
    )
    let consumer = AustinIsaacFixtureConsumer()
    let pipeline = AustinIsaacSealedCapturePipeline(
        artifactSink: alice,
        consumer: consumer
    )
    let backend = AustinIsaacBoundaryCaptureBackend()
    let capture = try AustinScreenCapture(
        backend: backend,
        sink: pipeline,
        randomBytes: { Data(repeating: 0x84, count: 32) }
    )
    let preparation = AustinVerifiedPreparation(
        preparationID: "00000000-0000-4000-8000-000000000621",
        requestID: "00000000-0000-4000-8000-000000000121",
        subjectID: "runtime.operator",
        operation: .observe,
        dataClass: .structural,
        route: .screenshot,
        selector: "persistent_programmatic",
        arguments: [:],
        preparationDigest: "sha256:" + String(repeating: "a", count: 64),
        issuedAtMilliseconds: 79_900,
        expiresAtMilliseconds: 81_000
    )
    let target = try AustinDesktopTargetBinding(
        targetID: "hmac-sha256:" + String(repeating: "8", count: 64),
        targetEpoch: 1,
        targetRevision: "isaac_1",
        fencingToken: 1,
        snapshotID: "00000000-0000-4000-8000-000000000821",
        snapshotSequence: 1,
        observedAtMilliseconds: 80_000,
        expiresAtMilliseconds: 81_000
    )
    let lease = try capture.issuePersistentLease(
        target: target,
        screenRecordingPermissionGranted: true,
        confirmation: AustinThomasConfirmationLease(
            preparationID: preparation.preparationID,
            action: .persistentCapture,
            issuedAtMilliseconds: 80_000
        ),
        preparationID: preparation.preparationID,
        redactionClassifier: AustinIsaacBoundaryClassifier(),
        redactionContext: AustinCaptureRedactionContext(preparation: preparation),
        nowMilliseconds: 80_000
    )
    let envelope = AustinVerifiedEnvelope(
        requestID: preparation.requestID,
        subjectID: preparation.subjectID,
        permitID: "00000000-0000-4000-8000-000000000521",
        requestDigest: "sha256:" + String(repeating: "d", count: 64),
        targetID: lease.target.targetID,
        targetEpoch: lease.target.targetEpoch,
        targetRevision: lease.target.targetRevision,
        fencingToken: lease.target.fencingToken,
        snapshotID: lease.target.snapshotID,
        snapshotSequence: lease.target.snapshotSequence,
        operation: .observe,
        dataClass: .structural,
        route: .screenshot,
        arguments: [:],
        expiresAtMilliseconds: lease.target.expiresAtMilliseconds
    )

    let outcome = capture.execute(envelope, nowMilliseconds: 80_001)
    let encoded = outcome.encoded(operation: .observe, route: .screenshot)
    let object = try AustinJSON.decodeCanonicalObject(encoded)

    #expect(outcome == AustinDesktopOutcome(.succeeded, "capture_persistent_redacted"))
    #expect(backend.calls == 1)
    #expect(consumer.frames.first?.rgbaBytes == [0, 0, 0, 255, 4, 5, 6, 255])
    #expect(try FileManager.default.contentsOfDirectory(atPath: directory.path).isEmpty)
    #expect(Set(object.keys) == ["operation", "protocol_version", "reason_code", "route", "status"])
    let text = String(decoding: encoded, as: UTF8.self)
    #expect(!text.contains("rgba"))
    #expect(!text.contains("artifact"))
    #expect(!text.contains("capability"))
}
