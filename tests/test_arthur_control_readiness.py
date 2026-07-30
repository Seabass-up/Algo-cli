from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from algo_cli import arthur_control_doctor
from algo_cli.arthur_control_readiness import (
    CHECK_SPECS,
    CheckCategory,
    CheckStatus,
    ConnectionState,
    ControlComponent,
    ControlReadinessReport,
    ControlReadinessState,
    GrantState,
    PairingState,
    ProtocolState,
    ReadinessCheck,
    ReadinessEvidenceRejected,
    make_checks,
    uninstalled_report,
)
from algo_cli.arthur_control_doctor import has_hardened_runtime, run_doctor
from algo_cli.oliver_control_installation import AUSTIN_APP_BUNDLE_NAME


ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Austin readiness probes use POSIX process and descriptor semantics",
)


def _native_probe_payload(**readiness_overrides: object) -> bytes:
    readiness: dict[str, object] = {
        "accessibility_permission": "granted",
        "apple_events_finder_permission": "granted",
        "apple_events_system_settings_permission": "granted",
        "control_protocol_enabled": False,
        "post_event_permission": "missing",
        "screen_recording_permission": "not_determined",
        "system_picker_available": True,
    }
    readiness.update(readiness_overrides)
    return json.dumps(
        {
            "protocol_version": 1,
            "readiness": readiness,
            "reason_code": "readiness_observed",
            "status": "succeeded",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def test_hardened_runtime_parser_matches_real_codesign_shape_only() -> None:
    assert has_hardened_runtime(
        "CodeDirectory v=20500 size=1244 flags=0x10000(runtime) hashes=28+7"
    )
    assert has_hardened_runtime(
        "Executable=/Applications/Example.app\n"
        "CodeDirectory v=20500 flags=0x10002(adhoc,runtime) hashes=1+1\n"
    )
    assert not has_hardened_runtime("flags=0x10000(runtime)")
    assert not has_hardened_runtime(
        "CodeDirectory v=20500 size=1244 flags=0x2(adhoc) hashes=28+7"
    )


def test_native_readiness_probe_schema_is_exact_canonical_and_content_free() -> None:
    decoded = arthur_control_doctor.decode_native_readiness_probe(
        _native_probe_payload()
    )

    assert decoded == {
        "accessibility_permission": "granted",
        "apple_events_finder_permission": "granted",
        "apple_events_system_settings_permission": "granted",
        "control_protocol_enabled": False,
        "post_event_permission": "missing",
        "screen_recording_permission": "not_determined",
        "system_picker_available": True,
    }


@pytest.mark.parametrize(
    "payload,reason",
    [
        (_native_probe_payload(control_protocol_enabled=1), "native_probe_boolean"),
        (_native_probe_payload(accessibility_permission=True), "native_probe_permission"),
        (_native_probe_payload(accessibility_permission="invented"), "native_probe_permission"),
        (_native_probe_payload() + b"\n", "native_probe_canonical"),
        (
            b'{"protocol_version":1,"protocol_version":1,"readiness":{},'
            b'"reason_code":"readiness_observed","status":"succeeded"}',
            "native_probe_json",
        ),
    ],
)
def test_native_readiness_probe_rejects_schema_and_encoding_bypasses(
    payload: bytes,
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        arthur_control_doctor.decode_native_readiness_probe(payload)


def test_native_readiness_probe_runner_sanitizes_environment_and_rechecks_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    relay = tmp_path / "austin-relay"
    relay.write_bytes(b"signed-fixture")
    captured: dict[str, object] = {}
    identity = (1, 2, len(b"signed-fixture"), "a" * 64)
    monkeypatch.setattr(
        arthur_control_doctor,
        "_regular_file_identity",
        lambda _path: identity,
    )
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "forbidden")
    monkeypatch.setenv("ALGO_AUSTIN_ADHOC_TEST", "1")

    def fake_capture(command, environment, *, timeout=15.0):
        del timeout
        captured["command"] = command
        captured["environment"] = environment
        return (0, _native_probe_payload())

    monkeypatch.setattr(
        arthur_control_doctor,
        "_capture_native_probe",
        fake_capture,
    )

    assert arthur_control_doctor._run_native_readiness_probe(relay) is not None
    assert captured["command"] == (str(relay), "--readiness-probe")
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert "DYLD_INSERT_LIBRARIES" not in environment
    assert "ALGO_AUSTIN_ADHOC_TEST" not in environment


@pytest.mark.parametrize("capture", [None, (0, _native_probe_payload())])
def test_native_readiness_probe_runner_rejects_capture_and_identity_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capture: tuple[int, bytes] | None,
) -> None:
    relay = tmp_path / "austin-relay"
    relay.write_bytes(b"signed-fixture")
    before = (1, 2, len(b"signed-fixture"), "a" * 64)
    after = before if capture is None else (1, 3, 14, "b" * 64)
    identities = iter((before, after))
    monkeypatch.setattr(
        arthur_control_doctor,
        "_regular_file_identity",
        lambda _path: next(identities),
    )
    monkeypatch.setattr(
        arthur_control_doctor,
        "_capture_native_probe",
        lambda _command, _environment: capture,
    )

    assert arthur_control_doctor._run_native_readiness_probe(relay) is None


@pytest.mark.parametrize(
    "body",
    [
        "sys.stdout.buffer.write(b'x' * 4097)",
        "sys.stderr.buffer.write(b'unexpected')",
    ],
)
def test_native_probe_capture_rejects_output_during_capture(
    tmp_path: Path,
    body: str,
) -> None:
    probe = tmp_path / "bounded-probe"
    probe.write_text(
        f"#!{sys.executable}\nimport sys\n{body}\n",
        encoding="utf-8",
    )
    probe.chmod(0o700)

    assert (
        arthur_control_doctor._capture_native_probe(
            (str(probe),),
            {},
            timeout=2.0,
        )
        is None
    )


def _trusted_austin_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    monkeypatch.setattr(arthur_control_doctor.sys, "platform", "darwin")
    app = tmp_path / "Algo CLI Control.app"
    contents = app / "Contents"
    paths = (
        contents / "MacOS" / "austin-control",
        contents / "Helpers" / "austin-relay",
        contents / "Helpers" / "austin-tcc-adapter",
        contents / "Helpers" / "austin-credential-migrator",
        contents / "Helpers" / "neon-native-host",
        contents / "Resources" / "AustinAuthorityPublicKey.bin",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (32 if path.name.endswith(".bin") else 1))
    monkeypatch.setattr(arthur_control_doctor, "_bundle_directory", lambda _path: True)
    monkeypatch.setattr(arthur_control_doctor, "_private_regular", lambda _path: True)
    monkeypatch.setattr(arthur_control_doctor, "_launch_agent_valid", lambda _path: True)

    def fake_run(command, *, timeout=15.0):
        del timeout
        if command[:3] == ("/usr/bin/codesign", "-d", "--verbose=4"):
            return (
                True,
                "CodeDirectory v=20500 flags=0x10000(runtime) hashes=1+1\n"
                "TeamIdentifier=ABCDEFGHIJ\n",
            )
        if command[:3] == ("/usr/bin/codesign", "-d", "-r-"):
            return (
                True,
                "designated => anchor apple generic and identifier "
                '"com.algo-cli.austin.control" and certificate leaf[subject.OU] = '
                '"ABCDEFGHIJ"',
            )
        return (True, "")

    monkeypatch.setattr(arthur_control_doctor, "_run", fake_run)

    def fake_entitlements(path: Path):
        if path.name in {"austin-control", "austin-relay"}:
            return {
                "com.apple.security.app-sandbox": True,
                "com.apple.security.application-groups": ["group.com.algo-cli.control"],
            }
        if path.name == "austin-tcc-adapter":
            return {"com.apple.security.automation.apple-events": True}
        return {}

    monkeypatch.setattr(arthur_control_doctor, "_entitlements", fake_entitlements)
    return app


def test_live_native_probe_maps_each_state_without_enabling_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _trusted_austin_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        arthur_control_doctor,
        "_run_native_readiness_probe",
        lambda _relay: arthur_control_doctor.decode_native_readiness_probe(
            _native_probe_payload()
        ),
    )

    report = arthur_control_doctor._austin_report(
        app,
        live_native_probe=True,
    )
    checks = {check.name: check for check in report.checks}

    assert report.pairing is PairingState.PAIRED
    assert report.connection is ConnectionState.CONNECTED
    assert report.protocol is ProtocolState.COMPATIBLE
    assert report.state is ControlReadinessState.PAIRED_MISSING_PERMISSIONS
    assert checks["xpc_peer_identity"].status is CheckStatus.PASS
    assert checks["xpc_connection"].status is CheckStatus.PASS
    assert checks["accessibility_permission"].status is CheckStatus.PASS
    assert checks["post_event_permission"].status is CheckStatus.FAIL
    assert checks["screen_recording_permission"].status is CheckStatus.FAIL
    assert checks["apple_events_permission"].status is CheckStatus.PASS
    assert checks["screen_capture_picker"].status is CheckStatus.PASS
    assert checks["dispatcher_enabled"].status is CheckStatus.FAIL
    assert checks["dispatcher_enabled"].reason_code == "control_protocol_disabled"


