"""Installed dry-run-first CLI for bounded native-control removal."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import time

from .ada_uninstall_recovery import (
    AdaUninstallRecoveryError,
    AdaUninstallRecoveryStore,
)
from .david_control_kernel import ControlVerifier
from .grace_key_store import KeyStoreError, KeyringKeyStore, load_control_signer
from .oliver_control_installation import (
    MAX_INVENTORY_BYTES,
    OliverInstallInventory,
    OliverInstallRoots,
    OliverLaunchctl,
    OliverLsofProcessProbe,
    OliverReceiptOutcome,
    OliverUninstallMode,
    OliverUninstallRejected,
    execute_oliver_uninstall,
    plan_oliver_uninstall,
    resume_oliver_uninstall,
)


def _default_state_path(name: str) -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Algo CLI Control"
        / name
    )


def _read_bounded(path: Path, *, maximum: int, reason_code: str) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise OliverUninstallRejected(reason_code)
    try:
        current = Path(path.anchor)
        for component in path.parts[1:-1]:
            current = current / component
            ancestor = current.lstat()
            if stat.S_ISLNK(ancestor.st_mode) or not stat.S_ISDIR(ancestor.st_mode):
                raise OSError
        value = path.lstat()
        current_uid = os.getuid() if hasattr(os, "getuid") else -1
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_nlink != 1
            or value.st_uid not in {0, current_uid}
            or stat.S_IMODE(value.st_mode) & 0o022
        ):
            raise OSError
        if value.st_size < 1 or value.st_size > maximum:
            raise OSError
        payload = path.read_bytes()
    except OSError as exc:
        raise OliverUninstallRejected(reason_code) from exc
    if len(payload) != value.st_size:
        raise OliverUninstallRejected(reason_code)
    return payload


def _emit(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _recovery_path(install_id: str) -> Path:
    return _default_state_path(f"AdaUninstallRecovery-{install_id}.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=_default_state_path("AdaInstallInventory.json"),
        help="Signed install inventory created by the native installer",
    )
    parser.add_argument(
        "--authority-public-key",
        type=Path,
        default=_default_state_path("AdaInstallAuthority.bin"),
        help="Independent 32-byte install authority public key",
    )
    parser.add_argument(
        "--purge-private-state",
        action="store_true",
        help="Also remove only the exact credential labels signed into a complete inventory",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute after a prior dry run; otherwise this command is read-only",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an interrupted plan from its signed Ada recovery record",
    )
    parser.add_argument("--plan-digest", help="Exact digest printed by the dry run")
    parser.add_argument("--confirm", help="Exact confirmation phrase printed by the dry run")
    args = parser.parse_args(argv)

    try:
        inventory = OliverInstallInventory.from_bytes(
            _read_bounded(
                args.inventory,
                maximum=MAX_INVENTORY_BYTES,
                reason_code="inventory_unavailable",
            )
        )
        public_key = _read_bounded(
            args.authority_public_key,
            maximum=32,
            reason_code="authority_key_unavailable",
        )
        if len(public_key) != 32:
            raise OliverUninstallRejected("authority_key_size")
        verifier = ControlVerifier.from_public_bytes(public_key)
        mode = (
            OliverUninstallMode.PURGE_PRIVATE_STATE
            if args.purge_private_state
            else OliverUninstallMode.RUNTIME_ONLY
        )
        roots = OliverInstallRoots.for_current_user()
        launch_controller = OliverLaunchctl()
        process_probe = OliverLsofProcessProbe()
        recovery_store = AdaUninstallRecoveryStore(
            _recovery_path(inventory.install_id),
            uid=roots.uid,
        )
        recovery_record = recovery_store.load()
        if args.resume:
            if args.execute or args.plan_digest or args.confirm:
                raise OliverUninstallRejected("recovery_argument_conflict")
            if recovery_record is None:
                raise OliverUninstallRejected("uninstall_recovery_absent")
            recovery_record.verify(verifier)
            recovery_record.verify_context(
                install_id=inventory.install_id,
                inventory_digest=inventory.digest,
                mode=recovery_record.mode,
            )
            credential_store = (
                KeyringKeyStore()
                if recovery_record.mode == OliverUninstallMode.PURGE_PRIVATE_STATE.value
                else None
            )
            signer = None
            if recovery_record.phase == "authorized":
                try:
                    signer = load_control_signer(
                        store=(credential_store or KeyringKeyStore())
                    )
                except KeyStoreError:
                    _emit(
                        {
                            "phase": recovery_record.phase,
                            "reason_code": "uninstall_recovery_authority_unavailable",
                            "status": "unknown_outcome",
                        }
                    )
                    return 2
            receipt = resume_oliver_uninstall(
                inventory=inventory,
                verifier=verifier,
                signer=signer,
                roots=roots,
                recovery_store=recovery_store,
                launch_controller=launch_controller,
                process_probe=process_probe,
                credential_store=credential_store,
                clock_ms=lambda: time.time_ns() // 1_000_000,
            )
            payload = receipt.to_dict()
            payload["status"] = (
                "completed"
                if receipt.payload["outcome"] == OliverReceiptOutcome.COMPLETED.value
                else "unknown_outcome"
            )
            _emit(payload)
            return 0 if payload["status"] == "completed" else 2
        if recovery_record is not None:
            recovery_record.verify(verifier)
            recovery_record.verify_context(
                install_id=inventory.install_id,
                inventory_digest=inventory.digest,
                mode=recovery_record.mode,
            )
            _emit(
                {
                    "mode": recovery_record.mode,
                    "phase": recovery_record.phase,
                    "plan_digest": recovery_record.plan_digest,
                    "status": "recovery_required",
                }
            )
            return 2
        credential_store = KeyringKeyStore() if args.purge_private_state else None
        plan = plan_oliver_uninstall(
            inventory=inventory,
            verifier=verifier,
            roots=roots,
            mode=mode,
            launch_controller=launch_controller,
            process_probe=process_probe,
            credential_store=credential_store,
        )
        if not args.execute:
            _emit(
                {
                    "confirmation": plan.confirmation_phrase,
                    "credential_count": len(plan.present_credential_ids),
                    "entry_count": len(plan.present_entry_ids),
                    "inventory_digest": plan.inventory_digest,
                    "mode": plan.mode.value,
                    "plan_digest": plan.digest,
                    "private_user_data_preserved": mode
                    is OliverUninstallMode.RUNTIME_ONLY,
                    "status": "ready_dry_run",
                }
            )
            return 0
        if not args.plan_digest or not args.confirm:
            raise OliverUninstallRejected("execution_confirmation_missing")
        signer = load_control_signer(
            store=credential_store if credential_store is not None else KeyringKeyStore()
        )
        if signer.verifier.key_id != verifier.key_id:
            raise OliverUninstallRejected("authority_key_changed")
        receipt = execute_oliver_uninstall(
            inventory=inventory,
            signer=signer,
            roots=roots,
            mode=mode,
            expected_plan_digest=args.plan_digest,
            confirmation=args.confirm,
            launch_controller=launch_controller,
            process_probe=process_probe,
            credential_store=credential_store,
            clock_ms=lambda: time.time_ns() // 1_000_000,
            recovery_store=recovery_store,
        )
        payload = receipt.to_dict()
        payload["status"] = (
            "completed"
            if receipt.payload["outcome"] == OliverReceiptOutcome.COMPLETED.value
            else "unknown_outcome"
        )
        _emit(payload)
        return 0 if payload["status"] == "completed" else 2
    except (
        AdaUninstallRecoveryError,
        OliverUninstallRejected,
        KeyStoreError,
        ValueError,
    ) as exc:
        reason = (
            exc.reason_code
            if isinstance(exc, (AdaUninstallRecoveryError, OliverUninstallRejected))
            else "credential_authority_unavailable"
        )
        _emit({"reason_code": reason, "status": "blocked"})
        return 1


__all__ = ["main"]
