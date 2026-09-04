from __future__ import annotations

from dataclasses import replace
import hashlib
import os
import subprocess
import sys

import pytest

from algo_cli import agent_blocks
from algo_cli import git_evidence
from algo_cli import grace_key_store
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
    protected: bool = False,
    receipt_key_store=None,
    run_nonce: str = FIXED_NONCE,
) -> run_contract.RunContract:
    cfg = Config(cwd=str(tmp_path), model="qwen3", num_ctx=8_192)
    cfg.algorithmic_tool_policy_enabled = policy
    cfg.echo_veil_enabled = protected
    return run_contract.compile_agent_run_contract(
        task=task,
        route=task_router.route_task(task),
        pipeline_name="code-change",
        blocks=pipeline or agent_blocks.code_change_pipeline(),
        cfg=cfg,
        approval_mode=approval_mode,  # type: ignore[arg-type]
        snapshot=_snapshot(),
        issued_at=FIXED_TIME,
        run_nonce=run_nonce,
        receipt_key_store=receipt_key_store,
    )


def test_contract_is_canonical_stable_and_binds_every_field(tmp_path) -> None:
    first = _compile(tmp_path)
    second = _compile(tmp_path)

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest == second.digest
    assert first.contract_id == (f"run-contract-v{run_contract.RUN_CONTRACT_SCHEMA_VERSION}:{first.digest}")
    assert len(first.digest) == 64
    assert replace(first, speed_tier="priority").digest != first.digest
    assert "Fix the failing login test" not in first.canonical_bytes().decode()


def test_protected_contract_uses_persistent_domain_separated_hmacs(
    tmp_path,
) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"k" * 32})
    first = _compile(
        tmp_path,
        task="yes",
        protected=True,
        receipt_key_store=store,
    )
    second = _compile(
        tmp_path,
        task="yes",
        protected=True,
        receipt_key_store=store,
        run_nonce="fedcba9876543210fedcba9876543210",
    )

    assert first.schema_version == run_contract.RUN_CONTRACT_SCHEMA_VERSION
    assert first.sensitive_digests.schema_version == 1
    assert first.sensitive_digests.scheme == "hmac-sha256-v1"
    assert first.sensitive_digests.key_backend == "static"
    assert first.sensitive_digests.key_id.startswith("sha256:")
    assert first.task_digest == second.task_digest
    assert first.blocks[0].prompt_digest == second.blocks[0].prompt_digest
    assert first.workspace == second.workspace
    assert first.workspace.initial_head == _snapshot().head
    assert first.task_digest != run_contract.sensitive_digest_text(
        "yes",
        domain="block-prompt",
        binding=first.sensitive_digests,
        key_store=store,
    )
    assert first.task_digest not in {hashlib.sha256(word.encode()).hexdigest() for word in ("yes", "no")}
    workspace_receipts = {
        first.workspace.status_digest,
        first.workspace.tracked_diff_digest,
        first.workspace.untracked_digest,
    }
    assert len(workspace_receipts) == 3
    assert not workspace_receipts.intersection(
        {
            _snapshot().status_digest,
            _snapshot().tracked_diff_digest,
            _snapshot().untracked_digest,
        }
    )
    canonical = first.canonical_bytes().decode("utf-8")
    assert '"yes"' not in canonical
    assert _snapshot().status_digest not in canonical
    assert _snapshot().tracked_diff_digest not in canonical
    assert _snapshot().untracked_digest not in canonical


def test_protected_receipts_are_stable_across_processes_with_the_same_key(
    tmp_path,
) -> None:
    script = r"""
import json
import sys

from algo_cli import agent_blocks, git_evidence, run_contract, task_router
from algo_cli.config import Config
from algo_cli.grace_key_store import StaticKeyStore
from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL

cwd, key_hex = sys.argv[1:]
task = "yes"
cfg = Config(cwd=cwd, model="qwen3", num_ctx=8192)
cfg.echo_veil_enabled = True
snapshot = git_evidence.GitSnapshot(
    available=True,
    error=None,
    head="a" * 40,
    status="## main",
    tracked_diff="",
    untracked_files=(),
    tracked_diff_digest="b" * 64,
    untracked_digest="c" * 64,
    status_digest="d" * 64,
)
store = StaticKeyStore({PRIVACY_KEY_LABEL: bytes.fromhex(key_hex)})
contract = run_contract.compile_agent_run_contract(
    task=task,
    route=task_router.route_task(task),
    pipeline_name="review",
    blocks=agent_blocks.review_pipeline(),
    cfg=cfg,
    approval_mode="interactive",
    snapshot=snapshot,
    issued_at="2026-07-23T12:00:00+00:00",
    run_nonce="cross-process-protected-receipts",
    receipt_key_store=store,
)
print(json.dumps({
    "task": contract.task_digest,
    "prompt": contract.blocks[0].prompt_digest,
    "workspace": contract.workspace.status_digest,
    "target": contract.digest_sensitive_text(
        "yes",
        domain="tool-target",
        key_store=store,
    ),
}, sort_keys=True))
"""
    command = [
        sys.executable,
        "-c",
        script,
        str(tmp_path),
        (b"x" * 32).hex(),
    ]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    first = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    second = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout

    assert first == second


def test_only_protected_contract_compilation_may_create_receipt_key(
    tmp_path,
) -> None:
    class CountingStaticKeyStore(grace_key_store.StaticKeyStore):
        def __init__(self) -> None:
            super().__init__()
            self.create_calls = 0
            self.existing_calls = 0

        def get_or_create(self, label, *, length=32):
            self.create_calls += 1
            return super().get_or_create(label, length=length)

        def get_existing(self, label, *, length=32):
            self.existing_calls += 1
            return super().get_existing(label, length=length)

    store = CountingStaticKeyStore()

    contract = _compile(
        tmp_path,
        protected=True,
        receipt_key_store=store,
    )
    create_calls = store.create_calls
    contract.assert_sensitive_digest_key(key_store=store)
    contract.digest_sensitive_text("yes", domain="task", key_store=store)

    assert create_calls == 1
    assert store.create_calls == create_calls
    assert store.existing_calls > 1


def test_protected_contract_verification_does_not_create_missing_key(
    tmp_path,
) -> None:
    contract = _compile(
        tmp_path,
        protected=True,
        receipt_key_store=grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"k" * 32}),
    )
    missing = grace_key_store.StaticKeyStore()

    with pytest.raises(
        run_contract.RunContractError,
        match="persistent protected run-receipt key is unavailable",
    ):
        contract.assert_sensitive_digest_key(key_store=missing)
    with pytest.raises(grace_key_store.KeyStoreError, match="absent"):
        missing.get_existing("irene-privacy-hmac-v1")


def test_protected_contract_fails_closed_without_persistent_key(
    tmp_path,
) -> None:
    class VolatileStore:
        def get_or_create(self, _label, *, length=32):
            return grace_key_store.KeyMaterial(
                b"v" * length,
                persistent=False,
                backend="volatile_process",
            )

    with pytest.raises(
        run_contract.RunContractError,
        match="persistent protected run-receipt key is unavailable",
    ):
        _compile(
            tmp_path,
            protected=True,
            receipt_key_store=VolatileStore(),
        )


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
        tracker.start_model_round(contract.budget.max_prompt_tokens_per_round + 1)
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
