from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from algo_cli import harness


@pytest.fixture
def indexed_root(monkeypatch, tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    source = root / "note.md"
    source.write_text("# Indexed root canary\n")
    spec = harness.SourceRoot("fixture", "wiki", root, ("*.md",), 10)
    index_path = tmp_path / "index.json"
    monkeypatch.setattr(harness, "INDEX_PATH", index_path)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (spec,))
    harness._set_index_cache(None)
    index = harness.build_index()
    index_path.write_text(json.dumps(index))
    yield root, spec, index
    harness._set_index_cache(None)


@pytest.mark.parametrize("change", ["delete", "rename"])
def test_whole_root_disappearance_invalidates_and_removes_records(indexed_root, change):
    root, _, index = indexed_root
    assert not harness.index_is_stale(allow_cached=False)
    if change == "delete":
        shutil.rmtree(root)
    else:
        root.rename(root.with_name("moved"))
    assert harness.index_is_stale(allow_cached=False)
    fresh = harness.load_index()
    assert not fresh["records"]
    assert fresh["refresh_stats"]["removed_records"] == index["record_count"]


def test_known_root_removal_from_configuration_does_not_preserve_old_records(indexed_root, monkeypatch):
    _, _, index = indexed_root
    monkeypatch.setattr(harness, "all_source_roots", lambda: ())
    assert harness.index_is_stale(allow_cached=False)
    fresh = harness.load_index()
    assert fresh["records"] == []
    assert fresh["refresh_stats"]["removed_records"] == index["record_count"]


def test_root_pattern_change_invalidates_same_directory(indexed_root, monkeypatch):
    root, _, _ = indexed_root
    monkeypatch.setattr(harness, "all_source_roots", lambda: (
        harness.SourceRoot("fixture", "wiki", root, ("*.txt",), 10),))
    assert harness.index_is_stale(allow_cached=False)
    assert harness.load_index()["records"] == []


def test_direct_lookup_checks_freshness_and_binds_to_returned_index(monkeypatch):
    snapshots = [{"records": [{"id": "old", "title": "first"}]},
                 {"records": [{"id": "new", "title": "second"}]}]
    calls = []

    def load():
        index = snapshots[min(len(calls), 1)]
        calls.append(True)
        return index

    harness._set_index_cache(None)
    monkeypatch.setattr(harness, "load_index", load)
    assert harness.get_record("old")["title"] == "first"
    assert harness.get_record("new")["title"] == "second"
    assert harness.get_record("old") is None
    assert len(calls) == 3


def test_context_reconfiguration_clears_lookup_snapshot(monkeypatch):
    index = {"records": [{"id": "old"}]}
    monkeypatch.setattr(harness, "load_index", lambda: index)
    monkeypatch.setattr(harness, "built_in_source_roots", lambda **kwargs: ())
    assert harness.get_record("old") is not None
    assert harness._ID_LOOKUP_SOURCE is index
    harness.configure_context_sources(external=False, index_compute_lab=False)
    assert harness._ID_LOOKUP is None and harness._ID_LOOKUP_SOURCE is None


def test_concurrent_lookup_returns_its_own_snapshot(monkeypatch):
    barrier = threading.Barrier(2)

    def load():
        identity = threading.current_thread().name
        snapshot = {"records": [{"id": "shared", "title": identity}]}
        barrier.wait(timeout=2)
        return snapshot

    harness._set_index_cache(None)
    monkeypatch.setattr(harness, "load_index", load)

    def get():
        expected = threading.current_thread().name
        assert harness.get_record("shared")["title"] == expected

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(get) for _ in range(2)]
        for future in futures:
            future.result(timeout=3)


@pytest.mark.parametrize("magnitude", [1e-300, 1e-30, 1e30, 1e300])
def test_historical_numpy_normalization_preserves_finite_extreme_vectors(magnitude):
    if harness._np is None:
        pytest.skip("Optional historical NumPy helper unavailable")
    candidates = [{"id": "aligned", "embedding": [magnitude, 0.0]},
                  {"id": "orthogonal", "embedding": [0.0, magnitude]}]
    harness._VECTOR_MATRIX_CACHE.clear()
    with harness._np.errstate(over="raise", under="ignore", invalid="raise", divide="raise"):
        rows, matrix = harness._normalized_candidate_matrix(
            candidates, model="historical-fixture", dimensions=2, harness_names=None,
            kind=None, excluded_kinds=frozenset())
    assert rows == candidates
    assert matrix.dtype == harness._np.float32
    assert harness._np.allclose(matrix, [[1, 0], [0, 1]])
