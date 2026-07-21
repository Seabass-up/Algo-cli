from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from algo_cli.david_control_kernel import ControlSigner, canonical_json_bytes
from algo_cli.grace_key_store import (
    AUTHORITY_ROTATION_ANCHOR_LABEL_PREFIX,
    GraceAuthorityRotationAnchorStore,
    KeyringKeyStore,
)
from algo_cli.henry_effect_control import TargetLeaseManager
from algo_cli.oliver_authority_rotation import (
    AdaAuthorityRotationStore,
    OLIVER_AUTHORITY_ROTATION_SIGNATURE_KIND,
    OliverAuthorityRotationError,
    OliverAuthorityRotationRecord,
    authority_rotation_anchor_id,
    authority_rotation_path_digest,
)


NOW_MS = 1_800_000_000_000
OLD_INSTALL_ID = "00000000-0000-4000-8000-000000000102"
OLD_INVENTORY_DIGEST = "sha256:" + ("b" * 64)
ANCHOR_ID = authority_rotation_anchor_id(OLD_INSTALL_ID, OLD_INVENTORY_DIGEST)
POSIX_STORE = pytest.mark.skipif(
    os.name != "posix",
    reason="descriptor-relative owner and flock store is POSIX-only",
)


def _digest(character: str) -> str:
    return "sha256:" + (character * 64)


def _signer(seed: int = 0) -> ControlSigner:
    return ControlSigner.from_private_bytes(bytes((seed + index) % 256 for index in range(32)))


def _authorized(
    signer: ControlSigner | None = None,
    *,
    rotation_id: str = "00000000-0000-4000-8000-000000000101",
    anchor_id: str | None = None,
    old_install_id: str = OLD_INSTALL_ID,
    new_install_id: str = "00000000-0000-4000-8000-000000000103",
    expires_at_ms: int = NOW_MS + 60_000,
) -> OliverAuthorityRotationRecord:
    selected = signer or _signer()
    return OliverAuthorityRotationRecord.authorize(
        rotation_id=rotation_id,
        anchor_id=(
            anchor_id
            if anchor_id is not None
            else authority_rotation_anchor_id(old_install_id, OLD_INVENTORY_DIGEST)
        ),
        old_install_id=old_install_id,
        new_install_id=new_install_id,
        old_inventory_digest=OLD_INVENTORY_DIGEST,
        new_inventory_digest=_digest("c"),
        old_app_digest=_digest("d"),
        new_app_digest=_digest("e"),
        database_path_digest=_digest("f"),
        evidence_path_digest=_digest("1"),
        old_database_digest=_digest("2"),
        old_samuel_key_id="ed25519:" + ("3" * 64),
        new_samuel_key_id="ed25519:" + ("4" * 64),
        old_app_version="0.18.0",
        old_app_build_number="18",
        new_app_version="0.19.0",
        new_app_build_number="19",
        reason_code="clock_floor",
        authorized_at_ms=NOW_MS,
        expires_at_ms=expires_at_ms,
        signer=selected,
    )


def _commit_ready(
    signer: ControlSigner | None = None,
    **kwargs: object,
) -> OliverAuthorityRotationRecord:
    selected = signer or _signer()
    return _authorized(selected, **kwargs).prepare_commit(
        quiescence_evidence_digest=_digest("5"),
        retained_evidence_digest=_digest("6"),
        updated_at_ms=NOW_MS + 1,
        signer=selected,
    )


class MemoryRotationAnchor:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _digest(value: bytes) -> str:
        return "sha256:" + hashlib.sha256(value).hexdigest()

    def load(self, anchor_id: str) -> bytes | None:
        with self.lock:
            value = self.values.get(anchor_id)
            return None if value is None else bytes(value)

    def compare_and_set(
        self,
        anchor_id: str,
        *,
        expected_digest: str | None,
        value: bytes,
    ) -> bool:
        with self.lock:
            current = self.values.get(anchor_id)
            observed = None if current is None else self._digest(current)
            if observed != expected_digest:
                return False
            self.values[anchor_id] = bytes(value)
            return True


