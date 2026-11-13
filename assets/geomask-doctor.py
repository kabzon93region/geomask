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
from urllib.parse import quote
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
TUNNELED_IFACES = {'Mihomo', 'AI', 'VPN-noP2P'}

ZAPRET_CONF = '/opt/zapret/config'
ZAPRET_HOSTLIST = '/opt/zapret/ipset/zapret-hosts-user.txt'
ZAPRET_EXCLUDE = '/opt/zapret/ipset/zapret-hosts-user-exclude.txt'
ZAPRET_AUTO = '/opt/zapret/ipset/zapret-hosts-auto.txt'


def plain_rule(rule):
    """A mihomo rule line in words a person who has never read config.yaml understands."""
    parts = [item.strip() for item in (rule or '').split(',') if item.strip()]
    kind = parts[0].upper() if parts else ''
    if kind == 'MATCH':
        return 'любой другой домен'
    if kind == 'RULE-SET':
        return 'список %s' % (parts[1] if len(parts) > 1 else '?')
    if kind == 'GEOSITE':
        return 'геосписок %s' % (parts[1] if len(parts) > 1 else '?')
    if kind == 'GEOIP':
        return 'страна %s' % (parts[1] if len(parts) > 1 else '?')
    if kind == 'DOMAIN-SUFFIX':
        return 'домен и его поддомены'
    if kind == 'DOMAIN':
        return 'точный домен'
    if kind.startswith('IP-CIDR'):
        return 'диапазон адресов'
    if kind in ('AND', 'OR', 'NOT'):
        return 'составное правило %s' % kind
    return rule or '?'


def plain_iface(iface):
    """Where a MagiTrickle group sends traffic, in words."""
    if not iface:
        return 'интерфейс не задан'
    if iface == 'Mihomo':
        return 'в mihomo (там выбирается узел VPN)'
    if iface == 'AI':
        return 'в AI-туннель'
    if iface == 'VPN-noP2P':
        return 'в VPN-туннель без P2P'
    return 'напрямую в интернет через %s' % iface


def _hostlist_match(domain, path):
    """Which line of a zapret host list covers this domain, '' when none does.

    zapret matches a line against the host itself and its subdomains, and the owner uses
    that deliberately: a bare `ru` in the exclude list drops every .ru domain from the desync.
    """
    domain = (domain or '').lower().strip('.')
    for raw in read_text(path).splitlines():
        line = raw.strip().lower()
        if not line or line.startswith('#'):
            continue
        line = line.lstrip('*').lstrip('.')
        if domain == line or domain.endswith('.' + line):
            return line
    return ''


def zapret_route(domain):
    """Does zapret (nfqws) process this domain, and if not, why not.

    The trace used to stop after the mihomo rule, so a domain going out directly looked like
    it had no DPI handling at all - while the owner's wardogs host had been in zapret's
    hostlist the whole time.
    """
    rc, out = run(['pidof', 'nfqws'])
    pids = (out or '').strip() if rc == 0 else ''
    # a running nfqws means nothing if nothing queues packets to it
    nrc, nout = run(['nft', 'list', 'table', 'inet', 'zapret'])
    queues = nrc == 0 and 'queue' in (nout or '')
    mode = ''
    for line in read_text(ZAPRET_CONF).splitlines():
        if line.strip().startswith('MODE_FILTER='):
            mode = line.split('=', 1)[1].strip().strip('"').strip("'")
            break
    return {'running': bool(pids), 'pids': pids, 'mode': mode, 'queues': queues,
            'listed': _hostlist_match(domain, ZAPRET_HOSTLIST),
            'excluded': _hostlist_match(domain, ZAPRET_EXCLUDE),
            'auto': bool(_hostlist_match(domain, ZAPRET_AUTO))}


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


def magitrickle_disabled(domain):
    """A group that lists the domain but with the rule switched off, if any.

    This is the case the owner hit with plati.market: the domain was in a group all along,
    the rule just had `enable: false`, so the trace said "not in any group" and sent them
    looking for a missing list instead of a checkbox.
    """
    for group in parse_magitrickle(read_text(MT_CONF)):
        for rule in group.get('rules') or []:
            if rule.get('enable', True):
                continue
            kind = (rule.get('type') or 'namespace').lower()
            if kind == 'subnet':
                continue
            if domain_matches(domain, rule.get('rule'), kind):
                return group, rule
    return None, None


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
                            'note': 'строка «%s» в списке %s' % (note, name), 'ruleset': path}
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
    lowered = out.lower()
    nxdomain = 'nxdomain' in lowered or "can't find" in lowered or 'not found' in lowered
    return {'ips': ips, 'blocked': blocked, 'nxdomain': nxdomain, 'raw': out}


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
        fixes = {
            'update-available': 'Обновите модуль: панель → Модули → «Обновить».',
            'modified': 'Вернуть каталожную версию: панель → Модули → «Перезаписать».',
            'incomplete': 'Файлов не хватает: панель → Модули → «Обновить»; если не помогло — переустановка.',
            'not-selected': 'Модуль не выбран при установке. Включить: sh geomask.sh --modules=core,path_vpn,%s.' % module_id,
        }
        return (states.get(item['state'], 'warn'), names.get(item['state'], item['state']),
                details, fixes.get(item['state'], ''))
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


