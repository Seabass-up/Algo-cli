"""Tools: path resolution, output caps, shell safe-mode, search fallback."""

from __future__ import annotations

from io import StringIO
import json
import os
import time
from types import SimpleNamespace

import pytest
from rich.console import Console

from algo_cli import tool_runtime, tools
from algo_cli.config import Config
from algo_cli.runtime_services import scoped_tool_runtime_env


def test_resolve_absolute_and_relative(tmp_path):
    abs_path = tools._resolve(str(tmp_path / "x.txt"))
    assert abs_path.is_absolute()
    rel_path = tools._resolve("x.txt", cwd=str(tmp_path))
    assert rel_path == (tmp_path / "x.txt").resolve()


def test_cap_truncates():
    short = tools._cap("hello", limit=100)
    assert short == "hello"
    long = tools._cap("x" * 50, limit=10)
    assert long.startswith("x" * 10)
    assert "truncated" in long


def test_deny_command_re():
    assert tools.DENY_COMMAND_RE.search("rm -rf /tmp/x")
    assert tools.DENY_COMMAND_RE.search("git reset --hard")
    assert tools.DENY_COMMAND_RE.search("Remove-Item foo")
    assert not tools.DENY_COMMAND_RE.search("echo hello")
    assert not tools.DENY_COMMAND_RE.search("python script.py")


@pytest.mark.parametrize(
    "command",
    [
        "git add ollama_cli/main.py",
        "git status; git add ollama_cli/main.py",
        "git commit -m update",
        "git restore ollama_cli/main.py",
        "git reset --hard",
        "git clean -fd",
        "Get-Content x | Set-Content y",
        "Add-Content x y",
        "Get-Content x | Out-File y",
        "Get-Process | Export-Csv state.csv",
        "Export-Clixml -Path state.xml -InputObject $x",
        "Move-Item x y",
        "Copy-Item x y",
        "Rename-Item x y",
        "powershell -Command \"Set-Content x 'quoted value'\"",
        "pwsh -c \"& { Remove-Item -LiteralPath 'x' }\"",
        "pwsh -c \"ni 'new-dir' -ItemType Directory\"",
        "python -c \"open('x.py', 'w').write('x')\"",
        "python -c \"open('x.py', mode='w')\"",
        "python -c \"from pathlib import Path; Path('x').write_text('x')\"",
        "python -c \"import os; os.replace('tmp', 'x')\"",
        "python -c \"import shutil; shutil.copy2('a', 'b')\"",
        "echo value > output.txt",
        "Get-Content x | tee y",
        "python -m pytest; Set-Content x y",
        "sed -i s/old/new/ file.py",
        "mkdir generated",
    ],
)
def test_shell_mutates_workspace_detects_mutation_commands(command):
    assert tools.shell_mutates_workspace(command)


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q",
        "python -m pytest -q 2>&1",
        "python -m pytest -q 2>$null",
        "ruff check . 2>NUL",
        "git diff --stat",
        "git status --short",
        "Get-Content file.py",
        "Select-String -Path file.py -Pattern status",
        "python3 -c \"with open('settings.json') as f: data = f.read(); assert data\"",
        "python3 -c \"with open('settings.json', mode='r') as f: data = f.read(); assert data\"",
        "python3 -c \"with open('settings.json', mode='rb') as f: data = f.read(); assert data\"",
    ],
)
def test_shell_mutates_workspace_allows_inspection_commands(command):
    assert not tools.shell_mutates_workspace(command)


def test_run_shell_safe_mode_blocks_destructive():
    out = tools.run_shell("rm -rf /tmp/something", safe_mode=True)
    assert "Blocked by safe mode" in out


@pytest.mark.parametrize(
    "command",
    [
        "python -c \"import shutil; shutil.rmtree('build')\"",
        "powershell -Command \"ri -Recurse -Force build\"",
        "cmd /c \"del output.txt\"",
        "robocopy source target /MIR",
    ],
)
def test_run_shell_safe_mode_uses_mutation_detector(command):
    out = tools.run_shell(command, safe_mode=True)

    assert "Blocked by safe mode" in out


def test_run_tool_passes_config_safe_mode_to_shell():
    cfg = Config()
    cfg.safe_mode = True

    out = tool_runtime.run_tool("run_shell", {"command": "rm -rf /tmp/something"}, cfg)

    assert "Blocked by safe mode" in out


def test_web_search_reads_pydantic_response_results_field(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "token")

    class SearchClient:
        def web_search(self, query, max_results=5):
            assert query == "algo cli"
            assert max_results == 2
            return SimpleNamespace(
                results=[
                    SimpleNamespace(title="First", url="https://one.test", content="alpha"),
                    {"title": "Second", "url": "https://two.test", "content": "beta"},
                ]
            )

    monkeypatch.setattr(tools, "active_ollama_client", lambda cloud=False: SearchClient())

    out = tools.web_search("algo cli", max_results=2)

    assert "### First" in out
    assert "https://one.test" in out
    assert "alpha" in out
    assert "### Second" in out
    assert "### (untitled)" not in out


def test_web_search_empty_results_field_reports_no_results(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "token")

    class SearchClient:
        def web_search(self, query, max_results=5):
            return SimpleNamespace(results=[])

    monkeypatch.setattr(tools, "active_ollama_client", lambda cloud=False: SearchClient())

    assert tools.web_search("nothing") == "No results found."


def test_web_search_preflights_missing_cloud_api_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(tools, "load_runtime_env", lambda **_kwargs: {})

    def fail_client(**_kwargs):
        raise AssertionError("web_search should not build a cloud client without OLLAMA_API_KEY")

    monkeypatch.setattr(tools, "active_ollama_client", fail_client)

    out = tools.web_search("algo cli")

    assert "OLLAMA_API_KEY is not set" in out
    assert "/doctor" in out


