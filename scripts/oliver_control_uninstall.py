#!/usr/bin/env python3
"""Source-checkout wrapper for the installed Oliver uninstall command."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algo_cli.oliver_control_uninstall import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
