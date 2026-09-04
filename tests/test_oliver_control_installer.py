from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat

import pytest

from algo_cli import oliver_control_installer
from algo_cli.david_control_kernel import ControlSigner
from algo_cli.grace_key_store import KEYRING_SERVICE
from algo_cli.oliver_control_installation import (
    AUSTIN_APP_BUNDLE_ID,
    OLIVER_FIXED_CREDENTIAL_LABELS,
    OliverInstallRoots,
    OliverUninstallRejected,
    capture_oliver_install_inventory,
    expected_austin_launch_agent,
    expected_neon_native_host,
)
from algo_cli.oliver_control_installer import (
    OliverInstallEvidencePaths,
    OliverMacOSReleaseIdentityProbe,
    capture_and_publish_oliver_install_evidence,
    publish_oliver_install_evidence,
)


pytestmark = pytest.mark.skipif(os.name != "posix", reason="macOS installer evidence")

EXTENSION_ORIGIN = "chrome-extension://" + "c" * 32 + "/"
TEAM_ID = "ABCDE12345"
FIRST_INSTALL_ID = "00000000-0000-4000-8000-000000000201"
SECOND_INSTALL_ID = "00000000-0000-4000-8000-000000000202"


class FakeCredentialStore:
    service = KEYRING_SERVICE

    def __init__(self) -> None:
        self.values = {
            label: f"opaque-{index}"
            for index, label in enumerate(sorted(OLIVER_FIXED_CREDENTIAL_LABELS))
        }

    def fingerprint(self, label: str) -> str | None:
        value = self.values.get(label)
        if value is None:
            return None
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    def compare_and_delete(self, label: str, *, expected_digest: str) -> bool:
        if self.fingerprint(label) != expected_digest:
            return False
        self.values.pop(label, None)
        return True

    def complete_inventory_labels(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))

    def complete_inventory_snapshot(self) -> tuple[tuple[str, str | None], ...]:
        return tuple(
            (label, self.fingerprint(label)) for label in sorted(self.values)
        )


class FakeIdentityProbe:
    def __init__(self, *, rejection: str | None = None) -> None:
        self.rejection = rejection
        self.calls = 0

    def verify(self, *, roots: OliverInstallRoots, team_id: str) -> None:
        self.calls += 1
        assert not roots.production
        assert team_id == TEAM_ID
        if self.rejection is not None:
            raise OliverUninstallRejected(self.rejection)


class SimulatedInstallInterruption(BaseException):
    pass