def test_live_native_probe_never_executes_before_static_trust_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _trusted_austin_fixture(monkeypatch, tmp_path)
    original_run = arthur_control_doctor._run

    def gatekeeper_rejected(command, *, timeout=15.0):
        if command[:3] == ("/usr/sbin/spctl", "--assess", "--type"):
            return (False, "")
        return original_run(command, timeout=timeout)

    monkeypatch.setattr(arthur_control_doctor, "_run", gatekeeper_rejected)
    monkeypatch.setattr(
        arthur_control_doctor,
        "_run_native_readiness_probe",
        lambda _relay: pytest.fail("live probe ran before static trust passed"),
    )

    report = arthur_control_doctor._austin_report(app, live_native_probe=True)
    checks = {check.name: check for check in report.checks}
    assert checks["gatekeeper"].status is CheckStatus.FAIL
    assert checks["xpc_connection"].status is CheckStatus.UNKNOWN
    assert report.pairing is PairingState.UNPAIRED


def _statuses(
    component: ControlComponent,
    *,
    identity: CheckStatus = CheckStatus.PASS,
    permission: CheckStatus = CheckStatus.PASS,
    availability: CheckStatus = CheckStatus.PASS,
) -> dict[str, tuple[CheckStatus, str]]:
    selected = {
        CheckCategory.IDENTITY: identity,
        CheckCategory.PERMISSION: permission,
        CheckCategory.AVAILABILITY: availability,
    }
    return {
        name: (
            selected[spec.category],
            "verified" if selected[spec.category] is CheckStatus.PASS else f"{spec.category.value}_unavailable",
        )
        for name, spec in CHECK_SPECS[component].items()
    }


