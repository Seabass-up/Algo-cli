from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

import algo_cli.boron_browser_wrapper as wrapper_module
from algo_cli.boron_browser_wrapper import (
    BORON_CHROME_PATH,
    BORON_MAX_PIPE_MESSAGE_BYTES,
    BoronNavigationMachine,
    BoronNavigationPlan,
    BoronNavigationState,
    BoronPipeDecoder,
    BoronPipeRejected,
    decode_boron_pipe_message,
    encode_boron_pipe_message,
    install_ephemeral_xenon_ca,
    launch_boron_chrome,
)


VERSION = "151.0.7922.34"


def _plan() -> BoronNavigationPlan:
    return BoronNavigationPlan("https://example.com/path?q=1", VERSION)


def _response(command: dict, result: dict | None = None) -> dict:
    row = {"id": command["id"], "result": result or {}}
    if "sessionId" in command:
        row["sessionId"] = command["sessionId"]
    return row


def _bootstrap(machine: BoronNavigationMachine) -> dict:
    version_command = machine.start()[0]
    context_command = machine.handle(
        _response(
            version_command,
            {
                "product": f"Chrome/{VERSION}",
                "protocolVersion": "1.3",
                "revision": "ignored",
                "userAgent": "ignored",
                "jsVersion": "ignored",
            },
        )
    )[0]
    context_commands = machine.handle(
        _response(context_command, {"browserContextId": "context-a"})
    )
    download_command = next(
        command for command in context_commands if command["method"] == "Browser.setDownloadBehavior"
    )
    target_command = next(
        command for command in context_commands if command["method"] == "Target.createTarget"
    )
    attach_command = machine.handle(
        _response(target_command, {"targetId": "target-a"})
    )[0]
    page_commands = list(
        machine.handle(_response(attach_command, {"sessionId": "session-a"}))
    )
    configure_commands = [download_command, *page_commands]
    navigate: dict | None = None
    for command in reversed(configure_commands):
        emitted = machine.handle(_response(command))
        if emitted:
            assert len(emitted) == 1
            navigate = emitted[0]
    assert navigate is not None
    assert navigate["method"] == "Page.navigate"
    return navigate


def _navigate_ack(machine: BoronNavigationMachine, command: dict) -> None:
    assert machine.handle(
        _response(command, {"frameId": "frame-a", "loaderId": "loader-a"})
    ) == ()


def _frame_event(url: str = "https://example.com/final") -> dict:
    return {
        "method": "Page.frameNavigated",
        "sessionId": "session-a",
        "params": {
            "frame": {
                "id": "frame-a",
                "loaderId": "loader-a",
                "url": url,
                "mimeType": "text/html",
                "securityOrigin": "https://example.com",
            },
        },
    }


def _load_event(*, frame: str = "frame-a", loader: str = "loader-a") -> dict:
    return {
        "method": "Page.lifecycleEvent",
        "sessionId": "session-a",
        "params": {"frameId": frame, "loaderId": loader, "name": "load", "timestamp": 1},
    }


def _ca(now: datetime) -> tuple[bytes, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Algo Xenon Session CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM), certificate


def test_pipe_codec_round_trips_fragmented_and_coalesced_messages() -> None:
    first = encode_boron_pipe_message({"id": 1, "result": {}})
    second = encode_boron_pipe_message({"method": "Page.loadEventFired", "params": {}})
    decoder = BoronPipeDecoder()
    assert decoder.feed(first[:3]) == []
    assert decoder.feed(first[3:] + second) == [
        {"id": 1, "result": {}},
        {"method": "Page.loadEventFired", "params": {}},
    ]
    decoder.finish()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b'{"id":1,"id":2}', "json_duplicate_key"),
        (b'{"value":1.5}', "json_float"),
        (b'{"value":NaN}', "json_constant"),
        (b"[]", "pipe_message_object"),
        (b"\xff", "pipe_frame_utf8"),
        (b"{", "pipe_frame_json"),
    ],
)
def test_pipe_decoder_rejects_ambiguous_or_open_json(payload: bytes, reason: str) -> None:
    with pytest.raises(BoronPipeRejected, match=reason):
        decode_boron_pipe_message(payload)


def test_pipe_decoder_bounds_frames_buffers_and_truncation() -> None:
    decoder = BoronPipeDecoder()
    with pytest.raises(BoronPipeRejected, match="pipe_frame_size"):
        decoder.feed(b"a" * (BORON_MAX_PIPE_MESSAGE_BYTES + 1))
    decoder = BoronPipeDecoder()
    decoder.feed(b'{"id":1}')
    with pytest.raises(BoronPipeRejected, match="pipe_frame_truncated"):
        decoder.finish()
    with pytest.raises(BoronPipeRejected, match="pipe_frame_size"):
        encode_boron_pipe_message({"x": ["a" * 65_000 for _ in range(17)]})


