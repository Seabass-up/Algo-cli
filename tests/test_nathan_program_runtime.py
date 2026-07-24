from __future__ import annotations

import dataclasses
import json

import pytest

from algo_cli.arthur_outcomes import ActionOutcome, OutcomeStatus, VerificationStatus
from algo_cli.config import Config
from algo_cli.grace_key_store import StaticKeyStore
from algo_cli.james_dispatch import DispatchCancellation
from algo_cli.nathan_runtime import PipelineToolResult
from algo_cli.nathan_program_runtime import (
    ProgramArtifactStore,
    ProgramAuthorization,
    ProgramLimits,
    ProgramStoreError,
    ProgramValidationError,
    authorization_for_actions,
    compile_program,
    execute_program,
    verify_receipt_chain,
)


def _authorization(*actions: str, force: tuple[str, ...] = ()) -> ProgramAuthorization:
    return ProgramAuthorization(frozenset(actions), frozenset(force))


def _program_store(tmp_path) -> ProgramArtifactStore:
    return ProgramArtifactStore(
        tmp_path / "program-store",
        key_store=StaticKeyStore({"alice-artifact-master-v1": b"nathan-test-artifact-key-material"[:32]}),
    )


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
        store=_program_store(tmp_path),
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
        store=_program_store(tmp_path),
    )

    assert result.outputs[0].preview == "[1,2,10]"


def test_large_action_output_is_artifact_backed_and_integrity_checked(tmp_path) -> None:
    content = "abcdefghij" * 100
    (tmp_path / "payload.json").write_text(content, encoding="utf-8")
    store = _program_store(tmp_path)
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
    assert result.outputs[0].artifact.run_id == result.run_id
    assert result.outputs[0].artifact.uri.startswith("artifact://private/v1/")
    assert result.receipts[0].artifact_uri == result.outputs[0].artifact.uri
    assert result.receipts[0].result_hash == result.outputs[0].sha256
    assert not (tmp_path / "program-store" / "artifacts").exists()
    stored_payloads = [path.read_bytes() for path in (tmp_path / "program-store").rglob("*") if path.is_file()]
    assert all(content.encode("utf-8") not in payload for payload in stored_payloads)


def test_reused_program_store_keeps_artifact_capabilities_run_scoped(tmp_path) -> None:
    content = "run scoped" * 100
    (tmp_path / "payload.json").write_text(content, encoding="utf-8")
    store = _program_store(tmp_path)
    limits = ProgramLimits(artifact_threshold_bytes=32)

    first = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        limits=limits,
        store=store,
    )
    second = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        limits=limits,
        store=store,
    )

    first_ref = first.outputs[0].artifact
    second_ref = second.outputs[0].artifact
    assert first_ref is not None
    assert second_ref is not None
    assert first_ref.run_id == first.run_id
    assert second_ref.run_id == second.run_id
    assert first_ref.run_id != second_ref.run_id
    assert first_ref.artifact_id != second_ref.artifact_id
    assert store.read(first_ref) == content.encode("utf-8")
    assert store.read(second_ref) == content.encode("utf-8")


def test_compact_program_does_not_require_or_create_artifact_key(tmp_path) -> None:
    class BrokenKeyStore:
        def get_or_create(self, *_args, **_kwargs):
            raise RuntimeError("must not be called")

    store = ProgramArtifactStore(
        tmp_path / "program-store",
        key_store=BrokenKeyStore(),
    )
    result = execute_program(
        {
            "version": 1,
            "steps": [
                {
                    "id": "small",
                    "kind": "transform",
                    "op": "json_stringify",
                    "input": {"ok": True},
                }
            ],
        },
        Config(cwd=str(tmp_path)),
        authorization=_authorization(),
        store=store,
    )

    assert result.worked is True
    assert result.outputs[0].artifact is None
    assert list((tmp_path / "program-store" / "alice-artifacts-v1" / "runs").iterdir()) == []


