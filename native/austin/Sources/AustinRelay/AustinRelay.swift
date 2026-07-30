import AustinCore
import Darwin
import Dispatch
import Foundation

private final class AustinRelayWaiter: @unchecked Sendable {
    private let lock = NSLock()
    private let semaphore = DispatchSemaphore(value: 0)
    private var result: Data?

    func resolve(_ data: Data) {
        lock.lock()
        var shouldSignal = false
        if result == nil {
            result = data
            shouldSignal = true
        }
        lock.unlock()
        if shouldSignal {
            semaphore.signal()
        }
    }

    func wait(seconds: TimeInterval) throws -> Data {
        lock.lock()
        if let result {
            lock.unlock()
            return result
        }
        lock.unlock()
        let milliseconds = max(1, Int((seconds * 1_000).rounded(.up)))
        guard semaphore.wait(
            timeout: DispatchTime.now() + .milliseconds(milliseconds)
        ) == .success else {
            throw AustinFailure("xpc_timeout")
        }
        lock.lock()
        defer { lock.unlock() }
        guard let result else { throw AustinFailure("xpc_timeout") }
        return result
    }
}

private enum AustinRelayIO {
    static func readFrame() throws -> Data {
        let header = try readExactly(4)
        let length = header.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        guard length > 0, length <= UInt32(austinMaximumFrameBytes) else {
            throw AustinFailure("frame_length")
        }
        let payload = try readExactly(Int(length))
        guard FileHandle.standardInput.readData(ofLength: 1).isEmpty else {
            throw AustinFailure("frame_trailing_data")
        }
        return payload
    }

    static func writeFrame(_ payload: Data) throws {
        guard !payload.isEmpty, payload.count <= austinMaximumFrameBytes else {
            throw AustinFailure("frame_size")
        }
        var length = UInt32(payload.count).bigEndian
        let header = withUnsafeBytes(of: &length) { Data($0) }
        FileHandle.standardOutput.write(header)
        FileHandle.standardOutput.write(payload)
    }

    private static func readExactly(_ count: Int) throws -> Data {
        var result = Data()
        while result.count < count {
            let chunk = FileHandle.standardInput.readData(ofLength: count - result.count)
            guard !chunk.isEmpty else { throw AustinFailure("frame_truncated") }
            result.append(chunk)
        }
        return result
    }
}

private final class AustinRelayClient: @unchecked Sendable {
    private let connection: NSXPCConnection

    init() throws {
        connection = NSXPCConnection(machServiceName: austinMachServiceName, options: [])
        connection.remoteObjectInterface = NSXPCInterface(with: AustinXPCProtocol.self)
        connection.setCodeSigningRequirement(
            try AustinPeerIdentity.peerRequirement(peerIdentifier: austinAdapterSigningIdentifier)
        )
        connection.activate()
    }

    deinit {
        connection.invalidate()
    }

    func open() throws -> Data {
        let clientNonce = encodeBase64URL(try AustinSession.secureRandomBytes())
        let hello = try AustinJSON.encodeCanonical([
            "client_nonce": clientNonce,
            "message_type": "austin.begin",
            "protocol_version": austinProtocolVersion,
        ])
        let waiter = AustinRelayWaiter()
        guard let proxy = proxy(waiter: waiter) else { throw AustinFailure("xpc_proxy") }
        proxy.beginSession(hello as NSData) { waiter.resolve($0 as Data) }
        let response = try waiter.wait(seconds: 5)
        let object = try AustinJSON.decodeCanonicalObject(response)
        guard object["status"] as? String == "succeeded",
              let capabilityText = object["capability"] as? String,
              let capability = decodeBase64URL(capabilityText),
              capability.count == 32,
              object["client_nonce"] as? String == clientNonce
        else {
            throw AustinFailure((object["reason_code"] as? String) ?? "session_denied")
        }
        return capability
    }

    func execute(_ envelope: Data, capability: Data) throws -> Data {
        let waiter = AustinRelayWaiter()
        guard let proxy = proxy(waiter: waiter) else { throw AustinFailure("xpc_proxy") }
        proxy.execute(
            envelope as NSData,
            capability: capability as NSData,
            sequence: NSNumber(value: 1)
        ) { waiter.resolve($0 as Data) }
        return try waiter.wait(seconds: 10)
    }

    func readiness(capability: Data) throws -> Data {
        let waiter = AustinRelayWaiter()
        guard let proxy = proxy(waiter: waiter) else { throw AustinFailure("xpc_proxy") }
        proxy.readiness(
            capability as NSData,
            sequence: NSNumber(value: 1)
        ) { waiter.resolve($0 as Data) }
        return try waiter.wait(seconds: 10)
    }

    func prepare(_ authorization: Data, capability: Data) throws -> Data {
        let waiter = AustinRelayWaiter()
        guard let proxy = proxy(waiter: waiter) else { throw AustinFailure("xpc_proxy") }
        proxy.prepare(
            authorization as NSData,
            capability: capability as NSData,
            sequence: NSNumber(value: 1)
        ) { waiter.resolve($0 as Data) }
        return try waiter.wait(seconds: 35)
    }

    func perform(_ payload: Data, capability: Data) throws -> Data {
        let object = try AustinJSON.decodeCanonicalObject(payload)
        guard let messageType = object["message_type"] as? String else {
            throw AustinFailure("message_type")
        }
        switch messageType {
        case "control.prepare":
            return try prepare(payload, capability: capability)
        case "control.execute":
            return try execute(payload, capability: capability)
        default:
            throw AustinFailure("message_type")
        }
    }

    private func proxy(waiter: AustinRelayWaiter) -> AustinXPCProtocol? {
        connection.remoteObjectProxyWithErrorHandler { error in
            let reason = "xpc_remote_error_\(abs((error as NSError).code))"
            waiter.resolve(AustinReply.encode(status: "failed", reasonCode: reason))
        } as? AustinXPCProtocol
    }
}

@main
struct AustinRelayMain {
    static func main() {
        do {
            let client = try AustinRelayClient()
            let capability = try client.open()
            if CommandLine.arguments == [CommandLine.arguments[0], "--probe"] {
                let result = AustinReply.encode(status: "succeeded", reasonCode: "xpc_authenticated")
                FileHandle.standardOutput.write(result)
                return
            }
            if CommandLine.arguments == [CommandLine.arguments[0], "--readiness-probe"] {
                FileHandle.standardOutput.write(try client.readiness(capability: capability))
                return
            }
            guard CommandLine.arguments.count == 1 else {
                throw AustinFailure("relay_arguments")
            }
            let payload = try AustinRelayIO.readFrame()
            try AustinRelayIO.writeFrame(try client.perform(payload, capability: capability))
        } catch let failure as AustinFailure {
            let result = AustinReply.encode(status: "failed", reasonCode: failure.reasonCode)
            if CommandLine.arguments.count == 2,
               ["--probe", "--readiness-probe"].contains(CommandLine.arguments[1]) {
                FileHandle.standardOutput.write(result)
            } else {
                try? AustinRelayIO.writeFrame(result)
            }
            Darwin.exit(1)
        } catch {
            let result = AustinReply.encode(status: "failed", reasonCode: "relay_internal")
            try? AustinRelayIO.writeFrame(result)
            Darwin.exit(1)
        }
    }
}
