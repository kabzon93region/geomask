#!/bin/sh
# geomask-dnscheck.sh - read-only DNS safety audit (geomask v1.22.1+).
#
#   geomask-dnscheck.sh            full audit (probes the network)
#   geomask-dnscheck.sh --quick    configuration only: NO network calls at all
#
# Exit contract (R10a):
#   0  every check this mode performs ran and passed (warnings do not fail the audit)
#   1  at least one performed check found a problem (FAIL)
#   2  UNKNOWN: a check could not be performed (no nslookup, no usable LAN address,
#      no python3, no mihomo config, an unreadable /etc/hosts) - never a silent OK
#
# It exists because of two incidents seen on routers of this kind:
#   1) a DoH proxy and an ad blocker both force-redirect port 53, the proxy bootstraps its
#      own server name through that redirected port, and LAN DNS stops answering entirely
#      (symptom: "site address not found", fixed by turning any VPN on);
#   2) a "bypass" address pinned into /etc/hosts goes stale, so every client keeps dialling
#      a dead IP and the site breaks even though the tunnel itself is fine.
# Nothing here changes anything: it only reports.
#
# R10a fixes:
#   * the nslookup exit code is checked, and the answer is read only AFTER `Name:` - the
#     server `Address:` line is never an answer; both `Address:` and BusyBox `Address N:`
#     shapes are understood;
#   * the LAN resolver address comes from UCI `network.lan.device`/`ifname` + `ip -4 addr
#     show` (fallback `br-lan`), never from a hardcoded 192.168.0.1;
#   * `--quick` really makes no outbound probe (the old code still ran two nslookups);
#   * a missing python3 or a missing /etc/mihomo/config.yaml is UNKNOWN (rc 2), not "working".
#
# Environment overrides exist for the Windows-hosted tests only; the router never sets them:
#   GM_MIHOMO_CONFIG  mihomo config (default /etc/mihomo/config.yaml)
#   GM_HOSTS          hosts file (default /etc/hosts)
#   GM_NET_DIR        tunnel sysfs directory (default /sys/class/net)
#   GM_RULESET_DIR    mihomo ruleset directory (default /etc/mihomo/ruleset)
#   GM_PYTHON         python3 binary (default python3)
QUICK=${1:-}
FAIL=0; WARN=0; UNKNOWN=0
ok()   { printf '  OK    %s\n' "$1"; }
warn() { printf '  WARN  %s\n' "$1"; WARN=$((WARN+1)); }
bad()  { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }
unknown() { printf '  UNKNOWN  %s\n' "$1"; UNKNOWN=$((UNKNOWN+1)); }
skip() { printf '  SKIP  %s\n' "$1"; }

MIHOMO_CONF=${GM_MIHOMO_CONFIG:-/etc/mihomo/config.yaml}
HOSTS_FILE=${GM_HOSTS:-/etc/hosts}
NET_DIR=${GM_NET_DIR:-/sys/class/net}
PYTHON=${GM_PYTHON:-python3}

echo "=== DNS safety check  $(date '+%F %T') ==="

echo
echo "--- 1) is the resolver actually alive ---"
# The answer is a line AFTER `Name:`. The first `Address:` line of nslookup output is the
# server that was asked; taking it made a SERVFAIL look like a working resolver (G01).
resolver_answer() { # <server>; prints the answer address, empty when the exchange failed
  _out=$(nslookup yandex.ru "$1" 2>/dev/null)
  _rc=$?
  [ "$_rc" -eq 0 ] || return 1
  printf '%s\n' "$_out" | awk '
    /^Name:/ { seen = 1; next }
    seen && /^Address[ \t]*[0-9]*:/ {
      sub(/^Address[ \t]*[0-9]*:[ \t]*/, "")
      if ($1 != "") { print $1; exit }
    }'
}
check_resolver() { # <server> <label>
  _ans=$(resolver_answer "$1")
  if [ -n "$_ans" ]; then
    ok "dnsmasq answers on $2 ($_ans)"
  else
    bad "dnsmasq does NOT answer on $2 - LAN clients cannot resolve anything"
  fi
}
if [ "$QUICK" = "--quick" ]; then
  skip "resolver liveness probes (--quick makes no network calls)"
