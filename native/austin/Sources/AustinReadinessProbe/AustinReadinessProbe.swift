import AustinDesktopCore
import Foundation

@main
struct AustinReadinessProbeMain {
    static func main() {
        let probe = AustinNativeReadinessProbe(
            backend: AustinSystemNativeReadinessBackend(),
            controlProtocolEnabled: false
        )
        FileHandle.standardOutput.write(probe.encoded())
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}
