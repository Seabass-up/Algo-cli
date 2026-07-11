#!/usr/bin/env python3
"""Verify the single-sourced package version and an optional release tag."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]


def package_version() -> str:
    text = (ROOT / "algo_cli" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if match is None:
        raise RuntimeError("algo_cli.__version__ was not found")
    return match.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Expected release tag, for example v0.14.0")
    args = parser.parse_args(argv)
    version = package_version()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if 'dynamic = ["version"]' not in pyproject or 'path = "algo_cli/__init__.py"' not in pyproject:
        print("pyproject.toml does not use algo_cli.__version__ as its dynamic version", file=sys.stderr)
        return 1
    if args.tag and args.tag != f"v{version}":
        print(f"Release tag {args.tag!r} does not match package version v{version}", file=sys.stderr)
        return 1
    print(f"Release version: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
