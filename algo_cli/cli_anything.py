"""Native installed-adapter discovery and explicitly gated CLI-Anything execution."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any

from .irene_memory_path_policy import protected_tool_policy_error
from .search_execution import run_search_process

_NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
_PREFIX = 'cli-anything-'
MAX_SCAN_ENTRIES = 30_000
MAX_PATH_DIRS = 64
MAX_OUTPUT_BYTES = 12_000


def _json(**value: Any) -> str:
    try:
        payload = json.dumps(value, separators=(',', ':'))
    except (ValueError, RecursionError):
        for step in value.get('steps', []):
            step.pop('result', None)
        value['parsed_result_omitted'] = True
        payload = json.dumps(value, separators=(',', ':'))
    if len(payload.encode('utf-8')) > 20_000:
        for step in value.get('steps', []):
            step.pop('result', None)
            for key in ('stdout', 'stderr'):
                if key in step:
                    step[key] = step[key][:512]
        value['output_display_truncated'] = True
        payload = json.dumps(value, separators=(',', ':'))
    return payload


def _name(adapter: str) -> str:
    if not isinstance(adapter, str):
        raise ValueError('adapter must be a CLI-Anything name')
    name = adapter.removeprefix(_PREFIX)
    if _NAME.fullmatch(name) is None:
        raise ValueError('adapter must be a lowercase name, not a path, URL or package expression')
    return name


def _path(cfg: Any) -> str:
    extra = getattr(cfg, 'cli_anything_bin_dir', '')
    if type(extra) is not str or (extra and not Path(extra).expanduser().is_absolute()):
        raise ValueError('cli_anything_bin_dir must be an absolute host-configured directory')
    return (str(Path(extra).expanduser()) + os.pathsep if extra else '') + os.environ.get('PATH', '')


def _which(command: str, cfg: Any = None) -> str | None:
    path = shutil.which(command, path=_path(cfg))
    if path and Path(path).suffix.lower() in {'.bat', '.cmd', '.ps1'}:
        raise ValueError('shell-script launchers on Windows require a qualified execution boundary')
    return os.path.abspath(path) if path else None


def _installed(cfg: Any) -> tuple[list[str], bool]:
    names: set[str] = set()
    directories = list(dict.fromkeys(_path(cfg).split(os.pathsep)))
    scanned = 0
    complete = len(directories) <= MAX_PATH_DIRS
    for directory in directories[:MAX_PATH_DIRS]:
        if not directory or not Path(directory).is_absolute():
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_SCAN_ENTRIES:
                        return sorted(names), False
                    command = entry.name
                    if os.name == 'nt' and command.lower().endswith('.exe'):
                        command = command[:-4]
                    if not command.startswith(_PREFIX):
                        continue
                    name = command[len(_PREFIX):]
                    if _NAME.fullmatch(name) and entry.is_file() and os.access(entry.path, os.X_OK):
                        names.add(name)
        except OSError:
            complete = False
    return sorted(names), complete


def cli_anything_discover(query: str = '', offset: int = 0, limit: int = 20, cfg: Any = None) -> str:
    """Discover installed CLI-Anything app adapters without running or installing code.

    Args:
        query: Optional adapter-name filter, such as blender or libreoffice.
        offset: Zero-based continuation offset.
        limit: Page size from 1 to 30.
    """
    if not isinstance(query, str) or len(query) > 128:
        return _json(status='error', error='query must be at most 128 characters')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 30:
        return _json(status='error', error='offset must be nonnegative and limit must be 1-30')
    try:
        names, complete = _installed(cfg)
    except (ValueError, OSError) as exc:
        return _json(status='error', error=str(exc))
    names = [name for name in names if query.casefold() in name.casefold()]
    end = min(offset + limit, len(names))
    reason = protected_tool_policy_error('cli_anything_run', {}, cfg)
    return _json(status='ok', items=[{'adapter': name, 'command': _PREFIX + name,
                                    'executable_found': True, 'backend': 'not_probed', 'version': 'not_probed'}
                                   for name in names[offset:end]],
                 total_observed=len(names), scan_complete=complete,
                 next_offset=end if end < len(names) else None,
                 execution={'status': 'policy_blocked' if reason else 'requires_runtime_preflight', 'reason': reason},
                 auto_install_on_missing=True, automatic_update='only_when_update_first_is_true')


def _deadline(timeout: float) -> float:
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError('timeout must be finite seconds in (0, 120]')
    return time.monotonic() + timeout


def _invoke(command: list[str], cfg: Any, deadline: float) -> dict[str, Any]:
    from .tools import _isolated_process_group_kwargs, _terminate_process_tree

    cancellation = getattr(cfg, '_algo_team_cancellation', None)
    if cancellation is not None and cancellation.is_set():
        return {'status': 'cancelled', 'invoked': False, 'error': 'cancelled before execution'}
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {'status': 'timed_out', 'invoked': False, 'error': 'shared operation deadline exhausted'}
    try:
        result = run_search_process(
            command, limit=10_000, max_bytes=MAX_OUTPUT_BYTES, timeout=remaining,
            process_kwargs={'cwd': cfg.cwd, 'shell': False,
                            'env': {**os.environ, 'PATH': _path(cfg), 'CLI_HUB_NO_ANALYTICS': '1'},
                            **_isolated_process_group_kwargs()},
            terminate=_terminate_process_tree, cancel_event=cancellation,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return {'status': 'unknown_outcome', 'invoked': True, 'error': str(exc)}
    status = ('cancelled' if result.cancelled else 'timed_out' if result.timed_out
              else 'unknown_outcome' if result.truncated or result.stderr_truncated
              else 'ok' if result.returncode == 0 else 'error')
    stdout = result.stdout.decode('utf-8', errors='replace')
    value: dict[str, Any] = {
        'status': status, 'invoked': True, 'exit_code': result.returncode,
        'stdout': stdout, 'stderr': result.stderr.decode('utf-8', errors='replace'),
        'stdout_truncated': result.truncated, 'stderr_truncated': result.stderr_truncated,
    }
    if not result.truncated:
        try:
            value['result'] = json.loads(stdout)
        except (ValueError, RecursionError):
            pass
    if status != 'ok':
        value['error'] = 'adapter or package operation did not complete successfully; inspect state before retrying'
    return value


def _manage(adapter: str, operation: str, cfg: Any, deadline: float) -> dict[str, Any]:
    manager = _which('cli-hub', cfg)
    if manager is None:
        return {'status': 'error', 'invoked': False, 'error': 'CLI-Hub is not installed on PATH; install CLI-Hub separately'}
    catalog = _invoke([manager, 'search', adapter, '--json'], cfg, deadline)
    if catalog['status'] != 'ok':
        return {'phase': 'catalog', **catalog}
    rows = catalog.get('result')
    matches = [row for row in rows if type(row) is dict and row.get('name') == adapter] if type(rows) is list else []
    if (len(matches) != 1 or matches[0].get('_source') != 'harness'
            or matches[0].get('entry_point') != _PREFIX + adapter):
        return {'phase': 'catalog', 'status': 'error', 'invoked': True,
                'error': 'catalog must identify exactly one matching CLI-Anything harness, not a public CLI'}
    result = {'phase': operation, **_invoke([manager, operation, adapter], cfg, deadline)}
    result['invoked'] = result.get('invoked') is True or catalog.get('invoked') is True
    result['catalog_entry_validated'] = True
    return result


def _finish(adapter: str, steps: list[dict[str, Any]]) -> str:
    invoked = any(step.get('invoked') is True for step in steps)
    return _json(status=steps[-1]['status'], adapter=adapter, invoked=invoked, steps=steps,
                 side_effects='possible' if invoked else 'none', retry_safe=False,
                 verification='not_performed')


def cli_anything_manage(adapter: str, operation: str = 'install', timeout: float = 120, cfg: Any = None) -> str:
    """Install or update one named CLI-Anything adapter through CLI-Hub under runtime approval.

    This runs external package code and may contact the registry. It is blocked
    in safe mode or with Continuum; never use it to bypass a denied shell call.

    Args:
        adapter: Adapter name, such as blender; no paths or package URLs.
        operation: Exactly install or update. No uninstall or bulk operations.
        timeout: Whole-operation timeout in seconds, at most 120.
    """
    reason = protected_tool_policy_error('cli_anything_manage', {}, cfg)
    if reason:
        return _json(status='denied', error=reason, invoked=False)
    steps: list[dict[str, Any]] = []
    try:
        name = _name(adapter)
        deadline = _deadline(timeout)
        if operation not in {'install', 'update'}:
            raise ValueError('operation must be install or update')
        result = _manage(name, operation, cfg, deadline)
        steps.append(result)
        if result['status'] == 'ok':
            result['executable_found'] = _which(_PREFIX + name, cfg) is not None
            if not result['executable_found']:
                result.update(status='error', error='package command exited zero but the adapter is not on PATH')
        return _finish(name, steps)
    except (TypeError, ValueError, OSError) as exc:
        steps.append({'status': 'error', 'error': str(exc), 'invoked': False})
        return _finish(str(adapter)[:80], steps)


def cli_anything_run(adapter: str, arguments: list[str] | None = None, install_missing: bool = True,
                     update_first: bool = False, timeout: float = 120, cfg: Any = None) -> str:
    """Run a CLI-Anything app adapter, automatically installing it if missing.

    External code execution, package changes and networks retain normal approval
    and policy. Safe mode and Continuum block this, including help and installs.
    Existing adapters are not updated unless update_first is explicitly true.

    Args:
        adapter: CLI-Anything adapter name, such as blender or libreoffice.
        arguments: Argument array, for example ["--json", "project", "info"]. Empty means --help.
        install_missing: Automatically install a missing adapter through CLI-Hub.
        update_first: Explicitly update an existing adapter before running it.
        timeout: Shared install/update/run deadline in seconds, at most 120.
    """
    reason = protected_tool_policy_error('cli_anything_run', {}, cfg)
    if reason:
        return _json(status='denied', error=reason, invoked=False)
    steps: list[dict[str, Any]] = []
    try:
        name = _name(adapter)
        deadline = _deadline(timeout)
        args = ['--help'] if arguments is None or arguments == [] else arguments
        if (type(args) is not list or len(args) > 64
                or any(type(arg) is not str or '\x00' in arg for arg in args)
                or sum(len(arg.encode('utf-8')) for arg in args) > 32_768):
            raise ValueError('arguments must be at most 64 strings / 32768 UTF-8 bytes, without NULs')
        if type(install_missing) is not bool or type(update_first) is not bool:
            raise ValueError('install_missing and update_first must be booleans')
        executable = _which(_PREFIX + name, cfg)
        operation = 'install' if executable is None and install_missing else 'update' if executable and update_first else None
        if operation:
            steps.append(_manage(name, operation, cfg, deadline))
            if steps[-1]['status'] != 'ok':
                return _finish(name, steps)
            executable = _which(_PREFIX + name, cfg)
        if executable is None:
            steps.append({'status': 'error', 'invoked': False, 'error': 'adapter is not installed on PATH'})
        else:
            steps.append({'phase': 'run', **_invoke([executable, *args], cfg, deadline)})
        return _finish(name, steps)
    except (TypeError, ValueError, OSError, UnicodeError) as exc:
        steps.append({'status': 'error', 'invoked': False, 'error': str(exc)})
        return _finish(str(adapter)[:80], steps)
