import AustinCore
import Foundation

@main
struct AustinAppMain {
    static func main() {
        FileHandle.standardError.write(Data("austin control: disabled foundation\n".utf8))
    }
}
