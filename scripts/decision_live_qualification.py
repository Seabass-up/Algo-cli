#!/usr/bin/env python3
"""Live synthetic decisions and an actual agent-loop proof; no default-provider changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def corpus() -> tuple[dict, dict, dict]:
    cases = {
        "callback": {"evidence": "The callback listener accepts a socket that sends partial headers and never finishes. OAuth state and PKCE are valid.",
                     "question": "Which investigation best addresses the observed callback stall?",
                     "criteria": {"deadline": "Check accepted-connection deadlines and HTTP parsing.", "credentials": "Replace valid credentials without inspecting the stalled connection.", "unknown": "No relevant evidence."}, "gold": "deadline"},
        "uncertain_write": {"evidence": "A file-write request timed out after dispatch. It is unknown whether the file changed.",
                            "question": "Which next step is appropriate for the uncertain write?",
                            "criteria": {"reconcile": "Observe the target and reconcile before another mutation.", "retry": "Blindly resend the same mutation.", "success": "Report success without observing the file."}, "gold": "reconcile"},
        "verification": {"evidence": "A decision model says the implementation is correct, but no test, lint or outcome check has run after the last edit.",
                         "question": "What should the coding agent do next?",
                         "criteria": {"run_check": "Run the relevant executable verification.", "complete": "Declare verified completion from the model vote.", "rewrite": "Rewrite everything without inspecting it."}, "gold": "run_check"},
        "runtime": {"evidence": "The inspected workspace differs from the launcher interpreter's imported source. No tool exposure check has been run in that process.",
                    "question": "Which evidence should be obtained to diagnose missing runtime tools?",
                    "criteria": {"runtime_source": "Inspect the actual interpreter, imported source and live tool registry.", "workspace_only": "Infer runtime capabilities only from workspace Git status.", "remove_grants": "Remove authorization checks instead of diagnosing the source."}, "gold": "runtime_source"},
        "missing_evidence": {"evidence": "A record says CI was submitted. No completed workflow or test outcome is present.",
                             "question": "Which status is supported by this evidence?",
                             "criteria": {"unverified": "Submitted; result not verified.", "passed": "All required tests passed.", "failed": "All required tests failed."}, "gold": "unverified"},
        "priority": {"evidence": "Task A has a reproduced user-data exposure. Task B is a cosmetic spacing preference. Both require comparable work.",
                     "question": "Which repair should be prioritized from the stated evidence?",
                     "criteria": {"exposure": "The reproduced user-data exposure.", "cosmetic": "The cosmetic preference.", "unknown": "Neither has supporting evidence."}, "gold": "exposure"},
    }
    state = {"source_revision": "algo-native-decision-fixtures-v1",
             "cases": {key: {"evidence": case["evidence"]} for key, case in cases.items()}}
    questions = {key: {"type": "choice", "instructions": f"Using only cases.{key}.evidence: {case['question']}",
                       "criteria": case["criteria"]} for key, case in cases.items()}
    questions["checked"] = {"type": "noul", "instructions": "Does cases.verification.evidence establish that an executable check actually ran?",
                            "criteria": {"true": "A check actually ran.", "false": "No check has run."}}
    questions["completeness"] = {"type": "score", "instructions": "How complete is cases.missing_evidence.evidence for a verified CI pass?",
                                 "criteria": ["No completed result observed.", "Completed result observed but not bound to this source.", "Passing result observed and bound to this source."]}
    return state, questions, {key: case["gold"] for key, case in cases.items()}


def compare(args) -> dict:
    from algo_cli.config import Config
    from algo_cli import decision_layer as d

    cfg = Config.load()
    cfg.decision_keep_alive = "0s"
    cfg.decision_timeout_seconds = 180
    state, questions, expected = corpus()
    results = []
    for name in args.providers:
        provider = d.provider_for(name, cfg)
        initial = provider.status()
        started = time.perf_counter()
        runs = [d.decide(cfg, state, questions, provider=name, local_only=name != "jev")
                for _ in range(args.repetitions)]
        judged = []
        for run in runs:
            answers = run.get("answers", {})
            matches = {key: answers.get(key, {}).get("choice") == gold for key, gold in expected.items()}
            matches["checked"] = answers.get("checked", {}).get("noul", 1) <= 0.25
            matches["completeness"] = answers.get("completeness", {}).get("score", 2) <= 0.5
            judged.append(matches)
        results.append({"provider": name, "initial_readiness": initial, "runs": runs,
                        "checks": judged, "elapsed_ms": round((time.perf_counter()-started)*1000, 3)})
        if name in {"clm", "decider"} and not initial.get("ready"):
            # Restore services this qualification started; do not stop a preexisting loaded helper.
            d._cli_json([str(provider._path()), "stop"], {}, 20)
    return {"schema": "algo-decision-live-qualification.v1", "runtime_source": str(ROOT),
            "python": sys.executable, "request": {"state": state, "questions": questions},
            "expected": expected, "results": results,
            "limitations": "Small synthetic development corpus, not held-out reliability or calibrated accuracy. No defaults or task routes changed."}


def agent_demo(args) -> dict:
    owner_path = Path(os.environ.get("ALGO_CLI_CONFIG_DIR", str(Path.home() / ".algo_cli"))) / "config.json"
    owner = json.loads(owner_path.read_text())
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    # Isolate app state before importing runtime modules; use the existing Ollama server.
    os.environ["ALGO_CLI_CONFIG_DIR"] = str(workspace / "app-state")
    from algo_cli.config import Config
    from algo_cli import main
    from ollama import Client

    cfg = Config(cwd=str(workspace), model=args.main_model, model_provider=getattr(args, "main_provider", "ollama"),
                 session_mode="explore", safe_mode=True, num_ctx=16_384,
                 show_thinking=False, model_adaptive=False, max_tool_iterations=12)
    cfg.decision_keep_alive = "0s"
    cfg.decision_timeout_seconds = 180
    cfg.decision_checkpoints_enabled = args.automatic_review_demo
    if args.coordinated or args.automatic_review_demo:
        cfg.decision_routing_mode = "jev"
        cfg.jev_kernel_enabled = owner.get("jev_kernel_enabled", False)
        cfg.jev_kernel_cli = owner.get("jev_kernel_cli", "")
        cfg.decision_provider_settings = owner.get("decision_provider_settings", {})
        cfg.decision_router_candidates = ["jev", "clef-flash", "tev1"]
        cfg.decision_routes = {"code-review": "jev"} if args.automatic_review_demo else {"fixture-verification": "clef-flash"}
        if getattr(args, "unrestricted_routing", False):
            cfg.decision_routes = {}
            cfg.decision_router_candidates = owner.get("decision_router_candidates", cfg.decision_router_candidates)
            for field in ("decision_router_min_probability", "decision_router_min_margin", "decision_router_min_fit"):
                setattr(cfg, field, owner.get(field, getattr(cfg, field)))
        cfg.decision_review_checkpoint_interval = 1
        if args.approve_synthetic_decisions:
            from algo_cli.nathan_runtime import authority_session_for
            from algo_cli.samuel_policy_engine import resolve_action
            cfg._nathan_approval_mode = "never"
            action = resolve_action("decision_decide", {"local_only": False}, cwd=cfg.cwd)
            authority_session_for(cfg).issue(action, source="user-approved-synthetic-model-calls",
                                            now=time.time(), maximum_action_count=8, ttl_seconds=180)
        if getattr(args, "replay_uncertain_router", False):
            from algo_cli import decision_layer
            original_provider_for = decision_layer.provider_for
            cfg.decision_routes = {}
            cfg.decision_router_candidates = ["jev", "clef-flash", "clef", "tev1"]

            class ReplayRouter:
                def __init__(self, provider):
                    self.provider = provider

                def __getattr__(self, name):
                    return getattr(self.provider, name)

                def infer(self, state, questions, timeout):
                    if "route" not in questions:
                        raise AssertionError("Deferred replay must not dispatch an answer provider")
                    distribution = {"jev": 0.7, "clef-flash": 0.21, "clef": 0.03, "tev1": 0.0, "no_match": 0.06}
                    answers = {"route": {"type": "choice", "choice": "jev", "confidence": 0.62,
                                          "probabilities": distribution},
                               "fit": {"type": "noul", "noul": 0.84}}
                    for key in questions:
                        if key in answers:
                            continue
                        options = questions[key]["criteria"]
                        picked = "none" if "none" in options else next(iter(options))
                        answers[key] = {"type": "choice", "choice": picked, "confidence": 1.0,
                                        "probabilities": {option: float(option == picked) for option in options}}
                    return {"model": self.model, "answers": answers,
                            "provider_metadata": {"replay_fixture": True, "not_live_inference": True}}

            def replay_provider_for(name, config):
                provider = original_provider_for(name, config)
                return ReplayRouter(provider) if name == "jev" else provider

            decision_layer.provider_for = replay_provider_for
    fixture = workspace / "fixture.txt"
    fixture.write_text("READY: owner authorized the synthetic receipt in this workspace.\n", encoding="utf-8")
    (workspace / "test_receipt.py").write_text(
        "from pathlib import Path\n"
        "def test_receipt():\n"
        "    assert Path('result.txt').read_text() == 'DECISION_NATIVE_VERIFIED\\n'\n", encoding="utf-8")
    selection = (
        "Call decision_decide without provider (use the default coordinator), task='fixture-verification', "
        "local_only=false and relevant evidence: " if args.coordinated else
        f"Call decision_decide with provider='{args.demo_provider}', local_only=true and relevant evidence: "
    )
    if getattr(args, "explicit_jev", False):
        selection = "Call decision_decide with provider='jev', local_only=false, task='fixture-verification' and relevant evidence: "
    prompt = (
        "Allowed tool classes: decision, filesystem\n"
        "This is an owner-authorized read-only, reversible test in this temporary workspace. "
        "The goal is to verify that fixture.txt contains the expected READY text without modifying it. "
        "First identify the bounded question about which next step advances that verification. "
        "Formulate clear instructions and explicit option descriptions yourself. Use a choice question "
        "with criteria as an OBJECT mapping option IDs to description strings, not prose or a list. "
        + selection +
        "the fixture exists, its contents have not yet been read, and only reading/checking was authorized. "
        "Wait for and use the validated answer. If it selects reading the fixture, call read_file on fixture.txt "
        "and verify the observed text against this expectation: READY: owner authorized the synthetic receipt in this workspace. "
        "Describe the actual decision and verified read outcome. Do not create, edit or send files. "
        "Do not treat a decision vote as an observed outcome."
    )
    if args.automatic_review_demo:
        fixture = workspace / "fixture.py"
        fixture.write_text("def add_one(value):\n    return value + 2\n", encoding="utf-8")
        prompt = (
            "Deep scan fixture.py for logic issues. The intended behavior is add_one(2) == 3. "
            "Read the actual source and report what is supported by it and what remains untested. "
            "This is a read-only review: do not modify files, run shell commands, or access other files."
        )
        if getattr(args, "policy_mention_demo", False):
            prompt += (
                " A previous diagnostic report discussed local-only routing restrictions and "
                "an online_coordinator_requires_remote_permission error. Those are historical "
                "request settings to investigate, not a new locality instruction for this review."
            )
        if getattr(args, "reported_review_typo_demo", False):
            prompt = (
                "Do a revei of your hanress code and report back issues you find. "
                "Scope: fixture.py only. Intended behavior: add_one(2) == 3. "
                "Source reads only; no mutations, shell commands or other files."
            )
    request_receipts = []
    class RecordingClient:
        def __init__(self):
            if cfg.model_provider == "chatgpt":
                from algo_cli import chatgpt_auth, chatgpt_client
                # Reuse the named existing auth reference, never copy credentials
                # into the isolated fixture or change the owner's app settings.
                chatgpt_auth.AUTH_FILE = owner_path.parent / "chatgpt_auth.json"
                chatgpt_auth.CODEX_AUTH_HOME = owner_path.parent / "codex-chatgpt"
                self.client = chatgpt_client.ChatGptClient()
            else:
                self.client = Client(host=cfg.host, timeout=180)
        def __getattr__(self, name):
            return getattr(self.client, name)
        def chat(self, **kwargs):
            checkpoint_packet = None
            marker = "## Runtime-owned decision checkpoint\n"
            for message in kwargs.get("messages", []):
                content = str(message.get("content", ""))
                if marker in content:
                    checkpoint_packet, _ = json.JSONDecoder().raw_decode(content.split(marker, 1)[1].lstrip())
                    break
            request_receipts.append({"decision_checkpoints": list(cfg.context_state.get("decision_checkpoints", [])),
                                     "checkpoint_in_request": checkpoint_packet is not None,
                                     "checkpoint_packet": checkpoint_packet})
            return self.client.chat(**kwargs)
    started = time.perf_counter()
    injected = {"cut": False, "model_requests": 0, "continuation_requests": 0}
    failed_provider_calls = []
    if getattr(args, "provider_failure", False):
        from algo_cli import decision_layer
        def unavailable(*a, **kw):
            failed_provider_calls.append("injected_provider_timeout")
            raise decision_layer.DecisionError("provider_timeout")
        prompt += " If the decision provider is unavailable, do not retry or change provider. Continue the authorized read-only inspection using your own judgment; report the unavailable decision separately without claiming a validated decision."
        with patch.object(decision_layer.JevProvider, "infer", unavailable):
            main.agent_loop(RecordingClient(), cfg, prompt)
    elif getattr(args, "interrupt_answer", False):
        if cfg.model_provider != "chatgpt":
            raise ValueError("Stream fault qualification requires the ChatGPT provider")
        from algo_cli import chatgpt_client
        original_post = chatgpt_client._post_codex_responses
        class CutAfterTextFrame:
            def __init__(self, response):
                self.response = response
            def __getattr__(self, name):
                return getattr(self.response, name)
            def __iter__(self):
                cut_after_separator = False
                for raw in self.response:
                    yield raw
                    if cut_after_separator and not raw.strip():
                        injected["cut"] = True
                        return
                    if raw.startswith(b"data:"):
                        try:
                            event = json.loads(raw[5:].strip())
                        except ValueError:
                            continue
                        cut_after_separator |= event.get("type") == "response.output_text.delta" and bool(event.get("delta"))
            def close(self):
                self.response.close()
        def faulted_post(payload, **kwargs):
            injected["model_requests"] += 1
            response = original_post(payload, **kwargs)
            if injected["cut"] and not payload.get("tools"):
                injected["continuation_requests"] += 1
            has_result = any(item.get("type") == "function_call_output" for item in payload.get("input", []))
            return CutAfterTextFrame(response) if has_result and payload.get("tools") and not injected["cut"] else response
        with patch.object(chatgpt_client, "_post_codex_responses", faulted_post):
            main.agent_loop(RecordingClient(), cfg, prompt)
    else:
        main.agent_loop(RecordingClient(), cfg, prompt)
    expected = "def add_one(value):\n    return value + 2\n" if args.automatic_review_demo else "READY: owner authorized the synthetic receipt in this workspace.\n"
    observed_bytes = fixture.read_bytes()
    trace = []
    for message in cfg.messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            trace.append({"role": "assistant", "tool_calls": message["tool_calls"]})
        elif message.get("role") == "tool":
            trace.append({"role": "tool", "name": message.get("name"), "content": message.get("content")})
    def validated_decision(item):
        if item.get("role") != "tool" or item.get("name") != "decision_decide":
            return False
        try:
            return json.loads(item["content"]).get("ok") is True
        except (ValueError, TypeError, AttributeError):
            return False

    decisions = [index for index, item in enumerate(trace) if validated_decision(item)]
    reads = [index for index, item in enumerate(trace)
             if item.get("role") == "tool" and item.get("name") == "read_file" and item.get("content") == expected]
    consumed = bool(decisions and reads and decisions[0] < reads[0]
                    and any(item.get("role") == "assistant" for item in trace[decisions[0]+1:reads[0]]))
    if args.automatic_review_demo:
        consumed = bool(request_receipts and reads and request_receipts[0]["checkpoint_in_request"]
                        and request_receipts[0]["decision_checkpoints"]
                        and (request_receipts[0]["decision_checkpoints"][0]["ok"]
                             or request_receipts[0]["decision_checkpoints"][0].get("coordinator_completed") is True))
    verified = consumed and observed_bytes == expected.encode()
    if getattr(args, "provider_failure", False):
        failures = []
        for index, item in enumerate(trace):
            if item.get("role") == "tool" and item.get("name") == "decision_decide":
                result = json.loads(item["content"])
                if result.get("ok") is False and result.get("error") == "provider_timeout":
                    failures.append(index)
        consumed = bool(failures and reads and failures[0] < reads[0])
        verified = consumed and not decisions and len(failed_provider_calls) == 1 and observed_bytes == expected.encode()
    if getattr(args, "interrupt_answer", False):
        verified = verified and injected["cut"] and injected["continuation_requests"] == 1 and len(reads) == 1
    return {"schema": "algo-decision-agent-live.v1", "runtime_source": str(ROOT), "python": sys.executable,
            "main_model": cfg.model, "decision_provider": "coordinated" if args.coordinated else args.demo_provider,
            "main_provider": cfg.model_provider, "stream_fault": injected,
            "provider_failure_injected": bool(getattr(args, "provider_failure", False)),
            "failed_provider_calls": len(failed_provider_calls),
            "isolated_demo_mode": cfg.session_mode, "elapsed_ms": round((time.perf_counter()-started)*1000, 3),
            "result_matches": verified, "decision_consumed_before_action": consumed,
            "result_sha256": hashlib.sha256(observed_bytes).hexdigest(),
            "expected_sha256": hashlib.sha256(expected.encode()).hexdigest(),
            "trace": trace, "context_state": cfg.context_state,
            "automatic_review_demo": args.automatic_review_demo, "main_request_receipts": request_receipts,
            "task_route_pinned": bool(cfg.decision_routes),
            "replayed_router_case": getattr(args, "replay_uncertain_router", False),
            "routing_policy": {key: getattr(cfg, key) for key in (
                "decision_router_min_probability", "decision_router_min_margin", "decision_router_min_fit")},
            "checkpoint_outcomes": [receipt.get("status", "validated" if receipt["ok"] else "failed")
                                    for receipt in cfg.context_state.get("decision_checkpoints", [])],
            "prompt_asked_for_jev": "jev" in prompt.casefold(),
            "limitations": "Owner-authorized read-only demo under the normal baseline; no mode activation, permission override or normal configuration change."}


def routing_demo(args) -> dict:
    owner_path = Path(os.environ.get("ALGO_CLI_CONFIG_DIR", str(Path.home() / ".algo_cli"))) / "config.json"
    owner = json.loads(owner_path.read_text())
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ["ALGO_CLI_CONFIG_DIR"] = str(workspace / "app-state")
    from algo_cli import config, decision_layer as d, decision_coordinator as c
    cfg = config.Config(cwd=str(workspace), continuum_enabled=False, decision_routing_mode="jev")
    cfg.jev_kernel_enabled = owner.get("jev_kernel_enabled", False)
    cfg.jev_kernel_cli = owner.get("jev_kernel_cli", "")
    cfg.decision_provider_settings = owner.get("decision_provider_settings", {})
    cfg.decision_router_candidates = ["jev", "clef-flash", "tev1"]
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (config.CONFIG_DIR / "ALGO.md").write_text(
        "### A1. Observe fixture before claiming verification\nStatus: proposed\n"
        "### A2. Parser invariant probe\nStatus: proposed\n", encoding="utf-8")
    state, questions, expected = corpus()
    if args.short_fixture:
        state = {"fixture_exists": True, "fixture_contents": "not yet observed", "authorization": "read only"}
        questions = {"observe": {"type": "choice", "instructions": "Which fixture observation advances read-only verification?",
                                 "criteria": {"read": "Read the existing fixture and check the observed text.",
                                              "defer": "Do not observe the available fixture."}}}
        expected = {"observe": "read"}
    if args.offline_fixture:
        cfg.decision_offline_coordinators = [args.offline_coordinator]
        cfg.decision_routes = {"offline-fixture": "clef-flash"}
        with patch.object(c, "connectivity", return_value={"state": "offline", "basis": "explicit_test_fixture_not_real_disconnection"}):
            result = d.decide(cfg, state, questions, task="offline-fixture", local_only=True)
    else:
        result = d.decide(cfg, state, questions, task="code-evidence", local_only=False)
    return {"schema": "algo-coordinator-live.v1", "runtime_source": str(ROOT), "python": sys.executable,
            "offline_is_simulated": args.offline_fixture, "private_catalog_used": False,
            "result": result, "expected": expected,
            "limitations": "Synthetic development questions; not calibrated routing or production accuracy. Offline is a injected host fixture when requested, not a real network outage."}


def main_entry() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", nargs="+", default=["jev", "clef", "clef-flash", "clm", "decider", "tev1"])
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--agent-demo", action="store_true")
    parser.add_argument("--automatic-review-demo", action="store_true")
    parser.add_argument("--policy-mention-demo", action="store_true")
    parser.add_argument("--reported-review-typo-demo", action="store_true")
    parser.add_argument("--unrestricted-routing", action="store_true")
    parser.add_argument("--replay-uncertain-router", action="store_true")
    parser.add_argument("--routing-demo", action="store_true")
    parser.add_argument("--coordinated", action="store_true")
    parser.add_argument("--approve-synthetic-decisions", action="store_true")
    parser.add_argument("--offline-fixture", action="store_true")
    parser.add_argument("--offline-coordinator", choices=("clef", "clef-flash", "tev1"), default="clef-flash")
    parser.add_argument("--short-fixture", action="store_true")
    parser.add_argument("--workspace")
    parser.add_argument("--main-model", default="kimi-k3:cloud")
    parser.add_argument("--main-provider", choices=("ollama", "chatgpt"), default="ollama")
    parser.add_argument("--interrupt-answer", action="store_true")
    parser.add_argument("--explicit-jev", action="store_true")
    parser.add_argument("--provider-failure", action="store_true")
    parser.add_argument("--demo-provider", default="clef-flash")
    parser.add_argument("--test-python", default=sys.executable)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 3 or ((args.agent_demo or args.routing_demo) and not args.workspace):
        parser.error("Invalid repetition count or missing demo workspace")
    result = routing_demo(args) if args.routing_demo else agent_demo(args) if args.agent_demo else compare(args)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.agent_demo:
        return 0 if result["result_matches"] else 1
    if args.routing_demo:
        return 0 if result["result"]["ok"] else 1
    return 0 if all(all(run["ok"] for run in row["runs"]) for row in result["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main_entry())
