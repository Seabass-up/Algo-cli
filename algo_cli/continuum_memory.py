"""Native Continuum Memory transport and Algo fact transactions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any


_MEMORY_MUTATION_NAMES = frozenset(
    {
        "memory_init",
        "memory_capture",
        "memory_remember",
        "memory_revoke",
        "memory_resolve",
        "memory_handoff",
        "remember",
        "append_lesson",
    }
)
_RECEIPT_HASH_RE = re.compile(r"(?:hmac-sha256:)?[0-9a-f]{64}\Z")
PROJECT = "legacy-memory"
HARNESS = "algo"
MAX_CONTEXT_BUDGET_BYTES = 32_768


class ContinuumMemoryError(RuntimeError):
    """Continuum could not safely complete an operation."""

    def __init__(self, message: str, *, reason_code: str = "continuum_unavailable") -> None:
        super().__init__(message)
        self.reason_code = reason_code


def selected(cfg: Any) -> bool:
    if getattr(cfg, "memory_config_error", ""):
        raise ContinuumMemoryError(
            "Memory configuration requires repair; select Continuum explicitly before continuing."
        )
    enabled = getattr(cfg, "continuum_enabled", False)
    if type(enabled) is not bool:
        raise ContinuumMemoryError("Invalid Continuum Memory authority selection.")
    return enabled


def _task_id() -> str:
    from . import config

    namespace = hashlib.sha256(str(config.CONFIG_DIR.resolve()).encode()).hexdigest()[:24]
    return f"algo-cli-memory-{namespace}"


def storage_root() -> Path:
    """Match the native CLI's default store; never inspect a retired store."""
    return Path.home() / "Library" / "Application Support" / "ContinuumMemory"


def _native_cli() -> Path:
    # Never resolve a program from a relative PATH entry or the working tree.
    cwd = Path.cwd().resolve()
    entries = [
        entry for entry in os.get_exec_path()
        if entry and Path(entry).is_absolute() and Path(entry).resolve() != cwd
    ]
    executable = shutil.which("continuum-memory", path=os.pathsep.join(entries))
    if executable is None:
        raise ContinuumMemoryError("Install the native continuum-memory command before selecting Continuum.")
    cli = Path(executable).resolve(strict=True)
    if not cli.is_file():
        raise ContinuumMemoryError("The native Continuum command is unavailable.")
    return cli


def invoke_continuum(
    cfg: Any,
    operation: str,
    payload: dict[str, Any],
    *,
    scope: str,
) -> dict[str, Any]:
    """Call Continuum directly, retaining its structured refusals and scope."""

    if not selected(cfg):
        raise ContinuumMemoryError("Continuum Memory is not selected.")
    if operation not in {
        "init",
        "verify",
        "status",
        "capture",
        "remember",
        "get",
        "read",
        "search",
        "context",
        "validate_context",
        "revoke",
        "resolve",
        "explain",
        "history",
        "handoff",
    }:
        raise ContinuumMemoryError("Unknown Continuum operation.")
    if scope not in {"shared", "private"} or type(payload) is not dict or "scope" in payload:
        raise ContinuumMemoryError("Continuum requires one explicit scope and an object payload.")
    try:
        cli = _native_cli()
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["PYTHONSAFEPATH"] = "1"
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if len(body.encode("utf-8")) > 8 * 1024 * 1024:
            raise ValueError("oversized request")
        result = subprocess.run(
            [
                str(cli),
                "--root",
                str(storage_root()),
                "--project",
                PROJECT,
                "--harness",
                HARNESS,
                "--scope",
                scope,
                operation,
            ],
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
            env=env,
            cwd=cli.parent,
        )
        if len(result.stdout.encode("utf-8")) > 1_048_576:
            raise ValueError("oversized reply")
        value = json.loads(result.stdout)
        if result.returncode not in {0, 2} or type(value) is not dict or type(value.get("ok")) is not bool:
            raise ValueError("unexpected reply")
        if (result.returncode == 0) != value["ok"]:
            raise ValueError("inconsistent reply")
        return value
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        raise ContinuumMemoryError(
            "Native Continuum unavailable; no legacy or plaintext fallback was used."
        ) from exc


def doctor(cfg: Any) -> dict[str, Any]:
    heads: dict[str, dict[str, Any]] = {}
    for scope in ("shared", "private"):
        payload = invoke_continuum(cfg, "verify", {}, scope=scope)
        namespace = {"owner": HARNESS if scope == "private" else None, "project": PROJECT, "scope": scope}
        head = payload.get("head")
        if (
            payload.get("ok") is not True
            or payload.get("namespace") != namespace
            or payload.get("encrypted_at_rest") is not True
            or type(head) is not dict
            or type(head.get("sequence")) is not int
            or head["sequence"] < 0
            or type(head.get("mac")) is not str
        ):
            raise ContinuumMemoryError("Continuum Memory verification failed; no fallback was used.")
        heads[scope] = head
    return {
        "ok": True,
        "backend": "continuum-memory",
        "harness": HARNESS,
        "project": PROJECT,
        "default_scope": "private",
        "verify": True,
        "tip_sequence": heads["private"]["sequence"],
        "heads": heads,
    }


