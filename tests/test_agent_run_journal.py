from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json

import pytest

from algo_cli import agent_blocks
from algo_cli import agent_context
from algo_cli import agent_run_journal
from algo_cli import git_evidence
from algo_cli import grace_key_store
from algo_cli import run_contract
from algo_cli import task_router
from algo_cli.config import Config


def _snapshot(*, changed: bool = False) -> git_evidence.GitSnapshot:
    status = "## main\n M app.py" if changed else "## main"
    tracked_diff = "+change" if changed else ""
    return git_evidence.GitSnapshot(
        available=True,
        error=None,
        head="a" * 40,
        status=status,
        tracked_diff=tracked_diff,
        untracked_files=(),
        tracked_diff_digest=hashlib.sha256(tracked_diff.encode()).hexdigest(),
        untracked_digest=hashlib.sha256(b"").hexdigest(),
        status_digest=hashlib.sha256(status.encode()).hexdigest(),
    )


def _contract(
    tmp_path,
    *,
    task: str = "Fix the failing login test",
    protected: bool = False,
    receipt_key_store=None,
    run_nonce: str = "journal-test-run",
) -> run_contract.RunContract:
    cfg = Config(cwd=str(tmp_path))
    cfg.echo_veil_enabled = protected
    return run_contract.compile_agent_run_contract(
        task=task,
        route=task_router.route_task(task),
        pipeline_name="code-change",
        blocks=agent_blocks.code_change_pipeline(),
        cfg=cfg,
        approval_mode="interactive",
        snapshot=_snapshot(),
        run_nonce=run_nonce,
        issued_at="2026-07-23T12:00:00+00:00",
        receipt_key_store=receipt_key_store,
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
    assert journal.verified_blocks()[0].context_digest == (agent_run_journal.digest_text("plan"))
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
    assert event.payload["context_digest"] == (bundle.receipt.context_digest)
    assert "private context body" not in raw


def test_protected_journal_keyed_receipts_resist_small_dictionaries(
    tmp_path,
) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"k" * 32})
    contract = _contract(
        tmp_path,
        task="yes",
        protected=True,
        receipt_key_store=store,
    )
    path = tmp_path / "protected.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=store,
        protected_expected=True,
    )
    bundle = agent_context.build_agent_context(
        "yes",
        [
            agent_context.AgentContextSource(
                name="memory",
                title="Memory",
                body="yes",
                priority=10,
                trust="governed_memory",
                provenance="yes",
            )
        ],
        max_tokens=500,
    )
    bound = journal.context_bound(bundle.receipt.payload())
    journal.block_started(0, "plan")
    journal.model_round_started(0, 0)
    response_sha = agent_run_journal.digest_json({"answer": "yes"})
    completed = journal.model_round_completed(
        0,
        0,
        status="completed",
        tool_call_count=1,
        response_digest=response_sha,
    )
    intent = journal.tool_intent(
        ordinal=0,
        round_number=0,
        tool_index=0,
        action="read_file",
        args={"answer": "yes"},
        call_id="yes",
        mutating=False,
        idempotency="pure",
        target="yes",
    )
    journal.tool_result(
        step_id="b0-r0-t0",
        status="succeeded",
        invoked=True,
        verification="passed",
        idempotency_key=hashlib.sha256(b"yes").hexdigest(),
    )
    verifier = journal.verifier_result(
        ordinal=0,
        verifier="block_output",
        status="passed",
        snapshot=_snapshot(),
    )
    finished = journal.block_finished(
        ordinal=0,
        role="plan",
        status="complete",
        verified=True,
        context_digest=hashlib.sha256(b"yes").hexdigest(),
        snapshot=_snapshot(),
    )

    raw = path.read_text(encoding="utf-8")
    raw_dictionary = {hashlib.sha256(word.encode()).hexdigest() for word in ("yes", "no")} | {
        agent_run_journal.digest_json({"answer": word}) for word in ("yes", "no")
    }
    raw_workspace_digests = {
        _snapshot().status_digest,
        _snapshot().tracked_diff_digest,
        _snapshot().untracked_digest,
    }
    assert '"yes"' not in raw
    assert not any(candidate in raw for candidate in raw_dictionary)
    assert not any(candidate in raw for candidate in raw_workspace_digests)
    assert contract.task_digest not in raw_dictionary
    assert contract.workspace.initial_head == _snapshot().head
    assert verifier.payload["workspace"]["head"] == _snapshot().head
    assert {
        verifier.payload["workspace"]["status_digest"],
        verifier.payload["workspace"]["tracked_diff_digest"],
        verifier.payload["workspace"]["untracked_digest"],
    }.isdisjoint(raw_workspace_digests)
    assert bound.payload["context_digest"] != bundle.receipt.context_digest
    assert (
        bound.payload["source_metadata"][0]["provenance_sha256"] != bundle.receipt.source_metadata[0].provenance_sha256
    )
    assert completed.payload["response_digest"] != response_sha
    assert intent.payload["args_digest"] not in raw_dictionary
    assert intent.payload["call_id_hash"] not in raw_dictionary
    assert intent.payload["target_hash"] not in raw_dictionary
    assert finished.payload["context_digest"] not in raw_dictionary
    assert all(
        event["event"]["schema_version"] == agent_run_journal.AGENT_RUN_JOURNAL_SCHEMA_VERSION
        for event in map(json.loads, raw.splitlines())
    )
    first_event = json.loads(raw.splitlines()[0])["event"]
    first_body = {key: value for key, value in first_event.items() if key != "event_hash"}
    assert first_event["event_hash"] == journal.digest_json(
        first_body,
        domain=agent_run_journal.AGENT_RUN_EVENT_AUTH_DOMAIN,
    )
    assert first_event["event_hash"] != journal.digest_json(
        first_body,
        domain=agent_run_journal.AGENT_RUN_ANCHOR_AUTH_DOMAIN,
    )
    state = journal.resume_state()
    assert state.workspace_matches(_snapshot()) is True
    assert state.workspace_matches(_snapshot(changed=True)) is False


