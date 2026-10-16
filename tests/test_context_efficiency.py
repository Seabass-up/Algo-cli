"""Optional tutorials must not change tools, Jev policy or memory admission."""

import json
from types import SimpleNamespace

import pytest

from algo_cli import context_budget, continuum_memory, main, prompt_guidance
from algo_cli.config import Config
from algo_cli.prompt_rules import DECISION_VERIFICATION_RULE


@pytest.fixture
def prompt_env(monkeypatch):
    monkeypatch.setattr(context_budget, "load_runtime_env", lambda **kwargs: None)
    monkeypatch.setattr(context_budget, "json_sink", lambda: None)
    monkeypatch.setattr(context_budget.identity, "build_identity_block", lambda **kwargs: "immutable identity")
    monkeypatch.setattr(context_budget, "_memory_prompt_section", lambda *a, **kw: "")
    return Config(model="fixture-model", reflex_enabled=False)


def test_ordinary_question_keeps_jev_safety_without_unrelated_tutorials(prompt_env):
    prompt = context_budget.build_system_prompt(prompt_env, user_message="What makes a good assistant?")
    assert "Jev is the default coordinator" in prompt
    assert "local_only=true forbids ALL remote" in prompt
    assert "local_only=false and runtime authority" in prompt
    assert "Only confirmed internet loss" in prompt
    assert "never private thought streams, credentials or bulk history" in prompt
    assert DECISION_VERIFICATION_RULE in prompt
    assert "No embedding or Lab-index backend" in prompt
    assert "Never publish the user's catalog or custom kernels" in prompt
    assert "## Session Mode" in prompt
    for title in ("## PDF Handling", "## Grok / xAI", "## Local Harness Bridge"):
        assert title not in prompt
    assert prompt_env.decision_checkpoints_enabled is True
    assert prompt_env.decision_routing_mode == "jev"
    assert prompt_env.decision_router_min_probability == 0.8


@pytest.mark.parametrize("task,section", [
    ("Read REPORT.PDF", "pdf"),
    ("Compare the PDFs", "pdf"),
    ("Does grok-4 work?", "xai"),
    ("Review my wiki notes", "harness"),
])
def test_task_guidance_is_selected_from_intent(task, section):
    assert set(prompt_guidance.task_guidance(task, [], xai=False)) == {section}


@pytest.mark.parametrize("tool,section", [("read_pdf", "pdf"), ("render_pdf_pages", "pdf"),
                                          ("vision_describe", "pdf"), ("harness_search", "harness")])
def test_discovered_tools_activate_guidance_but_history_does_not(tool, section):
    task = "Inspect the attachment"
    messages = [{"role": "user", "content": task},
                {"role": "assistant", "tool_calls": [{"function": {"name": tool, "arguments": {}}}]}]
    assert section in prompt_guidance.task_guidance(task, messages, xai=False)
    assert prompt_guidance.task_guidance("A different question", messages, xai=False) == {}
    messages.append({"role": "user", "content": task})
    assert prompt_guidance.task_guidance(task, messages, xai=False) == {}


def test_tool_output_cannot_select_tutorials_and_xai_route_is_supported():
    messages = [{"role": "user", "content": "Check it"},
                {"role": "tool", "content": "PDF grok harness", "name": "read_file"}]
    assert prompt_guidance.task_guidance("Check it", messages, xai=False) == {}
    assert set(prompt_guidance.task_guidance("Check it", [], xai=True)) == {"xai"}


def test_tool_activation_reads_only_name_not_arguments():
    class Function:
        name = "read_pdf"
        @property
        def arguments(self):
            pytest.fail("guidance must not read or parse tool payloads")
    messages = [{"role": "user", "content": "Inspect attachment"},
                {"role": "assistant", "tool_calls": [SimpleNamespace(function=Function())]}]
    assert "pdf" in prompt_guidance.task_guidance("Inspect attachment", messages, xai=False)


def test_harness_scope_rule_is_not_duplicated(prompt_env):
    prompt = context_budget.build_system_prompt(prompt_env, user_message="Review the harness")
    assert prompt.count("External local agent stores are disabled") == 1


def test_history_fallback_and_explicit_current_intent_are_distinct(prompt_env):
    prompt_env.messages = [{"role": "user", "content": "Read report.pdf"}]
    assert "## PDF Handling" in context_budget.build_system_prompt(prompt_env)
    assert "## PDF Handling" not in context_budget.build_system_prompt(prompt_env, user_message="A new task")


def test_current_task_tool_guidance_survives_compaction_without_cross_task_state(prompt_env):
    task = "Inspect attachment"
    prompt_env.messages = [{"role": "user", "content": task},
                           {"role": "assistant", "tool_calls": [{"function": {"name": "read_pdf"}}]}]
    observed = prompt_guidance.task_tool_names(prompt_env.messages, task)
    prompt_env.messages = []  # Compaction may remove the original user/tool messages.
    assert "## PDF Handling" in context_budget.build_system_prompt(prompt_env, user_message=task,
                                                                  guidance_tools=observed)
    assert "## PDF Handling" not in context_budget.build_system_prompt(prompt_env, user_message="New task")


