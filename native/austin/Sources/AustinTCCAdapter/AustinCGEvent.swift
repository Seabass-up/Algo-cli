import AppKit
import AustinCore
import CoreGraphics
import Foundation

public struct AustinCoordinateContext: Equatable, Sendable {
    public let process: AustinDesktopProcess
    public let displayIdentifier: UInt32
    public let logicalWidth: Int
    public let logicalHeight: Int
    public let pixelWidth: Int
    public let pixelHeight: Int
    public let scaleMilli: Int
    public let singleDisplay: Bool
    public let userSessionActive: Bool
    public let screenLocked: Bool
    public let environmentVerified: Bool

    public init(
        process: AustinDesktopProcess,
        displayIdentifier: UInt32,
        logicalWidth: Int,
        logicalHeight: Int,
        pixelWidth: Int,
        pixelHeight: Int,
        scaleMilli: Int,
        singleDisplay: Bool = true,
        userSessionActive: Bool = true,
        screenLocked: Bool = false,
        environmentVerified: Bool = true
    ) throws {
        guard displayIdentifier > 0,
              logicalWidth > 0, logicalWidth <= 16_384,
              logicalHeight > 0, logicalHeight <= 16_384,
              pixelWidth > 0, pixelWidth <= 32_768,
              pixelHeight > 0, pixelHeight <= 32_768,
              scaleMilli >= 500, scaleMilli <= 4_000
        else {
            throw AustinFailure("coordinate_context")
        }
        self.process = process
        self.displayIdentifier = displayIdentifier
        self.logicalWidth = logicalWidth
        self.logicalHeight = logicalHeight
        self.pixelWidth = pixelWidth
        self.pixelHeight = pixelHeight
        self.scaleMilli = scaleMilli
        self.singleDisplay = singleDisplay
        self.userSessionActive = userSessionActive
        self.screenLocked = screenLocked
        self.environmentVerified = environmentVerified
    }
}

public protocol AustinCGEventBackend: AnyObject {
    func currentContext() throws -> AustinCoordinateContext
    func hasPostEventPermission() -> Bool
    func postClick(x: Int, y: Int) -> AustinNativeEffect
    func postcondition() -> AustinNativePostcondition
}

public struct AustinCoordinateBinding: Equatable, Sendable {
    public let target: AustinDesktopTargetBinding
    public let context: AustinCoordinateContext
    public let x: Int
    public let y: Int

    public init(
        target: AustinDesktopTargetBinding,
        context: AustinCoordinateContext,
        x: Int,
        y: Int
    ) throws {
        guard x >= 0, x < context.logicalWidth,
              y >= 0, y < context.logicalHeight
        else {
            throw AustinFailure("coordinate_point")
        }
        self.target = target
        self.context = context
        self.x = x
        self.y = y
    }
}

private final class AustinCoordinateRecord: @unchecked Sendable {
    let binding: AustinCoordinateBinding
    var consumed = false

    init(binding: AustinCoordinateBinding) {
        self.binding = binding
    }
}

public final class AustinCGEvent: @unchecked Sendable {
    public static let maximumBindingLifetimeMilliseconds: Int64 = 2_000
    public static let maximumBindings = 64

    private let backend: AustinCGEventBackend
    private let lock = NSLock()
    private var records: [String: AustinCoordinateRecord] = [:]

    public init(backend: AustinCGEventBackend) {
        self.backend = backend
    }

    func bind(
        target: AustinDesktopTargetBinding,
        x: Int,
        y: Int,
        confirmation: AustinThomasConfirmationLease,
        preparationID: String,
        nowMilliseconds: Int64
    ) throws -> AustinCoordinateBinding {
        try confirmation.claim(
            action: .coordinateActivate,
            preparationID: preparationID,
            nowMilliseconds: nowMilliseconds
        )
        guard nowMilliseconds >= target.observedAtMilliseconds,
              nowMilliseconds < target.expiresAtMilliseconds,
              target.expiresAtMilliseconds - nowMilliseconds
                <= Self.maximumBindingLifetimeMilliseconds
        else {
            throw AustinFailure("coordinate_binding_time")
        }
        let context = try backend.currentContext()
        guard context.singleDisplay,
              context.userSessionActive,
              !context.screenLocked,
              context.environmentVerified
        else {
            throw AustinFailure("coordinate_environment_handoff")
        }
        let binding = try AustinCoordinateBinding(
            target: target,
            context: context,
            x: x,
            y: y
        )
        lock.lock()
        records = records.filter {
            !$0.value.consumed && $0.value.binding.target.expiresAtMilliseconds > nowMilliseconds
        }
        guard records[target.targetID] == nil else {
            lock.unlock()
            throw AustinFailure("coordinate_binding_conflict")
        }
        guard records.count < Self.maximumBindings else {
            lock.unlock()
            throw AustinFailure("coordinate_binding_capacity")
        }
        records[target.targetID] = AustinCoordinateRecord(binding: binding)
        lock.unlock()
        return binding
    }

