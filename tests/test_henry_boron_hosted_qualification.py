from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

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


@pytest.fixture(autouse=True)
def _captured_development_runtime():
    payloads = tuple((relative, (ROOT / relative).read_bytes()) for relative in SCRIPT.SOURCE_PATHS)
    with SCRIPT._runtime_from_payloads(payloads):
        yield


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _write_hosted_source_tree(root: Path, *, marker: str) -> None:
    for relative in SCRIPT.HOSTED_BUILD_CONTEXT_PATHS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{marker}:{relative}\n".encode("utf-8"))


def _accept_test_revision(snapshot, _revision: str) -> tuple[tuple[str, bytes], ...]:
    return snapshot.payloads()


def _commit_test_repository(root: Path, *, payload: bytes = b"trusted\n") -> str:
    subprocess.run(["/usr/bin/git", "init", "-q", str(root)], check=True)
    source = root / "source.py"
    source.write_bytes(payload)
    subprocess.run(["/usr/bin/git", "-C", str(root), "add", "--", "source.py"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(root),
            "-c",
            "user.name=Algo CLI Test",
            "-c",
            "user.email=algo-cli@example.test",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )
    return subprocess.run(
        ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _environment(**changes: str) -> dict[str, str]:
    value = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_WORKFLOW_SHA": "a" * 40,
        "GITHUB_REPOSITORY": "Seabass-up/Algo-cli",
        "GITHUB_REPOSITORY_ID": "1297752684",
        "GITHUB_RUN_ID": "987654321",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_WORKFLOW_REF": ("Seabass-up/Algo-cli/.github/workflows/oliver-ci.yml@refs/heads/main"),
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
    }
    value.update(changes)
    return value


def _build_evidence(*, source_digest: str = _digest("c")) -> dict[str, object]:
    return {
        "schema_version": 2,
        "platform": "linux/amd64",
        "qualification_source_digest": source_digest,
        "browser_tag": ("ghcr.io/seabass-up/algo-cli-boron-browser:run-987654321-2-" + "a" * 40),
        "browser_repository": "ghcr.io/seabass-up/algo-cli-boron-browser",
        "browser_index_digest": _digest("1"),
        "browser_platform_manifest_digest": _digest("6"),
        "browser_config_digest": _digest("c"),
        "browser_build_metadata_digest": _digest("d"),
        "browser_provenance_digest": _digest("7"),
        "browser_sbom_digest": _digest("8"),
        "browser_code_digest": _digest("2"),
        "browser_version": "151.0.7922.108",
        "browser_security_update_lag_ms": 0,
        "browser_security_max_update_lag_ms": 72 * 60 * 60 * 1000,
        "browser_security_latest_version": "151.0.7922.108",
        "browser_security_latest_release_at_ms": 1_786_046_667_459,
        "browser_security_evidence_observed_at_ms": 1_786_046_668_000,
        "browser_security_source": "google_version_history",
        "browser_security_source_digest": _digest("3"),
        "native_browser_built": False,
        "native_browser_fresh": False,
        "native_browser_freshness_reason": "upstream_patch_equivalence_unverified",
        "broker_tag": ("ghcr.io/seabass-up/algo-cli-xenon-broker:run-987654321-2-" + "a" * 40),
        "broker_repository": "ghcr.io/seabass-up/algo-cli-xenon-broker",
        "broker_index_digest": _digest("4"),
        "broker_platform_manifest_digest": _digest("9"),
        "broker_config_digest": _digest("e"),
        "broker_build_metadata_digest": _digest("f"),
        "broker_provenance_digest": _digest("a"),
        "broker_sbom_digest": _digest("b"),
        "broker_code_digest": _digest("5"),
        "cryptography_version": "50.0.0",
        "image_provenance": "ghcr_buildkit_max_sbom",
        "non_root_defaults": True,
    }


def _live_evidence(
    serial: int,
    *,
    source_digest: str = _digest("c"),
) -> dict[str, object]:
    assert 1 <= serial <= 9
    return {
        "schema_version": 2,
        "platform": "linux/amd64",
        "qualification_source_digest": source_digest,
        "browser_index_digest": _digest("1"),
        "browser_platform_manifest_digest": _digest("6"),
        "browser_config_digest": _digest("c"),
        "browser_build_metadata_digest": _digest("d"),
        "browser_provenance_digest": _digest("7"),
        "browser_sbom_digest": _digest("8"),
        "broker_index_digest": _digest("4"),
        "broker_platform_manifest_digest": _digest("9"),
        "broker_config_digest": _digest("e"),
        "broker_build_metadata_digest": _digest("f"),
        "broker_provenance_digest": _digest("a"),
        "broker_sbom_digest": _digest("b"),
        "broker_code_digest": _digest("5"),
        "topology_evidence_digest": _digest(str(serial)),
        "internal_participant_count": 2,
        "browser_state": "verified",
        "browser_major": 151,
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
    assert context.source_ref == "refs/heads/main"
    assert context.ref_protected is True
    assert context.repository == "Seabass-up/Algo-cli"
    assert context.repository_id == 1_297_752_684
    assert context.workflow_ref_digest.startswith("sha256:")

    for environment, reason in (
        ({}, "hosted_environment"),
        (_environment(RUNNER_ARCH="ARM64"), "hosted_native_amd64_required"),
        (_environment(RUNNER_OS="macOS"), "hosted_native_amd64_required"),
        (_environment(GITHUB_SHA="main"), "hosted_revision"),
        (_environment(GITHUB_EVENT_NAME="pull_request"), "hosted_event"),
        (_environment(GITHUB_REF="refs/heads/develop"), "hosted_protected_ref"),
        (_environment(GITHUB_REF_PROTECTED="false"), "hosted_protected_ref"),
        (_environment(GITHUB_WORKFLOW_SHA="b" * 40), "hosted_workflow_revision"),
        (_environment(RUNNER_ENVIRONMENT="self-hosted"), "hosted_runner_environment"),
        (_environment(GITHUB_REPOSITORY="attacker/repo"), "hosted_repository"),
        (_environment(GITHUB_REPOSITORY_ID="123456789"), "hosted_repository"),
        (_environment(GITHUB_RUN_ID="0"), "hosted_run_id"),
        (_environment(GITHUB_WORKFLOW_REF="bad\nref"), "hosted_workflow_ref"),
    ):
        with pytest.raises(SCRIPT.HostedQualificationRejected, match=reason):
            SCRIPT.HostedRunnerContext.from_environment(environment)


def test_repeated_runner_builds_once_and_retains_honest_denominators() -> None:
    builds = 0
    sessions = 0
    ticks = iter(range(0, 12_000_000, 1_000_000))
    source_digest = SCRIPT._source_digest()

    def build() -> dict[str, object]:
        nonlocal builds
        builds += 1
        return _build_evidence(source_digest=source_digest)

    def session(*, build_evidence, environment) -> dict[str, object]:
        nonlocal sessions
        assert build_evidence == _build_evidence(source_digest=source_digest)
        assert environment == _environment()
        sessions += 1
        return _live_evidence(sessions, source_digest=source_digest)

    report = SCRIPT.run_hosted_qualification(
        environment=_environment(),
        repetitions=5,
        build=build,
        session=session,
        monotonic_ns=lambda: next(ticks),
        now=lambda: datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
        revision_verifier=_accept_test_revision,
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


def test_runtime_session_receives_the_git_verified_captured_seccomp_bytes(
    monkeypatch,
) -> None:
    source_digest = SCRIPT._source_digest()
    expected_seccomp = (ROOT / "algo_cli/resources/boron_browser/boron_seccomp_profile.json").read_bytes()
    observed: list[bytes] = []
    ticks = iter(range(0, 12_000_000, 1_000_000))

    def session(*, build_evidence, environment, seccomp_profile):
        assert build_evidence == _build_evidence(source_digest=source_digest)
        assert environment == _environment()
        observed.append(seccomp_profile)
        return _live_evidence(len(observed), source_digest=source_digest)

    monkeypatch.setattr(SCRIPT, "run_live_session", session)
    report = SCRIPT.run_hosted_qualification(
        environment=_environment(),
        repetitions=5,
        build=lambda: _build_evidence(source_digest=source_digest),
        monotonic_ns=lambda: next(ticks),
        now=lambda: datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
        revision_verifier=_accept_test_revision,
    )

    assert report["status"] == "passed"
    assert observed == [expected_seccomp] * 5


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
    changed["browser_index_digest"] = _digest("d")
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
        (
            "qualification_source_digest",
            _digest("f"),
            "hosted_qualification_source_binding",
        ),
        ("browser_index_digest", _digest("a"), "hosted_browser_build_binding"),
        (
            "browser_platform_manifest_digest",
            _digest("a"),
            "hosted_browser_platform_binding",
        ),
        ("browser_config_digest", _digest("6"), "hosted_browser_config_binding"),
        (
            "browser_build_metadata_digest",
            _digest("6"),
            "hosted_browser_metadata_binding",
        ),
        (
            "browser_provenance_digest",
            _digest("6"),
            "hosted_browser_provenance_binding",
        ),
        ("browser_sbom_digest", _digest("6"), "hosted_browser_sbom_binding"),
        ("broker_index_digest", _digest("7"), "hosted_broker_build_binding"),
        (
            "broker_platform_manifest_digest",
            _digest("7"),
            "hosted_broker_platform_binding",
        ),
        ("broker_config_digest", _digest("7"), "hosted_broker_config_binding"),
        (
            "broker_build_metadata_digest",
            _digest("7"),
            "hosted_broker_metadata_binding",
        ),
        (
            "broker_provenance_digest",
            _digest("7"),
            "hosted_broker_provenance_binding",
        ),
        ("broker_sbom_digest", _digest("7"), "hosted_broker_sbom_binding"),
        ("broker_code_digest", _digest("8"), "hosted_broker_code_binding"),
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
    live["schema_version"] = True
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_live_evidence_identity"):
        SCRIPT._validated_live_evidence(live)

    live = _live_evidence(1)
    live["browser_state"] = "ready"
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_live_evidence_identity"):
        SCRIPT._validated_live_evidence(live)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        (
            "browser_tag",
            "ghcr.io/seabass-up/algo-cli-boron-browser:run-987654322-2-" + "a" * 40,
        ),
        (
            "broker_tag",
            "ghcr.io/seabass-up/algo-cli-xenon-broker:run-987654321-3-" + "a" * 40,
        ),
        ("browser_repository", "ghcr.io/attacker/algo-cli-boron-browser"),
        ("broker_repository", "ghcr.io/attacker/algo-cli-xenon-broker"),
    ],
)
def test_report_rejects_registry_evidence_not_bound_to_exact_hosted_run(
    field: str,
    replacement: str,
) -> None:
    build = _build_evidence()
    build[field] = replacement
    with pytest.raises(
        (SCRIPT.HostedQualificationRejected, SCRIPT.LiveSessionRejected),
        match="hosted_registry_build_binding|browser_build_evidence_identity",
    ):
        SCRIPT.build_hosted_report(
            context=SCRIPT.HostedRunnerContext.from_environment(_environment()),
            build_evidence=build,
            repetitions=[(_live_evidence(index), 100 + index) for index in range(1, 6)],
            generated_at="2026-07-20T04:00:00Z",
            source_digest=_digest("c"),
        )


def test_source_fingerprint_is_stable_content_only_and_workflow_bound() -> None:
    assert SCRIPT.SOURCE_PATHS is SCRIPT.HOSTED_BUILD_CONTEXT_PATHS
    assert ".github/workflows/oliver-ci.yml" in SCRIPT.SOURCE_PATHS
    first = SCRIPT._source_digest()
    second = SCRIPT._source_digest()
    assert first == second
    assert first.startswith("sha256:")
    assert str(ROOT) not in first


def test_source_snapshot_must_match_exact_git_revision_blobs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    revision = _commit_test_repository(root)
    snapshot = SCRIPT._SourceSnapshot.capture(
        root=root,
        source_paths=("source.py",),
    )
    try:
        assert SCRIPT._verified_revision_payloads(snapshot, revision) == (("source.py", b"trusted\n"),)
        object_id = subprocess.run(
            ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD:source.py"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_git_object"):
            SCRIPT._run_git_object(
                ("cat-file", "blob", object_id),
                maximum_bytes=4,
                root=root,
            )
    finally:
        snapshot.close()

    (root / "source.py").write_bytes(b"hostile-worktree\n")
    snapshot = SCRIPT._SourceSnapshot.capture(
        root=root,
        source_paths=("source.py",),
    )
    try:
        with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_revision_mismatch"):
            SCRIPT._verified_revision_payloads(snapshot, revision)
    finally:
        snapshot.close()


def test_source_snapshot_rejects_a_different_valid_commit_and_malformed_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    first_revision = _commit_test_repository(root)
    (root / "source.py").write_bytes(b"second\n")
    subprocess.run(["/usr/bin/git", "-C", str(root), "add", "--", "source.py"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(root),
            "-c",
            "user.name=Algo CLI Test",
            "-c",
            "user.email=algo-cli@example.test",
            "commit",
            "-q",
            "-m",
            "second",
        ],
        check=True,
    )
    snapshot = SCRIPT._SourceSnapshot.capture(root=root, source_paths=("source.py",))
    try:
        with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_revision_mismatch"):
            SCRIPT._verified_revision_payloads(snapshot, first_revision)

        def malformed_git_query(arguments, **_kwargs):
            if arguments[:2] == ("cat-file", "-t"):
                return b"commit\n"
            return b"120000 blob " + b"f" * 40 + b"\tsource.py\x00"

        with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_revision_tree"):
            SCRIPT._verified_revision_payloads(
                snapshot,
                "a" * 40,
                git_query=malformed_git_query,
            )
    finally:
        snapshot.close()


def test_verified_runtime_executes_captured_bytes_not_checkout_or_sys_modules(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payloads = tuple((relative, (ROOT / relative).read_bytes()) for relative in SCRIPT.SOURCE_PATHS)
    malicious_root = tmp_path / "repo"
    for module_path in SCRIPT._RUNTIME_MODULE_PATHS.values():
        path = malicious_root / module_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise RuntimeError('checkout payload executed')\n", encoding="utf-8")
    fake_builder = type(sys)("boron_browser_build_images")
    fake_builder.malicious_marker = True
    monkeypatch.setitem(sys.modules, "boron_browser_build_images", fake_builder)
    monkeypatch.setattr(SCRIPT, "ROOT", malicious_root)
    absent_name = "algo_cli.xenon_browser_entry"
    previous_absent = sys.modules.pop(absent_name, None)

    try:
        with SCRIPT._runtime_from_payloads(payloads) as runtime:
            assert runtime.build_module is not fake_builder
            assert not hasattr(runtime.build_module, "malicious_marker")
            assert runtime.build_module.CHROME_VERSION == SCRIPT.CHROME_VERSION
            sys.modules[absent_name] = type(sys)(absent_name)

        assert sys.modules["boron_browser_build_images"] is fake_builder
        assert absent_name not in sys.modules
    finally:
        if previous_absent is not None:
            sys.modules[absent_name] = previous_absent


def test_no_site_bootstrap_activates_locked_dependencies_only_after_capture() -> None:
    program = f"""
import importlib.util
from pathlib import Path
import sys
root = Path({str(ROOT)!r})
path = root / 'scripts/henry_boron_hosted_qualification.py'
spec = importlib.util.spec_from_file_location('henry_no_site_smoke', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
payloads = tuple((relative, (root / relative).read_bytes()) for relative in module.SOURCE_PATHS)
with module._runtime_from_payloads(payloads) as runtime:
    assert runtime.build_module.CHROME_VERSION == module.CHROME_VERSION
    assert runtime.live_module.XenonBrokerRejected.__name__ == 'XenonBrokerRejected'
assert all('site-packages' not in value for value in sys.path)
print('isolated-runtime-ok')
"""
    environment = dict(os.environ)
    environment["VIRTUAL_ENV"] = str(ROOT / ".venv")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-S", "-c", program],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "isolated-runtime-ok"
    assert result.stderr == ""


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


def test_source_snapshot_rejects_hardlinks(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    os.link(source, tmp_path / "second-name.py")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))

    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_identity"):
        SCRIPT._SourceSnapshot.capture()


def test_source_snapshot_final_open_is_nonblocking_and_rejects_a_fifo_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    real_open = SCRIPT.os.open
    swapped = False

    def open_after_fifo_swap(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "source.py" and not swapped:
            swapped = True
            assert flags & SCRIPT.os.O_NONBLOCK
            source.unlink()
            SCRIPT.os.mkfifo(source)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(SCRIPT.os, "open", open_after_fifo_swap)

    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_changed"):
        SCRIPT._SourceSnapshot.capture()
    assert swapped


def test_source_snapshot_rejects_a_symlinked_directory_ancestor(
    tmp_path: Path,
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "source.py").write_text("pass\n", encoding="utf-8")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual, target_is_directory=True)

    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_identity"):
        SCRIPT._SourceSnapshot.capture(
            root=linked_root,
            source_paths=("source.py",),
        )


def test_source_snapshot_detects_a_whole_root_directory_swap(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "source.py").write_text("pass\n", encoding="utf-8")
    snapshot = SCRIPT._SourceSnapshot.capture(
        root=root,
        source_paths=("source.py",),
    )
    try:
        displaced = tmp_path / "displaced"
        root.rename(displaced)
        root.mkdir()
        (root / "source.py").write_text("pass\n", encoding="utf-8")
        with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_changed"):
            snapshot.verify()
    finally:
        snapshot.close()


def test_source_snapshot_detects_symlink_and_atomic_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))

    snapshot = SCRIPT._SourceSnapshot.capture()
    try:
        target = tmp_path / "target.py"
        source.rename(target)
        source.symlink_to(target)
        with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_changed"):
            snapshot.verify()
    finally:
        snapshot.close()

    source.unlink()
    target.rename(source)
    snapshot = SCRIPT._SourceSnapshot.capture()
    try:
        replacement = tmp_path / "replacement.py"
        replacement.write_text("pass\n", encoding="utf-8")
        os.replace(replacement, source)
        with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_changed"):
            snapshot.verify()
    finally:
        snapshot.close()


