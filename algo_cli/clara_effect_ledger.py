"""Durable, content-free state machine for externally visible effects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import threading
from typing import Any
import uuid

from .henry_effect_control import TargetLeaseManager, target_digest
from .private_event_store import PrivateEventStore, RetentionPolicy


EFFECT_LEDGER_SCHEMA_VERSION = 2
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class EffectLedgerError(RuntimeError):
    pass


class EffectLedgerCorrupt(EffectLedgerError):
    pass


class InvalidEffectTransition(EffectLedgerError):
    pass


class StaleFencingToken(EffectLedgerError):
    pass


class InvocationReplayConflict(EffectLedgerError):
    pass


class EffectState(str, Enum):
    PREPARED = "prepared"
    STARTED = "started"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


_ALLOWED_TRANSITIONS: dict[EffectState, frozenset[EffectState]] = {
    EffectState.PREPARED: frozenset({EffectState.STARTED, EffectState.FAILED}),
    EffectState.STARTED: frozenset(
        {EffectState.APPLIED, EffectState.FAILED, EffectState.UNKNOWN}
    ),
    EffectState.APPLIED: frozenset({EffectState.VERIFIED, EffectState.UNKNOWN}),
    EffectState.UNKNOWN: frozenset({EffectState.VERIFIED, EffectState.FAILED}),
    EffectState.VERIFIED: frozenset(),
    EffectState.FAILED: frozenset(),
}


@dataclass(frozen=True)
class EffectRecord:
    effect_id: str
    idempotency_key: str
    invocation_id_hash: str
    action: str
    target_hash: str
    state: EffectState
    fencing_token: int
    sequence: int
    reason_code: str = ""
    verifier: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in {EffectState.VERIFIED, EffectState.FAILED}


@dataclass(frozen=True)
class PreparedEffect:
    record: EffectRecord
    created: bool


def _safe_identifier(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a bounded non-sensitive identifier")
    return normalized


class EffectLedger:
    """Append-only effect state backed by the private event store."""

    def __init__(
        self,
        store: PrivateEventStore,
        *,
        invocation_leases: TargetLeaseManager | None = None,
    ) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._invocation_leases = invocation_leases or TargetLeaseManager(
            store.path.parent / "clara-invocation-leases"
        )

    @classmethod
    def at_path(cls, path: str) -> "EffectLedger":
        return cls(
            PrivateEventStore(
                path,
                policy=RetentionPolicy(
                    max_records=10_000,
                    max_bytes=16 * 1024 * 1024,
                    max_age_seconds=365 * 24 * 60 * 60,
                ),
            )
        )

    @staticmethod
    def _decode(event: dict[str, Any]) -> tuple[EffectRecord, str]:
        if event.get("schema_version") != EFFECT_LEDGER_SCHEMA_VERSION:
            raise EffectLedgerCorrupt("unsupported effect ledger schema")
        try:
            state = EffectState(str(event.get("state") or ""))
        except ValueError as exc:
            raise EffectLedgerCorrupt("unknown effect state") from exc
        previous = str(event.get("previous_state") or "")
        effect_id = str(event.get("effect_id") or "")
        key = str(event.get("idempotency_key") or "")
        invocation_id_hash = str(event.get("invocation_id_hash") or "")
        action = str(event.get("action") or "")
        target_hash = str(event.get("target_hash") or "")
        reason_code = str(event.get("reason_code") or "")
        verifier = str(event.get("verifier") or "")
        token = event.get("fencing_token")
        sequence = event.get("sequence")
        if (
            not _SAFE_ID_RE.fullmatch(effect_id)
            or not _DIGEST_RE.fullmatch(key)
            or not _DIGEST_RE.fullmatch(invocation_id_hash)
            or not _SAFE_ID_RE.fullmatch(action)
            or not _DIGEST_RE.fullmatch(target_hash)
            or (reason_code and not _SAFE_ID_RE.fullmatch(reason_code))
            or (verifier and not _SAFE_ID_RE.fullmatch(verifier))
            or isinstance(token, bool)
            or not isinstance(token, int)
            or token <= 0
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
        ):
            raise EffectLedgerCorrupt("invalid effect ledger event")
        return (
            EffectRecord(
                effect_id=effect_id,
                idempotency_key=key,
                invocation_id_hash=invocation_id_hash,
                action=action,
                target_hash=target_hash,
                state=state,
                fencing_token=token,
                sequence=sequence,
                reason_code=reason_code,
                verifier=verifier,
            ),
            previous,
        )

    def records(self) -> dict[str, EffectRecord]:
        latest: dict[str, EffectRecord] = {}
        keys: dict[str, str] = {}
        invocation_ids: dict[str, str] = {}
        for event in self.store.read_events():
            record, previous = self._decode(event)
            prior = latest.get(record.effect_id)
            if prior is not None:
                if record.sequence != prior.sequence + 1 or previous != prior.state.value:
                    raise EffectLedgerCorrupt("effect transition chain is inconsistent")
                if (
                    record.idempotency_key != prior.idempotency_key
                    or record.invocation_id_hash != prior.invocation_id_hash
                    or record.action != prior.action
                    or record.target_hash != prior.target_hash
                    or record.fencing_token != prior.fencing_token
                ):
                    raise EffectLedgerCorrupt("effect identity changed during transition")
            existing_effect = keys.get(record.idempotency_key)
            if existing_effect is not None and existing_effect != record.effect_id:
                raise EffectLedgerCorrupt("idempotency key maps to multiple effects")
            keys[record.idempotency_key] = record.effect_id
            existing_invocation = invocation_ids.get(record.invocation_id_hash)
            if existing_invocation is not None and existing_invocation != record.effect_id:
                raise EffectLedgerCorrupt("invocation ID maps to multiple effects")
            invocation_ids[record.invocation_id_hash] = record.effect_id
            latest[record.effect_id] = record
        return latest

    def get(self, effect_id: str) -> EffectRecord | None:
        return self.records().get(str(effect_id))

    def find_by_idempotency(self, idempotency_key: str) -> EffectRecord | None:
        key = str(idempotency_key)
        for record in self.records().values():
            if record.idempotency_key == key:
                return record
        return None

    def _append(self, record: EffectRecord, *, previous_state: str) -> None:
        maintenance = self.store.append(
            {
                "schema_version": EFFECT_LEDGER_SCHEMA_VERSION,
                "effect_id": record.effect_id,
                "idempotency_key": record.idempotency_key,
                "invocation_id_hash": record.invocation_id_hash,
                "action": record.action,
                "target_hash": record.target_hash,
                "state": record.state.value,
                "previous_state": previous_state,
                "fencing_token": record.fencing_token,
                "sequence": record.sequence,
                "reason_code": record.reason_code,
                "verifier": record.verifier,
            }
        )
        if not maintenance.stored:
            raise EffectLedgerError("effect ledger event was not durably stored")

    def prepare(
        self,
        *,
        idempotency_key: str,
        invocation_id_hash: str,
        action: str,
        target: str,
        fencing_token: int,
    ) -> PreparedEffect:
        if not _DIGEST_RE.fullmatch(str(idempotency_key)):
            raise ValueError("idempotency_key must be a SHA-256 hex digest")
        if not _DIGEST_RE.fullmatch(str(invocation_id_hash)):
            raise ValueError("invocation_id_hash must be a SHA-256 hex digest")
        action_name = _safe_identifier(action, "action")
        if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token <= 0:
            raise ValueError("fencing_token must be a positive integer")
        with self._invocation_leases.acquire(f"invocation:{invocation_id_hash}"):
            with self._lock:
                records = self.records()
                existing = next(
                    (
                        record
                        for record in records.values()
                        if record.idempotency_key == idempotency_key
                    ),
                    None,
                )
                if existing is not None:
                    return PreparedEffect(existing, created=False)
                if any(
                    record.invocation_id_hash == invocation_id_hash
                    for record in records.values()
                ):
                    raise InvocationReplayConflict(
                        "invocation ID was already bound to a different action"
                    )
                record = EffectRecord(
                    effect_id=f"effect-{uuid.uuid4().hex}",
                    idempotency_key=idempotency_key,
                    invocation_id_hash=invocation_id_hash,
                    action=action_name,
                    target_hash=target_digest(target),
                    state=EffectState.PREPARED,
                    fencing_token=fencing_token,
                    sequence=0,
                )
                self._append(record, previous_state="")
                return PreparedEffect(record, created=True)

    def transition(
        self,
        effect_id: str,
        new_state: EffectState,
        *,
        fencing_token: int,
        reason_code: str = "",
        verifier: str = "",
    ) -> EffectRecord:
        safe_reason = _safe_identifier(reason_code, "reason_code") if reason_code else ""
        safe_verifier = _safe_identifier(verifier, "verifier") if verifier else ""
        with self._lock:
            current = self.get(effect_id)
            if current is None:
                raise EffectLedgerError("effect does not exist")
            if fencing_token != current.fencing_token:
                raise StaleFencingToken("effect transition used a stale fencing token")
            if new_state not in _ALLOWED_TRANSITIONS[current.state]:
                raise InvalidEffectTransition(
                    f"cannot transition effect from {current.state.value} to {new_state.value}"
                )
            updated = EffectRecord(
                effect_id=current.effect_id,
                idempotency_key=current.idempotency_key,
                invocation_id_hash=current.invocation_id_hash,
                action=current.action,
                target_hash=current.target_hash,
                state=new_state,
                fencing_token=current.fencing_token,
                sequence=current.sequence + 1,
                reason_code=safe_reason,
                verifier=safe_verifier,
            )
            self._append(updated, previous_state=current.state.value)
            return updated

    def reconcile(
        self,
        effect_id: str,
        *,
        fencing_token: int,
        observed_applied: bool,
        verifier: str,
    ) -> EffectRecord:
        return self.transition(
            effect_id,
            EffectState.VERIFIED if observed_applied else EffectState.FAILED,
            fencing_token=fencing_token,
            reason_code="reconciled_applied" if observed_applied else "reconciled_not_applied",
            verifier=verifier,
        )

    def recover_prepared(
        self,
        effect_id: str,
        *,
        recovery_fencing_token: int,
    ) -> EffectRecord:
        """Close a pre-dispatch crash using a strictly newer target fence."""

        with self._lock:
            current = self.get(effect_id)
            if current is None:
                raise EffectLedgerError("effect does not exist")
            if current.state is not EffectState.PREPARED:
                raise InvalidEffectTransition("only a prepared effect can be recovered as not dispatched")
            if (
                isinstance(recovery_fencing_token, bool)
                or not isinstance(recovery_fencing_token, int)
                or recovery_fencing_token <= current.fencing_token
            ):
                raise StaleFencingToken("pre-dispatch recovery requires a newer fencing token")
            return self.transition(
                effect_id,
                EffectState.FAILED,
                fencing_token=current.fencing_token,
                reason_code="recovered_before_dispatch",
                verifier="newer_target_fence",
            )

    def readiness(self) -> dict[str, Any]:
        readiness = self.store.readiness()
        try:
            records = self.records()
        except EffectLedgerError as exc:
            return {**readiness, "status": "error", "ledger_error": type(exc).__name__}
        states: dict[str, int] = {}
        for record in records.values():
            states[record.state.value] = states.get(record.state.value, 0) + 1
        return {**readiness, "effects": len(records), "states": states}


def default_effect_ledger() -> EffectLedger:
    from . import config as config_module

    return EffectLedger.at_path(
        str(config_module.CONFIG_DIR / "private" / "clara_effect_ledger.jsonl")
    )


__all__ = [
    "EFFECT_LEDGER_SCHEMA_VERSION",
    "EffectLedger",
    "EffectLedgerCorrupt",
    "EffectLedgerError",
    "EffectRecord",
    "EffectState",
    "InvalidEffectTransition",
    "InvocationReplayConflict",
    "PreparedEffect",
    "StaleFencingToken",
    "default_effect_ledger",
]
