#!/usr/bin/env python3
"""Apply module: managed pool compiler, preview, and apply orchestration."""
import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yaml
except ImportError:          # the panel reports a missing PyYAML instead of crashing
    yaml = None

# Load state module
STATE_MODULE_PATH = Path(__file__).with_name('geomask-audit-state.py')
_state_spec = importlib.util.spec_from_file_location('audit_state', STATE_MODULE_PATH)
_state = importlib.util.module_from_spec(_state_spec)
_state_spec.loader.exec_module(_state)

# Load config-tx module
TX_MODULE_PATH = Path(__file__).with_name('geomask-config-tx.py')
_tx_spec = importlib.util.spec_from_file_location('config_tx', TX_MODULE_PATH)
_tx = importlib.util.module_from_spec(_tx_spec)
_tx_spec.loader.exec_module(_tx)

# Paths from state module
DEFAULT_CONFIG = _state.DEFAULT_CONFIG
DEFAULT_STATE_DIR = _state.DEFAULT_STATE_DIR
DEFAULT_POOLS_DIR = _state.DEFAULT_POOLS_DIR
DEFAULT_REPORTS_DIR = _state.DEFAULT_REPORTS_DIR
DEFAULT_CONFIG = _state.DEFAULT_CONFIG

# Re-export from state module
PATHS = _state.PATHS
AuditError = _state.AuditError
stamp = _state.stamp
digest = _state.digest
encode = _state.encode
canonical_json = _state.canonical_json
strict_json = _state.strict_json
normalize_path = _state.path_id
countries = _state.countries
locked = _state.locked
atomic_write = _state.atomic_write
load_store = _state.load_store
save_store = _state.save_store
append_event = _state.append_event
get_events = _state.get_events
load_pool = _state.load_pool
save_pool = _state.save_pool
resolve_source_binding = _state.resolve_source_binding
compute_path_revision = _state.compute_path_revision

# Config-tx
start_transaction = _tx.start_transaction
load_node_snapshot = _state.load_node_snapshot

class ApplyError(Exception):
    pass

# Managed pool files must live inside the home directory mihomo is started with: the live
# daemon runs `-d /etc/mihomo`, and a provider path outside that root is rejected at parse
# time. Probed on the router: `/etc/mihomo/...` passes, `/root/.config/mihomo/...` does not.
POOL_DIR = Path(os.environ.get('GM_POOL_DIR', '/etc/mihomo/geomask-pools'))

def _require_path(value):
    """Validate a path id, reporting the failure as an ApplyError for this module."""
    try:
        return normalize_path(value)
    except AuditError as exc:
        raise ApplyError(str(exc)) from None

def _provider_id(path_id, generation):
    return 'audit-%s-g%d' % (path_id, generation)

def _fingerprint_of(definition, provider_name):
    """Recompute the audit fingerprint the same way the check did.

    The whole definition is covered, not a hand-picked subset of transport keys: a node
    whose credentials changed while its name, server and port stayed the same would
    otherwise keep the approved fingerprint.
    """
    identity = {
        'definition': definition,
        'provider_id': provider_name,
    }
    return digest(canonical_json(identity))[:16]

def _build_managed_proxies(path_id, report, eligible):
    """Return the verified definitions for the eligible nodes, refusing anything unproven.

    The snapshot holds exactly what the check measured. Every definition is re-fingerprinted
    and compared with the report, so a rewritten source cannot smuggle a different endpoint
    under an approved name.
    """
    snapshot = load_node_snapshot(path_id, report.get('job_id', ''))
    if not snapshot:
        raise ApplyError('node snapshot for this job is missing; run a fresh check')

    proxies = []
    for node in eligible:
        name = node['name']
        entry = snapshot.get(name)
        if not entry:
            raise ApplyError('node %r was not measured in this job; run a fresh check' % name)
        definition = entry.get('definition')
        provider_name = entry.get('provider')
        if not isinstance(definition, dict) or definition.get('name') != name:
            raise ApplyError('snapshot entry for %r is malformed' % name)
        expected = entry.get('node_fingerprint')
        if expected != _fingerprint_of(definition, provider_name):
            raise ApplyError('snapshot entry for %r does not match its fingerprint' % name)
        if node.get('node_fingerprint') and node['node_fingerprint'] != expected:
            raise ApplyError('node %r changed between the report and its snapshot' % name)
        proxies.append(definition)
    return proxies

