import AustinCore
import AustinDesktopCore
import Darwin
import Foundation
import Security

private final class AustinXPCReplyBox: @unchecked Sendable {
    private let lock = NSLock()
    private var reply: ((NSData) -> Void)?

    init(_ reply: @escaping (NSData) -> Void) {
        self.reply = reply
    }

    func resolve(_ data: Data) {
        lock.lock()
        let callback = reply
        reply = nil
        lock.unlock()
        callback?(data as NSData)
    }
}

private final class AustinConnectionHandler: NSObject, AustinXPCProtocol, @unchecked Sendable {
    private let peer: AustinPeer
    private let authority: AustinSamuelAuthority
    private let permitStore: AustinAdaPermitStore
    private let dispatcher: AustinDesktopDispatcher
    private let bindingCoordinator: AustinThomasBindingCoordinator?
    private let readinessProbe: AustinNativeReadinessProbe
    private let readinessGate: AustinNativeReadinessGate
    private let lock = NSLock()
    private var session: AustinSession?

    init(
        peer: AustinPeer,
        authority: AustinSamuelAuthority,
        permitStore: AustinAdaPermitStore,
        dispatcher: AustinDesktopDispatcher,
        bindingCoordinator: AustinThomasBindingCoordinator?,
        readinessProbe: AustinNativeReadinessProbe,
        readinessGate: AustinNativeReadinessGate
    ) {
        self.peer = peer
        self.authority = authority
        self.permitStore = permitStore
        self.dispatcher = dispatcher
        self.bindingCoordinator = bindingCoordinator
        self.readinessProbe = readinessProbe
        self.readinessGate = readinessGate
    }

    func beginSession(_ hello: NSData, withReply reply: @escaping (NSData) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        do {
            guard session == nil else { throw AustinFailure("session_already_open") }
            let nonce = try AustinSession.decodeHello(hello as Data)
            let opened = try AustinSession(
                peer: peer,
                nowMilliseconds: AustinClock.nowMilliseconds()
            )
            session = opened
            reply(AustinSession.encodeBeginReply(opened, clientNonce: nonce) as NSData)
        } catch let failure as AustinFailure {
            reply(AustinReply.encode(status: "denied", reasonCode: failure.reasonCode) as NSData)
        } catch {
            reply(AustinReply.encode(status: "failed", reasonCode: "session_internal") as NSData)
        }
    }

    func readiness(
        _ capability: NSData,
        sequence: NSNumber,
        withReply reply: @escaping (NSData) -> Void
    ) {
        lock.lock()
        do {
            guard let session else { throw AustinFailure("session_not_open") }
            let parsedSequence = try AustinJSON.integer(
                sequence,
                label: "session_sequence",
                minimum: 1,
                maximum: Int64(AustinSession.maximumCalls)
            )
            try session.consume(
                suppliedCapability: capability as Data,
                sequence: UInt64(parsedSequence),
                nowMilliseconds: AustinClock.nowMilliseconds()
            )
            guard readinessGate.claim() else {
                throw AustinFailure("readiness_busy")
            }
            lock.unlock()
        } catch let failure as AustinFailure {
            lock.unlock()
            reply(AustinReply.encode(status: "denied", reasonCode: failure.reasonCode) as NSData)
            return
        } catch {
            lock.unlock()
            reply(AustinReply.encode(status: "failed", reasonCode: "readiness_internal") as NSData)
            return
        }
        // Permission preflights may block in the OS. The authenticated session
        // sequence is consumed under the lock, but content-free observation is
        // deliberately outside it so invalidation and other calls cannot be
        // starved by a slow framework response.
        let response = readinessProbe.encoded()
        guard readinessGate.release() else {
            reply(AustinReply.encode(status: "failed", reasonCode: "readiness_internal") as NSData)
            return
        }
        reply(response as NSData)
    }

