"""Skill crystallization for Algo CLI.

After the user opts in with ``/skills on``, completed agent runs are summarized
locally. Every few runs, a local-only crystallizer reviews that bounded history,
extracts reusable discoveries — paths, config keys, command sequences, and
environment-specific workarounds — and places candidates in a non-indexed
quarantine. Only an explicit ``/skills approve NAME`` promotes a candidate into
~/.algo_cli/skills/ for harness retrieval.

Speed notes: completed runs are appended in bounded batches to a private JSONL
store. The crystallizer is one structured call against the small local
maintenance model, fired only every N substantive runs.
"""

from __future__ import annotations

import json
import hmac
import math
import os
import re
import stat
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import (
    CONFIG_DIR,
    _atomic_write_text,
    _exclusive_state_lock,
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
from .private_event_store import (
    PrivateEventStore,
    PrivateEventStoreError,
    RetentionPolicy,
)


SKILLS_DIR = CONFIG_DIR / "skills"
SKILL_QUARANTINE_DIR = CONFIG_DIR / "skill_quarantine"
RUN_HISTORY_PATH = CONFIG_DIR / "run_history.jsonl"
PRIVATE_RUN_HISTORY_PATH = CONFIG_DIR / "private" / "run_history.jsonl"
PROTECTED_RUN_HISTORY_PATH = CONFIG_DIR / "private" / "protected_run_history.jsonl"
LEGACY_SKILL_QUARANTINE_DIR = CONFIG_DIR / "legacy_skill_quarantine"
LEGACY_PROTECTED_RUN_SCHEMA_VERSION = 2
PROTECTED_RUN_SCHEMA_VERSION = 3
PROTECTED_RUN_STORE_SCHEMA_VERSION = 3
PROTECTED_RUN_HISTORY_MAX_BYTES = 512 * 1024
_PROTECTED_HISTORY_MISSING = object()

RUN_HISTORY_LIMIT = 60  # cap the JSONL file
CRYSTALLIZE_LOOKBACK = 6  # recent runs the crystallizer reviews
MAX_SKILLS_PER_PASS = 5  # guard against a model spamming files
GOAL_PREVIEW_CHARS = 200
OUTCOME_PREVIEW_CHARS = 320
MAX_CANDIDATE_ITEMS = 10
MAX_CANDIDATE_ITEM_CHARS = 400
MAX_CANDIDATE_TOTAL_CHARS = 4_000
_UNSAFE_CANDIDATE_RE = re.compile(
    r"(?i)\b(?:ignore|override|disregard)\b.{0,40}\b(?:system|developer|previous|safety)\b|"
    r"\b(?:password|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|private[_ -]?key)\b\s*[:=]"
)

# (system_prompt, user_prompt) -> assistant content
LLMFn = Callable[[str, str], str]


def _run_history_store() -> PrivateEventStore:
    return PrivateEventStore(
        PRIVATE_RUN_HISTORY_PATH,
        policy=RetentionPolicy(
            max_records=RUN_HISTORY_LIMIT,
            max_bytes=512 * 1024,
            max_age_seconds=180 * 24 * 60 * 60,
        ),
    )


def _safe_unlink_regular_or_link(path: Path) -> bool:
    try:
        descriptor = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if not (stat.S_ISREG(descriptor.st_mode) or stat.S_ISLNK(descriptor.st_mode)):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _path_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ElsieReceiptError("protected skill history path is unavailable") from exc
    return True


def _purge_untrusted_skill_candidates() -> int:
    try:
        descriptor = SKILL_QUARANTINE_DIR.lstat()
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    if stat.S_ISLNK(descriptor.st_mode) or not stat.S_ISDIR(descriptor.st_mode):
        return 0
    removed = 0
    try:
        paths = tuple(SKILL_QUARANTINE_DIR.iterdir())
    except OSError:
        return 0
    for path in paths:
        if path.suffix == ".json" and _safe_unlink_regular_or_link(path):
            removed += 1
    return removed


def _quarantine_unproven_active_skills() -> int:
    """Move legacy user skills outside indexed roots without reading content."""

    try:
        descriptor = SKILLS_DIR.lstat()
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    if stat.S_ISLNK(descriptor.st_mode):
        if not _safe_unlink_regular_or_link(SKILLS_DIR):
            raise ElsieReceiptError("active skill root symlink could not be disabled")
        return 1
    if not stat.S_ISDIR(descriptor.st_mode):
        raise ElsieReceiptError("active skill root is unsafe")
    try:
        LEGACY_SKILL_QUARANTINE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
        quarantine_info = LEGACY_SKILL_QUARANTINE_DIR.lstat()
        if stat.S_ISLNK(quarantine_info.st_mode) or not stat.S_ISDIR(quarantine_info.st_mode):
            raise ElsieReceiptError("legacy skill quarantine path is unsafe")
        if os.name == "posix":
            os.chmod(LEGACY_SKILL_QUARANTINE_DIR, 0o700)
    except OSError as exc:
        raise ElsieReceiptError("legacy skill quarantine is unavailable") from exc
    moved = 0
    try:
        paths = tuple(SKILLS_DIR.iterdir())
    except OSError as exc:
        raise ElsieReceiptError("active skill inventory is unavailable") from exc
    for path in paths:
        if path.suffix.casefold() != ".md":
            continue
        destination: Path | None = None
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                path.unlink()
                moved += 1
                continue
            # A hard-linked file cannot be isolated by moving only this name.
            # Drop the indexed name instead of retaining a second plaintext
            # alias in the legacy quarantine.
            if info.st_nlink != 1:
                path.unlink()
                moved += 1
                continue
            destination = LEGACY_SKILL_QUARANTINE_DIR / f"legacy-{uuid.uuid4().hex}.md"
            os.replace(path, destination)
            if os.name == "posix":
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                )
                destination_fd = os.open(destination, flags)
                try:
                    moved_info = os.fstat(destination_fd)
                    if (
                        not stat.S_ISREG(moved_info.st_mode)
                        or moved_info.st_dev != info.st_dev
                        or moved_info.st_ino != info.st_ino
                        or moved_info.st_nlink != 1
                    ):
                        raise ElsieReceiptError("legacy active skill changed during quarantine")
                    os.fchmod(destination_fd, 0o600)
                finally:
                    os.close(destination_fd)
            moved += 1
        except (OSError, ElsieReceiptError) as exc:
            if destination is not None:
                _safe_unlink_regular_or_link(destination)
            raise ElsieReceiptError("legacy active skill could not be quarantined") from exc
    return moved


