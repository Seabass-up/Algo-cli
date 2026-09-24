from __future__ import annotations

import json

import pytest

from algo_cli import tools
from algo_cli.action_registry import get_action_spec, policy_for_action
from algo_cli.config import Config
from algo_cli.tool_context import rank_texts_for_prompt, rank_tools_for_prompt, select_tools_for_prompt

EMAIL_QUERIES = ("check my email", "read my email messages", "email", "inbox", "gmail")


def _names(selected) -> list[str]:
    return [fn.__name__ for fn in selected]


def _slash_commands(payload: dict) -> list[str]:
    return [row["command"] for row in payload["slash_commands"]]


def _full_ceiling_cfg() -> Config:
    from algo_cli.nathan_program_runtime import authorization_for_actions

    cfg = Config()
    cfg._algo_program_authorization = authorization_for_actions(tuple(tools.TOOL_MAP))
    return cfg


@pytest.mark.parametrize("query", EMAIL_QUERIES)
def test_action_search_surfaces_gmail_slash_commands_for_email_intent(query: str) -> None:
    payload = json.loads(tools.action_search(query, limit=12))

    commands = _slash_commands(payload)
    assert any(command.startswith("/google gmail-list") for command in commands)
    assert any(command.startswith("/google gmail-get") for command in commands)
    assert payload["match"] == "found"
    gmail_list = next(row for row in payload["slash_commands"] if row["command"].startswith("/google gmail-list"))
    assert gmail_list["kind"] == "slash_command"
    assert gmail_list["via"] == "session_command"
    assert gmail_list["example"] == {"name": "session_command", "arguments": {"command": "/google gmail-list"}}
    assert gmail_list["example_requires_approval"] is False
    assert "algo-cli config setup google" in gmail_list["setup"]
    assert "session_command" in payload["next"]


def test_action_search_email_query_does_not_return_file_readers() -> None:
    payload = json.loads(tools.action_search("read my email messages", limit=12))

    names = {row["name"] for row in payload["actions"]}
    assert not {"read_file", "read_pdf"} & names


def test_action_search_no_match_is_not_reported_as_unavailable() -> None:
    payload = json.loads(tools.action_search("zzqx frobnicate", limit=12))

    assert payload["count"] == 0
    assert payload["slash_commands"] == []
    assert payload["match"] == "none"
    assert "no-match" in payload["next"]
    assert "unavailable" not in payload["next"].lower()
    assert "not proof the capability is unsupported" in payload["next"]


@pytest.mark.parametrize("query", ["read", "check", "get", "show", "list", "check my", "show me"])
def test_generic_verbs_alone_do_not_match(query: str) -> None:
    assert rank_tools_for_prompt(query, tools.ALL_TOOLS) == []
    payload = json.loads(tools.action_search(query, limit=12))
    assert payload["match"] == "none"


def test_generic_verb_with_domain_term_still_matches() -> None:
    assert "read_file" in _names(rank_tools_for_prompt("read a file", tools.ALL_TOOLS))
    assert "read_pdf" in _names(rank_tools_for_prompt("read this pdf", tools.ALL_TOOLS))


def test_exact_tool_name_still_matches_despite_generic_parts() -> None:
    assert _names(rank_tools_for_prompt("list_directory", [tools.list_directory])) == ["list_directory"]


def test_rank_texts_requires_non_generic_overlap() -> None:
    documents = ["read the file contents", "gmail list messages google email"]

    assert rank_texts_for_prompt("read my email", documents) == [1]
    assert rank_texts_for_prompt("read", documents) == []


@pytest.mark.parametrize("prompt", ["check my email", "read my email messages", "email"])
def test_email_prompt_exposes_session_command_not_only_file_tools(prompt: str) -> None:
    ranked = _names(rank_tools_for_prompt(prompt, tools.ALL_TOOLS))
    assert "read_file" not in ranked
    assert "session_command" in ranked
    assert "session_command" in _names(select_tools_for_prompt(prompt, tools.ALL_TOOLS))


def test_session_command_spec_carries_email_discovery_tags_without_policy_change() -> None:
    spec = get_action_spec("session_command")

    policy = policy_for_action("session_command")
    assert {"google", "gmail", "email", "mail", "inbox"} <= set(spec.tags)
    assert (spec.risk_level, spec.mutates_state, spec.requires_approval, spec.safe_retry) == (
        policy.maximum_risk,
        policy.mutates_state,
        policy.requires_approval,
        policy.safe_retry,
    )


@pytest.mark.parametrize("topic", ["email", "check my email", "read my email messages", "mail", "inbox"])
def test_available_actions_email_topic_matches_google_group(topic: str) -> None:
    payload = json.loads(tools.available_actions(topic))

    focused = payload["focused"]
    assert focused["match"] == "matched"
    assert "/google gmail-list [query] [--max N] [--label LABEL]" in focused["commands"]["google"]
    assert "/google gmail-get MESSAGE_ID" in focused["commands"]["google"]


