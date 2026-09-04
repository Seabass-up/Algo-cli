import Foundation
import Testing
@testable import AustinCore

@Test func xpcVocabularyIsFrozen() {
    #expect(austinMachServiceName == "group.com.algo-cli.control.austin.tcc-adapter")
    #expect(austinRelaySigningIdentifier == "com.algo-cli.austin.relay")
    #expect(austinAdapterSigningIdentifier == "com.algo-cli.austin.tcc-adapter")
    #expect(
        NSStringFromSelector(
            #selector(AustinXPCProtocol.readiness(_:sequence:withReply:))
        ) == "readiness:sequence:withReply:"
    )
    #expect(
        NSStringFromSelector(
            #selector(AustinXPCProtocol.prepare(_:capability:sequence:withReply:))
        ) == "prepare:capability:sequence:withReply:"
    )
}
