"""Task-local reasoning guidance for conflicting structured sources."""

from __future__ import annotations

import json
import os
import re
from collections import deque
from pathlib import Path
from typing import Any

from .chat_protocol import normalize_tool_call


_AUTHORITY_CUES = ("authoritative", "source of truth", "live files", "live manifest")
_STALE_CUES = ("stale", "retrieved context", "retrieved memory", "rag", "lower authority")
_STRUCTURED_CUES = ("settings", "config", "manifest", "json", "yaml", "structured")
_RECONCILIATION_MESSAGE_WINDOW = 128


def guidance_for_prompt(prompt: str) -> str | None:
    """Return a compact reconciliation algorithm only for clear conflict tasks."""

    lowered = (prompt or "").casefold()
    if not all(
        any(cue in lowered for cue in family)
        for family in (_AUTHORITY_CUES, _STALE_CUES, _STRUCTURED_CUES)
    ):
        return None
    return (
        "Use provenance-aware structured reconciliation:\n"
        "1. Establish source priority from the task and inspect the authoritative, target, and lower-authority sources.\n"
        "2. Preserve the target schema. Trace each existing target value to matching facts in the stale and authoritative sources; key names may differ across schemas.\n"
        "3. Replace every stale semantic slot with its authoritative counterpart. Do not leave the stale value in place or merely append a source-named duplicate.\n"
        "4. Turn every required field, objective assertion, and artifact-content requirement into a checklist. Confirm each exact authoritative value appears where required, then run fail-on-mismatch verification."
        "\n5. Treat negative artifact requirements strictly: if stale values must not be included or presented as facts, omit the literal stale values even from comparison sections; describe the overridden categories without quoting obsolete values."
    )


