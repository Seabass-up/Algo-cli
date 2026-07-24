from __future__ import annotations

import json

import pytest

from algo_cli import agent_blocks
from algo_cli import agent_context
from algo_cli import agent_run_journal
from algo_cli import git_evidence
from algo_cli import run_contract
from algo_cli import task_router
from algo_cli.config import Config


def _snapshot(*, changed: bool = False) -> git_evidence.GitSnapshot:
    return git_evidence.GitSnapshot(
        available=True,
        error=None,
        head="a" * 40,
        status="## main\n M app.py" if changed else "## main",
        tracked_diff="+change" if changed else "",
        untracked_files=(),
        tracked_diff_digest=("b" * 64) if changed else ("0" * 64),
        untracked_digest="1" * 64,
        status_digest=("c" * 64) if changed else ("2" * 64),
    )


def _contract(tmp_path) -> run_contract.RunContract:
    cfg = Config(cwd=str(tmp_path))
    task = "Fix the failing login test"
    return run_contract.compile_agent_run_contract(
        task=task,
        route=task_router.route_task(task),
        pipeline_name="code-change",
        blocks=agent_blocks.code_change_pipeline(),
        cfg=cfg,
        approval_mode="interactive",
        snapshot=_snapshot(),
        run_nonce="journal-test-run",
        issued_at="2026-07-23T12:00:00+00:00",
    )


def test_run_contract_round_trips_through_strict_payload(tmp_path) -> None:
    contract = _contract(tmp_path)

    restored = run_contract.RunContract.from_payload(contract.payload())

    assert restored == contract
    assert restored.digest == contract.digest
    tampered = contract.payload()
    tampered["extra"] = True
    with pytest.raises(run_contract.RunContractError):
        run_contract.RunContract.from_payload(tampered)


def test_journal_records_verified_boundary_and_resume_state(tmp_path) -> None:
    contract = _contract(tmp_path)
    path = tmp_path / "journal.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(contract, path=path)

    journal.block_started(0, "plan")
    journal.model_round_started(0, 0, prompt_tokens=321)
    journal.model_round_completed(
        0,
        0,
        status="completed",
        tool_call_count=0,
        response_digest=agent_run_journal.digest_text("plan"),
    )
    journal.verifier_result(
        ordinal=0,
        verifier="block_output",
        status="passed",
        snapshot=_snapshot(),
    )
    journal.block_finished(
        ordinal=0,
        role="plan",
        status="complete",
        verified=True,
        context_digest=agent_run_journal.digest_text("plan"),
        snapshot=_snapshot(),
    )

    loaded = agent_run_journal.AgentRunJournal.load(
        contract.run_nonce,
        path=path,
    )
    state = loaded.resume_state()

    assert state.can_resume is True
    assert state.completed_block_ordinals == (0,)
    assert state.next_block_ordinal == 1
    assert state.model_rounds == 1
    assert state.tool_calls == 0
    assert state.prompt_tokens == 321
    assert state.last_verified_sequence >= 0
    assert state.workspace_matches(_snapshot()) is True
    assert state.workspace_matches(_snapshot(changed=True)) is False
    assert journal.verified_blocks()[0].context_digest == (
        agent_run_journal.digest_text("plan")
    )
    assert journal.checkpoint_payload()["next_block_ordinal"] == 1


def test_resume_state_reconciles_against_initial_workspace_before_first_block(
    tmp_path,
) -> None:
    contract = _contract(tmp_path)
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=tmp_path / "journal.jsonl",
    )

    state = journal.resume_state()

    assert state.workspace_matches(_snapshot()) is True
    assert state.workspace_matches(_snapshot(changed=True)) is False
    journal.run_resumed(
        next_block_ordinal=0,
        last_verified_sequence=-1,
    )
    assert journal.records()[-1].kind == "run_resumed"


def test_context_receipt_is_journaled_without_context_body(tmp_path) -> None:
    contract = _contract(tmp_path)
    path = tmp_path / "journal.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
    )
    bundle = agent_context.build_agent_context(
        "Fix the failing login test",
        [
            agent_context.AgentContextSource(
                name="memory",
                title="Memory",
                body="private context body",
                priority=10,
                trust="governed_memory",
            )
        ],
        max_tokens=500,
    )

    event = journal.context_bound(bundle.receipt.payload())
    raw = path.read_text(encoding="utf-8")

    assert event.kind == "context_bound"
    assert event.payload["context_digest"] == (
        bundle.receipt.context_digest
    )
    assert "private context body" not in raw


