"""Deterministic project graph construction for local coding work.

The first implementation is intentionally Python-AST based. It is syntax-aware,
offline, dependency-free, and leaves room for optional tree-sitter/LSP enrichers
without making those heavy dependencies mandatory.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SOURCE_SUFFIXES = frozenset({".py", ".pyi"})
SKIP_DIRS = frozenset({
    ".git",
    ".algo",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
})


@dataclass(frozen=True)
class FileNode:
    path: str
    language: str
    is_test: bool
    size: int
    mtime_ns: int
    recent_commits: int = 0
    recency_score: float = 0.0


@dataclass(frozen=True)
class SymbolNode:
    symbol_id: str
    name: str
    qualname: str
    kind: str
    file: str
    line: int
    end_line: int


@dataclass(frozen=True)
class ImportEdge:
    source_file: str
    module: str
    names: tuple[str, ...] = ()
    target_file: str | None = None


@dataclass(frozen=True)
class TestMapping:
    test_file: str
    source_file: str
    score: float
    reasons: tuple[str, ...]


@dataclass
class ProjectGraph:
    root: str
    built_at: float
    files: dict[str, FileNode] = field(default_factory=dict)
    symbols: dict[str, SymbolNode] = field(default_factory=dict)
    imports: list[ImportEdge] = field(default_factory=list)
    test_mappings: list[TestMapping] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "built_at": self.built_at,
            "files": {path: asdict(node) for path, node in self.files.items()},
            "symbols": {key: asdict(node) for key, node in self.symbols.items()},
            "imports": [asdict(edge) for edge in self.imports],
            "test_mappings": [asdict(mapping) for mapping in self.test_mappings],
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectGraph":
        graph = cls(root=str(data.get("root", "")), built_at=float(data.get("built_at", 0.0) or 0.0))
        graph.files = {
            str(path): FileNode(**node)
            for path, node in (data.get("files") or {}).items()
            if isinstance(node, dict)
        }
        graph.symbols = {
            str(key): SymbolNode(**node)
            for key, node in (data.get("symbols") or {}).items()
            if isinstance(node, dict)
        }
        graph.imports = [
            ImportEdge(
                source_file=str(edge.get("source_file", "")),
                module=str(edge.get("module", "")),
                names=tuple(str(name) for name in edge.get("names", ()) or ()),
                target_file=edge.get("target_file"),
            )
            for edge in data.get("imports", []) or []
            if isinstance(edge, dict)
        ]
        graph.test_mappings = [
            TestMapping(
                test_file=str(mapping.get("test_file", "")),
                source_file=str(mapping.get("source_file", "")),
                score=float(mapping.get("score", 0.0) or 0.0),
                reasons=tuple(str(reason) for reason in mapping.get("reasons", ()) or ()),
            )
            for mapping in data.get("test_mappings", []) or []
            if isinstance(mapping, dict)
        ]
        graph.diagnostics = [
            item for item in data.get("diagnostics", []) or [] if isinstance(item, dict)
        ]
        return graph


def index_path(root: Path) -> Path:
    return root / ".algo" / "index" / "project_graph.json"


def load_project_graph(root: Path) -> ProjectGraph | None:
    path = index_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return ProjectGraph.from_dict(data) if isinstance(data, dict) else None


def save_project_graph(graph: ProjectGraph) -> Path:
    path = index_path(Path(graph.root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


def _iter_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in names:
            path = Path(current) / name
            if path.suffix.lower() in SOURCE_SUFFIXES:
                files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().lower())


def _is_test_path(rel: str) -> bool:
    path = rel.replace("\\", "/")
    name = Path(path).name
    return path.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")


def _module_to_rel(module: str, source_files: set[str]) -> str | None:
    if not module:
        return None
    candidate = module.replace(".", "/") + ".py"
    if candidate in source_files:
        return candidate
    package_candidate = module.replace(".", "/") + "/__init__.py"
    return package_candidate if package_candidate in source_files else None


def _git_recent_counts(root: Path) -> dict[str, int]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "log", "--since=30 days ago", "--name-only", "--pretty=format:"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    counts: dict[str, int] = {}
    for line in (proc.stdout or "").splitlines():
        rel = line.strip().replace("\\", "/")
        if rel:
            counts[rel] = counts.get(rel, 0) + 1
    return counts


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scope: list[str] = []
        self.symbols: list[SymbolNode] = []
        self.imports: list[ImportEdge] = []

    def _add_symbol(self, node: ast.AST, name: str, kind: str) -> None:
        qualname = ".".join([*self.scope, name])
        symbol_id = f"{self.rel}::{qualname}"
        self.symbols.append(
            SymbolNode(
                symbol_id=symbol_id,
                name=name,
                qualname=qualname,
                kind=kind,
                file=self.rel,
                line=int(getattr(node, "lineno", 1)),
                end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_symbol(node, node.name, "class")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add_symbol(node, node.name, "function" if not self.scope else "method")
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)  # type: ignore[arg-type]

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(ImportEdge(self.rel, alias.name, (alias.asname or alias.name,)))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * int(node.level or 0) + (node.module or "")
        names = tuple(alias.asname or alias.name for alias in node.names)
        self.imports.append(ImportEdge(self.rel, module, names))


def _parse_python(path: Path, root: Path) -> tuple[list[SymbolNode], list[ImportEdge]]:
    rel = path.relative_to(root).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=rel)
    except (OSError, SyntaxError):
        return [], []
    visitor = _SymbolVisitor(rel)
    visitor.visit(tree)
    return visitor.symbols, visitor.imports


def _build_test_mappings(files: dict[str, FileNode], imports: list[ImportEdge]) -> list[TestMapping]:
    source_files = [path for path, node in files.items() if not node.is_test]
    test_files = [path for path, node in files.items() if node.is_test]
    imports_by_test: dict[str, set[str]] = {}
    for edge in imports:
        if edge.source_file in test_files:
            values = imports_by_test.setdefault(edge.source_file, set())
            values.add(edge.module.lstrip("."))
            values.update(edge.names)

    mappings: list[TestMapping] = []
    for test_file in test_files:
        test_stem = Path(test_file).stem.replace("test_", "").replace("_test", "")
        for source_file in source_files:
            reasons: list[str] = []
            source_stem = Path(source_file).stem
            score = 0.0
            if test_stem and test_stem == source_stem:
                score += 0.7
                reasons.append("filename maps test stem to source stem")
            source_module = source_file.removesuffix(".py").replace("/", ".")
            imported = imports_by_test.get(test_file, set())
            if source_module in imported or source_stem in imported:
                score += 0.4
                reasons.append("test imports source module or symbol")
            if source_stem in test_file:
                score += 0.2
                reasons.append("source name appears in test path")
            if score > 0.0:
                mappings.append(
                    TestMapping(
                        test_file=test_file,
                        source_file=source_file,
                        score=round(min(score, 1.0), 3),
                        reasons=tuple(reasons),
                    )
                )
    mappings.sort(key=lambda item: (-item.score, item.test_file, item.source_file))
    return mappings


def build_project_graph(root: Path | str, *, persist: bool = True) -> ProjectGraph:
    root_path = Path(root).resolve()
    recent_counts = _git_recent_counts(root_path)
    files: dict[str, FileNode] = {}
    symbols: dict[str, SymbolNode] = {}
    imports: list[ImportEdge] = []
    source_paths = _iter_source_files(root_path)
    source_rel = {path.relative_to(root_path).as_posix() for path in source_paths}

    max_recent = max(recent_counts.values(), default=0)
    for path in source_paths:
        rel = path.relative_to(root_path).as_posix()
        try:
            stat = path.stat()
        except OSError:
            continue
        recent = recent_counts.get(rel, 0)
        files[rel] = FileNode(
            path=rel,
            language="python",
            is_test=_is_test_path(rel),
            size=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
            recent_commits=recent,
            recency_score=round(recent / max_recent, 3) if max_recent else 0.0,
        )
        parsed_symbols, parsed_imports = _parse_python(path, root_path)
        for symbol in parsed_symbols:
            symbols[symbol.symbol_id] = symbol
        for edge in parsed_imports:
            target_file = _module_to_rel(edge.module.lstrip("."), source_rel)
            imports.append(
                ImportEdge(
                    source_file=edge.source_file,
                    module=edge.module,
                    names=edge.names,
                    target_file=target_file,
                )
            )

    graph = ProjectGraph(
        root=str(root_path),
        built_at=time.time(),
        files=files,
        symbols=symbols,
        imports=imports,
    )
    graph.test_mappings = _build_test_mappings(files, imports)
    if persist:
        save_project_graph(graph)
    return graph


def query_project_graph(graph: ProjectGraph, term: str, *, limit: int = 20) -> list[dict[str, Any]]:
    needle = (term or "").strip().lower()
    if not needle:
        return []
    rows: list[dict[str, Any]] = []
    for file in graph.files.values():
        if needle in file.path.lower():
            rows.append({"kind": "file", "id": file.path, "score": 1.0, "path": file.path})
    for symbol in graph.symbols.values():
        haystack = f"{symbol.name} {symbol.qualname} {symbol.file}".lower()
        if needle in haystack:
            rows.append({
                "kind": "symbol",
                "id": symbol.symbol_id,
                "score": 1.0 if needle in symbol.qualname.lower() else 0.75,
                "path": symbol.file,
                "line": symbol.line,
                "qualname": symbol.qualname,
            })
    for edge in graph.imports:
        if needle in edge.module.lower() or any(needle in name.lower() for name in edge.names):
            rows.append({
                "kind": "import",
                "id": f"{edge.source_file}->{edge.module}",
                "score": 0.65,
                "path": edge.source_file,
                "module": edge.module,
                "target_file": edge.target_file,
            })
    rows.sort(key=lambda row: (-float(row["score"]), str(row["kind"]), str(row["id"])))
    return rows[:limit]
