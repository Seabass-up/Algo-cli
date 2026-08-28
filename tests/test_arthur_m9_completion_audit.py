from __future__ import annotations

from copy import deepcopy
import hashlib
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
        "result": ("hosted pass: synthetic external fixture" if external else "pass: synthetic local fixture"),
        "timestamp": "2026-07-20T07:00:00Z",
        "scope": "completion-auditor unit test",
        "limitations": "synthetic evidence is never production evidence",
    }


def _write_json(path: Path, value: dict[str, Any]) -> str:
    payload = (json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")
    path.write_bytes(payload)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _currency_fixture(
    hardening: Path,
    ledger: dict[str, Any],
) -> tuple[Path, Path]:
    artifacts = (
        "hardening/grace-m8-local-qualification.json",
        "hardening/nathan-agent-runtime-qualification.json",
    )
    for requirement_id in ("HARD-080", "HARD-081"):
        requirement = _requirement(ledger, requirement_id)
        requirement["evidence"] = [
            row
            for row in requirement["evidence"]
            if not any(SCRIPT._mentions_artifact(row.get("path_or_command"), artifact) for artifact in artifacts)
        ]
    m8_path = hardening / "grace-m8-local-qualification.json"
    nathan_path = hardening / "nathan-agent-runtime-qualification.json"
    m8_digest = _write_json(
        m8_path,
        {
            "qualification": "henry-m8-local-v1",
            "public_claim_eligible": False,
            "schema_version": 1,
            "status": "blocked",
        },
    )
    nathan_digest = _write_json(
        nathan_path,
        {
            "benchmark": "nathan-agent-runtime-hardening-v3",
            "public_claim_eligible": False,
            "schema_version": 3,
            "status": "pass",
        },
    )
    m8_evidence = _evidence("qualification")
    m8_evidence.update(
        {
            "digest": m8_digest,
            "path_or_command": "hardening/grace-m8-local-qualification.json",
            "result": "local pass / externally blocked; public_claim_eligible is false",
            "timestamp": "2026-08-10T00:00:00Z",
        }
    )
    nathan_evidence = _evidence("benchmark")
    nathan_evidence.update(
        {
            "digest": nathan_digest,
            "path_or_command": "hardening/nathan-agent-runtime-qualification.json",
            "result": "current source-bound local pass",
            "timestamp": "2026-08-10T00:00:01Z",
            "limitations": "the active freeze forbids public benchmark claims",
        }
    )
    m8_efficiency_evidence = deepcopy(m8_evidence)
    m8_efficiency_evidence.update(
        {
            "kind": "benchmark",
            "result": "current local measurements remain externally blocked; public_claim_eligible is false",
            "timestamp": "2026-08-10T00:00:02Z",
        }
    )
    _requirement(ledger, "HARD-080")["evidence"].append(m8_evidence)
    _requirement(ledger, "HARD-081")["evidence"].extend([nathan_evidence, m8_efficiency_evidence])
    return m8_path, nathan_path


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
        row["evidence"] = [_evidence(kind, external=external) for kind in kinds]
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
        "blocked": 13,
        "failed": 0,
        "total": 42,
        "verified": 29,
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


def test_external_requirement_needs_authoritative_evidence_for_every_kind() -> None:
    ledger = _ledger()
    row = _requirement(ledger, "HARD-050")
    row["status"] = "verified"
    row["evidence"].append(_evidence("qualification", external=True))

    report = SCRIPT.audit_ledger(ledger)
    audited = next(item for item in report["requirements"] if item["id"] == "HARD-050")
    assert audited["audit_status"] == "failed"
    assert audited["missing_evidence_kinds"] == []
    assert audited["external_authoritative_evidence"] is False

    row["evidence"].append(_evidence("workflow", external=True))
    report = SCRIPT.audit_ledger(ledger)
    audited = next(item for item in report["requirements"] if item["id"] == "HARD-050")
    assert audited["audit_status"] == "verified"
    assert audited["external_authoritative_evidence"] is True


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
    _requirement(invalid_time, "HARD-001")["evidence"][0]["timestamp"] = "2026-02-30T00:00:00Z"
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


def test_local_artifact_currency_binds_raw_digest_status_and_public_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger = _ledger()
    m8_path, nathan_path = _currency_fixture(hardening, ledger)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)

    currency = SCRIPT.audit_local_artifact_currency(
        ledger,
        m8_artifact_path=m8_path,
        nathan_artifact_path=nathan_path,
    )

    assert currency["m8"]["status"] == "blocked"
    assert currency["nathan"]["status"] == "pass"
    assert currency["m8_efficiency"]["status"] == "blocked"
    assert currency["m8"]["raw_sha256"] == currency["m8_efficiency"]["raw_sha256"]
    assert all(row["public_claim_eligible"] is False for row in currency.values())