def test_web_fetch_preflights_missing_cloud_api_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(tools, "load_runtime_env", lambda **_kwargs: {})

    def fail_client(**_kwargs):
        raise AssertionError("web_fetch should not build a cloud client without OLLAMA_API_KEY")

    monkeypatch.setattr(tools, "active_ollama_client", fail_client)

    out = tools.web_fetch("https://example.test", timeout=1)

    assert "OLLAMA_API_KEY is not set" in out
    assert "/doctor" in out


def test_web_fetch_timeout_returns_without_waiting_for_worker(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "token")

    class SlowClient:
        def web_fetch(self, _url):
            time.sleep(2)
            return {"content": "late"}

    monkeypatch.setattr(tools, "active_ollama_client", lambda cloud=False: SlowClient())

    started = time.perf_counter()
    out = tools.web_fetch("https://example.test", timeout=1)
    elapsed = time.perf_counter() - started

    assert "timed out after 1 seconds" in out
    assert elapsed < 1.5


def test_failed_attempt_skip_is_not_self_perpetuating():
    cfg = Config()
    signature = tool_runtime.tool_attempt_signature("read_file", {"path": "missing.txt"})
    cfg.attempt_ledger.append({"signature": signature, "status": "failed", "timestamp": time.time(), "summary": "missing"})

    first = tool_runtime.find_failed_attempt(cfg, signature)
    assert first is not None

    cfg.attempt_ledger.append({"signature": signature, "status": "skipped", "timestamp": time.time(), "summary": "skipped"})

    assert tool_runtime.find_failed_attempt(cfg, signature) is None


def test_denied_attempt_does_not_block_retry():
    cfg = Config()
    signature = tool_runtime.tool_attempt_signature("run_shell", {"command": "pytest"})
    cfg.attempt_ledger.append({"signature": signature, "status": "denied", "timestamp": time.time(), "summary": "denied"})

    assert tool_runtime.find_failed_attempt(cfg, signature) is None


def test_successful_workspace_mutation_invalidates_cached_failures(tmp_path):
    cfg = Config(cwd=str(tmp_path))
    signature = tool_runtime.tool_attempt_signature("run_shell", {"command": "python3 healthcheck.py"})
    cfg.attempt_ledger.append(
        {"signature": signature, "status": "failed", "timestamp": time.time(), "summary": "failed"}
    )

    tool_runtime.record_tool_attempt(
        cfg,
        name="edit_file",
        args={"path": "settings.py"},
        result="Edited settings.py: replaced 1 occurrence(s)",
        status="worked",
    )

    assert tool_runtime.find_failed_attempt(cfg, signature) is None


def test_classify_tool_status_marks_tool_errors_failed():
    assert tool_runtime.classify_tool_status("Error: file not found: missing.txt") == "failed"
    assert tool_runtime.classify_tool_status("Tool error for read_file: boom") == "failed"
    assert tool_runtime.classify_tool_status("tests failed\n[exit code: 1]") == "failed"
    assert tool_runtime.classify_tool_status("tests passed\n[exit code: 0]") == "worked"


@pytest.mark.parametrize("command", ["shutdown -h now", "format C:", "diskpart /s wipe.txt"])
def test_safe_mode_blocks_host_destructive_commands(command):
    assert tools.shell_is_dangerous(command)
    assert tools.run_shell(command, safe_mode=True).startswith("Blocked by safe mode")


def test_session_command_requires_approval(monkeypatch):
    cfg = Config()
    cfg.safe_mode = True
    prompted: dict[str, object] = {}

    def fake_input(_prompt):
        prompted["called"] = True
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)

    approved = tool_runtime.ask_approval("session_command", {"command": "/safe off"}, cfg)

    assert approved is False
    assert prompted["called"] is True


def test_approve_all_this_session_does_not_persist(monkeypatch, config_dir):
    from algo_cli.config import CONFIG_FILE

    cfg = Config()
    cfg.auto_mode = False
    cfg.save()

    monkeypatch.setattr("builtins.input", lambda _prompt: "a")

    approved = tool_runtime.ask_approval("run_shell", {"command": "echo hi"}, cfg)

    assert approved is True
    assert cfg.auto_mode is False
    assert cfg.session_auto_approve is True
    assert cfg.auto_approve_active is True

    # agent_loop saves cfg at the end of every turn; the flag must survive that.
    cfg.save()
    saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["auto_mode"] is False
    assert "session_auto_approve" not in saved

    # A new session starts with approvals required again.
    reloaded = Config.load()
    assert reloaded.auto_mode is False
    assert reloaded.session_auto_approve is False


def test_approve_all_skips_prompt_for_rest_of_session(monkeypatch):
    cfg = Config()
    answers = iter(["a"])

    def fake_input(_prompt):
        return next(answers)  # raises StopIteration if prompted again

    monkeypatch.setattr("builtins.input", fake_input)

    assert tool_runtime.ask_approval("run_shell", {"command": "echo hi"}, cfg) is True
    assert tool_runtime.ask_approval("write_file", {"path": "x", "content": "y"}, cfg) is True


