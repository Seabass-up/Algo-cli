from __future__ import annotations

from pathlib import Path

from algo_cli.config import Config
from algo_cli import session_commands


def test_session_slash_ls_and_cwd():
    cfg = Config()
    cfg.cwd = str(Path(__file__).resolve().parents[1])
    listing = session_commands.execute("/ls", cfg)
    assert "tests" in listing or "algo_cli" in listing
    cwd_line = session_commands.execute("/cwd", cfg)
    assert "cwd:" in cwd_line