from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import time

import pytest

from algo_cli.alice_artifact_store import (
    ArtifactAccessDenied,
    ArtifactIntegrityError,
    ArtifactPolicy,
    ArtifactQuotaExceeded,
    ArtifactStoreError,
    EncryptedArtifactStore,
    RunCapability,
    UnsafeArtifactPath,
)
from algo_cli.grace_key_store import StaticKeyStore


_MASTER_KEY = b"alice-test-master-key-material!!"[:32]


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _key_store() -> StaticKeyStore:
    return StaticKeyStore({"alice-artifact-master-v1": _MASTER_KEY})


def _store(
    root: Path,
    *,
    policy: ArtifactPolicy = ArtifactPolicy(),
    clock=time.time,
) -> EncryptedArtifactStore:
    return EncryptedArtifactStore(
        root,
        policy=policy,
        key_store=_key_store(),
        clock=clock,
    )


def _artifact_file(root: Path, capability: RunCapability, artifact_id: str) -> Path:
    return root / "runs" / capability.run_id / "artifacts" / f"{artifact_id}.alice"


def _manifest_file(root: Path, capability: RunCapability) -> Path:
    return root / "runs" / capability.run_id / "manifest.alice.json"


def _concurrent_put_worker(
    root: str,
    run_id: str,
    token: bytes,
    issued_at: float,
    expires_at: float,
    start_event,
    result_queue,
) -> None:
    policy = ArtifactPolicy(
        max_artifact_bytes=8,
        max_run_bytes=8,
        max_run_disk_bytes=4096,
        max_total_bytes=12,
        max_total_disk_bytes=8192,
        max_artifacts_per_run=1,
        max_runs=4,
        default_ttl_seconds=120,
        max_ttl_seconds=120,
    )
    store = _store(Path(root), policy=policy)
    capability = RunCapability(run_id, token, issued_at, expires_at)
    start_event.wait(timeout=10)
    try:
        store.put(capability, b"12345678", media_type="text/plain")
    except ArtifactQuotaExceeded:
        result_queue.put("quota")
    except Exception as exc:  # pragma: no cover - diagnostic for child failures
        result_queue.put(f"{type(exc).__name__}: {exc}")
    else:
        result_queue.put("stored")


def test_round_trip_is_ciphertext_only_capability_scoped_and_private(tmp_path) -> None:
    root = tmp_path / "alice"
    store = _store(root)
    capability = store.create_run(ttl_seconds=120)
    marker = b"SENSITIVE-PLAINTEXT-MARKER"

    first = store.put(capability, marker, media_type="text/plain")
    second = store.put(capability, marker, media_type="text/plain")

    assert store.read(capability, first) == marker
    assert first.artifact_id != second.artifact_id
    assert first.uri != second.uri
    assert first.content_id == second.content_id
    assert first.uri.startswith("artifact://private/v1/")
    assert capability.run_id not in first.uri
    assert capability.token.hex() not in repr(capability)
    assert capability.token.hex() not in json.dumps(capability.public_view())
    assert "token" not in capability.public_view()
    assert "run_id" not in first.public_view()

    first_payload = _artifact_file(root, capability, first.artifact_id).read_bytes()
    second_payload = _artifact_file(root, capability, second.artifact_id).read_bytes()
    assert marker not in first_payload
    assert marker not in second_payload
    assert first_payload != second_payload
    assert hashlib.sha256(marker).hexdigest().encode("ascii") not in first_payload
    assert capability.token not in _manifest_file(root, capability).read_bytes()

    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(_manifest_file(root, capability).stat().st_mode) == 0o600
        assert stat.S_IMODE(
            _artifact_file(root, capability, first.artifact_id).stat().st_mode
        ) == 0o600


