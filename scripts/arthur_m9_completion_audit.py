#!/usr/bin/env python3
"""Audit every hardening requirement before an Algo CLI freeze lift."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping, NoReturn


ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "hardening" / "ada-evidence-ledger.json"
REPORT_PATH = ROOT / "hardening" / "ada-m9-completion-audit.json"
AUDIT_ID = "arthur-m9-completion-v1"
EXPECTED_REQUIREMENT_IDENTITY_DIGEST = (
    "sha256:91ead279a77ec689290691f4f6c27d61e1b4ad0d3b759ffbc383939891781a04"
)
EXPECTED_CONTRACT_DIGEST = (
    "sha256:26a6b34c3a7f09df4255f922ce0f12e9da68f291e56da54a6e32a64053cacb32"
)
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_BASE_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_EXTERNAL_PASS_PREFIXES = ("hosted pass:", "production pass:", "signed pass:")
_LEDGER_FIELDS = {
    "schema_version",
    "freeze_id",
    "base_commit",
    "status",
    "policy",
    "authorized_paths",
    "milestones",
    "requirements",
}
_POLICY_FIELDS = {
    "allowed_change",
    "forbidden_change",
    "completion_rule",
    "uncertain_evidence",
}
_MILESTONE_FIELDS = {"id", "name", "status", "evidence"}
_REQUIREMENT_FIELDS = {"id", "milestone", "summary", "status", "evidence"}
_EVIDENCE_FIELDS = {
    "kind",
    "path_or_command",
    "digest",
    "result",
    "timestamp",
    "scope",
    "limitations",
}
_EVIDENCE_KINDS = {
    "artifact",
    "audit",
    "benchmark",
    "command",
    "environment",
    "qualification",
    "runtime",
    "test",
    "workflow",
}
_ITEM_STATUSES = {"pending", "in_progress", "verified", "not_verified"}

# id: (milestone, required evidence kinds, requires authoritative external execution)
REQUIREMENT_CONTRACT: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "HARD-001": ("M0", ("test",), False),
    "HARD-002": ("M0", ("test", "workflow"), False),
    "HARD-003": ("M0", ("audit",), False),
    "HARD-004": ("M0", ("command", "test"), False),
    "HARD-010": ("M1", ("test",), False),
    "HARD-011": ("M1", ("test",), False),
    "HARD-012": ("M1", ("test",), False),
    "HARD-013": ("M1", ("test",), False),
    "HARD-014": ("M1", ("command", "test"), False),
    "HARD-020": ("M2", ("command", "test"), False),
    "HARD-021": ("M2", ("audit", "test"), False),
    "HARD-022": ("M2", ("test",), False),
    "HARD-023": ("M2", ("test",), False),
    "HARD-024": ("M2", ("test",), False),
    "HARD-030": ("M3", ("test",), False),
    "HARD-031": ("M3", ("audit", "test"), False),
    "HARD-032": ("M3", ("test",), False),
    "HARD-033": ("M3", ("benchmark", "test"), False),
    "HARD-034": ("M3", ("audit", "test"), False),
    "HARD-035": ("M3", ("audit", "test"), False),
    "HARD-040": ("M4", ("audit", "test"), False),
    "HARD-041": ("M4", ("audit", "test"), False),
    "HARD-042": ("M4", ("command", "test"), False),
    "HARD-043": ("M4", ("audit", "test"), False),
    "HARD-050": ("M5", ("qualification", "workflow"), True),
    "HARD-051": ("M5", ("runtime", "test"), False),
    "HARD-052": ("M5", ("runtime", "test"), True),
    "HARD-053": ("M5", ("runtime", "test"), True),
    "HARD-054": ("M5", ("runtime", "test"), False),
    "HARD-060": ("M6", ("artifact", "qualification", "runtime"), True),
    "HARD-061": ("M6", ("runtime", "test"), True),
    "HARD-062": ("M6", ("runtime", "test"), True),
    "HARD-063": ("M6", ("runtime", "test"), True),
    "HARD-064": ("M6", ("runtime", "test"), True),
    "HARD-070": ("M7", ("runtime", "test"), True),
    "HARD-071": ("M7", ("audit", "test"), False),
    "HARD-072": ("M7", ("artifact", "audit", "workflow"), True),
    "HARD-073": ("M7", ("audit", "command"), False),
    "HARD-080": ("M8", ("benchmark", "qualification", "runtime"), True),
    "HARD-081": ("M8", ("benchmark", "runtime"), True),
    "HARD-090": ("M9", ("audit",), False),
    "HARD-091": ("M9", ("audit",), False),
}


class M9CompletionAuditRejected(RuntimeError):
    """The completion contract, ledger, or stored audit failed closed."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "m9_audit_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise M9CompletionAuditRejected(reason_code)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        _reject("m9_audit_json")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _plain_text(value: object, *, maximum: int, allow_empty: bool = False) -> bool:
    if type(value) is not str or not value.isascii() or _CONTROL_RE.search(value):
        return False
    size = len(value.encode("ascii"))
    return (0 if allow_empty else 1) <= size <= maximum


