"""Native CLI-Anything discovery, package management and execution boundaries."""
import inspect
import json
import os
import sys
import threading
import time

import pytest

from algo_cli import cli_anything as native, execution_guardrails as guardrails, nathan_runtime as runtime, tools
from algo_cli.config import Config
from algo_cli.marcus_authority import Capability, EffectClass, policy_for_action
from algo_cli.samuel_policy_engine import resolve_action


@pytest.fixture
def cfg(tmp_path):
    return Config(cwd=str(tmp_path), safe_mode=False, continuum_enabled=False)


def test_native_registration_and_hidden_host_fields():
    for name in ('cli_anything_discover', 'cli_anything_run', 'cli_anything_manage'):
        assert name in tools.TOOL_MAP
        signature = inspect.signature(tools.TOOL_MAP[name])
        assert not {'cfg', 'cwd', 'safe_mode', 'cancel_event'} & set(signature.parameters)
    action = resolve_action('cli_anything_discover', {}, cwd='/fixture')
    assert runtime.RuntimeAuthoritySession('/fixture').baseline_allows(action)
    for name in ('cli_anything_run', 'cli_anything_manage'):
        policy = policy_for_action(name)
        assert policy.effect_class is EffectClass.CODE_EXECUTION
        assert policy.capability_mask.value & Capability.SHELL.value
        assert policy.capability_mask.value & Capability.NETWORK.value
        assert policy.requires_approval and not policy.safe_retry
        assert not runtime.RuntimeAuthoritySession('/fixture').baseline_allows(
            resolve_action(name, {'adapter': 'fixture'}, cwd='/fixture'))


def test_discovery_is_native_offline_paged_metadata(monkeypatch, tmp_path, cfg):
    for name in ('blender', 'gimp', 'word'):
        path = tmp_path / ('cli-anything-' + name)
        path.write_text('must not execute')
        path.chmod(0o700)
    (tmp_path / 'cli-anything-not-executable').write_text('no')
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(native, '_invoke', lambda *_: pytest.fail('Discovery must not execute'))
    cfg.continuum_enabled = True
    first = json.loads(runtime.run_tool('cli_anything_discover', {'limit': 2}, cfg))
    second = json.loads(native.cli_anything_discover(offset=first['next_offset'], cfg=cfg))
    assert [row['adapter'] for row in first['items']] == ['blender', 'gimp']
    assert second['items'][0]['adapter'] == 'word'
    assert first['scan_complete'] and first['execution']['status'] == 'policy_blocked'
    assert 'Continuum' in first['execution']['reason']
    assert all(row['backend'] == row['version'] == 'not_probed' for row in first['items'])


def test_discovery_has_scan_budget_and_ignores_relative_path(monkeypatch, tmp_path, cfg):
    for number in range(6):
        (tmp_path / str(number)).write_text('')
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(native, 'MAX_SCAN_ENTRIES', 2)
    assert not json.loads(native.cli_anything_discover(cfg=cfg))['scan_complete']
    monkeypatch.setenv('PATH', '.')
    assert json.loads(native.cli_anything_discover(cfg=cfg))['items'] == []


@pytest.mark.parametrize('kwargs', [{'limit': 0}, {'limit': 31}, {'limit': True}, {'offset': -1},
                                     {'offset': False}, {'query': 1}, {'query': 'x' * 129}])
def test_discovery_rejects_invalid_bounds(kwargs, cfg):
    assert json.loads(native.cli_anything_discover(**kwargs, cfg=cfg))['status'] == 'error'


@pytest.mark.parametrize('tool', ['cli_anything_run', 'cli_anything_manage'])
@pytest.mark.parametrize('mode', ['execute', 'yolo'])
@pytest.mark.parametrize('protected,safe', [(True, False), (True, True), (False, True)])
def test_protection_precedes_resolution_or_install(monkeypatch, cfg, tool, mode, protected, safe):
    cfg.continuum_enabled, cfg.safe_mode, cfg.session_mode = protected, safe, mode
    monkeypatch.setattr(native, '_which', lambda *_: pytest.fail('No resolution before policy'))
    monkeypatch.setattr(native, '_invoke', lambda *_: pytest.fail('No execution before policy'))
    raw = runtime.run_tool(tool, {'adapter': 'fixture', 'cfg': Config()}, cfg)
    assert raw.startswith('Error:')
    result = json.loads(getattr(native, tool)('fixture', cfg=cfg))
    assert result['status'] == 'denied' and not result['invoked']
    assert runtime._pre_dispatch_tool_error(tool, {'adapter': 'fixture'}, cfg)


