"""Evidence novelty, not successful invocation count, controls tool liveness."""

import pytest

from algo_cli.tool_progress import ToolProgressGuard


def observe(guard, name="read_file", args=None, result="unchanged", **kwargs):
    guard.observe(
        name, args or {"path": "trace.json"}, result,
        status=kwargs.pop("status", "succeeded"),
        invoked=kwargs.pop("invoked", True),
        observation=kwargs.pop("observation", True),
        **kwargs,
    )
    return guard.finish_batch()


def test_blocked_runner_is_not_cleared_by_successful_rereads(tmp_path):
    guard = ToolProgressGuard(str(tmp_path))
    observe(guard, "run_shell", {"command": "trace"}, "denied", status="denied", invoked=False)
    boundaries = [observe(guard) for _ in range(6)]
    assert [item.stop for item in boundaries if item is not None] == [False, True]
    assert "run_shell" in boundaries[-1].reason
    assert "no new evidence" in boundaries[-1].reason


@pytest.mark.parametrize("novelty", ["source", "range", "result", "failure_result"])
def test_new_evidence_does_not_exhaust_a_read_only_task(tmp_path, novelty):
    guard = ToolProgressGuard(str(tmp_path))
    for index in range(30):
        args = {"path": f"file-{index}.txt" if novelty == "source" else "trace.raw"}
        if novelty == "range":
            args["start_char"] = index * 100
        result = str(index) if "result" in novelty else "same text in distinct ranges"
        status = "failed" if novelty == "failure_result" else "succeeded"
        assert observe(guard, args=args, result=result, status=status) is None


def test_path_aliases_and_larger_limits_do_not_hide_repetition(tmp_path):
    guard = ToolProgressGuard(str(tmp_path))
    boundary = None
    for index in range(6):
        boundary = observe(
            guard,
            args={"path": "./trace.json" if index % 2 else str(tmp_path / "trace.json"),
                  "max_chars": 1000 + index * 100, "limit": index + 1},
        )
    assert boundary is not None and boundary.stop


def test_rotating_files_with_occasional_new_reads_still_stops(tmp_path):
    guard = ToolProgressGuard(str(tmp_path))
    boundary = None
    for index in range(30):
        path = f"new-{index}.txt" if index % 6 == 0 else f"repeat-{index % 2}.txt"
        boundary = observe(guard, args={"path": path})
        if boundary is not None and boundary.stop:
            break
    assert boundary is not None and boundary.stop
    assert index < 20


def test_real_effect_resets_evidence_for_verification_but_deduplication_does_not(tmp_path):
    guard = ToolProgressGuard(str(tmp_path))
    for _ in range(4):
        observe(guard)
    assert observe(guard, "write_file", {"path": "trace.json"}, "Wrote", observation=False) is None
    assert observe(guard) is None
    for _ in range(3):
        observe(guard)
    observe(guard, "write_file", {"path": "trace.json"}, "Wrote", observation=False,
            invoked=False, deduplicated=True)
    boundary = observe(guard)
    assert boundary is not None and boundary.stop


def test_new_evidence_after_warning_recovers_without_a_stale_warning(tmp_path):
    guard = ToolProgressGuard(str(tmp_path))
    for _ in range(4):
        observe(guard)
    assert observe(guard, result="changed") is None
    assert observe(guard, result="changed again") is None
    assert observe(guard, result="another change") is None


@pytest.mark.parametrize("name", ["search_files", "action_search"])
def test_reworded_searches_with_identical_evidence_do_not_evade_the_guard(tmp_path, name):
    import json

    guard = ToolProgressGuard(str(tmp_path))
    boundary = None
    for index in range(6):
        query = f"another spelling {index}"
        result = json.dumps({"query": query, "actions": []}) if name == "action_search" else "same match"
        boundary = observe(guard, name, {"path": ".", "query": query, "pattern": query}, result)
    assert boundary is not None and boundary.stop


def test_new_route_clears_a_warning_even_with_a_mostly_repeated_history(tmp_path):
    guard = ToolProgressGuard(str(tmp_path))
    boundary = None
    # Several reads in a batch can make the recent window repetitive while a
    # small amount of fresh evidence keeps the batch useful.
    for index in range(4):
        for _ in range(3):
            guard.observe("read_file", {"path": "same"}, "same", status="succeeded",
                          invoked=True, observation=True)
        guard.observe("read_file", {"path": f"new-{index}"}, "fresh", status="succeeded",
                      invoked=True, observation=True)
        boundary = guard.finish_batch()
        if boundary is not None:
            break
    assert boundary is not None and not boundary.stop
    assert "another permitted route" in boundary.prompt(["action_search"])
    assert "action_search" in boundary.prompt(["action_search"])
    for index in range(20):
        assert observe(guard, "harness_read", {"id": f"different-source-{index}"}, "new evidence") is None
