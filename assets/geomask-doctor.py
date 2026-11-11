#!/usr/bin/env python3
"""geomask doctor - self-service diagnostics for the router, the network and the paths.

The owner should never have to run an experiment through an assistant to answer "why does
this site go there?" or "is that module healthy?". Every check here is deterministic,
writes its own log file and can be run from the panel with one button.

Commands:
  list                check registry plus the results of the last run (no work)
  run all|<id>        run one check or every non-interactive check, print JSON
  log <id>            tail of that check's log file
  domain <name>       full route of one domain: DNS, MagiTrickle, mihomo rule, exit
  prune               drop log blocks older than the retention window

Logs: /root/geomask-logs/<check>.log, one file per check, blocks older than 7 days are
dropped on every write and the file is capped, so the archive cannot grow without bound.
The panel shows the same text; nothing here changes the router's configuration.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

VERSION = '1.0.0'

LOG_DIR = '/root/geomask-logs'
LOG_RETENTION_DAYS = 7
LOG_MAX_BYTES = 200_000
CACHE = '/tmp/geomask-diag-last.json'
MIHOMO_CONF = '/etc/mihomo/config.yaml'
MIHOMO_API = 'http://127.0.0.1:9090'
MT_CONF = '/etc/magitrickle/state/config.yaml'
MODULE_INDEX = '/etc/geomask/modules-index.json'
MODULE_UPDATER = '/usr/lib/geomask/geomask-module-update.py'
MODULES_STATE = '/etc/geomask/modules.json'

# Russian node names in this subscription, as the backend's _node_cc maps them. The
# country is what the site on the other end sees, so it is worth naming it in words.
COUNTRIES = {
    'DE': 'Германия', 'NL': 'Нидерланды', 'FI': 'Финляндия', 'LV': 'Латвия',
    'LT': 'Литва', 'EE': 'Эстония', 'PL': 'Польша', 'DK': 'Дания', 'SE': 'Швеция',
    'NO': 'Норвегия', 'US': 'США', 'TR': 'Турция', 'AL': 'Албания', 'FR': 'Франция',
    'IT': 'Италия', 'ES': 'Испания', 'AT': 'Австрия', 'CH': 'Швейцария', 'GB': 'Великобритания',
    'RU': 'Россия', 'KZ': 'Казахстан', 'UA': 'Украина', 'CZ': 'Чехия', 'RO': 'Румыния',
    'BG': 'Болгария', 'HU': 'Венгрия', 'MD': 'Молдова', 'RS': 'Сербия', 'GR': 'Греция',
    'PT': 'Португалия', 'IE': 'Ирландия', 'BE': 'Бельгия', 'CA': 'Канада', 'JP': 'Япония',
    'SG': 'Сингапур', 'HK': 'Гонконг', 'KR': 'Корея', 'IN': 'Индия', 'BR': 'Бразилия',
    'AE': 'ОАЭ', 'IL': 'Израиль', 'AM': 'Армения', 'GE': 'Грузия', 'BY': 'Беларусь',
}
NODE_HINTS = (
    ('Германия', 'DE'), ('Germany', 'DE'), ('Нидерланды', 'NL'), ('Netherlands', 'NL'),
    ('Финляндия', 'FI'), ('Finland', 'FI'), ('Латвия', 'LV'), ('Latvia', 'LV'),
    ('Литва', 'LT'), ('Lithuania', 'LT'), ('Эстония', 'EE'), ('Estonia', 'EE'),
    ('Польша', 'PL'), ('Poland', 'PL'), ('Дания', 'DK'), ('Denmark', 'DK'),
    ('Швеция', 'SE'), ('Sweden', 'SE'), ('Норвегия', 'NO'), ('Norway', 'NO'),
    ('США', 'US'), ('USA', 'US'), ('United States', 'US'), ('Турция', 'TR'), ('Turkey', 'TR'),
    ('Албания', 'AL'), ('Albania', 'AL'), ('Франция', 'FR'), ('France', 'FR'),
    ('Италия', 'IT'), ('Italy', 'IT'), ('Испания', 'ES'), ('Spain', 'ES'),
    ('Австрия', 'AT'), ('Austria', 'AT'), ('Швейцария', 'CH'), ('Switzerland', 'CH'),
    ('Великобритания', 'GB'), ('United Kingdom', 'GB'), ('Казахстан', 'KZ'),
    ('Россия', 'RU'), ('Russia', 'RU'),
)


# --------------------------------------------------------------------------- shell
def run(cmd, timeout=8, stdin=None):
    """Run a command, never raise: (rc, text). rc=127 means 'not available'."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, input=stdin)
    except FileNotFoundError:
        return 127, ''
    except subprocess.TimeoutExpired:
        return 124, 'timeout'
    except Exception as exc:  # pragma: no cover - defensive
        return 125, str(exc)
    out = (proc.stdout or b'').decode('utf-8', 'replace')
    err = (proc.stderr or b'').decode('utf-8', 'replace')
    return proc.returncode, (out + err).strip()


def read_text(path, default=''):
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            return handle.read()
    except OSError:
        return default


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def now_iso():
    return datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')


# --------------------------------------------------------------------------- logs
BLOCK = re.compile(r'^=== (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})')


def log_path(check_id):
    return os.path.join(LOG_DIR, '%s.log' % check_id)


