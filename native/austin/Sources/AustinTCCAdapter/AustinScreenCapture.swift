import AustinCore
import CoreGraphics
import CoreVideo
import Foundation
import ScreenCaptureKit

public enum AustinCaptureMode: String, Sendable {
    case pickerScoped = "picker_scoped"
    case persistentProgrammatic = "persistent_programmatic"
}

public enum AustinCaptureSelectionKind: String, Sendable {
    case window
    case display
}

/// Content-free identity for exactly one picker-selected window or display.
/// Raw window titles, application names, bundle identifiers, and pixels are
/// never retained. Geometry uses exact IEEE-754 bit patterns so a substituted
/// or changed filter cannot compare equal through rounding.
public struct AustinCaptureSelectionIdentity: Equatable, Sendable {
    private let kind: AustinCaptureSelectionKind
    private let contentID: UInt32
    private let xBits: UInt64
    private let yBits: UInt64
    private let widthBits: UInt64
    private let heightBits: UInt64
    private let scaleBits: UInt64

    init(
        kind: AustinCaptureSelectionKind,
        contentID: UInt32,
        x: Double,
        y: Double,
        width: Double,
        height: Double,
        pointPixelScale: Double
    ) throws {
        guard contentID > 0,
              x.isFinite, y.isFinite,
              width.isFinite, height.isFinite, pointPixelScale.isFinite,
              abs(x) <= 1_000_000, abs(y) <= 1_000_000,
              width > 0, height > 0,
              width <= 1_000_000, height <= 1_000_000,
              pointPixelScale > 0, pointPixelScale <= 16
        else {
            throw AustinFailure("capture_picker_identity")
        }
        self.kind = kind
        self.contentID = contentID
        xBits = x.bitPattern
        yBits = y.bitPattern
        widthBits = width.bitPattern
        heightBits = height.bitPattern
        scaleBits = pointPixelScale.bitPattern
    }

    @available(macOS 15.2, *)
    fileprivate static func exact(filter: SCContentFilter) throws -> Self {
        let kind: AustinCaptureSelectionKind
        let contentID: UInt32
        switch filter.style {
        case .window:
            guard filter.includedWindows.count == 1 else {
                throw AustinFailure("capture_picker_target_count")
            }
            kind = .window
            contentID = filter.includedWindows[0].windowID
        case .display:
            guard filter.includedDisplays.count == 1 else {
                throw AustinFailure("capture_picker_target_count")
            }
            kind = .display
            contentID = filter.includedDisplays[0].displayID
        default:
            throw AustinFailure("capture_picker_target_kind")
        }
        return try Self(
            kind: kind,
            contentID: contentID,
            x: Double(filter.contentRect.origin.x),
            y: Double(filter.contentRect.origin.y),
            width: Double(filter.contentRect.width),
            height: Double(filter.contentRect.height),
            pointPixelScale: Double(filter.pointPixelScale)
        )
    }
}

/// The exact picker filter and the content-free identity derived from that
/// same object. Construction and revalidation require the macOS 15.2 identity
/// API; older picker-only systems fail before presenting UI.
public struct AustinBoundCaptureFilter: @unchecked Sendable {
    fileprivate let filter: SCContentFilter
    public let identity: AustinCaptureSelectionIdentity

    @available(macOS 15.2, *)
    fileprivate init(filter: SCContentFilter) throws {
        self.filter = filter
        identity = try AustinCaptureSelectionIdentity.exact(filter: filter)
    }

    @available(macOS 15.2, *)
    fileprivate func validatedFilter(
        expectedIdentity: AustinCaptureSelectionIdentity
    ) throws -> SCContentFilter {
        guard identity == expectedIdentity,
              try AustinCaptureSelectionIdentity.exact(filter: filter) == expectedIdentity
        else {
            throw AustinFailure("capture_picker_target_changed")
        }
        return filter
    }
}

public struct AustinCaptureRedaction: Equatable, Sendable {
    public let x: Int
    public let y: Int
    public let width: Int
    public let height: Int

    public init(x: Int, y: Int, width: Int, height: Int) throws {
        guard x >= 0, y >= 0, width > 0, height > 0,
              x <= 16_384, y <= 16_384,
              width <= 16_384, height <= 16_384
        else {
            throw AustinFailure("capture_redaction")
        }
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }
}

