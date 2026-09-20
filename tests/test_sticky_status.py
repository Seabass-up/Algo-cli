"""Sticky generation status bar helpers."""

from __future__ import annotations

from io import StringIO

import algo_cli.sticky_status as sticky_status
from algo_cli import main
from algo_cli.config import Config


def test_format_status_toolbar_plain_matches_key_chips() -> None:
    cfg = Config()
    main.RUNTIME_STATUS.clear()
    main.RUNTIME_STATUS.update(
        {
            "model": "deepseek-v4.1-flash:cloud",
            "mode": "local",
            "context_used": 4800,
            "context_total": 1_000_000,
            "context_pct_left": 99,
            "max_tool_iterations": 128,
            "tool_think_every": 50,
            "safe_mode": False,
            "auto_mode": True,
        }
    )
    line = main.format_status_toolbar_plain(cfg)
    assert "deepseek-v4.1-flash:cloud" in line
    assert "local" in line
    assert "▣" in line
    assert "4.8k/1M" in line
    assert "99%" in line
    assert "tools 128" in line
    assert "reflect 50" in line
    assert "safe off" in line
    assert "auto on" in line


def test_sticky_status_noop_on_non_tty() -> None:
    sticky_status._test_reset()
    stream = StringIO()
    sticky_status.start(lambda: "hello status", stream=stream)
    assert sticky_status.is_active() is False
    assert stream.getvalue() == ""
    sticky_status.refresh()
    sticky_status.stop()


class _TtyStream(StringIO):
    def isatty(self) -> bool:
        return True


def test_sticky_status_paints_and_clears_on_tty(monkeypatch) -> None:
    sticky_status._test_reset()

    class _Size:
        columns = 80
        rows = 24

    monkeypatch.setattr(
        sticky_status.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): _Size(),
    )
    stream = _TtyStream()
    sticky_status.start(lambda: "● model · local · tools 8", stream=stream)
    assert sticky_status.is_active() is True
    painted = stream.getvalue()
    assert "● model · local · tools 8" in painted
    assert "\033[1;23r" in painted
    sticky_status.stop()
    cleared = stream.getvalue()
    assert "\033[r" in cleared
    assert sticky_status.is_active() is False


def test_agent_loop_starts_and_stops_sticky(monkeypatch) -> None:
    calls: list[str] = []

    def fake_start(render, *, stream=None):
        calls.append(f"start:{render()}")

    def fake_stop():
        calls.append("stop")

    monkeypatch.setattr(sticky_status, "start", fake_start)
    monkeypatch.setattr(sticky_status, "stop", fake_stop)

    def boom(*_a, **_k):
        calls.append("body")
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "_agent_loop_body", boom)
    cfg = Config()
    main.RUNTIME_STATUS.update(
        {
            "model": "m",
            "mode": "local",
            "safe_mode": True,
            "auto_mode": False,
            "max_tool_iterations": 8,
            "tool_think_every": 10,
        }
    )

    try:
        main.agent_loop(object(), cfg, "hi")
    except RuntimeError as exc:
        assert str(exc) == "boom"

    assert calls[0].startswith("start:")
    assert "body" in calls
    assert calls[-1] == "stop"