def _unique_pool_names(proxies, generation):
    """Give every pooled node a name of its own.

    mihomo keys proxies by name across the whole process, so a pooled copy of a node that
    still exists in its source subscription is the same name twice. The dashboard then
    resolves the group's child to the wrong copy and renders nothing, and a delay test or a
    selection can hit a node the group does not actually use. The suffix keeps the original
    name recognisable while making the pooled instance unambiguous.
    """
    used = set()
    renamed = []
    mapping = {}
    for definition in proxies:
        original = str(definition.get('name') or '')
        candidate = '%s · g%d' % (original, generation)
        index = 2
        while candidate in used:
            candidate = '%s · g%d #%d' % (original, generation, index)
            index += 1
        used.add(candidate)
        clone = dict(definition)
        clone['name'] = candidate
        renamed.append(clone)
        mapping[original] = candidate
    return renamed, mapping


def _write_pool_file(path_id, generation, proxies):
    """Write the managed provider file; 0600 because it carries endpoint credentials."""
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    target = POOL_DIR / ('%s-g%d.yaml' % (path_id, generation))
    body = yaml.safe_dump({'proxies': proxies}, allow_unicode=True, sort_keys=False)
    atomic_write(target, body.encode('utf-8'))
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target

def _acting_countries(eligible):
    """The countries the applied pool actually reaches, from what the check measured."""
    found = set()
    for node in eligible or []:
        for probe in (node.get('probes') or {}).values():
            country = probe.get('country')
            if isinstance(country, str) and len(country) == 2 and country.isalpha():
                found.add(country.upper())
    return sorted(found)


def _managed_provider_of(config, provider_id):
    """True when this provider id was created by this module."""
    return isinstance(provider_id, str) and provider_id.startswith('audit-') \
        and provider_id in (config.get('proxy-providers') or {})


def _original_source(applied, current_use):
    """Which source a revoke must restore.

    Applying again while a managed pool is already in force must not record the managed
    provider as the thing to go back to: its generation file is pruned by that very apply,
    so a later revoke would point the group at a provider whose file no longer exists. The
    original subscription from the first apply is carried forward instead.
    """
    inherited = (applied or {}).get('previous_use') or []
    if inherited and any(str(p).startswith('audit-') for p in current_use):
        return list(inherited)
    return list(current_use)


def _prune_pool_files(path_id, keep_generation, also_keep=()):
    keep = {'%s-g%d.yaml' % (path_id, keep_generation)}
    for name in also_keep:
        keep.add(str(name))
    for old in sorted(POOL_DIR.glob(path_id + '-g*.yaml')):
        if old.name not in keep:
            try:
                old.unlink()
            except OSError:
                pass

# An applied pool is only trusted for this long after its audit.
POOL_MAX_AGE_HOURS = 24

def _read_config():
    """Read mihomo config."""
    with open(DEFAULT_CONFIG, encoding='utf-8') as f:
        return f.read()

def _write_config(text):
    """Write mihomo config atomically."""
    _tx.atomic_write(DEFAULT_CONFIG, text.encode('utf-8'))

def _reload_mihomo():
    """Reload mihomo via controller API."""
    text = open(DEFAULT_CONFIG, encoding='utf-8').read()
    secret = ''
    for line in text.splitlines():
        m = re.match(r'^secret:\s*["\']?([^"\'\s]+)', line)
        if m:
            secret = m.group(1)
            break
    
    url = 'http://127.0.0.1:9090/configs?force=true'
    req = urllib.request.Request(url,
                                data=json.dumps({'path': '/etc/mihomo/config.yaml'}).encode(),
                                method='PUT',
                                headers={'Content-Type': 'application/json'})
    if secret:
        req.add_header('Authorization', 'Bearer ' + secret)
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception as e:
        raise ApplyError(f'mihomo reload failed: {e}')

# ============================================================================
# POOL COMPILER
# ============================================================================

