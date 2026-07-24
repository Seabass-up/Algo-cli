from __future__ import annotations

from dataclasses import replace

import pytest

from algo_cli import agent_blocks
from algo_cli import git_evidence
from algo_cli import run_contract
from algo_cli import task_router
from algo_cli.config import Config
from algo_cli.nathan_runtime import approval_mode_for_config


FIXED_TIME = "2026-07-23T12:00:00+00:00"
FIXED_NONCE = "0123456789abcdef0123456789abcdef"
EMPTY_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _snapshot() -> git_evidence.GitSnapshot:
    return git_evidence.GitSnapshot(
        available=True,
        error=None,
        head="a" * 40,
        status="## hardening/foundation-freeze",
        tracked_diff="",
        untracked_files=(),
        tracked_diff_digest=EMPTY_DIGEST,
        untracked_digest=EMPTY_DIGEST,
        status_digest="b" * 64,
    )


def _compile(
    tmp_path,
    *,
    task: str = "Fix the failing login test",
    pipeline: list[agent_blocks.AgentBlock] | None = None,
    policy: bool = True,
    approval_mode: str = "interactive",
) -> run_contract.RunContract:
    cfg = Config(cwd=str(tmp_path), model="qwen3", num_ctx=8_192)
    cfg.algorithmic_tool_policy_enabled = policy
    return run_contract.compile_agent_run_contract(
        task=task,
        route=task_router.route_task(task),
        pipeline_name="code-change",
        blocks=pipeline or agent_blocks.code_change_pipeline(),
        cfg=cfg,
        approval_mode=approval_mode,  # type: ignore[arg-type]
        snapshot=_snapshot(),
        issued_at=FIXED_TIME,
        run_nonce=FIXED_NONCE,
    )


def test_contract_is_canonical_stable_and_binds_every_field(tmp_path) -> None:
    first = _compile(tmp_path)
    second = _compile(tmp_path)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest == second.digest
    assert first.contract_id == (
        f"run-contract-v{run_contract.RUN_CONTRACT_SCHEMA_VERSION}:"
        f"{first.digest}"
    )
    assert len(first.digest) == 64
    assert replace(first, speed_tier="priority").digest != first.digest
    assert "Fix the failing login test" not in first.canonical_bytes().decode()


def test_contract_compiles_enforced_policy_and_bounded_blocks(tmp_path) -> None:
    contract = _compile(tmp_path)

    assert contract.mode == "enforced"
    assert contract.approval_mode == "interactive"
    assert contract.safe_mode is True
    assert contract.session_preapproval is False
    assert contract.mutation_scope == "workspace"
    assert contract.required_verifiers == (
        "attributable_change",
        "block_output",
        "final_output",
        "post_mutation",
    )
    assert contract.budget.max_blocks == 4
    assert contract.budget.max_iterations_per_block == 8
    implement = contract.blocks[1]
    assert implement.role == "implement"
    assert implement.max_iterations == 8
    assert "write_file" in implement.admitted_tools
    assert "write_file" in implement.approval_required_tools
    assert implement.required_verifiers == (
        "attributable_change",
        "block_output",
        "post_mutation",
    )


def test_shadow_contract_records_policy_without_changing_effective_tools(tmp_path) -> None:
    task = "Fix credential handling"
    contract = _compile(tmp_path, task=task, policy=False)
    implement = contract.blocks[1]

    assert contract.mode == "shadow"
    assert "write_file" in implement.configured_tools
    assert "write_file" not in implement.admitted_tools
    assert "write_file" in implement.denied_tools
    assert "write_file" in implement.effective_tools("shadow")
    assert "write_file" not in implement.effective_tools("enforced")


