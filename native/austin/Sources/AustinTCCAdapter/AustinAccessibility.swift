import AppKit
import ApplicationServices
import AustinCore
import CryptoKit
import Foundation

public enum AustinAXSensitivity: String, Sendable {
    case normal
    case secure
    case authentication
    case payment
    case unknownInput = "unknown_input"
}

public enum AustinAXModalKind: String, Sendable {
    case none
    case application
    case system
    case authentication
    case payment
}

public struct AustinAXElementState: Equatable, Sendable {
    public let process: AustinDesktopProcess
    public let windowFingerprint: UInt64
    public let role: String
    public let subrole: String?
    public let enabled: Bool
    public let focusedWindow: Bool
    public let processRunning: Bool
    public let userSessionActive: Bool
    public let screenLocked: Bool
    public let environmentVerified: Bool
    public let modalKind: AustinAXModalKind
    public let sensitivity: AustinAXSensitivity
    public let valueFingerprint: String?

    public init(
        process: AustinDesktopProcess,
        windowFingerprint: UInt64,
        role: String,
        subrole: String? = nil,
        enabled: Bool,
        focusedWindow: Bool,
        processRunning: Bool = true,
        userSessionActive: Bool = true,
        screenLocked: Bool = false,
        environmentVerified: Bool = true,
        modalKind: AustinAXModalKind = .none,
        sensitivity: AustinAXSensitivity = .normal,
        valueFingerprint: String? = nil
    ) throws {
        guard windowFingerprint > 0,
              role.range(of: "^AX[A-Za-z0-9]{1,63}$", options: .regularExpression) != nil,
              subrole == nil || subrole?.range(
                  of: "^AX[A-Za-z0-9]{1,63}$",
                  options: .regularExpression
              ) != nil,
              valueFingerprint == nil || valueFingerprint?.range(
                  of: "^sha256:[0-9a-f]{64}$",
                  options: .regularExpression
              ) != nil
        else {
            throw AustinFailure("ax_state")
        }
        self.process = process
        self.windowFingerprint = windowFingerprint
        self.role = role
        self.subrole = subrole
        self.enabled = enabled
        self.focusedWindow = focusedWindow
        self.processRunning = processRunning
        self.userSessionActive = userSessionActive
        self.screenLocked = screenLocked
        self.environmentVerified = environmentVerified
        self.modalKind = modalKind
        self.sensitivity = sensitivity
        self.valueFingerprint = valueFingerprint
    }
}

public enum AustinAXBackendError: Error, Equatable, Sendable {
    case cannotComplete
    case apiDisabled
    case invalidElement
    case unsupported
    case internalFailure
}

public protocol AustinAccessibilityBackend: AnyObject {
    func currentState() throws -> AustinAXElementState
    func perform(
        operation: AustinOperation,
        arguments: [String: Any]
    ) -> AustinNativeEffect
    func postcondition(
        operation: AustinOperation,
        before: AustinAXElementState
    ) -> AustinNativePostcondition
}

public struct AustinAXBinding: Equatable, Sendable {
    public let target: AustinDesktopTargetBinding
    public let elementID: String

    public init(target: AustinDesktopTargetBinding, elementID: String) throws {
        guard elementID.range(
            of: "^hmac-sha256:[0-9a-f]{64}$",
            options: .regularExpression
        ) != nil else {
            throw AustinFailure("ax_element_id")
        }
        self.target = target
        self.elementID = elementID
    }
}

private final class AustinAXRecord: @unchecked Sendable {
    let binding: AustinAXBinding
    let operation: AustinOperation
    let initialState: AustinAXElementState
    let backend: AustinAccessibilityBackend
    var consumed = false

    init(
        binding: AustinAXBinding,
        operation: AustinOperation,
        initialState: AustinAXElementState,
        backend: AustinAccessibilityBackend
    ) {
        self.binding = binding
        self.operation = operation
        self.initialState = initialState
        self.backend = backend
    }
}

public final class AustinAccessibility: @unchecked Sendable {
    public static let maximumBindingLifetimeMilliseconds: Int64 = 5_000
    public static let maximumBindings = 128

