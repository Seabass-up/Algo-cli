"""Operator-only pattern inspection, trusted test execution, and local comparisons."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
from typing import Any

from . import harness
from .config import CONFIG_DIR
from .evals import pattern_evidence as evidence
from .pattern_catalog import exclusion_reasons


def run_command(argument: str, *, root: Path | None = None, directory: Path | None = None) -> dict[str, Any]:
    parts = shlex.split(argument) or ["status"]
    command = parts[0]
    if command == "explain" and len(parts) == 2:
        identity = parts[1].upper()
        index = harness.load_index()
        row = next((row for row in index.get("records", []) if row.get("pattern_id") == identity), None)
        if row is None:
            raise ValueError("Pattern not indexed; refresh the harness or check the ID.")
        reasons = exclusion_reasons(row, harness.runtime_pattern_context())
        return {
            "pattern_id": identity,
            "eligible": not reasons,
            "excluded_by": reasons,
            "applicability": row["applicability"],
            "declared": row["applicability_declared"],
            "source_digest": row["source_digest"],
            "status": row["status"],
            "authority": "Reference only. Fallback text is not automatically executed.",
        }
    root = evidence.repository_root(root)
    directory = directory or CONFIG_DIR / "pattern-evidence" / evidence.digest(str(root)).removeprefix("sha256:")[:16]
    if command == "status" and len(parts) <= 2:
        identities = [parts[1].upper()] if len(parts) == 2 else sorted(evidence.REGISTRY)
        return {
            "patterns": [evidence.evidence_status(root, identity, directory) for identity in identities],
            "evidence_directory": str(directory),
        }
    if command == "verify" and len(parts) == 2:
        return evidence.verify_pattern(root, parts[1].upper(), directory)
    if command == "compare" and len(parts) == 1:
        from .evals.pattern_comparison import run_comparison

        return run_comparison(root, directory)
    raise ValueError("Usage: /harness patterns [status [ID]|explain ID|verify ID|compare]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, help="Trusted checkout whose repository-owned tests may be executed")
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("command", nargs="*", default=["status"])
    args = parser.parse_args()
    try:
        result = run_command(shlex.join(args.command), root=args.repo, directory=args.output_directory)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Pattern command error: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 1 if result.get("status") in {"fail", "error", "stale"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