def test_repeated_failed_tool_call_is_skipped_after_runtime_defaults(tmp_path, monkeypatch):
    cfg = Config(cwd=str(tmp_path))
    monkeypatch.setattr(tool_runtime, "ask_approval", lambda *a, **k: True)
    monkeypatch.setattr(tool_runtime, "show_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(tool_runtime, "show_tool_result", lambda *a, **k: None)
    monkeypatch.setattr(tool_runtime, "record_perf_event", lambda *a, **k: None)

    args = {"path": "missing.txt"}
    first_msg, first_result = tool_runtime.execute_tool_call_for_pipeline("read_file", dict(args), cfg)
    second_msg, second_result = tool_runtime.execute_tool_call_for_pipeline("read_file", dict(args), cfg)

    assert "Error: file not found" in first_result
    assert "Skipped repeated failed attempt" in second_result
    assert first_msg["role"] == "tool"
    assert second_msg["role"] == "tool"


def test_attempt_signature_excludes_config_and_conversation(monkeypatch, config_dir):
    from algo_cli.config import CONFIG_FILE

    cfg = Config()
    cfg.messages = [{"role": "user", "content": "PRIVATE-CONVERSATION-MARKER " * 50}]
    monkeypatch.setattr(tool_runtime, "show_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(tool_runtime, "show_tool_result", lambda *a, **k: None)
    monkeypatch.setattr(tool_runtime, "record_perf_event", lambda *a, **k: None)

    runtime_args = tool_runtime.tool_runtime_args("remember", {"fact": "user likes tea"}, cfg)
    assert "cfg" not in runtime_args

    tool_runtime.execute_tool_call_for_pipeline("remember", {"fact": "user likes tea"}, cfg)
    entry = cfg.attempt_ledger[-1]
    assert "PRIVATE-CONVERSATION-MARKER" not in entry["signature"]
    assert len(entry["signature"]) < 200

    cfg.save()
    assert "PRIVATE-CONVERSATION-MARKER" not in CONFIG_FILE.read_text(encoding="utf-8")

    # Signature must be stable as the conversation grows, or dedupe never matches.
    cfg.messages.append({"role": "assistant", "content": "reply"})
    later_args = tool_runtime.tool_runtime_args("remember", {"fact": "user likes tea"}, cfg)
    assert tool_runtime.tool_attempt_signature("remember", later_args) == entry["signature"]


def test_run_tool_still_passes_config_to_cfg_bound_tools(config_dir):
    cfg = Config()

    result = tool_runtime.run_tool("remember", {"fact": "prefers dark mode"}, cfg)

    assert "Error" not in result
    assert any("prefers dark mode" in fact for fact in cfg.memories)


def test_read_only_session_command_skips_approval(monkeypatch):
    cfg = Config()
    prompted: dict[str, object] = {}

    def fake_input(_prompt):
        prompted["called"] = True
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)

    assert tool_runtime.ask_approval("session_command", {"command": "/status"}, cfg) is True
    assert tool_runtime.ask_approval("session_command", {"command": "/safe status"}, cfg) is True
    assert tool_runtime.ask_approval("session_command", {"command": "/reason guide"}, cfg) is True
    assert tool_runtime.ask_approval("session_command", {"command": "/agent threads"}, cfg) is True
    assert tool_runtime.ask_approval("session_command", {"command": "/agent show abc123"}, cfg) is True
    assert prompted == {}


def test_empty_toggle_session_commands_require_approval():
    for command in ("/auto", "/cloud", "/cloudauto", "/safe", "/thinking", "/verify"):
        assert tool_runtime.session_command_requires_approval(command) is True
        assert tool_runtime.session_command_requires_approval(f"{command} status") is False

    assert tool_runtime.session_command_requires_approval("/memory-auto") is False
    assert tool_runtime.session_command_requires_approval("/memory-auto status") is False
    assert tool_runtime.session_command_requires_approval("/memory-auto on") is True
    assert tool_runtime.session_command_requires_approval("/memory-auto off") is True
    assert tool_runtime.session_command_requires_approval("/code-rag") is False
    assert tool_runtime.session_command_requires_approval("/code-rag status") is False
    assert tool_runtime.session_command_requires_approval("/code-rag on") is True
    assert tool_runtime.session_command_requires_approval("/code-rag off") is True


def test_model_invoked_agent_execution_requires_approval(monkeypatch):
    cfg = Config()
    prompted: dict[str, object] = {}

    def fake_input(_prompt):
        prompted["called"] = True
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)

    approved = tool_runtime.ask_approval(
        "session_command",
        {"command": "/agent team Review the runtime"},
        cfg,
    )

    assert approved is False
    assert prompted["called"] is True


def test_available_actions_slash_guidance():
    out = tools.available_actions("slash")

    assert "slash_command_guidance" in out
    assert "session_command" in out
    assert "/auto [on|off|status]" in out
    assert "Writing '/command'" in out


def test_available_actions_points_to_reviewed_algo_doc():
    out = tools.available_actions("harness")

    assert "docs/ALGO.md" in out
    assert "update" in out


def test_available_actions_exposes_harness_maintenance_loop():
    payload = json.loads(tools.available_actions("harness"))

    commands = payload["commands"]["harness"]
    assert "/harness status" in commands
    assert "/harness refresh" in commands
    assert "/harness embed" in commands
    assert "/harness score" in commands
    assert "/harness compare" in commands
    assert "/hsearch QUERY" in commands
    assert "/hread RECORD_ID" in commands
    assert any("/harness status" in item and "/harness embed" in item for item in payload["slash_command_guidance"])
    assert any("/harness status" in item and "/harness embed" in item for item in payload["model_callable_tools"]["session"])
    assert any("harness_stats" in item and "harness_refresh" in item for item in payload["verification_layer"])


def test_available_actions_exposes_runtime_agent_threads():
    payload = json.loads(tools.available_actions("agent"))

    commands = payload["commands"]["agent"]
    assert "/agent team [--roles ROLE,ROLE[,ROLE,ROLE]] TASK" in commands
    assert "/agent threads" in commands
    assert "/agent resume THREAD [TASK]" in commands
    assert any("2-4" in item and "read-only" in item for item in payload["slash_command_guidance"])


def test_plugin_tool_wrappers_serialize_and_load_discovered_manifests(monkeypatch, tmp_path):
    from algo_cli import plugins

    root = tmp_path / "plugins"
    plugin_dir = root / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "version": "1.2.3",
                "description": "Demo plugin",
                "enabled": True,
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text("PLUGIN_READY = True\n", encoding="utf-8")
    monkeypatch.setattr(plugins, "PLUGINS_DIR", root)

    discovered = json.loads(tools.plugins_discover())
    loaded = json.loads(tools.plugins_load("DEMO"))

    assert discovered[0]["name"] == "demo"
    assert discovered[0]["version"] == "1.2.3"
    assert loaded["loaded"] is True
    assert loaded["name"] == "demo"
    assert loaded["path"] == "plugins/demo"
    assert str(tmp_path) not in json.dumps(loaded)


