#!/usr/bin/env python3
"""Independent desired country filters and isolated service-egress observations.
Schema v2: independent path revisions, source bindings, events, immutable pools.
Never applies filters to production. Unknown service geography is not admission.
"""
import argparse
import contextlib
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

# Load state module
import importlib.util
STATE_MODULE_PATH = Path(__file__).with_name('geomask-audit-state.py')
_state_spec = importlib.util.spec_from_file_location('audit_state', STATE_MODULE_PATH)
_state = importlib.util.module_from_spec(_state_spec)
_state_spec.loader.exec_module(_state)

# Path constants (can be patched by tests)
STORE = Path(os.environ.get('GM_AUDIT_STORE', '/etc/geomask/node-audit.json'))
STATE = Path(os.environ.get('GM_AUDIT_STATE', '/etc/geomask/node-audit/state'))
STATE = STATE  # alias for backward compat
CONFIG = Path(os.environ.get('GM_AUDIT_CONFIG', '/etc/mihomo/config.yaml'))
RECEIPT = Path(os.environ.get('GM_AUDIT_RECEIPT', '/tmp/geomask-node-audit-receipt.json'))
EVENTS = Path(os.environ.get('GM_AUDIT_EVENTS', '/etc/geomask/node-audit/events.jsonl'))
POOLS_DIR = Path(os.environ.get('GM_AUDIT_POOLS', '/etc/geomask/node-audit/pools'))
MAX_NODES = _state.MAX_NODES
REGISTRY = Path(os.environ.get('GM_AUDIT_REGISTRY', '/etc/geomask-subs.list'))

# Re-export from state module
PATHS = _state.PATHS

# Backward compat: atomic function
atomic = _state.atomic_write

AuditError = _state.AuditError
stamp = _state.stamp
_save_node_snapshot = _state.save_node_snapshot
load_node_snapshot = _state.load_node_snapshot
digest = _state.digest
encode = _state.encode
canonical_json = _state.canonical_json
strict_json = _state.strict_json
path_id = _state.path_id
countries = _state.countries
locked = _state.locked
atomic_write = _state.atomic_write

# State module functions with explicit path parameters
def _load_store():
    return _state.load_store(STORE)

def _save_store(data):
    return _state.save_store(data, STORE)

def _append_event(event):
    return _state.append_event(event, EVENTS)

def _get_events(path=None, before=None, limit=50):
    return _state.get_events(path, before, limit, EVENTS)

def _load_pool(path_id, generation):
    return _state.load_pool(path_id, generation, POOLS_DIR)

def _save_pool(path_id, generation, pool_data):
    return _state.save_pool(path_id, generation, pool_data, POOLS_DIR)

def _resolve_source_binding(config, ident, providers=None):
    return _state.resolve_source_binding(config, ident, providers)

def _compute_path_revision(path_data):
    return _state.compute_path_revision(path_data)

# Backward compat: expose load_store
load_store = _load_store

# Reports directory
REPORTS_DIR = Path(os.environ.get('GM_AUDIT_REPORTS', '/etc/geomask/node-audit/reports'))

# State module functions with explicit path parameters
def _save_report(report):
    return _state.save_report(report, REPORTS_DIR)

def _list_reports(path_id=None, limit=50):
    return _state.list_reports(path_id, limit, REPORTS_DIR)

# Legacy constants for backward compatibility
REVISION_KEY = 'revision'  # whole-store digest for legacy RPC

def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

def render_save(data, ident, allow):
    """Legacy render for backward compatibility with prepare-save."""
    result = json.loads(json.dumps(data))
    result['paths'].setdefault(ident, {})['countries_allow'] = allow
    result['generation'] = result.get('generation', 0) + 1
    return encode(result)

def prepare_save(ident, value, expected):
    """Legacy prepare-save: validates countries, returns candidate info using path_revision."""
    ident, allow = path_id(ident), countries(value)
    data, original_raw = _load_store()
    # Legacy: expected is whole-store revision, but we now use path_revision
    # Compute current whole-store digest for backward compat
    current_revision = digest(original_raw) if original_raw else digest(encode(data))
    if expected != current_revision:
        raise AuditError('STALE: reload settings before saving')
    # Check if change would actually modify anything
    current_allow = data['paths'].get(ident, {}).get('countries_allow', PATHS[ident][1])
    changed = current_allow != allow
    candidate = render_save(data, ident, allow) if changed else None
    return {'ok': True, 'before_sha256': current_revision,
            'candidate_sha256': digest(candidate) if candidate else '',
            'boot_id': boot_id(), 'path_id': ident, 'changed': changed}

