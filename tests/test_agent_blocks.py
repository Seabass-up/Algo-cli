from __future__ import annotations

import pytest

from algo_cli import agent_blocks


def test_default_pipeline_roles_and_tool_policy():
    pipeline = agent_blocks.default_pipeline()

    assert [block.role for block in pipeline] == ["plan", "research", "implement", "review", "final"]
    assert pipeline[0].allowed_tools == agent_blocks.NO_TOOLS
    assert "write_file" not in pipeline[1].allowed_tools
    assert "write_file" in pipeline[2].allowed_tools
    assert pipeline[2].requires_change is True
    assert "write_file" not in pipeline[3].allowed_tools
    assert pipeline[4].allowed_tools == agent_blocks.NO_TOOLS
    assert all(block.model is None for block in pipeline)


def test_default_pipeline_returns_fresh_blocks():
    first = agent_blocks.default_pipeline()
    second = agent_blocks.default_pipeline()

    first[0].status = "complete"
    assert second[0].status == "pending"


def test_named_pipelines_have_expected_roles():
    assert [block.role for block in agent_blocks.pipeline_by_name("code-change")] == [
        "plan",
        "implement",
        "review",
        "final",
    ]
    code_change = agent_blocks.code_change_pipeline()
    assert code_change[0].allowed_tools == agent_blocks.PLAN_TOOLS
    assert "read_file" in code_change[0].allowed_tools
    assert "query_knowledge_graph" in code_change[0].allowed_tools
    assert "write_file" not in code_change[0].allowed_tools
    assert code_change[1].requires_change is True
    assert code_change[0].max_iterations == 4
    assert code_change[1].max_iterations == 12
    assert "execute" in code_change[1].prompt.lower()
    assert "write_file" in code_change[1].prompt
    assert "overwrite=True" in code_change[1].prompt
    assert [block.role for block in agent_blocks.pipeline_by_name("research")] == [
        "plan",
        "research",
        "final",
    ]
    assert [block.role for block in agent_blocks.pipeline_by_name("review")] == ["review", "final"]


def test_review_prompts_direct_large_workspace_sampling():
    review_prompts = [
        next(block.prompt for block in agent_blocks.default_pipeline() if block.role == "review"),
        next(block.prompt for block in agent_blocks.code_change_pipeline() if block.role == "review"),
        agent_blocks.review_pipeline()[0].prompt,
    ]

    assert all("sample" in prompt.lower() for prompt in review_prompts)
    assert all("exhaust" in prompt.lower() for prompt in review_prompts)
    assert all("stop" in prompt.lower() for prompt in review_prompts)
    assert all("claim" in prompt.lower() for prompt in review_prompts)


def test_required_change_prompt_is_prescriptive_about_write_file():
    text = agent_blocks.REQUIRED_CHANGE_PROMPT

    assert "MUST use write_file" in text
    assert "MUST NOT use run_shell" in text
    assert "heredoc" in text.lower()
    assert "redirection" in text.lower() or "redirect" in text.lower()
    assert "verified change" in text.lower() or "evidence of a change" in text.lower()
    assert "verification" in text.lower()


def test_implement_prompts_forbid_shell_based_file_mutation():
    implement_prompts = [
        next(block.prompt for block in agent_blocks.default_pipeline() if block.role == "implement"),
        next(block.prompt for block in agent_blocks.code_change_pipeline() if block.role == "implement"),
    ]

    for prompt in implement_prompts:
        lowered = prompt.lower()
        assert "write_file" in prompt
        assert "do not use run_shell" in lowered
        assert "verification" in lowered


def test_starter_config_implement_blocks_forbid_shell_based_file_mutation():
    config = agent_blocks.STARTER_CONFIG

    assert "Do not use run_shell to write, edit, append to, or delete files" in config
    assert "shell-based file mutation does not count" in config
    assert "Use write and shell tools only when necessary" not in config


def test_unknown_pipeline_reports_available_names():
    try:
        agent_blocks.pipeline_by_name("missing")
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("pipeline_by_name should reject unknown names")

    assert "Unknown pipeline" in message
    assert "code-change" in message


def test_pipeline_context_threads_completed_outputs():
    block = agent_blocks.AgentBlock(role="plan", prompt="p", output="Do the work.")

    context = agent_blocks.pipeline_context("Build a thing", [block])

    assert "## Original Task\nBuild a thing" in context
    assert "## Output from plan\nDo the work." in context


def test_pipeline_context_uses_compacted_output():
    block = agent_blocks.AgentBlock(
        role="plan",
        prompt="p",
        output="full output",
        context_output="compacted output",
    )

    context = agent_blocks.pipeline_context("Build a thing", [block])

    assert "compacted output" in context
    assert "full output" not in context


def test_pipeline_context_exposes_missing_implementation_write_evidence():
    block = agent_blocks.AgentBlock(role="implement", prompt="p", output="Implementation complete.", requires_change=True)

    context = agent_blocks.pipeline_context("Build a thing", [block])

    assert "## Implementation Write Evidence" in context
    assert "Successful write_file targets: (none recorded)" in context
    assert "Do not claim requested code changes were completed" in context
    assert "## Implementation Git Evidence" in context
    assert "No Git evidence snapshot was captured." in context


