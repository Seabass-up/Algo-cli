"""Regression tests for the 2026-11-09 bug hunt (core runtime paths).

Each test names the defect it pins. See docs/lessons-learned.md, 2026-11-09.
"""

from __future__ import annotations

import http.client
import json
import time

import pytest

from algo_cli import (
    action_registry,
    agent_threads,
    display,
    execution_guardrails,
    harness,
    main,
    model_info,
    model_profile,
    nathan_program_runtime,
    theodore_runtime_services as runtime_services,
    tool_contract,
    tools,
    version_manifest,
)
from algo_cli.config import Config


# ---- edit_file / batch_edit must not rewrite bytes outside the match ----


def test_edit_file_preserves_crlf_line_endings(tmp_path):
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    result = tools.edit_file(str(target), "beta", "BETA")

    assert result.startswith("Edited ")
    assert target.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_edit_file_matches_lf_search_text_in_crlf_file(tmp_path):
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")

    result = tools.edit_file(str(target), "one\ntwo", "1\n2\n2b")

    assert result.startswith("Edited ")
    assert target.read_bytes() == b"1\r\n2\r\n2b\r\nthree\r\n"


def test_edit_file_leaves_lf_file_as_lf(tmp_path):
    target = tmp_path / "lf.txt"
    target.write_bytes(b"alpha\nbeta\n")

    tools.edit_file(str(target), "beta", "BETA\ngamma")

    assert target.read_bytes() == b"alpha\nBETA\ngamma\n"


def test_edit_file_refuses_non_utf8_file_without_changing_it(tmp_path):
    target = tmp_path / "latin.txt"
    original = b"caf\xe9 one\nline two\n"
    target.write_bytes(original)

    result = tools.edit_file(str(target), "line two", "line 2")

    assert result.startswith("Error:") and "not valid UTF-8" in result
    assert target.read_bytes() == original


def test_batch_edit_preserves_crlf_and_refuses_non_utf8(tmp_path):
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"a\r\nb\r\nc\r\n")

    result = tools.batch_edit(
        str(target),
        [{"old_string": "a\nb", "new_string": "A\nB"}, {"old_string": "c", "new_string": "C"}],
    )

    assert result.startswith("Batch-edited ")
    assert target.read_bytes() == b"A\r\nB\r\nC\r\n"

    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"\xff\xfe raw\n")
    assert "not valid UTF-8" in tools.batch_edit(str(bad), [{"old_string": "raw", "new_string": "x"}])
    assert bad.read_bytes() == b"\xff\xfe raw\n"


def test_x_search_accepts_string_max_results_after_the_search_ran(monkeypatch):
    class FakeXai:
        def search(self, **kwargs):
            assert kwargs["max_results"] == 2
            return {"content": "summary", "citations": ["https://x.test/1", "https://x.test/2", "https://x.test/3"]}

    from algo_cli import xai_auth, xai_client

    monkeypatch.setattr(xai_client, "active_xai_client", lambda: FakeXai())
    monkeypatch.setattr(xai_auth, "get_valid_token", lambda *_a, **_k: "token", raising=False)
    cfg = Config()
    cfg.continuum_enabled = True  # keep the test from writing a cache file

    result = tools.x_search("query", max_results="2", cfg=cfg)  # type: ignore[arg-type]

    assert "https://x.test/2" in result and "https://x.test/3" not in result
    assert tools.x_search("query", max_results="many", cfg=cfg).startswith(  # type: ignore[arg-type]
        "Error running x_search: max_results must be"
    )


# ---- harness index freshness and per-turn references ----