def _compile_eligible_pool(path_id, report, countries_allow):
    """Compile eligible pool from report based on countries_allow."""
    eligible_nodes = []
    excluded_nodes = []
    unknown_nodes = []
    
    for node in report.get('nodes', []):
        node_name = node.get('name', '')
        node_status = node.get('status', 'unknown')
        provider = node.get('provider', '')
        node_fp = node.get('node_fingerprint', '')
        
        if node_status == 'eligible':
            eligible_nodes.append({
                'name': node_name,
                'provider': provider,
                'node_fingerprint': node_fp,
                # The measurements travel with the entry: the applied record reports the
                # countries the pool actually reaches, and that is derived from these.
                'probes': node.get('probes') or {},
            })
        elif node_status == 'excluded':
            excluded_nodes.append({
                'name': node_name,
                'provider': provider,
                'reason': node.get('reasons', []),
            })
        else:
            unknown_nodes.append({
                'name': node_name,
                'provider': provider,
                'reason': node.get('reasons', []),
            })
    
    return {
        'eligible': eligible_nodes,
        'excluded': excluded_nodes,
        'unknown': unknown_nodes,
        'countries_allow': countries_allow,
        'total_eligible': len(eligible_nodes),
        'total_excluded': len(excluded_nodes),
        'total_unknown': len(unknown_nodes),
    }


def _preview_token(path_id, report, pool, countries_allow):
    """Deterministic identity of one exact preview: path + audited job + approved set."""
    material = {
        'path_id': path_id,
        'job_id': report.get('job_id', ''),
        'source_revision': report.get('source_revision', ''),
        'countries_allow': countries_allow,
        'eligible': sorted(n['name'] for n in pool['eligible']),
        'fingerprints': sorted(n.get('node_fingerprint', '') for n in pool['eligible']),
    }
    return digest(canonical_json(material))

def _hosts_regex(names):
    """Anchored alternation matching exactly these node names, nothing else."""
    return '^(' + '|'.join(re.escape(n) for n in names) + ')$'

