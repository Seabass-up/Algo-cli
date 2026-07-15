"""Pure helpers in main.py plus an import smoke check."""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from algo_cli.config import CODE_RAG_CONSENT_VERSION, Config
from algo_cli import main
from algo_cli import model_routing
from algo_cli import action_registry
from algo_cli import context_budget
from algo_cli import updater


def test_imports_ok():
    # Importing main pulls in tools, harness, identity, skills, display.
    import algo_cli.tools  # noqa: F401
    import algo_cli.harness  # noqa: F401
    import algo_cli.identity  # noqa: F401
    import algo_cli.skills  # noqa: F401


def test_update_command_exits_before_runtime_state_initialization(monkeypatch):
    result = updater.UpdateResult(
        returncode=0,
        manager="pip",
        before_version="0.15.0",
        after_version="0.16.0",
        message="Updated Algo CLI 0.15.0 → 0.16.0. Restart the command to use the new version.",
    )
    rendered: list[str] = []
    monkeypatch.setattr(main.sys, "argv", ["algo-cli", "update"])
    monkeypatch.setattr(main.updater, "update_algo_cli", lambda: result)
    monkeypatch.setattr(main.console, "print", lambda value, **_kwargs: rendered.append(str(value)))
    monkeypatch.setattr(
        main.Config,
        "load",
        lambda: pytest.fail("update must exit before loading user configuration"),
    )

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 0
    assert "0.15.0 → 0.16.0" in rendered[0]


def test_model_name_classifiers():
    assert main.is_cloud_model_name("glm-4.6:cloud") is True
    assert main.is_cloud_model_name("qwen3:235b-cloud") is True
    assert main.is_cloud_model_name("qwen3") is False

    assert main.is_embedding_model_name("nomic-embed-text-v2-moe:latest") is True
    assert main.is_embedding_model_name("embeddinggemma:latest") is True
    assert main.is_embedding_model_name("paraphrase-multilingual:latest") is True
    assert main.is_embedding_model_name("qwen3") is False

    assert main.is_vision_model_name("qwen3-vl:235b") is True
    assert main.is_vision_model_name("llava") is True
    assert main.is_vision_model_name("qwen3") is False
    assert main.is_vision_model_name("gemma3:27b") is False


def test_parallel_read_only_tools_exclude_writers_and_heavy_runtime_tools():
    assert "harness_refresh" not in main.READ_ONLY_TOOLS
    assert "embed_text" not in main.READ_ONLY_TOOLS
    assert "vision_describe" not in main.READ_ONLY_TOOLS


def test_effective_runtime_host_cloud_and_local(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "token")
    cloud_cfg = Config(model="minimax-m3", cloud=True, host="http://localhost:11434")
    assert main.uses_ollama_cloud(cloud_cfg) is True
    assert main.effective_runtime_host(cloud_cfg) == "https://ollama.com"

    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    local_cfg = Config(model="qwen3", cloud=False, host="http://127.0.0.1:11434")
    assert main.uses_ollama_cloud(local_cfg) is False
    assert main.effective_runtime_host(local_cfg) == "http://127.0.0.1:11434"


def test_cloud_mode_without_api_key_routes_local(monkeypatch, tmp_path):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ALGO_CLI_ENV_FILE", str(tmp_path / "missing-env"))
    cfg = Config(model="minimax-m3:cloud", cloud=True)
    assert main.uses_ollama_cloud(cfg) is False
    main.require_cloud_api_key(cfg)


def test_cloud_routing_loads_runtime_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "algo-env"
    env_file.write_text("OLLAMA_API_KEY=runtime-token\n", encoding="utf-8")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ALGO_CLI_ENV_FILE", str(env_file))
    cfg = Config(model="minimax-m3:cloud", cloud=True)

    assert model_routing.uses_ollama_cloud(cfg) is True
    assert main.uses_ollama_cloud(cfg) is True
    assert main.effective_runtime_host(cfg) == "https://ollama.com"
    assert main.os.environ.get("OLLAMA_API_KEY") == "runtime-token"


def test_doctor_and_context_load_runtime_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "algo-env"
    env_file.write_text("OLLAMA_API_KEY=runtime-token\n", encoding="utf-8")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ALGO_CLI_ENV_FILE", str(env_file))
    monkeypatch.setattr(action_registry, "_check_ollama_host", lambda _host: False)
    cfg = Config(model="minimax-m3:cloud", cloud=True)

    report = action_registry.build_doctor_report(cfg)
    assert any(finding.area == "ollama-cloud" and finding.status == "ready" for finding in report.findings)
    prompt = context_budget.build_system_prompt(cfg)
    assert "Provider route: Ollama Cloud direct API" in prompt


def test_legacy_xai_login_guides_user_to_config_setup(monkeypatch):
    infos: list[str] = []
    monkeypatch.setattr(main, "show_info", infos.append)

    main.run_xai_login()

    assert len(infos) == 1
    assert "API key" in infos[0]
    assert "algo-cli config setup xai" in infos[0]


def test_xai_status_describes_missing_api_key(monkeypatch):
    infos: list[str] = []
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_CLIENT_ID", raising=False)
    main.xai_auth.LEGACY_AUTH_FILE.unlink(missing_ok=True)
    monkeypatch.setattr(main, "show_info", infos.append)

    main.run_xai_status()

    assert len(infos) == 1
    assert "not configured" in infos[0]
    assert "algo-cli config setup xai" in infos[0]


def test_xai_status_never_echoes_api_key(monkeypatch):
    key = "xai-secret-not-for-terminal-log"
    infos: list[str] = []
    monkeypatch.setenv("XAI_API_KEY", key)
    monkeypatch.setattr(main, "show_info", infos.append)

    main.run_xai_status()

    output = "\n".join(infos)
    assert key not in output
    assert "configured" in output


def test_system_prompt_does_not_disclose_absolute_workspace_or_identity_paths(tmp_path):
    private_workspace = tmp_path / "private-project-name"
    cfg = Config(cwd=str(private_workspace))

    prompt = context_budget.build_system_prompt(cfg)

    assert str(private_workspace) not in prompt
    assert str(main.identity.IDENTITY_DIR) not in prompt
    assert "Relative tool paths resolve from the active session workspace" in prompt


def test_chatgpt_login_device_code_flag_uses_codex_device_flow(monkeypatch):
    infos: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(main, "show_info", infos.append)
    monkeypatch.setattr(main, "show_error", errors.append)
    monkeypatch.setattr(main.chatgpt_auth, "run_codex_device_login", lambda: {"access_token": "AT", "expires_at": int(time.time()) + 3600})
    monkeypatch.setattr(main, "_show_chatgpt_models_after_login", lambda: None)
    monkeypatch.setattr(main.chatgpt_client, "_MODEL_REQUEST_SCOPE_MISSING", True)

    assert main.run_chatgpt_login("--device-code") is True

    assert not errors
    assert any("https://auth.openai.com/codex/device" in message for message in infos)
    assert any("ChatGPT authentication successful" in message for message in infos)
    assert main.chatgpt_client._MODEL_REQUEST_SCOPE_MISSING is False


def test_chatgpt_login_defaults_to_codex_browser_oauth(monkeypatch):
    infos: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(main, "show_info", infos.append)
    monkeypatch.setattr(main, "show_error", errors.append)
    monkeypatch.setattr(main.chatgpt_auth, "select_redirect_port", lambda: 1455)
    monkeypatch.setattr(
        main.chatgpt_auth,
        "begin_login",
        lambda **kw: {
            "auth_url": "https://auth.openai.com/oauth/authorize?originator=pi",
            "redirect_uri": "http://localhost:1455/auth/callback",
            "redirect_port": "1455",
            "ssh_tunnel_cmd": "ssh -N -L 1455:localhost:1455 you@remote-host",
            "browser_opened": False,
            "code_verifier": "verifier",
            "state": "state",
        },
    )
    monkeypatch.setattr(main, "_show_chatgpt_models_after_login", lambda: None)
    monkeypatch.setattr(main.chatgpt_auth, "run_loopback_capture", lambda **kw: {"code": "CODE", "state": "state"})
    monkeypatch.setattr(
        main.chatgpt_auth,
        "complete_login",
        lambda verifier, state, callback: {"access_token": "AT", "expires_at": int(time.time()) + 3600},
    )

    assert main.run_chatgpt_login("") is True

    assert not errors
    assert any("Opening ChatGPT/OpenAI auth" in message for message in infos)
    assert any("ChatGPT authentication successful" in message for message in infos)


