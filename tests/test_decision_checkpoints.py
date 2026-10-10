from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from algo_cli import decision_checkpoints as checkpoint, decision_layer as decisions
from algo_cli.config import Config


def test_valid_routing_abstention_preserves_advice_and_allows_new_evidence(monkeypatch):
    from test_decision_coordinator import fixture
    from algo_cli import decision_coordinator, nathan_runtime

    cfg, providers = fixture(monkeypatch, decision_review_checkpoint_interval=1)
    providers["jev"].picked = "jev"
    providers["jev"].probability = 0.7
    providers["jev"].fit = 0.84
    monkeypatch.setattr(nathan_runtime, "preflight_runtime_tool", lambda *a: SimpleNamespace(
        allowed=True, policy=SimpleNamespace(grant_id="")))
    calls, notices = [], []

    def dispatch(name, args, cfg, **kwargs):
        calls.append(args)
        result = decisions.decide(cfg, **args)
        return SimpleNamespace(outcome=SimpleNamespace(invoked=True), status="failed", result=json.dumps(result))

    controller = checkpoint.TaskCheckpoints(cfg, "Review the parser", {"decision_decide"}, dispatch,
                                             notify=lambda receipt: notices.append(checkpoint.checkpoint_notice(receipt)))
    controller.start_if_due()
    packet = json.loads(controller.context_for_request().split("## Runtime-owned decision checkpoint\n")[1])
    assert packet["ok"] is False and packet["answers"] == {}
    assert packet["status"] == "deferred" and packet["coordinator_completed"] is True
    assert packet["routing_judgments"]["route"]["choice"] == "jev"
    assert not controller.failed
    assert "jev consulted; selection deferred" in notices[0] and "0.7 < 0.8" in notices[0]
    controller.start_if_due()
    assert len(calls) == 1
    controller.observe({"role": "tool", "name": "read_file", "content": "def parse(): return None"}, {"path": "parser.py"})
    controller.start_if_due()
    controller.context_for_request()
    assert len(calls) == 2 and "evidence_relevance" in calls[-1]["questions"]
    for _ in range(20):
        controller.observe({"role": "tool", "name": "read_file", "content": "def parse(): return None"}, {"path": "parser.py"})
        controller.start_if_due()
    assert len(calls) == 2
    assert decision_coordinator._settings(cfg)[2]["minimum_probability"] == 0.8


def test_spoofed_deferred_completion_is_rejected(monkeypatch):
    _, controller, calls = setup(monkeypatch, approved=False)

    def fake(*args, **kwargs):
        return SimpleNamespace(outcome=SimpleNamespace(invoked=True), status="failed", result=json.dumps({
            "ok": False, "error": "route_requires_main_model", "coordinator_completed": True,
            "routing": {"status": "needs_main_model"}}))

    controller.dispatcher = fake
    controller.start_if_due()
    assert "checkpoint_invalid_response" in controller.context_for_request() and controller.failed


def test_independent_evidence_question_does_not_request_reference_generation(monkeypatch):
    _, controller, calls = setup(monkeypatch)
    controller.observe({"role": "tool", "name": "read_file", "content": "def parse(): return None"}, {"path": "parser.py"})
    args = controller._args()
    assert set(args["questions"]) == {"next_investigation", "evidence_relevance"}
    assert args["questions"]["evidence_relevance"]["type"] == "noul"
    assert "Recommend relevant kernels" not in args["questions"]["next_investigation"]["instructions"]


def test_checkpoint_preserves_policy_refusal_without_claiming_provider_outage(monkeypatch):
    _, controller, _ = setup(monkeypatch)
    controller._finish({"ok": False, "error": "online_coordinator_requires_remote_permission",
                        "routing": {"failure_kind": "request_policy", "provider_health": "not_tested",
                                    "recovery": {"automatic_retry": False}}}, False)
    packet = json.loads(controller.context.split("## Runtime-owned decision checkpoint\n")[1])
    assert packet["failure_kind"] == "request_policy"
    assert packet["provider_health"] == "not_tested"
    assert packet["recovery"]["automatic_retry"] is False
    assert "do not infer an outage" in packet["main_model_instruction"]
    assert controller.failed