def test_legacy_plaintext_artifacts_are_safely_purged_on_upgrade(tmp_path) -> None:
    root = tmp_path / "program-store"
    bucket = root / "artifacts" / "aa"
    bucket.mkdir(parents=True)
    marker = b"LEGACY-PLAINTEXT-MUST-NOT-REMAIN"
    (bucket / ("a" * 64 + ".blob")).write_bytes(marker)

    store = _program_store(tmp_path)

    assert store.legacy_plaintext_artifacts_removed == 1
    assert not (root / "artifacts").exists()
    assert all(marker not in path.read_bytes() for path in root.rglob("*") if path.is_file())


def test_unknown_legacy_artifact_shape_blocks_before_deleting_anything(tmp_path) -> None:
    root = tmp_path / "program-store"
    bucket = root / "artifacts" / "aa"
    bucket.mkdir(parents=True)
    valid = bucket / ("a" * 64 + ".blob")
    unknown = bucket / "user-note.txt"
    valid.write_bytes(b"legacy artifact")
    unknown.write_text("do not delete", encoding="utf-8")

    with pytest.raises(ProgramStoreError, match="unknown shape"):
        _program_store(tmp_path)

    assert valid.exists()
    assert unknown.read_text(encoding="utf-8") == "do not delete"


def test_intermediate_byte_limit_stops_program_but_preserves_artifact_receipt(tmp_path) -> None:
    (tmp_path / "payload.json").write_text("x" * 200, encoding="utf-8")
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        limits=ProgramLimits(max_intermediate_bytes=100, artifact_threshold_bytes=32),
        store=_program_store(tmp_path),
    )

    assert result.status == "limit_exceeded"
    assert result.outputs == ()
    assert "intermediate values" in result.error
    assert result.receipts[0].status == "limit_exceeded"
    assert result.receipts[0].artifact_uri.startswith("artifact://private/v1/")
    assert verify_receipt_chain(result.receipts) is True


def test_force_approval_flag_is_passed_to_canonical_dispatch(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    calls: list[tuple[str, bool]] = []

    def fake_execute(
        name,
        args,
        cfg,
        *,
        tool_call_id=None,
        force_approval=False,
        deadline_monotonic=None,
        cancellation=None,
    ):
        del cfg, tool_call_id, deadline_monotonic, cancellation
        calls.append((name, force_approval))
        return ({"role": "tool", "content": "ok"}, "ok")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    (tmp_path / "payload.json").write_text("unused", encoding="utf-8")
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file", force=("read_file",)),
        store=_program_store(tmp_path),
    )

    assert result.worked is True
    assert calls == [("read_file", True)]


def test_timeout_aware_action_is_clamped_to_remaining_program_budget(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    calls: list[tuple[str, dict]] = []
    clock_values = iter((100.0, 102.0, 103.0))

    def fake_execute(
        name,
        args,
        cfg,
        *,
        tool_call_id=None,
        force_approval=False,
        deadline_monotonic=None,
        cancellation=None,
    ):
        del cfg, tool_call_id, force_approval, deadline_monotonic, cancellation
        calls.append((name, args))
        return ({"role": "tool", "content": "ok"}, "ok")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "command",
                "kind": "action",
                "action": "run_shell",
                "args": {"command": "pytest -q", "timeout": 60},
            }
        ],
    }

    result = execute_program(
        plan,
        Config(cwd=str(tmp_path)),
        authorization=_authorization("run_shell"),
        limits=ProgramLimits(max_runtime_seconds=5),
        store=_program_store(tmp_path),
        clock=lambda: next(clock_values),
    )

    assert result.worked is True
    assert calls == [("run_shell", {"command": "pytest -q", "timeout": 3.0})]


