import Darwin
import Foundation
import NeonNativeCore
import Testing

private let neonOrigin = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"

@Test func neonCallerOriginIsExactAndSingleArgument() throws {
    let executable = "/Applications/Algo CLI Control.app/Contents/Helpers/neon-native-host"
    #expect(
        try NeonInvocation.validateCallerOrigin(
            arguments: [executable, neonOrigin],
            allowedOrigin: neonOrigin
        ) == neonOrigin
    )
    #expect(throws: NeonNativeFailure.self) {
        try NeonInvocation.validateCallerOrigin(
            arguments: [executable, neonOrigin, "extra"],
            allowedOrigin: neonOrigin
        )
    }
    #expect(throws: NeonNativeFailure.self) {
        try NeonInvocation.validateCallerOrigin(
            arguments: [executable, neonOrigin],
            allowedOrigin: "chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/"
        )
    }
}

@Test func neonBundleDerivationRejectsLooseRelativeAndWrongNamedHosts() throws {
    let valid = try NeonInvocation.enclosingApplicationBundleURL(
        executablePath: "/Applications/Algo CLI Control.app/Contents/Helpers/neon-native-host"
    )
    #expect(valid.path == "/Applications/Algo CLI Control.app")
    #expect(throws: NeonNativeFailure.self) {
        try NeonInvocation.enclosingApplicationBundleURL(
            executablePath: "Contents/Helpers/neon-native-host"
        )
    }
    #expect(throws: NeonNativeFailure.self) {
        try NeonInvocation.enclosingApplicationBundleURL(
            executablePath: "/Applications/Algo CLI Control.app/Contents/Helpers/other-host"
        )
    }
}

@Test func neonFailureReasonsAreContentFreeAndBounded() {
    #expect(NeonNativeFailure("protocol_disabled").reasonCode == "protocol_disabled")
    #expect(NeonNativeFailure("/Users/example/private").reasonCode == "invalid_failure")
}

@Test func neonOriginResourceIsPinnedExactAndNotCallerWritable() throws {
    let bundle = FileManager.default.temporaryDirectory
        .appendingPathComponent(UUID().uuidString)
        .appendingPathExtension("app")
    let resources = bundle
        .appendingPathComponent("Contents")
        .appendingPathComponent("Resources")
    try FileManager.default.createDirectory(
        at: resources,
        withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: bundle) }
    let origin = resources.appendingPathComponent("NeonAllowedOrigin.txt")
    try Data(neonOrigin.utf8).write(to: origin)
    #expect(chmod(origin.path, 0o444) == 0)
    #expect(try NeonInvocation.loadAllowedOrigin(bundle: bundle) == neonOrigin)

    #expect(chmod(origin.path, 0o644) == 0)
    #expect(throws: NeonNativeFailure.self) {
        try NeonInvocation.loadAllowedOrigin(bundle: bundle)
    }

    try FileManager.default.removeItem(at: origin)
    let target = resources.appendingPathComponent("UntrustedOrigin.txt")
    try Data(neonOrigin.utf8).write(to: target)
    try FileManager.default.createSymbolicLink(at: origin, withDestinationURL: target)
    #expect(throws: NeonNativeFailure.self) {
        try NeonInvocation.loadAllowedOrigin(bundle: bundle)
    }
}