def test_run_tool_anchors_relative_search_to_configured_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    stale = tmp_path / "stale"
    (workspace / "ollama_cli").mkdir(parents=True)
    stale.mkdir()
    (workspace / "ollama_cli" / "main.py").write_text("def handle_command():\n    pass\n", encoding="utf-8")
    cfg = Config(cwd=str(workspace))

    result = main.run_tool(
        "search_files",
        {"pattern": "def handle_command", "path": "ollama_cli/main.py", "cwd": str(stale)},
        cfg,
    )

    assert "def handle_command" in result
    assert "Error: path not found" not in result


def test_ensure_harness_index_returns_true_for_incremental_embedding(monkeypatch):
    cfg = Config()
    messages: list[str] = []

    monkeypatch.setattr(main, "resolve_embed_backend", lambda _cfg: ("local", "ok"))
    monkeypatch.setattr(main.harness, "resolve_embed_model", lambda _cfg: "embed-model")
    monkeypatch.setattr(main, "make_embed_fn", lambda _cfg, _model: (lambda texts: [[1.0] for _ in texts], "local", "embed-model"))
    monkeypatch.setattr(main.harness, "embedded_count", lambda _model=None: (0, 10))
    monkeypatch.setattr(main, "host_is_local", lambda _host: True)
    monkeypatch.setattr(main, "ollama_server_ready", lambda _host: True)
    monkeypatch.setattr(main, "local_model_names", lambda _cfg: ["embed-model:latest"])
    monkeypatch.setattr(
        main.harness,
        "embed_index_records",
        lambda *_args, **_kwargs: {"ready": False, "embedded": 3, "pending": 7, "reason": "max_records_reached"},
    )
    monkeypatch.setattr(main, "show_info", messages.append)

    assert main.ensure_harness_index(cfg, max_records=3) is True
    assert any("partially ready" in message for message in messages)



def test_context_slash_command_registered():
    commands = {name for name, _description in main.SLASH_COMMANDS}
    assert "/context" in commands
    assert "/x-account" in commands
    assert "/intuition" in commands
    assert "/agent" in commands
    assert "/route" in commands
    assert "/policy" in commands
    assert "/status" in commands


def test_slash_toggles_accept_explicit_on_off_status(monkeypatch):
    cfg = Config()
    cfg.auto_mode = False
    cfg.safe_mode = True
    cfg.show_thinking = True
    cfg.verify_mode = False
    messages: list[str] = []
    monkeypatch.setattr(main, "show_info", messages.append)

    main.handle_command("/auto on", cfg, object())  # type: ignore[arg-type]
    main.handle_command("/safe off", cfg, object())  # type: ignore[arg-type]
    main.handle_command("/thinking status", cfg, object())  # type: ignore[arg-type]
    main.handle_command("/verify on", cfg, object())  # type: ignore[arg-type]

    assert cfg.auto_mode is True
    assert cfg.safe_mode is False
    assert cfg.show_thinking is True
    assert cfg.verify_mode is True
    assert any("Thinking display: ON" in message for message in messages)


def test_thinking_effort_is_stored_per_gpt_56_model(monkeypatch):
    cfg = Config(model="gpt-5.6-sol")
    messages: list[str] = []
    monkeypatch.setattr(cfg, "save", lambda: None)
    monkeypatch.setattr(main, "show_info", messages.append)

    main.handle_command("/thinking effort sol max", cfg, object())  # type: ignore[arg-type]
    main.handle_command("/thinking effort terra high", cfg, object())  # type: ignore[arg-type]
    main.handle_command("/thinking effort luna low", cfg, object())  # type: ignore[arg-type]

    assert cfg.chatgpt_reasoning_efforts == {
        "gpt-5.6-sol": "max",
        "gpt-5.6-terra": "high",
        "gpt-5.6-luna": "low",
    }
    assert any("gpt-5.6-sol: max" in message for message in messages)


def test_save_with_empty_sanitized_name_reports_error(monkeypatch):
    cfg = Config()
    errors: list[str] = []
    monkeypatch.setattr(main, "show_error", errors.append)

    handled, _client = main.handle_command("/save !!!", cfg, object())  # type: ignore[arg-type]

    assert handled is True
    assert any("Save name" in error for error in errors)


def test_reload_mutates_active_config_in_place(monkeypatch):
    cfg = Config(model="old-model", cloud=False)
    client = object()
    loaded = Config(model="new-model", cloud=True)
    new_client = object()
    monkeypatch.setattr(main, "reload_runtime", lambda: loaded)
    monkeypatch.setattr(main, "create_client", lambda reloaded_cfg: new_client)
    monkeypatch.setattr(main, "show_info", lambda _msg: None)

    handled, returned_client = main.handle_command("/reload", cfg, client)  # type: ignore[arg-type]

    assert handled is True
    assert returned_client is new_client
    assert cfg.model == "new-model"
    assert cfg.cloud is True


def test_reason_command_guidance_and_neuro_symbolic_alias(monkeypatch):
    cfg = Config()
    messages: list[str] = []
    monkeypatch.setattr(main, "show_info", messages.append)

    main.handle_command("/reason guide", cfg, object())  # type: ignore[arg-type]
    main.handle_command("/reason neuro-symbolic", cfg, object())  # type: ignore[arg-type]
    main.handle_command("/reason status", cfg, object())  # type: ignore[arg-type]

    assert cfg.reasoning_mode == "neuro_symbolic"
    assert any("Reasoning mode guide" in message for message in messages)
    assert any("Use /reason guide" in message for message in messages)


def test_context_clear_only_clears_summary():
    cfg = Config()
    cfg.session_summary = "old compressed context"
    cfg.messages = [{"role": "user", "content": "keep me"}]

    main.handle_context_command("clear", cfg, object())  # type: ignore[arg-type]

    assert cfg.session_summary == ""
    assert cfg.messages == [{"role": "user", "content": "keep me"}]


def test_status_command_reports_model_context_and_features(monkeypatch):
    cfg = Config(model="test-model")
    cfg.auto_mode = True
    cfg.safe_mode = False
    cfg.show_thinking = False
    cfg.algorithmic_tool_policy_enabled = True
    cfg.intuition_recall_enabled = True
    cfg.skill_crystallize_enabled = False
    lines: list[str] = []
    monkeypatch.setattr(main, "context_status", lambda _cfg, **_: (512, 8192, 7680, 8192, 8192))
    monkeypatch.setattr(main.console, "print", lambda text: lines.append(str(text)))

    handled, _client = main.handle_command("/status", cfg, object())  # type: ignore[arg-type]

    assert handled is True
    assert any("Model:" in line and "test-model" in line for line in lines)
    assert any("Context:" in line and "512/8192" in line and "7680 remaining" in line for line in lines)
    assert any("Features:" in line and "auto-approve" in line and "policy" in line and "intuition" in line for line in lines)
    assert all("safe-mode" not in line for line in lines)


def test_estimate_context_usage_cache_key_includes_identity_mtimes():
    from algo_cli import context_budget
    from algo_cli import identity

    identity.scaffold_if_needed()
    cfg = Config()
    context_budget.invalidate_context_usage_cache()
    context_budget.estimate_context_usage(cfg)
    key_before = context_budget.CONTEXT_USAGE_CACHE[0]
    time.sleep(0.02)
    identity.USER_PATH.write_text("# About the User\n\nCache key bump.\n", encoding="utf-8")
    context_budget.estimate_context_usage(cfg)
    key_after = context_budget.CONTEXT_USAGE_CACHE[0]
    assert key_before != key_after


