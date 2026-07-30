from __future__ import annotations

from types import SimpleNamespace


def _cfg(**overrides):
    values = {
        "cloud": False,
        "host": "http://127.0.0.1:9",
        "model": "llama3.2",
        "safe_mode": True,
        "auto_mode": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_action_registry_declares_mutation_and_approval_metadata() -> None:
    from algo_cli.action_registry import get_action_spec, list_action_specs

    names = {spec.name for spec in list_action_specs(include_archived=True)}
    assert {"read_file", "write_file", "run_shell", "/safe", "/doctor", "ollama-cli-env"}.issubset(names)

    write = get_action_spec("write_file")
    assert write.mutates_state is True
    assert write.requires_approval is True
    assert "file" in write.tags

    shell = get_action_spec("run_shell")
    assert shell.mutates_state is True
    assert shell.requires_approval is True
    assert shell.risk_level == "high"

    legacy = get_action_spec("ollama-cli-env")
    assert legacy.archived is True
    assert "ALGO_CLI" in legacy.replacement


def test_effective_action_specs_cover_runtime_tool_and_slash_surface() -> None:
    from algo_cli import action_registry
    from algo_cli.oliver_slash_dispatch import SLASH_COMMANDS
    from algo_cli.tools import TOOL_MAP

    specs = action_registry.effective_action_specs()
    tool_specs = {spec.name: spec for spec in specs if spec.kind == "tool"}
    slash_specs = {spec.name: spec for spec in specs if spec.kind == "slash"}

    assert set(TOOL_MAP).issubset(tool_specs)
    assert {command for command, _description in SLASH_COMMANDS}.issubset(slash_specs)
    assert tool_specs["write_file"].requires_approval is True
    assert tool_specs["read_file"].requires_approval is False
    assert slash_specs["/help"].requires_approval is False
    assert slash_specs["/config"].requires_approval is True
    assert slash_specs["/code-rag"].requires_approval is True
    assert "privacy" in slash_specs["/code-rag"].tags
    assert "generated" in slash_specs["/help"].tags


def test_live_capability_registry_separates_presence_policy_and_callability(monkeypatch) -> None:
    import sys

    from algo_cli import action_registry, harness

    monkeypatch.setattr(harness, "_EXTERNAL_SOURCES_ENABLED", False)
    records = action_registry.capability_registry_snapshot(
        verified_at="2026-07-29T00:00:00Z",
    )
    by_name = {record.name: record for record in records}

    assert len(by_name) == len(action_registry.effective_action_specs(include_archived=True))
    write = by_name["write_file"]
    assert write.installed is True
    assert write.enabled is True
    assert write.policy_allowed is True
    assert write.model_callable is True
    assert write.authenticated is None
    assert write.status == "ready"
    assert "local_mutation" in write.write_effects
    assert write.as_dict()["scope"] == {
        "project": "algo-cli",
        "platform": sys.platform,
        "version": "0.18.0",
    }

    external = by_name["harness.external_agent_stores"]
    assert external.installed is True
    assert external.enabled is False
    assert external.policy_allowed is False
    assert external.model_callable is False
    assert external.status == "disabled"
    assert "installed but disabled" in external.reason
    assert "explicitly configured roots" in external.reason

    archived = by_name["ollama-cli-env"]
    assert archived.enabled is False
    assert archived.status == "archived"
    assert archived.authority == "historical"


def test_provider_tool_schema_description_comes_from_action_registry() -> None:
    import json

    from algo_cli import action_registry, tools
    from algo_cli.tool_schema import serialized_tool_schemas

    schema = json.loads(serialized_tool_schemas([tools.harness_search]))[0]

    assert schema["function"]["description"] == action_registry.get_action_spec(
        "harness_search"
    ).description
    assert schema["function"]["description"].startswith(
        "Search only harness sources enabled"
    )


def test_external_store_capability_requires_enabled_current_index(monkeypatch) -> None:
    from algo_cli import action_registry, harness

    monkeypatch.setattr(
        harness,
        "source_roots_diagnostics",
        lambda records=None: {
            "built_in_adapter_roots": 4,
            "available_adapter_roots": 2,
            "indexed_records": len(records or []),
        },
    )
    monkeypatch.setattr(
        harness,
        "_INDEX_CACHE",
        {
            "source_policy": {"external_agent_stores": True},
            "records": [{"id": "codex:skill:one"}],
        },
    )
    cfg = SimpleNamespace(external_harness_sources_enabled=True)

    ready = action_registry._external_store_runtime_state(cfg)
    harness._INDEX_CACHE["source_policy"]["external_agent_stores"] = False
    stale = action_registry._external_store_runtime_state(cfg)

    assert ready["status"] == "ready"
    assert "1 records are indexed" in ready["reason"]
    assert stale["status"] == "degraded"
    assert "refresh is required" in stale["reason"]


def test_doctor_degrades_direct_cloud_api_without_key(monkeypatch, tmp_path) -> None:
    from algo_cli.action_registry import build_doctor_report

    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path / "missing-icl"))
    report = build_doctor_report(_cfg(cloud=True, model="qwen3:cloud"))
    data = report.as_dict()

    assert data["overall_status"] == "degraded"
    messages = "\n".join(finding["message"] for finding in data["findings"])
    assert "direct Cloud API disabled" in messages
    assert "optional index-compute-lab context is disabled" in messages


def test_doctor_reports_web_tools_degraded_without_cloud_key(monkeypatch, tmp_path) -> None:
    from algo_cli.action_registry import build_doctor_report, render_doctor

    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path / "missing-icl"))

    report = build_doctor_report(_cfg(cloud=False, model="glm-5.2:cloud"))
    rendered = render_doctor(report)

    assert "web-tools" in rendered
    assert "web_search/web_fetch disabled because OLLAMA_API_KEY is missing" in rendered
    assert "ALGO_CLI_ENV_FILE" in rendered
    assert "~/.algo_cli/env" in rendered


