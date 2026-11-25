"""B35. Declarative LLM Flow DAG + Evaluation Harness (PromptFlow Pattern).

Parses YAML-like flow definitions into a DAG of nodes, executes them in
topological order, and runs evaluation checks against the outputs.

Trusted-local library only: Python nodes execute with this process's privileges,
not in a sandbox. Callers must establish source trust and action authorization
before running a flow. Do not pass model-generated or untrusted flow definitions
to this executor. This library is not the agent runtime's authorized tool layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from collections import defaultdict


class FlowError(Exception):
    pass


@dataclass
class FlowNode:
    id: str
    kind: str  # "tool", "llm", "python"
    tool: str | None = None
    llm: str | None = None
    prompt: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    source_code: str | None = None


@dataclass
class FlowEval:
    name: str
    kind: str  # "assert_contains", "assert_not_empty", "custom"
    expected: Any = None
    check_fn: Callable[[str], bool] | None = None


@dataclass
class FlowDefinition:
    name: str
    inputs: dict[str, Any] = field(default_factory=dict)
    nodes: list[FlowNode] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    evals: list[FlowEval] = field(default_factory=list)


@dataclass
class FlowTrace:
    node_id: str
    inputs: dict[str, Any]
    output: str
    duration_ms: float = 0.0
    error: str | None = None


@dataclass
class FlowResult:
    outputs: dict[str, Any]
    traces: list[FlowTrace] = field(default_factory=list)
    eval_results: dict[str, bool] = field(default_factory=dict)
    success: bool = True


# ── parser ────────────────────────────────────────────────────────────


def parse_flow(data: dict) -> FlowDefinition:
    """Parse a dict (from YAML/JSON) into a FlowDefinition."""
    name = data.get("name", "unnamed")
    inputs = data.get("inputs", {})
    nodes = []
    for nd in data.get("nodes", []):
        nodes.append(FlowNode(
            id=nd["id"],
            kind=nd.get("kind", "tool"),
            tool=nd.get("tool"),
            llm=nd.get("llm"),
            prompt=nd.get("prompt"),
            inputs=nd.get("inputs", {}),
            source_code=nd.get("source"),
        ))
    outputs = data.get("outputs", {})
    evals = []
    for ev in data.get("evals", []):
        evals.append(FlowEval(
            name=ev["name"],
            kind=ev.get("kind", "assert_contains"),
            expected=ev.get("expected"),
            check_fn=ev.get("check_fn"),
        ))
    return FlowDefinition(name=name, inputs=inputs, nodes=nodes, outputs=outputs, evals=evals)


# ── DAG validation ────────────────────────────────────────────────────


def _build_adjacency(flow: FlowDefinition) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Build adjacency list and in-degree map from ${node.output} references."""
    adj: dict[str, list[str]] = defaultdict(list)
    in_deg: dict[str, int] = defaultdict(int)
    node_ids = {n.id for n in flow.nodes}
    for n in flow.nodes:
        in_deg.setdefault(n.id, 0)
        seen_deps: set[str] = set()
        for val in n.inputs.values():
            if isinstance(val, str) and "${" in val:
                try:
                    refs = _extract_refs(val)
                except FlowError as e:
                    raise FlowError(f"node {n.id!r}: {e}") from None
                for ref in refs:
                    if ref and ref in node_ids and ref not in seen_deps:
                        seen_deps.add(ref)
                        adj[ref].append(n.id)
                        in_deg[n.id] += 1
    return adj, in_deg


def _extract_refs(expr: str) -> list[str]:
    """Extract all node ids from ${node_id.field} or ${node_id} references."""
    refs: list[str] = []
    pos = 0
    while True:
        idx = expr.find("${", pos)
        if idx == -1:
            break
        start = idx + 2
        end = expr.find("}", start)
        if end == -1:
            raise FlowError(f"unterminated reference {expr!r}: missing '}}'")
        ref = expr[start:end].split(".")[0]
        refs.append(ref)
        pos = end + 1
    return refs


