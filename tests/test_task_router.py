from __future__ import annotations

from algo_cli import task_router


def test_route_question_stays_in_chat_mode():
    route = task_router.route_task("How do I read JSON in Python?")

    assert route.task_type == "question"
    assert route.recommended_mode == "chat"
    assert route.suggested_pipeline == "default"
    assert not task_router.should_suggest(route)


def test_route_coding_task_recommends_code_change_pipeline():
    route = task_router.route_task("Fix the failing auth test")

    assert route.task_type == "coding"
    assert route.recommended_mode == "agent"
    assert route.suggested_pipeline == "code-change"
    assert "write" in route.allowed_tool_groups
    assert task_router.should_suggest(route)


def test_route_review_task_wins_before_coding_terms():
    route = task_router.route_task("Review the implementation for bugs")

    assert route.task_type == "review"
    assert route.suggested_pipeline == "review"


def test_route_research_task_recommends_research_pipeline():
    route = task_router.route_task("Research the latest Ollama embedding models")

    assert route.task_type == "research"
    assert route.recommended_mode == "agent"
    assert route.suggested_pipeline == "research"


def test_route_high_risk_warns_without_agent_default():
    route = task_router.route_task("Delete the credential file after publishing")

    assert route.task_type == "sensitive"
    assert route.recommended_mode == "chat"
    assert route.risk == "high"
    assert task_router.should_suggest(route)


def test_route_sensitive_review_keeps_review_pipeline():
    route = task_router.route_task("Review auth.py for credential leaks")

    assert route.task_type == "review"
    assert route.suggested_pipeline == "review"
    assert route.risk == "high"


def test_route_remove_unused_import_is_not_high_risk():
    route = task_router.route_task("Remove the unused import in main.py")

    assert route.task_type == "coding"
    assert route.risk != "high"


def test_route_read_only_document_brief_uses_research_pipeline():
    route = task_router.route_task(
        "Research the policy source material. Read-only. Deliver sections 1-5. Do not publish changes."
    )

    assert route.task_type == "research"
    assert route.suggested_pipeline == "research"
