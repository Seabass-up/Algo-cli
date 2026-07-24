#!/usr/bin/env python3
"""Refresh or verify source-bound Algo Agent runtime qualification evidence."""

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

from algo_cli.evals import nathan_agent_runtime_hardening as benchmark  # noqa: E402


HARDENING_ROOT = ROOT / "hardening"
DEFAULT_ARTIFACT = (
    HARDENING_ROOT / "nathan-agent-runtime-qualification.json"
)


class AgentRuntimeQualificationError(RuntimeError):
    """Raised when qualification evidence is unavailable or unsafe."""


def _bounded_artifact(
    path: Path,
    *,
    allowed_root: Path = HARDENING_ROOT,
    require_exists: bool,
) -> Path:
    try:
        root = allowed_root.resolve(strict=True)
        candidate = (
            path if path.is_absolute() else ROOT / path
        ).absolute()
        parent = candidate.parent.resolve(strict=True)
        parent.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AgentRuntimeQualificationError(
            "qualification artifact path is outside its boundary"
        ) from exc
    if candidate.parent != parent:
        raise AgentRuntimeQualificationError(
            "qualification artifact parent contains a link"
        )
    if require_exists:
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise AgentRuntimeQualificationError(
                "qualification artifact is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or not 1 <= info.st_size <= benchmark.MAX_REPORT_BYTES
        ):
            raise AgentRuntimeQualificationError(
                "qualification artifact boundary rejected the file"
            )
    elif candidate.exists() or candidate.is_symlink():
        info = candidate.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
        ):
            raise AgentRuntimeQualificationError(
                "qualification output cannot replace this target"
            )
    return candidate


def verify_artifact(
    path: Path = DEFAULT_ARTIFACT,
    *,
    allowed_root: Path = HARDENING_ROOT,
) -> dict[str, Any]:
    candidate = _bounded_artifact(
        path,
        allowed_root=allowed_root,
        require_exists=True,
    )
    try:
        payload = candidate.read_bytes()
        report = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentRuntimeQualificationError(
            "qualification artifact is not valid JSON"
        ) from exc
    try:
        benchmark.validate_report(
            report,
            require_current_source=True,
        )
    except benchmark.AgentRuntimeBenchmarkError as exc:
        raise AgentRuntimeQualificationError(str(exc)) from exc
    if report["status"] != "pass":
        raise AgentRuntimeQualificationError(
            "qualification artifact does not pass"
        )
    return report


def _set_private_mode(descriptor: int, path: Path) -> None:
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        fchmod(descriptor, 0o600)
        return
    os.chmod(path, 0o600)


def _sync_parent_directory(path: Path) -> None:
    if os.name != "posix":
        return
    directory = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_artifact(
    path: Path,
    report: dict[str, Any],
    *,
    allowed_root: Path = HARDENING_ROOT,
) -> Path:
    try:
        benchmark.validate_report(
            report,
            require_current_source=True,
        )
    except benchmark.AgentRuntimeBenchmarkError as exc:
        raise AgentRuntimeQualificationError(str(exc)) from exc
    candidate = _bounded_artifact(
        path,
        allowed_root=allowed_root,
        require_exists=False,
    )
    payload = (
        json.dumps(
            report,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    if len(payload) > benchmark.MAX_REPORT_BYTES:
        raise AgentRuntimeQualificationError(
            "qualification artifact exceeds its size bound"
        )
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{candidate.name}.",
            suffix=".tmp",
            dir=candidate.parent,
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            _set_private_mode(handle.fileno(), temporary)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, candidate)
        temporary = None
        _sync_parent_directory(candidate.parent)
    except OSError as exc:
        raise AgentRuntimeQualificationError(
            "qualification artifact could not be stored atomically"
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return candidate


def refresh_artifact(
    path: Path = DEFAULT_ARTIFACT,
    *,
    contract_repetitions: int = 101,
    context_repetitions: int = 101,
    checkpoint_repetitions: int = 31,
    workload_repetitions: int = 31,
    warmups: int = 5,
    allowed_root: Path = HARDENING_ROOT,
) -> dict[str, Any]:
    report = benchmark.run_benchmark(
        contract_repetitions=contract_repetitions,
        context_repetitions=context_repetitions,
        checkpoint_repetitions=checkpoint_repetitions,
        workload_repetitions=workload_repetitions,
        warmups=warmups,
    )
    write_artifact(
        path,
        report,
        allowed_root=allowed_root,
    )
    return verify_artifact(
        path,
        allowed_root=allowed_root,
    )


def _receipt(
    report: dict[str, Any],
    *,
    artifact: Path,
) -> dict[str, Any]:
    try:
        artifact_label = str(artifact.resolve().relative_to(ROOT.resolve()))
    except (OSError, ValueError):
        artifact_label = artifact.name
    return {
        "benchmark": report["benchmark"],
        "status": report["status"],
        "artifact": artifact_label,
        "source_tree_sha256": report["source_tree_sha256"],
        "report_sha256": report["report_sha256"],
        "correctness": {
            "passed": report["correctness"]["passed"],
            "total": report["correctness"]["total"],
        },
        "effectiveness": {
            key: value
            for key, value in report["effectiveness"].items()
            if key != "workloads"
        },
        "p95_ms": {
            metric: row["p95_ms"]
            for metric, row in report["performance"].items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact",
        type=Path,
        default=DEFAULT_ARTIFACT,
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="run the benchmark and atomically replace the evidence artifact",
    )
    parser.add_argument("--contract-repetitions", type=int, default=101)
    parser.add_argument("--context-repetitions", type=int, default=101)
    parser.add_argument("--checkpoint-repetitions", type=int, default=31)
    parser.add_argument("--workload-repetitions", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        report = (
            refresh_artifact(
                arguments.artifact,
                contract_repetitions=arguments.contract_repetitions,
                context_repetitions=arguments.context_repetitions,
                checkpoint_repetitions=arguments.checkpoint_repetitions,
                workload_repetitions=arguments.workload_repetitions,
                warmups=arguments.warmups,
            )
            if arguments.refresh
            else verify_artifact(arguments.artifact)
        )
    except (
        AgentRuntimeQualificationError,
        benchmark.AgentRuntimeBenchmarkError,
    ) as exc:
        if not arguments.quiet:
            print(
                json.dumps(
                    {
                        "benchmark": benchmark.BENCHMARK_ID,
                        "status": "blocked",
                        "reason_code": type(exc).__name__,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        return 1
    if not arguments.quiet:
        print(
            json.dumps(
                _receipt(report, artifact=arguments.artifact),
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