def preview_apply(path_id, report_id, expected_path_revision, expected_source_revision):
    """Generate preview of what apply would do."""
    path_id = _require_path(path_id)
    data, _ = load_store()
    path_data = data['paths'].get(path_id, {})

    if path_data.get('path_revision') != expected_path_revision:
        raise ApplyError('STALE: path revision changed; reload before previewing')

    report_path = DEFAULT_STATE_DIR / f'{path_id}.json'
    if not report_path.exists():
        raise ApplyError('Report not found; run a check first')

    report = strict_json(report_path.read_bytes())

    if report.get('status') != 'complete':
        raise ApplyError('Report not complete; run a fresh check')
    if report.get('stale'):
        raise ApplyError('Report is stale; run a fresh check')

    # Staleness must be derived here, not trusted from the caller: a caller that echoes
    # the report's own revisions back would otherwise make a dead report look current.
    if report.get('settings_revision') != path_data.get('path_revision'):
        raise ApplyError('STALE: path settings changed since the check; check again')
    live_raw = DEFAULT_CONFIG.read_bytes()
    live_config_text = live_raw.decode('utf-8')
    if report.get('source_revision') != digest(live_raw):
        raise ApplyError('STALE: mihomo config changed since the check; check again')

    if report.get('job_id') != report_id:
        raise ApplyError('Report ID mismatch; run a fresh check')
    if report.get('source_revision') != expected_source_revision:
        raise ApplyError('STALE: subscription changed since the check')

    config = yaml_load(live_config_text)
    group_name = PATHS[path_id][0]
    current_group = None
    for g in config.get('proxy-groups', []):
        if g.get('name') == group_name:
            current_group = g
            break
    if current_group is None:
        raise ApplyError(f'group {group_name} not found in mihomo config')

    countries_allow = path_data.get('countries_allow', PATHS[path_id][1])
    pool = _compile_eligible_pool(path_id, report, countries_allow)

    if not pool['eligible']:
        raise ApplyError('No eligible nodes: refusing to apply an empty pool (no DIRECT fallback)')

    # Current effective membership is whatever the provider + filter already yields.
    current_nodes = set(current_group.get('proxies') or [])
    eligible_names = {n['name'] for n in pool['eligible']}
    excluded_names = {n['name'] for n in pool['excluded']}
    unknown_names = {n['name'] for n in pool['unknown']}

    warnings = []
    mechanism = 'managed-file-provider'
    snapshot_ready = False
    try:
        snapshot_ready = bool(load_node_snapshot(path_id, report.get('job_id', '')))
    except AuditError:
        snapshot_ready = False
    if not snapshot_ready:
        mechanism = 'unavailable-no-snapshot'
        warnings.append('нет снимка измеренных узлов для этой проверки: нужна новая проверка, '
                        'иначе пул нельзя собрать из подтверждённых определений')
    else:
        # Verify the snapshot here as well, not only inside apply: a snapshot that no longer
        # matches the report makes apply refuse, and preview must not promise a pool that
        # apply will then reject.
        try:
            _build_managed_proxies(path_id, report, pool['eligible'])
        except ApplyError as exc:
            raise ApplyError('снимок узлов не совпадает с отчётом (%s); нужна новая проверка'
                             % exc) from None
    if not current_group.get('use'):
        warnings.append('группа не привязана к провайдеру: применение будет отклонено, пока это не исправлено')
    if unknown_names:
        warnings.append('%d узл(ов) с неизвестной географией будут исключены' % len(unknown_names))
    restrictions = []
    if current_group.get('filter'):
        restrictions.append('filter')
    if current_group.get('exclude-filter'):
        restrictions.append('exclude-filter')
    if restrictions:
        warnings.append('в группе есть унаследованный %s; на время действия управляемого пула он '
                        'приостанавливается (пул уже содержит только одобренные узлы) и '
                        'восстанавливается при отзыве' % ' и '.join(restrictions))
    warnings.append('применение заменяет источник группы управляемым файл-провайдером только с '
                    'проверенными определениями; исходная подписка не меняется и снова '
                    'используется при следующей проверке')

    return {
        'ok': True,
        'preview_id': _preview_token(path_id, report, pool, countries_allow),
        'path_id': path_id,
        'group': group_name,
        'mechanism': mechanism,
        'current_nodes': sorted(current_nodes),
        'proposed_nodes': sorted(eligible_names),
        'to_add': sorted(eligible_names - current_nodes),
        'to_remove': sorted(current_nodes & (excluded_names | unknown_names)),
        'excluded_from_pool': sorted(excluded_names | unknown_names),
        'eligible_count': len(pool['eligible']),
        'excluded_count': len(pool['excluded']),
        'unknown_count': len(pool['unknown']),
        'current_node': current_group.get('now'),
        'previous_filter': current_group.get('filter', ''),
        'previous_exclude_filter': current_group.get('exclude-filter', ''),
        'previous_use': list(current_group.get('use') or []),
        'warnings': warnings,
    }

def _restore_applied_record(path_id, previous):
    """Put the applied record back after a refused transaction."""
    try:
        data, _ = load_store()
        if previous:
            data.setdefault('paths', {}).setdefault(path_id, {})['applied'] = previous
        else:
            data.setdefault('paths', {}).setdefault(path_id, {}).pop('applied', None)
        save_store(data)
    except Exception:
        # A failure to restore the bookkeeping must not mask the original error.
        pass


