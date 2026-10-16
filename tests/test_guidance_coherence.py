"""Operational guidance must match native runtime contracts, including cached records."""

import json

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
    record = harness.make_record(root, path)
    assert harness.should_exclude_from_index(path, harness.parse_frontmatter(legacy))
    assert harness._normalize_index_records({"records": [record]})["records"] == []
    monkeypatch.setattr(harness, "get_record", lambda _: record)
    assert "incompatible" in harness.read_record(record["id"])
    current = f"---\nname: {name}\nruntime_contract: {contract}\n---\n# Current guidance\n"
    path.write_text(current)
    assert not harness.should_exclude_from_index(path, harness.parse_frontmatter(current))
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


def test_saved_product_rule_is_updated_in_prompt_only_and_custom_text_survives():
    legacy = "- For Algo algorithm/pattern catalog guidance, use and update docs/ALGO.md."
    custom = "Custom: review docs/ALGO.md as a public template.\n\nDo not alter my instructions.\n"
    cfg = Config(system=legacy + "\n" + custom)
    projected = prompt_rules.current_catalog_system_prompt(cfg.system)
    assert legacy not in projected
    assert projected.endswith(custom)
    assert "runtime Algorithm Catalog" in projected
    assert cfg.system == legacy + "\n" + custom


def test_context_and_validation_use_same_explicit_project_selection(monkeypatch):
    calls = []
    cfg = Config(continuum_enabled=True, continuum_active_projects=["algo-cli"])
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: [])
    def invoke(cfg, operation, payload, *, scope):
        calls.append((operation, payload, scope))
        return {"ok": True, "context": {"items": [{"id": "required-global", "required": True}],
                "project_selection": {"requested": ["algo-cli"], "unknown_projects": []}}}
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


@pytest.mark.parametrize("selection", [None, {}, {"requested": ["wrong"], "unknown_projects": []}])
def test_mismatched_selection_never_injects_unvalidated_context(monkeypatch, selection):
    monkeypatch.setattr(continuum_memory, "recall_facts", lambda _: [])
    monkeypatch.setattr(continuum_memory, "invoke_continuum", lambda *a, **kw: {
        "ok": True, "context": {"project_selection": selection}})
    with pytest.raises(continuum_memory.ContinuumMemoryError, match="project selection"):
        continuum_memory.prompt_context(Config(continuum_enabled=True, continuum_active_projects=["algo-cli"]), "fixture")


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
            "requested": ["algo-cli"], "unknown_projects": []}}}
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