def _private_path(tmp_path: Path) -> Path:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    return parent / "AdaAuthorityRotation.json"


def _store(
    tmp_path: Path,
    anchors: MemoryRotationAnchor,
    *,
    path: Path | None = None,
    anchor_id: str = ANCHOR_ID,
) -> AdaAuthorityRotationStore:
    return AdaAuthorityRotationStore(
        path or _private_path(tmp_path),
        uid=os.getuid(),
        anchor_id=anchor_id,
        anchor_store=anchors,
    )


def test_rotation_record_round_trip_signature_and_content_free_fields() -> None:
    signer = _signer()
    record = _authorized(signer)

    parsed = OliverAuthorityRotationRecord.from_bytes(record.to_bytes())
    parsed.verify(signer.verifier)

    assert parsed == record
    assert parsed.phase == "authorized"
    assert parsed.revision == 1
    assert "/Users/" not in record.to_bytes().decode("utf-8")
    assert "clock_floor" in record.to_bytes().decode("utf-8")


def test_rotation_record_rejects_tampering_unknown_fields_and_noncanonical_json() -> None:
    signer = _signer()
    record = _authorized(signer)
    tampered = record.to_dict()
    tampered["reason_code"] = "integrity"
    parsed = OliverAuthorityRotationRecord.from_dict(tampered)
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_signature"):
        parsed.verify(signer.verifier)

    unknown = record.to_dict()
    unknown["private_note"] = "no"
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_schema"):
        OliverAuthorityRotationRecord.from_dict(unknown)

    noncanonical = json.dumps(record.to_dict(), indent=2).encode("utf-8")
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_noncanonical"):
        OliverAuthorityRotationRecord.from_bytes(noncanonical)


def test_verify_reparses_even_a_directly_constructed_signed_dataclass() -> None:
    signer = _signer()
    record = _authorized(signer)
    invalid_unsigned = {
        **record.unsigned,
        "new_app_build_number": record.old_app_build_number,
    }
    invalid = replace(
        record,
        new_app_build_number=record.old_app_build_number,
        signature=signer.sign(
            OLIVER_AUTHORITY_ROTATION_SIGNATURE_KIND,
            invalid_unsigned,
        ),
    )
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_rollback"):
        invalid.verify(signer.verifier)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("anchor_id", _digest("a"), "anchor_context"),
        ("new_install_id", "00000000-0000-4000-8000-000000000102", "install_reuse"),
        ("new_inventory_digest", _digest("b"), "identity_reuse"),
        ("new_app_digest", _digest("d"), "identity_reuse"),
        ("evidence_path_digest", _digest("f"), "path_reuse"),
        ("new_samuel_key_id", "ed25519:" + ("3" * 64), "key_reuse"),
        ("new_app_version", "0.17.9", "rollback"),
        ("new_app_build_number", "18", "rollback"),
        ("reason_code", "operator_choice", "reason"),
    ],
)
def test_rotation_record_rejects_identity_reuse_and_rollback(
    field: str,
    value: str,
    reason: str,
) -> None:
    row = _authorized().to_dict()
    row[field] = value
    with pytest.raises(OliverAuthorityRotationError, match=f"authority_rotation_{reason}"):
        OliverAuthorityRotationRecord.from_dict(row)


def test_rotation_record_enforces_bounded_expiry_and_phase_transitions() -> None:
    signer = _signer()
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_expiry"):
        _authorized(signer, expires_at_ms=NOW_MS + 3_600_001)

    authorized = _authorized(signer, expires_at_ms=NOW_MS + 2)
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_expired"):
        authorized.prepare_commit(
            quiescence_evidence_digest=_digest("5"),
            retained_evidence_digest=_digest("6"),
            updated_at_ms=NOW_MS + 3,
            signer=signer,
        )
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_transition"):
        authorized.complete(
            new_empty_store_digest=_digest("7"),
            qualification_evidence_digest=_digest("8"),
            updated_at_ms=NOW_MS + 1,
            signer=signer,
        )


