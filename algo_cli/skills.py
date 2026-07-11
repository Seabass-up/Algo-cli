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
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import CONFIG_DIR, _atomic_write_text
from .private_event_store import PrivateEventStore, RetentionPolicy


SKILLS_DIR = CONFIG_DIR / "skills"
SKILL_QUARANTINE_DIR = CONFIG_DIR / "skill_quarantine"
RUN_HISTORY_PATH = CONFIG_DIR / "run_history.jsonl"
PRIVATE_RUN_HISTORY_PATH = CONFIG_DIR / "private" / "run_history.jsonl"

RUN_HISTORY_LIMIT = 60          # cap the JSONL file
CRYSTALLIZE_LOOKBACK = 6        # recent runs the crystallizer reviews
MAX_SKILLS_PER_PASS = 5         # guard against a model spamming files
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
) -> None:
    """Append one opted-in completed-run summary to the private JSONL store."""
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
        return


def _trim_run_history() -> None:
    try:
        lines = RUN_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= RUN_HISTORY_LIMIT:
        return
    kept = lines[-RUN_HISTORY_LIMIT:]
    _atomic_write_text(RUN_HISTORY_PATH, "\n".join(kept) + "\n")


def recent_runs(n: int = CRYSTALLIZE_LOOKBACK) -> list[dict[str, Any]]:
    limit = max(1, n)
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


def quarantine_skill(candidate: dict[str, Any]) -> tuple[Path | None, str]:
    """Persist an untrusted skill candidate outside the active harness roots."""

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


def promote_quarantined_skill(name: str) -> Path:
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


def reject_quarantined_skill(name: str) -> Path:
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


def crystallize(llm_fn: LLMFn, lookback: int = CRYSTALLIZE_LOOKBACK) -> dict[str, Any]:
    """Review recent runs, extract skill candidates, write new SKILL.md files.

    Returns {"created": [slugs], "skipped": [names], "reason": str}.
    """
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


def skills_status() -> dict[str, Any]:
    titles = existing_skill_titles()
    try:
        run_count = len(recent_runs(RUN_HISTORY_LIMIT))
        run_history_readiness = _run_history_store().readiness()
    except OSError:
        run_count = 0
        run_history_readiness = {"status": "error"}
    return {
        "skills_dir": str(SKILLS_DIR),
        "skill_count": len(titles),
        "skills": titles,
        "run_history": str(PRIVATE_RUN_HISTORY_PATH),
        "run_count": run_count,
        "run_history_readiness": run_history_readiness,
        "quarantined": quarantined_skill_titles(),
        "quarantine_dir": str(SKILL_QUARANTINE_DIR),
    }