def _extract_ref(expr: str) -> str | None:
    """Extract first node id from ${node_id.field} or ${node_id}."""
    refs = _extract_refs(expr)
    return refs[0] if refs else None


def detect_cycles(flow: FlowDefinition) -> list[str] | None:
    """Return cycle path if found, else None."""
    adj, in_deg = _build_adjacency(flow)
    # Kahn's algorithm
    queue = [nid for nid, d in in_deg.items() if d == 0]
    visited = 0
    while queue:
        nid = queue.pop(0)
        visited += 1
        for nxt in adj[nid]:
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                queue.append(nxt)
    if visited != len(in_deg):
        # find a cycle path via DFS
        return _find_cycle_dfs(adj, set(in_deg.keys()))
    return None


def _find_cycle_dfs(adj: dict[str, list[str]], nodes: set[str]) -> list[str]:
    visited: set[str] = set()
    stack: list[str] = []
    on_stack: set[str] = set()

    def dfs(u: str) -> list[str] | None:
        visited.add(u)
        stack.append(u)
        on_stack.add(u)
        for v in adj.get(u, []):
            if v not in visited:
                result = dfs(v)
                if result:
                    return result
            elif v in on_stack:
                idx = stack.index(v)
                return stack[idx:] + [v]
        stack.pop()
        on_stack.discard(u)
        return None

    for n in nodes:
        if n not in visited:
            result = dfs(n)
            if result:
                return result
    return []


def topological_sort(flow: FlowDefinition) -> list[str]:
    """Return node ids in execution order."""
    cycle = detect_cycles(flow)
    if cycle:
        raise FlowError(f"Cycle detected: {' -> '.join(cycle)}")
    adj, in_deg = _build_adjacency(flow)
    queue = sorted([nid for nid, d in in_deg.items() if d == 0])
    order: list[str] = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        nexts = sorted(adj[nid])
        for nxt in nexts:
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                queue.append(nxt)
    return order


# ── executor ──────────────────────────────────────────────────────────


def _lookup_ref(ref: str, context: dict[str, Any]) -> Any:
    val: Any = context
    for p in ref.split("."):
        if isinstance(val, dict):
            val = val.get(p)
        elif p.startswith("_"):
            # A reference must not walk object internals (``__class__`` and friends).
            return None
        else:
            val = getattr(val, p, None)
    return val


def _resolve_value(expr: Any, context: dict[str, Any]) -> Any:
    """Resolve all ${node.field} references from context."""
    if not isinstance(expr, str) or "${" not in expr:
        return expr
    tokens: list[tuple[int, int, str]] = []
    pos = 0
    while True:
        idx = expr.find("${", pos)
        if idx == -1:
            break
        end = expr.find("}", idx + 2)
        if end == -1:
            raise FlowError(f"unterminated reference {expr!r}: missing '}}'")
        tokens.append((idx, end + 1, expr[idx + 2 : end]))
        pos = end + 1
    if len(tokens) == 1 and tokens[0][0] == 0 and tokens[0][1] == len(expr):
        val = _lookup_ref(tokens[0][2], context)
        return str(val) if isinstance(val, (str, int, float)) else val
    out: list[str] = []
    cursor = 0
    for start, end, ref in tokens:
        out.append(expr[cursor:start])
        val = _lookup_ref(ref, context)
        out.append("" if val is None else str(val))
        cursor = end
    out.append(expr[cursor:])
    return "".join(out)


