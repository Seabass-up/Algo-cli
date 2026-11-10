from __future__ import annotations

from typing import Optional

import pytest

from algo_cli.intelligence.changelog import ChangelogManager
from algo_cli.intelligence.code_graph import CodeGraph, EdgeKind, NodeKind
from algo_cli.intelligence.flow_dag import FlowDefinition, FlowError, FlowExecutor, FlowNode, parse_flow
from algo_cli.intelligence.kernel_plugins import validate_structured_output


# D1: validate_structured_output fallback for non-pydantic classes

class _Generic:
    items: list[str]
    maybe: Optional[int]


class _StringAnnotated:
    __annotations__ = {"name": "str", "extra": "NoSuchTypeAnywhere"}


def test_d1_parameterized_generic_annotation_does_not_raise():
    result = validate_structured_output({"items": ["a"], "maybe": None}, _Generic)
    assert result == {"valid": True, "data": {"items": ["a"], "maybe": None}}


def test_d1_parameterized_generic_still_rejects_wrong_origin_type():
    result = validate_structured_output({"items": "abc", "maybe": 1}, _Generic)
    assert result["valid"] is False
    assert "items" in result["error"]


def test_d1_optional_accepts_union_members_and_none():
    assert validate_structured_output({"items": [], "maybe": 3}, _Generic)["valid"] is True
    assert validate_structured_output({"items": [], "maybe": "x"}, _Generic)["valid"] is False


def test_d1_string_annotations_are_resolved_or_skipped_without_raising():
    result = validate_structured_output({"name": "a", "extra": 1}, _StringAnnotated)
    assert result == {"valid": True, "data": {"name": "a", "extra": 1}}
    result = validate_structured_output({"name": 1, "extra": 1}, _StringAnnotated)
    assert result["valid"] is False
    assert "name" in result["error"]


def test_d1_missing_field_still_reported():
    result = validate_structured_output({}, _Generic)
    assert result["valid"] is False
    assert "missing field: items" in result["error"]


# D2: ChangelogManager.load_from_dir skips unreadable or non-object change files

def test_d2_bad_change_files_are_skipped_and_remaining_files_load(tmp_path):
    (tmp_path / "a_array.json").write_text("[1, 2]", encoding="utf-8")
    (tmp_path / "b_string.json").write_text('"just a string"', encoding="utf-8")
    (tmp_path / "c_bytes.json").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "d_dir.json").mkdir()
    (tmp_path / "e_good.json").write_text('{"kind": "fix", "description": "ok"}', encoding="utf-8")
    (tmp_path / "f_good.json").write_text('{"kind": "feature", "description": "also ok"}', encoding="utf-8")

    loaded = ChangelogManager().load_from_dir(tmp_path)

    assert [c.filename for c in loaded] == ["e_good.json", "f_good.json"]
    assert [c.description for c in loaded] == ["ok", "also ok"]


# D3: CodeGraph builds one METHOD node and one call edge per method call site

def _build(tmp_path, source: str) -> CodeGraph:
    (tmp_path / "mod.py").write_text(source, encoding="utf-8")
    graph = CodeGraph()
    graph.build(tmp_path)
    return graph


def test_d3_method_is_one_method_node_not_also_a_function(tmp_path):
    graph = _build(tmp_path, "class A:\n    def m(self):\n        return 1\n")
    methods = [n for n in graph._nodes.values() if n.name == "m"]
    assert [n.kind for n in methods] == [NodeKind.METHOD]
    assert methods[0].qualified_name == "mod.A.m"


def test_d3_method_call_edge_recorded_once_and_module_function_kept(tmp_path):
    source = (
        "def helper():\n"
        "    return 1\n"
        "class A:\n"
        "    def m(self):\n"
        "        return helper()\n"
        "def top():\n"
        "    return helper()\n"
    )
    graph = _build(tmp_path, source)
    m_nodes = [n for n in graph._nodes.values() if n.name == "m"]
    assert len(m_nodes) == 1 and m_nodes[0].kind == NodeKind.METHOD
    m_calls = [e for e in graph._edges if e.kind == EdgeKind.CALLS and e.source == m_nodes[0].id]
    assert [e.target for e in m_calls] == ["helper"]
    top_nodes = [n for n in graph._nodes.values() if n.name == "top"]
    assert [n.kind for n in top_nodes] == [NodeKind.FUNCTION]
    assert len([n for n in graph._nodes.values() if n.name == "helper"]) == 1


def test_d3_dead_code_reports_each_method_once(tmp_path):
    graph = _build(tmp_path, "class A:\n    def unused(self):\n        pass\n")
    dead = [n.name for n in graph.dead_code()]
    assert dead.count("unused") == 1


# D4: malformed ${ references raise FlowError (not bare ValueError)

def _flow_with_input(value: str) -> FlowDefinition:
    return FlowDefinition("bad", nodes=[FlowNode("writer", "tool", inputs={"text": value})])


def test_d4_unterminated_input_reference_raises_flow_error_naming_node_and_expression():
    flow = _flow_with_input("hello ${oops")
    with pytest.raises(FlowError) as exc:
        FlowExecutor(tool_handler=lambda *_: "ok").run(flow)
    message = str(exc.value)
    assert "writer" in message
    assert "${oops" in message


def test_d4_unterminated_output_reference_raises_flow_error():
    flow = FlowDefinition("bad", nodes=[FlowNode("n", "tool")], outputs={"answer": "${n.text"})
    with pytest.raises(FlowError) as exc:
        FlowExecutor(tool_handler=lambda *_: "ok").run(flow)
    assert "${n.text" in str(exc.value)


def test_d4_parse_then_run_unterminated_reference_is_flow_error():
    flow = parse_flow({"name": "p", "nodes": [{"id": "a", "kind": "tool", "inputs": {"x": "${a"}}]})
    with pytest.raises(FlowError):
        FlowExecutor(tool_handler=lambda *_: "ok").run(flow)