def _canonical_timestamp(value: object) -> bool:
    if type(value) is not str or _UTC_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _validate_ledger_envelope(ledger: Mapping[str, Any]) -> None:
    if type(ledger) is not dict or set(ledger) != _LEDGER_FIELDS:
        _reject("m9_audit_ledger_schema")
    if type(ledger.get("schema_version")) is not int or ledger["schema_version"] != 1:
        _reject("m9_audit_ledger_schema")
    if not _plain_text(ledger.get("freeze_id"), maximum=128):
        _reject("m9_audit_ledger_schema")
    base_commit = ledger.get("base_commit")
    if type(base_commit) is not str or _BASE_COMMIT_RE.fullmatch(base_commit) is None:
        _reject("m9_audit_ledger_schema")
    policy = ledger.get("policy")
    if type(policy) is not dict or set(policy) != _POLICY_FIELDS:
        _reject("m9_audit_ledger_schema")
    if any(not _plain_text(policy.get(field), maximum=4096) for field in _POLICY_FIELDS):
        _reject("m9_audit_ledger_schema")
    if policy.get("uncertain_evidence") != "not_verified":
        _reject("m9_audit_ledger_schema")
    authorized_paths = ledger.get("authorized_paths")
    if (
        type(authorized_paths) is not list
        or not authorized_paths
        or any(not _plain_text(path, maximum=512) for path in authorized_paths)
        or len(set(authorized_paths)) != len(authorized_paths)
    ):
        _reject("m9_audit_ledger_schema")


def _safe_document(path: Path) -> dict[str, Any]:
    candidate = path if path.is_absolute() else ROOT / path
    try:
        hardening_root = (ROOT / "hardening").resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(hardening_root)
        before = candidate.lstat()
    except (OSError, RuntimeError, ValueError):
        _reject("m9_audit_path")
    if (
        resolved != candidate.absolute()
        or not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= MAX_DOCUMENT_BYTES
    ):
        _reject("m9_audit_path")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
        ):
            _reject("m9_audit_path")
        payload = bytearray()
        while len(payload) < opened.st_size:
            chunk = os.read(descriptor, min(64 * 1024, opened.st_size - len(payload)))
            if not chunk:
                _reject("m9_audit_path")
            payload.extend(chunk)
        if os.read(descriptor, 1):
            _reject("m9_audit_path")
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            _reject("m9_audit_path")
    except OSError:
        _reject("m9_audit_path")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _reject("m9_audit_json")
    if type(value) is not dict:
        _reject("m9_audit_json")
    return value


def _contract_projection() -> dict[str, Any]:
    return {
        "expected_requirement_identity_digest": EXPECTED_REQUIREMENT_IDENTITY_DIGEST,
        "requirements": [
            {
                "id": requirement_id,
                "milestone": milestone,
                "required_evidence_kinds": list(required_kinds),
                "requires_external_execution": external,
            }
            for requirement_id, (milestone, required_kinds, external) in sorted(
                REQUIREMENT_CONTRACT.items()
            )
        ],
    }


def contract_digest() -> str:
    observed = _digest(_contract_projection())
    if observed != EXPECTED_CONTRACT_DIGEST:
        _reject("m9_audit_contract_identity")
    return observed


def _requirement_identity_digest(requirements: list[Any]) -> str:
    projection: list[dict[str, Any]] = []
    for value in requirements:
        if type(value) is not dict:
            _reject("m9_audit_requirement_schema")
        if not all(type(value.get(key)) is str for key in ("id", "milestone", "summary")):
            _reject("m9_audit_requirement_schema")
        projection.append(
            {
                "id": value["id"],
                "milestone": value["milestone"],
                "summary": value["summary"],
            }
        )
    projection.sort(key=lambda row: row["id"])
    return _digest(projection)


def _evidence_rows(value: object) -> list[Mapping[str, Any]]:
    if type(value) is not list:
        _reject("m9_audit_evidence_schema")
    rows: list[Mapping[str, Any]] = []
    for item in value:
        if type(item) is not dict or set(item) != _EVIDENCE_FIELDS:
            _reject("m9_audit_evidence_schema")
        kind = item.get("kind")
        timestamp = item.get("timestamp")
        digest = item.get("digest")
        if (
            kind not in _EVIDENCE_KINDS
            or not _canonical_timestamp(timestamp)
            or type(digest) is not str
            or (digest and _DIGEST_RE.fullmatch(digest) is None)
            or any(
                not _plain_text(item.get(field), maximum=4096)
                for field in ("path_or_command", "result", "scope", "limitations")
            )
        ):
            _reject("m9_audit_evidence_schema")
        rows.append(item)
    return rows