def parse_listeners(text):
    """listeners: [{name, listen, port, proxy}] in either YAML style.

    The panel writes them in flow style and the installer in block style; the address is
    not always loopback (torrent-direct listens on the LAN address), so the probe has to
    read it instead of assuming 127.0.0.1.
    """
    listeners = []
    section = ''
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith('#'):
            continue
        if not line.startswith(' '):
            match = re.match(r'^([A-Za-z0-9_-]+):', line)
            if match:
                section = match.group(1)
                current = None
                continue
        if section != 'listeners':
            continue
        match = re.match(r'^\s*-\s*name:\s*(\S+)\s*$', line)
        if match:
            current = {'name': match.group(1), 'listen': '', 'port': '', 'proxy': ''}
            listeners.append(current)
            continue
        if current is None:
            continue
        for key in ('listen', 'port', 'proxy'):
            match = re.match(r'^\s+%s:\s*(\S+)\s*$' % key, line)
            if match:
                current[key] = match.group(1).strip('\'"')
    return listeners


def direct_country(live):
    """Country of the provider exit, measured from this router - '' when not measured.

    The trace used to print "Россия (RU)" for every DIRECT domain without looking, which is
    simply wrong on a router whose WAN is somewhere else.
    """
    if not live:
        return ''
    return fetch_country('') or ''


def country_label(code):
    if not code:
        return ''
    return '%s (%s)' % (COUNTRIES.get(code, code), code)


def exit_country_text(code):
    if not code:
        return 'страна выхода не измерялась (включите живую проверку)'
    return 'страна %s — измерено с роутера' % country_label(code)


EGRESS_ENDPOINTS = (
    ('https://ipinfo.io/json', ('country',)),
    ('https://api.myip.com', ('country', 'cc')),
    ('https://ifconfig.co/json', ('country_iso', 'country')),
)


def country_from_payload(payload, keys):
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and re.match(r'^[A-Za-z]{2}$', value):
            return value.upper()
    return ''


def fetch_country(proxy):
    """Country of the exit behind this proxy, from whichever geo service answers.

    One service is not enough: a lane's exit IP is often a datacenter range and the
    service answers 403 (observed on the AI lane), which says nothing about the route.
    """
    for url, keys in EGRESS_ENDPOINTS:
        cmd = ['curl', '-m', '8', '-s']
        if proxy:
            cmd += ['--proxy', proxy]
        cmd.append(url)
        rc, out = run(cmd, timeout=12)
        if rc != 0 or not out:
            continue
        try:
            payload = json.loads(out)
        except ValueError:
            continue
        if isinstance(payload, dict):
            code = country_from_payload(payload, keys)
            if code:
                return code
    return ''


def check_path_egress():
    """Live: which country each lane's exit looks like to the internet."""
    details = []
    state = 'ok'
    probes = [('по умолчанию', None)]
    for item in parse_listeners(read_text(MIHOMO_CONF)):
        proxy = item.get('proxy') or ''
        if not proxy or proxy.upper() == 'DIRECT':
            continue
        host = item.get('listen') or '127.0.0.1'
        if host in ('0.0.0.0', '::', '[::]'):
            host = '127.0.0.1'
        port = item.get('port') or ''
        if port:
            probes.append((proxy, 'http://%s:%s' % (host, port)))
    if len(probes) == 1:
        details.append('в конфиге нет слушателей с фиксированной группой')
    for label, proxy in probes:
        country = fetch_country(proxy)
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


def register(area, check_id, title, factory, hint='', live=False, fix=''):
    """fix: what the owner should do when the check is not ok. Static text is enough for
    most checks; the ones whose remedy depends on the state return it themselves."""
    CHECKS.append({'id': check_id, 'area': area, 'title': title, 'fn': factory,
                   'hint': hint, 'live': live, 'fix': fix})


