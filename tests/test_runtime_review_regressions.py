"""Regressions for the October 2026 installed-runtime review."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from algo_cli import config, context_budget, main, nathan_runtime, tools
from algo_cli import irene_memory_path_policy as path_policy


class SummaryClient:
    def __init__(self, content):
        self.content = content

    def chat(self, **_kwargs):
        if isinstance(self.content, Exception):
            raise self.content
        return {"message": {"content": self.content}}


@pytest.mark.parametrize(
    "content",
    [
        '{"summary":""}', '{"summary":"  "}', '{"summary":null}',
        '{"summary":42}', '{"summary":[]}', '{"summary":{}}',
        '{"summary":false}', '{}', '[]', 'null', 'not JSON',
    ],
    ids=["empty", "blank", "null", "number", "list", "object", "bool", "missing", "array", "scalar", "invalid-json"],
)
def test_invalid_summary_is_explicitly_degraded(content):
    client = SummaryClient(content)
    summary = context_budget.summarize_message_batch(
        config.Config(), [{"role": "user", "content": "SYNTHETIC_EVIDENCE"}],
        maintenance_client_fn=lambda *_: (client, "fixture"),
    )
    assert isinstance(summary, context_budget.FallbackSummary)
    assert "[degraded summary:" in summary.lower()
    assert "SYNTHETIC_EVIDENCE" in summary


@pytest.mark.parametrize("manual", [True, False], ids=["manual", "automatic"])
def test_lossy_summary_survives_persistence_and_recompaction(monkeypatch, manual):
    cfg = config.Config(model_adaptive=False)
    cfg.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"evidence {i}"}
                    for i in range(40)]
    client = SummaryClient(ConnectionError("synthetic outage"))
    monkeypatch.setattr(main, "small_maintenance_client", lambda *_: (client, "fixture"))
    notices = []
    monkeypatch.setattr(context_budget, "show_info", notices.append)
    if manual:
        ok, notice = context_budget.rebuild_context_summary(None, cfg)
        assert ok and "lossy fallback" in notice
    else:
        assert context_budget.maybe_compact_context(
            None, cfg, precomputed_used=9000, model_info={"context_length": 131072, "parameter_size": "8B"},
        )
        assert len(notices) == 1 and "lossy fallback" in notices[0]
    loaded = config.Config.load()
    assert "[degraded summary:" in loaded.session_summary.lower()
    monkeypatch.setattr(context_budget, "_memory_prompt_section", lambda *_a, **_k: "")
    assert "[degraded summary:" in context_budget.build_system_prompt(loaded).lower()
    client.content = json.dumps({"summary": "A successful later compression."})
    later = context_budget.summarize_message_batch(
        loaded, [{"role": "user", "content": "new message"}],
        maintenance_client_fn=lambda *_: (client, "fixture"),
    )
    assert "[degraded summary:" in later.lower()
    assert "A successful later compression." in later


def test_valid_summary_remains_plain_text():
    client = SummaryClient('{"summary":"  Verified facts.  "}')
    result = context_budget.summarize_message_batch(
        config.Config(), [{"role": "user", "content": "evidence"}],
        maintenance_client_fn=lambda *_: (client, "fixture"),
    )
    assert type(result) is str and result == "Verified facts."


_CONTINUATION = re.compile(r"\n\.\.\.\[truncated; read_file\(start_line=(\d+), start_char=(\d+)\)\]\Z")


@pytest.mark.parametrize("budget", [128, 511, 50_000])
@pytest.mark.parametrize("body", ["x" * 50_010, "one\ntwo\n" * 7000, "\u03bb\U0001f642\\\"\n" * 10_000], ids=["long-line", "lines", "unicode-escapes"])
def test_read_pages_reconstruct_model_visible_text(tmp_path, budget, body):
    target = tmp_path / "input.txt"
    target.write_text("ignored\n" + body, encoding="utf-8", newline="")
    cursor = 0
    pages = []
    while True:
        result = tools.read_file(str(target), max_chars=budget, start_line=2, start_char=cursor)
        delivered = nathan_runtime.tool_result_message("read_file", result)["content"]
        assert delivered == result
        assert len(delivered) <= min(budget, nathan_runtime.TOOL_RESULT_CONTENT_LIMIT)
        match = _CONTINUATION.search(delivered)
        if match is None:
            pages.append(delivered)
            break
        prefix = delivered[:match.start()]
        assert prefix
        assert int(match[1]) == 2
        assert int(match[2]) == cursor + len(prefix)
        cursor = int(match[2])
        pages.append(prefix)
        assert len(pages) < 5000
    assert "".join(pages) == body


def test_read_exact_limit_eof_and_legacy_line_alias(tmp_path):
    target = tmp_path / "input.txt"
    target.write_text("skip\r\nabc\r\ndef", encoding="utf-8", newline="")
    assert tools.read_file(str(target), offset=2, max_chars=7) == "abc\ndef"
    assert tools.read_file(str(target), start_line=2, start_char=7) == ""
    assert tools.read_file(str(target), start_line=999) == ""


def test_read_tiny_partial_budget_refuses_without_partial_body(tmp_path):
    target = tmp_path / "input.txt"
    target.write_text("SYNTHETIC_BODY_" * 100, encoding="utf-8")
    result = tools.read_file(str(target), max_chars=5)
    assert result.startswith("Error") and "max_chars" in result
    assert "SYNTHETIC_BODY" not in result


def test_read_cursor_respects_dynamic_transport_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(nathan_runtime, "TOOL_RESULT_CONTENT_LIMIT", 200)
    target = tmp_path / "input.txt"
    target.write_text("x" * 1000, encoding="utf-8")
    result = tools.read_file(str(target), max_chars=1000)
    assert len(result) <= 200 and _CONTINUATION.search(result)
    assert nathan_runtime.tool_result_message("read_file", result)["content"] == result


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("backend", ["rg", "fallback"])
@pytest.mark.parametrize("ignored", [False, True], ids=["unignored", "gitignored"])
@pytest.mark.parametrize("glob", [None, "*.txt"], ids=["default-glob", "text-glob"])
def test_managed_worktrees_excluded_but_other_hidden_sources_visible(
    tmp_path, monkeypatch, protected, backend, ignored, glob,
):
    root = tmp_path.resolve()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if ignored:
        (root / ".gitignore").write_text(".claude/\n.algo-cli-worktrees/\n", encoding="utf-8")
    for folder in (".claude/worktrees/old", ".algo-cli-worktrees/old", ".github/workflows", "src"):
        directory = root / folder
        directory.mkdir(parents=True)
        (directory / "fixture.txt").write_text("REVIEW_SEARCH_CANARY\n", encoding="utf-8")
    private = root / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(path_policy, "_known_protected_roots", lambda: (private,))
    cfg = config.Config(cwd=str(root), continuum_enabled=protected)
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _: None)
    elif shutil.which("rg") is None:
        pytest.skip("ripgrep not installed")
    result = tools.search_files("REVIEW_SEARCH_CANARY", cwd=str(root), cfg=cfg, glob=glob)
    assert not result.startswith("Error")
    assert ".claude" not in result and ".algo-cli-worktrees" not in result
    assert ".github" in result and "src" in result


def test_read_continuation_preserves_protected_path_denial(tmp_path, monkeypatch):
    private = tmp_path.resolve() / "private"
    private.mkdir(mode=0o700)
    target = private / "secret.txt"
    target.write_text("PRIVATE_CANARY", encoding="utf-8")
    monkeypatch.setattr(path_policy, "_known_protected_roots", lambda: (private,))
    cfg = config.Config(cwd=str(tmp_path.resolve()), continuum_enabled=True)
    result = tools.read_file(str(target), start_char=1, cfg=cfg)
    assert "protected memory paths are unavailable" in result and "PRIVATE_CANARY" not in result


@pytest.mark.parametrize("coordinator", [False, True])
def test_jev_boundary_remains_available_under_schema_pressure(coordinator):
    from algo_cli.tool_context import select_tools_for_prompt

    prompt = "can jev check my email in the browser"
    selected = select_tools_for_prompt(prompt, tools.ALL_TOOLS, decision_coordinator=coordinator)
    assert {"jev_kernel_status", "session_command"} <= {fn.__name__ for fn in selected}
    restricted = select_tools_for_prompt(
        "Allowed tool classes: filesystem. " + prompt, tools.ALL_TOOLS, decision_coordinator=coordinator,
    )
    assert "jev_kernel_status" not in {fn.__name__ for fn in restricted}
