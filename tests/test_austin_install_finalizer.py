from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
from dataclasses import replace

import pytest

from algo_cli.austin_install_finalizer import (
    AustinCredentialEnumerationRunner,
    AustinInstallRejected,
    finalize_austin_install,
)
from algo_cli.ada_credential_registry import AdaNativeCredentialEnumeration
from algo_cli.david_control_kernel import ControlSigner
from algo_cli.grace_key_store import KEYRING_SERVICE
from algo_cli.oliver_control_installation import (
    AUSTIN_APP_BUNDLE_ID,
    OLIVER_FIXED_CREDENTIAL_LABELS,
    OliverInstallInventory,
    OliverInstallRoots,
    OliverUninstallMode,
    execute_oliver_uninstall,
    expected_austin_launch_agent,
    expected_neon_native_host,
    plan_oliver_uninstall,
)
from algo_cli.oliver_control_installer import OliverInstallEvidencePaths


TEAM_ID = "ABCDE12345"
ORIGIN = "chrome-extension://" + "a" * 32 + "/"
INSTALL_ID = "00000000-0000-4000-8000-000000000301"
NOW_MS = 1_800_000_000_000


class FakeCredentialStore:
    service = KEYRING_SERVICE

    def __init__(self, *, complete: bool = True) -> None:
        self.complete = complete
        self.values = {
            label: f"opaque-{index}"
            for index, label in enumerate(sorted(OLIVER_FIXED_CREDENTIAL_LABELS))
        }

    def fingerprint(self, label: str) -> str | None:
        value = self.values.get(label)
        return None if value is None else "sha256:" + hashlib.sha256(value.encode()).hexdigest()

    def compare_and_delete(self, label: str, *, expected_digest: str) -> bool:
        if self.fingerprint(label) != expected_digest:
            return False
        self.values.pop(label, None)
        return True

    def complete_inventory_labels(self) -> tuple[str, ...] | None:
        return tuple(sorted(self.values)) if self.complete else None

    def complete_inventory_snapshot(self) -> tuple[tuple[str, str | None], ...] | None:
        if not self.complete:
            return None
        return tuple((label, self.fingerprint(label)) for label in sorted(self.values))


class FakeIdentityProbe:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls = 0

    def verify(self, *, roots: OliverInstallRoots, team_id: str) -> None:
        self.calls += 1
        assert not roots.production
        assert team_id == TEAM_ID
        if self.reject:
            from algo_cli.oliver_control_installation import OliverUninstallRejected

            raise OliverUninstallRejected("install_identity_rejected")


def _write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _fixture(tmp_path: Path):
    uid = os.getuid()
    root = (tmp_path / "root").resolve()
    roots = OliverInstallRoots._for_test(root, uid=uid)
    evidence = OliverInstallEvidencePaths._for_test(
        (tmp_path / "evidence").resolve(), uid=uid
    )
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
    disabled_native = ControlSigner.from_private_bytes(bytes(reversed(range(32))))
    _write(
        roots.app_bundle / "Contents" / "Info.plist",
        plistlib.dumps(
            {
                "CFBundleExecutable": "austin-control",
                "CFBundleIdentifier": AUSTIN_APP_BUNDLE_ID,
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.18.0",
                "CFBundleVersion": "1800",
            }
        ),
        mode=0o644,
    )
    _write(
        roots.app_bundle / "Contents" / "MacOS" / "austin-control",
        b"app",
        mode=0o755,
    )
    for name in (
        "austin-relay",
        "austin-tcc-adapter",
        "austin-credential-migrator",
        "neon-native-host",
    ):
        _write(
            roots.app_bundle / "Contents" / "Helpers" / name,
            name.encode("ascii"),
            mode=0o755,
        )
    _write(
        roots.app_bundle / "Contents" / "Resources" / "AustinAuthorityPublicKey.bin",
        disabled_native.verifier.public_bytes,
        mode=0o444,
    )
    _write(
        roots.app_bundle / "Contents" / "Resources" / "NeonAllowedOrigin.txt",
        ORIGIN.encode("utf-8"),
        mode=0o444,
    )
    roots.home.mkdir(parents=True, exist_ok=True)
    roots.home.chmod(0o700)
    return roots, evidence, signer


