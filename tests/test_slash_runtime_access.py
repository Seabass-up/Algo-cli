from __future__ import annotations

import json

from algo_cli import tools
from algo_cli.config import Config
from algo_cli.oliver_slash_dispatch import SLASH_COMMANDS
from algo_cli.samuel_policy_engine import session_command_requires_approval


def test_available_actions_lists_every_registered_slash_command():
    payload = json.loads(tools.available_actions("slash"))
    registered = {command for command, _description in SLASH_COMMANDS}
    catalog = {row["command"] for row in payload["slash_registry"]}
    assert registered <= catalog
    assert payload["slash_aliases"]["/hs"] == "/hsearch"
    assert payload["slash_aliases"]["/hr"] == "/hread"


def test_inspect_slash_commands_are_agent_accessible_without_approval():
    for command in (
        "/status",
        "/kernel list",
        "/kernel show benchmark",
        "/intel status",
        "/hsearch runtime kernel",
        "/hs runtime kernel",
        "/route preview this task",
        "/plugins",
        "/plugins list",
        "/model",
        "/models",
        "/theme",
        "/host",
        "/credentials",
        "/url-scheme help",
        "/google help",
        "/goal status",
    ):
        assert session_command_requires_approval(command) is False, command


def test_mutating_slash_commands_still_require_approval():
    for command in (
        "/safe off",
        "/mode execute",
        "/worktree new feature",
        "/ship push",
        "/intel reindex",
        "/host http://127.0.0.1:11434",
        "/host status",
        "/keepalive status",
        "/model qwen3",
        "/model status",
        "/system status",
        "/theme status",
        "/goal show",
        "/goal clear",
        "/goal resume",
        "/goal inspect the repository",
        "/exit",
        "/quit",
    ):
        assert session_command_requires_approval(command) is True, command


def test_session_command_blocks_exit_and_yolo_activation(tmp_path):
    cfg = Config(cwd=str(tmp_path))
    assert "only the user may exit" in tools.session_command("/exit", cfg)
    assert "only the user may enter YOLO" in tools.session_command("/mode yolo", cfg)


def test_runtime_agent_models_lists_without_opening_the_picker(monkeypatch, tmp_path):
    from algo_cli import main as main_module

    cfg = Config(cwd=str(tmp_path), model="test-model")
    monkeypatch.setattr(
        main_module,
        "model_picker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("picker")),
    )
    monkeypatch.setattr(main_module, "local_model_names", lambda _cfg: ["qwen3"])
    monkeypatch.setattr(main_module, "cloud_model_names", lambda: ["qwen3:cloud"])
    monkeypatch.setattr(main_module, "chatgpt_model_names", lambda: ([], False))
    monkeypatch.setattr(main_module, "xai_model_names", lambda: ([], False))
    monkeypatch.setattr("algo_cli.theodore_runtime_services.create_client", lambda _cfg: object())

    result = tools.session_command("/models", cfg)

    assert "Current model: test-model" in result
    assert "qwen3" in result
    assert "picker" not in result.lower()


def test_kernel_list_output_is_returned_to_the_runtime_agent(monkeypatch, tmp_path):
    monkeypatch.setattr("algo_cli.theodore_runtime_services.create_client", lambda _cfg: object())
    result = tools.session_command("/kernel list", Config(cwd=str(tmp_path)))
    assert "repo-intelligence" in result
    assert "Built-in kernels:" in result