def test_runner_fails_closed_when_source_mutates_during_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    source_digest = SCRIPT._source_digest()
    sessions = 0

    def build() -> dict[str, object]:
        source.write_text("changed\n", encoding="utf-8")
        return _build_evidence(source_digest=source_digest)

    def session(**_kwargs) -> dict[str, object]:
        nonlocal sessions
        sessions += 1
        return _live_evidence(1, source_digest=source_digest)

    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_changed"):
        SCRIPT.run_hosted_qualification(
            environment=_environment(),
            repetitions=5,
            build=build,
            session=session,
            revision_verifier=_accept_test_revision,
        )
    assert sessions == 0


def test_registry_build_consumes_immutable_snapshot_when_source_root_is_swapped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_hosted_source_tree(root, marker="trusted")
    monkeypatch.setattr(SCRIPT, "ROOT", root)
    sessions = 0
    selected = "algo_cli/__init__.py"

    def build_registry_images(
        *,
        environment,
        qualification_source_digest,
        context_archive,
    ) -> dict[str, object]:
        del environment
        with tarfile.open(fileobj=io.BytesIO(context_archive), mode="r:") as archive:
            source = archive.extractfile(selected)
            assert source is not None
            assert source.read() == f"trusted:{selected}\n".encode()

        displaced = tmp_path / "displaced"
        root.rename(displaced)
        root.mkdir()
        _write_hosted_source_tree(root, marker="malicious")
        return _build_evidence(source_digest=qualification_source_digest)

    def session(**_kwargs) -> dict[str, object]:
        nonlocal sessions
        sessions += 1
        return _live_evidence(1)

    monkeypatch.setattr(SCRIPT, "build_registry_images", build_registry_images)
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_changed"):
        SCRIPT.run_hosted_qualification(
            environment=_environment(),
            repetitions=5,
            session=session,
            revision_verifier=_accept_test_revision,
        )
    assert sessions == 0