@pytest.mark.parametrize('name', ['../gimp', '/bin/sh', '-x', 'https://example.com/tool', 'Gimp', '', 'x;id', 'a' * 65])
def test_names_cannot_be_paths_urls_flags_or_shell(monkeypatch, cfg, name):
    monkeypatch.setattr(native, '_which', lambda *_: pytest.fail('Invalid name must not resolve'))
    assert json.loads(native.cli_anything_run(name, cfg=cfg))['status'] == 'error'
    assert json.loads(native.cli_anything_manage(name, cfg=cfg))['status'] == 'error'


@pytest.mark.parametrize('args', ['--help', {}, [None], [1], ['\x00'], ['x'] * 65, ['x' * 32769]])
def test_argument_arrays_are_validated_before_execution(monkeypatch, cfg, args):
    monkeypatch.setattr(native, '_which', lambda *_: pytest.fail('No execution on invalid arguments'))
    assert json.loads(native.cli_anything_run('fixture', args, cfg=cfg))['status'] == 'error'


@pytest.mark.parametrize('timeout', [0, -1, 121, float('inf'), float('nan'), True, '30'])
def test_timeouts_are_validated(monkeypatch, cfg, timeout):
    monkeypatch.setattr(native, '_which', lambda *_: pytest.fail('Invalid deadline must not resolve'))
    assert json.loads(native.cli_anything_manage('fixture', timeout=timeout, cfg=cfg))['status'] == 'error'


def install_fixture(monkeypatch, *, installed=False, failure=False):
    state = {'installed': installed, 'calls': [], 'deadlines': []}
    def which(command, _cfg=None):
        return '/manager/cli-hub' if command == 'cli-hub' else '/adapter/fixture' if state['installed'] else None
    def invoke(command, _cfg, deadline):
        state['calls'].append(command)
        state['deadlines'].append(deadline)
        if command[1] == 'search':
            return {'status': 'ok', 'invoked': True, 'result': [
                {'name': 'fixture', '_source': 'harness', 'entry_point': 'cli-anything-fixture'}]}
        if command[0] == '/manager/cli-hub':
            if failure:
                return {'status': 'error', 'invoked': True, 'exit_code': 1}
            state['installed'] = True
        return {'status': 'ok', 'invoked': True, 'exit_code': 0, 'stdout': '{}'}
    monkeypatch.setattr(native, '_which', which)
    monkeypatch.setattr(native, '_invoke', invoke)
    return state


def test_missing_adapter_installs_automatically_then_reresolves(monkeypatch, cfg):
    state = install_fixture(monkeypatch)
    result = json.loads(native.cli_anything_run('fixture', ['--json', 'info'], cfg=cfg))
    assert state['calls'] == [['/manager/cli-hub', 'search', 'fixture', '--json'],
                             ['/manager/cli-hub', 'install', 'fixture'], ['/adapter/fixture', '--json', 'info']]
    assert len(set(state['deadlines'])) == 1
    assert result['status'] == 'ok' and result['invoked']
    assert result['verification'] == 'not_performed' and not result['retry_safe']


def test_installed_adapter_is_not_automatically_updated(monkeypatch, cfg):
    state = install_fixture(monkeypatch, installed=True)
    assert json.loads(native.cli_anything_run('fixture', cfg=cfg))['status'] == 'ok'
    assert state['calls'] == [['/adapter/fixture', '--help']]


