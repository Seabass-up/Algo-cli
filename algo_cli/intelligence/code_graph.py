"""B77. AST-Based Code Knowledge Graph.

Build a queryable graph from AST: CONTAINS, CALLS, IMPORTS, INHERITS.
Source: PyCodeKG pattern.
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path


class NodeKind(Enum):
    MODULE = auto()
    CLASS = auto()
    FUNCTION = auto()
    METHOD = auto()


class EdgeKind(Enum):
    CONTAINS = auto()
    CALLS = auto()
    IMPORTS = auto()
    INHERITS = auto()


@dataclass
class CodeNode:
    id: str
    kind: NodeKind
    name: str
    qualified_name: str
    file: str
    line: int
    signature: str = ""
    docstring: str = ""


@dataclass
class CodeEdge:
    source: str
    target: str
    kind: EdgeKind


class CodeGraph:
    """Queryable AST-derived graph of codebase structure."""

    def __init__(self) -> None:
        self._nodes: dict[str, CodeNode] = {}
        self._edges: list[CodeEdge] = []
        self._by_name: dict[str, list[str]] = {}  # name → node IDs

    def build(self, root: Path, pattern: str = "*.py") -> int:
        """Build graph from Python files. Returns node count."""
        for py_file in root.rglob(pattern):
            try:
                source = py_file.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(py_file))
                self._process_module(tree, str(py_file), str(root))
            except Exception:
                continue
        return len(self._nodes)

    def _process_module(self, tree: ast.Module, filepath: str, root: str) -> None:
        module_id = self._node_id(filepath, "module", 0)
        module_node = CodeNode(
            id=module_id, kind=NodeKind.MODULE, name=Path(filepath).stem,
            qualified_name=Path(filepath).relative_to(root).as_posix() if filepath.startswith(root) else filepath,
            file=filepath, line=0,
        )
        self._add_node(module_node)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                cls_id = self._node_id(filepath, node.name, node.lineno)
                cls_node = CodeNode(
                    id=cls_id, kind=NodeKind.CLASS, name=node.name,
                    qualified_name=f"{module_node.name}.{node.name}",
                    file=filepath, line=node.lineno,
                    docstring=ast.get_docstring(node) or "",
                )
                self._add_node(cls_node)
                self._edges.append(CodeEdge(source=module_id, target=cls_id, kind=EdgeKind.CONTAINS))

                # Inheritance
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        self._edges.append(CodeEdge(
                            source=cls_id, target=base.id, kind=EdgeKind.INHERITS
                        ))

                # Methods
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_id = self._node_id(filepath, f"{node.name}.{item.name}", item.lineno)
                        method_node = CodeNode(
                            id=method_id, kind=NodeKind.METHOD, name=item.name,
                            qualified_name=f"{module_node.name}.{node.name}.{item.name}",
                            file=filepath, line=item.lineno,
                            signature=self._signature(item),
                            docstring=ast.get_docstring(item) or "",
                        )
                        self._add_node(method_node)
                        self._edges.append(CodeEdge(source=cls_id, target=method_id, kind=EdgeKind.CONTAINS))
                        self._process_calls(method_id, item)

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_id = self._node_id(filepath, node.name, node.lineno)
                func_node = CodeNode(
                    id=func_id, kind=NodeKind.FUNCTION, name=node.name,
                    qualified_name=f"{module_node.name}.{node.name}",
                    file=filepath, line=node.lineno,
                    signature=self._signature(node),
                    docstring=ast.get_docstring(node) or "",
                )
                self._add_node(func_node)
                self._edges.append(CodeEdge(source=module_id, target=func_id, kind=EdgeKind.CONTAINS))
                self._process_calls(func_id, node)

            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self._process_imports(module_id, node)

        return None

    def _process_calls(
        self,
        source_id: str,
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    self._edges.append(CodeEdge(source=source_id, target=node.func.id, kind=EdgeKind.CALLS))
                elif isinstance(node.func, ast.Attribute):
                    self._edges.append(CodeEdge(source=source_id, target=node.func.attr, kind=EdgeKind.CALLS))

    def _process_imports(self, module_id: str, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                self._edges.append(CodeEdge(source=module_id, target=alias.name, kind=EdgeKind.IMPORTS))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                target = f"{module}.{alias.name}" if module else alias.name
                self._edges.append(CodeEdge(source=module_id, target=target, kind=EdgeKind.IMPORTS))

    def _signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        args = [a.arg for a in node.args.args]
        return f"({', '.join(args)})"

    def _node_id(self, filepath: str, name: str, line: int) -> str:
        return hashlib.md5(f"{filepath}:{name}:{line}".encode()).hexdigest()[:12]

    def _add_node(self, node: CodeNode) -> None:
        self._nodes[node.id] = node
        self._by_name.setdefault(node.name, []).append(node.id)

    def callers(self, name: str) -> list[CodeNode]:
        """Find all nodes that call the given function name."""
        caller_ids = {e.source for e in self._edges if e.kind == EdgeKind.CALLS and e.target == name}
        return [self._nodes[cid] for cid in caller_ids if cid in self._nodes]

    def callees(self, name: str) -> list[str]:
        """Find all functions called by the given function name."""
        return [e.target for e in self._edges if e.kind == EdgeKind.CALLS
                and any(self._nodes[nid].name == name for nid in [e.source] if nid in self._nodes)]

    def dead_code(self) -> list[CodeNode]:
        """Find functions/methods with zero callers."""
        called_names = {e.target for e in self._edges if e.kind == EdgeKind.CALLS}
        return [n for n in self._nodes.values()
                if n.kind in (NodeKind.FUNCTION, NodeKind.METHOD)
                and n.name not in called_names]

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)