def test_version_manifest_tool_uses_manifest_as_dict(monkeypatch):
    from algo_cli import version_manifest

    class Manifest:
        def as_dict(self):
            return {"cli_version": "test", "harness_record_count": 7}

    monkeypatch.setattr(version_manifest, "build_manifest", lambda: Manifest())

    assert json.loads(tools.version_manifest_build()) == {
        "cli_version": "test",
        "harness_record_count": 7,
    }


def test_url_scheme_tool_returns_valid_and_invalid_descriptors():
    valid = json.loads(tools.url_scheme_parse("algo-cli://skill/example"))
    invalid = json.loads(tools.url_scheme_parse("https://example.com"))

    assert valid["valid"] is True
    assert valid["action"] == "skill"
    assert valid["target"] == "example"
    assert invalid["valid"] is False


def test_credential_tools_round_trip_through_named_helper_without_plaintext(monkeypatch):
    from algo_cli import credential_helpers

    class FakeHelper(credential_helpers.CredentialHelper):
        def __init__(self):
            self.values: dict[str, str] = {}

        @property
        def name(self):
            return "fake-test"

        def get(self, key):
            return self.values.get(key)

        def store(self, key, value):
            self.values[key] = value

        def erase(self, key):
            self.values.pop(key, None)

        def list_keys(self):
            return sorted(self.values)

    monkeypatch.setitem(credential_helpers._REGISTRY, "fake-test", FakeHelper())

    stored = tools.credential_helpers_store("fake-test", "TOKEN", "super-secret")
    result = tools.credential_helpers_get("fake-test", "TOKEN")

    assert json.loads(stored) == {"helper": "fake-test", "key": "TOKEN", "stored": True}
    assert json.loads(result) == {
        "helper": "fake-test",
        "key": "TOKEN",
        "found": True,
        "value": "<redacted>",
    }
    assert "super-secret" not in stored
    assert "super-secret" not in result


def test_credential_store_reports_unknown_helper():
    result = tools.credential_helpers_store("missing-helper", "TOKEN", "super-secret")

    assert json.loads(result) == {
        "helper": "missing-helper",
        "key": "TOKEN",
        "stored": False,
    }
    assert "super-secret" not in result


def test_sensitive_tool_args_are_redacted_from_attempt_metadata():
    args = {"key": "TOKEN", "value": "super-secret"}

    signature = tool_runtime.tool_attempt_signature("credential_helpers_store", args)
    preview = tool_runtime.run_args_preview(args, name="credential_helpers_store")

    assert "super-secret" not in signature
    assert "super-secret" not in preview
    assert "redacted" in signature


def test_plugin_load_and_credential_store_require_approval(monkeypatch, capsys):
    cfg = Config()
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")

    assert tool_runtime.ask_approval("plugins_load", {"plugin_name": "demo"}, cfg) is False
    assert tool_runtime.ask_approval(
        "credential_helpers_store",
        {"helper": "env", "key": "TOKEN", "value": "super-secret"},
        cfg,
    ) is False
    assert len(prompts) == 2
    assert "super-secret" not in capsys.readouterr().out