@POSIX_STORE
def test_anchored_store_advances_exactly_and_issues_only_commit_ready_permit(
    tmp_path: Path,
) -> None:
    signer = _signer()
    anchors = MemoryRotationAnchor()
    store = _store(tmp_path, anchors)
    authorized = _authorized(signer)
    commit_ready = authorized.prepare_commit(
        quiescence_evidence_digest=_digest("5"),
        retained_evidence_digest=_digest("6"),
        updated_at_ms=NOW_MS + 1,
        signer=signer,
    )
    terminal = commit_ready.complete(
        new_empty_store_digest=_digest("7"),
        qualification_evidence_digest=_digest("8"),
        updated_at_ms=NOW_MS + 2,
        signer=signer,
    )

    assert store.publish(authorized, verifier=signer.verifier) == authorized
    with pytest.raises(OliverAuthorityRotationError, match="not_commit_ready"):
        store.commit_permit(signer.verifier, now_ms=NOW_MS)
    assert store.publish(commit_ready, verifier=signer.verifier) == commit_ready
    permit = store.commit_permit(signer.verifier, now_ms=NOW_MS + 1)
    assert permit.record_digest == commit_ready.digest
    assert permit.old_samuel_key_id != permit.new_samuel_key_id
    assert store.publish(terminal, verifier=signer.verifier) == terminal
    assert store.load(signer.verifier) == terminal
    with pytest.raises(OliverAuthorityRotationError, match="not_commit_ready"):
        store.commit_permit(signer.verifier, now_ms=NOW_MS + 2)


@POSIX_STORE
def test_commit_permit_rejects_future_reader_and_expired_record(tmp_path: Path) -> None:
    signer = _signer()
    anchors = MemoryRotationAnchor()
    store = _store(tmp_path, anchors)
    commit_ready = _commit_ready(signer)
    store.publish(_authorized(signer), verifier=signer.verifier)
    store.publish(commit_ready, verifier=signer.verifier)

    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_expired"):
        store.commit_permit(signer.verifier, now_ms=NOW_MS)
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_expired"):
        store.commit_permit(signer.verifier, now_ms=NOW_MS + 60_001)


@POSIX_STORE
def test_anchor_first_publish_recovers_cache_after_interruption(tmp_path: Path) -> None:
    signer = _signer()
    anchors = MemoryRotationAnchor()
    path = _private_path(tmp_path)

    class FailOnceStore(AdaAuthorityRotationStore):
        fail_once = True

        def _write_at(self, directory_fd: int, record: OliverAuthorityRotationRecord) -> None:
            if self.fail_once:
                self.fail_once = False
                raise OliverAuthorityRotationError("injected_after_anchor")
            super()._write_at(directory_fd, record)

    interrupted = FailOnceStore(
        path,
        uid=os.getuid(),
        anchor_id=ANCHOR_ID,
        anchor_store=anchors,
    )
    record = _authorized(signer)
    with pytest.raises(OliverAuthorityRotationError, match="injected_after_anchor"):
        interrupted.publish(record, verifier=signer.verifier)

    assert not path.exists()
    recovered = _store(tmp_path, anchors, path=path)
    assert recovered.load(signer.verifier) == record
    assert path.read_bytes() == record.to_bytes()


@POSIX_STORE
def test_uncertain_anchor_write_recovers_without_redispatch(tmp_path: Path) -> None:
    signer = _signer()

    class UncertainAnchor(MemoryRotationAnchor):
        fail_once = True

        def compare_and_set(
            self,
            anchor_id: str,
            *,
            expected_digest: str | None,
            value: bytes,
        ) -> bool:
            changed = super().compare_and_set(
                anchor_id,
                expected_digest=expected_digest,
                value=value,
            )
            if changed and self.fail_once:
                self.fail_once = False
                raise RuntimeError("uncertain")
            return changed

    anchors = UncertainAnchor()
    path = _private_path(tmp_path)
    store = _store(tmp_path, anchors, path=path)
    record = _authorized(signer)
    with pytest.raises(OliverAuthorityRotationError, match="anchor_unavailable"):
        store.publish(record, verifier=signer.verifier)

    assert not path.exists()
    assert store.load(signer.verifier) == record
    assert path.read_bytes() == record.to_bytes()


