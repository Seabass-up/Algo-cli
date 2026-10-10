from __future__ import annotations

import copy
import errno
import json
from types import SimpleNamespace

import pytest

from algo_cli import decision_coordinator as c, decision_layer as d, tools
from algo_cli.config import Config
from algo_cli.tool_context import select_tools_for_prompt


QUESTION = {"step": {"type": "choice", "instructions": "Which next observation fits?",
                     "criteria": {"read": "Read the current fixture", "defer": "No suitable observation"}}}


class Provider:
    capabilities = d.Capabilities()

    def __init__(self, name, *, failure=None, picked="clef-flash", probability=0.95, fit=0.95, ready=True):
        self.name, self.model, self.local = name, name + ":fixture", name != "jev"
        self.failure, self.picked, self.probability, self.fit, self.ready = failure, picked, probability, fit, ready
        self.calls = []

    def status(self):
        return {"ready": self.ready, "available": self.ready, "enabled": self.ready, "loaded": False}

    def infer(self, state, questions, timeout):
        self.calls.append((copy.deepcopy(state), copy.deepcopy(questions), timeout))
        if self.failure:
            raise d.DecisionError(self.failure)
        if "route" in questions:
            keys = list(questions["route"]["criteria"])
            probabilities = {key: ((1 - self.probability) / (len(keys) - 1)) for key in keys}
            probabilities[self.picked] = self.probability
            answers = {"route": {"type": "choice", "choice": self.picked, "probabilities": probabilities,
                                  "confidence": 0.5}, "fit": {"type": "noul", "noul": self.fit}}
            for kind in ("kernels", "algorithms"):
                if kind in questions:
                    keys = list(questions[kind]["criteria"])
                    answers[kind] = {"type": "choice", "choice": "item_0", "confidence": 0.8,
                                     "probabilities": {key: 0.95 if key == "item_0" else 0.05 / (len(keys) - 1) for key in keys}}
        else:
            answers = {"step": {"type": "choice", "choice": "read", "confidence": 0.8,
                                "probabilities": {"read": 0.95, "defer": 0.05}}}
        return {"model": self.model, "answers": answers}


def fixture(monkeypatch, **kwargs):
    providers = {name: Provider(name) for name in ("jev", "clef-flash", "tev1")}
    monkeypatch.setattr(d, "provider_for", lambda name, cfg: providers[name])
    monkeypatch.setattr(c, "connectivity", lambda **kw: {"state": "online", "basis": "test_fixture"})
    monkeypatch.setattr(c.decision_catalog, "candidates", lambda *a: {
        "kernels": [], "algorithms": [], "catalog_sha256": "fixture", "issues": [], "scope": "fixture"})
    cfg = Config(decision_router_candidates=list(providers), **kwargs)
    return cfg, providers