def _finalize(
    roots: OliverInstallRoots,
    evidence: OliverInstallEvidencePaths,
    signer: ControlSigner,
    credentials: FakeCredentialStore,
    probe: FakeIdentityProbe,
    *,
    now_ms: int = NOW_MS,
    install_id: str = INSTALL_ID,
):
    return finalize_austin_install(
        roots=roots,
        evidence_paths=evidence,
        signer=signer,
        credential_store=credentials,
        team_id=TEAM_ID,
        identity_probe=probe,
        clock_ms=lambda: now_ms,
        install_id=install_id,
        allow_test_paths=True,
    )


def test_finalizer_creates_inert_exact_surfaces_and_signed_evidence(tmp_path: Path) -> None:
    roots, evidence, signer = _fixture(tmp_path)
    credentials = FakeCredentialStore()
    probe = FakeIdentityProbe()

    first = _finalize(roots, evidence, signer, credentials, probe)
    second = _finalize(roots, evidence, signer, credentials, probe)

    assert first.status == "published"
    assert second.status == "unchanged"
    assert probe.calls == 4
    assert plistlib.loads(roots.launch_agent.read_bytes()) == expected_austin_launch_agent(roots)
    assert json.loads(roots.chrome_native_host.read_text(encoding="utf-8")) == expected_neon_native_host(
        roots, extension_origin=ORIGIN
    )
    assert stat.S_IMODE(roots.launch_agent.stat().st_mode) == 0o600
    assert stat.S_IMODE(roots.chrome_native_host.stat().st_mode) == 0o600
    assert evidence.inventory.exists()
    assert evidence.authority.read_bytes() == signer.verifier.public_bytes


def test_upgrade_rollback_rejection_and_runtime_uninstall_are_one_bounded_lifecycle(
    tmp_path: Path,
) -> None:
    class LaunchController:
        def state(self, *, uid: int, label: str) -> str:
            assert uid >= 0 and label
            return "absent"

        def bootout(self, *, uid: int, label: str) -> None:
            raise AssertionError("an unloaded agent must not be booted out")

    class ProcessProbe:
        def assert_stopped(self, executable_paths) -> None:
            assert len(tuple(executable_paths)) == 5

    roots, evidence, signer = _fixture(tmp_path)
    credentials = FakeCredentialStore()
    probe = FakeIdentityProbe()
    _finalize(roots, evidence, signer, credentials, probe)

    info_path = roots.app_bundle / "Contents" / "Info.plist"
    upgraded_info = plistlib.loads(info_path.read_bytes())
    upgraded_info["CFBundleShortVersionString"] = "0.18.1"
    upgraded_info["CFBundleVersion"] = "1801"
    _write(info_path, plistlib.dumps(upgraded_info), mode=0o644)
    _write(
        roots.app_bundle / "Contents" / "MacOS" / "austin-control",
        b"upgraded-app",
        mode=0o755,
    )
    upgraded = _finalize(
        roots,
        evidence,
        signer,
        credentials,
        probe,
        now_ms=NOW_MS + 1,
        install_id="00000000-0000-4000-8000-000000000302",
    )
    assert upgraded.replaced_previous_inventory
    upgraded_payload = evidence.inventory.read_bytes()

    rolled_back_info = dict(upgraded_info)
    rolled_back_info["CFBundleShortVersionString"] = "0.18.0"
    rolled_back_info["CFBundleVersion"] = "1800"
    _write(info_path, plistlib.dumps(rolled_back_info), mode=0o644)
    _write(
        roots.app_bundle / "Contents" / "MacOS" / "austin-control",
        b"app",
        mode=0o755,
    )
    with pytest.raises(AustinInstallRejected, match="install_evidence_version_rollback"):
        _finalize(
            roots,
            evidence,
            signer,
            credentials,
            probe,
            now_ms=NOW_MS + 2,
            install_id="00000000-0000-4000-8000-000000000303",
        )
    assert evidence.inventory.read_bytes() == upgraded_payload

    _write(info_path, plistlib.dumps(upgraded_info), mode=0o644)
    _write(
        roots.app_bundle / "Contents" / "MacOS" / "austin-control",
        b"upgraded-app",
        mode=0o755,
    )
    inventory = OliverInstallInventory.from_bytes(upgraded_payload)
    inventory.verify(signer.verifier)
    launch = LaunchController()
    processes = ProcessProbe()
    plan = plan_oliver_uninstall(
        inventory=inventory,
        verifier=signer.verifier,
        roots=roots,
        mode=OliverUninstallMode.RUNTIME_ONLY,
        launch_controller=launch,
        process_probe=processes,
        allow_test_roots=True,
    )
    times = iter((NOW_MS + 3, NOW_MS + 4))
    receipt = execute_oliver_uninstall(
        inventory=inventory,
        signer=signer,
        roots=roots,
        mode=OliverUninstallMode.RUNTIME_ONLY,
        expected_plan_digest=plan.digest,
        confirmation=plan.confirmation_phrase,
        launch_controller=launch,
        process_probe=processes,
        clock_ms=lambda: next(times),
        allow_test_roots=True,
    )

    receipt.verify(signer.verifier)
    assert receipt.payload["outcome"] == "completed"
    assert not roots.app_bundle.exists()
    assert not roots.launch_agent.exists()
    assert not roots.chrome_native_host.exists()
    assert evidence.inventory.exists()


