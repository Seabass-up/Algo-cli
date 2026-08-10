"""Durable Ada task ledger for /goal and long autonomous runs.

`/goal` and agent pipelines previously held all state in RAM: a crash, an
exit, or hitting the round cap lost the plan entirely. This module persists a
single active goal's progress to ``CONFIG_DIR/task_ledger.json`` so a run can
be inspected with ``/goal status`` and continued with ``/goal resume`` across
process restarts.

The ledger holds at most one active goal at a time (the common case for a
terminal session). Completing, blocking, or starting a new goal overwrites it.
Writes are atomic (tmp + os.replace) via config._atomic_write_text.
"""

from __future__ import annotations

import json
import hmac
import os
import stat
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import (
    CONFIG_DIR,
    _atomic_write_text,
    _exclusive_state_lock,
    _load_json_file,
    _state_descriptor_payload,
)
from .grace_memory_receipts import (
    ElsieReceiptAuthority,
    ElsieReceiptError,
    ReceiptNamespace,
    advance_elsie_store_anchor,
    elsie_staging_path,
    is_hmac_receipt,
    load_elsie_store_anchor,
    publish_elsie_staged_file,
    require_elsie_store_anchor,
)

LEDGER_PATH = CONFIG_DIR / "task_ledger.json"
LEDGER_SCHEMA_VERSION = 1
LEGACY_PROTECTED_LEDGER_SCHEMA_VERSION = 2
PROTECTED_LEDGER_SCHEMA_VERSION = 3
MAX_LEDGER_BYTES = 1_048_576
_LEDGER_MISSING = object()

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_BLOCKED = "blocked"
STATUS_STOPPED = "stopped"  # user-interrupted or round cap reached


@dataclass
class GoalRecord:
    # The goal is explicit user-authored operational state and intentionally
    # remains readable. Model-derived reasons/history are projected below when
    # Echo is the selected memory authority.
    goal: str
    status: str = STATUS_RUNNING
    rounds_done: int = 0
    max_rounds: int = 10
    reason: str = ""  # blocked/stopped explanation
    cwd: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)  # per-round notes
    reason_receipt: str = ""

    def add_round(self, summary: str) -> None:
        self.rounds_done += 1
        self.updated_at = time.time()
        self.history.append({"round": self.rounds_done, "at": self.updated_at, "summary": summary[:500]})

    @property
    def is_open(self) -> bool:
        return self.status in {STATUS_RUNNING, STATUS_STOPPED}


def _protected_history_entry(
    entry: object,
    authority: ElsieReceiptAuthority,
) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    round_number = entry.get("round")
    timestamp = entry.get("at")
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or round_number < 0
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or float(timestamp) < 0
    ):
        return None
    existing_receipt = entry.get("summary_receipt")
    if is_hmac_receipt(existing_receipt) and "summary" not in entry:
        receipt = str(existing_receipt)
        chars = max(0, int(entry.get("summary_chars") or 0))
    else:
        summary = str(entry.get("summary") or "")[:500]
        receipt = authority.receipt(ReceiptNamespace.GOAL_HISTORY, summary)
        chars = len(summary)
    return {
        "round": round_number,
        "at": float(timestamp),
        "summary_chars": chars,
        "summary_receipt": receipt,
    }


def _protected_goal_payload(
    record: GoalRecord,
    authority: ElsieReceiptAuthority,
) -> dict[str, Any]:
    reason = str(record.reason or "")[:500]
    reason_receipt = (
        record.reason_receipt
        if not reason and is_hmac_receipt(record.reason_receipt)
        else authority.receipt(ReceiptNamespace.GOAL_REASON, reason)
    )
    history = [
        projected
        for entry in record.history[-max(1, record.max_rounds) :]
        if (projected := _protected_history_entry(entry, authority)) is not None
    ]
    return {
        "goal": str(record.goal),
        "status": str(record.status),
        "rounds_done": int(record.rounds_done),
        "max_rounds": int(record.max_rounds),
        "reason": "",
        "reason_receipt": reason_receipt,
        "cwd": str(record.cwd),
        "created_at": float(record.created_at),
        "updated_at": float(record.updated_at),
        "history": history,
    }