    public func supports(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> Bool {
        guard envelope.route == .coordinate,
              envelope.operation == .coordinateActivate
        else {
            return false
        }
        lock.lock()
        defer { lock.unlock() }
        guard let record = records[envelope.targetID],
              let x = (envelope.arguments["x"] as? NSNumber)?.intValue,
              let y = (envelope.arguments["y"] as? NSNumber)?.intValue,
              let width = (envelope.arguments["viewport_width"] as? NSNumber)?.intValue,
              let height = (envelope.arguments["viewport_height"] as? NSNumber)?.intValue
        else {
            return false
        }
        return !record.consumed
            && record.binding.target.matches(envelope)
            && nowMilliseconds >= record.binding.target.observedAtMilliseconds
            && nowMilliseconds < record.binding.target.expiresAtMilliseconds
            && x == record.binding.x
            && y == record.binding.y
            && width == record.binding.context.logicalWidth
            && height == record.binding.context.logicalHeight
    }

    public func execute(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> AustinDesktopOutcome {
        guard envelope.route == .coordinate,
              envelope.operation == .coordinateActivate,
              let x = (envelope.arguments["x"] as? NSNumber)?.intValue,
              let y = (envelope.arguments["y"] as? NSNumber)?.intValue,
              let width = (envelope.arguments["viewport_width"] as? NSNumber)?.intValue,
              let height = (envelope.arguments["viewport_height"] as? NSNumber)?.intValue
        else {
            return AustinDesktopOutcome(.denied, "coordinate_request")
        }
        let record: AustinCoordinateRecord
        lock.lock()
        if let existing = records[envelope.targetID], !existing.consumed {
            existing.consumed = true
            record = existing
        } else {
            lock.unlock()
            return AustinDesktopOutcome(.denied, "coordinate_binding_stale")
        }
        lock.unlock()
        guard record.binding.target.matches(envelope) else {
            return AustinDesktopOutcome(.denied, "coordinate_target_changed")
        }
        guard nowMilliseconds >= record.binding.target.observedAtMilliseconds,
              nowMilliseconds < record.binding.target.expiresAtMilliseconds
        else {
            return AustinDesktopOutcome(.denied, "coordinate_binding_expired")
        }
        guard x == record.binding.x,
              y == record.binding.y,
              width == record.binding.context.logicalWidth,
              height == record.binding.context.logicalHeight
        else {
            return AustinDesktopOutcome(.denied, "coordinate_geometry_changed")
        }

        let current: AustinCoordinateContext
        do {
            current = try backend.currentContext()
        } catch {
            return AustinDesktopOutcome(.denied, "coordinate_environment_unavailable")
        }
        guard current == record.binding.context else {
            return AustinDesktopOutcome(.denied, "coordinate_context_changed")
        }
        guard current.singleDisplay,
              current.userSessionActive,
              !current.screenLocked,
              current.environmentVerified
        else {
            return AustinDesktopOutcome(.handoffRequired, "coordinate_environment_handoff")
        }
        guard backend.hasPostEventPermission() else {
            return AustinDesktopOutcome(.denied, "coordinate_permission_denied")
        }

        switch backend.postClick(x: x, y: y) {
        case .rejected(let reason):
            return AustinDesktopOutcome(.denied, reason)
        case .uncertain(let reason):
            return AustinDesktopOutcome(.unknownOutcome, reason)
        case .performed:
            switch backend.postcondition() {
            case .verified:
                return AustinDesktopOutcome(.succeeded, "coordinate_postcondition_verified")
            case .notVerified, .unavailable:
                return AustinDesktopOutcome(.unknownOutcome, "coordinate_postcondition_unverified")
            }
        }
    }
}

public final class AustinSystemCGEventBackend: AustinCGEventBackend, @unchecked Sendable {
    private let sessionSafety: AustinSessionSafetyProviding

    public init(sessionSafety: AustinSessionSafetyProviding) {
        self.sessionSafety = sessionSafety
    }

    public func currentContext() throws -> AustinCoordinateContext {
        guard let frontmost = NSWorkspace.shared.frontmostApplication,
              let bundle = frontmost.bundleIdentifier
        else {
            throw AustinFailure("coordinate_frontmost")
        }
        let start = try AustinPeerIdentity.processStartTime(
            processIdentifier: frontmost.processIdentifier
        )
        let process = try AustinDesktopProcess(
            processIdentifier: frontmost.processIdentifier,
            processStartSeconds: start.seconds,
            processStartMicroseconds: start.microseconds,
            bundleIdentifier: bundle
        )
        guard let main = NSScreen.main else { throw AustinFailure("coordinate_display") }
        let display = CGMainDisplayID()
        let session = sessionSafety.state(nowMilliseconds: AustinClock.nowMilliseconds())
        return try AustinCoordinateContext(
            process: process,
            displayIdentifier: display,
            logicalWidth: Int(main.frame.width.rounded()),
            logicalHeight: Int(main.frame.height.rounded()),
            pixelWidth: CGDisplayPixelsWide(display),
            pixelHeight: CGDisplayPixelsHigh(display),
            scaleMilli: Int((main.backingScaleFactor * 1_000).rounded()),
            singleDisplay: NSScreen.screens.count == 1,
            userSessionActive: session.sessionActive,
            screenLocked: !session.userPresenceFresh,
            environmentVerified: session.environmentVerified
        )
    }

    public func hasPostEventPermission() -> Bool {
        // Preflight only. This component never prompts or requests permission silently.
        CGPreflightPostEventAccess()
    }

    public func postClick(x: Int, y: Int) -> AustinNativeEffect {
        let point = CGPoint(x: x, y: y)
        guard let down = CGEvent(
            mouseEventSource: nil,
            mouseType: .leftMouseDown,
            mouseCursorPosition: point,
            mouseButton: .left
        ),
        let up = CGEvent(
            mouseEventSource: nil,
            mouseType: .leftMouseUp,
            mouseCursorPosition: point,
            mouseButton: .left
        ) else {
            return .rejected("coordinate_event_create")
        }
        // Exactly two posting calls. There is no event tap, listener, keyboard,
        // Unicode, layout, or IME fallback in this adapter.
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
        return .performed
    }

    public func postcondition() -> AustinNativePostcondition {
        // A generic click has no reliable semantic postcondition. The caller
        // must reconcile through a fresh AX observation before it may succeed.
        .unavailable
    }
}