def test_pipeline_context_includes_implementation_git_evidence():
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        output="Implementation complete.",
        requires_change=True,
        git_evidence="Git state changed during the implement block.\nChanged tracked files after implement:\nollama_cli/main.py",
    )

    context = agent_blocks.pipeline_context("Build a thing", [block])

    assert "Git state changed during the implement block." in context
    assert "ollama_cli/main.py" in context


def test_pipeline_context_includes_non_change_mutation_audit_without_claiming_implementation():
    block = agent_blocks.AgentBlock(
        role="review",
        prompt="p",
        output="Review complete.",
        audit_evidence="Audit notice: review executed run_shell: git add main.py",
    )

    context = agent_blocks.pipeline_context("Review a thing", [block])

    assert "## Mutation Audit Evidence" in context
    assert "Audit notice" in context
    assert "audit-only evidence" in context
    assert "## Implementation Write Evidence" not in context


def test_pipeline_context_includes_partial_status_reason_separately_from_output():
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        status="partial",
        status_reason="Required change not verified: no attributable Git delta was detected.",
        output="## Block Output\n\nReported attempt.",
    )

    context = agent_blocks.pipeline_context("Build a thing", [block])

    assert "## Output from implement\n## Block Output\n\nReported attempt." in context
    assert "## Block Status\nStatus: PARTIAL" in context
    assert "Code: -" in context
    assert "Reason: Required change not verified" in context


def test_pipeline_context_surfaces_verification_warning_separately_from_output():
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        status="complete",
        output="Implemented.",
        requires_change=True,
        successful_writes=["main.py"],
        verification_warning="Git verification was unavailable. Review must manually confirm the written files.",
    )

    context = agent_blocks.pipeline_context("Build a thing", [block])

    assert "## Output from implement\nImplemented." in context
    assert "## Verification\nGit verification was unavailable." in context
    assert "manually confirm" in context


def test_compact_block_output_preserves_paragraph_boundary():
    output = "first paragraph\n\nsecond paragraph that is long\n\nthird paragraph"

    compacted = agent_blocks.compact_block_output(output, limit_chars=20)

    assert compacted.startswith("first paragraph")
    assert "second paragraph" not in compacted
    assert "compacted from" in compacted


def test_load_configured_pipeline_expands_tool_groups_and_model(tmp_path):
    path = tmp_path / "blocks.toml"
    path.write_text(
        """version = 1

[pipelines.code-change]
[[pipelines.code-change.blocks]]
role = "implement"
prompt = "Make the change."
tools = ["read", "write"]
model = "qwen3"
max_iterations = 3
requires_change = true
""",
        encoding="utf-8",
    )

    pipeline, source = agent_blocks.resolve_pipeline("code-change", path)

    assert source == "configured"
    assert pipeline[0].role == "implement"
    assert pipeline[0].model == "qwen3"
    assert pipeline[0].max_iterations == 3
    assert pipeline[0].requires_change is True
    assert "read_file" in pipeline[0].allowed_tools
    assert "write_file" in pipeline[0].allowed_tools
    assert "web_search" not in pipeline[0].allowed_tools
    assert "## Block Output" in pipeline[0].prompt


def test_invalid_tool_group_rejects_configured_pipeline(tmp_path):
    path = tmp_path / "blocks.toml"
    path.write_text(
        """version = 1

[pipelines.review]
[[pipelines.review.blocks]]
role = "review"
prompt = "Review."
tools = ["write_fiel"]
""",
        encoding="utf-8",
    )

    with pytest.raises(agent_blocks.BlocksConfigError, match="unknown tool group"):
        agent_blocks.resolve_pipeline("review", path)


def test_invalid_requires_change_rejects_configured_pipeline(tmp_path):
    path = tmp_path / "blocks.toml"
    path.write_text(
        """version = 1

[pipelines.code-change]
[[pipelines.code-change.blocks]]
role = "implement"
prompt = "Implement."
tools = ["write"]
requires_change = "yes"
""",
        encoding="utf-8",
    )

    with pytest.raises(agent_blocks.BlocksConfigError, match="requires_change"):
        agent_blocks.resolve_pipeline("code-change", path)


def test_requires_change_without_write_group_rejects_configured_pipeline(tmp_path):
    path = tmp_path / "blocks.toml"
    path.write_text(
        """version = 1

[pipelines.code-change]
[[pipelines.code-change.blocks]]
role = "implement"
prompt = "Implement."
tools = ["read", "shell"]
requires_change = true
""",
        encoding="utf-8",
    )

    with pytest.raises(agent_blocks.BlocksConfigError, match="must be able to use write_file"):
        agent_blocks.resolve_pipeline("code-change", path)


def test_write_starter_config_preserves_existing_file(tmp_path):
    path = tmp_path / "blocks.toml"
    created = agent_blocks.write_starter_config(path)

    assert created == path
    configured = agent_blocks.load_configured_pipelines(path)
    assert set(configured) == {"default", "code-change", "research", "review"}

    with pytest.raises(FileExistsError):
        agent_blocks.write_starter_config(path)