def build_registry():
    if CHECKS:
        return CHECKS
    index = module_index()
    names = {'core': 'ядро', 'luci': 'Админка LuCI', 'guard': 'geomask-guard',
             'path_vpn': 'VPN-лейн', 'path_vpn_nop2p': 'VPN-noP2P',
             'path_ai': 'AI-лейн', 'ai_dns': 'AI-DNS', 'youtube': 'YouTube',
             'torrent': 'торренты', 'diagnostic': 'старые проверки',
             'device_modes': 'режимы устройств', 'backup_share': 'SMB-бэкапы',
             'self_update': 'самообновление', 'admission_service': 'служба допуска',
             'doctor': 'диагностика'}
    for module_id in sorted((index.get('modules') or {})):
        register('modules', 'mod_' + module_id, 'Модуль %s' % names.get(module_id, module_id),
                 check_module(module_id))
    register('router', 'router_system', 'Система и аптайм', check_router_system)
    register('router', 'router_resources', 'Ресурсы: память, диск, загрузка', check_router_resources,
             fix='Освободите место: старые бэкапы в /root/geomask-backups, логи в /root/geomask-logs. '
                 'При высокой загрузке посмотрите, кто её даёт: top -n1')
    register('router', 'router_services', 'Службы роутера', check_router_services,
             fix='Поднимите службу: /etc/init.d/<служба> restart. Если не поднимается — logread | tail -40')
    register('router', 'router_netfilter', 'Firewall и маршрутизация', check_router_netfilter,
             fix='Перезапустите: /etc/init.d/firewall restart, затем /etc/init.d/magitrickle restart; '
                 'после этого sh geomask.sh --step 5')
    register('router', 'router_backup', 'Свежесть бэкапа', check_router_backup,
             fix='Создайте бэкап: sh geomask.sh --backup — копия ляжет в /root/geomask-backups и на ПК')
    register('network', 'net_wan', 'WAN и маршрут по умолчанию', check_net_wan,
             fix='Проверьте кабель и PPPoE: logread | grep -i wan, ip -4 addr show; '
                 'перезапуск: /etc/init.d/network restart')
    register('network', 'net_dns', 'DNS и блокировка рекламы', check_net_dns,
             fix='Перезапустите dnsmasq: /etc/init.d/dnsmasq restart; upstream — LuCI → Сеть → DHCP/DNS')
    register('network', 'net_internet', 'Доступ в интернет', check_net_internet,
             fix='Проверьте WAN и текущий узел: панель → Обзор. Пустой узел — панель → Подписки → «Обновить»')
    register('network', 'net_tunnels', 'Туннели и MTU', check_net_tunnels,
             fix='Поднимите туннель: /etc/init.d/hev-socks5-tunnel restart; конфиги в /etc/mihomo/*.yml')
    for check_id, group, title in PATH_GROUPS:
        register('paths', check_id, 'Путь %s' % title, check_path(group),
                 fix='Обновите подписку: панель → Подписки → «Обновить». Если узлов нет — проблема на стороне провайдера')
    register('paths', 'path_egress', 'Страна выхода каждого пути', check_path_egress,
             'делает запрос наружу, занимает несколько секунд', live=True,
             fix='Слушатель не отвечает: проверьте узел группы в панели → Обзор; '
                 'для VPN — слушатель torrent-direct на 192.168.0.1:7894')
    register('paths', 'path_rules', 'Правила и валидность конфига', check_geo_invariants,
             fix='Верните правила: sh geomask.sh --step 5 (синк) или --reinstall; проверка: mihomo -d /etc/mihomo -t')
    register('router', 'update_state', 'Обновления модулей', check_update_state,
             fix='Панель → Модули → «Проверить обновления», затем обновите нужные модули')
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
                'summary': 'нет такой проверки', 'details': [], 'fix': ''}
    started = time.time()
    fix = check.get('fix') or ''
    try:
        outcome = check['fn']()
        if isinstance(outcome, tuple) and len(outcome) == 4:
            state, summary, details, fix = outcome
        else:
            state, summary, details = outcome
    except Exception as exc:  # a broken check must not break the tab
        state, summary, details = 'fail', 'проверка упала: %s' % exc, []
    result = {
        'id': check['id'], 'area': check['area'], 'title': check['title'],
        'state': state, 'summary': summary, 'details': details,
        'hint': check.get('hint') or '',
        'fix': fix if state != 'ok' else '',
        'took_ms': int((time.time() - started) * 1000),
        'log': log_path(check['id']),
    }
    lines = ['%s' % summary] + ['  ' + item for item in details]
    if result['fix']:
        lines.append('  → ' + result['fix'])
    write_log(check['id'], state, lines)
    return result


def verdict(checks):
    """One line the owner can read without opening anything."""
    counts = {'ok': 0, 'warn': 0, 'fail': 0}
    for item in checks:
        counts[item['state']] = counts.get(item['state'], 0) + 1
    if counts['fail']:
        text = 'есть проблемы: %d — откройте их, в каждой написано, что делать' % counts['fail']
    elif counts['warn']:
        text = 'всё работает, но %d проверок просят внимания' % counts['warn']
    else:
        text = 'всё в порядке'
    return {'counts': counts, 'text': text, 'state': 'fail' if counts['fail'] else
            ('warn' if counts['warn'] else 'ok')}


