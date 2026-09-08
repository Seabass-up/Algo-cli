from __future__ import annotations

from dataclasses import dataclass

from algo_cli.arthur_outcomes import OutcomeStatus, VerificationStatus
from algo_cli.clara_effect_ledger import EffectLedger, EffectState
from algo_cli.config import Config
from algo_cli.henry_effect_control import TargetLeaseManager
from algo_cli.james_dispatch import (
    DispatchCancellation,
    DispatchDependencies,
    dispatch_action,
)
from algo_cli.nathan_runtime import preflight_runtime_tool


def _dependencies(tmp_path, invoke, *, verifiers=None) -> DispatchDependencies:
    return DispatchDependencies(
        invoke=invoke,
        effect_ledger=EffectLedger.at_path(str(tmp_path / "effects" / "ledger.jsonl")),
        lease_manager=TargetLeaseManager(tmp_path / "leases"),
        effect_verifiers=verifiers or {},
    )


def test_dispatch_normalizes_observation_success(tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path))
    calls: list[tuple[str, dict]] = []

    def invoke(name, args, _cfg):
        calls.append((name, args))
        return "file contents"

    result = dispatch_action(
        "read_file",
        {"path": "README.md"},
        cfg,
        dependencies=_dependencies(tmp_path, invoke),
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.SUCCEEDED
    assert result.outcome.verification is VerificationStatus.PASSED
    assert result.outcome.invoked is True
    assert calls[0][0] == "read_file"


def test_successful_echo_memory_write_sets_semantic_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    result = dispatch_action(
        "echo_veil_remember",
        {"payload": "protected fact"},
        cfg,
        dependencies=_dependencies(
            tmp_path,
            lambda *_args: '{"stored": true}',
        ),
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.SUCCEEDED
    assert result.outcome.explicit_memory_write is True
    assert result.outcome.receipt()["explicit_memory_write"] is True


def test_successful_wrapped_remember_sets_semantic_receipt(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    result = dispatch_action(
        "session_command",
        {"command": "/remember protected fact"},
        cfg,
        dependencies=_dependencies(
            tmp_path,
            lambda *_args: "Memory saved.",
        ),
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.SUCCEEDED
    assert result.outcome.explicit_memory_write is True


def test_successful_protected_knowledge_note_sets_semantic_receipt(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    result = dispatch_action(
        "write_knowledge_graph_note",
        {"title": "Alias", "body": "Protected note"},
        cfg,
        dependencies=_dependencies(
            tmp_path,
            lambda *_args: "Protected knowledge note saved.",
        ),
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.SUCCEEDED
    assert result.outcome.explicit_memory_write is True
    assert result.outcome.receipt()["explicit_memory_write"] is True


def test_unknown_local_mutation_is_never_retried_automatically(monkeypatch, tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path))
    prompts: list[str] = []
    invocations: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")

    def invoke(name, _args, _cfg):
        invocations.append(name)
        return "Tool error for remember: connection lost"

    deps = _dependencies(tmp_path, invoke)
    first = dispatch_action(
        "remember",
        {"fact": "bounded"},
        cfg,
        tool_call_id="memory-1",
        dependencies=deps,
        render=False,
    )
    second = dispatch_action(
        "remember",
        {"fact": "bounded"},
        cfg,
        tool_call_id="memory-2",
        dependencies=deps,
        render=False,
    )

    assert first.outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert first.outcome.retry_allowed is False
    assert second.outcome.status is OutcomeStatus.SKIPPED
    assert invocations == ["remember"]
    assert len(prompts) == 1


def test_uncertain_local_action_stays_blocked_after_many_skips(monkeypatch, tmp_path):
    from algo_cli.nathan_runtime import ATTEMPT_LEDGER_LIMIT

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    cfg = Config(cwd=str(tmp_path))
    invoked = []
    deps = _dependencies(tmp_path, lambda *_args: invoked.append(True) or "Tool error for remember: lost response")
    outcomes = [dispatch_action("remember", {"fact": "bounded"}, cfg, tool_call_id=f"call-{i}",
                               dependencies=deps, render=False).outcome.status
                for i in range(ATTEMPT_LEDGER_LIMIT + 3)]
    assert outcomes[0] is OutcomeStatus.UNKNOWN_OUTCOME
    assert all(status is OutcomeStatus.SKIPPED for status in outcomes[1:])
    assert invoked == [True]
    assert any(item["status"] == "unknown_outcome" for item in cfg.attempt_ledger)


def test_noninteractive_denial_is_not_attributed_to_the_user(tmp_path):
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    cfg._nathan_approval_mode = "auto"
    result = dispatch_action("run_shell", {"command": "python -m pytest -q"}, cfg,
                             dependencies=_dependencies(tmp_path, lambda *_args: "must not run"), render=False)
    assert result.outcome.status is OutcomeStatus.DENIED
    assert result.outcome.error_code == "approval_unavailable"
    assert "noninteractive" in result.result
    assert "User denied" not in result.result


def test_full_retry_barrier_ledger_blocks_effects_but_not_reads(tmp_path):
    from algo_cli import nathan_runtime as runtime

    cfg = Config(cwd=str(tmp_path))
    for i in range(runtime.ATTEMPT_LEDGER_LIMIT):
        runtime.record_tool_attempt(cfg, name="remember", args={"fact": str(i)}, result="unknown",
                                    status="unknown_outcome", retry_allowed=False)
    approvals, invocations = [], []
    deps = _dependencies(tmp_path, lambda name, *_args: invocations.append(name) or "contents")
    deps.approve = lambda *_args, **_kwargs: approvals.append(True) or True
    blocked = dispatch_action("remember", {"fact": "new"}, cfg, dependencies=deps, render=False)
    assert blocked.outcome.status is OutcomeStatus.DENIED
    assert blocked.outcome.error_code == "unresolved_action_capacity"
    assert approvals == invocations == []
    observed = dispatch_action("read_file", {"path": "README.md"}, cfg, dependencies=deps, render=False)
    assert observed.outcome.status is OutcomeStatus.SUCCEEDED
    assert invocations == ["read_file"]
    assert len(cfg.attempt_ledger) == runtime.ATTEMPT_LEDGER_LIMIT
    assert all(item["status"] == "unknown_outcome" for item in cfg.attempt_ledger)


def test_retry_capacity_reserves_concurrent_effect_slots_and_releases(tmp_path):
    from algo_cli import nathan_runtime as runtime

    cfg = Config(cwd=str(tmp_path))
    for i in range(runtime.ATTEMPT_LEDGER_LIMIT - 1):
        runtime.record_tool_attempt(cfg, name="remember", args={"fact": str(i)}, result="unknown",
                                    status="unknown_outcome", retry_allowed=False)
    with runtime.reserve_retry_capacity(cfg, mutating=True) as first:
        assert first is True
        with runtime.reserve_retry_capacity(cfg, mutating=True) as second:
            assert second is False
        with runtime.reserve_retry_capacity(cfg, mutating=False) as observation:
            assert observation is True
    with runtime.reserve_retry_capacity(cfg, mutating=True) as available:
        assert available is True
    assert not hasattr(cfg, "_nathan_retry_slots")


def test_retry_capacity_is_atomic_between_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from algo_cli import nathan_runtime as runtime

    cfg = Config(cwd=str(tmp_path))
    for i in range(runtime.ATTEMPT_LEDGER_LIMIT - 1):
        runtime.record_tool_attempt(cfg, name="remember", args={"fact": str(i)}, result="unknown",
                                    status="unknown_outcome", retry_allowed=False)
    contenders = Barrier(2, timeout=5)

    def reserve():
        contenders.wait()
        with runtime.reserve_retry_capacity(cfg, mutating=True) as admitted:
            contenders.wait()
            return admitted

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [False, True]
    assert not hasattr(cfg, "_nathan_retry_slots")


def test_retry_capacity_releases_on_approval_exception(tmp_path):
    import pytest

    cfg = Config(cwd=str(tmp_path))
    deps = _dependencies(tmp_path, lambda *_args: "must not run")

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("approval unavailable")

    deps.approve = unavailable
    with pytest.raises(RuntimeError, match="approval unavailable"):
        dispatch_action("remember", {"fact": "bounded"}, cfg, dependencies=deps, render=False)
    assert not hasattr(cfg, "_nathan_retry_slots")


def test_dispatch_isolates_nested_arguments_after_approval(tmp_path):
    from algo_cli import execution_guardrails, nathan_runtime

    path = tmp_path / "result.txt"
    path.write_text("original")
    cfg = Config(cwd=str(tmp_path))
    args = {"path": "result.txt", "edits": [{"old_string": "original", "new_string": "reviewed"}]}
    deps = _dependencies(tmp_path, nathan_runtime.run_tool)
    deps.approve = lambda *_args, **_kwargs: True
    manager = deps.lease_manager

    class ChangingCaller:
        def acquire(self, target):
            args["edits"][0]["new_string"] = "unreviewed"
            return manager.acquire(target)

    deps.lease_manager = ChangingCaller()  # type: ignore[assignment]
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        execution_guardrails.record_read(path, success=True)
        result = dispatch_action("batch_edit", args, cfg, dependencies=deps, render=False)
    finally:
        execution_guardrails.end_execution_scope(scope)
    assert result.outcome.status is OutcomeStatus.SUCCEEDED
    assert path.read_text() == "reviewed"
    assert result.preflight.signature_args["edits"][0]["new_string"] == "reviewed"


def test_tool_cannot_mutate_the_recorded_argument_snapshot(tmp_path):
    cfg = Config(cwd=str(tmp_path))
    args = {"fact": "bounded", "metadata": {"label": "reviewed"}}

    def invoke(_name, values, _cfg):
        values["metadata"]["label"] = "mutated"
        return "Memory saved."

    deps = _dependencies(tmp_path, invoke)
    deps.approve = lambda *_args, **_kwargs: True
    result = dispatch_action("remember", args, cfg, dependencies=deps, render=False)
    assert result.preflight.signature_args["metadata"]["label"] == "reviewed"
    assert args["metadata"]["label"] == "reviewed"


def test_external_effect_is_verified_and_deduplicated_by_call_id(monkeypatch, tmp_path) -> None:
    prompts: list[str] = []
    invocations: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")

    def invoke(name, _args, _cfg):
        invocations.append(name)
        return '{"ok": true}'

    def verify_applied(_action, _result):
        return True

    deps = _dependencies(tmp_path, invoke, verifiers={"x_account_post": verify_applied})
    first = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="post-1",
        dependencies=deps,
        render=False,
    )
    second = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="post-1",
        dependencies=deps,
        render=False,
    )

    assert first.outcome.status is OutcomeStatus.SUCCEEDED
    assert first.outcome.verification is VerificationStatus.PASSED
    assert deps.effect_ledger.get(first.outcome.effect_id).state is EffectState.VERIFIED
    assert second.outcome.status is OutcomeStatus.SUCCEEDED
    assert second.outcome.deduplicated is True
    assert second.outcome.invoked is False
    assert invocations == ["x_account_post"]
    assert len(prompts) == 2


def test_replayed_call_id_with_changed_arguments_fails_without_second_effect(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    invocations: list[str] = []
    deps = _dependencies(
        tmp_path,
        lambda name, _args, _cfg: invocations.append(name) or '{"ok": true}',
        verifiers={"x_account_post": lambda _action, _result: True},
    )

    first = dispatch_action(
        "x_account_post",
        {"text": "first"},
        Config(cwd=str(tmp_path)),
        tool_call_id="private-provider-call",
        dependencies=deps,
        render=False,
    )
    replay = dispatch_action(
        "x_account_post",
        {"text": "changed"},
        Config(cwd=str(tmp_path)),
        tool_call_id="private-provider-call",
        dependencies=deps,
        render=False,
    )

    assert first.outcome.status is OutcomeStatus.SUCCEEDED
    assert replay.outcome.status is OutcomeStatus.FAILED
    assert replay.outcome.invoked is False
    assert replay.outcome.error_code == "InvocationReplayConflict"
    assert invocations == ["x_account_post"]
    raw = deps.effect_ledger.store.path.read_text(encoding="utf-8")
    assert "private-provider-call" not in raw


def test_external_effect_without_postcondition_stays_unknown_across_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    invocations: list[str] = []

    def invoke(name, _args, _cfg):
        invocations.append(name)
        return '{"ok": true}'

    deps = _dependencies(tmp_path, invoke)
    first = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="post-unknown",
        dependencies=deps,
        render=False,
    )
    restarted = _dependencies(tmp_path, invoke)
    second = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="post-unknown",
        dependencies=restarted,
        render=False,
    )

    assert first.outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert second.outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert second.outcome.deduplicated is True
    assert invocations == ["x_account_post"]


def test_failed_postcondition_is_known_failed_after_invocation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    invocations: list[str] = []

    def verify_not_applied(_action, _result):
        return False

    deps = _dependencies(
        tmp_path,
        lambda name, _args, _cfg: invocations.append(name) or '{"ok": true}',
        verifiers={"x_account_post": verify_not_applied},
    )
    cfg = Config(cwd=str(tmp_path))
    result = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        cfg,
        tool_call_id="post-failed",
        dependencies=deps,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.FAILED
    assert result.outcome.invoked is True
    assert result.outcome.retry_allowed is False
    assert deps.effect_ledger.get(result.outcome.effect_id).state is EffectState.FAILED
    cfg.attempt_ledger[-1]["timestamp"] = 0.0

    retry = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        cfg,
        tool_call_id="post-failed-retry",
        dependencies=deps,
        render=False,
    )

    assert retry.outcome.status is OutcomeStatus.SKIPPED
    assert invocations == ["x_account_post"]


def test_external_prepare_failure_prevents_invocation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    calls: list[str] = []
    deps = _dependencies(tmp_path, lambda name, _args, _cfg: calls.append(name) or "ok")
    monkeypatch.setattr(
        deps.effect_ledger,
        "prepare",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    result = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="post-prepare-failure",
        dependencies=deps,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.FAILED
    assert result.outcome.invoked is False
    assert calls == []


def test_restart_recovers_prepared_effect_as_known_not_dispatched(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    calls: list[str] = []
    deps = _dependencies(tmp_path, lambda name, _args, _cfg: calls.append(name) or "ok")
    original_transition = deps.effect_ledger.transition
    monkeypatch.setattr(
        deps.effect_ledger,
        "transition",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash before start")),
    )

    first = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="prepared-crash",
        dependencies=deps,
        render=False,
    )
    monkeypatch.setattr(deps.effect_ledger, "transition", original_transition)
    recovered = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="prepared-crash",
        dependencies=deps,
        render=False,
    )

    assert first.outcome.status is OutcomeStatus.FAILED
    assert first.outcome.invoked is False
    assert recovered.outcome.status is OutcomeStatus.FAILED
    assert recovered.outcome.invoked is False
    assert recovered.outcome.deduplicated is True
    assert recovered.outcome.error_code == "recovered_before_dispatch"
    assert calls == []
    record = deps.effect_ledger.get(recovered.outcome.effect_id)
    assert record is not None
    assert record.state is EffectState.FAILED


def test_external_effect_without_stable_id_fails_before_prompt_or_invoke(monkeypatch, tmp_path) -> None:
    prompts: list[str] = []
    calls: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")
    deps = _dependencies(tmp_path, lambda name, _args, _cfg: calls.append(name) or "ok")

    result = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        dependencies=deps,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.DENIED
    assert result.outcome.invoked is False
    assert result.outcome.error_code == "missing_idempotency_id"
    assert prompts == []
    assert calls == []


def test_agent_policy_ceiling_is_a_typed_denial_without_invocation(tmp_path) -> None:
    calls: list[str] = []
    deps = _dependencies(tmp_path, lambda name, _args, _cfg: calls.append(name) or "ok")

    result = dispatch_action(
        "read_file",
        {"path": "README.md"},
        Config(cwd=str(tmp_path)),
        dependencies=deps,
        policy_ceiling_code="agent_tool_not_allowed",
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.DENIED
    assert result.outcome.invoked is False
    assert "Tool not allowed" in result.result
    assert calls == []


def test_elapsed_deadline_is_typed_before_prompt_or_invocation(monkeypatch, tmp_path) -> None:
    prompts: list[str] = []
    calls: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")
    deps = _dependencies(tmp_path, lambda name, _args, _cfg: calls.append(name) or "ok")
    deps.monotonic = lambda: 10.0

    result = dispatch_action(
        "read_file",
        {"path": "README.md"},
        Config(cwd=str(tmp_path)),
        dependencies=deps,
        deadline_monotonic=5.0,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.TIMED_OUT
    assert result.outcome.invoked is False
    assert result.status == "timed_out"
    assert prompts == []
    assert calls == []


def test_backward_clock_jump_fails_before_invocation(tmp_path) -> None:
    calls: list[str] = []
    ticks = iter((5.0, 4.0))
    deps = _dependencies(tmp_path, lambda name, _args, _cfg: calls.append(name) or "ok")
    deps.monotonic = lambda: next(ticks)

    result = dispatch_action(
        "read_file",
        {"path": "README.md"},
        Config(cwd=str(tmp_path)),
        dependencies=deps,
        deadline_monotonic=10.0,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.FAILED
    assert result.outcome.invoked is False
    assert result.outcome.error_code == "clock_regression"
    assert calls == []


def test_cancellation_during_observation_is_typed_and_retryable(tmp_path) -> None:
    cancellation = DispatchCancellation()

    def invoke(_name, _args, _cfg):
        cancellation.cancel("test_cancel")
        return "late read result"

    result = dispatch_action(
        "read_file",
        {"path": "README.md"},
        Config(cwd=str(tmp_path)),
        dependencies=_dependencies(tmp_path, invoke),
        cancellation=cancellation,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.CANCELLED
    assert result.outcome.invoked is True
    assert result.outcome.retry_allowed is True
    assert result.outcome.error_code == "test_cancel"


def test_cancellation_during_mutation_becomes_unknown(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    cancellation = DispatchCancellation()

    def invoke(_name, _args, _cfg):
        cancellation.cancel("test_cancel")
        return "Remembered: bounded"

    result = dispatch_action(
        "remember",
        {"fact": "bounded"},
        Config(cwd=str(tmp_path)),
        dependencies=_dependencies(tmp_path, invoke),
        cancellation=cancellation,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert result.outcome.invoked is True
    assert result.outcome.retry_allowed is False
    assert result.outcome.error_code == "cancelled_after_dispatch"


def test_external_deadline_after_prepare_fails_before_dispatch(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    calls: list[str] = []
    ticks = iter((0.0, 0.0, 0.0, 10.0))
    deps = _dependencies(tmp_path, lambda name, _args, _cfg: calls.append(name) or "ok")
    deps.monotonic = lambda: next(ticks)

    result = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="deadline-before-dispatch",
        dependencies=deps,
        deadline_monotonic=5.0,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.TIMED_OUT
    assert result.outcome.invoked is False
    assert calls == []
    record = deps.effect_ledger.get(result.outcome.effect_id)
    assert record is not None
    assert record.state is EffectState.FAILED
    assert record.reason_code == "deadline_before_dispatch"


def test_verified_external_effect_retains_truth_after_deadline(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    ticks = iter((0.0, 0.0, 0.0, 0.0, 0.0, 10.0))
    deps = _dependencies(
        tmp_path,
        lambda _name, _args, _cfg: '{"ok": true}',
        verifiers={"x_account_post": lambda _action, _result: True},
    )
    deps.monotonic = lambda: next(ticks)

    result = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="verified-after-deadline",
        dependencies=deps,
        deadline_monotonic=5.0,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.SUCCEEDED
    assert result.outcome.verification is VerificationStatus.PASSED
    assert result.outcome.error_code == "deadline_after_dispatch"
    assert deps.effect_ledger.get(result.outcome.effect_id).state is EffectState.VERIFIED


def test_unverified_external_effect_after_deadline_is_unknown(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    ticks = iter((0.0, 0.0, 0.0, 0.0, 0.0, 10.0))
    deps = _dependencies(tmp_path, lambda _name, _args, _cfg: '{"ok": true}')
    deps.monotonic = lambda: next(ticks)

    result = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="unknown-after-deadline",
        dependencies=deps,
        deadline_monotonic=5.0,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert result.outcome.error_code == "deadline_after_dispatch"
    record = deps.effect_ledger.get(result.outcome.effect_id)
    assert record is not None
    assert record.state is EffectState.UNKNOWN
    assert record.reason_code == "deadline_after_dispatch"


def test_model_cannot_supply_trusted_confirmation_argument(tmp_path) -> None:
    preflight = preflight_runtime_tool(
        "x_account_post",
        {"text": "hello", "confirm": True},
        Config(cwd=str(tmp_path)),
    )

    assert "confirm" not in preflight.signature_args


@dataclass
class _FailingReleaseLease:
    fencing_token: int = 1

    def validate(self) -> bool:
        return True

    def release(self) -> None:
        raise OSError("release receipt failed")


class _FailingReleaseManager:
    def acquire(self, _target):
        return _FailingReleaseLease()


class _FailingAcquireManager:
    def acquire(self, _target):
        raise PermissionError("lease root is not writable")


@dataclass
class _StaleBeforeInvokeLease:
    fencing_token: int = 11
    validations: int = 0

    def validate(self) -> bool:
        self.validations += 1
        return self.validations == 1

    def release(self) -> None:
        return None


class _StaleBeforeInvokeManager:
    def __init__(self) -> None:
        self.last_lease: _StaleBeforeInvokeLease | None = None

    def acquire(self, _target):
        self.last_lease = _StaleBeforeInvokeLease()
        return self.last_lease


@dataclass
class _StaleAfterInvokeLease:
    fencing_token: int = 12
    validations: int = 0

    def validate(self) -> bool:
        self.validations += 1
        return self.validations <= 2

    def release(self) -> None:
        return None


class _StaleAfterInvokeManager:
    def acquire(self, _target):
        return _StaleAfterInvokeLease()


def test_mutation_release_failure_converts_success_to_unknown(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    deps = _dependencies(tmp_path, lambda _name, _args, _cfg: "Remembered: bounded")
    deps.lease_manager = _FailingReleaseManager()  # type: ignore[assignment]

    result = dispatch_action(
        "remember",
        {"fact": "bounded"},
        Config(cwd=str(tmp_path)),
        dependencies=deps,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert result.outcome.retry_allowed is False


def test_verified_external_effect_survives_lease_release_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    deps = _dependencies(
        tmp_path,
        lambda _name, _args, _cfg: '{"ok": true}',
        verifiers={"x_account_post": lambda _action, _result: True},
    )
    deps.lease_manager = _FailingReleaseManager()  # type: ignore[assignment]

    result = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="verified-release-failure",
        dependencies=deps,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.SUCCEEDED
    assert result.outcome.verification is VerificationStatus.PASSED
    assert result.outcome.error_code == "lease_release_OSError"


def test_unexpected_lease_acquire_error_is_typed_and_not_invoked(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    calls: list[str] = []
    deps = _dependencies(tmp_path, lambda name, _args, _cfg: calls.append(name) or "ok")
    deps.lease_manager = _FailingAcquireManager()  # type: ignore[assignment]

    result = dispatch_action(
        "remember",
        {"fact": "bounded"},
        Config(cwd=str(tmp_path)),
        dependencies=deps,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.FAILED
    assert result.outcome.invoked is False
    assert result.outcome.error_code == "PermissionError"
    assert calls == []


def test_stale_fence_before_external_invoke_is_durably_failed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    invocations: list[str] = []
    deps = _dependencies(
        tmp_path,
        lambda name, _args, _cfg: invocations.append(name) or '{"ok": true}',
    )
    manager = _StaleBeforeInvokeManager()
    deps.lease_manager = manager  # type: ignore[assignment]

    result = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="stale-before-invoke",
        dependencies=deps,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.FAILED
    assert result.outcome.invoked is False
    assert invocations == []
    assert manager.last_lease is not None
    record = deps.effect_ledger.get(result.outcome.effect_id)
    assert record is not None
    assert record.state is EffectState.FAILED
    assert record.reason_code == "stale_fence_before_invoke"


def test_late_reply_from_stale_holder_cannot_commit_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    invocations: list[str] = []
    deps = _dependencies(
        tmp_path,
        lambda name, _args, _cfg: invocations.append(name) or '{"ok": true}',
        verifiers={"x_account_post": lambda _action, _result: True},
    )
    deps.lease_manager = _StaleAfterInvokeManager()  # type: ignore[assignment]

    result = dispatch_action(
        "x_account_post",
        {"text": "hello"},
        Config(cwd=str(tmp_path)),
        tool_call_id="stale-after-invoke",
        dependencies=deps,
        render=False,
    )

    assert result.outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert result.outcome.error_code == "stale_fence_after_dispatch"
    assert invocations == ["x_account_post"]
    record = deps.effect_ledger.get(result.outcome.effect_id)
    assert record is not None
    assert record.state is EffectState.UNKNOWN
    assert record.reason_code == "stale_fence_after_dispatch"
