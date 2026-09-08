"""Repository-owned evidence registry. Catalog Markdown cannot select commands."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import xml.etree.ElementTree as ET

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from ..config import _atomic_write_text


@dataclass(frozen=True)
class PatternSpec:
    entrypoints: tuple[tuple[str, str], ...]
    tests: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    activation: str = "operator_only"


REGISTRY = {
    "H2": PatternSpec(
        (("algo_cli/intelligence/catalog_verifier.py", "CatalogVerifier"),), ("tests/test_pattern_catalog_runtime.py",)
    ),
    "O4": PatternSpec(
        (
            ("algo_cli/evals/algorithm_effectiveness.py", "run_algorithm_effectiveness_probe"),
            ("algo_cli/evals/diagnostic_graph.py", "run_diagnostic_graph"),
        ),
        ("tests/test_algorithm_effectiveness.py", "tests/test_diagnostic_graph.py"),
    ),
    "O8": PatternSpec(
        (("algo_cli/pattern_catalog.py", "exclusion_reasons"),),
        (
            "tests/test_pattern_catalog_runtime.py",
            "tests/test_harness_selection.py",
            "tests/test_harness_operational_retrieval.py",
            "tests/test_harness_query_profiles.py",
            "tests/test_harness_slice_cache.py",
        ),
        ("H2",),
        "automatic_retrieval_filter",
    ),
    "O5": PatternSpec(
        (("algo_cli/harness.py", "_merge_pattern_records"), ("algo_cli/harness.py", "read_record")),
        ("tests/test_pattern_catalog_runtime.py", "tests/test_harness.py"),
        ("O8",),
        "automatic_on_index_refresh",
    ),
    "O6": PatternSpec((("algo_cli/evals/pattern_evidence.py", "verify_pattern"),), ("tests/test_pattern_evidence.py",)),
    "O7": PatternSpec(
        (("algo_cli/evals/pattern_comparison.py", "run_comparison"),),
        ("tests/test_pattern_comparison.py",),
        ("O5", "O6"),
    ),
}


def repository_root(path: Path | None = None) -> Path:
    candidates = [path] if path is not None else [Path(__file__).resolve().parents[2], Path.cwd(), *Path.cwd().parents]
    for candidate in candidates:
        root = candidate.resolve()
        try:
            manifest = tomllib.loads(_read_owned(root, "pyproject.toml").decode("utf-8"))
            if (
                manifest.get("project", {}).get("name") == "algo-cli-runtime"
                and (root / "tests").is_dir()
                and (root / "algo_cli/harness.py").is_file()
            ):
                return root
        except (OSError, ValueError):
            continue
    raise ValueError(
        "Repository tests unavailable; use --repo with a trusted Algo-cli checkout and a pytest-enabled Python."
    )


def dependency_closure(identity: str) -> tuple[str, ...]:
    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError("pattern_registry_cycle")
        if name in ordered:
            return
        if name not in REGISTRY:
            raise ValueError("pattern_not_registered")
        visiting.add(name)
        for dependency in REGISTRY[name].dependencies:
            visit(dependency)
        visiting.remove(name)
        ordered.append(name)

    visit(identity)
    return tuple(ordered)


def _read_owned(root: Path, relative: str) -> bytes:
    path = root / relative
    if not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError("pattern_source_unavailable")
    return path.read_bytes()


def test_selection(identity: str) -> list[str]:
    return sorted({test for name in dependency_closure(identity) for test in REGISTRY[name].tests})


def source_snapshot(root: Path, identity: str) -> dict[str, str]:
    """Conservative transitive invalidation, including imports outside declared edges."""
    paths = {str(path.relative_to(root).as_posix()) for path in (root / "algo_cli").rglob("*.py")}
    paths.update(str(path.relative_to(root).as_posix()) for path in (root / "tests").rglob("*.py"))
    paths.update({"docs/ALGO.md", "pyproject.toml", "uv.lock", *test_selection(identity)})
    if (root / "tests/conftest.py").exists():
        paths.add("tests/conftest.py")
    for name in dependency_closure(identity):
        paths.update(path for path, _ in REGISTRY[name].entrypoints)
    if len(paths) > 4096:
        raise ValueError("pattern_source_count")
    return {path: "sha256:" + hashlib.sha256(_read_owned(root, path)).hexdigest() for path in sorted(paths)}


def digest(payload: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )


def save_report(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    report = {**payload, "artifact_digest": digest(payload)}
    _atomic_write_text(path, json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return report


def load_report(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 2_000_000:
            return None
        report = json.loads(path.read_text(encoding="utf-8"))
        if type(report) is not dict:
            return None
        checksum = report.pop("artifact_digest", None)
        if checksum != digest(report):
            return None
        return report
    except (OSError, ValueError, TypeError):
        return None


def evidence_status(root: Path, identity: str, directory: Path) -> dict[str, Any]:
    if identity not in REGISTRY:
        return {
            "pattern_id": identity,
            "implemented": None,
            "enabled": None,
            "tested": False,
            "measured": False,
            "reason": "No repository-owned evidence registration; catalog status is not verification.",
        }
    spec = REGISTRY[identity]
    try:
        sources = source_snapshot(root, identity)
        implemented = all(
            any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol
                for node in ast.parse(_read_owned(root, path)).body
            )
            for path, symbol in spec.entrypoints
        )
    except (OSError, ValueError, SyntaxError):
        sources = None
        implemented = False
    report = load_report(directory / f"{identity}.tests.json")
    tested = bool(
        sources
        and report
        and report.get("sources") == sources
        and report.get("pattern_id") == identity
        and report.get("schema_version") == 1
        and report.get("tests") == test_selection(identity)
        and report.get("status") == "pass"
        and type(report.get("executed")) is int
        and report["executed"] > 0
        and report.get("source_stable") is True
    )
    comparison = load_report(directory / "comparison.json")
    measured = False
    if sources and comparison and identity in {"O5", "O8"}:
        from .pattern_comparison import complete_report

        try:
            measured = (
                type(comparison.get("schema_version")) is int
                and comparison["schema_version"] == 1
                and comparison.get("source_stable") is True
                and comparison.get("status") == "complete"
                and comparison.get("sources") == source_snapshot(root, "O7")
                and complete_report(comparison)
            )
        except (OSError, ValueError):
            measured = False
    return {
        "pattern_id": identity,
        "implemented": implemented,
        "implementation_basis": "registered top-level entrypoints present; not proof of successful execution",
        "enabled": None,
        "activation": spec.activation,
        "activation_note": "Invocation/session dependent; catalog text never enables a feature.",
        "tested": tested,
        "measured": measured,
        "dependencies": list(spec.dependencies),
        "entrypoints": [f"{path}:{symbol}" for path, symbol in spec.entrypoints],
        "tests": test_selection(identity),
        "source_digest": digest(sources) if sources else None,
        "test_timestamp": report.get("created_at") if tested and report else None,
        "measurement_timestamp": comparison.get("created_at") if measured and comparison else None,
        "evidence_scope": "Local unsigned evidence; checksum is not independent approval or M8 qualification.",
    }


def verify_pattern(root: Path, identity: str, directory: Path) -> dict[str, Any]:
    root = repository_root(root)
    sources = source_snapshot(root, identity)
    tests = test_selection(identity)
    # Only this code-owned registry constructs commands. No Markdown commands, shell, or arbitrary flags.
    with tempfile.TemporaryDirectory(prefix="algo-pattern-tests-") as temporary:
        junit = Path(temporary) / "results.xml"
        command = [sys.executable, "-m", "pytest", *tests, "-q", "-o", "addopts=", f"--junitxml={junit}"]
        status, executed, skipped, returncode = "error", 0, 0, None
        with tempfile.TemporaryFile() as output:
            try:
                completed = subprocess.run(
                    command, cwd=root, stdout=output, stderr=subprocess.STDOUT, timeout=180, check=False
                )
                returncode = completed.returncode
                if junit.is_file() and junit.stat().st_size <= 2_000_000:
                    tree = ET.parse(junit)
                    cases = tree.findall(".//testcase")
                    skipped = sum(case.find("skipped") is not None for case in cases)
                    executed = len(cases) - skipped
                    failures = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
                    status = "pass" if returncode == 0 and executed > 0 and not failures else "fail"
                elif returncode:
                    status = "fail"
            except (subprocess.TimeoutExpired, OSError, ET.ParseError):
                status = "error"
    try:
        stable = sources == source_snapshot(root, identity)
    except (OSError, ValueError):
        stable = False
    if not stable:
        status = "stale"
    return save_report(
        directory / f"{identity}.tests.json",
        {
            "schema_version": 1,
            "pattern_id": identity,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sources": sources,
            "source_stable": stable,
            "tests": tests,
            "status": status,
            "executed": executed,
            "skipped": skipped,
            "returncode": returncode,
            "runner": f"Python {sys.version.split()[0]}",
            "timeout_seconds": 180,
        },
    )