def test_plan_is_canonical_https_only_and_chrome_argv_has_no_escape_surface() -> None:
    plan = _plan()
    argv = plan.chrome_argv()
    assert argv[0] == BORON_CHROME_PATH
    assert "--remote-debugging-pipe=JSON" in argv
    assert "--proxy-bypass-list=<-loopback>" in argv
    assert "--disable-quic" in argv
    assert not any("remote-debugging-port" in item for item in argv)
    assert "--no-sandbox" not in argv
    assert "--ignore-certificate-errors" not in argv
    assert argv[-1] == "about:blank"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "https://user@example.com/",
        "https://example.com:444/",
        "https://127.0.0.1/",
        "https://example.com/#fragment",
        "https://example.com",
        "https://EXAMPLE.com/",
        "https://example.com/\nnext",
    ],
)
def test_plan_rejects_noncanonical_or_dangerous_destinations(url: str) -> None:
    with pytest.raises(BoronPipeRejected):
        BoronNavigationPlan(url, VERSION)


def test_machine_verifies_only_after_ack_commit_and_matching_load() -> None:
    machine = BoronNavigationMachine(_plan())
    navigate = _bootstrap(machine)
    _navigate_ack(machine, navigate)
    assert machine.state is BoronNavigationState.NAVIGATING
    machine.handle(_frame_event())
    assert machine.state is BoronNavigationState.NAVIGATING
    machine.handle(_load_event())
    evidence = machine.evidence()
    assert evidence.state is BoronNavigationState.VERIFIED
    assert evidence.browser_major == 151
    assert evidence.command_count == 12
    assert evidence.origin_digest.startswith("sha256:")
    assert "example.com" not in repr(evidence)


def test_blank_page_and_reordered_events_cannot_false_verify() -> None:
    machine = BoronNavigationMachine(_plan())
    navigate = _bootstrap(machine)
    machine.handle(
        {
            "method": "Page.frameNavigated",
            "params": {"frame": {"id": "blank", "loaderId": "blank-loader", "url": "about:blank"}},
        }
    )
    machine.handle(
        {
            "method": "Page.lifecycleEvent",
            "params": {"frameId": "blank", "loaderId": "blank-loader", "name": "load"},
        }
    )
    assert machine.state is BoronNavigationState.NAVIGATING
    # Commit and load may arrive before the Page.navigate response. The machine
    # records them but cannot verify until the matching response arrives.
    machine.handle(_frame_event())
    machine.handle(_load_event())
    assert machine.state is BoronNavigationState.NAVIGATING
    _navigate_ack(machine, navigate)
    assert machine.evidence().state is BoronNavigationState.VERIFIED


@pytest.mark.parametrize(
    ("method", "expected_state", "reason"),
    [
        ("Page.javascriptDialogOpening", BoronNavigationState.HANDOFF, "dialog_handoff"),
        ("Page.windowOpen", BoronNavigationState.HANDOFF, "popup_handoff"),
        ("Page.fileChooserOpened", BoronNavigationState.HANDOFF, "upload_handoff"),
        ("Browser.downloadWillBegin", BoronNavigationState.HANDOFF, "download_denied"),
        ("Network.webSocketCreated", BoronNavigationState.FAILED, "websocket_denied"),
        ("Inspector.targetCrashed", BoronNavigationState.UNKNOWN, "target_crashed"),
    ],
)
def test_edge_events_fail_closed(method: str, expected_state: BoronNavigationState, reason: str) -> None:
    machine = BoronNavigationMachine(_plan())
    _bootstrap(machine)
    commands = machine.handle({"method": method, "params": {}})
    assert commands and commands[0]["method"] == "Page.stopLoading"
    evidence = machine.evidence()
    assert evidence.state is expected_state
    assert evidence.reason_code == reason


def test_origin_frame_loader_and_lifecycle_drift_reject() -> None:
    machine = BoronNavigationMachine(_plan())
    navigate = _bootstrap(machine)
    _navigate_ack(machine, navigate)
    machine.handle(_frame_event("https://other.example/final"))
    assert machine.evidence().reason_code == "origin_drift"

    machine = BoronNavigationMachine(_plan())
    navigate = _bootstrap(machine)
    _navigate_ack(machine, navigate)
    event = _frame_event()
    event["params"]["frame"]["id"] = "frame-b"
    machine.handle(event)
    assert machine.evidence().reason_code == "frame_drift"

    machine = BoronNavigationMachine(_plan())
    navigate = _bootstrap(machine)
    _navigate_ack(machine, navigate)
    machine.handle(_frame_event())
    machine.handle(_load_event(loader="stale-loader"))
    assert machine.evidence().reason_code == "lifecycle_drift"


