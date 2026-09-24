"""Jev readiness text and payloads state the advisory-only boundary and the real execution routes."""

from __future__ import annotations

import json

import pytest

from algo_cli import jev_kernel as jev
from algo_cli.config import Config


def _first_paragraph(fn) -> str:
    return " ".join((fn.__doc__ or "").strip().split("\n\n", 1)[0].split())


def _ready_invoke(_cfg, operation, _payload):
    if operation == "status":
        return {"ok": True, "mode": "advisory_only", "model": jev.MODEL}
    return {"ok": False, "schema_version": jev.RESULT_SCHEMA, "status": "invalid_contract"}


def _assert_boundary(payload: dict) -> None:
    assert payload["advisory_only"] is True
    assert payload["capability"] == "advisory_judgments_only"
    assert payload["not_for"] == ["browser_control", "email_access", "action_execution"]


@pytest.mark.parametrize("name", ["jev_kernel_status", "jev_question_contract"])
def test_schema_description_states_boundary_and_routes(name):
    from ollama._utils import convert_function_to_tool

    from algo_cli.tools import ALL_TOOLS

    tool = next(fn for fn in ALL_TOOLS if fn.__name__ == name)
    description = " ".join((convert_function_to_tool(tool).function.description or "").split())
    assert "cannot browse, read email or execute actions" in description
    assert "cobalt_*" in description
    assert "/google Gmail read/drafts after OAuth" in description


@pytest.mark.parametrize("fn", [jev.jev_kernel_status, jev.jev_question_contract])
def test_ranked_first_paragraph_has_no_email_or_browser_terms(fn):
    # tool_context ranks tools on the first paragraph only; route words there
    # would pull Jev tools into email prompts and evict session_command.
    first = _first_paragraph(fn).casefold()
    for term in ("email", "gmail", "draft", "browse", "browser", "cobalt", "/google"):
        assert term not in first


@pytest.mark.parametrize(
    "prompt",
    [
        "review email drafts",
        "review my email drafts",
        "triage gmail drafts",
        "review and read the email draft file",
    ],
)
def test_email_review_prompts_keep_session_command_route(prompt):
    from algo_cli.tool_context import select_tools_for_prompt
    from algo_cli.tools import ALL_TOOLS

    selected = {fn.__name__ for fn in select_tools_for_prompt(prompt, ALL_TOOLS)}
    assert "session_command" in selected
    assert "jev_kernel_status" not in selected
    assert "jev_question_contract" not in selected


@pytest.mark.parametrize("prompt", ["review my gmail messages", "review and execute the actions"])
def test_non_jev_review_prompts_do_not_select_jev_status(prompt):
    from algo_cli.tool_context import select_tools_for_prompt
    from algo_cli.tools import ALL_TOOLS

    assert "jev_kernel_status" not in {fn.__name__ for fn in select_tools_for_prompt(prompt, ALL_TOOLS)}


def test_ready_status_payload_carries_static_capability(monkeypatch):
    monkeypatch.setattr(jev, "_invoke", _ready_invoke)
    payload = jev.kernel_status(Config())
    assert payload["ok"] is True
    _assert_boundary(payload)
    _assert_boundary(json.loads(jev.jev_kernel_status(Config())))


@pytest.mark.parametrize("error", [jev.JevKernelError("jev_companion_not_configured"), OSError("gone")])
def test_failed_status_payload_carries_static_capability(monkeypatch, error):
    def invoke(*_a):
        raise error

    monkeypatch.setattr(jev, "_invoke", invoke)
    payload = jev.kernel_status(Config())
    assert payload["ok"] is False
    assert payload["fallback"] == "continue_with_existing_workflow"
    _assert_boundary(payload)


def test_contract_failure_payload_carries_static_capability(monkeypatch):
    monkeypatch.setattr(jev, "_invoke", lambda *_a: pytest.fail("companion invoked"))
    payload = jev.question_contract(Config(), {}, "run")
    assert payload["error"] == "jev_kernel_disabled"
    _assert_boundary(payload)


def test_not_for_is_not_shared_mutable_state(monkeypatch):
    monkeypatch.setattr(jev, "_invoke", _ready_invoke)
    jev.kernel_status(Config())["not_for"].append("anything")
    assert jev.kernel_status(Config())["not_for"] == list(jev.NOT_FOR)


def test_jev_browser_email_prompt_selects_status_with_boundary_schema():
    from ollama._utils import convert_function_to_tool

    from algo_cli.tool_context import select_tools_for_prompt
    from algo_cli.tools import ALL_TOOLS

    selected = {fn.__name__: fn for fn in select_tools_for_prompt("can jev check my email in the browser", ALL_TOOLS)}
    assert "jev_kernel_status" in selected
    assert "session_command" in selected
    description = " ".join((convert_function_to_tool(selected["jev_kernel_status"]).function.description or "").split())
    assert "cannot browse, read email or execute actions" in description
    assert "cobalt_*" in description