def test_context_status_uses_runtime_context_window(monkeypatch):
    # model_adaptive off: this test pins the raw num_ctx path. The adaptive
    # window path is covered in tests/test_context_accounting.py.
    cfg = Config(model="minimax-m3:cloud", cloud=True, num_ctx=8192, model_adaptive=False)
    monkeypatch.setattr(
        main._model_info_module,
        "resolve_model_info",
        lambda _cfg, _client: {"context_length": 524_288},
    )
    import algo_cli.context_budget as context_budget

    monkeypatch.setattr(context_budget, "estimate_context_usage", lambda _cfg: 3500)
    main.RUNTIME_STATUS.clear()
    used, total, remaining, runtime_cap, native = main.context_status(cfg)
    assert total == 8192
    assert native == 524_288
    assert runtime_cap == 8192
    assert used == 3500
    assert remaining == 8192 - 3500


def test_last_chat_token_usage_prefers_fresh_metrics():
    main.RUNTIME_STATUS["last_metrics"] = {
        "timestamp": __import__("time").time(),
        "prompt_eval_count": 3000,
        "eval_count": 500,
    }
    assert main._last_chat_token_usage() == 3500


def test_intuition_command_on_off_toggles_recall_and_capture():
    cfg = Config()

    main.handle_intuition_command("on", cfg)
    assert cfg.intuition_recall_enabled is True
    assert cfg.intuition_capture_enabled is True

    main.handle_intuition_command("off", cfg)
    assert cfg.intuition_recall_enabled is False
    assert cfg.intuition_capture_enabled is False


def test_policy_command_toggles_enforcement(monkeypatch):
    cfg = Config()
    monkeypatch.setattr(main, "show_info", lambda *args, **kwargs: None)

    handled, _client = main.handle_command("/policy on", cfg, object())  # type: ignore[arg-type]
    assert handled is True
    assert cfg.algorithmic_tool_policy_enabled is True

    main.handle_command("/policy off", cfg, object())  # type: ignore[arg-type]
    assert cfg.algorithmic_tool_policy_enabled is False


def test_cloud_command_without_api_key_uses_local_login_route(monkeypatch):
    cfg = Config(model="minimax-m3:cloud", cloud=False)
    old_client = object()
    new_client = object()
    infos: list[str] = []

    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setattr(main, "start_ollama_server", lambda _cfg: True)
    monkeypatch.setattr(main, "create_client", lambda route_cfg: new_client if route_cfg.cloud is False else old_client)
    monkeypatch.setattr(main, "show_info", lambda message: infos.append(message))

    handled, client = main.handle_command("/cloud", cfg, old_client)  # type: ignore[arg-type]

    assert handled is True
    assert client is new_client
    assert cfg.cloud is False
    assert any("local Ollama login" in message for message in infos)


def test_cloud_model_names_hidden_without_api_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    assert main.cloud_model_names() == []


def test_agent_init_writes_starter_without_overwriting(monkeypatch, tmp_path):
    from algo_cli import agent_pipeline

    cfg = Config()
    path = tmp_path / "blocks.toml"
    infos: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(main.agent_blocks, "BLOCKS_CONFIG_PATH", path)
    monkeypatch.setattr(agent_pipeline, "show_info", lambda message: infos.append(message))
    monkeypatch.setattr(agent_pipeline, "show_error", lambda message: errors.append(message))

    main.handle_command("/agent init", cfg, object())  # type: ignore[arg-type]
    main.handle_command("/agent init", cfg, object())  # type: ignore[arg-type]

    assert path.exists()
    assert any("starter config" in message for message in infos)
    assert any("already exists" in message for message in errors)


def test_route_warns_when_configured_pipeline_exceeds_budget(monkeypatch, tmp_path):
    cfg = Config()
    path = tmp_path / "blocks.toml"
    block = """
[[pipelines.code-change.blocks]]
role = "implement"
prompt = "Implement."
tools = ["read", "write"]
max_iterations = 12
"""
    path.write_text("version = 1\n[pipelines.code-change]\n" + block * 5, encoding="utf-8")
    infos: list[str] = []
    monkeypatch.setattr(main.agent_blocks, "BLOCKS_CONFIG_PATH", path)
    from algo_cli import agent_pipeline

    monkeypatch.setattr(agent_pipeline, "show_info", lambda message: infos.append(message))
    monkeypatch.setattr(agent_pipeline, "console", type("C", (), {"print": lambda *a, **k: None})())

    main.show_task_route(main.task_router.route_task("Fix the failing auth test"), cfg, "Fix the failing auth test")

    assert any("at most 4 blocks" in message for message in infos)
    assert any("at most 8 iterations/block" in message for message in infos)


def test_capture_intuition_block_respects_capture_flag(monkeypatch, tmp_path):
    cfg = Config()
    engine = main._IntuitionEngineCls(index_path=str(tmp_path / "intuition.json"))  # type: ignore[operator]
    monkeypatch.setattr(main, "_intuition_engine", engine)
    monkeypatch.setattr(main, "intuition_embed_fn", lambda _cfg: lambda texts: [[1.0, 0.0] for _ in texts])

    assert main.capture_intuition_block(cfg, "memory", "Python is preferred.", source="test") is None

    cfg.intuition_capture_enabled = True
    block_id = main.capture_intuition_block(cfg, "memory", "Python is preferred.", source="test")

    assert block_id is not None
    assert engine.status()["block_count"] == 1
    assert engine.status()["embedded"] == 1


def test_intuition_forget_command_removes_block(monkeypatch, tmp_path):
    cfg = Config()
    engine = main._IntuitionEngineCls(index_path=str(tmp_path / "intuition.json"))  # type: ignore[operator]
    block_id = engine.capture_block("memory", "Python is preferred.", source="test")
    monkeypatch.setattr(main, "_intuition_engine", engine)

    main.handle_intuition_command(f"forget {block_id}", cfg)

    assert engine.status()["block_count"] == 0


def test_intuition_reindex_command_updates_embeddings(monkeypatch, tmp_path):
    cfg = Config()
    engine = main._IntuitionEngineCls(index_path=str(tmp_path / "intuition.json"))  # type: ignore[operator]
    engine.capture_block("memory", "Python is preferred.", source="test")
    monkeypatch.setattr(main, "_intuition_engine", engine)
    monkeypatch.setattr(main, "intuition_embed_fn", lambda _cfg: lambda texts: [[1.0, 0.0] for _ in texts])

    main.handle_intuition_command("reindex", cfg)

    assert engine.status()["embedded"] == 1


def test_client_for_model_uses_active_client_for_default_model():
    cfg = Config()
    cfg.model = "qwen3"
    active = object()

    assert main.client_for_model("qwen3", cfg, active) is active


def test_create_client_applies_configured_chat_stream_timeout(monkeypatch):
    cfg = Config(host="http://localhost:11434")
    cfg.chat_stream_timeout_seconds = 42.5
    captured: dict[str, object] = {}

    def fake_client(*, host, **kwargs):
        captured["host"] = host
        captured.update(kwargs)
        return object()

    from algo_cli import runtime_services

    monkeypatch.setattr(runtime_services, "Client", fake_client)
    monkeypatch.setattr(runtime_services, "load_runtime_env", lambda **_kwargs: {})

    main.create_client(cfg)

    assert captured["host"] == cfg.host
    assert captured["timeout"] == 42.5


def test_maintenance_client_skips_embedding_only_local_models(monkeypatch):
    cfg = Config(model="deepseek-v4-pro:cloud")
    fallback = object()
    monkeypatch.setattr(main, "local_model_names", lambda _cfg: ["nomic-embed-text:latest"])

    client, model = main.small_maintenance_client(cfg, fallback)  # type: ignore[arg-type]

    assert client is fallback
    assert model == cfg.model


