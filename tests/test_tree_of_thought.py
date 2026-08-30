"""Tests for tree-of-thought reasoning helpers."""

from __future__ import annotations

import pytest

from algo_cli.reasoning import run_tot


def test_run_tot_rejects_invalid_strategy():
    class FakeClient:
        def chat(self, **_kwargs):
            raise AssertionError("client should not be invoked for invalid strategy")

    with pytest.raises(ValueError, match="strategy must be either 'bfs' or 'dfs'"):
        run_tot(task="test", client=FakeClient(), model="qwen3:latest", strategy="invalid")