def test_update_before_run_is_explicit_and_uses_shared_deadline(monkeypatch, cfg):
    state = install_fixture(monkeypatch, installed=True)
    result = json.loads(native.cli_anything_run('fixture', [], update_first=True, cfg=cfg))
    assert result['status'] == 'ok'
    assert state['calls'] == [['/manager/cli-hub', 'search', 'fixture', '--json'],
                             ['/manager/cli-hub', 'update', 'fixture'], ['/adapter/fixture', '--help']]
    assert len(set(state['deadlines'])) == 1


def test_missing_install_can_be_disabled_and_failure_is_not_replayed(monkeypatch, cfg):
    state = install_fixture(monkeypatch, failure=True)
    result = json.loads(native.cli_anything_run('fixture', install_missing=False, cfg=cfg))
    assert result['status'] == 'error' and not state['calls']
    result = json.loads(native.cli_anything_run('fixture', cfg=cfg))
    assert len(state['calls']) == 2 and result['side_effects'] == 'possible' and not result['retry_safe']


@pytest.mark.parametrize('operation', ['install', 'update'])
def test_explicit_native_management(monkeypatch, cfg, operation):
    state = install_fixture(monkeypatch)
    result = json.loads(runtime.run_tool('cli_anything_manage', {'adapter': 'fixture', 'operation': operation}, cfg))
    assert result['status'] == 'ok' and result['steps'][0]['executable_found']
    assert state['calls'] == [['/manager/cli-hub', 'search', 'fixture', '--json'],
                             ['/manager/cli-hub', operation, 'fixture']]


def test_management_zero_exit_requires_executable_and_keeps_side_effects(monkeypatch, cfg):
    monkeypatch.setattr(native, '_which', lambda name, _cfg=None: '/manager/cli-hub' if name == 'cli-hub' else None)
    monkeypatch.setattr(native, '_invoke', lambda command, *_: {
        'status': 'ok', 'invoked': True, 'exit_code': 0,
        'result': [{'name': 'fixture', '_source': 'harness', 'entry_point': 'cli-anything-fixture'}]
                  if command[1] == 'search' else {}})
    result = json.loads(native.cli_anything_manage('fixture', cfg=cfg))
    assert result['status'] == 'error' and result['invoked']
    assert result['steps'][0]['phase'] == 'install' and result['steps'][0]['catalog_entry_validated']


def test_windows_shell_launchers_are_not_implicitly_run(monkeypatch):
    monkeypatch.setattr(native.shutil, 'which', lambda *_a, **_kw: '/fixture/cli-anything-app.cmd')
    with pytest.raises(ValueError, match='shell-script'):
        native._which('cli-anything-app')


def test_literal_argv_no_shell_and_real_process_output(monkeypatch, cfg, tmp_path):
    marker = tmp_path / 'must-not-exist'
    literal = '; touch ' + str(marker)
    monkeypatch.setattr(native, '_which', lambda *_: sys.executable)
    result = json.loads(runtime.run_tool('cli_anything_run', {'adapter': 'fixture', 'arguments': [
        '-c', 'import json,sys; print(json.dumps(sys.argv[1:]))', literal]}, cfg))
    assert result['status'] == 'ok' and result['steps'][-1]['result'] == [literal]
    assert not marker.exists()


def test_real_timeout_retains_uncertainty(monkeypatch, cfg):
    monkeypatch.setattr(native, '_which', lambda *_: sys.executable)
    result = json.loads(native.cli_anything_run('fixture', ['-c', 'import time;time.sleep(10)'], timeout=0.1, cfg=cfg))
    assert result['status'] == 'timed_out' and result['side_effects'] == 'possible'
    assert not result['retry_safe'] and result['verification'] == 'not_performed'


def test_output_capture_and_json_envelope_are_bounded(monkeypatch, cfg):
    monkeypatch.setattr(native, '_which', lambda *_: sys.executable)
    raw = native.cli_anything_run('fixture', ['-c', 'import sys;sys.stdout.write("x"*100000)'], cfg=cfg)
    result = json.loads(raw)
    assert result['status'] == 'unknown_outcome' and result['steps'][0]['stdout_truncated']
    assert len(raw.encode()) <= 20_000
    raw = native._finish('fixture', [{'status': 'ok', 'invoked': True, 'stdout': '\x00' * 12000,
                                    'stderr': '\x00' * 12000}] * 2)
    assert len(raw.encode()) <= 20_000 and json.loads(raw)['output_display_truncated']


