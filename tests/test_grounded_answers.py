"""Correct values and a real citation do not establish claim support."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from algo_cli import harness
from algo_cli.evals import grounded_answers as evaluation


def bodies_for(case):
    result = {}
    for supports in case.claims.values():
        for support in supports:
            result[support.record_id] = result.get(support.record_id, "") + "\n\n" + support.text
    return result


def answer_for(case):
    supports = list(dict.fromkeys(supports[0] for supports in case.claims.values()))
    return {
        "answers": deepcopy(case.answers),
        "evidence": [{"record_id": support.record_id, "quote": support.text} for support in supports],
    }


@pytest.mark.parametrize("case", evaluation.CASES, ids=lambda case: case.name)
def test_every_frozen_case_has_a_positive_control(case):
    bodies = bodies_for(case)
    assert evaluation.validate_labels(case, bodies)
    result = evaluation.evaluate_answer(json.dumps(answer_for(case)), case, bodies)
    assert result["passed"], result
    assert set(result["supported_claims"]) == set(case.claims)
    assert result["missing_claims"] == []
    assert result["model_output_included"] is False


def test_original_false_acceptance_with_unrelated_quote_is_rejected():
    case = next(case for case in evaluation.CASES if case.name == "empty_stream_control")
    quote = "For model-not-found errors, distinguish authentication from model routing:"
    source = Path(__file__).parents[1] / "docs/provider-auth-recovery.md"
    body = source.read_text()
    assert quote in body
    answer = {"answers": case.answers, "evidence": [{"record_id": evaluation.AUTH, "quote": quote}]}
    report = evaluation.evaluate_answer(json.dumps(answer), case, {evaluation.AUTH: body})
    assert report["checks"] == {
        "valid_json_contract": True,
        "answer_values_correct": True,
        "quotes_bound_to_read_bodies": True,
        "all_claims_supported": False,
    }
    assert not report["passed"]


@pytest.mark.parametrize("source", [evaluation.AUTH, evaluation.EXECUTION])
def test_frozen_equivalent_authorities_support_both_retry_claims(source):
    case = next(case for case in evaluation.CASES if case.name == "empty_stream_control")
    quotes = [
        next(support.text for support in supports if support.record_id == source) for supports in case.claims.values()
    ]
    answer = {"answers": case.answers, "evidence": [{"record_id": source, "quote": "\n".join(quotes)}]}
    assert evaluation.evaluate_answer(json.dumps(answer), case, bodies_for(case))["passed"]


def test_all_declared_alternatives_match_current_public_source_bodies(monkeypatch, text_default_encoding):
    records = harness._runtime_capability_records()
    monkeypatch.setattr(harness, "load_index", lambda **_kwargs: {"records": records})
    bodies = {}
    for case in evaluation.CASES:
        for alternatives in case.claims.values():
            for support in alternatives:
                rid = support.record_id
                if ":runtime_capability:" in rid:
                    bodies[rid] = harness.read_record(rid).split("\n\n", 2)[2]
                else:
                    bodies[rid] = (Path(__file__).parents[1] / "docs" / rid.split(":", 2)[2]).read_text(
                        encoding="utf-8"
                    )
        assert evaluation.validate_labels(case, bodies), case.name


@pytest.mark.parametrize("defect", ["missing", "changed", "empty_claims", "unknown_field", "no_alternative"])
def test_label_failures_reject_case_before_execution(defect):
    case = deepcopy(evaluation.CASES[-2])
    bodies = bodies_for(case)
    if defect == "missing":
        bodies.pop(evaluation.AUTH)
    elif defect == "changed":
        bodies[evaluation.AUTH] = "The previously accepted alternative was removed."
    elif defect == "empty_claims":
        case = replace(case, claims={})
    elif defect == "unknown_field":
        case = replace(case, claims={"unrequested": next(iter(case.claims.values()))})
    else:
        case.claims["max_retries"] = ()
    assert not evaluation.validate_labels(case, bodies)


@pytest.mark.parametrize("missing", ["max_retries", "replays_completed_tools"])
def test_partial_support_does_not_qualify_complete_answer(missing):
    case = evaluation.CASES[-2]
    answer = answer_for(case)
    missing_quote = case.claims[missing][0].text
    answer["evidence"] = [item for item in answer["evidence"] if item["quote"] != missing_quote]
    report = evaluation.evaluate_answer(json.dumps(answer), case, bodies_for(case))
    assert report["checks"]["quotes_bound_to_read_bodies"]
    assert report["missing_claims"] == [missing]
    assert not report["passed"]


@pytest.mark.parametrize(
    "defect",
    [
        "unread",
        "invented",
        "wrong_source",
        "wrapper_only",
        "extra_key",
        "empty",
        "too_many",
        "short",
        "long",
        "not_object",
        "non_string_id",
        "non_string_quote",
    ],
)
def test_evidence_contract_fails_closed(defect):
    case = evaluation.CASES[0]
    bodies = bodies_for(case)
    answer = answer_for(case)
    item = answer["evidence"][0]
    if defect == "unread":
        bodies.clear()
    elif defect in {"invented", "wrapper_only"}:
        bodies[item["record_id"]] = "The source body does not contain that quote."
    elif defect == "wrong_source":
        bodies["other-public-source"] = item["quote"]
        item["record_id"] = "other-public-source"
    elif defect == "extra_key":
        item["invented_field"] = True
    elif defect == "empty":
        answer["evidence"] = []
    elif defect == "too_many":
        answer["evidence"] *= evaluation.MAX_EVIDENCE + 1
    elif defect == "short":
        item["quote"] = "too short"
    elif defect == "long":
        item["quote"] = "a" * 401
    elif defect == "not_object":
        answer["evidence"] = [item["quote"]]
    elif defect == "non_string_id":
        item["record_id"] = []
    else:
        item["quote"] = None
    assert not evaluation.evaluate_answer(json.dumps(answer), case, bodies)["passed"]


@pytest.mark.parametrize("value", [True, 2.0, "2", None, [], {}])
def test_answer_integer_contract_is_not_coerced(value):
    case = evaluation.CASES[-2]
    answer = answer_for(case)
    answer["answers"]["max_retries"] = value
    report = evaluation.evaluate_answer(json.dumps(answer), case, bodies_for(case))
    assert not report["checks"]["answer_values_correct"]
    assert not report["passed"]


@pytest.mark.parametrize(
    "defect", ["missing", "extra", "boolean_number", "duplicate_tool", "nested_tool", "extra_root"]
)
def test_exact_answer_shape_required(defect):
    case = evaluation.CASES[3] if "tool" in defect else evaluation.CASES[0]
    answer = answer_for(case)
    if defect == "missing":
        answer["answers"] = {}
    elif defect == "extra":
        answer["answers"]["extra"] = True
    elif defect == "boolean_number":
        answer["answers"]["live_evidence_overrides_saved_notes"] = 1
    elif defect == "duplicate_tool":
        answer["answers"]["tools"] = ["write_file", "edit_file", "write_file"]
    elif defect == "nested_tool":
        answer["answers"]["tools"] = ["write_file", "edit_file", {}]
    else:
        answer["extra"] = True
    assert not evaluation.evaluate_answer(json.dumps(answer), case, bodies_for(case))["passed"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "null",
        "[]",
        '{"answers":{},"answers":{},"evidence":[]}',
        '{"answers":{"x":1,"x":2},"evidence":[]}',
        '{"answers":NaN,"evidence":[]}',
        '{"answers":Infinity,"evidence":[]}',
        pytest.param(" " * (evaluation.MAX_ANSWER_CHARS + 1), id="oversized-answer"),
        pytest.param("[" * 2000 + "]" * 2000, id="deeply-nested-json"),
    ],
)
def test_malformed_json_is_a_failure_not_a_crash(text):
    report = evaluation.evaluate_answer(text, evaluation.CASES[0], {})
    assert not report["passed"]
    assert not report["checks"]["valid_json_contract"]


def test_whitespace_normalization_and_tool_order_are_explicitly_permitted():
    case = evaluation.CASES[3]
    answer = answer_for(case)
    answer["answers"]["tools"].reverse()
    for item in answer["evidence"]:
        item["quote"] = item["quote"].replace(" ", "\n")
    assert evaluation.evaluate_answer(json.dumps(answer), case, bodies_for(case))["passed"]


def test_report_does_not_include_rejected_output_or_arbitrary_identifiers():
    canary = "PRIVATE-UNTRUSTED-CONTENT-DO-NOT-EXPORT"
    case = evaluation.CASES[0]
    answer = answer_for(case)
    answer["evidence"] = [{"record_id": canary, "quote": canary}]
    result = evaluation.evaluate_answer(json.dumps(answer), case, {canary: canary})
    assert not result["passed"]
    assert canary not in json.dumps(result)
    assert set(result) == {
        "schema",
        "case",
        "passed",
        "checks",
        "supported_claims",
        "missing_claims",
        "model_output_included",
    }


def test_whitespace_only_quote_and_support_are_not_evidence():
    case = evaluation.CASES[0]
    answer = answer_for(case)
    answer["evidence"][0]["quote"] = " " * 20
    result = evaluation.evaluate_answer(json.dumps(answer), case, bodies_for(case))
    assert not result["checks"]["quotes_bound_to_read_bodies"]
    blank = evaluation.Support(evaluation.MEMORY, " " * 20)
    case = replace(case, claims={"live_evidence_overrides_saved_notes": (blank,)})
    assert not evaluation.validate_labels(case, {evaluation.MEMORY: "Unrelated source body"})


def test_prompts_do_not_disclose_labels_or_expected_values():
    for case in evaluation.CASES:
        prompt = evaluation.prompt(case)
        assert case.question in prompt
        assert all(key in prompt for key in case.answers)
        assert all(
            support.text not in prompt and support.record_id not in prompt
            for alternatives in case.claims.values()
            for support in alternatives
        )
    assert evaluation.protocol_cases()["schema"] == "algo-cli-documentation-answers-v2"
    assert evaluation.protocol_digest() == "sha256:59d839e135033fd81b0c347a7fae91637346905ca4e172d0832b1e3e91061a9e"