/// Content-free fields that bind post-acquisition classification to the
/// preparation that the native adapter preflighted and confirmed. The raw
/// preparation, target identity, application content, and pixels never enter
/// an XPC reply or persisted artifact metadata.
public struct AustinCaptureRedactionContext: Equatable, Sendable {
    public let preparationID: String
    public let requestID: String
    public let subjectID: String
    public let dataClass: AustinDataClass

    init(preparation: AustinVerifiedPreparation) {
        preparationID = preparation.preparationID
        requestID = preparation.requestID
        subjectID = preparation.subjectID
        dataClass = preparation.dataClass
    }
}

/// Trusted, code-sealed local redaction authority. Preflight proves that the
/// classifier is available before confirmation. Classification receives the
/// bounded frame only after capture and must return structural rectangles;
/// the screen-capture owner applies and validates those rectangles before any
/// sink can observe the frame.
public protocol AustinCaptureRedactionClassifying: AnyObject {
    func preflight(for preparation: AustinVerifiedPreparation) throws
    func redactions(
        for frame: AustinPixelFrame,
        context: AustinCaptureRedactionContext
    ) throws -> [AustinCaptureRedaction]
}

public struct AustinPixelFrame: Equatable, Sendable {
    public static let maximumBytes = 67_108_864

    public let width: Int
    public let height: Int
    public private(set) var rgbaBytes: [UInt8]

    public init(width: Int, height: Int, rgbaBytes: [UInt8]) throws {
        guard width > 0, height > 0, width <= 16_384, height <= 16_384,
              width <= Int.max / height,
              width * height <= Self.maximumBytes / 4,
              rgbaBytes.count == width * height * 4
        else {
            throw AustinFailure("capture_frame")
        }
        self.width = width
        self.height = height
        self.rgbaBytes = rgbaBytes
    }

    public mutating func redact(_ regions: [AustinCaptureRedaction]) throws {
        guard !regions.isEmpty, regions.count <= 64 else {
            throw AustinFailure("capture_redaction_count")
        }
        let maximumWorkPixels = width * height
        var workPixels = 0
        for region in regions {
            guard region.x <= width - region.width,
                  region.y <= height - region.height,
                  region.width <= Int.max / region.height
            else {
                throw AustinFailure("capture_redaction_bounds")
            }
            let regionPixels = region.width * region.height
            guard regionPixels <= maximumWorkPixels - workPixels else {
                throw AustinFailure("capture_redaction_work")
            }
            workPixels += regionPixels
        }
        for region in regions {
            for row in region.y..<(region.y + region.height) {
                for column in region.x..<(region.x + region.width) {
                    let offset = (row * width + column) * 4
                    rgbaBytes[offset] = 0
                    rgbaBytes[offset + 1] = 0
                    rgbaBytes[offset + 2] = 0
                    rgbaBytes[offset + 3] = 255
                }
            }
        }
    }

    public mutating func clear() {
        _ = rgbaBytes.withUnsafeMutableBytes { bytes in
            bytes.initializeMemory(as: UInt8.self, repeating: 0)
        }
        rgbaBytes.removeAll(keepingCapacity: false)
    }
}

public protocol AustinScreenCaptureBackend: AnyObject {
    func capture(
        mode: AustinCaptureMode,
        expectedSelection: AustinCaptureSelectionIdentity?
    ) throws -> AustinPixelFrame
}

public protocol AustinRedactedCaptureSink: AnyObject {
    func acceptRedacted(_ frame: AustinPixelFrame) throws
}

public struct AustinCaptureLease: Equatable, Sendable {
    public let leaseID: String
    public let mode: AustinCaptureMode
    public let target: AustinDesktopTargetBinding

    public init(
        leaseID: String,
        mode: AustinCaptureMode,
        target: AustinDesktopTargetBinding
    ) throws {
        guard leaseID.range(
            of: "^hmac-sha256:[0-9a-f]{64}$",
            options: .regularExpression
        ) != nil else {
            throw AustinFailure("capture_lease_id")
        }
        self.leaseID = leaseID
        self.mode = mode
        self.target = target
    }
}

private final class AustinCaptureRecord: @unchecked Sendable {
    let lease: AustinCaptureLease
    let fixedRedactions: [AustinCaptureRedaction]?
    let redactionClassifier: AustinCaptureRedactionClassifying?
    let redactionContext: AustinCaptureRedactionContext?
    let pickerSelection: AustinCaptureSelectionIdentity?
    var consumed = false

