"""Machine-keyed counterexamples retain advice without granting truth or authority."""

from __future__ import annotations

import copy
from fractions import Fraction
from itertools import permutations, product
import json

import pytest

from algo_cli import decision_layer as decisions
from algo_cli import execution_guardrails, nathan_runtime
from algo_cli.config import Config
from algo_cli.james_dispatch import dispatch_action


def path_costs(edge_cost: int) -> list[int]:
    edges = {("S", "A"): 4, ("S", "B"): 1, ("B", "A"): edge_cost,
             ("A", "T"): 1, ("B", "T"): 10}
    costs = []
    for size in range(3):
        for middle in permutations(("A", "B"), size):
            route = ("S", *middle, "T")
            hops = list(zip(route, route[1:]))
            if all(hop in edges for hop in hops):
                costs.append(sum(edges[hop] for hop in hops))
    return sorted(costs)


def test_supplied_graph_and_changed_premise_have_independent_exact_keys():
    assert path_costs(1) == [3, 5, 11]
    assert path_costs(8) == [5, 10, 11]
    assert min(path_costs(1)) != min(path_costs(8))


def test_reported_probability_and_modular_examples_have_exact_keys():
    dice = list(product(range(1, 7), repeat=2))
    conditioned = [pair for pair in dice if 6 in pair]
    successes = [pair for pair in conditioned if pair == (6, 6)]
    assert Fraction(len(successes), len(conditioned)) == Fraction(1, 11)
    assert Fraction(sum(n % 3 == 0 or n % 5 == 0 for n in range(1, 101)), 100) == Fraction(47, 100)
    assert [n for n in range(1, 101) if n % 7 == 3 and n % 5 == 4] == [24, 59, 94]
    assert abs(Fraction(54, 100) - Fraction(1, 11)) > Fraction(5, 100)
    assert abs(Fraction(22, 100) - Fraction(1, 11)) > Fraction(5, 100)


class ObservedWrongProvider:
    capabilities = decisions.Capabilities()
    name = "jev"
    model = "jev:synthetic-regression"
    local = True

    def __init__(self, answer: dict):
        self.answer = answer

    def infer(self, state, questions, timeout):
        return {"model": self.model, "answers": {"answer": copy.deepcopy(self.answer)}}


@pytest.mark.parametrize("edge_cost,wrong", [(1, "cost5"), (8, "cost3")])
def test_confident_wrong_graph_choice_is_typed_advice_not_truth(monkeypatch, edge_cost, wrong):
    labels = ("cost3", "cost5", "cost11")
    answer = {"type": "choice", "choice": wrong, "confidence": 0.95,
              "probabilities": {label: 0.95 if label == wrong else 0.025 for label in labels}}
    monkeypatch.setattr(decisions, "provider_for", lambda *args: ObservedWrongProvider(answer))
    questions = {"answer": {"type": "choice", "instructions": "Minimum directed S-to-T cost?",
                             "criteria": {label: label.removeprefix("cost") for label in labels}}}
    result = decisions.decide(Config(), {"b_to_a": edge_cost}, questions, provider="jev")
    assert result["ok"] and result["answers"]["answer"] == answer
    assert int(result["answers"]["answer"]["choice"][4:]) != min(path_costs(edge_cost))
    assert result["capabilities"]["confidence_semantics"] == "distribution_concentration_not_correctness"
    assert result["advisory_only"] and not result["action_authority"] and not result["real_world_verified"]


@pytest.mark.parametrize("observed", [0.54, 0.22, 0.95])
def test_wrong_noul_or_explicit_policy_cannot_claim_exact_probability(monkeypatch, observed):
    answer = {"type": "noul", "noul": observed}
    monkeypatch.setattr(decisions, "provider_for", lambda *args: ObservedWrongProvider(answer))
    questions = {"answer": {"type": "noul", "instructions": "Probability of success among 11 equiprobable outcomes?"}}
    policy = {"noul_thresholds": {"false_at_or_below": 0.1, "true_at_or_above": 0.9}}
    result = decisions.decide(Config(), {"successes": 1, "total": 11}, questions, provider="jev", policy=policy)
    assert result["ok"] and result["answers"]["answer"]["noul"] == observed
    assert Fraction(str(observed)) != Fraction(1, 11)
    assert result["derived"]["answer"] is (True if observed >= 0.9 else None)
    assert not result["action_authority"] and not result["real_world_verified"]


def test_actual_dispatch_of_wrong_advice_cannot_complete_workspace_mutation(monkeypatch, tmp_path):
    answer = {"type": "choice", "choice": "cost5", "confidence": 0.95,
              "probabilities": {"cost3": 0.025, "cost5": 0.95, "cost11": 0.025}}
    monkeypatch.setattr(decisions, "provider_for", lambda *args: ObservedWrongProvider(answer))
    cfg = Config(cwd=str(tmp_path))
    cfg._nathan_approval_mode = "never"
    args = {"state": {"edges": [["S", "A", 4], ["S", "B", 1], ["B", "A", 1], ["A", "T", 1], ["B", "T", 10]]},
            "questions": {"answer": {"type": "choice", "instructions": "Minimum cost?",
                                       "criteria": {"cost3": "3", "cost5": "5", "cost11": "11"}}},
            "provider": "jev", "local_only": True}
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        execution_guardrails.record_mutation("changed.py", success=True, operation="write_file")
        observed = dispatch_action("decision_decide", args, cfg, render=False)
        assert observed.outcome.invoked and json.loads(observed.result)["ok"]
        nathan_runtime.record_tool_attempt(cfg, name="decision_decide", args=args, result=observed.result, status="worked")
        assert not execution_guardrails.completion_decision().allowed
    finally:
        execution_guardrails.end_execution_scope(scope)