def _write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _runtime(tmp_path: Path):
    uid = os.getuid()
    roots = OliverInstallRoots._for_test((tmp_path / "runtime").resolve(), uid=uid)
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
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
    )
    _write(
        roots.app_bundle / "Contents" / "MacOS" / "austin-control",
        b"austin-control",
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
        signer.verifier.public_bytes,
        mode=0o444,
    )
    _write(
        roots.app_bundle / "Contents" / "Resources" / "NeonAllowedOrigin.txt",
        EXTENSION_ORIGIN.encode("utf-8"),
        mode=0o444,
    )
    _write(roots.launch_agent, plistlib.dumps(expected_austin_launch_agent(roots)))
    _write(
        roots.chrome_native_host,
        json.dumps(
            expected_neon_native_host(roots, extension_origin=EXTENSION_ORIGIN),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    credentials = FakeCredentialStore()
    paths = OliverInstallEvidencePaths._for_test(
        (tmp_path / "evidence").resolve(), uid=uid
    )
    return roots, paths, signer, credentials


def _capture(
    roots: OliverInstallRoots,
    signer: ControlSigner,
    credentials: FakeCredentialStore,
    *,
    install_id: str,
    installed_at_ms: int,
):
    return capture_oliver_install_inventory(
        roots=roots,
        signer=signer,
        team_id=TEAM_ID,
        extension_origin=EXTENSION_ORIGIN,
        installed_at_ms=installed_at_ms,
        install_id=install_id,
        credential_store=credentials,
        credential_labels=OLIVER_FIXED_CREDENTIAL_LABELS,
        credential_inventory_complete=True,
        allow_test_roots=True,
    )


def test_post_install_publication_is_exact_atomic_and_idempotent(tmp_path: Path) -> None:
    roots, paths, signer, credentials = _runtime(tmp_path)
    probe = FakeIdentityProbe()

    inventory, publication = capture_and_publish_oliver_install_evidence(
        roots=roots,
        paths=paths,
        signer=signer,
        team_id=TEAM_ID,
        extension_origin=EXTENSION_ORIGIN,
        installed_at_ms=1_800_000_000_000,
        install_id=FIRST_INSTALL_ID,
        credential_store=credentials,
        credential_labels=OLIVER_FIXED_CREDENTIAL_LABELS,
        credential_inventory_complete=True,
        identity_probe=probe,
        allow_test_roots=True,
        allow_test_paths=True,
    )

    assert probe.calls == 1
    assert publication.status == "published"
    assert publication.inventory_digest == inventory.digest
    assert paths.inventory.read_bytes() == inventory.to_bytes()
    assert paths.authority.read_bytes() == signer.verifier.public_bytes
    assert stat.S_IMODE(paths.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.inventory.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.authority.stat().st_mode) == 0o444
    assert stat.S_IMODE(paths.lock.stat().st_mode) == 0o600

    unchanged = publish_oliver_install_evidence(
        inventory=inventory,
        signer=signer,
        paths=paths,
        allow_test_paths=True,
    )
    assert unchanged.status == "unchanged"
    assert not unchanged.replaced_previous_inventory


def test_interrupted_first_publication_is_safely_reconcilable(tmp_path: Path) -> None:
    roots, paths, signer, credentials = _runtime(tmp_path)

    def interrupt(stage: str) -> None:
        if stage == "authority_committed":
            raise SimulatedInstallInterruption

    with pytest.raises(SimulatedInstallInterruption):
        capture_and_publish_oliver_install_evidence(
            roots=roots,
            paths=paths,
            signer=signer,
            team_id=TEAM_ID,
            extension_origin=EXTENSION_ORIGIN,
            installed_at_ms=1_800_000_000_000,
            install_id=FIRST_INSTALL_ID,
            credential_store=credentials,
            credential_labels=OLIVER_FIXED_CREDENTIAL_LABELS,
            credential_inventory_complete=True,
            identity_probe=FakeIdentityProbe(),
            allow_test_roots=True,
            allow_test_paths=True,
            fault_hook=interrupt,
        )

    assert paths.authority.read_bytes() == signer.verifier.public_bytes
    assert not paths.inventory.exists()
    inventory = _capture(
        roots,
        signer,
        credentials,
        install_id=FIRST_INSTALL_ID,
        installed_at_ms=1_800_000_000_000,
    )
    publication = publish_oliver_install_evidence(
        inventory=inventory,
        signer=signer,
        paths=paths,
        allow_test_paths=True,
    )
    assert publication.status == "published"
    assert paths.inventory.read_bytes() == inventory.to_bytes()


def test_only_a_newer_distinct_inventory_can_replace_prior_evidence(tmp_path: Path) -> None:
    roots, paths, signer, credentials = _runtime(tmp_path)
    first = _capture(
        roots,
        signer,
        credentials,
        install_id=FIRST_INSTALL_ID,
        installed_at_ms=1_800_000_000_000,
    )
    publish_oliver_install_evidence(
        inventory=first,
        signer=signer,
        paths=paths,
        allow_test_paths=True,
    )
    second = _capture(
        roots,
        signer,
        credentials,
        install_id=SECOND_INSTALL_ID,
        installed_at_ms=1_800_000_000_001,
    )
    replaced = publish_oliver_install_evidence(
        inventory=second,
        signer=signer,
        paths=paths,
        allow_test_paths=True,
    )
    assert replaced.status == "published"
    assert replaced.replaced_previous_inventory
    assert paths.inventory.read_bytes() == second.to_bytes()

    with pytest.raises(OliverUninstallRejected, match="install_evidence_stale"):
        publish_oliver_install_evidence(
            inventory=first,
            signer=signer,
            paths=paths,
            allow_test_paths=True,
        )


@pytest.mark.parametrize(
    ("version", "build"),
    [("0.17.9", "1900"), ("0.18.1", "1700")],
)
def test_signed_inventory_rejects_unapproved_version_or_build_rollback(
    tmp_path: Path,
    version: str,
    build: str,
) -> None:
    roots, paths, signer, credentials = _runtime(tmp_path)
    current = _capture(
        roots,
        signer,
        credentials,
        install_id=FIRST_INSTALL_ID,
        installed_at_ms=1_800_000_000_000,
    )
    publish_oliver_install_evidence(
        inventory=current,
        signer=signer,
        paths=paths,
        allow_test_paths=True,
    )
    _write(
        roots.app_bundle / "Contents" / "Info.plist",
        plistlib.dumps(
            {
                "CFBundleExecutable": "austin-control",
                "CFBundleIdentifier": AUSTIN_APP_BUNDLE_ID,
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": build,
            }
        ),
    )
    rollback = _capture(
        roots,
        signer,
        credentials,
        install_id=SECOND_INSTALL_ID,
        installed_at_ms=1_800_000_000_001,
    )

    with pytest.raises(
        OliverUninstallRejected, match="install_evidence_version_rollback"
    ):
        publish_oliver_install_evidence(
            inventory=rollback,
            signer=signer,
            paths=paths,
            allow_test_paths=True,
        )

    assert paths.inventory.read_bytes() == current.to_bytes()


@pytest.mark.parametrize("tamper", ["inventory", "authority"])
def test_existing_evidence_tampering_is_never_overwritten(
    tmp_path: Path, tamper: str
) -> None:
    roots, paths, signer, credentials = _runtime(tmp_path)
    inventory = _capture(
        roots,
        signer,
        credentials,
        install_id=FIRST_INSTALL_ID,
        installed_at_ms=1_800_000_000_000,
    )
    publish_oliver_install_evidence(
        inventory=inventory,
        signer=signer,
        paths=paths,
        allow_test_paths=True,
    )
    target = paths.inventory if tamper == "inventory" else paths.authority
    target.chmod(0o600)
    target.write_bytes(b"{}" if tamper == "inventory" else bytes(reversed(range(32))))
    target.chmod(0o600 if tamper == "inventory" else 0o444)

    reason = (
        "install_evidence_inventory_invalid"
        if tamper == "inventory"
        else "install_evidence_authority_changed"
    )
    with pytest.raises(OliverUninstallRejected, match=reason):
        publish_oliver_install_evidence(
            inventory=inventory,
            signer=signer,
            paths=paths,
            allow_test_paths=True,
        )


def test_symlinked_state_directory_and_identity_rejection_create_no_evidence(
    tmp_path: Path,
) -> None:
    roots, paths, signer, credentials = _runtime(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    paths.directory.symlink_to(elsewhere, target_is_directory=True)
    inventory = _capture(
        roots,
        signer,
        credentials,
        install_id=FIRST_INSTALL_ID,
        installed_at_ms=1_800_000_000_000,
    )
    with pytest.raises(OliverUninstallRejected, match="install_evidence_directory"):
        publish_oliver_install_evidence(
            inventory=inventory,
            signer=signer,
            paths=paths,
            allow_test_paths=True,
        )
    assert not (elsewhere / paths.inventory.name).exists()

    paths.directory.unlink()
    rejected_paths = OliverInstallEvidencePaths._for_test(
        (tmp_path / "rejected-evidence").resolve(), uid=os.getuid()
    )
    with pytest.raises(OliverUninstallRejected, match="identity_rejected"):
        capture_and_publish_oliver_install_evidence(
            roots=roots,
            paths=rejected_paths,
            signer=signer,
            team_id=TEAM_ID,
            extension_origin=EXTENSION_ORIGIN,
            installed_at_ms=1_800_000_000_000,
            install_id=FIRST_INSTALL_ID,
            credential_store=credentials,
            credential_labels=OLIVER_FIXED_CREDENTIAL_LABELS,
            credential_inventory_complete=True,
            identity_probe=FakeIdentityProbe(rejection="identity_rejected"),
            allow_test_roots=True,
            allow_test_paths=True,
        )
    assert not rejected_paths.directory.exists()


def test_install_evidence_lock_contention_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    roots, paths, signer, credentials = _runtime(tmp_path)
    inventory = _capture(
        roots,
        signer,
        credentials,
        install_id=FIRST_INSTALL_ID,
        installed_at_ms=1_800_000_000_000,
    )
    publish_oliver_install_evidence(
        inventory=inventory,
        signer=signer,
        paths=paths,
        allow_test_paths=True,
    )
    descriptor = os.open(paths.lock, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(oliver_control_installer, "_LOCK_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(oliver_control_installer, "_LOCK_RETRY_SECONDS", 0.001)
    try:
        with pytest.raises(OliverUninstallRejected, match="install_evidence_busy"):
            publish_oliver_install_evidence(
                inventory=inventory,
                signer=signer,
                paths=paths,
                allow_test_paths=True,
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_release_identity_probe_requires_exact_runtime_team_and_entitlements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = OliverInstallRoots.for_current_user()
    monkeypatch.setattr(oliver_control_installer.sys, "platform", "darwin")

    class FixtureProbe(OliverMacOSReleaseIdentityProbe):
        def __init__(self, *, include_runtime: bool) -> None:
            super().__init__()
            self.include_runtime = include_runtime

        @staticmethod
        def _verify_production_path(label: str, path: Path) -> None:
            assert label in {
                "bundle",
                "app",
                "relay",
                "adapter",
                "credential_migrator",
                "neon",
            }

        def _run(self, command: tuple[str, ...]) -> str:
            if "--entitlements" in command:
                path = command[-1]
                if path.endswith("austin-tcc-adapter"):
                    entitlements = {
                        "com.apple.security.automation.apple-events": True
                    }
                elif path.endswith(("neon-native-host", "austin-credential-migrator")):
                    entitlements = {}
                else:
                    entitlements = {
                        "com.apple.security.app-sandbox": True,
                        "com.apple.security.application-groups": [
                            "group.com.algo-cli.control"
                        ],
                    }
                return plistlib.dumps(entitlements).decode("utf-8")
            if "--verbose=4" in command and "-d" in command:
                path = command[-1]
                if path.endswith("austin-relay"):
                    identifier = "com.algo-cli.austin.relay"
                elif path.endswith("austin-tcc-adapter"):
                    identifier = "com.algo-cli.austin.tcc-adapter"
                elif path.endswith("austin-credential-migrator"):
                    identifier = "com.algo-cli.austin.credential-migrator"
                elif path.endswith("neon-native-host"):
                    identifier = "com.algo-cli.neon.host"
                else:
                    identifier = "com.algo-cli.austin.control"
                flags = "0x10000(runtime)" if self.include_runtime else "0x0(none)"
                return (
                    f"Identifier={identifier}\n"
                    f"CodeDirectory v=20500 flags={flags} hashes=1+1\n"
                    "Authority=Developer ID Application: Algo CLI (ABCDE12345)\n"
                    "Authority=Developer ID Certification Authority\n"
                    "Authority=Apple Root CA\n"
                    "TeamIdentifier=ABCDE12345\n"
                )
            return ""

    FixtureProbe(include_runtime=True).verify(roots=roots, team_id=TEAM_ID)
    with pytest.raises(OliverUninstallRejected, match="install_identity_mismatch"):
        FixtureProbe(include_runtime=False).verify(roots=roots, team_id=TEAM_ID)
