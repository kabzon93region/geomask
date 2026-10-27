#!/bin/sh
# Sync magitrickle (enabled groups with interface Mihomo) -> TWO mihomo rule-provider files:
#   /etc/mihomo/ruleset/mtrickle.yaml  (non-AI groups)  -> group VPN (sub #1 vlessi)
#   /etc/mihomo/ruleset/ai.yaml        (Google/AI)      -> group AI (sub #2 de5, Germany)
# AI detection: whole groups by name + domain patterns (AI domains live inside Geoblock too).
# Reloads mihomo only on change.
exec python3 - <<'PYEOF'
import os, re, json, sys, time, urllib.request

SRC = '/etc/magitrickle/state/config.yaml'
MT  = '/etc/mihomo/ruleset/mtrickle.yaml'
AI  = '/etc/mihomo/ruleset/ai.yaml'

# Whole groups routed to the German AI subscription.
AI_GROUPS = {'Google+AI to DE'}
# AI system/check domains that live inside non-AI groups (e.g. Geoblock).
AI_DOMAIN_RE = re.compile(
    r'^(?:[\w-]+\.)*(openai\.com|chatgpt\.com|claude\.ai|claude\.com|anthropic\.com'
    r'|oaistatic\.com|oaiusercontent\.com|sora\.com|suno\.ai|suno\.com|cursor\.com'
    r'|cursor\.sh|cursorapi\.com|statsig\.com|statsigapi\.net)$', re.IGNORECASE)
# Extra safety net (kept from previous version).
# ECH (Encrypted Client Hello) hides the real SNI; the outer ClientHello carries a
# public name instead (Cloudflare uses cloudflare-ech.com). Routing that public name
# via AI keeps every ECH handshake on the German node instead of leaking to DIRECT.
AI_EXTRA = ['DOMAIN-SUFFIX,statsig.com', 'DOMAIN-SUFFIX,statsigapi.net',
            'DOMAIN-SUFFIX,cloudflare-ech.com']

lines = open(SRC, encoding='utf-8').read().splitlines()
section = ''
grp = None
rule = None
GRPS = []

def flush_rule():
    global rule
    if rule is not None and grp is not None:
        grp['rules'].append(rule)
    rule = None

def flush_group():
    global grp
    flush_rule()
    if grp is not None:
        GRPS.append(grp)
    grp = None

for ln in lines:
    if not ln.strip():
        continue
    s = ln.lstrip()
    ind = len(ln) - len(s)
    if ind == 0 and not s.startswith('-') and ':' in s:
        flush_group()
        section = s.split(':', 1)[0]
        continue
    if section not in ('groups', 'subscriptions'):
        continue
    if ind == 0 and s.startswith('- id:'):
        flush_group()
        grp = {'interface': '', 'enable': 'true', 'name': '', 'rules': []}
        continue
    if grp is None:
        continue
    if ind == 2 and s.startswith('- '):
        flush_rule()
        rule = {'enable': 'true', 'type': '', 'rule': ''}
        k, _, v = s[2:].partition(':')
        k = k.strip()
        if k in ('type', 'rule', 'enable'):
            rule[k] = v.strip().strip('"').strip("'")
        continue
    if rule is not None and ind >= 4 and s.split(':', 1)[0] in ('type', 'rule', 'enable'):
        k, _, v = s.partition(':')
        rule[k.strip()] = v.strip().strip('"').strip("'")
        continue
    if ind == 2:
        flush_rule()
        k, _, v = s.partition(':')
        k = k.strip()
        if k in ('interface', 'enable', 'name'):
            grp[k] = v.strip().strip('"').strip("'")
flush_group()

def cidr(v, v6=False):
    v = v.strip()
    if '/' in v:
        return v
    return v + ('/128' if v6 else '/32')