def test_available_actions_unmatched_topic_reports_none_without_full_payload() -> None:
    raw = tools.available_actions("zzqx frobnicate")
    payload = json.loads(raw)

    assert payload["focused"] == {"match": "none", "commands": {}, "model_callable_tools": {}}
    assert "commands" not in payload
    assert "model_callable_tools" not in payload
    assert "slash_registry" not in payload
    assert "google" in payload["topics"]
    assert "unavailable" not in payload["next"].lower()
    assert len(raw) < 2_000


def test_available_actions_generic_verb_topic_does_not_match_by_tokens() -> None:
    payload = json.loads(tools.available_actions("check my"))

    assert payload["focused"]["match"] == "none"


def test_available_actions_legacy_single_word_topics_still_match() -> None:
    files = json.loads(tools.available_actions("files"))["focused"]
    assert "read_file" in files["model_callable_tools"]["files"]
    pdf = json.loads(tools.available_actions("pdf"))["focused"]
    assert pdf["commands"]["documents"]
    kernel = json.loads(tools.available_actions("kernel"))["focused"]
    assert "kernels" in kernel


def test_action_search_empty_policy_ceiling_keeps_policy_wording(tmp_path) -> None:
    from algo_cli.nathan_program_runtime import authorization_for_actions

    cfg = Config(cwd=str(tmp_path))
    cfg._algo_program_authorization = authorization_for_actions(())
    payload = json.loads(tools.action_search("zzqx frobnicate", limit=12, cfg=cfg))

    assert payload["match"] == "none"
    assert "current runtime policy" in payload["next"]


def test_action_search_hides_slash_commands_refused_by_runtime_policy(monkeypatch) -> None:
    import algo_cli.irene_memory_path_policy as policy

    monkeypatch.setattr(policy, "protected_tool_policy_error", lambda name, args, cfg: "Error: refused")
    payload = json.loads(tools.action_search("check my email", limit=12, cfg=_full_ceiling_cfg()))

    assert payload["slash_commands"] == []
    assert payload["match"] == "none"


def test_session_command_docstring_lists_gmail_commands() -> None:
    doc = tools.session_command.__doc__ or ""
    assert "/google gmail-list" in doc
    assert "/google gmail-get MESSAGE_ID" in doc


@pytest.mark.parametrize("ceiling", [(), ("read_file", "list_directory"), ("read_file", "session_command")])
def test_action_search_hides_slash_commands_outside_ordinary_chat_ceiling(ceiling) -> None:
    # Agent Blocks bind a narrower ceiling; ProgramAuthorization strips
    # session_command, so any restricted ceiling must fail closed.
    from algo_cli.nathan_program_runtime import authorization_for_actions

    cfg = Config()
    cfg._algo_program_authorization = authorization_for_actions(ceiling)
    payload = json.loads(tools.action_search("check my email", cfg=cfg))

    assert payload["slash_commands"] == []
    assert payload["slash_command_count"] == 0
    assert "session_command" not in payload["next"]
    if not ceiling:
        assert "No composable actions are available within the current runtime policy" in payload["next"]


def test_action_search_unbound_authorization_hides_slash_commands() -> None:
    payload = json.loads(tools.action_search("check my email", cfg=Config()))

    assert payload["slash_commands"] == []
    assert "current runtime policy" in payload["next"]


def test_action_search_full_ordinary_chat_ceiling_lists_gmail() -> None:
    payload = json.loads(tools.action_search("check my email", cfg=_full_ceiling_cfg()))

    assert any(command.startswith("/google gmail-list") for command in _slash_commands(payload))
    assert "session_command" in payload["next"]


@pytest.mark.parametrize("query", EMAIL_QUERIES)
def test_action_search_default_limit_ranks_gmail_commands_first(query: str) -> None:
    payload = json.loads(tools.action_search(query))

    top_two = _slash_commands(payload)[:2]
    assert any(command.startswith("/google gmail-list") for command in top_two)
    assert any(command.startswith("/google gmail-get") for command in top_two)


@pytest.mark.parametrize(
    ("query", "service"),
    [("open my drive files", "/google drive"), ("my calendar events", "/google calendar")],
)
def test_action_search_non_gmail_google_queries_rank_their_service_first(query: str, service: str) -> None:
    payload = json.loads(tools.action_search(query))

    assert _slash_commands(payload)[0].startswith(service)


def test_generic_verb_with_filename_keeps_pdf_reader_selected() -> None:
    assert "read_pdf" in _names(select_tools_for_prompt("read report.pdf", tools.ALL_TOOLS))
    assert "read_pdf" in _names(rank_tools_for_prompt("read report.pdf", tools.ALL_TOOLS))


@pytest.mark.parametrize("prompt", ["read README.md", "read notes.txt", "read the config", "show docs/guide.md"])
def test_generic_verb_with_file_object_still_ranks_read_file(prompt: str) -> None:
    assert "read_file" in _names(rank_tools_for_prompt(prompt, tools.ALL_TOOLS))
    payload = json.loads(tools.action_search(prompt))
    assert "read_file" in {row["name"] for row in payload["actions"]}


@pytest.mark.parametrize("prompt", ["check x.com", "read my email"])
def test_domains_and_email_are_not_file_objects(prompt: str) -> None:
    assert "read_file" not in _names(rank_tools_for_prompt(prompt, tools.ALL_TOOLS))