def test_identity_or_registry_failure_precedes_user_surface_mutation(tmp_path: Path) -> None:
    roots, evidence, signer = _fixture(tmp_path)

    with pytest.raises(AustinInstallRejected, match="install_identity_rejected"):
        _finalize(
            roots,
            evidence,
            signer,
            FakeCredentialStore(),
            FakeIdentityProbe(reject=True),
        )
    assert not roots.launch_agent.exists()
    assert not roots.chrome_native_host.exists()

    with pytest.raises(AustinInstallRejected, match="austin_install_credential_registry"):
        _finalize(
            roots,
            evidence,
            signer,
            FakeCredentialStore(complete=False),
            FakeIdentityProbe(),
        )
    assert not roots.launch_agent.exists()
    assert not roots.chrome_native_host.exists()


def test_conflicting_existing_surface_is_never_overwritten(tmp_path: Path) -> None:
    roots, evidence, signer = _fixture(tmp_path)
    _write(roots.launch_agent, b"user-owned", mode=0o600)

    with pytest.raises(AustinInstallRejected, match="austin_install_existing_mismatch"):
        _finalize(
            roots,
            evidence,
            signer,
            FakeCredentialStore(),
            FakeIdentityProbe(),
        )

    assert roots.launch_agent.read_bytes() == b"user-owned"
    assert not roots.chrome_native_host.exists()
    assert not evidence.inventory.exists()


def test_symlinked_parent_is_rejected_without_writing_through_it(tmp_path: Path) -> None:
    roots, evidence, signer = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    launch_parent = roots.launch_agent.parent
    launch_parent.parent.mkdir(parents=True, exist_ok=True)
    launch_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AustinInstallRejected, match="austin_install_directory"):
        _finalize(
            roots,
            evidence,
            signer,
            FakeCredentialStore(),
            FakeIdentityProbe(),
        )

    assert tuple(outside.iterdir()) == ()
    assert not evidence.inventory.exists()


def test_sealed_origin_mismatch_or_mutability_fails_before_surface_write(tmp_path: Path) -> None:
    roots, evidence, signer = _fixture(tmp_path)
    origin = roots.app_bundle / "Contents" / "Resources" / "NeonAllowedOrigin.txt"
    origin.chmod(0o666)

    with pytest.raises(AustinInstallRejected, match="austin_install_origin"):
        _finalize(
            roots,
            evidence,
            signer,
            FakeCredentialStore(),
            FakeIdentityProbe(),
        )

    assert not roots.launch_agent.exists()
    assert not roots.chrome_native_host.exists()