def _external_authoritative_evidence(
    evidence: list[Mapping[str, Any]],
    required_kinds: tuple[str, ...],
) -> bool:
    return any(
        item["kind"] in required_kinds
        and bool(item["digest"])
        and str(item["result"]).casefold().startswith(_EXTERNAL_PASS_PREFIXES)
        for item in evidence
    )


def audit_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    _validate_ledger_envelope(ledger)
    requirements_value = ledger.get("requirements")
    milestones_value = ledger.get("milestones")
    if type(requirements_value) is not list or type(milestones_value) is not list:
        _reject("m9_audit_ledger_schema")
    if _requirement_identity_digest(requirements_value) != EXPECTED_REQUIREMENT_IDENTITY_DIGEST:
        _reject("m9_audit_requirement_identity")
    if len(requirements_value) != len(REQUIREMENT_CONTRACT):
        _reject("m9_audit_requirement_identity")

    requirements_by_id: dict[str, Mapping[str, Any]] = {}
    for value in requirements_value:
        row = value
        if type(row) is not dict or set(row) != _REQUIREMENT_FIELDS:
            _reject("m9_audit_requirement_schema")
        requirement_id = row.get("id")
        if (
            type(requirement_id) is not str
            or requirement_id in requirements_by_id
            or requirement_id not in REQUIREMENT_CONTRACT
        ):
            _reject("m9_audit_requirement_identity")
        requirements_by_id[requirement_id] = row

    milestone_rows: list[dict[str, str]] = []
    milestone_statuses: dict[str, str] = {}
    for value in milestones_value:
        if type(value) is not dict or set(value) != _MILESTONE_FIELDS:
            _reject("m9_audit_milestone_schema")
        milestone_id = value.get("id")
        milestone_status = value.get("status")
        name = value.get("name")
        milestone_evidence = value.get("evidence")
        if (
            type(milestone_id) is not str
            or type(milestone_status) is not str
            or milestone_status not in _ITEM_STATUSES
            or not _plain_text(name, maximum=256)
            or type(milestone_evidence) is not list
            or any(not _plain_text(path, maximum=512) for path in milestone_evidence)
            or len(set(milestone_evidence)) != len(milestone_evidence)
            or (milestone_status == "verified" and not milestone_evidence)
            or milestone_id in milestone_statuses
        ):
            _reject("m9_audit_milestone_schema")
        milestone_statuses[milestone_id] = milestone_status
        milestone_rows.append({"id": milestone_id, "ledger_status": milestone_status})
    if set(milestone_statuses) != {f"M{index}" for index in range(10)}:
        _reject("m9_audit_milestone_schema")
    milestone_rows.sort(key=lambda row: int(row["id"][1:]))

    audit_rows: list[dict[str, Any]] = []
    latest_timestamps: list[str] = []
    for requirement_id, (milestone, required_kinds, external) in sorted(
        REQUIREMENT_CONTRACT.items()
    ):
        row = requirements_by_id[requirement_id]
        if (
            row.get("milestone") != milestone
            or row.get("status") not in _ITEM_STATUSES
            or not _plain_text(row.get("summary"), maximum=1024)
        ):
            _reject("m9_audit_requirement_schema")
        evidence = _evidence_rows(row.get("evidence"))
        observed_kinds = sorted({str(item["kind"]) for item in evidence})
        missing_kinds = sorted(set(required_kinds) - set(observed_kinds))
        timestamps = sorted(str(item["timestamp"]) for item in evidence)
        latest_timestamp = timestamps[-1] if timestamps else ""
        latest_timestamps.extend(timestamps)
        external_authoritative = (
            _external_authoritative_evidence(evidence, required_kinds)
            if external
            else True
        )
        requirement_ledger_status = str(row["status"])
        if requirement_ledger_status == "verified" and (
            missing_kinds or not external_authoritative
        ):
            audit_status = "failed"
        elif requirement_ledger_status == "verified":
            audit_status = "verified"
        else:
            audit_status = "blocked"
        audit_rows.append(
            {
                "audit_status": audit_status,
                "evidence_count": len(evidence),
                "external_authoritative_evidence": external_authoritative,
                "id": requirement_id,
                "latest_timestamp": latest_timestamp,
                "ledger_status": requirement_ledger_status,
                "milestone": milestone,
                "missing_evidence_kinds": missing_kinds,
                "observed_evidence_kinds": observed_kinds,
                "required_evidence_kinds": list(required_kinds),
                "requires_external_execution": external,
            }
        )

    counts = {
        status: sum(row["audit_status"] == status for row in audit_rows)
        for status in ("blocked", "failed", "verified")
    }
    ledger_status = ledger.get("status")
    if ledger_status not in {"active", "lifted"}:
        _reject("m9_audit_ledger_status")
    for milestone_id, milestone_status in milestone_statuses.items():
        requirement_statuses = {
            row["ledger_status"]
            for row in audit_rows
            if row["milestone"] == milestone_id
        }
        if milestone_status == "verified" and requirement_statuses != {"verified"}:
            _reject("m9_audit_milestone_consistency")
        if milestone_status == "pending" and requirement_statuses != {"pending"}:
            _reject("m9_audit_milestone_consistency")
    all_verified = counts == {"blocked": 0, "failed": 0, "verified": len(audit_rows)}
    m0_m8_verified = all(
        milestone_statuses[f"M{index}"] == "verified" for index in range(9)
    )
    hard_091 = requirements_by_id["HARD-091"]
    ready_for_lift = (
        counts["failed"] == 0
        and all(row["audit_status"] == "verified" for row in audit_rows if row["id"] != "HARD-091")
        and hard_091.get("status") == "pending"
        and m0_m8_verified
        and milestone_statuses["M9"] == "in_progress"
        and ledger_status == "active"
    )
    if counts["failed"]:
        status = "failed"
    elif all_verified and all(value == "verified" for value in milestone_statuses.values()):
        status = "passed" if ledger_status == "lifted" else "failed"
    elif ready_for_lift:
        status = "ready_for_lift"
    else:
        status = "blocked"
    return {
        "audit_id": AUDIT_ID,
        "audited_at": max(latest_timestamps) if latest_timestamps else "",
        "base_commit": ledger.get("base_commit"),
        "contract_digest": contract_digest(),
        "freeze_id": ledger.get("freeze_id"),
        "ledger_digest": _digest(ledger),
        "ledger_status": ledger_status,
        "limitations": (
            "This deterministic audit proves contract and evidence completeness only. "
            "It does not independently authenticate external services, runner images, "
            "signing identities, TCC state, browser containment, or benchmark truth."
        ),
        "milestones": milestone_rows,
        "public_claim_eligible": False,
        "requirements": audit_rows,
        "schema_version": 1,
        "status": status,
        "summary": {
            "blocked": counts["blocked"],
            "failed": counts["failed"],
            "total": len(audit_rows),
            "verified": counts["verified"],
        },
    }


