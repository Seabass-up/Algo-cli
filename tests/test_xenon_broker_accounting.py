from __future__ import annotations

from copy import deepcopy
from io import BytesIO

import pytest

from algo_cli.xenon_browser_broker import (
    XENON_MAX_CONNECTIONS,
    XenonBrokerRejected,
    validate_xenon_broker_accounting,
)
from algo_cli.xenon_browser_entry import read_xenon_entry_frame, write_xenon_entry_frame


def _accounting() -> dict:
    return {
        "schema_version": 1,
        "complete": True,
        "active_connection_count": 0,
        "verified_request_count": 1,
        "upstream_connection_ids": [2],
        "denials": [[1, "connect_origin"], [3, "connect_origin_static_service"]],
    }


def _validate(accounting, **overrides) -> None:
    arguments = {
        "connection_count": 3, "request_count": 1,
        "disposition": "blocked", "reason_code": "connect_origin",
    }
    arguments.update(overrides)
    validate_xenon_broker_accounting(accounting, **arguments)


def test_complete_disjoint_origin_denials_remain_blocked_but_qualify() -> None:
    accounting = _accounting()
    before = deepcopy(accounting)
    _validate(accounting)
    assert accounting == before


def test_zero_denials_still_requires_verified_requests() -> None:
    accounting = _accounting()
    accounting.update(upstream_connection_ids=[1], denials=[])
    _validate(accounting, connection_count=1, disposition="verified", reason_code="request_verified")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True), ("schema_version", 0),
        ("complete", False), ("complete", 1),
        ("active_connection_count", 1), ("active_connection_count", False),
        ("verified_request_count", 0), ("verified_request_count", True),
        ("upstream_connection_ids", []), ("upstream_connection_ids", [True]),
        ("upstream_connection_ids", [0]), ("upstream_connection_ids", [4]),
        ("upstream_connection_ids", [1]), ("upstream_connection_ids", [2, 2]),
        ("upstream_connection_ids", "2"), ("denials", None),
        ("denials", [[1, "connect_origin"]]),
        ("denials", [[1, "connect_origin"], [2, "connect_origin"]]),
        ("denials", [[3, "connect_origin"], [1, "connect_origin"]]),
        ("denials", [[1, "connect_origin"], [1, "connect_origin"]]),
        ("denials", [[True, "connect_origin"], [3, "connect_origin"]]),
        ("denials", [[1, "connect_origin"], [4, "connect_origin"]]),
        ("denials", [[1, "connect_origin"], [3, "request_method"]]),
        ("denials", [[1, "connect_origin"], [3, "other_rejection"]]),
        ("denials", [[1, "connect_origin"], [3, "private_canary"]]),
        ("denials", [[1, "connect_origin"], [3, {}]]),
        ("denials", [[1, "connect_origin"], [3, "connect_origin", 0]]),
    ],
)
def test_incomplete_forged_or_overlapping_accounting_is_rejected(field, value) -> None:
    accounting = _accounting()
    accounting[field] = value
    with pytest.raises(XenonBrokerRejected, match="^broker_accounting$"):
        _validate(accounting)


@pytest.mark.parametrize("field", list(_accounting()))
def test_missing_accounting_fields_fail_closed(field) -> None:
    accounting = _accounting()
    del accounting[field]
    with pytest.raises(XenonBrokerRejected, match="^broker_accounting$"):
        _validate(accounting)


@pytest.mark.parametrize("accounting", [None, {}, [], {**_accounting(), "unauthorized_forwarded": 0}])
def test_missing_or_self_asserted_zero_accounting_is_not_evidence(accounting) -> None:
    with pytest.raises(XenonBrokerRejected, match="^broker_accounting$"):
        _validate(accounting)


@pytest.mark.parametrize(
    "overrides",
    [
        {"connection_count": True}, {"connection_count": 0}, {"connection_count": 65},
        {"request_count": 0}, {"request_count": True}, {"request_count": 2},
        {"disposition": "verified"}, {"disposition": "handoff"},
        {"disposition": "failed"}, {"disposition": "unknown"},
        {"disposition": True}, {"reason_code": "request_verified"},
        {"reason_code": "connection_unknown"}, {"reason_code": "private_canary"},
        {"reason_code": {}},
    ],
)
def test_valid_denial_records_do_not_override_incompatible_session_results(overrides) -> None:
    with pytest.raises(XenonBrokerRejected, match="^broker_accounting$"):
        _validate(_accounting(), **overrides)


def test_maximum_permit_denial_ledger_fits_existing_frame_bounds_without_truncation() -> None:
    accounting = _accounting()
    accounting.update(
        upstream_connection_ids=[1],
        denials=[[index, "connect_origin"] for index in range(2, XENON_MAX_CONNECTIONS + 1)],
    )
    _validate(accounting, connection_count=XENON_MAX_CONNECTIONS)
    result = {
        "schema_version": 1, "protocol_version": 1, "type": "xenon.result",
        "disposition": "blocked", "connection_count": XENON_MAX_CONNECTIONS,
        "active_peak": 16, "request_count": 1, "redirect_count": 0, "bytes_to_browser": 1,
        "target_decision_digest": "sha256:" + "a" * 64,
        "ca_certificate_digest": "sha256:" + "b" * 64,
        "reason_code": "connect_origin", "accounting": accounting,
    }
    stream = BytesIO()
    write_xenon_entry_frame(stream, result)
    stream.seek(0)
    assert read_xenon_entry_frame(stream) == result