class FlowExecutor:
    """Executes a FlowDefinition with pluggable tool/llm handlers."""

    def __init__(
        self,
        tool_handler: Callable[[str, dict], str] | None = None,
        llm_handler: Callable[[str, str, dict], str] | None = None,
    ):
        self.tool_handler = tool_handler or (lambda tool, inputs: f"[tool:{tool}]")
        self.llm_handler = llm_handler or (lambda model, prompt, inputs: f"[llm:{model}]")

    def run(self, flow: FlowDefinition, inputs: dict[str, Any] | None = None) -> FlowResult:
        context: dict[str, Any] = dict(flow.inputs)
        if inputs:
            context.update(inputs)
        traces: list[FlowTrace] = []
        order = topological_sort(flow)
        node_map = {n.id: n for n in flow.nodes}
        for nid in order:
            node = node_map[nid]
            resolved = {k: _resolve_value(v, context) for k, v in node.inputs.items()}
            try:
                if node.kind == "tool":
                    output = self.tool_handler(node.tool or "", resolved)
                elif node.kind == "llm":
                    output = self.llm_handler(node.llm or "", node.prompt or "", resolved)
                elif node.kind == "python":
                    output = self._run_python(node.source_code or "", resolved)
                else:
                    raise FlowError(f"Unsupported flow node kind: {node.kind}")
                context[nid] = {**resolved, "text": output}
                traces.append(FlowTrace(node_id=nid, inputs=resolved, output=output))
            except Exception as e:
                context[nid] = {"text": "", "error": str(e)}
                traces.append(FlowTrace(node_id=nid, inputs=resolved, output="", error=str(e)))
        # resolve outputs
        outputs = {k: _resolve_value(v, context) for k, v in flow.outputs.items()}
        # run evals
        eval_results: dict[str, bool] = {}
        for ev in flow.evals:
            text = str(outputs.get(ev.name, ""))
            if ev.kind == "assert_contains":
                eval_results[ev.name] = str(ev.expected) in text
            elif ev.kind == "assert_not_empty":
                eval_results[ev.name] = bool(text.strip())
            elif ev.kind == "custom" and ev.check_fn:
                eval_results[ev.name] = ev.check_fn(text)
            else:
                eval_results[ev.name] = False
        success = all(trace.error is None for trace in traces) and all(eval_results.values())
        return FlowResult(outputs=outputs, traces=traces, eval_results=eval_results, success=success)

    _SAFE_BUILTINS: dict[str, Any] = {
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
        "enumerate": enumerate, "filter": filter, "float": float, "int": int,
        "len": len, "list": list, "map": map, "max": max, "min": min,
        "pow": pow, "range": range, "reversed": reversed, "round": round,
        "set": set, "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
        "zip": zip, "True": True, "False": False, "None": None,
        "isinstance": isinstance, "repr": repr,
        "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
        "KeyError": KeyError, "IndexError": IndexError, "RuntimeError": RuntimeError,
        "ArithmeticError": ArithmeticError, "ZeroDivisionError": ZeroDivisionError,
        "AssertionError": AssertionError,
    }
    # Generator, coroutine, frame, traceback and code attributes lead back to the real
    # builtins of the calling frame without any dunder name.
    _INTROSPECTION_ATTRIBUTES = frozenset(
        {
            "gi_frame", "gi_code", "gi_yieldfrom", "cr_frame", "cr_code", "cr_await",
            "ag_frame", "ag_code", "ag_await", "f_back", "f_builtins", "f_globals",
            "f_locals", "f_code", "tb_frame", "tb_next", "func_globals", "func_code",
        }
    )

    @classmethod
    def _run_python(cls, source: str, inputs: dict) -> str:
        # These checks reduce accidental misuse. They are not a sandbox: the module
        # docstring's trusted-local rule still applies to every flow definition.
        import ast
        tree = ast.parse(source, mode="exec")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                raise FlowError("imports are disallowed in python flow nodes")
            if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                raise FlowError("dunder attribute access is disallowed in python flow nodes")
            if isinstance(node, ast.Attribute) and node.attr in cls._INTROSPECTION_ATTRIBUTES:
                raise FlowError("frame introspection is disallowed in python flow nodes")
            if isinstance(node, ast.Name) and node.id.startswith("__"):
                raise FlowError("dunder name access is disallowed in python flow nodes")
        local_ns = dict(inputs)
        exec(compile(tree, "<flow_node>", "exec"), {"__builtins__": dict(cls._SAFE_BUILTINS)}, local_ns)
        return str(local_ns.get("result", ""))
