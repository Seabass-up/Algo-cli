import AustinCore
import CoreGraphics
import Foundation
import ImageIO
import Vision

struct AustinIsaacNormalizedRegion: Equatable, Sendable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double

    init(_ rectangle: CGRect) throws {
        let values = [
            Double(rectangle.origin.x),
            Double(rectangle.origin.y),
            Double(rectangle.size.width),
            Double(rectangle.size.height),
        ]
        guard values.allSatisfy(\.isFinite),
              values[0] >= 0,
              values[1] >= 0,
              values[2] > 0,
              values[3] > 0,
              values[0] <= 1,
              values[1] <= 1,
              values[2] <= 1,
              values[3] <= 1,
              values[0] + values[2] <= 1.000_001,
              values[1] + values[3] <= 1.000_001
        else {
            throw AustinFailure("capture_classifier_geometry")
        }
        x = min(1, values[0])
        y = min(1, values[1])
        width = min(1 - x, values[2])
        height = min(1 - y, values[3])
    }
}

protocol AustinIsaacSensitiveRegionDetecting: AnyObject {
    func regions(in frame: AustinPixelFrame) throws -> [AustinIsaacNormalizedRegion]
}

/// Local rectangle-only Vision detector. It never requests recognized text,
/// candidate strings, labels, or network work; only normalized text and face
/// boxes cross this internal seam.
final class AustinIsaacSystemSensitiveRegionDetector: AustinIsaacSensitiveRegionDetecting,
    @unchecked Sendable
{
    func regions(in frame: AustinPixelFrame) throws -> [AustinIsaacNormalizedRegion] {
        let byteCount = frame.rgbaBytes.count
        let storage = UnsafeMutableRawPointer.allocate(
            byteCount: byteCount,
            alignment: MemoryLayout<UInt8>.alignment
        )
        frame.rgbaBytes.withUnsafeBytes { bytes in
            if let baseAddress = bytes.baseAddress {
                storage.copyMemory(from: baseAddress, byteCount: byteCount)
            }
        }
        guard let provider = CGDataProvider(
            dataInfo: nil,
            data: storage,
            size: byteCount,
            releaseData: { _, pointer, count in
                let mutable = UnsafeMutableRawPointer(mutating: pointer)
                mutable.initializeMemory(as: UInt8.self, repeating: 0, count: count)
                mutable.deallocate()
            }
        ) else {
            storage.initializeMemory(as: UInt8.self, repeating: 0, count: byteCount)
            storage.deallocate()
            throw AustinFailure("capture_classifier_image")
        }
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let image = CGImage(
                  width: frame.width,
                  height: frame.height,
                  bitsPerComponent: 8,
                  bitsPerPixel: 32,
                  bytesPerRow: frame.width * 4,
                  space: colorSpace,
                  bitmapInfo: CGBitmapInfo(
                      rawValue: CGBitmapInfo.byteOrder32Big.rawValue
                          | CGImageAlphaInfo.premultipliedLast.rawValue
                  ),
                  provider: provider,
                  decode: nil,
                  shouldInterpolate: false,
                  intent: .defaultIntent
              )
        else {
            throw AustinFailure("capture_classifier_image")
        }

        let text = VNDetectTextRectanglesRequest()
        text.reportCharacterBoxes = false
        let faces = VNDetectFaceRectanglesRequest()
        let handler = VNImageRequestHandler(cgImage: image, orientation: .up, options: [:])
        do {
            try handler.perform([text, faces])
        } catch {
            throw AustinFailure("capture_classifier_vision")
        }
        let rectangles = (text.results ?? []).map(\.boundingBox)
            + (faces.results ?? []).map(\.boundingBox)
        do {
            return try rectangles.map(AustinIsaacNormalizedRegion.init)
        } catch {
            throw AustinFailure("capture_classifier_geometry")
        }
    }
}

private struct AustinIsaacPixelRegion: Equatable {
    var left: Int
    var top: Int
    var right: Int
    var bottom: Int

    var area: Int { (right - left) * (bottom - top) }