def test_same_capability_and_key_resume_after_store_restart(tmp_path) -> None:
    root = tmp_path / "alice"
    first_store = _store(root)
    capability = first_store.create_run(ttl_seconds=120)
    ref = first_store.put(capability, b"restart-safe", media_type="text/plain")

    restarted = _store(root)

    assert restarted.read(capability, ref) == b"restart-safe"
    assert restarted.cleanup().corrupt_runs == 0


def test_wrong_capability_wrong_run_and_wrong_master_key_fail_closed(tmp_path) -> None:
    root = tmp_path / "alice"
    store = _store(root)
    capability = store.create_run(ttl_seconds=120)
    other = store.create_run(ttl_seconds=120)
    ref = store.put(capability, b"private", media_type="text/plain")
    wrong_token = dataclasses.replace(capability, token=b"z" * 32)

    with pytest.raises(ArtifactAccessDenied, match="invalid"):
        store.read(wrong_token, ref)
    with pytest.raises(ArtifactAccessDenied, match="different run"):
        store.read(other, ref)

    wrong_key = EncryptedArtifactStore(
        root,
        key_store=StaticKeyStore({"alice-artifact-master-v1": b"w" * 32}),
    )
    with pytest.raises(ArtifactIntegrityError, match="signature"):
        wrong_key.read(capability, ref)


def test_ciphertext_manifest_and_reference_tampering_fail_integrity(tmp_path) -> None:
    root = tmp_path / "alice"
    store = _store(root)
    capability = store.create_run(ttl_seconds=120)
    ref = store.put(capability, b"tamper target", media_type="text/plain")
    path = _artifact_file(root, capability, ref.artifact_id)
    envelope = json.loads(path.read_text(encoding="ascii"))
    ciphertext = bytearray(base64.urlsafe_b64decode(envelope["ciphertext"]))
    ciphertext[-1] ^= 1
    envelope["ciphertext"] = base64.urlsafe_b64encode(ciphertext).decode("ascii")
    path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        encoding="ascii",
    )

    with pytest.raises(ArtifactIntegrityError, match="authentication"):
        store.read(capability, ref)

    # Restore a fresh artifact, then prove a structurally plausible ref cannot
    # swap metadata or content identity.
    clean = store.put(capability, b"clean target", media_type="text/plain")
    swapped = dataclasses.replace(clean, byte_count=clean.byte_count + 1)
    with pytest.raises(ArtifactIntegrityError, match="misbound|inconsistent"):
        store.read(capability, swapped)

    manifest_path = _manifest_file(root, capability)
    manifest = json.loads(manifest_path.read_text(encoding="ascii"))
    manifest["status"] = "revoked"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="ascii",
    )
    with pytest.raises(ArtifactIntegrityError, match="signature"):
        store.read(capability, clean)


def test_duplicate_keys_and_noncanonical_base64_reject(tmp_path) -> None:
    root = tmp_path / "alice"
    store = _store(root)
    capability = store.create_run(ttl_seconds=120)
    ref = store.put(capability, b"strict parser", media_type="text/plain")
    path = _artifact_file(root, capability, ref.artifact_id)
    payload = path.read_text(encoding="ascii")
    # Preserve the signed disk length while replacing run_id with a second
    # nonce key.  Whitespace before the colon keeps the byte count unchanged.
    path.write_text(payload.replace('"run_id":', '"nonce" :', 1), encoding="ascii")
    with pytest.raises(ArtifactIntegrityError, match="strict JSON"):
        store.read(capability, ref)

    # A new run avoids the intentionally corrupt active run during put-time
    # global health checks.
    isolated_root = tmp_path / "isolated"
    isolated = _store(isolated_root)
    isolated_cap = isolated.create_run(ttl_seconds=120)
    isolated_ref = isolated.put(isolated_cap, b"base64", media_type="text/plain")
    isolated_path = _artifact_file(isolated_root, isolated_cap, isolated_ref.artifact_id)
    envelope = json.loads(isolated_path.read_text(encoding="ascii"))
    envelope["nonce"] = envelope["nonce"][:-1] + "="
    isolated_path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        encoding="ascii",
    )
    with pytest.raises(ArtifactIntegrityError, match="base64"):
        isolated.read(isolated_cap, isolated_ref)