def _validate_protected_run(
    record: object,
    authority: ElsieReceiptAuthority,
) -> bool:
    if not isinstance(record, dict):
        return False
    if set(record) != {
        "schema_version",
        "receipt_binding",
        "timestamp",
        "goal_chars",
        "goal_receipt",
        "tool_calls",
        "outcome_chars",
        "outcome_receipt",
        "iterations",
        "duration_ms",
        "crystallizable",
    }:
        return False
    try:
        authority.require_binding(record.get("receipt_binding"))
    except ElsieReceiptError:
        return False
    if record.get("schema_version") != PROTECTED_RUN_SCHEMA_VERSION:
        return False
    if not is_hmac_receipt(record.get("goal_receipt")):
        return False
    if not is_hmac_receipt(record.get("outcome_receipt")):
        return False
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str) or not 1 <= len(timestamp) <= 64:
        return False
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    goal_chars = record.get("goal_chars")
    outcome_chars = record.get("outcome_chars")
    iterations = record.get("iterations")
    duration_ms = record.get("duration_ms")
    if (
        isinstance(goal_chars, bool)
        or not isinstance(goal_chars, int)
        or not 0 <= goal_chars <= GOAL_PREVIEW_CHARS
        or isinstance(outcome_chars, bool)
        or not isinstance(outcome_chars, int)
        or not 0 <= outcome_chars <= OUTCOME_PREVIEW_CHARS
        or isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or not 0 <= iterations <= 1_000_000
        or isinstance(duration_ms, bool)
        or not isinstance(duration_ms, (int, float))
        or not math.isfinite(float(duration_ms))
        or not 0 <= float(duration_ms) <= 31_536_000_000.0
        or record.get("crystallizable") is not False
    ):
        return False
    calls = record.get("tool_calls")
    if not isinstance(calls, list) or len(calls) > 256:
        return False
    return all(
        isinstance(call, dict)
        and set(call)
        == {
            "name_chars",
            "status_chars",
            "identity_receipt",
            "args_receipt",
            "explicit_memory_write",
        }
        and isinstance(call.get("name_chars"), int)
        and not isinstance(call.get("name_chars"), bool)
        and 1 <= int(call["name_chars"]) <= 128
        and isinstance(call.get("status_chars"), int)
        and not isinstance(call.get("status_chars"), bool)
        and 1 <= int(call["status_chars"]) <= 64
        and is_hmac_receipt(call.get("identity_receipt"))
        and is_hmac_receipt(call.get("args_receipt"))
        and isinstance(call.get("explicit_memory_write"), bool)
        for call in calls
    )


def _validate_legacy_protected_run(
    record: object,
    authority: ElsieReceiptAuthority,
) -> bool:
    """Recognize the former content-free JSONL event for one-way purge only."""

    if not isinstance(record, dict):
        return False
    if record.get("schema_version") != LEGACY_PROTECTED_RUN_SCHEMA_VERSION:
        return False
    calls = record.get("tool_calls")
    if not isinstance(calls, list) or len(calls) > 256:
        return False
    projected_calls: list[dict[str, Any]] = []
    for call in calls:
        if (
            not isinstance(call, dict)
            or set(call) != {"name", "status", "args_receipt", "explicit_memory_write"}
            or not isinstance(call.get("name"), str)
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", str(call.get("name"))) is None
            or not isinstance(call.get("status"), str)
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", str(call.get("status"))) is None
            or not is_hmac_receipt(call.get("args_receipt"))
            or not isinstance(call.get("explicit_memory_write"), bool)
        ):
            return False
        name = str(call["name"])
        status = str(call["status"])
        projected_calls.append(
            {
                "name_chars": len(name),
                "status_chars": len(status),
                "identity_receipt": authority.receipt(
                    ReceiptNamespace.SKILL_TOOL_IDENTITY,
                    {"name": name, "status": status},
                ),
                "args_receipt": call["args_receipt"],
                "explicit_memory_write": call["explicit_memory_write"],
            }
        )
    projected = dict(record)
    projected["schema_version"] = PROTECTED_RUN_SCHEMA_VERSION
    projected["tool_calls"] = projected_calls
    return _validate_protected_run(projected, authority)