    func prepare(
        _ authorization: NSData,
        capability: NSData,
        sequence: NSNumber,
        withReply reply: @escaping (NSData) -> Void
    ) {
        let replyBox = AustinXPCReplyBox(reply)
        let verified: AustinVerifiedPreparation
        lock.lock()
        do {
            guard let session else { throw AustinFailure("session_not_open") }
            let parsedSequence = try AustinJSON.integer(
                sequence,
                label: "session_sequence",
                minimum: 1,
                maximum: Int64(AustinSession.maximumCalls)
            )
            let now = AustinClock.nowMilliseconds()
            try session.consume(
                suppliedCapability: capability as Data,
                sequence: UInt64(parsedSequence),
                nowMilliseconds: now
            )
            verified = try authority.verifyPreparation(
                authorization as Data,
                nowMilliseconds: now
            )
            guard bindingCoordinator?.isEnabled(for: verified) == true else {
                throw AustinFailure("adapter_disabled")
            }
            try permitStore.claimPreparation(
                preparationID: verified.preparationID,
                preparationDigest: verified.preparationDigest,
                claimedAtMilliseconds: now,
                expiresAtMilliseconds: verified.expiresAtMilliseconds
            )
            lock.unlock()
        } catch let failure as AustinFailure {
            lock.unlock()
            replyBox.resolve(AustinReply.encode(status: "denied", reasonCode: failure.reasonCode))
            return
        } catch {
            lock.unlock()
            replyBox.resolve(AustinReply.encode(status: "failed", reasonCode: "adapter_internal"))
            return
        }

        guard let bindingCoordinator else {
            replyBox.resolve(AustinReply.encode(status: "denied", reasonCode: "adapter_disabled"))
            return
        }
        Task { @MainActor in
            do {
                replyBox.resolve(try bindingCoordinator.prepare(verified))
            } catch let failure as AustinFailure {
                replyBox.resolve(
                    AustinReply.encode(
                        status: "denied",
                        reasonCode: failure.reasonCode
                    )
                )
            } catch {
                replyBox.resolve(
                    AustinReply.encode(
                        status: "failed",
                        reasonCode: "preparation_internal"
                    )
                )
            }
        }
    }

    func execute(
        _ envelope: NSData,
        capability: NSData,
        sequence: NSNumber,
        withReply reply: @escaping (NSData) -> Void
    ) {
        lock.lock()
        defer { lock.unlock() }
        do {
            guard let session else { throw AustinFailure("session_not_open") }
            let parsedSequence = try AustinJSON.integer(
                sequence,
                label: "session_sequence",
                minimum: 1,
                maximum: Int64(AustinSession.maximumCalls)
            )
            let now = AustinClock.nowMilliseconds()
            try session.consume(
                suppliedCapability: capability as Data,
                sequence: UInt64(parsedSequence),
                nowMilliseconds: now
            )
            let verified = try authority.verify(envelope as Data, nowMilliseconds: now)
            guard dispatcher.isReady(for: verified, nowMilliseconds: now) else {
                reply(AustinReply.encode(status: "denied", reasonCode: "adapter_disabled") as NSData)
                return
            }
            try permitStore.claim(
                permitID: verified.permitID,
                requestDigest: verified.requestDigest,
                claimedAtMilliseconds: now,
                expiresAtMilliseconds: verified.expiresAtMilliseconds
            )
            reply(dispatcher.execute(verified, nowMilliseconds: now) as NSData)
        } catch let failure as AustinFailure {
            reply(AustinReply.encode(status: "denied", reasonCode: failure.reasonCode) as NSData)
        } catch {
            reply(AustinReply.encode(status: "failed", reasonCode: "adapter_internal") as NSData)
        }
    }

    func invalidate() {
        lock.lock()
        session?.invalidate()
        session = nil
        lock.unlock()
    }
}

private final class AustinAdapterListener: NSObject, NSXPCListenerDelegate, @unchecked Sendable {
    private let authority: AustinSamuelAuthority
    private let permitStore: AustinAdaPermitStore
    private let dispatcher: AustinDesktopDispatcher
    private let bindingCoordinator: AustinThomasBindingCoordinator?
    private let readinessProbe: AustinNativeReadinessProbe
    private let readinessGate = AustinNativeReadinessGate()

    init(
        authority: AustinSamuelAuthority,
        permitStore: AustinAdaPermitStore,
        dispatcher: AustinDesktopDispatcher,
        bindingCoordinator: AustinThomasBindingCoordinator?,
        readinessProbe: AustinNativeReadinessProbe
    ) {
        self.authority = authority
        self.permitStore = permitStore
        self.dispatcher = dispatcher
        self.bindingCoordinator = bindingCoordinator
        self.readinessProbe = readinessProbe
    }