    init(
        lease: AustinCaptureLease,
        fixedRedactions: [AustinCaptureRedaction]?,
        redactionClassifier: AustinCaptureRedactionClassifying?,
        redactionContext: AustinCaptureRedactionContext?,
        pickerSelection: AustinCaptureSelectionIdentity?
    ) {
        self.lease = lease
        self.fixedRedactions = fixedRedactions
        self.redactionClassifier = redactionClassifier
        self.redactionContext = redactionContext
        self.pickerSelection = pickerSelection
    }
}

public final class AustinScreenCapture: @unchecked Sendable {
    public static let maximumLeaseLifetimeMilliseconds: Int64 = 3_000
    public static let maximumLeases = 64

    private let backend: AustinScreenCaptureBackend
    private let sink: AustinRedactedCaptureSink
    private let issuer: AustinOpaqueTokenIssuer
    private let lock = NSLock()
    private var records: [String: AustinCaptureRecord] = [:]

    public init(
        backend: AustinScreenCaptureBackend,
        sink: AustinRedactedCaptureSink,
        randomBytes: @escaping () throws -> Data = AustinSession.secureRandomBytes
    ) throws {
        self.backend = backend
        self.sink = sink
        issuer = try AustinOpaqueTokenIssuer(randomBytes: randomBytes)
    }

    public func issuePickerLease(
        target: AustinDesktopTargetBinding,
        userGestureConfirmed: Bool,
        selection: AustinCaptureSelectionIdentity,
        redactions: [AustinCaptureRedaction],
        nowMilliseconds: Int64,
        lifetimeMilliseconds: Int64 = maximumLeaseLifetimeMilliseconds
    ) throws -> AustinCaptureLease {
        guard userGestureConfirmed else {
            throw AustinFailure("capture_picker_confirmation")
        }
        return try issue(
            mode: .pickerScoped,
            target: target,
            pickerSelection: selection,
            fixedRedactions: redactions,
            redactionClassifier: nil,
            redactionContext: nil,
            nowMilliseconds: nowMilliseconds,
            lifetimeMilliseconds: lifetimeMilliseconds
        )
    }

    func issuePersistentLease(
        target: AustinDesktopTargetBinding,
        screenRecordingPermissionGranted: Bool,
        confirmation: AustinThomasConfirmationLease,
        preparationID: String,
        redactionClassifier: AustinCaptureRedactionClassifying,
        redactionContext: AustinCaptureRedactionContext,
        nowMilliseconds: Int64,
        lifetimeMilliseconds: Int64 = maximumLeaseLifetimeMilliseconds
    ) throws -> AustinCaptureLease {
        guard screenRecordingPermissionGranted else {
            throw AustinFailure("capture_permission_denied")
        }
        guard redactionContext.preparationID == preparationID else {
            throw AustinFailure("capture_redaction_authority")
        }
        try confirmation.claim(
            action: .persistentCapture,
            preparationID: preparationID,
            nowMilliseconds: nowMilliseconds
        )
        return try issue(
            mode: .persistentProgrammatic,
            target: target,
            pickerSelection: nil,
            fixedRedactions: nil,
            redactionClassifier: redactionClassifier,
            redactionContext: redactionContext,
            nowMilliseconds: nowMilliseconds,
            lifetimeMilliseconds: lifetimeMilliseconds
        )
    }