def _report(
    component: ControlComponent = ControlComponent.MACOS_NATIVE,
    *,
    pairing: PairingState = PairingState.PAIRED,
    connection: ConnectionState = ConnectionState.DISCONNECTED,
    grant: GrantState = GrantState.NONE,
    protocol: ProtocolState = ProtocolState.COMPATIBLE,
    identity: CheckStatus = CheckStatus.PASS,
    permission: CheckStatus = CheckStatus.PASS,
    availability: CheckStatus = CheckStatus.PASS,
) -> ControlReadinessReport:
    return ControlReadinessReport(
        component=component,
        installed=True,
        pairing=pairing,
        connection=connection,
        grant=grant,
        protocol=protocol,
        checks=make_checks(
            component,
            _statuses(
                component,
                identity=identity,
                permission=permission,
                availability=availability,
            ),
        ),
    )


def test_all_required_readiness_states_are_distinct_and_derived() -> None:
    assert uninstalled_report(ControlComponent.MACOS_NATIVE).state is ControlReadinessState.NOT_INSTALLED
    assert (
        _report(pairing=PairingState.UNPAIRED, protocol=ProtocolState.UNKNOWN).state
        is ControlReadinessState.INSTALLED_UNPAIRED
    )
    assert _report(permission=CheckStatus.FAIL).state is ControlReadinessState.PAIRED_MISSING_PERMISSIONS
    assert _report().state is ControlReadinessState.READY_IDLE
    assert (
        _report(connection=ConnectionState.CONNECTED).state
        is ControlReadinessState.CONNECTED_NO_GRANT
    )
    assert (
        _report(connection=ConnectionState.CONNECTED, grant=GrantState.ACTIVE).state
        is ControlReadinessState.ACTIVE
    )
    assert _report(identity=CheckStatus.UNKNOWN).state is ControlReadinessState.DEGRADED
    assert (
        _report(protocol=ProtocolState.VERSION_MISMATCH).state
        is ControlReadinessState.VERSION_MISMATCH
    )


def test_availability_is_required_only_for_a_live_connection() -> None:
    assert _report(availability=CheckStatus.UNKNOWN).state is ControlReadinessState.READY_IDLE
    assert (
        _report(
            connection=ConnectionState.CONNECTED,
            availability=CheckStatus.UNKNOWN,
        ).state
        is ControlReadinessState.DEGRADED
    )