def save(ident, value, expected):
    """Save countries for a path using path_revision (v2) or whole-store revision (legacy)."""
    ident, allow = path_id(ident), countries(value)
    with locked('settings'):
        data, original_raw = _load_store()
        # Check revision: try path_revision first, fall back to whole-store for legacy
        current_revision = digest(original_raw) if original_raw else digest(encode(data))
        path_rev = data['paths'].get(ident, {}).get('path_revision', '')
        
        revision_ok = False
        if expected == path_rev:
            revision_ok = True
        elif expected == current_revision:
            # Legacy whole-store revision
            revision_ok = True
        if not revision_ok:
            raise AuditError('STALE: reload settings before saving')
        
        # No-op check
        current_allow = data['paths'].get(ident, {}).get('countries_allow', PATHS[ident][1])
        if current_allow == allow:
            return {'ok': True, 'changed': False, 'revision': path_rev or current_revision, 'applied': False}
        
        # Update path data
        data['paths'][ident]['countries_allow'] = allow
        data['paths'][ident]['path_revision'] = _compute_path_revision(data['paths'][ident])
        data['paths'][ident]['path_generation'] = data['paths'][ident].get('path_generation', 0) + 1
        data['paths'][ident]['configured'] = True
        
        _save_store(data)
        _append_event({
            'path': ident,
            'actor': 'luci',
            'action': 'save',
            'old_countries': current_allow,
            'new_countries': allow,
            'path_revision': data['paths'][ident]['path_revision'],
            'path_generation': data['paths'][ident]['path_generation'],
            'result': 'success'
        })
        return {'ok': True, 'changed': True, 'revision': data['paths'][ident]['path_revision'], 'applied': False}

def yaml_load(raw):
    import yaml
    try:
        value = _state.yaml12_load(raw)
        if not isinstance(value, dict):
            raise AuditError('expected YAML mapping')
        return value
    except yaml.YAMLError:
        raise AuditError('invalid YAML source') from None

def resolve_source(config, ident):
    """Legacy: returns group_name and provider_ids."""
    group_name = PATHS[path_id(ident)][0]
    matches = [g for g in config.get('proxy-groups', []) if g.get('name') == group_name]
    if len(matches) != 1:
        raise AuditError('missing or duplicate target group')
    providers = matches[0].get('use')
    if not isinstance(providers, list) or not 1 <= len(providers) <= 3 or any(not isinstance(p, str) for p in providers):
        raise AuditError('target group requires provider use list')
    registry = config.get('proxy-providers') or {}
    if any(p not in registry for p in providers):
        raise AuditError('target provider missing')
    return group_name, list(dict.fromkeys(providers))

MANAGED_PREFIX = 'audit-'

def resolve_audit_source(config, ident, path_data=None, previous_report=None):
    """Resolve the subscription this path audits, even while it is applied.

    An apply replaces the group's source with a managed file provider. Resolving naively
    would then point the next check at our own frozen pool, so an applied path could never
    be audited again -- and the nightly run would fail on it forever. The original
    providers are recorded at apply time and in the last report; use those.
    """
    group_name, providers = resolve_source(config, ident)
    managed = [p for p in providers if p.startswith(MANAGED_PREFIX)]
    if not managed:
        return group_name, providers

    recorded = ((path_data or {}).get('applied') or {}).get('previous_use') or []
    if not recorded and previous_report:
        recorded = [p for p in (previous_report.get('providers') or [])
                    if not str(p).startswith(MANAGED_PREFIX)]
    registry = config.get('proxy-providers') or {}
    usable = [p for p in recorded if p in registry]
    if not usable:
        raise AuditError('path is applied to a managed pool and its original source is not '
                         'recorded in the mihomo config; refusing to audit the frozen pool')
    return group_name, list(dict.fromkeys(usable))