else
  if ! command -v nslookup >/dev/null 2>&1; then
    unknown "nslookup is missing - the resolver cannot be probed"
  else
    check_resolver 127.0.0.1 "127.0.0.1:53"
    LAN_DEV=$(uci get network.lan.device 2>/dev/null)
    [ -n "$LAN_DEV" ] || LAN_DEV=$(uci get network.lan.ifname 2>/dev/null)
    for _lan in $LAN_DEV; do LAN_DEV=$_lan; break; done
    [ -n "$LAN_DEV" ] || LAN_DEV=br-lan
    LAN_IP=''
    if command -v ip >/dev/null 2>&1; then
      LAN_IP=$(ip -4 addr show dev "$LAN_DEV" 2>/dev/null \
        | awk '/^[[:space:]]*inet /{sub(/\/.*/, "", $2); print $2; exit}')
    fi
    if [ -n "$LAN_IP" ]; then
      check_resolver "$LAN_IP" "$LAN_IP:53 (LAN $LAN_DEV)"
    else
      unknown "cannot determine the LAN address of $LAN_DEV via UCI/ip - the LAN probe did not run"
    fi
  fi
  if command -v pgrep >/dev/null 2>&1; then
    if ! pgrep -f dnsmasq >/dev/null 2>&1; then bad "no dnsmasq process at all"; fi
  else
    unknown "pgrep is missing - cannot check that dnsmasq runs"
  fi
fi

echo
echo "--- 2) who owns port 53, and is anything redirected into a loop ---"
if ! command -v netstat >/dev/null 2>&1; then
  unknown "netstat is missing - cannot check who listens on :53"
else
  LIST=$(netstat -lntu 2>/dev/null | grep -cE ':53[[:space:]]')
  [ -n "$LIST" ] || LIST=0
  if [ "$LIST" -gt 0 ]; then ok "port 53 has a listener ($LIST socket(s))"
  else bad "nothing listens on :53"; fi
fi
DOH_PORT=$(uci get https-dns-proxy.@https-dns-proxy[0].listen_port 2>/dev/null)
[ -n "$DOH_PORT" ] && echo "  https-dns-proxy listens on :$DOH_PORT"
# the loop shape: a redirect of :53 that lands on the DoH proxy, while the proxy resolves its
# own server names through :53
NFORCE=0
for pkg in https-dns-proxy adblock-fast; do
  v=$(uci get $pkg.config.force_dns 2>/dev/null)
  [ "$v" = "1" ] && { NFORCE=$((NFORCE+1)); echo "  $pkg claims force_dns"; }
done
# NOTE: use if/elif here. The old `cond && warn ... || ok ...` chain printed BOTH lines,
# because warn() succeeds, so the following || branch still ran.
if [ "$NFORCE" -gt 1 ]; then
  warn "$NFORCE packages both force-redirect DNS - one of them must win, and the usual outcome is a loop"
elif [ "$NFORCE" = "1" ]; then
  ok "exactly one package forces DNS redirection"
else
  ok "no package forces DNS redirection"
fi
if [ -n "$DOH_PORT" ]; then
  if ! command -v nft >/dev/null 2>&1; then
    unknown "nft is missing - cannot check the :53 redirect into https-dns-proxy"
  else
    REDIR=$(nft list ruleset 2>/dev/null | grep -E 'dport 53' | grep -c "redirect to :$DOH_PORT")
    [ -n "$REDIR" ] || REDIR=0
    if [ "$REDIR" -gt 0 ]; then
      warn "port 53 is redirected to the DoH proxy :$DOH_PORT - if its bootstrap also uses :53, DNS dies"
    else
      ok "no :53 -> :$DOH_PORT redirect (proxy is not in the resolution path)"
    fi
  fi
fi