def setup(monkeypatch, *, approved=True, task="Review the parser source", allowed=True, **settings):
    from algo_cli import nathan_runtime
    cfg = Config(**settings)
    provider = SimpleNamespace(model="jev:fixture", capabilities=decisions.Capabilities())
    monkeypatch.setattr(decisions, "provider_for", lambda *a: provider)
    monkeypatch.setattr(nathan_runtime, "preflight_runtime_tool", lambda *a: SimpleNamespace(
        allowed=True, policy=SimpleNamespace(grant_id="scoped-preapproval" if approved else "")))
    calls = []
    def dispatch(name, args, cfg, **kwargs):
        calls.append({"name": name, "args": args, "thread": threading.current_thread().name,
                      "approval_mode": getattr(cfg, "_nathan_approval_mode", "interactive"), "auto": cfg.auto_approve_active})
        labels = args["questions"]["next_investigation"]["criteria"]
        probabilities = {key: 0.95 if key == "scope" else 0.05 / (len(labels) - 1) for key in labels}
        result = {"ok": True, "provider": "jev", "model": provider.model,
                  "action_authority": False, "real_world_verified": False,
                  "answers": {"next_investigation": {"type": "choice", "choice": "scope",
                                                     "probabilities": probabilities, "confidence": 0.9}},
                  "routing": {"coordinator": "jev", "status": "selected", "answers": {},
                              "recommendations": {"kernels": {"selected": {"id": "repo-intelligence", "status": "active", "execution_authority": False}}}}}
        if "evidence_relevance" in args["questions"]:
            result["answers"]["evidence_relevance"] = {"type": "noul", "noul": 0.9}
        return SimpleNamespace(outcome=SimpleNamespace(invoked=True), status="worked", result=json.dumps(result))
    controller = checkpoint.TaskCheckpoints(cfg, task, {"decision_decide"} if allowed else set(), dispatch)
    return cfg, controller, calls


def test_preapproved_task_runs_in_background_without_main_model_opt_in(monkeypatch):
    cfg, controller, calls = setup(monkeypatch)
    controller.start_if_due()
    context = controller.context_for_request()
    assert len(calls) == 1 and calls[0]["thread"].startswith("algo-task-checkpoint")
    assert calls[0]["args"]["task"] == "code-review"
    assert "repo-intelligence" in context and "Runtime-owned decision checkpoint" in context
    assert cfg.context_state["decision_checkpoints"][0]["background"] is True
    assert calls[0]["approval_mode"] == "never" and calls[0]["auto"] is False
    assert not hasattr(cfg, "_nathan_approval_mode")
    assert "parser source" not in json.dumps(cfg.context_state)
    controller.close()
    assert controller.executor is None


def test_interactive_approval_stays_on_main_thread(monkeypatch):
    _, controller, calls = setup(monkeypatch, approved=False)
    controller.start_if_due()
    assert calls[0]["thread"] == threading.current_thread().name
    assert controller.pending is None
    assert controller.context_for_request()


@pytest.mark.parametrize("task", ["hello", "What is your version?", "/harness status", "Review the code without Jev", "Review the code; do not call decision models"])
def test_non_tasks_do_not_send_model_calls(monkeypatch, task):
    _, controller, calls = setup(monkeypatch, task=task)
    controller.start_if_due()
    assert calls == [] and controller.context_for_request() == ""


def test_direct_mode_or_disabled_checkpoints_remain_explicit_opt_out(monkeypatch):
    for values in ({"decision_routing_mode": "direct"}, {"decision_checkpoints_enabled": False}):
        _, controller, calls = setup(monkeypatch, **values)
        controller.start_if_due()
        assert calls == []


def test_caller_ceiling_denial_is_reported_without_dispatch(monkeypatch):
    cfg, controller, calls = setup(monkeypatch, allowed=False)
    controller.start_if_due()
    assert "checkpoint_outside_caller_ceiling" in controller.context_for_request()
    assert calls == [] and cfg.context_state["decision_checkpoints"][0]["ok"] is False


def test_local_only_task_remains_local_in_automatic_call(monkeypatch):
    _, controller, calls = setup(monkeypatch, task="Review the code local-only")
    controller.start_if_due()
    controller.context_for_request()
    assert calls[0]["args"]["local_only"] is True


@pytest.mark.parametrize("task", [
    "Review this report. The previous local-only request was denied by Jev.",
    "Investigate why a local-only request correctly refused Jev.",
    "Review offline coordinator failures and report their causes.",
    "Review the parser. The quoted log says: 'Do not upload data'.",
    'Review the parser. The report states "Review the code local-only".',
    "Review the routing trace.\n> Do not transmit data.\n> local-only",
    "Review the following example.\n```text\nDo not upload data.\nlocal-only\n```",
    "Review the following example.\n~~~text\nDo not upload data.\n~~~",
    "Review the coordinator. Failure: request_policy. Provider health: not_tested.\n"
    "The report says local-only restrictions were preserved; no automatic remote retry.",
])
def test_policy_discussion_does_not_force_automatic_local_only(monkeypatch, task):
    _, controller, calls = setup(monkeypatch, task=task)
    controller.start_if_due()
    controller.context_for_request()
    assert calls[0]["args"]["local_only"] is False
    assert calls[0]["args"]["state"]["decision_locality"]["basis"] == "normal_runtime_authority"