def prune_blocks(text, days=LOG_RETENTION_DAYS):
    """Drop blocks whose header is older than the retention window."""
    keep = []
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    for block in re.split(r'(?m)^(?==== )', text):
        match = BLOCK.match(block)
        if match:
            try:
                stamp = datetime.strptime(match.group(1), '%Y-%m-%dT%H:%M:%S%z')
            except ValueError:
                stamp = None
            if stamp is not None and stamp < cutoff:
                continue
        if block.strip():
            keep.append(block.rstrip('\n') + '\n')
    return ''.join(keep)


def write_log(check_id, state, lines):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = log_path(check_id)
    body = '=== %s %s ===\n%s\n' % (now_iso(), state, '\n'.join(lines))
    text = prune_blocks(read_text(path)) + body
    if len(text) > LOG_MAX_BYTES:
        text = text[-LOG_MAX_BYTES:]
        cut = text.find('\n=== ')
        if cut > 0:
            text = text[cut + 1:]
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)
    os.replace(tmp, path)
    return path


def log_tail(check_id, limit=120):
    text = read_text(log_path(check_id))
    if not text:
        return ''
    lines = text.rstrip('\n').split('\n')
    return '\n'.join(lines[-limit:])


# --------------------------------------------------------------------------- modules
def module_index():
    return read_json(MODULE_INDEX, {}) or {}


def module_selection():
    data = read_json(MODULES_STATE, None)
    if not isinstance(data, dict) or not isinstance(data.get('selected'), list):
        return None
    return [str(item) for item in data['selected']]


