import AppKit
import AustinCore
import Foundation

public enum AustinConfirmationAction: String, CaseIterable, Sendable {
    case accessibilityActivate = "accessibility_activate"
    case accessibilitySelect = "accessibility_select"
    case accessibilityScroll = "accessibility_scroll"
    case coordinateActivate = "coordinate_activate"
    case persistentCapture = "persistent_capture"
    case appleEventActivate = "apple_event_activate"
    case shortcutReview = "shortcut_review"

    var fixedMessage: String {
        switch self {
        case .accessibilityActivate:
            "Allow one activation of the currently selected control?"
        case .accessibilitySelect:
            "Allow one selection in the currently focused control?"
        case .accessibilityScroll:
            "Allow one scroll action in the currently focused control?"
        case .coordinateActivate:
            "Allow one click at the reviewed on-screen coordinate?"
        case .persistentCapture:
            "Allow one locally redacted screen capture?"
        case .appleEventActivate:
            "Allow one reviewed application activation event?"
        case .shortcutReview:
            "Open the fixed shortcut in Shortcuts for manual review?"
        }
    }
}

public struct AustinSessionSafetyState: Equatable, Sendable {
    public let sessionActive: Bool
    public let screensAwake: Bool
    public let userPresenceFresh: Bool
    public let environmentVerified: Bool
    public let generation: Int64
}

public protocol AustinSessionSafetyProviding: AnyObject {
    func state(nowMilliseconds: Int64) -> AustinSessionSafetyState
}

/// Public-API session safety. Switch-away and screen-sleep notifications
/// invalidate user presence. Switch-back and wake only restore eligibility;
/// they never recreate the short confirmation lease.
public final class AustinSessionSafetyOracle: AustinSessionSafetyProviding,
    @unchecked Sendable
{
    public static let maximumPresenceLifetimeMilliseconds: Int64 = 1_000

    private let notificationCenter: NotificationCenter
    private let lock = NSLock()
    private var observers: [NSObjectProtocol] = []
    private var sessionActive = false
    private var screensAwake = false
    private var confirmedAtMilliseconds: Int64?
    private var generation: Int64 = 0

    public init(notificationCenter: NotificationCenter = NSWorkspace.shared.notificationCenter) {
        self.notificationCenter = notificationCenter
        observers = [
            notificationCenter.addObserver(
                forName: NSWorkspace.sessionDidResignActiveNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                self?.setSession(active: false)
            },
            notificationCenter.addObserver(
                forName: NSWorkspace.sessionDidBecomeActiveNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                self?.setSession(active: true)
            },
            notificationCenter.addObserver(
                forName: NSWorkspace.screensDidSleepNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                self?.setScreens(awake: false)
            },
            notificationCenter.addObserver(
                forName: NSWorkspace.screensDidWakeNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                self?.setScreens(awake: true)
            },
        ]
    }

    deinit {
        for observer in observers {
            notificationCenter.removeObserver(observer)
        }
    }

    func recordConfirmedUserPresence(nowMilliseconds: Int64) throws {
        lock.lock()
        defer { lock.unlock() }
        guard nowMilliseconds >= 0,
              nowMilliseconds <= austinMaximumSafeInteger,
              generation < austinMaximumSafeInteger
        else {
            throw AustinFailure("confirmation_time")
        }
        if let previous = confirmedAtMilliseconds, nowMilliseconds < previous {
            throw AustinFailure("confirmation_clock_rollback")
        }
        generation += 1
        sessionActive = true
        screensAwake = true
        confirmedAtMilliseconds = nowMilliseconds
    }

    public func state(nowMilliseconds: Int64) -> AustinSessionSafetyState {
        lock.lock()
        defer { lock.unlock() }
        let fresh: Bool
        if let confirmed = confirmedAtMilliseconds,
           nowMilliseconds >= confirmed,
           nowMilliseconds - confirmed <= Self.maximumPresenceLifetimeMilliseconds
        {
            fresh = true
        } else {
            fresh = false
        }
        return AustinSessionSafetyState(
            sessionActive: sessionActive,
            screensAwake: screensAwake,
            userPresenceFresh: fresh,
            environmentVerified: sessionActive && screensAwake && fresh,
            generation: generation
        )
    }

    private func setSession(active: Bool) {
        lock.lock()
        sessionActive = active
        if !active { confirmedAtMilliseconds = nil }
        lock.unlock()
    }

    private func setScreens(awake: Bool) {
        lock.lock()
        screensAwake = awake
        if !awake { confirmedAtMilliseconds = nil }
        lock.unlock()
    }
}

public enum AustinConfirmationResult: Equatable, Sendable {
    case confirmed
    case denied
    case timedOut
    case unavailable
}

/// Module-confined proof that the fixed native confirmation UI returned
/// `Allow Once` for one exact preparation. Adapter bind APIs consume this
/// object atomically; they no longer accept caller-supplied confirmation
/// booleans.
final class AustinThomasConfirmationLease: @unchecked Sendable {
    static let maximumLifetimeMilliseconds: Int64 = 1_000