def test_timeout_aware_action_keeps_shorter_requested_timeout(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    calls: list[dict] = []

    def fake_execute(
        name,
        args,
        cfg,
        *,
        tool_call_id=None,
        force_approval=False,
        deadline_monotonic=None,
        cancellation=None,
    ):
        del name, cfg, tool_call_id, force_approval, deadline_monotonic, cancellation
        calls.append(args)
        return ({"role": "tool", "content": "ok"}, "ok")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "command",
                "kind": "action",
                "action": "run_shell",
                "args": {"command": "pytest -q", "timeout": 1},
            }
        ],
    }

    result = execute_program(
        plan,
        Config(cwd=str(tmp_path)),
        authorization=_authorization("run_shell"),
        limits=ProgramLimits(max_runtime_seconds=5),
        store=_program_store(tmp_path),
    )

    assert result.worked is True
    assert calls == [{"command": "pytest -q", "timeout": 1.0}]


def test_mutation_keeps_per_action_approval_and_immutable_compact_receipt(monkeypatch, tmp_path) -> None:
    from algo_cli import tool_runtime

    approvals: list[str] = []

    def approve(name, args, cfg, *, force=False, preflight=None):
        del args, cfg, force, preflight
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
        store=_program_store(tmp_path),
    )
    compact = result.to_dict(compact=True)

    assert result.worked is True
    assert approvals == ["write_file"]
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "verified receipt"
    assert result.receipts[0].mutates_state is True
    assert compact["mutation_receipts"][0]["operation"] == "write_file"
    assert compact["mutation_receipts"][0]["receipt_hash"] == result.receipts[0].receipt_hash
    assert compact["verification_receipts"] == []
    assert verify_receipt_chain(result.receipts) is True


def test_program_verifier_receipt_repairs_missing_nested_ledger_event(monkeypatch, tmp_path) -> None:
    from algo_cli import execution_guardrails, program_runtime

    def successful_unrecorded_verifier(*_args, **_kwargs):
        result = "post-refresh verification passed\n[exit code: 0]"
        return {"role": "tool", "content": result}, result

    monkeypatch.setattr(
        program_runtime,
        "execute_tool_call_for_pipeline",
        successful_unrecorded_verifier,
    )
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    execution_guardrails.record_workspace_mutation(success=True)
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "verify",
                "kind": "action",
                "action": "run_shell",
                "args": {"command": "python3 -c \"assert True; print('post-refresh verification passed')\""},
            }
        ],
        "outputs": [{"$ref": "verify"}],
    }

    try:
        result = execute_program(
            plan,
            Config(cwd=str(tmp_path)),
            authorization=_authorization("run_shell"),
            store=_program_store(tmp_path),
        )
        decision = execution_guardrails.completion_decision()
    finally:
        execution_guardrails.end_execution_scope(scope)

    assert result.worked is True
    assert result.receipts[0].verification_kind == "test"
    assert result.to_dict(compact=True)["verification_receipts"][0]["verification_kind"] == "test"
    assert decision.allowed is True
    assert decision.verifier_kind == "test"
    assert verify_receipt_chain(result.receipts) is True


def test_receipts_are_frozen_and_tampering_breaks_hash_chain(tmp_path) -> None:
    (tmp_path / "payload.json").write_text("ok", encoding="utf-8")
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        store=_program_store(tmp_path),
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
        store=_program_store(tmp_path),
    )

    compact = result.to_dict(compact=True)

    assert "receipts" not in compact
    assert compact["receipt_count"] == 1
    assert compact["mutation_receipts"] == []
    assert compact["verification_receipts"] == []
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


def test_late_action_schema_failure_blocks_every_dispatch(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    calls: list[str] = []

    def fake_execute(*args, **kwargs):
        del kwargs
        calls.append(str(args[0]))
        return ({"role": "tool", "content": "must not run"}, "must not run")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "first",
                "kind": "action",
                "action": "read_file",
                "args": {"path": "payload.json"},
            },
            {
                "id": "late_invalid",
                "kind": "action",
                "action": "web_search",
                "args": {"query": 7},
            },
        ],
    }

    with pytest.raises(ProgramValidationError, match="query must be a string"):
        execute_program(
            plan,
            Config(cwd=str(tmp_path)),
            authorization=_authorization("read_file", "web_search"),
            store=_program_store(tmp_path),
        )

    assert calls == []


