#!/bin/sh
# geomask-ytwatch.sh - YouTube edge auto-pin watchdog (geomask v1.14.0+).
#
# Why: googlevideo.com edges are IP-blocked per hostname and Google rotates those names,
# so any static list lags. Default path is DIRECT (DPI/zapret). This watcher reads mihomo's
# OWN dial errors and pins only the hosts that actually fail into the MagiTrickle group
# "GoogleVideo VPN" (which sync turns into ytvpn.yaml, matched BEFORE ytdirect). A nightly
# --reset drops the automatic pins so healthy edges return to zapret.
#
#   geomask-ytwatch.sh            scan + pin (cron */5)
#   geomask-ytwatch.sh --reset    remove automatic pins (cron, nightly)
#   geomask-ytwatch.sh --status   JSON for the panel
#   geomask-ytwatch.sh --mode auto|blanket   switch mode (also called by the panel)
#
# Config: /etc/geomask-ytwatch.conf. Nothing here selects proxies or touches traffic directly.
exec python3 - "$@" <<'PYEOF'
import json, os, re, socket, subprocess, sys, time, urllib.request, http.client

CONF='/etc/geomask-ytwatch.conf'
STATE='/tmp/geomask-ytwatch.state'
JOURNAL='/root/geo-sync.log'
SOCKPATH='/var/run/magitrickle.sock'
BLANKET='googlevideo.com'

def log(msg):
    try:
        with open(JOURNAL,'a',encoding='utf-8') as f:
            f.write('%s YTWATCH %s\n' % (time.strftime('%F %T'), msg))
    except Exception:
        pass

cfgd={}
try:
    for ln in open(CONF, encoding='utf-8'):
        ln=ln.strip()
        if ln and not ln.startswith('#') and '=' in ln:
            k,_,v=ln.partition('='); cfgd[k.strip()]=v.strip()
except Exception as e:
    log('conf read failed: %s' % e)

mode=cfgd.get('mode','auto')
threshold=max(1,int(cfgd.get('fail_threshold','2')))
window=max(5,int(cfgd.get('window_min','30')))
cap=max(1,int(cfgd.get('max_pins','40')))
group_name=cfgd.get('group_name','GoogleVideo VPN')

# ---------- MagiTrickle API over unix socket (handles chunked responses) ----------
class U(http.client.HTTPConnection):
    def connect(self):
        s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(8); s.connect(SOCKPATH); self.sock=s

def api(method, path, body=None):
    try:
        c=U('localhost')
        c.request(method, path, body=(json.dumps(body) if body is not None else None),
                  headers=({'Content-Type':'application/json'} if body is not None else {}))
        r=c.getresponse(); raw=r.read().decode('utf-8','replace'); c.close()
        try: return r.status, json.loads(raw)
        except Exception: return r.status, {'raw': raw[:200]}
    except Exception as e:
        return 0, {'error': str(e)}

def group_id(name):
    st,gs=api('GET','/api/v1/groups')
    if st!=200: return None
    for g in (gs.get('groups',[]) if isinstance(gs,dict) else []):
        if g.get('name')==name: return g.get('id')
    return None

def group_rules(gid):
    st,r=api('GET','/api/v1/groups/%s/rules'%gid)
    return (r.get('rules',[]) if isinstance(r,dict) else [])

def rule_state(gid):
    rs=group_rules(gid)
    blanket=[x for x in rs if str(x.get('rule','')).lower()==BLANKET]
    edges={str(x.get('rule','')).lower(): x.get('id') for x in rs
           if str(x.get('rule','')).endswith('googlevideo.com') and x not in blanket}
    return blanket, edges

# ---------- failure scan from mihomo's own log ----------
FAIL_RE=re.compile(r'msg="\[(?:TCP|UDP)\] dial \S+ \(match [^)]*\) \S+ --> ([a-z0-9.\-]+\.googlevideo\.com):\d+ error: ')
TIME_RE=re.compile(r'time="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')

def recent_failures():
    counts={}
    try:
        out=subprocess.run(['logread'], capture_output=True, timeout=20).stdout.decode('utf-8','replace')
    except Exception as e:
        log('logread failed: %s' % e); return counts
    cutoff=time.time()-window*60
    for ln in out.splitlines():
        m=FAIL_RE.search(ln)
        if not m: continue
        tm=TIME_RE.search(ln)
        if tm:
            try:
                ts=time.mktime(time.strptime(tm.group(1).split('.')[0].replace('T',' '), '%Y-%m-%d %H:%M:%S'))
                if ts < cutoff: continue
            except Exception:
                pass
        host=m.group(1).lower()
        counts[host]=counts.get(host,0)+1
    return counts