def get():
    """Get all paths with v2 state: path_revision, source_binding, schedule, applied."""
    data, original_raw = _load_store()
    current_source = None
    config = {}
    try:
        current_raw = CONFIG.read_bytes()
        current_source = digest(current_raw)
        config = yaml_load(current_raw)
    except (OSError, AuditError):
        pass

    # The revision handed to the UI must be the same value save() compares against,
    # otherwise an un-migrated store can never be written.
    revision = digest(original_raw) if original_raw else digest(encode(data))

    result = []
    for ident, (label, defaults) in PATHS.items():
        path_data = data['paths'].get(ident, {})
        configured = path_data.get('configured', False)

        report = None
        state_file = STATE / (ident + '.json')
        if state_file.exists():
            try:
                report = strict_json(state_file.read_bytes())
            except (AuditError, OSError):
                report = {'status': 'error', 'error': 'unreadable report'}

        try:
            # Resolve the subscription this path audits, not our own frozen pool.
            _, providers = resolve_audit_source(config, ident, path_data, report)
            binding = _resolve_source_binding(config, ident, providers)
            source_revision = binding.get('source_fingerprint') if binding else None
        except AuditError:
            providers = []
            binding = None
            source_revision = None

        if report and report.get('status') != 'error':
            stale = False
            if report.get('settings_revision') != path_data.get('path_revision', ''):
                stale = True
            if current_source and report.get('source_revision') != current_source:
                stale = True
            if stale:
                report = dict(report, stale=True)

        applied = path_data.get('applied', {})
        applied_bool = applied.get('status') in ('applied', 'applying')
        result.append({
            'id': ident,
            'label': label,
            'providers': providers,
            'source_binding': binding,
            'source_revision': source_revision,
            'configured': configured,
            'countries_allow': path_data.get('countries_allow', defaults),
            'path_revision': path_data.get('path_revision', ''),
            'path_generation': path_data.get('path_generation', 0),
            'schedule': path_data.get('schedule', {'enabled': False, 'time': '03:30',
                                                   'mode': 'check-only'}),
            'applied': applied_bool,
            'applied_detail': {
                'generation': applied.get('generation', 0),
                'path_revision': applied.get('path_revision', ''),
                'runtime_verified': applied.get('runtime_verified', False),
                'status': applied.get('status', 'never'),
                'applied_at': applied.get('applied_at', ''),
                'node_count': applied.get('node_count', 0),
                'pool_digest': applied.get('pool_digest', ''),
                'acting_countries': applied.get('acting_countries') or [],
                'provider_id': applied.get('provider_id', ''),
            },
            'report': report,
        })

    return {
        'ok': True,
        'api_schema': 2,
        'revision': revision,
        'paths': result,
        'apply_supported': True,
        'scope': 'desired filters, isolated observations, and explicit per-path apply',
    }

def fetch_nodes(provider):
    url = provider.get('url', '')
    if not isinstance(url, str) or urllib.parse.urlsplit(url).scheme != 'https':
        raise AuditError('provider must use HTTPS for the isolated audit')
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password:
        raise AuditError('invalid provider endpoint')
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise AuditError('non-public subscription endpoint is not supported')
    except OSError:
        raise AuditError('subscription hostname could not be resolved') from None
    headers = {'User-Agent': 'clash-meta'}
    for key, value in (provider.get('header') or {}).items():
        headers[key] = str(value[0] if isinstance(value, list) else value)
    headers['User-Agent'] = 'clash-meta'
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                raise AuditError('subscription redirect refused; no credentials forwarded')
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise AuditError('subscription too large')
        nodes = yaml_load(raw).get('proxies')
    except Exception as exc:
        if isinstance(exc, AuditError):
            raise
        raise AuditError('subscription request failed (' + type(exc).__name__ + ')') from None
    if not isinstance(nodes, list) or not 3 <= len(nodes) <= MAX_NODES:
        raise AuditError('unexpected subscription node count (expected 3..100)')
    names = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get('name'), str) or not node.get('server'):
            raise AuditError('malformed subscription node')
        name = node['name']
        if name in names or any(ord(char) < 32 for char in name):
            raise AuditError('duplicate or invalid node name')
        names.add(name)
    return nodes, digest(raw)

# AI admission criterion, decided by the owner on 09.11.2026: a node is judged by the
# services whose geography it can actually report. Google does not disclose a country in any
# machine-readable form -- a real SERP fetched through a verified node carried no uule, no
# region/countryCode/country/gl field, no data-country and no "Google offered in" -- so
# requiring it would block every node forever while proving nothing about where it exits.
AI_REQUIRED_SERVICES = ['cloudflare', 'chatgpt']
# Probed and recorded, never used as a gate. The report says "unmeasurable", not "failed".
AI_INFORMATIONAL_SERVICES = ['google']

# Bumped when the measuring method changes; a report carries it so a later reader can tell
# which findings were produced by which method instead of comparing unlike numbers.
PROBE_VERSION = 'service-egress-2'

