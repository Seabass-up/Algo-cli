import Darwin
import Foundation
import Security

public struct NeonNativeFailure: Error, Equatable, Sendable, CustomStringConvertible {
    public let reasonCode: String

    public init(_ reasonCode: String) {
        let allowed = reasonCode.range(
            of: "^[a-z][a-z0-9_]{0,95}$",
            options: .regularExpression
        ) != nil
        self.reasonCode = allowed ? reasonCode : "invalid_failure"
    }

    public var description: String { reasonCode }
}

public enum NeonInvocation {
    public static let executableName = "neon-native-host"
    public static let allowedOriginResourceName = "NeonAllowedOrigin.txt"

    public static func validateCallerOrigin(
        arguments: [String],
        allowedOrigin: String
    ) throws -> String {
        guard arguments.count == 2,
              arguments[0].hasPrefix("/"),
              let actualOrigin = arguments.last,
              actualOrigin.range(
                  of: "^chrome-extension://[a-p]{32}/$",
                  options: .regularExpression
              ) != nil,
              allowedOrigin.range(
                  of: "^chrome-extension://[a-p]{32}/$",
                  options: .regularExpression
              ) != nil,
              actualOrigin == allowedOrigin
        else {
            throw NeonNativeFailure("extension_origin_rejected")
        }
        return actualOrigin
    }

    public static func enclosingApplicationBundleURL(
        executablePath: String
    ) throws -> URL {
        guard executablePath.hasPrefix("/") else {
            throw NeonNativeFailure("host_executable_path")
        }
        let executable = URL(fileURLWithPath: executablePath, isDirectory: false)
            .standardizedFileURL
            .resolvingSymlinksInPath()
        let helpers = executable.deletingLastPathComponent()
        let contents = helpers.deletingLastPathComponent()
        let application = contents.deletingLastPathComponent()
        guard executable.lastPathComponent == executableName,
              helpers.lastPathComponent == "Helpers",
              contents.lastPathComponent == "Contents",
              application.pathExtension == "app",
              application.path.hasPrefix("/")
        else {
            throw NeonNativeFailure("host_bundle_layout")
        }
        return application
    }

    public static func validateSealedApplicationBundle(_ bundle: URL) throws {
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(bundle as CFURL, SecCSFlags(), &code)
                == errSecSuccess,
              let code
        else {
            throw NeonNativeFailure("host_bundle_code")
        }
        let flags = SecCSFlags(
            rawValue: kSecCSStrictValidate
                | kSecCSCheckAllArchitectures
                | kSecCSCheckNestedCode
        )
        guard SecStaticCodeCheckValidity(code, flags, nil) == errSecSuccess else {
            throw NeonNativeFailure("host_bundle_signature")
        }
    }

    public static func loadAllowedOrigin(bundle: URL) throws -> String {
        let resource = bundle
            .appendingPathComponent("Contents", isDirectory: true)
            .appendingPathComponent("Resources", isDirectory: true)
            .appendingPathComponent(allowedOriginResourceName, isDirectory: false)
        let data = try readPinnedOriginResource(path: resource.path)
        guard let origin = String(data: data, encoding: .utf8) else {
            throw NeonNativeFailure("allowed_origin_resource")
        }
        guard origin.range(
            of: "^chrome-extension://[a-p]{32}/$",
            options: .regularExpression
        ) != nil else {
            throw NeonNativeFailure("allowed_origin_resource")
        }
        return origin
    }

    private static func readPinnedOriginResource(path: String) throws -> Data {
        let descriptor = Darwin.open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        guard descriptor >= 0 else {
            throw NeonNativeFailure("allowed_origin_resource")
        }
        defer { Darwin.close(descriptor) }

        var before = stat()
        guard fstat(descriptor, &before) == 0 else {
            throw NeonNativeFailure("allowed_origin_resource")
        }
        let mode = before.st_mode
        let ownerWritableByCaller = before.st_uid == geteuid() && (mode & 0o200) != 0
        guard (mode & S_IFMT) == S_IFREG,
              before.st_nlink == 1,
              before.st_uid == 0 || before.st_uid == geteuid(),
              (mode & 0o022) == 0,
              !ownerWritableByCaller,
              before.st_size > 0,
              before.st_size <= 64
        else {
            throw NeonNativeFailure("allowed_origin_resource")
        }

        let expectedSize = Int(before.st_size)
        var bytes = [UInt8](repeating: 0, count: expectedSize)
        var offset = 0
        while offset < expectedSize {
            let count = bytes.withUnsafeMutableBytes { buffer -> Int in
                guard let baseAddress = buffer.baseAddress else { return -1 }
                return Darwin.read(
                    descriptor,
                    baseAddress.advanced(by: offset),
                    expectedSize - offset
                )
            }
            guard count > 0 else {
                throw NeonNativeFailure("allowed_origin_resource")
            }
            offset += count
        }
        var extra: UInt8 = 0
        guard Darwin.read(descriptor, &extra, 1) == 0 else {
            throw NeonNativeFailure("allowed_origin_resource")
        }

        var after = stat()
        guard fstat(descriptor, &after) == 0,
              before.st_dev == after.st_dev,
              before.st_ino == after.st_ino,
              before.st_mode == after.st_mode,
              before.st_nlink == after.st_nlink,
              before.st_uid == after.st_uid,
              before.st_size == after.st_size,
              before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
              before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
              before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec,
              before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec
        else {
            throw NeonNativeFailure("allowed_origin_resource")
        }
        return Data(bytes)
    }

    @discardableResult
    public static func validate(arguments: [String]) throws -> String {
        guard let executablePath = arguments.first else {
            throw NeonNativeFailure("host_executable_path")
        }
        let bundle = try enclosingApplicationBundleURL(executablePath: executablePath)
        try validateSealedApplicationBundle(bundle)
        let allowedOrigin = try loadAllowedOrigin(bundle: bundle)
        try validateSealedApplicationBundle(bundle)
        return try validateCallerOrigin(
            arguments: arguments,
            allowedOrigin: allowedOrigin
        )
    }
}