def test_prompt_accounting_partitions_actual_text_once_without_content(monkeypatch, prompt_env):
    secret = "PRIVATE_MEMORY_CANARY" * 19
    calls = []
    monkeypatch.setattr(context_budget, "_memory_prompt_section", lambda *a, **kw: calls.append(1) or secret)
    prompt = context_budget.build_system_prompt(prompt_env, user_message="Read report.pdf")
    diagnostics = prompt_env._prompt_diagnostics
    assert calls == [1]
    assert sum(diagnostics["estimated_tokens"].values()) == context_budget.estimate_text_tokens(prompt)
    assert diagnostics["estimated_tokens"]["memory"] >= context_budget.estimate_text_tokens(secret)
    assert set(diagnostics["guidance_tokens"]) == {"pdf"}
    assert diagnostics["build_ms"] >= 0
    assert secret not in json.dumps(diagnostics)
    prompt_env.save()
    from algo_cli import config
    assert "_prompt_diagnostics" not in config.CONFIG_FILE.read_text()


def test_failed_build_clears_previous_diagnostics(monkeypatch, prompt_env):
    context_budget.build_system_prompt(prompt_env, user_message="Read report.pdf")
    def failed(*a, **kw):
        raise continuum_memory.ContinuumMemoryError("unavailable")
    monkeypatch.setattr(context_budget, "_memory_prompt_section", failed)
    with pytest.raises(continuum_memory.ContinuumMemoryError):
        context_budget.build_system_prompt(prompt_env, user_message="Check state")
    assert prompt_env._prompt_diagnostics == {}


@pytest.mark.parametrize("refusal", [False, True])
def test_memory_diagnostics_preserve_both_scope_payloads_and_fail_closed(monkeypatch, refusal):
    cfg = Config(continuum_enabled=True, continuum_active_projects=["algo-cli"])
    calls = []
    payloads = {}
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: ["PRIVATE_FACT_CANARY"])
    def invoke(cfg, operation, payload, *, scope):
        calls.append((operation, scope, payload.get("active_projects")))
        if operation == "validate_context":
            return {"ok": not (refusal and scope == "private")}
        context = {"items": [{"id": "private-id", "required": True, "data": {"text": "PRIVATE_MEMORY_CANARY"}},
                             {"id": "optional-id", "required": False}],
                   "project_selection": {"requested": ["algo-cli"], "effective": ["algo-cli", "research"],
                                         "unknown_projects": []}}
        payloads[scope] = context
        return {"ok": True, "context": context, "receipt": "verified-envelope"}
    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    if refusal:
        with pytest.raises(continuum_memory.ContinuumMemoryError, match="could not be verified"):
            continuum_memory.prompt_context(cfg, "review")
        assert cfg._memory_context_diagnostics == {}
    else:
        result = json.loads(continuum_memory.prompt_context(cfg, "review"))
        for scope in ("shared", "private"):
            assert result["contexts"][scope]["context"] == payloads[scope]
            assert cfg._memory_context_diagnostics["scopes"][scope] == {
                "required_items": 1, "optional_items": 1, "effective_projects": 2, "unknown_projects": 0}
        assert "PRIVATE" not in json.dumps(cfg._memory_context_diagnostics)
        assert "private-id" not in json.dumps(cfg._memory_context_diagnostics)
    assert calls == [("context", "shared", ["algo-cli"]), ("validate_context", "shared", ["algo-cli"]),
                     ("context", "private", ["algo-cli"]), ("validate_context", "private", ["algo-cli"])]


def test_context_status_exposes_only_content_free_build_diagnostics(monkeypatch, prompt_env, capsys):
    context_budget.build_system_prompt(prompt_env, user_message="Read report.pdf")
    monkeypatch.setattr(main, "context_status", lambda *a, **kw: (1, 8192, 8191, 8192, None))
    main.handle_context_command("status", prompt_env, object())
    output = capsys.readouterr().out
    assert "not provider token counts" in output
    assert "task_guidance" in output and "pdf" in output
    assert "report.pdf" not in output


def test_new_guidance_and_regressions_are_bound_to_qualification_sources():
    from algo_cli.evals import nathan_agent_runtime_hardening
    from scripts import henry_m8_qualification
    for paths in (nathan_agent_runtime_hardening.SOURCE_PATHS, henry_m8_qualification.SOURCE_PATHS):
        assert "algo_cli/prompt_guidance.py" in paths
        assert "tests/test_context_efficiency.py" in paths


def test_automated_prompt_keeps_core_and_records_no_interactive_tutorial(monkeypatch, prompt_env):
    monkeypatch.setattr(context_budget, "json_sink", lambda: object())
    prompt = context_budget.build_system_prompt(prompt_env, user_message="Read report.pdf using grok")
    assert "Jev is the default coordinator" in prompt
    assert DECISION_VERIFICATION_RULE in prompt
    assert "runtime policy permits it" in prompt
    assert prompt_env._prompt_diagnostics["guidance_tokens"] == {}
    assert sum(prompt_env._prompt_diagnostics["estimated_tokens"].values()) == context_budget.estimate_text_tokens(prompt)


@pytest.mark.parametrize("task,pdf", [("Read report.pdf", True), ("Explain the result", False)])
def test_actual_agent_loop_passes_current_intent_to_prompt_builder(monkeypatch, task, pdf):
    from test_main_helpers import _patch_agent_loop_for_tool_policy_test
    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    monkeypatch.setattr(context_budget, "json_sink", lambda: None)
    requests = []
    class Client:
        def chat(self, **kwargs):
            requests.append(kwargs)
            return iter([{"message": {"content": "Done."}}])
    cfg = Config(model="fixture", continuum_enabled=True, skill_crystallize_enabled=False,
                 memory_auto_capture_enabled=False)
    main.agent_loop(Client(), cfg, task)
    assert len(requests) == 1
    system = requests[0]["messages"][0]["content"]
    assert ("## PDF Handling" in system) is pdf
    assert "Jev is the default coordinator" in system
    assert DECISION_VERIFICATION_RULE in system
