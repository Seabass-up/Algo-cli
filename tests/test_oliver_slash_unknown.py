from __future__ import annotations

from algo_cli.config import Config
from algo_cli import main as main_module
from algo_cli import oliver_slash_dispatch as slash_dispatch
from algo_cli import tools
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document


def test_unknown_slash_command_not_handled():
    cfg = Config()
    handled, _client = main_module.handle_command("/not-a-real-command", cfg, None)
    assert handled is False


def test_unknown_slash_command_message_suggests_near_match():
    message = slash_dispatch.unknown_command_message("/inteligence query Alpha")

    assert "Unknown command: /inteligence" in message
    assert "Did you mean /intelligence?" in message
    assert "Use /help" in message


def test_multiword_slash_completion_replaces_the_full_partial_input():
    completer = slash_dispatch.SlashCommandCompleter(slash_dispatch.SLASH_COMMANDS)
    text = "/harness r"

    completions = list(
        completer.get_completions(
            Document(text=text, cursor_position=len(text)),
            CompleteEvent(),
        )
    )

    refresh = next(item for item in completions if item.text == "/harness refresh")
    completed = text[: len(text) + refresh.start_position] + refresh.text
    assert completed == "/harness refresh"


def test_slash_completion_does_not_replace_command_arguments():
    completer = slash_dispatch.SlashCommandCompleter(slash_dispatch.SLASH_COMMANDS)
    text = "/hsearch memory"

    completions = list(
        completer.get_completions(
            Document(text=text, cursor_position=len(text)),
            CompleteEvent(),
        )
    )

    assert completions == []


def test_harness_short_alias_runs_search(monkeypatch):
    cfg = Config()
    searches: list[str] = []
    monkeypatch.setattr(slash_dispatch.harness, "index_is_stale", lambda: False)
    monkeypatch.setattr(main_module, "print_harness_results", lambda query, cfg=None: searches.append(query))

    handled, _client = main_module.handle_command("/hs memory recall", cfg, None)

    assert handled is True
    assert searches == ["memory recall"]


def test_harness_read_short_alias_reads_record(monkeypatch):
    cfg = Config()
    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())
    monkeypatch.setattr(tools, "harness_read", lambda record_id: f"read:{record_id}")

    handled, _client = main_module.handle_command("/hr algo-cli:skill:qol", cfg, None)

    assert handled is True
    assert printed == ["read:algo-cli:skill:qol"]


def test_unknown_harness_subcommand_suggests_fix_instead_of_showing_stats(monkeypatch):
    cfg = Config()
    errors: list[str] = []
    monkeypatch.setattr(main_module, "show_error", errors.append)
    monkeypatch.setattr(tools, "harness_stats", lambda: (_ for _ in ()).throw(AssertionError("must not run")))

    handled, _client = main_module.handle_command("/harness refres", cfg, None)

    assert handled is True
    assert len(errors) == 1
    assert "Unknown /harness subcommand: refres" in errors[0]
    assert "Did you mean refresh?" in errors[0]
    assert "/hs" in errors[0]


def test_harness_subcommand_rejects_unexpected_arguments(monkeypatch):
    cfg = Config()
    errors: list[str] = []
    monkeypatch.setattr(main_module, "show_error", errors.append)
    monkeypatch.setattr(tools, "harness_refresh", lambda: (_ for _ in ()).throw(AssertionError("must not run")))

    handled, _client = main_module.handle_command("/harness refresh extra", cfg, None)

    assert handled is True
    assert len(errors) == 1
    assert "Unexpected arguments for /harness refresh: extra" in errors[0]


def test_session_command_reports_unknown_slash_with_suggestion():
    result = tools.session_command("/sttaus", Config())

    assert "Unknown command: /sttaus" in result
    assert "Did you mean /status?" in result


def test_mode_command_handled_when_present():
    cfg = Config()
    handled, _client = main_module.handle_command("/mode status", cfg, None)
    assert handled is True


def test_load_canonicalizes_name_before_config_load(monkeypatch):
    cfg = Config()
    called_with: list[str] = []
    infos: list[str] = []
    errors: list[str] = []

    def fake_load(name: str) -> int:
        called_with.append(name)
        return 0

    cfg.load_conversation = fake_load
    monkeypatch.setattr(main_module, "show_info", lambda message: infos.append(str(message)))
    monkeypatch.setattr(main_module, "show_error", lambda message: errors.append(str(message)))

    handled, _client = main_module.handle_command("/load my../session", cfg, None)

    assert handled is True
    assert called_with == ["mysession"]
    assert any("canonical" in message.lower() and "mysession" in message for message in infos)
    assert errors == []


