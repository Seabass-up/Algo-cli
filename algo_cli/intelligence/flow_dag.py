"""B35. Declarative LLM Flow DAG + Evaluation Harness (PromptFlow Pattern).

Parses YAML-like flow definitions into a DAG of nodes, executes them in
topological order, and runs evaluation checks against the outputs.
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
        for val in n.inputs.values():
            if isinstance(val, str) and "${" in val:
                ref = _extract_ref(val)
                if ref and ref in node_ids:
                    adj[ref].append(n.id)
                    in_deg[n.id] += 1
    return adj, in_deg


def _extract_ref(expr: str) -> str | None:
    """Extract node id from ${node_id.field} or ${node_id}."""
    if "${" not in expr:
        return None
    start = expr.index("${") + 2
    end = expr.index("}", start)
    ref = expr[start:end]
    # strip field accessor
    return ref.split(".")[0]


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


def _resolve_value(expr: Any, context: dict[str, Any]) -> Any:
    """Resolve ${node.field} references from context."""
    if not isinstance(expr, str) or "${" not in expr:
        return expr
    start = expr.index("${") + 2
    end = expr.index("}", start)
    ref = expr[start:end]
    parts = ref.split(".")
    val = context
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            val = getattr(val, p, None)
    # replace in string
    return expr.replace(f"${{{ref}}}", str(val)) if isinstance(val, (str, int, float)) else val


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
                    output = ""
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
        success = all(eval_results.values()) if eval_results else True
        return FlowResult(outputs=outputs, traces=traces, eval_results=eval_results, success=success)

    @staticmethod
    def _run_python(source: str, inputs: dict) -> str:
        local_ns = dict(inputs)
        exec(source, {}, local_ns)
        return str(local_ns.get("result", ""))