def _protected_history_subject() -> str:
    return os.path.abspath(os.fspath(PROTECTED_RUN_HISTORY_PATH))


def _protected_history_pending_path() -> Path:
    return elsie_staging_path(PROTECTED_RUN_HISTORY_PATH)


def _serialize_protected_history(payload: dict[str, Any]) -> bytes:
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ElsieReceiptError("protected skill history is not canonical JSON") from exc
    if not serialized or len(serialized) > PROTECTED_RUN_HISTORY_MAX_BYTES:
        raise ElsieReceiptError("protected skill history exceeds the bounded size")
    return serialized


def _protected_history_bytes(path: Path) -> bytes:
    try:
        payload = _state_descriptor_payload(
            path,
            max_bytes=PROTECTED_RUN_HISTORY_MAX_BYTES,
        )
        info = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ElsieReceiptError("protected skill history path is unsafe") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        or (os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077)
    ):
        raise ElsieReceiptError("protected skill history path is unsafe")
    if not payload:
        raise ElsieReceiptError("protected skill history is malformed")
    return payload


def _decode_protected_history(raw: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8", errors="strict"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ElsieReceiptError("protected skill history is malformed") from exc
    if not isinstance(decoded, dict):
        raise ElsieReceiptError("protected skill history is malformed")
    if not hmac.compare_digest(_serialize_protected_history(decoded), raw):
        raise ElsieReceiptError("protected skill history encoding is noncanonical")
    return decoded


def _read_protected_history(path: Path | None = None) -> object:
    selected = PROTECTED_RUN_HISTORY_PATH if path is None else path
    try:
        raw = _protected_history_bytes(selected)
    except FileNotFoundError:
        return _PROTECTED_HISTORY_MISSING
    return _decode_protected_history(raw)


def _protected_history_unsigned_payload(
    runs: list[dict[str, Any]],
    authority: ElsieReceiptAuthority,
    *,
    sequence: int,
    previous_store_receipt: str,
) -> dict[str, Any]:
    return {
        "schema_version": PROTECTED_RUN_STORE_SCHEMA_VERSION,
        "protected": True,
        "receipt_binding": authority.binding.as_dict(),
        "store_sequence": sequence,
        "previous_store_receipt": previous_store_receipt,
        "runs": runs,
    }


def _protected_history_payload(
    runs: list[dict[str, Any]],
    authority: ElsieReceiptAuthority,
    *,
    sequence: int,
    previous_store_receipt: str,
) -> dict[str, Any]:
    unsigned = _protected_history_unsigned_payload(
        runs,
        authority,
        sequence=sequence,
        previous_store_receipt=previous_store_receipt,
    )
    return {
        **unsigned,
        "store_receipt": authority.store_receipt(
            ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
            unsigned,
        ),
    }


def _validate_protected_history(
    data: object,
    authority: ElsieReceiptAuthority,
    *,
    anchor_store: Any | None = None,
    require_anchor_match: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "protected",
        "receipt_binding",
        "store_sequence",
        "previous_store_receipt",
        "runs",
        "store_receipt",
    }:
        raise ElsieReceiptError("protected skill history fields are invalid")
    if data.get("schema_version") != PROTECTED_RUN_STORE_SCHEMA_VERSION or data.get("protected") is not True:
        raise ElsieReceiptError("protected skill history marker is invalid")
    authority.require_binding(data.get("receipt_binding"))
    sequence = data.get("store_sequence")
    previous = data.get("previous_store_receipt")
    receipt = data.get("store_receipt")
    runs = data.get("runs")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
        or not isinstance(previous, str)
        or (sequence == 1 and previous)
        or (sequence > 1 and not is_hmac_receipt(previous))
        or not is_hmac_receipt(receipt)
        or not isinstance(runs, list)
        or len(runs) > RUN_HISTORY_LIMIT
        or any(not _validate_protected_run(run, authority) for run in runs)
    ):
        raise ElsieReceiptError("protected skill history schema is invalid")
    unsigned = {key: value for key, value in data.items() if key != "store_receipt"}
    expected = authority.store_receipt(
        ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
        unsigned,
    )
    if not hmac.compare_digest(str(receipt), expected):
        raise ElsieReceiptError("protected skill history authentication failed")
    if require_anchor_match:
        require_elsie_store_anchor(
            authority,
            ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
            subject=_protected_history_subject(),
            sequence=sequence,
            store_receipt=str(receipt),
            anchor_store=anchor_store,
        )
    return runs


def _anchor_matches(head: Any | None, sequence: int, receipt: str) -> bool:
    return bool(
        head is not None
        and head.sequence == sequence
        and hmac.compare_digest(
            str(head.head_digest),
            receipt.removeprefix("hmac-sha256:"),
        )
    )