    public func supports(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> Bool {
        guard envelope.route == .screenshot, envelope.operation == .observe else { return false }
        lock.lock()
        defer { lock.unlock() }
        guard let record = records[envelope.targetID] else { return false }
        return !record.consumed
            && record.lease.target.matches(envelope)
            && redactionAuthorityMatches(record, envelope: envelope)
            && nowMilliseconds >= record.lease.target.observedAtMilliseconds
            && nowMilliseconds < record.lease.target.expiresAtMilliseconds
    }

    public func execute(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> AustinDesktopOutcome {
        guard envelope.route == .screenshot, envelope.operation == .observe else {
            return AustinDesktopOutcome(.denied, "capture_operation_denied")
        }
        let record: AustinCaptureRecord
        lock.lock()
        if let existing = records[envelope.targetID], !existing.consumed {
            existing.consumed = true
            record = existing
        } else {
            lock.unlock()
            return AustinDesktopOutcome(.denied, "capture_lease_stale")
        }
        lock.unlock()
        guard record.lease.target.matches(envelope) else {
            return AustinDesktopOutcome(.denied, "capture_target_changed")
        }
        guard redactionAuthorityMatches(record, envelope: envelope) else {
            return AustinDesktopOutcome(.denied, "capture_redaction_authority")
        }
        guard nowMilliseconds >= record.lease.target.observedAtMilliseconds,
              nowMilliseconds < record.lease.target.expiresAtMilliseconds
        else {
            return AustinDesktopOutcome(.denied, "capture_lease_expired")
        }

        var frame: AustinPixelFrame
        do {
            frame = try backend.capture(
                mode: record.lease.mode,
                expectedSelection: record.pickerSelection
            )
        } catch {
            return AustinDesktopOutcome(.failed, "capture_failed")
        }
        defer { frame.clear() }
        do {
            let redactions: [AustinCaptureRedaction]
            switch record.lease.mode {
            case .pickerScoped:
                guard let fixedRedactions = record.fixedRedactions,
                      record.redactionClassifier == nil,
                      record.redactionContext == nil
                else {
                    throw AustinFailure("capture_redaction_authority")
                }
                redactions = fixedRedactions
            case .persistentProgrammatic:
                guard record.fixedRedactions == nil,
                      let classifier = record.redactionClassifier,
                      let context = record.redactionContext
                else {
                    throw AustinFailure("capture_redaction_authority")
                }
                redactions = try classifier.redactions(for: frame, context: context)
            }
            guard !redactions.isEmpty, redactions.count <= 64 else {
                throw AustinFailure("capture_redaction_classifier")
            }
            try frame.redact(redactions)
            try sink.acceptRedacted(frame)
        } catch {
            return AustinDesktopOutcome(.failed, "capture_redaction_failed")
        }
        return AustinDesktopOutcome(
            .succeeded,
            record.lease.mode == .pickerScoped
                ? "capture_picker_redacted"
                : "capture_persistent_redacted"
        )
    }

    private func issue(
        mode: AustinCaptureMode,
        target: AustinDesktopTargetBinding,
        pickerSelection: AustinCaptureSelectionIdentity?,
        fixedRedactions: [AustinCaptureRedaction]?,
        redactionClassifier: AustinCaptureRedactionClassifying?,
        redactionContext: AustinCaptureRedactionContext?,
        nowMilliseconds: Int64,
        lifetimeMilliseconds: Int64
    ) throws -> AustinCaptureLease {
        guard nowMilliseconds >= target.observedAtMilliseconds,
              nowMilliseconds < target.expiresAtMilliseconds,
              lifetimeMilliseconds > 0,
              lifetimeMilliseconds <= Self.maximumLeaseLifetimeMilliseconds,
              nowMilliseconds <= austinMaximumSafeInteger - lifetimeMilliseconds,
              (mode == .pickerScoped) == (pickerSelection != nil),
              (mode == .pickerScoped) == (fixedRedactions != nil),
              (mode == .persistentProgrammatic) == (redactionClassifier != nil),
              (mode == .persistentProgrammatic) == (redactionContext != nil),
              fixedRedactions?.isEmpty != true,
              (fixedRedactions?.count ?? 0) <= 64
        else {
            throw AustinFailure("capture_lease")
        }
        let expires = min(target.expiresAtMilliseconds, nowMilliseconds + lifetimeMilliseconds)
        let scopedTarget = try AustinDesktopTargetBinding(
            targetID: target.targetID,
            targetEpoch: target.targetEpoch,
            targetRevision: target.targetRevision,
            fencingToken: target.fencingToken,
            snapshotID: target.snapshotID,
            snapshotSequence: target.snapshotSequence,
            observedAtMilliseconds: target.observedAtMilliseconds,
            expiresAtMilliseconds: expires
        )
        let leaseID = try issuer.issue(
            domain: "capture_lease",
            fields: [target.targetID, mode.rawValue, String(expires)]
        )
        let lease = try AustinCaptureLease(leaseID: leaseID, mode: mode, target: scopedTarget)
        lock.lock()
        records = records.filter {
            !$0.value.consumed && $0.value.lease.target.expiresAtMilliseconds > nowMilliseconds
        }
        guard records[target.targetID] == nil else {
            lock.unlock()
            throw AustinFailure("capture_lease_conflict")
        }
        guard records.count < Self.maximumLeases else {
            lock.unlock()
            throw AustinFailure("capture_lease_capacity")
        }
        records[target.targetID] = AustinCaptureRecord(
            lease: lease,
            fixedRedactions: fixedRedactions,
            redactionClassifier: redactionClassifier,
            redactionContext: redactionContext,
            pickerSelection: pickerSelection
        )
        lock.unlock()
        return lease
    }

    private func redactionAuthorityMatches(
        _ record: AustinCaptureRecord,
        envelope: AustinVerifiedEnvelope
    ) -> Bool {
        switch record.lease.mode {
        case .pickerScoped:
            return record.fixedRedactions != nil
                && record.redactionClassifier == nil
                && record.redactionContext == nil
        case .persistentProgrammatic:
            guard record.fixedRedactions == nil,
                  record.redactionClassifier != nil,
                  let context = record.redactionContext
            else {
                return false
            }
            return context.requestID == envelope.requestID
                && context.subjectID == envelope.subjectID
                && context.dataClass == envelope.dataClass
        }
    }
}

public enum AustinScreenCapturePermission {
    public static func persistentPreflightOnly() -> Bool {
        CGPreflightScreenCaptureAccess()
    }

    public static var systemPickerAPIAvailable: Bool {
        if #available(macOS 15.2, *) {
            _ = SCContentSharingPicker.shared
            return true
        }
        return false
    }
}

private final class AustinScreenshotCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private let semaphore = DispatchSemaphore(value: 0)
    private var completed = false
    private var abandoned = false
    private var image: CGImage?

