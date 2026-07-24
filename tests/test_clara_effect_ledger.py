from __future__ import annotations

import json
import multiprocessing

import pytest

from algo_cli.clara_effect_ledger import (
    EffectLedger,
    EffectLedgerCorrupt,
    EffectState,
    InvalidEffectTransition,
    InvocationReplayConflict,
    StaleFencingToken,
)
from algo_cli.henry_effect_control import target_digest


KEY = "a" * 64
INVOCATION = "b" * 64


def _prepare_replayed_invocation(path, key, fencing_token, results) -> None:
    ledger = EffectLedger.at_path(path)
    try:
        prepared = ledger.prepare(
            idempotency_key=key,
            invocation_id_hash=INVOCATION,
            action="x_account_post",
            target="external-account:default",
            fencing_token=fencing_token,
        )
    except InvocationReplayConflict:
        results.put("replay")
    else:
        results.put("created" if prepared.created else "duplicate")


def _ledger(tmp_path) -> EffectLedger:
    return EffectLedger.at_path(str(tmp_path / "private" / "effects.jsonl"))


def test_effect_transitions_survive_restart_and_store_no_target_content(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    prepared = ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:private-name",
        fencing_token=7,
    )
    ledger.transition(prepared.record.effect_id, EffectState.STARTED, fencing_token=7)
    ledger.transition(prepared.record.effect_id, EffectState.APPLIED, fencing_token=7)
    final = ledger.transition(
        prepared.record.effect_id,
        EffectState.UNKNOWN,
        fencing_token=7,
        reason_code="postcondition_unavailable",
    )

    restarted = _ledger(tmp_path)
    assert restarted.get(final.effect_id) == final
    raw = (tmp_path / "private" / "effects.jsonl").read_text(encoding="utf-8")
    assert "private-name" not in raw
    assert target_digest("external-account:private-name") in raw


def test_idempotency_key_returns_existing_effect_without_second_prepare(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:default",
        fencing_token=1,
    )
    second = ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:default",
        fencing_token=2,
    )

    assert first.created is True
    assert second.created is False
    assert second.record.effect_id == first.record.effect_id
    assert len(ledger.records()) == 1


def test_replayed_invocation_id_cannot_bind_to_changed_action(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:default",
        fencing_token=1,
    )

    with pytest.raises(InvocationReplayConflict):
        ledger.prepare(
            idempotency_key="c" * 64,
            invocation_id_hash=INVOCATION,
            action="x_account_post",
            target="external-account:default",
            fencing_token=2,
        )


def test_replayed_invocation_is_atomic_across_processes(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    path = str(tmp_path / "private" / "effects.jsonl")
    processes = [
        context.Process(
            target=_prepare_replayed_invocation,
            args=(path, key, index + 1, results),
        )
        for index, key in enumerate((KEY, "c" * 64))
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=8.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
        assert process.exitcode == 0

    assert sorted(results.get(timeout=2.0) for _ in processes) == ["created", "replay"]
    assert len(_ledger(tmp_path).records()) == 1


def test_prepared_crash_recovery_requires_newer_fence(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    prepared = ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:default",
        fencing_token=3,
    ).record

    with pytest.raises(StaleFencingToken):
        ledger.recover_prepared(prepared.effect_id, recovery_fencing_token=3)
    recovered = ledger.recover_prepared(
        prepared.effect_id,
        recovery_fencing_token=4,
    )

    assert recovered.state is EffectState.FAILED
    assert recovered.reason_code == "recovered_before_dispatch"
    assert recovered.verifier == "newer_target_fence"


def test_invalid_transition_and_stale_fence_fail_closed(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    effect = ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:default",
        fencing_token=3,
    ).record

    with pytest.raises(InvalidEffectTransition):
        ledger.transition(effect.effect_id, EffectState.VERIFIED, fencing_token=3)
    with pytest.raises(StaleFencingToken):
        ledger.transition(effect.effect_id, EffectState.STARTED, fencing_token=2)


@pytest.mark.parametrize("observed,state", [(True, EffectState.VERIFIED), (False, EffectState.FAILED)])
def test_unknown_effect_requires_explicit_reconciliation(tmp_path, observed, state) -> None:
    ledger = _ledger(tmp_path)
    effect = ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:default",
        fencing_token=4,
    ).record
    ledger.transition(effect.effect_id, EffectState.STARTED, fencing_token=4)
    ledger.transition(effect.effect_id, EffectState.UNKNOWN, fencing_token=4)

    reconciled = ledger.reconcile(
        effect.effect_id,
        fencing_token=4,
        observed_applied=observed,
        verifier="test_observer",
    )
    assert reconciled.state is state


def test_corrupt_valid_json_event_is_not_silently_ignored(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.store.append({"schema_version": 1, "state": "verified"})

    with pytest.raises(EffectLedgerCorrupt):
        ledger.records()


def test_reason_codes_reject_free_form_content(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    effect = ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:default",
        fencing_token=1,
    ).record

    with pytest.raises(ValueError):
        ledger.transition(
            effect.effect_id,
            EffectState.STARTED,
            fencing_token=1,
            reason_code="private user content",
        )


def test_duplicate_idempotency_mapping_is_detected_as_corruption(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:default",
        fencing_token=1,
    )
    ledger.store.append(
        {
            "schema_version": 2,
            "effect_id": "effect-other",
            "idempotency_key": KEY,
            "invocation_id_hash": INVOCATION,
            "action": "x_account_post",
            "target_hash": "b" * 64,
            "state": "prepared",
            "previous_state": "",
            "fencing_token": 2,
            "sequence": 0,
            "reason_code": "",
            "verifier": "",
        }
    )

    with pytest.raises(EffectLedgerCorrupt):
        ledger.records()


def test_effect_payload_is_canonical_json_serializable(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    effect = ledger.prepare(
        idempotency_key=KEY,
        invocation_id_hash=INVOCATION,
        action="x_account_post",
        target="external-account:default",
        fencing_token=1,
    ).record
    json.dumps(effect.__dict__, default=str)
