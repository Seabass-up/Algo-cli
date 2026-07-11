from __future__ import annotations

from algo_cli import spawn_budget, task_router


def test_chat_route_has_no_agent_budget():
    budget = spawn_budget.compute_budget(task_router.route_task("How do I load JSON?"))

    assert budget.max_blocks == 0
    assert budget.parallelism == 0


def test_review_route_matches_two_block_pipeline():
    budget = spawn_budget.compute_budget(task_router.route_task("Review auth.py for bugs"))

    assert budget.max_blocks == 2
    assert budget.max_iterations_per_block == 8
    assert budget.parallelism == 0


def test_coding_route_matches_code_change_pipeline():
    budget = spawn_budget.compute_budget(task_router.route_task("Fix the failing auth test"))

    assert budget.max_blocks == 4
    assert budget.parallelism == 0


def test_research_comparison_marks_parallelism_optional():
    prompt = "Compare FastAPI, Litestar, and BlackSheep for async REST APIs"
    budget = spawn_budget.compute_budget(task_router.route_task(prompt), prompt)

    assert budget.max_blocks == 3
    assert budget.parallelism == 1
    assert spawn_budget.parallelism_label(budget.parallelism) == "optional"


def test_high_risk_route_does_not_recommend_expansion():
    prompt = "Review credential deletion logic for security issues"
    budget = spawn_budget.compute_budget(task_router.route_task(prompt), prompt)

    assert budget.max_blocks == 0
    assert budget.max_iterations_per_block == 0
    assert "user-directed" in budget.reasons[0]
