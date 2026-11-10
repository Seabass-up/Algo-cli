"""Diagnostics disclose effective policy and bounded public metadata, not memory."""
import json
from types import SimpleNamespace

import pytest

from algo_cli import decision_layer as decisions, tools
from algo_cli.config import Config
from algo_cli.irene_memory_path_policy import protected_tool_policy_error


@pytest.mark.parametrize('version,runner,allowed', [
    ('0.40.15', 'mlx', True), ('0.39.9', 'mlx', False), ('0.40.15', 'unknown', False),
])
def test_native_mlx_decision_compatibility(monkeypatch, version, runner, allowed):
    replies = {'/api/version': {'version': version},
               '/api/tags': {'models': [{'name': 'clef-flash:latest', 'digest': 'a' * 64}]},
               '/api/show': {'capabilities': ['decision'], 'details': {'format': 'safetensors', 'runner': runner}},
               '/api/ps': {'models': []}}
    monkeypatch.setattr(decisions, '_http_json', lambda _base, route, _payload, _timeout: replies[route])
    provider = decisions.OllamaProvider('clef-flash', Config(), {})
    if allowed:
        assert provider.status()['available'] is True
        assert provider.status()['inference'] == 'not_probed_by_status'
    else:
        with pytest.raises(decisions.DecisionError, match='incompatible_decision_model'):
            provider.status()


def test_provider_specific_status_does_not_report_success_on_failure(monkeypatch):
    monkeypatch.setattr(decisions, 'provider_for', lambda *_: SimpleNamespace(
        model='fixture', local=True, capabilities=decisions.Capabilities(),
        status=lambda: (_ for _ in ()).throw(decisions.DecisionError('incompatible_decision_model'))))
    result = json.loads(decisions.decision_status('clef-flash', Config()))
    assert result['ok'] is False
    assert result['provider_health'] == 'unavailable'
    from algo_cli.nathan_runtime import classify_tool_status
    assert classify_tool_status(json.dumps(result), name='decision_status') == 'failed'


def test_status_does_not_call_unprobed_remote_provider_available(monkeypatch):
    monkeypatch.setattr(decisions, 'provider_for', lambda *_: SimpleNamespace(
        model='fixture', local=False, capabilities=decisions.Capabilities(),
        status=lambda: {'configured': True, 'availability': 'not_probed', 'local': False}))
    result = json.loads(decisions.decision_status('clef-flash', Config()))
    assert result['provider_health'] == 'not_verified'


def test_diagnostics_pagination_is_bounded_and_preserves_continuation(monkeypatch, tmp_path):
    from algo_cli import harness
    roots = []
    records = []
    for n in range(8):
        path = tmp_path / f'harness-{n}.md'
        path.write_text('# Public harness guide\n')
        records.append({'id': 'forged', 'kind': 'skill', 'harness': 'algo-cli', 'path': str(path)})
    roots.append(harness.SourceRoot('algo-cli', 'skill', tmp_path, ('*.md',), 1))
    monkeypatch.setattr(harness, 'built_in_source_roots', lambda **_: roots)
    monkeypatch.setattr(harness, '_protected_memory_source_allowed', lambda _: True)
    monkeypatch.setattr(harness, 'retrieval_index', lambda **_: {'records': records})
    monkeypatch.setattr(harness, 'search_index', lambda *a, **kw: kw['index']['records'])
    first = json.loads(tools.runtime_diagnostics('search', query='harness', limit=3, cfg=Config()))
    second = json.loads(tools.runtime_diagnostics('search', query='harness', offset=first['next_offset'], limit=3, cfg=Config()))
    assert first['next_offset'] == 3 and second['next_offset'] == 6
    assert {r['id'] for r in first['items']}.isdisjoint(r['id'] for r in second['items'])
    assert len(json.dumps(first).encode()) < 1800


def test_verification_discovery_reports_effective_shell_denial(monkeypatch, tmp_path):
    from algo_cli import harness
    monkeypatch.setattr(harness, 'stats', lambda: {})
    monkeypatch.setattr('algo_cli.continuum_memory.doctor', lambda cfg: {})
    cfg = Config(cwd=str(tmp_path), continuum_enabled=True)
    result = json.loads(tools.available_actions('verification', cfg=cfg))
    assert result['focused']['match'] == 'matched'
    state = result['focused']['effective_tool_policy']['run_shell']
    assert state['status'] == 'policy_blocked'
    assert 'Continuum' in state['reason']
    assert 'runtime_diagnostics' in result['focused']['model_callable_tools']['verification']


def test_narrow_diagnostics_do_not_copy_conversation_or_summary(tmp_path):
    cfg = Config(cwd=str(tmp_path), continuum_enabled=True)
    cfg.messages = [{'role': 'user', 'content': 'PRIVATE-MESSAGE-CANARY'}]
    cfg.session_summary = 'PRIVATE-SUMMARY-CANARY'
    result = tools.runtime_diagnostics(section='context', cfg=cfg)
    value = json.loads(result)
    assert value['ok'] and value['data']['messages'] == 1
    assert 'PRIVATE-' not in result
    assert len(result.encode()) < 1800