def run_all(include_live=False):
    """Every fast check. Live probes (external requests) run only when asked for:
    rpcd has a call timeout, and the panel must stay responsive."""
    results = []
    for check in build_registry():
        if check.get('live') and not include_live:
            continue
        results.append(run_check(check['id']))
    summary = verdict(results)
    payload = {'ok': True, 'generated': now_iso(), 'version': VERSION,
               'verdict': summary['text'], 'verdict_state': summary['state'],
               'counts': summary['counts'], 'checks': results}
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
        item['last_fix'] = previous.get('fix', '')
        item['last_at'] = cached.get('generated', '') if previous else ''
        item['log'] = log_path(check['id'])
        checks.append(item)
    return {'ok': True, 'version': VERSION, 'generated': cached.get('generated', ''),
            'verdict': cached.get('verdict', ''),
            'verdict_state': cached.get('verdict_state', ''),
            'counts': cached.get('counts') or {},
            'checks': checks}


# --------------------------------------------------------------------------- domain
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) geomask-doctor'


def node_delay(name, timeout=5000):
    """Round-trip of one node in ms, or None when it does not answer at all."""
    if not name:
        return None
    data = api_get('/proxies/%s/delay?timeout=%d&url=%s'
                   % (quote(name, safe=''), timeout,
                      quote('https://www.gstatic.com/generate_204', safe='')))
    if isinstance(data, dict) and isinstance(data.get('delay'), int):
        return data['delay']
    return None


def http_probe(domain, timeout=20):
    """One HTTPS request from the router, explained instead of just "rc=28".

    curl exit codes carry the answer: 28 means nothing came back in time, 6 is DNS,
    35 is TLS, 56 is a reset. A timeout is the interesting one - it can be the exit node
    or the site refusing that exit, and the owner needs to know which.
    """
    rc, out = run(['curl', '-m', str(timeout), '--connect-timeout', '8', '-sS',
                   '-o', '/dev/null', '-A', USER_AGENT,
                   '-w', '%{http_code} %{time_total} %{remote_ip}', 'https://' + domain],
                  timeout=timeout + 8)
    parts = (out or '').split()
    code = parts[0] if parts else ''
    took = parts[1] if len(parts) > 1 else ''
    ip = parts[2] if len(parts) > 2 else ''
    if rc == 0 and code and code != '000':
        # 4xx is the site answering (a 404 on a game host is normal): the path works
        good = code.startswith(('2', '3', '4'))
        return {'ok': good, 'code': code, 'took': took, 'ip': ip, 'rc': 0,
                'kind': '' if good else 'http',
                'detail': 'HTTP %s за %s с%s' % (code, took, (', адрес ' + ip) if ip else '')}
    kinds = {28: 'timeout', 6: 'dns', 7: 'refused', 35: 'tls', 52: 'empty', 56: 'reset',
             60: 'cert'}
    kind = kinds.get(rc, 'other')
    words = {'timeout': 'нет ответа за %d с' % timeout, 'dns': 'имя не разрешилось',
             'refused': 'соединение отклонено', 'tls': 'TLS не поднялся',
             'empty': 'сервер закрыл соединение без ответа', 'reset': 'соединение сброшено',
             'cert': 'сертификат не проверен', 'other': 'сбой запроса'}
    return {'ok': False, 'code': code, 'took': took, 'ip': ip, 'rc': rc, 'kind': kind,
            'detail': '%s (curl rc=%s)' % (words.get(kind, 'сбой запроса'), rc)}


def host_of(url):
    """Hostname of a URL or of a bare host, '' for anything relative or non-http."""
    url = (url or '').strip().strip('\'"')
    match = re.match(r'^(?:https?:)?//([^/:?#]+)', url)
    if match:
        return match.group(1).lower().rstrip('.')
    if re.match(r'^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:[:/?#]|$)', url, re.I):
        return url.split('/')[0].split(':')[0].split('?')[0].lower().rstrip('.')
    return ''


SESSION_KINDS = {'form', 'iframe'}


PAGE_FILE = '/tmp/geomask-doctor-page.html'
HOPS_FILE = '/tmp/geomask-doctor-hops.txt'


def related_hosts(domain, limit=12):
    """Hosts the page itself depends on, with the kind of reference that found them.

    A shop is rarely one host: the catalogue answers from one domain and the payment
    processor from another, and the second one decides whether the page works. This finds
    the ones the HTML and the certificate mention. Requests made later by JavaScript are
    invisible from here - that page has to be loaded in a browser.

    The kind matters: pictures and stylesheets on another path are harmless, a form or an
    iframe on another path carries the session and can be rejected by the site.
    """
    page = PAGE_FILE
    hops = HOPS_FILE
    # a failed request leaves the previous page behind, and the previous site's hosts were
    # then reported as this domain's (a nonexistent host showed plati.market's six hosts)
    for stale in (page, hops):
        try:
            os.remove(stale)
        except OSError:
            pass
    found = []

    def add(host, kind):
        host = (host or '').lower().rstrip('.')
        if not host or host == domain or host.endswith('.' + domain):
            return
        if not re.match(r'^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$', host):
            return
        for item in found:
            if item['host'] == host:
                if kind not in item['kinds']:
                    item['kinds'].append(kind)
                return
        found.append({'host': host, 'kinds': [kind], 'session': kind in SESSION_KINDS})

    run(['curl', '-sS', '-L', '--max-time', '20', '-A', USER_AGENT,
         '-D', hops, '-o', page, 'https://' + domain], timeout=26)
    for line in read_text(hops).splitlines():
        if line.lower().startswith('location:'):
            add(host_of(line.split(':', 1)[1]), 'redirect')
    html = read_text(page)[:600000]
    for tag in ('img', 'script', 'iframe', 'link', 'form', 'a'):
        pattern = r'<%s\b[^>]{0,400}?(?:src|href|action)\s*=\s*["\']([^"\']{4,300})' % tag
        for match in re.finditer(pattern, html, re.I):
            add(host_of(match.group(1)), tag)
    rc, out = run(['sh', '-c',
                   'echo | timeout 8 openssl s_client -connect %s:443 -servername %s 2>/dev/null '
                   '| openssl x509 -noout -text 2>/dev/null' % (domain, domain)], timeout=12)
    for match in re.finditer(r'DNS:([^,\n]+)', out or ''):
        name = match.group(1).strip()
        if name.startswith('*.'):
            continue
        add(name, 'cert')
    return found[:limit]


