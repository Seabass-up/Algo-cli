@testable import AustinCore
@testable import AustinDesktopCore
import AppKit
import Foundation
import Testing

@Test func austinSessionSafetyRequiresFreshPresenceAndInvalidatesOnSystemEvents() throws {
    let center = NotificationCenter()
    let oracle = AustinSessionSafetyOracle(notificationCenter: center)
    #expect(
        oracle.state(nowMilliseconds: 100)
            == AustinSessionSafetyState(
                sessionActive: false,
                screensAwake: false,
                userPresenceFresh: false,
                environmentVerified: false,
                generation: 0
            )
    )

    try oracle.recordConfirmedUserPresence(nowMilliseconds: 100)
    #expect(oracle.state(nowMilliseconds: 100).environmentVerified)
    #expect(oracle.state(nowMilliseconds: 100).generation == 1)
    #expect(!oracle.state(nowMilliseconds: 1_101).userPresenceFresh)
    #expect(!oracle.state(nowMilliseconds: 99).userPresenceFresh)

    try oracle.recordConfirmedUserPresence(nowMilliseconds: 1_200)
    center.post(name: NSWorkspace.screensDidSleepNotification, object: nil)
    let sleeping = oracle.state(nowMilliseconds: 1_201)
    #expect(sleeping.sessionActive)
    #expect(!sleeping.screensAwake)
    #expect(!sleeping.userPresenceFresh)
    #expect(!sleeping.environmentVerified)
    center.post(name: NSWorkspace.screensDidWakeNotification, object: nil)
    let awake = oracle.state(nowMilliseconds: 1_202)
    #expect(awake.screensAwake)
    #expect(!awake.userPresenceFresh)

    try oracle.recordConfirmedUserPresence(nowMilliseconds: 1_300)
    center.post(name: NSWorkspace.sessionDidResignActiveNotification, object: nil)
    let resigned = oracle.state(nowMilliseconds: 1_301)
    #expect(!resigned.sessionActive)
    #expect(!resigned.userPresenceFresh)
    center.post(name: NSWorkspace.sessionDidBecomeActiveNotification, object: nil)
    let returned = oracle.state(nowMilliseconds: 1_302)
    #expect(returned.sessionActive)
    #expect(!returned.userPresenceFresh)
    #expect(!returned.environmentVerified)
}

@Test func austinSessionSafetyRejectsRollbackAndConfirmationCopyIsClosed() throws {
    let oracle = AustinSessionSafetyOracle(notificationCenter: NotificationCenter())
    try oracle.recordConfirmedUserPresence(nowMilliseconds: 2_000)
    #expect(throws: AustinFailure.self) {
        try oracle.recordConfirmedUserPresence(nowMilliseconds: 1_999)
    }
    #expect(AustinConfirmationAction.allCases.count == 7)
    for action in AustinConfirmationAction.allCases {
        #expect(!action.fixedMessage.isEmpty)
        #expect(action.fixedMessage.utf8.count <= 96)
        #expect(!action.fixedMessage.contains("{"))
        #expect(!action.fixedMessage.contains("}"))
        #expect(!action.fixedMessage.contains("%"))
    }
}
