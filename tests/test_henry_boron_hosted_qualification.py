from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Boron hosted qualification is bound to Linux container evidence",
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "henry_boron_hosted_qualification.py"
SCRIPTS = str(SCRIPT_PATH.parent)
sys.path.insert(0, SCRIPTS)
try:
    SPEC = importlib.util.spec_from_file_location(
        "henry_boron_hosted_qualification_script",
        SCRIPT_PATH,
    )
    assert SPEC is not None and SPEC.loader is not None
    SCRIPT = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = SCRIPT
    SPEC.loader.exec_module(SCRIPT)
finally:
    sys.path.remove(SCRIPTS)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _environment(**changes: str) -> dict[str, str]:
    value = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REPOSITORY_ID": "123456789",
        "GITHUB_RUN_ID": "987654321",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_WORKFLOW_REF": "algo-cli/algo-cli/.github/workflows/oliver-ci.yml@refs/pull/1/merge",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
    }
    value.update(changes)
    return value


def _build_evidence() -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform": "linux/amd64",
        "browser_tag": "algo-cli/boron-browser:m5-local",
        "browser_image_id": _digest("1"),
        "browser_code_digest": _digest("2"),
        "browser_version": "150.0.7871.128",
        "browser_security_update_lag_ms": 0,
        "browser_security_max_update_lag_ms": 72 * 60 * 60 * 1000,
        "browser_security_latest_version": "150.0.7871.128",
        "browser_security_latest_release_at_ms": 1_784_235_227_785,
        "browser_security_evidence_observed_at_ms": 1_784_235_228_000,
        "browser_security_source": "google_version_history",
        "browser_security_source_digest": _digest("3"),
        "native_browser_built": False,
        "native_browser_fresh": False,
        "native_browser_freshness_reason": "upstream_patch_equivalence_unverified",
        "broker_tag": "algo-cli/xenon-broker:m5-local",
        "broker_image_id": _digest("4"),
        "broker_code_digest": _digest("5"),
        "cryptography_version": "49.0.0",
        "non_root_defaults": True,
    }


def _live_evidence(serial: int) -> dict[str, object]:
    assert 1 <= serial <= 9
    return {
        "schema_version": 1,
        "platform": "linux/amd64",
        "browser_image_digest": _digest("1"),
        "broker_image_digest": _digest("4"),
        "broker_binary_digest": _digest("5"),
        "topology_evidence_digest": _digest(str(serial)),
        "internal_participant_count": 2,
        "browser_state": "verified",
        "browser_major": 150,
        "browser_security_update_lag_ms": 0,
        "browser_security_source_digest": _digest("3"),
        "browser_command_count": 4,
        "browser_event_count": 7,
        "broker_disposition": "verified",
        "broker_connection_count": 2,
        "broker_request_count": 1,
        "broker_redirect_count": 0,
        "broker_bytes_to_browser": 1024,
        "target_decision_digest": _digest("8"),
        "ca_certificate_digest": _digest("9"),
        "browser_stderr": {"byte_count": 0, "digest": _digest("a")},
        "broker_stderr": {"byte_count": 10, "digest": _digest("b")},
    }


def test_hosted_context_requires_github_native_amd64_and_bounded_identity() -> None:
    context = SCRIPT.HostedRunnerContext.from_environment(_environment())
    assert context.native_platform == "linux/amd64"
    assert context.runner_arch == "X64"
    assert context.workflow_ref_digest.startswith("sha256:")

    for environment, reason in (
        ({}, "hosted_environment"),
        (_environment(RUNNER_ARCH="ARM64"), "hosted_native_amd64_required"),
        (_environment(RUNNER_OS="macOS"), "hosted_native_amd64_required"),
        (_environment(GITHUB_SHA="main"), "hosted_revision"),
        (_environment(GITHUB_RUN_ID="0"), "hosted_run_id"),
        (_environment(GITHUB_WORKFLOW_REF="bad\nref"), "hosted_workflow_ref"),
    ):
        with pytest.raises(SCRIPT.HostedQualificationRejected, match=reason):
            SCRIPT.HostedRunnerContext.from_environment(environment)


def test_repeated_runner_builds_once_and_retains_honest_denominators() -> None:
    builds = 0
    sessions = 0
    ticks = iter(range(0, 12_000_000, 1_000_000))

    def build() -> dict[str, object]:
        nonlocal builds
        builds += 1
        return _build_evidence()

    def session(*, build_evidence) -> dict[str, object]:
        nonlocal sessions
        assert build_evidence == _build_evidence()
        sessions += 1
        return _live_evidence(sessions)

    report = SCRIPT.run_hosted_qualification(
        environment=_environment(),
        repetitions=5,
        build=build,
        session=session,
        monotonic_ns=lambda: next(ticks),
        now=lambda: datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
    )

    assert builds == 1
    assert sessions == 5
    assert report["status"] == "passed"
    assert report["public_claim_eligible"] is False
    assert report["supports"] == ["HARD-050"]
    assert report["summary"] == {
        "completed": 5,
        "denominator": 5,
        "duration_p50_ms": 1,
        "duration_p95_ms": 1,
        "maximum_security_update_lag_ms": 0,
        "native_amd64": True,
        "rate": 1.0,
        "unique_ephemeral_topologies": 5,
        "wilson_95": [0.565518, 1.0],
    }
    assert all(row["session_state"] == "fresh_ephemeral" for row in report["repetitions"])
    assert report["evidence_digest"].startswith("sha256:")
    assert str(Path.home()) not in json.dumps(report, sort_keys=True)
    assert "product readiness" in report["limitation"]