def test_cancellation_before_execution_does_not_spawn(monkeypatch, cfg):
    cfg._algo_team_cancellation = threading.Event()
    cfg._algo_team_cancellation.set()
    monkeypatch.setattr(native, 'run_search_process', lambda *_a, **_kw: pytest.fail('No process on cancelled task'))
    assert not native._invoke([sys.executable], cfg, time.monotonic() + 2)['invoked']


def test_live_cancellation_stops_bounded_process(cfg):
    event = threading.Event()
    cfg._algo_team_cancellation = event
    timer = threading.Timer(0.1, event.set)
    timer.start()
    try:
        result = native._invoke([sys.executable, '-c', 'import time; time.sleep(10)'], cfg, time.monotonic() + 5)
        assert result['status'] == 'cancelled' and result['invoked']
    finally:
        timer.cancel()


def test_native_execution_records_possible_mutation_not_verification(cfg, tmp_path):
    scope = guardrails.begin_execution_scope(tmp_path)
    try:
        runtime.record_tool_attempt(cfg, name='cli_anything_run', args={'adapter': 'fixture'},
            result=json.dumps({'status': 'ok', 'invoked': True}), status='worked')
        assert not guardrails.completion_decision().allowed
    finally:
        guardrails.end_execution_scope(scope)


def test_native_discovery_topic_reports_effective_policy(monkeypatch, cfg):
    monkeypatch.setattr(tools, '_harness_stats_for_config', lambda _: {})
    cfg.continuum_enabled = True
    result = json.loads(tools.available_actions('cli_anything', cfg=cfg))
    assert result['topic'] == 'cli-anything'
    assert 'cli_anything_discover' in json.dumps(result)
    assert 'policy_blocked' in json.dumps(result)


@pytest.mark.parametrize('name', ['cli_anything_run', 'cli_anything_manage'])
def test_model_cannot_override_workdir_or_safe_mode(name, cfg):
    result = runtime.run_tool(name, {'adapter': 'fixture', 'cwd': '/elsewhere', 'safe_mode': False}, cfg)
    assert 'cwd' in result or 'safe_mode' in result


def test_host_configured_environment_supports_new_adapter_resolution(monkeypatch, tmp_path, cfg):
    bin_dir = tmp_path / 'adapter-bin'
    bin_dir.mkdir()
    command = bin_dir / ('cli-anything-fixture.exe' if os.name == 'nt' else 'cli-anything-fixture')
    command.write_text('not executed by discovery')
    command.chmod(0o700)
    cfg.cli_anything_bin_dir = str(bin_dir)
    monkeypatch.setenv('PATH', '')
    assert native._which('cli-anything-fixture', cfg) == str(command)
    assert json.loads(native.cli_anything_discover(cfg=cfg))['items'][0]['adapter'] == 'fixture'
    cfg.cli_anything_bin_dir = 'relative-bin'
    assert json.loads(native.cli_anything_discover(cfg=cfg))['status'] == 'error'


def test_post_install_resolution_error_keeps_possible_package_changes(monkeypatch, cfg):
    calls = []
    def which(command, _cfg=None):
        if command == 'cli-hub':
            return '/manager/cli-hub'
        raise ValueError('unsupported launcher')
    monkeypatch.setattr(native, '_which', which)
    monkeypatch.setattr(native, '_invoke', lambda *args: calls.append(args) or {
        'status': 'ok', 'invoked': True, 'result': [
            {'name': 'fixture', '_source': 'harness', 'entry_point': 'cli-anything-fixture'}]})
    result = json.loads(native.cli_anything_manage('fixture', cfg=cfg))
    assert len(calls) == 2 and result['invoked'] and result['side_effects'] == 'possible'
    assert result['status'] == 'error'


@pytest.mark.parametrize('operation', ['uninstall', '--help', 'matrix', '', None, []])
def test_manager_cannot_expand_into_arbitrary_operations(monkeypatch, cfg, operation):
    monkeypatch.setattr(native, '_invoke', lambda *_: pytest.fail('No invalid package operation'))
    assert json.loads(native.cli_anything_manage('fixture', operation, cfg=cfg))['status'] == 'error'


