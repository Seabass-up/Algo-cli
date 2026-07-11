"""Tests for H1 — Algorithm Finding Record."""
from __future__ import annotations

from algo_cli.intelligence.finding_record import FindingRecord, FindingStatus


def test_create_finding() -> None:
    store = FindingRecord()
    f = store.create(
        id="H1",
        title="Test Finding",
        description="A test",
        source_repo="T3MP3ST",
        source_section="§8",
    )
    assert f.id == "H1"
    assert f.title == "Test Finding"
    assert f.status == FindingStatus.PROPOSED


def test_get_finding() -> None:
    store = FindingRecord()
    store.create("H1", "T", "D", "repo", "§1")
    f = store.get("H1")
    assert f is not None
    assert f.title == "T"


def test_get_missing_returns_none() -> None:
    store = FindingRecord()
    assert store.get("nope") is None


def test_duplicate_id_raises() -> None:
    store = FindingRecord()
    store.create("H1", "T", "D", "repo", "§1")
    try:
        store.create("H1", "T2", "D2", "repo", "§2")
        assert False, "Should raise"
    except ValueError:
        pass


def test_query_by_status() -> None:
    store = FindingRecord()
    store.create("H1", "T", "D", "repo", "§1")
    store.create("H2", "T2", "D2", "repo", "§2")
    store.update_status("H1", FindingStatus.VALIDATED)
    validated = store.query(status=FindingStatus.VALIDATED)
    assert len(validated) == 1
    assert validated[0].id == "H1"


def test_query_by_repo() -> None:
    store = FindingRecord()
    store.create("H1", "T", "D", "repo1", "§1")
    store.create("H2", "T2", "D2", "repo2", "§2")
    results = store.query(source_repo="repo1")
    assert len(results) == 1
    assert results[0].id == "H1"


def test_update_status() -> None:
    store = FindingRecord()
    store.create("H1", "T", "D", "repo", "§1")
    store.update_status("H1", FindingStatus.RETRACTED)
    assert store.get("H1").status == FindingStatus.RETRACTED


def test_update_missing_raises() -> None:
    store = FindingRecord()
    try:
        store.update_status("nope", FindingStatus.VALIDATED)
        assert False
    except KeyError:
        pass


def test_count() -> None:
    store = FindingRecord()
    assert store.count() == 0
    store.create("H1", "T", "D", "repo", "§1")
    assert store.count() == 1


def test_to_dict_roundtrip() -> None:
    store = FindingRecord()
    f = store.create("H1", "T", "D", "repo", "§1", provenance={"k": "v"}, evidence=["e1"])
    d = f.to_dict()
    assert d["id"] == "H1"
    assert d["provenance"] == {"k": "v"}
    assert d["evidence"] == ["e1"]