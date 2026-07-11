#!/usr/bin/env python3
"""Validate the installed wheel's built-in corpus under an isolated home."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

from algo_cli import harness


def main() -> int:
    harness.configure_context_sources(external=False, index_compute_lab=False)
    index = harness.load_index(refresh=True)
    records = [record for record in index.get("records", []) if isinstance(record, dict)]
    if not records:
        raise SystemExit("installed harness corpus is empty")

    relative_paths = {str(record.get("relative_path") or "") for record in records}
    if "ALGO.md" not in relative_paths:
        raise SystemExit("installed wheel is missing the reviewed algorithm catalog")
    missing_docs = set(harness.CURATED_PROJECT_MEMORY_DOCS) - relative_paths
    if missing_docs:
        raise SystemExit(f"installed wheel is missing curated memory docs: {sorted(missing_docs)}")

    quality = harness.stats().get("quality", {})
    missing_categories = quality.get("missing_product_memory_categories") or []
    if missing_categories:
        raise SystemExit(f"installed corpus is missing memory categories: {missing_categories}")
    if any(record.get("harness") != "algo-cli" for record in records):
        raise SystemExit("fresh install enrolled an external harness without consent")

    resource_root = Path(harness.PACKAGE_RESOURCE_DIR)
    if not resource_root.is_dir():
        raise SystemExit(f"installed package resource directory is missing: {resource_root}")
    print(f"Installed algo-cli {version('algo-cli')} corpus: {len(records)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