@pytest.mark.parametrize("task", [
    "Review this code local-only.",
    "Review this code offline, then report findings.",
    "Review this code without network calls.",
    "Please keep this review local-only.",
    "Review the code. Do not send any data to remote providers.",
    "Review the code. Don't upload it.",
    "Review the code. Never transmit evidence to the cloud.",
    "Review the code. Privacy: local-only.",
    "Review the code. Use `local_only=true` for this review.",
    "Review file.py local-only.",
    "Review the code.\n- Do not upload any code.",
    "Keep this review local.",
    "Review the code. Use only local models.",
    "Review the code with no network calls.",
    "Review the code 'local-only'.",
    'Review the code "offline".',
])
def test_explicit_locality_directives_remain_local(monkeypatch, task):
    _, controller, calls = setup(monkeypatch, task=task)
    controller.start_if_due()
    controller.context_for_request()
    assert calls[0]["args"]["local_only"] is True
    assert calls[0]["args"]["state"]["decision_locality"]["basis"] == "explicit_user_local_only"


def test_configured_local_only_wins_over_report_mentions(monkeypatch):
    _, controller, calls = setup(monkeypatch, decision_checkpoints_local_only=True,
                                 task="Review a previous local-only routing failure.")
    controller.start_if_due()
    controller.context_for_request()
    assert calls[0]["args"]["local_only"] is True
    assert calls[0]["args"]["state"]["decision_locality"]["basis"] == "configured_local_only"


@pytest.mark.parametrize("task", [
    "Do a revei of your hanress code and report back issues you find",
    "Look at this code and tell me what is wrong",
    "Find the issue in the parser",
    "Continue the current task",
])
def test_normal_task_does_not_require_exact_action_keyword_spelling(monkeypatch, task):
    _, controller, calls = setup(monkeypatch, task=task)
    assert controller.enabled
    controller.start_if_due()
    controller.context_for_request()
    assert len(calls) == 1 and calls[0]["args"]["local_only"] is False


def test_reported_review_typo_receives_evidence_followup(monkeypatch):
    _, controller, calls = setup(monkeypatch, task="Do a revei of your hanress code and report back issues you find",
                                 decision_review_checkpoint_interval=1)
    assert controller.review
    controller.start_if_due()
    controller.context_for_request()
    controller.observe({"role": "tool", "name": "read_file", "content": "def parse(): return None"})
    controller.start_if_due()
    controller.context_for_request()
    assert len(calls) == 2


def test_bounded_evidence_followup_runs_and_preserves_failures(monkeypatch):
    _, controller, calls = setup(monkeypatch, decision_review_checkpoint_interval=2)
    controller.start_if_due()
    controller.context_for_request()
    controller.observe({"role": "tool", "name": "read_file", "content": "def parse(): return None"})
    controller.start_if_due()
    assert len(calls) == 1
    controller.observe({"role": "tool", "name": "search_files", "content": "ERR read: permission denied"})
    controller.start_if_due()
    controller.context_for_request()
    assert len(calls) == 2 and calls[1]["args"]["state"]["phase"] == "review-evidence"
    assert "permission denied" in json.dumps(calls[1]["args"]["state"]["recent_evidence"])


def test_does_not_upload_memory_thoughts_or_oversized_tool_outputs(monkeypatch):
    _, controller, calls = setup(monkeypatch)
    controller.observe({"role": "tool", "name": "memory_context", "content": "PRIVATE MEMORY"})
    controller.observe({"role": "assistant", "thinking": "PRIVATE THINKING"})
    controller.observe({"role": "tool", "name": "read_file", "content": "x" * (checkpoint.MAX_EXCERPT_BYTES + 1)})
    controller.start_if_due()
    controller.context_for_request()
    state = calls[0]["args"]["state"]
    assert len(state["recent_evidence"]) == 1
    assert "excerpt" not in state["recent_evidence"][0] and state["recent_evidence"][0]["omitted"]
    assert "PRIVATE" not in json.dumps(state)


