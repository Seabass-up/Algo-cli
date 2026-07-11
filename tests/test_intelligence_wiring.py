from __future__ import annotations

from algo_cli import tool_runtime
from algo_cli.config import Config
from algo_cli import main as main_module
from algo_cli import slash_dispatch
from algo_cli import session_commands
from algo_cli import tools


def test_intelligence_command_is_listed():
    commands = {command for command, _description in slash_dispatch.SLASH_COMMANDS}

    assert "/intelligence" in commands
    assert "/intel" in commands
    assert "/intelagence" in commands


def test_intelligence_command_reports_importable_runtime_exports(monkeypatch):
    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())

    handled, _client = main_module.handle_command("/intelligence", Config(), None)

    assert handled is True
    joined = "\n".join(printed)
    assert "Intelligence layer" in joined
    assert "commands" in joined
    assert "build_project_graph" in joined
    assert "GraphRAGIndex" in joined


def test_intelligence_query_runs_project_graph_from_runtime(monkeypatch, tmp_path):
    (tmp_path / "alpha.py").write_text(
        "class Alpha:\n"
        "    def beta(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    cfg = Config()
    cfg.cwd = str(tmp_path)
    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())

    handled, _client = main_module.handle_command("/intelagence query Alpha", cfg, None)

    assert handled is True
    joined = "\n".join(printed)
    assert "Intelligence query" in joined
    assert "alpha.py" in joined
    assert "Alpha" in joined


def test_intel_alias_query_runs_project_graph_from_runtime(monkeypatch, tmp_path):
    (tmp_path / "alpha.py").write_text("class Alpha:\n    pass\n", encoding="utf-8")
    cfg = Config()
    cfg.cwd = str(tmp_path)
    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())

    handled, _client = main_module.handle_command("/intel query Alpha", cfg, None)

    assert handled is True
    joined = "\n".join(printed)
    assert "Intelligence query" in joined
    assert "alpha.py" in joined


def test_intelligence_is_available_to_agent_runtime():
    from algo_cli.action_registry import get_action_spec

    out = tools.available_actions("slash")
    catalog = session_commands.catalog_for_prompt()

    assert "/intelligence status|query TERM|reindex" in out
    assert "/intel status|query TERM|reindex" in out
    assert "/intelagence" in out
    assert "/intel query TERM" in catalog
    assert tool_runtime.session_command_requires_approval("/intelligence status") is False
    assert tool_runtime.session_command_requires_approval("/intel query Alpha") is False
    assert tool_runtime.session_command_requires_approval("/intel reindex") is True
    assert tool_runtime.session_command_requires_approval("/intelagence query Alpha") is False
    assert tool_runtime.session_command_requires_approval("/intelligence reindex") is True
    assert get_action_spec("/intelligence").kind == "slash"
    assert get_action_spec("/intel").replacement == "/intelligence"