def test_harness_scorecard_reports_rating_file_criteria(monkeypatch):
    from algo_cli import action_registry
    from algo_cli.evals import algorithm_effectiveness, harness_retrieval_benchmark

    monkeypatch.setattr(
        tools,
        "_collect_harness_index_integrity",
        lambda: {
            "status": "pass",
            "record_count": 332,
            "declared_count": 332,
            "fingerprint": "abc123",
            "checks": {"unique_ids": True, "source_current": True},
            "failed": [],
        },
    )

    stats_payload = {
        "record_count": 332,
        "quality": {
            "status": "ready",
            "memory_records": 3,
            "curated_product_memory_records": 3,
            "required_product_memory_categories": [
                "memory-lifecycle",
                "execution-verification",
                "algorithm-evidence",
            ],
            "covered_product_memory_categories": [
                "memory-lifecycle",
                "execution-verification",
                "algorithm-evidence",
            ],
            "missing_product_memory_categories": [],
            "wiki_records": 9,
            "extension_share": 0.325,
            "project_specific_share": 0.30,
            "embedding_complete": True,
            "recommendations": [],
        },
        "embeddings": {
            "active_model": "embed-model",
            "embedded_count": 332,
            "pending_count": 0,
            "high_value_pending": 0,
            "complete": True,
        },
        "echo_veil": {
            "installed": False,
            "enabled": False,
            "write_wired": False,
            "retrieval_wired": False,
            "persistence_wired": False,
            "readiness_source": "algo_cli.memory_echo_veil.get_echo_veil_readiness",
            "runtime": "cpython-test",
        },
        "runtime_event_store": {
            "status": "ready",
            "initialized": True,
            "directory_private": True,
            "file_private": True,
            "lock_private": True,
            "compaction_needed": False,
        },
    }
    monkeypatch.setattr(tools.harness, "stats", lambda: stats_payload)
    monkeypatch.setattr(
        tools.harness,
        "search_index",
        lambda query, limit=5: [
            {"id": "algo-cli:algorithm:ALGO.md", "harness": "algo-cli", "kind": "algorithm"},
            {"id": "algo-cli:skill:harness-search-first.md", "harness": "algo-cli", "kind": "skill"},
        ],
    )
    monkeypatch.setattr(tools, "query_knowledge_graph", lambda _query: "project:algo-cli  (187 edges)")
    monkeypatch.setattr(
        harness_retrieval_benchmark,
        "run_harness_retrieval_benchmark",
        lambda: {
            "benchmark_version": "harness-retrieval-v1",
            "status": "pass",
            "reason": "canaries and reusable-index benchmark passed",
            "correctness": {"passed": True, "stable_rankings": True},
            "performance": {"speedup": 2.5, "warm_mad_ratio": 0.01},
            "evidence": {"index_digest": "bench123"},
        },
    )
    monkeypatch.setattr(
        algorithm_effectiveness,
        "run_algorithm_effectiveness_probe",
        lambda: {
            "schema_version": 1,
            "probe": "harness-algorithm-effectiveness-v2",
            "status": "pass",
            "reason": "",
            "required_checks": [
                "bm25_lexical", "exact_vector", "rrf_fusion", "stable_top_k",
                "window_tinylfu", "embedding_priority", "memory_admission",
            ],
            "summary": {"required": 7, "passed": 7, "failed": 0},
            "checks": {name: {"status": "pass", "required": True} for name in (
                "bm25_lexical", "exact_vector", "rrf_fusion", "stable_top_k",
                "window_tinylfu", "embedding_priority", "memory_admission",
            )},
        },
    )

    class _Audit:
        overall_status = "ready"
        findings = ()

    monkeypatch.setattr(action_registry, "audit_action_registry_runtime", lambda: _Audit())
    monkeypatch.setattr(
        action_registry,
        "build_doctor_report",
        lambda _cfg: SimpleNamespace(
            overall_status="ready",
            findings=(SimpleNamespace(area="web-tools", status="ready", message="web ready"),),
        ),
    )

    payload = json.loads(tools.harness_scorecard())

    assert payload["score"] == 10
    assert payload["max_score"] == 10
    assert payload["schema_version"] == 2
    assert payload["overall_status"] == "ready"
    assert payload["scored_gate_count"] == 10
    assert payload["validation_errors"] == []
    statuses = {check["name"]: check["status"] for check in payload["checks"]}
    assert statuses["index integrity"] == "pass"
    assert statuses["embedding readiness"] == "pass"
    assert statuses["project memory/wiki coverage"] == "pass"
    assert statuses["corpus signal balance"] == "pass"
    assert statuses["meta-query retrieval"] == "pass"
    assert statuses["knowledge graph"] == "pass"
    assert statuses["action registry runtime audit"] == "pass"
    assert statuses["harness maintenance loop"] == "pass"
    assert statuses["retrieval benchmark"] == "pass"
    assert statuses["algorithm effectiveness"] == "pass"
    assert all(check["points"] == 1.0 for check in payload["checks"])
    capabilities = {item["name"]: item for item in payload["capabilities"]}
    assert capabilities["web tools"]["status"] == "pass"
    assert capabilities["google workspace wiring"]["status"] == "pass"
    assert all(item["scored"] is False for item in capabilities.values())

    stats_payload["echo_veil"]["enabled"] = True
    enabled_but_unwired = json.loads(tools.harness_scorecard())
    enabled_statuses = {
        check["name"]: check["status"] for check in enabled_but_unwired["checks"]
    }
    assert enabled_but_unwired["score"] == 9.0
    assert enabled_but_unwired["overall_status"] == "blocked"
    assert enabled_statuses["project memory/wiki coverage"] == "fail"
    stats_payload["echo_veil"]["enabled"] = False

    stats_payload["runtime_event_store"]["file_private"] = False
    unsafe_store = json.loads(tools.harness_scorecard())
    unsafe_statuses = {check["name"]: check["status"] for check in unsafe_store["checks"]}
    assert unsafe_store["score"] == 9.0
    assert unsafe_store["overall_status"] == "blocked"
    assert unsafe_statuses["project memory/wiki coverage"] == "fail"
    stats_payload["runtime_event_store"]["file_private"] = True

    monkeypatch.setattr(
        harness_retrieval_benchmark,
        "run_harness_retrieval_benchmark",
        lambda: {"status": "unavailable", "reason": "no current benchmark"},
    )
    monkeypatch.setattr(
        algorithm_effectiveness,
        "run_algorithm_effectiveness_probe",
        lambda: {"status": "unavailable", "reason": "no current algorithm evidence"},
    )

    wiring_only = json.loads(tools.harness_scorecard())

    wiring_statuses = {check["name"]: check["status"] for check in wiring_only["checks"]}
    assert wiring_only["score"] == 8.0
    assert wiring_only["overall_status"] == "degraded"
    assert wiring_statuses["retrieval benchmark"] == "unavailable"
    assert wiring_statuses["algorithm effectiveness"] == "unavailable"

    monkeypatch.setattr(
        harness_retrieval_benchmark,
        "run_harness_retrieval_benchmark",
        lambda: {"status": "pass"},
    )
    monkeypatch.setattr(
        algorithm_effectiveness,
        "run_algorithm_effectiveness_probe",
        lambda: {"status": "pass"},
    )

    counterfeit_pass = json.loads(tools.harness_scorecard())

    counterfeit_statuses = {
        check["name"]: check["status"] for check in counterfeit_pass["checks"]
    }
    assert counterfeit_pass["score"] == 8.0
    assert counterfeit_pass["overall_status"] == "blocked"
    assert counterfeit_statuses["retrieval benchmark"] == "fail"
    assert counterfeit_statuses["algorithm effectiveness"] == "fail"


def test_harness_index_integrity_rejects_duplicate_ids_and_nonfinite_vectors(monkeypatch):
    records = [
        {
            "id": "duplicate",
            "harness": "algo-cli",
            "kind": "wiki",
            "path": "/tmp/a.md",
            "embedding_model": "m",
            "embedding": [1.0, 0.0],
        },
        {
            "id": "duplicate",
            "harness": "algo-cli",
            "kind": "wiki",
            "path": "/tmp/b.md",
            "embedding_model": "m",
            "embedding": [float("nan"), 0.0],
        },
    ]
    monkeypatch.setattr(
        tools.harness,
        "load_index",
        lambda: {
            "generated": "2026-07-10T00:00:00",
            "record_count": 2,
            "roots": [{"root": "/tmp"}],
            "records": records,
        },
    )
    monkeypatch.setattr(tools.harness, "index_is_stale", lambda **_kwargs: False)

    report = tools._collect_harness_index_integrity()

    assert report["status"] == "fail"
    assert "unique_ids" in report["failed"]
    assert "embedding_vectors_well_formed" in report["failed"]


