from __future__ import annotations

from algo_cli.arthur_outcomes import OutcomeStatus, VerificationStatus, normalize_action_outcome
from algo_cli.samuel_policy_engine import resolve_action


def test_observation_failure_is_known_and_retryable_when_idempotent(tmp_path) -> None:
    action = resolve_action("read_file", {"path": "missing.txt"}, cwd=str(tmp_path))
    outcome = normalize_action_outcome(
        action,
        "Error: file not found",
        reported_status="failed",
        invoked=True,
    )

    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.retry_allowed is True
    assert outcome.verification is VerificationStatus.FAILED


def test_failed_unknown_possible_mutation_is_not_flattened_to_failure(tmp_path) -> None:
    action = resolve_action("run_shell", {"command": "unknown-command"}, cwd=str(tmp_path))
    outcome = normalize_action_outcome(
        action,
        "process disconnected",
        reported_status="failed",
        invoked=True,
    )

    assert outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert outcome.retry_allowed is False
    assert outcome.verification is VerificationStatus.UNKNOWN
    assert "Do not retry automatically" in outcome.model_text()


def test_preinvoke_denial_can_never_be_unknown(tmp_path) -> None:
    action = resolve_action("write_file", {"path": "x", "content": "y"}, cwd=str(tmp_path))
    outcome = normalize_action_outcome(
        action,
        "User denied this operation.",
        reported_status="denied",
        invoked=False,
    )

    assert outcome.status is OutcomeStatus.DENIED
    assert outcome.retry_allowed is False


def test_receipt_omits_result_content(tmp_path) -> None:
    action = resolve_action("remember", {"fact": "PRIVATE"}, cwd=str(tmp_path))
    outcome = normalize_action_outcome(
        action,
        "PRIVATE",
        reported_status="worked",
        invoked=True,
        effect_id="effect-1",
        idempotency_key="key-1",
        fencing_token=3,
    )

    receipt = outcome.receipt()
    assert "result" not in receipt
    assert "PRIVATE" not in str(receipt)
    assert receipt["fencing_token"] == 3
    assert receipt["compensation_action"] == ""


def test_preinvoke_timeout_and_cancellation_are_typed_and_retryable(tmp_path) -> None:
    action = resolve_action("read_file", {"path": "README.md"}, cwd=str(tmp_path))

    timed_out = normalize_action_outcome(
        action,
        "deadline elapsed",
        reported_status="timed_out",
        invoked=False,
    )
    cancelled = normalize_action_outcome(
        action,
        "caller cancelled",
        reported_status="cancelled",
        invoked=False,
    )

    assert timed_out.status is OutcomeStatus.TIMED_OUT
    assert timed_out.retry_allowed is True
    assert cancelled.status is OutcomeStatus.CANCELLED
    assert cancelled.retry_allowed is True
    assert "Timed out outcome:" in timed_out.model_text()
    assert "Cancelled outcome:" in cancelled.model_text()


def test_invoked_mutation_timeout_is_normalized_to_unknown(tmp_path) -> None:
    action = resolve_action("remember", {"fact": "bounded"}, cwd=str(tmp_path))

    outcome = normalize_action_outcome(
        action,
        "deadline elapsed after dispatch",
        reported_status="failed",
        invoked=True,
        error_code="deadline_after_dispatch",
    )

    assert outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert outcome.retry_allowed is False