@pytest.mark.parametrize(
    ("action", "args", "error"),
    [
        ("write_file", {"path": "result.txt"}, "does not match the action schema"),
        (
            "read_file",
            {"path": "payload.json", "unexpected": True},
            "does not match the action schema",
        ),
        ("read_file", {"path": 3}, "path must be a string"),
        ("read_file", {"path": "payload.json", "cwd": "/tmp"}, "runtime-owned"),
        ("run_shell", {"command": "pwd", "safe_mode": False}, "runtime-owned"),
        ("run_shell", {"command": "pwd", "timeout": True}, "finite number"),
    ],
)
def test_action_schema_is_closed_and_typed_before_execution(tmp_path, action, args, error) -> None:
    plan = {
        "version": 1,
        "steps": [{"id": "action", "kind": "action", "action": action, "args": args}],
    }

    with pytest.raises(ProgramValidationError, match=error):
        compile_program(
            plan,
            authorization=_authorization(action),
            cwd=str(tmp_path),
        )


@pytest.mark.parametrize(
    ("op", "transform_input", "args", "error"),
    [
        ("count", [], {"extra": True}, "unsupported fields"),
        ("get", {}, {}, "path is required"),
        ("get", {}, {"path": [True]}, "non-negative integers"),
        ("filter_eq", [], {}, "equals is required"),
        ("sort", [], {"descending": "yes"}, "must be a boolean"),
        ("take", [], {"count": True}, "non-negative integer"),
        ("select", [], {"fields": []}, "1 to 64"),
        ("join", [], {"separator": "x" * 33}, "at most 32"),
        ("json_parse", "not-json", {}, "literal transform is invalid"),
    ],
)
def test_transform_contracts_fail_during_compile(tmp_path, op, transform_input, args, error) -> None:
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "transform",
                "kind": "transform",
                "op": op,
                "input": transform_input,
                "args": args,
            }
        ],
    }

    with pytest.raises(ProgramValidationError, match=error):
        compile_program(plan, authorization=_authorization(), cwd=str(tmp_path))


@pytest.mark.parametrize(
    ("action", "args"),
    [
        ("run_shell", {"command": {"$ref": "source"}}),
        (
            "write_file",
            {"path": "result.txt", "content": {"$ref": "source"}},
        ),
        ("x_account_post", {"text": {"$ref": "source"}}),
    ],
)
def test_observations_cannot_flow_into_effectful_action_arguments(tmp_path, action, args) -> None:
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "source",
                "kind": "action",
                "action": "read_file",
                "args": {"path": "payload.json"},
            },
            {"id": "effect", "kind": "action", "action": action, "args": args},
        ],
        "outputs": [{"$ref": "effect"}],
    }

    with pytest.raises(ProgramValidationError, match="action inputs must be static"):
        compile_program(
            plan,
            authorization=_authorization("read_file", action),
            cwd=str(tmp_path),
        )


def test_mutation_structure_is_single_final_and_directly_returned(tmp_path) -> None:
    two_effects = {
        "version": 1,
        "steps": [
            {
                "id": "write",
                "kind": "action",
                "action": "write_file",
                "args": {"path": "result.txt", "content": "static"},
            },
            {
                "id": "shell",
                "kind": "action",
                "action": "run_shell",
                "args": {"command": "pwd"},
            },
        ],
        "outputs": [{"$ref": "shell"}],
    }
    mutation_then_transform = {
        "version": 1,
        "steps": [
            {
                "id": "write",
                "kind": "action",
                "action": "write_file",
                "args": {"path": "result.txt", "content": "static"},
            },
            {
                "id": "counted",
                "kind": "transform",
                "op": "count",
                "input": [1],
            },
        ],
    }
    indirect_output = {
        "version": 1,
        "steps": [two_effects["steps"][0]],
        "outputs": [{"$ref": "write", "path": [0]}],
    }

    with pytest.raises(ProgramValidationError, match="at most one"):
        compile_program(
            two_effects,
            authorization=_authorization("write_file", "run_shell"),
            cwd=str(tmp_path),
        )
    with pytest.raises(ProgramValidationError, match="must be the final"):
        compile_program(
            mutation_then_transform,
            authorization=_authorization("write_file"),
            cwd=str(tmp_path),
        )
    with pytest.raises(ProgramValidationError, match="directly reference"):
        compile_program(
            indirect_output,
            authorization=_authorization("write_file"),
            cwd=str(tmp_path),
        )