def test_main_question_routes_through_jev_then_one_answer_provider(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    result = d.decide(cfg, {"private_evidence": "not sent to router"}, QUESTION, local_only=False)
    assert result["ok"] and result["provider"] == "clef-flash"
    assert result["routing"]["coordinator"] == "jev" and result["routing"]["status"] == "selected"
    router_state = providers["jev"].calls[0][0]
    assert "private_evidence" not in json.dumps(router_state)
    assert router_state["question_specs"] == QUESTION
    assert providers["clef-flash"].calls[0][0] == {"private_evidence": "not sent to router"}
    assert len(providers["jev"].calls) == len(providers["clef-flash"].calls) == 1
    assert not providers["tev1"].calls
    assert not result["action_authority"] and not result["real_world_verified"]


@pytest.mark.parametrize("picked,p,fit", [("no_match", 0.95, 0.95), ("clef-flash", 0.5, 0.95), ("clef-flash", 0.95, 0.4)])
def test_uncertain_or_unfit_route_returns_to_main_without_inference(monkeypatch, picked, p, fit):
    cfg, providers = fixture(monkeypatch)
    providers["jev"].picked, providers["jev"].probability, providers["jev"].fit = picked, p, fit
    result = d.decide(cfg, "current evidence", QUESTION, local_only=False)
    assert not result["ok"] and result["error"] == "route_requires_main_model"
    assert result["routing"]["answers"]["fit"]["noul"] == fit
    assert not providers["clef-flash"].calls and not providers["tev1"].calls


def test_local_only_online_never_calls_jev_or_probes_external_network(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    probes = []
    monkeypatch.setattr(c, "connectivity", lambda **kw: probes.append(kw) or {"state": "unknown"})
    result = d.decide(cfg, "local evidence", QUESTION)
    assert result["error"] == "online_coordinator_requires_remote_permission"
    assert result["routing"]["failure_kind"] == "request_policy"
    assert result["routing"]["provider_health"] == "not_tested"
    assert result["routing"]["recovery"]["automatic_retry"] is False
    assert probes == [{"allow_probe": False}]
    assert all(not p.calls for p in providers.values())


def test_supplied_route_is_completed_but_only_probability_gate_rejects(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    providers["clef"] = Provider("clef")
    cfg.decision_router_candidates = ["jev", "clef-flash", "clef", "tev1"]
    original = providers["jev"].infer

    def supplied(state, questions, timeout):
        reply = original(state, questions, timeout)
        reply["answers"]["route"] = {"type": "choice", "choice": "jev", "confidence": 0.62,
            "probabilities": {"jev": 0.7, "clef-flash": 0.21, "clef": 0.03, "tev1": 0.0, "no_match": 0.06}}
        reply["answers"]["fit"] = {"type": "noul", "noul": 0.84}
        return reply

    providers["jev"].infer = supplied
    args = {"state": "public fixture", "questions": QUESTION, "task": "code-review"}
    result = d.decide(cfg, **args, local_only=False)
    assert result["ok"] is False and result["error"] == "route_requires_main_model"
    route = result["routing"]
    assert route["coordinator_completed"] is True and route["provider_health"] == "inference_completed"
    assert route["rejections"] == [{"check": "route_probability", "observed": 0.7, "required": 0.8}]
    assert route["route_margin"] == pytest.approx(0.49)
    assert route["fit_probability"] == 0.84
    assert c.validate_deferred_result(result, args, cfg)["rejections"] == route["rejections"]
    assert len(providers["jev"].calls) == 1 and all(not p.calls for k, p in providers.items() if k != "jev")
    with pytest.raises(d.DecisionError, match="invalid_deferred_routing"):
        c.validate_deferred_result(result, {**args, "state": "changed"}, cfg)


@pytest.mark.parametrize("local_only", [True, False])
def test_confirmed_offline_uses_local_coordinator_and_local_answer_only(monkeypatch, local_only):
    cfg, providers = fixture(monkeypatch, decision_fallbacks={"clef-flash": ["jev"]})
    monkeypatch.setattr(c, "connectivity", lambda **kw: {"state": "offline", "basis": "os_links_down"})
    result = d.decide(cfg, "local evidence", QUESTION, local_only=local_only)
    assert result["ok"] and result["local"] is True
    assert result["routing"]["coordinator"] == "clef-flash"
    assert result["routing"]["excluded"]["jev"] == "offline_forbids_remote_provider"
    assert not providers["jev"].calls and len(providers["clef-flash"].calls) == 2


def test_tev1_replaces_unavailable_offline_clef_coordinator(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    monkeypatch.setattr(c, "connectivity", lambda **kw: {"state": "offline"})
    providers["clef-flash"].failure = "unavailable"
    providers["tev1"].picked = "tev1"
    result = d.decide(cfg, "state", QUESTION)
    assert result["ok"] and result["routing"]["coordinator"] == "tev1"
    assert result["provider"] == "tev1" and not providers["jev"].calls


@pytest.mark.parametrize("failure", ["provider_http_401", "provider_http_429", "provider_http_503", "jev_disabled", "response_model_mismatch"])
def test_service_auth_or_contract_error_does_not_activate_offline_router(monkeypatch, failure):
    cfg, providers = fixture(monkeypatch)
    providers["jev"].failure = failure
    probes = []
    monkeypatch.setattr(c, "connectivity", lambda **kw: probes.append(kw) or {"state": "online"})
    result = d.decide(cfg, "state", QUESTION, local_only=False)
    assert not result["ok"] and result["error"] == failure
    assert probes == [{"allow_probe": False}]
    assert not providers["clef-flash"].calls


@pytest.mark.parametrize("reachability", ["online", "unknown"])
def test_transport_failure_without_confirmed_offline_never_falls_back(monkeypatch, reachability):
    cfg, providers = fixture(monkeypatch)
    providers["jev"].failure = "provider_unavailable"
    monkeypatch.setattr(c, "connectivity", lambda **kw: {"state": reachability if kw["allow_probe"] else "unknown"})
    result = d.decide(cfg, "state", QUESTION, local_only=False)
    assert result["error"] == "jev_unavailable_without_confirmed_offline"
    assert not providers["clef-flash"].calls


def test_transport_failure_then_observed_offline_restricts_catalog_and_dispatch(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    providers["jev"].failure = "provider_unavailable"
    monkeypatch.setattr(c, "connectivity", lambda **kw: {"state": "offline" if kw["allow_probe"] else "unknown"})
    result = d.decide(cfg, "state", QUESTION, local_only=False)
    assert result["ok"] and result["routing"]["coordinator"] == "clef-flash"
    assert len(providers["jev"].calls) == 1 and result["local"]


def test_unready_and_incompatible_candidates_are_removed_before_jev(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    providers["tev1"].ready = False
    result = d.decide(cfg, "state", QUESTION, local_only=False)
    assert result["ok"]
    assert "tev1" not in providers["jev"].calls[0][0]["providers"]
    assert result["routing"]["excluded"]["tev1"] == "provider_not_ready"


def test_explicit_provider_override_bypasses_router_but_not_privacy(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    result = d.decide(cfg, "state", QUESTION, provider="clef-flash")
    assert result["ok"] and result["routing"]["mode"] == "explicit_override"
    assert not providers["jev"].calls
    assert not d.decide(cfg, "state", QUESTION, provider="jev")["ok"]


def test_explicit_task_pin_constrains_router_not_global_defaults(monkeypatch):
    cfg, providers = fixture(monkeypatch, decision_routes={"review": "clef-flash"})
    result = d.decide(cfg, "state", QUESTION, task="review", local_only=False)
    assert result["ok"] and set(result["routing"]["candidates"]) == {"clef-flash"}
    assert cfg.decision_default_provider == "jev"


def test_invalid_request_never_contacts_router(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    result = d.decide(cfg, {"bad": float("nan")}, QUESTION, local_only=False)
    assert not result["ok"] and all(not p.calls for p in providers.values())


def test_malformed_router_answer_never_reaches_answer_provider(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    providers["jev"].picked = "unregistered"
    result = d.decide(cfg, "state", QUESTION, local_only=False)
    assert not result["ok"] and not providers["clef-flash"].calls


def test_coordinator_tool_exposure_preserves_declared_class_ceiling():
    def names(fs):
        return {f.__name__ for f in fs}
    assert "decision_decide" in names(select_tools_for_prompt("Review ALGO.md", tools.ALL_TOOLS, decision_coordinator=True))
    assert "decision_decide" in names(select_tools_for_prompt("Use Clef-flash for this task", tools.ALL_TOOLS))
    assert "decision_decide" not in names(select_tools_for_prompt("Allowed tool classes: filesystem", tools.ALL_TOOLS, decision_coordinator=True))
    email = names(select_tools_for_prompt("can jev check my email in the browser", tools.ALL_TOOLS, decision_coordinator=True))
    assert {"decision_decide", "jev_kernel_status", "session_command"} <= email


def test_cli_coordinate_preserves_owner_settings(monkeypatch, tmp_path):
    from algo_cli import cli_config, config
    path = tmp_path / "config.json"
    original = {"model": "owner-model", "cwd": str(tmp_path), "continuum_enabled": False,
                "unknown_owner_setting": {"keep": True}, "decision_default_provider": "clef-flash"}
    path.write_text(json.dumps(original))
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    assert cli_config.run(["decision", "coordinate", "jev", "--candidates", "jev", "clef-flash", "tev1"]) == 0
    current = json.loads(path.read_text())
    assert current["decision_default_provider"] == "jev" and current["decision_routing_mode"] == "jev"
    assert all(current[key] == value for key, value in original.items() if key != "decision_default_provider")
    before = path.read_bytes()
    assert cli_config.run(["decision", "coordinate", "jev", "--minimum-probability", "nan"]) == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("code,expected", [(errno.ENETUNREACH, "offline"), (None, "unknown"), (errno.ECONNREFUSED, "unknown")])
def test_connectivity_does_not_confuse_timeouts_or_refused_services_with_offline(monkeypatch, code, expected):
    monkeypatch.setattr(c.sys, "platform", "win32")
    def fail(*args, **kwargs):
        raise OSError(code, "synthetic")
    monkeypatch.setattr(c.socket, "create_connection", fail)
    assert c.connectivity(allow_probe=True)["state"] == expected
    assert c.connectivity(allow_probe=False)["state"] == "unknown"


def test_macos_disconnection_can_be_observed_without_any_external_probe(monkeypatch):
    monkeypatch.setattr(c.sys, "platform", "darwin")
    monkeypatch.setattr(c.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b"No network information\n"))
    monkeypatch.setattr(c.socket, "create_connection", lambda *a, **kw: pytest.fail("Must not probe externally"))
    assert c.connectivity(allow_probe=False) == {"state": "offline", "basis": "os_no_network_information"}


def test_router_returns_kernel_and_pattern_choices_bound_to_catalog(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    references = {"kernels": [{"id": "review", "description": "Review parser", "status": "active", "evidence": "Declared only"}],
                  "algorithms": [{"id": "A1", "description": "Parser invariants", "status": "proposed", "evidence": "Research only"}],
                  "catalog_sha256": "current", "issues": [], "scope": "fixture"}
    monkeypatch.setattr(c.decision_catalog, "candidates", lambda *a: references)
    result = d.decide(cfg, "state", QUESTION, local_only=False)
    assert result["ok"]
    picks = result["routing"]["recommendations"]
    assert picks["kernels"]["selected"]["id"] == "review"
    assert picks["algorithms"]["selected"]["status"] == "proposed"
    assert not picks["algorithms"]["execution_authority"]
    assert providers["jev"].calls[0][0]["reference_candidates"] == references


def test_catalog_change_during_routing_prevents_consuming_stale_selection(monkeypatch):
    cfg, providers = fixture(monkeypatch)
    revisions = iter(["first", "changed"])
    monkeypatch.setattr(c.decision_catalog, "candidates", lambda *a: {
        "kernels": [], "algorithms": [], "catalog_sha256": next(revisions), "issues": [], "scope": "fixture"})
    result = d.decide(cfg, "state", QUESTION, local_only=False)
    assert result["error"] == "reference_catalog_changed_during_routing"
    assert not providers["clef-flash"].calls