def test_local_crystallizer_never_falls_back_to_cloud(monkeypatch):
    cfg = Config(
        model="cloud-model",
        cloud=True,
        skill_crystallize_enabled=True,
        runs_since_crystallize=3,
    )
    monkeypatch.setattr(main, "host_is_local", lambda _host: True)
    monkeypatch.setattr(main, "ollama_server_ready", lambda _host: True)
    monkeypatch.setattr(main, "local_model_names", lambda _cfg: ["qwen3-embedding:latest"])
    monkeypatch.setattr(
        main,
        "create_client",
        lambda _cfg: (_ for _ in ()).throw(AssertionError("cloud fallback attempted")),
    )
    saves: list[bool] = []
    monkeypatch.setattr(cfg, "save", lambda: saves.append(True))
    monkeypatch.setattr(
        main.skills,
        "crystallize",
        lambda _fn: (_ for _ in ()).throw(AssertionError("crystallizer should be skipped")),
    )

    assert main.make_local_maintenance_llm_fn(cfg) is None
    main.maybe_crystallize_skills(cfg)

    assert cfg.runs_since_crystallize == 3
    assert saves == []


def test_agent_loop_retains_partial_output_when_stream_interrupts(monkeypatch):
    cfg = Config(model="test-model")
    cfg.skill_crystallize_enabled = False
    errors: list[str] = []
    memory_calls: list[dict] = []

    class InterruptedClient:
        def chat(self, **_kwargs):
            def chunks():
                yield {"message": {"content": "Partial answer."}}
                raise TimeoutError("read timed out")

            return chunks()

    monkeypatch.setattr(main.identity, "detect_changes", lambda: [])
    monkeypatch.setattr(main, "ensure_lessons_index", lambda _cfg: False)
    monkeypatch.setattr(main, "ensure_harness_index", lambda _cfg, _local=None: False)
    monkeypatch.setattr(main, "prune_stale_tool_messages", lambda _cfg: None)
    monkeypatch.setattr(main, "maybe_compact_context", lambda _client, _cfg, **_: None)
    monkeypatch.setattr(main._model_info_module, "ensure_model_info", lambda _client, _model: {})
    monkeypatch.setattr(main, "record_chat_metrics", lambda _cfg, _chunk: None)
    monkeypatch.setattr(main, "show_error", lambda message: errors.append(message))
    monkeypatch.setattr(main, "start_streaming_response", lambda: None)
    monkeypatch.setattr(main, "show_stream_text", lambda _text: None)
    monkeypatch.setattr(main, "finish_streaming_response", lambda: None)
    monkeypatch.setattr(
        main.memory_runtime,
        "capture_completed_user_turn",
        lambda _cfg, text, **kwargs: memory_calls.append({"text": text, **kwargs})
        or {"status": "skipped"},
    )

    main.agent_loop(InterruptedClient(), cfg, "review the wiki")  # type: ignore[arg-type]

    assert cfg.messages[-1] == {"role": "assistant", "content": "Partial answer."}
    assert any("Response stream interrupted after partial output" in error for error in errors)
    assert memory_calls == [
        {
            "text": "review the wiki",
            "completed": False,
            "tool_calls": [],
            "source": "chat",
        }
    ]


def test_safe_file_history_sanitizes_disk_and_current_session(tmp_path):
    path = tmp_path / "history"
    history = main.SafeFileHistory(str(path))

    history.append_string("unsafe-\udcff-text")

    current = history.get_strings()[0]
    persisted = path.read_text(encoding="utf-8")
    assert "\udcff" not in current
    assert "\udcff" not in persisted
    assert current.startswith("unsafe-") and current.endswith("-text")
    assert current in persisted


def test_safe_file_history_enforces_private_bounded_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "PROMPT_HISTORY_MAX_ENTRIES", 3)
    monkeypatch.setattr(main, "PROMPT_HISTORY_COMPACT_EVERY", 1)
    path = tmp_path / "private" / "history"
    history = main.SafeFileHistory(str(path))

    for index in range(6):
        history.append_string(f"entry-{index}")

    assert list(history.load_history_strings()) == ["entry-5", "entry-4", "entry-3"]
    assert history.get_strings() == ["entry-3", "entry-4", "entry-5"]
    if os.name == "posix":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_safe_file_history_compaction_normalizes_windows_newlines(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "PROMPT_HISTORY_COMPACT_EVERY", 1)
    path = tmp_path / "history"
    path.write_bytes(b"\r\n# 2026-01-01 00:00:00\r\n+older-entry\r\n")
    history = main.SafeFileHistory(str(path))

    history.append_string("newer-entry")

    assert list(history.load_history_strings()) == ["newer-entry", "older-entry"]
    assert b"\r" not in path.read_bytes()