def current_report() -> dict[str, Any]:
    return audit_ledger(_safe_document(LEDGER_PATH))


def write_current_report(report_path: Path = REPORT_PATH) -> dict[str, Any]:
    if not isinstance(report_path, Path) or not report_path.is_absolute():
        _reject("m9_audit_report_path")
    parent = report_path.parent.resolve()
    if report_path.exists() or report_path.is_symlink():
        information = report_path.lstat()
        if (
            report_path.is_symlink()
            or not stat.S_ISREG(information.st_mode)
            or information.st_nlink != 1
        ):
            _reject("m9_audit_report_identity")
    report = current_report()
    payload = (json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    if not 1 <= len(payload) <= MAX_DOCUMENT_BYTES:
        _reject("m9_audit_report_bounds")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{report_path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, report_path)
        directory_descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return report


def verify_stored_report(report_path: Path = REPORT_PATH) -> dict[str, Any]:
    expected = current_report()
    observed = _safe_document(report_path)
    if _canonical(observed) != _canonical(expected):
        _reject("m9_audit_report_stale")
    return expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-report", type=Path)
    parser.add_argument("--write-report", action="store_true")
    expectation = parser.add_mutually_exclusive_group()
    expectation.add_argument("--expect-blocked", action="store_true")
    expectation.add_argument("--require-ready-for-lift", action="store_true")
    expectation.add_argument("--require-complete", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.write_report and arguments.verify_report is not None:
        parser.error("--write-report and --verify-report are mutually exclusive")
    try:
        report = (
            verify_stored_report(arguments.verify_report)
            if arguments.verify_report is not None
            else write_current_report(REPORT_PATH)
            if arguments.write_report
            else current_report()
        )
    except M9CompletionAuditRejected as error:
        if not arguments.quiet:
            print(
                json.dumps(
                    {"reason_code": error.reason_code, "status": "blocked"},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return 2
    if not arguments.quiet:
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    expected_status = (
        "blocked"
        if arguments.expect_blocked
        else "ready_for_lift"
        if arguments.require_ready_for_lift
        else "passed"
        if arguments.require_complete
        else None
    )
    if expected_status is not None:
        return 0 if report["status"] == expected_status else 3
    return 0 if report["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
