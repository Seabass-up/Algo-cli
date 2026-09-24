# End-to-end scenarios

Unit tests check one function at a time. Some defects show up only across a whole
interaction: a Ctrl+C that leaves a tool call without a result, a cleanup step that hides a
provider error, a denied call that the model retries forever. The scenarios in this directory
run complete turns through the real agent loop, or a full `/agent` pipeline, with a scripted
model. Everything else is real: dispatch, policy, approval, outcome classification and history.

The scenarios are deterministic and offline, and the whole directory runs in a few seconds.
They never touch the real `~/.algo_cli`. `tests/conftest.py` points the config directory at a
temporary directory, and `conftest.py` here redirects `HOME`/`USERPROFILE`, blocks live
keychain receipt stores, and checks before and after each test that the config directory is
outside the real one. `Config.save()` is recorded, not written.

## Running

```bash
.venv/bin/pytest -q tests/scenarios            # just the scenarios
.venv/bin/pytest -q tests -k scenario -s       # also prints the metrics table
```

A metrics table is printed at the end of any run that executed a scenario. It has one row per
run and a totals line (tasks completed, wasted calls, recovery turns).

## Writing a scenario

A scenario is a script of model rounds. Each round is a list of items, streamed in order:

| Item | Meaning |
|---|---|
| `text("...")` | streamed answer text |
| `thinking("...")` | streamed reasoning |
| `tool("read_file", path="x")` | a tool call; adjacent calls form one parallel batch (`id=` pins the call id) |
| `interrupt()` | Ctrl+C at this point in the stream |
| `fail(ConnectionError("down"))` | the provider raises here |

Rounds after the script ends answer `"Done."`. You can also pass a callable
`round_number -> round`.

```python
from scenarios.scenario_harness import text, tool


def test_scenario_my_case(scenario):
    result = scenario.run(
        [[tool("read_file", path="README.md")], [text("It is a demo.")]],
        tools={"read_file": "# Demo"},       # fake result: str, exception, or callable(args)
    )
    assert result.completed and result.history_well_formed
```

`scenario.run(...)` options:

- `driver="oneshot"` (default) runs `run_oneshot` and records its JSON events.
  `driver="interactive"` calls `agent_loop` as the REPL does and records display lines in
  `result.display_text`. Approval prompts are declined unless you pass `approve=`.
- `tools={name: fake}` replaces tool bodies. `real_tools={"read_file"}` runs the real tool
  inside the scenario workspace (`scenario.workspace`). Policy and approval always run for real.
- `approval_mode="auto" | "never"` (one-shot), `approve=callable` (records every prompt in
  `result.approvals`), `save_error=` (make `Config.save()` raise), `config={...}` overrides.

`scenario.run_agent(rounds, task=..., pipeline_name="review")` runs a full pipeline. Each block
takes model rounds in order, and `result.pipeline` and `result.thread` hold the outcome and the
persisted thread record.

## What a result exposes

`completed`, `final_answer`, `tool_calls` (what the model requested), `invocations` (what
actually ran), `tool_results` / `status_by_call`, `denials`, `skips`, `identical_repeats`,
`denied_retries`, `wasted_calls`, `recovery_turns` (model rounds after the first non-ok tool
outcome), `unpaired_tool_calls` / `history_well_formed`, `cancel_latency` (seconds from the
injected Ctrl+C to the end of the run), `events`, `display_text`, `messages`, `model.calls`.

To inject Ctrl+C somewhere other than the model stream, such as inside a tool or while waiting on
a parallel batch, raise `KeyboardInterrupt` there and call `scenario.clock.mark_interrupt()`
first. That lets the harness measure latency (see
`test_scenario_ctrl_c_during_parallel_tools_bounds_the_wait`).

## Conventions

- Name files and tests `test_scenario_*` so `-k scenario` selects them. This directory is a
  package (`__init__.py`) so its `conftest.py` cannot shadow `tests/conftest.py`; import the
  harness as `scenarios.scenario_harness`.
- Assert what the user and the model see: statuses, the text in tool messages, done events,
  history pairing. Don't assert private internals.
- If you are asserting a behavior an open PR will change, put it in a future-behavior file
  (for example `test_scenario_pending_pr<N>.py`) with `xfail(strict=False, reason=...)`. It then
  flips to XPASS when the PR lands, and you remove the marker. A defect the harness finds but
  cannot fix yet gets `xfail(strict=True)` with the cause in `reason`, so the fix has to remove
  the marker.
- Platform-specific behavior must be simulated with fixtures so it runs on macOS as well as
  in CI.
