"""Exercise runner boundaries with public fixtures, without model or memory calls."""

from copy import deepcopy
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace

import pytest

from algo_cli import main as runtime, oliver_oneshot
from algo_cli.config import Config
from algo_cli.evals import grounded_answers as evaluation
from scripts import grounded_answer_qualification as runner

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX qualification supervisor")


@pytest.fixture
def context(monkeypatch):
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: deepcopy(cfg)))
    case = evaluation.CASES[0]
    support = next(iter(case.claims.values()))[0]
    monkeypatch.setattr(runner, "freeze_sources", lambda: ({"public": "fixture"}, frozenset({support.record_id})))
    monkeypatch.setattr(signal, "alarm", lambda _: 0)
    calls = []

    def public_tool(name, args, _cfg):
        calls.append(name)
        if name == "harness_search":
            return "- " + support.record_id
        assert args["record_id"] == support.record_id
        return "# Public fixture\n\nSource: fixture\n\n" + support.text

    monkeypatch.setattr(runtime, "run_tool", public_tool)

    def synthetic_model(*, prompt, approval_mode, cfg_overrides, stream):
        assert prompt == evaluation.prompt(case)
        assert approval_mode == "never"
        assert cfg_overrides["echo_veil_protection"] == "required"
        assert not cfg_overrides["memory_auto_capture_enabled"]
        assert not cfg_overrides["intuition_capture_enabled"]
        runtime.run_tool("harness_search", {"query": "public fixture"}, cfg)
        runtime.run_tool("harness_read", {"record_id": support.record_id}, cfg)
        answer = {"answers": case.answers, "evidence": [{"record_id": support.record_id, "quote": support.text}]}
        for event in (
            {"type": "session_start", "model": runner.MODEL},
            {"type": "model_round"},
            {"type": "tool_result", "status": "ok"},
            {"type": "content", "text": json.dumps(answer)},
            {
                "type": "done",
                "status": "complete",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        ):
            stream.write(json.dumps(event) + "\n")
        return 0

    monkeypatch.setattr(oliver_oneshot, "run_oneshot", synthetic_model)
    return cfg, case, support, calls


def protocol():
    return {"sources": {"public": "fixture"}, "case_protocol_digest": evaluation.protocol_digest()}


def test_public_fixture_runs_real_observation_and_grading(context, tmp_path):
    _, case, _, calls = context
    original = runtime.run_tool
    report = runner.run_case(case, protocol(), tmp_path)
    assert report["passed"], report
    assert calls == ["harness_search", "harness_read"]
    assert runtime.run_tool is original
    assert report["rounds"] == 1
    assert report["usage"]["total_tokens"] == 120
    assert not report["model_output_included"]


def test_required_run_does_not_require_changing_saved_optional_policy(context, tmp_path):
    cfg, case, _, _ = context
    cfg.echo_veil_protection = "optional"
    report = runner.run_case(case, protocol(), tmp_path)
    assert report["passed"], report
    assert cfg.echo_veil_protection == "optional"


def test_deadline_cancels_instead_of_becoming_a_transport_retry(context, monkeypatch, tmp_path):
    _, case, _, _ = context

    def expire(**kwargs):
        signal.getsignal(signal.SIGALRM)(signal.SIGALRM, None)

    monkeypatch.setattr(oliver_oneshot, "run_oneshot", expire)
    report = runner.run_case(case, protocol(), tmp_path)
    assert not report["passed"]
    assert not report["checks"]["deadline_not_expired"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("echo_veil_enabled", False),
        ("echo_veil_protection", "invalid"),
        ("memory_auto_capture_enabled", True),
        ("intuition_capture_enabled", True),
    ],
)
def test_missing_protection_or_capture_prevents_model_call(context, monkeypatch, tmp_path, field, value):
    cfg, case, _, calls = context
    setattr(cfg, field, value)
    monkeypatch.setattr(oliver_oneshot, "run_oneshot", lambda **_: pytest.fail("no model call"))
    with pytest.raises(ValueError):
        runner.run_case(case, protocol(), tmp_path)
    assert not calls


@pytest.mark.parametrize("field", ["sources", "case_protocol_digest"])
def test_source_or_protocol_change_stops_before_model(context, monkeypatch, tmp_path, field):
    _, case, _, _ = context
    frozen = protocol()
    frozen[field] = "changed"
    monkeypatch.setattr(oliver_oneshot, "run_oneshot", lambda **_: pytest.fail("no model call"))
    with pytest.raises(ValueError, match="protocol_source_changed"):
        runner.run_case(case, frozen, tmp_path)