def load_updater():
    """Import the per-module updater so its status logic is not duplicated here."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('module_update', MODULE_UPDATER)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


# --------------------------------------------------------------------------- mihomo
def mihomo_secret():
    for line in read_text(MIHOMO_CONF).splitlines():
        match = re.match(r'^secret:\s*(.*)$', line)
        if match:
            value = match.group(1).strip()
            if value.startswith('"') and value.endswith('"') and len(value) > 1:
                value = value[1:-1]
            return value
    return ''


def api_get(path):
    """GET the controller without putting the secret into argv."""
    secret = mihomo_secret()
    options = ''
    if secret:
        options = 'header = "Authorization: Bearer %s"\n' % secret
    rc, out = run(['curl', '-s', '-m', '5', '--config', '-', MIHOMO_API + path],
                  timeout=8, stdin=options.encode())
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def flag_code(name):
    """Country code from a flag emoji (two regional indicators encode A-Z)."""
    letters = [ord(ch) - 0x1F1E6 for ch in (name or '') if 0x1F1E6 <= ord(ch) <= 0x1F1FF]
    if len(letters) >= 2:
        return ''.join(chr(ord('A') + value) for value in letters[:2])
    return ''


def node_country(name):
    """Country code and human name for a subscription node, from its name."""
    if not name:
        return '', ''
    code = flag_code(name)
    if code:
        return code, COUNTRIES.get(code, code)
    for hint, code in NODE_HINTS:
        if hint in name:
            return code, COUNTRIES.get(code, code)
    return '', ''


def group_state(name):
    """Current node of a proxy group plus how many nodes it has."""
    data = api_get('/proxies/' + name.replace(' ', '%20'))
    if not isinstance(data, dict):
        return None
    now = data.get('now') or ''
    nodes = data.get('all') or []
    cc, label = node_country(now)
    return {'group': name, 'now': now, 'nodes': len(nodes), 'cc': cc, 'country': label}


# --------------------------------------------------------------------------- parsers
def parse_magitrickle(text):
    """groups: [{name, interface, enable, rules:[{type, rule, enable}]}] from config.yaml."""
    groups = []
    current = None
    in_rules = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith('#'):
            continue
        if line.startswith('- id:') or line.startswith('  - id:'):
            # a group starts at column 0 (`- id:`) and a rule at two spaces
            if line.startswith('- id:'):
                current = {'name': '', 'interface': '', 'enable': True, 'rules': []}
                groups.append(current)
                in_rules = False
                continue
            if current is not None and in_rules:
                current['rules'].append({'type': '', 'rule': '', 'enable': True})
                continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped == 'rules:':
            in_rules = True
            continue
        if line.startswith('  ') and not line.startswith('   ') and not in_rules:
            key, _, value = stripped.partition(':')
            if key == 'name':
                current['name'] = value.strip().strip('\'"')
            elif key == 'interface':
                current['interface'] = value.strip().strip('\'"')
            elif key == 'enable':
                current['enable'] = value.strip().lower() != 'false'
            continue
        if in_rules and stripped.startswith('type:'):
            current['rules'][-1]['type'] = stripped.split(':', 1)[1].strip()
            continue
        if in_rules and stripped.startswith('rule:'):
            value = stripped.split(':', 1)[1].strip()
            current['rules'][-1]['rule'] = value.strip('\'"')
            continue
        if in_rules and stripped.startswith('enable:'):
            current['rules'][-1]['enable'] = stripped.split(':', 1)[1].strip().lower() != 'false'
            continue
        if not line.startswith(' ') and stripped and not stripped.startswith('-'):
            in_rules = False
    return groups


def domain_matches(domain, pattern, kind='namespace'):
    domain = domain.lower().rstrip('.')
    pattern = (pattern or '').lower().strip().rstrip('.')
    kind = (kind or 'namespace').lower()
    if not pattern:
        return False
    if kind in ('namespace', 'suffix', 'domain-suffix'):
        return domain == pattern or domain.endswith('.' + pattern)
    if kind in ('domain', 'exact'):
        return domain == pattern
    if kind in ('keyword', 'domain-keyword'):
        return pattern in domain
    if kind in ('regex',):
        try:
            return re.search(pattern, domain) is not None
        except re.error:
            return False
    return False


def magitrickle_route(domain):
    """Which MagiTrickle group owns the domain, and through which interface."""
    groups = parse_magitrickle(read_text(MT_CONF))
    best = None
    for group in groups:
        if not group.get('enable', True):
            continue
        for rule in group.get('rules') or []:
            if not rule.get('enable', True):
                continue
            kind = (rule.get('type') or 'namespace').lower()
            if kind == 'subnet':
                continue
            if domain_matches(domain, rule.get('rule'), kind):
                # the most specific namespace wins, as MagiTrickle resolves it
                length = len(rule.get('rule') or '')
                if best is None or length > best[2]:
                    best = (group, rule, length)
    return best


def parse_mihomo(text):
    """(rule_providers {name: path}, rules [text]) from the mihomo config.

    The installer writes block style (`  - RULE,...`); mihomo and the panel rewrite the
    file in flow style (`- RULE,...` at column 0), so both spellings must be read.
    """
    providers = {}
    rules = []
    section = ''
    provider = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith('#'):
            continue
        if not line.startswith(' '):
            match = re.match(r'^([A-Za-z0-9_-]+):', line)
            if match:
                section = match.group(1)
                provider = None
                continue
        if section == 'rule-providers':
            match = re.match(r'^(\s+)([A-Za-z0-9_-]+):\s*$', line)
            if match:
                provider = match.group(2)
                providers[provider] = ''
                continue
            match = re.match(r'^\s+path:\s*(\S+)\s*$', line)
            if match and provider:
                providers[provider] = match.group(1).strip('\'"')
            continue
        if section == 'rules':
            match = re.match(r'^\s*- (.+)$', line)
            if match:
                rules.append(match.group(1).strip())
    return providers, rules


def split_rule(rule):
    """Split a mihomo rule on top-level commas (AND/OR carry commas in parentheses)."""
    parts = []
    depth = 0
    current = ''
    for char in rule:
        if char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)
        if char == ',' and depth == 0:
            parts.append(current)
            current = ''
            continue
        current += char
    parts.append(current)
    return [part.strip() for part in parts]


def ruleset_payload(path):
    """Payload lines of a classical ruleset file."""
    entries = []
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or line == 'payload:':
            continue
        if line.startswith('- '):
            entries.append(line[2:].strip())
    return entries


def match_condition(condition, domain, ips):
    """Does one rule condition match? Returns (matched, note)."""
    parts = split_rule(condition)
    kind = (parts[0] or '').upper()
    if kind in ('DOMAIN-SUFFIX', 'DOMAIN', 'DOMAIN-KEYWORD'):
        target = parts[1] if len(parts) > 1 else ''
        ok = domain_matches(domain, target, kind)
        return ok, '%s,%s' % (kind, target)
    if kind == 'IP-CIDR' or kind == 'IP-CIDR6':
        target = parts[1] if len(parts) > 1 else ''
        hit = ''
        for ip in ips:
            if ip_in_cidr(ip, target):
                hit = ip
                break
        return bool(hit), 'IP-CIDR,%s%s' % (target, (' ← ' + hit) if hit else '')
    return False, ''


def ip_in_cidr(ip, cidr):
    try:
        import ipaddress
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def mihomo_route(domain, ips):
    """Walk the rule list in order and return the first matching rule."""
    providers, rules = parse_mihomo(read_text(MIHOMO_CONF))
    for index, rule in enumerate(rules, 1):
        if rule.upper().startswith('MATCH'):
            parts = split_rule(rule)
            return {'index': index, 'rule': rule, 'policy': parts[1] if len(parts) > 1 else 'GLOBAL'}
        if rule.upper().startswith(('AND,', 'OR,')):
            # only the domain-shaped sub-conditions can be evaluated here
            inner = rule[rule.find('(') + 1:rule.rfind(')')]
            conditions = [c.strip().strip('()') for c in split_rule(inner)]
            hits = [match_condition(c, domain, ips) for c in conditions]
            if any(hit[0] for hit in hits):
                parts = split_rule(rule)
                policy = parts[-1]
                note = next(hit[1] for hit in hits if hit[0])
                return {'index': index, 'rule': rule, 'policy': policy, 'note': note}
            continue
        parts = split_rule(rule)
        kind = (parts[0] or '').upper()
        if kind == 'RULE-SET':
            name = parts[1] if len(parts) > 1 else ''
            policy = parts[2] if len(parts) > 2 else ''
            path = providers.get(name, '')
            for entry in ruleset_payload(path):
                ok, note = match_condition(entry, domain, ips)
                if ok:
                    return {'index': index, 'rule': rule, 'policy': policy,
                            'note': 'RULE-SET %s → %s' % (name, note), 'ruleset': path}
            continue
        ok, note = match_condition(rule, domain, ips)
        if ok:
            policy = parts[-1] if len(parts) > 1 else ''
            return {'index': index, 'rule': rule, 'policy': policy, 'note': note}
    return None


def resolve(domain):
    """A records for the domain via the router's own resolver."""
    rc, out = run(['nslookup', domain, '127.0.0.1'], timeout=8)
    ips = []
    blocked = False
    for line in out.splitlines():
        match = re.search(r'^Address(?: \d+)?:\s*(\S+)\s*$', line)
        if not match:
            continue
        value = match.group(1)
        if value in ('127.0.0.1', '::1'):
            continue
        if value in ('0.0.0.0', '::'):
            blocked = True
            continue
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', value):
            ips.append(value)
    return {'ips': ips, 'blocked': blocked, 'raw': out}


