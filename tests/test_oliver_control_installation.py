from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

from algo_cli.alice_artifact_store import MASTER_KEY_LABEL
from algo_cli.ada_credential_registry import ADA_CREDENTIAL_REGISTRY_LABEL
from algo_cli.ada_uninstall_recovery import AdaUninstallRecoveryStore
from algo_cli.david_control_kernel import ControlSigner
from algo_cli.grace_key_store import (
    CONTROL_SIGNING_KEY_LABEL,
    KEYRING_SERVICE,
    RECEIPT_ANCHOR_LABEL_PREFIX,
)
from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL
from algo_cli import oliver_control_uninstall as uninstall_cli
from algo_cli.oliver_control_installation import (
    AUSTIN_APP_BUNDLE_ID,
    OLIVER_FIXED_CREDENTIAL_LABELS,
    OliverInstallInventory,
    OliverInstallRoots,
    OliverReceiptOutcome,
    OliverUninstallMode,
    OliverUninstallRejected,
    capture_oliver_install_inventory,
    execute_oliver_uninstall,
    expected_austin_launch_agent,
    expected_neon_native_host,
    plan_oliver_uninstall,
    resume_oliver_uninstall,
)


EXTENSION_ORIGIN = "chrome-extension://" + "a" * 32 + "/"
TEAM_ID = "ABCDE12345"
INSTALL_ID = "00000000-0000-4000-8000-000000000101"
ANCHOR_LABEL = RECEIPT_ANCHOR_LABEL_PREFIX + "b" * 64

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Oliver native-control installation uses POSIX ownership and locking",
)
ROOT = Path(__file__).resolve().parents[1]


class FakeCredentialStore:
    service = KEYRING_SERVICE

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.registered = set(self.values)
        self.deleted_labels: list[str] = []

    def fingerprint(self, label: str) -> str | None:
        value = self.values.get(label)
        if value is None:
            return None
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    def compare_and_delete(self, label: str, *, expected_digest: str) -> bool:
        observed = self.fingerprint(label)
        if observed != expected_digest:
            return False
        self.values.pop(label, None)
        self.deleted_labels.append(label)
        return True

    def complete_inventory_labels(self) -> tuple[str, ...]:
        return tuple(sorted(self.registered))

    def complete_inventory_snapshot(self) -> tuple[tuple[str, str | None], ...]:
        return tuple(
            (label, self.fingerprint(label)) for label in sorted(self.registered)
        )


class FakeLaunchController:
    def __init__(self, state: str = "absent", *, bootout_failure: bool = False) -> None:
        self.current = state
        self.bootout_failure = bootout_failure
        self.bootout_calls = 0

    def state(self, *, uid: int, label: str) -> str:
        assert uid >= 0
        assert label
        return self.current

    def bootout(self, *, uid: int, label: str) -> None:
        self.bootout_calls += 1
        if self.bootout_failure:
            raise OliverUninstallRejected("launchctl_bootout_failed")
        self.current = "absent"


class StoppedProcessProbe:
    def assert_stopped(self, executable_paths) -> None:
        assert len(tuple(executable_paths)) == 5


class SimulatedPowerLoss(BaseException):
    pass