def test_denial_or_uncertainty_is_not_retried_by_background_hook(monkeypatch):
    cfg, controller, calls = setup(monkeypatch)
    def deny(name, args, cfg, **kw):
        calls.append(name)
        return SimpleNamespace(outcome=SimpleNamespace(invoked=False), status="denied", result="Not approved")
    controller.dispatcher = deny
    controller.start_if_due()
    assert "checkpoint_authorization_or_policy_denied" in controller.context_for_request()
    for _ in range(20):
        controller.observe({"role": "tool", "name": "read_file", "content": "safe public source"})
        controller.start_if_due()
    assert len(calls) == 1 and not cfg.context_state["decision_checkpoints"][0]["ok"]


def test_three_checkpoint_cap_and_cleanup_are_enforced(monkeypatch):
    _, controller, calls = setup(monkeypatch, decision_review_checkpoint_interval=1)
    for index in range(10):
        controller.start_if_due()
        controller.context_for_request()
        controller.observe({"role": "tool", "name": "read_file", "content": f"safe public source {index}"})
    controller.close()
    assert len(calls) == 3 and controller.executor is None


def test_secret_is_redacted_before_automatic_task_submission(monkeypatch):
    secret = "sk-" + "a" * 48
    _, controller, calls = setup(monkeypatch, task="Review the code; token=" + secret)
    controller.start_if_due()
    controller.context_for_request()
    assert secret not in json.dumps(calls)


def test_oversized_task_does_not_silently_truncate_or_dispatch(monkeypatch):
    _, controller, calls = setup(monkeypatch, task="Review " + "x" * 5000)
    controller.start_if_due()
    assert "checkpoint_input_unavailable" in controller.context_for_request()
    assert not calls


def test_secret_or_non_source_read_is_not_automatically_forwarded(monkeypatch):
    _, controller, calls = setup(monkeypatch)
    controller.observe({"role": "tool", "name": "read_file", "content": "PRIVATE RECORD"}, {"path": ".env"})
    controller.observe({"role": "tool", "name": "read_file", "content": "PRIVATE NOTES"}, {"path": "private-notes.md"})
    controller.start_if_due()
    controller.context_for_request()
    assert "PRIVATE" not in json.dumps(calls)
    assert all("omitted" in row for row in calls[0]["args"]["state"]["recent_evidence"])


def test_main_loop_receives_automatic_checkpoint_without_model_call(monkeypatch, tmp_path):
    from algo_cli import main
    from test_main_helpers import _patch_agent_loop_for_tool_policy_test
    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    cfg, controller, calls = setup(monkeypatch)
    cfg.cwd = str(tmp_path)
    cfg.model_adaptive = False
    cfg.max_tool_iterations = 2
    cfg.num_ctx = 131072
    monkeypatch.setattr(main, "dispatch_action", controller.dispatcher)
    monkeypatch.setattr(Config, "save", lambda self: None)
    requests = []
    class Client:
        def chat(self, **kwargs):
            requests.append(kwargs)
            assert calls and cfg.context_state["decision_checkpoints"][0]["ok"] is True
            assert "Runtime-owned decision checkpoint" in str(kwargs["messages"])
            return iter([{"message": {"content": "No executable verification has run; this is only a source review."}}])
    main.agent_loop(Client(), cfg, "Review the parser source")
    assert len(requests) == 1 and len(calls) == 1
    assert "Runtime-owned decision checkpoint" not in str(cfg.messages)


def test_revoked_preapproval_never_prompts_or_calls_provider_from_worker(monkeypatch, tmp_path):
    from algo_cli import main, nathan_runtime
    from algo_cli.james_dispatch import dispatch_action
    from algo_cli.nathan_program_runtime import authorization_for_actions
    from algo_cli.samuel_policy_engine import resolve_action
    cfg = Config(cwd=str(tmp_path))
    cfg._algo_program_authorization = authorization_for_actions(("decision_decide",))
    controller = checkpoint.TaskCheckpoints(cfg, "Review public source", {"decision_decide"}, dispatch_action)
    action = resolve_action("decision_decide", controller._args(), cwd=cfg.cwd)
    session = nathan_runtime.authority_session_for(cfg)
    session.issue(action, source="unit-test-owner-approval", now=time.time(), maximum_action_count=1, ttl_seconds=60)
    monkeypatch.setattr(main, "ask_approval", lambda *a, **kw: pytest.fail("Worker must not request approval"))
    monkeypatch.setattr(decisions, "provider_for", lambda *a: pytest.fail("Revoked grant must not call provider"))
    class Executor:
        def __init__(self, **kwargs):
            pass
        def submit(self, function, callback):
            session.revoke_all()
            future = Future()
            future.set_result(function(callback))
            return future
        def shutdown(self, **kwargs):
            pass
    monkeypatch.setattr(checkpoint, "CheckpointWorker", Executor)
    controller.start_if_due()
    assert "checkpoint_authorization_or_policy_denied" in controller.context_for_request()
    assert cfg.auto_approve_active is False