def _patch_agent_loop_for_tool_policy_test(monkeypatch):
    """Keep agent-loop policy tests local, deterministic, and network-free."""

    from algo_cli import tool_runtime

    def fake_make_embed_fn(*_args, **_kwargs):
        return (lambda texts: [[0.0] for _ in texts]), "test", "test-embed"

    monkeypatch.setattr(main, "json_sink", lambda: object())
    monkeypatch.setattr(main.identity, "detect_changes", lambda: [])
    monkeypatch.setattr(main, "make_embed_fn", fake_make_embed_fn)
    monkeypatch.setattr(main._model_info_module, "resolve_model_info", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(main, "local_model_names", lambda _cfg: [])
    monkeypatch.setattr(main, "ensure_lessons_index", lambda _cfg: False)
    monkeypatch.setattr(main, "ensure_harness_index", lambda _cfg, _local=None: False)
    monkeypatch.setattr(main, "host_is_local", lambda _host: False)
    monkeypatch.setattr(main, "prune_stale_tool_messages", lambda _cfg: None)
    monkeypatch.setattr(main, "maybe_compact_context", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(main, "record_chat_metrics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "show_tool_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "show_tool_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "finish_thinking_block", lambda: None)
    monkeypatch.setattr(main, "finish_streaming_response", lambda: None)
    monkeypatch.setattr(main, "flush_perf_records", lambda: None)
    monkeypatch.setattr(tool_runtime, "record_perf_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main.skills, "record_run", lambda **_kwargs: None)
    monkeypatch.setattr(main, "_intuition_engine", None)


def test_agent_loop_auto_memory_runs_only_at_normal_completion(monkeypatch):
    class ScriptedClient:
        def chat(self, **_kwargs):
            return iter([{"message": {"content": "Done."}}])

    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    calls: list[dict] = []
    infos: list[str] = []
    monkeypatch.setattr(
        main.memory_runtime,
        "capture_completed_user_turn",
        lambda _cfg, text, **kwargs: calls.append({"text": text, **kwargs})
        or {"status": "stored"},
    )
    monkeypatch.setattr(main, "show_info", infos.append)
    cfg = Config(model="test-model", skill_crystallize_enabled=False)

    main.agent_loop(
        ScriptedClient(),
        cfg,
        "Remember that our standard shell is zsh.",
    )  # type: ignore[arg-type]

    assert calls == [
        {
            "text": "Remember that our standard shell is zsh.",
            "completed": True,
            "tool_calls": [],
            "source": "chat",
        }
    ]
    assert infos == ["Saved 1 durable memory automatically; review it with /memories."]


def test_agent_loop_reports_session_save_failure_without_masking_completion(monkeypatch):
    class ScriptedClient:
        def chat(self, **_kwargs):
            return iter([{"message": {"content": "Done."}}])

    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    errors: list[str] = []
    monkeypatch.setattr(main, "show_error", errors.append)
    monkeypatch.setattr(
        main.memory_runtime,
        "capture_completed_user_turn",
        lambda *_args, **_kwargs: {"status": "skipped"},
    )
    cfg = Config(model="test-model", skill_crystallize_enabled=False)
    monkeypatch.setattr(
        cfg,
        "save",
        lambda: (_ for _ in ()).throw(OSError("disk full")),
    )

    main.agent_loop(ScriptedClient(), cfg, "finish")  # type: ignore[arg-type]

    assert errors == ["Session completed, but its local state could not be saved: disk full"]


def test_normal_chat_serial_policy_matches_pipeline_and_blocks_unsafe_shell(monkeypatch):
    from algo_cli import tool_runtime

    command = "touch should-not-exist.txt"
    pipeline_cfg = Config(safe_mode=True)
    monkeypatch.setattr(tool_runtime, "show_tool_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_runtime, "show_tool_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_runtime, "record_perf_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tool_runtime,
        "run_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("blocked pipeline call executed")),
    )
    _message, pipeline_result = tool_runtime.execute_tool_call_for_pipeline(
        "run_shell",
        {"command": command},
        pipeline_cfg,
    )

    class ScriptedClient:
        calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return iter(
                    [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "serial-policy",
                                        "function": {
                                            "name": "run_shell",
                                            "arguments": {"command": command},
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                )
            return iter([{"message": {"content": "done"}}])

    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    monkeypatch.setattr(main, "record_perf_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "run_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("blocked chat call executed")),
    )
    cfg = Config(model="test-model", safe_mode=True, skill_crystallize_enabled=False)

    main.agent_loop(ScriptedClient(), cfg, "make a file")  # type: ignore[arg-type]

    tool_results = [str(message.get("content") or "") for message in cfg.messages if message.get("role") == "tool"]
    assert tool_results == [pipeline_result]
    assert "Blocked by runtime policy chain" in pipeline_result


def test_agent_loop_withholds_unverified_final_and_requires_later_verifier(
    monkeypatch,
    tmp_path,
):
    class ScriptedClient:
        def __init__(self):
            self.calls: list[dict] = []

        def chat(self, **kwargs):
            self.calls.append(json.loads(json.dumps(kwargs, default=str)))
            turn = len(self.calls)
            if turn == 1:
                return iter(
                    [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "write",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": {"path": "created.py", "content": "x = 1\n"},
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                )
            if turn == 2:
                return iter([{"message": {"content": "Premature completion claim."}}])
            if turn == 3:
                return iter(
                    [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "verify",
                                        "function": {
                                            "name": "run_shell",
                                            "arguments": {"command": "pytest -q"},
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                )
            return iter([{"message": {"content": "Verified completion."}}])

    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    streamed: list[str] = []
    infos: list[str] = []
    monkeypatch.setattr(main, "start_streaming_response", lambda: None)
    monkeypatch.setattr(main, "show_stream_text", streamed.append)
    monkeypatch.setattr(main, "show_info", infos.append)
    monkeypatch.setattr(main, "record_perf_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "run_tool",
        lambda name, _args, _cfg: (
            "Wrote 6 characters to created.py"
            if name == "write_file"
            else "2 passed\n[exit code: 0]"
        ),
    )
    monkeypatch.setattr(
        main.memory_runtime,
        "capture_completed_user_turn",
        lambda *_args, **_kwargs: {"status": "skipped"},
    )
    cfg = Config(
        model="test-model",
        cwd=str(tmp_path),
        auto_mode=True,
        skill_crystallize_enabled=False,
        code_rag_enabled=False,
        index_compute_lab_auto_inject=False,
    )
    client = ScriptedClient()

    main.agent_loop(client, cfg, "create and verify a file")  # type: ignore[arg-type]

    assert len(client.calls) == 4
    assert "Premature completion claim." not in streamed
    assert "Verified completion." in streamed
    assert any("Unverified final text was withheld" in message for message in infos)
    third_request = client.calls[2]["messages"]
    assert any(
        "[Internal completion gate]" in str(message.get("content") or "")
        for message in third_request
    )


@pytest.mark.parametrize("iteration_cap", [1, 8])
def test_agent_loop_reserves_tool_free_finalization_after_iteration_cap(monkeypatch, iteration_cap):
    class ScriptedClient:
        def __init__(self):
            self.calls: list[dict] = []

        def chat(self, **kwargs):
            self.calls.append(json.loads(json.dumps(kwargs, default=str)))
            if len(self.calls) <= iteration_cap:
                return iter(
                    [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": f"read-{len(self.calls)}",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": {"path": "README.md"},
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                )
            return iter([{"message": {"content": "Verified result finalized."}}])

    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    streamed: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(main, "start_streaming_response", lambda: None)
    monkeypatch.setattr(main, "show_stream_text", streamed.append)
    monkeypatch.setattr(main, "show_error", errors.append)
    monkeypatch.setattr(main, "record_perf_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "run_tool", lambda *_args, **_kwargs: "README contents")
    monkeypatch.setattr(
        main.memory_runtime,
        "capture_completed_user_turn",
        lambda *_args, **_kwargs: {"status": "skipped"},
    )
    cfg = Config(model="test-model", max_tool_iterations=iteration_cap, skill_crystallize_enabled=False)
    client = ScriptedClient()

    main.agent_loop(client, cfg, "inspect the project")  # type: ignore[arg-type]

    assert len(client.calls) == iteration_cap + 1
    assert client.calls[-1]["tools"] == []
    assert any(
        "[Internal finalization turn]" in str(message.get("content") or "")
        for message in client.calls[-1]["messages"]
    )
    assert "Verified result finalized." in streamed
    assert errors == []


def test_terminal_final_answer_control_call_becomes_content() -> None:
    calls = [
        {
            "id": "terminal",
            "function": {
                "name": "final_answer",
                "arguments": {"answer": "Verified and complete."},
            },
        }
    ]

    assert main._terminal_answer_from_tool_calls(calls) == "Verified and complete."
    assert main._terminal_answer_from_tool_calls([]) is None
    assert main._terminal_answer_from_tool_calls(
        [{"function": {"name": "read_file", "arguments": {"path": "README.md"}}}]
    ) is None


def test_normal_chat_parallel_path_preflights_every_call(monkeypatch):
    class ScriptedClient:
        calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return iter(
                    [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": f"parallel-policy-{index}",
                                        "function": {"name": "stale_read_tool", "arguments": {}},
                                    }
                                    for index in range(2)
                                ]
                            }
                        }
                    ]
                )
            return iter([{"message": {"content": "done"}}])

    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    monkeypatch.setattr(main, "READ_ONLY_TOOLS", main.READ_ONLY_TOOLS | {"stale_read_tool"})
    monkeypatch.setattr(main, "record_perf_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "run_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("blocked parallel call executed")),
    )
    cfg = Config(model="test-model", safe_mode=True, skill_crystallize_enabled=False)

    main.agent_loop(ScriptedClient(), cfg, "inspect both")  # type: ignore[arg-type]

    tool_results = [str(message.get("content") or "") for message in cfg.messages if message.get("role") == "tool"]
    assert len(tool_results) == 2
    assert all("unknown runtime tool: stale_read_tool" in result for result in tool_results)
    assert [entry["status"] for entry in cfg.attempt_ledger[-2:]] == ["denied", "denied"]


def test_client_for_xai_model_without_token_falls_back(monkeypatch):
    cfg = Config()
    cfg.model = "qwen3"
    active = object()
    monkeypatch.setattr(main.xai_auth, "get_valid_token", lambda: None)

    assert main.client_for_model("grok-4.3", cfg, active) is active


def test_system_prompt_includes_authoritative_runtime_model():
    cfg = Config()
    cfg.model = "grok-4.3"
    cfg.cloud = False
    cfg.session_summary = "The active model is gpt-oss:120b-cloud."

    prompt = main.build_system_prompt(cfg, active_model_info=main._model_info_module.synthesize_xai_info(cfg.model))

    assert "## Runtime Model Status" in prompt
    assert "- Active model: grok-4.3" in prompt
    assert "- Provider route: xAI Grok API" in prompt
    assert prompt.index("## Runtime Model Status") < prompt.index("## Conversation Summary")
    assert "treat this runtime block as authoritative" in prompt


def test_context_rebuild_compacts_old_messages(monkeypatch):
    cfg = Config()
    cfg.messages = [{"role": "user", "content": f"message {i}"} for i in range(main.CONTEXT_KEEP_MESSAGES + 3)]

    def fake_summarize(config, batch, client, **kwargs):
        assert config is cfg
        assert len(batch) == 3
        return "rebuilt summary"

    import algo_cli.context_budget as context_budget

    monkeypatch.setattr(context_budget, "summarize_message_batch", fake_summarize)

    ok, message = main.rebuild_context_summary(object(), cfg)  # type: ignore[arg-type]

    assert ok is True
    assert "3 messages" in message
    assert cfg.session_summary == "rebuilt summary"
    assert len(cfg.messages) == main.CONTEXT_KEEP_MESSAGES


def test_context_rebuild_keeps_tool_exchange_boundary(monkeypatch):
    cfg = Config()
    prefix_count = 6
    cfg.messages = [{"role": "user", "content": f"old {i}"} for i in range(prefix_count)]
    cfg.messages.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}],
        }
    )
    cfg.messages.append({"role": "tool", "content": "tool result without id"})
    cfg.messages.extend({"role": "user", "content": f"tail {i}"} for i in range(main.CONTEXT_KEEP_MESSAGES - 1))

    def fake_summarize(config, batch, client, **kwargs):
        assert len(batch) == prefix_count
        return "rebuilt summary"

    import algo_cli.context_budget as context_budget

    monkeypatch.setattr(context_budget, "summarize_message_batch", fake_summarize)

    ok, _message = main.rebuild_context_summary(object(), cfg)  # type: ignore[arg-type]

    assert ok is True
    assert cfg.messages[0]["role"] == "assistant"
    assert cfg.messages[1]["role"] == "tool"


def test_xai_models_hidden_until_oauth(monkeypatch):
    monkeypatch.setattr(main.xai_auth, "get_valid_token", lambda: None)

    names, authenticated = main.xai_model_names()

    assert authenticated is False
    assert names == []


def test_chatgpt_models_visible_after_codex_oauth(monkeypatch):
    monkeypatch.setattr(main.chatgpt_auth, "get_valid_token", lambda: "token")
    monkeypatch.setattr(
        main.chatgpt_client,
        "get_codex_models",
        lambda: [
            {"slug": "gpt-5.6-sol"},
            {"slug": "gpt-5.6-terra"},
            {"slug": "gpt-5.6-luna"},
            {"slug": "gpt-5.5"},
        ],
    )

    names, authenticated = main.chatgpt_model_names()

    assert authenticated is True
    assert "gpt-5.6-sol" in names
    assert "gpt-5.6-terra" in names
    assert "gpt-5.6-luna" in names


def test_provider_model_discovery_failure_does_not_advertise_static_models(monkeypatch):
    monkeypatch.setattr(main.chatgpt_auth, "get_valid_token", lambda: "token")
    monkeypatch.setattr(
        main.chatgpt_client,
        "get_codex_models",
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(main.xai_auth, "get_valid_token", lambda: "key")
    from algo_cli import xai_client

    monkeypatch.setattr(
        xai_client,
        "get_models",
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    assert main.chatgpt_model_names() == ([], True)
    assert main.xai_model_names() == ([], True)


def test_model_picker_includes_authenticated_chatgpt_models(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(main, "local_model_names", lambda _cfg: [])
    monkeypatch.setattr(main, "cloud_model_names", lambda: [])
    monkeypatch.setattr(main, "xai_model_names", lambda: ([], False))
    monkeypatch.setattr(main, "chatgpt_model_names", lambda: (["gpt-5.5"], True))
    monkeypatch.setattr(main, "choose_from_menu", lambda _title, choices: captured.extend(choices) or None)

    assert main.model_picker(cfg) is False
    assert ("gpt-5.5", "OpenAI Codex · reasoning medium · subscription quota") in captured


def test_direct_provider_model_selection_requires_verified_catalog(monkeypatch):
    cfg = Config(model="existing-model")
    old_client = object()
    errors: list[str] = []
    monkeypatch.setattr(main, "show_error", errors.append)
    monkeypatch.setattr(
        main,
        "chatgpt_model_names",
        lambda: (["gpt-5.6-sol"], True),
    )

    handled, returned = main.handle_command("/model terra", cfg, old_client)  # type: ignore[arg-type]

    assert handled is True
    assert returned is old_client
    assert cfg.model == "existing-model"
    assert "not enabled" in errors[0]


def test_onboarding_remains_pending_when_model_selection_fails(monkeypatch):
    cfg = Config(onboarded=False)
    saves: list[bool] = []
    monkeypatch.setattr(main, "model_picker", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(cfg, "save", lambda: saves.append(True))

    main.onboard_if_needed(cfg)

    assert cfg.onboarded is False
    assert saves == []


def test_onboarding_completes_only_after_model_selection(monkeypatch):
    cfg = Config(onboarded=False)
    saves: list[bool] = []
    monkeypatch.setattr(main, "model_picker", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cfg, "save", lambda: saves.append(True))

    main.onboard_if_needed(cfg)

    assert cfg.onboarded is True
    assert saves == [True]


def test_harness_external_toggle_rebuilds_without_implicit_sources(monkeypatch):
    from algo_cli import harness

    cfg = Config(external_harness_sources_enabled=False, index_compute_lab_auto_inject=False)
    configured: list[tuple[bool, bool]] = []
    refreshes: list[bool] = []
    monkeypatch.setattr(cfg, "save", lambda: None)
    monkeypatch.setattr(
        harness,
        "configure_context_sources",
        lambda *, external, index_compute_lab: configured.append((external, index_compute_lab)),
    )
    monkeypatch.setattr(
        harness,
        "load_index",
        lambda *, refresh=False: refreshes.append(refresh) or {"records": []},
    )

    handled, _client = main.handle_command("/harness external on", cfg, None)

    assert handled is True
    assert cfg.external_harness_sources_enabled is True
    assert configured == [(True, False)]
    assert refreshes == [True]


def test_code_rag_toggle_records_consent_and_off_purges(monkeypatch):
    cfg = Config()
    saves: list[tuple[bool, int]] = []
    infos: list[str] = []
    purge_calls: list[bool] = []
    monkeypatch.setattr(
        cfg,
        "save",
        lambda: saves.append((cfg.code_rag_enabled, cfg.code_rag_consent_version)),
    )
    monkeypatch.setattr(main, "show_info", infos.append)
    monkeypatch.setattr(
        main.code_rag,
        "purge_persisted_indexes",
        lambda: purge_calls.append(True) or 2,
    )

    handled, _client = main.handle_command("/code-rag on", cfg, None)

    assert handled is True
    assert cfg.code_rag_enabled is True
    assert cfg.code_rag_consent_version == CODE_RAG_CONSENT_VERSION
    assert saves[-1] == (True, CODE_RAG_CONSENT_VERSION)
    assert any("cloud providers" in message for message in infos)

    handled, _client = main.handle_command("/code-rag off", cfg, None)

    assert handled is True
    assert cfg.code_rag_enabled is False
    assert cfg.code_rag_consent_version == 0
    assert saves[-1] == (False, 0)
    assert purge_calls == [True]
    assert any("purged 2" in message for message in infos)


def test_agent_loop_code_rag_requires_current_explicit_consent(monkeypatch, tmp_path):
    class ScriptedClient:
        def chat(self, **_kwargs):
            return iter([{"message": {"content": "Done."}}])

    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    monkeypatch.setattr(main, "host_is_local", lambda _host: True)
    monkeypatch.setattr(main, "ollama_server_ready", lambda _host: True)
    monkeypatch.setattr(main.code_rag, "looks_like_code_project", lambda _cwd: True)
    retrievals: list[str] = []
    monkeypatch.setattr(
        main.code_rag,
        "retrieve",
        lambda cwd, *_args, **_kwargs: retrievals.append(cwd) or [],
    )
    monkeypatch.setattr(
        main.memory_runtime,
        "capture_completed_user_turn",
        lambda *_args, **_kwargs: {"status": "skipped"},
    )

    legacy_cfg = Config(
        model="test-model",
        cwd=str(tmp_path),
        code_rag_enabled=True,
        code_rag_consent_version=0,
    )
    main.agent_loop(ScriptedClient(), legacy_cfg, "inspect the parser")  # type: ignore[arg-type]
    assert retrievals == []

    consented_cfg = Config(
        model="test-model",
        cwd=str(tmp_path),
        code_rag_enabled=True,
        code_rag_consent_version=CODE_RAG_CONSENT_VERSION,
    )
    main.agent_loop(ScriptedClient(), consented_cfg, "inspect the parser")  # type: ignore[arg-type]
    assert retrievals == [str(tmp_path)]


def test_agent_loop_records_skill_history_only_after_opt_in(monkeypatch):
    class ScriptedClient:
        def __init__(self):
            self.calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return iter(
                    [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "read-one",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": {"path": "README.md"},
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                )
            return iter([{"message": {"content": "Done."}}])

    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    monkeypatch.setattr(main, "run_tool", lambda *_args, **_kwargs: "README content")
    monkeypatch.setattr(
        main.memory_runtime,
        "capture_completed_user_turn",
        lambda *_args, **_kwargs: {"status": "skipped"},
    )
    recorded: list[str] = []
    monkeypatch.setattr(
        main.skills,
        "record_run",
        lambda **kwargs: recorded.append(str(kwargs["goal"])),
    )

    main.agent_loop(
        ScriptedClient(),
        Config(model="test-model", skill_crystallize_enabled=False),
        "private task",
    )  # type: ignore[arg-type]
    assert recorded == []

    main.agent_loop(
        ScriptedClient(),
        Config(model="test-model", skill_crystallize_enabled=True),
        "opted-in task",
    )  # type: ignore[arg-type]
    assert recorded == ["opted-in task"]


def test_models_command_force_refreshes_runtime_after_switch(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), model="glm-5.2:cloud")
    old_client = object()
    new_client = object()
    calls: list[tuple[str, object, bool]] = []

    def fake_picker(config):
        config.model = "gpt-5.5"
        config.cloud = False
        return True

    def fake_refresh(config, client, *, force=False):
        calls.append((config.model, client, force))

    monkeypatch.setattr(main, "model_picker", fake_picker)
    monkeypatch.setattr(main, "create_client", lambda _cfg: new_client)
    monkeypatch.setattr(main, "refresh_runtime_status", fake_refresh)
    monkeypatch.setattr(main, "invalidate_prompt_toolbar", lambda _session: None)

    handled, returned_client = main.handle_command("/models", cfg, old_client)  # type: ignore[arg-type]

    assert handled is True
    assert returned_client is new_client
    assert calls == [("gpt-5.5", new_client, True)]


@pytest.mark.parametrize(
    ("starting_cloud", "model", "expected_cloud"),
    [
        (True, "qwen3:8b", False),
        (False, "qwen3.6:cloud", True),
        (True, "grok-4-latest", False),
        (True, "gpt-5.5", False),
    ],
)
def test_direct_model_command_reconciles_provider_route(
    monkeypatch, tmp_path, starting_cloud, model, expected_cloud
):
    cfg = Config(cwd=str(tmp_path), model="old-model", cloud=starting_cloud)
    replacement_client = object()
    monkeypatch.setattr(cfg, "save", lambda: None)
    monkeypatch.setattr(main, "create_client", lambda _cfg: replacement_client)
    monkeypatch.setattr(main, "refresh_runtime_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "invalidate_prompt_toolbar", lambda _session: None)
    monkeypatch.setattr(main, "chatgpt_model_names", lambda: ([model], True))
    monkeypatch.setattr(main, "xai_model_names", lambda: ([model], True))

    handled, returned_client = main.handle_command(
        f"/model {model}", cfg, object()
    )  # type: ignore[arg-type]

    assert handled is True
    assert returned_client is replacement_client
    assert cfg.model == model
    assert cfg.cloud is expected_cloud


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("sol", "gpt-5.6-sol"),
        ("terra", "gpt-5.6-terra"),
        ("luna", "gpt-5.6-luna"),
        ("lunna", "gpt-5.6-luna"),
    ],
)
def test_direct_model_command_canonicalizes_codex_alias(monkeypatch, alias, canonical):
    cfg = Config(model="qwen3", cloud=True)
    replacement_client = object()
    monkeypatch.setattr(cfg, "save", lambda: None)
    monkeypatch.setattr(main, "create_client", lambda _cfg: replacement_client)
    monkeypatch.setattr(main, "refresh_runtime_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "invalidate_prompt_toolbar", lambda _session: None)
    monkeypatch.setattr(main, "chatgpt_model_names", lambda: ([canonical], True))

    handled, returned = main.handle_command(f"/model {alias}", cfg, object())  # type: ignore[arg-type]

    assert handled is True
    assert returned is replacement_client
    assert cfg.model == canonical
    assert cfg.cloud is False


@pytest.mark.parametrize(
    ("model", "expected_mode", "expected_host"),
    [
        ("grok-4-latest", "xai", "xai"),
        ("gpt-5.5", "chatgpt", "chatgpt"),
    ],
)
def test_dashboard_state_skips_ollama_probes_for_dedicated_providers(
    tmp_path, model, expected_mode, expected_host
):
    class DedicatedProviderClient:
        def list(self):
            raise AssertionError("dashboard must not call Ollama list()")

        def ps(self):
            raise AssertionError("dashboard must not call Ollama ps()")

    cfg = Config(cwd=str(tmp_path), model=model, cloud=True)

    installed, running, events = main.collect_dashboard_state(DedicatedProviderClient(), cfg)

    assert installed == []
    assert running == []
    assert f"connected {expected_host}" in events
    assert f"mode {expected_mode}" in events
    assert not any("unavailable" in event for event in events)


def test_refresh_runtime_status_labels_chatgpt_and_uses_adaptive_context(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), model="gpt-5.5")
    cfg.model_adaptive = True
    main.RUNTIME_STATUS.clear()
    monkeypatch.setattr(main, "local_model_names", lambda _cfg: [])

    main.refresh_runtime_status(cfg, client=None, force=True)

    assert main.RUNTIME_STATUS["model"] == "gpt-5.5"
    assert main.RUNTIME_STATUS["mode"] == "chatgpt"
    assert main.RUNTIME_STATUS["context_native"] == 1_000_000
    assert main.RUNTIME_STATUS["context_runtime_cap"] == 1_000_000


def test_host_is_local():
    assert main.host_is_local("http://localhost:11434") is True
    assert main.host_is_local("http://127.0.0.1:11434") is True
    assert main.host_is_local("https://ollama.com") is False


def test_format_short_count():
    assert main._format_short_count(42) == "42"
    assert main._format_short_count(1500) == "1.5k"
    assert main._format_short_count(8192) == "8.2k"
    assert main._format_short_count(1_000_000) == "1M"
    assert main._format_short_count(1_500_000) == "1.5M"
    assert main._format_short_count("bad") == "?"


def test_context_chip_formats_million_token_context_without_cap():
    palette = main.theme_colors("tokyo-night")
    main.RUNTIME_STATUS.clear()
    main.RUNTIME_STATUS.update(
        {
            "context_used": 5_200,
            "context_total": 1_000_000,
            "context_native": 1_000_000,
            "context_runtime_cap": 1_000_000,
            "context_pct_left": 99,
        }
    )

    chip = str(main._context_chip(palette))

    assert "5.2k/1M 99%" in chip
    assert "cap" not in chip


class _FakeFunction:
    def __init__(self, name="read_file", arguments=None, **extra):
        self.name = name
        self.arguments = arguments or {}
        for k, v in extra.items():
            setattr(self, k, v)


class _FakeToolCall:
    def __init__(self, function=None, **extra):
        self.function = function or _FakeFunction()
        for k, v in extra.items():
            setattr(self, k, v)


def test_serialize_tool_call_preserves_signature_on_call():
    """Gemini-via-Ollama-Cloud may surface thought_signature on the call object."""
    call = _FakeToolCall(
        function=_FakeFunction("read_file", {"path": "/x"}),
        id="call_1",
        type="function",
        thought_signature="OPAQUE_SIG",
    )
    out = main.serialize_tool_call(call)
    assert out["thought_signature"] == "OPAQUE_SIG"
    assert out["function"]["name"] == "read_file"


def test_serialize_tool_call_preserves_signature_on_function():
    """Provider may place signature on the inner function object instead."""
    call = _FakeToolCall(
        function=_FakeFunction("read_file", {"path": "/x"}, thoughtSignature="CAMEL_SIG"),
    )
    out = main.serialize_tool_call(call)
    assert out.get("thoughtSignature") == "CAMEL_SIG"


def test_serialize_tool_call_no_signature_field_when_absent():
    """No signature on input → no signature key on output."""
    call = _FakeToolCall(function=_FakeFunction("read_file", {"path": "/x"}))
    out = main.serialize_tool_call(call)
    assert "thought_signature" not in out
    assert "thoughtSignature" not in out


def test_serialize_tool_call_dict_passthrough_preserves_signature():
    """Dict inputs JSON-roundtrip — signature survives wherever it was."""
    call = {
        "function": {"name": "x", "arguments": {}},
        "thought_signature": "abc123",
    }
    out = main.serialize_tool_call(call)
    assert out["thought_signature"] == "abc123"


def test_collapse_gemini_history_basic():
    """A tool-call assistant + tool result collapse into one assistant content turn."""
    messages = [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "list_directory", "arguments": {"path": "."}}}],
        },
        {"role": "tool", "name": "list_directory", "content": "file1.md\nfile2.md"},
        {"role": "user", "content": "what next"},
    ]
    out = main.collapse_tool_history_for_gemini(messages)
    # Should have: user, collapsed assistant, user
    assert len(out) == 3
    assert out[0]["role"] == "user"
    assert out[1]["role"] == "assistant"
    assert "list_directory" in out[1]["content"]
    assert "file1.md" in out[1]["content"]
    assert "tool_calls" not in out[1]
    assert out[2]["role"] == "user"