def test_expiry_clock_rollback_and_nonfinite_clock_fail_closed(tmp_path) -> None:
    clock = MutableClock(100.0)
    root = tmp_path / "alice"
    store = _store(root, clock=clock)
    capability = store.create_run(ttl_seconds=10)
    ref = store.put(capability, b"short lived", media_type="text/plain", ttl_seconds=5)

    clock.value = 99.0
    with pytest.raises(ArtifactAccessDenied, match="backwards"):
        store.read(capability, ref)

    clock.value = 105.0
    with pytest.raises(ArtifactAccessDenied, match="expired"):
        store.read(capability, ref)

    clock.value = 111.0
    result = store.cleanup()
    assert result.deleted_runs == 1
    assert not (root / "runs" / capability.run_id).exists()

    clock.value = float("nan")
    with pytest.raises(ArtifactStoreError, match="finite"):
        store.create_run()


def test_per_item_run_store_count_and_disk_quotas_leave_no_orphans(tmp_path) -> None:
    policy = ArtifactPolicy(
        max_artifact_bytes=10,
        max_run_bytes=15,
        max_run_disk_bytes=4096,
        max_total_bytes=20,
        max_total_disk_bytes=8192,
        max_artifacts_per_run=2,
        max_runs=2,
        default_ttl_seconds=120,
        max_ttl_seconds=120,
    )
    root = tmp_path / "alice"
    store = _store(root, policy=policy)
    first = store.create_run()
    second = store.create_run()

    with pytest.raises(ArtifactQuotaExceeded, match="per-item"):
        store.put(first, b"x" * 11, media_type="text/plain")
    one = store.put(first, b"x" * 10, media_type="text/plain")
    with pytest.raises(ArtifactQuotaExceeded, match="this run"):
        store.put(first, b"y" * 6, media_type="text/plain")
    store.put(second, b"z" * 10, media_type="text/plain")
    with pytest.raises(ArtifactQuotaExceeded, match="store plaintext"):
        store.put(second, b"q", media_type="text/plain")
    with pytest.raises(ArtifactQuotaExceeded, match="run quota"):
        store.create_run()

    assert list((root / "runs" / first.run_id / "artifacts").glob("*.alice")) == [
        _artifact_file(root, first, one.artifact_id)
    ]
    assert store.cleanup().quota_satisfied is True