def test_protected_journal_receipts_are_stable_across_runs(tmp_path) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"s" * 32})
    first = agent_run_journal.AgentRunJournal.create(
        _contract(
            tmp_path,
            task="yes",
            protected=True,
            receipt_key_store=store,
            run_nonce="protected-run-one",
        ),
        path=tmp_path / "one.jsonl",
        receipt_key_store=store,
    )
    second = agent_run_journal.AgentRunJournal.create(
        _contract(
            tmp_path,
            task="yes",
            protected=True,
            receipt_key_store=store,
            run_nonce="protected-run-two",
        ),
        path=tmp_path / "two.jsonl",
        receipt_key_store=store,
    )

    assert first.digest_text("yes", domain="tool-target") == second.digest_text(
        "yes",
        domain="tool-target",
    )
    assert first.digest_json(
        {"answer": "yes"},
        domain="tool-arguments",
    ) == second.digest_json(
        {"answer": "yes"},
        domain="tool-arguments",
    )
    assert first.digest_text("yes", domain="tool-target") != first.digest_text(
        "yes",
        domain="tool-call-id",
    )


def test_protected_receipt_domains_do_not_alias_low_entropy_values(
    tmp_path,
) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"d" * 32})
    journal = agent_run_journal.AgentRunJournal.create(
        _contract(
            tmp_path,
            task="yes",
            protected=True,
            receipt_key_store=store,
            run_nonce="protected-domain-separation",
        ),
        path=tmp_path / "protected-domains.jsonl",
        receipt_key_store=store,
        protected_expected=True,
    )
    raw_digest = hashlib.sha256(b"yes").hexdigest()
    direct_domains = (
        "task",
        "block-prompt",
        "tool-target",
        "tool-call-id",
    )
    checkpoint_domains = (
        "bound-context",
        "context-provenance",
        "model-response",
        "block-context",
        "tool-idempotency-key",
    )
    receipts = {
        *(journal.digest_text("yes", domain=domain) for domain in direct_domains),
        *(journal.checkpoint_digest_text("yes", domain=domain) for domain in checkpoint_domains),
        journal.digest_json(
            {"answer": "yes"},
            domain="tool-arguments",
        ),
    }

    assert len(receipts) == len(direct_domains) + len(checkpoint_domains) + 1
    assert raw_digest not in receipts


