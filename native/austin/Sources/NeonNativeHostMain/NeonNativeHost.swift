import Darwin
import Foundation
import NeonNativeCore

@main
struct NeonNativeHostMain {
    static func main() {
        do {
            try NeonInvocation.validate(arguments: CommandLine.arguments)
            fail("protocol_disabled")
        } catch let failure as NeonNativeFailure {
            fail(failure.reasonCode)
        } catch {
            fail("host_startup_failed")
        }
    }

    private static func fail(_ reasonCode: String) -> Never {
        let failure = NeonNativeFailure(reasonCode)
        FileHandle.standardError.write(
            Data("neon native host: \(failure.reasonCode)\n".utf8)
        )
        Darwin.exit(78)
    }
}
