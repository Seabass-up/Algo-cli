#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/script/austin_build_and_run.sh"
PACKAGE="$ROOT/native/austin"
RESOURCES="$PACKAGE/Resources"
STAGE_ROOT="$PACKAGE/.build/austin-stage"
BUNDLE="$STAGE_ROOT/Algo CLI Control.app"
CONTENTS="$BUNDLE/Contents"
CONFIGURATION="${AUSTIN_CONFIGURATION:-debug}"
COMMAND="${1:-build}"
BUILD_LOCK="$PACKAGE/.build/AustinBuild.lock"
TEST_PUBLIC_KEY="A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
TEST_NEON_EXTENSION_ORIGIN="chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"

fail() {
    print -u2 -- "austin build: $1"
    exit 1
}

if [[ "${AUSTIN_BUILD_LOCK_HELD:-0}" != "1" ]]; then
    [[ ! -L "$PACKAGE" && -d "$PACKAGE" ]] || fail "package_path_unsafe"
    mkdir -p "$PACKAGE/.build"
    [[ ! -L "$PACKAGE/.build" && -d "$PACKAGE/.build" ]] \
        || fail "build_path_unsafe"
    exec /usr/bin/lockf -k -t 120 "$BUILD_LOCK" \
        /usr/bin/env AUSTIN_BUILD_LOCK_HELD=1 "$0" "$@"
fi

sign_binary() {
    local binary="$1"
    local identifier="$2"
    local entitlements="$3"
    local identity="${AUSTIN_DEVELOPER_ID_IDENTITY:--}"
    local timestamp=()
    if [[ "$identity" != "-" ]]; then
        timestamp=(--timestamp)
    fi
    codesign \
        --force \
        --sign "$identity" \
        --identifier "$identifier" \
        --options runtime \
        "${timestamp[@]}" \
        --entitlements "$entitlements" \
        "$binary"
}

