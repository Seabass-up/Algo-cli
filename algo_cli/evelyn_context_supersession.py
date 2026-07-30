"""Epoch-bound semantic supersession for repeated, read-only tool snapshots.

The chat history is also a provider protocol transcript: an assistant tool call
must keep its matching tool result.  Supersession therefore replaces only an
older result's *content* with a compact integrity receipt.  It never removes
or rewrites assistant calls, call IDs, signatures, failures, mutations, or
verification evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .action_registry import get_action_spec
from .chat_protocol import normalize_tool_call
from .irene_privacy_views import keyed_action_fingerprint


RECEIPT_PREFIX = "[Algo superseded result receipt v1"
VERIFICATION_RECEIPT_PREFIX = "[Algo verification result receipt v1"
VERIFICATION_COMPACT_MIN_CHARS = 1_200
VERIFICATION_EXCERPT_CHARS = 600
TARGET_EPOCH_FIELD = "_algo_target_epoch"
TARGET_EPOCH_SCHEMA_VERSION = 1
_MAX_EPOCH = (1 << 63) - 1
_TARGET_KINDS = frozenset({"browser_document", "desktop_surface", "external_resource"})
_EXTERNAL_TOOL_TARGET_KINDS = {
    "web_fetch": frozenset({"external_resource"}),
}
_SAFE_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HMAC_ID_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")

# Keep this list deliberately narrow.  A tool is eligible only when a newer
# successful call with the same normalized arguments represents the same live
# snapshot.  Mutation tools and verifier output (run_shell/git_diff) are absent
# by design and therefore remain immutable in the conversation transcript.
LOCAL_SUPERSEDABLE_TOOLS = frozenset(
    {
        "available_actions",
        "git_status",
        "harness_read",
        "harness_search",
        "harness_stats",
        "list_directory",
        "model_show",
        "query_knowledge_graph",
        "read_file",
        "read_pdf",
        "search_files",
    }
)
EXTERNAL_SUPERSEDABLE_TOOLS = frozenset({"web_fetch"})
SUPERSEDABLE_TOOLS = LOCAL_SUPERSEDABLE_TOOLS | EXTERNAL_SUPERSEDABLE_TOOLS

_PATH_TOOLS = frozenset({"read_file", "read_pdf", "list_directory", "search_files"})
_BAD_RESULT_PREFIXES = (
    "blocked",
    "error",
    "skipped",
    "tool argument error",
    "tool error",
    "unknown tool",
    "user denied",
)
_SAFE_RECEIPT_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


@dataclass(frozen=True)
class SupersessionStats:
    """Content-free measurements for one supersession pass."""

    candidates: int
    superseded: int
    before_tokens: int
    after_tokens: int
    saved_tokens: int
    reduction_pct: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "candidates": self.candidates,
            "superseded": self.superseded,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "saved_tokens": self.saved_tokens,
            "reduction_pct": self.reduction_pct,
        }


@dataclass(frozen=True)
class ExternalTargetEpoch:
    """HMAC-authenticated content-free external target generation."""

    target_kind: str
    target_id: str
    epoch: int
    revision: str
    fencing_token: int
    auth_tag: str
    schema_version: int = TARGET_EPOCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != TARGET_EPOCH_SCHEMA_VERSION:
            raise ValueError("target epoch schema version is unsupported")
        if type(self.target_kind) is not str or self.target_kind not in _TARGET_KINDS:
            raise ValueError("target epoch kind is unsupported")
        if type(self.target_id) is not str or not _HMAC_ID_RE.fullmatch(self.target_id):
            raise ValueError("target epoch identifier is invalid")
        if type(self.epoch) is not int or not 1 <= self.epoch <= _MAX_EPOCH:
            raise ValueError("target epoch must be a positive bounded integer")
        if type(self.revision) is not str or not _SAFE_REVISION_RE.fullmatch(self.revision):
            raise ValueError("target revision is invalid")
        if type(self.fencing_token) is not int or not 0 <= self.fencing_token <= _MAX_EPOCH:
            raise ValueError("target fencing token is invalid")
        if type(self.auth_tag) is not str or not _HMAC_ID_RE.fullmatch(self.auth_tag):
            raise ValueError("target epoch authentication tag is invalid")

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "epoch": self.epoch,
            "revision": self.revision,
            "fencing_token": self.fencing_token,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "auth_tag": self.auth_tag}

    def semantic_identity(self) -> dict[str, Any]:
        return {
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "epoch": self.epoch,
            "revision": self.revision,
            "fencing_token": self.fencing_token,
        }


def _target_epoch_auth_tag(fields: dict[str, Any]) -> str:
    return keyed_action_fingerprint("external_target_epoch_binding", dict(fields))


def _external_target_id(target_kind: str, target: Any) -> str:
    if type(target_kind) is not str or target_kind not in _TARGET_KINDS:
        raise ValueError("target epoch kind is unsupported")
    if type(target) is not str or not target or len(target) > 8_192:
        raise ValueError("external target must be a non-empty bounded string")
    try:
        encoded_target = target.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("external target is not valid Unicode") from exc
    if len(encoded_target) > 8_192:
        raise ValueError("external target must be a non-empty bounded string")
    if any(unicodedata.category(character).startswith("C") for character in target):
        raise ValueError("external target contains control characters")
    return keyed_action_fingerprint(
        "external_target_identity",
        {"target_kind": target_kind, "target": target},
    )


def issue_external_target_epoch(
    *,
    target_kind: str,
    target: str,
    epoch: int,
    revision: str,
    fencing_token: int = 0,
) -> ExternalTargetEpoch:
    """Issue a runtime-only authenticated binding without persisting its target."""

    target_id = _external_target_id(target_kind, target)
    placeholder = f"hmac-sha256:{'0' * 64}"
    draft = ExternalTargetEpoch(
        target_kind=target_kind,
        target_id=target_id,
        epoch=epoch,
        revision=revision,
        fencing_token=fencing_token,
        auth_tag=placeholder,
    )
    return ExternalTargetEpoch(
        target_kind=draft.target_kind,
        target_id=draft.target_id,
        epoch=draft.epoch,
        revision=draft.revision,
        fencing_token=draft.fencing_token,
        auth_tag=_target_epoch_auth_tag(draft.unsigned_dict()),
    )


def parse_external_target_epoch(value: Any) -> ExternalTargetEpoch | None:
    """Verify a closed authenticated binding; malformed metadata is ineligible."""

    # Runtime adapters attach decoded JSON dictionaries.  Reject Mapping
    # implementations and dict subclasses so parsing cannot invoke hostile
    # user-defined iteration or lookup behavior.
    if type(value) is not dict:
        return None
    expected_fields = {
        "schema_version",
        "target_kind",
        "target_id",
        "epoch",
        "revision",
        "fencing_token",
        "auth_tag",
    }
    if len(value) != len(expected_fields) or not all(type(key) is str for key in value):
        return None
    if set(value) != expected_fields:
        return None
    try:
        binding = ExternalTargetEpoch(
            schema_version=value["schema_version"],
            target_kind=value["target_kind"],
            target_id=value["target_id"],
            epoch=value["epoch"],
            revision=value["revision"],
            fencing_token=value["fencing_token"],
            auth_tag=value["auth_tag"],
        )
        expected = _target_epoch_auth_tag(binding.unsigned_dict())
    except (KeyError, TypeError, ValueError):
        return None
    return binding if hmac.compare_digest(binding.auth_tag, expected) else None


@dataclass(frozen=True)
class _ToolExchange:
    result_index: int
    call_id: str | None
    name: str
    args: dict[str, Any]
    mutation_epoch: int
    target_epoch: ExternalTargetEpoch | None


def is_supersession_receipt(content: Any) -> bool:
    """Return whether *content* is a receipt produced by this module."""

    return str(content or "").startswith(RECEIPT_PREFIX)


def is_verification_receipt(content: Any) -> bool:
    """Return whether *content* is a successful-verifier integrity receipt."""

    return str(content or "").startswith(VERIFICATION_RECEIPT_PREFIX)


def is_supersedable_tool(name: str) -> bool:
    """Return whether a tool produces a safe-to-supersede snapshot."""

    return str(name or "") in SUPERSEDABLE_TOOLS


def is_count_prunable_tool(name: str) -> bool:
    """Return whether lossy count pruning may remove a local snapshot pair.

    External observations always require an epoch-bound receipt.  They are
    therefore excluded from the older pair-deletion fallback even when a
    runtime adapter has attached a valid target binding.
    """

    return str(name or "") in LOCAL_SUPERSEDABLE_TOOLS


def _estimate_text_tokens(text: Any) -> int:
    value = str(text or "")
    if not value:
        return 0
    return max(1, (len(value) + 3) // 4)


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    total = 12
    total += _estimate_text_tokens(message.get("role", ""))
    total += _estimate_text_tokens(message.get("content", ""))
    total += _estimate_text_tokens(message.get("thinking", ""))
    total += _estimate_text_tokens(json.dumps(message.get("tool_calls", []), ensure_ascii=False, default=str))
    total += _estimate_text_tokens(message.get("tool_name", ""))
    return total


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages)


def _call_id(call: Any) -> str | None:
    if isinstance(call, dict):
        raw = call.get("id") or (call.get("function") or {}).get("id")
    else:
        raw = getattr(call, "id", None)
    text = str(raw or "").strip()
    return text or None


def _pair_tool_exchanges(messages: list[dict[str, Any]]) -> list[_ToolExchange]:
    """Pair tool results with calls without changing the provider transcript."""

    pending: list[tuple[str | None, str, dict[str, Any]]] = []
    by_id: dict[str, tuple[str | None, str, dict[str, Any]]] = {}
    exchanges: list[_ToolExchange] = []
    mutation_epoch = 0

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or ():
                name, args = normalize_tool_call(call)
                record = (_call_id(call), name, args)
                pending.append(record)
                if record[0]:
                    by_id[record[0]] = record
            continue
        if role != "tool":
            continue

        raw_result_id = str(message.get("tool_call_id") or "").strip() or None
        matched_record = by_id.pop(raw_result_id, None) if raw_result_id else None
        if matched_record is not None:
            try:
                pending.remove(matched_record)
            except ValueError:
                pass
        elif not raw_result_id and pending:
            # Ollama histories may omit IDs.  Consume in protocol order, matching
            # the OpenAI/xAI adapters' existing FIFO fallback.
            result_name = str(message.get("tool_name") or message.get("name") or "")
            match_index = next(
                (position for position, item in enumerate(pending) if not result_name or item[1] == result_name),
                0,
            )
            matched_record = pending.pop(match_index)
            if matched_record[0]:
                by_id.pop(matched_record[0], None)
        if matched_record is None:
            continue
        call_id, name, args = matched_record
        result_name = str(message.get("tool_name") or message.get("name") or name)
        if result_name and result_name != name:
            # A mismatched result name is not trusted as evidence for this call.
            continue
        exchanges.append(
            _ToolExchange(
                result_index=index,
                call_id=call_id,
                name=name,
                args=args,
                mutation_epoch=mutation_epoch,
                target_epoch=parse_external_target_epoch(message.get(TARGET_EPOCH_FIELD)),
            )
        )
        try:
            mutates_state = bool(get_action_spec(name).mutates_state)
        except KeyError:
            # Unknown/custom tools are not assumed to mutate; they are never
            # supersedable unless deliberately added to the narrow allowlist.
            mutates_state = False
        if mutates_state:
            # Advance conservatively even when the result reports denial or
            # failure. Losing a pre-attempt snapshot is worse than retaining
            # one extra piece of evidence.
            mutation_epoch += 1
    return exchanges


def _normalized_path(value: Any, cwd: str) -> str:
    raw = str(value or ".").strip() or "."
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(cwd or ".").expanduser() / path
        # normpath is lexical: it avoids filesystem access and symlink following.
        return os.path.normcase(os.path.normpath(str(path)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return raw


def _bounded_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _canonical_args(name: str, args: dict[str, Any], cwd: str) -> dict[str, Any]:
    """Normalize defaults that affect snapshot identity without executing I/O."""

    if name == "read_file":
        offset = args.get("offset")
        start_line = offset if offset is not None else args.get("start_line", 1)
        return {
            "path": _normalized_path(args.get("path"), cwd),
            "max_chars": _bounded_int(args.get("max_chars"), 50_000),
            "start_line": max(1, _bounded_int(start_line, 1)),
        }
    if name == "read_pdf":
        return {
            "path": _normalized_path(args.get("path"), cwd),
            "max_chars": _bounded_int(args.get("max_chars"), 50_000),
            "max_pages": _bounded_int(args.get("max_pages"), 24),
        }
    if name == "list_directory":
        return {
            "path": _normalized_path(args.get("path", "."), cwd),
            "limit": _bounded_int(args.get("limit"), 200),
        }
    if name == "search_files":
        return {
            "pattern": str(args.get("pattern") or ""),
            "path": _normalized_path(args.get("path", "."), cwd),
            "glob": str(args.get("glob") or ""),
            "limit": _bounded_int(args.get("limit"), 100),
        }
    if name == "git_status":
        return {"cwd": _normalized_path(".", cwd)}

    defaults: dict[str, dict[str, Any]] = {
        "available_actions": {"topic": None},
        "harness_read": {"max_chars": 20_000},
        "harness_search": {"harness_name": None, "kind": None, "limit": 10},
        "harness_stats": {},
        "model_show": {},
        "query_knowledge_graph": {"limit": 10},
    }
    normalized = dict(defaults.get(name, {}))
    normalized.update(args)
    # A model-provided cwd is ignored for scoped runtime tools; tool_runtime
    # always injects Config.cwd before execution.
    normalized.pop("cwd", None)
    return normalized


def _external_binding_allowed(exchange: _ToolExchange) -> bool:
    binding = exchange.target_epoch
    allowed_kinds = _EXTERNAL_TOOL_TARGET_KINDS.get(exchange.name, frozenset())
    if binding is None or binding.target_kind not in allowed_kinds:
        return False
    if exchange.name == "web_fetch":
        try:
            expected_target_id = _external_target_id(
                "external_resource",
                exchange.args.get("url"),
            )
        except Exception:
            return False
        return hmac.compare_digest(binding.target_id, expected_target_id)
    return False


def _external_invocation_id(exchange: _ToolExchange, cwd: str) -> str | None:
    """Return a content-free identity for one external tool invocation shape."""

    try:
        canonical_args = _canonical_args(exchange.name, exchange.args, cwd)
        return keyed_action_fingerprint(
            "external_supersession_invocation",
            {"tool": exchange.name, "args": canonical_args},
        )
    except Exception:
        # Malformed model arguments or unavailable key material make the
        # observation ineligible; they must never break context processing.
        return None


def _external_epoch_analysis(
    exchanges: list[_ToolExchange],
    cwd: str,
) -> tuple[set[int], dict[int, int]]:
    """Validate external epoch ordering and assign non-crossing segments.

    A target regression invalidates every observation from that target.  For a
    valid target, any missing binding or identity/generation change forms a
    barrier so a later repeated identity can never supersede across navigation,
    relaunch, adapter drift, or an unbound observation.
    """

    last_by_target: dict[tuple[str, str], ExternalTargetEpoch] = {}
    members_by_target: dict[tuple[str, str], set[int]] = {}
    invalid_targets: set[tuple[str, str]] = set()

    for exchange in exchanges:
        if exchange.name not in EXTERNAL_SUPERSEDABLE_TOOLS:
            continue
        binding = exchange.target_epoch
        if not _external_binding_allowed(exchange) or binding is None:
            continue
        target = (binding.target_kind, binding.target_id)
        members_by_target.setdefault(target, set()).add(exchange.result_index)
        previous = last_by_target.get(target)
        if previous is not None:
            regressed = (
                binding.epoch < previous.epoch
                or binding.fencing_token < previous.fencing_token
                or (
                    binding.epoch == previous.epoch
                    and binding.fencing_token == previous.fencing_token
                    and binding.revision != previous.revision
                )
            )
            if regressed:
                invalid_targets.add(target)
        last_by_target[target] = binding

    invalid_indexes = {index for target in invalid_targets for index in members_by_target.get(target, set())}

    # Segment by invocation as well as target binding. This prevents A -> B -> A
    # histories from merging the two A generations even when all tags are
    # individually valid.
    state_by_invocation: dict[
        str,
        tuple[tuple[str, str, int, str, int] | None, int],
    ] = {}
    segments: dict[int, int] = {}
    for exchange in exchanges:
        if exchange.name not in EXTERNAL_SUPERSEDABLE_TOOLS:
            continue
        invocation_id = _external_invocation_id(exchange, cwd)
        if invocation_id is None:
            continue
        previous_identity, segment = state_by_invocation.get(
            invocation_id,
            (None, 0),
        )
        binding = exchange.target_epoch
        if exchange.result_index in invalid_indexes or not _external_binding_allowed(exchange) or binding is None:
            state_by_invocation[invocation_id] = (None, segment + 1)
            continue
        identity = (
            binding.target_kind,
            binding.target_id,
            binding.epoch,
            binding.revision,
            binding.fencing_token,
        )
        if identity != previous_identity:
            segment += 1
        state_by_invocation[invocation_id] = (identity, segment)
        segments[exchange.result_index] = segment
    return invalid_indexes, segments


def _external_transcript_is_complete(
    messages: list[dict[str, Any]],
    exchanges: list[_ToolExchange],
) -> bool:
    """Require every external call/result to participate in exactly one pair."""

    external_call_count = 0
    explicit_result_indexes: set[int] = set()
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            external_call_count += sum(
                1
                for call in message.get("tool_calls") or ()
                if normalize_tool_call(call)[0] in EXTERNAL_SUPERSEDABLE_TOOLS
            )
        elif message.get("role") == "tool":
            result_name = str(message.get("tool_name") or message.get("name") or "")
            if result_name in EXTERNAL_SUPERSEDABLE_TOOLS:
                explicit_result_indexes.add(index)

    paired_result_indexes = {
        exchange.result_index for exchange in exchanges if exchange.name in EXTERNAL_SUPERSEDABLE_TOOLS
    }
    observed_result_indexes = explicit_result_indexes | paired_result_indexes
    return external_call_count == len(paired_result_indexes) == len(observed_result_indexes)


def _semantic_key(
    exchange: _ToolExchange,
    cwd: str,
    *,
    external_segment: int | None = None,
) -> str | None:
    try:
        canonical_args = _canonical_args(exchange.name, exchange.args, cwd)
        payload: dict[str, Any] = {
            "tool": exchange.name,
            "mutation_epoch": exchange.mutation_epoch,
        }
        if exchange.name in EXTERNAL_SUPERSEDABLE_TOOLS:
            if exchange.target_epoch is None or external_segment is None:
                return None
            payload.update(
                {
                    "args_id": keyed_action_fingerprint(
                        "external_supersession_invocation",
                        {"tool": exchange.name, "args": canonical_args},
                    ),
                    "target_epoch": exchange.target_epoch.semantic_identity(),
                    "segment": external_segment,
                }
            )
        else:
            payload["args"] = canonical_args
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    except Exception:
        return None


def _valid_snapshot_args(exchange: _ToolExchange) -> bool:
    """Reject malformed calls instead of merging their unrelated error paths."""

    required = {
        "harness_read": "record_id",
        "harness_search": "query",
        "model_show": "name",
        "query_knowledge_graph": "question",
        "read_file": "path",
        "read_pdf": "path",
        "search_files": "pattern",
        "web_fetch": "url",
    }.get(exchange.name)
    return required is None or bool(str(exchange.args.get(required) or "").strip())


def _successful_snapshot(content: Any) -> bool:
    text = str(content or "").strip()
    if not text or is_supersession_receipt(text):
        return False
    lowered = text.casefold()
    return not lowered.startswith(_BAD_RESULT_PREFIXES)


def _safe_receipt_id(value: str | None, fallback: str) -> str:
    text = _SAFE_RECEIPT_ID_RE.sub("_", str(value or fallback)).strip("_")
    return text[:64] or fallback


def _receipt(
    content: str,
    *,
    newer_call_id: str | None,
    newer_index: int,
    external: bool,
) -> str | None:
    encoded = content.encode("utf-8", errors="replace")
    newer = _safe_receipt_id(newer_call_id, f"result-{newer_index}")
    if external:
        try:
            content_id = keyed_action_fingerprint(
                "superseded_external_result",
                {"content": content},
            )
        except Exception:
            return None
        return f"{RECEIPT_PREFIX} content_id={content_id} bytes={len(encoded)} newer={newer}]"
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{RECEIPT_PREFIX} sha256={digest} bytes={len(encoded)} newer={newer}]"


def _verification_receipt(content: str) -> str:
    encoded = content.encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()
    excerpt = content[-VERIFICATION_EXCERPT_CHARS:].strip()
    return (
        f"{VERIFICATION_RECEIPT_PREFIX} sha256={digest} bytes={len(encoded)} "
        "status=passed exit_code=0]\n"
        f"Final verifier excerpt:\n{excerpt}"
    )


def _compact_successful_verifications(
    messages: list[dict[str, Any]],
    exchanges: list[_ToolExchange],
) -> tuple[int, int]:
    """Collapse only long, successful, recognized shell-verifier output."""

    from .execution_guardrails import classify_verification_command

    candidates = 0
    compacted = 0
    for exchange in exchanges:
        if exchange.name != "run_shell":
            continue
        content = str(messages[exchange.result_index].get("content") or "")
        if (
            len(content) < VERIFICATION_COMPACT_MIN_CHARS
            or is_verification_receipt(content)
            or "[exit code: 0]" not in content.casefold()
        ):
            continue
        command = str(exchange.args.get("command") or "")
        if not classify_verification_command(command).qualifies:
            continue
        candidates += 1
        receipt = _verification_receipt(content)
        if _estimate_text_tokens(receipt) >= _estimate_text_tokens(content):
            continue
        replacement = dict(messages[exchange.result_index])
        replacement["content"] = receipt
        messages[exchange.result_index] = replacement
        compacted += 1
    return candidates, compacted


def supersede_tool_results(
    messages: list[dict[str, Any]],
    *,
    cwd: str = ".",
) -> SupersessionStats:
    """Replace older equivalent snapshot results with compact integrity receipts.

    The operation is intentionally in-place so ``Config.messages`` remains the
    single conversation state.  Only result ``content`` fields change; provider
    call/result structure is preserved.  Calling the function again is a no-op.
    """

    before_tokens = _estimate_messages_tokens(messages)
    all_exchanges = _pair_tool_exchanges(messages)
    if _external_transcript_is_complete(messages, all_exchanges):
        invalid_external_indexes, external_segments = _external_epoch_analysis(
            all_exchanges,
            cwd,
        )
    else:
        invalid_external_indexes = {
            exchange.result_index for exchange in all_exchanges if exchange.name in EXTERNAL_SUPERSEDABLE_TOOLS
        }
        external_segments = {}
    exchanges = [
        exchange
        for exchange in all_exchanges
        if is_supersedable_tool(exchange.name)
        and (
            exchange.name not in EXTERNAL_SUPERSEDABLE_TOOLS
            or (exchange.result_index not in invalid_external_indexes and exchange.result_index in external_segments)
        )
        and _valid_snapshot_args(exchange)
        and _successful_snapshot(messages[exchange.result_index].get("content"))
    ]
    latest_by_key: dict[str, _ToolExchange] = {}
    candidates: list[tuple[_ToolExchange, _ToolExchange]] = []
    for exchange in reversed(exchanges):
        key = _semantic_key(
            exchange,
            cwd,
            external_segment=external_segments.get(exchange.result_index),
        )
        if key is None:
            continue
        newer = latest_by_key.get(key)
        if newer is None:
            latest_by_key[key] = exchange
        else:
            candidates.append((exchange, newer))

    superseded = 0
    for older, newer in candidates:
        old_message = messages[older.result_index]
        content = str(old_message.get("content") or "")
        receipt = _receipt(
            content,
            newer_call_id=newer.call_id,
            newer_index=newer.result_index,
            external=older.name in EXTERNAL_SUPERSEDABLE_TOOLS,
        )
        if receipt is None:
            continue
        # Never spend more tokens to describe supersession than the raw result.
        if _estimate_text_tokens(receipt) >= _estimate_text_tokens(content):
            continue
        replacement = dict(old_message)
        replacement["content"] = receipt
        messages[older.result_index] = replacement
        superseded += 1

    verification_candidates, verification_compacted = _compact_successful_verifications(
        messages,
        all_exchanges,
    )
    superseded += verification_compacted

    after_tokens = _estimate_messages_tokens(messages)
    saved_tokens = max(0, before_tokens - after_tokens)
    reduction_pct = round((saved_tokens / before_tokens) * 100.0, 2) if before_tokens else 0.0
    return SupersessionStats(
        candidates=len(candidates) + verification_candidates,
        superseded=superseded,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        saved_tokens=saved_tokens,
        reduction_pct=reduction_pct,
    )


__all__ = [
    "EXTERNAL_SUPERSEDABLE_TOOLS",
    "ExternalTargetEpoch",
    "LOCAL_SUPERSEDABLE_TOOLS",
    "RECEIPT_PREFIX",
    "SUPERSEDABLE_TOOLS",
    "TARGET_EPOCH_FIELD",
    "VERIFICATION_RECEIPT_PREFIX",
    "SupersessionStats",
    "is_count_prunable_tool",
    "is_supersedable_tool",
    "is_supersession_receipt",
    "is_verification_receipt",
    "issue_external_target_epoch",
    "parse_external_target_epoch",
    "supersede_tool_results",
]