def test_harness_index_integrity_allows_dimensions_to_differ_by_model(monkeypatch):
    records = [
        {
            "id": "a",
            "harness": "algo-cli",
            "kind": "wiki",
            "path": "/tmp/a.md",
            "embedding_model": "model-a",
            "embedding": [1.0, 0.0],
        },
        {
            "id": "b",
            "harness": "algo-cli",
            "kind": "wiki",
            "path": "/tmp/b.md",
            "embedding_model": "model-b",
            "embedding": [1.0, 0.0, 0.0],
        },
    ]
    monkeypatch.setattr(
        tools.harness,
        "load_index",
        lambda: {
            "generated": "2026-07-10T00:00:00",
            "record_count": 2,
            "roots": [{"root": "/tmp"}],
            "records": records,
        },
    )
    monkeypatch.setattr(tools.harness, "index_is_stale", lambda **_kwargs: False)

    report = tools._collect_harness_index_integrity()

    assert report["status"] == "pass"
    assert report["embedding_dimensions"] == {"model-a": [2], "model-b": [3]}


def test_show_help_contains_current_slash_commands(monkeypatch):
    from algo_cli import display
    from algo_cli import slash_dispatch

    output = StringIO()
    theme_name = getattr(display, "_active_theme_name", "tokyo-night")
    test_console = Console(
        file=output,
        force_terminal=False,
        color_system=None,
        theme=display.THEME_MAP.get(theme_name, display.THEME_MAP["tokyo-night"]),
    )
    monkeypatch.setattr(display, "console", test_console)

    display.show_help()

    text = output.getvalue()
    for command, _ in slash_dispatch.SLASH_COMMANDS:
        assert command in text


def test_available_actions_reason_guidance():
    out = tools.available_actions("reason")

    assert "reasoning_mode_guidance" in out
    assert "/reason guide" in out
    assert "neuro-symbolic" in out
    assert "routine reads" in out


def test_available_actions_focuses_high_roi_runtime_topics():
    agent = tools.available_actions("agent")
    intel = tools.available_actions("intel")
    google = tools.available_actions("google")
    chatgpt = tools.available_actions("chatgpt")

    assert "/agent help" in agent
    assert "/agent [--pipeline NAME] TASK" in agent
    assert "/route TASK" in agent
    assert "/intel query TERM" in intel
    assert "/intel reindex" in intel
    assert "/google-login" in google
    assert "/google drive-list" in google
    assert "/chatgpt-login" in chatgpt
    assert "/chatgpt-status" in chatgpt


def test_run_shell_allows_benign():
    out = tools.run_shell("echo ci-smoke-test", safe_mode=True)
    assert "ci-smoke-test" in out
    assert "exit code: 0" in out


def test_isolated_process_group_kwargs_are_portable(monkeypatch):
    assert tools._isolated_process_group_kwargs("posix") == {
        "start_new_session": True
    }

    monkeypatch.delattr(
        tools.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        raising=False,
    )
    assert tools._isolated_process_group_kwargs("nt") == {
        "creationflags": 0x00000200
    }


def test_git_tools_run_read_only_status_and_diff_commands(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="ollama_cli/main.py\n", stderr="")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    assert "ollama_cli/main.py" in tools.git_status(cwd=str(tmp_path))
    assert "ollama_cli/main.py" in tools.git_diff(cwd=str(tmp_path), names_only=True)

    assert calls[0] == ["git", "status", "--short", "--branch"]
    assert calls[1] == ["git", "diff", "--no-ext-diff", "--name-only", "HEAD"]


def test_read_file_missing_and_present(tmp_path):
    missing = tools.read_file(str(tmp_path / "nope.txt"))
    assert missing.startswith("Error")
    target = tmp_path / "ok.txt"
    target.write_text("file body", encoding="utf-8")
    assert tools.read_file(str(target)) == "file body"


def test_read_file_caps_model_requested_max_chars(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text("x" * (tools.MAX_READ_CHARS + 100), encoding="utf-8")

    out = tools.read_file(str(target), max_chars=10**9)

    assert len(out) == tools.MAX_READ_CHARS


def test_unpack_embed_response_dict_and_object():
    payload = tools.unpack_embed_response(
        {
            "model": "m1",
            "embeddings": [[1.0, 2.0, 3.0]],
            "total_duration": 10,
            "load_duration": 5,
            "prompt_eval_count": 1,
        },
        "fallback",
        "hello",
        truncate=True,
        dimensions=3,
    )
    assert payload["model"] == "m1"
    assert payload["vector_count"] == 1
    assert payload["vector_length"] == 3
    assert payload["preview"] == [1.0, 2.0, 3.0]
    assert payload["truncate"] is True
    assert payload["dimensions"] == 3

    class _Resp:
        model = "m2"
        embeddings = [[4.0]]
        total_duration = 20
        load_duration = 6
        prompt_eval_count = 2

    payload2 = tools.unpack_embed_response(_Resp(), "fallback", "x")
    assert payload2["model"] == "m2"
    assert payload2["vector_length"] == 1


def test_embed_text_accepts_object_response_from_current_ollama_client(monkeypatch):
    class EmbedResponse:
        model = "embeddinggemma"
        embeddings = [[0.25, 0.75]]
        total_duration = 12
        load_duration = 3
        prompt_eval_count = 2

    class EmbedClient:
        def embed(self, **_kwargs):
            return EmbedResponse()

    monkeypatch.setattr(tools, "gateway_embed", lambda *_args: None)
    monkeypatch.setattr(tools, "active_ollama_client", lambda **_kwargs: EmbedClient())

    payload = json.loads(tools.embed_text("hello", dimensions=2))

    assert payload["model"] == "embeddinggemma"
    assert payload["vector_count"] == 1
    assert payload["preview"] == [0.25, 0.75]


def test_model_create_translates_modelfile_to_current_ollama_api(monkeypatch):
    calls: list[dict[str, object]] = []

    class CreateClient:
        def create(self, **kwargs):
            calls.append(kwargs)
            return iter(
                [
                    {"status": "reading model metadata"},
                    SimpleNamespace(status="success"),
                ]
            )

    monkeypatch.setattr(
        tools,
        "active_ollama_client",
        lambda **_kwargs: CreateClient(),
    )
    modelfile = '''
FROM llama3.2:latest
SYSTEM """
You are concise.
"""
TEMPLATE """{{ .System }} {{ .Prompt }}"""
PARAMETER temperature 0.25
PARAMETER num_ctx 8192
PARAMETER stop "END"
PARAMETER stop "DONE"
MESSAGE user Hello
MESSAGE assistant Hi
LICENSE MIT
'''

    result = tools.model_create("concise:latest", modelfile)

    assert result == "Created model concise:latest: success"
    assert calls == [
        {
            "model": "concise:latest",
            "from_": "llama3.2:latest",
            "template": "{{ .System }} {{ .Prompt }}",
            "license": "MIT",
            "system": "You are concise.",
            "parameters": {
                "temperature": 0.25,
                "num_ctx": 8192,
                "stop": ["END", "DONE"],
            },
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ],
            "stream": True,
        }
    ]


