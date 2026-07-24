from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="M9 completion evidence validates POSIX private atomic writes",
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "arthur_m9_completion_audit.py"
SPEC = importlib.util.spec_from_file_location(
    "arthur_m9_completion_audit_script",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)


def _ledger() -> dict[str, Any]:
    return json.loads((ROOT / "hardening" / "ada-evidence-ledger.json").read_text())


def _requirement(ledger: dict[str, Any], requirement_id: str) -> dict[str, Any]:
    return next(row for row in ledger["requirements"] if row["id"] == requirement_id)


def _evidence(kind: str, *, external: bool = False) -> dict[str, str]:
    return {
        "kind": kind,
        "path_or_command": f"synthetic:{kind}",
        "digest": "sha256:" + "a" * 64,
        "result": (
            "hosted pass: synthetic external fixture"
            if external
            else "pass: synthetic local fixture"
        ),
        "timestamp": "2026-07-20T07:00:00Z",
        "scope": "completion-auditor unit test",
        "limitations": "synthetic evidence is never production evidence",
    }


def _completion_ledger(*, lifted: bool) -> dict[str, Any]:
    ledger = _ledger()
    for row in ledger["requirements"]:
        requirement_id = row["id"]
        if requirement_id == "HARD-091" and not lifted:
            row["status"] = "pending"
            row["evidence"] = []
            continue
        _milestone, kinds, external = SCRIPT.REQUIREMENT_CONTRACT[requirement_id]
        row["status"] = "verified"
        row["evidence"] = [
            _evidence(kind, external=external and index == 0)
            for index, kind in enumerate(kinds)
        ]
    for milestone in ledger["milestones"]:
        if milestone["id"] == "M9":
            milestone["status"] = "verified" if lifted else "in_progress"
            milestone["evidence"] = ["hardening/ada-m9-completion-audit.json"]
        else:
            milestone["status"] = "verified"
    ledger["status"] = "lifted" if lifted else "active"
    return ledger


def test_current_ledger_is_honestly_blocked() -> None:
    report = SCRIPT.audit_ledger(_ledger())

    assert report["status"] == "blocked"
    assert report["summary"] == {
        "blocked": 14,
        "failed": 0,
        "total": 42,
        "verified": 28,
    }
    assert report["public_claim_eligible"] is False
    assert report["contract_digest"] == SCRIPT.EXPECTED_CONTRACT_DIGEST


def test_requirement_and_contract_identity_are_pinned() -> None:
    changed = _ledger()
    _requirement(changed, "HARD-001")["summary"] += " weakened"
    with pytest.raises(
        SCRIPT.M9CompletionAuditRejected,
        match="m9_audit_requirement_identity",
    ):
        SCRIPT.audit_ledger(changed)

    original = SCRIPT.REQUIREMENT_CONTRACT["HARD-001"]
    SCRIPT.REQUIREMENT_CONTRACT["HARD-001"] = ("M0", ("runtime",), False)
    try:
        with pytest.raises(
            SCRIPT.M9CompletionAuditRejected,
            match="m9_audit_contract_identity",
        ):
            SCRIPT.contract_digest()
    finally:
        SCRIPT.REQUIREMENT_CONTRACT["HARD-001"] = original


def test_verified_requirement_missing_required_evidence_fails() -> None:
    ledger = _ledger()
    row = _requirement(ledger, "HARD-001")
    row["status"] = "verified"
    row["evidence"] = [_evidence("runtime")]

    report = SCRIPT.audit_ledger(ledger)
    audited = next(item for item in report["requirements"] if item["id"] == "HARD-001")
    assert report["status"] == "failed"
    assert audited["audit_status"] == "failed"
    assert audited["missing_evidence_kinds"] == ["test"]


def test_external_requirement_needs_digest_bound_authoritative_result() -> None:
    ledger = _ledger()
    row = _requirement(ledger, "HARD-050")
    row["status"] = "verified"
    row["evidence"] = []
    for kind in SCRIPT.REQUIREMENT_CONTRACT["HARD-050"][1]:
        evidence = _evidence(kind)
        evidence["digest"] = ""
        row["evidence"].append(evidence)

    report = SCRIPT.audit_ledger(ledger)
    audited = next(item for item in report["requirements"] if item["id"] == "HARD-050")
    assert report["status"] == "failed"
    assert audited["audit_status"] == "failed"
    assert audited["missing_evidence_kinds"] == []
    assert audited["external_authoritative_evidence"] is False