def host_route(host):
    """Where one host goes: group, mihomo policy and the country of that exit."""
    route = magitrickle_route(host)
    hit = mihomo_route(host, [])
    policy = (hit or {}).get('policy', 'GLOBAL')
    cc = country = ''
    if policy in ('VPN', 'VPN-noP2P', 'AI', 'GLOBAL'):
        info = group_state(policy) or {}
        cc, country = info.get('cc', ''), info.get('country', '')
        if not cc and (info.get('now') or '').upper() == 'DIRECT':
            cc, country = 'RU', 'Россия'     # GLOBAL defaults to DIRECT on this router
    elif policy == 'DIRECT':
        cc, country = 'RU', 'Россия'
    return {'host': host, 'group': (route[0]['name'] if route else ''),
            'policy': policy, 'cc': cc, 'country': country,
            'rule': (hit or {}).get('rule', '')}


def trace_domain(domain, live=False):
    domain = (domain or '').strip().lower()
    domain = re.sub(r'^[a-z]+://', '', domain).split('/')[0].split('?')[0].rstrip('.')
    if not re.match(r'^[a-z0-9.*_-]+\.[a-z0-9._-]+$', domain):
        return {'ok': False, 'error': 'похоже, это не домен: %r' % domain}
    steps = []
    route_path = []          # the path itself: zapret -> MagiTrickle -> mihomo -> internet
    route_country = ''
    mt_iface = ''
    lines = ['Домен: %s' % domain, '']

    def step(stage, verdict, detail, fix=''):
        steps.append({'stage': stage, 'verdict': verdict, 'detail': detail, 'fix': fix})
        lines.append('%-22s %s' % (stage + ':', detail))

    # 1. DNS
    probe = resolve(domain)
    if probe['blocked']:
        step('DNS', 'fail', '0.0.0.0 — домен заблокирован на роутере (adblock или hosts)',
             'Разрешите домен в LuCI → adblock-fast (список разрешённых) или уберите его из hosts.')
    elif probe['ips']:
        step('DNS', 'ok', '→ %s' % ', '.join(probe['ips'][:4]))
    elif probe.get('nxdomain'):
        step('DNS', 'fail', 'домен не существует (NXDOMAIN)',
             'Проверьте написание домена. Если он только что появился — подождите обновления DNS.')
    else:
        step('DNS', 'fail', 'роутер не смог узнать адрес домена — DNS не отвечает',
             'DNS не отвечает: /etc/init.d/dnsmasq restart; upstream — LuCI → Сеть → DHCP/DNS.')
    ips = probe['ips']

    # 2. MagiTrickle
    route = magitrickle_route(domain)
    if route is None:
        off_group, off_rule = magitrickle_disabled(domain)
        if off_group is not None:
            step('MagiTrickle', 'warn',
                 'правило есть в группе «%s», но выключено (enable: false)'
                 % (off_group['name'] or 'без имени'),
                 'Включите правило в MagiTrickle (галочка у домена %s) или перенесите домен '
                 'в нужную группу: geomask-lists-edit.py move --group "<группа>" --domain %s'
                 % (off_rule.get('rule'), domain))
        else:
            route_path.append('группы MagiTrickle нет')
            step('MagiTrickle', 'warn', 'домена нет ни в одной группе — сработает общее правило mihomo',
                 'Чтобы задать направление, добавьте домен в группу MagiTrickle: '
                 'geomask-lists-edit.py add --group "<группа>" --domain %s' % domain)
    else:
        group, rule, _ = route
        mt_iface = group['interface'] or ''
        route_path.append('MagiTrickle: группа «%s»' % group['name'])
        if mt_iface and mt_iface not in TUNNELED_IFACES:
            route_path.append(mt_iface)
        step('MagiTrickle', 'ok', 'группа «%s» → %s; правило %s:%s'
             % (group['name'], plain_iface(group['interface']),
                rule.get('type') or 'namespace', rule.get('rule')),
             'Поменять направление: MagiTrickle → группа «%s» (вкладка «Списки и синк»).' % group['name'])

    # 3. mihomo rule
    hit = mihomo_route(domain, ips)
    if mt_iface and mt_iface not in TUNNELED_IFACES:
        # a group bound to a physical interface hands the packet to the WAN directly:
        # mihomo never sees it, so its rules cannot be "missing"
        step('Правило mihomo', 'ok',
             'не используется: трафик уходит %s, mihomo его не видит' % plain_iface(mt_iface))
    elif hit is None:
        step('Правило mihomo', 'warn', 'ни одно правило не подошло — сработает правило «любой другой домен»',
             'Домен не покрыт ни одним списком mihomo: добавьте его в список нужной группы.')
    else:
        route_path.append('mihomo: %s' % plain_rule(hit['rule']))
        step('Правило mihomo', 'ok', '№%d %s → группа %s%s'
             % (hit['index'], plain_rule(hit['rule']), hit['policy'],
                (' — %s' % hit['note']) if hit.get('note') else ''))

    # 3b. where that rule set comes from - the owner asked whether it duplicates MagiTrickle
    if hit and hit.get('ruleset'):
        ruleset_path = hit['ruleset']
        head = read_text(ruleset_path)[:200]
        stamp = ''
        try:
            stamp = time.strftime('%d.%m %H:%M', time.localtime(os.path.getmtime(ruleset_path)))
        except OSError:
            pass
        name = os.path.basename(ruleset_path)
        if 'AUTO-GENERATED' in head:
            step('Источник правила', 'ok',
                 '%s (собран %s из групп MagiTrickle)' % (name, stamp or '?'),
                 'Домен вписан один раз, в MagiTrickle; файл для mihomo делает '
                 'sync_mtrickle_mihomo.sh при каждом изменении групп.')
        else:
            step('Источник правила', 'ok', '%s — отдельный список, в MagiTrickle его нет' % name,
                 'Этот файл ведётся отдельно от групп MagiTrickle: смотрите его историю в git.')

    # 4. exit
    policy = (hit or {}).get('policy', 'GLOBAL')
    if mt_iface and mt_iface not in TUNNELED_IFACES:
        route_path.append('интернет, мимо mihomo')
        _cc = direct_country(live)
        route_country = country_label(_cc)
        step('Выход', 'ok', 'напрямую в интернет через %s — так решила группа «%s»; %s'
             % (mt_iface, (group['name'] if route else ''), exit_country_text(_cc)),
             'Направление задаётся группой «%s»: вкладка «Списки и синк».' % (group['name'] if route else ''))
    elif policy in ('DIRECT', 'REJECT'):
        if policy == 'REJECT':
            # a QUIC reject is the deliberate policy for video hosts: UDP/443 is dropped,
            # the same page loads fine over TCP, so this is not a broken route
            if 'UDP' in ((hit or {}).get('rule') or '').upper():
                route_path.append('QUIC (UDP/443) отбрасывается, видео идёт по TCP')
                step('Выход', 'ok', 'видео-запросы по QUIC (UDP/443) отбрасываются намеренно — '
                     'то же видео идёт по TCP и работает')
            else:
                route_path.append('REJECT — трафик отбрасывается')
                step('Выход', 'fail', 'REJECT — трафик к домену отбрасывается, сайт не откроется',
                     'Правило REJECT блокирует домен: найдите его в правилах mihomo и уберите или смените политику.')
        else:
            route_path.append('интернет через провайдера')
            _cc = direct_country(live)
            route_country = country_label(_cc)
            step('Выход', 'ok', 'напрямую в интернет через провайдера; %s'
                 % exit_country_text(_cc))
    elif policy in ('VPN', 'VPN-noP2P', 'AI'):
        info = group_state(policy)
        if info and info['now']:
            route_path.append('туннель %s' % policy)
            route_path.append('узел «%s»' % info['now'])
            route_country = (info['country'] + (' (%s)' % info['cc'] if info.get('cc') else '')
                             if info['country'] else '')
            step('Выход', 'ok', 'через туннель %s на узел «%s»%s' % (
                policy, info['now'],
                ('; страна ' + info['country'] + ' (' + info['cc'] + ')') if info['country'] else ''))
        elif info:
            step('Выход', 'warn', '%s не выбрал узел (узлов: %d)' % (policy, info['nodes']),
                 'Группа %s пуста: панель → Подписки → «Обновить»; если узлов нет — проверьте провайдера.' % policy)
        else:
            step('Выход', 'warn', 'группа %s не отвечает через API' % policy,
                 'Mihomo не отвечает на 127.0.0.1:9090: /etc/init.d/mihomo restart.')
    elif policy == 'GLOBAL':
        info = group_state('GLOBAL')
        now = (info or {}).get('now') or ''
        if now:
            if now.upper() == 'DIRECT':
                _cc = direct_country(live)
                where = 'напрямую в интернет через провайдера; %s' % exit_country_text(_cc)
            else:
                where = 'через узел «%s»%s' % (now, ('; страна ' + info['country'])
                                               if info.get('country') else '')
            route_path.append('группа GLOBAL')
            route_path.append(where.split('; страна ')[0].strip())
            route_country = (country_label(direct_country(live)) if now.upper() == 'DIRECT'
                             else (info.get('country') or ''))
            step('Выход', 'warn',
                 'группа GLOBAL отправляет %s — домен не попал ни в одно правило' % where,
                 'Домен не попал ни в одно правило, поэтому работает группа GLOBAL, а её '
                 'выбор можно поменять в любой момент. Если нужен предсказуемый путь, '
                 'добавьте домен в группу MagiTrickle: '
                 'geomask-lists-edit.py add --group "<группа>" --domain %s' % domain)
        else:
            step('Выход', 'warn', 'группа GLOBAL не выбрала узел, а домен не попал ни в одно правило',
                 'Добавьте домен в группу MagiTrickle: geomask-lists-edit.py add '
                 '--group "<группа>" --domain %s' % domain)
    else:
        step('Выход', 'warn', 'политика %s — уточните вручную' % policy,
             'Проверьте правило в /etc/mihomo/config.yaml.')

    # 4b. zapret: a direct domain is only protected from DPI if zapret processes it
    zap = zapret_route(domain)
    _policy = (hit or {}).get('policy', 'GLOBAL')
    if not zap['running']:
        step('Запрет (DPI)', 'warn',
             'выключен: служба zapret не запущена — запросы идут как есть, '
             'блокировки провайдера не обходятся',
             'Запустите запрет: /etc/init.d/zapret restart; если не поднимается — '
             'проверьте /opt/zapret/config и установлен ли zapret.')
    elif _policy in ('VPN', 'VPN-noP2P', 'AI'):
        step('Запрет (DPI)', 'ok',
             'не нужен: трафик идёт через туннель %s, провайдер видит только зашифрованный канал'
             % _policy)
    elif zap['excluded']:
        step('Запрет (DPI)', 'warn',
             'выключен: домен (или его зона) «%s» в списке исключений zapret — '
             'запрос идёт как есть, провайдер видит сайт' % zap['excluded'],
             'Уберите домен (или его родителя) из /opt/zapret/ipset/zapret-hosts-user-exclude.txt '
             'и перезапустите zapret: /etc/init.d/zapret restart')
    elif zap['listed']:
        route_path.insert(0, 'zapret (запрос маскируется)')
        if zap.get('queues'):
            step('Запрет (DPI)', 'ok',
                 'включён: домен в списке zapret (строка «%s»), nfqws работает и правила '
                 'очередей в nft есть — запрос идёт через обход DPI (режим %s)'
                 % (zap['listed'], zap['mode'] or '?'))
        else:
            step('Запрет (DPI)', 'warn',
                 'настроен, но не подтверждён: домен в списке, nfqws работает, а правил '
                 'очередей в nft не видно (строка «%s»)' % zap['listed'],
                 'Проверьте, что таблица inet zapret загружена: nft list table inet zapret; '
                 'перезапустите запрет: /etc/init.d/zapret restart')
    elif zap['auto']:
        route_path.insert(0, 'zapret (запрос маскируется)')
        step('Запрет (DPI)', 'ok',
             'включён: zapret добавил домен сам после нескольких неудачных попыток — '
             'запрос к сайту маскируется от провайдера')
    else:
        step('Запрет (DPI)', 'warn',
             'выключен: домена нет в списке zapret — запрос идёт как есть, '
             'провайдер видит сайт',
             'Добавьте домен в /opt/zapret/ipset/zapret-hosts-user.txt и перезапустите zapret: '
             '/etc/init.d/zapret restart (или включите режим autohostlist).')

    # 5. live probe - and the reason when it fails
    if live:
        probe = http_probe(domain)
        if probe['ok']:
            step('Проверка HTTP', 'ok', probe['detail'] + ' — сайт ответил (запрос с роутера)')
        elif probe['kind'] == 'http':
            step('Проверка HTTP', 'warn', probe['detail'] + ' — сайт ответил (запрос с роутера)',
                 'Сайт ответил HTTP %s: соединение проходит, это ответ самого сайта. '
                 'Откройте страницу в браузере — если и там та же ошибка, дело в сайте, '
                 'а не в маршруте.' % probe['code'])
        else:
            node = ''
            if policy in ('VPN', 'VPN-noP2P', 'AI'):
                state = group_state(policy) or {}
                node = state.get('now') or ''
            delay = node_delay(node) if node else None
            if probe['kind'] == 'timeout' and delay is not None:
                step('Проверка HTTP', 'warn', probe['detail'] + ' (с роутера)',
                     'Узел «%s» живой (отклик %d мс), но сайт не ответил за 20 с — обычно это '
                     'блокировка выходного адреса сайтом или его антибот. Смените узел в группе '
                     '%s (панель → Обзор) или проверьте сайт с телефона через тот же VPN.'
                     % (node, delay, policy))
            elif probe['kind'] == 'timeout':
                step('Проверка HTTP', 'warn', probe['detail'] + ' (с роутера)',
                     'Узел «%s» не ответил даже на служебный запрос — смените узел в группе %s '
                     '(панель → Обзор → «Сменить»), затем повторите трассу.' % (node or '?', policy or 'VPN'))
            elif probe['kind'] in ('tls', 'cert'):
                step('Проверка HTTP', 'warn', probe['detail'] + ' (с роутера)',
                     'TLS не поднялся: сайт мог отклонить выходной адрес или подменить сертификат. '
                     'Проверьте тот же адрес в браузере через VPN; если там всё открывается — '
                     'дело в проверке TLS у сайта, а не в маршруте.')
            elif probe['kind'] in ('reset', 'empty'):
                step('Проверка HTTP', 'warn', probe['detail'] + ' (с роутера)',
                     'Соединение сбросили: чаще всего сайт блокирует выходной адрес. '
                     'Смените узел в группе %s и повторите.' % (policy or 'VPN'))
            else:
                step('Проверка HTTP', 'warn', probe['detail'] + ' (с роутера)',
                     'Запрос не прошёл (%s). Повторите трассу; если повторяется — '
                     'Диагностика → «Сеть».' % probe['kind'])

    # 6. the hosts this page depends on (v1.30.2): the second domain is the one that matters
    if live:
        related = related_hosts(domain)
        if related:
            main_policy = (hit or {}).get('policy', 'GLOBAL')
            rows = []
            off = []
            for item in related:
                info = host_route(item['host'])
                kinds = '/'.join(item['kinds'])
                rows.append('%s (%s) → %s%s' % (
                    item['host'], kinds, info['policy'],
                    (' · ' + info['country']) if info['country'] else ''))
                if info['policy'] != main_policy:
                    off.append((item, info))
            detail = 'страница тянет данные с %d домен(ов): %s' % (len(rows), '; '.join(rows))
            # a picture on another path is fine; a form or an iframe carries the session
            session_off = [pair for pair in off if pair[0]['session']]
            if session_off and not probe['ok']:
                item, info = session_off[0]
                step('Связанные хосты', 'warn', detail,
                     '%s — это %s, он несёт сессию, а идёт другим путём (%s), чем %s (%s). '
                     'Если сайт не пускает или сбрасывает вход, перенесите его в ту же группу: '
                     'geomask-lists-edit.py move --group "<группа %s>" --domain %s'
                     % (item['host'], '/'.join(item['kinds']), info['policy'], domain,
                        main_policy, main_policy, item['host']))
            elif session_off:
                item, info = session_off[0]
                step('Связанные хосты', 'ok', detail,
                     '%s (%s) идёт другим путём, но страница ответила. Если вход или оплата '
                     'начнут сбрасываться — перенесите его в группу «%s».'
                     % (item['host'], '/'.join(item['kinds']), main_policy))
            elif off:
                names = ', '.join(item['host'] for item, _ in off)
                # the explanation belongs in the line itself: an "ok" step's advice is not
                # printed at the bottom, and this is exactly the question the owner asks
                step('Связанные хосты', 'ok',
                     detail + ' — другим путём только статика (%s), на работу сайта не влияет'
                     % names)
            else:
                step('Связанные хосты', 'ok', detail)
        else:
            # a 404 page or a non-HTML host simply has nothing to list: that is not a fault,
            # and the route above already answered the question
            if not probe['ok']:
                step('Связанные хосты', 'ok', 'не проверялось — сайт не ответил')
            else:
                step('Связанные хосты', 'ok',
                     'не найдено — страница не ссылается на другие домены')

    lines.append('')
    if route_path:
        summary = ' → '.join(route_path)
        if route_country:
            summary += '; страна %s' % route_country
    else:
        exit_step = next((item for item in steps if item['stage'] == 'Выход'), None)
        summary = exit_step['detail'] if exit_step else ''
    lines.append('Итог: %s' % summary)
    fix = ''
    for wanted in ('fail', 'warn'):
        for item in steps:
            if item['verdict'] == wanted and item.get('fix'):
                fix = item['fix']
                break
        if fix:
            break
    if fix:
        lines.append('Что делать: ' + fix)
    state = 'ok'
    for item in steps:
        if item['verdict'] == 'fail':
            state = 'fail'
            break
        if item['verdict'] == 'warn' and state == 'ok':
            state = 'warn'
    write_log('domain_trace', state, lines)
    return {'ok': True, 'domain': domain, 'state': state, 'steps': steps,
            'lines': lines, 'fix': fix, 'log': log_path('domain_trace')}


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