def test_unfinished_mutation_requires_reconciliation_and_hides_arguments(
    tmp_path,
) -> None:
    contract = _contract(tmp_path)
    path = tmp_path / "journal.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(contract, path=path)
    journal.block_started(0, "plan")
    journal.model_round_started(0, 0)
    journal.model_round_completed(
        0,
        0,
        status="completed",
        tool_call_count=1,
        response_digest=agent_run_journal.digest_text("write"),
    )
    journal.tool_intent(
        ordinal=0,
        round_number=0,
        tool_index=0,
        action="write_file",
        args={"path": "secret-name.txt", "content": "private payload"},
        call_id="call-sensitive",
        mutating=True,
        idempotency="non_idempotent",
        target="workspace:/private/secret-name.txt",
    )

    state = journal.resume_state()
    raw = path.read_text(encoding="utf-8")

    assert state.can_resume is False
    assert state.uncertain_mutation_steps == ("b0-r0-t0",)
    assert "secret-name.txt" not in raw
    assert "private payload" not in raw
    assert "call-sensitive" not in raw


def test_tool_result_closes_intent_and_terminal_run_cannot_extend(tmp_path) -> None:
    contract = _contract(tmp_path)
    path = tmp_path / "journal.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(contract, path=path)
    journal.block_started(0, "plan")
    journal.model_round_started(0, 0)
    journal.model_round_completed(
        0,
        0,
        status="completed",
        tool_call_count=1,
        response_digest=agent_run_journal.digest_text("read"),
    )
    journal.tool_intent(
        ordinal=0,
        round_number=0,
        tool_index=0,
        action="read_file",
        args={"path": "README.md"},
        call_id="read-1",
        mutating=False,
        idempotency="pure",
        target="workspace:README.md",
    )
    journal.tool_result(
        step_id="b0-r0-t0",
        status="succeeded",
        invoked=True,
        verification="passed",
    )
    journal.verifier_result(
        ordinal=0,
        verifier="block_output",
        status="failed",
        snapshot=_snapshot(),
    )
    journal.block_finished(
        ordinal=0,
        role="plan",
        status="partial",
        verified=False,
        context_digest=agent_run_journal.digest_text("partial"),
        snapshot=_snapshot(),
    )
    journal.run_finished(
        status="partial",
        last_verified_sequence=-1,
    )

    state = journal.resume_state()
    assert state.terminal is True
    assert state.terminal_status == "partial"
    assert state.uncertain_mutation_steps == ()
    with pytest.raises(agent_run_journal.AgentRunJournalError):
        journal.block_started(0, "plan")


def test_journal_detects_hash_tampering(tmp_path) -> None:
    contract = _contract(tmp_path)
    path = tmp_path / "journal.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(contract, path=path)
    journal.block_started(0, "plan")

    lines = path.read_text(encoding="utf-8").splitlines()
    envelope = json.loads(lines[-1])
    envelope["event"]["payload"]["role"] = "tampered"
    lines[-1] = json.dumps(envelope, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(agent_run_journal.AgentRunJournalCorrupt):
        journal.records()


def test_journal_rejects_rehashed_payload_outside_contract(tmp_path) -> None:
    contract = _contract(tmp_path)
    path = tmp_path / "journal.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(contract, path=path)
    journal.block_started(0, "plan")

    lines = path.read_text(encoding="utf-8").splitlines()
    envelope = json.loads(lines[-1])
    event = envelope["event"]
    event["payload"]["role"] = "forged-role"
    event["event_hash"] = agent_run_journal.digest_json(
        {
            key: value
            for key, value in event.items()
            if key != "event_hash"
        }
    )
    lines[-1] = json.dumps(envelope, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="differs from the run contract",
    ):
        journal.records()


def test_journal_rejects_verified_boundary_without_passed_verifier(
    tmp_path,
) -> None:
    contract = _contract(tmp_path)
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=tmp_path / "journal.jsonl",
    )
    journal.block_started(0, "plan")

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="lacks its passed verifiers",
    ):
        journal.block_finished(
            ordinal=0,
            role="plan",
            status="complete",
            verified=True,
            context_digest=agent_run_journal.digest_text("forged"),
            snapshot=_snapshot(),
        )


def test_journal_rejects_tool_intent_without_model_batch(tmp_path) -> None:
    contract = _contract(tmp_path)
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=tmp_path / "journal.jsonl",
    )
    journal.block_started(0, "plan")

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="no model tool batch",
    ):
        journal.tool_intent(
            ordinal=0,
            round_number=0,
            tool_index=0,
            action="read_file",
            args={"path": "README.md"},
            call_id="orphan-read",
            mutating=False,
            idempotency="pure",
            target="workspace:README.md",
        )