build_bundle() {
    [[ "$CONFIGURATION" == "debug" || "$CONFIGURATION" == "release" ]] \
        || fail "invalid_configuration"
    local identity="${AUSTIN_DEVELOPER_ID_IDENTITY:--}"
    if [[ "$CONFIGURATION" == "release" ]]; then
        [[ "$identity" =~ '^Developer ID Application: [^\r\n]{1,160} \([A-Z0-9]{10}\)$' ]] \
            || fail "developer_id_application_required"
        [[ "${AUSTIN_RELEASE_VERSION:-}" =~ '^[0-9]+\.[0-9]+\.[0-9]+$' ]] \
            || fail "release_version_required"
        [[ "${AUSTIN_RELEASE_BUILD:-}" =~ '^[1-9][0-9]{0,8}$' ]] \
            || fail "release_build_required"
    fi
    swift build --package-path "$PACKAGE" --configuration "$CONFIGURATION"
    local bin_path
    bin_path="$(swift build --package-path "$PACKAGE" --configuration "$CONFIGURATION" --show-bin-path)"

    rm -rf "$STAGE_ROOT"
    mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Helpers" "$CONTENTS/Resources"
    install -m 0755 "$bin_path/austin-control" "$CONTENTS/MacOS/austin-control"
    install -m 0755 "$bin_path/austin-relay" "$CONTENTS/Helpers/austin-relay"
    install -m 0755 "$bin_path/austin-tcc-adapter" "$CONTENTS/Helpers/austin-tcc-adapter"
    install -m 0755 "$bin_path/austin-credential-migrator" \
        "$CONTENTS/Helpers/austin-credential-migrator"
    install -m 0755 "$bin_path/neon-native-host" "$CONTENTS/Helpers/neon-native-host"
    install -m 0644 "$RESOURCES/AustinApp-Info.plist" "$CONTENTS/Info.plist"
    if [[ "$CONFIGURATION" == "release" ]]; then
        plutil -replace CFBundleShortVersionString -string \
            "$AUSTIN_RELEASE_VERSION" "$CONTENTS/Info.plist"
        plutil -replace CFBundleVersion -string \
            "$AUSTIN_RELEASE_BUILD" "$CONTENTS/Info.plist"
    fi
    install -m 0644 "$RESOURCES/AustinLaunchAgent.plist" "$CONTENTS/Resources/AustinLaunchAgent.plist"

    if [[ -n "${AUSTIN_AUTHORITY_PUBLIC_KEY_FILE:-}" ]]; then
        [[ -f "$AUSTIN_AUTHORITY_PUBLIC_KEY_FILE" ]] || fail "authority_key_missing"
        [[ "$(stat -f %z "$AUSTIN_AUTHORITY_PUBLIC_KEY_FILE")" == "32" ]] \
            || fail "authority_key_size"
        install -m 0444 "$AUSTIN_AUTHORITY_PUBLIC_KEY_FILE" \
            "$CONTENTS/Resources/AustinAuthorityPublicKey.bin"
    elif [[ "$CONFIGURATION" == "debug" ]]; then
        print -n -- "${TEST_PUBLIC_KEY}=" | tr '_-' '/+' | base64 -D \
            > "$CONTENTS/Resources/AustinAuthorityPublicKey.bin"
        chmod 0444 "$CONTENTS/Resources/AustinAuthorityPublicKey.bin"
    else
        fail "sealed_authority_key_required"
    fi
    [[ "$(stat -f %z "$CONTENTS/Resources/AustinAuthorityPublicKey.bin")" == "32" ]] \
        || fail "staged_authority_key_size"

    local neon_origin="${NEON_EXTENSION_ORIGIN:-}"
    if [[ -z "$neon_origin" && "$CONFIGURATION" == "debug" ]]; then
        neon_origin="$TEST_NEON_EXTENSION_ORIGIN"
    fi
    [[ "$neon_origin" =~ '^chrome-extension://[a-p]{32}/$' ]] \
        || fail "neon_extension_origin_required"
    print -rn -- "$neon_origin" > "$CONTENTS/Resources/NeonAllowedOrigin.txt"
    chmod 0444 "$CONTENTS/Resources/NeonAllowedOrigin.txt"

    sign_binary \
        "$CONTENTS/Helpers/austin-relay" \
        "com.algo-cli.austin.relay" \
        "$RESOURCES/AustinRelay.entitlements"
    sign_binary \
        "$CONTENTS/Helpers/austin-tcc-adapter" \
        "com.algo-cli.austin.tcc-adapter" \
        "$RESOURCES/AustinTCCAdapter.entitlements"
    sign_binary \
        "$CONTENTS/Helpers/austin-credential-migrator" \
        "com.algo-cli.austin.credential-migrator" \
        "$RESOURCES/AustinCredentialMigrator.entitlements"
    sign_binary \
        "$CONTENTS/Helpers/neon-native-host" \
        "com.algo-cli.neon.host" \
        "$RESOURCES/NeonNativeHost.entitlements"
    sign_binary \
        "$CONTENTS/MacOS/austin-control" \
        "com.algo-cli.austin.control" \
        "$RESOURCES/AustinApp.entitlements"

    local timestamp=()
    if [[ "$identity" != "-" ]]; then
        timestamp=(--timestamp)
    fi
    codesign \
        --force \
        --sign "$identity" \
        --identifier "com.algo-cli.austin.control" \
        --options runtime \
        "${timestamp[@]}" \
        --entitlements "$RESOURCES/AustinApp.entitlements" \
        "$BUNDLE"

    "$ROOT/.venv/bin/python" "$ROOT/scripts/austin_native_package_audit.py" --bundle "$BUNDLE"
}

