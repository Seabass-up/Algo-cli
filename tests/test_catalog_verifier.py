"""Tests for H2 — Algorithm Catalog Verifier."""
from __future__ import annotations

from algo_cli.intelligence.catalog_verifier import CatalogVerifier


SAMPLE_MARKDOWN = """# ALGO.md

## Track H

### H1. Algorithm Finding Record
Status: implemented

### H2. Algorithm Catalog Verifier
Status: implemented

### H3. Retraction Ledger
Status: proposed
"""


def test_parse_catalog() -> None:
    verifier = CatalogVerifier()
    entries = verifier.parse_catalog(SAMPLE_MARKDOWN)
    assert len(entries) == 3
    assert entries[0].id == "H1"
    assert entries[0].title == "Algorithm Finding Record"
    assert entries[0].status == "implemented"


def test_parse_catalog_finds_status() -> None:
    verifier = CatalogVerifier()
    entries = verifier.parse_catalog(SAMPLE_MARKDOWN)
    h3 = next(e for e in entries if e.id == "H3")
    assert h3.status == "proposed"


def test_verify_all_pass() -> None:
    verifier = CatalogVerifier()
    entries = verifier.parse_catalog(SAMPLE_MARKDOWN)
    report = verifier.verify(entries, test_results={"H1": True, "H2": True})
    assert report.all_verified is True
    assert report.verified_count == 3  # H3 is "proposed" — auto-verified


def test_verify_fails_on_missing_tests() -> None:
    verifier = CatalogVerifier()
    entries = verifier.parse_catalog(SAMPLE_MARKDOWN)
    report = verifier.verify(entries, test_results={})
    assert report.failed_count == 2  # H1 and H2 claimed implemented but no tests
    assert report.all_verified is False


def test_verify_fails_on_test_failure() -> None:
    verifier = CatalogVerifier()
    entries = verifier.parse_catalog(SAMPLE_MARKDOWN)
    report = verifier.verify(entries, test_results={"H1": False, "H2": True})
    assert report.failed_count == 1
    assert report.all_verified is False


def test_verify_proposed_not_checked() -> None:
    verifier = CatalogVerifier()
    entries = verifier.parse_catalog(SAMPLE_MARKDOWN)
    report = verifier.verify(entries, test_results={"H1": True, "H2": True})
    h3_result = next(r for r in report.results if r.entry_id == "H3")
    assert h3_result.verified is True
    assert "no verification needed" in h3_result.reason


def test_report_to_dict() -> None:
    verifier = CatalogVerifier()
    entries = verifier.parse_catalog(SAMPLE_MARKDOWN)
    report = verifier.verify(entries, test_results={"H1": True, "H2": True})
    d = report.to_dict()
    assert d["total_entries"] == 3
    assert d["all_verified"] is True


def test_parse_empty_markdown() -> None:
    verifier = CatalogVerifier()
    entries = verifier.parse_catalog("")
    assert entries == []


def test_entry_to_dict() -> None:
    verifier = CatalogVerifier()
    entries = verifier.parse_catalog(SAMPLE_MARKDOWN)
    d = entries[0].to_dict()
    assert d["id"] == "H1"
    assert d["status"] == "implemented"