def test_collapse_gemini_history_no_tool_calls_unchanged():
    """History without tool calls passes through unchanged."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = main.collapse_tool_history_for_gemini(messages)
    assert out == messages


def test_collapse_gemini_history_multiple_tool_calls():
    """Assistant with multiple tool calls + multiple tool results collapse together."""
    messages = [
        {"role": "user", "content": "do stuff"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "/a"}}},
                {"function": {"name": "read_file", "arguments": {"path": "/b"}}},
            ],
        },
        {"role": "tool", "name": "read_file", "content": "contents of a"},
        {"role": "tool", "name": "read_file", "content": "contents of b"},
    ]
    out = main.collapse_tool_history_for_gemini(messages)
    assert len(out) == 2  # user + collapsed
    collapsed = out[1]["content"]
    assert "read_file" in collapsed
    assert "contents of a" in collapsed
    assert "contents of b" in collapsed


def test_collapse_gemini_history_preserves_existing_content():
    """Assistant content alongside tool_calls is preserved in the collapsed message."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "Let me check that file.",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {}}}],
        },
        {"role": "tool", "name": "read_file", "content": "ok"},
    ]
    out = main.collapse_tool_history_for_gemini(messages)
    assert "Let me check that file" in out[1]["content"]


def test_collapse_gemini_history_truncates_long_tool_results():
    """Tool results are truncated so collapsed history stays compact."""
    long_result = "x" * 5000
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {}}}],
        },
        {"role": "tool", "name": "read_file", "content": long_result},
    ]
    out = main.collapse_tool_history_for_gemini(messages)
    assert len(out[0]["content"]) < len(long_result)
    assert "…" in out[0]["content"]


