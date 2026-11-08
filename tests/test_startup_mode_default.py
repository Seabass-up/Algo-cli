"""Persistent startup intent must create fresh, scoped interactive activation."""

import json

from algo_cli import config, session_mode, tools
from algo_cli.config import Config
from algo_cli.nathan_runtime import preflight_runtime_tool
from algo_cli.oliver_slash_dispatch import handle_command


def test_explicit_yolo_default_survives_restart_without_persisting_a_grant(tmp_path):
    cfg = Config(cwd=str(tmp_path), session_mode="execute")
    handle_command("/mode default yolo", cfg, None, user_initiated=True)
    assert session_mode.active_mode(cfg) == "yolo"
    saved = json.loads(config.CONFIG_FILE.read_text())
    assert saved["default_session_mode"] == "yolo"
    assert saved["session_mode"] == "execute"
    loaded = Config.load()
    assert session_mode.active_mode(loaded) == "execute"
    assert not loaded.auto_approve_active
    session_mode.activate_startup_default(loaded)
    assert session_mode.active_mode(loaded) == "yolo"
    assert loaded.auto_approve_active


def test_model_tools_cannot_set_the_startup_default(tmp_path):
    cfg = Config(cwd=str(tmp_path))
    handle_command("/mode default yolo", cfg, None)
    assert cfg.default_session_mode == ""
    assert not preflight_runtime_tool("session_command", {"command": "/mode default yolo"}, cfg).allowed
    assert "user" in tools.session_command("/mode default yolo", cfg).lower()


def test_startup_default_is_not_inherited_by_delegated_execution(tmp_path):
    cfg = Config(cwd=str(tmp_path), default_session_mode="yolo")
    with session_mode.delegated_scope():
        assert session_mode.activate_startup_default(cfg) == []
        assert session_mode.active_mode(cfg) == "explore"
    session_mode.activate_startup_default(cfg)
    with session_mode.delegated_scope():
        assert session_mode.active_mode(cfg) == "explore"


def test_user_can_change_the_startup_default_back(tmp_path):
    cfg = Config(cwd=str(tmp_path))
    handle_command("/mode default yolo", cfg, None, user_initiated=True)
    handle_command("/mode default execute", cfg, None, user_initiated=True)
    loaded = Config.load()
    session_mode.activate_startup_default(loaded)
    assert loaded.default_session_mode == "execute"
    assert session_mode.active_mode(loaded) == "execute"