@pytest.mark.parametrize("repetitions", [0, 4, 21, True])
def test_repetition_denominator_is_closed(repetitions: int) -> None:
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_repetitions"):
        SCRIPT.run_hosted_qualification(
            environment=_environment(),
            repetitions=repetitions,
            build=_build_evidence,
            session=lambda **_kwargs: _live_evidence(1),
        )


def test_report_rejects_reused_topology_and_changed_images() -> None:
    context = SCRIPT.HostedRunnerContext.from_environment(_environment())
    rows = [(_live_evidence(index), 100 + index) for index in range(1, 6)]
    duplicate = dict(rows[-1][0])
    duplicate["topology_evidence_digest"] = rows[0][0]["topology_evidence_digest"]
    rows[-1] = (duplicate, 105)
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_topology_reused"):
        SCRIPT.build_hosted_report(
            context=context,
            build_evidence=_build_evidence(),
            repetitions=rows,
            generated_at="2026-07-20T04:00:00Z",
            source_digest=_digest("c"),
        )

    rows = [(_live_evidence(index), 100 + index) for index in range(1, 6)]
    changed = dict(rows[-1][0])
    changed["browser_image_digest"] = _digest("d")
    rows[-1] = (changed, 105)
    with pytest.raises(
        SCRIPT.HostedQualificationRejected,
        match="hosted_browser_build_binding",
    ):
        SCRIPT.build_hosted_report(
            context=context,
            build_evidence=_build_evidence(),
            repetitions=rows,
            generated_at="2026-07-20T04:00:00Z",
            source_digest=_digest("c"),
        )


@pytest.mark.parametrize(
    ("field", "replacement", "reason"),
    [
        ("browser_image_digest", _digest("6"), "hosted_browser_build_binding"),
        ("broker_image_digest", _digest("7"), "hosted_broker_build_binding"),
        ("broker_binary_digest", _digest("8"), "hosted_broker_binary_binding"),
        (
            "browser_security_source_digest",
            _digest("9"),
            "hosted_release_evidence_binding",
        ),
    ],
)
def test_report_cross_binds_every_live_result_to_the_exact_build(
    field: str,
    replacement: str,
    reason: str,
) -> None:
    rows = [(_live_evidence(index), 100 + index) for index in range(1, 6)]
    changed = dict(rows[2][0])
    changed[field] = replacement
    rows[2] = (changed, rows[2][1])
    with pytest.raises(SCRIPT.HostedQualificationRejected, match=reason):
        SCRIPT.build_hosted_report(
            context=SCRIPT.HostedRunnerContext.from_environment(_environment()),
            build_evidence=_build_evidence(),
            repetitions=rows,
            generated_at="2026-07-20T04:00:00Z",
            source_digest=_digest("c"),
        )


def test_live_and_build_evidence_reconstruct_exact_schemas() -> None:
    build = _build_evidence()
    assert SCRIPT._validated_build_evidence(build) == build
    build["extra"] = "untrusted"
    with pytest.raises(SCRIPT.LiveSessionRejected, match="browser_build_evidence_shape"):
        SCRIPT._validated_build_evidence(build)

    build = _build_evidence()
    build["schema_version"] = True
    with pytest.raises(SCRIPT.LiveSessionRejected, match="browser_build_evidence_identity"):
        SCRIPT._validated_build_evidence(build)

    live = _live_evidence(1)
    assert SCRIPT._validated_live_evidence(live) == live
    live["browser_state"] = "ready"
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_live_evidence_identity"):
        SCRIPT._validated_live_evidence(live)


def test_source_fingerprint_is_stable_content_only_and_workflow_bound() -> None:
    assert ".github/workflows/oliver-ci.yml" in SCRIPT.SOURCE_PATHS
    first = SCRIPT._source_digest()
    second = SCRIPT._source_digest()
    assert first == second
    assert first.startswith("sha256:")
    assert str(ROOT) not in first


def test_source_fingerprint_rejects_links(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    assert SCRIPT._source_digest().startswith("sha256:")

    linked = tmp_path / "linked.py"
    source.rename(linked)
    source.symlink_to(linked)
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_identity"):
        SCRIPT._source_digest()


def test_ci_runs_repeated_cell_and_attests_push_evidence() -> None:
    workflow = (ROOT / ".github/workflows/oliver-ci.yml").read_text(encoding="utf-8")
    assert "scripts/henry_boron_hosted_qualification.py --repetitions 5" in workflow
    assert "grace-boron-hosted-qualification.json" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6" in workflow
    browser_job = workflow.split("  browser-isolation:\n", 1)[1].split(
        "  browser-evidence-attestation:\n",
        1,
    )[0]
    attestation_job = workflow.split("  browser-evidence-attestation:\n", 1)[1].split(
        "  macos-native:\n",
        1,
    )[0]
    assert "id-token: write" not in browser_job
    assert "if: ${{ github.event_name == 'push' }}" in attestation_job
    assert "id-token: write" in attestation_job