def test_protected_journal_load_rejects_wrong_key(tmp_path) -> None:
    first_store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"a" * 32})
    wrong_store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"b" * 32})
    contract = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=first_store,
    )
    path = tmp_path / "protected.jsonl"
    agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=first_store,
    )

    loaded = agent_run_journal.AgentRunJournal.load(
        contract.run_nonce,
        path=path,
        receipt_key_store=first_store,
        protected_expected=True,
    )
    assert loaded.protected is True
    assert loaded.contract.sensitive_digests == contract.sensitive_digests

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="key does not match",
    ):
        agent_run_journal.AgentRunJournal.load(
            contract.run_nonce,
            path=path,
            receipt_key_store=wrong_store,
            protected_expected=True,
        )


def test_protected_journal_load_does_not_create_missing_key(tmp_path) -> None:
    first_store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"a" * 32})
    contract = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=first_store,
    )
    path = tmp_path / "protected.jsonl"
    agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=first_store,
        protected_expected=True,
    )
    missing_store = grace_key_store.StaticKeyStore()

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="persistent protected run-receipt key is unavailable",
    ):
        agent_run_journal.AgentRunJournal.load(
            contract.run_nonce,
            path=path,
            receipt_key_store=missing_store,
            protected_expected=True,
        )
    with pytest.raises(grace_key_store.KeyStoreError, match="absent"):
        missing_store.get_existing("irene-privacy-hmac-v1")


def test_legacy_journal_is_refused_when_protected_authority_is_expected(
    tmp_path,
) -> None:
    legacy = replace(
        _contract(tmp_path),
        schema_version=run_contract.LEGACY_RUN_CONTRACT_SCHEMA_VERSION,
    )
    path = tmp_path / "legacy.jsonl"
    agent_run_journal.AgentRunJournal.create(legacy, path=path)
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"k" * 32})

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="migration is unsafe",
    ):
        agent_run_journal.AgentRunJournal.load(
            legacy.run_nonce,
            path=path,
            receipt_key_store=store,
            protected_expected=True,
        )

    loaded = agent_run_journal.AgentRunJournal.load(
        legacy.run_nonce,
        path=path,
        protected_expected=False,
    )
    assert loaded.contract.schema_version == 3


def test_current_journal_refuses_memory_authority_downgrade(tmp_path) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"d" * 32})
    contract = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=store,
    )
    path = tmp_path / "protected.jsonl"
    agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=store,
    )

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="authority differs",
    ):
        agent_run_journal.AgentRunJournal.load(
            contract.run_nonce,
            path=path,
            receipt_key_store=store,
            protected_expected=False,
        )


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
        {key: value for key, value in event.items() if key != "event_hash"}
    )
    lines[-1] = json.dumps(envelope, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="differs from the run contract",
    ):
        journal.records()


def test_protected_journal_rejects_semantic_rewrite_and_rechain(
    tmp_path,
) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"r" * 32})
    contract = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=store,
        run_nonce="protected-semantic-rewrite",
    )
    path = tmp_path / "protected-rewrite.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=store,
        protected_expected=True,
    )
    journal.block_started(0, "plan")
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

    envelopes = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rewrite_start = -1
    for index, envelope in enumerate(envelopes):
        event = envelope["event"]
        if event["kind"] == "verifier_result":
            event["payload"]["status"] = "passed"
            rewrite_start = index
        elif event["kind"] == "block_finished":
            event["payload"]["status"] = "complete"
            event["payload"]["verified"] = True
    assert rewrite_start > 0
    previous_hash = envelopes[rewrite_start - 1]["event"]["event_hash"]
    for envelope in envelopes[rewrite_start:]:
        event = envelope["event"]
        event["previous_hash"] = previous_hash
        event["event_hash"] = agent_run_journal.digest_json(
            {key: value for key, value in event.items() if key != "event_hash"}
        )
        previous_hash = event["event_hash"]
    path.write_text(
        "\n".join(json.dumps(envelope, sort_keys=True, separators=(",", ":")) for envelope in envelopes) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="hash does not match",
    ):
        agent_run_journal.AgentRunJournal.load(
            contract.run_nonce,
            path=path,
            receipt_key_store=store,
            protected_expected=True,
        )


