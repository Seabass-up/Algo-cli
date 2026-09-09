"""Reviewed product documentation must not widen credential-file discovery."""

import os

import pytest

from algo_cli import harness


def test_shipped_supervisor_guide_is_discovered_by_the_builtin_wiki(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    guide = docs / "supervised-action-review.md"
    guide.write_text("# Supervised One-Shot Action Review\nExact-action confirmations require operator review.")
    monkeypatch.setattr(harness, "_algo_cli_docs_dir", lambda: docs)
    root = next(root for root in harness.built_in_source_roots() if root.kind == "wiki" and root.root == docs)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (root,))
    assert guide in harness.iter_files(root)
    record = harness.make_record(root, guide)
    assert record["id"] == "algo-cli:wiki:supervised-action-review.md"
    monkeypatch.setattr(harness, "get_record", lambda _identity: record)
    body = harness.read_record(record["id"])
    assert "Source: algo-cli:supervised-action-review.md" in body
    assert "Exact-action confirmations require operator review." in body


def test_reviewed_auth_runbook_is_indexed_but_credential_files_are_not(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(harness, "_algo_cli_repo_dir", lambda: tmp_path)
    for name in ("provider-auth-recovery.md", "auth.json", "credentials.md", "secrets.md", "tokens.json"):
        (docs / name).write_text("synthetic public/private filename fixture")
    root = harness.SourceRoot("algo-cli", "wiki", docs, ("*.md", "*.json"), 10)
    assert [path.name for path in harness.iter_files(root)] == ["provider-auth-recovery.md"]
    record = harness.make_record(root, docs / "provider-auth-recovery.md")
    monkeypatch.setattr(harness, "get_record", lambda _identity: record)
    body = harness.read_record(record["id"])
    assert not body.startswith("Error:")
    assert "synthetic public/private filename fixture" in body


def test_external_auth_runbook_copies_are_not_exempt(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "external"
    outside.mkdir()
    monkeypatch.setattr(harness, "_algo_cli_repo_dir", lambda: repo)
    (outside / "provider-auth-recovery.md").write_text("unreviewed copy")
    root = harness.SourceRoot("algo-cli", "wiki", outside, ("*.md",), 10)
    assert harness.iter_files(root) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_symlink_cannot_impersonate_the_public_runbook(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(harness, "_algo_cli_repo_dir", lambda: tmp_path)
    secret = tmp_path / "auth.json"
    secret.write_text("private canary")
    (docs / "provider-auth-recovery.md").symlink_to(secret)
    root = harness.SourceRoot("algo-cli", "wiki", docs, ("*.md",), 10)
    assert harness.iter_files(root) == []
    (docs / "provider-auth-recovery.md").unlink()
    os.link(secret, docs / "provider-auth-recovery.md")
    assert harness.iter_files(root) == []


def test_reviewed_auth_runbook_edits_participate_in_source_freshness(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(harness, "_algo_cli_repo_dir", lambda: tmp_path)
    runbook = docs / "provider-auth-recovery.md"
    runbook.write_text("public recovery guidance")
    root = harness.SourceRoot("algo-cli", "wiki", docs, ("*.md",), 10)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (root,))
    harness.INDEX_PATH.write_text("{}")
    modified_ns = harness.INDEX_PATH.stat().st_mtime_ns + 2_000_000_000
    os.utime(runbook, ns=(modified_ns, modified_ns))
    assert harness._source_watermark_ns({"records": [{"path": str(runbook)}]}) == modified_ns