    func resolve(_ image: CGImage?) {
        lock.lock()
        guard !completed, !abandoned else {
            lock.unlock()
            return
        }
        completed = true
        self.image = image
        lock.unlock()
        semaphore.signal()
    }

    func wait(timeoutMilliseconds: Int) -> Bool {
        semaphore.wait(timeout: .now() + .milliseconds(timeoutMilliseconds)) == .success
    }

    func take() -> CGImage? {
        lock.lock()
        defer { lock.unlock() }
        let value = image
        image = nil
        return value
    }

    func abandon() {
        lock.lock()
        abandoned = true
        image = nil
        lock.unlock()
    }
}

/// The production ScreenCaptureKit bridge. Picker and persistent filters are
/// supplied through distinct closures so one authorization path cannot be
/// silently substituted for the other. The bridge captures one cursor-free,
/// audio-free frame and converts it to bounded RGBA memory for local redaction.
public final class AustinSystemScreenCaptureBackend: AustinScreenCaptureBackend,
    @unchecked Sendable
{
    public static let captureTimeoutMilliseconds = 2_000

    public typealias PickerFilterProvider = @Sendable () throws -> AustinBoundCaptureFilter
    public typealias PersistentFilterProvider = @Sendable () throws -> SCContentFilter
    typealias CaptureOperation = @Sendable (
        AustinCaptureMode,
        AustinCaptureSelectionIdentity?,
        @escaping @Sendable (CGImage?) -> Void
    ) throws -> Void

    private let captureOperation: CaptureOperation
    private let timeoutMilliseconds: Int

    public convenience init(
        pickerFilterProvider: @escaping PickerFilterProvider,
        persistentFilterProvider: @escaping PersistentFilterProvider
    ) throws {
        try self.init(
            timeoutMilliseconds: Self.captureTimeoutMilliseconds,
            captureOperation: { mode, expectedSelection, completion in
                let filter: SCContentFilter
                switch mode {
                case .pickerScoped:
                    guard #available(macOS 15.2, *), let expectedSelection else {
                        throw AustinFailure("capture_picker_identity_unavailable")
                    }
                    filter = try pickerFilterProvider().validatedFilter(
                        expectedIdentity: expectedSelection
                    )
                case .persistentProgrammatic:
                    guard expectedSelection == nil else {
                        throw AustinFailure("capture_mode_binding")
                    }
                    filter = try persistentFilterProvider()
                }
                let configuration = try Self.configuration(for: filter)
                SCScreenshotManager.captureImage(
                    contentFilter: filter,
                    configuration: configuration
                ) { image, error in
                    completion(error == nil ? image : nil)
                }
            }
        )
    }

    init(
        timeoutMilliseconds: Int,
        captureOperation: @escaping CaptureOperation
    ) throws {
        guard timeoutMilliseconds > 0,
              timeoutMilliseconds <= Self.captureTimeoutMilliseconds
        else {
            throw AustinFailure("capture_timeout")
        }
        self.timeoutMilliseconds = timeoutMilliseconds
        self.captureOperation = captureOperation
    }

    public func capture(
        mode: AustinCaptureMode,
        expectedSelection: AustinCaptureSelectionIdentity?
    ) throws -> AustinPixelFrame {
        guard (mode == .pickerScoped) == (expectedSelection != nil) else {
            throw AustinFailure("capture_mode_binding")
        }
        let completion = AustinScreenshotCompletion()
        do {
            try captureOperation(mode, expectedSelection) { image in
                completion.resolve(image)
            }
        } catch {
            completion.abandon()
            throw AustinFailure("capture_start")
        }
        guard completion.wait(timeoutMilliseconds: timeoutMilliseconds) else {
            completion.abandon()
            throw AustinFailure("capture_timeout")
        }
        guard let image = completion.take() else {
            throw AustinFailure("capture_image")
        }
        return try Self.rgbaFrame(from: image)
    }

    static func configuration(for filter: SCContentFilter) throws -> SCStreamConfiguration {
        let rawWidth = Double(filter.contentRect.width) * Double(filter.pointPixelScale)
        let rawHeight = Double(filter.contentRect.height) * Double(filter.pointPixelScale)
        let maximumDimension = 16_384.0
        let maximumPixels = Double(AustinPixelFrame.maximumBytes / 4)
        guard rawWidth.isFinite, rawHeight.isFinite,
              rawWidth > 0, rawHeight > 0,
              rawWidth * rawHeight > 0,
              (rawWidth * rawHeight).isFinite
        else {
            throw AustinFailure("capture_filter_geometry")
        }
        let scale = min(
            1.0,
            maximumDimension / rawWidth,
            maximumDimension / rawHeight,
            sqrt(maximumPixels / (rawWidth * rawHeight))
        )
        let width = max(1, Int(floor(rawWidth * scale)))
        let height = max(1, Int(floor(rawHeight * scale)))
        guard width <= Int.max / height,
              width * height <= AustinPixelFrame.maximumBytes / 4
        else {
            throw AustinFailure("capture_filter_geometry")
        }
        let configuration = SCStreamConfiguration()
        configuration.width = width
        configuration.height = height
        configuration.pixelFormat = kCVPixelFormatType_32BGRA
        configuration.showsCursor = false
        configuration.capturesAudio = false
        configuration.excludesCurrentProcessAudio = true
        configuration.queueDepth = 1
        configuration.scalesToFit = true
        return configuration
    }

    static func rgbaFrame(from image: CGImage) throws -> AustinPixelFrame {
        let width = image.width
        let height = image.height
        guard width > 0, height > 0,
              width <= 16_384, height <= 16_384,
              width <= Int.max / height,
              width * height <= AustinPixelFrame.maximumBytes / 4,
              let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)
        else {
            throw AustinFailure("capture_image_geometry")
        }
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        let bitmapInfo = CGBitmapInfo.byteOrder32Big.rawValue
            | CGImageAlphaInfo.premultipliedLast.rawValue
        let rendered = bytes.withUnsafeMutableBytes { storage -> Bool in
            guard let baseAddress = storage.baseAddress,
                  let context = CGContext(
                      data: baseAddress,
                      width: width,
                      height: height,
                      bitsPerComponent: 8,
                      bytesPerRow: width * 4,
                      space: colorSpace,
                      bitmapInfo: bitmapInfo
                  )
            else {
                return false
            }
            context.interpolationQuality = .none
            context.translateBy(x: 0, y: CGFloat(height))
            context.scaleBy(x: 1, y: -1)
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard rendered else {
            _ = bytes.withUnsafeMutableBytes {
                $0.initializeMemory(as: UInt8.self, repeating: 0)
            }
            throw AustinFailure("capture_image_render")
        }
        return try AustinPixelFrame(width: width, height: height, rgbaBytes: bytes)
    }
}