def test_version_error_and_replayed_response_fail_closed() -> None:
    machine = BoronNavigationMachine(_plan())
    command = machine.start()[0]
    with pytest.raises(BoronPipeRejected, match="browser_version_skew"):
        machine.handle(
            _response(command, {"product": "Chrome/150.0.1.2", "protocolVersion": "1.3"})
        )

    machine = BoronNavigationMachine(_plan())
    command = machine.start()[0]
    machine.handle(
        _response(command, {"product": f"Chrome/{VERSION}", "protocolVersion": "1.3"})
    )
    with pytest.raises(BoronPipeRejected, match="cdp_response_id"):
        machine.handle(_response(command))


def test_ephemeral_ca_import_is_exact_read_back_and_pem_is_removed(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pem, certificate = _ca(now)
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        stdout = pem.decode("ascii") if argv[1] == "-L" else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    digest = install_ephemeral_xenon_ca(
        pem,
        now_ms=int(now.timestamp() * 1000),
        profile_path=tmp_path / "profile",
        runner=runner,
    )
    assert digest == "sha256:" + certificate.fingerprint(hashes.SHA256()).hex()
    assert [call[1] for call in calls] == ["-N", "-A", "-L"]
    assert not (tmp_path / "profile" / "xenon-session-ca.pem").exists()


def test_ca_rejects_non_ca_expired_and_readback_mismatch(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pem, _certificate = _ca(now)
    other_pem, _ = _ca(now)

    def mismatch(argv, **_kwargs):
        stdout = other_pem.decode("ascii") if argv[1] == "-L" else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    with pytest.raises(BoronPipeRejected, match="ca_readback"):
        install_ephemeral_xenon_ca(
            pem,
            now_ms=int(now.timestamp() * 1000),
            profile_path=tmp_path / "mismatch",
            runner=mismatch,
        )

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Not CA")])
    leaf = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )
    with pytest.raises(BoronPipeRejected, match="ca_constraints"):
        install_ephemeral_xenon_ca(
            leaf,
            now_ms=int(now.timestamp() * 1000),
            profile_path=tmp_path / "leaf",
            runner=mismatch,
        )


def test_launch_uses_only_fixed_argv_and_fd_pipe_transport(monkeypatch) -> None:
    captured: dict = {}

    def spawn(path, argv, environment, **kwargs):
        captured["path"] = path
        captured["argv"] = argv
        captured["environment"] = environment
        captured["kwargs"] = kwargs
        return 424242

    monkeypatch.setattr(wrapper_module.os, "posix_spawn", spawn)
    monkeypatch.setattr(
        wrapper_module.os,
        "waitpid",
        lambda pid, _flags: (pid, 0),
    )
    process = launch_boron_chrome(_plan())
    try:
        assert captured["path"] == BORON_CHROME_PATH
        assert captured["argv"] == list(_plan().chrome_argv())
        assert captured["environment"] == {
            "HOME": "/home/algo",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TZ": "UTC",
        }
        actions = captured["kwargs"]["file_actions"]
        assert any(action[0] == os.POSIX_SPAWN_DUP2 and action[2] == 3 for action in actions)
        assert any(action[0] == os.POSIX_SPAWN_DUP2 and action[2] == 4 for action in actions)
        assert captured["kwargs"]["setsid"] is True
    finally:
        process.close()


def test_evidence_requires_terminal_state_and_terminal_messages_do_not_reopen() -> None:
    machine = BoronNavigationMachine(_plan())
    with pytest.raises(BoronPipeRejected, match="evidence_not_terminal"):
        machine.evidence()
    _bootstrap(machine)
    machine.handle({"method": "Network.webSocketCreated", "params": {}})
    with pytest.raises(BoronPipeRejected, match="machine_terminal"):
        machine.handle({"method": "Page.loadEventFired", "params": {}})


def test_pipe_output_is_canonical_ascii_and_contains_no_file_descriptors() -> None:
    payload = encode_boron_pipe_message({"params": {"enabled": True}, "id": 1})
    assert payload == b'{"id":1,"params":{"enabled":true}}\x00'
    assert b"/algo-profile" not in payload
    assert json.loads(payload[:-1]) == {"id": 1, "params": {"enabled": True}}
    # Avoid leaking real inherited pipe numbers into a serialized protocol row.
    assert str(os.getpid()).encode() not in payload
