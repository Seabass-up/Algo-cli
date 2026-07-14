"""Tests for task-local model context selection and reconciliation guidance."""

from __future__ import annotations

from algo_cli import deliberation, reconciliation, tools
from algo_cli.tool_context import (
    CORE_TOOL_NAMES,
    DEFAULT_TOOL_LIMIT,
    declared_tool_classes,
    rank_tools_for_prompt,
    select_tools_for_prompt,
)


def _names(selected: list) -> set[str]:
    return {tool.__name__ for tool in selected}


def test_declared_tool_classes_are_normalized() -> None:
    prompt = "Allowed tool classes:\nfilesystem, shell, structured log, final answer\n"
    assert declared_tool_classes(prompt) == (
        "filesystem",
        "shell",
        "structured_log",
        "final_answer",
    )


def test_explicit_filesystem_and_shell_classes_reduce_model_tool_context() -> None:
    selected = select_tools_for_prompt(
        "Allowed tool classes: filesystem, shell, structured_log, final_answer",
        tools.ALL_TOOLS,
    )
    names = _names(selected)
    assert {"read_file", "edit_file", "write_file", "run_shell", "git_diff"} <= names
    assert "web_search" not in names
    assert "remember" not in names
    assert len(selected) < len(tools.ALL_TOOLS) / 2


def test_missing_declaration_uses_bounded_discovery_but_unknown_class_fails_closed() -> None:
    selected_default = select_tools_for_prompt("Inspect this project", tools.ALL_TOOLS)
    assert set(CORE_TOOL_NAMES) <= _names(selected_default)
    assert len(selected_default) <= DEFAULT_TOOL_LIMIT
    assert len(selected_default) < len(tools.ALL_TOOLS)
    assert select_tools_for_prompt("Allowed tool classes: browser", tools.ALL_TOOLS) == []
    selected = select_tools_for_prompt("Allowed tool classes: filesystem, browser", tools.ALL_TOOLS)
    assert "read_file" in _names(selected)
    assert "web_search" not in _names(selected)


def test_ordinary_prompt_ranking_recalls_required_code_tools_and_is_stable() -> None:
    prompt = "Fix the failing parser and run tests, then verify the diff."
    selections = [select_tools_for_prompt(prompt, tools.ALL_TOOLS) for _ in range(5)]
    names = _names(selections[0])

    assert {"read_file", "search_files", "edit_file", "run_shell", "git_diff"} <= names
    assert len(selections[0]) <= DEFAULT_TOOL_LIMIT
    assert [[tool.__name__ for tool in selected] for selected in selections] == [
        [tool.__name__ for tool in selections[0]]
    ] * 5
    assert [tool.__name__ for tool in rank_tools_for_prompt(prompt, tools.ALL_TOOLS)] == [
        tool.__name__ for tool in rank_tools_for_prompt(prompt, tools.ALL_TOOLS)
    ]


def test_incidental_harness_word_does_not_activate_specialist_context() -> None:
    prompt = (
        "You are participating in a controlled agent-harness benchmark. "
        "Read the task file, fix the source, run its verification, and summarize."
    )

    names = _names(select_tools_for_prompt(prompt, tools.ALL_TOOLS))

    assert {"read_file", "edit_file", "run_shell", "git_diff"} <= names
    assert not any(name.startswith("harness_") for name in names)
    assert "screenshot_description_verify" not in names
    assert "url_scheme_parse" not in names


def test_explicit_harness_search_intent_passes_specialist_gate() -> None:
    names = _names(
        select_tools_for_prompt(
            "Search the harness memory records and show harness stats.",
            tools.ALL_TOOLS,
        )
    )

    assert {"harness_search", "harness_stats"} <= names


def test_deferred_runtime_tools_are_always_visible_when_installed() -> None:
    def action_search(query: str) -> str:
        return query

    def action_program(steps: list[dict]) -> str:
        return str(steps)

    selected = select_tools_for_prompt(
        "A deliberately unrelated request",
        [*tools.ALL_TOOLS, action_search, action_program],
    )

    assert {"action_search", "action_program"} <= _names(selected)
    assert "available_actions" not in _names(selected)


def test_reconciliation_guidance_is_task_local_and_schema_aware() -> None:
    prompt = (
        "Retrieved context may be stale. Treat the live manifest as authoritative, "
        "then update the JSON settings."
    )
    guidance = reconciliation.guidance_for_prompt(prompt)
    assert guidance is not None
    assert "Preserve the target schema" in guidance
    assert "key names may differ" in guidance
    augmented = reconciliation.augment_read_result("read_file", prompt)
    assert "[Algo reasoning strategy]" in augmented
    assert reconciliation.augment_read_result("run_shell", prompt) == prompt
    assert reconciliation.guidance_for_prompt("Fix the parser and run tests") is None


