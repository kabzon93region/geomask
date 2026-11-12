#!/usr/bin/env python3
"""Schema v2 state module for node-audit: independent path revisions, source bindings, events."""
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

# Canonical paths (defaults, can be overridden by passing explicit paths to functions)
DEFAULT_STORE = Path(os.environ.get('GM_AUDIT_STORE', '/etc/geomask/node-audit.json'))
DEFAULT_EVENTS = Path(os.environ.get('GM_AUDIT_EVENTS', '/etc/geomask/node-audit/events.jsonl'))
DEFAULT_STATE_DIR = Path(os.environ.get('GM_AUDIT_STATE', '/etc/geomask/node-audit/state'))
DEFAULT_POOLS_DIR = Path(os.environ.get('GM_AUDIT_POOLS', '/etc/geomask/node-audit/pools'))
DEFAULT_REPORTS_DIR = Path(os.environ.get('GM_AUDIT_REPORTS', '/etc/geomask/node-audit/reports'))
DEFAULT_SNAPSHOTS_DIR = Path(os.environ.get('GM_AUDIT_SNAPSHOTS', '/etc/geomask/node-snapshots'))
DEFAULT_CONFIG = Path(os.environ.get('GM_AUDIT_CONFIG', '/etc/mihomo/config.yaml'))
DEFAULT_REGISTRY = Path(os.environ.get('GM_AUDIT_REGISTRY', '/etc/geomask-subs.list'))

# Retention limits
MAX_EVENTS_PER_PATH = 20
MAX_EVENTS_TOTAL = 200
MAX_EVENTS_SIZE = 2 * 1024 * 1024  # 2 MiB

# Immutable report retention
MAX_REPORTS_PER_PATH = 20
MAX_REPORTS_TOTAL = 100
MAX_REPORTS_SIZE = 10 * 1024 * 1024  # 10 MiB

# Audited node definitions are private material: root-only, one file per job, pruned.
MAX_SNAPSHOTS_PER_PATH = 4
# Node definitions carry endpoint credentials, so they share one bounded budget. Exhausting
# it must fail the new operation rather than delete material an apply may still need.
MAX_SNAPSHOTS_BYTES = int(os.environ.get('GM_AUDIT_SNAPSHOT_BUDGET', 8 * 1024 * 1024))


SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')

def _job_token(job_id):
    """Validate a job id before it becomes part of a file name."""
    if not isinstance(job_id, str) or not SAFE_ID.fullmatch(job_id):
        raise AuditError('invalid job id')
    return job_id

def save_node_snapshot(ident, job_id, definitions, snapshots_dir=None, protect=()):
    """Persist exactly the node definitions a job measured.

    An apply must build its managed pool from measured definitions, not from a source
    that may have moved since. Keeping this separate from the report also keeps
    subscription payloads out of anything the panel or an export would read.

    The snapshots share one byte budget. When it is exhausted the new snapshot is refused
    and the caller fails loudly: silently deleting an older snapshot could pull the
    definitions out from under a pending preview or an applied pool.
    """
    if not job_id or not definitions:
        return None
    job_id = _job_token(job_id)
    directory = Path(snapshots_dir) if snapshots_dir else DEFAULT_SNAPSHOTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    payload = {'path_id': ident, 'job_id': job_id, 'created': stamp(), 'nodes': definitions}
    target = directory / ('%s-%s.json' % (ident, job_id))
    body = encode(payload)
    used = 0
    for existing in directory.glob('*.json'):
        if existing.name != target.name:
            try:
                used += existing.stat().st_size
            except OSError:
                pass
    if used + len(body) > MAX_SNAPSHOTS_BYTES:
        raise AuditError('node snapshot budget exhausted (%d of %d bytes); '
                         'delete an old job or raise GM_AUDIT_SNAPSHOT_BUDGET'
                         % (used, MAX_SNAPSHOTS_BYTES))
    atomic_write(target, body)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    # Retention must not pull the definitions out from under a record that still points at
    # them: an applied manifest or a report a preview can still use keeps its snapshot.
    protected = {str(job) for job in (protect or ()) if job}
    kept = sorted(directory.glob(ident + '-*.json'), key=lambda p: p.stat().st_mtime)
    for old in kept[:-MAX_SNAPSHOTS_PER_PATH]:
        if old.stem in {('%s-%s' % (ident, job)) for job in protected}:
            continue
        try:
            old.unlink()
        except OSError:
            pass
    return target