def test_protected_journal_anchor_detects_prefix_rollback(tmp_path) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"t" * 32})
    contract = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=store,
        run_nonce="protected-tail-rollback",
    )
    path = tmp_path / "protected-rollback.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=store,
        protected_expected=True,
    )
    journal.block_started(0, "plan")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n", encoding="utf-8")

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="rollback",
    ):
        agent_run_journal.AgentRunJournal.load(
            contract.run_nonce,
            path=path,
            receipt_key_store=store,
            protected_expected=True,
        )


@pytest.mark.parametrize(
    "mutation",
    ("delete_middle", "reorder", "torn_tail"),
)
def test_protected_journal_rejects_deleted_reordered_or_torn_events(
    tmp_path,
    mutation,
) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"o" * 32})
    contract = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=store,
        run_nonce=f"protected-order-{mutation}",
    )
    path = tmp_path / f"protected-order-{mutation}.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=store,
        protected_expected=True,
    )
    journal.block_started(0, "plan")
    journal.model_round_started(0, 0)
    lines = path.read_text(encoding="utf-8").splitlines()
    if mutation == "delete_middle":
        lines.pop(1)
    elif mutation == "reorder":
        lines[1], lines[2] = lines[2], lines[1]
    else:
        lines[-1] = lines[-1][: len(lines[-1]) // 2]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(agent_run_journal.AgentRunJournalCorrupt):
        agent_run_journal.AgentRunJournal.load(
            contract.run_nonce,
            path=path,
            receipt_key_store=store,
            protected_expected=True,
        )


def test_protected_journal_crash_window_is_read_only_and_recoverable(
    tmp_path,
) -> None:
    class FailingAnchorStore(grace_key_store.StaticKeyStore):
        fail_next_anchor = False

        def compare_and_set(
            self,
            journal_id,
            *,
            expected_digest,
            value,
        ):
            if self.fail_next_anchor:
                self.fail_next_anchor = False
                raise RuntimeError("simulated anchor outage")
            return super().compare_and_set(
                journal_id,
                expected_digest=expected_digest,
                value=value,
            )

    store = FailingAnchorStore({"irene-privacy-hmac-v1": b"c" * 32})
    contract = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=store,
        run_nonce="protected-anchor-crash-window",
    )
    path = tmp_path / "protected-crash.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=store,
        protected_expected=True,
    )
    journal.block_started(0, "plan")
    journal.model_round_started(0, 0)
    journal.model_round_completed(
        0,
        0,
        status="completed",
        tool_call_count=1,
        response_digest=agent_run_journal.digest_text("write"),
    )
    prior_anchor = store.load(journal.anchor_id)
    store.fail_next_anchor = True

    with pytest.raises(
        agent_run_journal.AgentRunJournalError,
        match="anchor is unavailable",
    ):
        journal.tool_intent(
            ordinal=0,
            round_number=0,
            tool_index=0,
            action="write_file",
            args={"path": "secret.txt", "content": "private"},
            call_id="write-crash-window",
            mutating=True,
            idempotency="idempotent",
            target="workspace:secret.txt",
        )

    loaded = agent_run_journal.AgentRunJournal.load(
        contract.run_nonce,
        path=path,
        receipt_key_store=store,
        protected_expected=True,
    )
    assert store.load(journal.anchor_id) == prior_anchor
    state = loaded.resume_state()
    assert state.uncertain_mutation_steps == ("b0-r0-t0",)
    assert state.can_resume is False
    assert store.load(journal.anchor_id) == prior_anchor

    loaded.synchronize_anchor()
    assert store.load(journal.anchor_id) != prior_anchor
    assert loaded.records()[-1].kind == "tool_intent"