def _unlink_protected_pending(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ElsieReceiptError("protected skill recovery path is unsafe") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        or (os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077)
    ):
        raise ElsieReceiptError("protected skill recovery path is unsafe")
    try:
        path.unlink()
    except OSError as exc:
        raise ElsieReceiptError("protected skill recovery state could not be removed") from exc


def _recover_protected_history_unlocked(
    authority: ElsieReceiptAuthority,
    *,
    anchor_store: Any | None = None,
) -> bool:
    pending_path = _protected_history_pending_path()
    pending = _read_protected_history(pending_path)
    if pending is _PROTECTED_HISTORY_MISSING:
        return False
    if not isinstance(pending, dict):
        raise ElsieReceiptError("protected skill recovery state is malformed")
    _validate_protected_history(
        pending,
        authority,
        anchor_store=anchor_store,
        require_anchor_match=False,
    )
    pending_raw = _serialize_protected_history(pending)
    pending_sequence = int(pending["store_sequence"])
    pending_previous = str(pending["previous_store_receipt"])
    pending_receipt = str(pending["store_receipt"])

    current = _read_protected_history()
    current_sequence = 0
    current_receipt = ""
    current_raw: bytes | None = None
    if current is not _PROTECTED_HISTORY_MISSING:
        if not isinstance(current, dict):
            raise ElsieReceiptError("protected skill history is malformed")
        _validate_protected_history(
            current,
            authority,
            anchor_store=anchor_store,
            require_anchor_match=False,
        )
        current_sequence = int(current["store_sequence"])
        current_receipt = str(current["store_receipt"])
        current_raw = _serialize_protected_history(current)

    head = load_elsie_store_anchor(
        authority,
        ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
        subject=_protected_history_subject(),
        anchor_store=anchor_store,
    )
    if _anchor_matches(head, pending_sequence, pending_receipt):
        if current_sequence == pending_sequence:
            if current_raw is None or not hmac.compare_digest(current_raw, pending_raw):
                raise ElsieReceiptError("protected skill recovery replay is inconsistent")
            _unlink_protected_pending(pending_path)
        else:
            if pending_sequence != current_sequence + 1 or not hmac.compare_digest(pending_previous, current_receipt):
                raise ElsieReceiptError("protected skill recovery sequence is invalid")
            publish_elsie_staged_file(
                pending_path,
                PROTECTED_RUN_HISTORY_PATH,
                expected_payload=pending_raw,
            )
    else:
        head_matches_current = (head is None and current_sequence == 0) or _anchor_matches(
            head, current_sequence, current_receipt
        )
        if (
            not head_matches_current
            or pending_sequence != current_sequence + 1
            or not hmac.compare_digest(pending_previous, current_receipt)
        ):
            raise ElsieReceiptError("protected skill recovery sequence is invalid")
        # The stage was durable but its external CAS never committed. Discarding
        # this exact authenticated stage is the only safe pre-CAS recovery.
        _unlink_protected_pending(pending_path)
        return True

    published = _read_protected_history()
    _validate_protected_history(
        published,
        authority,
        anchor_store=anchor_store,
    )
    return True


def _legacy_protected_jsonl(raw: bytes, authority: ElsieReceiptAuthority) -> bool:
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError:
        return False
    if not 1 <= len(lines) <= RUN_HISTORY_LIMIT:
        return False
    for line in lines:
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            return False
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"version", "stored_at", "event"}
            or envelope.get("version") != 1
            or isinstance(envelope.get("stored_at"), bool)
            or not isinstance(envelope.get("stored_at"), (int, float))
            or not math.isfinite(float(envelope["stored_at"]))
            or not (
                _validate_protected_run(envelope.get("event"), authority)
                or _validate_legacy_protected_run(envelope.get("event"), authority)
            )
        ):
            return False
    return True


