"""Runtime diagnostics report the live session, or say plainly that it is unknown.

A model-called self-check used to run without the session configuration, so it
reported Continuum as not selected and an empty context, which read like a real
status. Page bounds were also invisible to the model.
"""
import json

import pytest
from ollama._utils import convert_function_to_tool

from algo_cli import nathan_runtime, tools
from algo_cli.config import Config


def _protected_session(tmp_path):
    cfg = Config(cwd=str(tmp_path), continuum_enabled=True)
    cfg.messages = [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'second'}]
    return cfg


def test_model_tool_call_reports_the_live_session(tmp_path):
    cfg = _protected_session(tmp_path)

    result = json.loads(nathan_runtime.run_tool('runtime_diagnostics', {'section': 'selfcheck'}, cfg))

    assert result['ok'] is True
    data = result['data']
    assert data['configuration'] == 'live'
    assert data['continuum_selected'] is True
    assert data['shell'] == 'policy_blocked'
    assert data['context']['messages'] == 2


def test_model_tool_call_context_section_reports_the_live_session(tmp_path):
    cfg = _protected_session(tmp_path)

    result = json.loads(nathan_runtime.run_tool('runtime_diagnostics', {'section': 'context'}, cfg))

    assert result['data']['configuration'] == 'live'
    assert result['data']['messages'] == 2


def test_missing_configuration_is_unknown_not_false_or_zero():
    result = json.loads(tools.runtime_diagnostics('selfcheck', cfg=None))

    data = result['data']
    assert data['configuration'] == 'unavailable'
    assert data['continuum_selected'] is None
    assert data['shell'] == 'unknown_without_configuration'
    assert data['context'] == {'configuration': 'unavailable'}


def test_context_section_without_configuration_is_unavailable():
    result = json.loads(tools.runtime_diagnostics('context', cfg=None))

    assert result['data'] == {'configuration': 'unavailable'}


@pytest.mark.parametrize('offset,limit', [(0, 0), (0, 11), (0, 15), (-1, 3)])
def test_page_errors_state_the_permitted_range(offset, limit):
    result = json.loads(tools.runtime_diagnostics('search', offset=offset, limit=limit, cfg=Config()))

    assert result == {'ok': False, 'error': 'invalid_diagnostic_page',
                      'offset_minimum': 0, 'limit_minimum': 1, 'limit_maximum': 10}


def test_tool_schema_states_the_page_bounds():
    fn = next(f for f in tools.ALL_TOOLS if getattr(f, '__name__', '') == 'runtime_diagnostics')
    properties = convert_function_to_tool(fn).model_dump(exclude_none=True)['function']['parameters']['properties']

    assert 'cfg' not in properties
    assert '1 to 10' in properties['limit']['description']
    assert 'next_offset' in properties['offset']['description']
    assert 'selfcheck' in properties['section']['description']