def test_doctor_reports_web_tools_ready_with_cloud_key(monkeypatch, tmp_path) -> None:
    from algo_cli.action_registry import build_doctor_report, render_doctor

    monkeypatch.setenv("OLLAMA_API_KEY", "token")
    root = tmp_path / "icl"
    atoms = root / "atoms"
    atoms.mkdir(parents=True)
    (root / "query.py").write_text("", encoding="utf-8")
    (atoms / "ranked-association-map.json").write_text("{}", encoding="utf-8")
    (atoms / "alias-table.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(root))

    report = build_doctor_report(_cfg(cloud=False, model="glm-5.2:cloud"))
    rendered = render_doctor(report)

    assert "READY    web-tools: web_search/web_fetch configured via OLLAMA_API_KEY" in rendered


def test_doctor_reports_unconfigured_xai_api_as_optional(monkeypatch, tmp_path) -> None:
    from algo_cli.action_registry import build_doctor_report, render_doctor

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_CLIENT_ID", raising=False)
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path / "missing-icl"))

    rendered = render_doctor(build_doctor_report(_cfg()))

    assert "READY    xai-api: optional xAI API key is not configured" in rendered
    assert "algo-cli config setup xai" in rendered


def test_doctor_reports_configured_xai_without_exposing_api_key(monkeypatch, tmp_path) -> None:
    from algo_cli.action_registry import build_doctor_report, render_doctor

    api_key = "configured-xai-api-key-not-for-output"
    monkeypatch.setenv("XAI_API_KEY", api_key)
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path / "missing-icl"))

    rendered = render_doctor(build_doctor_report(_cfg()))

    assert "READY    xai-api: optional xAI API key configured" in rendered
    assert api_key not in rendered


def test_doctor_reports_icl_ranked_map_ready(monkeypatch, tmp_path) -> None:
    from algo_cli.action_registry import build_doctor_report

    root = tmp_path / "icl"
    atoms = root / "atoms"
    atoms.mkdir(parents=True)
    (root / "query.py").write_text("", encoding="utf-8")
    (atoms / "ranked-association-map.json").write_text("{}", encoding="utf-8")
    (atoms / "alias-table.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(root))

    report = build_doctor_report(_cfg(index_compute_lab_auto_inject=True))
    messages = "\n".join(finding["message"] for finding in report.as_dict()["findings"])
    assert "index-compute-lab graph ready" in messages


def test_action_registry_runtime_audit_checks_tool_and_slash_existence(monkeypatch) -> None:
    from algo_cli import action_registry

    ready = action_registry.audit_action_registry_runtime()
    assert ready.overall_status == "ready"
    ready_messages = "\n".join(finding.message for finding in ready.findings)
    assert "runtime surface:" in ready_messages
    assert "ActionSpec coverage:" in ready_messages
    assert "tools covered" in ready_messages
    assert "slash commands covered" in ready_messages
    assert "unique live capability records expose the complete truth contract" in ready_messages

    fake_tool = action_registry._spec(
        "missing_tool_for_test",
        "tool",
        "Missing tool fixture.",
        "test",
        ("test",),
        "Exercises registry audit.",
        "low",
        False,
        False,
        True,
    )
    fake_slash = action_registry._spec(
        "/missing-slash-for-test",
        "slash",
        "Missing slash fixture.",
        "test",
        ("test",),
        "Exercises registry audit.",
        "low",
        False,
        False,
        True,
    )
    monkeypatch.setattr(action_registry, "ACTION_SPECS", action_registry.ACTION_SPECS + (fake_tool, fake_slash))

    report = action_registry.audit_action_registry_runtime()
    messages = "\n".join(finding.message for finding in report.findings)

    assert report.overall_status == "blocked"
    assert "missing_tool_for_test" in messages
    assert "/missing-slash-for-test" in messages


def test_action_registry_audit_detects_declared_but_undispatched_slash(monkeypatch) -> None:
    from algo_cli import action_registry, oliver_slash_dispatch as slash_dispatch

    monkeypatch.setattr(
        slash_dispatch,
        "SLASH_COMMANDS",
        slash_dispatch.SLASH_COMMANDS + [("/declared-only", "Missing dispatch fixture")],
    )

    report = action_registry.audit_action_registry_runtime()
    messages = "\n".join(finding.message for finding in report.findings)

    assert report.overall_status == "blocked"
    assert "slash commands declared but not dispatched" in messages
    assert "/declared-only" in messages


def test_agent_runtime_kernel_actions_have_curated_risk_metadata() -> None:
    from algo_cli.action_registry import get_action_spec

    delegate = get_action_spec("agent.delegate")
    report = get_action_spec("agent.report")
    resume = get_action_spec("agent.thread.resume")

    assert delegate.kind == "kernel"
    assert delegate.mutates_state is True
    assert delegate.requires_approval is True
    assert report.mutates_state is False
    assert report.requires_approval is False
    assert resume.requires_approval is True


def test_tool_approval_policy_uses_registry_and_protects_durable_memory() -> None:
    from algo_cli.action_registry import action_requires_approval

    assert action_requires_approval("model_pull") is True
    assert action_requires_approval("harness_refresh") is True
    assert action_requires_approval("plugins_load") is True
    assert action_requires_approval("credential_helpers_store") is True
    assert action_requires_approval("remember") is True
    assert action_requires_approval("append_lesson") is True
    assert action_requires_approval("read_file") is False