# --------------------------------------------------------------------------- checks
def _state(ok, warn=False):
    if not ok:
        return 'fail'
    return 'warn' if warn else 'ok'


def check_module(module_id):
    """File integrity of one module, straight from the updater's status()."""
    def probe():
        updater = load_updater()
        index = module_index()
        if updater is None or not index:
            return 'warn', 'каталог модулей недоступен', ['нет %s или %s' % (MODULE_UPDATER, MODULE_INDEX)]
        state = updater.load_state()
        selected = updater.load_selection() if hasattr(updater, 'load_selection') else None
        report = updater.status(index, state, module_id, selected=selected)
        if not report:
            return 'warn', 'модуль не описан в каталоге', []
        item = report[0]
        details = ['версия %s → %s' % (item['installed_version'], item['available_version']),
                   'файлов: %s' % item['files']]
        for path in (item.get('missing') or [])[:5]:
            details.append('нет файла: %s' % path)
        for path in (item.get('modified') or [])[:5]:
            details.append('изменён: %s' % path)
        names = {'current': 'актуален', 'update-available': 'есть обновление',
                 'modified': 'изменён локально', 'incomplete': 'неполный',
                 'not-selected': 'не выбран на этом роутере'}
        states = {'current': 'ok', 'update-available': 'warn', 'modified': 'warn',
                  'incomplete': 'fail', 'not-selected': 'ok'}
        return states.get(item['state'], 'warn'), names.get(item['state'], item['state']), details
    return probe


