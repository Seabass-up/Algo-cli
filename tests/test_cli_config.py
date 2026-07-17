"""Focused terminal provider setup and hidden-legacy slash-route tests."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from algo_cli import cli_config, config, main, slash_dispatch
from algo_cli.config import Config


def _capture() -> object:
    return cli_config.console.capture()


def test_config_status_redacts_provider_values(monkeypatch) -> None:
    secret = "xai-not-for-status-output"
    monkeypatch.setenv("XAI_API_KEY", secret)

    with _capture() as captured:
        assert cli_config.run(["status"], interactive=False) == 0

    output = captured.get()
    assert secret not in output
    assert "xAI API" in output
    assert "configured" in output


def test_config_setup_xai_writes_private_env_file(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / "env"
    monkeypatch.setattr(config, "DEFAULT_RUNTIME_ENV_FILE", env_path)
    monkeypatch.setattr(config, "DOTENV_RUNTIME_ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("ALGO_CLI_ENV_FILE", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    with _capture() as captured:
        result = cli_config.run(
            ["setup", "xai"],
            interactive=True,
            secret_input=lambda _prompt: "xai-test-secret",
        )

    assert result == 0
    assert "xai-test-secret" not in captured.get()
    assert "XAI_API_KEY=xai-test-secret" in env_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_runtime_env_update_preserves_other_settings_and_round_trips_quoted_values(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / "env"
    env_path.write_text("# keep this comment\nOTHER_SETTING=unchanged\nXAI_API_KEY=old\nXAI_API_KEY=duplicate\n", encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_RUNTIME_ENV_FILE", env_path)
    monkeypatch.setattr(config, "DOTENV_RUNTIME_ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("ALGO_CLI_ENV_FILE", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)

    config.update_runtime_env(
        {
            "XAI_API_KEY": 'xai value with # and "quotes"',
            "GOOGLE_OAUTH_CLIENT_ID": "desktop.apps.googleusercontent.com",
        }
    )

    rendered = env_path.read_text(encoding="utf-8")
    assert "# keep this comment" in rendered
    assert "OTHER_SETTING=unchanged" in rendered
    assert rendered.count("XAI_API_KEY=") == 1
    if os.name != "nt":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    loaded = config.load_runtime_env(env_path, override=True)
    assert loaded["XAI_API_KEY"] == 'xai value with # and "quotes"'
    assert loaded["GOOGLE_OAUTH_CLIENT_ID"] == "desktop.apps.googleusercontent.com"


def test_config_setup_google_uses_desktop_client_then_starts_login(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / "env"
    monkeypatch.setattr(config, "DEFAULT_RUNTIME_ENV_FILE", env_path)
    monkeypatch.setattr(config, "DOTENV_RUNTIME_ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("ALGO_CLI_ENV_FILE", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        cli_config,
        "_provider_main",
        lambda: SimpleNamespace(run_google_login=lambda arg="": calls.append(arg) or True),
    )

    with _capture() as captured:
        result = cli_config.run(
            ["setup", "google", "--manual"],
            interactive=True,
            input_fn=lambda _prompt: "desktop-client.apps.googleusercontent.com",
            secret_input=lambda _prompt: "",
        )

    assert result == 0
    assert calls == ["--manual"]
    assert "GOOGLE_OAUTH_CLIENT_ID=desktop-client.apps.googleusercontent.com" in env_path.read_text(encoding="utf-8")
    assert "Desktop app" in captured.get()


def test_config_setup_and_auth_propagate_provider_failure(monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "desktop.apps.googleusercontent.com")
    monkeypatch.setattr(
        cli_config,
        "_provider_main",
        lambda: SimpleNamespace(
            run_google_login=lambda _arg="": False,
            run_chatgpt_login=lambda _arg="": False,
        ),
    )

    assert cli_config.run(
        ["setup", "google"], interactive=False, secret_input=lambda _prompt: ""
    ) == 1
    assert cli_config.run(["auth", "chatgpt", "login"], interactive=False) == 1


def test_config_setup_requires_explicit_provider_when_noninteractive() -> None:
    with _capture() as captured:
        result = cli_config.run(["setup"], interactive=False)

    assert result == 2
    assert "Choose a provider" in captured.get()


@pytest.mark.parametrize(
    ("argv", "suggestion"),
    [
        (["setup", "googl"], "Did you mean `google`?"),
        (["auth", "chatgtp"], "Did you mean `chatgpt`?"),
    ],
)
def test_config_provider_typos_get_actionable_suggestions(argv, suggestion) -> None:
    with _capture() as captured:
        result = cli_config.run(argv, interactive=False)

    assert result == 2
    assert suggestion in captured.get()


def test_config_auth_xai_remove_deletes_saved_value(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / "env"
    monkeypatch.setattr(config, "DEFAULT_RUNTIME_ENV_FILE", env_path)
    monkeypatch.setattr(config, "DOTENV_RUNTIME_ENV_FILE", tmp_path / ".env")
    config.update_runtime_env({"XAI_API_KEY": "xai-test-secret"})

    with _capture():
        assert cli_config.run(["auth", "xai", "remove"], interactive=False) == 0

    assert "XAI_API_KEY" not in env_path.read_text(encoding="utf-8")


def test_main_routes_top_level_config_before_runtime_initialization(monkeypatch) -> None:
    calls: list[list[str]] = []
    lifecycle: list[str] = []
    monkeypatch.setattr(main.sys, "argv", ["algo-cli", "config", "status"])
    monkeypatch.setattr(main, "has_legacy_data", lambda: True)
    monkeypatch.setattr(main, "perform_legacy_migration", lambda: lifecycle.append("full") or True)
    monkeypatch.setattr(main, "migrate_legacy_sidecar_files", lambda: lifecycle.append("sidecar") or [])
    monkeypatch.setattr(main, "load_runtime_env", lambda **_kwargs: lifecycle.append("env"))
    monkeypatch.setattr(
        cli_config,
        "run",
        lambda argv: lifecycle.append("config") or calls.append(list(argv)) or 0,
    )
    monkeypatch.setattr(main.Config, "load", lambda: pytest.fail("config must not initialize a chat runtime"))

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 0
    assert calls == [["status"]]
    assert lifecycle == ["full", "sidecar", "env", "config"]


def test_repl_config_delegates_to_focused_runner(monkeypatch) -> None:
    cfg = Config()
    client = object()
    calls: list[str] = []
    monkeypatch.setattr(main, "run_config_command", lambda arg="": calls.append(arg))

    handled, returned = slash_dispatch.handle_command("/config status", cfg, client)

    assert handled is True
    assert returned is client
    assert calls == ["status"]


def test_legacy_google_callback_dispatches_once(monkeypatch) -> None:
    cfg = Config()
    client = object()
    calls: list[str] = []
    monkeypatch.setattr(main, "run_google_callback", lambda arg="": calls.append(arg))

    handled, returned = slash_dispatch.handle_command("/google-callback --clipboard", cfg, client)

    assert handled is True
    assert returned is client
    assert calls == ["--clipboard"]


def test_auth_setup_routes_are_hidden_from_slash_palette() -> None:
    commands = {name for name, _description in slash_dispatch.SLASH_COMMANDS}

    assert "/config" in commands
    assert "/google-login" not in commands
    assert "/xai-login" not in commands
    assert "/chatgpt-login" not in commands