    func touches(_ other: Self) -> Bool {
        left <= other.right && other.left <= right
            && top <= other.bottom && other.top <= bottom
    }

    func union(_ other: Self) -> Self {
        Self(
            left: min(left, other.left),
            top: min(top, other.top),
            right: max(right, other.right),
            bottom: max(bottom, other.bottom)
        )
    }
}

/// Unqualified production candidate for post-acquisition redaction. Structural
/// and public frames redact Vision text/face rectangles. Private, empty,
/// overloaded, or overly fragmented classifications fall back to one full-
/// frame redaction. This type is deliberately internal and is not assembled by
/// AustinThomasProductionControl until a signed accuracy and live-TCC matrix is
/// available.
final class AustinIsaacVisionRedactionCandidate: AustinCaptureRedactionClassifying,
    @unchecked Sendable
{
    static let maximumDetectedRegions = 256
    static let maximumOutputRegions = 64
    static let paddingPixels = 4

    private let detector: AustinIsaacSensitiveRegionDetecting

    init(detector: AustinIsaacSensitiveRegionDetecting = AustinIsaacSystemSensitiveRegionDetector()) {
        self.detector = detector
    }

    func preflight(for preparation: AustinVerifiedPreparation) throws {
        guard preparation.operation == .observe,
              preparation.route == .screenshot,
              preparation.selector == AustinCaptureMode.persistentProgrammatic.rawValue,
              preparation.arguments.isEmpty
        else {
            throw AustinFailure("capture_classifier_preflight")
        }
    }

    func redactions(
        for frame: AustinPixelFrame,
        context: AustinCaptureRedactionContext
    ) throws -> [AustinCaptureRedaction] {
        if context.dataClass == .private {
            return [try fullFrame(frame)]
        }
        let detected = try detector.regions(in: frame)
        guard !detected.isEmpty,
              detected.count <= Self.maximumDetectedRegions
        else {
            return [try fullFrame(frame)]
        }
        var regions: [AustinIsaacPixelRegion] = []
        regions.reserveCapacity(min(detected.count, Self.maximumOutputRegions))
        for normalized in detected {
            var candidate = try pixelRegion(normalized, frame: frame)
            var index = 0
            while index < regions.count {
                if candidate.touches(regions[index]) {
                    candidate = candidate.union(regions.remove(at: index))
                    index = 0
                } else {
                    index += 1
                }
            }
            regions.append(candidate)
        }
        guard regions.count <= Self.maximumOutputRegions else {
            return [try fullFrame(frame)]
        }
        regions.sort {
            ($0.top, $0.left, $0.bottom, $0.right)
                < ($1.top, $1.left, $1.bottom, $1.right)
        }
        let maximumWork = frame.width * frame.height
        var work = 0
        for region in regions {
            guard region.area > 0, region.area <= maximumWork - work else {
                return [try fullFrame(frame)]
            }
            work += region.area
        }
        return try regions.map {
            try AustinCaptureRedaction(
                x: $0.left,
                y: $0.top,
                width: $0.right - $0.left,
                height: $0.bottom - $0.top
            )
        }
    }

    private func pixelRegion(
        _ region: AustinIsaacNormalizedRegion,
        frame: AustinPixelFrame
    ) throws -> AustinIsaacPixelRegion {
        let padding = Self.paddingPixels
        let left = max(0, Int(floor(region.x * Double(frame.width))) - padding)
        let right = min(
            frame.width,
            Int(ceil((region.x + region.width) * Double(frame.width))) + padding
        )
        let top = max(
            0,
            Int(floor((1 - region.y - region.height) * Double(frame.height))) - padding
        )
        let bottom = min(
            frame.height,
            Int(ceil((1 - region.y) * Double(frame.height))) + padding
        )
        guard left < right, top < bottom else {
            throw AustinFailure("capture_classifier_geometry")
        }
        return AustinIsaacPixelRegion(left: left, top: top, right: right, bottom: bottom)
    }

    private func fullFrame(_ frame: AustinPixelFrame) throws -> AustinCaptureRedaction {
        try AustinCaptureRedaction(x: 0, y: 0, width: frame.width, height: frame.height)
    }
}