    private let issuer: AustinOpaqueTokenIssuer
    private let lock = NSLock()
    private var generation: Int64 = 0
    private var records: [String: AustinAXRecord] = [:]

    public init(randomBytes: @escaping () throws -> Data = AustinSession.secureRandomBytes) throws {
        issuer = try AustinOpaqueTokenIssuer(randomBytes: randomBytes)
    }

    func bind(
        backend: AustinAccessibilityBackend,
        operation: AustinOperation,
        confirmation: AustinThomasConfirmationLease,
        preparationID: String,
        nowMilliseconds: Int64,
        lifetimeMilliseconds: Int64 = maximumBindingLifetimeMilliseconds
    ) throws -> AustinAXBinding {
        guard nowMilliseconds >= 0,
              lifetimeMilliseconds > 0,
              lifetimeMilliseconds <= Self.maximumBindingLifetimeMilliseconds,
              nowMilliseconds <= austinMaximumSafeInteger - lifetimeMilliseconds
        else {
            throw AustinFailure("ax_binding_time")
        }
        let confirmationAction: AustinConfirmationAction
        switch operation {
        case .activate:
            confirmationAction = .accessibilityActivate
        case .selectOption:
            confirmationAction = .accessibilitySelect
        case .scroll:
            confirmationAction = .accessibilityScroll
        case .observe, .inputText, .upload, .coordinateActivate, .handoff:
            throw AustinFailure("ax_operation_denied")
        }
        try confirmation.claim(
            action: confirmationAction,
            preparationID: preparationID,
            nowMilliseconds: nowMilliseconds
        )
        let state = try backend.currentState()
        guard state.processRunning,
              state.userSessionActive,
              !state.screenLocked,
              state.environmentVerified,
              state.focusedWindow,
              state.enabled
        else {
            throw AustinFailure("ax_target_unavailable")
        }

        lock.lock()
        records = records.filter {
            !$0.value.consumed && $0.value.binding.target.expiresAtMilliseconds > nowMilliseconds
        }
        guard records.count < Self.maximumBindings else {
            lock.unlock()
            throw AustinFailure("ax_binding_capacity")
        }
        guard generation < austinMaximumSafeInteger else {
            lock.unlock()
            throw AustinFailure("ax_binding_exhausted")
        }
        generation += 1
        let currentGeneration = generation
        lock.unlock()
        let processFields = [
            String(state.process.processIdentifier),
            String(state.process.processStartSeconds),
            String(state.process.processStartMicroseconds),
            state.process.bundleIdentifier,
            String(state.windowFingerprint),
            String(currentGeneration),
        ]
        let targetID = try issuer.issue(domain: "ax_target", fields: processFields)
        let elementID = try issuer.issue(
            domain: "ax_element",
            fields: [targetID, state.role, state.subrole ?? "none"]
        )
        let target = try AustinDesktopTargetBinding(
            targetID: targetID,
            targetEpoch: currentGeneration,
            targetRevision: "ax_\(currentGeneration)",
            fencingToken: currentGeneration,
            snapshotID: UUID().uuidString.lowercased(),
            snapshotSequence: 1,
            observedAtMilliseconds: nowMilliseconds,
            expiresAtMilliseconds: nowMilliseconds + lifetimeMilliseconds
        )
        let binding = try AustinAXBinding(target: target, elementID: elementID)
        let record = AustinAXRecord(
            binding: binding,
            operation: operation,
            initialState: state,
            backend: backend
        )
        lock.lock()
        records[elementID] = record
        lock.unlock()
        return binding
    }