def test_model_create_rejects_unsupported_modelfile_instruction(monkeypatch):
    monkeypatch.setattr(
        tools,
        "active_ollama_client",
        lambda **_kwargs: pytest.fail("invalid Modelfile must not contact Ollama"),
    )

    result = tools.model_create(
        "bad:latest",
        "FROM llama3.2\nADAPTER ./adapter.gguf",
    )

    assert "unsupported Modelfile instruction 'ADAPTER'" in result


def test_read_file_supports_line_offset_alias(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("line 1\nline 2\ndef handle_command():\n    pass\n", encoding="utf-8")

    assert tools.read_file(str(target), offset=3).startswith("def handle_command")
    assert tools.read_file(str(target), start_line=2).startswith("line 2")


def test_list_directory(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    out = tools.list_directory(str(tmp_path))
    assert "a.txt" in out
    assert "sub/" in out


def test_search_files_fallback_caps(tmp_path, monkeypatch):
    # Force the rg-unavailable fallback path.
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def needle():\n    return 1\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("needle in node_modules\n", encoding="utf-8")
    big = tmp_path / "big.log"
    big.write_text("needle\n" + ("x" * (tools.SEARCH_FALLBACK_MAX_FILE_BYTES + 100)), encoding="utf-8")

    out = tools.search_files("needle", path=str(tmp_path))
    assert "app.py" in out
    assert "node_modules" not in out          # heavy dir pruned
    assert "big.log" not in out               # over the size cap


def test_search_files_supports_single_file_target(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("line 1\ndef handle_command():\n    pass\n", encoding="utf-8")

    out = tools.search_files("def handle_command", path=str(target))

    assert f"{target}:2:def handle_command():" in out


def test_search_files_reports_rg_error_as_error(tmp_path, monkeypatch):
    class Proc:
        returncode = 2
        stdout = ""
        stderr = "regex parse error"

    monkeypatch.setattr(tools.shutil, "which", lambda name: "rg")
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: Proc())

    out = tools.search_files("[", path=str(tmp_path))

    assert out.startswith("Error searching:")
    assert "regex parse error" in out


def test_session_command_allows_runtime_agent_delegation(monkeypatch):
    from algo_cli import agent_pipeline, runtime_services

    cfg = Config()
    calls: list[tuple[str, object, object]] = []
    client = object()
    monkeypatch.setattr(runtime_services, "create_client", lambda _cfg: client)
    monkeypatch.setattr(
        agent_pipeline,
        "execute_agent_command",
        lambda arg, received_cfg, received_client: calls.append((arg, received_cfg, received_client)) or "delegated",
    )

    out = tools.session_command("/agent team review auth", cfg=cfg)

    assert out == "delegated"
    assert calls == [("team review auth", cfg, client)]


def test_session_command_rejects_recursive_agent_delegation(monkeypatch):
    from algo_cli import agent_pipeline

    monkeypatch.setattr(agent_pipeline._execution_state, "depth", 1, raising=False)

    out = tools.session_command("/agent do nested work", cfg=Config())

    assert "recursive /agent delegation is blocked" in out


def test_scoped_tool_runtime_env_restores_ollama_host(monkeypatch):
    cfg = Config()
    cfg.host = "http://new-host:11434"
    monkeypatch.setenv("OLLAMA_HOST", "http://old-host:11434")

    with scoped_tool_runtime_env(cfg):
        assert os.environ["OLLAMA_HOST"] == "http://new-host:11434"

    assert os.environ["OLLAMA_HOST"] == "http://old-host:11434"


def test_scoped_tool_runtime_env_keeps_shared_values_until_last_thread_exits(monkeypatch):
    import threading

    monkeypatch.setenv("OLLAMA_HOST", "before")
    cfg = Config(host="http://shared:11434")
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    observed: list[str | None] = []

    def worker(entered: threading.Event, release: threading.Event) -> None:
        with scoped_tool_runtime_env(cfg):
            entered.set()
            assert release.wait(timeout=2)
            observed.append(os.environ.get("OLLAMA_HOST"))

    first = threading.Thread(target=worker, args=(first_entered, release_first))
    second = threading.Thread(target=worker, args=(second_entered, release_second))
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    assert second_entered.wait(timeout=2)
    release_first.set()
    first.join(timeout=2)
    assert os.environ.get("OLLAMA_HOST") == "http://shared:11434"
    release_second.set()
    second.join(timeout=2)

    assert observed == ["http://shared:11434", "http://shared:11434"]
    assert os.environ.get("OLLAMA_HOST") == "before"


def test_scoped_tool_runtime_env_serializes_different_values(monkeypatch):
    import threading

    monkeypatch.setenv("OLLAMA_HOST", "before")
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    observed: list[str | None] = []

    def first_worker() -> None:
        with scoped_tool_runtime_env(Config(host="http://first:11434")):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second_worker() -> None:
        with scoped_tool_runtime_env(Config(host="http://second:11434")):
            observed.append(os.environ.get("OLLAMA_HOST"))
            second_entered.set()

    first = threading.Thread(target=first_worker)
    second = threading.Thread(target=second_worker)
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    assert not second_entered.wait(timeout=0.05)
    release_first.set()
    first.join(timeout=2)
    assert second_entered.wait(timeout=2)
    second.join(timeout=2)

    assert observed == ["http://second:11434"]
    assert os.environ.get("OLLAMA_HOST") == "before"


def test_query_knowledge_graph_invokes_public_cli_with_bounded_limit(tmp_path, monkeypatch):
    from algo_cli import index_compute_lab

    root = tmp_path / "index-compute-lab"
    atoms = root / "atoms"
    atoms.mkdir(parents=True)
    (root / "query.py").write_text("# query entrypoint\n", encoding="utf-8")
    (atoms / "ranked-association-map.json").write_text("{}", encoding="utf-8")
    (atoms / "alias-table.json").write_text("{}", encoding="utf-8")
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="person:alex-rivera\n", stderr="")

    monkeypatch.setattr(index_compute_lab, "resolve_lab_root", lambda: root)
    monkeypatch.setattr("algo_cli.index_compute_lab.subprocess.run", fake_run)

    out = tools.query_knowledge_graph("what is alex about?", limit=999)

    assert "person:alex-rivera" in out
    assert captured["command"][1:] == [str(root / "query.py"), "ask", "what is alex about?", "--limit", "20"]
    assert captured["cwd"] == root
    assert captured["timeout"] == 20


def test_query_knowledge_graph_reports_missing_assets(tmp_path, monkeypatch):
    from algo_cli import index_compute_lab

    monkeypatch.setattr(index_compute_lab, "resolve_lab_root", lambda: tmp_path)

    out = tools.query_knowledge_graph("what is alex about?")

    assert out.startswith("Error: index-compute-lab not ready")


def test_query_knowledge_graph_expands_harness_meta_query_when_no_canonicals(monkeypatch):
    calls: list[str] = []

    def fake_run_ask(question: str, **_kwargs) -> str:
        calls.append(question)
        if question == "rate your harness":
            return "No matching canonicals."
        return "project:algo-cli  (187 edges, 1 aggregated)"

    monkeypatch.setattr(tools._index_compute_lab, "run_ask", fake_run_ask)

    out = tools.query_knowledge_graph("rate your harness", limit=5)

    assert "project:algo-cli" in out
    assert calls == ["rate your harness", "Algo CLI harness self-evaluation capability audit"]


def test_x_search_requires_auth(monkeypatch):
    from algo_cli import xai_auth

    monkeypatch.setattr(xai_auth, "get_valid_token", lambda: None)
    out = tools.x_search("anything")
    assert "Run /xai-login" in out


def test_x_search_rejects_empty_query(monkeypatch):
    from algo_cli import xai_auth

    monkeypatch.setattr(xai_auth, "get_valid_token", lambda: "fake-token")
    assert "empty" in tools.x_search("").lower()
    assert "empty" in tools.x_search("   ").lower()


def test_x_search_writes_cache_and_returns_summary(monkeypatch, config_dir):
    from algo_cli import xai_auth, xai_client

    monkeypatch.setattr(xai_auth, "get_valid_token", lambda: "fake-token")

    class _FakeClient:
        def search(self, *, query: str, sources, max_results: int):
            assert query == "ollama releases"
            assert sources == [{"type": "x"}]
            return {
                "content": "Recent posts mention v0.6 and Gemini-3 routing.",
                "citations": ["https://x.com/u/1", "https://x.com/u/2"],
            }

    monkeypatch.setattr(xai_client, "active_xai_client", lambda: _FakeClient())
    out = tools.x_search("ollama releases", max_results=5)

    assert "Recent posts" in out
    assert "https://x.com/u/1" in out
    cache_dir = config_dir / "x_search_cache"
    files = list(cache_dir.glob("*.md"))
    assert files, "expected an x_search_cache .md file"
    body = files[0].read_text(encoding="utf-8")
    assert "kind: x_search" in body
    assert "Recent posts" in body
    assert "https://x.com/u/2" in body


def test_x_search_clamps_max_results(monkeypatch, config_dir):
    from algo_cli import xai_auth, xai_client

    monkeypatch.setattr(xai_auth, "get_valid_token", lambda: "fake-token")
    captured: dict = {}

    class _FakeClient:
        def search(self, *, query: str, sources, max_results: int):
            captured["max_results"] = max_results
            return {"content": "ok", "citations": []}

    monkeypatch.setattr(xai_client, "active_xai_client", lambda: _FakeClient())
    tools.x_search("q", max_results=999)
    assert captured["max_results"] == 30
    tools.x_search("q", max_results=0)
    assert captured["max_results"] == 1


def test_x_account_draft_tools():
    post = tools.x_account_draft_post("hello X")
    assert "compose/post" in post
    reply = tools.x_account_draft_reply("https://x.com/u/status/123", "agreed")
    assert "in_reply_to=123" in reply


def test_x_account_post_tool_blocks_without_confirm(monkeypatch):
    from ollama_cli import x_account

    monkeypatch.setattr(x_account, "_run_xurl", lambda *args, **kwargs: pytest.fail("should not run xurl"))
    out = tools.x_account_post("hello", confirm=False)
    assert "Blocked write" in out


def test_x_account_post_action_tool_blocks_without_confirm(monkeypatch):
    from ollama_cli import x_account

    monkeypatch.setattr(x_account, "_run_xurl", lambda *args, **kwargs: pytest.fail("should not run xurl"))
    out = tools.x_account_post_action("like", "123", confirm=False)
    assert "Blocked write" in out