def test_invalid_schema_status_and_calendar_timestamp_reject() -> None:
    extra = _ledger()
    extra["smuggled"] = True
    with pytest.raises(SCRIPT.M9CompletionAuditRejected, match="m9_audit_ledger_schema"):
        SCRIPT.audit_ledger(extra)

    invalid_status = _ledger()
    _requirement(invalid_status, "HARD-050")["status"] = "probably"
    with pytest.raises(
        SCRIPT.M9CompletionAuditRejected,
        match="m9_audit_requirement_schema",
    ):
        SCRIPT.audit_ledger(invalid_status)

    invalid_time = _ledger()
    _requirement(invalid_time, "HARD-001")["evidence"][0]["timestamp"] = (
        "2026-02-30T00:00:00Z"
    )
    with pytest.raises(
        SCRIPT.M9CompletionAuditRejected,
        match="m9_audit_evidence_schema",
    ):
        SCRIPT.audit_ledger(invalid_time)


def test_ready_for_lift_requires_every_pre_lift_condition() -> None:
    report = SCRIPT.audit_ledger(_completion_ledger(lifted=False))

    assert report["status"] == "ready_for_lift"
    assert report["summary"] == {
        "blocked": 1,
        "failed": 0,
        "total": 42,
        "verified": 41,
    }
    assert report["ledger_status"] == "active"


def test_complete_requires_explicit_lifted_ledger_and_m9() -> None:
    report = SCRIPT.audit_ledger(_completion_ledger(lifted=True))

    assert report["status"] == "passed"
    assert report["summary"] == {
        "blocked": 0,
        "failed": 0,
        "total": 42,
        "verified": 42,
    }
    assert report["ledger_status"] == "lifted"

    not_lifted = _completion_ledger(lifted=True)
    not_lifted["status"] = "active"
    assert SCRIPT.audit_ledger(not_lifted)["status"] == "failed"


def test_stored_report_verification_fails_closed_when_ledger_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger_path = hardening / "ada-evidence-ledger.json"
    report_path = hardening / "ada-m9-completion-audit.json"
    ledger = _ledger()
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    report_path.write_text(json.dumps(SCRIPT.audit_ledger(ledger)), encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "LEDGER_PATH", ledger_path)

    assert SCRIPT.verify_stored_report(report_path)["status"] == "blocked"

    ledger["authorized_paths"].append("tests/synthetic_authorized_path.py")
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(
        SCRIPT.M9CompletionAuditRejected,
        match="m9_audit_report_stale",
    ):
        SCRIPT.verify_stored_report(report_path)


def test_repository_report_is_exact_and_freeze_workflow_enforces_it() -> None:
    assert SCRIPT.verify_stored_report()["status"] == "blocked"
    workflow = (ROOT / ".github" / "workflows" / "henry-hardening-freeze.yml").read_text()
    invocation = (
        "python scripts/arthur_m9_completion_audit.py "
        "--verify-report hardening/ada-m9-completion-audit.json "
        "--expect-blocked --quiet"
    )
    assert invocation in " ".join(workflow.split())


def test_report_writer_is_atomic_private_and_rejects_symlink_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = SCRIPT.audit_ledger(_ledger())
    monkeypatch.setattr(SCRIPT, "current_report", lambda: deepcopy(report))
    output = (tmp_path / "ada-m9-completion-audit.json").resolve()

    assert SCRIPT.write_current_report(output) == report
    assert json.loads(output.read_text(encoding="ascii")) == report
    assert output.stat().st_mode & 0o777 == 0o600

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="ascii")
    output.unlink()
    output.symlink_to(target)
    with pytest.raises(SCRIPT.M9CompletionAuditRejected, match="m9_audit_report_identity"):
        SCRIPT.write_current_report(output)


def test_cli_can_regenerate_only_the_fixed_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = SCRIPT.audit_ledger(_ledger())
    output = (tmp_path / "ada-m9-completion-audit.json").resolve()
    monkeypatch.setattr(SCRIPT, "current_report", lambda: deepcopy(report))
    monkeypatch.setattr(SCRIPT, "REPORT_PATH", output)

    assert SCRIPT.main(["--write-report", "--quiet"]) == 3
    assert json.loads(output.read_text(encoding="ascii")) == report
    with pytest.raises(SystemExit):
        SCRIPT.main(["--write-report", "--verify-report", str(output), "--quiet"])


def test_cli_expectations_are_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = SCRIPT.audit_ledger(_ledger())
    monkeypatch.setattr(SCRIPT, "current_report", lambda: deepcopy(blocked))

    assert SCRIPT.main(["--expect-blocked", "--quiet"]) == 0
    assert SCRIPT.main(["--require-ready-for-lift", "--quiet"]) == 3
    assert SCRIPT.main(["--require-complete", "--quiet"]) == 3
    assert SCRIPT.main(["--quiet"]) == 3
