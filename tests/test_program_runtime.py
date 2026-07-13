from __future__ import annotations

import dataclasses
import json

import pytest

from algo_cli.config import Config
from algo_cli.program_runtime import (
    ProgramArtifactStore,
    ProgramAuthorization,
    ProgramLimits,
    ProgramValidationError,
    authorization_for_actions,
    compile_program,
    execute_program,
    verify_receipt_chain,
)


def _authorization(*actions: str, force: tuple[str, ...] = ()) -> ProgramAuthorization:
    return ProgramAuthorization(frozenset(actions), frozenset(force))


def _read_plan(*, outputs=None):
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "source",
                "kind": "action",
                "action": "read_file",
                "args": {"path": "payload.json"},
            }
        ],
    }
    if outputs is not None:
        plan["outputs"] = outputs
    return plan


def test_compile_rejects_forward_refs_and_recursive_meta_calls() -> None:
    authorization = _authorization("read_file", "session_command")
    forward = {
        "version": 1,
        "steps": [
            {
                "id": "first",
                "kind": "action",
                "action": "read_file",
                "args": {"path": {"$ref": "later"}},
            },
            {
                "id": "later",
                "kind": "transform",
                "op": "json_stringify",
                "input": "payload.json",
            },
        ],
    }
    recursive = {
        "version": 1,
        "steps": [
            {
                "id": "nested",
                "kind": "action",
                "action": "session_command",
                "args": {"command": "/actions"},
            }
        ],
    }

    with pytest.raises(ProgramValidationError, match="earlier step"):
        compile_program(forward, authorization=authorization)
    with pytest.raises(ProgramValidationError, match="meta action"):
        compile_program(recursive, authorization=authorization)


def test_runtime_authorization_filters_non_composable_meta_actions() -> None:
    authorization = authorization_for_actions(
        ("read_file", "action_program", "action_search", "session_command", "plugins_load")
    )

    assert authorization.allowed_actions == frozenset({"read_file"})


def test_compile_enforces_capability_ceiling_and_step_limit() -> None:
    plan = _read_plan()
    with pytest.raises(ProgramValidationError, match="capability ceiling"):
        compile_program(plan, authorization=_authorization("git_status"))

    plan["steps"] *= 2
    plan["steps"][1] = {**plan["steps"][1], "id": "source_two"}
    with pytest.raises(ProgramValidationError, match="maximum is 1"):
        compile_program(
            plan,
            authorization=_authorization("read_file"),
            limits=ProgramLimits(max_steps=1),
        )


def test_compile_caps_plan_bytes_and_output_count() -> None:
    authorization = _authorization("read_file")
    oversized = _read_plan()
    oversized["steps"][0]["args"]["padding"] = "x" * 200
    with pytest.raises(ProgramValidationError, match="bytes; maximum"):
        compile_program(
            oversized,
            authorization=authorization,
            limits=ProgramLimits(max_plan_bytes=100),
        )

    too_many_outputs = _read_plan(outputs=["source", "source"])
    with pytest.raises(ProgramValidationError, match="outputs; maximum is 1"):
        compile_program(
            too_many_outputs,
            authorization=authorization,
            limits=ProgramLimits(max_outputs=1),
        )


def test_execute_dispatches_action_through_runtime_and_runs_deterministic_transforms(tmp_path) -> None:
    payload = {
        "rows": [
            {"name": "slow", "score": 2, "ready": True},
            {"name": "skip", "score": 9, "ready": False},
            {"name": "fast", "score": 7, "ready": True},
        ]
    }
    (tmp_path / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    cfg = Config(cwd=str(tmp_path))
    plan = {
        "version": 1,
        "steps": [
            {"id": "source", "kind": "action", "action": "read_file", "args": {"path": "payload.json"}},
            {"id": "parsed", "kind": "transform", "op": "json_parse", "input": {"$ref": "source"}},
            {
                "id": "rows",
                "kind": "transform",
                "op": "get",
                "input": {"$ref": "parsed"},
                "args": {"path": ["rows"]},
            },
            {
                "id": "ready",
                "kind": "transform",
                "op": "filter_eq",
                "input": {"$ref": "rows"},
                "args": {"path": ["ready"], "equals": True},
            },
            {
                "id": "ranked",
                "kind": "transform",
                "op": "sort",
                "input": {"$ref": "ready"},
                "args": {"path": ["score"], "descending": True},
            },
            {
                "id": "top",
                "kind": "transform",
                "op": "take",
                "input": {"$ref": "ranked"},
                "args": {"count": 1},
            },
        ],
        "outputs": [{"$ref": "top", "path": [0, "name"]}],
    }

    result = execute_program(
        plan,
        cfg,
        authorization=_authorization("read_file"),
        store=ProgramArtifactStore(tmp_path / "program-store"),
    )

    assert result.worked is True
    assert result.outputs[0].preview == "fast"
    assert [receipt.operation for receipt in result.receipts] == [
        "read_file",
        "json_parse",
        "get",
        "filter_eq",
        "sort",
        "take",
    ]
    assert cfg.attempt_ledger[-1]["tool"] == "read_file"
    assert cfg.attempt_ledger[-1]["status"] == "worked"
    assert verify_receipt_chain(result.receipts) is True
    assert result.receipt_uri.startswith("receipt://sha256/")


def test_numeric_sort_uses_numeric_not_lexicographic_order(tmp_path) -> None:
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "sorted",
                "kind": "transform",
                "op": "sort",
                "input": [2, 10, 1],
                "args": {},
            }
        ],
    }

    result = execute_program(
        plan,
        Config(cwd=str(tmp_path)),
        authorization=_authorization(),
        store=ProgramArtifactStore(tmp_path / "program-store"),
    )

    assert result.outputs[0].preview == "[1,2,10]"