def test_runner_fails_closed_when_source_mutates_between_live_phases(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    source_digest = SCRIPT._source_digest()
    sessions = 0

    def session(**_kwargs) -> dict[str, object]:
        nonlocal sessions
        sessions += 1
        source.write_text("changed\n", encoding="utf-8")
        return _live_evidence(sessions, source_digest=source_digest)

    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_changed"):
        SCRIPT.run_hosted_qualification(
            environment=_environment(),
            repetitions=5,
            build=lambda: _build_evidence(source_digest=source_digest),
            session=session,
            revision_verifier=_accept_test_revision,
        )
    assert sessions == 1


def test_main_rechecks_source_immediately_before_report_emission(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    output = tmp_path / "report.json"
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    for key, value in _environment().items():
        monkeypatch.setenv(key, value)

    def mutate_before_return(**_kwargs) -> dict[str, object]:
        source.write_text("changed\n", encoding="utf-8")
        return {"status": "passed"}

    monkeypatch.setattr(SCRIPT, "run_hosted_qualification", mutate_before_return)
    monkeypatch.setattr(SCRIPT, "_verified_revision_payloads", _accept_test_revision)

    assert SCRIPT.main(["--repetitions", "5", "--output-report", str(output)]) == 2
    assert not output.exists()
    assert json.loads(capsys.readouterr().out) == {
        "reason_code": "hosted_source_changed",
        "status": "blocked",
    }


def test_main_never_labels_a_dirty_worktree_as_the_git_revision(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    revision = _commit_test_repository(root)
    (root / "source.py").write_bytes(b"dirty\n")
    monkeypatch.setattr(SCRIPT, "ROOT", root)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    environment = _environment(
        GITHUB_SHA=revision,
        GITHUB_WORKFLOW_SHA=revision,
    )
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        SCRIPT,
        "run_hosted_qualification",
        lambda **_kwargs: pytest.fail("dirty source reached qualification"),
    )

    assert SCRIPT.main(["--repetitions", "5"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "reason_code": "hosted_revision_mismatch",
        "status": "blocked",
    }


def test_ci_runs_repeated_cell_and_attests_push_evidence() -> None:
    workflow = (ROOT / ".github/workflows/oliver-ci.yml").read_text(encoding="utf-8")
    assert "scripts/henry_boron_hosted_qualification.py --repetitions 5" in workflow
    assert "grace-boron-hosted-qualification.json" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6" in workflow
    contracts_job = workflow.split("  browser-contracts:\n", 1)[1].split(
        "  browser-authority:\n",
        1,
    )[0]
    authority_job = workflow.split("  browser-authority:\n", 1)[1].split("  browser-isolation:\n", 1)[0]
    browser_job = workflow.split("  browser-isolation:\n", 1)[1].split("  browser-evidence-validation:\n", 1)[0]
    validation_job = workflow.split("  browser-evidence-validation:\n", 1)[1].split(
        "  browser-evidence-attestation:\n", 1
    )[0]
    attestation_job = workflow.split("  browser-evidence-attestation:\n", 1)[1].split(
        "  macos-native:\n",
        1,
    )[0]
    assert "Verify focused no-publish contracts" in contracts_job
    assert "packages: write" not in contracts_job
    assert "docker/login-action" not in contracts_job
    protected_main_base = (
        "github.event_name == 'push' && github.repository == 'Seabass-up/Algo-cli' "
        "&& github.repository_id == '1297752684' && github.ref == 'refs/heads/main' "
        "&& github.ref_protected && github.workflow_sha == github.sha"
    )
    protected_main = protected_main_base + " && vars.BORON_HARDENING_ENVIRONMENT_READY == 'true'"
    authority_trigger = (
        "github.event_name == 'push' && github.repository == 'Seabass-up/Algo-cli' && github.ref == 'refs/heads/main'"
    )
    assert authority_trigger in authority_job
    assert "Boron protected-environment authority" in authority_job
    assert 'test "${HENRY_REPOSITORY_ID}" = "1297752684"' in authority_job
    assert 'test "${HENRY_REF_PROTECTED}" = "true"' in authority_job
    assert 'test "${HENRY_WORKFLOW_SHA}" = "${HENRY_SOURCE_SHA}"' in authority_job
    assert 'test "${BORON_HARDENING_ENVIRONMENT_READY}" = "true"' in authority_job
    assert "api.github.com/repos/Seabass-up/Algo-cli/environments/browser-hardening" in authority_job
    assert 'document.get("can_admins_bypass") is not False' in authority_job
    assert 'policy.get("protected_branches") is not True' in authority_job
    assert 'reviewers[0].get("prevent_self_review") is not True' in authority_job
    assert "Boron environment authority verification failed" in authority_job
    assert "secrets." not in authority_job
    assert "actions/checkout" not in authority_job
    assert "packages: write" not in authority_job
    assert protected_main in browser_job
    assert "needs: [browser-contracts, browser-authority]" in browser_job
    assert "name: browser-hardening" in browser_job
    assert "contents: read" in browser_job
    assert "packages: write" in browser_job
    assert "id-token: write" not in browser_job
    assert "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c" in browser_job
    assert "id: henry-buildx" in browser_job
    assert "version: v0.36.1" in browser_job
    assert "cache-binary: false" in browser_job
    assert "buildkitd-flags: --log-format text" in browser_job
    assert "buildkitd-flags: --debug" not in browser_job
    assert "cleanup: false" in browser_job
    assert (
        "image=moby/buildkit:v0.32.2@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8"
    ) in browser_job
    assert "docker/login-action@dbcb813823bdd20940b903addbd779551569679f" in browser_job
    assert "registry: ghcr.io" in browser_job
    assert "username: ${{ github.actor }}" in browser_job
    assert "password: ${{ secrets.GITHUB_TOKEN }}" in browser_job
    assert "logout: true" in browser_job
    assert "DOCKER_CONFIG: ${{ runner.temp }}/henry-boron-docker-config" not in browser_job
    assert 'docker_config="${RUNNER_TEMP}/henry-boron-docker-config"' in browser_job
    assert 'install -d -m 0700 "${docker_config}"' in browser_job
    assert 'printf \'%s\\n\' "DOCKER_CONFIG=${docker_config}" >> "${GITHUB_ENV}"' in browser_job
    assert "set -euo pipefail" in browser_job
    assert 'test "$(git rev-parse --verify HEAD)" = "${GITHUB_SHA}"' in browser_job
    assert 'git diff --quiet --no-ext-diff --no-textconv "${GITHUB_SHA}" --' in browser_job
    assert 'git diff --cached --quiet --no-ext-diff --no-textconv "${GITHUB_SHA}" --' in browser_job
    assert "git status --porcelain=v1 --untracked-files=all" in browser_job
    assert 'buildx_plugin="${DOCKER_CONFIG}/cli-plugins/docker-buildx"' in browser_job
    assert 'test -f "${buildx_plugin}"' in browser_job
    assert 'test ! -L "${buildx_plugin}"' in browser_job
    assert "sha256sum --" in browser_job
    assert "48af8a397ebd60178778bf63611dbcebe5f5e7a9be90eb9147b24b9587455778" in browser_job
    assert ("github.com/docker/buildx v0.36.1 1d8dde89b8aba914e05e45366770736fea1fd690") in browser_job
    assert "BuildKit:[[:space:]]+v0\\.32\\.2" in browser_job
    assert "id: henry-docker-authority" in browser_job
    assert 'buildx_container_name="buildx_buildkit_${HENRY_BUILDX_NAME}0"' in browser_job
    assert "docker container inspect --format '{{.Id}}'" in browser_job
    assert "docker volume inspect --format '{{.Name}}'" in browser_job
    assert 'echo "container-id=${buildx_container_id}" >> "${GITHUB_OUTPUT}"' in browser_job
    assert 'echo "volume-name=${buildx_volume_name}" >> "${GITHUB_OUTPUT}"' in browser_job
    assert "| tee" not in browser_job
    assert "--output-report" in browser_job
    assert browser_job.count("python -I -B -S \\") == 2
    assert "End and verify Henry GHCR and Buildx authority" in browser_job
    assert "if: ${{ always() }}" in browser_job
    assert "HENRY_BUILDX_NAME: ${{ steps.henry-buildx.outputs.name }}" in browser_job
    assert "HENRY_BUILDX_CONTAINER_ID: ${{ steps.henry-docker-authority.outputs.container-id }}" in browser_job
    assert "HENRY_BUILDX_VOLUME_NAME: ${{ steps.henry-docker-authority.outputs.volume-name }}" in browser_job
    assert "docker logout ghcr.io" in browser_job
    assert 'Path(os.environ["DOCKER_CONFIG"]) / "config.json"' in browser_job
    assert "GHCR credential cleanup verification failed" in browser_job
    assert 'docker buildx rm --force --timeout 30s "${HENRY_BUILDX_NAME}"' in browser_job
    assert "docker buildx ls --format '{{.Name}}'" in browser_job
    assert "docker container ls --all --no-trunc --format '{{.ID}}'" in browser_job
    assert "docker volume ls --format '{{.Name}}'" in browser_job
    assert "timeout --signal=TERM --kill-after=5s" in browser_job
    assert browser_job.index("git rev-parse --verify HEAD") < browser_job.index("docker/login-action")
    assert browser_job.index("docker/login-action") < browser_job.index("--output-report")
    assert browser_job.index("docker logout ghcr.io") < browser_job.index("Retain Boron evidence")
    assert browser_job.index("docker buildx rm --force") < browser_job.index("Retain Boron evidence")
    assert "name: boron-browser-boundary-attempt-${{ github.run_attempt }}" in browser_job
    assert "evidence-artifact-id: ${{ steps.boron-evidence.outputs.artifact-id }}" in browser_job
    assert "if-no-files-found: error" in browser_job
    assert browser_job.count("--verify-report") == 1
    assert protected_main in validation_job
    assert "packages: write" not in validation_job
    assert "id-token: write" not in validation_job
    assert "--subject-digest-only" in validation_job
    assert "artifact-ids: ${{ needs.browser-isolation.outputs.evidence-artifact-id }}" in validation_job
    assert validation_job.count("python -I -B -S \\") == 1
    assert "report-digest" in validation_job
    assert protected_main in attestation_job
    assert "name: browser-hardening" in attestation_job
    assert "id-token: write" in attestation_job
    assert "attestations: write" in attestation_job
    assert "packages: write" not in attestation_job
    assert "actions/checkout" not in attestation_job
    assert "run:" not in attestation_job
    assert "subject-name: grace-boron-hosted-qualification.json" in attestation_job
    assert "subject-digest: ${{ needs.browser-evidence-validation.outputs.report-digest }}" in (attestation_job)


def test_verify_report_reconstructs_exact_evidence_and_rejects_blocked_or_tampered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _environment()
    context = SCRIPT.HostedRunnerContext.from_environment(environment)
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    source_digest = SCRIPT._source_digest()
    report = SCRIPT.build_hosted_report(
        context=context,
        build_evidence=_build_evidence(source_digest=source_digest),
        repetitions=[(_live_evidence(index, source_digest=source_digest), 100 + index) for index in range(1, 6)],
        generated_at="2026-07-20T04:00:00Z",
        source_digest=source_digest,
    )

    assert (
        SCRIPT.verify_hosted_report(
            report,
            environment=environment,
            verified_source_digest=source_digest,
        )
        == report
    )

    blocked = {"reason_code": "repo_digest_shape", "status": "blocked"}
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_report_shape"):
        SCRIPT.verify_hosted_report(
            blocked,
            environment=environment,
            verified_source_digest=source_digest,
        )

    tampered = json.loads(json.dumps(report))
    tampered["summary"]["rate"] = 0.5
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_report_mismatch"):
        SCRIPT.verify_hosted_report(
            tampered,
            environment=environment,
            verified_source_digest=source_digest,
        )


def test_subject_digest_only_emits_the_exact_verified_report_byte_digest(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    environment = _environment()
    context = SCRIPT.HostedRunnerContext.from_environment(environment)
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    source_digest = SCRIPT._source_digest()
    report = SCRIPT.build_hosted_report(
        context=context,
        build_evidence=_build_evidence(source_digest=source_digest),
        repetitions=[(_live_evidence(index, source_digest=source_digest), 100 + index) for index in range(1, 6)],
        generated_at="2026-07-20T04:00:00Z",
        source_digest=source_digest,
    )
    report_path = tmp_path / "report.json"
    SCRIPT._write_passed_report(report_path, report)
    expected = "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(SCRIPT, "_verified_revision_payloads", _accept_test_revision)

    assert SCRIPT.main(["--verify-report", str(report_path), "--subject-digest-only"]) == 0
    assert capsys.readouterr().out.strip() == expected


def test_strict_report_reader_rejects_duplicate_keys_and_links(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"status":"passed","status":"blocked"}', encoding="utf-8")
    with pytest.raises(
        SCRIPT.HostedQualificationRejected,
        match="hosted_report_duplicate_key",
    ):
        SCRIPT._strict_report(duplicate)

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    linked = tmp_path / "linked.json"
    linked.symlink_to(target)
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_report_file"):
        SCRIPT._strict_report(linked)


def test_pass_report_writer_is_atomic_exclusive_and_rejects_blocked(
    tmp_path: Path,
) -> None:
    output = tmp_path / "report.json"
    report = {"status": "passed"}
    SCRIPT._write_passed_report(output, report)
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == report

    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_report_output"):
        SCRIPT._write_passed_report(output, report)
    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_report_status"):
        SCRIPT._write_passed_report(tmp_path / "blocked.json", {"status": "blocked"})


def test_pass_report_writer_rejects_a_swapped_temporary_name_and_rolls_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "report.json"
    real_link = SCRIPT.os.link

    def swap_before_link(source, destination, **kwargs):
        source_directory = kwargs["src_dir_fd"]
        SCRIPT.os.unlink(source, dir_fd=source_directory)
        attacker = SCRIPT.os.open(
            source,
            SCRIPT.os.O_WRONLY | SCRIPT.os.O_CREAT | SCRIPT.os.O_EXCL,
            0o600,
            dir_fd=source_directory,
        )
        try:
            SCRIPT.os.write(attacker, b'{"status":"passed","secret":"attacker"}\n')
        finally:
            SCRIPT.os.close(attacker)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(SCRIPT.os, "link", swap_before_link)

    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_report_output"):
        SCRIPT._write_passed_report(output, {"status": "passed"})
    assert not output.exists()


def test_pass_report_writer_rolls_back_after_a_post_link_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "report.json"
    real_fsync = SCRIPT.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if SCRIPT.stat.S_ISDIR(SCRIPT.os.fstat(descriptor).st_mode):
            raise OSError("sensitive late failure")
        real_fsync(descriptor)

    monkeypatch.setattr(SCRIPT.os, "fsync", fail_directory_fsync)

    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_report_output"):
        SCRIPT._write_passed_report(output, {"status": "passed"})
    assert not output.exists()


def test_pass_report_writer_rolls_back_when_the_source_guard_fails_after_link(
    tmp_path: Path,
) -> None:
    output = tmp_path / "report.json"
    checks = 0

    def guard() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise SCRIPT.HostedQualificationRejected("hosted_source_changed")

    with pytest.raises(SCRIPT.HostedQualificationRejected, match="hosted_source_changed"):
        SCRIPT._write_passed_report(
            output,
            {"status": "passed"},
            guard=guard,
        )
    assert checks == 2
    assert not output.exists()


def test_blocked_main_run_returns_nonzero_and_never_creates_report(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output = tmp_path / "blocked-report.json"
    monkeypatch.setenv("GITHUB_ACTIONS", "false")

    assert SCRIPT.main(["--repetitions", "5", "--output-report", str(output)]) == 2
    assert not output.exists()
    assert json.loads(capsys.readouterr().out) == {
        "reason_code": "hosted_environment",
        "status": "blocked",
    }


def test_main_never_emits_unvalidated_dependency_exception_text(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(SCRIPT, "ROOT", tmp_path)
    monkeypatch.setattr(SCRIPT, "SOURCE_PATHS", ("source.py",))
    for key, value in _environment().items():
        monkeypatch.setenv(key, value)

    def fail_build(**_kwargs):
        raise SCRIPT.BuildRejected("private_token_material")

    monkeypatch.setattr(SCRIPT, "run_hosted_qualification", fail_build)
    monkeypatch.setattr(SCRIPT, "_verified_revision_payloads", _accept_test_revision)

    assert SCRIPT.main(["--repetitions", "5"]) == 2
    output = capsys.readouterr().out
    assert "private_token_material" not in output
    assert json.loads(output) == {
        "reason_code": "hosted_build_failed",
        "status": "blocked",
    }
