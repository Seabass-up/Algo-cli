"""Fail-closed preparation for Echo-protected auxiliary stores."""

from __future__ import annotations

from typing import Any


_GENERIC_REASON = "echo_auxiliary_unavailable"
_SAFE_REASON_CODES = frozenset(
    {
        "credential_registry_migration_required",
        "credential_registry_native_enumeration_required",
        "credential_registry_unavailable",
    }
)


class EchoAuxiliaryPreflightError(RuntimeError):
    """Echo-protected derived state could not be made safe for retrieval."""

    def __init__(self, reason_code: str = _GENERIC_REASON) -> None:
        candidate = str(reason_code or "")
        self.reason_code = candidate if candidate in _SAFE_REASON_CODES else _GENERIC_REASON
        super().__init__("Echo-protected auxiliary state is unavailable")

    @classmethod
    def from_exception(cls, error: BaseException) -> "EchoAuxiliaryPreflightError":
        """Retain only a bounded infrastructure code from a private cause chain."""

        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            candidate = str(current)
            if candidate in _SAFE_REASON_CODES:
                return cls(candidate)
            current = current.__cause__ or current.__context__
        return cls()


def prepare_echo_auxiliary_state(
    config: Any,
    *,
    receipt_key_store: Any | None = None,
    receipt_anchor_store: Any | None = None,
) -> dict[str, int | bool]:
    """Prepare every Echo-protected auxiliary store before retrieval or work."""

    from . import (
        agent_threads,
        code_rag,
        harness,
        identity,
        julia_memory_candidates as memory_candidates,
        skills,
        ada_task_ledger as task_ledger,
        tools,
    )
    from .ada_memory_echo_veil import echo_veil_authority_selected
    from .config import CONFIG_DIR
    from .grace_memory_receipts import ElsieReceiptAuthority

    if not echo_veil_authority_selected(config):
        harness.configure_protected_memory_authority(False)
        return {"protected": False, "invalidated_skill_records": 0}
    try:
        authority = (
            ElsieReceiptAuthority.from_key_store(store=receipt_key_store) if receipt_key_store is not None else None
        )
        invalidated_memory = harness.configure_protected_memory_authority(True)
        purged_x_search = tools.purge_x_search_cache()
        purged_code_indexes = code_rag.purge_persisted_indexes()
        cleared_identity_cache = identity.clear_plaintext_identity_cache()
        purged_lessons_index = identity.purge_legacy_lessons_index()
        prepared = skills.prepare_protected_skill_history(
            receipt_authority=authority,
            anchor_store=receipt_anchor_store,
        )
        goal_prepared = task_ledger.prepare_protected_goal_store(
            receipt_authority=authority,
            anchor_store=receipt_anchor_store,
        )
        candidate_prepared = memory_candidates.prepare_protected_candidate_state(
            CONFIG_DIR / "memory_candidate_state.json",
            receipt_authority=authority,
            anchor_store=receipt_anchor_store,
        )
        thread_prepared = agent_threads.prepare_protected_thread_store(
            receipt_authority=authority,
            anchor_store=receipt_anchor_store,
        )
        invalidated = harness.invalidate_user_skill_records()
    except Exception as exc:
        raise EchoAuxiliaryPreflightError.from_exception(exc) from exc
    return {
        "protected": True,
        "invalidated_skill_records": max(0, int(invalidated)),
        "invalidated_mutable_memory_records": max(0, int(invalidated_memory)),
        "purged_x_search_cache_entries": max(0, int(purged_x_search)),
        "purged_plaintext_code_index_entries": max(
            0,
            int(purged_code_indexes),
        ),
        "cleared_plaintext_identity_cache_entries": max(
            0,
            int(cleared_identity_cache),
        ),
        "purged_legacy_lessons_index": bool(purged_lessons_index),
        "recovered_goal_store": bool(goal_prepared),
        "recovered_candidate_store": bool(candidate_prepared),
        "recovered_thread_store": bool(thread_prepared),
        "quarantined_unproven_active_skills": max(
            0,
            int(prepared.get("quarantined_unproven_active_skills", 0)),
        ),
    }


__all__ = ["EchoAuxiliaryPreflightError", "prepare_echo_auxiliary_state"]