def _normalized_field(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _read_records(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Pair read results with their model-requested paths."""

    pending: deque[tuple[str, str | None]] = deque()
    records: list[tuple[str, str]] = []
    for message in messages[-_RECONCILIATION_MESSAGE_WINDOW:]:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or ():
                name, args = normalize_tool_call(call)
                if name == "read_file":
                    pending.append((str(args.get("path") or ""), str(call.get("id") or "") or None))
            continue
        if message.get("role") != "tool" or message.get("name") != "read_file":
            continue
        call_id = str(message.get("tool_call_id") or "") or None
        path = ""
        if call_id:
            for index, (candidate_path, candidate_id) in enumerate(pending):
                if candidate_id == call_id:
                    path = candidate_path
                    del pending[index]
                    break
        elif pending:
            path, _candidate_id = pending.popleft()
        records.append((path, str(message.get("content") or "")))
    return records


def _json_objects(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    objects: list[tuple[str, dict[str, Any]]] = []
    for path, content in _read_records(messages):
        try:
            value = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            objects.append((path, value))
    return objects


def _stale_label(text: str, value: str) -> str | None:
    pattern = re.compile(
        rf"([A-Za-z][A-Za-z _/-]{{1,40}}?)\s+[`\"']?{re.escape(value)}(?:[`\"']|\b)",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    label = matches[-1].group(1).strip().rsplit(" and ", 1)[-1]
    return _normalized_field(label)


def _lineage_replacements(
    messages: list[dict[str, Any]],
    stale_text: str,
) -> list[tuple[str, str, str, str, str]]:
    objects = _json_objects(messages)
    authoritative = next(
        (
            (path, obj)
            for path, obj in objects
            if any(
                isinstance(value, str)
                and ("source of truth" in value.casefold() or "authoritative" in value.casefold())
                for value in obj.values()
            )
        ),
        None,
    )
    if authoritative is None:
        return []
    _authoritative_path, authoritative_object = authoritative
    authority_by_field = {
        _normalized_field(str(key)): value for key, value in authoritative_object.items()
    }
    replacements: list[tuple[str, str, str, str, str]] = []
    for target_path, target in objects:
        if target is authoritative_object:
            continue
        for target_key, old_value in target.items():
            if not isinstance(old_value, str) or old_value not in stale_text:
                continue
            label = _stale_label(stale_text, old_value)
            if not label:
                continue
            authority_key = next(
                (
                    key
                    for key in authority_by_field
                    if key == label or key.endswith(f"_{label}") or label.endswith(f"_{key}")
                ),
                None,
            )
            if authority_key is None:
                continue
            new_value = authority_by_field[authority_key]
            if isinstance(new_value, str) and new_value != old_value:
                replacements.append(
                    (target_path, str(target_key), old_value, authority_key, new_value)
                )
    return list(dict.fromkeys(replacements))


def lineage_guidance(messages: list[dict[str, Any]], stale_text: str) -> str | None:
    """Infer cross-schema replacements by tracing target values to stale labels."""

    replacements = _lineage_replacements(messages, stale_text)
    if not replacements:
        return None
    lines = [
        f"- Keep target key `{target_key}` and set its value to authoritative `{new_value}` "
        f"because its current `{old_value}` value matches the stale `{authority_key}` fact. "
        f"Do not remove or rename `{target_key}`, and do not add `{authority_key}` as a duplicate."
        for _target_path, target_key, old_value, authority_key, new_value in replacements
    ]
    return (
        "[Algo provenance inference]\n"
        "Cross-schema value lineage found:\n"
        + "\n".join(lines)
        + "\nDo not retain those stale target values or add duplicate source-named keys."
    )


def lineage_constraints(
    messages: list[dict[str, Any]],
) -> list[tuple[str, str, str, str, str]]:
    """Recover unique provenance constraints from recent read results."""

    constraints: list[tuple[str, str, str, str, str]] = []
    for _path, text in _read_records(messages):
        constraints.extend(_lineage_replacements(messages, text))
    return list(dict.fromkeys(constraints))


_STALE_VALUE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bproject(?:\s+name)?(?:\s+is)?\s+([^,.\n]+)",
        r"\bapproval\s+ticket\s+([^,.\n]+)",
        r"\bstatus\s+endpoint\s+([^,.\n]+)",
        r"\bfeature\s+flag\s+([^,.\n]+)",
        r"\boperations\s+contact\s+([^,.\n]+)",
        r"\bgo-live\s+date(?:/window)?\s+([^,.\n]+)",
    )
)


def _stale_fact_values(messages: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for _path, text in _read_records(messages):
        source_text = text.split("\n\n[Algo ", 1)[0]
        lowered = source_text.casefold()
        if (
            "stale" not in lowered
            or "claim" not in lowered
            or not any(cue in lowered for cue in ("rag", "cache", "lower authority"))
        ):
            continue
        for pattern in _STALE_VALUE_PATTERNS:
            for match in pattern.finditer(source_text):
                value = match.group(1).strip().strip(".`'\"")
                if value:
                    values.append(value)
    return list(dict.fromkeys(values))


def structured_write_violation(
    name: str,
    args: dict[str, Any],
    messages: list[dict[str, Any]],
) -> str | None:
    """Reject full-object JSON writes that violate inferred target-schema lineage."""

    if name not in {"write_file", "edit_file"}:
        return None
    raw = args.get("content") if name == "write_file" else args.get("new_string")
    write_path = str(args.get("path") or "")
    write_name = Path(write_path).name.casefold()
    if name == "write_file" and write_name:
        explicit_omission = any(
            "do not include stale" in str(message.get("content") or "").casefold()
            and write_name in str(message.get("content") or "").casefold()
            for message in messages[-_RECONCILIATION_MESSAGE_WINDOW:]
        )
        if explicit_omission:
            stale_present = [value for value in _stale_fact_values(messages) if value in str(raw or "")]
            if stale_present:
                return (
                    "Artifact omission invariant blocked this write: the task says not to include stale "
                    "values in the summary. Omit the literal stale values entirely, including in comparison "
                    f"or 'not facts' sections. Present: {', '.join(stale_present[:6])}. Describe the "
                    "overridden categories generically and include only authoritative values."
                )
    try:
        candidate = json.loads(str(raw or ""))
    except json.JSONDecodeError:
        return None
    if not isinstance(candidate, dict):
        return None
    normalized_write_path = os.path.normcase(os.path.normpath(write_path))
    for target_path, target_key, old_value, authority_key, new_value in lineage_constraints(messages):
        if not target_path or os.path.normcase(os.path.normpath(target_path)) != normalized_write_path:
            continue
        if candidate.get(target_key) != new_value:
            return (
                "Provenance invariant blocked this structured write: preserve target schema by "
                f"keeping `{target_key}` and setting its value to `{new_value}`. Do not remove or "
                f"rename `{target_key}`, and do not substitute a new `{authority_key}` key."
            )
    return None


def augment_read_result(
    name: str,
    result: str,
    *,
    messages: list[dict[str, Any]] | None = None,
) -> str:
    """Attach guidance when a newly read task/file reveals a source conflict."""

    if name != "read_file":
        return result
    additions = []
    guidance = guidance_for_prompt(result)
    if guidance is not None:
        additions.append(f"[Algo reasoning strategy]\n{guidance}")
    lineage = lineage_guidance(messages or [], result)
    if lineage is not None:
        additions.append(lineage)
    if not additions:
        return result
    return f"{result}\n\n" + "\n\n".join(additions)


__all__ = [
    "augment_read_result",
    "guidance_for_prompt",
    "lineage_constraints",
    "lineage_guidance",
    "structured_write_violation",
]