@pytest.mark.parametrize(
    "failure", ["exception", "cancellation", "changed_settings", "changed_source", "removed_source"]
)
def test_runtime_failures_are_retained_and_hooks_restored(context, monkeypatch, tmp_path, failure):
    cfg, case, _, _ = context
    original_model, original_tool = oliver_oneshot.run_oneshot, runtime.run_tool

    def failing_model(**kwargs):
        if failure == "exception":
            raise RuntimeError("PRIVATE-CANARY")
        if failure == "cancellation":
            raise KeyboardInterrupt
        code = original_model(**kwargs)
        if failure == "changed_settings":
            cfg.echo_veil_embedding_gpu_layers += 1
        elif failure == "changed_source":
            monkeypatch.setattr(runner, "freeze_sources", lambda: ({"public": "changed"}, frozenset()))
        else:

            def removed():
                raise ValueError("PRIVATE-CANARY")

            monkeypatch.setattr(runner, "freeze_sources", removed)
        return code

    monkeypatch.setattr(oliver_oneshot, "run_oneshot", failing_model)
    report = runner.run_case(case, protocol(), tmp_path)
    assert not report["passed"]
    assert runtime.run_tool is original_tool
    assert "PRIVATE-CANARY" not in json.dumps(report)
    if failure == "changed_settings":
        assert not report["checks"]["configuration_restored"]
    if "source" in failure:
        assert not report["checks"]["source_stable"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("write_file", {"path": "PRIVATE-CANARY"}),
        ("remember", {"text": "PRIVATE-CANARY"}),
        ("PRIVATE-CANARY", {}),
        ("harness_read", {"record_id": "PRIVATE-CANARY"}),
        ("harness_read", {"record_id": []}),
    ],
)
def test_guard_never_executes_disallowed_tools_or_nonpublic_reads(name, args):
    trace = runner.ReadOnlyTrace(lambda *_: pytest.fail("no execution"), frozenset())
    assert trace(name, args, Config()).startswith("Error:")
    assert trace.rows[0]["blocked"]
    assert "PRIVATE-CANARY" not in json.dumps(trace.rows)


def test_trace_saves_only_allowlisted_public_ids_and_no_query_or_result():
    rid = evaluation.MEMORY
    trace = runner.ReadOnlyTrace(lambda *_: f"- {rid}\n- PRIVATE-CANARY\nPRIVATE-CANARY", frozenset({rid}))
    trace("harness_search", {"query": "PRIVATE-CANARY"}, Config())
    assert trace.rows == [{"name": "harness_search", "blocked": False, "error": False, "ranked_ids": [rid]}]
    assert "PRIVATE-CANARY" not in json.dumps(trace.rows)


@pytest.mark.parametrize("response", [None, "Error: PRIVATE-CANARY"])
def test_bad_reads_do_not_create_evidence(response):
    trace = runner.ReadOnlyTrace(lambda *_: response, frozenset({evaluation.MEMORY}))
    trace("harness_read", {"record_id": evaluation.MEMORY}, Config())
    assert trace.rows[0]["error"]
    assert not trace.bodies
    assert "PRIVATE-CANARY" not in json.dumps(trace.rows)


@pytest.mark.parametrize("limits", [(1000, 20), (20, 1000), (1000, 1000)])
def test_shorter_reread_does_not_discard_observed_evidence(limits):
    case = evaluation.CASES[0]
    support = next(iter(case.claims.values()))[0]
    body = "Public introductory material. " * 4 + support.text

    def read(_name, args, _cfg):
        return "# Public fixture\n\nSource: fixture\n\n" + body[: args["max_chars"]]

    trace = runner.ReadOnlyTrace(read, frozenset({support.record_id}))
    for limit in limits:
        trace("harness_read", {"record_id": support.record_id, "max_chars": limit}, Config())
    answer = {"answers": case.answers, "evidence": [{"record_id": support.record_id, "quote": support.text}]}
    assert evaluation.evaluate_answer(json.dumps(answer), case, trace.bodies)["passed"]
    assert trace.bodies[support.record_id] == body
    assert not any(row["error"] for row in trace.rows)


