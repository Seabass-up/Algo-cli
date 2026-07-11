from __future__ import annotations

from algo_cli import main as main_module
from algo_cli import slash_dispatch
from algo_cli import tools
from algo_cli.config import Config


class _RecordingConsole:
    def __init__(self) -> None:
        self.values: list[str] = []

    def print(self, value: object = "") -> None:
        self.values.append(str(value))


def test_help_and_memories_dispatch_to_the_display_module(monkeypatch):
    cfg = Config()
    cfg.memories = ["Prefer bounded queues."]
    calls: list[object] = []
    monkeypatch.setattr(slash_dispatch.display, "show_help", lambda: calls.append("help"))
    monkeypatch.setattr(
        slash_dispatch.display,
        "show_memory",
        lambda facts: calls.append(list(facts)),
    )

    help_handled, _client = main_module.handle_command("/help", cfg, None)
    memories_handled, _client = main_module.handle_command("/memories", cfg, None)

    assert help_handled is True
    assert memories_handled is True
    assert calls == ["help", ["Prefer bounded queues."]]


def test_perf_and_metrics_dispatch_to_the_telemetry_module(monkeypatch, tmp_path):
    cfg = Config()
    history_path = tmp_path / "perf_history.jsonl"
    history_path.write_text("{}\n", encoding="utf-8")
    summaries: list[str] = []
    infos: list[str] = []
    monkeypatch.setattr(
        slash_dispatch.perf_telemetry,
        "show_perf_summary",
        lambda: summaries.append("shown"),
    )
    monkeypatch.setattr(slash_dispatch.perf_telemetry, "PERF_HISTORY_FILE", history_path)
    monkeypatch.setattr(main_module, "show_info", infos.append)
    main_module.RUNTIME_STATUS["last_metrics"] = {"total_duration": 1}
    main_module.RUNTIME_STATUS["last_tool_metrics"] = {"duration_ms": 1}

    perf_handled, _client = main_module.handle_command("/perf", cfg, None)
    metrics_handled, _client = main_module.handle_command("/metrics reset", cfg, None)

    assert perf_handled is True
    assert metrics_handled is True
    assert summaries == ["shown"]
    assert not history_path.exists()
    assert "last_metrics" not in main_module.RUNTIME_STATUS
    assert "last_tool_metrics" not in main_module.RUNTIME_STATUS
    assert infos == ["Performance metrics reset."]


def test_theme_listing_uses_the_display_theme_registry(monkeypatch):
    cfg = Config()
    console = _RecordingConsole()
    infos: list[str] = []
    monkeypatch.setattr(slash_dispatch.display, "available_themes", lambda: ["one", "two"])
    monkeypatch.setattr(main_module, "console", console)
    monkeypatch.setattr(main_module, "show_info", infos.append)

    handled, _client = main_module.handle_command("/theme", cfg, None)

    assert handled is True
    assert infos == [f"Current theme: {cfg.theme}"]
    assert console.values == ["Available themes: one, two"]


def test_numeric_slash_commands_accept_their_distinct_value_types(monkeypatch):
    cfg = Config()
    monkeypatch.setattr(cfg, "save", lambda: None)
    monkeypatch.setattr(main_module, "show_info", lambda _message: None)

    main_module.handle_command("/ctx 8192", cfg, None)
    main_module.handle_command("/temp 0.75", cfg, None)
    main_module.handle_command("/toolmax 12", cfg, None)
    main_module.handle_command("/thinkevery 3", cfg, None)

    assert cfg.num_ctx == 8192
    assert cfg.temperature == 0.75
    assert cfg.max_tool_iterations == 12
    assert cfg.tool_think_every == 3


def test_ls_and_read_keep_their_string_results_independent(monkeypatch):
    cfg = Config()
    console = _RecordingConsole()
    monkeypatch.setattr(main_module, "console", console)
    monkeypatch.setattr(tools, "list_directory", lambda *_args, **_kwargs: "directory-result")
    monkeypatch.setattr(tools, "read_file", lambda *_args, **_kwargs: "file-result")

    ls_handled, _client = main_module.handle_command("/ls .", cfg, None)
    read_handled, _client = main_module.handle_command("/read README.md", cfg, None)

    assert ls_handled is True
    assert read_handled is True
    assert console.values == ["directory-result", "file-result"]
