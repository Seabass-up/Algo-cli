from __future__ import annotations

from algo_cli.marcus_authority import (
    Capability,
    ConfirmationMode,
    ConfirmationReceipt,
    ConsentGrant,
    EffectClass,
)
from algo_cli.samuel_policy_engine import PolicyDisposition, evaluate_action, resolve_action


def _grant(action: object, *, expires_at: float = 100.0) -> ConsentGrant:
    return ConsentGrant(
        grant_id="grant-1",
        capability_mask=action.capability_mask,
        allowed_actions=frozenset({action.name}),
        allowed_targets=frozenset({action.target}),
        expires_at=expires_at,
        maximum_action_count=1,
    )


def test_unknown_action_is_denied_even_with_unclassified_capability() -> None:
    action = resolve_action("browser_click", {"x": 1, "y": 2}, cwd="/tmp")
    grant = ConsentGrant(
        grant_id="bad-grant",
        capability_mask=Capability.UNCLASSIFIED.value,
        allowed_actions=frozenset({"browser_click"}),
        allowed_targets=frozenset({action.target}),
        expires_at=100.0,
        maximum_action_count=1,
    )
    decision = evaluate_action(action, grant=grant, confirmation=None, now=1.0)
    assert decision.disposition is PolicyDisposition.DENY


def test_read_requires_a_real_target_scoped_runtime_grant(tmp_path) -> None:
    action = resolve_action("read_file", {"path": "README.md"}, cwd=str(tmp_path))
    assert evaluate_action(action, grant=None, confirmation=None, now=1.0).disposition is PolicyDisposition.DENY
    assert evaluate_action(
        action,
        grant=_grant(action),
        confirmation=None,
        now=1.0,
    ).disposition is PolicyDisposition.ALLOW


def test_grant_cannot_cross_target_action_capability_or_expiry(tmp_path) -> None:
    action = resolve_action("write_file", {"path": "one.txt", "content": "x"}, cwd=str(tmp_path))
    other = resolve_action("write_file", {"path": "two.txt", "content": "x"}, cwd=str(tmp_path))
    grant = _grant(action)
    assert evaluate_action(other, grant=grant, confirmation=None, now=1.0).disposition is PolicyDisposition.CONFIRM
    expired = _grant(action, expires_at=1.0)
    assert evaluate_action(action, grant=expired, confirmation=None, now=1.0).disposition is PolicyDisposition.CONFIRM


def test_action_time_confirmation_binds_exact_digest_and_expiry(tmp_path) -> None:
    action = resolve_action("write_file", {"path": "one.txt", "content": "x"}, cwd=str(tmp_path))
    grant = _grant(action)
    missing = evaluate_action(action, grant=grant, confirmation=None, now=10.0)
    assert missing.disposition is PolicyDisposition.CONFIRM
    receipt = ConfirmationReceipt(
        receipt_id="confirm-1",
        action_digest=action.action_digest,
        confirmation_mode=ConfirmationMode.ACTION_TIME,
        confirmed_at=9.0,
        expires_at=11.0,
    )
    assert evaluate_action(
        action,
        grant=grant,
        confirmation=receipt,
        now=10.0,
    ).disposition is PolicyDisposition.ALLOW
    changed = resolve_action("write_file", {"path": "one.txt", "content": "changed"}, cwd=str(tmp_path))
    assert evaluate_action(
        changed,
        grant=_grant(changed),
        confirmation=receipt,
        now=10.0,
    ).disposition is PolicyDisposition.CONFIRM
    assert evaluate_action(
        action,
        grant=grant,
        confirmation=receipt,
        now=11.0,
    ).disposition is PolicyDisposition.CONFIRM


def test_auto_approve_cannot_bypass_action_time_confirmation(tmp_path) -> None:
    action = resolve_action("write_file", {"path": "x.txt", "content": "x"}, cwd=str(tmp_path))
    decision = evaluate_action(
        action,
        grant=_grant(action),
        confirmation=None,
        now=1.0,
        auto_approve=True,
    )
    assert decision.disposition is PolicyDisposition.CONFIRM
    assert "auto approval is not sufficient" in decision.reason


def test_handoff_action_stays_handoff_even_with_a_matching_grant(tmp_path) -> None:
    action = resolve_action("plugins_load", {"plugin_name": "demo"}, cwd=str(tmp_path))
    decision = evaluate_action(action, grant=_grant(action), confirmation=None, now=1.0, auto_approve=True)
    assert decision.disposition is PolicyDisposition.HANDOFF


def test_canonical_web_origin_excludes_path_query_and_fragment(tmp_path) -> None:
    action = resolve_action(
        "web_fetch",
        {"url": "HTTPS://Example.COM:443/private?q=secret#fragment"},
        cwd=str(tmp_path),
    )
    assert action.target == "origin:https://example.com:443"
    assert "secret" not in action.target


def test_session_wrapper_is_resolved_from_the_exact_subcommand(tmp_path) -> None:
    status = resolve_action("session_command", {"command": "/status"}, cwd=str(tmp_path))
    mutation = resolve_action("session_command", {"command": "/safe off"}, cwd=str(tmp_path))

    assert status.effect_class is EffectClass.OBSERVE
    assert status.confirmation_mode is ConfirmationMode.NONE
    assert status.capability_mask == Capability.READ.value
    assert status.target == "runtime:session:/status"
    assert mutation.effect_class is EffectClass.CONFIGURATION
    assert mutation.confirmation_mode is ConfirmationMode.ACTION_TIME
    assert mutation.capability_mask & Capability.WRITE.value
    assert mutation.target == "runtime:session:/safe"


def test_session_read_resolves_and_binds_the_workspace_path(tmp_path) -> None:
    inside = resolve_action("session_slash", {"command": "/read notes.txt"}, cwd=str(tmp_path))
    outside = resolve_action("session_slash", {"command": "/read ../secret.txt"}, cwd=str(tmp_path))

    assert inside.target == f"workspace:{(tmp_path / 'notes.txt').resolve()}"
    assert inside.target_scope.value == "workspace"
    assert outside.target == f"workspace:{(tmp_path / '../secret.txt').resolve()}"


def test_malformed_web_origin_fails_closed_without_raising(tmp_path) -> None:
    action = resolve_action(
        "web_fetch",
        {"url": "https://example.com:not-a-port/private"},
        cwd=str(tmp_path),
    )
    assert action.target == "provider:unresolved"
    decision = evaluate_action(action, grant=_grant(action), confirmation=None, now=1.0)
    assert decision.disposition is PolicyDisposition.DENY