def test_forged_production_roots_are_rejected_before_identity_or_write(
    tmp_path: Path,
) -> None:
    roots, evidence, signer = _fixture(tmp_path)
    forged_roots = replace(roots, production=True)
    forged_evidence = replace(evidence, production=True)
    probe = FakeIdentityProbe()

    with pytest.raises(AustinInstallRejected, match="austin_install_context"):
        finalize_austin_install(
            roots=forged_roots,
            evidence_paths=forged_evidence,
            signer=signer,
            credential_store=FakeCredentialStore(),
            team_id=TEAM_ID,
            identity_probe=probe,
        )

    assert probe.calls == 0
    assert not roots.launch_agent.exists()
    assert not roots.chrome_native_host.exists()


def test_cli_preflights_app_identity_before_opening_credential_store(
    monkeypatch,
) -> None:
    from algo_cli import austin_install_finalizer as finalizer
    from algo_cli.oliver_control_installation import OliverUninstallRejected

    class RejectingProbe:
        def verify(self, *, roots: OliverInstallRoots, team_id: str) -> None:
            assert roots.production
            assert team_id == TEAM_ID
            raise OliverUninstallRejected("install_identity_rejected")

    opened = 0

    def open_store():
        nonlocal opened
        opened += 1
        raise AssertionError("credential store must not open before identity")

    monkeypatch.setattr(finalizer, "OliverMacOSReleaseIdentityProbe", RejectingProbe)
    monkeypatch.setattr(finalizer, "KeyringKeyStore", open_store)

    assert finalizer.main(["--team-id", TEAM_ID]) == 1
    assert opened == 0


def test_signed_helper_enumeration_is_identity_checked_before_and_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import austin_install_finalizer as finalizer

    roots, _evidence, _signer = _fixture(tmp_path)
    production_roots = replace(roots, production=True)
    monkeypatch.setattr(
        finalizer.OliverInstallRoots,
        "for_current_user",
        classmethod(lambda cls: production_roots),
    )
    requirement, requirement_digest = finalizer._credential_migrator_requirement(
        TEAM_ID
    )
    assert "credential-migrator" in requirement
    payload = AdaNativeCredentialEnumeration(
        service=KEYRING_SERVICE,
        nonce="a" * 64,
        generated_at_ms=NOW_MS,
        code_identifier="com.algo-cli.austin.credential-migrator",
        team_id=TEAM_ID,
        designated_requirement_digest=requirement_digest,
        registry_present=False,
        unexpected_label_count=0,
        records=(),
    ).to_bytes()

    class ProductionProbe:
        calls = 0

        def verify(self, *, roots: OliverInstallRoots, team_id: str) -> None:
            assert roots == production_roots
            assert team_id == TEAM_ID
            self.calls += 1

    probe = ProductionProbe()
    monkeypatch.setattr(
        AustinCredentialEnumerationRunner,
        "_run_bounded",
        staticmethod(lambda executable, request, timeout_seconds: payload),
    )
    runner = AustinCredentialEnumerationRunner(
        identity_probe=probe,
        clock_ms=lambda: NOW_MS,
    )

    evidence = runner.enumerate(
        roots=production_roots,
        team_id=TEAM_ID,
        nonce="a" * 64,
    )

    assert evidence.to_bytes() == payload
    assert probe.calls == 2


def test_credential_enumerator_bounds_output_and_nonzero_exit(tmp_path: Path) -> None:
    oversized = tmp_path / "AustinOversizedEnumerator"
    oversized.write_text(
        "#!/bin/sh\n/usr/bin/python3 -c 'import sys; sys.stdout.write(\"x\" * 70000)'\n",
        encoding="utf-8",
    )
    oversized.chmod(0o700)
    with pytest.raises(AustinInstallRejected, match="austin_install_credential_enumerator"):
        AustinCredentialEnumerationRunner._run_bounded(
            oversized,
            b"{}",
            timeout_seconds=5,
        )

    rejected = tmp_path / "AustinRejectedEnumerator"
    rejected.write_text("#!/bin/sh\nexit 78\n", encoding="utf-8")
    rejected.chmod(0o700)
    with pytest.raises(AustinInstallRejected, match="austin_install_credential_enumerator"):
        AustinCredentialEnumerationRunner._run_bounded(
            rejected,
            b"{}",
            timeout_seconds=5,
        )