def test_save_reports_canonical_name_when_name_is_normalized(monkeypatch, tmp_path):
    cfg = Config()
    infos: list[str] = []

    def fake_save(name: str):
        assert name == "mysession"
        return tmp_path / "mysession.json"

    cfg.save_conversation = fake_save
    monkeypatch.setattr(main_module, "show_info", lambda message: infos.append(str(message)))

    handled, _client = main_module.handle_command("/save my../session", cfg, None)

    assert handled is True
    assert any("canonical" in message.lower() and "mysession" in message for message in infos)


def test_selfcheck_surfaces_action_registry_runtime_audit(monkeypatch):
    cfg = Config()
    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())
    monkeypatch.setattr(tools, "harness_stats", lambda: "{}")
    monkeypatch.setattr(tools, "available_actions", lambda _topic=None: "{}")
    monkeypatch.setattr(tools, "harness_search", lambda **_kwargs: "No harness matches.")

    handled, _client = main_module.handle_command("/selfcheck", cfg, None)

    assert handled is True
    joined = "\n".join(printed)
    assert "Action registry runtime audit" in joined
    assert "Kernel audit:" in joined
    assert "0 blocked" in joined
    assert "Runtime quality diagnostics" in joined
    assert "reasoning quality: not_collected" in joined
    assert "READY" in joined


def test_harness_status_alias_prints_harness_stats(monkeypatch):
    cfg = Config()
    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())
    monkeypatch.setattr(tools, "harness_stats", lambda: '{"quality": {"status": "ready"}}')

    handled, _client = main_module.handle_command("/harness status", cfg, None)

    assert handled is True
    assert printed == ['{"quality": {"status": "ready"}}']


def test_harness_score_prints_harness_scorecard(monkeypatch):
    cfg = Config()
    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())
    monkeypatch.setattr(tools, "harness_scorecard", lambda: '{"score": 10}')

    handled, _client = main_module.handle_command("/harness score", cfg, None)

    assert handled is True
    assert printed == ['{"score": 10}']


def test_harness_compare_prints_competitive_rating(monkeypatch):
    cfg = Config()
    printed: list[str] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(str(value))

    monkeypatch.setattr(main_module, "console", _Console())
    monkeypatch.setattr(tools, "harness_competitive_rating", lambda: '{"claim": "blocked"}')

    handled, _client = main_module.handle_command("/harness compare", cfg, None)

    assert handled is True
    assert printed == ['{"claim": "blocked"}']


def test_harness_status_and_embed_are_listed_slash_commands():
    commands = {command for command, _description in slash_dispatch.SLASH_COMMANDS}

    assert "/harness status" in commands
    assert "/harness embed" in commands
    assert "/harness score" in commands
    assert "/harness compare" in commands


def test_every_registered_slash_root_has_dispatch_or_valid_alias():
    from algo_cli.action_registry import _declared_dispatch_commands

    roots = {command.split()[0] for command, _description in slash_dispatch.SLASH_COMMANDS}
    dispatched = _declared_dispatch_commands()

    assert roots - dispatched - set(slash_dispatch.SLASH_COMMAND_ALIASES) == set()
    assert all(
        source in roots and target in dispatched
        for source, target in slash_dispatch.SLASH_COMMAND_ALIASES.items()
    )


def test_stale_dashboard_back_command_is_not_advertised():
    commands = {command for command, _description in slash_dispatch.SLASH_COMMANDS}

    assert "/back" not in commands


def test_forget_rejects_zero_and_negative_indexes(monkeypatch):
    cfg = Config()
    errors: list[str] = []
    calls: list[int] = []
    monkeypatch.setattr(cfg, "forget_memory_index", lambda index: calls.append(index) or "removed")
    monkeypatch.setattr(main_module, "show_error", errors.append)

    main_module.handle_command("/forget 0", cfg, None)
    main_module.handle_command("/forget -1", cfg, None)

    assert calls == []
    assert errors == ["Usage: /forget <number>", "Usage: /forget <number>"]


def test_memory_auto_command_reports_and_persists_explicit_state(monkeypatch):
    cfg = Config(
        memory_auto_daily_limit=500,
        memory_auto_entry_limit=5_000,
        memory_auto_char_limit=5_000_000,
    )
    infos: list[str] = []
    errors: list[str] = []
    saves: list[bool] = []
    monkeypatch.setattr(cfg, "save", lambda: saves.append(cfg.memory_auto_capture_enabled))
    monkeypatch.setattr(main_module, "show_info", infos.append)
    monkeypatch.setattr(main_module, "show_error", errors.append)

    main_module.handle_command("/memory-auto status", cfg, None)
    main_module.handle_command("/memory-auto off", cfg, None)
    main_module.handle_command("/memory-auto on", cfg, None)
    main_module.handle_command("/memory-auto maybe", cfg, None)

    assert "Automatic memory capture: ON" in infos[0]
    assert "daily limit 5" in infos[0]
    assert "fingerprint cap 64" in infos[0]
    assert "memory budget 12000 chars" in infos[0]
    assert saves == [False, True]
    assert cfg.memory_auto_capture_enabled is True
    assert errors == ["Usage: /memory-auto [on|off|status]"]


