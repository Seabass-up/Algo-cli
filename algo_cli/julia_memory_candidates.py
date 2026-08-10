"""Deterministic Julia privacy gate for durable-memory candidate processing.

Only the original user-authored text is accepted as input. The module does no
model calls, embeddings, retrieval, or inspection of assistant/tool output.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import _atomic_write_text, _exclusive_state_lock, _state_descriptor_payload
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

STATE_VERSION = 1
LEGACY_PROTECTED_STATE_VERSION = 2
PROTECTED_STATE_VERSION = 3
PURGED_LEGACY_STATE_VERSION = 0
MAX_SOURCE_CHARS = 12_000
MAX_CANDIDATES_PER_TURN = 3
MAX_STORED_PER_TURN = 1
MAX_DAILY_WRITES = 5
MAX_AUTO_FINGERPRINTS = 64
MAX_MEMORY_CHARS = 12_000
MIN_WORDS = 3
MAX_WORDS = 40
MAX_CANDIDATE_CHARS = 240
NEAR_DUPLICATE_JACCARD = 0.90
NEAR_DUPLICATE_LENGTH_RATIO = 0.80
MAX_STATE_BYTES = 256 * 1024
_STATE_MISSING = object()
_PURGED_LEGACY_STATE = {
    "version": PURGED_LEGACY_STATE_VERSION,
    "protected": True,
    "state": "legacy_fingerprints_purged",
}

PersistenceFn = Callable[[str], bool]
TelemetryFn = Callable[[dict[str, Any]], None]

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_FORWARDED_RE = re.compile(r"^\s*-{2,}\s*(?:original|forwarded) message\s*-{2,}\s*$", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"'”’])\s+|[\r\n]+")
_REMEMBER_RE = re.compile(
    r"^(?:(?:also|and)\s+)?(?:please\s+)?remember(?:\s+that)?\s*[:,-]?\s+(.+)$",
    re.I,
)
_GLOBAL_PREFIXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("from_now_on", re.compile(r"^from now on\s*[,;:-]?\s+(.+)$", re.I)),
    ("going_forward", re.compile(r"^going forward\s*[,;:-]?\s+(.+)$", re.I)),
    ("by_default", re.compile(r"^by default\s*[,;:-]?\s+(.+)$", re.I)),
)
_STANDING_RE = re.compile(
    r"^(?:i|we|you)\s+(?:should\s+)?(?:always|never)\b.+$|^(?:always|never)\b.+$",
    re.I,
)
_WORD_RE = re.compile(r"[\w./~+:-]+", re.UNICODE)
_INLINE_CODE_RE = re.compile(r"`|\{\{|\}\}|=>|\(\)\s*[;{]|\b[A-Z][A-Z0-9_]{2,}\s*=|<[/!]?[A-Za-z][^>]*>")
_TRANSIENT_RE = re.compile(
    r"\b(?:now|today|tomorrow|yesterday|tonight|this (?:week|month|year)|"
    r"next (?:week|month)|right now|for now|currently|"
    r"at the moment|in this (?:task|turn|session|request)|this (?:task|turn|session|request)|"
    r"the current (?:task|turn|session|request|branch|commit)|temporary|temporarily|"
    r"pending|in progress|next step|just failed|just finished)\b|"
    r"\buntil\s+(?:today|tomorrow|tonight|next\b)|\b\d{4}-\d{2}-\d{2}\b",
    re.I,
)
_TASK_RE = re.compile(
    r"^to\s+\w+\b|^(?:run|fix|update|check|review|create|delete|commit|push|merge|"
    r"build|test|open|read|write|install|send|call|buy|schedule|deploy|publish)\b|"
    r"\b(?:todo|to-do|(?:i|we|you)\s+(?:still\s+)?need to|need to finish|"
    r"must finish|finish this|complete this|remind me to)\b",
    re.I,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(?:password|passwd|passphrase|api[ _-]?key|client[ _-]?secret|"
    r"access[ _-]?token|refresh[ _-]?token|id[ _-]?token|private[ _-]?key)\b"
    r"\s*(?:=|:|\bis\b)\s*[\"']?\S+",
    re.I,
)
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"gh[pousr]_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"AIza[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|ya29\.[A-Za-z0-9_-]{12,})\b"
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PEM_RE = re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.I)
_CREDENTIALED_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.I)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w-])", re.I)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)")
_SSN_RE = re.compile(
    r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)|"
    r"\b(?:ssn|social security(?: number)?)\D{0,12}\d{9}\b",
    re.I,
)
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_ENTROPY_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_DURABILITY_BOILERPLATE = frozenset({"always", "default", "going", "forward", "please", "prefer", "remember", "that"})
_NEGATIONS = frozenset({"never", "no", "not", "without"})


@dataclass(frozen=True)
class MemoryCandidate:
    text: str
    marker: str


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str
    fingerprint: str


def _bounded_source(text: str) -> str:
    raw = str(text or "")
    if len(raw) <= MAX_SOURCE_CHARS:
        return raw
    # Slicing and joining the head/tail could cross a removed quote/fence
    # boundary and turn pasted content into an apparent user-authored marker.
    # Oversized turns therefore fail closed instead of being reassembled.
    return ""


def _strip_untrusted_blocks(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    fence = ""
    for raw_line in _bounded_source(text).splitlines():
        fence_match = _FENCE_RE.match(raw_line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence = marker
            elif marker == fence:
                in_fence = False
                fence = ""
            continue
        if in_fence:
            continue
        if raw_line.lstrip().startswith(">"):
            continue
        if _FORWARDED_RE.match(raw_line):
            break
        lines.append(raw_line)
    return "\n".join(lines)


def _clean_candidate_text(text: str) -> str:
    return " ".join(str(text or "").strip().strip("\"'").split())


def _is_wrapped_quote(text: str) -> bool:
    stripped = str(text or "").strip()
    return len(stripped) >= 2 and (stripped[0], stripped[-1]) in {
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
    }


def _extract_candidates_with_overflow(text: str) -> tuple[list[MemoryCandidate], int]:
    extracted: list[MemoryCandidate] = []
    seen: set[tuple[str, str]] = set()
    total = 0
    for segment in _SENTENCE_SPLIT_RE.split(_strip_untrusted_blocks(text)):
        if _is_wrapped_quote(segment):
            continue
        segment = _clean_candidate_text(segment)
        if not segment:
            continue
        marker = ""
        body = ""
        remember_match = _REMEMBER_RE.match(segment)
        if remember_match:
            marker = "remember"
            body = remember_match.group(1)
        else:
            for candidate_marker, pattern in _GLOBAL_PREFIXES:
                match = pattern.match(segment)
                if match:
                    marker = candidate_marker
                    body = match.group(1)
                    break
            if not marker and _STANDING_RE.match(segment):
                marker = "standing_rule"
                body = segment
        if not marker:
            continue
        body = _clean_candidate_text(body)
        if not body:
            continue
        key = (marker, normalize_memory_text(body))
        if key in seen:
            continue
        seen.add(key)
        total += 1
        if len(extracted) < MAX_CANDIDATES_PER_TURN:
            extracted.append(MemoryCandidate(text=body, marker=marker))
    return extracted, max(0, total - len(extracted))


def extract_candidates(text: str) -> list[MemoryCandidate]:
    """Extract at most three candidates from explicit durable-marker sentences."""

    candidates, _overflow = _extract_candidates_with_overflow(text)
    return candidates


def normalize_memory_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    normalized = re.sub(r"\b(?:from now on|going forward|by default|please remember(?: that)?)\b", " ", normalized)
    normalized = re.sub(r"(?<=\w)[.!?,;:]+(?=\s|$)", " ", normalized)
    normalized = re.sub(r"[^\w./~+:-]+", " ", normalized)
    return " ".join(normalized.split())


def memory_fingerprint(
    text: str,
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
) -> str:
    normalized = normalize_memory_text(text)
    if protected:
        authority = receipt_authority or ElsieReceiptAuthority.from_key_store()
        return authority.receipt(ReceiptNamespace.MEMORY_CANDIDATE, normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _dedupe_tokens(text: str) -> set[str]:
    return {token for token in normalize_memory_text(text).split() if token not in _DURABILITY_BOILERPLATE}


def _near_duplicate(left: str, right: str) -> bool:
    left_tokens = _dedupe_tokens(left)
    right_tokens = _dedupe_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if (left_tokens & _NEGATIONS) != (right_tokens & _NEGATIONS):
        return False
    length_ratio = min(len(left_tokens), len(right_tokens)) / max(len(left_tokens), len(right_tokens))
    if length_ratio < NEAR_DUPLICATE_LENGTH_RATIO:
        return False
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) >= NEAR_DUPLICATE_JACCARD


def _luhn_valid(number: str) -> bool:
    digits = [int(char) for char in number if char.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        value = digit
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        checksum += value
    return checksum % 10 == 0


def _entropy(token: str) -> float:
    counts = Counter(token)
    length = len(token)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _has_high_entropy_token(text: str) -> bool:
    for token in _ENTROPY_TOKEN_RE.findall(text):
        categories = sum(
            (
                any(char.islower() for char in token),
                any(char.isupper() for char in token),
                any(char.isdigit() for char in token),
                any(not char.isalnum() for char in token),
            )
        )
        if categories >= 3 and _entropy(token) >= 3.5:
            return True
    return False


def _privacy_reason(text: str) -> str | None:
    if (
        _SECRET_ASSIGNMENT_RE.search(text)
        or _SECRET_TOKEN_RE.search(text)
        or _BEARER_RE.search(text)
        or _JWT_RE.search(text)
        or _PEM_RE.search(text)
        or _CREDENTIALED_URL_RE.search(text)
        or _has_high_entropy_token(text)
    ):
        return "secret"
    if _EMAIL_RE.search(text):
        return "email"
    if _PHONE_RE.search(text):
        return "phone"
    if _SSN_RE.search(text):
        return "ssn"
    if any(_luhn_valid(match.group(0)) for match in _CARD_CANDIDATE_RE.finditer(text)):
        return "payment_card"
    return None


def evaluate_candidate(
    candidate: MemoryCandidate,
    existing_memories: Sequence[str] = (),
    accepted_fingerprints: Sequence[str] = (),
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
) -> EligibilityDecision:
    """Apply deterministic durability, privacy, length, and duplicate gates."""

    text = _clean_candidate_text(candidate.text)
    fingerprint = memory_fingerprint(
        text,
        protected=protected,
        receipt_authority=receipt_authority,
    )
    privacy_reason = _privacy_reason(text)
    if privacy_reason:
        return EligibilityDecision(False, privacy_reason, fingerprint)
    if len(text) > MAX_CANDIDATE_CHARS:
        return EligibilityDecision(False, "too_long", fingerprint)
    word_count = len(_WORD_RE.findall(text))
    if word_count < MIN_WORDS:
        return EligibilityDecision(False, "too_short", fingerprint)
    if word_count > MAX_WORDS:
        return EligibilityDecision(False, "too_many_words", fingerprint)
    if _INLINE_CODE_RE.search(text):
        return EligibilityDecision(False, "code", fingerprint)
    if candidate.marker == "remember" and _TASK_RE.search(text):
        return EligibilityDecision(False, "task_or_imperative", fingerprint)
    if _TRANSIENT_RE.search(text):
        return EligibilityDecision(False, "transient", fingerprint)
    if fingerprint in set(accepted_fingerprints):
        return EligibilityDecision(False, "duplicate_fingerprint", fingerprint)
    normalized = normalize_memory_text(text)
    for existing in existing_memories:
        if normalized == normalize_memory_text(existing):
            return EligibilityDecision(False, "duplicate_exact", fingerprint)
        if _near_duplicate(text, str(existing)):
            return EligibilityDecision(False, "duplicate_near", fingerprint)
    return EligibilityDecision(True, "eligible", fingerprint)


def _empty_state(
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
) -> dict[str, Any]:
    if not protected:
        return {"version": STATE_VERSION, "accepted": [], "stored_total": 0}
    if receipt_authority is None:
        raise ElsieReceiptError("protected memory candidate authority is missing")
    authority = receipt_authority
    return {
        "version": PROTECTED_STATE_VERSION,
        "protected": True,
        "receipt_binding": authority.binding.as_dict(),
        "accepted": [],
        "stored_total": 0,
        "_store_sequence": 0,
        "_store_receipt": "",
    }


def _state_subject(path: Path) -> str:
    return os.path.abspath(os.fspath(path))


def _protected_state_payload(
    state: Mapping[str, Any],
    authority: ElsieReceiptAuthority,
    *,
    sequence: int,
    previous_store_receipt: str,
) -> dict[str, Any]:
    unsigned = {
        "version": PROTECTED_STATE_VERSION,
        "protected": True,
        "receipt_binding": authority.binding.as_dict(),
        "store_sequence": sequence,
        "previous_store_receipt": previous_store_receipt,
        "accepted": list(state.get("accepted") or []),
        "stored_total": max(0, int(state.get("stored_total") or 0)),
    }
    return {
        **unsigned,
        "store_receipt": authority.store_receipt(
            ReceiptNamespace.MEMORY_CANDIDATE_STORE,
            unsigned,
        ),
    }


def _commit_protected_state(
    path: Path,
    state: dict[str, Any],
    authority: ElsieReceiptAuthority,
    *,
    anchor_store: Any | None = None,
) -> None:
    if _pending_candidate_exists(path):
        raise ElsieReceiptError("protected memory candidate recovery is pending")
    current_sequence = int(state.get("_store_sequence") or 0)
    previous_receipt = str(state.get("_store_receipt") or "")
    sequence = current_sequence + 1
    payload = _protected_state_payload(
        state,
        authority,
        sequence=sequence,
        previous_store_receipt=previous_receipt,
    )
    pending_path = elsie_staging_path(path)
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    _atomic_write_text(pending_path, serialized)
    staged = _read_state_json(pending_path)
    _validate_protected_state_payload(
        staged,
        path,
        entry_limit=MAX_AUTO_FINGERPRINTS,
        authority=authority,
        anchor_store=anchor_store,
        require_anchor_match=False,
    )
    advance_elsie_store_anchor(
        authority,
        ReceiptNamespace.MEMORY_CANDIDATE_STORE,
        subject=_state_subject(path),
        sequence=sequence,
        previous_store_receipt=previous_receipt,
        store_receipt=str(payload["store_receipt"]),
        anchor_store=anchor_store,
    )
    publish_elsie_staged_file(
        pending_path,
        path,
        expected_payload=serialized.encode("utf-8"),
    )
    published = _read_state_json(path)
    _validate_protected_state_payload(
        published,
        path,
        entry_limit=MAX_AUTO_FINGERPRINTS,
        authority=authority,
        anchor_store=anchor_store,
    )
    state["_store_sequence"] = sequence
    state["_store_receipt"] = str(payload["store_receipt"])


def _effective_limit(value: int | None, default: int) -> int:
    if value is None:
        return default
    try:
        # Config may lower a safety limit, but cannot expand bounded state.
        return min(default, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _read_state_json(path: Path) -> Any:
    try:
        encoded = _state_descriptor_payload(path, max_bytes=MAX_STATE_BYTES)
    except FileNotFoundError:
        return _STATE_MISSING
    except OSError as exc:
        raise ValueError("memory candidate state path is unsafe") from exc
    try:
        return json.loads(encoded.decode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ValueError("memory candidate state encoding is invalid") from exc


def _pending_candidate_exists(path: Path) -> bool:
    pending = elsie_staging_path(path)
    try:
        info = pending.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ElsieReceiptError("protected memory candidate recovery path is unsafe") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ElsieReceiptError("protected memory candidate recovery path is unsafe")
    return True


def _validate_protected_state_payload(
    payload: object,
    path: Path,
    *,
    entry_limit: int,
    authority: ElsieReceiptAuthority,
    anchor_store: Any | None = None,
    require_anchor_match: bool = True,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ElsieReceiptError("protected memory candidate state is malformed")
    if payload.get("version") != PROTECTED_STATE_VERSION or payload.get("protected") is not True:
        raise ValueError("unsupported protected memory candidate state")
    authority.require_binding(payload.get("receipt_binding"))
    if set(payload) != {
        "version",
        "protected",
        "receipt_binding",
        "store_sequence",
        "previous_store_receipt",
        "accepted",
        "stored_total",
        "store_receipt",
    }:
        raise ElsieReceiptError("protected memory candidate state fields are invalid")
    sequence = payload.get("store_sequence")
    previous = payload.get("previous_store_receipt")
    store_receipt = payload.get("store_receipt")
    stored_total = payload.get("stored_total")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
        or not isinstance(previous, str)
        or (sequence == 1 and previous)
        or (sequence > 1 and not is_hmac_receipt(previous))
        or not is_hmac_receipt(store_receipt)
        or isinstance(stored_total, bool)
        or not isinstance(stored_total, int)
        or stored_total < 0
    ):
        raise ElsieReceiptError("protected memory candidate state sequence is invalid")
    unsigned = {key: value for key, value in payload.items() if key != "store_receipt"}
    expected = authority.store_receipt(
        ReceiptNamespace.MEMORY_CANDIDATE_STORE,
        unsigned,
    )
    if not hmac.compare_digest(str(store_receipt), expected):
        raise ElsieReceiptError("protected memory candidate state authentication failed")
    if require_anchor_match:
        require_elsie_store_anchor(
            authority,
            ReceiptNamespace.MEMORY_CANDIDATE_STORE,
            subject=_state_subject(path),
            sequence=sequence,
            store_receipt=str(store_receipt),
            anchor_store=anchor_store,
        )
    accepted = payload.get("accepted")
    if not isinstance(accepted, list):
        raise ElsieReceiptError("protected memory candidate accepted list is malformed")
    cleaned: list[dict[str, str]] = []
    bounded_entries = accepted[-entry_limit:] if entry_limit else []
    for entry in bounded_entries:
        if not isinstance(entry, Mapping) or set(entry) != {
            "fingerprint",
            "day",
            "outcome",
        }:
            raise ElsieReceiptError("protected memory candidate entry is malformed")
        fingerprint = entry.get("fingerprint")
        day = entry.get("day")
        outcome = entry.get("outcome")
        if (
            not is_hmac_receipt(fingerprint)
            or not isinstance(day, str)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None
            or outcome not in {"pending", "stored"}
        ):
            raise ElsieReceiptError("protected memory candidate entry is malformed")
        cleaned.append(
            {
                "fingerprint": str(fingerprint),
                "day": day,
                "outcome": str(outcome),
            }
        )
    return {
        "version": PROTECTED_STATE_VERSION,
        "protected": True,
        "receipt_binding": authority.binding.as_dict(),
        "accepted": cleaned,
        "stored_total": stored_total,
        "_store_sequence": sequence,
        "_store_receipt": str(store_receipt),
    }


def _recover_protected_candidate_state_unlocked(
    path: Path,
    authority: ElsieReceiptAuthority,
    *,
    entry_limit: int,
    anchor_store: Any | None = None,
) -> bool:
    """Reconcile one exact authenticated candidate-state stage under lock."""

    pending_path = elsie_staging_path(path)
    pending = _read_state_json(pending_path)
    if pending is _STATE_MISSING:
        return False
    _validate_protected_state_payload(
        pending,
        path,
        entry_limit=entry_limit,
        authority=authority,
        anchor_store=anchor_store,
        require_anchor_match=False,
    )
    if not isinstance(pending, dict):  # pragma: no cover - validated above
        raise ElsieReceiptError("protected memory candidate recovery state is malformed")
    sequence = int(pending["store_sequence"])
    previous = str(pending["previous_store_receipt"])
    receipt = str(pending["store_receipt"])
    head = load_elsie_store_anchor(
        authority,
        ReceiptNamespace.MEMORY_CANDIDATE_STORE,
        subject=_state_subject(path),
        anchor_store=anchor_store,
    )
    receipt_hex = receipt.removeprefix("hmac-sha256:")
    if head is not None and (head.sequence == sequence and hmac.compare_digest(head.head_digest, receipt_hex)):
        pass
    else:
        if head is None:
            if sequence != 1 or previous:
                raise ElsieReceiptError("protected memory candidate recovery sequence is invalid")
        else:
            anchored_receipt = "hmac-sha256:" + head.head_digest
            if sequence != head.sequence + 1 or not hmac.compare_digest(previous, anchored_receipt):
                raise ElsieReceiptError("protected memory candidate recovery sequence is invalid")
        advance_elsie_store_anchor(
            authority,
            ReceiptNamespace.MEMORY_CANDIDATE_STORE,
            subject=_state_subject(path),
            sequence=sequence,
            previous_store_receipt=previous,
            store_receipt=receipt,
            anchor_store=anchor_store,
        )
    serialized = json.dumps(pending, indent=2, sort_keys=True)
    publish_elsie_staged_file(
        pending_path,
        path,
        expected_payload=serialized.encode("utf-8"),
    )
    published = _read_state_json(path)
    _validate_protected_state_payload(
        published,
        path,
        entry_limit=entry_limit,
        authority=authority,
        anchor_store=anchor_store,
    )
    return True


def _load_state(
    path: Path,
    *,
    entry_limit: int,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> dict[str, Any]:
    if protected and _pending_candidate_exists(path):
        raise ElsieReceiptError("protected memory candidate recovery is pending")
    payload = _read_state_json(path)
    if payload is _STATE_MISSING:
        if protected and receipt_authority is not None:
            existing_head = load_elsie_store_anchor(
                receipt_authority,
                ReceiptNamespace.MEMORY_CANDIDATE_STORE,
                subject=_state_subject(path),
                anchor_store=anchor_store,
            )
            if existing_head is not None:
                raise ElsieReceiptError("protected memory candidate state is missing")
        return _empty_state(
            protected=protected,
            receipt_authority=receipt_authority,
        )
    if not isinstance(payload, dict):
        raise ValueError("unsupported memory candidate state")
    if payload == _PURGED_LEGACY_STATE:
        if protected and receipt_authority is not None:
            existing_head = load_elsie_store_anchor(
                receipt_authority,
                ReceiptNamespace.MEMORY_CANDIDATE_STORE,
                subject=_state_subject(path),
                anchor_store=anchor_store,
            )
            if existing_head is not None:
                raise ElsieReceiptError("protected memory candidate tombstone conflicts with anchor")
        return _empty_state(
            protected=protected,
            receipt_authority=receipt_authority,
        )
    if protected:
        authority = receipt_authority
        if authority is None:
            raise ElsieReceiptError("protected memory candidate authority is missing")
        if payload.get("version") == STATE_VERSION:
            # Raw SHA-256 fingerprints are offline-dictionary recoverable.
            # They cannot be migrated into the protected domain, so replace
            # them with an empty authenticated state at the next atomic write.
            return _empty_state(protected=True, receipt_authority=authority)
        return _validate_protected_state_payload(
            payload,
            path,
            entry_limit=entry_limit,
            authority=authority,
            anchor_store=anchor_store,
        )
    if payload.get("version") != STATE_VERSION:
        raise ValueError("unsupported memory candidate state")
    accepted = payload.get("accepted")
    if not isinstance(accepted, list):
        raise ValueError("memory candidate accepted list is malformed")
    cleaned: list[dict[str, str]] = []
    bounded_entries = accepted[-entry_limit:] if entry_limit else []
    for entry in bounded_entries:
        if not isinstance(entry, Mapping):
            continue
        fingerprint = str(entry.get("fingerprint") or "")
        day = str(entry.get("day") or "")
        fingerprint_valid = re.fullmatch(r"[0-9a-f]{64}", fingerprint) is not None
        if fingerprint_valid and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            cleaned.append({"fingerprint": fingerprint, "day": day})
    state = {
        "version": STATE_VERSION,
        "accepted": cleaned,
        "stored_total": max(0, int(payload.get("stored_total") or 0)),
    }
    return state


def _emit_telemetry(callback: TelemetryFn | None, result: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(dict(result))
    except Exception:
        return


def _prepare_protected_candidate_state(
    state_path: Path | str,
    *,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
    entry_limit: int | None = None,
) -> tuple[bool, ElsieReceiptAuthority | None, bool, bool]:
    """Recover/validate protected state and purge legacy raw fingerprints.

    The first result reports any recovery or migration; the last reports a raw
    legacy-fingerprint purge specifically. Missing state is a non-mutating
    no-op. A wrong key, corrupt schema, or unsafe path fails closed.
    """

    path = Path(state_path)
    pending_exists = _pending_candidate_exists(path)
    try:
        preliminary = _read_state_json(path)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ElsieReceiptError("memory candidate state is malformed or unsafe") from exc
    if preliminary is _STATE_MISSING and not pending_exists:
        authority = receipt_authority
        if authority is None:
            authority = ElsieReceiptAuthority.from_optional_existing_key_store()
            if authority is None:
                return False, None, False, False
        existing_head = load_elsie_store_anchor(
            authority,
            ReceiptNamespace.MEMORY_CANDIDATE_STORE,
            subject=_state_subject(path),
            anchor_store=anchor_store,
        )
        if existing_head is not None:
            raise ElsieReceiptError("protected memory candidate state is missing")
        return False, authority, False, False
    effective_entry_limit = _effective_limit(entry_limit, MAX_AUTO_FINGERPRINTS)
    with _exclusive_state_lock(path):
        changed = False
        authority = receipt_authority
        if _pending_candidate_exists(path):
            authority = authority or ElsieReceiptAuthority.from_existing_key_store()
            changed = _recover_protected_candidate_state_unlocked(
                path,
                authority,
                entry_limit=effective_entry_limit,
                anchor_store=anchor_store,
            )
        try:
            payload = _read_state_json(path)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ElsieReceiptError("memory candidate state is malformed or unsafe") from exc
        if payload is _STATE_MISSING:
            return changed, authority, changed, False
        if not isinstance(payload, dict):
            raise ElsieReceiptError("memory candidate state is malformed")
        if payload.get("version") == STATE_VERSION:
            resolved_for_purge = authority
            if resolved_for_purge is None:
                resolved_for_purge = ElsieReceiptAuthority.from_optional_existing_key_store()
            if (
                resolved_for_purge is not None
                and load_elsie_store_anchor(
                    resolved_for_purge,
                    ReceiptNamespace.MEMORY_CANDIDATE_STORE,
                    subject=_state_subject(path),
                    anchor_store=anchor_store,
                )
                is not None
            ):
                raise ElsieReceiptError("legacy memory candidate state conflicts with protected anchor")
            # Purging an offline-dictionary-recoverable legacy digest does not
            # require (and must never create) receipt authority.  The exact
            # keyless tombstone cannot authenticate as protected state; a
            # later concrete protected write replaces it with schema v2.
            _atomic_write_text(
                path,
                json.dumps(_PURGED_LEGACY_STATE, indent=2, sort_keys=True),
            )
            return True, resolved_for_purge, True, True
        if payload == _PURGED_LEGACY_STATE:
            resolved_for_tombstone = authority
            if resolved_for_tombstone is None:
                resolved_for_tombstone = ElsieReceiptAuthority.from_optional_existing_key_store()
            if (
                resolved_for_tombstone is not None
                and load_elsie_store_anchor(
                    resolved_for_tombstone,
                    ReceiptNamespace.MEMORY_CANDIDATE_STORE,
                    subject=_state_subject(path),
                    anchor_store=anchor_store,
                )
                is not None
            ):
                raise ElsieReceiptError("protected memory candidate tombstone conflicts with anchor")
            return changed, resolved_for_tombstone, True, False
        authority = authority or ElsieReceiptAuthority.from_existing_key_store()
        if payload.get("version") == LEGACY_PROTECTED_STATE_VERSION:
            if (
                set(payload)
                != {
                    "version",
                    "protected",
                    "receipt_binding",
                    "accepted",
                    "stored_total",
                }
                or payload.get("protected") is not True
            ):
                raise ElsieReceiptError("legacy protected memory candidate state is malformed")
            authority.require_binding(payload.get("receipt_binding"))
            accepted = payload.get("accepted")
            stored_total = payload.get("stored_total")
            if (
                not isinstance(accepted, list)
                or isinstance(stored_total, bool)
                or not isinstance(stored_total, int)
                or stored_total < 0
            ):
                raise ElsieReceiptError("legacy protected memory candidate state is malformed")
            cleaned: list[dict[str, str]] = []
            bounded = accepted[-effective_entry_limit:] if effective_entry_limit else []
            for entry in bounded:
                if not isinstance(entry, Mapping):
                    continue
                fingerprint = entry.get("fingerprint")
                day = entry.get("day")
                if is_hmac_receipt(fingerprint) and isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    cleaned.append(
                        {
                            "fingerprint": str(fingerprint),
                            "day": day,
                            "outcome": "stored",
                        }
                    )
            migrated_state = _empty_state(
                protected=True,
                receipt_authority=authority,
            )
            migrated_state["accepted"] = cleaned
            migrated_state["stored_total"] = stored_total
            _commit_protected_state(
                path,
                migrated_state,
                authority,
                anchor_store=anchor_store,
            )
            return True, authority, True, True
        _load_state(
            path,
            entry_limit=effective_entry_limit,
            protected=True,
            receipt_authority=authority,
            anchor_store=anchor_store,
        )
        return changed, authority, True, False


def prepare_protected_candidate_state(
    state_path: Path | str,
    *,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
    entry_limit: int | None = None,
) -> bool:
    """Explicitly recover/migrate protected state without creating a key."""

    changed, _authority, _existed, _legacy_purged = _prepare_protected_candidate_state(
        state_path,
        receipt_authority=receipt_authority,
        anchor_store=anchor_store,
        entry_limit=entry_limit,
    )
    return changed


def process_memory_candidates(
    original_user_text: str,
    existing_memories: Sequence[str],
    state_path: Path | str,
    enabled: bool,
    persist: PersistenceFn,
    *,
    now: datetime | None = None,
    telemetry: TelemetryFn | None = None,
    daily_limit: int | None = None,
    entry_limit: int | None = None,
    char_limit: int | None = None,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> dict[str, Any]:
    """Evaluate and persist at most one privacy-safe durable memory.

    The returned payload and optional telemetry contain aggregate reason counts
    only. Candidate text and rejected fingerprints are deliberately omitted.
    """

    effective_daily_limit = _effective_limit(daily_limit, MAX_DAILY_WRITES)
    effective_entry_limit = _effective_limit(entry_limit, MAX_AUTO_FINGERPRINTS)
    effective_char_limit = _effective_limit(char_limit, MAX_MEMORY_CHARS)
    reason_counts: Counter[str] = Counter()
    base_result: dict[str, Any] = {
        "version": PROTECTED_STATE_VERSION if protected else STATE_VERSION,
        "status": "disabled" if not enabled else "no_candidates",
        "reason": "automatic memory candidates are disabled" if not enabled else "no durable marker found",
        "counts": {"extracted": 0, "evaluated": 0, "eligible": 0, "stored": 0, "rejected": 0},
        "reason_counts": {},
        "limits": {
            "candidates_per_turn": MAX_CANDIDATES_PER_TURN,
            "stored_per_turn": MAX_STORED_PER_TURN,
            "daily_writes": effective_daily_limit,
            "auto_fingerprints": effective_entry_limit,
            "memory_chars": effective_char_limit,
        },
        "state": {"auto_fingerprints": 0, "daily_writes": 0},
    }
    authority = receipt_authority if protected else None
    if protected:
        try:
            (
                _changed,
                resolved_authority,
                _existed,
                legacy_purged,
            ) = _prepare_protected_candidate_state(
                state_path,
                receipt_authority=authority,
                anchor_store=anchor_store,
                entry_limit=effective_entry_limit,
            )
            authority = resolved_authority
            if legacy_purged:
                reason_counts["legacy_state_purged"] += 1
                base_result["reason_counts"] = dict(sorted(reason_counts.items()))
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError, ElsieReceiptError):
            reason_counts["state_error"] += 1
            base_result["status"] = "error"
            base_result["reason"] = "protected memory candidate state was unavailable"
            base_result["reason_counts"] = dict(reason_counts)
            _emit_telemetry(telemetry, base_result)
            return base_result

    if not enabled:
        _emit_telemetry(telemetry, base_result)
        return base_result

    candidates, overflow = _extract_candidates_with_overflow(original_user_text)
    base_result["counts"]["extracted"] = len(candidates)
    if overflow:
        reason_counts["candidate_limit"] += overflow
    if not candidates:
        reason_counts["no_durable_marker"] += 1
        base_result["reason_counts"] = dict(sorted(reason_counts.items()))
        _emit_telemetry(telemetry, base_result)
        return base_result

    if protected and authority is None:
        try:
            # No protected state exists yet and this turn has a concrete write
            # candidate, so this is the sole path allowed to create the key.
            authority = ElsieReceiptAuthority.from_key_store()
        except ElsieReceiptError:
            reason_counts["receipt_authority_error"] += 1
            base_result["status"] = "error"
            base_result["reason"] = "protected memory receipt authority was unavailable"
            base_result["reason_counts"] = dict(reason_counts)
            _emit_telemetry(telemetry, base_result)
            return base_result

    path = Path(state_path)
    utc_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = utc_now.date().isoformat()
    current_memories = [str(memory) for memory in existing_memories]
    memory_chars = sum(len(memory) for memory in current_memories)
    stored_count = 0
    eligible_count = 0
    try:
        with _exclusive_state_lock(path):
            state = _load_state(
                path,
                entry_limit=effective_entry_limit,
                protected=protected,
                receipt_authority=authority,
                anchor_store=anchor_store,
            )
            accepted = list(state["accepted"])
            accepted_fingerprints = [entry["fingerprint"] for entry in accepted]
            daily_writes = sum(1 for entry in accepted if entry["day"] == today)
            for candidate in candidates:
                base_result["counts"]["evaluated"] += 1
                decision = evaluate_candidate(
                    candidate,
                    current_memories,
                    accepted_fingerprints,
                    protected=protected,
                    receipt_authority=authority,
                )
                if not decision.eligible:
                    reason_counts[decision.reason] += 1
                    continue
                eligible_count += 1
                if stored_count >= MAX_STORED_PER_TURN:
                    reason_counts["turn_write_limit"] += 1
                    continue
                if daily_writes >= effective_daily_limit:
                    reason_counts["daily_write_limit"] += 1
                    continue
                if len(accepted) >= effective_entry_limit:
                    reason_counts["auto_fingerprint_capacity"] += 1
                    continue
                if memory_chars + len(candidate.text) > effective_char_limit:
                    reason_counts["memory_char_capacity"] += 1
                    continue
                pending_entry: dict[str, str] | None = None
                if protected:
                    if authority is None:  # pragma: no cover - guarded above
                        raise ElsieReceiptError("protected receipt authority is missing")
                    pending_entry = {
                        "fingerprint": decision.fingerprint,
                        "day": today,
                        "outcome": "pending",
                    }
                    accepted.append(pending_entry)
                    accepted_fingerprints.append(decision.fingerprint)
                    daily_writes += 1
                    state["accepted"] = accepted
                    # Persist the at-most-once attempt receipt before invoking
                    # Echo. A crash or unknown outcome then suppresses replay.
                    _commit_protected_state(
                        path,
                        state,
                        authority,
                        anchor_store=anchor_store,
                    )
                try:
                    persisted = bool(persist(candidate.text))
                except Exception:
                    reason_counts["persistence_error"] += 1
                    continue
                if not persisted:
                    reason_counts["persistence_rejected"] += 1
                    current_memories.append(candidate.text)
                    if protected and pending_entry is not None:
                        if authority is None:  # pragma: no cover - guarded above
                            raise ElsieReceiptError("protected receipt authority is missing")
                        accepted.remove(pending_entry)
                        accepted_fingerprints.remove(decision.fingerprint)
                        daily_writes -= 1
                        state["accepted"] = accepted
                        _commit_protected_state(
                            path,
                            state,
                            authority,
                            anchor_store=anchor_store,
                        )
                    continue
                if protected:
                    if pending_entry is None:  # pragma: no cover - guarded above
                        raise ElsieReceiptError("protected memory attempt is missing")
                    pending_entry["outcome"] = "stored"
                else:
                    accepted.append({"fingerprint": decision.fingerprint, "day": today})
                    accepted_fingerprints.append(decision.fingerprint)
                    daily_writes += 1
                current_memories.append(candidate.text)
                memory_chars += len(candidate.text)
                stored_count += 1
                reason_counts["stored"] += 1
                if protected:
                    if authority is None:  # pragma: no cover - guarded above
                        raise ElsieReceiptError("protected receipt authority is missing")
                    state["accepted"] = accepted
                    state["stored_total"] = int(state.get("stored_total") or 0) + 1
                    _commit_protected_state(
                        path,
                        state,
                        authority,
                        anchor_store=anchor_store,
                    )
            if not protected:
                payload = {
                    "version": STATE_VERSION,
                    "accepted": (accepted[-effective_entry_limit:] if effective_entry_limit else []),
                    "stored_total": int(state.get("stored_total") or 0) + stored_count,
                }
                _atomic_write_text(
                    path,
                    json.dumps(payload, indent=2, sort_keys=True),
                )
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, ElsieReceiptError):
        reason_counts["state_error"] += 1
        base_result["status"] = "error"
        base_result["reason"] = "memory candidate state was unavailable"
        base_result["counts"]["eligible"] = eligible_count
        base_result["counts"]["stored"] = stored_count
        base_result["counts"]["rejected"] = len(candidates) - stored_count
        base_result["reason_counts"] = dict(sorted(reason_counts.items()))
        _emit_telemetry(telemetry, base_result)
        return base_result

    base_result["counts"]["eligible"] = eligible_count
    base_result["counts"]["stored"] = stored_count
    base_result["counts"]["rejected"] = len(candidates) - stored_count
    base_result["reason_counts"] = dict(sorted(reason_counts.items()))
    base_result["state"] = {
        "auto_fingerprints": len(accepted),
        "daily_writes": daily_writes,
    }
    if stored_count:
        base_result["status"] = "stored"
        base_result["reason"] = "stored one durable memory candidate"
    else:
        base_result["status"] = "rejected"
        base_result["reason"] = "no candidate passed every eligibility and capacity gate"
    _emit_telemetry(telemetry, base_result)
    return base_result


__all__ = [
    "EligibilityDecision",
    "MemoryCandidate",
    "evaluate_candidate",
    "extract_candidates",
    "memory_fingerprint",
    "normalize_memory_text",
    "prepare_protected_candidate_state",
    "PROTECTED_STATE_VERSION",
    "process_memory_candidates",
]
