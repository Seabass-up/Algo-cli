"""Ada regression gates for Echo-protected goal-ledger persistence."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from algo_cli import ada_task_ledger as task_ledger
from algo_cli.grace_memory_receipts import ElsieReceiptAuthority, ElsieReceiptError
from algo_cli.grace_key_store import StaticKeyStore
from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL


def _authority(byte: bytes = b"g") -> ElsieReceiptAuthority:
    return ElsieReceiptAuthority.from_key_store(store=StaticKeyStore({PRIVACY_KEY_LABEL: byte * 32}))


def _shared_authority(
    byte: bytes = b"g",
) -> tuple[ElsieReceiptAuthority, StaticKeyStore]:
    store = StaticKeyStore({PRIVACY_KEY_LABEL: byte * 32})
    return ElsieReceiptAuthority.from_key_store(store=store), store


def test_protected_goal_keeps_explicit_goal_but_projects_derived_text() -> None:
    authority = _authority()
    reason = "SECRET_GOAL_REASON_CANARY"
    summary = "SECRET_GOAL_SUMMARY_CANARY"
    record = task_ledger.GoalRecord(goal="explicit user goal", cwd="/tmp/project")
    record.reason = reason
    record.add_round(summary)

    task_ledger.save_goal(
        record,
        protected=True,
        receipt_authority=authority,
    )

    serialized = task_ledger.LEDGER_PATH.read_text(encoding="utf-8")
    payload = json.loads(serialized)
    assert payload["schema_version"] == task_ledger.PROTECTED_LEDGER_SCHEMA_VERSION
    assert payload["receipt_binding"] == authority.binding.as_dict()
    assert "explicit user goal" in serialized
    assert reason not in serialized
    assert summary not in serialized
    assert hashlib.sha256(reason.encode()).hexdigest() not in serialized
    assert hashlib.sha256(summary.encode()).hexdigest() not in serialized
    loaded = task_ledger.load_goal(
        protected=True,
        receipt_authority=authority,
    )
    assert loaded is not None
    assert loaded.goal == "explicit user goal"
    assert loaded.reason == ""
    projection = task_ledger.goal_status_projection(loaded, protected=True)
    assert projection["reason"] == ""
    assert projection["reason_receipt"].startswith("hmac-sha256:")
    assert projection["last_summary"] == ""
    assert projection["last_summary_receipt"].startswith("hmac-sha256:")
    if os.name == "posix":
        assert stat.S_IMODE(task_ledger.LEDGER_PATH.stat().st_mode) == 0o600
        assert stat.S_IMODE(task_ledger.LEDGER_PATH.parent.stat().st_mode) == 0o700


def test_protected_load_atomically_migrates_legacy_derived_fields() -> None:
    reason = "SECRET_LEGACY_REASON_CANARY"
    summary = "SECRET_LEGACY_SUMMARY_CANARY"
    legacy = task_ledger.GoalRecord(goal="explicit legacy goal")
    legacy.reason = reason
    legacy.add_round(summary)
    task_ledger.save_goal(legacy)

    loaded = task_ledger.load_goal(
        protected=True,
        receipt_authority=_authority(),
    )

    assert loaded is not None
    assert loaded.goal == "explicit legacy goal"
    assert loaded.status == task_ledger.STATUS_BLOCKED
    assert loaded.is_open is False
    serialized = task_ledger.LEDGER_PATH.read_text(encoding="utf-8")
    assert reason not in serialized
    assert summary not in serialized
    assert json.loads(serialized)["schema_version"] == task_ledger.PROTECTED_LEDGER_SCHEMA_VERSION


def test_protected_goal_external_anchor_rejects_valid_file_rollback() -> None:
    authority, _store = _shared_authority()
    record = task_ledger.GoalRecord(goal="explicit goal")
    task_ledger.save_goal(record, protected=True, receipt_authority=authority)
    old_file = task_ledger.LEDGER_PATH.read_bytes()
    record.add_round("SECRET_ROUND_TWO_CANARY")
    task_ledger.save_goal(record, protected=True, receipt_authority=authority)

    task_ledger.LEDGER_PATH.write_bytes(old_file)

    with pytest.raises(ElsieReceiptError, match="rollback or rewrite"):
        task_ledger.load_goal(protected=True, receipt_authority=authority)


def test_protected_goal_clear_anchor_prevents_resurrection() -> None:
    authority, _store = _shared_authority()
    task_ledger.save_goal(
        task_ledger.GoalRecord(goal="explicit goal"),
        protected=True,
        receipt_authority=authority,
    )
    old_file = task_ledger.LEDGER_PATH.read_bytes()

    assert (
        task_ledger.clear_goal(
            protected=True,
            receipt_authority=authority,
        )
        is True
    )
    assert (
        task_ledger.load_goal(
            protected=True,
            receipt_authority=authority,
        )
        is None
    )
    task_ledger.LEDGER_PATH.write_bytes(old_file)
    with pytest.raises(ElsieReceiptError, match="rollback or rewrite"):
        task_ledger.load_goal(protected=True, receipt_authority=authority)


def test_protected_goal_rejects_stale_concurrent_record() -> None:
    authority, _store = _shared_authority()
    task_ledger.save_goal(
        task_ledger.GoalRecord(goal="explicit goal"),
        protected=True,
        receipt_authority=authority,
    )
    first = task_ledger.load_goal(protected=True, receipt_authority=authority)
    second = task_ledger.load_goal(protected=True, receipt_authority=authority)
    assert first is not None and second is not None
    first.add_round("first")
    task_ledger.save_goal(first, protected=True, receipt_authority=authority)
    second.add_round("second")

    with pytest.raises(ElsieReceiptError, match="stale"):
        task_ledger.save_goal(second, protected=True, receipt_authority=authority)


def test_missing_protected_goal_file_does_not_create_key(monkeypatch) -> None:
    def forbidden_create(cls):
        pytest.fail("missing protected ledger must not create receipt authority")

    def forbidden_existing(cls):
        pytest.fail("missing protected ledger needs no key lookup")

    monkeypatch.setattr(
        task_ledger.ElsieReceiptAuthority,
        "from_key_store",
        classmethod(forbidden_create),
    )
    monkeypatch.setattr(
        task_ledger.ElsieReceiptAuthority,
        "from_existing_key_store",
        classmethod(forbidden_existing),
    )

    assert task_ledger.load_goal(protected=True) is None


def test_protected_goal_wrong_key_fails_closed_and_preserves_file() -> None:
    task_ledger.save_goal(
        task_ledger.GoalRecord(goal="explicit goal"),
        protected=True,
        receipt_authority=_authority(b"a"),
    )
    before = task_ledger.LEDGER_PATH.read_bytes()

    with pytest.raises(ElsieReceiptError, match="binding mismatch"):
        task_ledger.load_goal(
            protected=True,
            receipt_authority=_authority(b"b"),
        )

    assert task_ledger.LEDGER_PATH.read_bytes() == before


def test_protected_goal_rejects_unknown_plaintext_fields() -> None:
    authority = _authority()
    task_ledger.save_goal(
        task_ledger.GoalRecord(goal="explicit goal"),
        protected=True,
        receipt_authority=authority,
    )
    payload = json.loads(task_ledger.LEDGER_PATH.read_text(encoding="utf-8"))
    payload["goal"]["assistant_output"] = "SECRET_EXTRA_CANARY"
    task_ledger.LEDGER_PATH.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ElsieReceiptError, match="authentication"):
        task_ledger.load_goal(protected=True, receipt_authority=authority)


def test_protected_goal_load_missing_key_never_creates_or_backs_up_raw(
    monkeypatch,
) -> None:
    task_ledger.save_goal(
        task_ledger.GoalRecord(goal="explicit goal"),
        protected=True,
        receipt_authority=_authority(),
    )

    def forbidden_create(cls):
        pytest.fail("protected goal reads must not create a receipt key")

    def missing_existing(cls):
        raise ElsieReceiptError("missing existing key")

    monkeypatch.setattr(
        task_ledger.ElsieReceiptAuthority,
        "from_key_store",
        classmethod(forbidden_create),
    )
    monkeypatch.setattr(
        task_ledger.ElsieReceiptAuthority,
        "from_existing_key_store",
        classmethod(missing_existing),
    )
    with pytest.raises(ElsieReceiptError, match="missing existing"):
        task_ledger.load_goal(protected=True)

    task_ledger.LEDGER_PATH.write_text("SECRET_CORRUPT_LEDGER_CANARY{", encoding="utf-8")
    with pytest.raises(ElsieReceiptError, match="malformed"):
        task_ledger.load_goal(protected=True)
    assert not task_ledger.LEDGER_PATH.with_suffix(".json.corrupt").exists()


def test_protected_goal_recovers_post_anchor_publication_failure_only_explicitly(
    monkeypatch,
) -> None:
    authority, _store = _shared_authority()
    original_publish = task_ledger.publish_elsie_staged_file

    def fail_publish(*_args, **_kwargs):
        raise ElsieReceiptError("simulated post-anchor rename failure")

    monkeypatch.setattr(task_ledger, "publish_elsie_staged_file", fail_publish)
    with pytest.raises(ElsieReceiptError, match="post-anchor"):
        task_ledger.save_goal(
            task_ledger.GoalRecord(goal="explicit crash-safe goal"),
            protected=True,
            receipt_authority=authority,
        )

    pending = task_ledger._pending_goal_path()
    assert pending.is_file()
    assert not task_ledger.LEDGER_PATH.exists()
    with pytest.raises(ElsieReceiptError, match="recovery is pending"):
        task_ledger.load_goal(
            protected=True,
            receipt_authority=authority,
        )

    monkeypatch.setattr(
        task_ledger,
        "publish_elsie_staged_file",
        original_publish,
    )
    assert (
        task_ledger.prepare_protected_goal_store(
            receipt_authority=authority,
        )
        is True
    )
    recovered = task_ledger.load_goal(
        protected=True,
        receipt_authority=authority,
    )
    assert recovered is not None
    assert recovered.goal == "explicit crash-safe goal"
    assert not pending.exists()


def test_protected_goal_recovers_pre_anchor_staged_write(monkeypatch) -> None:
    authority, _store = _shared_authority()
    original_advance = task_ledger.advance_elsie_store_anchor

    def fail_before_anchor(*_args, **_kwargs):
        raise ElsieReceiptError("simulated pre-anchor failure")

    monkeypatch.setattr(task_ledger, "advance_elsie_store_anchor", fail_before_anchor)
    with pytest.raises(ElsieReceiptError, match="pre-anchor"):
        task_ledger.save_goal(
            task_ledger.GoalRecord(goal="explicit staged goal"),
            protected=True,
            receipt_authority=authority,
        )

    assert task_ledger._pending_goal_path().is_file()
    monkeypatch.setattr(
        task_ledger,
        "advance_elsie_store_anchor",
        original_advance,
    )
    assert (
        task_ledger.prepare_protected_goal_store(
            receipt_authority=authority,
        )
        is True
    )
    assert (
        task_ledger.load_goal(
            protected=True,
            receipt_authority=authority,
        ).goal
        == "explicit staged goal"
    )


def test_protected_goal_post_replace_error_is_restart_readable(monkeypatch) -> None:
    authority, _store = _shared_authority()
    original_publish = task_ledger.publish_elsie_staged_file

    def publish_then_report_fsync_error(*args, **kwargs):
        original_publish(*args, **kwargs)
        raise ElsieReceiptError("simulated post-replace fsync report")

    monkeypatch.setattr(
        task_ledger,
        "publish_elsie_staged_file",
        publish_then_report_fsync_error,
    )
    with pytest.raises(ElsieReceiptError, match="post-replace"):
        task_ledger.save_goal(
            task_ledger.GoalRecord(goal="explicit durable goal"),
            protected=True,
            receipt_authority=authority,
        )

    assert not task_ledger._pending_goal_path().exists()
    loaded = task_ledger.load_goal(
        protected=True,
        receipt_authority=authority,
    )
    assert loaded is not None
    assert loaded.goal == "explicit durable goal"


def test_protected_goal_clear_recovers_anchor_ahead_tombstone(monkeypatch) -> None:
    authority, _store = _shared_authority()
    task_ledger.save_goal(
        task_ledger.GoalRecord(goal="explicit goal to clear"),
        protected=True,
        receipt_authority=authority,
    )
    original_publish = task_ledger.publish_elsie_staged_file

    def fail_publish(*_args, **_kwargs):
        raise ElsieReceiptError("simulated tombstone publication failure")

    monkeypatch.setattr(task_ledger, "publish_elsie_staged_file", fail_publish)
    with pytest.raises(ElsieReceiptError, match="tombstone"):
        task_ledger.clear_goal(
            protected=True,
            receipt_authority=authority,
        )
    with pytest.raises(ElsieReceiptError, match="recovery is pending"):
        task_ledger.load_goal(
            protected=True,
            receipt_authority=authority,
        )

    monkeypatch.setattr(
        task_ledger,
        "publish_elsie_staged_file",
        original_publish,
    )
    assert (
        task_ledger.prepare_protected_goal_store(
            receipt_authority=authority,
        )
        is True
    )
    assert (
        task_ledger.load_goal(
            protected=True,
            receipt_authority=authority,
        )
        is None
    )


def test_empty_goal_preflight_is_nonmutating_and_needs_no_key(monkeypatch) -> None:
    def absent_optional(cls):
        return None

    def forbidden_create(cls):
        pytest.fail("empty protected goal preflight must not create a key")

    monkeypatch.setattr(
        task_ledger.ElsieReceiptAuthority,
        "from_optional_existing_key_store",
        classmethod(absent_optional),
    )
    monkeypatch.setattr(
        task_ledger.ElsieReceiptAuthority,
        "from_key_store",
        classmethod(forbidden_create),
    )

    assert task_ledger.prepare_protected_goal_store() is False


def test_protected_goal_recovers_update_staged_before_anchor(monkeypatch) -> None:
    authority, _store = _shared_authority()
    record = task_ledger.GoalRecord(goal="explicit update goal")
    task_ledger.save_goal(
        record,
        protected=True,
        receipt_authority=authority,
    )
    before = task_ledger.LEDGER_PATH.read_bytes()
    record.add_round("SECRET_UPDATE_ROUND_CANARY")
    original_advance = task_ledger.advance_elsie_store_anchor

    def fail_before_anchor(*_args, **_kwargs):
        raise ElsieReceiptError("simulated update pre-anchor failure")

    monkeypatch.setattr(task_ledger, "advance_elsie_store_anchor", fail_before_anchor)
    with pytest.raises(ElsieReceiptError, match="update pre-anchor"):
        task_ledger.save_goal(
            record,
            protected=True,
            receipt_authority=authority,
        )
    assert task_ledger.LEDGER_PATH.read_bytes() == before

    monkeypatch.setattr(
        task_ledger,
        "advance_elsie_store_anchor",
        original_advance,
    )
    assert (
        task_ledger.prepare_protected_goal_store(
            receipt_authority=authority,
        )
        is True
    )
    recovered = task_ledger.load_goal(
        protected=True,
        receipt_authority=authority,
    )
    assert recovered is not None
    assert recovered.rounds_done == 1


def test_protected_goal_rejects_old_valid_pending_stage_replay() -> None:
    authority, _store = _shared_authority()
    record = task_ledger.GoalRecord(goal="explicit replay goal")
    task_ledger.save_goal(
        record,
        protected=True,
        receipt_authority=authority,
    )
    old_valid = task_ledger.LEDGER_PATH.read_bytes()
    record.add_round("newer round")
    task_ledger.save_goal(
        record,
        protected=True,
        receipt_authority=authority,
    )
    current = task_ledger.LEDGER_PATH.read_bytes()
    task_ledger._pending_goal_path().write_bytes(old_valid)

    with pytest.raises(ElsieReceiptError, match="recovery sequence"):
        task_ledger.prepare_protected_goal_store(
            receipt_authority=authority,
        )

    assert task_ledger.LEDGER_PATH.read_bytes() == current
    assert task_ledger._pending_goal_path().read_bytes() == old_valid


def test_protected_goal_pending_wrong_or_missing_key_is_nonmutating(
    monkeypatch,
) -> None:
    authority, _store = _shared_authority(b"a")
    original_publish = task_ledger.publish_elsie_staged_file

    def fail_publish(*_args, **_kwargs):
        raise ElsieReceiptError("simulated pending goal")

    monkeypatch.setattr(task_ledger, "publish_elsie_staged_file", fail_publish)
    with pytest.raises(ElsieReceiptError, match="pending goal"):
        task_ledger.save_goal(
            task_ledger.GoalRecord(goal="explicit pending goal"),
            protected=True,
            receipt_authority=authority,
        )
    pending = task_ledger._pending_goal_path()
    before = pending.read_bytes()
    monkeypatch.setattr(
        task_ledger,
        "publish_elsie_staged_file",
        original_publish,
    )

    with pytest.raises(ElsieReceiptError, match="binding mismatch"):
        task_ledger.prepare_protected_goal_store(
            receipt_authority=_authority(b"b"),
        )
    assert pending.read_bytes() == before

    def missing_existing(cls):
        raise ElsieReceiptError("existing key missing")

    def forbidden_create(cls):
        pytest.fail("pending goal recovery must not create a key")

    monkeypatch.setattr(
        task_ledger.ElsieReceiptAuthority,
        "from_existing_key_store",
        classmethod(missing_existing),
    )
    monkeypatch.setattr(
        task_ledger.ElsieReceiptAuthority,
        "from_key_store",
        classmethod(forbidden_create),
    )
    with pytest.raises(ElsieReceiptError, match="existing key missing"):
        task_ledger.prepare_protected_goal_store()
    assert pending.read_bytes() == before


def test_protected_goal_missing_anchor_rejects_without_rewrite() -> None:
    authority, store = _shared_authority()
    task_ledger.save_goal(
        task_ledger.GoalRecord(goal="explicit anchored goal"),
        protected=True,
        receipt_authority=authority,
    )
    before = task_ledger.LEDGER_PATH.read_bytes()
    store._anchors.clear()

    with pytest.raises(ElsieReceiptError, match="rollback or rewrite"):
        task_ledger.load_goal(
            protected=True,
            receipt_authority=authority,
        )

    assert task_ledger.LEDGER_PATH.read_bytes() == before


def test_protected_goal_preflight_detects_deleted_anchored_ledger() -> None:
    authority, _store = _shared_authority()
    task_ledger.save_goal(
        task_ledger.GoalRecord(goal="explicit deletion goal"),
        protected=True,
        receipt_authority=authority,
    )
    task_ledger.LEDGER_PATH.unlink()

    with pytest.raises(ElsieReceiptError, match="ledger is missing"):
        task_ledger.prepare_protected_goal_store(
            receipt_authority=authority,
        )

    assert not task_ledger.LEDGER_PATH.exists()
    assert not task_ledger._pending_goal_path().exists()


def test_protected_goal_preflight_detects_corruption_without_backup() -> None:
    authority, _store = _shared_authority()
    task_ledger.save_goal(
        task_ledger.GoalRecord(goal="explicit corrupt goal"),
        protected=True,
        receipt_authority=authority,
    )
    corrupt = b"SECRET_CORRUPT_GOAL_CANARY{"
    task_ledger.LEDGER_PATH.write_bytes(corrupt)

    with pytest.raises(ElsieReceiptError, match="malformed"):
        task_ledger.prepare_protected_goal_store(
            receipt_authority=authority,
        )

    assert task_ledger.LEDGER_PATH.read_bytes() == corrupt
    assert not task_ledger.LEDGER_PATH.with_suffix(".json.corrupt").exists()