def _current_protected_history(
    authority: ElsieReceiptAuthority,
    *,
    anchor_store: Any | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    current = _read_protected_history()
    if current is _PROTECTED_HISTORY_MISSING:
        head = load_elsie_store_anchor(
            authority,
            ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
            subject=_protected_history_subject(),
            anchor_store=anchor_store,
        )
        if head is not None:
            raise ElsieReceiptError("protected skill history is missing")
        return [], 0, ""
    if not isinstance(current, dict):
        raise ElsieReceiptError("protected skill history is malformed")
    runs = _validate_protected_history(
        current,
        authority,
        anchor_store=anchor_store,
    )
    return runs, int(current["store_sequence"]), str(current["store_receipt"])


def _commit_protected_history(
    runs: list[dict[str, Any]],
    authority: ElsieReceiptAuthority,
    *,
    anchor_store: Any | None = None,
    expected_sequence: int | None = None,
    expected_store_receipt: str | None = None,
    fault_injector: Callable[[str], None] | None = None,
    append: bool = False,
) -> None:
    """Commit one bounded state through exact stage -> CAS -> publish."""

    with _exclusive_state_lock(PROTECTED_RUN_HISTORY_PATH):
        _recover_protected_history_unlocked(
            authority,
            anchor_store=anchor_store,
        )
        current_runs, current_sequence, current_receipt = _current_protected_history(
            authority,
            anchor_store=anchor_store,
        )
        if (expected_sequence is not None and expected_sequence != current_sequence) or (
            expected_store_receipt is not None and not hmac.compare_digest(expected_store_receipt, current_receipt)
        ):
            raise ElsieReceiptError("protected skill history update is stale")
        selected_runs = [*current_runs, *runs] if append else runs
        bounded_runs = list(selected_runs[-RUN_HISTORY_LIMIT:])
        if any(not _validate_protected_run(run, authority) for run in bounded_runs):
            raise ElsieReceiptError("protected skill history event is invalid")
        while True:
            payload = _protected_history_payload(
                bounded_runs,
                authority,
                sequence=current_sequence + 1,
                previous_store_receipt=current_receipt,
            )
            try:
                serialized = _serialize_protected_history(payload)
                break
            except ElsieReceiptError as exc:
                if "bounded size" not in str(exc) or len(bounded_runs) <= 1:
                    raise
                bounded_runs.pop(0)
        pending_path = _protected_history_pending_path()
        _atomic_write_text(pending_path, serialized.decode("utf-8"))
        staged = _read_protected_history(pending_path)
        _validate_protected_history(
            staged,
            authority,
            anchor_store=anchor_store,
            require_anchor_match=False,
        )
        if fault_injector is not None:
            fault_injector("after_stage_before_cas")
        advance_elsie_store_anchor(
            authority,
            ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
            subject=_protected_history_subject(),
            sequence=current_sequence + 1,
            previous_store_receipt=current_receipt,
            store_receipt=str(payload["store_receipt"]),
            anchor_store=anchor_store,
        )
        if fault_injector is not None:
            fault_injector("after_cas_before_publish")
        publish_elsie_staged_file(
            pending_path,
            PROTECTED_RUN_HISTORY_PATH,
            expected_payload=serialized,
        )
        if fault_injector is not None:
            fault_injector("after_publish")
        published = _read_protected_history()
        _validate_protected_history(
            published,
            authority,
            anchor_store=anchor_store,
        )


def prepare_protected_skill_history(
    *,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> dict[str, int]:
    """Purge legacy state and recover/validate the anchored protected store."""

    removed_history = 0
    for path in (RUN_HISTORY_PATH, PRIVATE_RUN_HISTORY_PATH):
        try:
            with _exclusive_state_lock(path):
                removed_history += int(_safe_unlink_regular_or_link(path))
        except (OSError, TimeoutError):
            raise ElsieReceiptError("legacy skill history could not be purged") from None
    pending_path = _protected_history_pending_path()
    target_present = _path_exists_no_follow(PROTECTED_RUN_HISTORY_PATH)
    pending_present = _path_exists_no_follow(pending_path)
    authority = receipt_authority
    if authority is None:
        if target_present or pending_present:
            authority = ElsieReceiptAuthority.from_existing_key_store()
        else:
            authority = ElsieReceiptAuthority.from_optional_existing_key_store()
    removed_legacy_protected = 0
    if authority is not None:
        with _exclusive_state_lock(PROTECTED_RUN_HISTORY_PATH):
            _recover_protected_history_unlocked(
                authority,
                anchor_store=anchor_store,
            )
            try:
                current = _read_protected_history()
            except ElsieReceiptError as exc:
                try:
                    raw = _protected_history_bytes(PROTECTED_RUN_HISTORY_PATH)
                except FileNotFoundError:
                    raise exc
                head = load_elsie_store_anchor(
                    authority,
                    ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
                    subject=_protected_history_subject(),
                    anchor_store=anchor_store,
                )
                if head is not None or not _legacy_protected_jsonl(raw, authority):
                    raise exc
                if not _safe_unlink_regular_or_link(PROTECTED_RUN_HISTORY_PATH):
                    raise ElsieReceiptError("legacy protected skill history could not be purged")
                removed_legacy_protected = 1
                current = _PROTECTED_HISTORY_MISSING
            if current is _PROTECTED_HISTORY_MISSING:
                head = load_elsie_store_anchor(
                    authority,
                    ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
                    subject=_protected_history_subject(),
                    anchor_store=anchor_store,
                )
                if head is not None:
                    raise ElsieReceiptError("protected skill history is missing")
            else:
                _validate_protected_history(
                    current,
                    authority,
                    anchor_store=anchor_store,
                )
    removed_candidates = _purge_untrusted_skill_candidates()
    quarantined_active = _quarantine_unproven_active_skills()
    return {
        "removed_legacy_histories": removed_history,
        "removed_legacy_protected_history": removed_legacy_protected,
        "removed_untrusted_candidates": removed_candidates,
        "quarantined_unproven_active_skills": quarantined_active,
    }


CRYSTALLIZE_SYSTEM = """You review recent runs from a terminal coding agent and crystallize reusable skills.

Create a skill ONLY when ALL of these hold:
- The run used more than 2 tool calls to accomplish something concrete.
- A non-obvious discovery was made: a file path, config key, API quirk, command
  sequence, or environment-specific workaround that is not generic knowledge.
- The same kind of task is likely to recur.

Return ONLY compact JSON: a list of skill objects. Each object has:
- name: short kebab-case slug, no spaces
- description: one specific line, used for retrieval matching
- trigger: the signal that means this skill applies
- steps: array of short imperative strings; append " -> verify: <check>" where useful
- discoveries: array of concrete facts learned (exact paths, configs, gotchas)
- environment: optional string — OS/tool context if the skill is environment-specific

Rules:
- Do NOT create skills for trivial one-step tasks, pure Q&A, or generic programming knowledge.
- Do NOT recreate skills whose names already exist (the existing names are listed for you).
- If nothing qualifies, return [].
- Keep every field terse. A small local model must scan and apply this fast.
- Return at most 5 skills."""


def ensure_dirs() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(SKILLS_DIR, 0o700)


def ensure_quarantine_dir() -> None:
    SKILL_QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(SKILL_QUARANTINE_DIR, 0o700)


def record_run(
    goal: str,
    tool_calls: list[dict[str, Any]],
    outcome: str,
    iterations: int,
    duration_ms: float,
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> bool:
    """Append one opted-in completed-run summary to the private history."""
    if protected:
        try:
            authority = receipt_authority or ElsieReceiptAuthority.from_key_store()
            prepare_protected_skill_history(
                receipt_authority=authority,
                anchor_store=anchor_store,
            )
            goal_preview = (goal or "").strip()[:GOAL_PREVIEW_CHARS]
            outcome_preview = (outcome or "").strip()[:OUTCOME_PREVIEW_CHARS]
            protected_calls: list[dict[str, Any]] = []
            for raw_call in tool_calls[:256]:
                call = raw_call if isinstance(raw_call, dict) else {}
                name = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(call.get("name") or "unknown"))[:128]
                status = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(call.get("status") or "unknown"))[:64]
                args_value = call.get("args", "")
                protected_calls.append(
                    {
                        "name_chars": len(name or "unknown"),
                        "status_chars": len(status or "unknown"),
                        "identity_receipt": authority.receipt(
                            ReceiptNamespace.SKILL_TOOL_IDENTITY,
                            {
                                "name": name or "unknown",
                                "status": status or "unknown",
                            },
                        ),
                        "args_receipt": authority.receipt(
                            ReceiptNamespace.SKILL_TOOL_ARGUMENTS,
                            args_value,
                        ),
                        "explicit_memory_write": bool(call.get("explicit_memory_write", False)),
                    }
                )
            record = {
                "schema_version": PROTECTED_RUN_SCHEMA_VERSION,
                "receipt_binding": authority.binding.as_dict(),
                "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                "goal_chars": len(goal_preview),
                "goal_receipt": authority.receipt(
                    ReceiptNamespace.SKILL_GOAL,
                    goal_preview,
                ),
                "tool_calls": protected_calls,
                "outcome_chars": len(outcome_preview),
                "outcome_receipt": authority.receipt(
                    ReceiptNamespace.SKILL_OUTCOME,
                    outcome_preview,
                ),
                "iterations": int(iterations),
                "duration_ms": round(float(duration_ms), 1),
                "crystallizable": False,
            }
            _commit_protected_history(
                [record],
                authority,
                anchor_store=anchor_store,
                append=True,
            )
            return True
        except (
            ElsieReceiptError,
            OSError,
            PrivateEventStoreError,
            TypeError,
            ValueError,
            TimeoutError,
        ):
            return False
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "goal": (goal or "").strip()[:GOAL_PREVIEW_CHARS],
        "tool_calls": tool_calls,
        "outcome": (outcome or "").strip()[:OUTCOME_PREVIEW_CHARS],
        "iterations": int(iterations),
        "duration_ms": round(float(duration_ms), 1),
    }
    try:
        _run_history_store().append(record)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _trim_run_history() -> None:
    try:
        lines = RUN_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= RUN_HISTORY_LIMIT:
        return
    kept = lines[-RUN_HISTORY_LIMIT:]
    _atomic_write_text(RUN_HISTORY_PATH, "\n".join(kept) + "\n")