def apply_preview(path_id, preview_id, expected_path_revision):
    """Apply a previewed pool by pinning the target group to exactly those nodes."""
    path_id = _require_path(path_id)
    data, _ = load_store()
    path_data = data['paths'].get(path_id, {})
    previous_applied = path_data.get('applied')

    if path_data.get('path_revision') != expected_path_revision:
        raise ApplyError('STALE: path revision changed; reload before applying')
    report_path = DEFAULT_STATE_DIR / f'{path_id}.json'
    if not report_path.exists():
        raise ApplyError('Report not found; run a check first')
    report = strict_json(report_path.read_bytes())

    if report.get('status') != 'complete' or report.get('stale'):
        raise ApplyError('Report is stale or incomplete; run a fresh check')

    # Settings and config can both move after the check; derive, do not trust.
    if report.get('settings_revision') != path_data.get('path_revision'):
        raise ApplyError('STALE: path settings changed since the check; check again')
    live_raw = DEFAULT_CONFIG.read_bytes()
    live_config = live_raw.decode('utf-8')
    if report.get('source_revision') != digest(live_raw):
        raise ApplyError('STALE: mihomo config changed since the check; check again')

    countries_allow = path_data.get('countries_allow', PATHS[path_id][1])
    pool = _compile_eligible_pool(path_id, report, countries_allow)

    if not pool['eligible']:
        raise ApplyError('No eligible nodes: refusing to apply an empty pool (no DIRECT fallback)')
    if _preview_token(path_id, report, pool, countries_allow) != preview_id:
        raise ApplyError('STALE: preview no longer matches the audited result; preview again')

    config = yaml_load(live_config)
    group_name = PATHS[path_id][0]
    target = None
    for g in config.get('proxy-groups', []):
        if g.get('name') == group_name:
            target = g
            break
    if target is None:
        raise ApplyError(f'group {group_name} not found in mihomo config')

    applied_before = path_data.get('applied') or {}
    previous_filter = target.get('filter', '')
    previous_use = _original_source(applied_before, list(target.get('use') or []))
    if not previous_use:
        raise ApplyError('group %s is not provider-backed; refusing to guess its source'
                         % group_name)
    # An already-managed generation is replaced, not chained: its provider entry and file
    # are dropped once the new one is verified, otherwise the group would collect dead
    # providers and revoke could point at a pruned file.
    stale_provider = applied_before.get('provider_id') or ''

    generation = int(path_data.get('applied', {}).get('generation', 0)) + 1
    provider_id = _provider_id(path_id, generation)
    proxies = _build_managed_proxies(path_id, report, pool['eligible'])
    # Unique names: the same node also exists in its source subscription, and mihomo keys
    # proxies by name process-wide (see _unique_pool_names).
    proxies, renames = _unique_pool_names(proxies, generation)
    eligible_names = [p['name'] for p in proxies]

    # The pool file is written before the transaction so `mihomo -t` can actually load it;
    # it is removed again if the transaction refuses the candidate.
    pool_path = _write_pool_file(path_id, generation, proxies)

    config.setdefault('proxy-providers', {})[provider_id] = {
        'type': 'file',
        'path': str(pool_path),
    }
    if stale_provider and stale_provider != provider_id:
        (config.get('proxy-providers') or {}).pop(stale_provider, None)
    target['use'] = [provider_id]
    target.pop('filter', None)
    previous_exclude = target.pop('exclude-filter', '')

    audit = {
        'generation': generation,
        'path_revision': expected_path_revision,
        'preview_id': preview_id,
        'job_id': report.get('job_id', ''),
        'source_revision': report.get('source_revision', ''),
        'applied_at': stamp(),
        'runtime_verified': False,
        'pool_digest': digest(canonical_json(sorted(
            (n['name'], n.get('node_fingerprint', '')) for n in pool['eligible']))),
        'status': 'applying',
        'node_count': len(eligible_names),
        # What the pool really reaches, so the panel can show desired against acting.
        'acting_countries': _acting_countries(pool['eligible']),
        'previous_filter': previous_filter,
        'previous_exclude_filter': previous_exclude,
        'previous_use': previous_use,
        'provider_id': provider_id,
        'pool_file': str(pool_path),
        'group': group_name,
        # original report name -> pooled name, so the panel and later readers can map back
        'renames': renames,
    }

    try:
        # Record the in-flight apply before the transaction starts. A crash between the
        # commit and the bookkeeping used to leave no trace that the group might already be
        # pinned to a managed provider, so nobody could tell a clean group from a half-done
        # apply. Everything needed for a manual restore is in this record.
        intent_data, _ = load_store()
        intent_data.setdefault('paths', {}).setdefault(path_id, {})['applied'] = audit
        save_store(intent_data)

        result = start_transaction(
            {str(DEFAULT_CONFIG): yaml.safe_dump(config, allow_unicode=True,
                                                 sort_keys=False).encode('utf-8')},
            require_exactly={group_name: eligible_names})
    except Exception:
        _restore_applied_record(path_id, previous_applied)
        try:
            pool_path.unlink()
        except OSError:
            pass
        raise
    if not result.get('ok'):
        _restore_applied_record(path_id, previous_applied)
        try:
            pool_path.unlink()
        except OSError:
            pass
        raise ApplyError('transaction rejected the candidate; config left unchanged')

    # Nothing points at the replaced generation any more: previous_use carries the original
    # subscription forward, so its file and provider entry are dropped here.
    _prune_pool_files(path_id, generation)

    data, _ = load_store()
    data['paths'][path_id]['applied'] = dict(audit, status='applied', runtime_verified=True,
                                             applied_at=stamp())
    save_store(data)
    append_event({
        'path': path_id, 'actor': 'luci', 'action': 'apply',
        'preview_id': preview_id, 'job_id': audit['job_id'],
        'node_count': len(eligible_names), 'result': 'success',
    })
    return {
        'ok': True, 'applied': True, 'path_id': path_id,
        'tx_id': result.get('tx_id'), 'config_digest': result.get('config_digest'),
        'node_count': len(eligible_names), 'pool_digest': audit['pool_digest'],
    }

