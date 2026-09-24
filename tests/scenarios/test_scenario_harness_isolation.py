"""The harness itself must keep every tool call inside the recorder and away from real services."""

from __future__ import annotations

import socket
import sys
import urllib.request

import pytest

from algo_cli import config as config_module
from algo_cli import jev_kernel, main, x_account
from algo_cli.x_account import XAccountResult
from scenarios.scenario_harness import ExternalCallBlocked, ScenarioRunner, text, tool


def test_scenario_approved_x_account_post_is_recorded_never_executed(scenario, external_guard):
    result = scenario.run(
        [[tool("x_account_post", text="hello from a scenario")], [text("Posted.")]],
        driver="interactive",
        approve=lambda *_args, **_kwargs: True,
        tools={"x_account_post": XAccountResult(True, "post", "posted (fake)").to_json()},
    )

    assert external_guard.violations == []
    assert result.invocations == [("x_account_post", {"text": "hello from a scenario"})]
    assert [name for name, _args in result.approvals] == ["x_account_post"]
    # The runtime cannot verify an external post from a fake, so it stays honest about the outcome.
    assert result.status_by_call == {"call-1": "unknown_outcome"}
    assert result.completed and result.history_well_formed


@pytest.mark.parametrize(
    "reach",
    [
        lambda: x_account._run_xurl(["post", "x"]),
        lambda: jev_kernel._invoke(None, "status", {}),
        lambda: urllib.request.urlopen("https://example.invalid/"),
        lambda: socket.create_connection(("192.0.2.1", 443), timeout=0.01),
    ],
    ids=["xurl", "typesafe", "http", "network"],
)
def test_scenario_guard_blocks_and_records_external_clients(external_guard, reach):
    with pytest.raises(ExternalCallBlocked):
        reach()

    assert len(external_guard.violations) == 1
    external_guard.violations.clear()  # observed; keep teardown clean


def test_scenario_repeated_runs_use_the_original_dispatcher(scenario):
    (scenario.workspace / "notes.txt").write_text("real notes\n", encoding="utf-8")

    first = scenario.run(
        [[tool("read_file", path="notes.txt")], [tool("run_shell", command="make")], [text("Built.")]],
        tools={"read_file": "faked notes", "run_shell": "faked build"},
        approve=lambda *_args, **_kwargs: True,
        driver="interactive",
    )
    first_approvals = list(first.approvals)
    second = scenario.run(
        [[tool("read_file", path="notes.txt")], [tool("run_shell", command="make")], [text("Stopped.")]],
        real_tools={"read_file"},
        approval_mode="never",
    )

    def tool_contents(result):
        return [message["content"] for message in result.messages if message.get("role") == "tool"]

    assert "faked notes" in tool_contents(first)[0]
    assert "real notes" in tool_contents(second)[0]
    assert first.status_by_call["call-2"] == "ok"
    # The one-shot run denies protected tools; the first run's approving callback must not decide for it.
    assert second.status_by_call["call-2"] != "ok"
    assert first.approvals == first_approvals
    assert [(name, args["path"]) for name, args in second.invocations] == [("read_file", "notes.txt")]


def test_scenario_agent_pipeline_tool_calls_are_recorded_and_faked(scenario):
    (scenario.workspace / "app.py").write_text("REAL SOURCE\n", encoding="utf-8")

    result = scenario.run_agent(
        [
            [tool("read_file", path="app.py")],
            [text("## Block Output\nOne finding.")],
            [text("## Block Output\nShip it.")],
        ],
        tools={"read_file": "FAKE SOURCE"},
        approve=lambda *_args, **_kwargs: True,
    )

    assert result.completed
    assert [(name, args["path"]) for name, args in result.invocations] == [("read_file", "app.py")]
    assert [name for name, _args in result.approvals] == ["read_file"]
    assert [request.name for request in result.tool_calls] == ["read_file"]
    tool_messages = [message for message in result.messages if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "FAKE SOURCE" in tool_messages[0]["content"]
    assert "REAL SOURCE" not in tool_messages[0]["content"]
    assert result.status_by_call == {"call-1": "ok"}
    assert result.history_well_formed and result.wasted_calls == 0


def test_scenario_guard_blocks_child_processes_from_real_shell(scenario, external_guard):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    child = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=2); print('connected')"
    try:
        result = scenario.run(
            [[tool("run_shell", command=f'"{sys.executable}" -c "{child}"')], [text("Ran it.")]],
            driver="interactive",
            real_tools={"run_shell"},
            approve=lambda *_args, **_kwargs: True,
            config={"safe_mode": False},
        )
        accepted = 0
        try:
            listener.accept()[0].close()
            accepted = 1
        except BlockingIOError:
            pass
    finally:
        listener.close()

    assert accepted == 0
    assert result.status_by_call["call-1"] != "ok"
    assert [violation.split(":")[0] for violation in external_guard.violations] == ["process"]
    external_guard.violations.clear()  # observed; keep teardown clean


def test_scenario_guard_lets_an_allowed_test_launch_child_processes(scenario, external_guard):
    scenario.allow_external("process")
    result = scenario.run(
        [[tool("run_shell", command=f'"{sys.executable}" -c "print(41 + 1)"')], [text("Ran it.")]],
        driver="interactive",
        real_tools={"run_shell"},
        approve=lambda *_args, **_kwargs: True,
    )

    assert result.status_by_call["call-1"] == "ok"
    assert "42" in next(message["content"] for message in result.messages if message.get("role") == "tool")
    assert external_guard.violations == []


def test_scenario_test_patch_after_a_run_does_not_leak_the_run_wrapper(tmp_path, external_guard):
    # Mirrors fixture teardown: the scenario fixture closes the runner before the test's monkeypatch undoes.
    workspace = tmp_path / "leak-workspace"
    workspace.mkdir()
    originals = {
        "ask_approval": main.ask_approval,
        "run_tool": main.run_tool,
        "save": config_module.Config.__dict__["save"],
        "load": config_module.Config.__dict__["load"],
    }
    monkeypatch = pytest.MonkeyPatch()
    runner = ScenarioRunner(monkeypatch, workspace, "leak-probe", guard=external_guard)
    try:
        runner.run([[text("Done.")]], driver="interactive", approve=lambda *_args, **_kwargs: True)
        monkeypatch.setattr(main, "ask_approval", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(config_module.Config, "save", lambda _self: None)
    finally:
        runner.close()
        monkeypatch.undo()

    assert main.ask_approval is originals["ask_approval"]
    assert main.run_tool is originals["run_tool"]
    assert config_module.Config.__dict__["save"] is originals["save"]
    assert config_module.Config.__dict__["load"] is originals["load"]


def test_scenario_later_run_does_not_clobber_a_test_patch_made_between_runs(scenario, monkeypatch):
    scenario.run([[text("Done.")]], driver="interactive")
    shown: list[str] = []
    monkeypatch.setattr(main, "show_error", lambda *args, **_kwargs: shown.append(str(args[0])))
    scenario.run([[text("Done.")]])  # one-shot never patches show_error

    main.show_error("still mine")
    assert shown == ["still mine"]