def _write(path: Path, value: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(mode)


def _fixture(tmp_path: Path):
    uid = os.getuid() if hasattr(os, "getuid") else 0
    roots = OliverInstallRoots._for_test(tmp_path.resolve(), uid=uid)
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
    info = {
        "CFBundleExecutable": "austin-control",
        "CFBundleIdentifier": AUSTIN_APP_BUNDLE_ID,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.18.0",
        "CFBundleVersion": "1800",
    }
    _write(roots.app_bundle / "Contents" / "Info.plist", plistlib.dumps(info))
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
    credentials = FakeCredentialStore(
        {
            label: f"opaque-{index}"
            for index, label in enumerate(sorted((*OLIVER_FIXED_CREDENTIAL_LABELS, ANCHOR_LABEL)))
        }
    )
    inventory = capture_oliver_install_inventory(
        roots=roots,
        signer=signer,
        team_id=TEAM_ID,
        extension_origin=EXTENSION_ORIGIN,
        installed_at_ms=1_800_000_000_000,
        install_id=INSTALL_ID,
        credential_store=credentials,
        credential_labels=(*OLIVER_FIXED_CREDENTIAL_LABELS, ANCHOR_LABEL),
        credential_inventory_complete=True,
        allow_test_roots=True,
    )
    return roots, signer, credentials, inventory


def _plan(
    roots: OliverInstallRoots,
    signer: ControlSigner,
    credentials: FakeCredentialStore,
    inventory: OliverInstallInventory,
    *,
    mode: OliverUninstallMode = OliverUninstallMode.RUNTIME_ONLY,
    launch: FakeLaunchController | None = None,
):
    controller = launch or FakeLaunchController()
    return plan_oliver_uninstall(
        inventory=inventory,
        verifier=signer.verifier,
        roots=roots,
        mode=mode,
        launch_controller=controller,
        process_probe=StoppedProcessProbe(),
        credential_store=(
            credentials if mode is OliverUninstallMode.PURGE_PRIVATE_STATE else None
        ),
        allow_test_roots=True,
    )


def _recovery_store(tmp_path: Path) -> AdaUninstallRecoveryStore:
    parent = tmp_path / "AdaRecovery"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return AdaUninstallRecoveryStore(
        parent / "AdaUninstallRecovery.json",
        uid=uid,
    )


def test_signed_inventory_is_canonical_content_free_and_round_trips(tmp_path: Path) -> None:
    roots, signer, _credentials, inventory = _fixture(tmp_path)

    inventory.verify(signer.verifier)
    assert OliverInstallInventory.from_bytes(inventory.to_bytes()) == inventory
    assert inventory.app_bundle_id == AUSTIN_APP_BUNDLE_ID
    assert inventory.schema_version == 2
    assert inventory.app_version == "0.18.0"
    assert inventory.app_build_number == "1800"
    assert len(inventory.entries) >= 10
    payload = inventory.to_bytes().decode("utf-8")
    assert str(roots.home) not in payload
    assert str(roots.app_bundle) not in payload
    assert "opaque-" not in payload


def test_private_state_inventory_labels_match_their_owning_modules() -> None:
    assert MASTER_KEY_LABEL in OLIVER_FIXED_CREDENTIAL_LABELS
    assert PRIVACY_KEY_LABEL in OLIVER_FIXED_CREDENTIAL_LABELS


def test_complete_credential_inventory_requires_authoritative_enumeration(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, _inventory = _fixture(tmp_path)
    labels = tuple(sorted(OLIVER_FIXED_CREDENTIAL_LABELS))
    credentials.values["untracked-anchor"] = "opaque"
    credentials.registered.add("untracked-anchor")
    with pytest.raises(OliverUninstallRejected, match="credential_inventory_incomplete"):
        capture_oliver_install_inventory(
            roots=roots,
            signer=signer,
            team_id=TEAM_ID,
            extension_origin=EXTENSION_ORIGIN,
            installed_at_ms=1_800_000_000_001,
            install_id="00000000-0000-4000-8000-000000000104",
            credential_store=credentials,
            credential_labels=labels,
            credential_inventory_complete=True,
            allow_test_roots=True,
        )

    class UnenumerableCredentialStore(FakeCredentialStore):
        def complete_inventory_labels(self) -> None:
            return None

        def complete_inventory_snapshot(self) -> None:
            return None

    unenumerable = UnenumerableCredentialStore(credentials.values)
    with pytest.raises(OliverUninstallRejected, match="credential_inventory_unprovable"):
        capture_oliver_install_inventory(
            roots=roots,
            signer=signer,
            team_id=TEAM_ID,
            extension_origin=EXTENSION_ORIGIN,
            installed_at_ms=1_800_000_000_001,
            install_id="00000000-0000-4000-8000-000000000105",
            credential_store=unenumerable,
            credential_labels=tuple(sorted(unenumerable.values)),
            credential_inventory_complete=True,
            allow_test_roots=True,
        )


def test_inventory_capture_requires_a_finite_sealed_native_authority_key(tmp_path: Path) -> None:
    roots, signer, credentials, _inventory = _fixture(tmp_path)
    authority = (
        roots.app_bundle
        / "Contents"
        / "Resources"
        / "AustinAuthorityPublicKey.bin"
    )
    authority.chmod(0o644)
    _write(authority, b"short", mode=0o444)

    with pytest.raises(OliverUninstallRejected, match="installed_authority_key"):
        capture_oliver_install_inventory(
            roots=roots,
            signer=signer,
            team_id=TEAM_ID,
            extension_origin=EXTENSION_ORIGIN,
            installed_at_ms=1_800_000_000_001,
            install_id="00000000-0000-4000-8000-000000000102",
            credential_store=credentials,
            credential_labels=(*OLIVER_FIXED_CREDENTIAL_LABELS, ANCHOR_LABEL),
            credential_inventory_complete=True,
            allow_test_roots=True,
        )


def test_install_inventory_authority_is_independent_from_disabled_native_key(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, _inventory = _fixture(tmp_path)
    authority = (
        roots.app_bundle
        / "Contents"
        / "Resources"
        / "AustinAuthorityPublicKey.bin"
    )
    authority.chmod(0o644)
    disabled_native = ControlSigner.from_private_bytes(bytes(reversed(range(32))))
    _write(authority, disabled_native.verifier.public_bytes, mode=0o444)

    inventory = capture_oliver_install_inventory(
        roots=roots,
        signer=signer,
        team_id=TEAM_ID,
        extension_origin=EXTENSION_ORIGIN,
        installed_at_ms=1_800_000_000_001,
        install_id="00000000-0000-4000-8000-000000000107",
        credential_store=credentials,
        credential_labels=(*OLIVER_FIXED_CREDENTIAL_LABELS, ANCHOR_LABEL),
        credential_inventory_complete=True,
        allow_test_roots=True,
    )

    inventory.verify(signer.verifier)
    assert inventory.authority_key_id == signer.key_id
    assert inventory.authority_key_id != disabled_native.key_id


def test_inventory_capture_requires_every_declared_runtime_executable(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, _inventory = _fixture(tmp_path)
    (roots.app_bundle / "Contents" / "Helpers" / "neon-native-host").unlink()

    with pytest.raises(OliverUninstallRejected, match="installed_runtime_file"):
        capture_oliver_install_inventory(
            roots=roots,
            signer=signer,
            team_id=TEAM_ID,
            extension_origin=EXTENSION_ORIGIN,
            installed_at_ms=1_800_000_000_001,
            install_id="00000000-0000-4000-8000-000000000103",
            credential_store=credentials,
            credential_labels=(*OLIVER_FIXED_CREDENTIAL_LABELS, ANCHOR_LABEL),
            credential_inventory_complete=True,
            allow_test_roots=True,
        )


def test_inventory_capture_binds_the_sealed_neon_origin(tmp_path: Path) -> None:
    roots, signer, credentials, _inventory = _fixture(tmp_path)
    allowed_origin = (
        roots.app_bundle / "Contents" / "Resources" / "NeonAllowedOrigin.txt"
    )
    allowed_origin.chmod(0o644)
    _write(
        allowed_origin,
        ("chrome-extension://" + "b" * 32 + "/").encode("utf-8"),
        mode=0o444,
    )

    with pytest.raises(OliverUninstallRejected, match="installed_native_host_origin"):
        capture_oliver_install_inventory(
            roots=roots,
            signer=signer,
            team_id=TEAM_ID,
            extension_origin=EXTENSION_ORIGIN,
            installed_at_ms=1_800_000_000_001,
            install_id="00000000-0000-4000-8000-000000000106",
            credential_store=credentials,
            credential_labels=(*OLIVER_FIXED_CREDENTIAL_LABELS, ANCHOR_LABEL),
            credential_inventory_complete=True,
            allow_test_roots=True,
        )


def test_inventory_signature_and_closed_schema_reject_tampering(tmp_path: Path) -> None:
    _roots, signer, _credentials, inventory = _fixture(tmp_path)
    changed = inventory.to_dict()
    changed["team_id"] = "ZZZZZ99999"
    forged = OliverInstallInventory.from_dict(changed)

    with pytest.raises(OliverUninstallRejected, match="inventory_signature"):
        forged.verify(signer.verifier)

    extra = inventory.to_dict()
    extra["private_path"] = "/Users/example"
    with pytest.raises(OliverUninstallRejected, match="inventory_schema"):
        OliverInstallInventory.from_dict(extra)


def test_dry_run_is_deterministic_and_non_mutating(tmp_path: Path) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    before = inventory.to_bytes()

    first = _plan(roots, signer, credentials, inventory)
    second = _plan(roots, signer, credentials, inventory)

    assert first == second
    assert first.digest == second.digest
    assert first.confirmation_phrase.startswith("UNINSTALL ALGO CLI CONTROL ")
    assert roots.app_bundle.exists()
    assert roots.launch_agent.exists()
    assert roots.chrome_native_host.exists()
    assert inventory.to_bytes() == before
    assert credentials.values


def test_runtime_uninstall_removes_only_signed_surface_and_preserves_private_state(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    launch = FakeLaunchController("loaded")
    plan = _plan(roots, signer, credentials, inventory, launch=launch)
    times = iter((1_800_000_000_100, 1_800_000_000_101))

    receipt = execute_oliver_uninstall(
        inventory=inventory,
        signer=signer,
        roots=roots,
        mode=OliverUninstallMode.RUNTIME_ONLY,
        expected_plan_digest=plan.digest,
        confirmation=plan.confirmation_phrase,
        launch_controller=launch,
        process_probe=StoppedProcessProbe(),
        clock_ms=lambda: next(times),
        allow_test_roots=True,
    )

    receipt.verify(signer.verifier)
    assert receipt.payload["outcome"] == OliverReceiptOutcome.COMPLETED.value
    assert receipt.payload["deleted_entry_count"] == len(inventory.entries)
    assert launch.bootout_calls == 1
    assert not roots.app_bundle.exists()
    assert not roots.launch_agent.exists()
    assert not roots.chrome_native_host.exists()
    assert set(credentials.values) == {
        *OLIVER_FIXED_CREDENTIAL_LABELS,
        ANCHOR_LABEL,
    }


def test_private_state_purge_deletes_only_exact_signed_labels_and_signer_last(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    recovery = _recovery_store(tmp_path)
    credentials.values["unrelated-service-item"] = "must-survive"
    plan = _plan(
        roots,
        signer,
        credentials,
        inventory,
        mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
    )
    times = iter((1_800_000_000_200, 1_800_000_000_201))

    receipt = execute_oliver_uninstall(
        inventory=inventory,
        signer=signer,
        roots=roots,
        mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
        expected_plan_digest=plan.digest,
        confirmation=plan.confirmation_phrase,
        launch_controller=FakeLaunchController(),
        process_probe=StoppedProcessProbe(),
        credential_store=credentials,
        clock_ms=lambda: next(times),
        allow_test_roots=True,
        recovery_store=recovery,
    )

    receipt.verify(signer.verifier)
    assert receipt.payload["outcome"] == OliverReceiptOutcome.COMPLETED.value
    assert receipt.payload["deleted_credential_count"] == len(inventory.credentials)
    assert credentials.values == {"unrelated-service-item": "must-survive"}
    assert CONTROL_SIGNING_KEY_LABEL not in credentials.values
    assert credentials.deleted_labels[-2:] == [
        ADA_CREDENTIAL_REGISTRY_LABEL,
        CONTROL_SIGNING_KEY_LABEL,
    ]
    record = recovery.load()
    assert record is not None and record.phase == "commit_ready"
    assert record.terminal_receipt == receipt.to_dict()


def test_private_state_purge_requires_durable_recovery_before_mutation(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    plan = _plan(
        roots,
        signer,
        credentials,
        inventory,
        mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
    )

    with pytest.raises(OliverUninstallRejected, match="uninstall_recovery_required"):
        execute_oliver_uninstall(
            inventory=inventory,
            signer=signer,
            roots=roots,
            mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
            expected_plan_digest=plan.digest,
            confirmation=plan.confirmation_phrase,
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            credential_store=credentials,
            clock_ms=lambda: 1_800_000_000_205,
            allow_test_roots=True,
        )

    assert roots.app_bundle.exists()
    assert set(credentials.values) == {
        *OLIVER_FIXED_CREDENTIAL_LABELS,
        ANCHOR_LABEL,
    }


def test_recovery_record_precedes_mutation_and_terminal_receipt_is_durable(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    recovery = _recovery_store(tmp_path)
    plan = _plan(roots, signer, credentials, inventory)
    times = iter((1_800_000_000_210, 1_800_000_000_211))
    stages: list[str] = []

    receipt = execute_oliver_uninstall(
        inventory=inventory,
        signer=signer,
        roots=roots,
        mode=OliverUninstallMode.RUNTIME_ONLY,
        expected_plan_digest=plan.digest,
        confirmation=plan.confirmation_phrase,
        launch_controller=FakeLaunchController(),
        process_probe=StoppedProcessProbe(),
        clock_ms=lambda: next(times),
        allow_test_roots=True,
        recovery_store=recovery,
        fault_injector=stages.append,
    )

    record = recovery.load()
    assert record is not None
    record.verify(signer.verifier)
    assert record.phase == "terminal"
    assert stages[0] == "recovery_authorized"
    assert stages[-1] == "terminal_recovery_published"
    assert record.terminal_receipt == receipt.to_dict()
    recovered = resume_oliver_uninstall(
        inventory=inventory,
        verifier=signer.verifier,
        signer=None,
        roots=roots,
        recovery_store=recovery,
        launch_controller=FakeLaunchController(),
        process_probe=StoppedProcessProbe(),
        credential_store=None,
        clock_ms=lambda: 1_800_000_000_212,
        allow_test_roots=True,
    )
    assert recovered == receipt


def test_runtime_power_loss_resumes_only_the_signed_remaining_surface(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    recovery = _recovery_store(tmp_path)
    plan = _plan(roots, signer, credentials, inventory)

    def fail_after_first_entry(stage: str) -> None:
        if stage == "entry_deleted":
            raise SimulatedPowerLoss

    with pytest.raises(SimulatedPowerLoss):
        execute_oliver_uninstall(
            inventory=inventory,
            signer=signer,
            roots=roots,
            mode=OliverUninstallMode.RUNTIME_ONLY,
            expected_plan_digest=plan.digest,
            confirmation=plan.confirmation_phrase,
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            clock_ms=lambda: 1_800_000_000_220,
            allow_test_roots=True,
            recovery_store=recovery,
            fault_injector=fail_after_first_entry,
        )

    record = recovery.load()
    assert record is not None and record.phase == "authorized"
    receipt = resume_oliver_uninstall(
        inventory=inventory,
        verifier=signer.verifier,
        signer=signer,
        roots=roots,
        recovery_store=recovery,
        launch_controller=FakeLaunchController(),
        process_probe=StoppedProcessProbe(),
        credential_store=None,
        clock_ms=lambda: 1_800_000_000_221,
        allow_test_roots=True,
    )
    assert receipt.payload["outcome"] == OliverReceiptOutcome.COMPLETED.value
    assert receipt.payload["deleted_entry_count"] == len(record.present_entry_ids)
    assert not roots.app_bundle.exists()
    assert set(credentials.values) == {
        *OLIVER_FIXED_CREDENTIAL_LABELS,
        ANCHOR_LABEL,
    }


def test_private_purge_resumes_after_registry_loss_while_signer_is_last(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    recovery = _recovery_store(tmp_path)
    plan = _plan(
        roots,
        signer,
        credentials,
        inventory,
        mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
    )

    def fail_after_registry(stage: str) -> None:
        if (
            stage == "credential_deleted"
            and credentials.deleted_labels[-1] == ADA_CREDENTIAL_REGISTRY_LABEL
        ):
            raise SimulatedPowerLoss

    with pytest.raises(SimulatedPowerLoss):
        execute_oliver_uninstall(
            inventory=inventory,
            signer=signer,
            roots=roots,
            mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
            expected_plan_digest=plan.digest,
            confirmation=plan.confirmation_phrase,
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            credential_store=credentials,
            clock_ms=lambda: 1_800_000_000_230,
            allow_test_roots=True,
            recovery_store=recovery,
            fault_injector=fail_after_registry,
        )

    assert ADA_CREDENTIAL_REGISTRY_LABEL not in credentials.values
    assert CONTROL_SIGNING_KEY_LABEL in credentials.values
    record = recovery.load()
    assert record is not None and record.phase == "authorized"
    receipt = resume_oliver_uninstall(
        inventory=inventory,
        verifier=signer.verifier,
        signer=signer,
        roots=roots,
        recovery_store=recovery,
        launch_controller=FakeLaunchController(),
        process_probe=StoppedProcessProbe(),
        credential_store=credentials,
        clock_ms=lambda: 1_800_000_000_231,
        allow_test_roots=True,
    )
    assert receipt.payload["outcome"] == OliverReceiptOutcome.COMPLETED.value
    assert not credentials.values
    terminal = recovery.load()
    assert terminal is not None and terminal.phase == "commit_ready"
    assert resume_oliver_uninstall(
        inventory=inventory,
        verifier=signer.verifier,
        signer=None,
        roots=roots,
        recovery_store=recovery,
        launch_controller=FakeLaunchController(),
        process_probe=StoppedProcessProbe(),
        credential_store=credentials,
        clock_ms=lambda: 1_800_000_000_232,
        allow_test_roots=True,
    ) == receipt


def test_power_loss_after_final_signer_delete_recovers_presigned_receipt(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    recovery = _recovery_store(tmp_path)
    plan = _plan(
        roots,
        signer,
        credentials,
        inventory,
        mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
    )

    def fail_after_signer(stage: str) -> None:
        if (
            stage == "credential_deleted"
            and credentials.deleted_labels[-1] == CONTROL_SIGNING_KEY_LABEL
        ):
            raise SimulatedPowerLoss

    with pytest.raises(SimulatedPowerLoss):
        execute_oliver_uninstall(
            inventory=inventory,
            signer=signer,
            roots=roots,
            mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
            expected_plan_digest=plan.digest,
            confirmation=plan.confirmation_phrase,
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            credential_store=credentials,
            clock_ms=lambda: 1_800_000_000_240,
            allow_test_roots=True,
            recovery_store=recovery,
            fault_injector=fail_after_signer,
        )

    assert not credentials.values
    record = recovery.load()
    assert record is not None and record.phase == "commit_ready"
    receipt = resume_oliver_uninstall(
        inventory=inventory,
        verifier=signer.verifier,
        signer=None,
        roots=roots,
        recovery_store=recovery,
        launch_controller=FakeLaunchController(),
        process_probe=StoppedProcessProbe(),
        credential_store=credentials,
        clock_ms=lambda: 1_800_000_000_241,
        allow_test_roots=True,
    )
    assert receipt.to_dict() == record.terminal_receipt
    assert receipt.payload["outcome"] == OliverReceiptOutcome.COMPLETED.value


@pytest.mark.parametrize("changed_label", [MASTER_KEY_LABEL, CONTROL_SIGNING_KEY_LABEL])
def test_commit_ready_rejects_reappeared_or_changed_credentials(
    tmp_path: Path,
    changed_label: str,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    original = dict(credentials.values)
    recovery = _recovery_store(tmp_path)
    plan = _plan(
        roots,
        signer,
        credentials,
        inventory,
        mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
    )

    def fail_after_commit(stage: str) -> None:
        if stage == "commit_ready_published":
            raise SimulatedPowerLoss

    with pytest.raises(SimulatedPowerLoss):
        execute_oliver_uninstall(
            inventory=inventory,
            signer=signer,
            roots=roots,
            mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
            expected_plan_digest=plan.digest,
            confirmation=plan.confirmation_phrase,
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            credential_store=credentials,
            clock_ms=lambda: 1_800_000_000_245,
            allow_test_roots=True,
            recovery_store=recovery,
            fault_injector=fail_after_commit,
        )

    record = recovery.load()
    assert record is not None and record.phase == "commit_ready"
    if changed_label == CONTROL_SIGNING_KEY_LABEL:
        credentials.values[changed_label] = "changed-control-key"
        expected_reason = "credential_changed"
    else:
        credentials.values[changed_label] = original[changed_label]
        expected_reason = "uninstall_recovery_commit_state"
    with pytest.raises(OliverUninstallRejected, match=expected_reason):
        resume_oliver_uninstall(
            inventory=inventory,
            verifier=signer.verifier,
            signer=None,
            roots=roots,
            recovery_store=recovery,
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            credential_store=credentials,
            clock_ms=lambda: 1_800_000_000_246,
            allow_test_roots=True,
        )


@pytest.mark.parametrize(
    "mode",
    [OliverUninstallMode.RUNTIME_ONLY, OliverUninstallMode.PURGE_PRIVATE_STATE],
)
def test_power_loss_matrix_covers_every_durable_uninstall_boundary(
    tmp_path: Path,
    mode: OliverUninstallMode,
) -> None:
    baseline_root = tmp_path / "baseline"
    roots, signer, credentials, inventory = _fixture(baseline_root)
    recovery = _recovery_store(baseline_root)
    plan = _plan(roots, signer, credentials, inventory, mode=mode)
    stages: list[str] = []
    execute_oliver_uninstall(
        inventory=inventory,
        signer=signer,
        roots=roots,
        mode=mode,
        expected_plan_digest=plan.digest,
        confirmation=plan.confirmation_phrase,
        launch_controller=FakeLaunchController(),
        process_probe=StoppedProcessProbe(),
        credential_store=(
            credentials if mode is OliverUninstallMode.PURGE_PRIVATE_STATE else None
        ),
        clock_ms=lambda: 1_800_000_000_250,
        allow_test_roots=True,
        recovery_store=recovery,
        fault_injector=stages.append,
    )
    assert stages[0] == "recovery_authorized"
    if mode is OliverUninstallMode.RUNTIME_ONLY:
        assert stages[-2:] == [
            "before_terminal_recovery",
            "terminal_recovery_published",
        ]
    else:
        assert stages[-3:] == [
            "commit_ready_published",
            "credential_deleted",
            "control_signer_deleted",
        ]

    for cutoff in range(len(stages)):
        case_root = tmp_path / f"case-{mode.value}-{cutoff}"
        roots, signer, credentials, inventory = _fixture(case_root)
        recovery = _recovery_store(case_root)
        plan = _plan(roots, signer, credentials, inventory, mode=mode)
        observed = 0

        def fail_at_cutoff(_stage: str) -> None:
            nonlocal observed
            current = observed
            observed += 1
            if current == cutoff:
                raise SimulatedPowerLoss

        with pytest.raises(SimulatedPowerLoss):
            execute_oliver_uninstall(
                inventory=inventory,
                signer=signer,
                roots=roots,
                mode=mode,
                expected_plan_digest=plan.digest,
                confirmation=plan.confirmation_phrase,
                launch_controller=FakeLaunchController(),
                process_probe=StoppedProcessProbe(),
                credential_store=(
                    credentials
                    if mode is OliverUninstallMode.PURGE_PRIVATE_STATE
                    else None
                ),
                clock_ms=lambda: 1_800_000_000_251,
                allow_test_roots=True,
                recovery_store=recovery,
                fault_injector=fail_at_cutoff,
            )
        record = recovery.load()
        assert record is not None
        signer_available = (
            mode is OliverUninstallMode.RUNTIME_ONLY
            or CONTROL_SIGNING_KEY_LABEL in credentials.values
        )
        receipt = resume_oliver_uninstall(
            inventory=inventory,
            verifier=signer.verifier,
            signer=(signer if signer_available else None),
            roots=roots,
            recovery_store=recovery,
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            credential_store=(
                credentials
                if mode is OliverUninstallMode.PURGE_PRIVATE_STATE
                else None
            ),
            clock_ms=lambda: 1_800_000_000_252,
            allow_test_roots=True,
        )
        assert receipt.payload["outcome"] == OliverReceiptOutcome.COMPLETED.value
        terminal = recovery.load()
        expected_phase = (
            "terminal"
            if mode is OliverUninstallMode.RUNTIME_ONLY
            else "commit_ready"
        )
        assert terminal is not None and terminal.phase == expected_phase


def test_private_purge_blocks_if_signed_registry_changed_after_install(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    later_anchor = RECEIPT_ANCHOR_LABEL_PREFIX + "c" * 64
    credentials.registered.add(later_anchor)
    credentials.values[later_anchor] = "later-anchor"

    with pytest.raises(OliverUninstallRejected, match="credential_inventory_changed"):
        _plan(
            roots,
            signer,
            credentials,
            inventory,
            mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
        )

    runtime_plan = _plan(roots, signer, credentials, inventory)
    assert runtime_plan.present_credential_ids == ()


def test_wrong_confirmation_and_plan_digest_never_mutate(tmp_path: Path) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    plan = _plan(roots, signer, credentials, inventory)

    with pytest.raises(OliverUninstallRejected, match="confirmation_required"):
        execute_oliver_uninstall(
            inventory=inventory,
            signer=signer,
            roots=roots,
            mode=OliverUninstallMode.RUNTIME_ONLY,
            expected_plan_digest=plan.digest,
            confirmation="UNINSTALL",
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            clock_ms=lambda: 1_800_000_000_000,
            allow_test_roots=True,
        )
    with pytest.raises(OliverUninstallRejected, match="plan_changed"):
        execute_oliver_uninstall(
            inventory=inventory,
            signer=signer,
            roots=roots,
            mode=OliverUninstallMode.RUNTIME_ONLY,
            expected_plan_digest="sha256:" + "0" * 64,
            confirmation=plan.confirmation_phrase,
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            clock_ms=lambda: 1_800_000_000_000,
            allow_test_roots=True,
        )
    assert roots.app_bundle.exists()


def test_unknown_extra_file_symlink_hardlink_and_writable_file_block_planning(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    _write(roots.app_bundle / "Contents" / "unexpected.txt", b"foreign")
    with pytest.raises(OliverUninstallRejected, match="unexpected_installed_entry"):
        _plan(roots, signer, credentials, inventory)
    (roots.app_bundle / "Contents" / "unexpected.txt").unlink()

    target = roots.app_bundle / "Contents" / "MacOS" / "austin-control"
    link = roots.app_bundle / "Contents" / "MacOS" / "linked-control"
    os.link(target, link)
    with pytest.raises(OliverUninstallRejected, match="entry_hardlink"):
        _plan(roots, signer, credentials, inventory)
    link.unlink()
    target.chmod(0o777)
    with pytest.raises(OliverUninstallRejected, match="entry_writable"):
        _plan(roots, signer, credentials, inventory)
    target.chmod(0o755)
    target.unlink()
    target.symlink_to("../Helpers/austin-relay")
    with pytest.raises(OliverUninstallRejected, match="entry_symlink"):
        _plan(roots, signer, credentials, inventory)


def test_changed_file_and_incomplete_credential_inventory_fail_closed(tmp_path: Path) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    target = roots.app_bundle / "Contents" / "Helpers" / "austin-relay"
    target.write_bytes(b"changed")
    target.chmod(0o755)
    with pytest.raises(OliverUninstallRejected, match="installed_entry_changed"):
        _plan(roots, signer, credentials, inventory)
    target.write_bytes(b"austin-relay")
    target.chmod(0o755)

    incomplete = OliverInstallInventory.create(
        install_id=inventory.install_id,
        installed_at_ms=inventory.installed_at_ms,
            user_uid=inventory.user_uid,
            team_id=inventory.team_id,
            app_version=inventory.app_version,
            app_build_number=inventory.app_build_number,
            extension_origin=inventory.extension_origin,
        credential_inventory_complete=False,
        entries=inventory.entries,
        credentials=(),
        signer=signer,
    )
    with pytest.raises(OliverUninstallRejected, match="credential_inventory_incomplete"):
        _plan(
            roots,
            signer,
            credentials,
            incomplete,
            mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
        )


def test_non_removable_parent_is_rejected_before_launchd_or_file_mutation(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, _inventory = _fixture(tmp_path)
    helpers = roots.app_bundle / "Contents" / "Helpers"
    helpers.chmod(0o555)
    inventory = capture_oliver_install_inventory(
        roots=roots,
        signer=signer,
        team_id=TEAM_ID,
        extension_origin=EXTENSION_ORIGIN,
        installed_at_ms=1_800_000_000_000,
        install_id=INSTALL_ID,
        credential_store=credentials,
        credential_labels=(*OLIVER_FIXED_CREDENTIAL_LABELS, ANCHOR_LABEL),
        credential_inventory_complete=True,
        allow_test_roots=True,
    )
    launch = FakeLaunchController("loaded")
    try:
        with pytest.raises(OliverUninstallRejected, match="privileged_removal_required"):
            _plan(roots, signer, credentials, inventory, launch=launch)
        assert launch.bootout_calls == 0
        assert roots.app_bundle.exists()
    finally:
        helpers.chmod(0o755)


def test_interrupted_partial_tree_can_be_reconciled_without_recursive_delete(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    missing = roots.app_bundle / "Contents" / "Helpers" / "austin-relay"
    missing.unlink()

    plan = _plan(roots, signer, credentials, inventory)
    times = iter((1_800_000_000_300, 1_800_000_000_301))
    receipt = execute_oliver_uninstall(
        inventory=inventory,
        signer=signer,
        roots=roots,
        mode=OliverUninstallMode.RUNTIME_ONLY,
        expected_plan_digest=plan.digest,
        confirmation=plan.confirmation_phrase,
        launch_controller=FakeLaunchController(),
        process_probe=StoppedProcessProbe(),
        clock_ms=lambda: next(times),
        allow_test_roots=True,
    )

    assert receipt.payload["outcome"] == OliverReceiptOutcome.COMPLETED.value
    assert receipt.payload["already_absent_entry_count"] == 1
    assert not roots.app_bundle.exists()


def test_launchctl_failure_returns_signed_unknown_receipt_before_file_removal(
    tmp_path: Path,
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    launch = FakeLaunchController("loaded", bootout_failure=True)
    plan = _plan(roots, signer, credentials, inventory, launch=launch)
    times = iter((1_800_000_000_400, 1_800_000_000_401))

    receipt = execute_oliver_uninstall(
        inventory=inventory,
        signer=signer,
        roots=roots,
        mode=OliverUninstallMode.RUNTIME_ONLY,
        expected_plan_digest=plan.digest,
        confirmation=plan.confirmation_phrase,
        launch_controller=launch,
        process_probe=StoppedProcessProbe(),
        clock_ms=lambda: next(times),
        allow_test_roots=True,
    )

    receipt.verify(signer.verifier)
    assert receipt.payload["outcome"] == OliverReceiptOutcome.UNKNOWN_OUTCOME.value
    assert receipt.payload["reason_code"] == "launchctl_bootout_failed"
    assert receipt.payload["deleted_entry_count"] == 0
    assert roots.app_bundle.exists()


def test_uninstall_cli_is_dry_run_first_and_content_free_when_receipt_is_absent(
    tmp_path: Path,
) -> None:
    missing_inventory = tmp_path / "AdaInstallInventory.json"
    missing_authority = tmp_path / "AdaInstallAuthority.bin"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/oliver_control_uninstall.py"),
            "--inventory",
            str(missing_inventory),
            "--authority-public-key",
            str(missing_authority),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "reason_code": "inventory_unavailable",
        "status": "blocked",
    }
    assert str(tmp_path) not in completed.stdout + completed.stderr


def test_installed_cli_dry_run_execute_and_terminal_resume_share_signed_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("algo_cli.oliver_control_installation.sys.platform", "darwin")
    roots, signer, credentials, inventory = _fixture(tmp_path)
    production_roots = replace(roots, production=True)
    inventory_path = tmp_path / "AdaInstallInventory.json"
    authority_path = tmp_path / "AdaInstallAuthority.bin"
    _write(inventory_path, inventory.to_bytes(), mode=0o600)
    _write(authority_path, signer.verifier.public_bytes, mode=0o444)
    state = tmp_path / "AdaCliState"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    recovery_path = state / f"AdaUninstallRecovery-{inventory.install_id}.json"

    monkeypatch.setattr(
        uninstall_cli.OliverInstallRoots,
        "for_current_user",
        classmethod(lambda cls: production_roots),
    )
    monkeypatch.setattr(
        uninstall_cli,
        "OliverLaunchctl",
        lambda: FakeLaunchController(),
    )
    monkeypatch.setattr(
        uninstall_cli,
        "OliverLsofProcessProbe",
        lambda: StoppedProcessProbe(),
    )
    monkeypatch.setattr(uninstall_cli, "_recovery_path", lambda _install_id: recovery_path)
    monkeypatch.setattr(uninstall_cli, "KeyringKeyStore", lambda: credentials)
    monkeypatch.setattr(
        uninstall_cli,
        "load_control_signer",
        lambda *, store: signer,
    )

    common = [
        "--inventory",
        str(inventory_path),
        "--authority-public-key",
        str(authority_path),
    ]
    assert uninstall_cli.main(common) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["status"] == "ready_dry_run"
    assert not recovery_path.exists()
    assert not (state / ".AdaUninstallRecovery.lock").exists()

    assert uninstall_cli.main(
        [
            *common,
            "--execute",
            "--plan-digest",
            dry_run["plan_digest"],
            "--confirm",
            dry_run["confirmation"],
        ]
    ) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed"
    terminal = AdaUninstallRecoveryStore(
        recovery_path,
        uid=production_roots.uid,
    ).load()
    assert terminal is not None and terminal.phase == "terminal"

    monkeypatch.setattr(
        uninstall_cli,
        "load_control_signer",
        lambda *, store: pytest.fail("terminal recovery must not reload the signer"),
    )
    assert uninstall_cli.main([*common, "--resume"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed == completed


def test_installed_cli_reports_missing_recovery_authority_as_unknown_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    roots, signer, credentials, inventory = _fixture(tmp_path)
    production_roots = replace(roots, production=True)
    inventory_path = tmp_path / "AdaInstallInventory.json"
    authority_path = tmp_path / "AdaInstallAuthority.bin"
    _write(inventory_path, inventory.to_bytes(), mode=0o600)
    _write(authority_path, signer.verifier.public_bytes, mode=0o444)
    state = tmp_path / "AdaCliState"
    state.mkdir(mode=0o700)
    recovery_path = state / f"AdaUninstallRecovery-{inventory.install_id}.json"
    plan = _plan(
        roots,
        signer,
        credentials,
        inventory,
        mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
    )
    authorized = AdaUninstallRecoveryStore(
        recovery_path,
        uid=production_roots.uid,
    )
    from algo_cli.ada_uninstall_recovery import AdaUninstallRecoveryRecord

    authorized.publish(
        AdaUninstallRecoveryRecord.authorize(
            install_id=inventory.install_id,
            inventory_digest=inventory.digest,
            plan_digest=plan.digest,
            mode=plan.mode.value,
            present_entry_ids=plan.present_entry_ids,
            present_credential_ids=plan.present_credential_ids,
            launch_agent_state=plan.launch_agent_state,
            created_at_ms=1_800_000_000_500,
            signer=signer,
        ),
        verifier=signer.verifier,
    )

    monkeypatch.setattr(
        uninstall_cli.OliverInstallRoots,
        "for_current_user",
        classmethod(lambda cls: production_roots),
    )
    monkeypatch.setattr(uninstall_cli, "_recovery_path", lambda _install_id: recovery_path)
    monkeypatch.setattr(uninstall_cli, "KeyringKeyStore", lambda: credentials)

    def unavailable(*, store):
        del store
        from algo_cli.grace_key_store import KeyStoreError

        raise KeyStoreError("absent")

    monkeypatch.setattr(uninstall_cli, "load_control_signer", unavailable)
    result = uninstall_cli.main(
        [
            "--inventory",
            str(inventory_path),
            "--authority-public-key",
            str(authority_path),
            "--resume",
        ]
    )
    assert result == 2
    assert json.loads(capsys.readouterr().out) == {
        "phase": "authorized",
        "reason_code": "uninstall_recovery_authority_unavailable",
        "status": "unknown_outcome",
    }


def test_installed_cli_resumes_commit_ready_purge_without_private_signer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("algo_cli.oliver_control_installation.sys.platform", "darwin")
    roots, signer, credentials, inventory = _fixture(tmp_path)
    production_roots = replace(roots, production=True)
    inventory_path = tmp_path / "AdaInstallInventory.json"
    authority_path = tmp_path / "AdaInstallAuthority.bin"
    _write(inventory_path, inventory.to_bytes(), mode=0o600)
    _write(authority_path, signer.verifier.public_bytes, mode=0o444)
    state = tmp_path / "AdaCliState"
    state.mkdir(mode=0o700)
    recovery_path = state / f"AdaUninstallRecovery-{inventory.install_id}.json"
    recovery = AdaUninstallRecoveryStore(recovery_path, uid=production_roots.uid)
    plan = _plan(
        roots,
        signer,
        credentials,
        inventory,
        mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
    )

    def fail_after_commit(stage: str) -> None:
        if stage == "commit_ready_published":
            raise SimulatedPowerLoss

    with pytest.raises(SimulatedPowerLoss):
        execute_oliver_uninstall(
            inventory=inventory,
            signer=signer,
            roots=roots,
            mode=OliverUninstallMode.PURGE_PRIVATE_STATE,
            expected_plan_digest=plan.digest,
            confirmation=plan.confirmation_phrase,
            launch_controller=FakeLaunchController(),
            process_probe=StoppedProcessProbe(),
            credential_store=credentials,
            clock_ms=lambda: 1_800_000_000_510,
            allow_test_roots=True,
            recovery_store=recovery,
            fault_injector=fail_after_commit,
        )
    commit_ready = recovery.load()
    assert commit_ready is not None and commit_ready.phase == "commit_ready"
    assert set(credentials.values) == {CONTROL_SIGNING_KEY_LABEL}

    monkeypatch.setattr(
        uninstall_cli.OliverInstallRoots,
        "for_current_user",
        classmethod(lambda cls: production_roots),
    )
    monkeypatch.setattr(
        uninstall_cli,
        "OliverLaunchctl",
        lambda: FakeLaunchController(),
    )
    monkeypatch.setattr(
        uninstall_cli,
        "OliverLsofProcessProbe",
        lambda: StoppedProcessProbe(),
    )
    monkeypatch.setattr(uninstall_cli, "_recovery_path", lambda _install_id: recovery_path)
    monkeypatch.setattr(uninstall_cli, "KeyringKeyStore", lambda: credentials)
    monkeypatch.setattr(
        uninstall_cli,
        "load_control_signer",
        lambda *, store: pytest.fail(
            f"commit-ready recovery must not load the private signer from {store!r}"
        ),
    )

    result = uninstall_cli.main(
        [
            "--inventory",
            str(inventory_path),
            "--authority-public-key",
            str(authority_path),
            "--resume",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload == {**commit_ready.terminal_receipt, "status": "completed"}
    assert not credentials.values