def test_index_is_not_perpetually_stale_when_a_source_is_excluded(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "normal.md").write_text("---\ntitle: Normal\ndescription: a note\n---\nbody\n", encoding="utf-8")
    (wiki / "clip.md").write_text(
        "---\ntitle: Clip\ndescription: ChatGPT conversation about things\n---\nclipped\n", encoding="utf-8"
    )
    spec = harness.SourceRoot("fixture", "wiki", wiki, ("*.md",), 10)
    monkeypatch.setattr(harness, "INDEX_PATH", tmp_path / "harness_index.json")
    monkeypatch.setattr(harness, "all_source_roots", lambda: (spec,))

    index = harness.load_index()
    assert [record["relative_path"] for record in index["records"] if record.get("kind") == "wiki"] == ["normal.md"]
    built_at = harness.INDEX_PATH.stat().st_mtime_ns
    harness._STALE_CHECK_CACHE = None

    assert harness.index_is_stale() is False
    harness._STALE_CHECK_CACHE = None
    harness.load_index()
    assert harness.INDEX_PATH.stat().st_mtime_ns == built_at


def test_retrieved_context_falls_back_to_summary_when_no_snippet():
    block = harness.format_retrieved_context(
        [{"id": "wiki/a.md", "harness": "algo-cli", "kind": "wiki", "title": "A", "summary": "the body excerpt"}]
    )

    assert "the body excerpt" in block


# ---- local server start must not respawn and block on every prompt ----