def test_safe_mode_blocks_dangerous_shell_during_preflight(tmp_path) -> None:
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "shell",
                "kind": "action",
                "action": "run_shell",
                "args": {"command": "rm -rf /"},
            }
        ],
    }

    with pytest.raises(ProgramValidationError, match="safe shell policy"):
        compile_program(
            plan,
            authorization=_authorization("run_shell"),
            cwd=str(tmp_path),
            safe_mode=True,
        )


def test_compiled_program_revalidates_workspace_safe_mode_and_authority(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "payload.json").write_text("ok", encoding="utf-8")
    compiled = compile_program(
        _read_plan(),
        authorization=_authorization("read_file"),
        cwd=str(first),
        safe_mode=False,
    )
    calls: list[str] = []

    def fake_execute(*args, **kwargs):
        del kwargs
        calls.append(str(args[0]))
        return ({"role": "tool", "content": "must not run"}, "must not run")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)

    for cfg, authorization in (
        (Config(cwd=str(second), safe_mode=False), _authorization("read_file")),
        (Config(cwd=str(first), safe_mode=True), _authorization("read_file")),
        (Config(cwd=str(first), safe_mode=False), _authorization("web_search")),
    ):
        with pytest.raises(ProgramValidationError):
            execute_program(
                compiled,
                cfg,
                authorization=authorization,
                store=_program_store(tmp_path),
            )

    assert calls == []