def _ledger_subject() -> str:
    return os.path.abspath(os.fspath(LEDGER_PATH))


def _protected_unsigned_payload(
    goal: dict[str, Any] | None,
    authority: ElsieReceiptAuthority,
    *,
    sequence: int,
    previous_store_receipt: str,
) -> dict[str, Any]:
    return {
        "schema_version": PROTECTED_LEDGER_SCHEMA_VERSION,
        "protected": True,
        "receipt_binding": authority.binding.as_dict(),
        "store_sequence": sequence,
        "previous_store_receipt": previous_store_receipt,
        "goal": goal,
    }


def _protected_ledger_payload(
    goal: dict[str, Any] | None,
    authority: ElsieReceiptAuthority,
    *,
    sequence: int,
    previous_store_receipt: str,
) -> dict[str, Any]:
    unsigned = _protected_unsigned_payload(
        goal,
        authority,
        sequence=sequence,
        previous_store_receipt=previous_store_receipt,
    )
    return {
        **unsigned,
        "store_receipt": authority.store_receipt(
            ReceiptNamespace.GOAL_STORE,
            unsigned,
        ),
    }


def _read_protected_ledger(path: Any = None) -> object:
    selected = LEDGER_PATH if path is None else path
    try:
        raw = _state_descriptor_payload(selected, max_bytes=MAX_LEDGER_BYTES)
    except FileNotFoundError:
        return _LEDGER_MISSING
    except OSError as exc:
        raise ElsieReceiptError("protected goal ledger path is unsafe") from exc
    try:
        return json.loads(raw.decode("utf-8", errors="strict"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ElsieReceiptError("protected goal ledger is malformed") from exc


def _validate_protected_ledger(
    data: object,
    authority: ElsieReceiptAuthority,
    *,
    anchor_store: Any | None = None,
    require_anchor_match: bool = True,
) -> GoalRecord | None:
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "protected",
        "receipt_binding",
        "store_sequence",
        "previous_store_receipt",
        "goal",
        "store_receipt",
    }:
        raise ElsieReceiptError("protected goal ledger fields are invalid")
    if data.get("schema_version") != PROTECTED_LEDGER_SCHEMA_VERSION or data.get("protected") is not True:
        raise ElsieReceiptError("protected goal ledger marker is invalid")
    authority.require_binding(data.get("receipt_binding"))
    sequence = data.get("store_sequence")
    previous = data.get("previous_store_receipt")
    receipt = data.get("store_receipt")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
        or not isinstance(previous, str)
        or (sequence == 1 and previous)
        or (sequence > 1 and not is_hmac_receipt(previous))
        or not is_hmac_receipt(receipt)
    ):
        raise ElsieReceiptError("protected goal ledger sequence is invalid")
    unsigned = {key: value for key, value in data.items() if key != "store_receipt"}
    expected = authority.store_receipt(ReceiptNamespace.GOAL_STORE, unsigned)
    if not hmac.compare_digest(str(receipt), expected):
        raise ElsieReceiptError("protected goal ledger authentication failed")
    if require_anchor_match:
        require_elsie_store_anchor(
            authority,
            ReceiptNamespace.GOAL_STORE,
            subject=_ledger_subject(),
            sequence=sequence,
            store_receipt=str(receipt),
            anchor_store=anchor_store,
        )
    goal_data = data.get("goal")
    if goal_data is None:
        return None
    record = _record_from_mapping(goal_data, protected=True)
    if record is None:
        raise ElsieReceiptError("protected goal ledger is malformed")
    setattr(record, "_elsie_store_sequence", sequence)
    setattr(record, "_elsie_store_receipt", str(receipt))
    return record


