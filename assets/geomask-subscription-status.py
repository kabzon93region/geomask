#!/usr/bin/env python3
"""Read-only: which subscription backs each path's group.

The panel used to derive this from the mihomo config text with a regex that expected
`  - name: ...` and a double-quoted provider URL. Applying a verified pool re-serialises
the config with PyYAML, which writes `- name: ...` and bare scalars, so every path lost
its assignment at once. A managed pool provider has no URL either, so even a tolerant
parser cannot tell which subscription feeds it: that link is recorded in the node-audit
state at apply time.

This module reads the three existing sources structurally and never writes anything:
no seed, no migration, no directories. Unknown is reported as unknown, never guessed.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys

MANAGED_PREFIX = 'audit-'
APPLIED_STATES = ('applied', 'expired', 'applying')
PATH_IDS = ('vpn', 'vpn_nop2p', 'ai')

DEFAULT_CONFIG = Path(os.environ.get('GM_SUBSTATUS_CONFIG', '/etc/mihomo/config.yaml'))
DEFAULT_STORE = Path(os.environ.get('GM_SUBSTATUS_STORE', '/etc/geomask/node-audit.json'))
DEFAULT_REGISTRY = Path(os.environ.get('GM_SUBSTATUS_REGISTRY', '/etc/geomask-subs.list'))


def _load_state_module():
    candidates = [Path(__file__).with_name('geomask-audit-state.py'),
                  Path('/usr/lib/geomask/geomask-audit-state.py')]
    for candidate in candidates:
        if candidate.exists():
            spec = importlib.util.spec_from_file_location('substatus_state', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError('audit state module not found')


_state = _load_state_module()
PATHS = _state.PATHS


def parse_registry(raw):
    """Registry rows are `id|name|url`, one per line, `#` comments allowed."""
    rows = []
    for line in raw.splitlines():
        if not line or line.startswith('#'):
            continue
        fields = line.split('|', 2)
        if len(fields) != 3 or not fields[0]:
            continue
        rows.append({'id': fields[0], 'name': fields[1], 'url': fields[2]})
    return rows


def _url_index(rows):
    index = {}
    for row in rows:
        if row['url']:
            index.setdefault(row['url'], []).append(row['id'])
    return index


def _group_providers(config, group_name):
    groups = [g for g in (config.get('proxy-groups') or [])
              if isinstance(g, dict) and g.get('name') == group_name]
    if not groups:
        return None, 'group_missing'
    if len(groups) > 1:
        return None, 'group_duplicate'
    use = groups[0].get('use')
    if not isinstance(use, list) or not use:
        return [], 'group_has_no_providers'
    if any(not isinstance(p, str) or not p for p in use):
        return None, 'group_bad_use'
    return use, None


def _managed_source(config, store, ident, group_name, provider_id, providers, index):
    """Link a managed file provider back to the subscription recorded at apply time.

    Only a record that provably describes this very provider and group is used. A stale
    or partial record must not light up somebody else's row.
    """
    applied = ((store.get('paths') or {}).get(ident) or {}).get('applied') or {}
    spec = providers.get(provider_id) or {}
    if applied.get('status') not in APPLIED_STATES:
        return None, 'managed_source_unrecorded'
    if applied.get('provider_id') != provider_id:
        return None, 'managed_source_unrecorded'
    if applied.get('group') and applied.get('group') != group_name:
        return None, 'managed_source_unrecorded'
    if spec.get('type') != 'file':
        return None, 'managed_source_unverified'
    previous = applied.get('previous_use')
    if not isinstance(previous, list) or not previous:
        return None, 'managed_source_unrecorded'
    ids, sources = [], []
    for pid in previous:
        if not isinstance(pid, str) or pid not in providers:
            return None, 'managed_source_missing'
        url = (providers.get(pid) or {}).get('url')
        if not isinstance(url, str) or not url:
            return None, 'managed_source_missing'
        sources.append(pid)
        ids.extend(index.get(url, []))
    return {'ids': list(dict.fromkeys(ids)), 'sources': sources}, None


def _resolve_one(config, store, index, ident):
    group_name = PATHS[ident][0]
    providers = config.get('proxy-providers') or {}
    use, problem = _group_providers(config, group_name)
    record = {
        'status': 'unknown',
        'subscription_ids': [],
        'actual_provider_ids': list(use or []),
        'source_provider_ids': [],
        'via': None,
        'reason_code': problem,
    }
    if use is None:
        record['status'] = 'not-configured' if problem == 'group_missing' else 'unknown'
        return record
    if not use:
        record['status'] = 'not-configured'
        return record

    ids, sources = [], []
    vias = set()
    reasons = []
    for provider_id in use:
        if provider_id.startswith(MANAGED_PREFIX):
            resolved, problem = _managed_source(config, store, ident, group_name,
                                                provider_id, providers, index)
            vias.add('managed')
        else:
            spec = providers.get(provider_id) or {}
            url = spec.get('url')
            if not isinstance(url, str) or not url:
                resolved, problem = None, 'provider_without_url'
            else:
                resolved, problem = {'ids': index.get(url, []), 'sources': [provider_id]}, None
            vias.add('direct')
        if resolved is None:
            reasons.append(problem)
            continue
        ids.extend(resolved['ids'])
        sources.extend(resolved['sources'])

    record['subscription_ids'] = list(dict.fromkeys(ids))
    record['source_provider_ids'] = list(dict.fromkeys(sources))
    record['via'] = None
    if reasons and record['subscription_ids']:
        record['status'] = 'partial'
        record['reason_code'] = reasons[0]
    elif reasons:
        record['status'] = 'unknown'
        record['reason_code'] = reasons[0]
    elif record['subscription_ids']:
        record['status'] = 'resolved'
        record['reason_code'] = None
        record['via'] = vias.pop() if len(vias) == 1 else None
    else:
        record['status'] = 'unregistered'
        record['reason_code'] = 'registry_has_no_match'
    return record


def resolve_subscription_bindings(config, store, registry_rows):
    """Pure resolver: config/state/registry in, per-path binding out."""
    index = _url_index(registry_rows)
    return {ident: _resolve_one(config, store or {}, index, ident) for ident in PATH_IDS}


def _read_snapshot(config_path, store_path, registry_path):
    def read(path):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError('unreadable_input') from exc
    return read(config_path), read(store_path), read(registry_path)


def _parse_snapshot(config_bytes, store_bytes, registry_bytes):
    if config_bytes is None:
        raise RuntimeError('config_unreadable')
    config = _state.yaml12_load(config_bytes)
    if not isinstance(config, dict):
        raise RuntimeError('config_unreadable')
    if store_bytes is None:
        store = {}
    else:
        store = _state.strict_json(store_bytes)
        if not isinstance(store, dict):
            raise RuntimeError('state_unreadable')
    registry_raw = '' if registry_bytes is None else registry_bytes.decode('utf-8', 'replace')
    bindings = resolve_subscription_bindings(config, store, parse_registry(registry_raw))
    return bindings, config, store


def _read_and_resolve(config_path=None, store_path=None, registry_path=None):
    """Read the snapshot twice when it changes underneath us; repeat instability is unknown."""
    config_path = Path(config_path or DEFAULT_CONFIG)
    store_path = Path(store_path or DEFAULT_STORE)
    registry_path = Path(registry_path or DEFAULT_REGISTRY)
    first = _read_snapshot(config_path, store_path, registry_path)
    result = _parse_snapshot(*first)
    if _read_snapshot(config_path, store_path, registry_path) != first:
        second = _read_snapshot(config_path, store_path, registry_path)
        result = _parse_snapshot(*second)
        if _read_snapshot(config_path, store_path, registry_path) != second:
            raise RuntimeError('snapshot_changed')
    return result


def read_subscription_bindings(config_path=None, store_path=None, registry_path=None):
    try:
        bindings, _, _ = _read_and_resolve(config_path, store_path, registry_path)
        return bindings
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RuntimeError) and str(exc) else 'unreadable_input'
        if reason not in ('config_unreadable', 'state_unreadable', 'snapshot_changed',
                          'unreadable_input'):
            reason = 'unreadable_input'
        return {ident: {'status': 'unknown', 'subscription_ids': [],
                        'actual_provider_ids': [], 'source_provider_ids': [],
                        'via': None, 'reason_code': reason} for ident in PATH_IDS}


def _legacy_fields(bindings):
    """Old scalar fields stay single-valued; a shared source is genuinely ambiguous there."""
    out = {}
    for ident in PATH_IDS:
        ids = bindings[ident]['subscription_ids']
        out[ident] = ids[0] if bindings[ident]['status'] == 'resolved' and len(ids) == 1 else ''
    return out


def _source_urls(bindings, config):
    urls = {}
    for ident in PATH_IDS:
        record = bindings[ident]
        url = ''
        if record['status'] == 'resolved' and len(record['source_provider_ids']) == 1:
            candidate = ((config.get('proxy-providers') or {})
                         .get(record['source_provider_ids'][0]) or {}).get('url')
            if isinstance(candidate, str):
                url = candidate
        urls[ident] = url
    return urls


def main(argv):
    if len(argv) < 2 or argv[1] not in ('status', 'ai-sub'):
        print('usage: geomask-subscription-status.py status|ai-sub', file=sys.stderr)
        return 2
    mode = argv[1]
    raw = sys.stdin.read() if mode == 'status' else ''
    if mode == 'status' and raw.strip():
        # A malformed base must not be replaced by a bindings-only payload: the panel would
        # lose every other field. Say nothing and let the caller keep its own response.
        try:
            json.loads(raw)
        except ValueError:
            print('base status is not valid JSON', file=sys.stderr)
            return 3
    try:
        bindings, config, _ = _read_and_resolve()
        failure = None
    except Exception as exc:
        bindings = read_subscription_bindings()
        config = {}
        failure = str(exc) if isinstance(exc, RuntimeError) and str(exc) else 'unreadable_input'

    if mode == 'ai-sub':
        record = bindings['ai']
        provider = (record['actual_provider_ids'] or [''])[0]
        url = ''
        if failure is None and provider:
            candidate = ((config.get('proxy-providers') or {}).get(provider) or {}).get('url')
            if isinstance(candidate, str):
                url = candidate
        print(json.dumps({'provider': provider, 'url': url,
                          'source_provider_ids': record['source_provider_ids'],
                          'source_subscription_ids': record['subscription_ids']},
                         ensure_ascii=False))
        return 0

    try:
        base = json.loads(raw) if raw.strip() else {}
    except ValueError:
        base = {}
    if not isinstance(base, dict):
        base = {}
    legacy = _legacy_fields(bindings)
    base['subscription_bindings'] = bindings
    base['vpn_sub'] = legacy['vpn']
    base['vpn_nop2p_sub'] = legacy['vpn_nop2p']
    base['ai_sub'] = legacy['ai']
    if failure is None:
        urls = _source_urls(bindings, config)
        base['vpn_url'] = urls['vpn']
        base['vpn_nop2p_url'] = urls['vpn_nop2p']
        base['ai_url'] = urls['ai']
    print(json.dumps(base, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
