from __future__ import annotations

from pathlib import Path

from algo_cli.config import Config
from algo_cli import workspace_resolver


def test_algo_cli_task_resolves_to_repo(tmp_path, monkeypatch):
    cfg = Config()
    root = tmp_path / "algo-cli"
    (root / "algo_cli").mkdir(parents=True)
    monkeypatch.setattr(workspace_resolver, "candidate_workspaces", lambda _task: [root])
    assert workspace_resolver.resolve_agent_workspace("fix algo_cli/reflex.py", cfg) is True
    assert Path(cfg.cwd) == root.resolve()