enum AustinCaptureSelectionState: String, Sendable {
    case idle
    case presenting
    case selected
    case consumed
    case cancelled
    case failed
}

final class AustinOneShotCaptureSelection<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var state: AustinCaptureSelectionState = .idle
    private var value: Value?

    func begin() throws {
        lock.lock()
        defer { lock.unlock() }
        guard state != .presenting, state != .selected else {
            throw AustinFailure("capture_picker_busy")
        }
        value = nil
        state = .presenting
    }

    func select(_ value: Value) throws {
        lock.lock()
        defer { lock.unlock() }
        guard state == .presenting else {
            throw AustinFailure("capture_picker_stale_selection")
        }
        self.value = value
        state = .selected
    }

    func cancel() {
        lock.lock()
        if state == .presenting {
            value = nil
            state = .cancelled
        }
        lock.unlock()
    }

    /// Explicit shutdown is stronger than a late picker callback: it revokes
    /// either an in-flight presentation or an unconsumed selection.
    func revoke() {
        lock.lock()
        if state == .presenting || state == .selected {
            value = nil
            state = .cancelled
        }
        lock.unlock()
    }

    func fail() {
        lock.lock()
        if state == .presenting {
            value = nil
            state = .failed
        }
        lock.unlock()
    }

    func consume() throws -> Value {
        lock.lock()
        defer { lock.unlock() }
        guard state == .selected, let value else {
            throw AustinFailure("capture_picker_selection_unavailable")
        }
        self.value = nil
        state = .consumed
        return value
    }

    func currentState() -> AustinCaptureSelectionState {
        lock.lock()
        defer { lock.unlock() }
        return state
    }
}