def load_node_snapshot(ident, job_id, snapshots_dir=None):
    """Read a job's audited definitions, or None when there is no snapshot."""
    job_id = _job_token(job_id)
    directory = Path(snapshots_dir) if snapshots_dir else DEFAULT_SNAPSHOTS_DIR
    target = directory / ('%s-%s.json' % (ident, job_id))
    if not target.is_file():
        return None
    data = strict_json(target.read_bytes())
    if data.get('path_id') != ident or data.get('job_id') != job_id:
        raise AuditError('snapshot does not belong to this job')
    nodes = data.get('nodes')
    if not isinstance(nodes, dict):
        raise AuditError('malformed node snapshot')
    return nodes

# Node limits (from original module)
MAX_NODES = 100

PATHS = {
    'vpn': ('VPN', ['NL', 'DE', 'FR', 'SE']),
    'vpn_nop2p': ('VPN-noP2P', ['DE', 'PL']),
    'ai': ('AI', ['US'])
}

_lock = threading.Lock()

class AuditError(Exception):
    pass

def stamp():
    return datetime.datetime.now().astimezone().isoformat()

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def canonical_json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()

def encode(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()

def strict_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AuditError('duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise AuditError('invalid JSON') from None

def path_id(value: str) -> str:
    if value not in PATHS:
        raise AuditError('invalid path')
    return value


class _Yaml12Loader:
    """Marker type; the real class is built lazily by yaml12_loader()."""


_YAML12 = None

def yaml12_loader():
    """A PyYAML loader that follows YAML 1.2 booleans, which is what mihomo uses.

    PyYAML implements YAML 1.1, where `off`, `on`, `yes` and `no` are booleans. mihomo
    writes `find-process-mode: off` and reads it as the string "off". Loading that with
    the default resolver turns it into False, and dumping the config back produces
    `false`, which mihomo rejects with "invalid find process mode" -- a round-trip through
    the config silently broke it. This loader keeps only true/false as booleans.
    """
    global _YAML12
    if _YAML12 is not None:
        return _YAML12
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    resolutions = {}
    for first, rules in yaml.SafeLoader.yaml_implicit_resolvers.items():
        kept = [(tag, regexp) for tag, regexp in rules
                if tag not in ('tag:yaml.org,2002:bool', 'tag:yaml.org,2002:timestamp')]
        if kept:
            resolutions[first] = kept
    Loader.yaml_implicit_resolvers = resolutions
    bool_re = re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$')
    for first in 'tTfF':
        Loader.add_implicit_resolver('tag:yaml.org,2002:bool', bool_re, [first])
    _YAML12 = Loader
    return Loader

def yaml12_load(raw):
    """Parse YAML with YAML 1.2 boolean semantics."""
    import yaml
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode('utf-8')
    return yaml.load(raw, Loader=yaml12_loader())

def countries(value: str) -> list:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z]{2}(?:,[A-Z]{2})*', value):
        raise AuditError('countries must be nonempty ISO-2 codes: NL,DE')
    values = value.split(',')
    if len(values) != len(set(values)):
        raise AuditError('duplicate country')
    iso = set('AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW'.split())
    if not set(values) <= iso:
        raise AuditError('unknown ISO country')
    return sorted(values)

@contextlib.contextmanager
def locked(name: str, state_dir: Path = None, nonblocking: bool = False):
    """Advisory lock.

    Unix uses flock on a persistent inode, so a killed holder releases the lock
    automatically and a slow writer cannot block a reader by leaving a file behind.
    Where flock is unavailable the lock degrades to an O_EXCL file, which is the best
    that platform offers; that path is only used on non-routers (tests, Windows).
    """
    if state_dir is None:
        state_dir = DEFAULT_STATE_DIR
    lock_path = state_dir / f'.lock-{name}'
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import fcntl
    except ImportError:
        fcntl = None

    if fcntl is not None:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError:
                raise AuditError(f'busy: {name} lock held') from None
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        return

    fd = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        yield
    except FileExistsError:
        raise AuditError(f'busy: {name} lock held')
    finally:
        if fd is not None:
            try:
                os.unlink(lock_path)
            except OSError:
                pass

def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix=f'.{path.name}-', delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    try:
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

def ensure_dirs(store: Path = None, events: Path = None, state_dir: Path = None, pools_dir: Path = None):
    if store: store.parent.mkdir(parents=True, exist_ok=True)
    if events: events.parent.mkdir(parents=True, exist_ok=True)
    if state_dir: state_dir.mkdir(parents=True, exist_ok=True)
    if pools_dir: pools_dir.mkdir(parents=True, exist_ok=True)

def load_store(store: Path = None):
    """Load v2 store; migrate from v1 if needed. Returns (data, original_raw_for_revision)."""
    store = store or DEFAULT_STORE
    ensure_dirs(store=store)
    raw = store.read_bytes() if store.exists() else b''
    if not raw:
        if store.exists():
            raise AuditError('empty settings; refusing defaults reset')
        return create_default_v2(), b''
    data = strict_json(raw)
    if not isinstance(data, dict):
        raise AuditError('unsupported settings schema')
    schema = data.get('schema')
    # Explicitly reject boolean schema (True == 1 in Python but bool is not int)
    if type(schema) is not int:
        raise AuditError('schema must be integer')
    if schema == 1:
        # Validate v1 structure before migration
        if not isinstance(data.get('generation'), int):
            raise AuditError('invalid generation')
        paths_v1 = data.get('paths')
        if not isinstance(paths_v1, dict):
            raise AuditError('invalid paths structure')
        for ident, path_data in paths_v1.items():
            if not isinstance(path_data, dict):
                raise AuditError('invalid path data')
            countries_allow = path_data.get('countries_allow')
            if countries_allow is not None:
                if not isinstance(countries_allow, list):
                    raise AuditError('countries_allow must be a list')
                for c in countries_allow:
                    if not isinstance(c, str):
                        raise AuditError('country entries must be strings')
        # Migrate but don't auto-write; return migrated data and original raw for revision
        migrated = migrate_v1_to_v2(data)
        return migrated, raw
    if schema != 2:
        raise AuditError('unsupported settings schema')
    # Ensure all paths exist with v2 structure
    for ident, (label, defaults) in PATHS.items():
        if ident not in data.get('paths', {}):
            data.setdefault('paths', {})[ident] = create_path_v2(ident, defaults)
    return data, raw

def create_default_v2():
    return {
        'schema': 2,
        'generation': 1,
        'paths': {ident: create_path_v2(ident, defaults, configured=False) for ident, (label, defaults) in PATHS.items()},
        'events': [],
        'event_index': {'next_id': 1, 'truncated_before': None}
    }

def create_path_v2(ident: str, defaults: list, configured: bool = False) -> dict:
    path_data = {
        'countries_allow': list(defaults),
        'path_generation': 1,
        'path_revision': '',
        'source_binding': None,
        'configured': configured,
        'schedule': {'enabled': False, 'time': '03:30', 'mode': 'check-only'},
        'applied': {
            'generation': 0,
            'path_revision': '',
            'preview_id': '',
            'job_id': '',
            'applied_at': '',
            'runtime_verified': False,
            'pool_digest': '',
            'status': 'never'
        }
    }
    path_data['path_revision'] = compute_path_revision(path_data)
    return path_data

def compute_path_revision(path_data: dict) -> str:
    """Compute revision from semantic content only: countries + source_binding."""
    content = {
        'countries_allow': path_data.get('countries_allow', []),
        'source_binding': path_data.get('source_binding')
    }
    # Only include source_binding if it's not None to avoid hashing null
    if content['source_binding'] is None:
        content.pop('source_binding')
    return digest(canonical_json(content))

def migrate_v1_to_v2(v1_data: dict) -> dict:
    """Migrate schema v1 to v2, preserving user's chosen countries."""
    paths_v1 = v1_data.get('paths', {})
    v2 = {
        'schema': 2,
        'generation': 1,
        'paths': {},
        'events': [],
        'event_index': {'next_id': 1, 'truncated_before': None}
    }
    max_gen = 0
    for ident, (label, defaults) in PATHS.items():
        v1_path = paths_v1.get(ident, {})
        countries_allow = v1_path.get('countries_allow', defaults)
        # Ensure consistent sorted order
        countries_allow = sorted(countries_allow)
        # Path is configured if it was explicitly in v1 data
        configured = ident in paths_v1
        path_data = create_path_v2(ident, countries_allow, configured=configured)
        path_data['path_revision'] = compute_path_revision(path_data)
        path_data['path_generation'] = 1
        v2['paths'][ident] = path_data
        max_gen = max(max_gen, 1)
    # Preserve original generation from v1, or start at 1
    original_gen = v1_data.get('generation', 1)
    if isinstance(original_gen, int) and original_gen > 0:
        v2['generation'] = original_gen
    else:
        v2['generation'] = max_gen + 1
    # Do NOT auto-write; caller (save_store) will persist when appropriate
    return v2

def save_store(data: dict, store: Path = None):
    """Atomic write of entire store under lock."""
    store = store or DEFAULT_STORE
    with locked('store', store.parent / 'state' if store else None):
        # Re-read and recompute only changed path_revision
        current, _ = load_store(store)
        for ident in PATHS:
            if ident in data.get('paths', {}):
                path_data = data['paths'][ident]
                # Only recompute revision if countries or source_binding changed
                current_path = current['paths'].get(ident, {})
                if (path_data.get('countries_allow') != current_path.get('countries_allow') or
                    path_data.get('source_binding') != current_path.get('source_binding')):
                    path_data['path_revision'] = compute_path_revision(path_data)
                    path_data['path_generation'] = current_path.get('path_generation', 0) + 1
                else:
                    path_data['path_revision'] = current_path.get('path_revision', '')
                    path_data['path_generation'] = current_path.get('path_generation', 1)
        # Increment global generation on each save (legacy behavior)
        current_gen = data.get('generation', 0)
        data['generation'] = current_gen + 1 if current_gen > 0 else 1
        atomic_write(store, encode(data))

def append_event(event: dict, events: Path = None):
    """Append sanitized event to JSONL; maintain bounded retention."""
    events = events or DEFAULT_EVENTS
    with locked('events', events.parent / 'state' if events else None):
        ensure_dirs(events=events)
        ts = stamp()
        event['timestamp'] = ts
        event['event_id'] = f"{ts}-{hashlib.md5(canonical_json(event)).hexdigest()[:8]}"
        line = json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n'
        # Append
        with open(events, 'ab') as f:
            f.write(line.encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        # Trim if needed
        trim_events(events)

def trim_events(events: Path = None):
    """Keep last MAX_EVENTS_PER_PATH per path and MAX_EVENTS_TOTAL / MAX_EVENTS_SIZE."""
    events = events or DEFAULT_EVENTS
    if not events.exists():
        return
    lines = events.read_text(encoding='utf-8').splitlines()
    # Always apply per-path retention
    by_path = {}
    for line in lines:
        try:
            ev = json.loads(line)
            p = ev.get('path', '_')
            by_path.setdefault(p, []).append(line)
        except Exception:
            by_path.setdefault('_', []).append(line)
    kept = []
    for p, ev_lines in by_path.items():
        kept.extend(ev_lines[-MAX_EVENTS_PER_PATH:])
    # Grouping by path reorders the file; restore chronological order, because the reader
    # walks the file backwards and treats it as newest-last.
    def sort_key(line):
        try:
            event = json.loads(line)
        except Exception:
            return ''
        return str(event.get('timestamp') or event.get('event_id') or '')
    kept.sort(key=sort_key)
    # Global count cap
    if len(kept) > MAX_EVENTS_TOTAL:
        kept = kept[-MAX_EVENTS_TOTAL:]
    # Size cap
    total = sum(len(l.encode('utf-8')) for l in kept)
    while total > MAX_EVENTS_SIZE and kept:
        removed = kept.pop(0)
        total -= len(removed.encode('utf-8'))
    atomic_write(events, '\n'.join(kept).encode('utf-8') + (b'\n' if kept else b''))

def get_events(path: str = None, before: str = None, limit: int = 50, events: Path = None) -> list:
    """Paginated event query."""
    events = events or DEFAULT_EVENTS
    if not events.exists():
        return []
    lines = events.read_text(encoding='utf-8').splitlines()
    result = []
    for line in reversed(lines):
        try:
            ev = json.loads(line)
            if path and ev.get('path') != path:
                continue
            if before and ev.get('event_id') >= before:
                continue
            # Normalize timestamp for legacy events without timestamp field
            if 'timestamp' not in ev and ev.get('event_id'):
                # event_id format: 2026-11-09T14:16:28.149342+03:00-xxx
                m = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)', ev['event_id'])
                if m:
                    ev = dict(ev, timestamp=m.group(1))
            result.append(ev)
            if len(result) >= limit:
                break
        except Exception:
            continue
    return result

def load_pool(path_id: str, generation: int, pools_dir: Path = None) -> dict:
    """Load immutable verified pool for path/generation."""
    pools_dir = pools_dir or DEFAULT_POOLS_DIR
    pool_file = pools_dir / f'{path_id}-gen{generation}.json'
    if not pool_file.exists():
        raise AuditError('pool not found')
    return strict_json(pool_file.read_bytes())

def save_pool(path_id: str, generation: int, pool_data: dict, pools_dir: Path = None):
    """Save immutable pool (0600)."""
    pools_dir = pools_dir or DEFAULT_POOLS_DIR
    pools_dir.mkdir(parents=True, exist_ok=True)
    pool_file = pools_dir / f'{path_id}-gen{generation}.json'
    atomic_write(pool_file, encode(pool_data))
    os.chmod(pool_file, 0o600)

def save_report(report: dict, reports_dir: Path = None):
    """Save immutable audit report keyed by job_id and path."""
    reports_dir = reports_dir or DEFAULT_REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    job_id = report.get('job_id', 'unknown')
    path_id = report.get('path_id', 'unknown')
    ts = report.get('started', stamp().replace(':', '-'))
    safe_ts = ts.replace(':', '-').replace('T', '_')
    filename = f'{path_id}-{safe_ts}-{job_id}.json'
    report_file = reports_dir / filename
    atomic_write(report_file, encode(report))
    os.chmod(report_file, 0o600)
    # Apply retention
    trim_reports(reports_dir)

def trim_reports(reports_dir: Path = None):
    """Keep last MAX_REPORTS_PER_PATH per path and MAX_REPORTS_TOTAL / MAX_REPORTS_SIZE."""
    reports_dir = reports_dir or DEFAULT_REPORTS_DIR
    if not reports_dir.exists():
        return
    files = list(reports_dir.glob('*.json'))
    # Per-path retention
    by_path = {}
    for f in files:
        parts = f.stem.split('-', 1)
        path_key = parts[0] if parts else '_'
        by_path.setdefault(path_key, []).append(f)
    kept = []
    for path_key, path_files in by_path.items():
        # Sort by modification time (newest last)
        path_files.sort(key=lambda x: x.stat().st_mtime)
        kept.extend(path_files[-MAX_REPORTS_PER_PATH:])
    # Global count cap
    if len(kept) > MAX_REPORTS_TOTAL:
        kept.sort(key=lambda x: x.stat().st_mtime)
        kept = kept[-MAX_REPORTS_TOTAL:]
    # Size cap
    total = sum(f.stat().st_size for f in kept)
    kept.sort(key=lambda x: x.stat().st_mtime)
    while total > MAX_REPORTS_SIZE and kept:
        removed = kept.pop(0)
        total -= removed.stat().st_size
        removed.unlink(missing_ok=True)
    # Delete unkept files
    kept_set = set(kept)
    for f in files:
        if f not in kept_set:
            f.unlink(missing_ok=True)

def list_reports(path_id: str = None, limit: int = 50, reports_dir: Path = None) -> list:
    """List immutable reports, newest first."""
    reports_dir = reports_dir or DEFAULT_REPORTS_DIR
    if not reports_dir.exists():
        return []
    files = list(reports_dir.glob('*.json'))
    if path_id:
        files = [f for f in files if f.stem.startswith(path_id + '-')]
    # Sort by mtime newest first
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    result = []
    for f in files[:limit]:
        try:
            data = strict_json(f.read_bytes())
            data['_report_file'] = str(f)
            data['_report_mtime'] = datetime.datetime.fromtimestamp(f.stat().st_mtime).astimezone().isoformat()
            result.append(data)
        except Exception:
            continue
    return result

def resolve_source_binding(config: dict, ident: str, providers=None):
    """Compute source binding fingerprint from mihomo config.

    ``providers`` lets the caller name the subscription explicitly. That matters once a
    path is applied: the group then points at a managed file provider, and binding to it
    would describe our frozen pool instead of the subscription that feeds the audit.
    """
    group_name = PATHS[ident][0]
    groups = config.get('proxy-groups', [])
    matches = [g for g in groups if g.get('name') == group_name]
    if len(matches) != 1:
        return None
    if providers is None:
        providers = matches[0].get('use', [])
    if not isinstance(providers, list):
        return None
    registry = config.get('proxy-providers', {})
    specs = []
    for pid in providers:
        prov = registry.get(pid)
        if not isinstance(prov, dict):
            continue
        spec = {
            'id': pid,
            'url': prov.get('url', ''),
            'header': prov.get('header', {}),
            'transport': {k: v for k, v in prov.items() if k not in ('url', 'header', 'name')}
        }
        specs.append(spec)
    if not specs:
        return None
    return {
        'provider_ids': providers,
        'source_fingerprint': digest(canonical_json(specs)),
        'subscription_url_digest': digest(specs[0].get('url', '').encode()) if specs else '',
        'bound_at': stamp()
    }


if __name__ == '__main__':
    # Quick self-test
    import sys
    print('State module loaded OK')
    print(f'DEFAULT_STORE={DEFAULT_STORE}')
    print(f'DEFAULT_EVENTS={DEFAULT_EVENTS}')
    print(f'DEFAULT_STATE_DIR={DEFAULT_STATE_DIR}')
    print(f'DEFAULT_POOLS_DIR={DEFAULT_POOLS_DIR}')
    print('Run with: python3 -c "import geomask_audit_state; print(geomask_audit_state.load_store())"')
