#!/usr/bin/env python3
"""Refresh or verify the source-bound prompt-capsule qualification."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algo_cli.evals import nathan_prompt_capsule_qualification as qualification  # noqa: E402


HARDENING_ROOT = ROOT / "hardening"
DEFAULT_ARTIFACT = HARDENING_ROOT / "nathan-prompt-capsule-qualification.json"


class PromptCapsuleArtifactError(RuntimeError):
    """Raised when the qualification artifact crosses its boundary."""


def _bounded_path(path: Path, *, require_exists: bool) -> Path:
    try:
        root = HARDENING_ROOT.resolve(strict=True)
        candidate = (path if path.is_absolute() else ROOT / path).absolute()
        parent = candidate.parent.resolve(strict=True)
        parent.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PromptCapsuleArtifactError("qualification artifact is outside hardening") from exc
    if candidate.parent != parent:
        raise PromptCapsuleArtifactError("qualification artifact parent contains a link")
    if require_exists:
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise PromptCapsuleArtifactError("qualification artifact is unavailable") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or not 1 <= info.st_size <= qualification.MAX_REPORT_BYTES
        ):
            raise PromptCapsuleArtifactError("qualification artifact boundary rejected the file")
    elif candidate.exists() or candidate.is_symlink():
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
            raise PromptCapsuleArtifactError("qualification output cannot replace this target")
    return candidate


def verify_artifact(path: Path = DEFAULT_ARTIFACT) -> dict[str, Any]:
    candidate = _bounded_path(path, require_exists=True)
    try:
        report = json.loads(candidate.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromptCapsuleArtifactError("qualification artifact is not valid JSON") from exc
    try:
        qualification.validate_report(report, require_current_source=True)
    except qualification.PromptCapsuleQualificationError as exc:
        raise PromptCapsuleArtifactError(str(exc)) from exc
    if report["status"] != "pass":
        raise PromptCapsuleArtifactError("qualification artifact does not pass")
    return report


def write_artifact(path: Path, report: dict[str, Any]) -> Path:
    qualification.validate_report(report, require_current_source=True)
    candidate = _bounded_path(path, require_exists=False)
    payload = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True).encode("ascii") + b"\n"
    if len(payload) > qualification.MAX_REPORT_BYTES:
        raise PromptCapsuleArtifactError("qualification artifact exceeds its size bound")
    temporary: Path | None = None
    try:
        descriptor, raw = tempfile.mkstemp(prefix=f".{candidate.name}.", suffix=".tmp", dir=candidate.parent)
        temporary = Path(raw)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, candidate)
        temporary = None
        if os.name == "posix":
            directory = os.open(candidate.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        raise PromptCapsuleArtifactError("qualification artifact could not be written atomically") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return candidate


def refresh_artifact(
    path: Path = DEFAULT_ARTIFACT,
    *,
    repetitions: int = qualification.DEFAULT_REPETITIONS,
) -> dict[str, Any]:
    report = qualification.run_qualification(repetitions=repetitions)
    write_artifact(path, report)
    return verify_artifact(path)


def _receipt(report: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "artifact": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name,
        "benchmark": report["benchmark"],
        "status": report["status"],
        "source_revision": report["source_revision"],
        "source_tree_sha256": report["source_tree_sha256"],
        "registry_sha256": report["registry_sha256"],
        "report_sha256": report["report_sha256"],
        "gates": report["gates"],
        "public_claim_eligible": report["public_claim_eligible"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--repetitions", type=int, default=qualification.DEFAULT_REPETITIONS)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        report = (
            refresh_artifact(arguments.artifact, repetitions=arguments.repetitions)
            if arguments.refresh
            else verify_artifact(arguments.artifact)
        )
    except (PromptCapsuleArtifactError, qualification.PromptCapsuleQualificationError) as exc:
        if not arguments.quiet:
            print(json.dumps({"status": "failed", "reason": str(exc)}, sort_keys=True))
        return 2
    if not arguments.quiet:
        print(json.dumps(_receipt(report, arguments.artifact), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