def _pending_goal_path() -> Any:
    return elsie_staging_path(LEDGER_PATH)


def _pending_goal_exists() -> bool:
    pending = _pending_goal_path()
    try:
        info = pending.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ElsieReceiptError("protected goal recovery path is unsafe") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ElsieReceiptError("protected goal recovery path is unsafe")
    return True


def _recover_protected_goal_store_unlocked(
    authority: ElsieReceiptAuthority,
    *,
    anchor_store: Any | None = None,
) -> bool:
    """Reconcile one authenticated staged goal transaction under the lock."""

    pending_path = _pending_goal_path()
    pending = _read_protected_ledger(pending_path)
    if pending is _LEDGER_MISSING:
        return False
    if not isinstance(pending, dict):
        raise ElsieReceiptError("protected goal recovery state is malformed")
    _validate_protected_ledger(
        pending,
        authority,
        anchor_store=anchor_store,
        require_anchor_match=False,
    )
    sequence = int(pending["store_sequence"])
    previous = str(pending["previous_store_receipt"])
    receipt = str(pending["store_receipt"])
    head = load_elsie_store_anchor(
        authority,
        ReceiptNamespace.GOAL_STORE,
        subject=_ledger_subject(),
        anchor_store=anchor_store,
    )
    receipt_hex = receipt.removeprefix("hmac-sha256:")
    if head is not None and (head.sequence == sequence and hmac.compare_digest(head.head_digest, receipt_hex)):
        pass
    else:
        if head is None:
            if sequence != 1 or previous:
                raise ElsieReceiptError("protected goal recovery sequence is invalid")
        else:
            anchored_receipt = "hmac-sha256:" + head.head_digest
            if sequence != head.sequence + 1 or not hmac.compare_digest(previous, anchored_receipt):
                raise ElsieReceiptError("protected goal recovery sequence is invalid")
        advance_elsie_store_anchor(
            authority,
            ReceiptNamespace.GOAL_STORE,
            subject=_ledger_subject(),
            sequence=sequence,
            previous_store_receipt=previous,
            store_receipt=receipt,
            anchor_store=anchor_store,
        )
    expected_payload = json.dumps(pending, indent=2).encode("utf-8")
    publish_elsie_staged_file(
        pending_path,
        LEDGER_PATH,
        expected_payload=expected_payload,
    )
    published = _read_protected_ledger()
    _validate_protected_ledger(
        published,
        authority,
        anchor_store=anchor_store,
    )
    return True