def test_concurrent_processes_cannot_race_past_store_quota(tmp_path) -> None:
    policy = ArtifactPolicy(
        max_artifact_bytes=8,
        max_run_bytes=8,
        max_run_disk_bytes=4096,
        max_total_bytes=12,
        max_total_disk_bytes=8192,
        max_artifacts_per_run=1,
        max_runs=4,
        default_ttl_seconds=120,
        max_ttl_seconds=120,
    )
    root = tmp_path / "alice"
    store = _store(root, policy=policy)
    capabilities = (store.create_run(), store.create_run())
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    workers = [
        context.Process(
            target=_concurrent_put_worker,
            args=(
                str(root),
                capability.run_id,
                capability.token,
                capability.issued_at,
                capability.expires_at,
                start_event,
                result_queue,
            ),
        )
        for capability in capabilities
    ]
    for worker in workers:
        worker.start()
    start_event.set()
    outcomes = [result_queue.get(timeout=15) for _worker in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    assert sorted(outcomes) == ["quota", "stored"]
    cleanup = store.cleanup()
    assert cleanup.active_artifacts == 1
    assert cleanup.active_plaintext_bytes == 8


def test_revocation_is_persisted_before_best_effort_ciphertext_deletion(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "alice"
    store = _store(root)
    capability = store.create_run(ttl_seconds=120)
    ref = store.put(capability, b"revoke me", media_type="text/plain")
    original_unlink = Path.unlink

    def deny_artifact_unlink(path: Path, *args, **kwargs):
        if path.suffix == ".alice":
            raise OSError("simulated deletion failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_artifact_unlink)
    result = store.revoke_run(capability)

    assert result.already_revoked is False
    assert result.ciphertext_files_deleted == 0
    assert result.ciphertext_files_pending == 1
    assert _artifact_file(root, capability, ref.artifact_id).exists()
    assert json.loads(_manifest_file(root, capability).read_text())["status"] == "revoked"
    with pytest.raises(ArtifactAccessDenied, match="revoked"):
        store.read(capability, ref)


def test_cleanup_removes_crash_orphans_temps_and_expired_runs(tmp_path) -> None:
    clock = MutableClock(100.0)
    root = tmp_path / "alice"
    store = _store(root, clock=clock)
    capability = store.create_run(ttl_seconds=20)
    store.put(capability, b"kept", media_type="text/plain")
    artifacts = root / "runs" / capability.run_id / "artifacts"
    orphan = artifacts / ("f" * 32 + ".alice")
    orphan.write_bytes(b"partial crash artifact")
    orphan.chmod(0o600)
    temporary = artifacts / ".partial.tmp"
    temporary.write_bytes(b"partial")
    temporary.chmod(0o600)
    staging = root / "runs" / ".create-crash.tmp"
    staging.mkdir(mode=0o700)
    (staging / "partial").write_bytes(b"partial")

    cleaned = store.cleanup()

    assert cleaned.deleted_orphans == 1
    assert cleaned.deleted_temporary_paths >= 3
    assert cleaned.corrupt_runs == 0
    assert not orphan.exists()
    assert not temporary.exists()
    assert not staging.exists()

    clock.value = 121.0
    expired = store.cleanup()
    assert expired.deleted_runs == 1
    assert not (root / "runs" / capability.run_id).exists()


def test_write_path_prunes_expired_ciphertext_before_enforcing_total_quota(tmp_path) -> None:
    clock = MutableClock(100.0)
    policy = ArtifactPolicy(
        max_artifact_bytes=8,
        max_run_bytes=8,
        max_run_disk_bytes=4096,
        max_total_bytes=8,
        max_total_disk_bytes=4096,
        max_artifacts_per_run=1,
        max_runs=1,
        default_ttl_seconds=10,
        max_ttl_seconds=10,
    )
    root = tmp_path / "alice"
    store = _store(root, policy=policy, clock=clock)
    expired = store.create_run()
    store.put(expired, b"12345678", media_type="text/plain")

    clock.value = 111.0
    replacement = store.create_run()

    assert replacement.run_id != expired.run_id
    assert not (root / "runs" / expired.run_id).exists()
    assert store.put(replacement, b"abcdefgh", media_type="text/plain").byte_count == 8


def test_readiness_detects_but_does_not_mutate_crash_orphans(tmp_path) -> None:
    root = tmp_path / "alice"
    store = _store(root)
    capability = store.create_run(ttl_seconds=120)
    artifacts = root / "runs" / capability.run_id / "artifacts"
    orphan = artifacts / ("e" * 32 + ".alice")
    orphan.write_bytes(b"crash orphan")
    orphan.chmod(0o600)

    readiness = store.readiness()

    assert readiness["status"] == "not_ready"
    assert readiness["error_class"] == "ArtifactIntegrityError"
    assert orphan.exists()
    assert store.cleanup().deleted_orphans == 1
    assert not orphan.exists()


def test_cleanup_repairs_signed_counter_drift_but_not_missing_ciphertext(tmp_path) -> None:
    root = tmp_path / "alice"
    store = _store(root)
    capability = store.create_run(ttl_seconds=120)
    ref = store.put(capability, b"counter", media_type="text/plain")
    master = store._master_key()
    manifest, keys, _matches = store._decode_manifest(
        capability.run_id,
        master,
        allow_counter_repair=True,
    )
    manifest["artifact_count"] = 0
    manifest["plaintext_bytes"] = 0
    manifest["disk_bytes"] = 0
    store._write_manifest(manifest, keys)

    repaired = store.cleanup()

    assert repaired.repaired_manifests == 1
    assert repaired.active_artifacts == 1
    assert repaired.active_plaintext_bytes == len(b"counter")
    assert store.read(capability, ref) == b"counter"

    _artifact_file(root, capability, ref.artifact_id).unlink()
    corrupt = store.cleanup()
    assert corrupt.corrupt_runs == 1
    assert (root / "runs" / capability.run_id).exists()


def test_cleanup_authenticates_each_active_ciphertext_without_deleting_corruption(
    tmp_path,
) -> None:
    root = tmp_path / "alice"
    store = _store(root)
    capability = store.create_run(ttl_seconds=120)
    ref = store.put(capability, b"integrity sweep", media_type="text/plain")
    path = _artifact_file(root, capability, ref.artifact_id)
    envelope = json.loads(path.read_text(encoding="ascii"))
    ciphertext = bytearray(base64.urlsafe_b64decode(envelope["ciphertext"]))
    ciphertext[len(ciphertext) // 2] ^= 1
    envelope["ciphertext"] = base64.urlsafe_b64encode(ciphertext).decode("ascii")
    path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        encoding="ascii",
    )

    result = store.cleanup()

    assert result.corrupt_runs == 1
    assert result.deleted_runs == 0
    assert path.exists()


def test_root_and_artifact_symlinks_are_rejected_without_following(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(target, target_is_directory=True)
    with pytest.raises(UnsafeArtifactPath, match="real directories"):
        _store(linked_root)

    root = tmp_path / "alice"
    store = _store(root)
    capability = store.create_run(ttl_seconds=120)
    ref = store.put(capability, b"safe", media_type="text/plain")
    artifact = _artifact_file(root, capability, ref.artifact_id)
    artifact.unlink()
    artifact.symlink_to(tmp_path / "outside")
    with pytest.raises(UnsafeArtifactPath, match="non-symlink"):
        store.read(capability, ref)


def test_media_type_content_type_and_key_source_fail_closed(tmp_path) -> None:
    store = _store(tmp_path / "alice")
    capability = store.create_run(ttl_seconds=120)

    with pytest.raises(TypeError, match="bytes"):
        store.put(capability, "plaintext", media_type="text/plain")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="MIME"):
        store.put(capability, b"x", media_type="text/plain; charset=utf-8")

    class BrokenKeyStore:
        def get_or_create(self, *_args, **_kwargs):
            raise RuntimeError("unavailable")

    unavailable = EncryptedArtifactStore(
        tmp_path / "unavailable",
        key_store=BrokenKeyStore(),
    )
    with pytest.raises(ArtifactStoreError, match="unavailable"):
        unavailable.create_run()
    readiness = unavailable.readiness()
    assert readiness == {
        "status": "not_ready",
        "error_class": "ArtifactStoreError",
        "secure_erasure": False,
    }
    assert "unavailable" not in json.dumps(readiness)


def test_readiness_is_content_free_and_admits_no_secure_erase_claim(tmp_path) -> None:
    store = _store(tmp_path / "alice")
    capability = store.create_run(ttl_seconds=120)
    marker = b"READINESS-MUST-NOT-LEAK"
    store.put(capability, marker, media_type="text/plain")

    readiness = store.readiness()
    rendered = json.dumps(readiness, sort_keys=True)

    assert readiness["status"] == "ready"
    assert readiness["secure_erasure"] is False
    assert readiness["active_runs"] == 1
    assert readiness["active_plaintext_bytes"] == len(marker)
    assert capability.run_id not in rendered
    assert marker.decode("ascii") not in rendered