echo
echo "--- 3) can the DoH proxy bootstrap at all (this is what breaks silently) ---"
BOOT=$(uci get https-dns-proxy.@https-dns-proxy[0].bootstrap_dns 2>/dev/null)
if [ -n "$BOOT" ]; then
  echo "  bootstrap_dns = $BOOT"
  if [ "$QUICK" = "--quick" ]; then
    skip "bootstrap reachability probe (--quick)"
  else
    FIRST=$(echo "$BOOT" | cut -d, -f1 | tr -d ' ')
    case "$FIRST" in
      *[a-zA-Z]*) warn "bootstrap is a hostname ($FIRST) - it needs working DNS to get working DNS" ;;
      *)
        if ! command -v "$PYTHON" >/dev/null 2>&1; then
          unknown "python3 is missing - the bootstrap UDP:53 probe did not run"
        else
          R=$("$PYTHON" - "$FIRST" <<'PY'
import socket, sys
ip=sys.argv[1]
try:
    s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(4)
    # a DNS query for the root NS over UDP; no reply means the ISP drops port 53
    q=bytes.fromhex('abcd01000001000000000000') + b'\x07iso-3166\x02eu\x00\x00\x0c\x00\x01'
    s.sendto(q,(ip,53)); s.recv(512); print('ok')
except Exception:
    print('dead')
PY
)
          _py_rc=$?
          R=$(printf '%s' "$R" | tr -d '\r')
          if [ "$_py_rc" -ne 0 ]; then
            unknown "the bootstrap probe did not return a verdict (python3 rc=$_py_rc)"
          elif [ "$R" = "ok" ]; then
            ok "bootstrap $FIRST answers over UDP:53"
          else
            warn "bootstrap $FIRST does NOT answer over UDP:53 - if the proxy ever takes over DNS, clients will hang"
          fi
        fi
        ;;
    esac
  fi
else
  ok "no https-dns-proxy bootstrap configured"
fi
FAM=$(uci get https-dns-proxy.config.force_ip_family 2>/dev/null)
if [ "$FAM" = "auto" ]; then
  warn "force_ip_family=auto - pin it to 4 (or 6) so the proxy cannot pick a path it redirects itself"
elif [ -n "$FAM" ]; then
  ok "force_ip_family=$FAM"
fi

echo
echo "--- 4) stale bypass pins in /etc/hosts (the Telegram case) ---"
if [ ! -r "$HOSTS_FILE" ]; then
  unknown "$HOSTS_FILE is unreadable - the pinned-address check did not run"
elif [ "$QUICK" = "--quick" ]; then
  PINS=$(grep -cE '^[0-9]' "$HOSTS_FILE" 2>/dev/null)
  [ -n "$PINS" ] || PINS=0
  echo "  pinned address lines: $PINS"
  skip "pin liveness probe (--quick)"
