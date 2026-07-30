// swift-tools-version: 6.1

import PackageDescription

let package = Package(
    name: "AustinNativeControl",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "AustinCore", targets: ["AustinCore"]),
        .library(name: "AustinDesktopCore", targets: ["AustinDesktopCore"]),
        .library(name: "NeonNativeCore", targets: ["NeonNativeCore"]),
        .executable(name: "austin-relay", targets: ["AustinRelay"]),
        .executable(name: "austin-tcc-adapter", targets: ["AustinTCCAdapter"]),
        .executable(name: "austin-control", targets: ["AustinApp"]),
        .executable(
            name: "austin-readiness-probe",
            targets: ["AustinReadinessProbe"]
        ),
        .executable(
            name: "austin-credential-migrator",
            targets: ["AustinCredentialMigrator"]
        ),
        .executable(
            name: "austin-ada-crash-probe",
            targets: ["AustinAdaCrashProbe"]
        ),
        .executable(name: "neon-native-host", targets: ["NeonNativeHost"]),
    ],
    targets: [
        .target(
            name: "AustinDarwinBridge",
            publicHeadersPath: "include"
        ),
        .target(
            name: "AustinCore",
            dependencies: ["AustinDarwinBridge"],
            linkerSettings: [
                .linkedFramework("Security"),
                .linkedLibrary("sqlite3"),
            ]
        ),
        .executableTarget(
            name: "AustinRelay",
            dependencies: ["AustinCore"]
        ),
        .target(
            name: "AustinDesktopCore",
            dependencies: ["AustinCore"],
            path: "Sources/AustinTCCAdapter",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("ApplicationServices"),
                .linkedFramework("CoreGraphics"),
                .linkedFramework("LocalAuthentication"),
                .linkedFramework("ScreenCaptureKit"),
                .linkedFramework("Vision"),
            ]
        ),
        .executableTarget(
            name: "AustinTCCAdapter",
            dependencies: ["AustinCore", "AustinDesktopCore"],
            path: "Sources/AustinTCCAdapterMain"
        ),
        .executableTarget(
            name: "AustinApp",
            dependencies: ["AustinCore"],
            linkerSettings: [.linkedFramework("AppKit")]
        ),
        .executableTarget(
            name: "AustinReadinessProbe",
            dependencies: ["AustinDesktopCore"]
        ),
        .executableTarget(
            name: "AustinCredentialMigrator",
            dependencies: ["AustinCore"],
            path: "Sources/AustinCredentialMigratorMain"
        ),
        .executableTarget(
            name: "AustinAdaCrashProbe",
            dependencies: ["AustinCore"],
            path: "Sources/AustinAdaCrashProbeMain"
        ),
        .target(
            name: "NeonNativeCore",
            linkerSettings: [.linkedFramework("Security")]
        ),
        .executableTarget(
            name: "NeonNativeHost",
            dependencies: ["NeonNativeCore"],
            path: "Sources/NeonNativeHostMain"
        ),
        .testTarget(
            name: "AustinCoreTests",
            dependencies: ["AustinCore", "AustinDesktopCore"]
        ),
        .testTarget(
            name: "AustinIntegrationTests",
            dependencies: ["AustinCore"]
        ),
        .testTarget(
            name: "NeonNativeCoreTests",
            dependencies: ["NeonNativeCore"]
        ),
    ]
)
