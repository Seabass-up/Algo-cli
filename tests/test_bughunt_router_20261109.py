from __future__ import annotations

from types import SimpleNamespace

import pytest

from algo_cli import deliberation, decision_checkpoints, julia_memory_runtime, reflex, session_commands, task_router
from algo_cli.config import Config


@pytest.mark.parametrize(
    "prompt",
    [
        "How does the address parser handle empty input?",
        "What does the update function do?",
        "What is the prefix of this path?",
        "Explain padding in CSS",
    ],
)
def test_substring_terms_do_not_route_plain_questions_as_mutating_coding(prompt):
    route = task_router.route_task(prompt)

    assert route.task_type != "coding"
    assert route.mutation_intent is False
    assert route.suggested_pipeline != "code-change"


@pytest.mark.parametrize(
    "prompt",
    [
        "add a retry to the fetch function",
        "fix the failing test in tests/test_x.py",
        "update the README to mention uv",
        "fixing the parser bug",
        "Why is the build failing? Fix it.",
    ],
)
def test_real_change_requests_still_route_as_coding_with_mutation_intent(prompt):
    route = task_router.route_task(prompt)

    assert route.task_type == "coding"
    assert route.suggested_pipeline == "code-change"
    assert route.mutation_intent is True


def test_has_any_matches_whole_words_and_phrases_only():
    assert task_router._has_any("add a retry", ("add ",))
    assert not task_router._has_any("address the issue", ("add ",))
    assert not task_router._has_any("the padding", ("add",))
    assert task_router._has_any("fix: broken", ("fix",))
    assert not task_router._has_any("prefix", ("fix",))
    assert task_router._has_any("deliver: sections", ("deliver:",))


def test_deliberation_cue_does_not_match_inside_longer_word():
    assert deliberation.needs_deliberation("Please improve that function") is False
    assert deliberation.needs_deliberation("Prove that the loop terminates") is True


@pytest.mark.parametrize(
    "text",
    ["reviewing the module", "auditing code", "debugging the loop", "scanning files", "investigating the crash",
     "the investigation found", "reviews of the code", "debugged it", "scans the tree", "reveiw the diff"],
)
def test_review_regex_matches_inflections(text):
    assert decision_checkpoints._REVIEW_RE.search(text)


def test_review_regex_does_not_match_unrelated_words():
    assert decision_checkpoints._REVIEW_RE.search("a scandal in the scandal sheet") is None


def test_reset_error_retries_clears_only_error_retry_keys():
    cfg = SimpleNamespace(context_state={"reflex_cycles": 2})
    reflex._increment_reflex_error_retry(cfg, "read_file", {"path": "a.txt"})
    reflex._increment_reflex_error_retry(cfg, "search_files", {"pattern": "x"})

    reflex.reset_error_retries(cfg)

    assert cfg.context_state == {"reflex_cycles": 2}


def test_begin_agent_pipeline_clears_error_retries_per_run():
    cfg = SimpleNamespace(context_state={})
    reflex._increment_reflex_error_retry(cfg, "read_file", {"path": "a.txt"})

    reflex.begin_agent_pipeline(cfg)

    assert not any(str(key).startswith("('reflex_error_retries'") for key in cfg.context_state)


def test_empty_slash_command_returns_empty_command_message(tmp_path):
    cfg = Config(cwd=str(tmp_path))

    for raw in ("", "   "):
        result = session_commands.execute(raw, cfg)
        assert result == "Error: empty command. Allowed: /read, /ls, /cd, /cwd"


def test_memory_add_pinned_tier_is_case_insensitive_and_points_to_remember():
    with pytest.raises(julia_memory_runtime.MemorySystemError, match="Use /remember"):
        julia_memory_runtime._parse_add(["--tier", "PINNED", "some", "text"])
