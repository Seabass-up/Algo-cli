from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from algo_cli.evals import pattern_evidence as evidence


ROOT = Path(__file__).resolve().parents[1]


def test_registration_does_not_promote_unknown_catalog_patterns(tmp_path):
    assert evidence.evidence_status(ROOT, "B1", tmp_path)["implemented"] is None
    result = evidence.evidence_status(ROOT, "O5", tmp_path)
    assert result["implemented"] is True
    assert result["enabled"] is None
    assert result["tested"] is False
    assert result["measured"] is False
    assert evidence.dependency_closure("O5") == ("H2", "O8", "O5")


def test_installed_runner_finds_checkout_from_working_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence, "__file__", str(tmp_path / "site-packages/algo_cli/evals/pattern_evidence.py"))
    monkeypatch.chdir(ROOT / "docs")
    assert evidence.repository_root() == ROOT


def test_unrelated_project_is_not_an_implicit_test_authority(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "not-algo"\n')
    with pytest.raises(ValueError, match="Repository tests unavailable"):
        evidence.repository_root(tmp_path)


def test_changed_transitive_dependency_invalidates_test_evidence(monkeypatch, tmp_path):
    sources = {"algo_cli/pattern_catalog.py": "before"}
    monkeypatch.setattr(evidence, "source_snapshot", lambda *_args: dict(sources))
    evidence.save_report(
        tmp_path / "O5.tests.json",
        {
            "schema_version": 1,
            "sources": dict(sources),
            "pattern_id": "O5",
            "tests": evidence.test_selection("O5"),
            "status": "pass",
            "executed": 10,
            "source_stable": True,
            "created_at": "2026-09-04T00:00:00+00:00",
        },
    )
    assert evidence.evidence_status(ROOT, "O5", tmp_path)["tested"] is True
    sources["algo_cli/pattern_catalog.py"] = "after"
    assert evidence.evidence_status(ROOT, "O5", tmp_path)["tested"] is False


def test_snapshot_hashes_transitive_imports_and_tests():
    snapshot = evidence.source_snapshot(ROOT, "O5")
    assert "algo_cli/pattern_catalog.py" in snapshot
    assert "algo_cli/intelligence/catalog_verifier.py" in snapshot
    assert "tests/test_harness.py" in snapshot
    assert "docs/ALGO.md" in snapshot


def test_missing_or_external_source_fails_closed(tmp_path):
    (tmp_path / "outside.py").symlink_to(ROOT / "algo_cli/harness.py")
    with pytest.raises(ValueError, match="source_unavailable"):
        evidence._read_owned(tmp_path, "outside.py")
    with pytest.raises(ValueError, match="source_unavailable"):
        evidence._read_owned(tmp_path, "deleted.py")


def test_registry_rejects_cycles_and_commands(monkeypatch):
    monkeypatch.setitem(evidence.REGISTRY, "X1", evidence.PatternSpec((), (), ("X2",)))
    monkeypatch.setitem(evidence.REGISTRY, "X2", evidence.PatternSpec((), (), ("X1",)))
    with pytest.raises(ValueError, match="registry_cycle"):
        evidence.dependency_closure("X1")
    with pytest.raises(ValueError, match="not_registered"):
        evidence.dependency_closure("O5; touch /tmp/unsafe")


@pytest.mark.parametrize(
    "xml,code,expected",
    [
        ("<testsuites><testsuite><testcase/></testsuite></testsuites>", 0, "pass"),
        ("<testsuites><testsuite><testcase><skipped/></testcase></testsuite></testsuites>", 0, "fail"),
        ("<testsuites><testsuite><testcase><failure/></testcase></testsuite></testsuites>", 1, "fail"),
        ("<testsuites/>", 0, "fail"),
    ],
)
def test_verifier_uses_fixed_commands_and_requires_executed_tests(monkeypatch, tmp_path, xml, code, expected):
    def fake_run(command, **kwargs):
        assert command[1:3] == ["-m", "pytest"]
        assert "shell" not in kwargs
        assert kwargs["cwd"] == ROOT
        assert kwargs["timeout"] == 180
        assert "tests/test_diagnostic_graph.py" in command
        Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml="))).write_text(xml)
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    report = evidence.verify_pattern(ROOT, "O4", tmp_path)
    assert report["status"] == expected
    assert evidence.evidence_status(ROOT, "O4", tmp_path)["tested"] is (expected == "pass")


def test_mutation_during_tests_cannot_create_current_evidence(monkeypatch, tmp_path):
    snapshots = iter([{"source": "old"}, {"source": "new"}])
    monkeypatch.setattr(evidence, "source_snapshot", lambda *_args: next(snapshots))
    monkeypatch.setattr(evidence.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    assert evidence.verify_pattern(ROOT, "O4", tmp_path)["status"] == "stale"


def test_corrupt_report_is_not_current_evidence(tmp_path):
    path = tmp_path / "O4.tests.json"
    evidence.save_report(path, {"status": "pass"})
    path.write_text(path.read_text().replace('"pass"', '"fail"'))
    assert evidence.load_report(path) is None
