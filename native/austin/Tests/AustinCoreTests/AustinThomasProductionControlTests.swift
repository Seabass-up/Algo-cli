import Foundation
import Testing
@testable import AustinCore
@testable import AustinDesktopCore

private func austinActivation(_ text: String) -> Data {
    Data(text.utf8)
}

@Test @MainActor func productionControlIsDisabledWithoutAnExactSealedActivation() throws {
    let missing = try AustinThomasProductionControl.system(activationPayload: nil)
    #expect(!missing.controlProtocolEnabled)
    #expect(missing.coordinator == nil)
    #expect(missing.enabledRoutes.isEmpty)

    let disabled = try AustinThomasProductionControl.system(
        activationPayload: austinActivation(
            "{\"mode\":\"disabled\",\"schema_version\":1}"
        )
    )
    #expect(!disabled.controlProtocolEnabled)
    #expect(disabled.coordinator == nil)
    #expect(disabled.enabledRoutes.isEmpty)
}

@Test @MainActor func sealedActivationAssemblesOnlyReviewedProductionRoutes() throws {
    let payload = austinActivation(
        "{\"enabled_routes\":[\"apple_event\",\"ax\",\"coordinate\",\"shortcut\"],"
            + "\"mode\":\"enabled\",\"schema_version\":1}"
    )
    let activation = try AustinThomasControlActivation.decode(payload)
    #expect(activation.controlProtocolEnabled)
    #expect(activation.enabledRoutes == [.appleEvent, .ax, .coordinate, .shortcut])
    #expect(!activation.enables(.screenshot))

    let control = try AustinThomasProductionControl.system(activationPayload: payload)
    #expect(control.controlProtocolEnabled)
    #expect(control.coordinator != nil)
    #expect(control.enabledRoutes == [.appleEvent, .ax, .coordinate, .shortcut])
}

@Test func activationRejectsCaptureDuplicatesUnsortedAndNoncanonicalPolicies() {
    let rejected = [
        "{\"enabled_routes\":[\"screenshot\"],\"mode\":\"enabled\",\"schema_version\":1}",
        "{\"enabled_routes\":[\"ax\",\"ax\"],\"mode\":\"enabled\",\"schema_version\":1}",
        "{\"enabled_routes\":[\"shortcut\",\"ax\"],\"mode\":\"enabled\",\"schema_version\":1}",
        "{\"mode\":\"enabled\",\"schema_version\":1,\"enabled_routes\":[\"ax\"]}",
        "{\"extra\":false,\"mode\":\"disabled\",\"schema_version\":1}",
        "{\"enabled_routes\":[],\"mode\":\"enabled\",\"schema_version\":1}",
    ]
    for policy in rejected {
        #expect(throws: AustinFailure.self) {
            try AustinThomasControlActivation.decode(austinActivation(policy))
        }
    }
}