    func listener(
        _ listener: NSXPCListener,
        shouldAcceptNewConnection connection: NSXPCConnection
    ) -> Bool {
        do {
            let peer = try AustinPeerIdentity.validateConnectionPeer(connection)
            let handler = AustinConnectionHandler(
                peer: peer,
                authority: authority,
                permitStore: permitStore,
                dispatcher: dispatcher,
                bindingCoordinator: bindingCoordinator,
                readinessProbe: readinessProbe,
                readinessGate: readinessGate
            )
            connection.exportedInterface = NSXPCInterface(with: AustinXPCProtocol.self)
            connection.exportedObject = handler
            connection.invalidationHandler = { handler.invalidate() }
            connection.interruptionHandler = { handler.invalidate() }
            connection.activate()
            return true
        } catch let failure as AustinFailure {
            FileHandle.standardError.write(Data("austin adapter: \(failure.reasonCode)\n".utf8))
            connection.invalidate()
            return false
        } catch {
            FileHandle.standardError.write(Data("austin adapter: peer_rejected\n".utf8))
            connection.invalidate()
            return false
        }
    }
}

@main
struct AustinTCCAdapterMain {
    @MainActor
    static func main() {
        do {
            let authority = try AustinSamuelAuthority(publicKeyData: loadAuthorityKey())
            let store = try AustinAdaPermitStore(databaseURL: permitStoreURL())
            let control = try AustinThomasProductionControl.system(
                activationPayload: loadControlActivation()
            )
            let listener = NSXPCListener(machServiceName: austinMachServiceName)
            let requirement = try AustinPeerIdentity.peerRequirement(
                peerIdentifier: austinRelaySigningIdentifier
            )
            listener.setConnectionCodeSigningRequirement(requirement)
            let delegate = AustinAdapterListener(
                authority: authority,
                permitStore: store,
                dispatcher: control.dispatcher,
                bindingCoordinator: control.coordinator,
                readinessProbe: AustinNativeReadinessProbe(
                    backend: AustinSystemNativeReadinessBackend(),
                    controlProtocolEnabled: control.controlProtocolEnabled
                )
            )
            listener.delegate = delegate
            listener.activate()
            RunLoop.current.run()
        } catch let failure as AustinFailure {
            FileHandle.standardError.write(Data("austin adapter: \(failure.reasonCode)\n".utf8))
            Darwin.exit(78)
        } catch {
            FileHandle.standardError.write(Data("austin adapter: startup_failed\n".utf8))
            Darwin.exit(78)
        }
    }

    private static func loadAuthorityKey() throws -> Data {
        #if DEBUG
        if ProcessInfo.processInfo.environment["ALGO_AUSTIN_ADHOC_TEST"] == "1",
           let encoded = ProcessInfo.processInfo.environment["ALGO_AUSTIN_TEST_AUTHORITY_KEY"],
           let decoded = decodeBase64URL(encoded),
           decoded.count == 32 {
            return decoded
        }
        #endif
        let bundle = try enclosingApplicationBundleURL()
        try validateSealedApplicationBundle(bundle)
        let url = bundle
            .appendingPathComponent("Contents", isDirectory: true)
            .appendingPathComponent("Resources", isDirectory: true)
            .appendingPathComponent("AustinAuthorityPublicKey.bin", isDirectory: false)
        var information = stat()
        guard lstat(url.path, &information) == 0,
              (information.st_mode & S_IFMT) == S_IFREG,
              information.st_nlink == 1,
              information.st_uid == 0 || information.st_uid == geteuid(),
              (information.st_mode & 0o022) == 0,
              let data = try? Data(contentsOf: url, options: [.mappedIfSafe]),
              data.count == 32
        else {
            throw AustinFailure("sealed_authority_key_missing")
        }
        return data
    }

