from __future__ import annotations

import copy
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import time

import pytest

from algo_cli import decision_layer as d, tools
from algo_cli.config import Config
from algo_cli.james_dispatch import dispatch_action
from algo_cli.marcus_authority import Capability, ConfirmationMode, TargetScope
from algo_cli.nathan_runtime import authority_session_for, preflight_runtime_tool
from algo_cli.tool_context import select_tools_for_prompt


QUESTIONS = {
    "pick": {"type": "choice", "instructions": "Which evidence is relevant?",
             "criteria": {"current": "Current observation", "unknown": "No supporting observation"}},
    "present": {"type": "noul", "instructions": "Is current evidence present?",
                "criteria": {"true": "Current evidence exists", "false": "No current evidence"}},
    "quality": {"type": "score", "instructions": "How complete is the observation?",
                "criteria": ["Missing", "Observed", "Observed and checked"]},
}
ANSWERS = {
    "pick": {"type": "choice", "choice": "current", "probabilities": {"current": 0.8, "unknown": 0.2}, "confidence": 0.4},
    "present": {"type": "noul", "noul": 0.75},
    "quality": {"type": "score", "score": 1.5, "legend": {"0": "Missing", "1": "Observed", "2": "Observed and checked"},
                "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6}, "confidence": 0.2},
}


class FakeProvider:
    capabilities = d.Capabilities()

    def __init__(self, name="clef", *, local=True, failure=None, answer=None):
        self.name, self.model, self.local = name, name + ":fixture", local
        self.failure, self.answer = failure, answer
        self.calls = []

    def infer(self, state, questions, timeout):
        self.calls.append((state, questions, timeout))
        if self.failure:
            raise d.DecisionError(self.failure)
        return {"model": self.model, "answers": copy.deepcopy(self.answer or ANSWERS), "usage": {"input_tokens": 12}}

    def status(self):
        return {"ready": True, "inference": "not_probed"}


def use_provider(monkeypatch, provider):
    monkeypatch.setattr(d, "provider_for", lambda _name, _cfg: provider)


def test_typed_result_preserves_probabilities_without_implicit_boolean_or_integer(monkeypatch):
    p = FakeProvider()
    use_provider(monkeypatch, p)
    result = d.decide(Config(), {"evidence": "synthetic"}, QUESTIONS, provider="clef")
    assert result["ok"] and result["answers"] == ANSWERS
    assert result["answers"]["quality"]["score"] == 1.5
    assert type(result["answers"]["present"]["noul"]) is float
    assert result["derived"] == {} and result["action_authority"] is False
    assert result["real_world_verified"] is False and len(p.calls) == 1


def test_explicit_threshold_policy_retains_raw_result_and_gray_band(monkeypatch):
    use_provider(monkeypatch, FakeProvider())
    policy = {"noul_thresholds": {"false_at_or_below": 0.2, "true_at_or_above": 0.9},
              "minimum_choice_probability": 0.85}
    result = d.decide(Config(), "state", QUESTIONS, provider="clef", policy=policy)
    assert result["ok"] and result["derived"] == {"present": None, "pick": None}
    assert result["answers"] == ANSWERS and result["applied_policy"] == policy


@pytest.mark.parametrize("probabilities", [None, "absent"])
def test_choice_policy_abstains_when_distribution_is_unavailable(monkeypatch, probabilities):
    answers = copy.deepcopy(ANSWERS)
    if probabilities is None:
        answers["pick"]["probabilities"] = None
    else:
        answers["pick"].pop("probabilities")
    provider = FakeProvider("fixture", answer=answers)
    provider.capabilities = d.Capabilities(distributions=False)
    monkeypatch.setattr(d, "_PROVIDER_FACTORIES", {})
    d.register_provider("fixture", lambda cfg: provider)
    result = d.decide(Config(), "state", QUESTIONS, provider="fixture",
                      policy={"minimum_choice_probability": 0.5})
    assert result["ok"] and result["answers"] == answers
    assert result["derived"] == {"pick": None}
    assert result["action_authority"] is False and result["real_world_verified"] is False


@pytest.mark.parametrize("details", [None, [], "gguf", 42])
def test_malformed_ollama_details_fail_structurally_in_status_and_decide(monkeypatch, details):
    calls = []

    def http(base, route, payload, timeout):
        calls.append(route)
        return {
            "/api/version": {"version": "0.35.1"},
            "/api/tags": {"models": [{"name": "clef:latest"}]},
            "/api/show": {"capabilities": ["decision"], "details": details},
        }[route]

    monkeypatch.setattr(d, "_http_json", http)
    cfg = Config()
    status = json.loads(d.decision_status("clef", cfg))["providers"]["clef"]
    assert status == {"ok": False, "error": "incompatible_decision_model"}
    result = d.decide(cfg, "state", QUESTIONS, provider="clef")
    assert not result["ok"] and result["error"] == "incompatible_decision_model"
    assert result["answers"] == {} and "/v1/systemone" not in calls


@pytest.mark.parametrize("route,field,value,error", [
    ("/api/tags", "models", None, "invalid_ollama_discovery"),
    ("/api/tags", "models", [None], "invalid_ollama_discovery"),
    ("/api/show", "capabilities", None, "incompatible_decision_model"),
    ("/api/show", "capabilities", "decision", "incompatible_decision_model"),
    ("/api/ps", "models", None, "invalid_ollama_discovery"),
    ("/api/ps", "models", [None], "invalid_ollama_discovery"),
])
def test_malformed_nested_ollama_discovery_returns_structured_error(monkeypatch, route, field, value, error):
    replies = {
        "/api/version": {"version": "0.35.1"},
        "/api/tags": {"models": [{"name": "clef:latest"}]},
        "/api/show": {"capabilities": ["decision"], "details": {"format": "gguf"}},
        "/api/ps": {"models": []},
    }
    replies[route][field] = value
    monkeypatch.setattr(d, "_http_json", lambda base, route, payload, timeout: replies[route])
    result = d.decide(Config(), "state", QUESTIONS, provider="clef")
    assert not result["ok"] and result["error"] == error and not result["answers"]


@pytest.mark.parametrize("runtime", [None, [], "revision", 42, "absent"])
def test_malformed_decider_runtime_returns_structured_failure(monkeypatch, tmp_path, runtime):
    provider = d.CompanionProvider("decider", {})
    monkeypatch.setattr(provider, "_path", lambda: tmp_path / "decider_local.py")
    reply = {"ok": True, "response": {"model": provider.model, "answers": ANSWERS}}
    if runtime != "absent":
        reply["runtime"] = runtime
    monkeypatch.setattr(d, "_cli_json", lambda *_: copy.deepcopy(reply))
    use_provider(monkeypatch, provider)
    result = d.decide(Config(), "state", QUESTIONS, provider="decider")
    assert not result["ok"] and result["error"] == "invalid_companion_response"
    assert result["answers"] == {} and result["action_authority"] is False


@pytest.mark.parametrize("mutate,code", [
    (lambda a: a.pop("present"), "answer_ids_mismatch"),
    (lambda a: a["present"].update(noul=True), "invalid_answer_number"),
    (lambda a: a["present"].update(noul=float("nan")), "invalid_answer_number"),
    (lambda a: a["present"].update(noul=1.1), "invalid_answer_number"),
    (lambda a: a["present"].update(confidence=1), "invalid_noul_answer"),
    (lambda a: a["pick"].update(choice="other"), "invalid_choice"),
    (lambda a: a["pick"].update(choice="unknown"), "choice_distribution_mismatch"),
    (lambda a: a["pick"]["probabilities"].update(extra=0), "probability_labels_mismatch"),
    (lambda a: a["pick"]["probabilities"].update(current=0.4), "probabilities_not_normalized"),
    (lambda a: a["pick"].pop("confidence"), "invalid_answer_number"),
    (lambda a: a["quality"].update(score=0), "score_distribution_mismatch"),
    (lambda a: a["quality"].update(legend={}), "score_legend_mismatch"),
    (lambda a: a["quality"].update(type="choice"), "answer_type_mismatch"),
    (lambda a: a["pick"].update(command="execute arbitrary command"), "unsupported_answer_field"),
])
def test_malformed_answers_cannot_be_consumed(monkeypatch, mutate, code):
    answers = copy.deepcopy(ANSWERS)
    mutate(answers)
    use_provider(monkeypatch, FakeProvider(answer=answers))
    result = d.decide(Config(), "state", QUESTIONS, provider="clef")
    assert not result["ok"] and result["answers"] == {} and result["error"] == code


@pytest.mark.parametrize("questions,state", [
    ({}, "state"), ([], "state"), (QUESTIONS, ""), (QUESTIONS, " " * 3),
    ({"q": {"type": "choice", "instructions": "Pick", "criteria": {"one": "Only one"}}}, "state"),
    ({"q": {"type": "noul", "instructions": "Check", "criteria": {"yes": "Yes"}}}, "state"),
    ({"q": {"type": "score", "instructions": "Check", "criteria": ["Only one"]}}, "state"),
    ({"q": {"type": "noul", "instructions": "Check", "images": ["private"]}}, "state"),
    (QUESTIONS, {"bad": float("inf")}), (QUESTIONS, "x" * 16_001),
], ids=["empty", "not-map", "empty-state", "blank-state", "single-choice", "noul-label", "single-score", "images", "nonfinite", "oversize"])
def test_invalid_request_never_calls_provider(monkeypatch, questions, state):
    p = FakeProvider()
    use_provider(monkeypatch, p)
    assert not d.decide(Config(), state, questions, provider="clef")["ok"]
    assert p.calls == []


def test_local_only_cannot_send_primary_or_fallback_to_remote(monkeypatch):
    local = FakeProvider(failure="timeout")
    remote = FakeProvider("jev", local=False)
    monkeypatch.setattr(d, "provider_for", lambda name, cfg: {"clef": local, "jev": remote}[name])
    cfg = Config(decision_fallbacks={"clef": ["jev"]})
    result = d.decide(cfg, "state", QUESTIONS, provider="clef")
    assert not result["ok"] and len(local.calls) == 1 and not remote.calls
    assert result["error"] == "local_only_forbids_remote_provider"
    assert not d.decide(cfg, "state", QUESTIONS, provider="jev")["ok"] and not remote.calls


def test_explicit_task_route_and_fallback_are_visible_and_do_not_change_default(monkeypatch):
    a = FakeProvider("clef", failure="unavailable")
    b = FakeProvider("decider")
    monkeypatch.setattr(d, "provider_for", lambda name, cfg: {"clef": a, "decider": b}[name])
    cfg = Config(decision_routing_mode="direct", decision_routes={"triage": "clef"}, decision_fallbacks={"clef": ["decider"]})
    result = d.decide(cfg, "state", QUESTIONS, task="triage")
    assert result["ok"] and result["provider"] == "decider" and len(result["attempts"]) == 2
    assert result["attempts"][0]["error"] == "unavailable"
    assert cfg.decision_default_provider == "jev" and cfg.model == Config().model


def test_invalid_policy_cannot_spend_inference(monkeypatch):
    p = FakeProvider()
    use_provider(monkeypatch, p)
    result = d.decide(Config(), "state", QUESTIONS, provider="clef",
                      policy={"noul_thresholds": {"false_at_or_below": 0.9, "true_at_or_above": 0.1}})
    assert not result["ok"] and not p.calls


def test_decoder_rejects_duplicate_keys_and_nonfinite_fields():
    for payload in (b'{"answers":{},"answers":{}}', b'{"p":NaN}', b'[]'):
        with pytest.raises(d.DecisionError):
            d.decode(payload)


def test_native_schema_survives_actual_ollama_sdk_and_openai_adapter():
    import ollama
    from algo_cli import chatgpt_client
    from algo_cli.tool_schema import provider_tools

    prepared = provider_tools([d.decision_decide])
    assert prepared[0]["function"]["parameters"]["properties"]["state"]["type"] == ["string", "object", "array"]
    wire = ollama.Tool.model_validate(prepared[0]).model_dump(exclude_none=True)
    assert wire["function"]["parameters"]["required"] == ["state", "questions"]
    assert wire["function"]["parameters"]["properties"]["state"]["type"] == ["string", "object", "array"]
    assert chatgpt_client._build_openai_tools(prepared)[0]["function"]["parameters"]["properties"]["state"]["type"] == ["string", "object", "array"]


def test_decision_observation_cannot_complete_a_workspace_mutation(monkeypatch, tmp_path):
    from algo_cli import execution_guardrails as g, nathan_runtime as runtime
    use_provider(monkeypatch, FakeProvider())
    cfg = Config(cwd=str(tmp_path))
    cfg._nathan_approval_mode = "never"
    scope = g.begin_execution_scope(tmp_path)
    try:
        g.record_mutation("changed.py", success=True, operation="write_file")
        args = {"state": "state", "questions": QUESTIONS, "provider": "clef", "local_only": True}
        observed = dispatch_action("decision_decide", args, cfg, render=False)
        assert observed.outcome.invoked and json.loads(observed.result)["ok"]
        runtime.record_tool_attempt(cfg, name="decision_decide", args=args, result=observed.result, status="worked")
        assert not g.completion_decision().allowed
    finally:
        g.end_execution_scope(scope)


def test_cli_updates_only_selected_decision_fields(monkeypatch, tmp_path):
    from algo_cli import cli_config, config as storage
    original = {"model": "owner-model", "model_provider": "ollama", "cwd": str(tmp_path),
                "continuum_enabled": False, "unrecognized_owner_setting": {"retain": True},
                "decision_provider_settings": {"clef": {"model": "clef:latest"}}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(original))
    monkeypatch.setattr(storage, "CONFIG_FILE", path)
    assert cli_config.run(["decision", "configure", "clef-flash", "--model", "clef-flash:latest"]) == 0
    current = json.loads(path.read_text())
    assert all(current[key] == value for key, value in original.items() if key != "decision_provider_settings")
    assert current["decision_provider_settings"]["clef"] == original["decision_provider_settings"]["clef"]
    assert current.get("decision_default_provider", "jev") == "jev"
    assert cli_config.run(["decision", "select", "clef-flash"]) == 0
    assert json.loads(path.read_text())["decision_default_provider"] == "clef-flash"


def test_result_is_bound_to_question_order_and_state(monkeypatch):
    use_provider(monkeypatch, FakeProvider())
    first = d.decide(Config(), "one", QUESTIONS, provider="clef")
    second = d.decide(Config(), "two", QUESTIONS, provider="clef")
    third = d.decide(Config(), "one", dict(reversed(list(QUESTIONS.items()))), provider="clef")
    assert len({x["request_sha256"] for x in (first, second, third)}) == 3


def test_registered_adapter_uses_same_validator_and_cannot_replace_builtin(monkeypatch):
    monkeypatch.setattr(d, "_PROVIDER_FACTORIES", {})
    d.register_provider("fixture", lambda cfg: FakeProvider("fixture"))
    assert d.decide(Config(), "state", QUESTIONS, provider="fixture")["ok"]
    with pytest.raises(d.DecisionError):
        d.register_provider("jev", lambda cfg: FakeProvider())


def test_decider_specific_uncertainty_is_validated_and_preserved(monkeypatch, tmp_path):
    provider = d.CompanionProvider("decider", {})
    monkeypatch.setattr(provider, "_path", lambda: tmp_path / "decider_local.py")
    native = copy.deepcopy(ANSWERS)
    native["pick"].update(certainty=0.4, x_p_max=0.8)
    native["quality"].update(certainty=0.2, x_p_max=0.6, level_fit={"0": 0.1, "1": 0.3, "2": 0.6}, fit_mass=1.0)
    request = {"state": "state", "questions": QUESTIONS}
    reply = {"ok": True, "input_sha256": d.digest(request).removeprefix("sha256:"),
             "runtime": {"model_revision": "eb5fbdfc9448473ec25e399882912863afbdb70e"},
             "response": {"model": "decider-4b-v2.1", "answers": native}}
    monkeypatch.setattr(d, "_cli_json", lambda *_: copy.deepcopy(reply))
    result = provider.infer("state", QUESTIONS, 20)
    assert result["answers"] == ANSWERS
    assert result["provider_metadata"]["native_uncertainty"]["quality"]["fit_mass"] == 1.0
    reply["response"]["answers"]["quality"]["level_fit"]["0"] = 0.8
    with pytest.raises(d.DecisionError, match="invalid_decider_fit_mass"):
        provider.infer("state", QUESTIONS, 20)


@pytest.mark.parametrize("local_only,scope,confirmation", [
    (True, TargetScope.RUNTIME, ConfirmationMode.NONE),
    (False, TargetScope.PROVIDER, ConfirmationMode.SESSION_PREAPPROVAL),
])
def test_real_dispatcher_keeps_decision_and_action_authority_separate(monkeypatch, tmp_path, local_only, scope, confirmation):
    p = FakeProvider(local=local_only)
    use_provider(monkeypatch, p)
    cfg = Config(cwd=str(tmp_path))
    cfg._nathan_approval_mode = "never"
    args = {"state": "synthetic", "questions": QUESTIONS, "provider": "clef", "local_only": local_only}
    preflight = preflight_runtime_tool("decision_decide", args, cfg)
    assert preflight.policy.action.target_scope is scope
    assert preflight.policy.action.confirmation_mode is confirmation
    if local_only:
        assert preflight.policy.action.capability_mask == Capability.READ.value | Capability.MODEL.value
        result = dispatch_action("decision_decide", args, cfg, render=False)
        assert result.outcome.invoked and json.loads(result.result)["ok"]
        assert p.calls and json.loads(result.result)["action_authority"] is False
        assert not preflight_runtime_tool("write_file", {"path": "file", "content": "x"}, cfg).policy.allowed
        denied = dispatch_action("decision_decide", args, cfg, policy_ceiling_code="agent_tool_not_allowed", render=False)
        assert not denied.outcome.invoked
    else:
        assert not authority_session_for(cfg).baseline_allows(preflight.policy.action)
        result = dispatch_action("decision_decide", args, cfg, render=False)
        assert not result.outcome.invoked and not p.calls


def test_decision_tools_are_discoverable_without_expanding_declared_ceiling():
    picked = select_tools_for_prompt("Use decision_decide with Clef to classify relevant evidence", tools.ALL_TOOLS)
    assert "decision_decide" in {x.__name__ for x in picked}
    declared = select_tools_for_prompt("Allowed tool classes: decision", tools.ALL_TOOLS)
    assert {x.__name__ for x in declared} == {"decision_status", "decision_decide"}
    assert "decision_decide" not in {x.__name__ for x in select_tools_for_prompt("Allowed tool classes: filesystem", tools.ALL_TOOLS)}
    focused = json.loads(tools.available_actions("decision"))
    assert "decision_decide" in str(focused)


def test_ollama_native_endpoint_and_cold_state_are_verified_with_real_http(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value):
            body = d.encoded(value)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            requests.append(self.path)
            values = {"/api/version": {"version": "0.35.1"},
                      "/api/tags": {"models": [{"name": "clef:latest", "digest": "a" * 64}]},
                      "/api/ps": {"models": []}}
            self.reply(values[self.path])

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, payload))
            if self.path == "/api/show":
                self.reply({"capabilities": ["decision"], "details": {"format": "gguf"}})
            else:
                assert self.path == "/v1/systemone"
                self.reply({"model": payload["model"], "answers": ANSWERS, "usage": {"input_tokens": 10, "output_tokens": 0}})

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        cfg = Config(host=f"http://127.0.0.1:{server.server_port}")
        result = d.decide(cfg, {"evidence": "current"}, QUESTIONS, provider="clef")
        assert result["ok"] and result["provider_metadata"]["loaded_before"] is False
        assert result["provider_metadata"]["loaded_after"] is False
        native = [p for p in requests if isinstance(p, tuple) and p[0] == "/v1/systemone"]
        assert len(native) == 1 and native[0][1]["questions"] == QUESTIONS
        assert native[0][1]["state"] == {"evidence": "current"}
        assert not any("chat" in str(p) for p in requests)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(1)


def test_absolute_http_deadline_interrupts_http10_drip_body():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(20):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        started = time.monotonic()
        with pytest.raises(d.DecisionError, match="timeout"):
            d._http_json(f"http://127.0.0.1:{server.server_port}", "/", None, 0.1)
        assert time.monotonic() - started < 0.4
    finally:
        server.shutdown()
        server.server_close()
        worker.join(1)