def test_deliberation_is_adaptive_for_deep_one_shot_tasks() -> None:
    assert deliberation.needs_deliberation("Perform a security audit and threat model") is True
    assert deliberation.needs_deliberation("Fix the failing test and verify it") is False


def test_reconciliation_infers_cross_schema_lineage_from_values() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "live.json"}}}
            ],
        },
        {
            "role": "tool",
            "name": "read_file",
            "content": '{"approval_ticket":"LIVE-9","authority_note":"authoritative source of truth"}',
        },
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "settings.json"}}}
            ],
        },
        {
            "role": "tool",
            "name": "read_file",
            "content": '{"project_code":"OLD-2"}',
        },
    ]
    stale = "The stale cache claims approval ticket OLD-2."

    guidance = reconciliation.lineage_guidance(messages, stale)

    assert guidance is not None
    assert "`project_code`" in guidance
    assert "`LIVE-9`" in guidance
    assert "Do not remove or rename `project_code`" in guidance

    constraint_messages = messages + [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "stale.md"}}}
            ],
        },
        {"role": "tool", "name": "read_file", "content": stale},
    ]

    bad_write = reconciliation.structured_write_violation(
        "write_file",
        {"path": "settings.json", "content": '{"approval_ticket":"LIVE-9"}'},
        constraint_messages,
    )
    assert bad_write is not None
    assert "keeping `project_code`" in bad_write

    good_write = reconciliation.structured_write_violation(
        "write_file",
        {"path": "settings.json", "content": '{"project_code":"LIVE-9"}'},
        constraint_messages,
    )
    assert good_write is None

    unrelated_write = reconciliation.structured_write_violation(
        "write_file",
        {"path": "report.json", "content": '{"approval_ticket":"LIVE-9"}'},
        constraint_messages,
    )
    assert unrelated_write is None


def test_reconciliation_blocks_literal_stale_values_in_omission_summary() -> None:
    messages = [
        {
            "role": "tool",
            "name": "read_file",
            "content": "Do not include stale retrieved-context values in live_fact_summary.md as facts.",
        },
        {
            "role": "tool",
            "name": "read_file",
            "content": (
                "This stale RAG cache claims the project is Old Project, approval ticket OLD-2, "
                "status endpoint `/old`, feature flag `old_mode`, operations contact Old Person, "
                "and go-live date 2020-01-01."
            ),
        },
    ]

    violation = reconciliation.structured_write_violation(
        "write_file",
        {"path": "artifacts/live_fact_summary.md", "content": "Old Project was stale."},
        messages,
    )

    assert violation is not None
    assert "Omit the literal stale values" in violation

    allowed = reconciliation.structured_write_violation(
        "write_file",
        {
            "path": "artifacts/live_fact_summary.md",
            "content": "The authoritative live manifest provides the current values.",
        },
        messages,
    )
    assert allowed is None


def test_reconciliation_omission_constraint_survives_long_tool_chain() -> None:
    messages = [
        {
            "role": "tool",
            "name": "read_file",
            "content": "Do not include stale retrieved-context values in live_fact_summary.md as facts.",
        },
        {
            "role": "tool",
            "name": "read_file",
            "content": "This stale RAG cache claims the feature flag legacy_mode.",
        },
        *({"role": "tool", "name": "run_shell", "content": "ok"} for _ in range(48)),
    ]

    violation = reconciliation.structured_write_violation(
        "write_file",
        {
            "path": "artifacts/live_fact_summary.md",
            "content": "The stale flag legacy_mode was replaced.",
        },
        messages,
    )

    assert violation is not None
    assert "legacy_mode" in violation


def test_reconciliation_does_not_mine_task_instructions_as_stale_facts() -> None:
    messages = [
        {
            "role": "tool",
            "name": "read_file",
            "content": (
                "Stale retrieved context may conflict with the live project manifest. "
                "The summary must include the live feature flag name and project facts."
            ),
        },
        {
            "role": "tool",
            "name": "read_file",
            "content": (
                "This stale RAG cache claims the feature flag legacy_mode.\n\n"
                "[Algo reasoning strategy]\nInspect the live project manifest."
            ),
        },
    ]

    allowed = reconciliation.structured_write_violation(
        "write_file",
        {
            "path": "artifacts/live_fact_summary.md",
            "content": "Live project manifest facts include the current feature flag name.",
        },
        messages,
    )

    assert allowed is None