def check_router_system():
    model = read_text('/tmp/sysinfo/model').strip() or 'неизвестно'
    release = ''
    for line in read_text('/etc/openwrt_release').splitlines():
        if line.startswith('DISTRIB_RELEASE='):
            release = line.split('=', 1)[1].strip('\'"')
    rc, kernel = run(['uname', '-r'])
    rc, up = run(['cat', '/proc/uptime'])
    hours = 0
    if up:
        try:
            hours = int(float(up.split()[0]) // 3600)
        except (ValueError, IndexError):
            hours = 0
    details = ['модель: %s' % model,
               'OpenWrt: %s, ядро %s' % (release or '?', kernel.strip() or '?'),
               'аптайм: %s ч' % hours]
    return 'ok', 'роутер работает', details


def check_router_resources():
    details = []
    state = 'ok'
    rc, loadavg = run(['cat', '/proc/loadavg'])
    load1 = 0.0
    if loadavg:
        try:
            load1 = float(loadavg.split()[0])
        except (ValueError, IndexError):
            load1 = 0.0
        details.append('load average: %s' % loadavg.split()[0])
    mem = {}
    for line in read_text('/proc/meminfo').splitlines():
        key, _, rest = line.partition(':')
        match = re.search(r'(\d+)', rest)
        if match:
            mem[key] = int(match.group(1))
    total = mem.get('MemTotal') or 0
    avail = mem.get('MemAvailable') or mem.get('MemFree') or 0
    if total:
        free_pct = 100.0 * avail / total
        details.append('память свободна: %.0f%% (%d МБ из %d МБ)'
                       % (free_pct, avail // 1024, total // 1024))
        if free_pct < 12:
            state = 'fail'
            details.append('критически мало памяти')
        elif free_pct < 22:
            state = 'warn'
    for label, path in (('overlay', '/'), ('/tmp', '/tmp')):
        rc, out = run(['df', '-m', path])
        if rc != 0:
            continue
        parts = out.splitlines()[-1].split()
        if len(parts) >= 4 and parts[3].isdigit():
            free = int(parts[3])
            details.append('%s свободно: %d МБ' % (label, free))
            if label == 'overlay' and free < 8:
                state = 'fail'
                details.append('overlay почти заполнен')
            elif label == 'overlay' and free < 25 and state == 'ok':
                state = 'warn'
    if load1 > 4:
        state = 'fail' if load1 > 8 else ('warn' if state == 'ok' else state)
        details.append('высокая загрузка: %.1f' % load1)
    summary = 'ресурсы в норме' if state == 'ok' else 'ресурсы требуют внимания'
    return state, summary, details


SERVICES = (
    ('dnsmasq', 'dnsmasq', True),
    ('mihomo', 'mihomo', True),
    ('MagiTrickle', 'magitrickled', True),
    ('hev-socks5-tunnel', 'hev-socks5-tunnel', True),
    ('rpcd', 'rpcd', True),
)


def check_router_services():
    details = []
    state = 'ok'
    for label, pid, critical in SERVICES:
        rc, _ = run(['pidof', pid])
        if rc == 0:
            details.append('%s: работает' % label)
        else:
            details.append('%s: НЕ работает' % label)
            state = 'fail' if critical else ('warn' if state == 'ok' else state)
    rc, _ = run(['pgrep', '-f', 'geomask-syncd.sh'])
    details.append('geomask-syncd: %s' % ('работает' if rc == 0 else 'не запущен'))
    if rc != 0 and state == 'ok':
        state = 'warn'
    summary = 'все службы работают' if state == 'ok' else 'есть остановленные службы'
    return state, summary, details


def check_router_netfilter():
    """Firewall and routing as they actually are, not as one particular version wrote them."""
    details = []
    state = 'ok'
    rc, tables = run(['nft', 'list', 'tables'])
    if rc == 127:
        return 'warn', 'nft недоступен', ['команда nft не найдена']
    has_fw4 = 'inet fw4' in tables
    details.append('таблица inet fw4: %s' % ('есть' if has_fw4 else 'НЕТ'))
    if not has_fw4:
        state = 'fail'
    # MagiTrickle lives in table ip mangle as MT_<group> chains plus geomask-excl bypasses
    rc, mangle = run(['nft', 'list', 'table', 'ip', 'mangle'])
    chains = len(re.findall(r'jump MT_[0-9a-f]+', mangle)) if rc == 0 else 0
    bypass = len(re.findall(r'geomask-excl', mangle)) if rc == 0 else 0
    if chains:
        details.append('MagiTrickle: %d цепочек MT_ в table ip mangle' % chains)
    else:
        details.append('MagiTrickle: цепочек MT_ нет (mangle)')
        if state == 'ok':
            state = 'fail'
    if bypass:
        details.append('исключения устройств (geomask-excl): %d' % bypass)
    rc, rules = run(['ip', '-4', 'rule', 'show'])
    if rc != 0:
        details.append('ip rule недоступен')
        if state == 'ok':
            state = 'fail'
    else:
        lookups = sorted({int(m) for m in re.findall(r'lookup (\d+)', rules)})
        marks = len(re.findall(r'fwmark', rules))
        details.append('таблиц в ip rule: %s' % (', '.join(str(x) for x in lookups) or 'нет'))
        details.append('правил по метке: %d' % marks)
        if not lookups:
            if state == 'ok':
                state = 'fail'
    summary = ('фильтрация и маршрутизация на месте' if state == 'ok'
               else 'в netfilter не хватает таблиц')
    return state, summary, details


def check_router_backup():
    newest = None
    folder = '/root/geomask-backups'
    try:
        for name in os.listdir(folder):
            if name.startswith('router-backup-') and name.endswith('.tar.gz'):
                path = os.path.join(folder, name)
                stamp = os.path.getmtime(path)
                if newest is None or stamp > newest[0]:
                    newest = (stamp, name, os.path.getsize(path))
    except OSError:
        return 'warn', 'каталог бэкапов недоступен', [folder]
    if newest is None:
        return 'warn', 'бэкапов нет', ['создайте: sh geomask.sh --backup']
    age_days = (time.time() - newest[0]) / 86400.0
    details = ['последний: %s (%.1f МБ)' % (newest[1], newest[2] / 1048576.0),
               'возраст: %.1f дн.' % age_days]
    if age_days > 30:
        return 'warn', 'бэкап давно не обновлялся', details
    return 'ok', 'бэкап свежий', details


def check_net_wan():
    details = []
    state = 'ok'
    data = None
    rc, out = run(['ubus', 'call', 'network.interface.wan', 'status'])
    if rc == 0 and out:
        try:
            data = json.loads(out)
        except ValueError:
            data = None
    if isinstance(data, dict):
        up = data.get('up')
        device = data.get('l3_device') or data.get('device') or ''
        addresses = [item.get('address') for item in data.get('ipv4-address') or []
                     if isinstance(item, dict) and item.get('address')]
        details.append('интерфейс WAN: %s, %s' % (device or '?', 'поднят' if up else 'ОПУЩЕН'))
        details.append('адрес: %s' % (', '.join(addresses) or 'нет'))
        if not up or not addresses:
            state = 'fail'
    else:
        rc, out = run(['ip', '-4', 'route', 'get', '1.1.1.1'])
        details.append('WAN через ubus недоступен, маршрут: %s' % (out.splitlines()[0] if out else '?'))
        if rc != 0:
            state = 'fail'
    rc, out = run(['ip', '-4', 'route', 'show', 'default'])
    if rc == 0 and out:
        details.append('маршрут по умолчанию: %s' % out.splitlines()[0])
    else:
        details.append('маршрут по умолчанию отсутствует')
        state = 'fail'
    summary = 'WAN в порядке' if state == 'ok' else 'WAN требует внимания'
    return state, summary, details


def check_net_dns():
    details = []
    state = 'ok'
    probe = resolve('ya.ru')
    if probe['ips']:
        details.append('ya.ru → %s' % ', '.join(probe['ips'][:4]))
    else:
        details.append('ya.ru не разрешается через 127.0.0.1')
        state = 'fail'
    ad = resolve('doubleclick.net')
    if ad['blocked']:
        details.append('doubleclick.net заблокирован (0.0.0.0) — adblock работает')
    elif ad['ips']:
        details.append('doubleclick.net разрешается в %s — adblock не фильтрует'
                       % ', '.join(ad['ips'][:2]))
    rc, out = run(['pgrep', '-f', 'adblock'])
    details.append('служба adblock: %s' % ('работает' if rc == 0 else 'не запущена'))
    summary = 'DNS в порядке' if state == 'ok' else 'DNS не отвечает'
    return state, summary, details


def check_net_internet():
    rc, out = run(['curl', '-m', '6', '-s', '-o', '/dev/null', '-w', '%{http_code} %{time_total}',
                   'http://www.gstatic.com/generate_204'], timeout=10)
    if rc != 0:
        return 'fail', 'нет доступа в интернет', ['curl rc=%s %s' % (rc, out[:200])]
    parts = out.split()
    code = parts[0] if parts else ''
    took = parts[1] if len(parts) > 1 else '?'
    details = ['generate_204: HTTP %s за %s с' % (code, took)]
    if code != '204':
        return 'fail', 'интернет отвечает нештатно (HTTP %s)' % code, details
    return 'ok', 'интернет доступен (%.2f с)' % float(took or 0), details


def check_net_tunnels():
    details = []
    state = 'ok'
    for device in ('Mihomo', 'AI', 'VPN-noP2P', 'Wireguard'):
        rc, out = run(['ip', '-4', 'addr', 'show', device])
        if rc != 0 or not out:
            if device in ('Mihomo',):
                details.append('%s: НЕТ' % device)
                state = 'fail'
            else:
                details.append('%s: нет (не обязателен)' % device)
            continue
        mtu = re.search(r'mtu (\d+)', out)
        address = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out)
        details.append('%s: адрес %s, mtu %s' % (device, address.group(1) if address else '?',
                                                mtu.group(1) if mtu else '?'))
        if device == 'Mihomo' and mtu and int(mtu.group(1)) > 1400:
            state = 'warn' if state == 'ok' else state
            details.append('mtu Mihomo больше 1400 — возможна фрагментация')
    summary = 'туннели на месте' if state == 'ok' else 'туннели требуют внимания'
    return state, summary, details


PATH_GROUPS = (
    ('path_vpn', 'VPN', 'основной VPN-лейн'),
    ('path_vpn_nop2p', 'VPN-noP2P', 'лейн без P2P'),
    ('path_ai', 'AI', 'AI-лейн'),
)


def check_path(group):
    def probe():
        info = group_state(group)
        if info is None:
            return 'fail', 'группа %s не отвечает в mihomo' % group, ['проверьте mihomo и контроллер 9090']
        details = ['узлов в группе: %d' % info['nodes'],
                   'текущий узел: %s' % (info['now'] or '—')]
        if info['country']:
            details.append('страна выхода: %s (%s)' % (info['country'], info['cc']))
        else:
            details.append('страна выхода: не определена по имени узла')
        if not info['nodes']:
            return 'fail', 'группа %s пуста' % group, details
        if not info['now']:
            return 'warn', 'группа %s не выбрала узел' % group, details
        return 'ok', '%s: %s' % (group, info['now']), details
    return probe


def check_path_egress():
    """Live: which country each lane's exit looks like to the internet."""
    details = []
    state = 'ok'
    probes = [('по умолчанию', None),
              ('AI', 'http://127.0.0.1:7891'),
              ('VPN', 'http://127.0.0.1:7894'),
              ('VPN-noP2P', 'http://127.0.0.1:7892')]
    for label, proxy in probes:
        cmd = ['curl', '-m', '8', '-s']
        if proxy:
            cmd += ['--proxy', proxy]
        cmd.append('https://ipinfo.io/json')
        rc, out = run(cmd, timeout=12)
        country = ''
        if rc == 0 and out:
            try:
                country = (json.loads(out) or {}).get('country') or ''
            except ValueError:
                country = ''
        if country:
            details.append('%s: выход %s (%s)' % (label, COUNTRIES.get(country, country), country))
        else:
            details.append('%s: выход не определён (нет ответа)' % label)
            if label == 'по умолчанию':
                state = 'warn'
    summary = 'выходы определены' if state == 'ok' else 'часть выходов не отвечает'
    return state, summary, details


def check_geo_invariants():
    """The routing rules that must never regress, read from the live config."""
    text = read_text(MIHOMO_CONF)
    details = []
    state = 'ok'
    providers, rules = parse_mihomo(text)
    if not rules:
        return 'fail', 'в конфиге нет правил', [MIHOMO_CONF]
    if not any('RULE-SET,mtrickle,' in rule for rule in rules):
        details.append('нет правила RULE-SET,mtrickle')
        state = 'fail'
    if 'RULE-SET,ytvpn,' not in text:
        details.append('нет правила ytvpn')
        state = 'warn' if state == 'ok' else state
    if not providers:
        details.append('не найдены rule-providers')
        state = 'warn' if state == 'ok' else state
    for name, path in sorted(providers.items()):
        if path and not os.path.exists(path):
            details.append('ruleset %s отсутствует: %s' % (name, path))
            state = 'fail' if state == 'ok' else state
    rc, _ = run(['mihomo', '-d', '/etc/mihomo', '-t'], timeout=20)
    details.append('mihomo -t: %s' % ('конфиг валиден' if rc == 0 else 'ОШИБКА'))
    if rc != 0:
        state = 'fail'
    summary = 'правила и конфиг в порядке' if state == 'ok' else 'правила требуют внимания'
    return state, summary, details


def check_update_state():
    updater = load_updater()
    index = module_index()
    if updater is None or not index:
        return 'warn', 'каталог модулей недоступен', []
    state = updater.load_state()
    selected = updater.load_selection() if hasattr(updater, 'load_selection') else None
    report = updater.status(index, state, selected=selected)
    pending = [item for item in report if item['state'] in ('update-available', 'incomplete')]
    details = ['модулей в каталоге: %d' % len(report)]
    for item in pending[:6]:
        details.append('%s: %s' % (item['id'], item['state']))
    if pending:
        return 'warn', 'есть обновления или неполные модули: %d' % len(pending), details
    return 'ok', 'все модули актуальны', details


CHECKS = []


def register(area, check_id, title, factory, hint='', live=False):
    CHECKS.append({'id': check_id, 'area': area, 'title': title, 'fn': factory,
                   'hint': hint, 'live': live})


def build_registry():
    if CHECKS:
        return CHECKS
    index = module_index()
    names = {'core': 'ядро и панель', 'path_vpn': 'VPN-лейн', 'path_vpn_nop2p': 'VPN-noP2P',
             'path_ai': 'AI-лейн', 'ai_dns': 'AI-DNS', 'youtube': 'YouTube',
             'torrent': 'торренты', 'diagnostic': 'старые проверки',
             'device_modes': 'режимы устройств', 'backup_share': 'SMB-бэкапы',
             'self_update': 'самообновление', 'admission_service': 'служба допуска',
             'doctor': 'диагностика'}
    for module_id in sorted((index.get('modules') or {})):
        register('modules', 'mod_' + module_id, 'Модуль %s' % names.get(module_id, module_id),
                 check_module(module_id))
    register('router', 'router_system', 'Система и аптайм', check_router_system)
    register('router', 'router_resources', 'Ресурсы: память, диск, загрузка', check_router_resources)
    register('router', 'router_services', 'Службы роутера', check_router_services)
    register('router', 'router_netfilter', 'Firewall и маршрутизация', check_router_netfilter)
    register('router', 'router_backup', 'Свежесть бэкапа', check_router_backup)
    register('network', 'net_wan', 'WAN и маршрут по умолчанию', check_net_wan)
    register('network', 'net_dns', 'DNS и блокировка рекламы', check_net_dns)
    register('network', 'net_internet', 'Доступ в интернет', check_net_internet)
    register('network', 'net_tunnels', 'Туннели и MTU', check_net_tunnels)
    for check_id, group, title in PATH_GROUPS:
        register('paths', check_id, 'Путь %s' % title, check_path(group))
    register('paths', 'path_egress', 'Страна выхода каждого пути', check_path_egress,
             'делает запрос наружу, занимает несколько секунд', live=True)
    register('paths', 'path_rules', 'Правила и валидность конфига', check_geo_invariants)
    register('router', 'update_state', 'Обновления модулей', check_update_state)
    return CHECKS


def find_check(check_id):
    for check in build_registry():
        if check['id'] == check_id:
            return check
    return None


def run_check(check_id):
    check = find_check(check_id)
    if check is None:
        return {'id': check_id, 'state': 'fail', 'title': check_id,
                'summary': 'нет такой проверки', 'details': []}
    started = time.time()
    try:
        state, summary, details = check['fn']()
    except Exception as exc:  # a broken check must not break the tab
        state, summary, details = 'fail', 'проверка упала: %s' % exc, []
    result = {
        'id': check['id'], 'area': check['area'], 'title': check['title'],
        'state': state, 'summary': summary, 'details': details,
        'hint': check.get('hint') or '',
        'took_ms': int((time.time() - started) * 1000),
        'log': log_path(check['id']),
    }
    lines = ['%s' % summary] + ['  ' + item for item in details]
    write_log(check['id'], state, lines)
    return result


def run_all(include_live=False):
    """Every fast check. Live probes (external requests) run only when asked for:
    rpcd has a call timeout, and the panel must stay responsive."""
    results = []
    for check in build_registry():
        if check.get('live') and not include_live:
            continue
        results.append(run_check(check['id']))
    payload = {'ok': True, 'generated': now_iso(), 'version': VERSION, 'checks': results}
    try:
        with open(CACHE + '.tmp', 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(CACHE + '.tmp', CACHE)
    except OSError:
        pass
    return payload


def list_checks():
    cached = read_json(CACHE, {}) or {}
    last = {item['id']: item for item in cached.get('checks') or []}
    checks = []
    for check in build_registry():
        item = dict(check)
        item.pop('fn', None)
        previous = last.get(check['id']) or {}
        item['last_state'] = previous.get('state', '')
        item['last_summary'] = previous.get('summary', '')
        item['last_at'] = cached.get('generated', '') if previous else ''
        item['log'] = log_path(check['id'])
        checks.append(item)
    return {'ok': True, 'version': VERSION, 'generated': cached.get('generated', ''),
            'checks': checks}


# --------------------------------------------------------------------------- domain
def trace_domain(domain, live=False):
    domain = (domain or '').strip().lower()
    domain = re.sub(r'^[a-z]+://', '', domain).split('/')[0].split('?')[0].rstrip('.')
    if not re.match(r'^[a-z0-9.*_-]+\.[a-z0-9._-]+$', domain):
        return {'ok': False, 'error': 'похоже, это не домен: %r' % domain}
    steps = []
    lines = ['Домен: %s' % domain, '']

    def step(stage, verdict, detail):
        steps.append({'stage': stage, 'verdict': verdict, 'detail': detail})
        lines.append('%-22s %s' % (stage + ':', detail))

    # 1. DNS
    probe = resolve(domain)
    if probe['blocked']:
        step('DNS', 'fail', 'домен заблокирован (0.0.0.0) — adblock или hosts')
    elif probe['ips']:
        step('DNS', 'ok', '→ %s' % ', '.join(probe['ips'][:4]))
    else:
        step('DNS', 'fail', 'не разрешается через 127.0.0.1')
    ips = probe['ips']

    # 2. MagiTrickle
    route = magitrickle_route(domain)
    if route is None:
        step('MagiTrickle', 'warn', 'домена нет ни в одной группе — пойдёт по общим правилам')
    else:
        group, rule, _ = route
        step('MagiTrickle', 'ok', 'группа «%s» (интерфейс %s), правило %s:%s'
             % (group['name'], group['interface'] or '?', rule.get('type') or 'namespace',
                rule.get('rule')))

    # 3. mihomo rule
    hit = mihomo_route(domain, ips)
    if hit is None:
        step('Правило mihomo', 'warn', 'ни одно правило не совпало — сработает MATCH')
    else:
        step('Правило mihomo', 'ok', '#%d %s → %s%s'
             % (hit['index'], hit['rule'], hit['policy'],
                (' (' + hit['note'] + ')') if hit.get('note') else ''))

    # 4. exit
    policy = (hit or {}).get('policy', 'GLOBAL')
    if policy in ('DIRECT', 'REJECT'):
        if policy == 'REJECT':
            step('Выход', 'fail', 'REJECT — трафик отбрасывается')
        else:
            step('Выход', 'ok', 'напрямую через провайдера — страна Россия (RU)')
    elif policy in ('VPN', 'VPN-noP2P', 'AI'):
        info = group_state(policy)
        if info and info['now']:
            step('Выход', 'ok', '%s → узел «%s»%s' % (
                policy, info['now'],
                (', страна ' + info['country'] + ' (' + info['cc'] + ')') if info['country'] else ''))
        elif info:
            step('Выход', 'warn', '%s не выбрал узел (узлов: %d)' % (policy, info['nodes']))
        else:
            step('Выход', 'warn', 'группа %s не отвечает через API' % policy)
    else:
        step('Выход', 'warn', 'политика %s — уточните вручную' % policy)

    # 5. live probe
    if live:
        rc, out = run(['curl', '-m', '10', '-s', '-o', '/dev/null',
                       '-w', '%{http_code} %{time_total}', 'https://' + domain], timeout=14)
        if rc == 0 and out:
            code = out.split()[0]
            took = out.split()[1] if len(out.split()) > 1 else '?'
            step('Проверка HTTP', 'ok' if code.startswith(('2', '3')) else 'warn',
                 'HTTP %s за %s с (с роутера)' % (code, took))
        else:
            step('Проверка HTTP', 'warn', 'нет ответа (rc=%s)' % rc)

    lines.append('')
    lines.append('Итог: %s' % (steps[2]['detail'] if len(steps) > 2 else ''))
    if route is not None:
        lines.append('Изменить направление: MagiTrickle → группа «%s» (вкладка «Списки и синк»).'
                     % route[0]['name'])
    else:
        lines.append('Домена нет в списках MagiTrickle: чтобы задать направление, добавьте его '
                     'в нужную группу (вкладка «Списки и синк»).')
    state = 'ok'
    for item in steps:
        if item['verdict'] == 'fail':
            state = 'fail'
            break
        if item['verdict'] == 'warn' and state == 'ok':
            state = 'warn'
    write_log('domain_trace', state, lines)
    return {'ok': True, 'domain': domain, 'state': state, 'steps': steps,
            'lines': lines, 'log': log_path('domain_trace')}


# --------------------------------------------------------------------------- cli
def main(argv=None):
    parser = argparse.ArgumentParser(prog='geomask-doctor')
    parser.add_argument('command', choices=['list', 'run', 'log', 'domain', 'prune'])
    parser.add_argument('target', nargs='?')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--tail', type=int, default=120)
    args = parser.parse_args(argv)

    if args.command == 'list':
        print(json.dumps(list_checks(), ensure_ascii=False))
        return 0
    if args.command == 'run':
        target = args.target or 'all'
        if target == 'all':
            print(json.dumps(run_all(include_live=args.live), ensure_ascii=False))
            return 0
        result = run_check(target)
        print(json.dumps({'ok': result['state'] != 'fail', 'checks': [result]},
                         ensure_ascii=False))
        return 0 if result['state'] != 'fail' else 0
    if args.command == 'log':
        target = args.target or ''
        text = log_tail(target, args.tail)
        print(json.dumps({'ok': bool(text), 'id': target, 'text': text,
                          'log': log_path(target)}, ensure_ascii=False))
        return 0
    if args.command == 'domain':
        print(json.dumps(trace_domain(args.target or '', live=args.live), ensure_ascii=False))
        return 0
    if args.command == 'prune':
        removed = 0
        for name in sorted(os.listdir(LOG_DIR)) if os.path.isdir(LOG_DIR) else []:
            if not name.endswith('.log'):
                continue
            path = os.path.join(LOG_DIR, name)
            before = read_text(path)
            after = prune_blocks(before)
            if after != before:
                with open(path, 'w', encoding='utf-8', newline='\n') as handle:
                    handle.write(after)
                removed += 1
        print(json.dumps({'ok': True, 'pruned': removed, 'dir': LOG_DIR}, ensure_ascii=False))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