def prepare_protected_goal_store(
    *,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> bool:
    """Explicitly recover and validate the protected-goal store.

    Missing state is a non-mutating no-op when the persistent receipt key is
    proven absent. If that key exists, the external anchor is checked so a
    deleted local ledger cannot appear empty. Ordinary reads never call this
    API implicitly.
    """

    pending_exists = _pending_goal_exists()
    initial = _read_protected_ledger()
    authority = receipt_authority
    if authority is None:
        if pending_exists or initial is not _LEDGER_MISSING:
            authority = ElsieReceiptAuthority.from_existing_key_store()
        else:
            authority = ElsieReceiptAuthority.from_optional_existing_key_store()
            if authority is None:
                return False
    with _exclusive_state_lock(LEDGER_PATH):
        recovered = _recover_protected_goal_store_unlocked(
            authority,
            anchor_store=anchor_store,
        )
        current = _read_protected_ledger()
        if current is _LEDGER_MISSING:
            head = load_elsie_store_anchor(
                authority,
                ReceiptNamespace.GOAL_STORE,
                subject=_ledger_subject(),
                anchor_store=anchor_store,
            )
            if head is not None:
                raise ElsieReceiptError("protected goal ledger is missing")
            return recovered
        if not isinstance(current, dict):
            raise ElsieReceiptError("protected goal ledger is malformed")
        if current.get("schema_version") != PROTECTED_LEDGER_SCHEMA_VERSION:
            raise ElsieReceiptError("legacy goal ledger requires safe migration")
        _validate_protected_ledger(
            current,
            authority,
            anchor_store=anchor_store,
        )
        return recovered


def _publish_protected_goal_payload_unlocked(
    payload: dict[str, Any],
    authority: ElsieReceiptAuthority,
    *,
    anchor_store: Any | None = None,
) -> None:
    """Stage, anchor, and publish one protected goal payload transaction."""

    pending_path = _pending_goal_path()
    serialized = json.dumps(payload, indent=2)
    _atomic_write_text(pending_path, serialized)
    staged = _read_protected_ledger(pending_path)
    _validate_protected_ledger(
        staged,
        authority,
        anchor_store=anchor_store,
        require_anchor_match=False,
    )
    sequence = int(payload["store_sequence"])
    previous = str(payload["previous_store_receipt"])
    receipt = str(payload["store_receipt"])
    advance_elsie_store_anchor(
        authority,
        ReceiptNamespace.GOAL_STORE,
        subject=_ledger_subject(),
        sequence=sequence,
        previous_store_receipt=previous,
        store_receipt=receipt,
        anchor_store=anchor_store,
    )
    publish_elsie_staged_file(
        pending_path,
        LEDGER_PATH,
        expected_payload=serialized.encode("utf-8"),
    )
    published = _read_protected_ledger()
    _validate_protected_ledger(
        published,
        authority,
        anchor_store=anchor_store,
    )


def save_goal(
    record: GoalRecord,
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> None:
    record.updated_at = time.time()
    if protected:
        authority = receipt_authority or ElsieReceiptAuthority.from_key_store()
        with _exclusive_state_lock(LEDGER_PATH):
            _recover_protected_goal_store_unlocked(
                authority,
                anchor_store=anchor_store,
            )
            current = _read_protected_ledger()
            current_sequence = 0
            previous_receipt = ""
            if current is not _LEDGER_MISSING:
                if not isinstance(current, dict):
                    raise ElsieReceiptError("protected goal ledger is malformed")
                version = current.get("schema_version")
                if version == PROTECTED_LEDGER_SCHEMA_VERSION:
                    existing = _validate_protected_ledger(
                        current,
                        authority,
                        anchor_store=anchor_store,
                    )
                    current_sequence = int(current["store_sequence"])
                    previous_receipt = str(current["store_receipt"])
                    cursor = getattr(record, "_elsie_store_sequence", 0)
                    if cursor and cursor != current_sequence:
                        raise ElsieReceiptError("protected goal update is stale")
                    if existing is None and cursor:
                        raise ElsieReceiptError("protected goal was already cleared")
                elif version in {
                    LEDGER_SCHEMA_VERSION,
                    LEGACY_PROTECTED_LEDGER_SCHEMA_VERSION,
                }:
                    # Legacy ledgers had no store-level authentication. They
                    # may be projected, but never trusted as resumable state.
                    if (
                        load_elsie_store_anchor(
                            authority,
                            ReceiptNamespace.GOAL_STORE,
                            subject=_ledger_subject(),
                            anchor_store=anchor_store,
                        )
                        is not None
                    ):
                        raise ElsieReceiptError("legacy goal ledger conflicts with protected anchor")
                else:
                    raise ElsieReceiptError("unsupported protected goal ledger schema")
            else:
                existing_head = load_elsie_store_anchor(
                    authority,
                    ReceiptNamespace.GOAL_STORE,
                    subject=_ledger_subject(),
                    anchor_store=anchor_store,
                )
                if existing_head is not None:
                    raise ElsieReceiptError("protected goal ledger is missing")
            sequence = current_sequence + 1
            payload = _protected_ledger_payload(
                _protected_goal_payload(record, authority),
                authority,
                sequence=sequence,
                previous_store_receipt=previous_receipt,
            )
            _publish_protected_goal_payload_unlocked(
                payload,
                authority,
                anchor_store=anchor_store,
            )
            setattr(record, "_elsie_store_sequence", sequence)
            setattr(record, "_elsie_store_receipt", str(payload["store_receipt"]))
        return
    serialized = asdict(record)
    serialized.pop("reason_receipt", None)
    payload = {"schema_version": LEDGER_SCHEMA_VERSION, "goal": serialized}
    _atomic_write_text(LEDGER_PATH, json.dumps(payload, indent=2))


def _record_from_mapping(goal_data: object, *, protected: bool) -> GoalRecord | None:
    if not isinstance(goal_data, dict) or not isinstance(goal_data.get("goal"), str):
        return None
    if not goal_data.get("goal"):
        return None
    known = GoalRecord.__dataclass_fields__.keys()
    filtered = {k: v for k, v in goal_data.items() if k in known}
    try:
        record = GoalRecord(**filtered)
    except (TypeError, ValueError):
        return None
    if record.status not in {STATUS_RUNNING, STATUS_COMPLETE, STATUS_BLOCKED, STATUS_STOPPED}:
        return None
    if (
        isinstance(record.rounds_done, bool)
        or not isinstance(record.rounds_done, int)
        or record.rounds_done < 0
        or isinstance(record.max_rounds, bool)
        or not isinstance(record.max_rounds, int)
        or not 1 <= record.max_rounds <= 100_000
        or record.rounds_done > record.max_rounds
        or not isinstance(record.history, list)
        or len(record.history) > record.max_rounds
    ):
        return None
    if protected:
        if set(goal_data) != {
            "goal",
            "status",
            "rounds_done",
            "max_rounds",
            "reason",
            "reason_receipt",
            "cwd",
            "created_at",
            "updated_at",
            "history",
        }:
            return None
        if record.reason:
            return None
        if not is_hmac_receipt(record.reason_receipt):
            return None
        for entry in record.history:
            if not isinstance(entry, dict) or set(entry) != {
                "round",
                "at",
                "summary_chars",
                "summary_receipt",
            }:
                return None
            if not is_hmac_receipt(entry.get("summary_receipt")):
                return None
    return record


def load_goal(
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
    migrate_legacy: bool = True,
) -> GoalRecord | None:
    if protected:
        if _pending_goal_exists():
            raise ElsieReceiptError("protected goal recovery is pending")
        data = _read_protected_ledger()
        if data is _LEDGER_MISSING:
            return None
        if not isinstance(data, dict):
            raise ElsieReceiptError("protected goal ledger is malformed")
        authority = receipt_authority or ElsieReceiptAuthority.from_existing_key_store()
        schema_version = data.get("schema_version")
        goal_data = data.get("goal")
        if schema_version == PROTECTED_LEDGER_SCHEMA_VERSION:
            return _validate_protected_ledger(
                data,
                authority,
                anchor_store=anchor_store,
            )
        if schema_version not in {
            LEDGER_SCHEMA_VERSION,
            LEGACY_PROTECTED_LEDGER_SCHEMA_VERSION,
        }:
            raise ElsieReceiptError("unsupported protected goal ledger schema")
        legacy_protected = schema_version == LEGACY_PROTECTED_LEDGER_SCHEMA_VERSION
        if legacy_protected:
            if (
                set(data)
                != {
                    "schema_version",
                    "protected",
                    "receipt_binding",
                    "goal",
                }
                or data.get("protected") is not True
            ):
                raise ElsieReceiptError("legacy protected goal ledger fields are invalid")
            authority.require_binding(data.get("receipt_binding"))
        # Legacy structural progress was not store-authenticated. Preserve the
        # explicit user goal/cwd but force a non-resumable state so replaying a
        # modified old file cannot duplicate agent mutations.
        legacy = _record_from_mapping(goal_data, protected=legacy_protected)
        if legacy is None:
            raise ElsieReceiptError("legacy goal ledger is malformed")
        if not migrate_legacy:
            raise ElsieReceiptError("legacy goal ledger requires safe migration")
        legacy.status = STATUS_BLOCKED
        legacy.reason = "legacy goal progress requires an explicit restart"
        legacy.rounds_done = min(legacy.rounds_done, legacy.max_rounds)
        legacy.history = []
        legacy.reason_receipt = ""
        save_goal(
            legacy,
            protected=True,
            receipt_authority=authority,
            anchor_store=anchor_store,
        )
        return load_goal(
            protected=True,
            receipt_authority=authority,
            anchor_store=anchor_store,
            migrate_legacy=False,
        )
    data = _load_json_file(LEDGER_PATH, None, preserve_corrupt=False)
    if not isinstance(data, dict):
        return None
    schema_version = data.get("schema_version")
    goal_data = data.get("goal")
    if schema_version not in {
        LEDGER_SCHEMA_VERSION,
        LEGACY_PROTECTED_LEDGER_SCHEMA_VERSION,
        PROTECTED_LEDGER_SCHEMA_VERSION,
    }:
        return None
    if schema_version in {
        LEGACY_PROTECTED_LEDGER_SCHEMA_VERSION,
        PROTECTED_LEDGER_SCHEMA_VERSION,
    }:
        # Protected records are safe to inspect structurally even after Echo is
        # disabled, but their content receipts are deliberately not reversed.
        if goal_data is None:
            return None
        return _record_from_mapping(goal_data, protected=True)
    return _record_from_mapping(goal_data, protected=False)


def goal_status_projection(record: GoalRecord, *, protected: bool = False) -> dict[str, Any]:
    """Return bounded display fields without exposing protected derived text."""

    projection: dict[str, Any] = {
        "goal": record.goal,
        "status": record.status,
        "rounds_done": record.rounds_done,
        "max_rounds": record.max_rounds,
        "cwd": record.cwd,
        "reason": record.reason if not protected else "",
        "reason_receipt": record.reason_receipt if protected else "",
        "last_summary": "",
        "last_summary_receipt": "",
    }
    if record.history:
        last = record.history[-1]
        if isinstance(last, dict):
            if protected:
                projection["last_summary_receipt"] = str(last.get("summary_receipt") or "")
            else:
                projection["last_summary"] = str(last.get("summary") or "")[:160]
    if protected and (
        not is_hmac_receipt(projection["reason_receipt"])
        or (record.history and not is_hmac_receipt(projection["last_summary_receipt"]))
    ):
        raise ElsieReceiptError("protected goal status projection is malformed")
    return projection


def clear_goal(
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> bool:
    if protected:
        initial = _read_protected_ledger()
        if initial is _LEDGER_MISSING and not _pending_goal_exists():
            return False
        authority = receipt_authority or ElsieReceiptAuthority.from_existing_key_store()
        with _exclusive_state_lock(LEDGER_PATH):
            _recover_protected_goal_store_unlocked(
                authority,
                anchor_store=anchor_store,
            )
            current = _read_protected_ledger()
            if current is _LEDGER_MISSING:
                return False
            record = _validate_protected_ledger(
                current,
                authority,
                anchor_store=anchor_store,
            )
            if record is None:
                return False
            if not isinstance(current, dict):  # pragma: no cover - validated above
                raise ElsieReceiptError("protected goal ledger is malformed")
            sequence = int(current["store_sequence"]) + 1
            previous = str(current["store_receipt"])
            tombstone = _protected_ledger_payload(
                None,
                authority,
                sequence=sequence,
                previous_store_receipt=previous,
            )
            _publish_protected_goal_payload_unlocked(
                tombstone,
                authority,
                anchor_store=anchor_store,
            )
            return True
    if not LEDGER_PATH.exists():
        return False
    try:
        LEDGER_PATH.lstat()
        if not LEDGER_PATH.is_file() or LEDGER_PATH.is_symlink():
            return False
        LEDGER_PATH.unlink()
        return True
    except OSError:
        return False