def test_collapse_gemini_history_empty_messages():
    """Empty input gives empty output, no crash."""
    assert main.collapse_tool_history_for_gemini([]) == []


def test_collapse_gemini_history_orphan_assistant_tool_calls():
    """Assistant tool_calls with no following tool messages still collapses cleanly."""
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "/x"}}}],
        },
    ]
    out = main.collapse_tool_history_for_gemini(messages)
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert "tool_calls" not in out[0]
    assert "read_file" in out[0]["content"]


def test_estimate_tokens():
    assert main.estimate_text_tokens("") == 0
    assert main.estimate_text_tokens("abcd") >= 1
    msg = {"role": "user", "content": "hello world"}
    assert main.estimate_message_tokens(msg) > 0


def test_serialize_tool_call_dict_passthrough():
    call = {"function": {"name": "read_file", "arguments": {"path": "x"}}, "id": "1"}
    out = main.serialize_tool_call(call)
    assert out["function"]["name"] == "read_file"
    assert out["id"] == "1"


def test_normalize_tool_call_string_arguments():
    call = {"function": {"name": "read_file", "arguments": '{"path": "x.py"}'}}
    name, args = main.normalize_tool_call(call)
    assert name == "read_file"
    assert args == {"path": "x.py"}


def test_normalize_tool_call_bad_json_arguments():
    call = {"function": {"name": "t", "arguments": "not json"}}
    name, args = main.normalize_tool_call(call)
    assert name == "t"
    assert args == {"raw": "not json"}


