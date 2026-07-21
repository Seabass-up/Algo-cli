from __future__ import annotations

import base64
from io import BytesIO
import json
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes
import pytest

from algo_cli.boron_browser_entry import (
    BORON_ENTRY_MAX_FRAME_BYTES,
    BORON_ENTRY_PROTOCOL_VERSION,
    BORON_ENTRY_SCHEMA_VERSION,
    BoronEntryRejected,
    BoronStartConfig,
    execute_boron_start,
    main,
    read_boron_entry_frame,
    write_boron_entry_frame,
)
from algo_cli.boron_browser_wrapper import (
    BoronNavigationEvidence,
    BoronNavigationState,
    BoronPipeRejected,
)
from algo_cli.xenon_browser_broker import XenonEphemeralCertificateAuthority


NOW_MS = int(time.time() * 1000)
VERSION = "150.0.7871.128"
URL = "https://example.com/start?q=1"


def _ca() -> XenonEphemeralCertificateAuthority:
    return XenonEphemeralCertificateAuthority.create(
        now_ms=NOW_MS,
        expires_at_ms=NOW_MS + 60_000,
    )


def _row() -> dict[str, object]:
    ca = _ca()
    pem = ca.certificate_pem
    return {
        "schema_version": BORON_ENTRY_SCHEMA_VERSION,
        "protocol_version": BORON_ENTRY_PROTOCOL_VERSION,
        "type": "boron.start",
        "session_id": "11111111-1111-4111-8111-111111111111",
        "canonical_url": URL,
        "expected_browser_version": VERSION,
        "proxy_host": "172.30.0.3",
        "proxy_port": 3128,
        "maximum_duration_ms": 30_000,
        "ca_pem_base64url": base64.urlsafe_b64encode(pem).decode("ascii").rstrip("="),
        "ca_pem_digest": "sha256:" + __import__("hashlib").sha256(pem).hexdigest(),
        "ca_certificate_digest": ca.certificate_digest,
    }

def test_start_config_checks_both_ca_digests_and_hides_sensitive_fields() -> None:
    config = BoronStartConfig.from_dict(_row())
    certificate = x509.load_pem_x509_certificate(config.ca_pem)
    assert config.ca_certificate_digest == (
        "sha256:" + certificate.fingerprint(hashes.SHA256()).hex()
    )
    assert config.plan.canonical_url == URL
    assert URL not in repr(config)
    assert config.ca_pem_base64url not in repr(config)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("ca_pem_digest", "sha256:" + "0" * 64, "ca_pem_digest_mismatch"),
        (
            "ca_certificate_digest",
            "sha256:" + "0" * 64,
            "ca_certificate_digest_mismatch",
        ),
        ("canonical_url", "https://EXAMPLE.com/", "plan_navigation_not_canonical"),
        ("expected_browser_version", "latest", "plan_browser_version"),
        ("proxy_port", 80, "plan_proxy_port"),
    ],
)
def test_start_config_rejects_tamper_and_noncanonical_plans(
    field: str, value: object, reason: str
) -> None:
    row = _row()
    row[field] = value
    with pytest.raises(BoronEntryRejected, match=reason):
        BoronStartConfig.from_dict(row)


def test_start_config_rejects_schema_expansion_and_noncanonical_base64() -> None:
    row = _row()
    row["unexpected"] = True
    with pytest.raises(BoronEntryRejected, match="start_schema"):
        BoronStartConfig.from_dict(row)
    row = _row()
    row["ca_pem_base64url"] = str(row["ca_pem_base64url"]) + "="
    with pytest.raises(BoronEntryRejected, match="ca_encoding"):
        BoronStartConfig.from_dict(row)


def test_execute_installs_exact_certificate_then_returns_structural_evidence() -> None:
    config = BoronStartConfig.from_dict(_row())
    observed: list[object] = []

    def install(ca_pem: bytes, *, now_ms: int) -> str:
        observed.extend((ca_pem, now_ms))
        return config.ca_certificate_digest

    def navigate(plan) -> BoronNavigationEvidence:
        observed.append(plan)
        return BoronNavigationEvidence(
            BoronNavigationState.VERIFIED,
            150,
            12,
            5,
            0,
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            "navigation_verified",
        )

    result = execute_boron_start(
        config,
        navigation_runner=navigate,
        ca_installer=install,
        clock_ms=lambda: NOW_MS,
    )
    assert result["state"] == "verified"
    assert result["browser_major"] == 150
    assert result["ca_pem_digest"] == config.ca_pem_digest
    assert result["ca_certificate_digest"] == config.ca_certificate_digest
    assert URL not in json.dumps(result)
    assert observed == [config.ca_pem, NOW_MS, config.plan]


def test_execute_fails_closed_on_ca_or_navigation_adapter_errors() -> None:
    config = BoronStartConfig.from_dict(_row())
    with pytest.raises(BoronEntryRejected, match="ca_install_digest"):
        execute_boron_start(
            config,
            ca_installer=lambda *_args, **_kwargs: "sha256:" + "0" * 64,
        )

    def rejected(*_args, **_kwargs):
        raise BoronPipeRejected("ca_import")

    with pytest.raises(BoronEntryRejected, match="ca_ca_import"):
        execute_boron_start(config, ca_installer=rejected)

    with pytest.raises(BoronEntryRejected, match="navigation_evidence"):
        execute_boron_start(
            config,
            ca_installer=lambda *_args, **_kwargs: config.ca_certificate_digest,
            navigation_runner=lambda _plan: object(),  # type: ignore[arg-type,return-value]
        )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b'{"a":1}', "entry_truncated"),
        (b'{"a":1}\x00{"b":2}\x00', "entry_frame_count"),
        (b'{"a":1,"a":2}\x00', "entry_json_duplicate_key"),
        (b"[]\x00", "entry_pipe_message_object"),
        (b"a" * (BORON_ENTRY_MAX_FRAME_BYTES + 1), "entry_frame_size"),
    ],
)
def test_entry_reader_rejects_open_ambiguous_and_oversized_frames(
    payload: bytes, reason: str
) -> None:
    with pytest.raises(BoronEntryRejected, match=reason):
        read_boron_entry_frame(BytesIO(payload))


def test_entry_frames_round_trip_and_stream_errors_are_normalized() -> None:
    stream = BytesIO()
    write_boron_entry_frame(stream, {"type": "test", "count": 1})
    assert read_boron_entry_frame(BytesIO(stream.getvalue())) == {
        "type": "test",
        "count": 1,
    }

    class Broken:
        def read(self, _size: int) -> bytes:
            raise OSError("raw path and secret")

    with pytest.raises(BoronEntryRejected, match="entry_stream"):
        read_boron_entry_frame(Broken())  # type: ignore[arg-type]


def test_main_returns_one_content_free_error_frame() -> None:
    output = BytesIO()
    assert main(BytesIO(b"{}\x00"), output) == 2
    row = read_boron_entry_frame(BytesIO(output.getvalue()))
    assert row == {
        "protocol_version": BORON_ENTRY_PROTOCOL_VERSION,
        "reason_code": "start_schema",
        "schema_version": BORON_ENTRY_SCHEMA_VERSION,
        "type": "boron.error",
    }