probe_xpc() {
    [[ "$CONFIGURATION" == "debug" ]] || fail "probe_requires_debug"
    [[ "${AUSTIN_DEVELOPER_ID_IDENTITY:--}" == "-" ]] || fail "probe_requires_adhoc"
    build_bundle
    # AMFI refuses to launch an App-Sandboxed ad-hoc binary. Re-sign only the
    # ephemeral staged relay with an empty test profile so the XPC handshake can
    # be exercised. This probe is not network-isolation or release evidence.
    codesign \
        --force \
        --sign - \
        --identifier "com.algo-cli.austin.relay" \
        --options runtime \
        --entitlements "$RESOURCES/AustinRelayProbe.entitlements" \
        "$CONTENTS/Helpers/austin-relay"

    local temporary
    temporary="$(mktemp -d "${TMPDIR:-/tmp}/AustinXPC.XXXXXX")"
    local plist="$temporary/AustinLaunchAgent.plist"
    local store="$temporary/private/AdaPermitClaims.sqlite3"
    local domain="gui/$(id -u)"
    local service="group.com.algo-cli.control.austin.tcc-adapter"
    cp "$RESOURCES/AustinLaunchAgent.plist" "$plist"
    plutil -replace ProgramArguments -json \
        "[\"$CONTENTS/Helpers/austin-tcc-adapter\"]" "$plist"
    plutil -replace StandardErrorPath -string "$temporary/AustinAdapter.err" "$plist"
    plutil -insert EnvironmentVariables -json \
        "{\"ALGO_AUSTIN_ADHOC_TEST\":\"1\",\"ALGO_AUSTIN_TEST_AUTHORITY_KEY\":\"$TEST_PUBLIC_KEY\",\"ALGO_AUSTIN_TEST_STORE\":\"$store\"}" \
        "$plist"
    plutil -lint "$plist" >/dev/null

    launchctl bootout "$domain/$service" >/dev/null 2>&1 || true
    launchctl bootstrap "$domain" "$plist"
    trap "rc=\$?; launchctl bootout '$domain/$service' >/dev/null 2>&1 || true; rm -rf '$temporary'; exit \$rc" EXIT INT TERM

    local response
    local relay_status
    set +e
    response="$(ALGO_AUSTIN_ADHOC_TEST=1 "$CONTENTS/Helpers/austin-relay" --probe \
        2> "$temporary/AustinRelay.err")"
    relay_status=$?
    set -e
    if [[ "$relay_status" != "0" ]]; then
        print -u2 -- "austin probe: $response"
        if [[ -s "$temporary/AustinAdapter.err" ]]; then
            sed -n '1,8p' "$temporary/AustinAdapter.err" >&2
        fi
        fail "xpc_probe_process_$relay_status"
    fi
    [[ "$response" == *'"reason_code":"xpc_authenticated"'* ]] \
        || fail "xpc_probe_failed"

    local printed pid
    printed="$(launchctl print "$domain/$service")"
    pid="$(print -r -- "$printed" | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)$/\1/p' | head -n 1)"
    [[ -n "$pid" ]] || fail "adapter_pid_missing"
    if lsof -nP -a -p "$pid" -iTCP -iUDP 2>/dev/null | grep -q .; then
        fail "adapter_network_socket"
    fi

    print -r -- \
        "{\"adapter_network_sockets\":0,\"peer_authentication\":\"adhoc_debug_only\",\"status\":\"passed\"}"
}

probe_neon() {
    [[ "$CONFIGURATION" == "debug" ]] || fail "neon_probe_requires_debug"
    [[ "${AUSTIN_DEVELOPER_ID_IDENTITY:--}" == "-" ]] \
        || fail "neon_probe_requires_adhoc"
    build_bundle

    local temporary
    temporary="$(mktemp -d "${TMPDIR:-/tmp}/NeonNativeHost.XXXXXX")"
    trap "rc=\$?; rm -rf '$temporary'; exit \$rc" EXIT INT TERM
    local host="$CONTENTS/Helpers/neon-native-host"

    probe_neon_case() {
        local label="$1"
        local expected_reason="$2"
        shift 2
        local output="$temporary/$label.out"
        local error="$temporary/$label.err"
        local exit_code
        set +e
        "$host" "$@" </dev/null >"$output" 2>"$error"
        exit_code=$?
        set -e
        [[ "$exit_code" == "78" ]] || fail "neon_probe_exit"
        [[ ! -s "$output" ]] || fail "neon_probe_stdout"
        [[ "$(<"$error")" == "neon native host: $expected_reason" ]] \
            || fail "neon_probe_reason"
    }

    probe_neon_case "no_origin" "extension_origin_rejected"
    probe_neon_case \
        "wrong_origin" \
        "extension_origin_rejected" \
        "chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/"
    probe_neon_case "sealed_origin" "protocol_disabled" "$TEST_NEON_EXTENSION_ORIGIN"

    rm -rf "$temporary"
    trap - EXIT INT TERM
    print -r -- \
        '{"cases":3,"exit_code":78,"protocol":"disabled","status":"passed","stdout_bytes":0}'
}