def test_protected_journal_missing_initial_anchor_is_not_bootstrapped_on_load(
    tmp_path,
) -> None:
    class InitialAnchorFailure(grace_key_store.StaticKeyStore):
        def compare_and_set(
            self,
            _journal_id,
            *,
            expected_digest,
            value,
        ):
            del expected_digest, value
            raise RuntimeError("simulated initial anchor outage")

    store = InitialAnchorFailure({"irene-privacy-hmac-v1": b"i" * 32})
    contract = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=store,
        run_nonce="protected-initial-anchor-missing",
    )
    path = tmp_path / "protected-initial-anchor.jsonl"

    with pytest.raises(
        agent_run_journal.AgentRunJournalError,
        match="anchor is unavailable",
    ):
        agent_run_journal.AgentRunJournal.create(
            contract,
            path=path,
            receipt_key_store=store,
            protected_expected=True,
        )
    before = path.read_bytes()
    assert before

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="anchor is missing",
    ):
        agent_run_journal.AgentRunJournal.load(
            contract.run_nonce,
            path=path,
            receipt_key_store=store,
            protected_expected=True,
        )
    assert path.read_bytes() == before


def test_protected_journal_concurrent_appends_are_serialized(tmp_path) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"q" * 32})
    contract = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=store,
        run_nonce="protected-concurrent-appends",
    )
    path = tmp_path / "protected-concurrent.jsonl"
    agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=store,
        protected_expected=True,
    )
    journals = tuple(
        agent_run_journal.AgentRunJournal.load(
            contract.run_nonce,
            path=path,
            receipt_key_store=store,
            protected_expected=True,
        )
        for _index in range(2)
    )
    receipt = {
        "schema_version": 1,
        "max_tokens": 10,
        "base_tokens": 1,
        "used_tokens": 1,
        "included_sources": [],
        "truncated_sources": [],
        "omitted_sources": [],
        "context_digest": hashlib.sha256(b"context").hexdigest(),
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        events = tuple(pool.map(lambda item: item.context_bound(receipt), journals))

    loaded = agent_run_journal.AgentRunJournal.load(
        contract.run_nonce,
        path=path,
        receipt_key_store=store,
        protected_expected=True,
    )
    records = loaded.records()
    assert {event.sequence for event in events} == {1, 2}
    assert [event.sequence for event in records] == [0, 1, 2]
    anchored = grace_key_store.ContentFreeReceiptHead.from_bytes(store.load(loaded.anchor_id) or b"")
    assert anchored.sequence == 2
    assert anchored.head_digest == records[-1].event_hash


def test_protected_journal_rejects_wrong_and_missing_anchors_without_repair(
    tmp_path,
) -> None:
    store = grace_key_store.StaticKeyStore({"irene-privacy-hmac-v1": b"a" * 32})
    first = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=store,
        run_nonce="protected-anchor-first",
    )
    first_path = tmp_path / "protected-anchor-first.jsonl"
    first_journal = agent_run_journal.AgentRunJournal.create(
        first,
        path=first_path,
        receipt_key_store=store,
        protected_expected=True,
    )
    first_journal.block_started(0, "plan")
    first_blob = store.load(first_journal.anchor_id)
    assert first_blob is not None

    second = _contract(
        tmp_path,
        protected=True,
        receipt_key_store=store,
        run_nonce="protected-anchor-second",
    )
    second_path = tmp_path / "protected-anchor-second.jsonl"
    second_journal = agent_run_journal.AgentRunJournal.create(
        second,
        path=second_path,
        receipt_key_store=store,
        protected_expected=True,
    )
    wrong_blob = store.load(second_journal.anchor_id)
    assert wrong_blob is not None
    assert store.compare_and_set(
        first_journal.anchor_id,
        expected_digest="sha256:" + hashlib.sha256(first_blob).hexdigest(),
        value=wrong_blob,
    )

    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="different authority",
    ):
        agent_run_journal.AgentRunJournal.load(
            first.run_nonce,
            path=first_path,
            receipt_key_store=store,
            protected_expected=True,
        )

    store.delete_anchor(first_journal.anchor_id)
    with pytest.raises(
        agent_run_journal.AgentRunJournalCorrupt,
        match="anchor is missing",
    ):
        agent_run_journal.AgentRunJournal.load(
            first.run_nonce,
            path=first_path,
            receipt_key_store=store,
            protected_expected=True,
        )
    assert store.load(first_journal.anchor_id) is None


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