    private static func loadControlActivation() throws -> Data? {
        #if DEBUG
        if ProcessInfo.processInfo.environment["ALGO_AUSTIN_ADHOC_TEST"] == "1" {
            return nil
        }
        #endif
        let bundle = try enclosingApplicationBundleURL()
        try validateSealedApplicationBundle(bundle)
        let url = bundle
            .appendingPathComponent("Contents", isDirectory: true)
            .appendingPathComponent("Resources", isDirectory: true)
            .appendingPathComponent("AustinNativeControlActivation.json", isDirectory: false)
        let descriptor = Darwin.open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        if descriptor < 0, errno == ENOENT {
            return nil
        }
        guard descriptor >= 0 else {
            throw AustinFailure("sealed_control_activation")
        }
        defer { Darwin.close(descriptor) }

        var before = stat()
        guard fstat(descriptor, &before) == 0,
              (before.st_mode & S_IFMT) == S_IFREG,
              before.st_nlink == 1,
              before.st_uid == 0 || before.st_uid == geteuid(),
              (before.st_mode & 0o022) == 0,
              before.st_size > 0,
              before.st_size <= 4_096
        else {
            throw AustinFailure("sealed_control_activation")
        }

        var payload = Data()
        payload.reserveCapacity(Int(before.st_size))
        var remaining = Int(before.st_size)
        while remaining > 0 {
            var buffer = [UInt8](repeating: 0, count: min(remaining, 4_096))
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count < 0, errno == EINTR { continue }
            guard count > 0 else {
                throw AustinFailure("sealed_control_activation")
            }
            payload.append(contentsOf: buffer.prefix(count))
            remaining -= count
        }
        var extra: UInt8 = 0
        guard Darwin.read(descriptor, &extra, 1) == 0 else {
            throw AustinFailure("sealed_control_activation")
        }
        var after = stat()
        guard fstat(descriptor, &after) == 0,
              before.st_dev == after.st_dev,
              before.st_ino == after.st_ino,
              before.st_size == after.st_size,
              before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
              before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
              before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec,
              before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec
        else {
            throw AustinFailure("sealed_control_activation")
        }
        try validateSealedApplicationBundle(bundle)
        _ = try AustinThomasControlActivation.decode(payload)
        return payload
    }

    private static func enclosingApplicationBundleURL() throws -> URL {
        let argument = CommandLine.arguments[0]
        guard argument.hasPrefix("/") else { throw AustinFailure("adapter_executable_path") }
        let executable = URL(fileURLWithPath: argument, isDirectory: false)
            .standardizedFileURL
            .resolvingSymlinksInPath()
        let helpers = executable.deletingLastPathComponent()
        let contents = helpers.deletingLastPathComponent()
        let application = contents.deletingLastPathComponent()
        guard executable.lastPathComponent == "austin-tcc-adapter",
              helpers.lastPathComponent == "Helpers",
              contents.lastPathComponent == "Contents",
              application.pathExtension == "app",
              application.path.hasPrefix("/")
        else {
            throw AustinFailure("adapter_bundle_layout")
        }
        return application
    }

    private static func validateSealedApplicationBundle(_ bundle: URL) throws {
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(bundle as CFURL, SecCSFlags(), &code) == errSecSuccess,
              let code
        else {
            throw AustinFailure("adapter_bundle_code")
        }
        let flags = SecCSFlags(
            rawValue: kSecCSStrictValidate
                | kSecCSCheckAllArchitectures
                | kSecCSCheckNestedCode
        )
        guard SecStaticCodeCheckValidity(code, flags, nil) == errSecSuccess else {
            throw AustinFailure("adapter_bundle_signature")
        }
    }

    private static func permitStoreURL() throws -> URL {
        #if DEBUG
        if ProcessInfo.processInfo.environment["ALGO_AUSTIN_ADHOC_TEST"] == "1",
           let path = ProcessInfo.processInfo.environment["ALGO_AUSTIN_TEST_STORE"],
           path.hasPrefix("/") {
            return URL(fileURLWithPath: path, isDirectory: false)
        }
        #endif
        guard let root = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first else {
            throw AustinFailure("permit_store_location")
        }
        return root
            .appendingPathComponent("com.algo-cli.austin", isDirectory: true)
            .appendingPathComponent("AdaPermitClaims.sqlite3", isDirectory: false)
    }
}