def test_numeric_runtime_commands_reject_unsafe_or_non_finite_values(monkeypatch):
    cfg = Config()
    original = (cfg.num_ctx, cfg.temperature, cfg.max_tool_iterations, cfg.tool_think_every)
    errors: list[str] = []
    monkeypatch.setattr(main_module, "show_error", errors.append)
    monkeypatch.setattr(main_module, "show_info", lambda _message: None)

    main_module.handle_command("/ctx 1", cfg, None)
    main_module.handle_command("/temp nan", cfg, None)
    main_module.handle_command("/toolmax 1000000", cfg, None)
    main_module.handle_command("/thinkevery 0", cfg, None)

    assert (cfg.num_ctx, cfg.temperature, cfg.max_tool_iterations, cfg.tool_think_every) == original
    assert len(errors) == 4


def test_reasoning_bounds_and_explicit_toggles_reject_invalid_values(monkeypatch):
    cfg = Config()
    original = (
        cfg.reasoning_depth,
        cfg.reasoning_branches,
        cfg.reasoning_auto_reflexion,
        cfg.reasoning_auto_verify,
        cfg.reasoning_chat_enabled,
    )
    errors: list[str] = []
    monkeypatch.setattr(main_module, "show_error", errors.append)

    main_module.handle_command("/reason depth 999", cfg, None)
    main_module.handle_command("/reason branches 999", cfg, None)
    main_module.handle_command("/reason auto-reflexion maybe", cfg, None)
    main_module.handle_command("/reason auto-verify maybe", cfg, None)
    main_module.handle_command("/reason chat maybe", cfg, None)

    assert (
        cfg.reasoning_depth,
        cfg.reasoning_branches,
        cfg.reasoning_auto_reflexion,
        cfg.reasoning_auto_verify,
        cfg.reasoning_chat_enabled,
    ) == original
    assert len(errors) == 5


def test_invalid_knowledge_subcommands_report_usage(monkeypatch):
    cfg = Config()
    errors: list[str] = []
    monkeypatch.setattr(main_module, "show_error", errors.append)

    main_module.handle_command("/lessons wat", cfg, None)
    main_module.handle_command("/skills wat", cfg, None)
    main_module.handle_command("/reflex wat", cfg, None)

    assert errors == [
        "Usage: /lessons [status|reindex]",
        "Usage: /skills [status|crystallize|approve NAME|reject NAME|on|off]",
        "Usage: /reflex [on|off|status|reset]",
    ]


def test_credentials_get_preserves_key_case(monkeypatch):
    from algo_cli import credential_helpers

    cfg = Config()
    requests: list[tuple[str, str]] = []
    infos: list[str] = []
    monkeypatch.setattr(
        credential_helpers,
        "get_credential",
        lambda helper, key: requests.append((helper, key)) or "secret-value",
    )
    monkeypatch.setattr(main_module, "show_info", infos.append)

    handled, _client = main_module.handle_command(
        "/credentials get fake-helper MixedCaseKey",
        cfg,
        None,
    )

    assert handled is True
    assert requests == [("fake-helper", "MixedCaseKey")]
    assert infos == ["fake-helper/MixedCaseKey: configured (value redacted)"]
    assert "secret-value" not in "\n".join(infos)


def test_plugins_list_handles_manifest_objects(monkeypatch):
    from algo_cli import william_plugins as plugins

    cfg = Config()
    printed: list[object] = []

    class _Console:
        def print(self, value="") -> None:
            printed.append(value)

    monkeypatch.setattr(main_module, "console", _Console())
    monkeypatch.setattr(
        plugins,
        "discover_plugins",
        lambda: [
            plugins.PluginManifest(
                name="demo",
                version="1.0.0",
                description="Demo plugin",
            )
        ],
    )

    handled, _client = main_module.handle_command("/plugins list", cfg, None)

    assert handled is True
    assert len(printed) == 1
    table = printed[0]
    assert [str(cell) for cell in table.columns[0]._cells] == ["demo"]


def test_url_scheme_slash_parses_valid_link_and_reports_invalid_link(monkeypatch):
    cfg = Config()
    infos: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(main_module, "show_info", infos.append)
    monkeypatch.setattr(main_module, "show_error", errors.append)

    handled, _client = main_module.handle_command(
        "/url-scheme algo-cli://skill/example",
        cfg,
        None,
    )
    invalid_handled, _client = main_module.handle_command(
        "/url-scheme https://example.com",
        cfg,
        None,
    )

    assert handled is True
    assert invalid_handled is True
    assert "Action: skill" in infos
    assert "Target: example" in infos
    assert any("does not start with algo-cli://" in error for error in errors)