/// Owns the public system picker and turns exactly one confirmed selection
/// into exactly one identity-bound ScreenCaptureKit filter. Presentation is
/// main-thread and user-gesture gated; capture workers can only consume the
/// selected filter on systems that expose exact target identity.
public final class AustinSystemPickerCaptureSource: NSObject,
    SCContentSharingPickerObserver,
    @unchecked Sendable
{
    private let selection = AustinOneShotCaptureSelection<AustinBoundCaptureFilter>()
    private let picker: SCContentSharingPicker
    private var observing = false

    public override convenience init() {
        self.init(picker: .shared)
    }

    init(picker: SCContentSharingPicker) {
        self.picker = picker
        super.init()
    }

    public func present(userGestureConfirmed: Bool) throws {
        guard Thread.isMainThread else {
            throw AustinFailure("capture_picker_main_thread")
        }
        guard userGestureConfirmed else {
            throw AustinFailure("capture_picker_user_gesture")
        }
        guard #available(macOS 15.2, *) else {
            throw AustinFailure("capture_picker_identity_unavailable")
        }
        try selection.begin()
        var configuration = SCContentSharingPickerConfiguration()
        configuration.allowedPickerModes = [.singleWindow, .singleDisplay]
        configuration.excludedBundleIDs = ["com.algo-cli.austin.control"]
        configuration.excludedWindowIDs = []
        configuration.allowsChangingSelectedContent = false
        picker.configuration = configuration
        picker.maximumStreamCount = 1
        if !observing {
            picker.add(self)
            observing = true
        }
        picker.isActive = true
        picker.present()
    }

    public func consumeSelectedFilter() throws -> AustinBoundCaptureFilter {
        try selection.consume()
    }

    public func stop() throws {
        guard Thread.isMainThread else {
            throw AustinFailure("capture_picker_main_thread")
        }
        selection.revoke()
        if observing {
            picker.remove(self)
            observing = false
        }
        picker.isActive = false
    }

    public func contentSharingPicker(
        _ picker: SCContentSharingPicker,
        didCancelFor stream: SCStream?
    ) {
        guard picker === self.picker, stream == nil else { return }
        selection.cancel()
    }

    public func contentSharingPicker(
        _ picker: SCContentSharingPicker,
        didUpdateWith filter: SCContentFilter,
        for stream: SCStream?
    ) {
        guard picker === self.picker,
              stream == nil,
              filter.contentRect.width.isFinite,
              filter.contentRect.height.isFinite,
              filter.pointPixelScale.isFinite,
              filter.contentRect.width > 0,
              filter.contentRect.height > 0,
              filter.pointPixelScale > 0
        else {
            selection.fail()
            return
        }
        guard #available(macOS 15.2, *) else {
            selection.fail()
            return
        }
        do {
            try selection.select(AustinBoundCaptureFilter(filter: filter))
        } catch {
            selection.fail()
        }
    }

    public func contentSharingPickerStartDidFailWithError(_ error: any Error) {
        selection.fail()
    }
}
