import AppKit
import AustinCore
import Foundation

/// A finite set of review-only Shortcuts handoffs. These cases open the
/// Shortcuts editor; they never use `run-shortcut`, the `shortcuts` process,
/// clipboard input, caller text, or output files.
public enum AustinReviewedShortcut: String, CaseIterable, Sendable {
    case reviewCurrentTask = "review_current_task"

    public var shortcutName: String {
        switch self {
        case .reviewCurrentTask:
            "Algo CLI Review Current Task"
        }
    }

    public var reviewURL: URL? {
        var components = URLComponents()
        components.scheme = "shortcuts"
        components.host = "open-shortcut"
        components.queryItems = [URLQueryItem(name: "name", value: shortcutName)]
        return components.url
    }
}

public enum AustinShortcutBackendResult: Equatable, Sendable {
    case openedForReview
    case unavailable
    case uncertain
}

public protocol AustinShortcutBackend: AnyObject {
    func openForReview(_ adapter: AustinReviewedShortcut) -> AustinShortcutBackendResult
}

public struct AustinShortcutBinding: Equatable, Sendable {
    public let target: AustinDesktopTargetBinding
    public let elementID: String
    public let adapter: AustinReviewedShortcut

    public init(
        target: AustinDesktopTargetBinding,
        elementID: String,
        adapter: AustinReviewedShortcut
    ) throws {
        guard elementID.range(
            of: "^hmac-sha256:[0-9a-f]{64}$",
            options: .regularExpression
        ) != nil else {
            throw AustinFailure("shortcut_element")
        }
        self.target = target
        self.elementID = elementID
        self.adapter = adapter
    }
}

private final class AustinShortcutRecord: @unchecked Sendable {
    let binding: AustinShortcutBinding
    var consumed = false

    init(binding: AustinShortcutBinding) {
        self.binding = binding
    }
}

public final class AustinShortcut: @unchecked Sendable {
    public static let maximumBindingLifetimeMilliseconds: Int64 = 5_000
    public static let maximumBindings = 64

    private let backend: AustinShortcutBackend
    private let issuer: AustinOpaqueTokenIssuer
    private let lock = NSLock()
    private var records: [String: AustinShortcutRecord] = [:]

    public init(
        backend: AustinShortcutBackend,
        randomBytes: @escaping () throws -> Data = AustinSession.secureRandomBytes
    ) throws {
        self.backend = backend
        issuer = try AustinOpaqueTokenIssuer(randomBytes: randomBytes)
    }

    func bind(
        target: AustinDesktopTargetBinding,
        adapter: AustinReviewedShortcut,
        confirmation: AustinThomasConfirmationLease,
        preparationID: String,
        nowMilliseconds: Int64
    ) throws -> AustinShortcutBinding {
        try confirmation.claim(
            action: .shortcutReview,
            preparationID: preparationID,
            nowMilliseconds: nowMilliseconds
        )
        guard nowMilliseconds >= target.observedAtMilliseconds,
              nowMilliseconds < target.expiresAtMilliseconds,
              target.expiresAtMilliseconds - nowMilliseconds
                <= Self.maximumBindingLifetimeMilliseconds
        else {
            throw AustinFailure("shortcut_binding_time")
        }
        let elementID = try issuer.issue(
            domain: "shortcut",
            fields: [target.targetID, adapter.rawValue, adapter.shortcutName]
        )
        let binding = try AustinShortcutBinding(
            target: target,
            elementID: elementID,
            adapter: adapter
        )
        lock.lock()
        records = records.filter {
            !$0.value.consumed && $0.value.binding.target.expiresAtMilliseconds > nowMilliseconds
        }
        guard records.count < Self.maximumBindings else {
            lock.unlock()
            throw AustinFailure("shortcut_capacity")
        }
        records[elementID] = AustinShortcutRecord(binding: binding)
        lock.unlock()
        return binding
    }

    public func supports(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> Bool {
        guard envelope.route == .shortcut,
              envelope.operation == .activate,
              let elementID = envelope.arguments["element_id"] as? String
        else {
            return false
        }
        lock.lock()
        defer { lock.unlock() }
        guard let record = records[elementID] else { return false }
        return !record.consumed
            && record.binding.target.matches(envelope)
            && nowMilliseconds >= record.binding.target.observedAtMilliseconds
            && nowMilliseconds < record.binding.target.expiresAtMilliseconds
    }

    public func execute(
        _ envelope: AustinVerifiedEnvelope,
        nowMilliseconds: Int64
    ) -> AustinDesktopOutcome {
        guard envelope.route == .shortcut,
              envelope.operation == .activate,
              let elementID = envelope.arguments["element_id"] as? String
        else {
            return AustinDesktopOutcome(.denied, "shortcut_request")
        }
        let record: AustinShortcutRecord
        lock.lock()
        if let existing = records[elementID], !existing.consumed {
            existing.consumed = true
            record = existing
        } else {
            lock.unlock()
            return AustinDesktopOutcome(.denied, "shortcut_stale")
        }
        lock.unlock()
        guard record.binding.target.matches(envelope) else {
            return AustinDesktopOutcome(.denied, "shortcut_target_changed")
        }
        guard nowMilliseconds >= record.binding.target.observedAtMilliseconds,
              nowMilliseconds < record.binding.target.expiresAtMilliseconds
        else {
            return AustinDesktopOutcome(.denied, "shortcut_expired")
        }
        switch backend.openForReview(record.binding.adapter) {
        case .openedForReview:
            return AustinDesktopOutcome(.handoffRequired, "shortcut_review_handoff")
        case .unavailable:
            return AustinDesktopOutcome(.denied, "shortcut_unavailable")
        case .uncertain:
            return AustinDesktopOutcome(.unknownOutcome, "shortcut_open_unknown")
        }
    }
}

public final class AustinSystemShortcutBackend: AustinShortcutBackend, @unchecked Sendable {
    public init() {}

    public func openForReview(_ adapter: AustinReviewedShortcut) -> AustinShortcutBackendResult {
        guard let url = adapter.reviewURL else { return .unavailable }
        return NSWorkspace.shared.open(url) ? .openedForReview : .unavailable
    }
}
