from __future__ import annotations

import json

import pytest

from algo_cli.irene_privacy_views import (
    PrivacyProjectionError,
    PrivacyView,
    keyed_action_fingerprint,
    project_action_args,
)


HMAC_KEY = b"k" * 32


def _render(value) -> str:
    return json.dumps(value, sort_keys=True)


def test_nested_secrets_are_removed_from_every_visible_projection() -> None:
    marker = "PRIVATE-secret-marker"
    args = {
        "outer": {
            "access_token": marker,
            "rows": [{"client_secret": marker}, {"safe": "identifier"}],
        }
    }

    for view in (PrivacyView.CONFIRMATION, PrivacyView.MODEL, PrivacyView.AUDIT):
        rendered = _render(project_action_args("custom_action", args, view, hmac_key=HMAC_KEY))
        assert marker not in rendered
    telemetry = project_action_args("custom_action", args, PrivacyView.TELEMETRY)
    assert marker not in _render(telemetry)
    assert telemetry["classes"]["secret"] == 2


def test_confirmation_shows_exact_external_message_but_summarizes_file_content() -> None:
    post = project_action_args(
        "x_account_post",
        {"text": "Publish this exact sentence."},
        PrivacyView.CONFIRMATION,
        hmac_key=HMAC_KEY,
    )
    write = project_action_args(
        "write_file",
        {"path": "notes.txt", "content": "private file body"},
        PrivacyView.CONFIRMATION,
        hmac_key=HMAC_KEY,
    )

    assert post["text"] == "Publish this exact sentence."
    assert write["path"] == "notes.txt"
    assert write["content"]["redacted"] is True
    assert "private file body" not in _render(write)


def test_model_view_preserves_required_content_but_redacts_credentials() -> None:
    projected = project_action_args(
        "custom_action",
        {"content": "needed by model", "nested": {"password": "nope"}},
        PrivacyView.MODEL,
        hmac_key=HMAC_KEY,
    )

    assert projected["content"] == "needed by model"
    assert projected["nested"]["password"] == "<redacted>"


def test_confirmation_url_drops_userinfo_query_and_fragment() -> None:
    projected = project_action_args(
        "web_fetch",
        {"url": "https://user:pass@example.com/path?token=SECRET#frag"},
        PrivacyView.CONFIRMATION,
        hmac_key=HMAC_KEY,
    )

    rendered = projected["url"]
    assert rendered == "https://example.com/path?<redacted>#<redacted>"
    assert "user" not in rendered
    assert "pass" not in rendered
    assert "SECRET" not in rendered


def test_audit_projection_uses_keyed_distinct_identities_without_plaintext() -> None:
    one = project_action_args(
        "credential_helpers_store",
        {"name": "service", "value": "secret-one"},
        PrivacyView.AUDIT,
        hmac_key=HMAC_KEY,
    )
    two = project_action_args(
        "credential_helpers_store",
        {"name": "service", "value": "secret-two"},
        PrivacyView.AUDIT,
        hmac_key=HMAC_KEY,
    )

    assert "secret-one" not in _render(one)
    assert "secret-two" not in _render(two)
    one_secret = next(value for value in one.values() if value["class"] == "secret")
    two_secret = next(value for value in two.values() if value["class"] == "secret")
    assert one_secret["hmac_sha256"] != two_secret["hmac_sha256"]
    assert "value" not in _render(one)


def test_camel_case_secret_names_are_redacted() -> None:
    projected = project_action_args(
        "custom_action",
        {"apiKey": "one", "accessToken": "two", "clientSecret": "three"},
        PrivacyView.MODEL,
        hmac_key=HMAC_KEY,
    )

    assert projected == {
        "apiKey": "<redacted>",
        "accessToken": "<redacted>",
        "clientSecret": "<redacted>",
    }


def test_keyed_action_fingerprint_is_stable_order_independent_and_secret() -> None:
    first = keyed_action_fingerprint(
        "run_shell",
        {"command": "echo PRIVATE", "timeout": 3},
        hmac_key=HMAC_KEY,
    )
    second = keyed_action_fingerprint(
        "run_shell",
        {"timeout": 3, "command": "echo PRIVATE"},
        hmac_key=HMAC_KEY,
    )

    assert first == second
    assert "PRIVATE" not in first
    assert first.startswith("hmac-sha256:")


def test_telemetry_projection_contains_only_aggregate_shape() -> None:
    args = {
        "url": "https://example.com/private?q=secret",
        "selector": "#password",
        "content": "private body",
    }

    projected = project_action_args("future_browser_action", args, PrivacyView.TELEMETRY)
    rendered = _render(projected)

    for forbidden in ("example.com", "private body", "#password"):
        assert forbidden not in rendered
    assert projected["field_count"] == 3
    assert projected["classes"] == {"content": 1, "selector": 1, "unknown": 1, "url": 1}


def test_cycles_and_oversized_depth_fail_closed() -> None:
    cyclic: dict = {}
    cyclic["child"] = cyclic

    with pytest.raises(PrivacyProjectionError, match="cycle"):
        project_action_args("custom_action", cyclic, PrivacyView.MODEL, hmac_key=HMAC_KEY)
    with pytest.raises(PrivacyProjectionError, match="cycle"):
        keyed_action_fingerprint("custom_action", cyclic, hmac_key=HMAC_KEY)

    nested: dict = {}
    cursor = nested
    for _ in range(20):
        child: dict = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(PrivacyProjectionError, match="depth"):
        project_action_args("custom_action", nested, PrivacyView.MODEL, hmac_key=HMAC_KEY)


def test_unsupported_objects_never_call_repr_or_str() -> None:
    class Hostile:
        def __str__(self) -> str:
            raise AssertionError("must not stringify hostile objects")

        def __repr__(self) -> str:
            raise AssertionError("must not repr hostile objects")

    projected = project_action_args(
        "custom_action",
        {"object": Hostile()},
        PrivacyView.MODEL,
        hmac_key=HMAC_KEY,
    )

    assert projected["object"] == {
        "class": "unknown",
        "type": "Hostile",
        "redacted": True,
    }