@pytest.mark.parametrize("approval_mode", ("interactive", "never", "auto"))
def test_contract_binds_approval_mode_without_reinterpreting_it(
    tmp_path,
    approval_mode,
) -> None:
    contract = _compile(tmp_path, approval_mode=approval_mode)

    contract.assert_live_approval_mode(approval_mode)
    contract.assert_live_authority(
        approval_mode=approval_mode,
        safe_mode=True,
        session_preapproval=approval_mode == "auto",
    )
    different = "never" if approval_mode != "never" else "auto"
    with pytest.raises(run_contract.RunContractViolation):
        contract.assert_live_approval_mode(different)


def test_nonapproval_read_only_block_remains_prompt_free_and_admitted(tmp_path) -> None:
    pipeline = agent_blocks.review_pipeline()
    contract = _compile(
        tmp_path,
        task="Review auth.py for bugs",
        pipeline=pipeline,
        approval_mode="never",
    )
    review = contract.blocks[0]

    assert contract.mutation_scope == "none"
    assert "read_file" in review.effective_tools("enforced")
    assert "read_file" not in review.approval_required_tools
    assert review.required_verifiers == ("block_output",)


def test_explicit_read_only_route_rejects_mutating_pipeline(tmp_path) -> None:
    task = "Inspect the runtime read-only; do not write"

    with pytest.raises(
        run_contract.RunContractError,
        match="read-only",
    ):
        _compile(
            tmp_path,
            task=task,
            pipeline=agent_blocks.code_change_pipeline(),
            approval_mode="never",
        )


def test_high_risk_mutation_contract_disables_automatic_recovery(
    tmp_path,
) -> None:
    contract = _compile(
        tmp_path,
        task="Fix credential deletion handling",
        pipeline=agent_blocks.code_change_pipeline(),
    )
    implement = contract.blocks[1]

    assert contract.risk == "high"
    assert implement.max_recovery_attempts == 0
    assert implement.recovery_codes == ()


def test_explicit_high_risk_agent_contract_keeps_bounded_user_directed_pipeline(
    tmp_path,
) -> None:
    contract = _compile(
        tmp_path,
        task="Review credential deletion logic for security issues",
        pipeline=agent_blocks.review_pipeline(),
        approval_mode="never",
    )

    assert contract.risk == "high"
    assert contract.budget.max_blocks == 2
    assert contract.budget.max_parallelism == 0
    assert all(not block.approval_required_tools for block in contract.blocks)


def test_invalid_live_approval_mode_fails_closed(tmp_path) -> None:
    cfg = Config(cwd=str(tmp_path))
    setattr(cfg, "_nathan_approval_mode", "unexpected")

    assert approval_mode_for_config(cfg) == "never"
    contract = _compile(tmp_path, approval_mode="never")
    with pytest.raises(run_contract.RunContractViolation):
        contract.assert_live_approval_mode("unexpected")


def test_contract_tracker_enforces_order_tools_and_wall_time(tmp_path) -> None:
    contract = _compile(tmp_path)
    now = [10.0]
    tracker = run_contract.RunContractTracker(contract, clock=lambda: now[0])

    tracker.start_block(0)
    tracker.start_model_round(256)
    tracker.reserve_tool_calls(1)
    assert tracker.blocks_started == 1
    assert tracker.model_rounds == 1
    assert tracker.tool_calls == 1
    assert tracker.prompt_tokens == 256

    with pytest.raises(
        run_contract.RunContractViolation,
        match="per-round prompt budget",
    ):
        tracker.start_model_round(
            contract.budget.max_prompt_tokens_per_round + 1
        )
    assert tracker.model_rounds == 1
    assert tracker.prompt_tokens == 256

    with pytest.raises(run_contract.RunContractViolation):
        tracker.start_block(2)

    now[0] += contract.budget.max_wall_time_seconds + 1
    with pytest.raises(run_contract.RunContractViolation):
        tracker.check_wall_time()


def test_workspace_contract_rejects_git_fields_when_git_is_unavailable(
    tmp_path,
) -> None:
    with pytest.raises(run_contract.RunContractError):
        run_contract.WorkspaceContract(
            root=str(tmp_path),
            git_available=False,
            initial_head="a" * 64,
        )