@POSIX_STORE
def test_one_revision_anchor_ahead_repairs_cache_but_file_ahead_fails(
    tmp_path: Path,
) -> None:
    signer = _signer()
    anchors = MemoryRotationAnchor()
    path = _private_path(tmp_path)
    store = _store(tmp_path, anchors, path=path)
    authorized = _authorized(signer)
    commit_ready = _commit_ready(signer)
    store.publish(authorized, verifier=signer.verifier)

    anchors.values[ANCHOR_ID] = commit_ready.to_bytes()
    assert store.load(signer.verifier) == commit_ready
    assert path.read_bytes() == commit_ready.to_bytes()

    anchors.values[ANCHOR_ID] = authorized.to_bytes()
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_rollback"):
        store.load(signer.verifier)


@POSIX_STORE
def test_missing_anchor_never_bootstraps_from_owner_file(tmp_path: Path) -> None:
    signer = _signer()
    anchors = MemoryRotationAnchor()
    store = _store(tmp_path, anchors)
    record = _authorized(signer)
    store.publish(record, verifier=signer.verifier)
    anchors.values.clear()

    with pytest.raises(OliverAuthorityRotationError, match="anchor_missing"):
        store.load(signer.verifier)


@POSIX_STORE
def test_cache_rollback_and_two_revision_gap_fail_closed(tmp_path: Path) -> None:
    signer = _signer()
    anchors = MemoryRotationAnchor()
    path = _private_path(tmp_path)
    store = _store(tmp_path, anchors, path=path)
    authorized = _authorized(signer)
    commit_ready = _commit_ready(signer)
    terminal = commit_ready.complete(
        new_empty_store_digest=_digest("7"),
        qualification_evidence_digest=_digest("8"),
        updated_at_ms=NOW_MS + 2,
        signer=signer,
    )
    for record in (authorized, commit_ready, terminal):
        store.publish(record, verifier=signer.verifier)

    path.write_bytes(authorized.to_bytes())
    path.chmod(0o600)
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_rollback"):
        store.load(signer.verifier)


@POSIX_STORE
def test_foreign_anchor_context_and_signature_fail_closed(tmp_path: Path) -> None:
    signer = _signer()
    anchors = MemoryRotationAnchor()
    store = _store(tmp_path, anchors)
    foreign = _authorized(
        signer,
        old_install_id="00000000-0000-4000-8000-000000000902",
    )
    anchors.values[ANCHOR_ID] = foreign.to_bytes()
    with pytest.raises(OliverAuthorityRotationError, match="anchor_context"):
        store.load(signer.verifier)

    anchors.values[ANCHOR_ID] = _authorized(_signer(1)).to_bytes()
    with pytest.raises(OliverAuthorityRotationError, match="anchor_value"):
        store.load(signer.verifier)


@POSIX_STORE
def test_concurrent_conflicting_authorizations_have_one_winner(tmp_path: Path) -> None:
    signer = _signer()
    anchors = MemoryRotationAnchor()
    path = _private_path(tmp_path)
    store = _store(tmp_path, anchors, path=path)
    first = _authorized(signer)
    second = _authorized(
        signer,
        rotation_id="00000000-0000-4000-8000-000000000201",
        old_install_id="00000000-0000-4000-8000-000000000202",
        new_install_id="00000000-0000-4000-8000-000000000203",
    )

    def publish(record: OliverAuthorityRotationRecord) -> bool:
        try:
            store.publish(record, verifier=signer.verifier)
        except OliverAuthorityRotationError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(publish, (first, second)))

    assert sum(results) == 1
    assert store.load(signer.verifier) in {first, second}


