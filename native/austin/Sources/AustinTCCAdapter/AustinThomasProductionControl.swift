import AustinCore
import Foundation

/// Exact, code-sealed activation policy for the native control process. A
/// missing resource is the disabled foundation. A present resource must be
/// canonical and may enable only routes whose concrete system adapters are
/// fully assembled below. Screen capture is intentionally absent until its
/// classifier, OS-backed key, exact filter binding, and artifact-consumer
/// contracts are complete.
public struct AustinThomasControlActivation: Equatable, Sendable {
    public static let productionRoutes: [AustinRoute] = [
        .appleEvent,
        .ax,
        .coordinate,
        .shortcut,
    ]

    public let enabledRoutes: [AustinRoute]

    public var controlProtocolEnabled: Bool { !enabledRoutes.isEmpty }

    public static func decode(_ payload: Data?) throws -> AustinThomasControlActivation {
        guard let payload else {
            return AustinThomasControlActivation(enabledRoutes: [])
        }
        let root = try AustinJSON.decodeCanonicalObject(payload)
        let mode = try AustinJSON.string(
            root["mode"],
            label: "native_activation_mode",
            maximumBytes: 16
        )
        _ = try AustinJSON.integer(
            root["schema_version"],
            label: "native_activation_version",
            minimum: 1,
            maximum: 1
        )
        switch mode {
        case "disabled":
            _ = try AustinJSON.exactObject(
                root,
                keys: ["mode", "schema_version"],
                label: "native_activation"
            )
            return AustinThomasControlActivation(enabledRoutes: [])
        case "enabled":
            _ = try AustinJSON.exactObject(
                root,
                keys: ["enabled_routes", "mode", "schema_version"],
                label: "native_activation"
            )
            let routeNames = try AustinJSON.strings(
                root["enabled_routes"],
                label: "native_activation_routes",
                maximumCount: Self.productionRoutes.count
            )
            let allowed = Set(Self.productionRoutes.map(\.rawValue))
            guard routeNames == routeNames.sorted(),
                  routeNames.allSatisfy(allowed.contains)
            else {
                throw AustinFailure("native_activation_routes")
            }
            let routes = try routeNames.map { name in
                guard let route = AustinRoute(rawValue: name) else {
                    throw AustinFailure("native_activation_routes")
                }
                return route
            }
            return AustinThomasControlActivation(enabledRoutes: routes)
        default:
            throw AustinFailure("native_activation_mode")
        }
    }

    public func enables(_ route: AustinRoute) -> Bool {
        enabledRoutes.contains(route)
    }
}

/// Owns one production dispatcher/coordinator pair. Construction performs no
/// TCC request and no native action. Permission discovery remains preflight
/// only; every admitted action still needs Samuel preparation, fixed native
/// confirmation, a target-bound permit, and the one-use dispatcher claim.
public struct AustinThomasProductionControl: Sendable {
    public let dispatcher: AustinDesktopDispatcher
    public let coordinator: AustinThomasBindingCoordinator?
    public let enabledRoutes: [AustinRoute]

    public var controlProtocolEnabled: Bool { coordinator != nil && !enabledRoutes.isEmpty }

    @MainActor
    public static func system(
        activationPayload: Data?
    ) throws -> AustinThomasProductionControl {
        let activation = try AustinThomasControlActivation.decode(activationPayload)
        guard activation.controlProtocolEnabled else {
            return AustinThomasProductionControl(
                dispatcher: .disabledFoundation(),
                coordinator: nil,
                enabledRoutes: []
            )
        }

        let sessionSafety = AustinSessionSafetyOracle()
        let confirmation = AustinSystemConfirmationBackend(sessionSafety: sessionSafety)

        let accessibility: AustinAccessibility?
        let accessibilityDiscovery: AustinThomasBindingCoordinator.AccessibilityDiscovery?
        if activation.enables(.ax) {
            accessibility = try AustinAccessibility()
            accessibilityDiscovery = {
                try AustinSystemAccessibilityBackend.focusedElement(
                    sessionSafety: sessionSafety
                )
            }
        } else {
            accessibility = nil
            accessibilityDiscovery = nil
        }

        let appleEvent: AustinAppleEvent?
        if activation.enables(.appleEvent) {
            appleEvent = try AustinAppleEvent(backend: AustinSystemAppleEventBackend())
        } else {
            appleEvent = nil
        }

        let shortcut: AustinShortcut?
        if activation.enables(.shortcut) {
            shortcut = try AustinShortcut(backend: AustinSystemShortcutBackend())
        } else {
            shortcut = nil
        }

        let cgEvent: AustinCGEvent?
        if activation.enables(.coordinate) {
            cgEvent = AustinCGEvent(
                backend: AustinSystemCGEventBackend(sessionSafety: sessionSafety)
            )
        } else {
            cgEvent = nil
        }

        let coordinator = try AustinThomasBindingCoordinator(
            confirmation: confirmation,
            accessibility: accessibility,
            accessibilityDiscovery: accessibilityDiscovery,
            appleEvent: appleEvent,
            shortcut: shortcut,
            cgEvent: cgEvent
        )
        let dispatcher = AustinDesktopDispatcher(
            accessibility: accessibility,
            appleEvent: appleEvent,
            shortcut: shortcut,
            cgEvent: cgEvent,
            preparationCoordinator: coordinator
        )
        return AustinThomasProductionControl(
            dispatcher: dispatcher,
            coordinator: coordinator,
            enabledRoutes: activation.enabledRoutes
        )
    }
}