def test_normalize_tool_call_non_dict_json_arguments():
    call = {"function": {"name": "t", "arguments": '["not", "a", "dict"]'}}
    name, args = main.normalize_tool_call(call)
    assert name == "t"
    assert args == {"raw": ["not", "a", "dict"]}


def test_resolve_multimodal_model_uses_installed_fallback(monkeypatch):
    errors: list[str] = []
    monkeypatch.setattr(main, "show_error", lambda message: errors.append(message))
    cfg = Config()

    resolved = main.resolve_multimodal_model(
        cfg,
        explicit_model=None,
        available=["fallback-model"],
        predicate=lambda _name: False,
        fallback="fallback-model",
        install_hint="install",
        missing_hint="missing",
    )

    assert resolved == "fallback-model"
    assert errors == []


def test_run_args_preview_caps():
    preview = main._run_args_preview({"path": "x" * 200}, limit=40)
    assert len(preview) <= 40


def test_parse_benchmark_embed_args_defaults_and_overrides():
    assert main._parse_benchmark_embed_args("benchmark-embed") == (20, None)
    assert main._parse_benchmark_embed_args("benchmark-embed --count 50") == (50, None)
    assert main._parse_benchmark_embed_args("benchmark-embed --count 7 --model foo:bar") == (7, "foo:bar")
    assert main._parse_benchmark_embed_args("benchmark-embed --count not-a-number") == (20, None)


def test_log_embed_perf_writes_private_bounded_event(monkeypatch, tmp_path):
    from algo_cli import perf_telemetry

    perf_path = tmp_path / "perf_history.jsonl"
    monkeypatch.setattr(perf_telemetry, "PERF_HISTORY_FILE", perf_path)

    main.log_embed_perf(
        {"event": "batch", "batch_size": 4, "wall_ms": 123.4, "model": "m"},
        source="unit-test",
    )

    events = perf_telemetry._private_perf_store().read_events()
    assert len(events) == 1
    assert events[0]["kind"] == "embed"
    payload = events[0]["record"]
    assert payload["event"] == "batch"
    assert payload["source"] == "unit-test"
    assert payload["batch_size"] == 4
    assert payload["model"] == "m"
    assert "timestamp" in payload


def test_force_utf8_console_idempotent_and_safe():
    """UTF-8 console reconfig must be safe to call twice and never raise.

    Regression guard for the display-mojibake fix (2026-06-10): the helper is
    called from main() at every CLI startup, so it must be idempotent and
    never raise on any platform (Win32 calls are wrapped; stream reconfigure
    ignores unsupported streams).
    """
    import sys
    from algo_cli.main import _force_utf8_console

    before_encoding = sys.stdout.encoding
    _force_utf8_console()
    _force_utf8_console()  # second call must not raise
    after_encoding = sys.stdout.encoding
    # Stream is always at least usable as utf-8 (or whatever it already was)
    assert after_encoding is not None
    # Encoding is preserved if it was already utf-8, or replaced with utf-8
    assert (after_encoding or "").lower().replace("-", "") in {
        "utf8",
        before_encoding.lower().replace("-", "") if before_encoding else "",
    }


def test_force_utf8_console_main_call():
    """main() must invoke _force_utf8_console as its first action.

    Guards against future refactors that move the call site and break the
    fix for the Windows mojibake issue.
    """
    import inspect
    from algo_cli import main

    src = inspect.getsource(main.main)
    # The call must appear before load_runtime_env (the next statement).
    utf8_idx = src.find("_force_utf8_console()")
    env_idx = src.find("load_runtime_env")
    assert utf8_idx != -1, "_force_utf8_console call missing from main()"
    assert env_idx != -1, "load_runtime_env call missing from main()"
    assert utf8_idx < env_idx, (
        "_force_utf8_console must be called before load_runtime_env so the "
        "console is in UTF-8 mode before any output occurs"
    )
