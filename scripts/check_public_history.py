#!/usr/bin/env python3
"""Reject known private paths and author metadata anywhere in reachable Git history."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PATH_MARKERS = (
    "personal/",
    "x" + "ds",
    "char" + "lie",
    "lodge",
    "cloud" + "storage",
    "google" + "drive-",
)
PRIVATE_AUTHOR_MARKERS = ("sco" + "tt", "whit" + "lock")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def main() -> int:
    private_paths = 0
    private_authors = 0
    nonprivate_emails = 0
    for line in _git("rev-list", "--objects", "--all").splitlines():
        _object_id, _separator, path = line.partition(" ")
        lowered = path.casefold()
        if path and any(marker in lowered for marker in PRIVATE_PATH_MARKERS):
            private_paths += 1

    for line in _git("log", "--all", "--format=%H%x09%an%x09%ae").splitlines():
        _commit, _separator, identity = line.partition("\t")
        lowered = identity.casefold()
        email = identity.rsplit("\t", 1)[-1].strip().casefold()
        if any(marker in lowered for marker in PRIVATE_AUTHOR_MARKERS):
            private_authors += 1
        if email and not email.endswith("@users.noreply.github.com"):
            nonprivate_emails += 1

    findings = []
    if private_paths:
        findings.append(f"{private_paths} reachable objects use private paths")
    if private_authors:
        findings.append(f"{private_authors} commits contain private author names")
    if nonprivate_emails:
        findings.append(f"{nonprivate_emails} commits use non-private author email addresses")
    if findings:
        print("Public-history scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        print("Publish from a reviewed squashed/orphan history or a new public repository.", file=sys.stderr)
        return 1
    print("Public-history metadata scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