def _facts_record_id() -> str:
    return "algo-native:" + hashlib.sha256(_task_id().encode("utf-8")).hexdigest()


def _read_facts(cfg: Any) -> tuple[list[str], int]:
    from .julia_memory_runtime import _validate_content

    payload = invoke_continuum(cfg, "get", {"memory_id": _facts_record_id()}, scope="private")
    if payload.get("ok") is False and payload.get("error") == "not_found":
        return [], 0
    if payload.get("ok") is not True:
        raise ContinuumMemoryError("Continuum Memory refused recall; no fallback was used.")
    try:
        memory = payload["memory"]
        semantic = memory["data"]
        revision = memory["revision"]
        if (
            memory["id"] != _facts_record_id()
            or memory["kind"] != "fact"
            or type(revision) is not int
            or revision < 1
            or semantic["schema"] != "algo-cli-facts-v1"
        ):
            raise ValueError("unexpected projection")
        facts = semantic["facts"]
        if not isinstance(facts, list) or any(type(fact) is not str for fact in facts):
            raise ValueError("invalid facts")
        validated = [_validate_content(fact) for fact in facts]
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise ContinuumMemoryError("Continuum Memory returned an invalid memory projection.") from exc
    return list(dict.fromkeys(validated)), revision


def recall_facts(cfg: Any) -> list[str]:
    doctor(cfg)
    validated, _ = _read_facts(cfg)
    cfg.memories = list(dict.fromkeys(validated))
    return list(cfg.memories)


def remember_fact(cfg: Any, fact: str) -> bool:
    from . import config
    from .julia_memory_runtime import _validate_content

    fact = _validate_content(fact)
    # Serialize local writers and bind the backend write to the observed revision.
    with config._exclusive_state_lock(config.CONFIG_DIR / "continuum-memory-transaction"):
        doctor(cfg)
        facts, revision = _read_facts(cfg)
        if fact in facts:
            return False
        semantic = {"schema": "algo-cli-facts-v1", "facts": [*facts, fact]}
        if len(json.dumps(semantic, ensure_ascii=False, allow_nan=False, separators=(",", ":"))) > 20_000:
            raise ContinuumMemoryError("Continuum Memory snapshot is full; no facts were discarded.")
        request_digest = hashlib.sha256(
            json.dumps([_facts_record_id(), revision, semantic], sort_keys=True).encode("utf-8")
        ).hexdigest()
        result = invoke_continuum(cfg, "remember", {
            "memory_id": _facts_record_id(),
            "data": semantic,
            "kind": "fact",
            "expected_revision": revision,
            "request_id": f"algo-facts:{request_digest}",
        }, scope="private")
        if result.get("ok") is not True:
            raise ContinuumMemoryError("Continuum Memory refused the write; no fallback was used.")
        # Never retry an uncertain append. A verified readback must match it.
        observed, observed_revision = _read_facts(cfg)
        if observed != semantic["facts"] or observed_revision != result.get("revision"):
            raise ContinuumMemoryError("Continuum Memory write could not be verified; do not automatically retry.")
        cfg.memories = observed
    return True