def test_large_action_output_is_artifact_backed_and_integrity_checked(tmp_path) -> None:
    content = "abcdefghij" * 100
    (tmp_path / "payload.json").write_text(content, encoding="utf-8")
    store = ProgramArtifactStore(tmp_path / "program-store")
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        limits=ProgramLimits(artifact_threshold_bytes=64, output_preview_chars=20),
        store=store,
    )

    assert result.worked is True
    assert result.outputs[0].preview.startswith(content[:20])
    assert result.outputs[0].artifact is not None
    assert store.read(result.outputs[0].artifact) == content.encode("utf-8")
    assert result.receipts[0].artifact_uri == result.outputs[0].artifact.uri
    assert result.receipts[0].result_hash == result.outputs[0].sha256


def test_intermediate_byte_limit_stops_program_but_preserves_artifact_receipt(tmp_path) -> None:
    (tmp_path / "payload.json").write_text("x" * 200, encoding="utf-8")
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        limits=ProgramLimits(max_intermediate_bytes=100, artifact_threshold_bytes=32),
        store=ProgramArtifactStore(tmp_path / "program-store"),
    )

    assert result.status == "limit_exceeded"
    assert result.outputs == ()
    assert "intermediate values" in result.error
    assert result.receipts[0].status == "limit_exceeded"
    assert result.receipts[0].artifact_uri.startswith("artifact://sha256/")
    assert verify_receipt_chain(result.receipts) is True


def test_force_approval_flag_is_passed_to_canonical_dispatch(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    calls: list[tuple[str, bool]] = []

    def fake_execute(name, args, cfg, *, tool_call_id=None, force_approval=False):
        calls.append((name, force_approval))
        return ({"role": "tool", "content": "ok"}, "ok")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    (tmp_path / "payload.json").write_text("unused", encoding="utf-8")
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file", force=("read_file",)),
        store=ProgramArtifactStore(tmp_path / "program-store"),
    )

    assert result.worked is True
    assert calls == [("read_file", True)]


def test_mutation_keeps_per_action_approval_and_immutable_compact_receipt(monkeypatch, tmp_path) -> None:
    from algo_cli import tool_runtime

    approvals: list[str] = []

    def approve(name, args, cfg, *, force=False):
        approvals.append(name)
        return True

    monkeypatch.setattr(tool_runtime, "ask_approval", approve)
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "write",
                "kind": "action",
                "action": "write_file",
                "args": {"path": "created.txt", "content": "verified receipt"},
            }
        ],
        "outputs": [{"$ref": "write"}],
    }

    result = execute_program(
        plan,
        Config(cwd=str(tmp_path)),
        authorization=_authorization("write_file"),
        store=ProgramArtifactStore(tmp_path / "program-store"),
    )
    compact = result.to_dict(compact=True)

    assert result.worked is True
    assert approvals == ["write_file"]
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "verified receipt"
    assert result.receipts[0].mutates_state is True
    assert compact["mutation_receipts"][0]["operation"] == "write_file"
    assert compact["mutation_receipts"][0]["receipt_hash"] == result.receipts[0].receipt_hash
    assert verify_receipt_chain(result.receipts) is True


def test_receipts_are_frozen_and_tampering_breaks_hash_chain(tmp_path) -> None:
    (tmp_path / "payload.json").write_text("ok", encoding="utf-8")
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        store=ProgramArtifactStore(tmp_path / "program-store"),
    )
    receipt = result.receipts[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.status = "failed"  # type: ignore[misc]
    tampered = dataclasses.replace(receipt, result_bytes=receipt.result_bytes + 1)
    assert verify_receipt_chain((tampered,)) is False


def test_compact_result_keeps_chain_and_mutation_receipts_without_verbose_steps(tmp_path) -> None:
    (tmp_path / "payload.json").write_text("ok", encoding="utf-8")
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        store=ProgramArtifactStore(tmp_path / "program-store"),
    )

    compact = result.to_dict(compact=True)

    assert "receipts" not in compact
    assert compact["receipt_count"] == 1
    assert compact["mutation_receipts"] == []
    assert compact["receipt_chain_hash"] == result.receipt_chain_hash


def test_model_callable_wrapper_requires_runtime_owned_authorization(tmp_path) -> None:
    from algo_cli.tools import action_program

    (tmp_path / "payload.json").write_text("ok", encoding="utf-8")
    cfg = Config(cwd=str(tmp_path))

    unbound = json.loads(action_program(_read_plan(), cfg))
    assert unbound["status"] == "error"
    assert "authorization was not bound" in unbound["error"]

    setattr(cfg, "_algo_program_authorization", authorization_for_actions(("git_status",)))
    denied = json.loads(action_program(_read_plan(), cfg))
    assert denied["status"] == "error"
    assert "capability ceiling" in denied["error"]

    setattr(cfg, "_algo_program_authorization", authorization_for_actions(("read_file",)))
    worked = json.loads(action_program(_read_plan(), cfg))
    assert worked["status"] == "worked"
    assert worked["receipt_count"] == 1
    assert "receipts" not in worked