def test_initial_checkpoint_wait_is_bounded_and_pending_is_not_failure(monkeypatch):
    cfg, controller, calls = setup(monkeypatch, decision_initial_wait_seconds=0.01)
    release = threading.Event()
    original = controller.dispatcher
    controller.dispatcher = lambda *a, **kw: release.wait(1) and original(*a, **kw)
    controller.start_if_due()
    start = time.monotonic()
    context = controller.context_for_request()
    assert time.monotonic() - start < 0.1
    assert "Pending decision checkpoint" in context and not controller.failed
    assert cfg.context_state["decision_checkpoints"][-1]["status"] == "pending"
    release.set()
    controller.pending.result(timeout=1)
    assert "Runtime-owned decision checkpoint" in controller.context_for_request()
    assert cfg.context_state["decision_checkpoints"][-1]["status"] == "validated"
    assert len(cfg.context_state["decision_checkpoints"]) == 1
    controller.close()


def test_evidence_followup_does_not_wait_or_publish_after_close(monkeypatch):
    cfg, controller, calls = setup(monkeypatch, decision_review_checkpoint_interval=1)
    controller.start_if_due()
    controller.context_for_request()
    controller.observe({"role": "tool", "name": "read_file", "content": "def new(): pass"}, {"path": "new.py"})
    entered, release = threading.Event(), threading.Event()
    original = controller.dispatcher
    def dispatch(*a, **kw):
        entered.set()
        release.wait(1)
        return original(*a, **kw)
    controller.dispatcher = dispatch
    controller.start_if_due()
    assert entered.wait(1)
    worker_future = controller.pending
    start = time.monotonic()
    assert "Pending decision checkpoint" in controller.context_for_request()
    assert time.monotonic() - start < 0.05
    assert controller.executor.thread.daemon
    controller.close()
    assert controller.cancel_event.is_set()
    assert cfg.context_state["decision_checkpoints"][-1]["status"] == "cancelled"
    snapshot = json.dumps(cfg.context_state)
    release.set()
    worker_future.result(timeout=1)
    controller.context_for_request()
    assert json.dumps(cfg.context_state) == snapshot


def test_timeout_boundary_completion_is_not_mislabeled_failed(monkeypatch):
    cfg, controller, _calls = setup(monkeypatch)
    controller.count = 1
    valid = {"ok": True, "provider": "jev", "answers": {"fixture": "advisory"}}
    class FinishingFuture:
        def __init__(self):
            self.reads = 0
        def result(self, timeout=None):
            self.reads += 1
            if self.reads == 1:
                raise checkpoint.FutureTimeout()
            return valid
        def done(self):
            return True
    controller.pending = FinishingFuture()
    assert '"status":"validated"' in controller.context_for_request()
    assert cfg.context_state["decision_checkpoints"][-1]["status"] == "validated"


def test_close_never_publishes_a_completed_but_unconsumed_result(monkeypatch):
    cfg, controller, _calls = setup(monkeypatch)
    controller.count = 1
    notices = []
    controller.notify = notices.append
    controller.pending = Future()
    controller.pending.set_result({"ok": True, "provider": "jev", "answers": {"fixture": "advisory"}})
    controller.close()
    assert notices == [] and controller.context == ""
    assert cfg.context_state["decision_checkpoints"][-1]["status"] == "not_consumed"
    assert cfg.context_state["decision_checkpoints"][-1]["delivered_to_main"] is False


def test_completed_worker_timeout_exception_remains_a_failure(monkeypatch):
    cfg, controller, _calls = setup(monkeypatch)
    controller.pending = Future()
    controller.pending.set_exception(TimeoutError("worker timed out"))
    controller.context_for_request()
    assert cfg.context_state["decision_checkpoints"][-1]["status"] == "failed"
    assert controller.pending is None


def test_cancellation_after_routing_prevents_specialist_dispatch(monkeypatch):
    from test_decision_coordinator import fixture
    cfg, providers = fixture(monkeypatch)
    cfg._decision_cancel_event = threading.Event()
    original = providers["jev"].infer
    def route(*a, **kw):
        response = original(*a, **kw)
        cfg._decision_cancel_event.set()
        return response
    providers["jev"].infer = route
    result = decisions.decide(cfg, "public fixture", {"step": {"type": "noul", "instructions": "Is this present?"}}, local_only=False)
    assert not result["ok"] and result["error"] == "decision_cancelled"
    assert not providers["clef-flash"].calls