def test_incompatible_source_views_invalidate_evidence_instead_of_joining_it():
    views = iter(["First public source body.", "Different public source body."])
    trace = runner.ReadOnlyTrace(
        lambda *_: "# Public fixture\n\nSource: fixture\n\n" + next(views), frozenset({evaluation.MEMORY})
    )
    for _ in range(2):
        trace("harness_read", {"record_id": evaluation.MEMORY}, Config())
    assert trace.rows[-1]["error"]
    assert evaluation.MEMORY not in trace.bodies


def test_event_extraction_ignores_reasoning_and_pretool_drafts():
    events = [
        {"type": "session_start", "model": runner.MODEL},
        {"type": "content", "text": "PRIVATE-CANARY"},
        {"type": "tool_result", "status": "ok", "summary": "PRIVATE-CANARY"},
        {"type": "thinking", "text": "PRIVATE-CANARY"},
        {"type": "model_round", "private": "PRIVATE-CANARY"},
        {"type": "content", "text": "answer"},
        {
            "type": "done",
            "status": "complete",
            "usage": {"total_tokens": 5, "prompt_tokens": True, "private": "PRIVATE-CANARY"},
        },
    ]
    final, checks, metrics = runner.event_summary("\n".join(json.dumps(event) for event in events))
    assert final == "answer"
    assert all(checks.values())
    assert metrics == {"rounds": 1, "usage": {"total_tokens": 5}}
    assert "PRIVATE-CANARY" not in json.dumps((final, checks, metrics))


@pytest.mark.parametrize("text", ["[]", "garbled PRIVATE-CANARY", '{"type":"done","status":"partial"}', ""])
def test_malformed_or_partial_events_fail(text):
    _, checks, _ = runner.event_summary(text)
    assert not all(checks.values())


def test_capture_is_bounded(monkeypatch):
    monkeypatch.setattr(runner, "MAX_CAPTURE_CHARS", 10)
    output = runner.BoundedCapture()
    assert output.write("12345") == 5
    with pytest.raises(ValueError, match="capture_limit"):
        output.write("123456")
    assert output.getvalue() == "12345"


def test_existing_receipt_is_never_overwritten(tmp_path):
    path = tmp_path / "report.json"
    runner.write_report(path, {"passed": False})
    with pytest.raises(FileExistsError):
        runner.write_report(path, {"passed": True})
    assert json.loads(path.read_text()) == {"passed": False}


def test_parent_freezes_protocol_before_worker_and_keeps_failed_results(context, monkeypatch, tmp_path):
    _, case, _, _ = context
    monkeypatch.setattr(evaluation, "CASES", (case,))
    monkeypatch.setattr(runner.sys, "flags", SimpleNamespace(isolated=True))
    monkeypatch.setattr(runner.sys, "prefix", str(Path(runner.harness.__file__).parents[1]))
    output = tmp_path / "fresh"
    monkeypatch.setattr(runner.sys, "argv", ["qualification", "--output", str(output)])
    called = []

    def worker(command, **kwargs):
        assert (output / "protocol.json").exists()
        assert kwargs["timeout"] == 150
        assert command[1] == "-I"
        called.append(command[-1])
        return SimpleNamespace(
            returncode=1, stdout=json.dumps({"case": case.name, "passed": False, "model_output_included": False})
        )

    monkeypatch.setattr(runner.subprocess, "run", worker)
    assert runner.main() == 1
    assert called == [case.name]
    summary = json.loads((output / "summary.json").read_text())
    assert summary["passed"] == 0 and summary["total"] == 1
    assert json.loads((output / f"{case.name}.json").read_text())["passed"] is False


@pytest.mark.parametrize("defect", ["missing", "historical", "wrapper_only", "changed_span"])
def test_source_preflight_requires_live_body_support(monkeypatch, defect):
    case = evaluation.CASES[0]
    rid = evaluation.MEMORY
    monkeypatch.setattr(evaluation, "CASES", (case,))
    monkeypatch.setattr(runner, "source_snapshot", lambda _: {})
    monkeypatch.setattr(
        harness := runner.harness,
        "retrieval_index",
        lambda **_: {"records": [] if defect == "missing" else [{"id": rid}]},
    )
    monkeypatch.setattr(harness, "is_excluded_from_retrieval", lambda _: defect == "historical")
    support = next(iter(case.claims.values()))[0].text
    response = (
        f"# {support}\n\nSource: wrapper\n\nUnrelated body."
        if defect == "wrapper_only"
        else "# Public\n\nSource: fixture\n\nThe supporting span changed."
    )
    monkeypatch.setattr(harness, "read_record", lambda *_args, **_kwargs: response)
    with pytest.raises(ValueError):
        runner.freeze_sources()