def emit(r):
    t = r.get('type', '')
    v = r.get('rule', '').strip()
    if not v:
        return None
    if t == 'namespace':
        return 'DOMAIN-SUFFIX,' + v.lower()
    if t == 'domain':
        return 'DOMAIN,' + v.lower()
    if t == 'regex':
        return 'DOMAIN-REGEX,' + v
    if t == 'wildcard':
        pat = ''.join('.*' if c == '*' else '.' if c == '?' else re.escape(c) for c in v)
        return 'DOMAIN-REGEX,' + pat
    if t == 'subnet':
        return 'IP-CIDR,' + cidr(v) + ',no-resolve'
    if t == 'subnet6':
        return 'IP-CIDR6,' + cidr(v) + ',no-resolve'
    return None

def is_ai_domain(e):
    # DOMAIN-SUFFIX,x / DOMAIN,x -> test x against AI pattern
    m = re.match(r'^DOMAIN(?:-SUFFIX)?,(.+)$', e)
    return bool(m and AI_DOMAIN_RE.match(m.group(1)))

mt_payload, ai_payload = [], []
seen_mt, seen_ai = set(), set()
moved = 0
for g in GRPS:
    if g.get('interface') != 'Mihomo' or str(g.get('enable')).lower() != 'true':
        continue
    is_ai = g.get('name') in AI_GROUPS
    for r in g['rules']:
        if str(r.get('enable')).lower() != 'true':
            continue
        e = emit(r)
        if not e:
            continue
        if is_ai:
            if e not in seen_ai:
                seen_ai.add(e)
                ai_payload.append(e)
        else:
            if is_ai_domain(e):
                if e not in seen_ai:
                    seen_ai.add(e)
                    ai_payload.append(e)
                    moved += 1
                continue
            if e not in seen_mt:
                seen_mt.add(e)
                mt_payload.append(e)

for e in AI_EXTRA:
    if e not in seen_ai:
        seen_ai.add(e)
        ai_payload.append(e)

if not mt_payload or not ai_payload:
    print('ERROR: empty payload (mt=%d ai=%d), refusing to overwrite' % (len(mt_payload), len(ai_payload)))
    sys.exit(1)

hdr = '# AUTO-GENERATED by /root/sync_mtrickle_mihomo.sh from magitrickle config - DO NOT EDIT\n'

def write_if_changed(path, payload):
    body = hdr + 'payload:\n' + ''.join('  - %s\n' % p for p in payload)
    old = open(path).read() if os.path.exists(path) else None
    if old == body:
        return False, len(payload)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    open(tmp, 'w').write(body)
    os.replace(tmp, path)
    return True, len(payload)

c1, n1 = write_if_changed(MT, mt_payload)
c2, n2 = write_if_changed(AI, ai_payload)
print('mtrickle.yaml: %d rules (%s)' % (n1, 'updated' if c1 else 'up-to-date'))
print('ai.yaml:       %d rules (%s, + %d moved from non-AI groups by domain pattern)' % (n2, 'updated' if c2 else 'up-to-date', moved))

if c1 or c2:
    _r_ok = False
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:9090/configs?force=true',
            data=json.dumps({'path': '/etc/mihomo/config.yaml'}).encode(),
            method='PUT', headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5).read()
        _r_ok = True
        print('mihomo reloaded')
    except Exception as e:
        print('reload FAILED: %s (restart needed)' % e)

# v1.10.0: machine-readable last-run state for the panel; journal line only on real change
try:
    _st = {'ts': time.strftime('%F %T'), 'mt': n1, 'ai': n2, 'moved': moved,
           'changed': bool(c1 or c2), 'reload': _r_ok if (c1 or c2) else None}
    with open('/tmp/geomask-sync.state.tmp', 'w') as f:
        json.dump(_st, f)
    os.replace('/tmp/geomask-sync.state.tmp', '/tmp/geomask-sync.state')
except Exception:
    pass
if c1 or c2:
    try:
        with open('/root/geo-sync.log', 'a') as f:
            f.write('%s SYNC changed: VPN=%d AI=%d moved-to-AI=%d reload=%s\n' %
                    (_st['ts'], n1, n2, moved, 'ok' if _r_ok else 'FAILED'))
    except Exception:
        pass
PYEOF