def _context_packet(cfg: Any, query: str, *, scope: str, budget: int) -> dict[str, Any]:
    """Retry a size-only refusal once; never omit required state to fit a packet."""
    for attempt in range(2):
        packet = invoke_continuum(cfg, "context", {
            "query": query,
            "budget_bytes": budget,
        }, scope=scope)
        if packet.get("ok") is True:
            return packet
        details = packet.get("details")
        if (
            attempt == 0 and packet.get("decision") == "REFUSE_BUDGET" and packet.get("error") == "budget"
            and type(details) is dict and type(details.get("budget_bytes")) is int
            and details["budget_bytes"] == budget and type(details.get("required_bytes")) is int
            and budget < details["required_bytes"] <= MAX_CONTEXT_BUDGET_BYTES
        ):
            # Leave room for receipt timestamps and small concurrent growth.
            budget = min(MAX_CONTEXT_BUDGET_BYTES, ((details["required_bytes"] + 2047) // 1024) * 1024)
            continue
        budget_refused = packet.get("decision") == "REFUSE_BUDGET" and packet.get("error") == "budget"
        reason = "continuum_context_budget_exceeded" if budget_refused else "continuum_context_refused"
        raise ContinuumMemoryError(
            f"Continuum {scope} context is unavailable ({reason}). "
            "Required records were not omitted; no fallback was used.", reason_code=reason,
        )
    raise AssertionError("bounded context request did not terminate")


def prompt_context(cfg: Any, query: str) -> str:
    """Retrieve both authorized scopes and retain their bounded packet metadata."""
    facts = recall_facts(cfg)
    packets = {}
    for scope, budget in (("shared", 12_000), ("private", 6_000)):
        packet = _context_packet(
            cfg, query.strip()[:1000] or "Algo CLI current goals constraints and open tasks",
            scope=scope, budget=budget,
        )
        verified = invoke_continuum(cfg, "validate_context", {"packet": packet}, scope=scope)
        if verified.get("ok") is not True:
            raise ContinuumMemoryError("Continuum context changed or could not be verified; no fallback was used.")
        packets[scope] = packet
    return json.dumps({"facts": facts, "contexts": packets}, ensure_ascii=False, allow_nan=False)


def _reconciliation_counts(cfg: Any, *, receipt_root: Path | None = None) -> dict[str, int | bool]:
    """Count content-free memory outcomes without reading protected artifacts."""

    from . import config

    root = receipt_root or (config.CONFIG_DIR / "private" / "program_runtime" / "receipts")
    effects: dict[str, bool] = {}
    skipped_effects: set[str] = set()
    legacy_unknown = 0
    legacy_skipped = 0
    malformed = 0
    if root.is_dir():
        try:
            all_paths = sorted(root.glob("*.jsonl"), key=lambda path: (path.stat().st_mtime_ns, path.name))
            if len(all_paths) > 10_000:
                malformed += len(all_paths) - 10_000
            paths = all_paths[-10_000:]
        except OSError:
            paths = []
            malformed += 1
        for path in paths:
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    malformed += 1
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                malformed += 1
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    malformed += 1
                    continue
                if (
                    type(row) is not dict
                    or row.get("mutates_state") is not True
                    or row.get("operation") not in _MEMORY_MUTATION_NAMES
                    or row.get("status") not in {"worked", "skipped", "unknown_outcome"}
                ):
                    continue
                status = str(row["status"])
                identity = str(row.get("effect_identity") or "")
                if _RECEIPT_HASH_RE.fullmatch(identity) is None:
                    if status == "unknown_outcome":
                        legacy_unknown += 1
                    elif status == "skipped":
                        legacy_skipped += 1
                    continue
                if status == "skipped":
                    skipped_effects.add(identity)
                requires = row.get("requires_reconciliation") is True or status == "unknown_outcome"
                if requires:
                    effects[identity] = True
                elif status in {"worked", "skipped"}:
                    effects[identity] = False

    ledger = getattr(cfg, "attempt_ledger", [])
    if isinstance(ledger, list):
        for row in ledger:
            if type(row) is not dict or not str(row.get("tool") or "").startswith("memory_"):
                continue
            status = str(row.get("status") or "")
            identity = str(row.get("args_receipt") or "")
            if status not in {"worked", "skipped", "unknown_outcome"} or _RECEIPT_HASH_RE.fullmatch(identity) is None:
                continue
            if status == "skipped":
                skipped_effects.add(identity)
            if status == "unknown_outcome":
                effects[identity] = True
            elif status == "worked" or (status == "skipped" and row.get("reconciled") is True):
                effects[identity] = False

    unresolved = legacy_unknown + sum(1 for required in effects.values() if required)
    return {
        "unresolved_unknown_outcomes": unresolved,
        "skipped_duplicates": legacy_skipped + len(skipped_effects),
        "reconciliation_required_operations": unresolved,
        "receipt_scan_incomplete": malformed > 0,
        "malformed_receipt_files_or_rows": malformed,
    }


def memory_status_report(cfg: Any, *, receipt_root: Path | None = None) -> dict[str, Any]:
    """Return authoritative Continuum counts plus local reconciliation state."""

    facts = recall_facts(cfg)
    status = invoke_continuum(cfg, "status", {"limit": 100}, scope="private")
    if status.get("ok") is not True:
        raise ContinuumMemoryError("Continuum Memory status is unavailable; no fallback was used.")
    counts = status.get("counts")
    namespace = status.get("namespace")
    if type(counts) is not dict or namespace != {"project": PROJECT, "owner": HARNESS, "scope": "private"}:
        raise ContinuumMemoryError("Continuum Memory returned invalid status metadata.")
    usable = counts.get("usable", 0)
    revoked = counts.get("revoked", 0)
    records = status.get("records", 0)
    if any(type(value) is not int or value < 0 for value in (usable, revoked, records)):
        raise ContinuumMemoryError("Continuum Memory returned invalid record counts.")
    return {
        "backend": "Continuum Memory",
        "harness": "algo",
        "scope": "private",
        "verified_current_records": usable,
        "verified_current_facts": len(facts),
        "total_records_including_unusable": records,
        "revoked_records": revoked,
        **_reconciliation_counts(cfg, receipt_root=receipt_root),
        "authoritative_source": "exact Continuum recall/status plus content-free local receipts",
        "counter_inference_used": False,
    }
