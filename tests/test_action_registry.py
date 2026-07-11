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
    from algo_cli.slash_dispatch import SLASH_COMMANDS
    from algo_cli.tools import TOOL_MAP

    specs = action_registry.effective_action_specs()
    tool_specs = {spec.name: spec for spec in specs if spec.kind == "tool"}
    slash_specs = {spec.name: spec for spec in specs if spec.kind == "slash"}

    assert set(TOOL_MAP).issubset(tool_specs)
    assert {command for command, _description in SLASH_COMMANDS}.issubset(slash_specs)
    assert tool_specs["write_file"].requires_approval is True
    assert tool_specs["read_file"].requires_approval is False
    assert slash_specs["/help"].requires_approval is False
    assert slash_specs["/google-login"].requires_approval is True
    assert slash_specs["/code-rag"].requires_approval is True
    assert "privacy" in slash_specs["/code-rag"].tags
    assert "generated" in slash_specs["/help"].tags


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


def test_doctor_reports_unconfigured_xai_oauth_as_optional(monkeypatch, tmp_path) -> None:
    from algo_cli.action_registry import build_doctor_report, render_doctor

    monkeypatch.delenv("XAI_CLIENT_ID", raising=False)
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path / "missing-icl"))

    rendered = render_doctor(build_doctor_report(_cfg()))

    assert "READY    xai-oauth: optional xAI subscription OAuth is not configured" in rendered
    assert "no bundled client id" in rendered


def test_doctor_reports_configured_xai_without_exposing_client_id(monkeypatch, tmp_path) -> None:
    from algo_cli.action_registry import build_doctor_report, render_doctor

    client_id = "configured-xai-client-id-not-for-output"
    monkeypatch.setenv("XAI_CLIENT_ID", client_id)
    monkeypatch.setenv("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT", str(tmp_path / "missing-icl"))

    rendered = render_doctor(build_doctor_report(_cfg()))

    assert "READY    xai-oauth: optional xAI OAuth client configured; no active session" in rendered
    assert client_id not in rendered


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
    from algo_cli import action_registry, slash_dispatch

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


def test_tool_approval_policy_uses_registry_with_safe_memory_exceptions() -> None:
    from algo_cli.action_registry import action_requires_approval

    assert action_requires_approval("model_pull") is True
    assert action_requires_approval("harness_refresh") is True
    assert action_requires_approval("plugins_load") is True
    assert action_requires_approval("credential_helpers_store") is True
    assert action_requires_approval("remember") is False
    assert action_requires_approval("append_lesson") is False
    assert action_requires_approval("read_file") is False