probe_migration() {
    [[ "$CONFIGURATION" == "debug" ]] || fail "migration_probe_requires_debug"
    [[ "${AUSTIN_DEVELOPER_ID_IDENTITY:--}" == "-" ]] \
        || fail "migration_probe_requires_adhoc"
    build_bundle

    local temporary
    temporary="$(mktemp -d "${TMPDIR:-/tmp}/AustinMigration.XXXXXX")"
    trap "rc=\$?; rm -rf '$temporary'; exit \$rc" EXIT INT TERM
    local output="$temporary/AdaMigration.out"
    local error="$temporary/AustinMigration.err"
    local exit_code
    set +e
    print -rn -- \
        '{"nonce":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","protocol_version":1}' \
        | "$CONTENTS/Helpers/austin-credential-migrator" >"$output" 2>"$error"
    exit_code=$?
    set -e
    [[ "$exit_code" == "78" ]] || fail "migration_probe_exit"
    [[ ! -s "$error" ]] || fail "migration_probe_stderr"
    [[ "$(<"$output")" == \
        '{"protocol_version":1,"reason_code":"credential_identity","status":"blocked"}' ]] \
        || fail "migration_probe_reason"

    rm -rf "$temporary"
    trap - EXIT INT TERM
    print -r -- \
        '{"exit_code":78,"keychain_query":"not_reached","status":"passed","stdout_content":"structural"}'
}

probe_readiness() {
    [[ "$CONFIGURATION" == "debug" ]] || fail "readiness_probe_requires_debug"
    [[ "${AUSTIN_DEVELOPER_ID_IDENTITY:--}" == "-" ]] \
        || fail "readiness_probe_requires_adhoc"

    swift build \
        --package-path "$PACKAGE" \
        --configuration "$CONFIGURATION" \
        --product austin-readiness-probe
    local bin_path
    bin_path="$(swift build \
        --package-path "$PACKAGE" \
        --configuration "$CONFIGURATION" \
        --show-bin-path)"
    local probe="$bin_path/austin-readiness-probe"
    [[ -x "$probe" && ! -L "$probe" ]] || fail "readiness_probe_binary_missing"
    codesign \
        --force \
        --sign - \
        --identifier "com.algo-cli.austin.readiness-probe" \
        --options runtime \
        "$probe"
    "$probe"
}

run_local_test() {
    [[ "$CONFIGURATION" == "debug" ]] || fail "local_test_requires_debug"
    [[ "${AUSTIN_DEVELOPER_ID_IDENTITY:--}" == "-" ]] \
        || fail "local_test_requires_adhoc"

    # Each probe runs in a child process so its EXIT trap removes temporary
    # LaunchAgent state before the next probe begins. The inherited build-lock
    # marker keeps the children inside this process's single exclusive lease.
    local probe_command
    for probe_command in probe neon-probe migration-probe readiness-probe audit; do
        AUSTIN_BUILD_LOCK_HELD=1 "$SCRIPT" "$probe_command"
    done

    print -r -- \
        '{"activation":"disabled","installation":"none","mode":"adhoc_debug","persistent_runtime_writes":0,"status":"passed","tcc_prompts":0}'
}

case "$COMMAND" in
    build)
        build_bundle
        print -r -- "$BUNDLE"
        ;;
    probe)
        probe_xpc
        ;;
    neon-probe)
        probe_neon
        ;;
    migration-probe)
        probe_migration
        ;;
    readiness-probe)
        probe_readiness
        ;;
    local-test)
        run_local_test
        ;;
    audit)
        [[ -d "$BUNDLE" ]] || fail "bundle_missing"
        "$ROOT/.venv/bin/python" "$ROOT/scripts/austin_native_package_audit.py" --bundle "$BUNDLE"
        ;;
    clean)
        swift package --package-path "$PACKAGE" clean
        rm -rf "$STAGE_ROOT"
        ;;
    *)
        fail "unknown_command"
        ;;
esac
