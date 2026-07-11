"""Tests for the /goal command loop (main.run_goal_loop)."""

from algo_cli import main
from algo_cli.config import Config


def test_parse_goal_args_defaults():
    assert main.parse_goal_args("ship it") == ("ship it", main.GOAL_DEFAULT_MAX_ROUNDS)


def test_parse_goal_args_rounds_flag():
    assert main.parse_goal_args("--rounds 3 fix the tests") == ("fix the tests", 3)


def test_parse_goal_args_bad_rounds_ignored():
    task, rounds = main.parse_goal_args("--rounds nope fix")
    assert task == "fix"
    assert rounds == main.GOAL_DEFAULT_MAX_ROUNDS


def _run_goal(monkeypatch, replies, arg="do the thing"):
    """Drive run_goal_loop with a stubbed agent_loop returning canned replies."""
    cfg = Config()
    calls = []

    def fake_agent_loop(client, cfg_, prompt):
        calls.append(prompt)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        cfg_.messages.append({"role": "assistant", "content": reply})

    monkeypatch.setattr(main, "agent_loop", fake_agent_loop)
    main.run_goal_loop(client=None, cfg=cfg, arg=arg)
    return calls


def test_goal_stops_on_complete_marker(monkeypatch):
    calls = _run_goal(monkeypatch, ["working...", f"done. {main.GOAL_COMPLETE_MARKER}"])
    assert len(calls) == 2
    assert calls[0].startswith("GOAL: do the thing")


def test_goal_stops_on_blocked_marker(monkeypatch):
    calls = _run_goal(monkeypatch, [f"{main.GOAL_BLOCKED_MARKER} need credentials"])
    assert len(calls) == 1


def test_goal_respects_round_cap(monkeypatch):
    calls = _run_goal(monkeypatch, ["still going"], arg="--rounds 3 do the thing")
    assert len(calls) == 3


def test_goal_requires_task(monkeypatch):
    calls = _run_goal(monkeypatch, ["never called"], arg="")
    assert calls == []


def test_goal_persists_to_ledger(monkeypatch):
    from algo_cli import task_ledger

    _run_goal(monkeypatch, [f"done {main.GOAL_COMPLETE_MARKER}"], arg="ship it")
    record = task_ledger.load_goal()
    assert record is not None
    assert record.goal == "ship it"
    assert record.status == task_ledger.STATUS_COMPLETE
    assert record.rounds_done == 1


def test_goal_blocked_recorded(monkeypatch):
    from algo_cli import task_ledger

    _run_goal(monkeypatch, [f"{main.GOAL_BLOCKED_MARKER} need a key"], arg="do it")
    record = task_ledger.load_goal()
    assert record.status == task_ledger.STATUS_BLOCKED
    assert "need a key" in record.reason


def test_goal_resume_continues_from_saved(monkeypatch):
    from algo_cli import task_ledger
    from algo_cli.config import Config

    # First run stops at the round cap (model never marks complete).
    _run_goal(monkeypatch, ["still working"], arg="--rounds 2 big task")
    mid = task_ledger.load_goal()
    assert mid.rounds_done == 2
    assert mid.status == task_ledger.STATUS_STOPPED

    # Resume with 1 more round, this time completing.
    cfg = Config()
    calls = []

    def fake_agent_loop(client, cfg_, prompt):
        calls.append(prompt)
        cfg_.messages.append({"role": "assistant", "content": f"ok {main.GOAL_COMPLETE_MARKER}"})

    monkeypatch.setattr(main, "agent_loop", fake_agent_loop)
    main.run_goal_loop(client=None, cfg=cfg, arg="resume --rounds 1")
    after = task_ledger.load_goal()
    assert after.status == task_ledger.STATUS_COMPLETE
    assert after.rounds_done == 3
    assert len(calls) == 1


def test_goal_clear(monkeypatch):
    from algo_cli import task_ledger
    from algo_cli.config import Config

    _run_goal(monkeypatch, [f"done {main.GOAL_COMPLETE_MARKER}"], arg="x task")
    assert task_ledger.load_goal() is not None
    main.run_goal_loop(client=None, cfg=Config(), arg="clear")
    assert task_ledger.load_goal() is None
