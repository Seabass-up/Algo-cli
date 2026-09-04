"""Persistent agent-thread storage and handoff tests."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess

import pytest

from algo_cli import agent_threads
from algo_cli.grace_memory_receipts import (
    ElsieReceiptAuthority,
    ElsieReceiptError,
    ReceiptNamespace,
    load_elsie_store_anchor,
)
from algo_cli.grace_key_store import StaticKeyStore
from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL


HMAC_RECEIPT = re.compile(r"hmac-sha256:[0-9a-f]{64}\Z")


def _authority(key: bytes = b"t" * 32) -> ElsieReceiptAuthority:
    return ElsieReceiptAuthority.from_key_store(store=StaticKeyStore({PRIVACY_KEY_LABEL: key}))


def _authority_and_store(
    key: bytes = b"t" * 32,
) -> tuple[ElsieReceiptAuthority, StaticKeyStore]:
    store = StaticKeyStore({PRIVACY_KEY_LABEL: key})
    return ElsieReceiptAuthority.from_key_store(store=store), store


def test_thread_lifecycle_and_prefix_resolution(tmp_path):
    path = tmp_path / "threads.json"
    record = agent_threads.create_thread(
        "Inspect the runtime",
        role="reviewer",
        pipeline="review",
        model="qwen3",
        path=path,
    )

    agent_threads.begin_turn(record["id"], "Inspect the runtime", path=path)
    finished = agent_threads.finish_turn(
        record["id"],
        status="complete",
        output="Verified output",
        blocks=[{"role": "review", "status": "complete"}],
        path=path,
    )

    assert finished["status"] == "complete"
    assert finished["turns"][-1]["output"] == "Verified output"
    assert agent_threads.resolve_thread(record["id"][:5], path=path)["id"] == record["id"]
    assert "Verified output" in agent_threads.context_handoff(finished)


def test_child_thread_is_linked_to_parent(tmp_path):
    path = tmp_path / "threads.json"
    parent = agent_threads.create_thread("Parent task", path=path)
    child = agent_threads.create_thread(
        "Child task",
        role="critic",
        pipeline="specialist",
        parent_id=parent["id"],
        path=path,
    )

    reloaded_parent = agent_threads.resolve_thread(parent["id"], path=path)

    assert child["parent_id"] == parent["id"]
    assert child["id"] in reloaded_parent["children"]


def test_unknown_and_ambiguous_thread_references_are_rejected(tmp_path, monkeypatch):
    path = tmp_path / "threads.json"
    values = iter(["abc11111", "abc22222"])
    monkeypatch.setattr(agent_threads, "_new_id", lambda _existing: next(values))
    agent_threads.create_thread("One", path=path)
    agent_threads.create_thread("Two", path=path)

    with pytest.raises(KeyError, match="ambiguous"):
        agent_threads.resolve_thread("abc", path=path)
    with pytest.raises(KeyError, match="Unknown"):
        agent_threads.resolve_thread("missing", path=path)


def test_thread_output_and_history_are_bounded(tmp_path):
    path = tmp_path / "threads.json"
    record = agent_threads.create_thread("Task", path=path)
    for index in range(agent_threads.MAX_THREAD_TURNS + 3):
        agent_threads.begin_turn(record["id"], f"turn {index}", path=path)
        agent_threads.finish_turn(
            record["id"],
            status="complete",
            output="x" * (agent_threads.MAX_THREAD_OUTPUT_CHARS + 100),
            path=path,
        )

    loaded = agent_threads.resolve_thread(record["id"], path=path)

    assert loaded["task"] == "Task"
    assert len(loaded["turns"]) == agent_threads.MAX_THREAD_TURNS
    assert len(loaded["output"]) == agent_threads.MAX_THREAD_OUTPUT_CHARS


def test_thread_workspace_preserves_initial_head_and_updates_git_evidence(tmp_path):
    path = tmp_path / "threads.json"
    initial = {
        "available": True,
        "workspace_root": "/workspace/feature",
        "repository_root": "/workspace/repo",
        "branch": "algo/feature",
        "head": "a" * 40,
        "clean": True,
    }
    record = agent_threads.create_thread("Task", workspace=initial, path=path)

    finished = agent_threads.finish_turn(
        record["id"],
        status="complete",
        workspace={
            **initial,
            "head": "b" * 40,
            "clean": False,
            "status": " M algo_cli/main.py",
            "status_digest": "e" * 64,
            "tracked_diff_digest": "c" * 64,
            "untracked_digest": "d" * 64,
        },
        path=path,
    )

    assert finished["workspace"]["initial_head"] == "a" * 40
    assert finished["workspace"]["head"] == "b" * 40
    assert finished["workspace"]["clean"] is False
    assert finished["workspace"]["status_digest"] == "e" * 64
    assert finished["workspace"]["tracked_diff_digest"] == "c" * 64
    handoff = agent_threads.context_handoff(finished)
    assert "algo/feature" in handoff
    assert "Initial HEAD" in handoff
    assert "/workspace/feature" not in handoff


def test_version_one_thread_store_migrates_without_losing_records(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(
        '{"version": 1, "threads": [{"id": "legacy01", "status": "complete", '
        '"task": "old task", "turns": [], "blocks": [], "children": []}]}',
        encoding="utf-8",
    )

    records = agent_threads.load_threads(path)

    assert records[0]["id"] == "legacy01"
    assert records[0]["workspace"] == {}
    assert records[0]["run_contract"] == {}
    assert records[0]["checkpoint"] == {}


def test_version_two_thread_store_migrates_without_losing_records(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(
        '{"version": 2, "threads": [{"id": "legacy02", "status": "partial", '
        '"task": "old task", "turns": [], "blocks": [], "children": [], '
        '"workspace": {"available": true, "head": "' + ("a" * 40) + '"}}]}',
        encoding="utf-8",
    )

    records = agent_threads.load_threads(path)

    assert records[0]["id"] == "legacy02"
    assert records[0]["workspace"]["head"] == "a" * 40
    assert records[0]["run_contract"] == {}
    assert records[0]["checkpoint"] == {}


def test_run_contract_checkpoint_and_block_context_round_trip_bounded(tmp_path):
    path = tmp_path / "threads.json"
    record = agent_threads.create_thread(
        "Sensitive original task",
        run_contract={
            "contract_id": "run-contract-v1:" + ("a" * 64),
            "digest": "a" * 64,
            "run_nonce": "nonce1234",
            "mode": "enforced",
            "approval_mode": "never",
            "journal_file": "nonce1234.jsonl",
            "task": "must not be copied",
        },
        checkpoint={
            "next_block_ordinal": 1,
            "last_verified_sequence": 7,
            "uncertain_mutation_steps": ["b1-r0-t0"],
            "terminal": False,
        },
        path=path,
    )
    context = "x" * (agent_threads.MAX_BLOCK_CONTEXT_CHARS + 100)

    finished = agent_threads.finish_turn(
        record["id"],
        status="partial",
        blocks=[
            {
                "role": "plan",
                "status": "complete",
                "context_output": context,
                "tool_calls": 3,
            }
        ],
        run_contract=record["run_contract"],
        checkpoint={
            "next_block_ordinal": 1,
            "last_verified_sequence": 9,
            "uncertain_mutation_steps": [],
            "terminal": True,
            "terminal_status": "partial",
        },
        path=path,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["version"] == agent_threads.THREADS_SCHEMA_VERSION
    assert finished["run_contract"] == {
        "contract_id": "run-contract-v1:" + ("a" * 64),
        "digest": "a" * 64,
        "run_nonce": "nonce1234",
        "mode": "enforced",
        "approval_mode": "never",
        "journal_file": "nonce1234.jsonl",
    }
    assert "task" not in finished["run_contract"]
    assert finished["checkpoint"]["next_block_ordinal"] == 1
    assert finished["checkpoint"]["last_verified_sequence"] == 9
    assert finished["checkpoint"]["terminal"] is True
    assert len(finished["blocks"][0]["context_output"]) == (agent_threads.MAX_BLOCK_CONTEXT_CHARS)


def test_missing_clean_evidence_stays_unknown_instead_of_false(tmp_path):
    path = tmp_path / "threads.json"
    record = agent_threads.create_thread(
        "Read-only task",
        workspace={
            "available": True,
            "workspace_root": "/workspace",
            "branch": "feature/read",
            "head": "a" * 40,
        },
        path=path,
    )

    loaded = agent_threads.resolve_thread(record["id"], path=path)

    assert "clean" not in loaded["workspace"]


def test_protected_thread_lifecycle_persists_only_structure_and_keyed_receipts(
    tmp_path,
) -> None:
    path = tmp_path / "threads.json"
    authority = _authority()
    canary = "PROTECTED_AGENT_THREAD_CANARY"
    low_entropy_digest = hashlib.sha256(b" M protected.py").hexdigest()
    record = agent_threads.create_thread(
        f"Task {canary}",
        title=f"Title {canary}",
        workspace={
            "available": True,
            "workspace_root": f"/private/{canary}",
            "branch": f"branch-{canary}",
            "head": "a" * 40,
            "status_digest": low_entropy_digest,
            "tracked_diff_digest": hashlib.sha256(b"diff").hexdigest(),
            "untracked_digest": hashlib.sha256(b"none").hexdigest(),
        },
        start_turn=True,
        protected=True,
        receipt_authority=authority,
        path=path,
    )
    finished = agent_threads.finish_turn(
        record["id"],
        status="complete",
        output=f"Output {canary}",
        error=f"Error {canary}",
        blocks=[
            {
                "role": "review",
                "status": "complete",
                "status_reason": f"Reason {canary}",
                "context_output": f"Context {canary}",
                "verification_warning": f"Warning {canary}",
                "successful_writes": [f"/private/{canary}"],
                "status_digest": low_entropy_digest,
                "tracked_diff_digest": hashlib.sha256(b"diff").hexdigest(),
                "untracked_digest": hashlib.sha256(b"none").hexdigest(),
            }
        ],
        protected=True,
        receipt_authority=authority,
        path=path,
    )

    serialized = path.read_text(encoding="utf-8")
    assert canary not in serialized
    assert low_entropy_digest not in serialized
    assert finished["protected_memory_authority"] is True
    assert finished["task"] == ""
    assert finished["output"] == ""
    assert finished["error"] == ""
    assert HMAC_RECEIPT.fullmatch(finished["task_receipt"])
    assert HMAC_RECEIPT.fullmatch(finished["output_receipt"])
    assert HMAC_RECEIPT.fullmatch(finished["error_receipt"])
    assert HMAC_RECEIPT.fullmatch(finished["blocks"][0]["context_output_receipt"])
    assert finished["blocks"][0]["successful_writes"] == []
    assert finished["blocks"][0]["successful_write_count"] == 1
    assert "workspace_root" not in finished["workspace"]
    assert "branch" not in finished["workspace"]
    assert HMAC_RECEIPT.fullmatch(finished["workspace"]["status_receipt"])
    assert HMAC_RECEIPT.fullmatch(finished["blocks"][0]["status_receipt"])
    assert finished["workspace"]["status_receipt"] != ("hmac-sha256:" + low_entropy_digest)
    document = json.loads(serialized)
    assert document["receipt_binding"] == authority.binding.as_dict()
    assert document["store_sequence"] == 2
    assert HMAC_RECEIPT.fullmatch(document["store_receipt"])


def test_protected_load_scrubs_legacy_thread_and_preserves_receipts_idempotently(
    tmp_path,
) -> None:
    path = tmp_path / "threads.json"
    authority = _authority()
    canary = "LEGACY_AGENT_THREAD_CANARY"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "threads": [
                    {
                        "id": "legacy01",
                        "status": "complete",
                        "title": canary,
                        "task": canary,
                        "output": canary,
                        "error": canary,
                        "turns": [
                            {
                                "status": "complete",
                                "task": canary,
                                "output": canary,
                                "error": canary,
                            }
                        ],
                        "blocks": [
                            {
                                "role": "review",
                                "status": "complete",
                                "context_output": canary,
                                "status_reason": canary,
                                "verification_warning": canary,
                            }
                        ],
                        "children": [],
                        "workspace": {
                            "available": True,
                            "workspace_root": f"/private/{canary}",
                            "branch": canary,
                            "head": "b" * 40,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert (
        agent_threads.prepare_protected_thread_store(
            path,
            receipt_authority=authority,
        )
        is True
    )
    first = agent_threads.load_threads(
        path,
        protected=True,
        receipt_authority=authority,
    )
    first_serialized = path.read_text(encoding="utf-8")
    second = agent_threads.load_threads(
        path,
        protected=True,
        receipt_authority=authority,
    )

    assert canary not in first_serialized
    assert first == second
    assert HMAC_RECEIPT.fullmatch(first[0]["task_receipt"])
    assert HMAC_RECEIPT.fullmatch(first[0]["turns"][0]["output_receipt"])
    assert HMAC_RECEIPT.fullmatch(first[0]["blocks"][0]["context_output_receipt"])
    assert "workspace_root" not in first[0]["workspace"]

    document = json.loads(first_serialized)
    document["threads"][0]["task_receipt"] = "sha256:" + ("0" * 64)
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ElsieReceiptError, match="store authentication failed"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )
    assert path.read_bytes() == before


def test_protected_thread_store_rejects_wrong_or_missing_receipt_binding(tmp_path) -> None:
    path = tmp_path / "threads.json"
    authority = _authority(b"a" * 32)
    agent_threads.create_thread(
        "protected task",
        protected=True,
        path=path,
        receipt_authority=authority,
    )
    original = path.read_bytes()

    with pytest.raises(ElsieReceiptError, match="binding mismatch"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=_authority(b"b" * 32),
        )
    assert path.read_bytes() == original

    document = json.loads(original)
    document.pop("receipt_binding")
    path.write_text(json.dumps(document), encoding="utf-8")
    missing_binding = path.read_bytes()
    with pytest.raises(ElsieReceiptError, match="store fields are invalid"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )
    assert path.read_bytes() == missing_binding


def test_legacy_protected_thread_store_is_rejected_without_rewrite(tmp_path) -> None:
    path = tmp_path / "threads.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "threads": [
                    {
                        "id": "legacy-protected",
                        "status": "complete",
                        "protected_memory_authority": True,
                        "task": "",
                        "task_receipt": "hmac-sha256:" + ("0" * 64),
                        "turns": [],
                        "blocks": [],
                        "children": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original = path.read_bytes()

    with pytest.raises(ElsieReceiptError, match="legacy protected"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=_authority(),
        )
    assert path.read_bytes() == original


def test_empty_protected_thread_read_does_not_create_key_or_store(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "threads.json"

    existing_lookups: list[bool] = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("protected read attempted to create a key")

    def missing_existing(*_args, **_kwargs):
        existing_lookups.append(True)
        return None

    monkeypatch.setattr(agent_threads.ElsieReceiptAuthority, "from_key_store", forbidden)
    monkeypatch.setattr(
        agent_threads.ElsieReceiptAuthority,
        "from_optional_existing_key_store",
        classmethod(lambda _cls, **_kwargs: missing_existing()),
    )

    assert agent_threads.load_threads(path, protected=True) == []
    assert existing_lookups == [True]
    assert not path.exists()
    assert not path.with_suffix(".json.lock").exists()


def test_missing_protected_thread_store_propagates_receipt_backend_failure(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "threads.json"

    def unavailable(*_args, **_kwargs):
        raise ElsieReceiptError("existing persistent elsie key is unavailable")

    monkeypatch.setattr(
        agent_threads.ElsieReceiptAuthority,
        "from_optional_existing_key_store",
        classmethod(lambda _cls, **_kwargs: unavailable()),
    )

    with pytest.raises(ElsieReceiptError, match="unavailable"):
        agent_threads.load_threads(path, protected=True)
    with pytest.raises(ElsieReceiptError, match="unavailable"):
        agent_threads.prepare_protected_thread_store(path)
    assert not path.exists()


def test_protected_thread_store_rejects_local_rewrite_and_rollback(tmp_path) -> None:
    path = tmp_path / "threads.json"
    authority = _authority()
    record = agent_threads.create_thread(
        "protected task",
        protected=True,
        receipt_authority=authority,
        path=path,
    )
    first = path.read_bytes()

    rewritten = json.loads(first)
    rewritten["threads"][0]["status"] = "complete"
    path.write_text(json.dumps(rewritten), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ElsieReceiptError, match="store authentication failed"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )
    assert path.read_bytes() == before

    path.write_bytes(first)
    agent_threads.update_thread(
        record["id"],
        status="complete",
        protected=True,
        receipt_authority=authority,
        path=path,
    )
    latest = path.read_bytes()
    path.write_bytes(first)
    with pytest.raises(ElsieReceiptError, match="rollback or rewrite"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )
    assert path.read_bytes() == first
    assert latest != first


def test_protected_thread_store_rejects_missing_external_anchor(tmp_path) -> None:
    path = tmp_path / "threads.json"
    authority, store = _authority_and_store()
    agent_threads.create_thread(
        "protected task",
        protected=True,
        receipt_authority=authority,
        path=path,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    head = load_elsie_store_anchor(
        authority,
        ReceiptNamespace.AGENT_THREAD_STORE,
        subject=str(path.absolute()),
    )
    assert head is not None
    store.delete_anchor(head.journal_id)
    before = path.read_bytes()

    with pytest.raises(ElsieReceiptError, match="rollback or rewrite"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )

    assert path.read_bytes() == before
    assert document["store_sequence"] == 1


def test_protected_thread_store_recovers_post_anchor_publication_failure(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "threads.json"
    authority = _authority()
    original_atomic_write = agent_threads.config._atomic_write_text
    failed = False

    def fail_target_once(target, content):
        nonlocal failed
        if target == path and not failed:
            failed = True
            raise OSError("simulated post-anchor publication failure")
        return original_atomic_write(target, content)

    monkeypatch.setattr(
        agent_threads.config,
        "_atomic_write_text",
        fail_target_once,
    )
    with pytest.raises(OSError, match="post-anchor"):
        agent_threads.create_thread(
            "PROTECTED_RECOVERY_CANARY",
            protected=True,
            receipt_authority=authority,
            path=path,
        )
    pending = agent_threads._pending_store_path(path)
    assert not path.exists()
    assert pending.is_file()

    monkeypatch.setattr(
        agent_threads.config,
        "_atomic_write_text",
        original_atomic_write,
    )
    with pytest.raises(ElsieReceiptError, match="recovery is required"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )
    assert (
        agent_threads.prepare_protected_thread_store(
            path,
            receipt_authority=authority,
        )
        is True
    )
    recovered = agent_threads.load_threads(
        path,
        protected=True,
        receipt_authority=authority,
    )

    assert len(recovered) == 1
    assert recovered[0]["task"] == ""
    assert HMAC_RECEIPT.fullmatch(recovered[0]["task_receipt"])
    assert "PROTECTED_RECOVERY_CANARY" not in path.read_text(encoding="utf-8")
    assert not pending.exists()


def test_protected_thread_store_discards_committed_stage_after_unlink_failure(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "threads.json"
    authority = _authority()
    original_discard = agent_threads._discard_pending_store
    failed = False

    def fail_once(target):
        nonlocal failed
        if target == path and not failed:
            failed = True
            raise OSError("simulated post-publication unlink failure")
        return original_discard(target)

    monkeypatch.setattr(agent_threads, "_discard_pending_store", fail_once)
    with pytest.raises(OSError, match="post-publication"):
        agent_threads.create_thread(
            "COMMITTED_STAGE_CANARY",
            protected=True,
            receipt_authority=authority,
            path=path,
        )
    pending = agent_threads._pending_store_path(path)
    assert path.is_file()
    assert pending.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(pending.read_text(encoding="utf-8"))

    monkeypatch.setattr(agent_threads, "_discard_pending_store", original_discard)
    with pytest.raises(ElsieReceiptError, match="recovery is required"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )
    assert (
        agent_threads.prepare_protected_thread_store(
            path,
            receipt_authority=authority,
        )
        is True
    )
    assert not pending.exists()
    assert (
        len(
            agent_threads.load_threads(
                path,
                protected=True,
                receipt_authority=authority,
            )
        )
        == 1
    )


def test_protected_thread_store_rejects_swapped_committed_stage(
    tmp_path,
) -> None:
    path = tmp_path / "threads.json"
    authority = _authority()
    agent_threads.create_thread(
        "protected task",
        protected=True,
        receipt_authority=authority,
        path=path,
    )
    pending = agent_threads._pending_store_path(path)
    swapped = json.loads(path.read_text(encoding="utf-8"))
    swapped["threads"][0]["status"] = "complete"
    agent_threads.config._atomic_write_text(pending, json.dumps(swapped))

    with pytest.raises(ElsieReceiptError, match="authentication failed"):
        agent_threads.prepare_protected_thread_store(
            path,
            receipt_authority=authority,
        )
    assert pending.exists()


def test_windows_pending_store_uses_identity_and_dacl_not_projected_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "threads.json"
    pending = agent_threads._pending_store_path(path)
    pending.write_text("{}", encoding="utf-8")
    pending.chmod(0o666)
    monkeypatch.setattr(agent_threads.os, "name", "nt")
    monkeypatch.setattr(agent_threads.config, "_path_is_reparse_point", lambda *_args: False)
    monkeypatch.setattr(agent_threads.config, "_windows_private_dacl", lambda _path: True)

    assert agent_threads._pending_store_exists(path) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows protected pending-store DACL contract")
def test_windows_pending_store_requires_private_dacl(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    pending = agent_threads._pending_store_path(path)
    agent_threads.config._atomic_write_text(pending, "{}")

    assert agent_threads._pending_store_exists(path) is True
    system_root = Path(os.environ["SystemRoot"])
    icacls = system_root / "System32" / "icacls.exe"
    granted = subprocess.run(
        [str(icacls), str(pending), "/grant", "*S-1-1-0:R"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert granted.returncode == 0, granted.stderr.decode(errors="replace")

    with pytest.raises(ElsieReceiptError, match="recovery state is unsafe"):
        agent_threads._pending_store_exists(path)


@pytest.mark.parametrize("existing", [False, True])
def test_protected_thread_store_discards_stage_after_definite_pre_cas_failure(
    tmp_path,
    monkeypatch,
    existing: bool,
) -> None:
    path = tmp_path / "threads.json"
    authority = _authority()
    record = None
    before = b""
    if existing:
        record = agent_threads.create_thread(
            "existing protected task",
            protected=True,
            receipt_authority=authority,
            path=path,
        )
        before = path.read_bytes()

    def refuse_before_cas(*_args, **_kwargs):
        raise ElsieReceiptError("simulated pre-CAS refusal")

    monkeypatch.setattr(
        agent_threads,
        "advance_elsie_store_anchor",
        refuse_before_cas,
    )
    with pytest.raises(ElsieReceiptError, match="pre-CAS"):
        if record is None:
            agent_threads.create_thread(
                "first protected task",
                protected=True,
                receipt_authority=authority,
                path=path,
            )
        else:
            agent_threads.update_thread(
                record["id"],
                status="complete",
                protected=True,
                receipt_authority=authority,
                path=path,
            )

    assert not agent_threads._pending_store_path(path).exists()
    assert path.read_bytes() == before if existing else not path.exists()

    monkeypatch.undo()
    created = agent_threads.create_thread(
        "subsequent protected task",
        protected=True,
        receipt_authority=authority,
        path=path,
    )
    assert created["protected_memory_authority"] is True


def test_protected_thread_store_rejects_deletion_corruption_and_symlink(
    tmp_path,
) -> None:
    path = tmp_path / "threads.json"
    authority = _authority()
    agent_threads.create_thread(
        "protected task",
        protected=True,
        receipt_authority=authority,
        path=path,
    )
    original = path.read_bytes()

    path.unlink()
    with pytest.raises(ElsieReceiptError, match="missing or rolled back"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )

    path.write_text("{", encoding="utf-8")
    corrupt = path.read_bytes()
    with pytest.raises(ElsieReceiptError, match="store is malformed"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )
    assert path.read_bytes() == corrupt

    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(original)
    path.symlink_to(outside)
    with pytest.raises(ElsieReceiptError, match="store is unavailable"):
        agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
        )
    assert outside.read_bytes() == original
