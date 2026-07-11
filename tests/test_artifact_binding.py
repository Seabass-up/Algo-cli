"""Tests for H9 — Ground-Truth Artifact Binding."""
from __future__ import annotations

from algo_cli.intelligence.artifact_binding import ArtifactBinder


def test_bind_artifact() -> None:
    binder = ArtifactBinder()
    binding = binder.bind("claim-1", "/path/to/artifact.json", '{"k": "v"}')
    assert binding.claim_id == "claim-1"
    assert binding.artifact_path == "/path/to/artifact.json"
    assert len(binding.content_hash) == 64


def test_verify_correct_content() -> None:
    binder = ArtifactBinder()
    binder.bind("claim-1", "/path", '{"k": "v"}')
    assert binder.verify("claim-1", '{"k": "v"}') is True


def test_verify_wrong_content() -> None:
    binder = ArtifactBinder()
    binder.bind("claim-1", "/path", '{"k": "v"}')
    assert binder.verify("claim-1", '{"k": "wrong"}') is False


def test_verify_missing_claim() -> None:
    binder = ArtifactBinder()
    assert binder.verify("nope", "content") is False


def test_get_binding() -> None:
    binder = ArtifactBinder()
    binder.bind("claim-1", "/path", "content")
    b = binder.get("claim-1")
    assert b is not None
    assert b.claim_id == "claim-1"


def test_get_missing_returns_none() -> None:
    binder = ArtifactBinder()
    assert binder.get("nope") is None


def test_count() -> None:
    binder = ArtifactBinder()
    assert binder.count() == 0
    binder.bind("c1", "/p", "x")
    assert binder.count() == 1


def test_remove() -> None:
    binder = ArtifactBinder()
    binder.bind("c1", "/p", "x")
    assert binder.remove("c1") is True
    assert binder.remove("c1") is False


def test_bind_bytes_content() -> None:
    binder = ArtifactBinder()
    binder.bind("c1", "/p", b"binary content")
    assert binder.verify("c1", b"binary content") is True


def test_to_dict() -> None:
    binder = ArtifactBinder()
    b = binder.bind("c1", "/p", "x", artifact_type="xml", metadata={"src": "test"})
    d = b.to_dict()
    assert d["artifact_type"] == "xml"
    assert d["metadata"] == {"src": "test"}