else
  if ! command -v "$PYTHON" >/dev/null 2>&1; then
    unknown "python3 is missing - the /etc/hosts pin liveness check did not run"
  else
    PY_OUT=$("$PYTHON" - "$HOSTS_FILE" <<'PY'
import socket, os, re, subprocess, sys
hosts_file = sys.argv[1]
ruleset_dir = os.environ.get('GM_RULESET_DIR', '/etc/mihomo/ruleset')
# A pin whose names are routed through a subscription cannot be judged from the router: the
# whole point of those addresses is that a foreign exit reaches them. Probing them directly
# produced a false alarm for the AI pins (45.155.204.190), so tunneled names are now tested
# through the local proxy chain instead, and only non-tunneled names are judged directly.
tunneled=set()
for f in os.listdir(ruleset_dir) if os.path.isdir(ruleset_dir) else []:
    if not f.endswith('.yaml'): continue
    try: txt=open(os.path.join(ruleset_dir, f), encoding='utf-8', errors='replace').read()
    except Exception: continue
    for m in re.finditer(r'DOMAIN-SUFFIX,\s*([a-z0-9.\-]+)', txt):
        tunneled.add(m.group(1).lower())
def is_tunneled(names):
    return any(n == t or n.endswith('.'+t) for n in names for t in tunneled)
def via_chain(host):
    # 7894 is the TORRENT listener, not a general path - judging a pin by it produced false
    # alarms (rutor.info answers 200 directly and through both lanes, but 000 via 7894).
    # A tunneled pin is only dead if no lane can serve it at all.
    for port in (7891, 7892, 7894):
        try:
            code=subprocess.run(['curl','-s','-o','/dev/null','-m','12','-x','http://127.0.0.1:%d'%port,
                                 '-w','%{http_code}','https://%s/'%host],
                                capture_output=True, text=True).stdout
            if code.isdigit() and code != '000':
                return True
        except Exception:
            pass
    return False
dead_direct=[]; dead_chain=[]; alive=0; tun=0
seen=set()
for ln in open(hosts_file, encoding='utf-8', errors='replace'):
    ln=ln.strip()
    if not ln or ln.startswith('#'): continue
    p=ln.split()
    if len(p) < 2: continue
    ip=p[0]; names=[x.lower() for x in p[1:]]
    if not ip[0].isdigit() or ip in ('127.0.0.1','0.0.0.0') or ip in seen: continue
    seen.add(ip)
    if is_tunneled(names):
        tun += 1
        host=names[0]
        if not via_chain(host): dead_chain.append((ip, host))
        else: alive += 1
        continue
    ok=False
    for port in (443, 80):
        try:
            s=socket.create_connection((ip,port), timeout=3); s.close(); ok=True; break
        except Exception: pass
    if ok: alive += 1
    else: dead_direct.append((ip, names[0], len(names)))
print('  pinned addresses: %d total (%d of them belong to tunneled names)' % (len(seen), tun))
for ip, host in dead_chain:
    print('  WARN  %s (%s) is pinned and tunneled, but NO lane can serve it' % (ip, host))
for ip, host, n in dead_direct[:10]:
    print('  WARN  %s (serves %d name%s, e.g. %s) answers neither directly - clients dial a dead address'
          % (ip, n, 's' if n>1 else '', host))
if not dead_chain and not dead_direct:
    print('  all pins reachable (directly, or through the lane that serves them)')
PY
)
    _py_rc=$?
    PY_OUT=$(printf '%s\n' "$PY_OUT" | tr -d '\r')
    if [ "$_py_rc" -ne 0 ]; then
      unknown "the pin liveness check did not return a verdict (python3 rc=$_py_rc)"
      printf '%s\n' "$PY_OUT"
    else
      printf '%s\n' "$PY_OUT"
      _dead=$(printf '%s\n' "$PY_OUT" | grep -c '^  WARN')
      case "$_dead" in ''|*[!0-9]*) _dead=0 ;; esac
      WARN=$((WARN + _dead))
    fi
  fi
fi

echo
echo "--- 5) the tunnels must not be asked to resolve themselves ---"
if [ ! -e "$MIHOMO_CONF" ]; then
  unknown "no $MIHOMO_CONF - cannot verify which resolver mihomo uses"
elif [ ! -r "$MIHOMO_CONF" ]; then
  unknown "$MIHOMO_CONF is unreadable - cannot verify which resolver mihomo uses"
elif grep -qE '^dns:' "$MIHOMO_CONF" 2>/dev/null; then
  warn "mihomo has its own dns: section - verify it does not point at the redirected port"
else
  ok "mihomo uses the system resolver (no own dns section to loop through)"
fi
for t in Mihomo AI VPN-noP2P; do
  if [ -e "$NET_DIR/$t" ]; then echo "  tunnel $t: present"; else warn "tunnel $t missing"; fi
done

echo
if [ "$FAIL" -gt 0 ]; then
  echo "=== RESULT: $FAIL problem(s) - DNS is likely broken for clients ==="
  exit 1
fi
if [ "$UNKNOWN" -gt 0 ]; then
  echo "=== RESULT: UNKNOWN - $UNKNOWN check(s) could not be performed (not a silent OK) ==="
  exit 2
fi
if [ "$WARN" -gt 0 ]; then
  echo "=== RESULT: working, but $WARN warning(s) above are worth a look ==="
else
  echo "=== RESULT: DNS looks healthy ==="
fi
exit 0