    public func supports(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> Bool {
        guard envelope.route == .ax,
              [.activate, .inputText, .selectOption, .scroll].contains(envelope.operation),
              let elementID = envelope.arguments["element_id"] as? String
        else {
            return false
        }
        lock.lock()
        defer { lock.unlock() }
        guard let record = records[elementID] else { return false }
        return !record.consumed
            && record.operation == envelope.operation
            && record.binding.target.matches(envelope)
            && nowMilliseconds >= record.binding.target.observedAtMilliseconds
            && nowMilliseconds < record.binding.target.expiresAtMilliseconds
    }

    public func execute(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> AustinDesktopOutcome {
        guard envelope.route == .ax,
              let elementID = envelope.arguments["element_id"] as? String
        else {
            return AustinDesktopOutcome(.denied, "ax_request")
        }
        let record: AustinAXRecord
        lock.lock()
        if let existing = records[elementID], !existing.consumed {
            existing.consumed = true
            record = existing
        } else {
            lock.unlock()
            return AustinDesktopOutcome(.denied, "ax_element_stale")
        }
        lock.unlock()

        guard record.binding.target.matches(envelope) else {
            return AustinDesktopOutcome(.denied, "ax_target_changed")
        }
        guard record.operation == envelope.operation else {
            return AustinDesktopOutcome(.denied, "ax_operation_changed")
        }
        guard nowMilliseconds >= record.binding.target.observedAtMilliseconds,
              nowMilliseconds < record.binding.target.expiresAtMilliseconds
        else {
            return AustinDesktopOutcome(.denied, "ax_element_expired")
        }
        if envelope.operation == .inputText {
            return AustinDesktopOutcome(.handoffRequired, "ax_text_handoff")
        }

        let current: AustinAXElementState
        do {
            current = try record.backend.currentState()
        } catch let error as AustinAXBackendError {
            return mapPreflight(error)
        } catch {
            return AustinDesktopOutcome(.unknownOutcome, "ax_preflight_unknown")
        }
        if let rejection = validate(current: current, against: record.initialState) {
            return rejection
        }
        guard operationAllowed(envelope.operation, role: current.role) else {
            return AustinDesktopOutcome(.denied, "ax_role_operation_denied")
        }

        switch record.backend.perform(operation: envelope.operation, arguments: envelope.arguments) {
        case .rejected(let reason):
            return AustinDesktopOutcome(.denied, reason)
        case .uncertain(let reason):
            return AustinDesktopOutcome(.unknownOutcome, reason)
        case .performed:
            switch record.backend.postcondition(operation: envelope.operation, before: current) {
            case .verified:
                return AustinDesktopOutcome(.succeeded, "ax_postcondition_verified")
            case .notVerified, .unavailable:
                return AustinDesktopOutcome(.unknownOutcome, "ax_postcondition_unverified")
            }
        }
    }

    private func validate(
        current: AustinAXElementState,
        against initial: AustinAXElementState
    ) -> AustinDesktopOutcome? {
        guard current.processRunning,
              current.userSessionActive,
              !current.screenLocked,
              current.environmentVerified
        else {
            return AustinDesktopOutcome(.denied, "ax_target_unavailable")
        }
        guard current.process == initial.process else {
            return AustinDesktopOutcome(.denied, "ax_process_changed")
        }
        guard current.windowFingerprint == initial.windowFingerprint else {
            return AustinDesktopOutcome(.denied, "ax_window_changed")
        }
        guard current.focusedWindow else {
            return AustinDesktopOutcome(.denied, "ax_focus_changed")
        }
        guard current.role == initial.role,
              current.subrole == initial.subrole,
              current.enabled
        else {
            return AustinDesktopOutcome(.denied, "ax_element_changed")
        }
        if [.secure, .authentication, .payment, .unknownInput].contains(current.sensitivity) {
            return AustinDesktopOutcome(.handoffRequired, "ax_sensitive_handoff")
        }
        if [.system, .authentication, .payment].contains(current.modalKind) {
            return AustinDesktopOutcome(.handoffRequired, "ax_modal_handoff")
        }
        if current.modalKind != initial.modalKind {
            return AustinDesktopOutcome(.denied, "ax_modal_changed")
        }
        return nil
    }

    private func operationAllowed(_ operation: AustinOperation, role: String) -> Bool {
        switch operation {
        case .activate:
            ["AXButton", "AXCheckBox", "AXRadioButton", "AXMenuItem"].contains(role)
        case .selectOption:
            ["AXMenuItem", "AXRadioButton", "AXPopUpButton"].contains(role)
        case .scroll:
            ["AXScrollArea", "AXList", "AXOutline", "AXTable"].contains(role)
        case .observe, .inputText, .upload, .coordinateActivate, .handoff:
            false
        }
    }

    private func mapPreflight(_ error: AustinAXBackendError) -> AustinDesktopOutcome {
        switch error {
        case .cannotComplete:
            AustinDesktopOutcome(.unknownOutcome, "ax_cannot_complete")
        case .apiDisabled:
            AustinDesktopOutcome(.denied, "ax_permission_denied")
        case .invalidElement:
            AustinDesktopOutcome(.denied, "ax_element_stale")
        case .unsupported:
            AustinDesktopOutcome(.denied, "ax_unsupported")
        case .internalFailure:
            AustinDesktopOutcome(.unknownOutcome, "ax_internal_unknown")
        }
    }
}

public final class AustinSystemAccessibilityBackend: AustinAccessibilityBackend, @unchecked Sendable {
    private let application: AXUIElement
    private let element: AXUIElement
    private let window: AXUIElement
    private let sessionSafety: AustinSessionSafetyProviding

    private init(
        application: AXUIElement,
        element: AXUIElement,
        window: AXUIElement,
        sessionSafety: AustinSessionSafetyProviding
    ) {
        self.application = application
        self.element = element
        self.window = window
        self.sessionSafety = sessionSafety
    }

    public static func focusedElement(
        sessionSafety: AustinSessionSafetyProviding
    ) throws -> AustinSystemAccessibilityBackend {
        guard AXIsProcessTrusted() else { throw AustinAXBackendError.apiDisabled }
        let system = AXUIElementCreateSystemWide()
        let application = try copyElement(
            system,
            attribute: kAXFocusedApplicationAttribute as CFString
        )
        let element = try copyElement(system, attribute: kAXFocusedUIElementAttribute as CFString)
        let window = try copyElement(
            application,
            attribute: kAXFocusedWindowAttribute as CFString
        )
        return AustinSystemAccessibilityBackend(
            application: application,
            element: element,
            window: window,
            sessionSafety: sessionSafety
        )
    }

    public func currentState() throws -> AustinAXElementState {
        var pid: pid_t = 0
        guard AXUIElementGetPid(element, &pid) == .success, pid > 0 else {
            throw AustinAXBackendError.invalidElement
        }
        let start: (seconds: UInt64, microseconds: UInt64)
        do {
            start = try AustinPeerIdentity.processStartTime(processIdentifier: pid)
        } catch {
            throw AustinAXBackendError.invalidElement
        }
        guard let running = NSRunningApplication(processIdentifier: pid),
              let bundle = running.bundleIdentifier
        else {
            throw AustinAXBackendError.invalidElement
        }
        let process = try AustinDesktopProcess(
            processIdentifier: pid,
            processStartSeconds: start.seconds,
            processStartMicroseconds: start.microseconds,
            bundleIdentifier: bundle
        )
        let role = try Self.copyString(element, attribute: kAXRoleAttribute as CFString)
        let subrole = try? Self.copyString(element, attribute: kAXSubroleAttribute as CFString)
        let enabled = (
            try? Self.copyBoolean(element, attribute: kAXEnabledAttribute as CFString)
        ) ?? false
        let focusedWindow = try Self.currentFocusedWindowMatches(application: application, window: window)
        let modal = (
            try? Self.copyBoolean(window, attribute: kAXModalAttribute as CFString)
        ) == true
            ? AustinAXModalKind.application
            : AustinAXModalKind.none
        let sensitivity: AustinAXSensitivity
        if subrole == kAXSecureTextFieldSubrole as String {
            sensitivity = .secure
        } else if role == kAXTextFieldRole as String || role == kAXTextAreaRole as String {
            sensitivity = .unknownInput
        } else {
            sensitivity = .normal
        }
        let session = sessionSafety.state(nowMilliseconds: AustinClock.nowMilliseconds())
        return try AustinAXElementState(
            process: process,
            windowFingerprint: UInt64(bitPattern: Int64(CFHash(window))),
            role: role,
            subrole: subrole,
            enabled: enabled,
            focusedWindow: focusedWindow,
            processRunning: !running.isTerminated,
            userSessionActive: session.sessionActive,
            screenLocked: !session.userPresenceFresh,
            environmentVerified: session.environmentVerified,
            modalKind: modal,
            sensitivity: sensitivity,
            valueFingerprint: try Self.nonSensitiveValueFingerprint(element: element, role: role)
        )
    }

    public func perform(
        operation: AustinOperation,
        arguments: [String: Any]
    ) -> AustinNativeEffect {
        let action: CFString
        switch operation {
        case .activate, .selectOption:
            action = kAXPressAction as CFString
        case .scroll:
            guard let deltaY = (arguments["delta_y"] as? NSNumber)?.int64Value else {
                return .rejected("ax_scroll_arguments")
            }
            action = deltaY < 0 ? kAXDecrementAction as CFString : kAXIncrementAction as CFString
        case .observe, .inputText, .upload, .coordinateActivate, .handoff:
            return .rejected("ax_operation_denied")
        }
        return Self.effect(from: AXUIElementPerformAction(element, action))
    }

    public func postcondition(
        operation: AustinOperation,
        before: AustinAXElementState
    ) -> AustinNativePostcondition {
        guard let previous = before.valueFingerprint,
              let state = try? currentState(),
              let current = state.valueFingerprint,
              current != previous
        else {
            return .unavailable
        }
        return .verified
    }

    private static func copyElement(_ source: AXUIElement, attribute: CFString) throws -> AXUIElement {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(source, attribute, &value)
        guard result == .success,
              let value,
              CFGetTypeID(value) == AXUIElementGetTypeID()
        else {
            throw map(result)
        }
        return unsafeDowncast(value, to: AXUIElement.self)
    }

    private static func copyString(_ source: AXUIElement, attribute: CFString) throws -> String {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(source, attribute, &value)
        guard result == .success, let text = value as? String, !text.isEmpty else {
            throw map(result)
        }
        return text
    }

    private static func copyBoolean(_ source: AXUIElement, attribute: CFString) throws -> Bool {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(source, attribute, &value)
        guard result == .success, let number = value as? NSNumber else {
            throw map(result)
        }
        return number.boolValue
    }

    private static func currentFocusedWindowMatches(
        application: AXUIElement,
        window: AXUIElement
    ) throws -> Bool {
        let current = try copyElement(
            application,
            attribute: kAXFocusedWindowAttribute as CFString
        )
        return CFEqual(current, window)
    }

    private static func nonSensitiveValueFingerprint(
        element: AXUIElement,
        role: String
    ) throws -> String? {
        guard ["AXCheckBox", "AXRadioButton", "AXMenuItem"].contains(role) else {
            return nil
        }
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value)
        guard result == .success, let value else { return nil }
        let digest = SHA256.hash(data: Data(String(describing: value).utf8))
        return "sha256:" + digest.map { String(format: "%02x", $0) }.joined()
    }

    private static func effect(from error: AXError) -> AustinNativeEffect {
        switch error {
        case .success:
            .performed
        case .cannotComplete:
            .uncertain("ax_cannot_complete")
        case .apiDisabled:
            .rejected("ax_permission_denied")
        case .invalidUIElement:
            .rejected("ax_element_stale")
        case .actionUnsupported, .attributeUnsupported, .notImplemented:
            .rejected("ax_unsupported")
        default:
            .uncertain("ax_framework_unknown")
        }
    }

    private static func map(_ error: AXError) -> AustinAXBackendError {
        switch error {
        case .cannotComplete:
            .cannotComplete
        case .apiDisabled:
            .apiDisabled
        case .invalidUIElement:
            .invalidElement
        case .attributeUnsupported, .actionUnsupported, .notImplemented:
            .unsupported
        default:
            .internalFailure
        }
    }
}