def apply_status(path_id):
    """Report the applied state honestly, including staleness of the evidence."""
    path_id = _require_path(path_id)
    data, _ = load_store()
    applied = data['paths'].get(path_id, {}).get('applied', {})

    status = applied.get('status', 'never')
    if status == 'applying':
        # The record is written before the transaction, so this state means either an apply
        # in flight or one that died between commit and bookkeeping. The group may already
        # point at the managed provider, so never claim the path is unrestricted here.
        return {
            'ok': True,
            'path_id': path_id,
            'applied': False,
            'filter_in_force': True,
            'evidence_age_hours': None,
            'applied_detail': dict(applied, status='applying',
                                   warning='применение не завершено; проверьте пул и при '
                                           'необходимости отзовите фильтр'),
        }
    if status in ('applied', 'expired'):
        applied_at = applied.get('applied_at', '')
        age_hours = None
        if applied_at:
            try:
                applied_time = datetime.fromisoformat(applied_at.replace('Z', '+00:00'))
                age_hours = (datetime.now(applied_time.tzinfo) - applied_time).total_seconds() / 3600.0
            except ValueError:
                pass
        stale = age_hours is not None and age_hours > POOL_MAX_AGE_HOURS
        # A stale pool is still in force: the filter was never removed. Say so.
        in_force = status == 'applied' and not stale
        detail = dict(applied)
        if stale:
            detail['status'] = 'expired'
            detail['warning'] = ('Filter is still in force but its evidence is older than %dh; '
                                 'run a fresh check and re-apply. The pool was not widened.'
                                 % POOL_MAX_AGE_HOURS)
        return {
            'ok': True,
            'path_id': path_id,
            'applied': in_force,
            'filter_in_force': status in ('applied', 'expired'),
            'evidence_age_hours': None if age_hours is None else round(age_hours, 1),
            'applied_detail': detail,
        }

    return {
        'ok': True,
        'path_id': path_id,
        'applied': False,
        'filter_in_force': False,
        'evidence_age_hours': None,
        'applied_detail': applied,
    }

