from __future__ import annotations

import json
from types import SimpleNamespace

from algo_cli import decision_catalog as catalog
from algo_cli.config import Config
from algo_cli.kernels.manifest import KernelSpec


def test_catalog_ranks_local_metadata_without_importing_custom_implementations(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog.config, "CONFIG_DIR", tmp_path)
    (tmp_path / "ALGO.md").write_text(
        '### A1. Parser boundary invariant\nStatus: proposed\n**Applicability:** {}\n'
        '### A2. Windows parser\nStatus: implemented\n**Applicability:** {"environments":["windows"]}\n'
        '### A3. Parser retired\nStatus: retired\n'
    )
    specs = [KernelSpec("parser-review", "Inspect parser boundaries", ["must.not.import"], [], [], "low", "active", "user"),
             KernelSpec("future-parser", "Parser automation", [], [], [], "low", "planned")]
    monkeypatch.setattr(catalog.manifest, "list_kernels", lambda: specs)
    monkeypatch.setattr(catalog.harness, "runtime_pattern_context", lambda: catalog.pattern_catalog.PatternContext(environment="darwin"))
    question = {"q": {"type": "noul", "instructions": "Does this parser have a boundary error?"}}
    result = catalog.candidates(Config(), question, "parser-review")
    assert [row["id"] for row in result["kernels"]] == ["parser-review"]
    assert [row["id"] for row in result["algorithms"]] == ["A1"]
    assert result["algorithms"][0]["status"] == "proposed" and not result["algorithms"][0]["executable"]
    assert "must.not.import" not in json.dumps(result)
    assert "path" not in json.dumps(result)
    assert catalog.questions_for(result)["algorithms"]["criteria"]["none"]


def test_catalog_errors_and_empty_public_template_do_not_invent_algorithms(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog.config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(catalog.manifest, "list_kernels", lambda: [])
    question = {"q": {"type": "noul", "instructions": "Is the parser valid?"}}
    (tmp_path / "ALGO.md").write_text("### A1. Parser\n### A1. Duplicate\n")
    invalid = catalog.candidates(Config(), question, "")
    assert invalid["algorithms"] == [] and invalid["issues"]
    (tmp_path / "ALGO.md").write_text("# Empty personal template\n")
    empty = catalog.candidates(Config(), question, "")
    assert empty["algorithms"] == [] and not empty["issues"]


def test_low_probability_and_no_match_never_become_action_authority():
    local = {"kernels": [{"id": "parser", "status": "active"}],
             "algorithms": [{"id": "A1", "status": "proposed", "executable": False}]}
    answers = {"kernels": {"choice": "item_0", "probabilities": {"item_0": 0.55, "none": 0.45}},
               "algorithms": {"choice": "item_0", "probabilities": {"item_0": 0.95, "none": 0.05}}}
    result = catalog.recommendations(local, answers, 0.8)
    assert result["kernels"]["selected"] is None
    assert result["algorithms"]["selected"]["status"] == "proposed"
    assert all(not row["execution_authority"] and row["requires_main_model_judgment"] for row in result.values())


def test_exact_catalog_reader_rejects_symlink_instead_of_following_it(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog.config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(catalog.manifest, "list_kernels", lambda: [])
    target = tmp_path / "other.md"
    target.write_text("### A1. Parser\n")
    (tmp_path / "ALGO.md").symlink_to(target)
    result = catalog.candidates(SimpleNamespace(), {"q": {"instructions": "Parser"}}, "")
    assert result["algorithms"] == [] and result["issues"]
