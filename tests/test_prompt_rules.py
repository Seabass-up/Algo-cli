"""The injected prompt has one navigation rule and one summary rule."""

from algo_cli.config import DEFAULT_SYSTEM, LEGACY_DEFAULT_SYSTEM
from algo_cli.context_budget import SUMMARY_RULE
from algo_cli.harness import MERCURY_STOP_CONDITIONS_COMPACT
from algo_cli.identity import DEFAULT_SOUL
from algo_cli.prompt_rules import NAMED_PATH_RULE
from algo_cli.session_mode import prompt_section


def test_focused_shell_discovery_does_not_dump_unrelated_families() -> None:
    import json

    from algo_cli import tools

    raw = tools.available_actions("shell")
    payload = json.loads(raw)
    assert payload["topic"] == "shell"
    assert "run_shell" in payload["focused"]["model_callable_tools"]["shell"]
    assert "slash_registry" not in payload
    assert "reasoning_mode_guidance" not in payload
    assert "google" not in payload.get("focused", {}).get("commands", {})
    assert "/google" not in raw
    assert len(raw) < 8_000


def test_named_tool_outside_ceiling_is_not_reported_as_missing(tmp_path) -> None:
    import json
    import subprocess

    from algo_cli.config import Config
    from algo_cli.tools import action_search, git_status

    payload = json.loads(action_search("run_shell", limit=1, cfg=Config()))
    assert payload["match"] == "policy_blocked"
    assert payload["outside_ceiling"] == ["run_shell"]
    assert payload["count"] == 0
    assert "exists in the running package" in payload["next"]
    assert "not proof the launcher lacks the tool" in payload["next"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    status = git_status(cwd=str(tmp_path))
    assert status.startswith("runtime_source: ")
    assert f"\nworkdir: {tmp_path}\n" in status
    assert "not a git repository" not in status


def test_named_path_rule_is_the_only_navigation_instruction():
    surfaces = {
        "default_system": DEFAULT_SYSTEM,
        "legacy_system": LEGACY_DEFAULT_SYSTEM,
        "mercury": MERCURY_STOP_CONDITIONS_COMPACT,
        "execute": prompt_section("execute"),
    }
    forbidden = (
        "Required first step when files are named",
        "call session_slash /ls then session_slash /read",
        "Do not list directories merely to confirm named paths",
    )
    for name, text in surfaces.items():
        assert NAMED_PATH_RULE in text, name
        for phrase in forbidden:
            assert phrase not in text, (name, phrase)


def test_summary_rule_does_not_ask_for_a_per_tool_narration():
    assert SUMMARY_RULE in DEFAULT_SOUL
    assert "After a tool call, summarize the result before the next step." not in DEFAULT_SOUL
    assert SUMMARY_RULE == (
        "Do not narrate each tool call. Summarize outcomes in plain language after "
        "the work completes."
    )


def test_unknown_outcome_recovery_names_a_check_and_not_a_path():
    from algo_cli.context_budget import format_attempt_ledger_line
    from algo_cli.prompt_rules import UNKNOWN_OUTCOME_RECOVERY

    line = format_attempt_ledger_line(
        {
            "status": "unknown_outcome",
            "tool": "edit_file",
            "args_receipt": "hmac-sha256:" + "ab" * 32,
            "summary": "status=unknown_outcome; chars=1; bytes=1; digest=hmac-sha256:" + "cd" * 32,
        }
    )
    assert "recovery=read_and_compare" in line
    assert "src/" not in line
    assert "router.py" not in line
    assert "does not name the target" in UNKNOWN_OUTCOME_RECOVERY
    worked = format_attempt_ledger_line({"status": "worked", "tool": "read_file", "args_receipt": "x", "summary": "ok"})
    assert "recovery=" not in worked


def test_semantic_decision_rule_reaches_interactive_and_one_shot_prompts(monkeypatch):
    from algo_cli import context_budget
    from algo_cli.config import Config
    from algo_cli.prompt_rules import DECISION_VERIFICATION_RULE

    monkeypatch.setattr(context_budget, "_memory_prompt_section", lambda *a, **k: "")
    cfg = Config(continuum_enabled=False)
    for sink in (None, object()):
        monkeypatch.setattr(context_budget, "json_sink", lambda: sink)
        prompt = context_budget.build_system_prompt(cfg)
        assert prompt.count(DECISION_VERIFICATION_RULE) == 1
        assert "not factual correctness" in prompt
        assert "report it unverified" in prompt
