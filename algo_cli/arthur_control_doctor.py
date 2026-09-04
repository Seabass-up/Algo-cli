"""Read-only, content-free readiness doctor for disabled control surfaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Any


from .arthur_control_readiness import (
    CHECK_SPECS,
    CheckStatus,
    ConnectionState,
    ControlComponent,
    ControlReadinessReport,
    GrantState,
    PairingState,
    ProtocolState,
    make_checks,
    uninstalled_report,
)
from .oliver_control_installation import (
    AUSTIN_APP_BUNDLE_NAME,
    AUSTIN_SERVICE_LABEL,
)


_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
_HARDENED_RUNTIME_RE = re.compile(
    r"^CodeDirectory\b[^\r\n]*\bflags=[^\r\n]*\bruntime\b",
    re.MULTILINE,
)
_APP_GROUP = "group.com.algo-cli.control"
_SERVICE_NAME = AUSTIN_SERVICE_LABEL
_NATIVE_PROBE_KEYS = frozenset(
    {
        "accessibility_permission",
        "apple_events_finder_permission",
        "apple_events_system_settings_permission",
        "control_protocol_enabled",
        "post_event_permission",
        "screen_recording_permission",
        "system_picker_available",
    }
)
_PERMISSION_OBSERVATIONS = frozenset(
    {
        "granted",
        "missing",
        "denied",
        "not_determined",
        "target_unavailable",
        "unknown",
    }
)
_DEFINITELY_MISSING_PERMISSION = frozenset(
    {"missing", "denied", "not_determined"}
)
_MAX_NATIVE_PROBE_BYTES = 4_096


def _status(passed: bool, failure: str) -> tuple[CheckStatus, str]:
    return (CheckStatus.PASS, "verified") if passed else (CheckStatus.FAIL, failure)


def _unknown(reason: str) -> tuple[CheckStatus, str]:
    return (CheckStatus.UNKNOWN, reason)


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def decode_native_readiness_probe(payload: bytes) -> dict[str, Any]:
    """Validate the signed adapter's exact, canonical, content-free reply."""

    if type(payload) is not bytes or not 0 < len(payload) <= _MAX_NATIVE_PROBE_BYTES:
        raise ValueError("native_probe_size")
    try:
        row = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("native_probe_number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("native_probe_json") from error
    if type(row) is not dict or set(row) != {
        "protocol_version",
        "readiness",
        "reason_code",
        "status",
    }:
        raise ValueError("native_probe_schema")
    if (
        type(row["protocol_version"]) is not int
        or row["protocol_version"] != 1
        or row["status"] != "succeeded"
        or type(row["status"]) is not str
        or row["reason_code"] != "readiness_observed"
        or type(row["reason_code"]) is not str
    ):
        raise ValueError("native_probe_identity")
    readiness = row["readiness"]
    if type(readiness) is not dict or set(readiness) != _NATIVE_PROBE_KEYS:
        raise ValueError("native_probe_readiness_schema")
    for key in (
        "accessibility_permission",
        "apple_events_finder_permission",
        "apple_events_system_settings_permission",
        "post_event_permission",
        "screen_recording_permission",
    ):
        value = readiness[key]
        if type(value) is not str or value not in _PERMISSION_OBSERVATIONS:
            raise ValueError("native_probe_permission")
    for key in ("control_protocol_enabled", "system_picker_available"):
        if type(readiness[key]) is not bool:
            raise ValueError("native_probe_boolean")
    canonical = json.dumps(
        row,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if canonical != payload:
        raise ValueError("native_probe_canonical")
    return dict(readiness)


def _permission_status(value: str, reason: str) -> tuple[CheckStatus, str]:
    if value == "granted":
        return (CheckStatus.PASS, "verified")
    if value in _DEFINITELY_MISSING_PERMISSION:
        return (CheckStatus.FAIL, reason)
    return (CheckStatus.UNKNOWN, reason + "_unverified")


def has_hardened_runtime(details: str) -> bool:
    """Parse the actual `codesign -d --verbose=4` CodeDirectory line."""

    return type(details) is str and bool(_HARDENED_RUNTIME_RE.search(details))


def _private_regular(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_nlink != 1:
        return False
    if hasattr(os, "getuid") and value.st_uid not in {0, os.getuid()}:
        return False
    return not bool(value.st_mode & 0o022)


def _bundle_directory(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
        return False
    if hasattr(os, "getuid") and value.st_uid not in {0, os.getuid()}:
        return False
    return not bool(value.st_mode & 0o022)


def _run(command: tuple[str, ...], *, timeout: float = 15.0) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return (False, "")
    output = completed.stdout[:131_072].decode("utf-8", errors="replace")
    return (completed.returncode == 0, output)


def _regular_file_identity(path: Path) -> tuple[int, int, int, str] | None:
    if not _private_regular(path):
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    digest = hashlib.sha256()
    try:
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_size <= 0
            or value.st_size > 67_108_864
            or (hasattr(os, "getuid") and value.st_uid not in {0, os.getuid()})
            or value.st_mode & 0o022
        ):
            return None
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            digest.update(chunk)
        return (value.st_dev, value.st_ino, value.st_size, digest.hexdigest())
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _stop_native_probe(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _capture_native_probe(
    command: tuple[str, ...],
    environment: dict[str, str],
    *,
    timeout: float = 15.0,
) -> tuple[int, bytes] | None:
    """Capture a content-free probe without ever buffering unbounded output."""

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
    except OSError:
        return None
    stdout_pipe = process.stdout
    stderr_pipe = process.stderr
    if stdout_pipe is None or stderr_pipe is None:
        _stop_native_probe(process)
        return None

    selector = selectors.DefaultSelector()
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        for pipe, label in ((stdout_pipe, "stdout"), (stderr_pipe, "stderr")):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, label)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            events = selector.select(remaining)
            if not events:
                return None
            for key, _mask in events:
                try:
                    chunk = os.read(
                        key.fd,
                        min(8_192, _MAX_NATIVE_PROBE_BYTES + 1 - len(output)),
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    return None
                output.extend(chunk)
                if len(output) > _MAX_NATIVE_PROBE_BYTES:
                    return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return None
        return (return_code, bytes(output))
    except OSError:
        return None
    finally:
        selector.close()
        _stop_native_probe(process)
        stdout_pipe.close()
        stderr_pipe.close()


def _run_native_readiness_probe(relay: Path) -> dict[str, Any] | None:
    before = _regular_file_identity(relay)
    if before is None:
        return None
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DYLD_") and not key.startswith("ALGO_AUSTIN_")
    }
    captured = _capture_native_probe(
        (str(relay), "--readiness-probe"),
        environment,
    )
    after = _regular_file_identity(relay)
    if captured is None or captured[0] != 0 or before != after:
        return None
    try:
        return decode_native_readiness_probe(captured[1])
    except ValueError:
        return None


def _entitlements(path: Path) -> dict[str, Any] | None:
    ok, output = _run(("/usr/bin/codesign", "-d", "--entitlements", ":-", str(path)))
    if not ok:
        return None
    start = output.find("<?xml")
    if start < 0:
        start = output.find("<plist")
    if start < 0:
        return None
    try:
        value = plistlib.loads(output[start:].encode("utf-8"))
    except plistlib.InvalidFileException:
        return None
    return value if type(value) is dict else None


def _launch_agent_valid(app: Path) -> bool:
    path = Path.home() / "Library" / "LaunchAgents" / f"{_SERVICE_NAME}.plist"
    if not _private_regular(path):
        return False
    try:
        row = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return False
    adapter = app / "Contents" / "Helpers" / "austin-tcc-adapter"
    return type(row) is dict and row == {
        "Label": _SERVICE_NAME,
        "LimitLoadToSessionType": "Aqua",
        "MachServices": {_SERVICE_NAME: True},
        "ProcessType": "Interactive",
        "ProgramArguments": [str(adapter)],
        "RunAtLoad": False,
        "StandardErrorPath": "/dev/null",
        "StandardOutPath": "/dev/null",
        "ThrottleInterval": 30,
    }


def _austin_report(
    app: Path,
    *,
    live_native_probe: bool = False,
) -> ControlReadinessReport:
    if sys.platform != "darwin" or not app.exists():
        return uninstalled_report(ControlComponent.MACOS_NATIVE)

    statuses = {
        name: _unknown("not_checked")
        for name in CHECK_SPECS[ControlComponent.MACOS_NATIVE]
    }
    expected = {
        "app": app / "Contents" / "MacOS" / "austin-control",
        "relay": app / "Contents" / "Helpers" / "austin-relay",
        "adapter": app / "Contents" / "Helpers" / "austin-tcc-adapter",
        "credential_migrator": app
        / "Contents"
        / "Helpers"
        / "austin-credential-migrator",
        "neon": app / "Contents" / "Helpers" / "neon-native-host",
        "authority": app / "Contents" / "Resources" / "AustinAuthorityPublicKey.bin",
    }
    layout_ok = _bundle_directory(app) and all(_private_regular(path) for path in expected.values())
    if layout_ok:
        try:
            layout_ok = len(expected["authority"].read_bytes()) == 32
        except OSError:
            layout_ok = False
    statuses["install_path"] = _status(layout_ok, "invalid_install_path")

    signature_ok, display = _run(
        ("/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=4", str(app))
    )
    statuses["bundle_signature"] = _status(signature_ok, "invalid_bundle_signature")

    display_ok, display_details = _run(
        ("/usr/bin/codesign", "-d", "--verbose=4", str(app))
    )
    details = display + "\n" + display_details
    team_match = re.search(r"^TeamIdentifier=([^\s]+)$", details, re.MULTILINE)
    team_id = team_match.group(1) if team_match else ""
    team_ok = display_ok and bool(_TEAM_ID_RE.fullmatch(team_id))
    statuses["team_identity"] = _status(team_ok, "developer_id_identity_required")
    hardened = display_ok and has_hardened_runtime(details)
    statuses["hardened_runtime"] = _status(hardened, "hardened_runtime_required")

    requirement_ok, requirement = _run(("/usr/bin/codesign", "-d", "-r-", str(app)))
    designated = (
        requirement_ok
        and team_ok
        and "designated => anchor apple generic" in requirement
        and team_id in requirement
    )
    statuses["designated_requirement"] = _status(
        designated,
        "designated_requirement_invalid",
    )

    gatekeeper, _ = _run(("/usr/sbin/spctl", "--assess", "--type", "execute", str(app)))
    statuses["gatekeeper"] = _status(gatekeeper, "gatekeeper_rejected")
    notarized, _ = _run(("/usr/bin/xcrun", "stapler", "validate", str(app)))
    statuses["notarization"] = _status(notarized, "notarization_missing")

    expected_app = {
        "com.apple.security.app-sandbox": True,
        "com.apple.security.application-groups": [_APP_GROUP],
    }
    expected_adapter = {"com.apple.security.automation.apple-events": True}
    entitlement_ok = (
        _entitlements(expected["app"]) == expected_app
        and _entitlements(expected["relay"]) == expected_app
        and _entitlements(expected["adapter"]) == expected_adapter
        and _entitlements(expected["credential_migrator"]) == {}
        and _entitlements(expected["neon"]) == {}
    )
    statuses["entitlement_allowlist"] = _status(
        entitlement_ok,
        "entitlement_allowlist_invalid",
    )
    statuses["launch_agent"] = _status(
        _launch_agent_valid(app),
        "launch_agent_missing_or_invalid",
    )

    # These require a signed live app/adapter exchange. The networked Python
    # process must not infer TCC or peer authority from filesystem presence.
    statuses["xpc_peer_identity"] = _unknown("live_signed_probe_required")
    statuses["accessibility_permission"] = _unknown("live_tcc_probe_required")
    statuses["screen_recording_permission"] = _unknown("live_tcc_probe_required")
    statuses["post_event_permission"] = _unknown("live_tcc_probe_required")
    statuses["apple_events_permission"] = _unknown("live_tcc_probe_required")
    statuses["xpc_connection"] = _unknown("live_signed_probe_required")
    statuses["screen_capture_picker"] = _unknown("live_tcc_probe_required")
    statuses["dispatcher_enabled"] = _unknown("live_signed_probe_required")

    pairing = PairingState.UNPAIRED
    connection = ConnectionState.DISCONNECTED
    protocol = ProtocolState.UNKNOWN
    static_trust_checks = (
        "install_path",
        "bundle_signature",
        "team_identity",
        "designated_requirement",
        "hardened_runtime",
        "gatekeeper",
        "notarization",
        "entitlement_allowlist",
        "launch_agent",
    )
    if live_native_probe and all(
        statuses[name][0] is CheckStatus.PASS for name in static_trust_checks
    ):
        evidence = _run_native_readiness_probe(expected["relay"])
        if evidence is None:
            statuses["xpc_connection"] = (
                CheckStatus.FAIL,
                "live_native_probe_failed",
            )
        else:
            statuses["xpc_peer_identity"] = (CheckStatus.PASS, "verified")
            statuses["xpc_connection"] = (CheckStatus.PASS, "verified")
            statuses["accessibility_permission"] = _permission_status(
                evidence["accessibility_permission"],
                "accessibility_permission_missing",
            )
            statuses["screen_recording_permission"] = _permission_status(
                evidence["screen_recording_permission"],
                "screen_recording_permission_missing",
            )
            statuses["post_event_permission"] = _permission_status(
                evidence["post_event_permission"],
                "post_event_permission_missing",
            )
            apple_event_states = {
                evidence["apple_events_finder_permission"],
                evidence["apple_events_system_settings_permission"],
            }
            if apple_event_states == {"granted"}:
                statuses["apple_events_permission"] = (
                    CheckStatus.PASS,
                    "verified",
                )
            elif apple_event_states & _DEFINITELY_MISSING_PERMISSION:
                statuses["apple_events_permission"] = (
                    CheckStatus.FAIL,
                    "apple_events_permission_missing",
                )
            else:
                statuses["apple_events_permission"] = (
                    CheckStatus.UNKNOWN,
                    "apple_events_permission_unverified",
                )
            statuses["screen_capture_picker"] = _status(
                evidence["system_picker_available"],
                "system_picker_unavailable",
            )
            statuses["dispatcher_enabled"] = _status(
                evidence["control_protocol_enabled"],
                "control_protocol_disabled",
            )
            pairing = PairingState.PAIRED
            connection = ConnectionState.CONNECTED
            protocol = ProtocolState.COMPATIBLE

    return ControlReadinessReport(
        component=ControlComponent.MACOS_NATIVE,
        installed=True,
        pairing=pairing,
        connection=connection,
        grant=GrantState.NONE,
        protocol=protocol,
        checks=make_checks(ControlComponent.MACOS_NATIVE, statuses),
    )


def run_doctor(
    *,
    austin_app: Path,
    live_native_probe: bool = False,
) -> dict[str, Any]:
    reports = (
        _austin_report(austin_app, live_native_probe=live_native_probe),
        uninstalled_report(ControlComponent.CHROME_SELECTED_TAB),
        uninstalled_report(ControlComponent.MANAGED_BROWSER),
    )
    return {
        "schema_version": 1,
        "reports": [report.to_dict() for report in reports],
        "overall_ready": all(report.state.value in {"ready_idle", "connected_no_grant", "active"} for report in reports),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--austin-app",
        type=Path,
        default=Path("/Applications") / AUSTIN_APP_BUNDLE_NAME,
        help="Exact installed Austin app bundle to verify",
    )
    parser.add_argument(
        "--live-native-probe",
        action="store_true",
        help=(
            "Launch the already trusted installed relay and collect only "
            "non-prompting permission preflights; never grants permission"
        ),
    )
    args = parser.parse_args(argv)
    result = run_doctor(
        austin_app=args.austin_app,
        live_native_probe=args.live_native_probe,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["overall_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