@pytest.mark.parametrize("grant", [GrantState.NONE, GrantState.EXPIRED, GrantState.REVOKED])
def test_non_active_grants_never_report_active(grant: GrantState) -> None:
    report = _report(connection=ConnectionState.CONNECTED, grant=grant)
    assert report.state is ControlReadinessState.CONNECTED_NO_GRANT


def test_report_round_trip_recomputes_state_instead_of_trusting_input() -> None:
    report = _report(connection=ConnectionState.CONNECTED, grant=GrantState.ACTIVE)
    assert ControlReadinessReport.from_dict(report.to_dict()) == report
    forged = report.to_dict()
    forged["state"] = "ready_idle"
    with pytest.raises(ReadinessEvidenceRejected, match="derived_state"):
        ControlReadinessReport.from_dict(forged)


def test_missing_extra_duplicate_and_wrong_category_checks_fail_closed() -> None:
    component = ControlComponent.CHROME_SELECTED_TAB
    statuses = _statuses(component)
    statuses.pop("allowed_origin")
    with pytest.raises(ReadinessEvidenceRejected, match="checks_complete"):
        make_checks(component, statuses)

    statuses = _statuses(component)
    statuses["invented_check"] = (CheckStatus.PASS, "verified")
    with pytest.raises(ReadinessEvidenceRejected, match="checks_complete"):
        make_checks(component, statuses)

    checks = list(make_checks(component, _statuses(component)))
    selected = next(index for index, check in enumerate(checks) if check.name == "allowed_origin")
    checks[selected] = ReadinessCheck(
        name=checks[selected].name,
        category=CheckCategory.PERMISSION,
        status=CheckStatus.PASS,
        reason_code="verified",
    )
    with pytest.raises(ReadinessEvidenceRejected, match="check_category_binding"):
        ControlReadinessReport(
            component=component,
            installed=True,
            pairing=PairingState.UNPAIRED,
            connection=ConnectionState.DISCONNECTED,
            grant=GrantState.NONE,
            protocol=ProtocolState.UNKNOWN,
            checks=tuple(checks),
        )


def test_contradictory_install_pairing_connection_and_grant_states_reject() -> None:
    checks = uninstalled_report(ControlComponent.MANAGED_BROWSER).checks
    with pytest.raises(ReadinessEvidenceRejected, match="not_installed_state"):
        ControlReadinessReport(
            component=ControlComponent.MANAGED_BROWSER,
            installed=False,
            pairing=PairingState.PAIRED,
            connection=ConnectionState.DISCONNECTED,
            grant=GrantState.NONE,
            protocol=ProtocolState.UNKNOWN,
            checks=checks,
        )
    with pytest.raises(ReadinessEvidenceRejected, match="active_without_connection"):
        _report(grant=GrantState.ACTIVE)


def test_check_reason_cannot_claim_verified_on_failure() -> None:
    with pytest.raises(ReadinessEvidenceRejected, match="check_reason"):
        ReadinessCheck(
            name="bundle_signature",
            category=CheckCategory.IDENTITY,
            status=CheckStatus.FAIL,
            reason_code="verified",
        )


def test_doctor_reports_absent_components_without_paths_or_private_content(tmp_path: Path) -> None:
    result = run_doctor(austin_app=tmp_path / "missing.app")

    assert result["overall_ready"] is False
    assert [row["component"] for row in result["reports"]] == [
        "macos_native",
        "chrome_selected_tab",
        "managed_browser",
    ]
    assert all(row["state"] == "not_installed" for row in result["reports"])
    serialized = json.dumps(result, sort_keys=True)
    assert str(tmp_path) not in serialized


def test_doctor_cli_is_nonzero_and_structural_when_foundation_is_absent(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/arthur_control_doctor.py",
            "--austin-app",
            str(tmp_path / "missing.app"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["overall_ready"] is False
    assert {row["state"] for row in payload["reports"]} == {"not_installed"}


def test_doctor_default_matches_the_staged_canonical_bundle_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Path] = {}

    def fake_doctor(*, austin_app: Path, live_native_probe: bool):
        captured["path"] = austin_app
        captured["live"] = live_native_probe
        return {"overall_ready": False, "reports": []}

    monkeypatch.setattr(arthur_control_doctor, "run_doctor", fake_doctor)

    assert arthur_control_doctor.main([]) == 1
    assert captured["path"] == Path("/Applications") / AUSTIN_APP_BUNDLE_NAME
    assert captured["live"] is False
    assert json.loads(capsys.readouterr().out)["overall_ready"] is False

    assert arthur_control_doctor.main(["--live-native-probe"]) == 1
    assert captured["live"] is True
