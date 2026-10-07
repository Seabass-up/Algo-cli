"""Native and legacy index records obey the Python metadata-only boundary."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from algo_cli import harness, tools


MARKER = "SYNTHETIC_PRIVATE_VALUE"


def legacy_record(path):
    return {
        "id": "openclaw:extension:" + path.name, "harness": "openclaw", "kind": "extension",
        "path": str(path), "relative_path": path.name, "title": MARKER, "summary": MARKER,
        "index_text": MARKER, "search_text": MARKER.lower(), "heading_text": MARKER,
        "description": MARKER, "tags": [MARKER], "links": [MARKER], "snippet": MARKER,
        "extra_body": {"unknown": MARKER}, "file_size": 0, "file_mtime_ns": 0,
        "embedding": [1.0], "embedding_model": "fixture", "embedding_dimensions": 1,
        "embedding_identity": {"model": "fixture"},
    }


@pytest.mark.parametrize("name", [
    "openclaw.json", "OPENCLAW.JSON", "installs.json", ".mcp.json", "custom.app.json",
    ".codex-remote-plugin-install.json", ".codex-plugin/plugin.json", ".CODEX-PLUGIN/plugin.json",
    ".Codex-Plugin/PLUGIN.JSON",
])
def test_legacy_metadata_drops_every_body_copy_and_derived_embedding(tmp_path, name):
    original = legacy_record(tmp_path / name)
    clean = harness._normalize_index_records({"records": [original]})
    serialized = json.dumps(clean).lower()
    assert MARKER.lower() not in serialized
    assert clean["records"][0]["id"] == original["id"]
    assert clean["records"][0]["path"] == original["path"]
    assert "metadata" in serialized
    assert all(key not in clean["records"][0] for key in harness._EMBEDDING_FIELDS)
    assert harness._normalize_index_records(clean) == clean
    assert original["summary"] == MARKER


def test_windows_spelling_and_unknown_body_keys_do_not_bypass_migration():
    original = legacy_record(Path("placeholder"))
    original.update(path=r"C:\fixture\.CODEX-PLUGIN\plugin.json", relative_path="unrelated.json")
    assert MARKER.lower() not in json.dumps(harness._normalize_index_records({"records": [original]})).lower()


def test_fresh_python_metadata_and_safe_embeddings_are_preserved(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"search": {"env": {"CUSTOM": MARKER}}}}))
    record = harness.make_record(harness.SourceRoot("codex", "mcp", tmp_path, ("*.json",)), path)
    record.update(embedding=[1.0], embedding_model="fixture")
    clean = harness._normalize_index_records({"records": [record]})
    assert clean["records"] == [record]
    assert "search" in record["title"] and MARKER not in json.dumps(clean)


def test_old_native_binary_is_staged_before_canonical_publication(monkeypatch, tmp_path):
    canonical = tmp_path / "harness_index.json"
    canonical.write_text('{"records":[]}')
    original = canonical.read_bytes()
    record = legacy_record(tmp_path / "openclaw.json")
    staged_paths = []

    def run(command, **_kwargs):
        output = Path(command[2])
        assert output != canonical and output.parent.parent == tmp_path
        assert canonical.read_bytes() == original
        output.write_text(json.dumps({"records": [record], "indexer": "rust"}))
        staged_paths.append(output)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(harness, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(harness, "INDEX_PATH", canonical)
    monkeypatch.setattr(harness, "_EXTERNAL_SOURCES_ENABLED", True)
    monkeypatch.setattr(harness, "find_rust_indexer", lambda: Path("synthetic-indexer"))
    monkeypatch.setattr(harness.subprocess, "run", run)
    monkeypatch.setattr(harness, "_merge_runtime_capability_records", lambda index, **_: index)
    monkeypatch.setattr(harness, "_merge_pattern_records", lambda index, **_: index)
    clean = harness.build_index_with_rust({"records": [record]})
    assert clean is not None and MARKER.lower() not in json.dumps(clean).lower()
    assert "embedding" not in clean["records"][0]
    assert canonical.read_bytes() == original
    assert all(not output.parent.exists() for output in staged_paths)


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("name", ["openclaw.json", ".CODEX-PLUGIN/plugin.json", ".Codex-Plugin/PLUGIN.JSON"])
def test_restart_rewrites_legacy_index_before_tools_or_embeddings(monkeypatch, tmp_path, refresh, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"unknown": {"value": MARKER}}))
    record = legacy_record(path)
    canonical = tmp_path / "harness_index.json"
    canonical.write_text(json.dumps({"records": [record]}))
    monkeypatch.setattr(harness, "INDEX_PATH", canonical)
    monkeypatch.setattr(harness, "index_is_stale", lambda **_: False)
    monkeypatch.setattr(harness, "all_source_roots", lambda: ())
    harness._set_index_cache(None)
    clean = harness.load_index(refresh=refresh)
    assert MARKER.lower() not in json.dumps(clean).lower()
    assert MARKER.lower() not in canonical.read_text().lower()
    assert harness.load_index() == clean
    assert "metadata" in tools.harness_read(record["id"])
    assert MARKER.lower() not in tools.harness_search("openclaw").lower()
    assert MARKER.lower() not in harness._record_text_for_embed(clean["records"][0]).lower()


@pytest.mark.parametrize("name", [".CODEX-PLUGIN/plugin.json", ".Codex-Plugin/PLUGIN.JSON"])
def test_fresh_mixed_case_plugin_body_is_metadata_only(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir()
    path.write_text(json.dumps({"configuration": {"env": {"CUSTOM": MARKER}}}))
    root = harness.SourceRoot("codex", "plugin", tmp_path, ("*.json",))
    assert MARKER.lower() not in json.dumps(harness.make_record(root, path)).lower()


def test_native_document_fields_use_existing_credential_redaction(tmp_path):
    record = legacy_record(tmp_path / "guide.md")
    record["summary"] = "access_token = synthetic-token-value"
    record["search_text"] = "authorization: bearer synthetic-bearer-value"
    clean = harness._normalize_index_records({"records": [record]})
    serialized = json.dumps(clean)
    assert "synthetic-token-value" not in serialized and "synthetic-bearer-value" not in serialized
    assert "embedding" not in clean["records"][0]


def test_sanitized_native_embeddings_survive_cache_and_restart(monkeypatch, tmp_path):
    canonical = tmp_path / "harness_index.json"
    canonical.write_text(json.dumps({"records": [legacy_record(tmp_path / "openclaw.json")]}))
    monkeypatch.setattr(harness, "INDEX_PATH", canonical)
    monkeypatch.setattr(harness, "index_is_stale", lambda **_: False)
    harness._set_index_cache(None)
    calls = []

    def embed(texts):
        calls.extend(texts)
        assert MARKER.lower() not in json.dumps(texts).lower()
        return [[1.0, 0.0] for _ in texts]

    first = harness.embed_index_records(embed, "fixture", dimensions=2)
    assert first["embedded"] == 1 and first["ready"] is True
    assert harness.load_index()["records"][0]["embedding"] == [1.0, 0.0]
    harness._set_index_cache(None)
    assert harness.load_index()["records"][0]["embedding"] == [1.0, 0.0]
    second = harness.embed_index_records(embed, "fixture", dimensions=2)
    assert second["embedded"] == 0 and second["ready"] is True
    assert len(calls) == 1


def test_fallback_marker_does_not_preserve_tampered_fields_or_vectors(tmp_path):
    clean = harness._normalize_record_privacy(legacy_record(tmp_path / "openclaw.json"))
    clean.update(embedding=[1.0], extra_body=MARKER)
    normalized = harness._normalize_record_privacy(clean)
    assert MARKER not in json.dumps(normalized)
    assert "embedding" not in normalized
