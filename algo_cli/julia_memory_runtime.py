"""Algo CLI's governed, searchable long-term memory runtime and catalog.

The catalog complements the small, always-on ``memory.json`` list.  It is an
original Algo CLI implementation: lifecycle metadata and human-editable text
live in ``system_memory.json`` while optional vectors live in a rebuildable
sidecar.  Lexical retrieval remains available when the local embedding backend
is offline or an index is incomplete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shlex
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config as config_module
from . import memory_candidates
from .config import Config
from .dorothy_perf_telemetry import record_perf_event


logger = logging.getLogger(__name__)


CATALOG_VERSION = 1
INDEX_VERSION = 1
VALID_TIERS = frozenset({"pinned", "curated", "history"})
VALID_STATUSES = frozenset({"active", "archived", "superseded"})
VALID_SENSITIVITIES = frozenset({"normal", "confidential", "restricted"})
DEFAULT_TOP_K = 6
MIN_SEMANTIC_SCORE = 0.55
MAX_CONTENT_CHARS = 4_000
MAX_SCOPE_CHARS = 80
MAX_SLOT_CHARS = 120
_LEXICAL_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "should",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
    }
)
_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "artifact": ("document", "file"),
    "billing": ("invoice",),
    "charge": ("payment",),
    "interpreter": ("shell",),
    "settling": ("payment",),
}

EmbedFn = Callable[[list[str]], list[list[float]]]
BENCHMARK_VERSION = 1


class MemorySystemError(RuntimeError):
    """Base error for a governed memory operation."""


class MemorySafetyError(MemorySystemError):
    """Raised when content fails the deterministic privacy gate."""


class MemoryConflictError(MemorySystemError):
    """Raised when a write would create two active values for one slot."""

    def __init__(self, slot: str, record_ids: Sequence[str]):
        self.slot = slot
        self.record_ids = tuple(record_ids)
        joined = ", ".join(self.record_ids)
        super().__init__(
            f"Memory slot '{slot}' already has an active value ({joined}); "
            "use explicit supersession instead of storing a contradiction."
        )


def catalog_path() -> Path:
    from . import config as config_module

    return config_module.CONFIG_DIR / "system_memory.json"


def index_path() -> Path:
    from . import config as config_module

    return config_module.CONFIG_DIR / "system_memory_index.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized(text: object) -> str:
    return memory_candidates.normalize_memory_text(str(text or ""))


def _tokens(text: object) -> list[str]:
    return [token for token in _normalized(text).split() if token and token not in _LEXICAL_STOPWORDS]


def _query_tokens(text: object) -> list[str]:
    tokens = _tokens(text)
    expanded = list(tokens)
    for token in tokens:
        expanded.extend(_QUERY_ALIASES.get(token, ()))
    return list(dict.fromkeys(expanded))


def _content_hash(text: object) -> str:
    return hashlib.sha256(_normalized(text).encode("utf-8")).hexdigest()


def _record_id(content: str, existing_ids: set[str]) -> str:
    digest = _content_hash(content)
    for width in (16, 20, 24, 32, 64):
        candidate = f"mem_{digest[:width]}"
        if candidate not in existing_ids:
            return candidate
    raise MemorySystemError("Unable to allocate a collision-free memory ID.")


def _clean_label(value: object, *, field: str, max_chars: int) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if len(cleaned) > max_chars:
        raise MemorySystemError(f"{field} exceeds {max_chars} characters.")
    return cleaned


def _validate_content(content: object) -> str:
    cleaned = " ".join(str(content or "").strip().split())
    if not cleaned:
        raise MemorySystemError("Memory content is empty.")
    if len(cleaned) > MAX_CONTENT_CHARS:
        raise MemorySystemError(
            f"Memory content exceeds {MAX_CONTENT_CHARS} characters; store a curated document instead."
        )
    privacy_reason = memory_candidates._privacy_reason(cleaned)
    if privacy_reason:
        raise MemorySafetyError(
            f"Memory rejected by the privacy gate ({privacy_reason}); secrets and sensitive identifiers are not durable memory."
        )
    return cleaned


def _infer_slot(content: str) -> str:
    """Infer a conservative semantic slot for simple ``subject is value`` facts."""

    normalized = _normalized(content)
    separators = (" is ", " are ", " equals ", " should use ", " must use ")
    for separator in separators:
        if separator not in normalized:
            continue
        subject, _value = normalized.split(separator, 1)
        subject_tokens = subject.split()
        if 1 < len(subject_tokens) <= 8:
            digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16]
            return f"inferred:{digest}"
    return ""


def _empty_catalog() -> dict[str, Any]:
    return {"version": CATALOG_VERSION, "updated_at": "", "records": []}


def _empty_index() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "model": "", "updated_at": "", "records": {}}


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    from . import config as config_module

    config_module._atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def _read_json(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemorySystemError(f"Memory state is unreadable: {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise MemorySystemError(f"Memory state has an invalid root object: {path.name}")
    return value


def _record_copy(record: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in record.items()}


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _valid_vector(value: object) -> list[float] | None:
    if not isinstance(value, list) or not value or len(value) > 16_384:
        return None
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(item) for item in vector):
        return None
    return vector


def _source_weight(source: object) -> float:
    return {
        "verified": 1.0,
        "user_explicit": 1.0,
        "legacy": 0.92,
        "auto_capture": 0.82,
        "imported": 0.72,
    }.get(str(source or ""), 0.80)


def _freshness_weight(record: Mapping[str, Any], *, now: datetime) -> float:
    if str(record.get("tier") or "") != "history":
        return 1.0
    updated = _parse_time(record.get("updated_at"))
    if updated is None:
        return 0.85
    age_days = max(0.0, (now - updated).total_seconds() / 86_400.0)
    return max(0.75, 1.0 / (1.0 + age_days / 3_650.0))


class MemoryCatalog:
    """Persistent governed memory records plus a rebuildable vector sidecar."""

    def __init__(self, path: Path | None = None, vector_path: Path | None = None):
        self.path = path or catalog_path()
        self.vector_path = vector_path or index_path()

    def _load(self) -> dict[str, Any]:
        payload = _read_json(self.path, default=_empty_catalog())
        if payload.get("version") != CATALOG_VERSION:
            raise MemorySystemError("Unsupported system memory catalog version.")
        records = payload.get("records")
        if not isinstance(records, list) or not all(isinstance(item, Mapping) for item in records):
            raise MemorySystemError("System memory records are malformed.")
        return {
            "version": CATALOG_VERSION,
            "updated_at": str(payload.get("updated_at") or ""),
            "records": [_record_copy(item) for item in records],
        }

    def _save(self, records: Sequence[Mapping[str, Any]]) -> None:
        _atomic_write_json(
            self.path,
            {
                "version": CATALOG_VERSION,
                "updated_at": _utc_now(),
                "records": [_record_copy(record) for record in records],
            },
        )

    def records(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        records = self._load()["records"]
        if include_inactive:
            return records
        now = datetime.now(timezone.utc)
        return [record for record in records if self._eligible(record, now=now)]

    @staticmethod
    def _eligible(record: Mapping[str, Any], *, now: datetime) -> bool:
        if str(record.get("status") or "") != "active":
            return False
        expires = _parse_time(record.get("expires_at"))
        return expires is None or expires > now

    @staticmethod
    def _new_record(
        content: str,
        *,
        record_id: str,
        tier: str,
        source: str,
        scope: str,
        slot: str,
        confidence: float,
        sensitivity: str,
        pinned_in_legacy: bool,
        supersedes: str = "",
    ) -> dict[str, Any]:
        now = _utc_now()
        return {
            "id": record_id,
            "content": content,
            "tier": tier,
            "status": "active",
            "scope": scope,
            "slot": slot,
            "source": source,
            "confidence": round(min(1.0, max(0.0, float(confidence))), 4),
            "sensitivity": sensitivity,
            "created_at": now,
            "updated_at": now,
            "reviewed_at": "",
            "expires_at": "",
            "supersedes": supersedes,
            "superseded_by": "",
            "pinned_in_legacy": bool(pinned_in_legacy),
        }

    def sync_legacy_facts(
        self,
        facts: Iterable[str],
        *,
        source: str = "legacy",
        source_overrides: Mapping[str, str] | None = None,
        authoritative: bool = False,
    ) -> dict[str, int]:
        """Backfill stable catalog records for the compatibility fact list."""

        from . import config as config_module

        clean_facts = [" ".join(str(fact).strip().split()) for fact in facts if str(fact).strip()]
        desired = {_normalized(fact): fact for fact in clean_facts}
        overrides = {_normalized(key): str(value) for key, value in (source_overrides or {}).items()}
        added = 0
        updated = 0
        removed = 0
        with config_module._exclusive_state_lock(self.path):
            payload = self._load()
            records = payload["records"]
            active_by_content = {
                _normalized(record.get("content")): record
                for record in records
                if str(record.get("status") or "") == "active"
            }
            existing_ids = {str(record.get("id") or "") for record in records}
            for key, fact in desired.items():
                record = active_by_content.get(key)
                if record is None:
                    privacy_reason = memory_candidates._privacy_reason(fact)
                    record = self._new_record(
                        fact,
                        record_id=_record_id(fact, existing_ids),
                        tier="pinned",
                        source=overrides.get(key, source),
                        scope="global",
                        slot=_infer_slot(fact),
                        confidence=1.0 if overrides.get(key, source) != "auto_capture" else 0.85,
                        sensitivity="restricted" if privacy_reason else "normal",
                        pinned_in_legacy=True,
                    )
                    records.append(record)
                    existing_ids.add(str(record["id"]))
                    active_by_content[key] = record
                    added += 1
                    continue
                changed = False
                if record.get("tier") != "pinned":
                    record["tier"] = "pinned"
                    changed = True
                if not record.get("pinned_in_legacy"):
                    record["pinned_in_legacy"] = True
                    changed = True
                override = overrides.get(key)
                if override and record.get("source") == "legacy":
                    record["source"] = override
                    changed = True
                if changed:
                    record["updated_at"] = _utc_now()
                    updated += 1
            if authoritative:
                retained: list[dict[str, Any]] = []
                for record in records:
                    key = _normalized(record.get("content"))
                    if (
                        str(record.get("status") or "") == "active"
                        and bool(record.get("pinned_in_legacy"))
                        and key not in desired
                    ):
                        removed += 1
                        continue
                    retained.append(record)
                records = retained
            if added or updated or removed:
                self._save(records)
        return {"added": added, "updated": updated, "removed": removed}

    def add(
        self,
        content: str,
        *,
        tier: str = "history",
        source: str = "user_explicit",
        scope: str = "global",
        slot: str = "",
        confidence: float = 1.0,
        sensitivity: str = "normal",
        pinned_in_legacy: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        from . import config as config_module

        clean_content = _validate_content(content)
        clean_tier = str(tier or "").strip().lower()
        if clean_tier not in VALID_TIERS:
            raise MemorySystemError(f"Unknown memory tier: {tier}")
        clean_sensitivity = str(sensitivity or "normal").strip().lower()
        if clean_sensitivity not in VALID_SENSITIVITIES:
            raise MemorySystemError(f"Unknown sensitivity: {sensitivity}")
        clean_scope = _clean_label(scope or "global", field="scope", max_chars=MAX_SCOPE_CHARS) or "global"
        clean_slot = _clean_label(slot, field="slot", max_chars=MAX_SLOT_CHARS) or _infer_slot(clean_content)

        with config_module._exclusive_state_lock(self.path):
            records = self._load()["records"]
            normalized = _normalized(clean_content)
            for record in records:
                if str(record.get("status") or "") == "active" and _normalized(record.get("content")) == normalized:
                    return _record_copy(record), False
            conflicts = [
                str(record.get("id") or "")
                for record in records
                if clean_slot
                and str(record.get("status") or "") == "active"
                and str(record.get("scope") or "global") == clean_scope
                and str(record.get("slot") or "") == clean_slot
            ]
            if conflicts:
                raise MemoryConflictError(clean_slot, conflicts)
            existing_ids = {str(record.get("id") or "") for record in records}
            record = self._new_record(
                clean_content,
                record_id=_record_id(clean_content, existing_ids),
                tier=clean_tier,
                source=str(source or "user_explicit"),
                scope=clean_scope,
                slot=clean_slot,
                confidence=confidence,
                sensitivity=clean_sensitivity,
                pinned_in_legacy=pinned_in_legacy,
            )
            records.append(record)
            self._save(records)
            return _record_copy(record), True

    def get(self, record_id: str) -> dict[str, Any]:
        needle = str(record_id or "").strip()
        matches = [record for record in self.records() if str(record.get("id") or "") == needle]
        if not matches:
            raise MemorySystemError(f"Unknown memory ID: {needle}")
        return matches[0]

    def _set_state(self, record_id: str, *, tier: str | None = None, status: str | None = None) -> dict[str, Any]:
        from . import config as config_module

        with config_module._exclusive_state_lock(self.path):
            records = self._load()["records"]
            for record in records:
                if str(record.get("id") or "") != record_id:
                    continue
                if tier is not None:
                    if tier not in VALID_TIERS:
                        raise MemorySystemError(f"Unknown memory tier: {tier}")
                    record["tier"] = tier
                    record["pinned_in_legacy"] = tier == "pinned"
                if status is not None:
                    if status not in VALID_STATUSES:
                        raise MemorySystemError(f"Unknown memory status: {status}")
                    record["status"] = status
                    if status != "active":
                        record["pinned_in_legacy"] = False
                record["updated_at"] = _utc_now()
                self._save(records)
                return _record_copy(record)
        raise MemorySystemError(f"Unknown memory ID: {record_id}")

    def set_tier(self, record_id: str, tier: str) -> dict[str, Any]:
        return self._set_state(record_id, tier=tier)

    def archive(self, record_id: str) -> dict[str, Any]:
        return self._set_state(record_id, status="archived")

    def supersede(self, record_id: str, content: str, *, source: str = "user_explicit") -> dict[str, Any]:
        from . import config as config_module

        clean_content = _validate_content(content)
        with config_module._exclusive_state_lock(self.path):
            records = self._load()["records"]
            old = next(
                (record for record in records if str(record.get("id") or "") == record_id),
                None,
            )
            if old is None:
                raise MemorySystemError(f"Unknown memory ID: {record_id}")
            if str(old.get("status") or "") != "active":
                raise MemorySystemError(f"Memory {record_id} is not active.")
            normalized = _normalized(clean_content)
            duplicate = next(
                (
                    record
                    for record in records
                    if str(record.get("status") or "") == "active"
                    and str(record.get("id") or "") != record_id
                    and _normalized(record.get("content")) == normalized
                ),
                None,
            )
            if duplicate is not None:
                raise MemorySystemError(f"Replacement duplicates active memory {duplicate.get('id')}.")
            existing_ids = {str(record.get("id") or "") for record in records}
            replacement = self._new_record(
                clean_content,
                record_id=_record_id(clean_content, existing_ids),
                tier=str(old.get("tier") or "history"),
                source=source,
                scope=str(old.get("scope") or "global"),
                slot=str(old.get("slot") or "") or _infer_slot(clean_content),
                confidence=float(old.get("confidence") or 1.0),
                sensitivity=str(old.get("sensitivity") or "normal"),
                pinned_in_legacy=bool(old.get("pinned_in_legacy")),
                supersedes=record_id,
            )
            old["status"] = "superseded"
            old["superseded_by"] = replacement["id"]
            old["pinned_in_legacy"] = False
            old["updated_at"] = _utc_now()
            records.append(replacement)
            self._save(records)
            return _record_copy(replacement)

    def hard_delete_content(self, content: str) -> int:
        """Delete a fact and its vector record for explicit ``/forget`` semantics."""

        from . import config as config_module

        normalized = _normalized(content)
        removed_ids: set[str] = set()
        with config_module._exclusive_state_lock(self.path):
            records = self._load()["records"]
            retained = []
            for record in records:
                if _normalized(record.get("content")) == normalized:
                    removed_ids.add(str(record.get("id") or ""))
                else:
                    retained.append(record)
            if removed_ids:
                self._save(retained)
        if removed_ids:
            self._drop_index_ids(removed_ids)
        return len(removed_ids)

    def _load_index(self) -> dict[str, Any]:
        payload = _read_json(self.vector_path, default=_empty_index())
        if payload.get("version") != INDEX_VERSION:
            raise MemorySystemError("Unsupported system memory index version.")
        records = payload.get("records")
        if not isinstance(records, dict):
            raise MemorySystemError("System memory vector index is malformed.")
        return {
            "version": INDEX_VERSION,
            "model": str(payload.get("model") or ""),
            "updated_at": str(payload.get("updated_at") or ""),
            "records": {str(key): value for key, value in records.items() if isinstance(value, Mapping)},
        }

    def _save_index(self, model: str, records: Mapping[str, Mapping[str, Any]]) -> None:
        _atomic_write_json(
            self.vector_path,
            {
                "version": INDEX_VERSION,
                "model": model,
                "updated_at": _utc_now(),
                "records": {str(key): dict(value) for key, value in records.items()},
            },
        )

    def _drop_index_ids(self, record_ids: set[str]) -> None:
        from . import config as config_module

        if not self.vector_path.exists():
            return
        with config_module._exclusive_state_lock(self.vector_path):
            try:
                payload = self._load_index()
            except MemorySystemError:
                return
            records = payload["records"]
            changed = False
            for record_id in record_ids:
                changed = records.pop(record_id, None) is not None or changed
            if changed:
                self._save_index(payload["model"], records)

    @staticmethod
    def _lexical_scores(query: str, records: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        query_terms = _query_tokens(query)
        if not query_terms or not records:
            return {}
        documents = [_tokens(record.get("content")) for record in records]
        average_length = sum(len(document) for document in documents) / max(1, len(documents))
        document_frequency: Counter[str] = Counter()
        for document in documents:
            document_frequency.update(set(document))
        scores: dict[str, float] = {}
        k1 = 1.5
        b = 0.75
        for record, document in zip(records, documents):
            frequencies = Counter(document)
            score = 0.0
            for term in query_terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                count = document_frequency.get(term, 0)
                inverse_frequency = math.log(1.0 + (len(documents) - count + 0.5) / (count + 0.5))
                denominator = frequency + k1 * (1.0 - b + b * len(document) / max(1.0, average_length))
                score += inverse_frequency * frequency * (k1 + 1.0) / denominator
            normalized_query = _normalized(query)
            normalized_content = _normalized(record.get("content"))
            if normalized_query and normalized_query in normalized_content:
                score += 1.5
            if score > 0.0:
                scores[str(record.get("id") or "")] = score
        return scores

    def _semantic_scores(
        self,
        query: str,
        records: Sequence[Mapping[str, Any]],
        *,
        embed_fn: EmbedFn | None,
        embedding_model: str,
    ) -> tuple[dict[str, float], str]:
        if embed_fn is None or not records:
            return {}, "unavailable"
        from . import config as config_module

        try:
            with config_module._exclusive_state_lock(self.vector_path):
                try:
                    payload = self._load_index()
                except MemorySystemError:
                    payload = _empty_index()
                cached = payload["records"] if payload.get("model") == embedding_model else {}
                missing = [
                    record
                    for record in records
                    if (
                        str(record.get("id") or "") not in cached
                        or str(cached[str(record.get("id") or "")].get("content_hash") or "")
                        != _content_hash(record.get("content"))
                        or _valid_vector(cached[str(record.get("id") or "")].get("embedding")) is None
                    )
                ]
                texts = [str(record.get("content") or "") for record in missing]
                vectors = embed_fn([query, *texts])
                if len(vectors) != len(texts) + 1:
                    return {}, "invalid_embedding_response"
                query_vector = _valid_vector(vectors[0])
                if query_vector is None:
                    return {}, "invalid_query_embedding"
                changed = False
                for record, raw_vector in zip(missing, vectors[1:]):
                    vector = _valid_vector(raw_vector)
                    if vector is None or len(vector) != len(query_vector):
                        continue
                    cached[str(record.get("id") or "")] = {
                        "content_hash": _content_hash(record.get("content")),
                        "embedding": vector,
                    }
                    changed = True
                if changed or payload.get("model") != embedding_model:
                    self._save_index(embedding_model, cached)
                scores: dict[str, float] = {}
                for record in records:
                    entry = cached.get(str(record.get("id") or ""), {})
                    vector = _valid_vector(entry.get("embedding"))
                    if vector is None or len(vector) != len(query_vector):
                        continue
                    score = max(0.0, _cosine(query_vector, vector))
                    if score >= MIN_SEMANTIC_SCORE:
                        scores[str(record.get("id") or "")] = score
                return scores, "ready"
        except Exception:
            return {}, "embedding_failed"

    def search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        embed_fn: EmbedFn | None = None,
        embedding_model: str = "local-default",
        tiers: set[str] | frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        clean_query = " ".join(str(query or "").strip().split())
        if not clean_query:
            return []
        now = datetime.now(timezone.utc)
        records = [
            record
            for record in self.records(include_inactive=False)
            if str(record.get("sensitivity") or "normal") != "restricted"
            and (tiers is None or str(record.get("tier") or "") in tiers)
        ]
        lexical = self._lexical_scores(clean_query, records)
        semantic, semantic_status = self._semantic_scores(
            clean_query,
            records,
            embed_fn=embed_fn,
            embedding_model=embedding_model,
        )
        lexical_order = sorted(lexical, key=lambda record_id: lexical[record_id], reverse=True)
        semantic_order = sorted(semantic, key=lambda record_id: semantic[record_id], reverse=True)
        lexical_rank = {record_id: rank for rank, record_id in enumerate(lexical_order, 1)}
        semantic_rank = {record_id: rank for rank, record_id in enumerate(semantic_order, 1)}
        max_lexical = max(lexical.values(), default=0.0)
        candidates = set(lexical_rank) | set(semantic_rank)
        by_id = {str(record.get("id") or ""): record for record in records}
        ranked: list[dict[str, Any]] = []
        for record_id in candidates:
            record = by_id.get(record_id)
            if record is None:
                continue
            reciprocal = 0.0
            available_weight = 0.0
            if record_id in lexical_rank:
                reciprocal += 0.45 / (60.0 + lexical_rank[record_id])
                available_weight += 0.45
            if record_id in semantic_rank:
                reciprocal += 0.55 / (60.0 + semantic_rank[record_id])
                available_weight += 0.55
            normalized_rrf = reciprocal / max(1e-9, available_weight / 61.0)
            lexical_strength = float(lexical.get(record_id, 0.0)) / max_lexical if max_lexical > 0.0 else 0.0
            evidence_strength = max(
                lexical_strength,
                float(semantic.get(record_id, 0.0)),
            )
            confidence = min(1.0, max(0.0, float(record.get("confidence") or 0.0)))
            authority = _source_weight(record.get("source")) * (0.75 + 0.25 * confidence)
            freshness = _freshness_weight(record, now=now)
            tier_boost = 1.04 if record.get("tier") == "pinned" else 1.0
            score = min(
                1.0,
                normalized_rrf * evidence_strength * authority * freshness * tier_boost,
            )
            result = _record_copy(record)
            result.update(
                {
                    "score": round(score, 6),
                    "lexical_score": round(float(lexical.get(record_id, 0.0)), 6),
                    "semantic_score": round(float(semantic.get(record_id, 0.0)), 6),
                    "semantic_status": semantic_status,
                }
            )
            ranked.append(result)
        ranked.sort(
            key=lambda item: (
                float(item.get("score") or 0.0),
                float(item.get("semantic_score") or 0.0),
                float(item.get("lexical_score") or 0.0),
                str(item.get("updated_at") or ""),
            ),
            reverse=True,
        )
        return ranked[: max(1, min(50, int(top_k)))]

    def reindex(self, embed_fn: EmbedFn | None, embedding_model: str) -> dict[str, Any]:
        if embed_fn is None:
            return {"ready": False, "reason": "embedding backend unavailable", "indexed": 0}
        records = [
            record
            for record in self.records(include_inactive=False)
            if str(record.get("sensitivity") or "normal") != "restricted"
        ]
        try:
            vectors = embed_fn([str(record.get("content") or "") for record in records])
        except Exception as exc:
            return {"ready": False, "reason": type(exc).__name__, "indexed": 0}
        if len(vectors) != len(records):
            return {"ready": False, "reason": "invalid embedding response", "indexed": 0}
        indexed: dict[str, dict[str, Any]] = {}
        expected_dimensions = 0
        for record, raw_vector in zip(records, vectors):
            vector = _valid_vector(raw_vector)
            if vector is None:
                continue
            if not expected_dimensions:
                expected_dimensions = len(vector)
            if len(vector) != expected_dimensions:
                continue
            indexed[str(record.get("id") or "")] = {
                "content_hash": _content_hash(record.get("content")),
                "embedding": vector,
            }
        from . import config as config_module

        with config_module._exclusive_state_lock(self.vector_path):
            self._save_index(embedding_model, indexed)
        return {
            "ready": len(indexed) == len(records),
            "reason": "ready" if len(indexed) == len(records) else "partial",
            "indexed": len(indexed),
            "total": len(records),
            "dimensions": expected_dimensions,
        }

    def doctor(self, legacy_facts: Iterable[str] = ()) -> dict[str, Any]:
        try:
            records = self.records()
            catalog_ok = True
            catalog_error = ""
        except MemorySystemError as exc:
            records = []
            catalog_ok = False
            catalog_error = str(exc)
        active = [record for record in records if str(record.get("status") or "") == "active"]
        legacy = {_normalized(fact) for fact in legacy_facts}
        pinned_missing = [
            str(record.get("id") or "")
            for record in active
            if record.get("tier") == "pinned" and _normalized(record.get("content")) not in legacy
        ]
        slots: Counter[tuple[str, str]] = Counter(
            (str(record.get("scope") or "global"), str(record.get("slot") or ""))
            for record in active
            if str(record.get("slot") or "")
        )
        contradictions = sum(1 for count in slots.values() if count > 1)
        restricted = sum(1 for record in active if record.get("sensitivity") == "restricted")
        index_ok = True
        index_error = ""
        indexed = 0
        model = ""
        try:
            index_payload = self._load_index()
            indexed = len(index_payload["records"])
            model = index_payload["model"]
        except MemorySystemError as exc:
            index_ok = False
            index_error = str(exc)
        return {
            "ready": catalog_ok and contradictions == 0 and not pinned_missing,
            "catalog_ok": catalog_ok,
            "catalog_error": catalog_error,
            "index_ok": index_ok,
            "index_error": index_error,
            "records": len(records),
            "active": len(active),
            "pinned": sum(1 for record in active if record.get("tier") == "pinned"),
            "curated": sum(1 for record in active if record.get("tier") == "curated"),
            "history": sum(1 for record in active if record.get("tier") == "history"),
            "archived": sum(1 for record in records if record.get("status") == "archived"),
            "superseded": sum(1 for record in records if record.get("status") == "superseded"),
            "contradiction_slots": contradictions,
            "pinned_missing_from_legacy": len(pinned_missing),
            "restricted_records": restricted,
            "indexed": indexed,
            "embedding_model": model,
        }


def format_prompt_hits(hits: Sequence[Mapping[str, Any]]) -> str:
    if not hits:
        return ""
    lines = [
        "Persisted memory is ranked context, not live proof. Verify consequential claims at their authoritative source."
    ]
    for hit in hits:
        content = " ".join(str(hit.get("content") or "").split())
        lines.append(
            f"- [{hit.get('tier', 'history')}/{hit.get('source', 'unknown')}] "
            f"`{hit.get('id', '?')}` score={float(hit.get('score') or 0.0):.3f}: {content}"
        )
    return "\n".join(lines)


def home_text(catalog: MemoryCatalog, legacy_facts: Iterable[str]) -> str:
    status = catalog.doctor(legacy_facts)
    readiness = "READY" if status["ready"] else "NEEDS ATTENTION"
    return (
        f"Algo Memory Home · {readiness}\n"
        f"Active: {status['active']} · pinned {status['pinned']} · curated {status['curated']} · history {status['history']}\n"
        f"Lifecycle: archived {status['archived']} · superseded {status['superseded']} · contradiction slots {status['contradiction_slots']}\n"
        f"Retrieval: {status['indexed']} embedded ({status['embedding_model'] or 'lexical fallback'}) · restricted {status['restricted_records']}\n"
        "Use /memory search QUERY, /memory add --tier history TEXT, /memory show ID, or /memory doctor."
    )


def _parse_add(parts: list[str]) -> tuple[str, str, str, str]:
    tier = "history"
    scope = "global"
    slot = ""
    content_parts: list[str] = []
    index = 0
    while index < len(parts):
        token = parts[index]
        if token in {"--tier", "--scope", "--slot"}:
            if index + 1 >= len(parts):
                raise MemorySystemError(f"Missing value for {token}.")
            value = parts[index + 1]
            if token == "--tier":
                tier = value
            elif token == "--scope":
                scope = value
            else:
                slot = value
            index += 2
            continue
        content_parts.extend(parts[index:])
        break
    if tier == "pinned":
        raise MemorySystemError(
            "Use /remember for pinned always-on facts; /memory add accepts curated or history records."
        )
    return tier, scope, slot, " ".join(content_parts)


def command_text(
    arg: str,
    cfg: Any,
    *,
    embed_fn: EmbedFn | None = None,
    embedding_model: str = "local-default",
) -> str:
    """Execute the ``/memory`` surface and return terminal-friendly text."""

    catalog = MemoryCatalog()
    catalog.sync_legacy_facts(getattr(cfg, "memories", ()), authoritative=False)
    try:
        parts = shlex.split(arg or "")
    except ValueError as exc:
        raise MemorySystemError(f"Unable to parse /memory arguments: {exc}") from exc
    subcommand = parts[0].lower() if parts else "home"
    remainder = parts[1:]
    if subcommand in {"home", "status", "show-home"}:
        return home_text(catalog, getattr(cfg, "memories", ()))
    if subcommand in {"help", "?"}:
        return (
            "/memory home | search QUERY | show ID | doctor | benchmark | reindex\n"
            "/memory add [--tier curated|history] [--scope NAME] [--slot KEY] TEXT\n"
            "/memory supersede ID TEXT | promote ID | demote ID | archive ID"
        )
    if subcommand == "doctor":
        status = catalog.doctor(getattr(cfg, "memories", ()))
        return json.dumps(status, indent=2, sort_keys=True)
    if subcommand == "benchmark":
        return json.dumps(
            run_benchmark(embed_fn=embed_fn, embedding_model=embedding_model),
            indent=2,
            sort_keys=True,
        )
    if subcommand == "search":
        query = " ".join(remainder)
        if not query:
            raise MemorySystemError("Usage: /memory search QUERY")
        hits = catalog.search(
            query,
            embed_fn=embed_fn,
            embedding_model=embedding_model,
        )
        if not hits:
            return "No relevant active memory found."
        return "\n".join(
            f"{index}. {hit['id']} · {hit['tier']} · score {hit['score']:.3f} · {hit['content']}"
            for index, hit in enumerate(hits, 1)
        )
    if subcommand == "show":
        if len(remainder) != 1:
            raise MemorySystemError("Usage: /memory show ID")
        return json.dumps(catalog.get(remainder[0]), indent=2, ensure_ascii=False, sort_keys=True)
    if subcommand == "add":
        tier, scope, slot, content = _parse_add(remainder)
        record, added = catalog.add(content, tier=tier, scope=scope, slot=slot)
        state = "Added" if added else "Already present"
        return f"{state}: {record['id']} · {record['tier']}"
    if subcommand == "supersede":
        if len(remainder) < 2:
            raise MemorySystemError("Usage: /memory supersede ID REPLACEMENT")
        old = catalog.get(remainder[0])
        replacement = catalog.supersede(remainder[0], " ".join(remainder[1:]))
        if old.get("tier") == "pinned":
            _remove_legacy_fact(cfg, str(old.get("content") or ""))
            remember_fact(cfg, str(replacement.get("content") or ""), source="user_explicit")
        return f"Superseded {remainder[0]} with {replacement['id']}."
    if subcommand == "promote":
        if len(remainder) != 1:
            raise MemorySystemError("Usage: /memory promote ID")
        record = catalog.get(remainder[0])
        remember_fact(cfg, str(record.get("content") or ""), source="user_explicit")
        promoted = catalog.set_tier(remainder[0], "pinned")
        return f"Promoted {promoted['id']} to pinned memory."
    if subcommand == "demote":
        if len(remainder) != 1:
            raise MemorySystemError("Usage: /memory demote ID")
        record = catalog.set_tier(remainder[0], "history")
        _remove_legacy_fact(cfg, str(record.get("content") or ""))
        return f"Demoted {record['id']} to searchable history."
    if subcommand == "archive":
        if len(remainder) != 1:
            raise MemorySystemError("Usage: /memory archive ID")
        existing = catalog.get(remainder[0])
        record = catalog.archive(remainder[0])
        _remove_legacy_fact(cfg, str(existing.get("content") or ""))
        return f"Archived {record['id']}."
    if subcommand == "reindex":
        result = catalog.reindex(embed_fn, embedding_model)
        return json.dumps(result, indent=2, sort_keys=True)
    raise MemorySystemError("Unknown /memory command. Use /memory help.")


def _latest_legacy_facts(cfg: Config) -> list[str]:
    loaded = config_module._load_json_file(config_module.MEMORY_FILE, cfg.memories)
    if not isinstance(loaded, list):
        return [str(item) for item in cfg.memories]
    return [str(item) for item in loaded]


def remember_fact(
    cfg: Config,
    fact: str,
    *,
    source: str = "user_explicit",
) -> bool:
    """Persist one pinned fact through the legacy and governed stores."""

    clean_fact = _validate_content(fact)
    catalog = MemoryCatalog()
    current = _latest_legacy_facts(cfg)
    catalog.sync_legacy_facts(current, authoritative=False)
    record, catalog_added = catalog.add(
        clean_fact,
        tier="pinned",
        source=source,
        pinned_in_legacy=True,
        confidence=0.85 if source == "auto_capture" else 1.0,
    )
    previous_tier = str(record.get("tier") or "history")
    if not catalog_added and previous_tier != "pinned":
        catalog.set_tier(str(record["id"]), "pinned")
    try:
        added = cfg.remember_fact(clean_fact)
    except Exception:
        if catalog_added:
            catalog.hard_delete_content(clean_fact)
        elif previous_tier != "pinned":
            catalog.set_tier(str(record["id"]), previous_tier)
        raise
    catalog.sync_legacy_facts(
        cfg.memories,
        source_overrides={clean_fact: source},
        authoritative=False,
    )
    return added


def _remove_legacy_fact(cfg: Config, fact: str) -> bool:
    target = " ".join(str(fact or "").strip().split()).casefold()
    if not target:
        return False
    with config_module._exclusive_state_lock(config_module.MEMORY_FILE):
        loaded = config_module._load_json_file(config_module.MEMORY_FILE, [])
        current = [str(item) for item in loaded] if isinstance(loaded, list) else []
        retained = [item for item in current if " ".join(item.strip().split()).casefold() != target]
        removed = len(retained) != len(current)
        if removed:
            config_module._atomic_write_text(
                config_module.MEMORY_FILE,
                json.dumps(retained, indent=2),
            )
    cfg.memories = retained
    return removed


def forget_memory_index(cfg: Config, index: int) -> str:
    """Apply explicit hard-delete semantics to both memory stores."""

    removed = cfg.forget_memory_index(index)
    MemoryCatalog().hard_delete_content(removed)
    return removed


_EXPLICIT_MEMORY_TOOLS = frozenset({"remember", "append_lesson"})
_SUCCESSFUL_TOOL_STATUSES = frozenset({"worked"})


def _successful_explicit_memory_write(tool_calls: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        str(call.get("name") or "") in _EXPLICIT_MEMORY_TOOLS
        and str(call.get("status") or "") in _SUCCESSFUL_TOOL_STATUSES
        for call in tool_calls
    )


def _record_result(result: Mapping[str, Any], *, source: str) -> None:
    counts_value = result.get("counts")
    state_value = result.get("state")
    reasons_value = result.get("reason_counts")
    counts = counts_value if isinstance(counts_value, Mapping) else {}
    state = state_value if isinstance(state_value, Mapping) else {}
    reasons = reasons_value if isinstance(reasons_value, Mapping) else {}
    record_perf_event(
        "memory_candidate",
        source="agent" if source == "agent" else "chat",
        status=str(result.get("status") or "unknown"),
        reason=str(result.get("reason") or ""),
        extracted=int(counts.get("extracted") or 0),
        evaluated=int(counts.get("evaluated") or 0),
        eligible=int(counts.get("eligible") or 0),
        stored=int(counts.get("stored") or 0),
        rejected=int(counts.get("rejected") or 0),
        daily_writes=int(state.get("daily_writes") or 0),
        auto_fingerprints=int(state.get("auto_fingerprints") or 0),
        reason_counts={str(key): int(value) for key, value in reasons.items()},
    )


def capture_completed_user_turn(
    cfg: Config,
    original_user_text: str,
    *,
    completed: bool,
    tool_calls: Sequence[Mapping[str, Any]] = (),
    source: str = "chat",
) -> dict[str, Any]:
    """Capture at most one memory after a verified completion boundary.

    Only ``original_user_text`` reaches the deterministic candidate processor.
    Assistant, tool, retrieval, and specialist output are never inspected.
    """

    safe_source = "agent" if source == "agent" else "chat"
    if not completed:
        result = {
            "status": "skipped",
            "reason": "incomplete_turn",
            "counts": {},
            "reason_counts": {},
            "state": {},
        }
        _record_result(result, source=safe_source)
        return result
    if _successful_explicit_memory_write(tool_calls):
        result = {
            "status": "skipped",
            "reason": "explicit_memory_write",
            "counts": {},
            "reason_counts": {},
            "state": {},
        }
        _record_result(result, source=safe_source)
        return result

    try:
        return memory_candidates.process_memory_candidates(
            original_user_text,
            tuple(str(item) for item in cfg.memories),
            config_module.MEMORY_CANDIDATE_STATE_FILE,
            bool(cfg.memory_auto_capture_enabled),
            lambda fact: remember_fact(cfg, fact, source="auto_capture"),
            telemetry=lambda result: _record_result(result, source=safe_source),
            daily_limit=int(cfg.memory_auto_daily_limit),
            entry_limit=int(cfg.memory_auto_entry_limit),
            char_limit=int(cfg.memory_auto_char_limit),
        )
    except Exception as exc:  # Completion must never fail because memory capture did.
        logger.debug("Automatic memory capture failed: %s", exc)
        result = {
            "status": "error",
            "reason": type(exc).__name__,
            "counts": {},
            "reason_counts": {"runtime_error": 1},
            "state": {},
        }
        _record_result(result, source=safe_source)
        return result


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1),
    )
    return ordered[index]


def run_benchmark(
    *,
    embed_fn: EmbedFn | None,
    embedding_model: str = "local-default",
) -> dict[str, Any]:
    """Run frozen memory qualification without reading real operator memories."""

    with tempfile.TemporaryDirectory(prefix="algo-memory-benchmark-") as temp_dir:
        root = Path(temp_dir)
        catalog = MemoryCatalog(
            path=root / "system_memory.json",
            vector_path=root / "system_memory_index.json",
        )
        old_shell, _ = catalog.add(
            "Our standard shell is zsh.",
            tier="history",
            slot="environment.shell",
            source="verified",
        )
        current_shell = catalog.supersede(
            old_shell["id"],
            "Our standard shell is fish.",
            source="verified",
        )
        invoice, _ = catalog.add(
            "Payment evidence does not prove an official invoice PDF was issued.",
            tier="curated",
            slot="operations.invoice_evidence",
            source="verified",
        )
        catalog.add(
            "Use a dark terminal theme.",
            tier="history",
            source="user_explicit",
        )
        release, _ = catalog.add(
            "The release process requires a signed package before distribution.",
            tier="curated",
            source="verified",
        )
        catalog.add(
            "An imported forum note suggests unsigned release packages are acceptable.",
            tier="history",
            source="imported",
            confidence=0.35,
        )

        cases = (
            ("exact_shell", "standard shell fish", current_shell["id"], "exact"),
            ("exact_invoice", "invoice PDF payment evidence", invoice["id"], "exact"),
            (
                "paraphrase_shell",
                "Which command interpreter should I use?",
                current_shell["id"],
                "paraphrase",
            ),
            (
                "paraphrase_invoice",
                "Does settling a charge guarantee the formal billing artifact exists?",
                invoice["id"],
                "paraphrase",
            ),
            (
                "multilingual_shell",
                "¿Qué intérprete de comandos debo usar?",
                current_shell["id"],
                "multilingual",
            ),
            (
                "multilingual_invoice",
                "¿Un pago confirma que se emitió el PDF oficial de la factura?",
                invoice["id"],
                "multilingual",
            ),
            (
                "authority_release",
                "Which package policy governs a release?",
                release["id"],
                "authority",
            ),
        )
        observations: list[dict[str, Any]] = []
        latencies_ms: list[float] = []
        for name, query, expected_id, category in cases:
            started = time.perf_counter()
            hits = catalog.search(
                query,
                top_k=3,
                embed_fn=embed_fn,
                embedding_model=embedding_model,
            )
            latencies_ms.append((time.perf_counter() - started) * 1_000.0)
            ranked_ids = [str(hit.get("id") or "") for hit in hits]
            rank = ranked_ids.index(expected_id) + 1 if expected_id in ranked_ids else 0
            expected_hit = next(
                (hit for hit in hits if str(hit.get("id") or "") == expected_id),
                None,
            )
            observations.append(
                {
                    "name": name,
                    "category": category,
                    "expected_id": expected_id,
                    "rank": rank,
                    "recalled_at_3": rank > 0,
                    "semantic_recalled": bool(expected_hit and float(expected_hit.get("semantic_score") or 0.0) > 0.0),
                }
            )

        stale_hits = catalog.search(
            "standard shell",
            top_k=5,
            embed_fn=embed_fn,
            embedding_model=embedding_model,
        )
        stale_hit_rate = sum(1 for hit in stale_hits if hit.get("id") == old_shell["id"]) / max(1, len(stale_hits))
        unrelated_hits = catalog.search(
            "What pizza topping do I prefer?",
            top_k=5,
            embed_fn=embed_fn,
            embedding_model=embedding_model,
        )
        lexical_fallback = catalog.search(
            "invoice PDF payment evidence",
            top_k=3,
            embed_fn=None,
            embedding_model=embedding_model,
        )
        lexical_fallback_ok = any(hit.get("id") == invoice["id"] for hit in lexical_fallback)

        exact = [item for item in observations if item["category"] == "exact"]
        paraphrase = [item for item in observations if item["category"] == "paraphrase"]
        multilingual = [item for item in observations if item["category"] == "multilingual"]
        authority = [item for item in observations if item["category"] == "authority"]
        exact_recall = sum(bool(item["recalled_at_3"]) for item in exact) / len(exact)
        paraphrase_recall = sum(bool(item["recalled_at_3"]) for item in paraphrase) / len(paraphrase)
        semantic_paraphrase_recall = sum(bool(item["semantic_recalled"]) for item in paraphrase) / len(paraphrase)
        multilingual_recall = sum(bool(item["recalled_at_3"]) for item in multilingual) / len(multilingual)
        semantic_multilingual_recall = sum(bool(item["semantic_recalled"]) for item in multilingual) / len(multilingual)
        authority_precision = sum(int(item["rank"] == 1) for item in authority) / len(authority)
        mrr = sum((1.0 / int(item["rank"])) if item["rank"] else 0.0 for item in observations) / len(observations)
        unrelated_rejection = len(unrelated_hits) == 0
        gates = {
            "exact_recall_at_3": exact_recall == 1.0,
            "paraphrase_recall_at_3": paraphrase_recall == 1.0,
            "semantic_paraphrase_recall": semantic_paraphrase_recall == 1.0,
            "multilingual_recall_at_3": multilingual_recall == 1.0,
            "semantic_multilingual_recall": semantic_multilingual_recall == 1.0,
            "authority_precision_at_1": authority_precision == 1.0,
            "stale_hit_rate": stale_hit_rate == 0.0,
            "unrelated_rejection": unrelated_rejection,
            "lexical_fallback": lexical_fallback_ok,
        }
        return {
            "version": BENCHMARK_VERSION,
            "passed": all(gates.values()),
            "embedding_model": (embedding_model if embed_fn is not None else "unavailable"),
            "metrics": {
                "exact_recall_at_3": round(exact_recall, 6),
                "paraphrase_recall_at_3": round(paraphrase_recall, 6),
                "semantic_paraphrase_recall": round(semantic_paraphrase_recall, 6),
                "multilingual_recall_at_3": round(multilingual_recall, 6),
                "semantic_multilingual_recall": round(semantic_multilingual_recall, 6),
                "authority_precision_at_1": round(authority_precision, 6),
                "mrr": round(mrr, 6),
                "stale_hit_rate": round(stale_hit_rate, 6),
                "unrelated_rejection": unrelated_rejection,
                "lexical_fallback": lexical_fallback_ok,
                "mean_latency_ms": round(sum(latencies_ms) / len(latencies_ms), 3),
                "p95_latency_ms": round(_percentile(latencies_ms, 0.95), 3),
            },
            "gates": gates,
            "cases": observations,
        }


__all__ = [
    "BENCHMARK_VERSION",
    "MemoryCatalog",
    "MemoryConflictError",
    "MemorySafetyError",
    "MemorySystemError",
    "catalog_path",
    "capture_completed_user_turn",
    "command_text",
    "forget_memory_index",
    "format_prompt_hits",
    "home_text",
    "index_path",
    "remember_fact",
    "run_benchmark",
]
