"""Emit a completion receipt only after a benchmark checker returns normally.

This observer detects missing/partial execution. It is not an isolation or
attestation boundary against code that compromises its own Python process.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import sys
from typing import Any


SCHEMA = "algo-cli-checker-completion-v1"


class PytestCompletion:
    def __init__(self) -> None:
        self.collected: list[str] = []
        self.reports: list[dict[str, Any]] = []
        self.exit_code: int | None = None

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report: Any) -> None:
        self.reports.append(
            {
                "nodeid": report.nodeid,
                "phase": report.when,
                "outcome": report.outcome,
                "xfail": hasattr(report, "wasxfail"),
            }
        )

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        self.exit_code = int(exitstatus)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("kind", choices=("pytest", "script"))
    parser.add_argument("target")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    os.chdir(workspace)
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "nonce": args.nonce,
        "kind": args.kind,
        "completed": True,
        "exit_code": 0,
        "collected": [],
        "reports": [],
    }
    if args.kind == "pytest":
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        import pytest

        observer = PytestCompletion()
        sys.path.insert(0, str(workspace / "src"))
        code = int(
            pytest.main(
                [
                    "-q",
                    "--noconftest",
                    f"--rootdir={workspace}",
                    "-o",
                    "addopts=",
                    "-o",
                    "pythonpath=",
                    args.target,
                ],
                plugins=[observer],
            )
        )
        receipt.update(
            exit_code=code, collected=observer.collected, reports=observer.reports, completed=observer.exit_code == code
        )
    else:
        # A SystemExit, including SystemExit(0), is not normal checker completion.
        try:
            runpy.run_path(str(workspace / args.target), run_name="__main__")
            code = 0
        except AssertionError:
            code = 1
        except KeyError as exc:
            if (
                args.target != "healthcheck.py"
                or len(exc.args) != 1
                or not str(exc.args[0]).startswith("Missing route: ")
            ):
                raise
            print(f"FAIL {exc}")
            code = 1
        except SystemExit as exc:
            if not isinstance(exc.code, str) or not exc.code.startswith("FAIL "):
                raise
            print(exc.code)
            code = 1
        receipt["exit_code"] = code
    with args.receipt.open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle, sort_keys=True, allow_nan=False)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
