import Foundation

@objc public protocol AustinXPCProtocol {
    func beginSession(_ hello: NSData, withReply reply: @escaping (NSData) -> Void)
    func readiness(
        _ capability: NSData,
        sequence: NSNumber,
        withReply reply: @escaping (NSData) -> Void
    )
    func prepare(
        _ authorization: NSData,
        capability: NSData,
        sequence: NSNumber,
        withReply reply: @escaping (NSData) -> Void
    )
    func execute(
        _ envelope: NSData,
        capability: NSData,
        sequence: NSNumber,
        withReply reply: @escaping (NSData) -> Void
    )
}

public let austinMachServiceName = "group.com.algo-cli.control.austin.tcc-adapter"
public let austinRelaySigningIdentifier = "com.algo-cli.austin.relay"
public let austinAdapterSigningIdentifier = "com.algo-cli.austin.tcc-adapter"