def test_local_artifact_currency_snapshots_each_unique_artifact_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger = _ledger()
    m8_path, nathan_path = _currency_fixture(hardening, ledger)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    original = SCRIPT._safe_document_with_digest
    reads: list[Path] = []

    def counted(path: Path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(SCRIPT, "_safe_document_with_digest", counted)

    currency = SCRIPT.audit_local_artifact_currency(
        ledger,
        m8_artifact_path=m8_path,
        nathan_artifact_path=nathan_path,
    )

    assert reads == [m8_path, nathan_path]
    assert currency["m8"]["raw_sha256"] == currency["m8_efficiency"]["raw_sha256"]
    assert currency["m8"]["raw_sha256"].startswith("sha256:")
    assert currency["nathan"]["raw_sha256"].startswith("sha256:")


@pytest.mark.parametrize(
    ("artifact", "field", "value", "reason"),
    [
        ("m8", "status", "pass", "m9_audit_local_status"),
        ("nathan", "status", "blocked", "m9_audit_local_status"),
        ("m8", "public_claim_eligible", True, "m9_audit_local_public_claim"),
        ("nathan", "public_claim_eligible", True, "m9_audit_local_public_claim"),
        ("nathan", "schema_version", 2, "m9_audit_local_schema"),
    ],
)
def test_local_artifact_currency_rejects_tampered_contract_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
    field: str,
    value: object,
    reason: str,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger = _ledger()
    m8_path, nathan_path = _currency_fixture(hardening, ledger)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    path = m8_path if artifact == "m8" else nathan_path
    document = json.loads(path.read_text(encoding="ascii"))
    document[field] = value
    _write_json(path, document)

    with pytest.raises(SCRIPT.M9CompletionAuditRejected, match=reason):
        SCRIPT.audit_local_artifact_currency(
            ledger,
            m8_artifact_path=m8_path,
            nathan_artifact_path=nathan_path,
        )


def test_local_artifact_currency_rejects_missing_public_claim_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger = _ledger()
    m8_path, nathan_path = _currency_fixture(hardening, ledger)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    document = json.loads(nathan_path.read_text(encoding="ascii"))
    del document["public_claim_eligible"]
    _write_json(nathan_path, document)

    with pytest.raises(
        SCRIPT.M9CompletionAuditRejected,
        match="m9_audit_local_public_claim",
    ):
        SCRIPT.audit_local_artifact_currency(
            ledger,
            m8_artifact_path=m8_path,
            nathan_artifact_path=nathan_path,
        )


def test_local_artifact_currency_rejects_stale_or_ambiguous_latest_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger = _ledger()
    m8_path, nathan_path = _currency_fixture(hardening, ledger)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    nathan = _requirement(ledger, "HARD-081")
    latest = deepcopy(nathan["evidence"][-2])
    latest["timestamp"] = "2026-08-10T00:00:03Z"
    latest["digest"] = "sha256:" + "0" * 64
    nathan["evidence"].append(latest)

    with pytest.raises(SCRIPT.M9CompletionAuditRejected, match="m9_audit_local_digest"):
        SCRIPT.audit_local_artifact_currency(
            ledger,
            m8_artifact_path=m8_path,
            nathan_artifact_path=nathan_path,
        )

    latest["digest"] = currency_digest = "sha256:" + hashlib.sha256(nathan_path.read_bytes()).hexdigest()
    duplicate = deepcopy(latest)
    duplicate["digest"] = currency_digest
    nathan["evidence"].append(duplicate)
    with pytest.raises(SCRIPT.M9CompletionAuditRejected, match="m9_audit_local_evidence"):
        SCRIPT.audit_local_artifact_currency(
            ledger,
            m8_artifact_path=m8_path,
            nathan_artifact_path=nathan_path,
        )


def test_local_artifact_currency_ignores_later_unrelated_path_mentions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger = _ledger()
    m8_path, nathan_path = _currency_fixture(hardening, ledger)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    unrelated = _evidence("test")
    unrelated.update(
        {
            "path_or_command": "delete hardening/grace-m8-local-qualification.json",
            "timestamp": "2026-08-10T00:00:03Z",
            "digest": "sha256:" + "0" * 64,
            "result": "pass: deletion authorization only",
        }
    )
    _requirement(ledger, "HARD-002")["evidence"].append(unrelated)

    currency = SCRIPT.audit_local_artifact_currency(
        ledger,
        m8_artifact_path=m8_path,
        nathan_artifact_path=nathan_path,
    )

    assert currency["m8"]["ledger_requirement"] == "HARD-080"


def test_local_artifact_currency_rejects_ledger_overclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger = _ledger()
    m8_path, nathan_path = _currency_fixture(hardening, ledger)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    latest = _requirement(ledger, "HARD-081")["evidence"][-2]
    latest["limitations"] = "all public claims are authorized"

    with pytest.raises(
        SCRIPT.M9CompletionAuditRejected,
        match="m9_audit_local_public_claim",
    ):
        SCRIPT.audit_local_artifact_currency(
            ledger,
            m8_artifact_path=m8_path,
            nathan_artifact_path=nathan_path,
        )


def test_local_artifact_currency_rejects_latest_evidence_kind_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger = _ledger()
    m8_path, nathan_path = _currency_fixture(hardening, ledger)
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    latest = _requirement(ledger, "HARD-081")["evidence"][-2]
    latest["kind"] = "qualification"

    with pytest.raises(
        SCRIPT.M9CompletionAuditRejected,
        match="m9_audit_local_evidence",
    ):
        SCRIPT.audit_local_artifact_currency(
            ledger,
            m8_artifact_path=m8_path,
            nathan_artifact_path=nathan_path,
        )


def test_stored_report_verification_fails_closed_when_ledger_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hardening = tmp_path / "hardening"
    hardening.mkdir()
    ledger_path = hardening / "ada-evidence-ledger.json"
    report_path = hardening / "ada-m9-completion-audit.json"
    ledger = _ledger()
    m8_path, nathan_path = _currency_fixture(hardening, ledger)
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "LEDGER_PATH", ledger_path)
    monkeypatch.setattr(SCRIPT, "M8_ARTIFACT_PATH", m8_path)
    monkeypatch.setattr(SCRIPT, "NATHAN_ARTIFACT_PATH", nathan_path)
    report_path.write_text(json.dumps(SCRIPT.current_report()), encoding="utf-8")

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