@POSIX_STORE
def test_store_rejects_symlink_hardlink_and_insecure_directory(tmp_path: Path) -> None:
    signer = _signer()
    anchors = MemoryRotationAnchor()
    path = _private_path(tmp_path)
    path.symlink_to(tmp_path / "elsewhere")
    store = _store(tmp_path, anchors, path=path)
    anchors.values[ANCHOR_ID] = _authorized(signer).to_bytes()
    with pytest.raises(OliverAuthorityRotationError, match="store_file"):
        store.load(signer.verifier)

    path.unlink()
    path.write_bytes(_authorized(signer).to_bytes())
    path.chmod(0o600)
    os.link(path, path.with_suffix(".linked"))
    with pytest.raises(OliverAuthorityRotationError, match="store_file"):
        store.load(signer.verifier)

    path.unlink()
    path.with_suffix(".linked").unlink()
    path.parent.chmod(0o755)
    with pytest.raises(OliverAuthorityRotationError, match="store_directory"):
        store.load(signer.verifier)


@POSIX_STORE
def test_path_digest_is_exact_normalized_and_content_free(tmp_path: Path) -> None:
    path = tmp_path / "private" / "ada.sqlite3"
    digest = authority_rotation_path_digest(path)
    assert digest.startswith("sha256:")
    assert str(path) not in digest
    assert digest == authority_rotation_path_digest(str(path))
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_path"):
        authority_rotation_path_digest(Path("relative/ada.sqlite3"))
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_path"):
        authority_rotation_path_digest(path.parent / ".." / "private" / path.name)
    with pytest.raises(OliverAuthorityRotationError, match="authority_rotation_path"):
        authority_rotation_path_digest(Path("/"))


class FakeBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def test_grace_rotation_anchor_is_registered_context_bound_and_monotonic(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    key_store = KeyringKeyStore(
        backend,
        lease_manager=TargetLeaseManager(tmp_path / "leases"),
        clock_ms=lambda: NOW_MS,
    )
    key_store.initialize_fresh_credential_registry()
    anchors = GraceAuthorityRotationAnchorStore(key_store)
    signer = _signer()
    authorized = _authorized(signer)
    commit_ready = _commit_ready(signer)

    assert anchors.load(authorized.anchor_id) is None
    assert anchors.compare_and_set(
        authorized.anchor_id,
        expected_digest=None,
        value=authorized.to_bytes(),
    )
    assert anchors.load(authorized.anchor_id) == authorized.to_bytes()
    assert not anchors.compare_and_set(
        authorized.anchor_id,
        expected_digest=None,
        value=commit_ready.to_bytes(),
    )
    assert anchors.compare_and_set(
        authorized.anchor_id,
        expected_digest=authorized.digest,
        value=commit_ready.to_bytes(),
    )
    label = AUTHORITY_ROTATION_ANCHOR_LABEL_PREFIX + ANCHOR_ID.removeprefix(
        "sha256:"
    )
    assert label in (key_store.complete_inventory_labels() or ())
    assert ("algo-cli-runtime", label) in backend.values

    with pytest.raises(Exception, match="rotation_anchor_value"):
        anchors.compare_and_set(
            authorized.anchor_id,
            expected_digest=commit_ready.digest,
            value=_authorized(
                signer,
                old_install_id="00000000-0000-4000-8000-000000000902",
            ).to_bytes(),
        )


def test_rotation_foundation_is_not_runtime_wired_or_dispatch_enabled() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in ("algo_cli/main.py", "algo_cli/action_registry.py"):
        assert "oliver_authority_rotation" not in (root / relative).read_text(
            encoding="utf-8"
        )
    adapter = (
        root / "native/austin/Sources/AustinTCCAdapterMain/AustinTCCAdapter.swift"
    ).read_text(encoding="utf-8")
    production = (
        root
        / "native/austin/Sources/AustinTCCAdapter/AustinThomasProductionControl.swift"
    ).read_text(encoding="utf-8")
    assert "AustinThomasProductionControl.system" in adapter
    assert "loadControlActivation" in adapter
    assert "dispatcher: .disabledFoundation()" in production
    assert "oliver_authority_rotation" not in adapter + production


def test_canonical_payload_is_bounded() -> None:
    record = _authorized()
    assert record.to_bytes() == canonical_json_bytes(record.to_dict())
    assert len(record.to_bytes()) < 16 * 1024