@pytest.mark.parametrize('command', ['/selfcheck', '/context', '/context status'])
def test_exact_protected_diagnostics_are_admitted_without_broad_dispatch(command, tmp_path, monkeypatch):
    cfg = Config(cwd=str(tmp_path), continuum_enabled=True)
    monkeypatch.setattr('algo_cli.theodore_runtime_services.create_client',
                        lambda *_: pytest.fail('Diagnostic must not create a model client'))
    assert protected_tool_policy_error('session_command', {'command': command}, cfg) is None
    assert json.loads(tools.session_command(command, cfg=cfg))['ok']


@pytest.mark.parametrize('command', ['/context clear', '/context rebuild', '/selfcheck extra'])
def test_diagnostic_projection_does_not_admit_mutation_forms(command, tmp_path):
    cfg = Config(cwd=str(tmp_path), continuum_enabled=True)
    assert protected_tool_policy_error('session_command', {'command': command}, cfg) is not None


def test_public_search_projection_omits_paths_descriptions_and_owner_catalog(monkeypatch, tmp_path):
    from algo_cli import harness
    path = tmp_path / 'harness-search-first.md'
    path.write_text('# Public harness skill\nSearch public product references.\n')
    root = harness.SourceRoot('algo-cli', 'skill', tmp_path, ('*.md',), 1)
    monkeypatch.setattr(harness, 'built_in_source_roots', lambda **_: (root,))
    monkeypatch.setattr(harness, '_protected_memory_source_allowed', lambda _: True)
    monkeypatch.setattr(harness, 'retrieval_index', lambda **_: {'records': [
        {'id': 'FORGED-PRIVATE-ID', 'harness': 'algo-cli', 'kind': 'skill',
         'relative_path': 'harness-search-first.md', 'path': str(path), 'summary': 'PRIVATE-SUMMARY'},
        {'id': 'owner:secret', 'harness': 'algo-cli', 'kind': 'algorithm', 'summary': 'PRIVATE-ALGORITHM'},
    ]})
    monkeypatch.setattr(harness, 'search_index', lambda *args, **kwargs: kwargs['index']['records'])
    result = tools.runtime_diagnostics(section='search', query='harness', cfg=Config())
    assert 'harness-search-first.md' in result
    assert 'PRIVATE' not in result and 'owner:secret' not in result and str(tmp_path) not in result


def test_program_reports_withholding_and_continuation_without_releasing_sensitive_output():
    from algo_cli.nathan_program_runtime import ProgramOutput, ProgramTaint, StepReference
    from algo_cli.marcus_authority import DataClass
    taint = ProgramTaint(True, False, (DataClass.SENSITIVE,), ('search',))
    output = ProgramOutput(StepReference('search'), 'text/plain', 500, 'a' * 64,
                           '[protected output omitted; 500 bytes encrypted]', taint).to_dict()
    assert output['content_visibility'] == 'protected'
    assert output['continuation']['tool'] == 'runtime_diagnostics'
    assert 'sha256' not in output


def test_diagnostic_arguments_cannot_declassify_protected_references():
    from algo_cli.nathan_program_runtime import ProgramAuthorization, ProgramValidationError, compile_program
    plan = {'version': 1, 'steps': [
        {'id': 'source', 'kind': 'action', 'action': 'harness_search', 'args': {'query': 'harness'}},
        {'id': 'metadata', 'kind': 'action', 'action': 'runtime_diagnostics',
         'args': {'section': 'search', 'query': {'$ref': 'source'}}},
    ], 'outputs': [{'$ref': 'metadata'}]}
    with pytest.raises(ProgramValidationError, match='action inputs must be static'):
        compile_program(plan, authorization=ProgramAuthorization(frozenset({'harness_search', 'runtime_diagnostics'})))


def test_diagnostic_registered_as_observation_not_sensitive_output():
    from algo_cli.marcus_authority import DataClass, EffectClass, policy_for_action
    from algo_cli.nathan_runtime import RuntimeAuthoritySession
    from algo_cli.samuel_policy_engine import resolve_action
    policy = policy_for_action('runtime_diagnostics')
    assert policy.effect_class is EffectClass.OBSERVE
    assert DataClass.SENSITIVE not in policy.data_classes
    assert RuntimeAuthoritySession('/tmp').baseline_allows(resolve_action('runtime_diagnostics', {}, cwd='/tmp'))


def test_oversized_unicode_identifier_cannot_create_a_nonadvancing_page(monkeypatch):
    from algo_cli import harness
    rows = [{'id': 'algo-cli:tool:' + '\U0001f680' * 200, 'kind': 'runtime_capability', 'harness': 'algo-cli'},
            {'id': 'algo-cli:tool:read_file', 'kind': 'runtime_capability', 'harness': 'algo-cli'}]
    monkeypatch.setattr(harness, 'retrieval_index', lambda **_: {'records': rows})
    monkeypatch.setattr(harness, 'built_in_source_roots', lambda **_: ())
    monkeypatch.setattr(harness, '_runtime_capability_records', lambda **_: rows)
    monkeypatch.setattr(harness, 'search_index', lambda *a, **kw: kw['index']['records'])
    result = json.loads(tools.runtime_diagnostics('search', query='read', limit=1, cfg=Config()))
    assert result['items'] == [{'id': 'algo-cli:tool:read_file', 'kind': 'runtime_capability'}]
    assert result['next_offset'] is None
    assert result['data']['omitted_oversized_records'] == 1