def recent_runs(
    n: int = CRYSTALLIZE_LOOKBACK,
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, n)
    if protected:
        pending = _read_protected_history(_protected_history_pending_path())
        if pending is not _PROTECTED_HISTORY_MISSING:
            raise ElsieReceiptError("protected skill history recovery requires explicit preflight")
        current = _read_protected_history()
        authority = receipt_authority
        if authority is None:
            if current is _PROTECTED_HISTORY_MISSING:
                authority = ElsieReceiptAuthority.from_optional_existing_key_store()
            else:
                authority = ElsieReceiptAuthority.from_existing_key_store()
        if authority is None:
            return []
        runs, _sequence, _receipt = _current_protected_history(
            authority,
            anchor_store=anchor_store,
        )
        return runs[-limit:]
    legacy_runs: list[dict[str, Any]] = []
    if RUN_HISTORY_PATH.exists():
        if os.name == "posix":
            try:
                os.chmod(RUN_HISTORY_PATH, 0o600)
            except OSError:
                pass
        try:
            lines = RUN_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for raw in lines[-limit:]:
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                legacy_runs.append(item)
    try:
        private_runs = _run_history_store().read_events(limit=limit)
    except OSError:
        private_runs = []
    return [*legacy_runs, *private_runs][-limit:]


def existing_skill_titles() -> list[str]:
    if not SKILLS_DIR.exists():
        return []
    return sorted(p.stem for p in SKILLS_DIR.glob("*.md"))