def test_compiled_program_revalidates_policy_resolution(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    compiled = compile_program(
        _read_plan(),
        authorization=_authorization("read_file"),
        cwd=str(tmp_path),
    )
    original_resolve = program_runtime.resolve_action

    def drifted_resolve(*args, **kwargs):
        resolved = original_resolve(*args, **kwargs)
        return dataclasses.replace(resolved, target=resolved.target + "#policy-drift")

    monkeypatch.setattr(program_runtime, "resolve_action", drifted_resolve)

    with pytest.raises(ProgramValidationError, match="no longer matches"):
        execute_program(
            compiled,
            Config(cwd=str(tmp_path)),
            authorization=_authorization("read_file"),
            store=_program_store(tmp_path),
        )


def test_taint_propagates_through_transforms(tmp_path) -> None:
    (tmp_path / "payload.json").write_text('{"items":[1,2]}', encoding="utf-8")
    plan = {
        "version": 1,
        "steps": [
            {
                "id": "source",
                "kind": "action",
                "action": "read_file",
                "args": {"path": "payload.json"},
            },
            {
                "id": "parsed",
                "kind": "transform",
                "op": "json_parse",
                "input": {"$ref": "source"},
            },
            {
                "id": "items",
                "kind": "transform",
                "op": "get",
                "input": {"$ref": "parsed"},
                "args": {"path": ["items"]},
            },
        ],
    }

    result = execute_program(
        plan,
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        store=_program_store(tmp_path),
    )
    taint = result.outputs[0].taint

    assert result.outputs[0].preview == "[1,2]"
    assert taint.untrusted is True
    assert taint.model_controlled is True
    assert [item.value for item in taint.data_classes] == ["local_content"]
    assert taint.source_steps == ("source",)
    assert taint.protected is False


def test_protected_output_is_encrypted_and_omitted_from_all_receipts(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    secret = "private-query-result-never-persist-plaintext"

    def fake_execute(
        name,
        args,
        cfg,
        *,
        tool_call_id=None,
        force_approval=False,
        deadline_monotonic=None,
        cancellation=None,
    ):
        del name, args, cfg, tool_call_id, force_approval, deadline_monotonic, cancellation
        return ({"role": "tool", "content": secret}, secret)

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    store = _program_store(tmp_path)
    result = execute_program(
        {
            "version": 1,
            "steps": [
                {
                    "id": "search",
                    "kind": "action",
                    "action": "web_search",
                    "args": {"query": "bounded public query"},
                }
            ],
        },
        Config(cwd=str(tmp_path)),
        authorization=_authorization("web_search"),
        store=store,
    )
    output = result.outputs[0]
    serialized = json.dumps(result.to_dict(compact=False), sort_keys=True)
    receipt_files = list((store.root / "receipts").glob("*.jsonl"))
    stored_payloads = [path.read_bytes() for path in store.root.rglob("*") if path.is_file()]

    assert result.worked is True
    assert output.taint.protected is True
    assert output.preview.startswith("[protected output omitted;")
    assert output.artifact is not None
    assert store.read(output.artifact) == secret.encode("utf-8")
    assert "sha256" not in output.to_dict()
    assert result.receipts[0].input_hash.startswith("hmac-sha256:")
    assert result.receipts[0].result_hash.startswith("hmac-sha256:")
    assert secret not in serialized
    assert len(receipt_files) == 1
    assert secret.encode("utf-8") not in receipt_files[0].read_bytes()
    assert all(secret.encode("utf-8") not in payload for payload in stored_payloads)


def test_cancellation_before_first_step_is_content_free_and_never_dispatches(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    calls: list[str] = []

    def fake_execute(*args, **kwargs):
        del kwargs
        calls.append(str(args[0]))
        return ({"role": "tool", "content": "must not run"}, "must not run")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    cancellation = DispatchCancellation()
    cancellation.cancel("operator_cancelled")
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        store=_program_store(tmp_path),
        cancellation=cancellation,
    )

    assert calls == []
    assert result.status == "cancelled"
    assert result.receipts[0].status == "cancelled"
    assert "operator_cancelled" not in json.dumps(result.to_dict())


def test_dispatch_deadline_and_cancellation_are_forwarded(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    captured: dict[str, object] = {}
    cancellation = DispatchCancellation()

    def fake_execute(
        name,
        args,
        cfg,
        *,
        tool_call_id=None,
        force_approval=False,
        deadline_monotonic=None,
        cancellation=None,
    ):
        del name, args, cfg, tool_call_id, force_approval
        captured["deadline"] = deadline_monotonic
        captured["cancellation"] = cancellation
        return ({"role": "tool", "content": "ok"}, "ok")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        limits=ProgramLimits(max_runtime_seconds=5),
        store=_program_store(tmp_path),
        clock=lambda: 10.0,
        dispatch_clock=lambda: 50.0,
        cancellation=cancellation,
    )

    assert result.worked is True
    assert captured == {"deadline": 55.0, "cancellation": cancellation}


def test_typed_cancellation_during_observation_stops_program(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    cancellation = DispatchCancellation()

    def fake_execute(*args, **kwargs):
        del args, kwargs
        cancellation.cancel("during_observation")
        outcome = ActionOutcome(
            action="read_file",
            status=OutcomeStatus.CANCELLED,
            result="Cancelled outcome: observation stopped.",
            invoked=True,
            retry_allowed=True,
            verification=VerificationStatus.FAILED,
            error_code="during_observation",
        )
        return PipelineToolResult(
            {"role": "tool", "content": outcome.model_text()},
            outcome.model_text(),
            outcome,
        )

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        store=_program_store(tmp_path),
        cancellation=cancellation,
    )

    assert result.status == "cancelled"
    assert result.receipts[0].status == "cancelled"


def test_typed_unknown_mutation_requires_reconciliation(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    secret = "external-provider-uncertainty-detail"

    def fake_execute(*args, **kwargs):
        del args, kwargs
        outcome = ActionOutcome(
            action="x_account_post",
            status=OutcomeStatus.UNKNOWN_OUTCOME,
            result=secret,
            invoked=True,
            retry_allowed=False,
            verification=VerificationStatus.UNKNOWN,
            effect_id="effect-unknown",
            error_code="provider_disconnect",
        )
        return PipelineToolResult(
            {"role": "tool", "content": outcome.model_text()},
            outcome.model_text(),
            outcome,
        )

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    store = _program_store(tmp_path)
    result = execute_program(
        {
            "version": 1,
            "steps": [
                {
                    "id": "post",
                    "kind": "action",
                    "action": "x_account_post",
                    "args": {"text": "static reviewed announcement"},
                }
            ],
            "outputs": [{"$ref": "post"}],
        },
        Config(cwd=str(tmp_path)),
        authorization=_authorization("x_account_post"),
        store=store,
    )
    serialized = json.dumps(result.to_dict(compact=False), sort_keys=True)

    assert result.status == "unknown_outcome"
    assert result.requires_reconciliation is True
    assert result.receipts[0].status == "unknown_outcome"
    assert result.receipts[0].mutates_state is True
    assert result.receipts[0].input_hash.startswith("hmac-sha256:")
    assert result.receipts[0].result_hash.startswith("hmac-sha256:")
    assert "protected details were omitted" in result.error
    assert secret not in serialized


def test_clock_regression_before_step_fails_without_dispatch(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    calls: list[str] = []
    clock_values = iter((10.0, 9.0))

    def fake_execute(*args, **kwargs):
        del kwargs
        calls.append(str(args[0]))
        return ({"role": "tool", "content": "must not run"}, "must not run")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        store=_program_store(tmp_path),
        clock=lambda: next(clock_values),
    )

    assert calls == []
    assert result.status == "failed"
    assert result.receipts[0].status == "failed"
    assert "clock regressed before" in result.error


def test_clock_regression_during_step_fails_after_recording_dispatch(monkeypatch, tmp_path) -> None:
    from algo_cli import program_runtime

    clock_values = iter((10.0, 11.0, 10.5))

    def fake_execute(*args, **kwargs):
        del args, kwargs
        return ({"role": "tool", "content": "observed"}, "observed")

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    result = execute_program(
        _read_plan(),
        Config(cwd=str(tmp_path)),
        authorization=_authorization("read_file"),
        store=_program_store(tmp_path),
        clock=lambda: next(clock_values),
    )

    assert result.status == "failed"
    assert result.receipts[0].status == "failed"
    assert "clock regressed during" in result.error


@pytest.mark.parametrize("stop_kind", ["runtime", "intermediate"])
def test_outer_limits_do_not_rewrite_a_successful_mutation_outcome(monkeypatch, tmp_path, stop_kind) -> None:
    from algo_cli import program_runtime

    result_text = "mutation accepted" + ("x" * 200 if stop_kind == "intermediate" else "")

    def fake_execute(*args, **kwargs):
        del args, kwargs
        return ({"role": "tool", "content": result_text}, result_text)

    monkeypatch.setattr(program_runtime, "execute_tool_call_for_pipeline", fake_execute)
    limits = (
        ProgramLimits(max_runtime_seconds=1) if stop_kind == "runtime" else ProgramLimits(max_intermediate_bytes=32)
    )
    clock_values = iter((0.0, 0.0, 2.0)) if stop_kind == "runtime" else None
    result = execute_program(
        {
            "version": 1,
            "steps": [
                {
                    "id": "write",
                    "kind": "action",
                    "action": "write_file",
                    "args": {"path": "result.txt", "content": "static reviewed content"},
                }
            ],
            "outputs": [{"$ref": "write"}],
        },
        Config(cwd=str(tmp_path)),
        authorization=_authorization("write_file"),
        limits=limits,
        store=_program_store(tmp_path),
        clock=(lambda: next(clock_values)) if clock_values is not None else (lambda: 0.0),
    )

    assert result.status == "limit_exceeded"
    assert result.outputs == ()
    assert result.receipts[0].status == "worked"
    assert result.receipts[0].mutates_state is True
