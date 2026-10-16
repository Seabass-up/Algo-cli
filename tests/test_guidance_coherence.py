"""Operational guidance must match native runtime contracts, including cached records."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from algo_cli import continuum_memory, harness, prompt_rules
from algo_cli.config import Config


@pytest.mark.parametrize("name,contract", [
    ("memory-recall-ritual", "continuum-memory-v1"),
    ("smart-error-recovery", "mutation-recovery-v1"),
])
def test_legacy_skill_is_rejected_at_index_cache_and_read_boundaries(monkeypatch, tmp_path, name, contract):
    path = tmp_path / f"{name}.md"
    legacy = f"---\nname: {name}\n---\n# Old operational guidance\n"
    path.write_text(legacy)
    root = harness.SourceRoot("algo-cli", "skill", tmp_path, ("*.md",), 10)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (root,))
    record = harness.make_record(root, path)
    assert harness.should_exclude_from_index(path, harness.parse_frontmatter(legacy), kind="skill")
    assert harness._normalize_index_records({"records": [record]})["records"] == []
    monkeypatch.setattr(harness, "get_record", lambda _: record)
    assert "incompatible" in harness.read_record(record["id"])
    current = f"---\nname: {name}\nruntime_contract: {contract}\n---\n# Current guidance\n"
    path.write_text(current)
    assert not harness.should_exclude_from_index(path, harness.parse_frontmatter(current), kind="skill")
    record = harness.make_record(root, path)
    assert harness._normalize_index_records({"records": [record]})["records"] == [record]
    assert "Current guidance" in harness.read_record(record["id"])
    # A retained cache entry cannot authorize reading a newly replaced legacy body.
    path.write_text(legacy)
    assert "incompatible" in harness.read_record(record["id"])


def test_historical_documents_are_not_keyword_censored(tmp_path):
    path = tmp_path / "history.md"
    path.write_text("# Historical memory.json and timeout retry behavior\n")
    assert not harness.should_exclude_from_index(path, {})


@pytest.mark.parametrize("name,contract", [
    ("memory-recall-ritual", "continuum-memory-v1"),
    ("smart-error-recovery", "mutation-recovery-v1"),
])
def test_renamed_declared_skill_is_checked_at_every_boundary(monkeypatch, tmp_path, name, contract):
    path = tmp_path / "SKILL.md"
    root = harness.SourceRoot("fixture", "skill", tmp_path, ("*.md",), 10)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (root,))
    legacy = f"---\nname: {name}\ntitle: Unrelated display title\n---\n# Legacy\n"
    path.write_text(legacy)
    record = harness.make_record(root, path)
    assert harness.should_exclude_from_index(path, harness.parse_frontmatter(legacy), kind="skill")
    assert harness._normalize_index_records({"records": [record]})["records"] == []
    monkeypatch.setattr(harness, "get_record", lambda _: record)
    assert "incompatible" in harness.read_record(record["id"])
    current = legacy.replace("---\n# Legacy", f"runtime_contract: {contract}\n---\n# Current")
    path.write_text(current)
    record = harness.make_record(root, path)
    assert not harness._incompatible_skill_record(record)
    assert "Current" in harness.read_record(record["id"])
    path.write_text(legacy)
    assert "incompatible" in harness.read_record(record["id"])


@pytest.mark.parametrize("name", ["memory-recall-ritual", "smart-error-recovery"])
def test_wiki_name_collision_is_not_an_operational_skill(monkeypatch, tmp_path, name):
    path = tmp_path / f"{name}.md"
    path.write_text(f"---\nname: {name}\n---\n# Historical notes\n")
    root = harness.SourceRoot("fixture", "wiki", tmp_path, ("*.md",), 10)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (root,))
    monkeypatch.setattr(harness, "_merge_runtime_capability_records", lambda index, **kw: index)
    result = harness.build_index()
    assert len(result["records"]) == 1
    record = result["records"][0]
    assert record["kind"] == "wiki"
    monkeypatch.setattr(harness, "get_record", lambda _: record)
    assert "Historical notes" in harness.read_record(record["id"])


@pytest.mark.parametrize("current_contract", [True, False])
def test_identity_less_renamed_cache_is_rebuilt_even_with_matching_signature(monkeypatch, tmp_path, current_contract):
    path = tmp_path / "SKILL.md"
    contract = "runtime_contract: continuum-memory-v1\n" if current_contract else ""
    path.write_text(f"---\nname: memory-recall-ritual\n{contract}---\n# Guidance\n")
    root = harness.SourceRoot("fixture", "skill", tmp_path, ("*.md",), 10)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (root,))
    monkeypatch.setattr(harness, "_merge_runtime_capability_records", lambda index, **kw: index)
    record = harness.make_record(root, path)
    record.pop("skill_name", None)
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps({"source_policy": harness._source_policy(), "records": [record]}))
    assert harness.index_is_stale()
    result = harness.load_index()
    if not current_contract:
        assert result["records"] == []
        return
    current = next(row for row in result["records"] if row["id"] == record["id"])
    assert current["skill_name"] == "memory-recall-ritual"
    assert result["refresh_stats"]["rebuilt_records"] == 1


def test_old_native_skill_schema_falls_back_before_publication(monkeypatch, tmp_path):
    def run(command, **kwargs):
        Path(command[2]).write_text(json.dumps({"records": [{
            "kind": "skill", "path": str(tmp_path / "SKILL.md"),
            "title": "Unrelated display title", "runtime_contract": "",
        }]}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(harness, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(harness, "_EXTERNAL_SOURCES_ENABLED", True)
    monkeypatch.setattr(harness, "find_rust_indexer", lambda: Path("synthetic-indexer"))
    monkeypatch.setattr(harness.subprocess, "run", run)
    assert harness.build_index_with_rust() is None


def test_bundled_skill_contracts_and_recovery_rules():
    root = harness._algo_cli_repo_skills_dir()
    memory = (root / "memory-recall-ritual.md").read_text()
    recovery = (root / "smart-error-recovery.md").read_text()
    assert "runtime_contract: continuum-memory-v1" in memory
    assert "Continuum" in memory and "no plaintext fallback" in memory
    assert "runtime_contract: mutation-recovery-v1" in recovery
    assert "unknown_outcome" in recovery and "retry_allowed" in recovery
    assert "switch to `edit_file`" not in recovery


def test_catalog_guidance_uses_configured_root(monkeypatch, tmp_path):
    from algo_cli import config
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    guidance = prompt_rules.catalog_guidance()
    assert str(tmp_path / "ALGO.md") in guidance
    assert "public template" in guidance and "~/.algo_cli/ALGO.md" not in guidance


@pytest.mark.parametrize("legacy", [
    "- For Algo algorithm/pattern catalog guidance, use and update docs/ALGO.md.",
    "- For Algo algorithm/pattern catalog guidance, use and update the live catalog at ~/.algo_cli/ALGO.md. docs/ALGO.md is only the empty public template, not the live catalog.",
    "- To read/update the live catalog, use the path in the runtime Algorithm Catalog section; docs/ALGO.md is only the empty public template.",
])
def test_saved_product_rule_is_updated_in_prompt_only_and_custom_text_survives(legacy):
    custom = "Custom: review docs/ALGO.md as a public template.\n\nDo not alter my instructions.\n"
    cfg = Config(system=legacy + "\n" + custom)
    projected = prompt_rules.current_catalog_system_prompt(cfg.system)
    assert legacy not in projected
    assert projected.endswith(custom)
    assert projected == custom
    assert prompt_rules.current_catalog_system_prompt(projected) == projected
    assert cfg.system == legacy + "\n" + custom


def test_context_and_validation_use_same_explicit_project_selection(monkeypatch):
    calls = []
    cfg = Config(continuum_enabled=True, continuum_active_projects=["algo-cli"])
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: [])
    def invoke(cfg, operation, payload, *, scope):
        calls.append((operation, payload, scope))
        return {"ok": True, "context": {"items": [{"id": "required-global", "required": True}],
                "project_selection": {"requested": ["algo-cli"], "effective": ["algo-cli"], "unknown_projects": []}}}
    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    result = json.loads(continuum_memory.prompt_context(cfg, "review harness"))
    assert set(result["contexts"]) == {"shared", "private"}
    assert "required-global" in json.dumps(result)
    assert len(calls) == 4
    assert all(payload["active_projects"] == ["algo-cli"] for _, payload, _ in calls)
    assert cfg.continuum_active_projects == ["algo-cli"]


@pytest.mark.parametrize("projects", [["Unknown"], ["algo-cli", "algo-cli"], "algo-cli", [True]])
def test_invalid_project_selection_fails_before_transport(monkeypatch, projects):
    cfg = Config(continuum_enabled=True, continuum_active_projects=projects)
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: [])
    monkeypatch.setattr(continuum_memory, "invoke_continuum", lambda *a, **kw: pytest.fail("unexpected call"))
    with pytest.raises(continuum_memory.ContinuumMemoryError, match="project selection"):
        continuum_memory.prompt_context(cfg, "fixture")


def test_unknown_native_project_retains_explicit_global_fallback_diagnostic(monkeypatch):
    cfg = Config(continuum_enabled=True, continuum_active_projects=["not-registered"])
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: [])
    monkeypatch.setattr(continuum_memory, "invoke_continuum", lambda *a, **kw: {
        "ok": True, "context": {"items": [{"id": "global-required", "required": True}],
            "project_selection": {"requested": ["not-registered"], "effective": None,
                                  "unknown_projects": ["not-registered"]}}})
    result = json.loads(continuum_memory.prompt_context(cfg, "fixture"))
    for packet in result["contexts"].values():
        assert packet["context"]["project_selection"]["unknown_projects"] == ["not-registered"]
        assert packet["context"]["project_selection"]["effective"] is None
        assert packet["context"]["items"][0]["id"] == "global-required"


@pytest.mark.parametrize("selection", [
    None, {}, {"requested": ["wrong"], "effective": ["algo-cli"], "unknown_projects": []},
    {"requested": ["algo-cli"], "effective": ["other"], "unknown_projects": []},
    {"requested": ["algo-cli"], "effective": None, "unknown_projects": []},
    {"requested": ["algo-cli"], "unknown_projects": ["algo-cli"]},
    {"requested": ["algo-cli"], "effective": None, "unknown_projects": ["other"]},
    {"requested": ["algo-cli"], "effective": ["algo-cli"], "unknown_projects": ["algo-cli"]},
    {"requested": ["algo-cli"], "effective": ["algo-cli"], "unknown_projects": [True]},
    {"requested": ["algo-cli"], "effective": [], "unknown_projects": []},
    {"requested": ["algo-cli"], "effective": ["algo-cli", "algo-cli"], "unknown_projects": []},
    {"requested": ["algo-cli"], "effective": ["algo-cli", "Other"], "unknown_projects": []},
    {"requested": ["algo-cli"], "effective": None, "unknown_projects": ["algo-cli", "algo-cli"]},
])
def test_mismatched_selection_never_injects_unvalidated_context(monkeypatch, selection):
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: [])
    monkeypatch.setattr(continuum_memory, "invoke_continuum", lambda *a, **kw: {
        "ok": True, "context": {"project_selection": selection}})
    with pytest.raises(continuum_memory.ContinuumMemoryError, match="project selection"):
        continuum_memory.prompt_context(Config(continuum_enabled=True, continuum_active_projects=["algo-cli"]), "fixture")


def test_legitimate_required_project_expansion_is_retained(monkeypatch):
    cfg = Config(continuum_enabled=True, continuum_active_projects=["algo-cli"])
    selection = {"requested": ["algo-cli"], "effective": ["algo-cli", "research"], "unknown_projects": []}
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: [])
    monkeypatch.setattr(continuum_memory, "invoke_continuum", lambda *a, **kw: {
        "ok": True, "context": {"project_selection": selection, "items": [{"id": "required-dependency", "required": True}]}})
    result = json.loads(continuum_memory.prompt_context(cfg, "fixture"))
    for packet in result["contexts"].values():
        assert packet["context"]["project_selection"] == selection
        assert packet["context"]["items"][0]["id"] == "required-dependency"


def test_no_project_selection_preserves_legacy_global_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: [])
    def invoke(cfg, operation, payload, *, scope):
        calls.append(payload)
        return {"ok": True, "context": {"items": [{"id": "required", "required": True}]}}
    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    assert "required" in continuum_memory.prompt_context(Config(continuum_enabled=True), "fixture")
    assert all("active_projects" not in payload for payload in calls)


def test_project_selection_survives_budget_retry_and_validation_refusal(monkeypatch):
    calls = []
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: [])
    def invoke(cfg, operation, payload, *, scope):
        calls.append((operation, payload, scope))
        if operation == "validate_context":
            return {"ok": False, "decision": "REFUSE_STALE"}
        if payload["budget_bytes"] == 12_000:
            return {"ok": False, "decision": "REFUSE_BUDGET", "error": "budget",
                    "details": {"budget_bytes": 12_000, "required_bytes": 13_000}}
        return {"ok": True, "context": {"project_selection": {
            "requested": ["algo-cli"], "effective": ["algo-cli"], "unknown_projects": []}}}
    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    with pytest.raises(continuum_memory.ContinuumMemoryError, match="could not be verified"):
        continuum_memory.prompt_context(Config(continuum_enabled=True, continuum_active_projects=["algo-cli"]), "fixture")
    assert [op for op, _, _ in calls] == ["context", "context", "validate_context"]
    assert all(payload["active_projects"] == ["algo-cli"] for _, payload, _ in calls)


def test_lexical_retrieval_provenance_is_observed_metadata(monkeypatch):
    monkeypatch.setattr(harness, "load_index", lambda: {"records": []})
    assert harness.retrieval_index()["retrieval_backend"] == "lexical"


def test_refresh_rebuilds_legacy_cache_even_when_file_signature_matches(monkeypatch, tmp_path):
    path = tmp_path / "smart-error-recovery.md"
    path.write_text("---\nruntime_contract: mutation-recovery-v1\n---\n# Safe recovery\n")
    root = harness.SourceRoot("algo-cli", "skill", tmp_path, ("*.md",), 10)
    record = harness.make_record(root, path)
    record.pop("runtime_contract")
    monkeypatch.setattr(harness, "all_source_roots", lambda: (root,))
    monkeypatch.setattr(harness, "_merge_runtime_capability_records", lambda index, **kw: index)
    result = harness.build_index({"records": [record]})
    current = next(row for row in result["records"] if row["id"] == record["id"])
    assert current["runtime_contract"] == "mutation-recovery-v1"
    assert result["refresh_stats"]["rebuilt_records"] == 1