def _validated_candidate(candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    name = str(candidate.get("name") or "").strip()
    description = str(candidate.get("description") or "").strip()
    trigger = str(candidate.get("trigger") or "").strip()
    if not name or not description:
        return None, "name_and_description_required"
    if "\n" in name or "\n" in description or len(name) > 80 or len(description) > 240:
        return None, "invalid_name_or_description"
    steps_value = candidate.get("steps") or []
    discoveries_value = candidate.get("discoveries") or []
    if not isinstance(steps_value, list) or not isinstance(discoveries_value, list):
        return None, "steps_and_discoveries_must_be_lists"
    if len(steps_value) > MAX_CANDIDATE_ITEMS or len(discoveries_value) > MAX_CANDIDATE_ITEMS:
        return None, "too_many_items"
    steps = [str(item).strip() for item in steps_value if str(item).strip()]
    discoveries = [str(item).strip() for item in discoveries_value if str(item).strip()]
    environment = str(candidate.get("environment") or "").strip()
    fields = [name, description, trigger, environment, *steps, *discoveries]
    if any(len(item) > MAX_CANDIDATE_ITEM_CHARS for item in [trigger, environment, *steps, *discoveries]):
        return None, "item_too_long"
    if sum(len(item) for item in fields) > MAX_CANDIDATE_TOTAL_CHARS:
        return None, "candidate_too_large"
    if any(_UNSAFE_CANDIDATE_RE.search(item) for item in fields):
        return None, "unsafe_instruction_or_secret"
    return {
        "name": _slugify(name),
        "description": description,
        "trigger": trigger,
        "steps": steps,
        "discoveries": discoveries,
        "environment": environment,
    }, "ok"


def _quarantine_payload_path(name: str) -> Path:
    return SKILL_QUARANTINE_DIR / f"{_slugify(name)}.json"


def quarantine_skill(
    candidate: dict[str, Any],
    *,
    protected: bool = False,
) -> tuple[Path | None, str]:
    """Persist an untrusted skill candidate outside the active harness roots."""

    if protected:
        return None, "protected_history_not_crystallizable"

    validated, reason = _validated_candidate(candidate)
    if validated is None:
        return None, reason
    path = _quarantine_payload_path(validated["name"])
    if path.exists() or (SKILLS_DIR / f"{validated['name']}.md").exists():
        return None, "already_exists"
    ensure_quarantine_dir()
    payload = {
        "schema_version": 1,
        "status": "quarantined",
        "created": datetime.now().isoformat(timespec="seconds"),
        "candidate": validated,
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    if os.name == "posix":
        os.chmod(path, 0o600)
    return path, "ok"


def _load_quarantine_payload(name: str) -> tuple[Path, dict[str, Any]]:
    path = _quarantine_payload_path(name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported quarantined skill payload")
    return path, payload


def quarantined_skill_titles() -> list[str]:
    if not SKILL_QUARANTINE_DIR.exists():
        return []
    titles: list[str] = []
    for path in sorted(SKILL_QUARANTINE_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("status") == "quarantined":
            titles.append(path.stem)
    return titles


def promote_quarantined_skill(name: str, *, protected: bool = False) -> Path:
    if protected:
        raise ValueError("protected run history cannot promote plaintext skill candidates")
    path, payload = _load_quarantine_payload(name)
    if payload.get("status") != "quarantined":
        raise ValueError(f"Skill candidate is not pending: {_slugify(name)}")
    raw_candidate = payload.get("candidate")
    candidate = raw_candidate if isinstance(raw_candidate, dict) else {}
    validated, reason = _validated_candidate(candidate)
    if validated is None:
        raise ValueError(f"Skill candidate failed validation: {reason}")
    promoted = write_skill(validated)
    if promoted is None:
        raise FileExistsError(f"Active skill already exists: {validated['name']}")
    payload["status"] = "promoted"
    payload["promoted"] = datetime.now().isoformat(timespec="seconds")
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return promoted


def reject_quarantined_skill(name: str, *, protected: bool = False) -> Path:
    if protected:
        raise ValueError("protected run history has no trusted plaintext candidates")
    path, payload = _load_quarantine_payload(name)
    if payload.get("status") != "quarantined":
        raise ValueError(f"Skill candidate is not pending: {_slugify(name)}")
    payload["status"] = "rejected"
    payload["rejected"] = datetime.now().isoformat(timespec="seconds")
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return slug or "skill"


def _format_runs_for_prompt(runs: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for index, run in enumerate(runs, 1):
        calls = run.get("tool_calls", []) or []
        call_strs = []
        for call in calls:
            name = call.get("name", "?")
            status = call.get("status", "?")
            args = call.get("args", "")
            call_strs.append(f"{name}({args}) {status}" if args else f"{name} {status}")
        blocks.append(
            f"RUN {index} ({len(calls)} tool calls, {run.get('duration_ms', '?')} ms):\n"
            f"  goal: {run.get('goal', '')}\n"
            f"  tools: {', '.join(call_strs) or '(none)'}\n"
            f"  outcome: {run.get('outcome', '')}"
        )
    return "\n\n".join(blocks)


def _extract_json_array(text: str) -> list[Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _render_skill(candidate: dict[str, Any]) -> str:
    name = str(candidate.get("name", "skill")).strip()
    slug = _slugify(name)
    description = str(candidate.get("description", "")).strip()
    trigger = str(candidate.get("trigger", "")).strip()
    steps = candidate.get("steps", []) or []
    discoveries = candidate.get("discoveries", []) or []
    environment = str(candidate.get("environment", "")).strip()
    title = name.replace("-", " ").title()

    lines = [
        "---",
        f"name: {slug}",
        f"description: {description}",
        "tags: [crystallized, algo-cli]",
        f"created: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {title}",
        "",
        "## Trigger",
        trigger or "(not specified)",
        "",
        "## Steps",
    ]
    if steps:
        for i, step in enumerate(steps, 1):
            lines.append(f"{i}. {str(step).strip()}")
    else:
        lines.append("(not specified)")
    lines += ["", "## Key Discoveries"]
    if discoveries:
        for disc in discoveries:
            lines.append(f"- {str(disc).strip()}")
    else:
        lines.append("- (none recorded)")
    if environment:
        lines += ["", "## Environment", environment]
    lines.append("")
    return "\n".join(lines)


def write_skill(candidate: dict[str, Any]) -> Path | None:
    """Write one skill candidate to SKILLS_DIR. Returns the path, or None if skipped."""
    name = candidate.get("name")
    description = candidate.get("description")
    if not name or not description:
        return None
    slug = _slugify(name)
    path = SKILLS_DIR / f"{slug}.md"
    if path.exists():
        return None  # never overwrite an existing skill
    ensure_dirs()
    _atomic_write_text(path, _render_skill(candidate))
    return path


def crystallize(
    llm_fn: LLMFn,
    lookback: int = CRYSTALLIZE_LOOKBACK,
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> dict[str, Any]:
    """Review recent runs, extract skill candidates, write new SKILL.md files.

    Returns {"created": [slugs], "skipped": [names], "reason": str}.
    """
    if protected:
        prepare_protected_skill_history(
            receipt_authority=receipt_authority,
            anchor_store=anchor_store,
        )
        return {
            "created": [],
            "quarantined": [],
            "skipped": [],
            "reason": "protected run history is content-free and cannot be crystallized",
        }
    runs = recent_runs(lookback)
    substantive = [r for r in runs if len(r.get("tool_calls", []) or []) > 2]
    if not substantive:
        return {"created": [], "skipped": [], "reason": "no substantive runs in recent history"}

    existing = sorted({*existing_skill_titles(), *quarantined_skill_titles()})
    user_prompt = (
        f"EXISTING SKILL NAMES (do not recreate): {', '.join(existing) or '(none yet)'}\n\n"
        f"RECENT RUNS:\n{_format_runs_for_prompt(substantive)}"
    )
    try:
        raw = llm_fn(CRYSTALLIZE_SYSTEM, user_prompt)
    except Exception as exc:
        return {"created": [], "skipped": [], "reason": f"crystallizer call failed: {exc}"}

    candidates = _extract_json_array(raw)
    if not candidates:
        return {"created": [], "skipped": [], "reason": "no skill candidates returned"}

    created: list[str] = []
    quarantined: list[str] = []
    skipped: list[str] = []
    for candidate in candidates[:MAX_SKILLS_PER_PASS]:
        if not isinstance(candidate, dict):
            continue
        path, reason = quarantine_skill(candidate)
        if path is not None:
            quarantined.append(path.stem)
        else:
            skipped.append(f"{candidate.get('name', '?')}:{reason}")
    return {
        "created": created,
        "quarantined": quarantined,
        "skipped": skipped,
        "reason": "awaiting_explicit_promotion" if quarantined else "no_candidate_accepted",
    }


def skills_status(
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
) -> dict[str, Any]:
    titles = existing_skill_titles()
    try:
        run_count = len(
            recent_runs(
                RUN_HISTORY_LIMIT,
                protected=protected,
                receipt_authority=receipt_authority,
            )
        )
        if protected:
            run_history_readiness = {
                "status": "ready",
                "record_count": run_count,
                "authenticated": True,
                "rollback_protected": True,
            }
        else:
            run_history_readiness = _run_history_store().readiness()
    except (OSError, ElsieReceiptError, PrivateEventStoreError):
        run_count = 0
        run_history_readiness = {"status": "error"}
    return {
        "skills_dir": str(SKILLS_DIR),
        "skill_count": len(titles),
        "skills": titles,
        "run_history": str(PROTECTED_RUN_HISTORY_PATH if protected else PRIVATE_RUN_HISTORY_PATH),
        "run_count": run_count,
        "run_history_readiness": run_history_readiness,
        "quarantined": quarantined_skill_titles(),
        "quarantine_dir": str(SKILL_QUARANTINE_DIR),
    }