def revoke_applied_pool(path_id):
    """Drop our applied pool filter and restore the group's previous filter."""
    path_id = _require_path(path_id)
    data, _ = load_store()
    applied = data['paths'].get(path_id, {}).get('applied', {})

    if applied.get('status') not in ('applied', 'expired'):
        return {'ok': True, 'revoked': False, 'message': 'No applied pool to revoke'}

    config = yaml_load(open(DEFAULT_CONFIG, encoding='utf-8').read())
    group_name = PATHS[path_id][0]
    target = None
    for g in config.get('proxy-groups', []):
        if g.get('name') == group_name:
            target = g
            break
    if target is None:
        raise ApplyError(f'group {group_name} not found in mihomo config')

    # Restore the group's original source and filter, and drop the managed provider.
    previous_use = applied.get('previous_use') or []
    provider_id = applied.get('provider_id') or ''
    if previous_use:
        target['use'] = list(previous_use)
    elif provider_id:
        target.pop('use', None)
    previous = applied.get('previous_filter') or ''
    if previous:
        target['filter'] = previous
    else:
        target.pop('filter', None)
    previous_exclude = applied.get('previous_exclude_filter') or ''
    if previous_exclude:
        target['exclude-filter'] = previous_exclude
    else:
        target.pop('exclude-filter', None)
    providers = config.get('proxy-providers') or {}
    if provider_id and provider_id in providers:
        providers.pop(provider_id)
    pool_file = applied.get('pool_file')

    # Widening is the point of a revoke, so an empty result here is not a failure mode
    # to guard against: the previous source and filter are restored as recorded.
    result = start_transaction({str(DEFAULT_CONFIG): yaml.safe_dump(
        config, allow_unicode=True, sort_keys=False).encode('utf-8')})
    if not result.get('ok'):
        raise ApplyError('transaction rejected the candidate; group left as it was')

    if pool_file:
        try:
            Path(pool_file).unlink()
        except OSError:
            pass

    data, _ = load_store()
    data['paths'][path_id]['applied'] = {
        'generation': int(applied.get('generation', 0)) + 1,
        'path_revision': data['paths'][path_id].get('path_revision', ''),
        'preview_id': '',
        'job_id': '',
        'source_revision': '',
        'applied_at': stamp(),
        'runtime_verified': False,
        'pool_digest': '',
        'status': 'revoked',
        'group': group_name,
    }
    save_store(data)
    append_event({'path': path_id, 'actor': 'luci', 'action': 'revoke', 'result': 'success'})
    return {'ok': True, 'revoked': True, 'path_id': path_id,
            'restored_filter': previous, 'tx_id': result.get('tx_id')}

def handle_expired_pools():
    """Mark stale pools as expired without widening them.

    Expiry means the evidence is old, not that the exclusion is void. Dropping the pinned
    filter here would return the group to the full provider pool, including nodes proven
    to sit in a disallowed country -- a fail-open. So expiry only records the state and
    raises the flag; widening stays an explicit human action through revoke.
    """
    data, _ = load_store()
    expired_any = False
    changed = False
    for ident, path_data in data.get('paths', {}).items():
        applied = path_data.get('applied', {})
        if applied.get('status') != 'applied':
            continue
        applied_at = applied.get('applied_at', '')
        if not applied_at:
            continue
        try:
            applied_time = datetime.fromisoformat(applied_at.replace('Z', '+00:00'))
        except ValueError:
            continue
        if datetime.now(applied_time.tzinfo) - applied_time <= timedelta(hours=POOL_MAX_AGE_HOURS):
            continue
        data['paths'][ident]['applied'] = dict(
            applied, status='expired',
            expired_at=stamp(),
            width_preserved=True)
        changed = True
        expired_any = True
        append_event({'path': ident, 'actor': 'system', 'action': 'expire',
                      'result': 'success',
                      'detail': 'filter kept; pool not widened, re-check required'})
    if changed:
        save_store(data)
    return {'ok': True, 'expired_any': expired_any, 'widened': False}


def yaml_load(raw):
    if yaml is None:
        raise ApplyError('PyYAML is not installed on the router')
    try:
        value = _state.yaml12_load(raw)
        if not isinstance(value, dict):
            raise ApplyError('expected YAML mapping')
        return value
    except yaml.YAMLError:
        raise ApplyError('invalid YAML source') from None


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['preview', 'apply', 'revoke', 'status', 'expire'])
    parser.add_argument('args', nargs='*')
    parsed = parser.parse_args()
    try:
        if parsed.command == 'preview' and len(parsed.args) == 4:
            value = preview_apply(parsed.args[0], parsed.args[1], parsed.args[2], parsed.args[3])
        elif parsed.command == 'apply' and len(parsed.args) == 3:
            value = apply_preview(parsed.args[0], parsed.args[1], parsed.args[2])
        elif parsed.command == 'revoke' and len(parsed.args) == 1:
            value = revoke_applied_pool(parsed.args[0])
        elif parsed.command == 'status' and len(parsed.args) == 1:
            value = apply_status(parsed.args[0])
        elif parsed.command == 'expire' and not parsed.args:
            value = handle_expired_pools()
        else:
            raise ApplyError('invalid command arguments')
    except Exception as exc:
        value = {'ok': False, 'applied': False,
                 'error': str(exc) if isinstance(exc, (ApplyError, AuditError)) else type(exc).__name__}
    print(json.dumps(value, ensure_ascii=False))
    raise SystemExit(0 if value.get('ok') else 1)
