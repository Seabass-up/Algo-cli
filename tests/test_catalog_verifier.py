"""Tests for H2 — Algorithm Catalog Verifier."""

from __future__ import annotations

from pathlib import Path

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

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ALGO_CATALOG = REPOSITORY_ROOT / "docs" / "ALGO.md"


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


def test_parse_catalog_is_fence_aware_and_supports_namespaced_and_suffixed_ids() -> None:
    markdown = """# Catalog

## Track A

```markdown
### B1. Example inside a fence
```

### A12a. Suffixed entry
Status: proposed

## Track M

### DM1. Namespaced entry
Status: implemented
"""

    entries = CatalogVerifier().parse_catalog(markdown)

    assert [entry.id for entry in entries] == ["A12a", "DM1"]
    assert entries[0].section == "Track A"
    assert entries[1].section == "Track M"


def test_lint_catalog_rejects_duplicate_ids() -> None:
    markdown = """## Track B
**Pattern namespace:** `B`

### B1. First

### B1. Duplicate
"""

    report = CatalogVerifier().lint_catalog(markdown)

    assert report.all_valid is False
    duplicate = next(diagnostic for diagnostic in report.diagnostics if diagnostic.code == "duplicate_id")
    assert duplicate.entry_id == "B1"
    assert duplicate.line_number == 6
    assert "line 4" in duplicate.message


def test_lint_catalog_rejects_unclosed_fence() -> None:
    report = CatalogVerifier().lint_catalog("""## Track A
**Pattern namespace:** `A`

```text
### A1. Hidden by an unclosed fence
""")

    assert report.all_valid is False
    assert [(diagnostic.code, diagnostic.line_number) for diagnostic in report.diagnostics] == [("unclosed_fence", 4)]


def test_lint_catalog_rejects_malformed_pattern_heading() -> None:
    report = CatalogVerifier().lint_catalog("""## Track A
**Pattern namespace:** `A`

### A1 Missing separator
""")

    assert report.all_valid is False
    assert [(diagnostic.code, diagnostic.line_number) for diagnostic in report.diagnostics] == [
        ("malformed_pattern_heading", 4)
    ]


def test_lint_catalog_rejects_declared_namespace_mismatch() -> None:
    report = CatalogVerifier().lint_catalog("""## Track QoL
**Pattern namespace:** `Q`

### A3. Wrong track
""")

    assert report.all_valid is False
    mismatch = next(diagnostic for diagnostic in report.diagnostics if diagnostic.code == "namespace_mismatch")
    assert mismatch.entry_id == "A3"
    assert mismatch.line_number == 4
    assert "Q" in mismatch.message


def test_repository_catalog_structure_is_valid() -> None:
    markdown = ALGO_CATALOG.read_text(encoding="utf-8")

    report = CatalogVerifier().lint_catalog(markdown)

    assert report.all_valid, "\n".join(diagnostic.format() for diagnostic in report.diagnostics)
    entries = CatalogVerifier().parse_catalog(markdown)
    by_id = {entry.id: entry for entry in entries}
    assert {f"Q{number}" for number in range(1, 14)} <= set(by_id)
    assert by_id["A3"].section.startswith("Track A")
    assert {"DM1", "DM2"} <= set(by_id)
    assert "M2" not in by_id
    assert "M5" not in by_id


def test_repository_track_n_is_a_planned_anomaly_signal_contract() -> None:
    markdown = ALGO_CATALOG.read_text(encoding="utf-8")
    track_n = markdown.split("## Track N —", maxsplit=1)[1]

    assert track_n.startswith(" Black-Box Behavioral Anomaly Signals")
    assert track_n.count("**Evidence state:** `planned`") == 4
    for required in (
        "`not_flagged`",
        "no anomaly was observed",
        "prompt/probe-conditioned",
        "sample standard deviation",
        "independently labeled",
        "Hedges' g",
        "bootstrap",
        "retrieved 2026-08-18",
    ):
        assert required in track_n
    for forbidden in (
        "Catches backdoors",
        "second, independent signal",
        "model is clean",
        "zero citations",
        "separation confidence",
    ):
        assert forbidden not in track_n


def test_repository_catalog_documents_reserved_ranges() -> None:
    markdown = ALGO_CATALOG.read_text(encoding="utf-8")

    assert "Reserved IDs `B215-B299`" in markdown
    assert "Reserved IDs `B432-B437`" in markdown


def test_repository_catalog_documents_the_blocking_structural_gate() -> None:
    markdown = ALGO_CATALOG.read_text(encoding="utf-8")
    h2 = markdown.split("### H2. Algorithm Catalog Verifier", maxsplit=1)[1].split("### H3.", maxsplit=1)[0]

    for required in (
        "fence-aware",
        "duplicate IDs",
        "malformed pattern headings",
        "declared pattern namespaces",
        "repository-exact CI test",
    ):
        assert required in h2