def classify(ident, allow, probes):
    required = AI_REQUIRED_SERVICES if ident == 'ai' else ['cloudflare']
    reasons = []
    unknown = False
    failed = False
    for service in required:
        probe = probes.get(service, {})
        country = probe.get('country')
        if probe.get('status') != 'ok' or not country:
            unknown = True
            reasons.append(service + ': geography unknown or probe failed')
        elif country not in allow:
            failed = True
            reasons.append(service + ': ' + country + ' not allowed')
    status = 'excluded' if failed else 'unknown' if unknown else 'eligible'
    result = {'status': status, 'reasons': reasons, 'admitted': status == 'eligible'}
    if ident == 'ai':
        # Recorded so the panel can say what is known and what simply cannot be measured.
        informational = {}
        for service in AI_INFORMATIONAL_SERVICES:
            probe = probes.get(service) or {}
            country = probe.get('country')
            informational[service] = {
                'measured': bool(country),
                'country': country or None,
                'policy': 'not-required',
            }
        result['informational'] = informational
    return result

def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

class IsolatedProbe:
    def __init__(self, nodes):
        self.nodes = nodes
        self.work = None
        self.process = None
        self.watchdog = None
        self.port = free_port()
        self.api_port = free_port()
        self.secret = secrets.token_hex(24)

    def api(self, path, value=None):
        headers = {'Authorization': 'Bearer ' + self.secret}
        if value is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request('http://127.0.0.1:' + str(self.api_port) + path,
            data=json.dumps(value).encode() if value is not None else None,
            method='PUT' if value is not None else 'GET', headers=headers)
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}

    def __enter__(self):
        import yaml
        self.work = Path(tempfile.mkdtemp(prefix='geomask-egress-', dir=str(STATE)))
        try:
            os.chmod(self.work, 0o700)
            config = {'mixed-port': 0, 'allow-lan': False, 'bind-address': '127.0.0.1',
                'external-controller': '127.0.0.1:' + str(self.api_port), 'secret': self.secret,
                'mode': 'rule', 'log-level': 'silent', 'ipv6': False,
                'listeners': [{'name': 'isolated-audit', 'type': 'mixed', 'listen': '127.0.0.1',
                               'port': self.port, 'proxy': 'AUDIT'}],
                'proxies': self.nodes,
                'proxy-groups': [{'name': 'AUDIT', 'type': 'select', 'proxies': [n['name'] for n in self.nodes]}],
                'rules': ['MATCH,AUDIT']}
            candidate = self.work / 'config.yaml'
            candidate.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
            os.chmod(candidate, 0o600)
            checked = subprocess.run(['mihomo', '-d', str(self.work), '-f', str(candidate), '-t'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
            if checked.returncode:
                raise AuditError('isolated candidate validation failed')
            self.process = subprocess.Popen(['mihomo', '-d', str(self.work), '-f', str(candidate)],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._watch()
            for _ in range(40):
                if self.process.poll() is not None:
                    break
                try:
                    self.api('/version')
                    return self
                except Exception:
                    time.sleep(0.2)
            raise AuditError('isolated controller did not start')
        except Exception:
            self.__exit__(None, None, None)
            raise

    def _watch(self):
        pid = self.process.pid
        proc = Path('/proc') / str(pid)
        identity = (proc / 'stat').read_text().split(') ', 1)[1].split()[19]
        work = str(self.work)
        child = os.fork()
        if child == 0:
            try:
                time.sleep(900)
                current = (proc / 'stat').read_text().split(') ', 1)[1].split()[19]
                command = (proc / 'cmdline').read_bytes()
                if current == identity and work.encode() in command and b'mihomo' in command:
                    os.kill(pid, 15)
                    time.sleep(5)
                    current = (proc / 'stat').read_text().split(') ', 1)[1].split()[19]
                    command = (proc / 'cmdline').read_bytes()
                    if current == identity and work.encode() in command and b'mihomo' in command:
                        os.kill(pid, 9)
            except (OSError, ValueError):
                pass
            finally:
                shutil.rmtree(work, ignore_errors=True)
            os._exit(0)
        self.watchdog = child

    def probe(self, node, service):
        endpoints = {'cloudflare': 'https://www.cloudflare.com/cdn-cgi/trace',
                     'chatgpt': 'https://chatgpt.com/cdn-cgi/trace',
                     'google': 'https://www.google.com/generate_204'}
        self.api('/proxies/AUDIT', {'name': node})
        args = ['curl', '--disable', '--silent', '--show-error', '--noproxy', '', '--connect-timeout', '6',
                '--max-time', '12', '--max-filesize', '16000', '--proxy',
                'socks5h://127.0.0.1:' + str(self.port), '--write-out', '\nhttp_code=%{http_code}\n',
                endpoints[service] + '?_=' + secrets.token_hex(6)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        fields = dict(re.findall(r'(?m)^(ip|loc|colo|http_code)=([^\r\n]+)', result.stdout))
        code = fields.get('http_code')
        if service == 'google':
            return {'status': 'unknown', 'availability': result.returncode == 0 and code == '204',
                    'country': None, 'http_code': code, 'reason': 'HTTP 204 does not prove Google geography',
                    'dns_context': 'remote SOCKS5h', 'time': stamp()}
        country = fields.get('loc')
        if result.returncode or code != '200' or not re.fullmatch(r'[A-Z]{2}', country or ''):
            return {'status': 'unknown', 'country': None, 'http_code': code,
                    'reason': 'trace unavailable or malformed', 'time': stamp()}
        try:
            ipaddress.ip_address(fields.get('ip', ''))
        except ValueError:
            return {'status': 'unknown', 'country': None, 'reason': 'invalid observed exit IP'}
        return {'status': 'ok', 'country': country, 'ip': fields['ip'], 'colo': fields.get('colo'),
                'http_code': code, 'dns_context': 'remote SOCKS5h', 'time': stamp(), 'tls_verified': True}

    def __exit__(self, *unused):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.watchdog:
            try:
                os.kill(self.watchdog, 15)
                os.waitpid(self.watchdog, 0)
            except ProcessLookupError:
                pass
        if self.work:
            shutil.rmtree(self.work, ignore_errors=True)

@contextlib.contextmanager
def job_budget(seconds):
    import signal
    deadline = time.monotonic() + seconds
    if not hasattr(signal, 'SIGALRM'):
        yield deadline
        return
    def expired(signum, frame):
        raise AuditError('whole-job time budget exceeded; partial results are not a pass')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield deadline
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

@contextlib.contextmanager
def audit_locks(ident):
    with locked('runtime', nonblocking=True), locked('job-' + ident, nonblocking=True):
        yield

def check(ident, owns_lock=False, job_id=None, actor='cli'):
    path_id(ident)
    lock = contextlib.nullcontext() if owns_lock else audit_locks(ident)
    with lock, job_budget(720) as deadline:
        data, _ = _load_store()
        path_data = data['paths'].get(ident, {})
        raw = CONFIG.read_bytes()
        config = yaml_load(raw)
        target = STATE / (ident + '.json')
        previous_report = None
        try:
            previous_report = strict_json(target.read_bytes()) if target.exists() else None
        except (OSError, AuditError):
            previous_report = None
        group, providers = resolve_audit_source(config, ident, path_data, previous_report)
        allow = path_data.get('countries_allow', PATHS[ident][1])
        path_rev = path_data.get('path_revision', '')
        binding = _resolve_source_binding(config, ident, providers)
        source_fingerprint = binding.get('source_fingerprint') if binding else None
        # Use whole config digest for staleness (matches original behavior and tests)
        config_digest = digest(raw)
        
        report = {'job_id': job_id or secrets.token_hex(12), 'path_id': ident, 'group': group, 'providers': providers, 
                  'settings_revision': path_rev, 'source_revision': config_digest, 'countries_allow': allow, 'started': stamp(),
                  'status': 'running', 'applied': False, 'nodes': [],
                  'configured': ident in data['paths'],
                  'probe_version': PROBE_VERSION,
                  'probe_context': {'dns': 'remote SOCKS5h', 'services': (
                      AI_REQUIRED_SERVICES + AI_INFORMATIONAL_SERVICES if ident == 'ai'
                      else ['cloudflare']),
                      'admission': ('chatgpt+cloudflare' if ident == 'ai' else 'cloudflare')},
                  'scope': 'observed service egress with remote DNS; not actual client/hosts route',
                  'limit': 'No automatic apply. Google geography is not measurable and is not '
                           'used for admission; AI is judged by ChatGPT and Cloudflare. '
                           'No claim for untested services.'}
        atomic_write(target, encode(report))
        audited_definitions = {}
        try:
            for provider_name in providers:
                nodes, source = fetch_nodes(config['proxy-providers'][provider_name])
                with IsolatedProbe(nodes) as probe:
                    for node in nodes:
                        if time.monotonic() > deadline:
                            raise AuditError('audit time budget exceeded; partial results are not a pass')
                        # Check for cancellation request
                        try:
                            current_report = strict_json(target.read_bytes())
                            if current_report.get('status') == 'cancelled':
                                report['status'] = 'cancelled'
                                report['ok'] = False
                                report['error'] = 'cancelled by user'
                                report['finished'] = stamp()
                                atomic_write(target, encode(report))
                                _append_event({
                                    'path': ident,
                                    'actor': actor,
                                    'action': 'cancel',
                                    'job_id': report['job_id'],
                                    'result': 'success'
                                })
                                return report
                        except (OSError, AuditError):
                            pass
                        services = (AI_REQUIRED_SERVICES + AI_INFORMATIONAL_SERVICES
                                    if ident == 'ai' else ['cloudflare'])
                        observations = {}
                        for service in services:
                            try:
                                observations[service] = probe.probe(node['name'], service)
                            except Exception as exc:
                                observations[service] = {'status': 'unknown', 'country': None,
                                                         'reason': 'probe error (' + type(exc).__name__ + ')'}
                        # Compute node identity fingerprint. The whole definition is covered:
                        # a hand-picked subset would let a rewritten credential keep the
                        # fingerprint of the node that was actually measured.
                        node_identity = {
                            'definition': node,
                            'provider_id': provider_name,
                        }
                        node_fingerprint = digest(canonical_json(node_identity))
                        audited_definitions[node['name']] = {
                            'provider': provider_name,
                            'node_fingerprint': node_fingerprint[:16],
                            'definition': node,
                        }
                        report['nodes'].append({'name': node['name'], 'provider': provider_name,
                            'node_fingerprint': node_fingerprint[:16], 'source_digest': source, 'probes': observations, **classify(ident, allow, observations)})
                        atomic_write(target, encode(report))
            # Check staleness using path_revision and config digest (matches original behavior)
            current_data, _ = _load_store()
            current_path_rev = current_data['paths'].get(ident, {}).get('path_revision', '')
            current_config_raw = CONFIG.read_bytes()
            current_config_digest = digest(current_config_raw)
            stale = current_path_rev != path_rev or current_config_digest != config_digest
            report['stale'] = stale
            report['status'] = 'stale' if stale else 'complete'
            report['counts'] = {key: sum(n['status'] == key for n in report['nodes'])
                                for key in ('eligible', 'excluded', 'unknown')}
            present = {p.get('country') for n in report['nodes'] for p in n['probes'].values()}
            report['missing_countries'] = sorted(set(allow) - present)
            report['ok'] = not stale
        except Exception as exc:
            report['status'] = 'error'
            report['ok'] = False
            report['error'] = str(exc) if isinstance(exc, AuditError) else type(exc).__name__
        report['finished'] = stamp()
        atomic_write(target, encode(report))
        # Save immutable report
        _save_report(report)
        # Persist the exact definitions that were probed, so an apply can build a managed
        # provider from what was actually measured instead of re-reading a moving source.
        if report.get('status') in ('complete', 'stale') and audited_definitions:
            # Keep the snapshots an applied manifest or the previous report still refer to;
            # retention must not delete material a pending preview or a pool depends on.
            protected = [path_data.get('applied', {}).get('job_id', '')]
            if previous_report:
                protected.append(previous_report.get('job_id', ''))
            _save_node_snapshot(ident, report['job_id'], audited_definitions,
                                protect=protected)
        # Append event
        _append_event({
            'path': ident,
            'actor': actor,
            'action': 'check',
            'job_id': report['job_id'],
            'status': report['status'],
            'counts': report.get('counts', {}),
            'stale': report.get('stale', False),
            'result': 'success' if report.get('ok') else 'failed'
        })
        return report

def start(ident, actor='luci'):
    path_id(ident)
    with audit_locks(ident):
        data, _ = _load_store()
        path_data = data['paths'].get(ident, {})
        raw = CONFIG.read_bytes()
        config = yaml_load(raw)
        resolve_source(config, ident)
        job_id = secrets.token_hex(12)
        queued = {'job_id': job_id, 'path_id': ident, 'status': 'queued', 'started': stamp(),
                  'settings_revision': path_data.get('path_revision', ''), 'source_revision': digest(raw), 'applied': False}
        atomic_write(STATE / (ident + '.json'), encode(queued))
        try:
            child = os.fork()
        except OSError:
            queued.update(status='error', ok=False, finished=stamp(), error='worker fork failed')
            atomic_write(STATE / (ident + '.json'), encode(queued))
            raise AuditError('worker fork failed') from None
        if child == 0:
            try:
                os.setsid()
                with open(os.devnull, 'r+b', buffering=0) as null:
                    for descriptor in (0, 1, 2):
                        os.dup2(null.fileno(), descriptor)
                # The parent releases its reservation when start() returns, so the worker
                # waits for it rather than racing it, then owns the slot for its own run.
                with locked('runtime'), locked('job-' + ident):
                    check(ident, owns_lock=True, job_id=job_id, actor=actor)
            except Exception as exc:
                queued.update(status='error', ok=False, finished=stamp(),
                              error=str(exc) if isinstance(exc, AuditError) else type(exc).__name__)
                atomic_write(STATE / (ident + '.json'), encode(queued))
            finally:
                os._exit(0)
        return {'ok': True, 'started': True, 'job_id': job_id, 'path_id': ident, 'applied': False}

def source_in_use(url):
    """Report whether a subscription URL backs a provider some group actually uses.

    Deleting a registry entry that a path still depends on would leave the group pointing
    at a provider whose source is gone. The caller refuses the delete on a hit.
    """
    if not isinstance(url, str) or not url:
        raise AuditError('source url required')
    try:
        config = yaml_load(CONFIG.read_bytes())
    except OSError:
        raise AuditError('mihomo config is unreadable') from None
    providers = config.get('proxy-providers') or {}
    used = set()
    for group in config.get('proxy-groups') or []:
        for provider_id in group.get('use') or []:
            used.add(provider_id)
    hits = sorted(pid for pid, spec in providers.items()
                  if isinstance(spec, dict) and spec.get('url') == url and pid in used)
    return {'ok': True, 'in_use': bool(hits), 'providers': hits}


def audit_events_get(path_id=None, before=None, limit=50):
    """Get paginated audit events."""
    events = _get_events(path_id, before, min(int(limit), 100))
    return {'ok': True, 'events': events}

def audit_job_get(job_id):
    """Get job status by job_id. Searches all path state files."""
    try:
        for state_file in STATE.glob('*.json'):
            report = strict_json(state_file.read_bytes())
            if report.get('job_id') == job_id:
                return {'ok': True, 'job': report}
        return {'ok': False, 'error': 'job not found'}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}

def audit_cancel(job_id, actor='luci'):
    """Cancel a running job by job_id. Only works for queued/running jobs."""
    try:
        for state_file in STATE.glob('*.json'):
            report = strict_json(state_file.read_bytes())
            if report.get('job_id') == job_id:
                status = report.get('status', '')
                if status in ('queued', 'running'):
                    report['status'] = 'cancelled'
                    report['ok'] = False
                    report['finished'] = stamp()
                    report['error'] = 'cancelled by user'
                    atomic_write(state_file, encode(report))
                    _append_event({
                        'path': report.get('path_id', ''),
                        'actor': actor,
                        'action': 'cancel',
                        'job_id': job_id,
                        'result': 'success'
                    })
                    return {'ok': True, 'cancelled': True}
                else:
                    return {'ok': False, 'error': f'job not cancellable (status: {status})'}
        return {'ok': False, 'error': 'job not found'}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}

def schedule_get(pid):
    """Get schedule configuration for a path."""
    try:
        path_id(pid)
    except AuditError as e:
        return {'ok': False, 'error': str(e)}
    data, _ = load_store()
    path_data = data['paths'].get(pid, {})
    schedule = path_data.get('schedule', {'enabled': False, 'time': '03:30', 'mode': 'check-only'})
    return {'ok': True, 'schedule': schedule}

def schedule_set(pid, enabled, time, mode):
    """Set schedule configuration for a path."""
    path_id(pid)
    if enabled not in ('true', 'false'):
        raise AuditError('enabled must be true or false')
    if not re.fullmatch(r'([01]?[0-9]|2[0-3]):[0-5][0-9]', time):
        raise AuditError('time must be HH:MM format')
    if mode not in ('check-only', 'check-and-apply'):
        raise AuditError('mode must be check-only or check-and-apply')
    
    with locked('settings'):
        data, _ = load_store()
        path_data = data['paths'].get(pid, {})
        path_data['schedule'] = {
            'enabled': enabled == 'true',
            'time': time,
            'mode': mode,
        }
        _save_store(data)
        _append_event({
            'path': pid,
            'actor': 'luci',
            'action': 'schedule-set',
            'enabled': enabled == 'true',
            'time': time,
            'mode': mode,
            'result': 'success'
        })
        return {'ok': True, 'schedule': path_data['schedule']}

def audit_cancel(job_id, actor='luci'):
    """Cancel a running job by job_id. Only works for queued/running jobs."""
    try:
        for state_file in STATE.glob('*.json'):
            report = strict_json(state_file.read_bytes())
            if report.get('job_id') == job_id:
                status = report.get('status', '')
                if status in ('queued', 'running'):
                    report['status'] = 'cancelled'
                    report['ok'] = False
                    report['finished'] = stamp()
                    report['error'] = 'cancelled by user'
                    atomic_write(state_file, encode(report))
                    _append_event({
                        'path': report.get('path_id', ''),
                        'actor': actor,
                        'action': 'cancel',
                        'job_id': job_id,
                        'result': 'success'
                    })
                    return {'ok': True, 'cancelled': True}
                else:
                    return {'ok': False, 'error': f'job not cancellable (status: {status})'}
        return {'ok': False, 'error': 'job not found'}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}

def validate_rpc(method, raw):
    if len(raw.encode('utf-8')) > 16384:
        raise AuditError('RPC request too large')
    fields = {'nodes_paths_get': set(), 'nodes_path_check': {'path_id'},
              'nodes_path_save': {'path_id', 'countries', 'expected_revision'},
              'nodes_audit_job_get': {'job_id'},
              'nodes_audit_cancel': {'job_id'},
              'nodes_audit_events_get': {'path_id', 'before', 'limit'},
              'nodes_audit_schedule_get': {'path_id'},
              'nodes_audit_schedule_set': {'path_id', 'enabled', 'time', 'mode'}}
    if method not in fields:
        raise AuditError('unknown audit RPC')
    request = strict_json(raw)
    if not isinstance(request, dict):
        raise AuditError('invalid RPC object')
    has_session = 'ubus_rpc_session' in request
    session = request.pop('ubus_rpc_session', None)
    if has_session and (not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{32}', session)):
        raise AuditError('invalid RPC session metadata')
    if set(request) != fields[method]:
        raise AuditError('invalid RPC fields')
    for value in request.values():
        if not isinstance(value, str) or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise AuditError('RPC values must be strings without controls')
    if 'path_id' in request:
        path_id(request['path_id'])
    if 'countries' in request:
        countries(request['countries'])
        if not re.fullmatch(r'[a-f0-9]{64}', request['expected_revision']):
            raise AuditError('invalid revision')
    if 'job_id' in request and not re.fullmatch(r'[a-f0-9]{24}', request['job_id']):
        raise AuditError('invalid job_id')
    if 'enabled' in request and request['enabled'] not in ('true', 'false'):
        return {'ok': False, 'error': 'enabled must be true or false'}
    if 'time' in request and not re.fullmatch(r'([01]?[0-9]|2[0-3]):[0-5][0-9]', request['time']):
        return {'ok': False, 'error': 'time must be HH:MM format'}
    if 'mode' in request and request['mode'] not in ('check-only', 'check-and-apply'):
        return {'ok': False, 'error': 'mode must be check-only or check-and-apply'}
    return {'ok': True}

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['get', 'save', 'check', 'start', 'validate-rpc', 'prepare-save', 'audit-events', 'audit-job', 'audit-cancel', 'schedule-get', 'schedule-set', 'source-in-use'])
    parser.add_argument('args', nargs='*')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate-rpc' and len(args.args) == 1:
            value = validate_rpc(args.args[0], sys.stdin.read(16385))
        elif args.command == 'get' and not args.args:
            value = get()
        elif args.command == 'prepare-save' and len(args.args) == 3:
            value = prepare_save(*args.args)
        elif args.command == 'save' and len(args.args) == 3:
            value = save(*args.args)
        elif args.command == 'start' and len(args.args) == 1:
            value = start(args.args[0], actor='cli')
        elif args.command == 'check' and len(args.args) == 1:
            value = check(args.args[0], actor='cli')
        elif args.command == 'audit-events' and len(args.args) <= 3:
            path_id = args.args[0] if len(args.args) > 0 else None
            before = args.args[1] if len(args.args) > 1 else None
            limit = args.args[2] if len(args.args) > 2 else '50'
            value = audit_events_get(path_id, before, limit)
        elif args.command == 'audit-job' and len(args.args) == 1:
            value = audit_job_get(args.args[0])
        elif args.command == 'audit-cancel' and len(args.args) == 1:
            value = audit_cancel(args.args[0], actor='cli')
        elif args.command == 'schedule-get' and len(args.args) == 1:
            value = schedule_get(args.args[0])
        elif args.command == 'schedule-set' and len(args.args) == 4:
            value = schedule_set(*args.args)
        elif args.command == 'source-in-use' and len(args.args) == 1:
            value = source_in_use(args.args[0])
        else:
            raise AuditError('invalid command arguments')
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value.get('ok', True) else 1
    except Exception as exc:
        error = str(exc) if isinstance(exc, AuditError) else type(exc).__name__
        print(json.dumps({'ok': False, 'error': error, 'applied': False}))
        return 1

if __name__ == '__main__':
    sys.exit(main())