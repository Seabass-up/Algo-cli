"""Bounded metadata diagnostics, without private context or broad slash dispatch."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def protected_diagnostic_command(command: str) -> str | None:
    words = str(command).strip().casefold().split()
    if words == ['/selfcheck']:
        return 'selfcheck'
    if words in (['/context'], ['/context', 'status']):
        return 'context'
    return None


def _context(cfg: Any) -> dict[str, Any]:
    messages = getattr(cfg, 'messages', [])
    summary = getattr(cfg, 'session_summary', '')
    context_tokens = getattr(cfg, 'num_ctx', None)
    if type(context_tokens) is not int or not 256 <= context_tokens <= 2_000_000:
        context_tokens = None
    return {'messages': len(messages) if isinstance(messages, list) else 0,
            'summary_active': bool(summary),
            'configured_context_tokens': context_tokens,
            'token_usage': 'not_evaluated_without_building_private_context'}


def runtime_diagnostics(section: str = 'selfcheck', query: str = '', provider: str = '',
                        offset: int = 0, limit: int = 3, cfg: Any = None) -> str:
    """Read bounded public/runtime metadata; never memory bodies or arbitrary files.

    Sections: selfcheck, context, harness, search, decisions. Search returns only
    public product record identities, never snippets, paths or owner algorithms.
    Follow next_offset for another bounded page. This is not a functional test.
    """
    from . import continuum_memory, harness
    from .irene_memory_path_policy import protected_tool_policy_error

    def error(code: str) -> str:
        return json.dumps({'ok': False, 'error': code}, separators=(',', ':'))

    if section not in {'selfcheck', 'context', 'harness', 'search', 'decisions'}:
        return error('invalid_diagnostic_section')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10:
        return error('invalid_diagnostic_page')
    if not isinstance(query, str) or len(query.encode('utf-8')) > 1024:
        return error('invalid_diagnostic_query')
    try:
        protected = continuum_memory.selected(cfg) if cfg is not None else False
        rows: list[dict[str, Any]] = []
        if section == 'context':
            data = _context(cfg)
        elif section in {'selfcheck', 'harness'}:
            data = {'scope': 'bounded_metadata_not_full_audit', 'continuum_selected': protected}
            if section == 'selfcheck':
                refusal = protected_tool_policy_error('run_shell', {}, cfg)
                data.update({'shell': 'policy_blocked' if refusal else 'requires_runtime_preflight',
                             'context': _context(cfg), 'functional_verification': 'not_run'})
            else:
                index = harness.retrieval_index(protected_memory=protected)
                records = [r for r in index.get('records', []) if isinstance(r, dict)]
                data.update({'records': len(records), 'retrieval_backend': 'lexical',
                             'runtime_capabilities': sum(r.get('kind') == 'runtime_capability' for r in records)})
        elif section == 'search':
            index = harness.retrieval_index(protected_memory=protected)
            # Never add the owner-catalog projection or return another harness's data.
            roots = [root for root in harness.built_in_source_roots(include_external=False)
                     if harness._protected_memory_source_allowed(root) and root.kind != 'algorithm']
            current_capabilities = {r['id']: r for r in harness._runtime_capability_records(previous={})}
            records = []
            for row in index.get('records', []):
                if not isinstance(row, dict) or row.get('harness') != 'algo-cli':
                    continue
                if row.get('kind') == 'runtime_capability':
                    if row.get('id') in current_capabilities:
                        records.append(current_capabilities[row['id']])
                    continue
                for root in roots:
                    if row.get('kind') != root.kind:
                        continue
                    path = Path(str(row.get('path', ''))).absolute()
                    try:
                        relative = path.relative_to(root.root.absolute())
                    except ValueError:
                        continue
                    if any(relative.match(pattern) for pattern in root.patterns):
                        payload = harness._read_public_source(path, max_bytes=256 * 1024)
                        records.append(harness.make_record(root, path, raw_text=payload.decode('utf-8')))
                        break
            public = {str(r.get('id')): r for r in records}
            hits = harness.search_index(query, 'algo-cli', None, 50, index={**index, 'records': records})
            omitted = 0
            for hit in hits:
                row = public.get(str(hit.get('id')))
                if row is None:
                    continue
                identity = str(row['id'])
                if len(identity.encode('utf-8')) > 256:
                    omitted += 1
                    continue
                rows.append({'id': identity, 'kind': str(row.get('kind', ''))[:32]})
            data = {'scope': 'public_product_metadata_only', 'matches': len(rows),
                    'omitted_oversized_records': omitted}
        else:
            from .decision_layer import BUILTIN_PROVIDERS, decision_status

            name = provider or getattr(cfg, 'decision_default_provider', 'jev')
            if name not in BUILTIN_PROVIDERS:
                return error('unsupported_diagnostic_provider')
            status = json.loads(decision_status(provider=name, cfg=cfg))
            item = status['providers'][name]
            inner = item.get('status', {})
            data = {'provider': name, 'provider_health': status['provider_health'],
                    'available': item.get('ok') is not False,
                    'inference': 'not_probed_by_status'}
            if item.get('ok') is False:
                data['error'] = item.get('error', 'unavailable')
            for key in ('available', 'loaded', 'local', 'ready'):
                if type(inner.get(key)) is bool:
                    data[key] = inner[key]
        end = min(len(rows), offset + limit)
        result: dict[str, Any] = {'ok': True, 'section': section, 'data': data, 'items': rows[offset:end],
                  'next_offset': end if end < len(rows) else None,
                  'action_authority': False, 'functional_verification': 'not_run'}
        while len(json.dumps(result, separators=(',', ':')).encode()) > 1700 and result['items']:
            result['items'].pop()
            end -= 1
            result['next_offset'] = end
        if len(json.dumps(result, separators=(',', ':')).encode()) > 1700:
            return error('diagnostic_metadata_exceeds_budget')
        return json.dumps(result, separators=(',', ':'))
    except (continuum_memory.ContinuumMemoryError, OSError, ValueError, TypeError, KeyError):
        return error('diagnostic_unavailable')
