@testable import AustinCore
@testable import AustinDesktopCore
import CoreGraphics
import Foundation
import Testing

private final class AustinIsaacFixtureRegionDetector: AustinIsaacSensitiveRegionDetecting,
    @unchecked Sendable
{
    var values: [AustinIsaacNormalizedRegion]
    var calls = 0

    init(_ rectangles: [CGRect]) throws {
        values = try rectangles.map(AustinIsaacNormalizedRegion.init)
    }

    func regions(in frame: AustinPixelFrame) throws -> [AustinIsaacNormalizedRegion] {
        calls += 1
        return values
    }
}

private func austinIsaacPreparation(
    dataClass: AustinDataClass = .structural,
    route: AustinRoute = .screenshot
) -> AustinVerifiedPreparation {
    AustinVerifiedPreparation(
        preparationID: "00000000-0000-4000-8000-000000000611",
        requestID: "00000000-0000-4000-8000-000000000111",
        subjectID: "runtime.operator",
        operation: .observe,
        dataClass: dataClass,
        route: route,
        selector: "persistent_programmatic",
        arguments: [:],
        preparationDigest: "sha256:" + String(repeating: "a", count: 64),
        issuedAtMilliseconds: 100,
        expiresAtMilliseconds: 1_000
    )
}

private func austinIsaacFrame(width: Int = 100, height: Int = 80) throws -> AustinPixelFrame {
    try AustinPixelFrame(
        width: width,
        height: height,
        rgbaBytes: [UInt8](repeating: 0x7F, count: width * height * 4)
    )
}

@Test func isaacVisionCandidateMapsBottomLeftGeometryOutwardAndMergesOverlap() throws {
    let detector = try AustinIsaacFixtureRegionDetector([
        CGRect(x: 0.25, y: 0.25, width: 0.5, height: 0.5),
        CGRect(x: 0.70, y: 0.30, width: 0.10, height: 0.10),
    ])
    let classifier = AustinIsaacVisionRedactionCandidate(detector: detector)
    let preparation = austinIsaacPreparation()
    try classifier.preflight(for: preparation)
    let frame = try austinIsaacFrame()

    let regions = try classifier.redactions(
        for: frame,
        context: AustinCaptureRedactionContext(preparation: preparation)
    )

    #expect(detector.calls == 1)
    #expect(regions == [try AustinCaptureRedaction(x: 21, y: 16, width: 63, height: 48)])
}

@Test func isaacVisionCandidateFallsBackToFullFrameForPrivateEmptyAndOverflow() throws {
    let empty = try AustinIsaacFixtureRegionDetector([])
    let classifier = AustinIsaacVisionRedactionCandidate(detector: empty)
    let frame = try austinIsaacFrame(width: 16, height: 8)
    let structural = austinIsaacPreparation()
    let expected = try AustinCaptureRedaction(x: 0, y: 0, width: 16, height: 8)

    #expect(
        try classifier.redactions(
            for: frame,
            context: AustinCaptureRedactionContext(preparation: structural)
        ) == [expected]
    )
    let privatePreparation = austinIsaacPreparation(dataClass: .private)
    #expect(
        try classifier.redactions(
            for: frame,
            context: AustinCaptureRedactionContext(preparation: privatePreparation)
        ) == [expected]
    )
    #expect(empty.calls == 1)

    let overflow = try AustinIsaacFixtureRegionDetector(
        (0...AustinIsaacVisionRedactionCandidate.maximumDetectedRegions).map { index in
            CGRect(
                x: Double(index % 16) / 32,
                y: Double((index / 16) % 16) / 32,
                width: 0.01,
                height: 0.01
            )
        }
    )
    let overflowClassifier = AustinIsaacVisionRedactionCandidate(detector: overflow)
    #expect(
        try overflowClassifier.redactions(
            for: frame,
            context: AustinCaptureRedactionContext(preparation: structural)
        ) == [expected]
    )
}

@Test func isaacVisionCandidateRejectsWrongPreflightAndSystemDetectorStaysBounded() throws {
    let classifier = AustinIsaacVisionRedactionCandidate(
        detector: try AustinIsaacFixtureRegionDetector([])
    )
    #expect(throws: AustinFailure.self) {
        try classifier.preflight(for: austinIsaacPreparation(route: .ax))
    }

    let frame = try austinIsaacFrame(width: 64, height: 64)
    let regions = try AustinIsaacSystemSensitiveRegionDetector().regions(in: frame)
    #expect(regions.count <= AustinIsaacVisionRedactionCandidate.maximumDetectedRegions)
}