# ---------- state (which pins are automatic) ----------
def load_state():
    try:
        with open(STATE,encoding='utf-8') as f: return json.load(f)
    except Exception: return {'pins': [], 'last': None, 'failures': {}}

def save_state(st):
    st['last']=time.strftime('%F %T')
    try:
        with open(STATE+'.tmp','w',encoding='utf-8') as f: json.dump(st,f)
        os.replace(STATE+'.tmp', STATE)
    except Exception as e:
        log('state write failed: %s' % e)

# ---------- actions ----------
def sync_now():
    try:
        subprocess.run(['sh','/root/sync_mtrickle_mihomo.sh'], capture_output=True, timeout=60)
    except Exception as e:
        log('sync failed: %s' % e)

def do_status():
    gid=group_id(group_name)
    out={'mode': mode, 'group': group_name, 'group_found': bool(gid),
         'threshold': threshold, 'window_min': window, 'max_pins': cap}
    st=load_state()
    out['auto_pins']=sorted(st.get('pins',[]))
    if gid:
        blanket, edges = rule_state(gid)
        out['blanket_present']=bool(blanket)
        out['pinned_hosts']=sorted(edges.keys())
        out['pinned_count']=len(edges)
    return out

def set_mode(new_mode):
    global mode
    if new_mode not in ('auto','blanket'):
        log('bad mode %r' % new_mode); return False
    gid=group_id(group_name)
    if not gid:
        log('group %s not found' % group_name); return False
    blanket, edges = rule_state(gid)
    if new_mode=='blanket':
        if not blanket:
            api('POST','/api/v1/groups/%s/rules?save=true'%gid,
                {'type':'namespace','rule':BLANKET,'name':'','enable':True})
            log('mode=blanket: added blanket rule')
    else:
        for x in blanket:
            api('DELETE','/api/v1/groups/%s/rules/%s?save=true'%(gid,x['id']))
            log('mode=auto: removed blanket rule')
    # persist the choice
    try:
        t=open(CONF,encoding='utf-8').read()
        t=re.sub(r'(?m)^mode=.*$','mode=%s'%new_mode, t)
        open(CONF,'w',encoding='utf-8',newline='\n').write(t)
    except Exception as e:
        log('mode persist failed: %s' % e)
    mode=new_mode
    sync_now()
    return True

def do_reset():
    gid=group_id(group_name)
    if not gid:
        log('reset: group %s not found' % group_name); return 0
    st=load_state()
    blanket, edges = rule_state(gid)
    removed=0
    for host in st.get('pins',[]):
        rid=edges.get(host.lower())
        if rid:
            api('DELETE','/api/v1/groups/%s/rules/%s?save=true'%(gid,rid)); removed+=1
    st['pins']=[]
    save_state(st)
    if removed:
        log('reset: released %d auto-pins back to zapret' % removed)
        sync_now()
    return removed

def do_scan():
    gid=group_id(group_name)
    if not gid:
        log('group %s not found - nothing to do' % group_name); return
    st=load_state()
    if mode!='auto':
        # in blanket mode everything is already on VPN; just keep the record fresh
        st['failures']=recent_failures(); save_state(st); return
    blanket, edges = rule_state(gid)
    if blanket:
        log('mode=auto but blanket rule present - removing it')
        for x in blanket:
            api('DELETE','/api/v1/groups/%s/rules/%s?save=true'%(gid,x['id']))
        sync_now()
        blanket, edges = rule_state(gid)
    counts=recent_failures()
    st['failures']=counts
    added=[]
    for host,n in sorted(counts.items()):
        if n < threshold: continue
        if host in edges: continue
        if len(edges)+len(added) >= cap:
            log('max_pins=%d reached, %d candidate(s) not pinned' % (cap, len(counts)))
            break
        r=api('POST','/api/v1/groups/%s/rules?save=true'%gid,
              {'type':'domain','rule':host,'name':'ytwatch','enable':True})
        if r[0]==200:
            added.append(host); edges[host]=True
    if added:
        st['pins']=sorted(set(st.get('pins',[])) | set(added))
        save_state(st)
        log('pinned %d edge(s): %s' % (len(added), ', '.join(added[:6]) + ('…' if len(added)>6 else '')))
        sync_now()
    else:
        save_state(st)

args=[a for a in sys.argv[1:]]
if '--status' in args:
    print(json.dumps(do_status(), ensure_ascii=False)); sys.exit(0)
if '--reset' in args:
    do_reset(); sys.exit(0)
if '--mode' in args:
    i=args.index('--mode')
    ok=set_mode(args[i+1] if i+1 < len(args) else '')
    print(json.dumps({'ok': ok, 'mode': mode})); sys.exit(0 if ok else 1)
do_scan()
PYEOF