    let preparationID: String
    let action: AustinConfirmationAction
    private let issuedAtMilliseconds: Int64
    private let expiresAtMilliseconds: Int64
    private let lock = NSLock()
    private var consumed = false

    init(
        preparationID: String,
        action: AustinConfirmationAction,
        issuedAtMilliseconds: Int64
    ) throws {
        guard preparationID.range(
            of: "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            options: .regularExpression
        ) != nil,
        let parsed = UUID(uuidString: preparationID),
        parsed.uuidString.lowercased() == preparationID,
        issuedAtMilliseconds >= 0,
        issuedAtMilliseconds
            <= austinMaximumSafeInteger - Self.maximumLifetimeMilliseconds
        else {
            throw AustinFailure("confirmation_lease")
        }
        self.preparationID = preparationID
        self.action = action
        self.issuedAtMilliseconds = issuedAtMilliseconds
        expiresAtMilliseconds = issuedAtMilliseconds + Self.maximumLifetimeMilliseconds
    }

    func claim(
        action expectedAction: AustinConfirmationAction,
        preparationID expectedPreparationID: String,
        nowMilliseconds: Int64
    ) throws {
        lock.lock()
        defer { lock.unlock() }
        guard !consumed else { throw AustinFailure("confirmation_lease_replay") }
        guard action == expectedAction,
              preparationID == expectedPreparationID
        else {
            throw AustinFailure("confirmation_lease_scope")
        }
        guard nowMilliseconds >= issuedAtMilliseconds,
              nowMilliseconds <= expiresAtMilliseconds
        else {
            throw AustinFailure("confirmation_lease_expired")
        }
        consumed = true
    }
}

public protocol AustinConfirmationBackend: AnyObject {
    @MainActor
    func confirm(
        action: AustinConfirmationAction,
        timeoutMilliseconds: Int64
    ) -> AustinConfirmationResult
}

@MainActor
private final class AustinConfirmationTimeoutTarget: NSObject {
    private let alert: NSAlert
    private let state: AustinConfirmationTimeoutState

    init(alert: NSAlert, state: AustinConfirmationTimeoutState) {
        self.alert = alert
        self.state = state
    }

    @objc func fire() {
        state.markTimedOut()
        NSApplication.shared.abortModal()
        alert.window.orderOut(nil)
    }
}

private final class AustinConfirmationTimeoutState: @unchecked Sendable {
    private let lock = NSLock()
    private var timedOut = false

    func markTimedOut() {
        lock.lock()
        timedOut = true
        lock.unlock()
    }

    func didTimeOut() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return timedOut
    }
}

/// Main-thread, fixed-copy, bounded native confirmation. No model text,
/// application name, path, selector, content, or mutable action description is
/// displayed. Confirmation records a short user-presence lease only after the
/// Allow Once button returns.
@MainActor
public final class AustinSystemConfirmationBackend: AustinConfirmationBackend,
    @unchecked Sendable
{
    public static let maximumTimeoutMilliseconds: Int64 = 30_000

    private let sessionSafety: AustinSessionSafetyOracle
    private let nowMilliseconds: @Sendable () -> Int64
    private var presenting = false

    public init(
        sessionSafety: AustinSessionSafetyOracle,
        nowMilliseconds: @escaping @Sendable () -> Int64 = AustinClock.nowMilliseconds
    ) {
        self.sessionSafety = sessionSafety
        self.nowMilliseconds = nowMilliseconds
    }

    public func confirm(
        action: AustinConfirmationAction,
        timeoutMilliseconds: Int64
    ) -> AustinConfirmationResult {
        guard timeoutMilliseconds > 0,
              timeoutMilliseconds <= Self.maximumTimeoutMilliseconds,
              !presenting
        else {
            return .unavailable
        }
        presenting = true
        defer { presenting = false }
        let application = NSApplication.shared
        application.activate()
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Algo CLI computer-use confirmation"
        alert.informativeText = action.fixedMessage
        alert.showsSuppressionButton = false
        alert.addButton(withTitle: "Allow Once")
        alert.addButton(withTitle: "Cancel")

        let timeoutState = AustinConfirmationTimeoutState()
        let timeoutTarget = AustinConfirmationTimeoutTarget(alert: alert, state: timeoutState)
        let timer = Timer(
            timeInterval: TimeInterval(timeoutMilliseconds) / 1_000,
            target: timeoutTarget,
            selector: #selector(AustinConfirmationTimeoutTarget.fire),
            userInfo: nil,
            repeats: false
        )
        RunLoop.main.add(timer, forMode: .default)
        RunLoop.main.add(timer, forMode: .modalPanel)
        let response = alert.runModal()
        timer.invalidate()
        if timeoutState.didTimeOut() {
            return .timedOut
        }
        guard response == .alertFirstButtonReturn else {
            return .denied
        }
        do {
            try sessionSafety.recordConfirmedUserPresence(
                nowMilliseconds: nowMilliseconds()
            )
            return .confirmed
        } catch {
            return .unavailable
        }
    }
}
