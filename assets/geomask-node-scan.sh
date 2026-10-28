#!/bin/sh
# geomask-node-scan.sh - nightly subscription node audit (geomask v1.11.0+).
# Downloads the VPN subscription, resolves every node SERVER through a geo db
# (country + ASN), blocks nodes matching bad_countries/bad_asns by adding their
# exact names to the VPN group exclude-filter, unblocks clean ones, validates
# (mihomo -t) and hot-reloads ONLY when the filter changed. Never selects nodes,
# never touches traffic. Config: /etc/geomask-nodes.conf.
exec python3 - <<'PYEOF'
import re, sys, json, time, urllib.request, urllib.parse, subprocess, os

CONF='/etc/geomask-nodes.conf'
CFG='/etc/mihomo/config.yaml'
BLOCKF='/etc/geomask-node-block.txt'
STATE='/tmp/geomask-nodescan.state'
LOG='/root/geo-sync.log'

def log(msg):
    line='%s NODESCAN %s\n' % (time.strftime('%F %T'), msg)
    try:
        with open(LOG,'a') as f: f.write(line)
    except Exception: pass

cfgd={}
try:
    for ln in open(CONF, encoding='utf-8'):
        ln=ln.strip()
        if ln and not ln.startswith('#') and '=' in ln:
            k,_,v=ln.partition('='); cfgd[k.strip()]=v.strip()
except Exception as e:
    log('conf read failed: %s' % e); sys.exit(1)

bad_countries={c.strip().upper() for c in cfgd.get('bad_countries','RU,BY').split(',') if c.strip()}
bad_asns={('AS'+a.replace('AS','').strip().upper()) for a in cfgd.get('bad_asns','').split(',') if a.strip()}
base_filter=cfgd.get('base_filter','Россия')
provider=cfgd.get('sub_provider','sub')

text=open(CFG, encoding='utf-8').read()
# provider url (+ optional header x-hwid lines)
m=re.search(r'%s:\s*\n(?:[^\n]*\n)*?\s*url:\s*"?([^"\n]+)"?' % re.escape(provider), text)
if not m:
    log('provider %s url not found' % provider); sys.exit(1)
url=m.group(1).strip()
hdrs={}
hm=re.search(r'%s:\s*\n((?:[^\n]*\n)*?)\s*url:' % re.escape(provider), text)
if hm and 'x-hwid' in hm.group(1):
    hv=re.search(r'-\s*"?([^"\n]+)"?', hm.group(1)[hm.group(1).find('x-hwid'):])
    if hv: hdrs['x-hwid']=hv.group(1).strip()

try:
    req=urllib.request.Request(url, headers=hdrs)
    raw=urllib.request.urlopen(req, timeout=25).read().decode('utf-8','replace')
except Exception as e:
    log('subscription download failed: %s' % e); sys.exit(1)
sub_text=raw
if not re.search(r'(?m)^(vless|ss|trojan|vmess|anytls|hysteria2?)://', raw):
    try:
        import base64
        sub_text=base64.b64decode(raw.strip()+'===').decode('utf-8','replace')
    except Exception:
        pass

nodes=[]
for ln in sub_text.splitlines():
    ln=ln.strip()
    mm=re.match(r'^(vless|ss|trojan|vmess|anytls|hysteria2?)://\S+', ln, re.I)
    if not mm: continue
    frag=''
    if '#' in ln:
        frag=ln.split('#',1)[1]
        frag=urllib.parse.unquote(frag.split('|')[0]).strip()
    srv=None
    sm=re.search(r'@([A-Za-z0-9.\-]+):(\d+)', ln)
    if sm: srv=sm.group(1)
    if frag and srv and frag.lower()!='ip':
        nodes.append((frag, srv))
# yaml-style provider cache fallback
if not nodes:
    for nm,sv in re.findall(r'-\s*name:\s*"?([^"\n]+)"?\s*\n\s*server:\s*"?([^"\n]+)"?', sub_text):
        nodes.append((nm.strip(), sv.strip()))

if not nodes:
    log('no nodes parsed from subscription'); sys.exit(1)

# geodb per unique server (cached)
cache={}
def lookup(host):
    if host in cache: return cache[host]
    r=('', '')
    try:
        j=json.load(urllib.request.urlopen('http://ip-api.com/json/%s?fields=status,countryCode,as' % host, timeout=10))
        if j.get('status')=='success':
            r=(j.get('countryCode',''), (j.get('as','').split(' ')[0]).upper())
    except Exception:
        r=None
    cache[host]=r
    return r

blocked=set(); total=0; errs=0
for name, srv in nodes:
    total+=1
    res=lookup(srv)
    if res is None:
        errs+=1
        continue  # lookup failed: keep node as-is (do not block on doubt)
    cc, asn = res
    bad = (cc in bad_countries) or (asn in bad_asns)
    if bad:
        blocked.add(name)

# compose exclude-filter: base + exact blocked names (regex-escaped)
parts=[base_filter.strip()] if base_filter.strip() else []
for nm in sorted(blocked):
    parts.append(re.escape(nm))
new_filter='|'.join(parts)

def apply_filter(cfgtext, filt):
    # escape for double-quoted YAML scalar
    esc=filt.replace('\\','\\\\').replace('"','\\"')
    old=re.search(r'(exclude-filter: )"([^"]*)"|exclude-filter: ([^\n]*)', cfgtext)
    if not old: return cfgtext, False
    m=re.search(r'^( )*exclude-filter:.*$', cfgtext, re.M)
    indent=m.group(1) if m else '    '
    new_line='%sexclude-filter: "%s"' % (indent, esc)
    out=cfgtext[:m.start()]+new_line+cfgtext[m.end():]
    return out, out!=cfgtext

out, changed = apply_filter(text, new_filter)
if changed:
    open(CFG+'.bak_nodescan','w',encoding='utf-8').write(text)
    open(CFG+'.tmp','w',encoding='utf-8').write(out)
    try:
        rc=subprocess.run(['mihomo','-d','/etc/mihomo','-f',CFG+'.tmp','-t'], capture_output=True, timeout=30).returncode
    except Exception:
        rc=1
    if rc==0:
        os.replace(CFG+'.tmp', CFG)
        try:
            r=urllib.request.Request('http://127.0.0.1:9090/configs?force=true',
                data=json.dumps({'path':CFG}).encode(), method='PUT',
                headers={'Content-Type':'application/json'})
            urllib.request.urlopen(r, timeout=8).read()
        except Exception as e:
            log('reload failed: %s' % e)
        log('filter updated, blocked=%d total=%d lookup-errors=%d' % (len(blocked), total, errs))
    else:
        try: os.remove(CFG+'.tmp')
        except Exception: pass
        log('VALIDATION FAILED - config untouched (test rc=%d)' % rc)
else:
    log('no change (blocked=%d of %d, errors=%d)' % (len(blocked), total, errs))

# remember list + state for the panel
try:
    with open(BLOCKF,'w',encoding='utf-8') as f:
        f.write('\n'.join(sorted(blocked))+'\n')
except Exception: pass
st={'ts':time.strftime('%F %T'),'total':total,'blocked':len(blocked),'errors':errs,'changed':bool(changed)}
try:
    with open(STATE+'.tmp','w') as f: json.dump(st,f)
    os.replace(STATE+'.tmp', STATE)
except Exception: pass
print('nodes=%d blocked=%d errors=%d changed=%s' % (total, len(blocked), errs, bool(changed)))
PYEOF