def test_installed_adapter_ignores_forged_model_config(monkeypatch, cfg):
    state = install_fixture(monkeypatch, installed=True)
    result = json.loads(runtime.run_tool('cli_anything_run', {
        'adapter': 'fixture', 'cfg': Config(continuum_enabled=True, safe_mode=True)}, cfg))
    assert result['status'] == 'ok' and state['calls']


def test_dispatch_requires_approval_before_adapter_or_package_execution(tmp_path, cfg):
    from algo_cli.james_dispatch import dispatch_action
    from test_james_dispatch import _dependencies
    dependencies = _dependencies(tmp_path, lambda *_: pytest.fail('No dispatch without approval'))
    dependencies.approve = lambda *_a, **_kw: False
    result = dispatch_action('cli_anything_run', {'adapter': 'fixture'}, cfg, dependencies=dependencies, render=False)
    assert not result.outcome.invoked


def test_dispatch_invokes_native_adapter_but_does_not_verify_artifacts(monkeypatch, tmp_path, cfg):
    from algo_cli.arthur_outcomes import VerificationStatus
    from algo_cli.james_dispatch import dispatch_action
    from test_james_dispatch import _dependencies
    monkeypatch.setattr(native, '_which', lambda *_: sys.executable)
    dependencies = _dependencies(tmp_path, runtime.run_tool)
    dependencies.approve = lambda *_a, **_kw: True
    result = dispatch_action('cli_anything_run', {'adapter': 'fixture', 'arguments': [
        '-c', 'from pathlib import Path;Path("native-result.txt").write_text("fixture")']},
        cfg, dependencies=dependencies, render=False)
    assert result.outcome.invoked
    assert (tmp_path / 'native-result.txt').read_text() == 'fixture'
    assert result.outcome.verification is not VerificationStatus.PASSED


def test_actual_agent_loop_can_request_native_discovery(monkeypatch, tmp_path):
    from test_agent_progress_recovery import call, run_script
    result = run_script(monkeypatch, tmp_path,
        lambda turn: call('cli_anything_discover', {'query': 'fixture'}, turn) if turn == 1
        else {'content': 'Discovery complete; backend availability has not been tested.'},
        invoke=runtime.run_tool)
    assert result[0] == 0 and result[3] == ['cli_anything_discover']
    assert result[1][-1]['status'] == 'complete'


@pytest.mark.parametrize('row', [None, {'name': 'fixture', '_source': 'public', 'entry_point': 'different'},
                                 {'name': 'other', '_source': 'harness', 'entry_point': 'cli-anything-other'}])
def test_management_does_not_install_non_adapter_catalog_entries(monkeypatch, cfg, row):
    calls = []
    monkeypatch.setattr(native, '_which', lambda *_: '/manager/cli-hub')
    monkeypatch.setattr(native, '_invoke', lambda command, *_: calls.append(command) or {
        'status': 'ok', 'invoked': True, 'result': [row]})
    result = json.loads(native.cli_anything_manage('fixture', cfg=cfg))
    assert result['status'] == 'error' and len(calls) == 1 and calls[0][1] == 'search'


def test_unexpected_invocation_error_cannot_skip_mutation_tracking(cfg, tmp_path):
    scope = guardrails.begin_execution_scope(tmp_path)
    try:
        runtime.record_tool_attempt(cfg, name='cli_anything_run', args={'adapter': 'fixture'},
            result='Tool error for cli_anything_run: unexpected adapter failure', status='failed', invoked=True)
        assert not guardrails.completion_decision().allowed
    finally:
        guardrails.end_execution_scope(scope)


def test_deep_adapter_json_does_not_lose_process_outcome():
    result = []
    for _ in range(1100):
        result = [result]
    raw = native._finish('fixture', [{'status': 'ok', 'invoked': True, 'stdout': 'retained', 'result': result}])
    value = json.loads(raw)
    assert value['invoked'] and value['parsed_result_omitted']
    assert value['steps'][0]['stdout'] == 'retained'