def test_failed_local_start_is_not_repeated_within_cooldown(monkeypatch):
    spawned: list[list[str]] = []

    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr(runtime_services, "ollama_server_ready", lambda _host: False)
    monkeypatch.setattr(runtime_services.subprocess, "Popen", lambda argv, **_kw: spawned.append(argv) or FakeProcess())
    monkeypatch.setattr(runtime_services.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime_services, "LOCAL_STARTUP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runtime_services, "show_info", lambda *_a, **_k: None)
    errors: list[str] = []
    monkeypatch.setattr(runtime_services, "show_error", errors.append)
    runtime_services._LOCAL_SERVE_PROCESSES.clear()
    runtime_services._LOCAL_START_FAILED_AT.clear()
    host = "http://127.0.0.1:59999"
    try:
        assert runtime_services.start_local_ollama_host(host) is False
        assert runtime_services.start_local_ollama_host(host) is False
        assert runtime_services.start_local_ollama_host(host) is False
    finally:
        runtime_services._LOCAL_SERVE_PROCESSES.clear()
        runtime_services._LOCAL_START_FAILED_AT.clear()

    assert spawned == [["ollama", "serve"]]
    assert "still not responding" in errors[-1]


def test_readiness_probe_treats_http_protocol_errors_as_not_ready(monkeypatch):
    def broken(*_args, **_kwargs):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(runtime_services, "urlopen", broken)
    runtime_services.SERVER_READY_CACHE.pop("http://127.0.0.1:59998", None)

    assert runtime_services.ollama_server_ready("http://127.0.0.1:59998") is False


def test_footer_model_listing_never_starts_the_server(monkeypatch):
    def must_not_start(_host):
        raise AssertionError("footer refresh must not start ollama")

    monkeypatch.setattr(main, "start_local_ollama_host", must_not_start)
    monkeypatch.setattr(main, "ollama_server_ready", lambda _host: False)
    monkeypatch.setattr(main._model_info_module, "resolve_model_info", lambda *_a, **_k: {})
    monkeypatch.setattr(main, "context_status", lambda *_a, **_k: (0, 0, 0, 0, 0))
    cfg = Config()
    cfg.cloud = False
    cfg.model_provider = "ollama"
    cfg.model = "qwen3"

    main.refresh_runtime_status(cfg, None, force=True)

    assert main.RUNTIME_STATUS["local_models"] == []
    assert main._MODEL_LISTING_OBSERVE_ONLY is False


# ---- completion gate: commands that run no tests are not test verification ----


@pytest.mark.parametrize(
    "command",
    [
        "cargo test --no-run",
        "go test -n ./...",
        "make -n test",
        "mvn test -DskipTests",
        "mvn verify -Dmaven.test.skip=true",
        "npm test -- --passWithNoTests",
        "gradle build -x test",
    ],
)
def test_commands_that_skip_tests_do_not_qualify_as_verification(command):
    assert execution_guardrails.classify_verification_command(command).qualifies is False


@pytest.mark.parametrize(
    "command",
    ["cargo test", "go test ./...", "make test", "mvn test", "mvn verify -DskipTests=false", "pytest -n 4", "gradle test"],
)
def test_real_test_commands_still_qualify(command):
    assert execution_guardrails.classify_verification_command(command).qualifies is True


# ---- doctor host probe ----


def test_doctor_probe_degrades_on_invalid_port_and_normalises_host(monkeypatch):
    assert action_registry._check_ollama_host("http://localhost:abc") is False

    seen: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(action_registry, "urlopen", lambda url, timeout: seen.append(url) or Response())

    assert action_registry._check_ollama_host("127.0.0.1:11434") is True
    assert seen == ["http://127.0.0.1:11434/api/tags"]


# ---- goal mode must not complete from withheld text ----


def test_goal_is_not_completed_by_text_from_a_turn_that_was_not_accepted(monkeypatch):
    cfg = Config()
    calls: list[str] = []

    def fake_agent_loop(_client, cfg_, prompt):
        calls.append(prompt)
        # The completion gate withheld this text, then the turn ended unaccepted.
        cfg_.messages.append({"role": "assistant", "content": f"all done {main.GOAL_COMPLETE_MARKER}"})
        setattr(cfg_, "_turn_completed_normally", False)

    monkeypatch.setattr(main, "agent_loop", fake_agent_loop)
    infos: list[str] = []
    monkeypatch.setattr(main, "show_info", infos.append)
    monkeypatch.setattr(main, "show_error", lambda *_a, **_k: None)

    main.run_goal_loop(client=None, cfg=cfg, arg="--rounds 2 do the thing")

    assert len(calls) == 2
    assert not any("marked complete" in message for message in infos)


def test_goal_still_completes_from_an_accepted_turn(monkeypatch):
    cfg = Config()

    def fake_agent_loop(_client, cfg_, _prompt):
        cfg_.messages.append({"role": "assistant", "content": f"done {main.GOAL_COMPLETE_MARKER}"})
        setattr(cfg_, "_turn_completed_normally", True)

    monkeypatch.setattr(main, "agent_loop", fake_agent_loop)
    infos: list[str] = []
    monkeypatch.setattr(main, "show_info", infos.append)

    main.run_goal_loop(client=None, cfg=cfg, arg="do the thing")

    assert any("marked complete" in message for message in infos)


# ---- model metadata ----


def test_unresolved_model_info_is_not_refetched_on_every_call(monkeypatch):
    fetches: list[str] = []
    monkeypatch.setattr(model_info, "ensure_model_info", lambda *_a, **_k: {"name": "m"})
    monkeypatch.setattr(model_info, "fetch_model_info_from_cli", lambda *_a, **_k: fetches.append("cli") or {})
    monkeypatch.setattr(model_info, "load_model_info", lambda *_a, **_k: None)
    monkeypatch.setattr(model_info, "fetch_model_info", lambda *_a, **_k: fetches.append("sdk") or {"error": "x"})
    cfg = Config()
    cfg.model = "no-context-model"
    cfg.model_provider = "ollama"

    first = model_info.resolve_model_info(cfg, object())
    second = model_info.resolve_model_info(cfg, object())

    assert first == second == {"name": "no-context-model"}
    assert fetches == ["cli", "sdk"]


def test_explicit_ctx_equal_to_default_is_not_overridden_by_profile():
    cfg = Config()
    cfg.model = "qwen3:14b"
    cfg.num_ctx = model_profile.DEFAULT_NUM_CTX
    untouched = model_profile.effective_params(cfg, {"context_length": 131072, "parameter_size": "14B"})

    cfg.num_ctx_explicit = True
    explicit = model_profile.effective_params(cfg, {"context_length": 131072, "parameter_size": "14B"})

    assert explicit.num_ctx == model_profile.DEFAULT_NUM_CTX
    assert "num_ctx" not in explicit.adapted_fields
    # Guard: the untouched default may still be adapted by the profile.
    assert untouched.num_ctx >= model_profile.DEFAULT_NUM_CTX


# ---- tool error reporting ----


def test_type_error_inside_a_tool_body_is_not_reported_as_an_argument_error():
    def tool(path: str, limit: int = 5) -> str:
        return path + limit  # type: ignore[operator]

    try:
        tool(path="a", limit=3)
    except TypeError as exc:
        message = tool_contract.correct_tool_error("tool", {"path": "a", "limit": 3}, exc, tool)

    assert message.startswith("Tool error for tool: TypeError:")
    assert "Re-issue the call" not in message


def test_missing_argument_is_still_reported_as_an_argument_error():
    def tool(path: str) -> str:
        return path

    try:
        tool()  # type: ignore[call-arg]
    except TypeError as exc:
        message = tool_contract.correct_tool_error("tool", {}, exc, tool)

    assert message.startswith("Tool argument error for tool:")
    assert "Re-issue the call" in message


def test_program_step_error_is_bounded_with_a_visible_marker():
    long_output = "E" * 30_000

    bounded = nathan_program_runtime._bounded_step_error(long_output)

    assert len(bounded) < 4_200
    assert "characters omitted" in bounded
    assert nathan_program_runtime._bounded_step_error("short") == "short"


# ---- persistence and display ----


def test_corrupt_thread_store_keeps_a_backup_before_it_can_be_rewritten(config_dir):
    target = config_dir / agent_threads.THREADS_FILE_NAME
    target.write_text('{"version": 1, "threads": [', encoding="utf-8")

    agent_threads.list_threads()

    backup = target.with_suffix(target.suffix + ".corrupt")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8").startswith('{"version": 1')


def test_markdown_preview_keeps_line_structure_and_closes_fences():
    text = "# Title\n\n- one\n- two\n\n```python\nprint(1)\nprint(2)\n" + ("x = 1\n" * 200) + "```\n"

    preview = display._markdown_preview(text, 80)

    assert "\n- one\n- two" in preview
    assert preview.count("```") % 2 == 0
    assert len(preview) < 120


def test_loading_a_corrupt_saved_conversation_does_not_claim_it_is_missing(config_dir):
    from algo_cli import config as config_module

    config_module.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    (config_module.HISTORY_DIR / "broken.json").write_text('{"messages": [', encoding="utf-8")
    cfg = Config()

    with pytest.raises(ValueError, match="exists but could not be read"):
        cfg.load_conversation("broken")
    with pytest.raises(FileNotFoundError, match="No saved conversation named"):
        cfg.load_conversation("absent")


def test_version_string_does_not_print_unknown_index_version(config_dir):
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(
        json.dumps({"generated": "2026-11-09T09:14:00", "indexer": "python", "records": [{"id": "a"}]}),
        encoding="utf-8",
    )

    text = version_manifest.format_version_string(version_manifest.build_manifest())

    assert "1 records (index built python 2026-11-09T09:14:00)" in text
    assert "vunknown" not in text


def test_local_start_cooldown_expires(monkeypatch):
    monkeypatch.setattr(runtime_services, "ollama_server_ready", lambda _host: True)
    runtime_services._LOCAL_START_FAILED_AT["http://127.0.0.1:59997"] = time.monotonic()
    try:
        assert runtime_services.start_local_ollama_host("http://127.0.0.1:59997") is True
        assert "http://127.0.0.1:59997" not in runtime_services._LOCAL_START_FAILED_AT
    finally:
        runtime_services._LOCAL_START_FAILED_AT.clear()
