#!/bin/sh
# geo-backend.sh - backend for LuCI "Гео-маскировка и AI" panel (geo-sync ubus object).
# Commands:
#   status | leases | log | sync_lists | google | backups | stack | nodescan
#   nodes_get | nodes_set <0|1> <HH:MM> <countries> <asns> <base> <provider>
#   sync_set <N> | syncd_set <on|off> | pin_set <on|off>   (v1.12.0 automation controls)
#   lists_get | lists_seed   (v1.13.0 bundled MagiTrickle lists template: status + restore)
#   global <direct|vpn> | ipv6 <on|off>
#   dev_add <ip> <mode> [mac] [name] | dev_del <ip>
#     modes: bypass (real IP) | realtz (VPN, own tz) | ai (FULLY via AI subscription)
#   ai_sub_get                       (which provider the AI group rides; AI owns no subscription)
#   subs_list | sub_add <name> <url> | sub_del <id>
#   ai_sub_use <id> | vpn_sub_use <id>  (switch provider url from registry, no mihomo restart)
#   upd_check [force] | upd_status | upd_apply   (self-update from GitHub releases, v1.7.0+)
# Device modes (per-device geo policy):
#   bypass  - device routes DIRECT (real IP), beats fwmark+Mihomo; real tz (GeoSync off on PC)
#   realtz  - device routes via mihomo (masked IP), but PC keeps real tz (GeoSync off on PC)
# NOTE: router timezone is NOT used for geo-masking (Google reads the PC browser tz), so no
#       tz commands here. PC-side tz is handled by GeoSync.ps1 on each PC.
# Output: JSON on stdout. Exit 0.

DEVICES=/etc/geo-devices.list
EXCL_OLD=/etc/geo-exclude.list   # legacy (ip name) -> migrated to DEVICES as bypass
SUBS_FILE=/etc/geomask-subs.list # registry: id|name|url (one per line)
BACKUP_DIR=${GM_BACKUP_DIR:-/root/geomask-backups}
# The backup folder is published over SMB only after the owner opts in with this marker; the
# diagnostic bundle must not land there without it (review 15.11, #14).
SMB_SHARE_ENABLE=${GM_SMB_SHARE_ENABLE:-/etc/geomask/backup-share.enable}
# How long a bundle may sit in the temporary directory before it is removed (hours).
CONSOLE_DIAG_KEEP_H=${GM_CONSOLE_DIAG_KEEP:-24}
DHCP_OWNED=/etc/geomask/dhcp-hosts   # id|ip|mac of host sections geomask created
# Device/subscription lists are read-modify-written by the panel and the guard. The RMW is
# serialised by the shared kernel `devices` lock and the temporary file is per-process, so two
# writers cannot install a half-rewritten list or overwrite each other's temp (report R08).
DEVICES_LOCK=${GM_DEVICES_LOCK:-/tmp/geomask-locks/devices.lock}
AI_TUN_DEV=AI
AI_TUN_IP=198.19.0.1
AI_SOCKS_PORT=7891
LOG=/root/geo-sync.log
MIHOMO_API=http://127.0.0.1:9090
MIHOMO_CONF=/etc/mihomo/config.yaml
TUN=http://127.0.0.1:7890
SYNCLISTS=/root/sync_mtrickle_mihomo.sh
AI_RULES=/etc/mihomo/ruleset/ai.yaml
MT_RULES=/etc/mihomo/ruleset/mtrickle.yaml
BACKEND_VERSION="@@BACKEND_VERSION@@"
# self-update state (panel button): check cache, status file, detached runner
UPD_DIR=/tmp/geomask-upd
# safe_write resources
SAFE_WRITE_PY=/usr/bin/safe_write.py
# Shared route-table helper (also sourced by /etc/hotplug.d/iface/99-default-vpn-route).
GM_ROUTES_LIB=${GM_ROUTES_LIB:-/usr/lib/geomask/geomask-routes.sh}
# The one lock helper (R04): kernel flock on a persistent inode - route tables, the console
# lane, crontab/devices read-modify-write, the updater and the config transaction share the
# same primitive and the same rank order. No PID heuristics, no stale-lock reclaim.
GM_LOCK_LIB=${GM_LOCK_LIB:-/usr/lib/geomask/geomask-lock.sh}
if ! command -v gm_lock_acquire >/dev/null 2>&1 && [ -r "$GM_LOCK_LIB" ]; then
  . "$GM_LOCK_LIB"
fi

# ---- write gate ------------------------------------------------------------
# The project rule is "a full settings backup before ANY change on the router". The panel
# used to change uci, dhcp, dns and network without one. Only an archive that a backup
# creator verified and recorded in the marker file counts - a bare file with a fresh mtime
# proves nothing. There is no environment bypass, so tests point GM_BACKUP_DIR at their tree.
BACKUP_OK=${GM_BACKUP_OK:-/etc/geomask/backup-ok}

_write_backup_marker() { # _write_backup_marker <verified-archive>
  mkdir -p "$(dirname "$BACKUP_OK")" 2>/dev/null || return 1
  { printf 'file=%s\n' "$1"; printf 'ts=%s\n' "$(date +%s)"; } > "$BACKUP_OK.tmp" 2>/dev/null || return 1
  mv -f "$BACKUP_OK.tmp" "$BACKUP_OK" 2>/dev/null
}

_write_gate() { # _write_gate [max-age-seconds]
  _wg_max=${1:-1800}
  [ -f "$BACKUP_OK" ] || return 1
  _wg_file=$(sed -n 's/^file=//p' "$BACKUP_OK" 2>/dev/null | head -1)
  [ -n "$_wg_file" ] || return 1
  [ -f "$_wg_file" ] || return 1
  case "$_wg_file" in "$BACKUP_DIR"/router-backup-*.tar.gz) ;; *) return 1 ;; esac
  _wg_m=$(stat -c %Y "$_wg_file" 2>/dev/null || echo 0)
  case "$_wg_m" in ''|*[!0-9]*) return 1 ;; esac
  _wg_now=$(date +%s 2>/dev/null || echo 0)
  [ $((_wg_now - _wg_m)) -le "$_wg_max" ]
}

_require_backup() {
  _write_gate 1800 && return 0
  printf '{"ok":false,"error":"нужен свежий полный бэкап: нажмите «Создать бэкап» в панели (вкладка «Бэкапы»)"}\n'
  return 1
}

# Create a full router backup from the panel. Same checks as the installer: the archive must
# be a readable tar with the router tree, and it is recorded as verified before it counts.
cmd_backup() {
  mkdir -p "$BACKUP_DIR" 2>/dev/null || { printf '{"ok":false,"error":"cannot create %s"}\n' "$BACKUP_DIR"; return 1; }
  _ts=$(date +%Y%m%d-%H%M%S 2>/dev/null || echo bak)
  _tmp="/tmp/geomask-bk-$_ts.tar.gz"
  rm -f "$_tmp"
  if ! sysupgrade -b "$_tmp" >/dev/null 2>&1 \
     || ! tar -tzf "$_tmp" >/dev/null 2>&1 \
     || ! tar -tzf "$_tmp" 2>/dev/null | grep -qE '(^|/)etc/'; then
    rm -f "$_tmp"
    printf '{"ok":false,"error":"sysupgrade -b не создал пригодный архив (нет tar/etc)"}\n'
    return 1
  fi
  _dest="$BACKUP_DIR/router-backup-$_ts.tar.gz"
  if ! mv -f "$_tmp" "$_dest" 2>/dev/null || [ ! -s "$_dest" ]; then
    rm -f "$_tmp"
    printf '{"ok":false,"error":"не удалось сохранить архив в %s"}\n' "$BACKUP_DIR"
    return 1
  fi
  chmod 600 "$_dest" 2>/dev/null
  _n=0
  for _old in $(ls -1t "$BACKUP_DIR"/router-backup-*.tar.gz 2>/dev/null); do
    _n=$((_n+1))
    [ "$_n" -gt 1 ] && [ "$_old" != "$_dest" ] && rm -f "$_old"
  done
  _write_backup_marker "$_dest" || { printf '{"ok":false,"error":"cannot record the verified backup"}\n'; return 1; }
  printf '%s BACKUP via panel: %s\n' "$(date +%FT%T)" "$_dest" >> "$LOG"
  printf '{"ok":true,"file":"%s","size":%s}\n' "$_dest" "$(wc -c < "$_dest" | tr -d ' ')"
}
# read-only subscription-assignment resolver (config + audit state + registry, never writes)
SUB_STATUS_PY=/usr/lib/geomask/geomask-subscription-status.py
# Interpreter for the python helpers; GM_PYTHON only lets tests point at their own python.
PYTHON_BIN=${GM_PYTHON:-python3}
SUB_STATUS_PYTHON=$PYTHON_BIN
UPD_CHECK=$UPD_DIR/check.json
UPD_STATUS=$UPD_DIR/status.json
UPD_PIDF=$UPD_DIR/pid              # legacy, read-only: the lock replaced it
UPD_LOCK=${GM_UPD_LOCK:-/tmp/geomask-locks/updater.lock}
UPD_TTL=3600

# safe_write shell wrapper
# usage: _safe_write <resource> <live_content> <editable_json> [backup_age_seconds]
# returns: 0=ok, 1=validation fail, 2=backup gate fail, 3=trial fail, 4=install/reload fail, 5=rollback fail
# stdout: new live content on success
_safe_write() {
    _resource="$1"
    _live_content="$2"
    _editable_json="$3"
    _max_age="${4:-300}"

    if [ ! -f "$SAFE_WRITE_PY" ]; then
        printf '{"ok":false,"error":"safe_write.py not installed"}\n' >&2
        return 1
    fi

    python3 "$SAFE_WRITE_PY" "$_resource" "$_live_content" "$_editable_json" "$_max_age"
}
UPD_REPO=kabzon93region/geomask
DEPLOY_JS=/www/luci-static/resources/view/geo-sync/main.js
DEPLOY_GUARD=/usr/bin/geomask-guard.sh
DEPLOY_BACKEND=/usr/bin/geo-backend.sh

# Keep authentication out of curl argv and diagnostics; read the active config on each call.
controller_curl() {
  python3 - "$MIHOMO_CONF" "$@" <<'GM_CONTROLLER_PY'
import json, re, subprocess, sys

def controller_headers(path):
    with open(path, encoding='utf-8') as config:
        for line in config:
            match = re.match(r'^secret:\s*(.*)$', line.rstrip('\r\n'))
            if not match:
                continue
            value = match.group(1).strip()
            if value.startswith('"'):
                value, end = json.JSONDecoder().raw_decode(value)
                tail = match.group(1).strip()[end:].strip()
                if tail and not tail.startswith('#'):
                    raise ValueError('Invalid controller secret')
            elif value.startswith("'"):
                quoted = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
                if not quoted:
                    raise ValueError('Invalid controller secret')
                value = quoted.group(1).replace("''", "'")
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
                if value.startswith('#'):
                    value = ''
                if value.startswith(('|', '>', '[', '{', '&', '*', '!')):
                    raise ValueError('Unsupported controller secret')
            if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError('Invalid controller secret')
            return {'Authorization': 'Bearer ' + value} if value else {}
    return {}

try:
    headers = controller_headers(sys.argv[1])
    options = ''.join('header = ' + json.dumps(k + ': ' + v, ensure_ascii=False) + '\n'
                      for k, v in headers.items())
    result = subprocess.run(['curl', '--config', '-', *sys.argv[2:]],
                            input=options.encode('utf-8'))
    sys.exit(result.returncode)
except Exception:
    print('Controller request failed (configuration or transport error)', file=sys.stderr)
    sys.exit(1)
GM_CONTROLLER_PY
}

# escape a string for JSON: backslash, quote, tab; CR stripped; newline -> \n
json_esc() {
  printf '%s' "$1" | awk '
    BEGIN { ORS="" }
    {
      gsub(/\\/, "\\\\")
      gsub(/"/, "\\\"")
      gsub(/\t/, "\\t")
      gsub(/\r/, "")
      if (NR>1) printf "\\n"
      print
    }
  '
}

# current node of a mihomo group (VPN or AI)
_grp_node() {
  controller_curl -s -m3 "$MIHOMO_API/proxies/$1" | grep -oE '"now":"[^"]*"' | head -1 | sed 's/"now":"//; s/"$//'
}

# count rules in a rule-provider yaml (payload lines starting with "  - ")
_count_rules() {
  # grep -c prints 0 AND exits 1 on no match; `|| echo 0` then produced "0\n0" and the
  # status JSON became invalid. Count with awk, which always prints one number.
  awk 'BEGIN{c=0} /^[[:space:]]*- /{c++} END{print c}' "$1" 2>/dev/null || echo 0
}

# Diagnostics go to stderr: stdout is the RPC JSON and must stay parseable.
warn() { printf '%s\n' "$*" >&2; printf '%s WARN %s\n' "$(date +%FT%T)" "$*" >> "$LOG" 2>/dev/null; }

# migrate legacy /etc/geo-exclude.list (ip name) -> /etc/geo-devices.list (ip mode name)
_migrate() {
  if [ -f "$EXCL_OLD" ] && [ ! -f "$DEVICES" ]; then
    while read -r dip dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      echo "$dip bypass ${dname:-$dip}" >> "$DEVICES"
    done < "$EXCL_OLD"
    echo "$(date +%FT%T) MIGRATE exclude->devices (all bypass)" >> "$LOG"
  fi
}

_prov_of_group() { # _prov_of_group <group> -> provider name used by that mihomo group ('' if none)
  python3 -c '
import re,sys
try: t=open("/etc/mihomo/config.yaml",encoding="utf-8").read()
except Exception: sys.exit(0)
m=re.search(r"(?m)^[ \t]*- name: %s\s*\n(?:^(?:[ \t]+.*|\s*)\n)*" % re.escape(sys.argv[1]), t)
if not m: sys.exit(0)
u=re.search(r"use:\n\s+-\s+(\S+)", m.group(0))
print(u.group(1) if u else "")
' "$1" 2>/dev/null
}

_url_of_provider() { # _url_of_provider <provider> -> its url line
  P="$1"
  awk -v p="  $P:" '$0==p{f=1;next} f&&/^    url:/{gsub(/^[^"]*"/,"");gsub(/".*/,"");print;exit} f&&/^  [A-Za-z0-9_-]+:/{exit}' "$MIHOMO_CONF" 2>/dev/null
}

cmd_status() {
  _migrate
  VPN_NODE=$(_grp_node VPN)          # slot VPN: P2P-tolerant tunnel (may be absent)
  VPNNP_NODE=$(_grp_node VPN-noP2P)  # slot VPN-noP2P: the subscription whose provider forbids P2P
  AI_NODE=$(_grp_node AI)
  GLOBAL=$(controller_curl -s -m3 "$MIHOMO_API/proxies/GLOBAL" | grep -oE '"now":"[^"]*"' | head -1 | sed 's/"now":"//; s/"$//')
  if crontab -l 2>/dev/null | grep -q 'sync_mtrickle_mihomo'; then LCRON=1; else LCRON=0; fi
  if [ -f /etc/sysctl.d/99-disable-ipv6.conf ]; then V6=0;
    elif [ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null)" = "1" ]; then V6=0; else V6=1; fi
  DOH=$(uci -q get https-dns-proxy.@https-dns-proxy[0].resolver_url || echo '')
  AI_RC=$(_count_rules "$AI_RULES")
  MT_RC=$(_count_rules "$MT_RULES")
  GOOGLE_CC=$(cat /tmp/geo-google 2>/dev/null || echo '')
  SYNC_AT=$(cat /tmp/geo-sync-at 2>/dev/null || echo '')
  DEV_LIST='['
  FIRST=1
  if [ -f "$DEVICES" ]; then
    while read -r dip dmode dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      [ "$FIRST" = 1 ] || DEV_LIST="$DEV_LIST,"
      DEV_LIST="$DEV_LIST{\"ip\":\"$dip\",\"mode\":\"$dmode\",\"name\":\"$(json_esc "${dname:-}")\"}"
      FIRST=0
    done < "$DEVICES"
  fi
  DEV_LIST="$DEV_LIST]"
  BYPASS=$(printf '%s' "$DEV_LIST" | grep -o '"mode":"bypass"' | wc -l | tr -d ' ')
  REALTZ=$(printf '%s' "$DEV_LIST" | grep -o '"mode":"realtz"' | wc -l | tr -d ' ')
  N_AI=$(printf '%s' "$DEV_LIST" | grep -o '"mode":"ai"' | wc -l | tr -d ' ')
  # grep -c always prints its count; appending '|| echo 0' used to yield "0\n0" and
  # produced invalid JSON whenever no rule matched.
  RULES=$(ip rule show 2>/dev/null | grep -c 'lookup main')
  RULES=${RULES:-0}
  _sub2u=$(_url_of_provider "$(_prov_of_group VPN)")
  _subu=$(_url_of_provider "$(_prov_of_group VPN-noP2P)")
  _aiu=$(_url_of_provider "$(_prov_of_group AI)")
  VPN_SUB_ID=$(_sub_id_by_url "$_sub2u" 2>/dev/null || echo '')
  VPNNP_SUB_ID=$(_sub_id_by_url "$_subu" 2>/dev/null || echo '')
  AI_SUB_ID=$(_sub_id_by_url "$_aiu" 2>/dev/null || echo '')
  if ip link show "$AI_TUN_DEV" >/dev/null 2>&1; then AI_TUN=1; else AI_TUN=0; fi
  if netstat -lntp 2>/dev/null | grep -q ':7891'; then AI_LST=1; else AI_LST=0; fi
  # AI-DNS (no DNS leak for AI devices): 0=off/absent, 1=working, 2=partial
  # "working" means the CLIENT DNS is redirected to the computed address, the marked sockets
  # route into the AI tunnel and the listener exists. The old check hard-coded one owner's
  # 192.168.0.53 and ignored the DNAT rule entirely (audit 14.11.2026, F08).
  if [ -f "$AIDNS_OFF_FILE" ] || [ ! -f "$AIDNS_UID_FILE" ]; then AIDNS=0
  else
    _aip=$(_gm_aidns_ip)
    _atbl=$(nft list table inet geomask_ai_dns 2>/dev/null)
    _aiips=$(awk '$2=="ai" && $1 !~ /^#/ {print $1}' "$DEVICES" 2>/dev/null)
    if [ -z "$_aip" ]; then AIDNS=2
    elif ! ip route get 1.1.1.1 mark 0x64 2>/dev/null | grep -q 'dev AI'; then AIDNS=2
    elif ! netstat -ltnpu 2>/dev/null | grep -q "$_aip:$AIDNS_DNS_PORT "; then AIDNS=2
    elif [ -n "$_aiips" ] && ! printf '%s' "$_atbl" | grep -q "dnat ip to $_aip:$AIDNS_DNS_PORT"; then AIDNS=2
    elif printf '%s' "$_atbl" | grep -q 'counter reject' && [ -n "$_aiips" ]; then AIDNS=2
    else AIDNS=1; fi
  fi
  # The assignment badges used to be derived here from config text with a regex that
  # expected `  - name:` and a quoted URL. Applying a pool re-serialises the config with
  # PyYAML, so every path lost its badge at once. The resolver reads the same three files
  # structurally and also links a managed pool back to its recorded subscription.
  _STATUS_JSON=$(printf '{"vpn_node":"%s","vpn_cc":"%s","vpn_nop2p_node":"%s","vpn_nop2p_cc":"%s","ai_node":"%s","ai_cc":"%s","global":"%s","lcron":%s,"ipv6":%s,"doh":"%s","ai_rules":%s,"mt_rules":%s,"google":"%s","sync_at":"%s","devices":%s,"n_bypass":%s,"n_realtz":%s,"rules_ok":%s,"n_ai":%s,"ai_tun":%s,"ai_listener":%s,"vpn_sub":"%s","ai_sub":"%s","vpn_nop2p_sub":"%s","vpn_url":"%s","ai_url":"%s","vpn_nop2p_url":"%s","aidns":%s}' \
    "$(json_esc "$VPN_NODE")" "$(_node_cc "$VPN_NODE")" \
    "$(json_esc "$VPNNP_NODE")" "$(_node_cc "$VPNNP_NODE")" \
    "$(json_esc "$AI_NODE")" "$(_node_cc "$AI_NODE")" \
    "$(json_esc "$GLOBAL")" "$LCRON" "$V6" "$(json_esc "$DOH")" \
    "$AI_RC" "$MT_RC" "$(json_esc "$GOOGLE_CC")" "$(json_esc "$SYNC_AT")" \
    "$DEV_LIST" "$BYPASS" "$REALTZ" "$RULES" \
    "$N_AI" "$AI_TUN" "$AI_LST" "$(json_esc "$VPN_SUB_ID")" "$(json_esc "$AI_SUB_ID")" "$(json_esc "$VPNNP_SUB_ID")" \
    "$(json_esc "$(_mask_url "$_sub2u")")" "$(json_esc "$(_mask_url "$_aiu")")" "$(json_esc "$(_mask_url "$_subu")")" "$AIDNS")
  if [ -f "$SUB_STATUS_PY" ]; then
    _MERGED=$(printf '%s' "$_STATUS_JSON" | "$SUB_STATUS_PYTHON" -B "$SUB_STATUS_PY" status 2>/dev/null)
    case "$_MERGED" in
      '{'*'}') _STATUS_JSON=$_MERGED ;;
    esac
  fi
  printf '%s\n' "$_STATUS_JSON"
}

# country fragment from a node name (flag emoji or RU word) - coarse, for display
_node_cc() {
  n="$1"
  case "$n" in
    *Германия*|*Germany*|*🇩🇪*) echo DE ;;
    *Нидерланды*|*Netherlands*|*🇳🇱*) echo NL ;;
    *Финляндия*|*Finland*|*🇫🇮*) echo FI ;;
    *Латвия*|*Latvia*|*🇱🇻*) echo LV ;;
    *Литва*|*Lithuania*|*🇱🇹*) echo LT ;;
    *Эстония*|*Estonia*|*🇪🇪*) echo EE ;;
    *Польша*|*Poland*|*🇵🇱*) echo PL ;;
    *Дания*|*Denmark*|*🇩🇰*) echo DK ;;
    *Швеция*|*Sweden*|*🇸🇪*) echo SE ;;
    *Норвегия*|*Norway*|*🇳🇴*) echo NO ;;
    *США*|*USA*|*🇺🇸*) echo US ;;
    *Турция*|*Turkey*|*🇹🇷*) echo TR ;;
    *Албания*|*Albania*|*🇦🇱*) echo AL ;;
    *Франция*|*France*|*🇫🇷*) echo FR ;;
    *Италия*|*Italy*|*🇮🇹*) echo IT ;;
    *Испания*|*Spain*|*🇪🇸*) echo ES ;;
    *Австрия*|*Austria*|*🇦🇹*) echo AT ;;
    *Швейцария*|*Switzerland*|*🇨🇭*) echo CH ;;
    *Великобритания*|*UK*|*🇬🇧*) echo GB ;;
    *) echo '' ;;
  esac
}

cmd_leases() {
  printf '{"leases":['
  [ -f /tmp/dhcp.leases ] || { printf ']}'; return; }
  FIRST=1
  while read -r ts mac ip name rest; do
    [ -n "$ip" ] || continue
    [ $FIRST -eq 1 ] || printf ','
    printf '{"ip":"%s","mac":"%s","name":"%s"}' "$ip" "$mac" "$(json_esc "${name:-}")"
    FIRST=0
  done < /tmp/dhcp.leases
  printf ']}'
}

cmd_log() {
  # A subscription URL may end up in a log line: mask the token before it reaches the page.
  _logtail=$(tail -30 "$LOG" 2>/dev/null \
    | sed -E 's#(https?://[^/[:space:]]+/?)[^[:space:]]*#\1…#g')
  printf '{"log":"%s"}' "$(json_esc "$_logtail")"
}

# manual list sync (magitrickle -> mihomo rulesets), runs the cron script immediately
cmd_sync_lists() {
  OUT=$(sh "$SYNCLISTS" 2>&1)
  date '+%F %T' > /tmp/geo-sync-at
  echo "$(date +%FT%T) SYNC_LISTS manual" >> "$LOG"
  AI_RC=$(_count_rules "$AI_RULES")
  MT_RC=$(_count_rules "$MT_RULES")
  printf '{"ai_rules":%s,"mt_rules":%s,"output":"%s"}' "$AI_RC" "$MT_RC" "$(json_esc "$OUT")"
}

# quick Google geo-check: country of the exit that Google traffic actually uses.
# Google/AI domains are routed to group AI (ai.yaml), so asking an IP-geo service
# through the AI listener (mihomo listeners: proxy=AI) reports exactly what Google sees.
cmd_google() {
  CC=$(curl -s -m15 -x "http://127.0.0.1:$AI_SOCKS_PORT" \
    'https://ipinfo.io/country' 2>/dev/null | tr -d '[:space:]')
  case "$CC" in [A-Z][A-Z]) ;; *) CC='' ;; esac
  [ -n "$CC" ] && printf '%s\n' "$CC" > /tmp/geo-google
  AI_NODE=$(_grp_node AI)
  printf '{"google":"%s","ai_node":"%s"}' "$(json_esc "$CC")" "$(json_esc "$AI_NODE")"
}

cmd_global() {
  case "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" in
    direct) NAME=DIRECT ;;
    vpn)    NAME=VPN ;;
    *) printf '{"error":"bad arg"}'; return 1 ;;
  esac
  controller_curl -s -m5 -X PUT "$MIHOMO_API/proxies/GLOBAL" -H 'Content-Type: application/json' \
    -d "{\"name\":\"$NAME\"}" >/dev/null
  cmd_status
}

# IPv6 off/on. geomask tunnels IPv4 only: `ip rule`, table 100, `default dev Mihomo` and the
# AI-DNS DNAT all match IPv4 addresses, so a global IPv6 address on the router or on the LAN
# leaves straight out of the WAN and shows the real address and country. IPv6 is therefore
# OFF by default (owner decision 14.11.2026); `on` restores exactly what was there before.
#
# The original values are recorded ONCE, on the way out. The first version of this code called
# the recorder on both paths, so `off` -> `on` recorded our own `ip6assign=0` and restored it:
# the original prefix size was lost (audit G04).
IPV6_STATE=/etc/geomask/ipv6.state
IPV6_NFT=/etc/nftables.d/99-geomask-no-v6.nft
IPV6_SYSCTL=/etc/sysctl.d/99-disable-ipv6.conf
IPV6_PROC=/proc/sys/net/ipv6/conf
# ip6assign lives in network.lan, the rest in dhcp.lan (odhcpd). All four are restored.
# The record also carries the kernel switch (`sysctl_all`/`sysctl_default`) and whether our nft
# guard file was there: without them `on` assumed "IPv6 was on before geomask" and wrote 0 over
# a router that had it disabled for another reason (review 15.11, point 19).
IPV6_KEYS="ip6assign ra dhcpv6 ndp"

_ipv6_state_get() { # recorded value for one key ('' when the record has no such line)
  sed -n "s/^$1=//p" "$IPV6_STATE" 2>/dev/null | head -1
}

_ipv6_record_ok() { # a complete record is what makes `on` possible; a partial one is garbage
  [ -f "$IPV6_STATE" ] || return 1
  for _k in $IPV6_KEYS; do
    [ -n "$(_ipv6_state_get "$_k")" ] || return 1
  done
  return 0
}

_ipv6_read_kernel() { # kernel switch and guard presence, one `key=value` per line
  for _kd in all default; do
    _kv=$(cat "$IPV6_PROC/$_kd/disable_ipv6" 2>/dev/null | tr -dc '01')
    [ -n "$_kv" ] || _kv=unset
    printf '%s=%s\n' "sysctl_$_kd" "$_kv"
  done
  if [ -f "$IPV6_NFT" ]; then printf 'nft=present\n'; else printf 'nft=absent\n'; fi
}

_ipv6_save() { # record the current values once; never overwrite a valid record
  # A record that already carries the original prefix must stay untouched: a second `off`
  # would otherwise replace the owner's 56 with our own 0 and `on` could never go back (G04).
  [ -s "$IPV6_STATE" ] && [ -n "$(_ipv6_state_get ip6assign)" ] && return 0
  mkdir -p "$(dirname "$IPV6_STATE")" 2>/dev/null || return 1
  _tmp="$IPV6_STATE.tmp"
  : > "$_tmp" 2>/dev/null || return 1
  for _k in $IPV6_KEYS; do
    _v=$(uci -q get "network.lan.$_k" 2>/dev/null)
    [ -n "$_v" ] || _v=$(uci -q get "dhcp.lan.$_k" 2>/dev/null)
    [ -n "$_v" ] || _v=unset
    printf '%s=%s\n' "$_k" "$_v" >> "$_tmp" 2>/dev/null || { rm -f "$_tmp"; return 1; }
  done
  _ipv6_read_kernel >> "$_tmp" 2>/dev/null || { rm -f "$_tmp"; return 1; }
  chmod 600 "$_tmp" 2>/dev/null
  mv -f "$_tmp" "$IPV6_STATE" 2>/dev/null || { rm -f "$_tmp"; return 1; }
}

_ipv6_restore() { # restore the recorded values; no record at all -> nothing to restore
  [ -f "$IPV6_STATE" ] || return 0
  _ipv6_record_ok || return 1
  for _k in $IPV6_KEYS; do
    _v=$(_ipv6_state_get "$_k")
    case "$_k" in
      ip6assign)
        case "$_v" in
          ''|unset) uci -q delete network.lan.ip6assign 2>/dev/null ;;
          *[!0-9]*) return 1 ;;
          *) uci set network.lan.ip6assign="$_v" 2>/dev/null || return 1 ;;
        esac
        ;;
      *)
        case "$_v" in
          ''|unset) uci -q delete "dhcp.lan.$_k" 2>/dev/null ;;
          *) uci set "dhcp.lan.$_k=$_v" 2>/dev/null || return 1 ;;
        esac
        ;;
    esac
  done
  return 0
}

# Belt and braces for the kernel switch: with IPv6 disabled there is nothing to forward, so a
# drop rule costs nothing and still holds if a global address appears before sysctl applies.
_ipv6_guard_install() {
  mkdir -p "$(dirname "$IPV6_NFT")" 2>/dev/null || return 1
  cat > "$IPV6_NFT" 2>/dev/null <<'EOF'
table inet fw4 {
    chain geomask_no_v6 {
        type filter hook forward priority -10; policy accept;
        meta nfproto ipv6 counter drop comment "geomask: IPv6 forwarding disabled"
    }
}
EOF
}

_ipv6_guard_remove() { rm -f "$IPV6_NFT" 2>/dev/null; }

_ipv6_kernel() { # _ipv6_kernel <0|1> - sysctl file plus every interface already up
  if [ "$1" = 1 ]; then
    mkdir -p "$(dirname "$IPV6_SYSCTL")" 2>/dev/null || return 1
    printf '%s\n%s\n' 'net.ipv6.conf.all.disable_ipv6 = 1' \
      'net.ipv6.conf.default.disable_ipv6 = 1' > "$IPV6_SYSCTL" 2>/dev/null || return 1
    # If the kernel refuses the switch there is no point in claiming IPv6 is off.
    sysctl -p "$IPV6_SYSCTL" >/dev/null 2>&1 || return 1
  else
    rm -f "$IPV6_SYSCTL" 2>/dev/null || return 1
  fi
  # Per-interface writes are belt and braces: `all`/`default` already cover interfaces that
  # appear later, so a single uncooperative entry must not fail the whole switch.
  for d in "$IPV6_PROC"/*; do echo "$1" > "$d/disable_ipv6" 2>/dev/null || true; done
  return 0
}

_ipv6_kernel_restore() { # put the recorded kernel switch back instead of assuming "0"
  _ra=$(_ipv6_state_get sysctl_all)
  _rd=$(_ipv6_state_get sysctl_default)
  case "$_ra" in 0|1) ;; *) _ra='' ;; esac
  case "$_rd" in 0|1) ;; *) _rd='' ;; esac
  if [ -z "$_ra" ] || [ -z "$_rd" ]; then
    # A record written by an older build kept no kernel state. Leaving the kernel disabled
    # would report "IPv6 restored" while nothing came back, so the documented default is on.
    _ra=0; _rd=0
  fi
  for _d in "$IPV6_PROC"/*; do
    [ -e "$_d/disable_ipv6" ] || continue
    printf '%s\n' "$_rd" > "$_d/disable_ipv6" 2>/dev/null || return 1
  done
  if [ "$_ra" = 1 ] || [ "$_rd" = 1 ]; then
    mkdir -p "$(dirname "$IPV6_SYSCTL")" 2>/dev/null || return 1
    printf '%s\n%s\n' "net.ipv6.conf.all.disable_ipv6 = $_ra" \
      "net.ipv6.conf.default.disable_ipv6 = $_rd" > "$IPV6_SYSCTL" 2>/dev/null || return 1
    sysctl -p "$IPV6_SYSCTL" >/dev/null 2>&1 || return 1
  else
    rm -f "$IPV6_SYSCTL" 2>/dev/null || return 1
  fi
  return 0
}

_ipv6_guard_restore() { # our guard file existed before geomask -> put our own copy back
  case "$(_ipv6_state_get nft)" in
    present) _ipv6_guard_install ;;
    *) _ipv6_guard_remove ;;
  esac
}

_ipv6_apply_off() { # every write of the disabled state; no commit, no reload
  # Record BEFORE the first change, and stop if it cannot be written: turning IPv6 off with
  # no way back is exactly the trap that G04 described.
  _ipv6_save || return 1
  _ipv6_kernel 1 || return 1
  uci set network.lan.ip6assign='0' 2>/dev/null || return 1
  uci set dhcp.lan.ra='disabled' 2>/dev/null || return 1
  uci set dhcp.lan.dhcpv6='disabled' 2>/dev/null || return 1
  uci set dhcp.lan.ndp='disabled' 2>/dev/null || return 1
  _ipv6_guard_install || return 1
  return 0
}

_ipv6_apply_on() { # every write of the restored state; no commit, no reload
  _ipv6_kernel_restore || return 1
  _ipv6_restore || return 1
  _ipv6_guard_restore || return 1
  return 0
}

cmd_ipv6() {
  _require_backup || return 1
  case "$1" in
    off|on) ;;
    *) printf '{"error":"bad arg"}'; return 1 ;;
  esac
  if [ "$1" = off ]; then
    cp -n /etc/config/network "/etc/config/network.bak_v6off_$(date +%s)" 2>/dev/null || true
    if ! _ipv6_apply_off; then
      printf '{"ok":false,"error":"cannot apply IPv6 off - previous settings restored"}\n'
      _ipv6_restore; uci commit network 2>/dev/null; uci commit dhcp 2>/dev/null
      return 1
    fi
  else
    if ! _ipv6_apply_on; then
      printf '{"ok":false,"error":"cannot restore the previous IPv6 settings - record kept"}\n'
      return 1
    fi
  fi
  if ! uci commit network 2>/dev/null || ! uci commit dhcp 2>/dev/null; then
    printf '{"ok":false,"error":"uci commit failed - previous settings restored"}\n'
    _ipv6_restore; uci commit network 2>/dev/null; uci commit dhcp 2>/dev/null
    return 1
  fi
  if ! reload_config 2>/dev/null && ! ubus call network reload >/dev/null 2>&1; then
    printf '{"ok":false,"error":"network reload failed - check /etc/config/network"}\n'
    return 1
  fi
  # Only a successful `on` consumes the record: the next `off` must save the value that is
  # current then, not the one from months ago.
  [ "$1" = on ] && rm -f "$IPV6_STATE" 2>/dev/null
  sleep 1
  # Report OUR own result. The previous version ended with cmd_status, whose JSON has no "ok"
  # field, so the installer read a successful switch as a failure every time.
  _v6g=$(ip -6 addr show scope global 2>/dev/null | grep -c 'inet6 ')
  case "$_v6g" in ''|*[!0-9]*) _v6g=0 ;; esac
  printf '{"ok":true,"state":"%s","global":%s,"ip6assign":"%s","ra":"%s","dhcpv6":"%s"}\n' \
    "$1" "$_v6g" "$(uci -q get network.lan.ip6assign 2>/dev/null)" \
    "$(uci -q get dhcp.lan.ra 2>/dev/null)" "$(uci -q get dhcp.lan.dhcpv6 2>/dev/null)"
}

# AI-DNS on demand: regenerate the nft DNAT list for ai-mode devices + reload firewall.
# Installs nothing (that is the installer's aidns step); only re-syncs the device list.
cmd_aidns_apply() {
  if [ -f "$AIDNS_OFF_FILE" ]; then
    printf '{"ok":true,"warn":"AI-DNS disabled (/etc/geomask-aidns.off)"}'
    return 0
  fi
  if [ ! -f "$AIDNS_UID_FILE" ]; then
    printf '{"ok":false,"warn":"AI-DNS not installed - run the aidns step of geomask"}'
    return 0
  fi
  _before=$(sha256sum "$AIDNS_NFT_DEST" 2>/dev/null | awk '{print $1}')
  _aidns_sync
  _after=$(sha256sum "$AIDNS_NFT_DEST" 2>/dev/null | awk '{print $1}')
  if [ "$(cat "$AIDNS_STATE" 2>/dev/null)" = error ]; then
    printf '{"ok":false,"error":"AI-DNS listen address is unknown: client DNS is rejected (fail closed)"}'
    return 1
  fi
  if [ "$_before" != "$_after" ]; then
    echo "$(date +%FT%T) AI-DNS apply (manual)" >> "$LOG"
    printf '{"ok":true,"warn":""}'
  else
    printf '{"ok":true,"warn":"no changes"}'
  fi
  return 0
}

# strict IPv4: four octets, 0-255, not the unspecified/broadcast address
_ipv4_ok() {
  awk -v ip="$1" 'BEGIN{
    n=split(ip,A,"."); if (n != 4) exit 1;
    for (i=1; i<=4; i++) { if (A[i] !~ /^[0-9]+$/ || A[i] < 0 || A[i] > 255) exit 1 }
    if (ip == "0.0.0.0" || ip == "255.255.255.255") exit 1;
    exit 0
  }'
}

_devices_lock() {
  command -v gm_lock_acquire >/dev/null 2>&1 || return 1
  gm_lock_acquire devices 20 "$DEVICES_LOCK"
}

_devices_unlock() {
  command -v gm_lock_release >/dev/null 2>&1 || return 0
  gm_lock_release devices
}

_dhcp_owned_add() { # _dhcp_owned_add <section> <ip> <mac>
  mkdir -p "$(dirname "$DHCP_OWNED")" 2>/dev/null
  _do_tmp="$DHCP_OWNED.tmp.$$"
  _devices_lock || return 1
  if [ -f "$DHCP_OWNED" ]; then
    awk -F'|' -v id="$1" '$1!=id' "$DHCP_OWNED" > "$_do_tmp" 2>/dev/null || { rm -f "$_do_tmp"; _devices_unlock; return 1; }
    mv -f "$_do_tmp" "$DHCP_OWNED" 2>/dev/null || { rm -f "$_do_tmp"; _devices_unlock; return 1; }
  fi
  printf '%s|%s|%s\n' "$1" "$2" "$3" >> "$DHCP_OWNED"
  chmod 600 "$DHCP_OWNED" 2>/dev/null
  _devices_unlock
}

# add/update a device geo-policy: dev_add <ip> <mode> [mac] [name]
cmd_dev_add() {
  IP="$1"; MODE="$2"; MAC="$3"; NAME="${4:-$IP}"
  _ipv4_ok "$IP" || { printf '{"error":"bad ip"}'; return 1; }
  case "$MODE" in bypass|realtz|ai) ;; *) printf '{"error":"bad mode"}'; return 1 ;; esac
  NAME=$(printf '%s' "$NAME" | tr -d '"\\"\n\r\t' | head -c 64)
  _require_backup || return 1
  mkdir -p "$(dirname "$DEVICES")"; touch "$DEVICES"
  # replace existing entry for this ip (exact first field, not a regex: a dotted address used
  # as a regex matches unrelated lines). A failed awk (ENOSPC) must NOT leave an empty file
  # behind: the old `|| : > tmp` truncated the list and dropped every device policy.
  # The rewrite runs under the shared `devices` lock and through a per-process temp, so two
  # panel calls cannot install a list built from the same stale baseline (report R08).
  _devices_lock || { printf '{"ok":false,"error":"device list is busy: another writer holds the lock"}\n'; return 1; }
  _dv_tmp="$DEVICES.tmp.$$"
  if ! awk -v ip="$IP" '$1 != ip' "$DEVICES" > "$_dv_tmp" 2>/dev/null; then
    rm -f "$_dv_tmp"; _devices_unlock
    printf '{"ok":false,"error":"cannot rewrite %s (disk full?) - nothing changed"}\n' "$DEVICES"
    return 1
  fi
  if ! mv -f "$_dv_tmp" "$DEVICES"; then
    rm -f "$_dv_tmp"; _devices_unlock
    printf '{"ok":false,"error":"cannot replace %s - nothing changed"}\n' "$DEVICES"
    return 1
  fi
  echo "$IP $MODE $NAME" >> "$DEVICES"
  _devices_unlock
  # stable lease if mac given (strict MAC format only)
  if [ -n "$MAC" ] && echo "$MAC" | grep -qE '^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$'; then
    if ! uci show dhcp 2>/dev/null | grep -q "\.mac='$MAC'"; then
      SEC=$(uci add dhcp host 2>/dev/null)
      if [ -n "$SEC" ]; then
        uci set "$SEC.mac=$MAC"; uci set "$SEC.ip=$IP"; uci set "$SEC.name=$NAME"
        if uci commit dhcp 2>/dev/null; then
          _dhcp_owned_add "$SEC" "$IP" "$MAC"
        else
          uci -q delete "$SEC" 2>/dev/null
          warn "dhcp host commit failed - static lease not created"
        fi
      fi
      /etc/init.d/dnsmasq restart >/dev/null 2>&1 || true
    fi
  fi
  _apply_routing
  echo "$(date +%FT%T) DEV add $IP mode=$MODE ($NAME)" >> "$LOG"
  cmd_status
}

cmd_dev_del() {
  IP="$1"
  _ipv4_ok "$IP" || { printf '{"error":"bad ip"}'; return 1; }
  _require_backup || return 1
  if [ -f "$DEVICES" ]; then
    _devices_lock || { printf '{"ok":false,"error":"device list is busy: another writer holds the lock"}\n'; return 1; }
    _dv_tmp="$DEVICES.tmp.$$"
    if ! awk -v ip="$IP" '$1 != ip' "$DEVICES" > "$_dv_tmp" 2>/dev/null; then
      rm -f "$_dv_tmp"; _devices_unlock
      printf '{"ok":false,"error":"cannot rewrite %s (disk full?) - nothing changed"}\n' "$DEVICES"
      return 1
    fi
    mv -f "$_dv_tmp" "$DEVICES" || { rm -f "$_dv_tmp"; _devices_unlock; printf '{"ok":false,"error":"cannot replace %s"}\n' "$DEVICES"; return 1; }
    _devices_unlock
  fi
  # remove rules for this ip explicitly per table (bypass -> main, ai -> 101).
  # NEVER delete by pref alone: all per-device rules share priority 19900, so
  # `ip rule del pref` would drop another device's rule (and _apply_routing re-adds it)
  ip rule del from "$IP" lookup main 2>/dev/null || true
  ip rule del from "$IP" lookup 101 2>/dev/null || true
  # DHCP: only a host section geomask created for this exact address may be removed. The old
  # code took the first section whose ip matched and deleted it - including the owner's own
  # static lease (audit 14.11.2026, F09).
  _sec=$(awk -F'|' -v ip="$IP" '$2==ip {print $1; exit}' "$DHCP_OWNED" 2>/dev/null)
  if [ -n "$_sec" ]; then
    _sip=$(uci -q get "$_sec.ip" 2>/dev/null)
    _smac=$(uci -q get "$_sec.mac" 2>/dev/null)
    if [ "$_sip" = "$IP" ] && [ -n "$_smac" ]; then
      if uci -q delete "$_sec" 2>/dev/null && uci commit dhcp 2>/dev/null; then
        _do_tmp="$DHCP_OWNED.tmp.$$"
        if _devices_lock; then
          awk -F'|' -v id="$_sec" '$1!=id' "$DHCP_OWNED" > "$_do_tmp" 2>/dev/null \
            && mv -f "$_do_tmp" "$DHCP_OWNED" 2>/dev/null
          rm -f "$_do_tmp"
          _devices_unlock
        fi
      else
        warn "cannot remove dhcp host $_sec - section left in place"
      fi
    else
      warn "dhcp host $_sec no longer matches $IP - left in place"
    fi
  fi
  _apply_routing
  echo "$(date +%FT%T) DEV del $IP" >> "$LOG"
  cmd_status
}

# AI-DNS: keep the nft DNAT list in sync with the ai-mode devices of /etc/geo-devices.list.
# No-op when the step was not installed (/etc/geomask-aidns.uid absent) or disabled
# (/etc/geomask-aidns.off present). Firewall is reloaded only when the list changes.
# NOTE: the AI dnsmasq listens on the LAN alias at :5353 (NOT :53) - the main dnsmasq
# rebinds every interface address including our alias, so :53 there is taken.
# The AI-DNS address belongs to this router's LAN, not to one owner's network: read it from
# the config the installer wrote, then from the UCI alias. It is never derived from the
# router address - `.53` is outside a /25 and equals the router itself on some LANs.
_gm_lan_prefix() {
  ip -4 addr show "$(_gm_lan_iface)" 2>/dev/null \
    | sed -n 's/.*inet [0-9.]*\/\([0-9][0-9]*\).*/\1/p' | head -1
}

_gm_lan_ip() {
  ip -4 addr show "$(_gm_lan_iface)" 2>/dev/null \
    | sed -n 's/.*inet \([0-9][0-9.]*\)\/.*/\1/p' | head -1
}

_gm_aidns_usable() { # _gm_aidns_usable <candidate> <lan-ip> <prefix>
  awk -v ip="$1" -v lan="$2" -v p="$3" 'BEGIN{
    n=split(ip,A,"."); m=split(lan,B,".");
    if (n != 4 || m != 4) exit 1;
    for (i=1; i<=4; i++) { if (A[i] !~ /^[0-9]+$/ || A[i] < 0 || A[i] > 255) exit 1 }
    for (i=1; i<=4; i++) { if (B[i] !~ /^[0-9]+$/ || B[i] < 0 || B[i] > 255) exit 1 }
    ipn=((A[1]*256+A[2])*256+A[3])*256+A[4];
    lann=((B[1]*256+B[2])*256+B[3])*256+B[4];
    size=2^(32-p); net=int(lann/size)*size;
    if (ipn < net+1 || ipn > net+size-2) exit 1;
    if (ipn == lann) exit 1;
    exit 0
  }'
}

# The AI-DNS address belongs to this router's LAN, not to one owner's network: read it from
# the config the installer wrote, then from the UCI alias - and validate it against the LIVE
# subnet. The old version returned whatever the file contained (audit 14.11.2026, F11).
_gm_aidns_ip() {
  _a=$(sed -n 's/^listen-address=\([0-9.]*\).*/\1/p' /etc/dnsmasq-ai.conf 2>/dev/null | head -1)
  if [ -z "$_a" ]; then
    _a=$(uci -q get network.lanai.ipaddr 2>/dev/null)
  fi
  case "$_a" in *.*.*.*) ;; *) printf ''; return 0 ;; esac
  _lip=$(_gm_lan_ip)
  _lpfx=$(_gm_lan_prefix)
  case "$_lpfx" in ''|*[!0-9]*) _lpfx=24 ;; esac
  [ "$_lpfx" -ge 1 ] 2>/dev/null || _lpfx=24
  [ "$_lpfx" -le 30 ] 2>/dev/null || { printf ''; return 0; }
  case "$_lip" in *.*.*.*) ;; *) printf ''; return 0 ;; esac
  _gm_aidns_usable "$_a" "$_lip" "$_lpfx" || { printf ''; return 0; }
  printf '%s' "$_a"
}

_gm_lan_iface() {
  _i=$(uci -q get network.lan.device 2>/dev/null)
  [ -n "$_i" ] || _i=$(uci -q get network.lan.ifname 2>/dev/null)
  case "$_i" in ''|*[!a-zA-Z0-9._-]*) _i=br-lan ;; esac
  printf '%s' "$_i"
}

_gm_lan_devices() { # every LAN-zone device, not just br-lan/br-lan.1
  # A configured device is included even when it does not exist yet: nft `iifname` matches by
  # name at packet time, and filtering on `ip link show` meant a VLAN/guest bridge created
  # later stayed without an accept rule until the panel ran again (audit 14.11.2026, F18).
  _devs=""
  for _z in $(uci -q show firewall 2>/dev/null | sed -n 's/^firewall\.\([^.=]*\)=zone$/\1/p'); do
    [ "$(uci -q get "firewall.$_z.name" 2>/dev/null)" = "lan" ] || continue
    for _n in $(uci -q get "firewall.$_z.network" 2>/dev/null); do
      _d=$(uci -q get "network.$_n.device" 2>/dev/null)
      [ -n "$_d" ] || _d=$(uci -q get "network.$_n.ifname" 2>/dev/null)
      # a list of ports is a bridge: the interface that carries the address is br-<network>
      case "$_d" in *" "*|"") _d="br-$_n" ;; esac
      case "$_d" in ''|*[!a-zA-Z0-9._-]*) continue ;; esac
      case " $_devs " in *" $_d "*) ;; *) _devs="$_devs $_d" ;; esac
    done
  done
  _d=$(_gm_lan_iface)
  case " $_devs " in *" $_d "*) ;; *) _devs="$_devs $_d" ;; esac
  printf '%s' "$_devs"
}

_gm_nft_names() { # "a b" -> "a", "b"  (nft set syntax)
  _out=""
  for _n in $1; do _out="$_out\"$_n\", "; done
  printf '%s' "${_out%, }"
}

_ai_drop_write() { # regenerate the AI accept rule for the current LAN devices
  _devs=$(_gm_lan_devices)
  _names=$(_gm_nft_names "$_devs")
  mkdir -p /usr/share/nftables.d/ruleset-post
  {
    printf '# geomask-lan: %s\n' "$_devs"
    printf 'table inet fw4 {\n'
    printf '    chain accept_to_AI {\n'
    [ -n "$_names" ] && printf '        iifname { %s } oifname "AI" counter accept comment "custom: LAN clients to AI tunnel"\n' "$_names"
    printf '        iifname "Wireguard" oifname "AI" counter accept comment "custom: WG peers to AI tunnel"\n'
    printf '    }\n}\n'
  } > /usr/share/nftables.d/ruleset-post/99-ai-accept.nft
}

_gm_sync_routes() { # _gm_sync_routes <table> - mirror LAN connected routes only
  # One implementation, shared with the ifup hotplug script: the old copy here mirrored
  # every non-default route and deleted anything it did not find in `main`, including
  # routes another policy router owned (audit 14.11.2026, F01/F02).
  [ -r "$GM_ROUTES_LIB" ] || return 1
  . "$GM_ROUTES_LIB"
  geomask_routes_sync "$1"
}

AIDNS_DNS_PORT=5353
AIDNS_NFT_DEST=${GM_AIDNS_NFT:-/usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft}
AIDNS_UID_FILE=${GM_AIDNS_UID:-/etc/geomask-aidns.uid}
AIDNS_OFF_FILE=${GM_AIDNS_OFF:-/etc/geomask-aidns.off}
# Outcome of the last sync, read by status. A parameterless hard path here was one of the ways
# the tests wrote into the host's /tmp (review 15.11, point 1).
AIDNS_STATE=${GM_AIDNS_STATE:-/tmp/geomask-aidns.state}

_aidns_sync() {
  [ -f "$AIDNS_UID_FILE" ] || return 0
  [ -f "$AIDNS_OFF_FILE" ] && return 0
  _uid=$(cat "$AIDNS_UID_FILE" 2>/dev/null | tr -dc '0-9')
  [ -n "$_uid" ] || return 0
  _ips=$(awk '$2=="ai" && $1 !~ /^#/ {print $1}' "$DEVICES" 2>/dev/null)
  _list=""
  for _ip in $_ips; do _list="$_list $_ip,"; done
  _list=${_list%,}
  _aidns=$(_gm_aidns_ip)
  _lip=$(_gm_lan_ip)
  if [ -z "$_lip" ]; then
    # The LAN address itself is unreadable (ip missing / interface down): that is a transient
    # condition, not a bad alias. Changing the ruleset now could reject DNS for good.
    echo "$(date +%FT%T) AI-DNS: LAN address unreadable - ruleset left as is" >> "$LOG"
    return 0
  fi
  _tmp=/tmp/geomask-aidns.nft.$$
  {
    printf '# geomask AI-DNS (auto-generated from /etc/geo-devices.list - do not edit)\n'
    printf '# DNS of AI-mode devices goes into the AI tunnel: no DNS leak, no geo mismatch.\n'
    printf '# idempotent include pattern (same as 20-https-dns-proxy-notrack.nft)\n'
    printf 'add table inet geomask_ai_dns\n'
    printf 'flush table inet geomask_ai_dns\n'
    printf 'add chain inet geomask_ai_dns pre_nat { type nat hook prerouting priority -110; policy accept; }\n'
    printf 'add chain inet geomask_ai_dns out_mark { type route hook output priority -150; policy accept; }\n'
    printf 'add rule inet geomask_ai_dns out_mark meta skuid %s mark set 0x64\n' "$_uid"
    if [ -n "$_list" ]; then
      if [ -n "$_aidns" ]; then
        printf 'add rule inet geomask_ai_dns pre_nat ip saddr { %s } meta l4proto { tcp, udp } th dport 53 dnat ip to %s:%s\n' "$_list" "$_aidns" "$AIDNS_DNS_PORT"
      else
        # FAIL CLOSED. A DNAT to a guessed address black-holes DNS, and leaving the clients
        # alone sends them to the router's ordinary resolver - the leak this feature exists to
        # prevent. Neither may look like success (audit 14.11.2026, F08).
        printf 'add rule inet geomask_ai_dns pre_nat ip saddr { %s } meta l4proto { tcp, udp } th dport 53 counter reject\n' "$_list"
      fi
    fi
  } > "$_tmp" 2>/dev/null || { rm -f "$_tmp"; return 0; }
  if ! nft -c -f "$_tmp" >/dev/null 2>&1; then
    rm -f "$_tmp"
    echo "$(date +%FT%T) AI-DNS: generated rules failed syntax check - nothing applied" >> "$LOG"
    return 0
  fi
  # Compare the WHOLE ruleset, not just the device list: an address or uid that changed while
  # the list stayed the same must still be applied (audit 14.11.2026, F08).
  if cmp -s "$_tmp" "$AIDNS_NFT_DEST"; then
    rm -f "$_tmp"
    [ -n "$_list" ] && [ -z "$_aidns" ] && printf 'error' > "$AIDNS_STATE"
    return 0
  fi
  if ! cp "$_tmp" "$AIDNS_NFT_DEST" 2>/dev/null; then
    rm -f "$_tmp"
    echo "$(date +%FT%T) AI-DNS: cannot write $AIDNS_NFT_DEST" >> "$LOG"
    return 0
  fi
  rm -f "$_tmp"
  if [ -n "$_list" ] && [ -z "$_aidns" ]; then
    printf 'error' > "$AIDNS_STATE"
    echo "$(date +%FT%T) AI-DNS: listen address unknown - client DNS REJECTED (fail closed)" >> "$LOG"
  else
    printf 'ok' > "$AIDNS_STATE"
  fi
  if ! /etc/init.d/firewall reload >/dev/null 2>&1; then
    sleep 1
    if ! /etc/init.d/firewall reload >/dev/null 2>&1; then
      echo "$(date +%FT%T) AI-DNS: firewall reload failed - rules may not be active" >> "$LOG"
      return 0
    fi
  fi
  printf '%s' "$_list" > /tmp/geomask-aidns-last
  echo "$(date +%FT%T) AI-DNS: ruleset updated -> firewall reloaded" >> "$LOG"
  return 0
}

# (re)apply routing for all managed devices:
#   bypass -> direct (19900) | realtz -> normal mihomo path | ai -> AI tunnel (19900 -> table 101 -> dev AI)
_apply_routing() {
  # drop stale per-device rules first (we re-add per mode)
  if [ -f "$DEVICES" ]; then
    while read -r dip dmode dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      ip rule del from "$dip" lookup main 2>/dev/null || true
      ip rule del from "$dip" lookup 101 2>/dev/null || true
    done < "$DEVICES"
    while read -r dip dmode dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      if [ "$dmode" = "bypass" ]; then
        ip rule show | grep -q "from $dip" || ip rule add priority 19900 from "$dip" lookup main
      elif [ "$dmode" = "ai" ]; then
        _ai_tun_ensure
           ip rule show | grep -q "from $dip" || ip rule add priority 19900 from "$dip" lookup 101
      fi
    done < "$DEVICES"
  fi
  _hotplug_sync
  _aidns_sync
}

# provision AI tunnel on first use: hev-socks5-tunnel instance #2 (own TUN "AI",
# socks 127.0.0.1:7891) + network iface + firewall zone. The WORKING main tunnel
# is never restarted - hev-ai is a separate init.d service.
_ai_tun_ensure() {
  mkdir -p /etc/hev-socks5-tunnel
  if [ ! -f /etc/hev-socks5-tunnel/ai.yml ]; then
    cat > /etc/hev-socks5-tunnel/ai.yml <<'EOF'
tunnel:
  name: AI
  mtu: 8500
  multi-queue: false
  ipv4: 198.19.0.1
socks5:
  port: 7891
  address: 127.0.0.1
  udp: 'udp'
EOF
  fi
  if [ ! -x /etc/init.d/hev-ai ]; then
    cat > /etc/init.d/hev-ai <<'EOF'
#!/bin/sh /etc/rc.common
USE_PROCD=1
START=99
start_service() {
  procd_open_instance
  procd_set_param command /usr/bin/hev-socks5-tunnel /etc/hev-socks5-tunnel/ai.yml
  procd_set_param file /etc/hev-socks5-tunnel/ai.yml
  procd_set_param limits core="unlimited"
  procd_set_param limits nofile="1000000 1000000"
  procd_set_param stdout 1
  procd_set_param stderr 1
  procd_set_param respawn
  procd_close_instance
}
EOF
    chmod +x /etc/init.d/hev-ai
  fi
  ip link show "$AI_TUN_DEV" >/dev/null 2>&1 || {
    /etc/init.d/hev-ai enable >/dev/null 2>&1
    /etc/init.d/hev-ai start >/dev/null 2>&1
    _i=0
    while [ $_i -lt 10 ] && ! ip link show "$AI_TUN_DEV" >/dev/null 2>&1; do
      sleep 1; _i=$((_i+1))
    done
  }
  uci -q get network.AI >/dev/null 2>&1 || {
    uci set network.AI=interface
    uci set network.AI.proto='none'
    uci set network.AI.device="$AI_TUN_DEV"
    uci commit network
  }
  if ! uci -q show firewall 2>/dev/null | grep -q "name='AI'"; then
    # clone the working Mihomo zone + its forwardings for the AI tunnel
    ZN=''
    for s in $(uci -q show firewall | sed -n 's/^\(firewall\.[^.]*\)=zone$/\1/p'); do
      [ "$(uci -q get "$s.name")" = "Mihomo" ] && { ZN="$s"; break; }
    done
    if [ -n "$ZN" ]; then
      NEW=$(uci add firewall zone)
      uci set "firewall.$NEW.name=AI"
      uci set "firewall.$NEW.input=$(uci -q get "$ZN.input")"
      uci set "firewall.$NEW.output=$(uci -q get "$ZN.output")"
      uci set "firewall.$NEW.forward=$(uci -q get "$ZN.forward")"
      uci set "firewall.$NEW.masq=$(uci -q get "$ZN.masq")"
      uci set "firewall.$NEW.mtu_fix=$(uci -q get "$ZN.mtu_fix")"
      uci set "firewall.$NEW.network=AI"
      for f in $(uci -q show firewall | sed -n 's/^\(firewall\.[^.]*\)=forwarding$/\1/p'); do
        [ "$(uci -q get "$f.src")" = "lan" ] && [ "$(uci -q get "$f.dest")" = "Mihomo" ] || continue
        NF=$(uci add firewall forwarding)
        uci set "firewall.$NF.src=lan"
        uci set "firewall.$NF.dest=AI"
      done
      for f in $(uci -q show firewall | sed -n 's/^\(firewall\.[^.]*\)=forwarding$/\1/p'); do
        [ "$(uci -q get "$f.src")" = "Mihomo" ] && [ "$(uci -q get "$f.dest")" = "lan" ] || continue
        NF=$(uci add firewall forwarding)
        uci set "firewall.$NF.src=AI"
        uci set "firewall.$NF.dest=lan"
      done
      uci commit firewall
      # uci forwarding lan->AI alone does NOT generate an accept rule (the zone
      # subnet is a /32 assigned by the TUN itself, so fw4 leaves accept_to_AI
      # empty and forwarded LAN packets are dropped). Drop-in adds the accept.
      _ai_drop_write
      /etc/init.d/firewall reload >/dev/null 2>&1
    fi
  fi
  # v1.27.10: the accept drop-in above was written only when the AI zone was first
  # created. On a box where the zone already existed (ours), a WireGuard peer routed
  # by MagiTrickle into the AI tunnel hit forward_wg with no matching accept, so fw4
  # answered the phone with a TCP reset and YouTube stalled after the first second.
  # Ensure the WG rule on every run, not just at creation time.
  _ai_drop=/usr/share/nftables.d/ruleset-post/99-ai-accept.nft
  if [ ! -s "$_ai_drop" ] || ! grep -q 'iifname "Wireguard" oifname "AI"' "$_ai_drop" 2>/dev/null \
     || ! grep -qxF "# geomask-lan: $(_gm_lan_devices)" "$_ai_drop" 2>/dev/null; then
    _ai_drop_write
    /etc/init.d/firewall reload >/dev/null 2>&1
  fi
  # table 101: LAN/host routes must stay local, only internet goes to AI tunnel
  _gm_sync_routes 101
  ip route replace table 101 default dev "$AI_TUN_DEV" 2>/dev/null
  echo "$(date +%FT%T) AI_TUN provisioned" >> "$LOG"
}

_hotplug_sync() {
  # The installer owns this file: it is the only code that knows this router's LAN device
  # and TUN name. Rewriting it here (as this function did until v1.30.19) replaced the
  # portable version with a br-lan/Mihomo heredoc on every device add/delete, so the
  # portability fix did not survive the first panel action.
  _hp=/etc/hotplug.d/iface/99-default-vpn-route
  if [ ! -s "$_hp" ]; then
    if [ -s /usr/lib/geomask/99-default-vpn-route.dist ]; then
      mkdir -p /etc/hotplug.d/iface 2>/dev/null
      cp -f /usr/lib/geomask/99-default-vpn-route.dist "$_hp" 2>/dev/null
      chmod 755 "$_hp" 2>/dev/null
      echo "$(date +%FT%T) hotplug route script restored from the shipped copy" >> "$LOG"
    else
      echo "$(date +%FT%T) hotplug route script missing - re-run the geomask installer" >> "$LOG"
    fi
  fi
  if ! sh -n "$_hp" 2>/dev/null; then
    # Diagnostics must not go to stdout: the caller prints its own JSON answer.
    warn "hotplug route script has a syntax error: $_hp"
  fi
}

# ---- AI path (no own subscription since v1.23.0; rides the provider its group uses) ----
cmd_ai_sub_get() {
  # `provider` stays the provider the AI group actually rides (for a managed pool that is
  # the audit-* file provider). The resolved source subscription is an added field.
  if [ -f "$SUB_STATUS_PY" ]; then
    _AIJSON=$("$SUB_STATUS_PYTHON" -B "$SUB_STATUS_PY" ai-sub 2>/dev/null)
    case "$_AIJSON" in
      '{"provider"'*) printf '%s\n' "$_AIJSON"; return 0 ;;
    esac
  fi
  p=$(_prov_of_group AI)
  u=''
  [ -n "$p" ] && u=$(_url_of_provider "$p")
  printf '{"url":"%s","provider":"%s"}' "$(json_esc "$u")" "$(json_esc "$p")"
}

# ---- slot patcher: point a group (VPN / VPN-noP2P / AI) at a subscription URL ----
# The patcher used to rewrite the config as TEXT and required `  - name: GROUP` with a
# double-quoted provider URL. Applying a verified pool re-serialises the config with PyYAML
# (`- name:` at column 0, bare scalars), so every switch failed: the AI slot claimed its
# anchor was missing while the group was present. assets/geomask-slot-patch.py parses and
# writes YAML structurally (YAML 1.2 booleans) and is syntax-checked by the test suite.
# Isolation: a provider shared with another group, or a managed audit-* file pool, is never
# re-pointed in place - the group gets its own "slot" provider, so switching AI cannot move
# VPN/VPN-noP2P or rewrite a verified pool.
SLOT_PATCH_PY=/usr/lib/geomask/geomask-slot-patch.py
# A subscription URL is a credential: keep the token out of logs and process arguments.
_mask_url() {
  # Drop the userinfo part first: some providers hand out https://user:token@host/sub, and
  # masking only the path would keep the password in the log and in the panel JSON.
  printf '%s' "$1" | sed -E 's#(https?://)[^/@[:space:]]+@#\1#; s#(https?://[^/[:space:]]+/?)[^[:space:]]*#\1…#'
}
_slot_apply() { # _slot_apply <group> <url> -> patch config so <group> is fed by <url>
  _g="$1"; URL="$2"
  case "$URL" in
    http://*|https://*) ;;
    *) printf '{"error":"url must start with http:// or https://"}'; return 1 ;;
  esac
  if printf '%s' "$URL" | grep -qE '[[:space:]"]'; then
    printf '{"error":"url contains forbidden characters"}'; return 1
  fi
  [ -f "$MIHOMO_CONF" ] || { printf '{"error":"mihomo config missing"}'; return 1; }
  if [ ! -f "$SLOT_PATCH_PY" ]; then
    printf '{"error":"slot patcher not installed"}'; return 1
  fi
  _require_backup || return 1
  cp -p "$MIHOMO_CONF" "$MIHOMO_CONF.bak_slot" 2>/dev/null
  # The URL goes through stdin, never argv: /proc/<pid>/cmdline is world-readable.
  _res=$(printf '%s\n' "$URL" | "$PYTHON_BIN" -B "$SLOT_PATCH_PY" "$MIHOMO_CONF" "$_g" - 2>&1)
  _rc=$?
  if [ "$_rc" -ne 0 ]; then
    mv -f "$MIHOMO_CONF.bak_slot" "$MIHOMO_CONF" 2>/dev/null
    printf '{"ok":false,"error":"%s"}' "$(json_esc "$_res")"; return 1
  fi
  if ! /usr/bin/mihomo -t -d "$(dirname "$MIHOMO_CONF")" >/dev/null 2>&1; then
    mv -f "$MIHOMO_CONF.bak_slot" "$MIHOMO_CONF" 2>/dev/null
    printf '{"ok":false,"error":"mihomo config test failed - old config kept"}'; return 1
  fi
  rm -f "$MIHOMO_CONF.bak_slot"
  _mihomo_reload
  echo "$(date +%FT%T) SLOT $_g <- $(_mask_url "$URL") ($_res)" >> "$LOG"
  printf '{"ok":true,"slot":"%s","change":"%s","url":"%s","node":"%s"}' \
    "$_g" "$(json_esc "$_res")" "$(json_esc "$(_mask_url "$URL")")" "$(json_esc "$(_grp_node "$_g")")"
}

cmd_ai_sub_set() {
  # v1.25.2: AI subscription switch is back - it points the AI group at this URL.
  # When the AI group shares its provider with VPN/VPN-noP2P, a dedicated provider is
  # created instead of silently re-pointing the shared one (isolation).
  _slot_apply AI "$1"
}

# ---- subscription registry (id|name|url) ----
_subs_json() {
  FIRST=1
  printf '['
  [ -f "$SUBS_FILE" ] || { printf ']'; return; }
  while IFS='|' read -r sid sname surl; do
    case "$sid" in ''|'#'*) continue ;; esac
    [ "$FIRST" -eq 1 ] || printf ','
    printf '{"id":"%s","name":"%s","url":"%s"}' \
      "$(json_esc "${sid:-}")" "$(json_esc "${sname:-}")" "$(json_esc "$(_mask_url "${surl:-}")")"
    FIRST=0
  done < "$SUBS_FILE"
  printf ']'
}

_find_sub_url() { # _find_sub_url <id> -> url on stdout, rc=1 if not found
  [ -f "$SUBS_FILE" ] || return 1
  awk -F'|' -v id="$1" '$1==id {print $3; found=1; exit} END{exit found?0:1}' "$SUBS_FILE"
}

_sub_id_by_url() { # _sub_id_by_url <url> -> id, rc=1 if not found
  [ -f "$SUBS_FILE" ] || return 1
  awk -F'|' -v u="$1" '$3==u {print $1; found=1; exit} END{exit found?0:1}' "$SUBS_FILE"
}

cmd_subs_list() {
  # fresh/empty registry: auto-import the providers currently in mihomo config
  if [ ! -s "$SUBS_FILE" ] || ! grep -qE '^[^#]' "$SUBS_FILE" 2>/dev/null; then
    _subs_seed_internal
  fi
  printf '{"subs":%s}' "$(_subs_json)"
}

_subs_count() { grep -cE '^[^#]' "$SUBS_FILE" 2>/dev/null || echo 0; }

# import current config providers into the registry (skips urls already present)
_subs_seed_internal() {
  _subu=$(awk '/^  sub:/{f=1;next} f&&/^    url:/{gsub(/^[^"]*"/,"");gsub(/".*/,"");print;exit} f&&/^  [A-Za-z0-9_-]+:/{f=0} f&&/^[A-Za-z]/{f=0}' "$MIHOMO_CONF" 2>/dev/null)
  if [ -n "$_subu" ]; then
    _subs_json | grep -Fq "$_subu" || cmd_sub_add "VPN (текущая)" "$_subu" internal >/dev/null
  fi
  echo "$(date +%FT%T) SUB seed (auto)" >> "$LOG"
}

cmd_subs_seed() {
  _subs_seed_internal
  printf '{"ok":true,"subs":%s}' "$(_subs_json)"
}

cmd_sub_add() {
  NAME="$1"; URL="$2"; _internal="${3:-}"
  case "$URL" in
    http://*|https://*) ;;
    *) printf '{"error":"url must start with http:// or https://"}'; return 1 ;;
  esac
  if printf '%s' "$URL" | grep -qE '[[:space:]"]'; then
    printf '{"error":"url contains forbidden characters"}'; return 1
  fi
  # The auto-import of the providers already in the config is not an owner action: it must
  # not fail just because no fresh backup exists yet.
  [ "$_internal" = internal ] || _require_backup || return 1
  [ -n "$NAME" ] || NAME=$(printf '%s' "$URL" | sed 's|^https\?://||; s|/.*||')
  NAME=$(printf '%s' "$NAME" | tr -d '|' | head -c 48)
  # The registry is read-modify-written (id allocation + append / delete): the shared `subs`
  # kernel lock makes the id unique and keeps a concurrent delete from dropping this append.
  command -v gm_lock_acquire >/dev/null 2>&1 && gm_lock_acquire subs 20 || {
    printf '{"ok":false,"error":"subscription registry is busy"}\n'; return 1; }
  ID=$(date +%s | tail -c 9)
  _sa_i=0
  while grep -q "^$ID|" "$SUBS_FILE" 2>/dev/null && [ "$_sa_i" -lt 100 ]; do ID=$((ID+1)); _sa_i=$((_sa_i+1)); done
  echo "$ID|$NAME|$URL" >> "$SUBS_FILE"
  # The registry holds subscription tokens: root-only, like the mihomo config itself.
  chmod 600 "$SUBS_FILE" 2>/dev/null
  command -v gm_lock_release >/dev/null 2>&1 && gm_lock_release subs
  echo "$(date +%FT%T) SUB add $ID $NAME" >> "$LOG"
  printf '{"ok":true,"subs":%s}' "$(_subs_json)"
}

cmd_sub_del() {
  ID="$1"
  case "$ID" in ''|*[!0-9]*) printf '{"error":"bad id"}'; return 1 ;; esac
  [ -f "$SUBS_FILE" ] || { printf '{"error":"registry empty"}'; return 1; }
  # A path may still be backed by this subscription. Deleting the registry entry would
  # leave its group pointing at a provider whose source no longer exists, so refuse until
  # the path is switched to another subscription.
  URL=$(awk -F'|' -v id="$ID" '$1==id {print $3; exit}' "$SUBS_FILE")
  if [ -n "$URL" ] && [ -f "$NODE_AUDIT" ]; then
    USED=$("$PYTHON_BIN" "$NODE_AUDIT" source-in-use "$URL" 2>/dev/null)
    case "$USED" in
      *'"in_use": true'*|*'"in_use":true'*)
        printf '{"error":"subscription is used by a path; switch the path to another subscription before deleting it"}'
        return 1 ;;
    esac
  fi
  command -v gm_lock_acquire >/dev/null 2>&1 && gm_lock_acquire subs 20 || {
    printf '{"ok":false,"error":"subscription registry is busy"}\n'; return 1; }
  _sd_tmp="$SUBS_FILE.tmp.$$"
  if ! grep -v "^$ID|" "$SUBS_FILE" > "$_sd_tmp"; then
    rm -f "$_sd_tmp"; command -v gm_lock_release >/dev/null 2>&1 && gm_lock_release subs
    printf '{"ok":false,"error":"cannot rewrite the subscription registry"}\n'; return 1
  fi
  if ! mv -f "$_sd_tmp" "$SUBS_FILE"; then
    rm -f "$_sd_tmp"; command -v gm_lock_release >/dev/null 2>&1 && gm_lock_release subs
    printf '{"ok":false,"error":"cannot replace the subscription registry"}\n'; return 1
  fi
  command -v gm_lock_release >/dev/null 2>&1 && gm_lock_release subs
  echo "$(date +%FT%T) SUB del $ID" >> "$LOG"
  printf '{"ok":true,"subs":%s}' "$(_subs_json)"
}

_mihomo_reload() { # re-read the config in the running core (no service restart)
  controller_curl -s -m15 -f -X PUT "$MIHOMO_API/configs" \
    -H 'Content-Type: application/json' -d "{\"path\":\"$MIHOMO_CONF\"}" >/dev/null 2>&1
  sleep 2
}

cmd_ai_sub_use() {
  # v1.25.2: the AI slot switch is back - it points the AI group at this registry subscription.
  # When the AI group shares its provider with VPN/VPN-noP2P, a dedicated provider is created
  # instead of silently re-pointing the shared one (isolation).
  _slot_use AI "$1"
}

# v1.18.2: slots are resolved through the GROUP, not a hardcoded provider name - on a box that was
# migrated the default tunnel may be fed by "sub", on a fresh install it always is.
#   slot vpn        -> group VPN         (P2P-tolerant tunnel, the default one)
#   slot vpn_nop2p  -> group VPN-noP2P   (subscription whose provider forbids P2P; created on demand)
#   slot ai         -> group AI          (v1.25.2: switch restored; a shared provider is isolated,
#                                          never silently re-pointed - switching AI cannot move VPN)

_slot_use() { # _slot_use <group> <id>  -> assigns the registry subscription to that mihomo group
  _g="$1"; ID="$2"
  URL=$(_find_sub_url "$ID") || { printf '{"error":"subscription not found"}'; return 1; }
  _slot_apply "$_g" "$URL"
}

cmd_vpn_sub_use()    { _slot_use VPN "$1"; }
cmd_vpnnp_sub_use()  { _slot_use VPN-noP2P "$1"; }

# ---------------- self-update (v1.7.0): check / status / apply ----------------
# check -> $UPD_CHECK (cache TTL), apply -> detached runner writes $UPD_STATUS.
# Flow: full backup -> download release artifact -> sh -n + version + sha256
# -> "sh new --reinstall --yes" (module files only, no service restarts) -> rpcd reload.

_upd_esc() { sed 's/\\/\\\\/g; s/"/\\"/g'; }   # stdin -> json-safe

_upd_mark() { # $1=file, $2=marker name; supports NAME="v" and NAME = 'v'
  [ -f "$1" ] || { printf 'none'; return 0; }
  sed -n "s/.*$2[[:space:]]*=[[:space:]]*[\"']\([^\"']*\)[\"'].*/\1/p" "$1" 2>/dev/null | head -1
}

_upd_core_ver() { # release identity: the deployed marker first, then an installer copy
  # v1.30.3: /etc/geomask/version is written by the installer and by a deploy, so it always
  # describes THIS router. The /root and /tmp copies are leftovers from old runs: one of them
  # (1.25.3) made the panel offer a "downgrade" to the release it already had.
  if [ -s /etc/geomask/version ]; then
    _v=$(sed -n 's/^[[:space:]]*\([0-9][0-9.]*\).*/\1/p' /etc/geomask/version 2>/dev/null | head -1)
    [ -n "$_v" ] && { printf '%s' "$_v"; return 0; }
  fi
  for f in /tmp/geomask.sh /root/geomask.sh; do
    [ -f "$f" ] || continue
    _v=$(sed -n 's/^GM_VERSION="\([^"]*\)".*/\1/p' "$f" 2>/dev/null | head -1)
    [ -n "$_v" ] && { printf '%s' "$_v"; return 0; }
  done
  printf 'none'
}

_upd_newer() { # $1 candidate > $2 current ? (numeric X.Y.Z, missing parts = 0)
  _a=$(printf '%s' "$1" | sed 's/^v//; s/[^0-9.].*$//')
  _b=$(printf '%s' "$2" | sed 's/^v//; s/[^0-9.].*$//')
  [ -n "$_a" ] && [ -n "$_b" ] || return 1
  _i=1
  while [ "$_i" -le 3 ]; do
    _x=$(printf '%s' "$_a" | cut -d. -f"$_i"); _y=$(printf '%s' "$_b" | cut -d. -f"$_i")
    _x=${_x:-0}; _y=${_y:-0}
    case "$_x$_y" in *[!0-9]*) return 1 ;; esac
    [ "$_x" -gt "$_y" ] && return 0
    [ "$_x" -lt "$_y" ] && return 1
    _i=$((_i+1))
  done
  return 1
}

_upd_bin_info() { # first existing path -> "file YYYY-MM-DD"
  for f in "$@"; do
    if [ -f "$f" ]; then printf 'file %s' "$(date -r "$f" +%F 2>/dev/null || printf '?')"; return 0; fi
  done
  printf 'not found'
}

_upd_stack_json() { # display-only versions of the managed stack
  _mv=none
  if command -v mihomo >/dev/null 2>&1; then
    _mv=$(mihomo -v 2>&1 | head -1 | tr -d '\r' | head -c 90)
    [ -n "$_mv" ] || _mv=none
  fi
  _o="{\"n\":\"mihomo\",\"v\":\"$(printf '%s' "$_mv" | _upd_esc)\"}"
  _o="$_o,{\"n\":\"magitrickle\",\"v\":\"$(_upd_bin_info /usr/bin/magitrickled)\"}"
  _o="$_o,{\"n\":\"hev-socks5-tunnel\",\"v\":\"$(_upd_bin_info /usr/sbin/hev-socks5-tunnel /usr/bin/hev-socks5-tunnel)\"}"
  _o="$_o,{\"n\":\"tg-ws-proxy\",\"v\":\"$(_upd_bin_info /usr/bin/tg-ws-proxy)\"}"
  _o="$_o,{\"n\":\"https-dns-proxy\",\"v\":\"$(_upd_bin_info /usr/sbin/https-dns-proxy)\"}"
  _o="$_o,{\"n\":\"zapret / nfqws\",\"v\":\"$(_upd_bin_info /opt/zapret/nfq/nfqws)\"}"
  printf '[%s]' "$_o"
}

# Live "installed versions" block + need flag for a given latest version. Sets _LBLK and _LNEED.
# v1.14.2: the update card used to serve these straight from the TTL cache, so after a deploy the
# panel kept showing the OLD installed versions for up to an hour (looked like "not updated").
_upd_local_block() { # $1 = latest version (no leading v)
  _core=$(_upd_core_ver)
  _panel=$(_upd_mark "$DEPLOY_JS" PANEL_VERSION); [ -n "$_panel" ] || _panel=none
  _guard=$(_upd_mark "$DEPLOY_GUARD" GUARD_VERSION); [ -n "$_guard" ] || _guard=none
  _backend=$(_upd_mark "$DEPLOY_BACKEND" BACKEND_VERSION); [ -n "$_backend" ] || _backend=none
  _LNEED=false
  # The release identity comes from the deployed (flash) components - newest wins. The core
  # marker is only a fallback: an installer copy under /tmp dies on every reboot, and a stale
  # copy in /root once made a 1.30.0 router compare itself against 1.25.3 and offer a
  # "downgrade" to the release it already had.
  _cur=none
  for _cand in "$_backend" "$_panel" "$_guard"; do
    [ "$_cand" = "none" ] && continue
    [ -z "$_cand" ] && continue
    if [ "$_cur" = "none" ]; then _cur=$_cand
    elif _upd_newer "$_cand" "$_cur"; then _cur=$_cand
    fi
  done
  [ "$_cur" = "none" ] && _cur=$_core
  if [ "$_cur" = "none" ]; then _LNEED=true          # nothing deployed at all -> really needs install
  elif _upd_newer "$1" "$_cur"; then _LNEED=true
  elif [ "$_cur" = "$1" ] && { [ "$_panel" != "$1" ] || [ "$_guard" != "$1" ] || [ "$_backend" != "$1" ]; }; then
    _LNEED=true   # partial/misaligned deploy of the same release -> repair
  fi
  _LBLK=$(printf '{"core":"%s","panel":"%s","guard":"%s","backend":"%s"}' "$_core" "$_panel" "$_guard" "$_backend")
}

_upd_check_now() { # $1 = "force" bypasses the TTL cache
  mkdir -p "$UPD_DIR" 2>/dev/null
  _age=$(( $(date +%s) - $(date -r "$UPD_CHECK" +%s 2>/dev/null || echo 0) ))
  if [ "$1" != "force" ] && [ -s "$UPD_CHECK" ] && [ "$_age" -lt "$UPD_TTL" ]; then
    # cached GitHub answer, but installed versions are always recomputed live
    _lv=$(sed -n 's/.*"latest":{[^}]*"version":"\([^"]*\)".*/\1/p' "$UPD_CHECK" | head -1)
    if [ -n "$_lv" ]; then
      _upd_local_block "$_lv"
      sed -e 's#"local":{[^}]*}#"local":'"$_LBLK"'#' -e 's#"need":[a-z]*#"need":'"$_LNEED"'#' "$UPD_CHECK"
      return 0
    fi
    cat "$UPD_CHECK"; return 0
  fi
  _j=$(curl -fsS -m 10 -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$UPD_REPO/releases/latest" 2>/dev/null)
  # GitHub answers pretty-printed JSON -> allow whitespace around ':'
  _tag=$(printf '%s' "$_j" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
  if [ -n "$_tag" ]; then
    _src=github
    _dig=$(printf '%s' "$_j" | sed -n 's/.*"digest"[[:space:]]*:[[:space:]]*"\(sha256:[0-9a-f]*\)".*/\1/p' | head -1)
    _pub=$(printf '%s' "$_j" | sed -n 's/.*"published_at"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
  else
    # fallback: version from raw master (no digest -> sha256 check skipped at apply)
    _rv=$(curl -fsS -m 12 "https://raw.githubusercontent.com/$UPD_REPO/master/geomask.sh" 2>/dev/null \
      | sed -n 's/^GM_VERSION="\([^"]*\)".*/\1/p' | head -1)
    [ -n "$_rv" ] || { printf '{"ok":false,"error":"version source unreachable"}'; return 1; }
    _tag="v$_rv"; _src=raw; _dig=""; _pub=""
  fi
  _ver=$(printf '%s' "$_tag" | sed 's/^v//')
  _upd_local_block "$_ver"
  _need=$_LNEED
  _now=$(date '+%F %T')
  printf '{"ok":true,"checked_at":"%s","src":"%s","local":%s,"latest":{"tag":"%s","version":"%s","digest":"%s","published":"%s"},"need":%s,"stack":%s}\n' \
    "$_now" "$_src" "$_LBLK" \
    "$_tag" "$_ver" "$_dig" "$_pub" "$_need" "$(_upd_stack_json)" > "$UPD_CHECK.tmp" \
    && mv -f "$UPD_CHECK.tmp" "$UPD_CHECK"
  cat "$UPD_CHECK"
}

_upd_running() {
  # The runner holds the shared kernel `updater` lock from the moment it is spawned until it
  # exits; a pid file was check-then-write and a killed runner left a pid that could be
  # reused, so the lock is the truth here (R10).
  command -v gm_lock_busy >/dev/null 2>&1 || return 1
  gm_lock_busy updater "$UPD_LOCK"
}

cmd_upd_check() { _upd_check_now "${1:-}"; printf '\n'; }

# v1.10.0: "stack state" for the panel - sync/last-run, guard events, pins, DPI strategy, health
cmd_stack() {
  LANIP=$(ip -4 addr show br-lan 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -1)
  [ -n "$LANIP" ] || LANIP=192.168.0.1
  _sync=$(cat /tmp/geomask-sync.state 2>/dev/null)
  case "$_sync" in '{'*'}'*) : ;; *) _sync='{"ts":"","changed":false,"reload":null,"mt":0,"ai":0,"moved":0}' ;; esac
  _sd=false; pgrep -f geomask-syncd.sh >/dev/null 2>&1 && _sd=true
  _sde=false; ls /etc/rc.d/ 2>/dev/null | grep -q geomask-syncd && _sde=true
  _sci=$(crontab -l 2>/dev/null | grep 'sync_mtrickle_mihomo.sh' | head -1 | awk '{print $1}')
  [ -n "$_sci" ] || _sci='*/5'
  _nse=false; crontab -l 2>/dev/null | grep -q 'geomask-nodescan$' && _nse=true
  _ns=$(cat /tmp/geomask-nodescan.state 2>/dev/null)
  case "$_ns" in '{'*'}'*) : ;; *) _ns=null ;; esac
  _guard=$(logread 2>/dev/null | grep geomask-guard | grep -v crond | tail -5 | while IFS= read -r _l; do printf '"%s",' "$(json_esc "$_l")"; done | sed 's/,$//')
  _pin=false; [ -e /root/.geomask-norestart ] && _pin=true
  _strat=$(json_esc "$(sed -n "/option NFQWS_OPT '/,+1p" /etc/config/zapret 2>/dev/null | tail -1 | tr -d " \t'")")
  _nu=$(grep -c . /opt/zapret/ipset/zapret-hosts-user.txt 2>/dev/null)
  _ne=$(grep -c . /opt/zapret/ipset/zapret-hosts-user-exclude.txt 2>/dev/null)
  _nc=$(grep -cE '#[[:space:]]*nochange' /opt/zapret/ipset/zapret-hosts-user-exclude.txt 2>/dev/null)
  _ng=$(grep -c . /opt/zapret/ipset/zapret-hosts-google.txt 2>/dev/null)
  _nr=$(grep -c . /opt/zapret/ipset/zapret-hosts-rkn.txt 2>/dev/null)
  _nt=$(pgrep -f tg-ws-proxy 2>/dev/null | wc -l)
  _land=false; wget -q -T 3 -O /dev/null http://127.0.0.1:8000/ 2>/dev/null && _land=true
  printf '{"ok":true,"sync":%s,"syncd":%s,"syncd_enabled":%s,"sync_interval":"%s","nodescan_enabled":%s,"nodescan":%s,"guard":[%s],"pin":%s,"strategy":"%s","lists":{"user":%d,"exclude":%d,"nochange":%d,"google":%d,"rkn":%d},"health":{"tg":%d,"landing":%s},"links":{"magitrickle":"http://%s:8080","zapret":"http://%s:7788","landing":"http://%s:8000","luci":"http://%s/"}}\n' \
    "$_sync" "$_sd" "$_sde" "$_sci" "$_nse" "$_ns" "$_guard" "$_pin" "$_strat" \
    "${_nu:-0}" "${_ne:-0}" "${_nc:-0}" "${_ng:-0}" "${_nr:-0}" \
    "${_nt:-0}" "$_land" "$LANIP" "$LANIP" "$LANIP" "$LANIP"
}

# v1.9.0: backup folder inventory for the panel (SMB link + files list, newest first)
# v1.9.4: UNC contains backslashes - MUST go through json_esc (raw \g breaks rpcd parsing)
cmd_backups() {
  DIR="$BACKUP_DIR"
  LANIP=$(ip -4 addr show br-lan 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -1)
  [ -n "$LANIP" ] || LANIP=192.168.0.1
  _sh=false
  grep -qF '[geomask-backups]' /etc/ksmbd/ksmbd.conf 2>/dev/null && _sh=true
  _unc=$(json_esc "$(printf '\\\\%s\\geomask-backups' "$LANIP")")
  _smbu=$(json_esc "$(printf 'smb://%s/geomask-backups' "$LANIP")")
  _files=''
  for f in $(ls -t "$DIR"/router-backup-*.tar.gz 2>/dev/null); do
    [ -f "$f" ] || continue
    _n=$(basename "$f")
    _sz=$(wc -c < "$f" 2>/dev/null | tr -d ' ')
    _t=$(date -r "$f" '+%F %T' 2>/dev/null)
    _files="$_files{\"n\":\"$_n\",\"s\":${_sz:-0},\"t\":\"$_t\"},"
  done
  _files=${_files%,}
  # No credentials are advertised: the share is created only on explicit opt-in and uses a
  # named ksmbd user the owner creates. The old fields exposed a known backup/backup login.
  _optin=false
  [ -f /etc/geomask/backup-share.enable ] && _optin=true
  printf '{"ok":true,"dir":"%s","unc":"%s","smb":"%s","share_ok":%s,"optin":%s,"auth":"named user (ksmbd.adduser)","files":[%s]}\n' \
    "$DIR" "$_unc" "$_smbu" "$_sh" "$_optin" "$_files"
}

MODULE_UPDATER=/usr/lib/geomask/geomask-module-update.py
DOCTOR=/usr/lib/geomask/geomask-doctor.py

# The diagnostics engine ships as its own optional module: without it the tab must say so
# instead of failing with an empty answer.
_diag_missing() {
  printf '{"ok":false,"error":"модуль doctor не установлен"}\n'
  return 1
}

cmd_diag_list() {
  # Registry plus the results of the last run; never runs a check, so the tab paints fast.
  [ -f "$DOCTOR" ] || { _diag_missing; return 1; }
  python3 "$DOCTOR" list 2>&1
}

cmd_diag_run() { # $1 = check id or "all"
  [ -f "$DOCTOR" ] || { _diag_missing; return 1; }
  case "$1" in
    ''|*[!A-Za-z0-9_]*) printf '{"ok":false,"error":"bad check id"}\n'; return 1 ;;
  esac
  python3 "$DOCTOR" run "$1" 2>&1
}

cmd_diag_log() { # $1 = check id
  [ -f "$DOCTOR" ] || { _diag_missing; return 1; }
  case "$1" in
    ''|*[!A-Za-z0-9_]*) printf '{"ok":false,"error":"bad check id"}\n'; return 1 ;;
  esac
  python3 "$DOCTOR" log "$1" 2>&1
}

cmd_diag_domain() { # $1 = domain, $2 = "live" to make a real request
  [ -f "$DOCTOR" ] || { _diag_missing; return 1; }
  case "$1" in
    ''|*[!A-Za-z0-9._-]*) printf '{"ok":false,"error":"bad domain"}\n'; return 1 ;;
  esac
  if [ "$2" = "live" ] || [ "$2" = "1" ]; then
    python3 "$DOCTOR" domain "$1" --live 2>&1
  else
    python3 "$DOCTOR" domain "$1" 2>&1
  fi
}

cmd_mod_list() {
  # Installed modules and what the cached index says about them. Read-only: never
  # touches the network, so the panel can paint the list instantly.
  if [ ! -f "$MODULE_UPDATER" ]; then
    printf '{"ok":false,"error":"module updater not installed"}\n'; return 1
  fi
  python3 "$MODULE_UPDATER" status 2>&1
}

cmd_mod_check() {
  # Force a fresh index fetch; falls back to the cache when the network is down.
  if [ ! -f "$MODULE_UPDATER" ]; then
    printf '{"ok":false,"error":"module updater not installed"}\n'; return 1
  fi
  python3 "$MODULE_UPDATER" check 2>&1
}

cmd_mod_update() { # $1 = module id, $2 = "force" to overwrite local edits
  if [ ! -f "$MODULE_UPDATER" ]; then
    printf '{"ok":false,"error":"module updater not installed"}\n'; return 1
  fi
  case "$1" in
    ''|*[!A-Za-z0-9_]*) printf '{"ok":false,"error":"bad module id"}\n'; return 1 ;;
  esac
  _require_backup || return 1
  if [ "$2" = "force" ]; then
    python3 "$MODULE_UPDATER" update "$1" --force 2>&1
  else
    python3 "$MODULE_UPDATER" update "$1" 2>&1
  fi
}

cmd_upd_status() {
  mkdir -p "$UPD_DIR" 2>/dev/null
  if [ -s "$UPD_STATUS" ]; then _b=$(cat "$UPD_STATUS"); else _b='{"phase":"idle"}'; fi
  # v1.8.0 console list: tail of steps.log as a "lines" JSON array (each say() appends there)
  _lines=$(tail -60 "$UPD_DIR/steps.log" 2>/dev/null | while IFS= read -r _l; do printf '"%s",' "$(json_esc "$_l")"; done | sed 's/,$//')
  printf '%s,"lines":[%s]}\n' "${_b%\}}" "$_lines"
}

cmd_upd_apply() {
  mkdir -p "$UPD_DIR" 2>/dev/null
  if _upd_running; then printf '{"ok":false,"err":"already running"}\n'; return 1; fi
  _upd_check_now "" >/dev/null 2>&1 || true
  [ -s "$UPD_CHECK" ] || { printf '{"ok":false,"err":"version source unreachable - check network"}\n'; return 1; }
  _tag=$(sed -n 's/.*"tag":"\([^"]*\)".*/\1/p' "$UPD_CHECK" | head -1)
  [ -n "$_tag" ] || { printf '{"ok":false,"err":"bad check cache"}\n'; return 1; }
  # An installer runs as root: without a digest from the release API there is nothing to
  # verify the download against, and a hash from the same source proves nothing (F22).
  _dig=$(sed -n 's/.*"digest":"\(sha256:[0-9a-f]*\)".*/\1/p' "$UPD_CHECK" | head -1)
  [ -n "$_dig" ] || { printf '{"ok":false,"err":"release digest unavailable - refusing to run an unverified installer"}\n'; return 1; }
  # Single instance: the kernel lock is acquired BEFORE the runner script is written, and the
  # detached runner inherits the descriptor, so the lock lives exactly as long as the update.
  # The old check-then-write pid let two callers both pass the check (R10).
  command -v gm_lock_acquire >/dev/null 2>&1 || {
    printf '{"ok":false,"err":"lock helper missing - refusing to start an update"}\n'; return 1; }
  gm_lock_acquire updater 0 "$UPD_LOCK" || {
    printf '{"ok":false,"err":"already running"}\n'; return 1; }
  cat > "$UPD_DIR/run.sh" <<'RUNEOF'
#!/bin/sh
# geomask self-update runner (detached): backup -> download -> validate -> reinstall
# v1.8.0: every step appended to $D/steps.log (panel console list) + /root/geo-sync.log;
#         check cache removed after success so the page shows real fresh versions.
D=/tmp/geomask-upd
ST=$D/status.json
SL=$D/steps.log
LOGF=/root/geo-sync.log
say() { _t=$(date '+%F %T'); _j=$(printf '%s' "$2" | sed 's/\\/\\\\/g; s/"/\\"/g'); printf '{"phase":"%s","msg":"%s","ts":"%s"}' "$1" "$_j" "$_t" > "$ST.tmp"; mv -f "$ST.tmp" "$ST"; printf '%s [%s] %s\n' "$_t" "$1" "$2" >> "$SL"; printf '%s GEOMASK-UPD [%s] %s\n' "$_t" "$1" "$2" >> "$LOGF"; }
trap ':' EXIT
TAG="$1"
say queued "update to $TAG started"
say backup "creating full router backup (sysupgrade -b)..."
TS=$(date +%Y%m%d-%H%M%S)
BKD=/root/geomask-backups
PARK=$D/bk-park-$$
BK=$D/router-backup-$TS.tar.gz
# Park the previous copies instead of deleting them: they must not be inside /root while
# sysupgrade archives it (nesting), but a failed sysupgrade must not cost the router its
# only backup. The installer has behaved this way since v1.30.15; the updater did not.
# Copies from /root and /tmp are parked apart: one `mv` into a single directory let a /tmp
# file with the same name overwrite the permanent copy before the backup even started.
mkdir -p "$BKD" "$PARK/root" "$PARK/tmp" 2>/dev/null
_park_fail=0
for _f in "$BKD"/router-backup-*.tar.gz; do
  [ -f "$_f" ] || continue
  mv -f "$_f" "$PARK/root/" 2>/dev/null || _park_fail=1
done
for _f in /tmp/router-backup-*.tar.gz; do
  [ -f "$_f" ] || continue
  mv -f "$_f" "$PARK/tmp/" 2>/dev/null || _park_fail=1
done
# rc=0 only when every parked copy is back; the park is never deleted on a failed restore.
restore_old() {
  _r=0
  for _f in "$PARK/root"/*.tar.gz; do
    [ -f "$_f" ] || continue
    mv -f "$_f" "$BKD/" 2>/dev/null || _r=1
  done
  for _f in "$PARK/tmp"/*.tar.gz; do
    [ -f "$_f" ] || continue
    mv -f "$_f" /tmp/ 2>/dev/null || _r=1
  done
  [ "$_r" = 0 ] && rm -rf "$PARK"
  return $_r
}
if [ "$_park_fail" = 1 ]; then
  restore_old || say error "previous copies left in $PARK - move them back manually"
  say error "backup rotation failed - update aborted (previous backup kept)"; exit 1
fi
# rc=0 and a non-empty file are not enough: the archive must list entries (a gzip stream of
# arbitrary bytes passes `gzip -t` and is not a backup) and carry the router tree, or it is
# a readable tar of one random file (audit 14.11.2026, F29).
if ! sysupgrade -b "$BK" >/dev/null 2>&1 || ! tar -tzf "$BK" >/dev/null 2>&1 \
   || ! tar -tzf "$BK" 2>/dev/null | grep -qE '(^|/)etc/'; then
  rm -f "$BK"
  restore_old || say error "previous copies left in $PARK - move them back manually"
  say error "backup failed - update aborted (previous backup kept)"; exit 1
fi
SZ=$(wc -c < "$BK")
_md=$(md5sum "$BK" 2>/dev/null | awk '{print $1}')
BKF="$BKD/router-backup-$TS.tar.gz"
# A copy in /tmp is not a permanent backup: a failed move must abort the update, not pretend
# that the router still has something to restore after a reboot.
if ! mv -f "$BK" "$BKF" 2>/dev/null || [ ! -s "$BKF" ]; then
  rm -f "$BKF"
  restore_old || say error "previous copies left in $PARK - move them back manually"
  say error "cannot store backup in $BKD - update aborted (previous backup kept, new copy left at $BK)"; exit 1
fi
# The archive carries keys and subscription tokens: private even when the folder is shared.
chmod 600 "$BKF" 2>/dev/null
# the new archive is verified and in place: the parked copies are now superseded
rm -rf "$PARK" 2>/dev/null
echo "file=$BKF md5=$_md" > "$D/backup.info"
LANIP=$(ip -4 addr show br-lan 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -1)
say backup "backup OK: $BKF ($SZ bytes, md5 $_md)"
say backup "SMB (retention: freshest one): \\\\$LANIP\\geomask-backups\\${BKF##*/} - only when the share was opted in (touch /etc/geomask/backup-share.enable) and a named ksmbd user exists; PC history: fetch_backup.ps1"
say download "downloading geomask $TAG..."
rm -f "$D/geomask.sh.new"
if ! curl -fsSL -m 90 -o "$D/geomask.sh.new" "https://cdn.jsdelivr.net/gh/kabzon93region/geomask@${TAG}/geomask.sh" && \
   ! curl -fsSL -m 90 -o "$D/geomask.sh.new" "https://github.com/kabzon93region/geomask/releases/download/${TAG}/geomask.sh"; then
  say error "download failed (jsdelivr + github)"; exit 1
fi
say download "downloaded $(wc -c < "$D/geomask.sh.new") bytes"
say validate "checking syntax (sh -n)..."
if ! sh -n "$D/geomask.sh.new"; then say error "syntax check failed"; exit 1; fi
NEWV=$(sed -n 's/^GM_VERSION="\([^"]*\)".*/\1/p' "$D/geomask.sh.new" | head -1)
if [ -z "$NEWV" ] || [ "v$NEWV" != "$TAG" ]; then
  say error "version mismatch (file=${NEWV:-none} tag=$TAG)"; exit 1
fi
DIG=$(sed -n 's/.*"digest":"\(sha256:[0-9a-f]*\)".*/\1/p' "$D/check.json" | head -1)
if [ -z "$DIG" ]; then
  # No digest, no run: the whole point of the check is to verify the bytes before root runs them.
  say error "release digest missing - refusing to run an unverified installer"; exit 1
fi
ACT="sha256:$(sha256sum "$D/geomask.sh.new" | awk '{print $1}')"
[ "$ACT" = "$DIG" ] || { say error "sha256 mismatch"; exit 1; }
say validate "version $NEWV ok, sha256 matches release digest"
say apply "installing modules and panel (no service restarts)..."
if ! sh "$D/geomask.sh.new" --reinstall --yes >> "$D/upd.log" 2>&1; then
  say error "install failed - see $D/upd.log"; exit 1
fi
cp -f "$D/geomask.sh.new" /tmp/geomask.sh 2>/dev/null && chmod +x /tmp/geomask.sh 2>/dev/null
/etc/init.d/rpcd reload >/dev/null 2>&1 || true
rm -f "$D/check.json"   # force fresh version check on the next page load
sleep 1
say done "updated to $NEWV - refresh the panel page (Ctrl+F5)"
RUNEOF
  printf '{"phase":"queued","msg":"starting update to %s","ts":"%s"}\n' "$_tag" "$(date '+%F %T')" > "$UPD_STATUS"
  sh "$UPD_DIR/run.sh" "$_tag" >/dev/null 2>&1 &
  # The runner inherited the `updater` descriptor: the lock is released by the kernel when it
  # (and every copy of that open file description) exits. Do NOT unlock here - that would
  # release the lock of the running update.
  printf '{"ok":true,"to":"%s"}\n' "$_tag"
}

# ---------- v1.12.0: node audit control + automation settings ----------
NODESCONF=/etc/geomask-nodes.conf
NODESBLOCK=/etc/geomask-node-block.txt
NODESCAN_TAG='# geomask-nodescan'
SYNC_TAG='geomask-sync'

# read key from nodes conf
_nconf() { sed -n "s/^$1=//p" "$NODESCONF" 2>/dev/null | head -1; }

cmd_nodes_get() {
  _en=false
  crontab -l 2>/dev/null | grep -q 'geomask-nodescan$' && _en=true
  _line=$(crontab -l 2>/dev/null | grep -F "$NODESCAN_TAG" | head -1)
  _min=$(printf '%s' "$_line" | awk '{print $1}')
  _hr=$(printf '%s' "$_line" | awk '{print $2}')
  [ -n "$_hr" ] || { _hr=3; _min=30; }
  _sched=$(printf '%02d:%02d' "${_hr:-3}" "${_min:-30}" 2>/dev/null || echo '03:30')
  _st=$(cat /tmp/geomask-nodescan.state 2>/dev/null)
  case "$_st" in '{'*'}'*) : ;; *) _st=null ;; esac
  _blk=$(while IFS= read -r _l; do [ -n "$_l" ] && printf '"%s",' "$(json_esc "$_l")"; done < "$NODESBLOCK" 2>/dev/null | sed 's/,$//')
  _lastlog=$(grep NODESCAN "$LOG" 2>/dev/null | tail -3 | while IFS= read -r _l; do printf '"%s",' "$(json_esc "$_l")"; done | sed 's/,$//')
  # conf fields with node-scan defaults (must mirror geomask-node-scan.sh)
  printf '{"ok":true,"enabled":%s,"schedule":"%s","state":%s,"conf":{"bad_countries":"%s","bad_asns":"%s","base_filter":"%s","sub_provider":"%s","name_filter_groups":"%s","sub_user_agent":"%s","ai_guard":"%s","ai_group":"%s","ai_egress_url":"%s","ai_expect":"%s","ai_listener":"%s","ai_exclude_base":"%s"},"blocked":[%s],"log":[%s]}\n' \
    "$_en" "$_sched" "$_st" \
    "$(_nconf bad_countries)" "$(_nconf bad_asns)" "$(_nconf base_filter)" "$(_nconf sub_provider)" \
    "$(_nconf name_filter_groups)" "$(_nconf sub_user_agent)" "$(_nconf ai_guard)" \
    "$(_nconf ai_group)" "$(_nconf ai_egress_url)" "$(_nconf ai_expect)" \
    "$(_nconf ai_listener)" "$(_nconf ai_exclude_base)" \
    "$_blk" "$_lastlog"
}

# args: enabled(0|1) HH:MM countries asns base provider
# The settings file belongs to the owner: only the four editable keys may change, and the
# file and the cron line are one transaction. The old version rewrote the whole file with a
# four-line template (dropping ai_*, name_filter_groups, ...) and accepted a newline inside
# base_filter, which appended an arbitrary key such as ai_guard=0 (audit 14.11.2026, F07).
# The lock is the shared kernel `cron` lock: it serialises this file together with every
# other read-modify-write of /etc/crontabs/root (cmd_sync_set, the installer, uninstall).
NODES_LOCK=${GM_NODES_LOCK:-/tmp/geomask-locks/cron.lock}

_nodes_lock() {
  command -v gm_lock_acquire >/dev/null 2>&1 || return 1
  gm_lock_acquire cron 20 "$NODES_LOCK"
}

_nodes_unlock() {
  command -v gm_lock_release >/dev/null 2>&1 || return 0
  gm_lock_release cron
}

_nodes_candidate() { # stdin: live conf -> stdout: candidate with the four keys replaced
  awk -v cc="$1" -v as="$2" -v bf="$3" -v pv="$4" '
    BEGIN{dcc=das=dbf=dpv=0}
    /^[ \t]*#/ {print; next}
    /^bad_countries=/ {print "bad_countries=" cc; dcc=1; next}
    /^bad_asns=/      {print "bad_asns=" as;      das=1; next}
    /^base_filter=/   {print "base_filter=" bf;   dbf=1; next}
    /^sub_provider=/  {print "sub_provider=" pv;  dpv=1; next}
    {print}
    END{
      if (!dcc) print "bad_countries=" cc
      if (!das) print "bad_asns=" as
      if (!dbf) print "base_filter=" bf
      if (!dpv) print "sub_provider=" pv
    }'
}

cmd_nodes_set() {
  _en=$1; _sched=$2; _cc=$3; _as=$4; _base=$5; _prov=$6
  case "$_en" in 0|1) : ;; *) printf '{"ok":false,"error":"bad enabled"}\n'; return 1;; esac
  printf '%s' "$_sched" | grep -qE '^([01][0-9]|2[0-3]):[0-5][0-9]$' || { printf '{"ok":false,"error":"bad schedule (HH:MM)"}\n'; return 1; }
  [ -z "$_cc" ] || printf '%s' "$_cc" | grep -qE '^([A-Z][A-Z],)*[A-Z][A-Z]$' || { printf '{"ok":false,"error":"bad countries (AA,BB)"}\n'; return 1; }
  [ -z "$_as" ] || printf '%s' "$_as" | grep -qE '^(AS?[0-9]+,)*AS?[0-9]+$' || { printf '{"ok":false,"error":"bad asns"}\n'; return 1; }
  printf '%s' "$_base" | grep -qE '["'"'"'\\]' && { printf '{"ok":false,"error":"base filter: no quotes/backslashes"}\n'; return 1; }
  [ -z "$_prov" ] && _prov=sub
  printf '%s' "$_prov" | grep -qE '^[a-z0-9_-]+$' || { printf '{"ok":false,"error":"bad provider name"}\n'; return 1; }
  # No control characters anywhere: a newline in a value writes a second key into the file.
  for _nv in "$_sched" "$_cc" "$_as" "$_base" "$_prov"; do
    case "$_nv" in *[[:cntrl:]]*) printf '{"ok":false,"error":"control characters are not allowed"}\n'; return 1;; esac
  done
  _nodes_lock || { printf '{"ok":false,"error":"another settings write is running"}\n'; return 1; }
  _ntmp=$(mktemp -d /tmp/geomask-nodes.XXXXXX 2>/dev/null) || { _nodes_unlock; printf '{"ok":false,"error":"cannot create a work directory"}\n'; return 1; }
  _nold=0
  if [ -f "$NODESCONF" ]; then
    # If the previous file cannot be saved there is no rollback source: writing a four-key
    # stub over the owner's settings would lose ai_* and every unknown field.
    cp -p "$NODESCONF" "$_ntmp/conf.old" 2>/dev/null || {
      rm -rf "$_ntmp"; _nodes_unlock
      printf '{"ok":false,"error":"cannot back up %s - settings unchanged"}\n' "$NODESCONF"; return 1; }
    _nold=1
  fi
  crontab -l > "$_ntmp/cron.old" 2>/dev/null || : > "$_ntmp/cron.old"
  if [ "$_nold" = 1 ]; then
    _nodes_candidate "$_cc" "$_as" "$_base" "$_prov" < "$NODESCONF" > "$_ntmp/conf.new" || {
      rm -rf "$_ntmp"; _nodes_unlock; printf '{"ok":false,"error":"cannot render the settings file"}\n'; return 1; }
  else
    _nodes_candidate "$_cc" "$_as" "$_base" "$_prov" < /dev/null > "$_ntmp/conf.new" || {
      rm -rf "$_ntmp"; _nodes_unlock; printf '{"ok":false,"error":"cannot render the settings file"}\n'; return 1; }
  fi
  _hr=${_sched%:*}; _min=${_sched#*:}
  _hrn=$(printf '%s' "$_hr" | sed 's/^0//'); _minn=$(printf '%s' "$_min" | sed 's/^0//')
  [ -n "$_hrn" ] || _hrn=0
  grep -vF "$NODESCAN_TAG" "$_ntmp/cron.old" > "$_ntmp/cron.new" 2>/dev/null || : > "$_ntmp/cron.new"
  if [ "$_en" = 1 ]; then
    printf '%s %s * * * /usr/bin/geomask-node-scan.sh >/dev/null 2>&1 %s\n' "$_minn" "$_hrn" "$NODESCAN_TAG" >> "$_ntmp/cron.new"
  fi
  # 1) cron first: if it cannot be written nothing else has changed yet
  if ! crontab "$_ntmp/cron.new" 2>/dev/null; then
    rm -rf "$_ntmp"; _nodes_unlock
    printf '{"ok":false,"error":"crontab write failed - settings unchanged"}\n'; return 1
  fi
  # 2) settings file, with the previous bytes kept for the rollback
  if ! cp "$_ntmp/conf.new" "$NODESCONF" 2>/dev/null; then
    crontab "$_ntmp/cron.old" 2>/dev/null
    rm -rf "$_ntmp"; _nodes_unlock
    printf '{"ok":false,"error":"cannot write %s - settings and cron restored"}\n' "$NODESCONF"; return 1
  fi
  chmod 644 "$NODESCONF" 2>/dev/null
  # 3) read back: a short write must not be reported as success
  _nbad=""
  [ "$(_nconf bad_countries)" = "$_cc" ] || _nbad="$_nbad bad_countries"
  [ "$(_nconf bad_asns)" = "$_as" ] || _nbad="$_nbad bad_asns"
  [ "$(_nconf base_filter)" = "$_base" ] || _nbad="$_nbad base_filter"
  [ "$(_nconf sub_provider)" = "$_prov" ] || _nbad="$_nbad sub_provider"
  if [ -n "$_nbad" ]; then
    if [ "$_nold" = 1 ]; then cp -p "$_ntmp/conf.old" "$NODESCONF" 2>/dev/null; else rm -f "$NODESCONF"; fi
    crontab "$_ntmp/cron.old" 2>/dev/null
    rm -rf "$_ntmp"; _nodes_unlock
    printf '{"ok":false,"error":"settings did not survive the write:%s - restored"}\n' "$_nbad"; return 1
  fi
  rm -rf "$_ntmp"
  _nodes_unlock
  printf '%s NODESCAN cfg saved via panel (enabled=%s sched=%s)\n' "$(date '+%F %T')" "$_en" "$_sched" >> "$LOG"
  printf '{"ok":true,"enabled":"%s","schedule":"%s"}\n' "$_en" "$_sched"
}

# sync insurance cron interval: */N minutes (N: 1,2,3,5,10,15,30)
cmd_sync_set() {
  case "$1" in 1|2|3|5|10|15|30) : ;; *) printf '{"ok":false,"error":"bad interval"}\n'; return 1;; esac
  # The crontab is shared with nodes_set, the installer and uninstall: same kernel lock, so a
  # concurrent read-modify-write can no longer drop the other writer's line.
  _nodes_lock || { printf '{"ok":false,"error":"crontab is busy: another writer holds the lock"}\n'; return 1; }
  _tmp=/tmp/gm-cron.$$
  crontab -l 2>/dev/null | grep -vF "/root/sync_mtrickle_mihomo.sh" > "$_tmp" || true
  printf '*/%s * * * * %s # %s\n' "$1" "$SYNCLISTS" "$SYNC_TAG" >> "$_tmp"
  crontab "$_tmp" && rm -f "$_tmp" || { rm -f "$_tmp"; _nodes_unlock; printf '{"ok":false,"error":"crontab write failed"}\n'; return 1; }
  _nodes_unlock
  printf '%s SYNC cron set to */%s\n' "$(date '+%F %T')" "$1" >> "$LOG"
  printf '{"ok":true,"interval":"*/%s"}\n' "$1"
}

# event watchdog on/off
cmd_syncd_set() {
  case "$1" in on|off) : ;; *) printf '{"ok":false,"error":"bad arg"}\n'; return 1;; esac
  if [ "$1" = off ]; then
    /etc/init.d/geomask-syncd stop >/dev/null 2>&1; /etc/init.d/geomask-syncd disable >/dev/null 2>&1
  else
    /etc/init.d/geomask-syncd enable >/dev/null 2>&1; /etc/init.d/geomask-syncd restart >/dev/null 2>&1
  fi
  sleep 1
  _run=false; pgrep -f geomask-syncd.sh >/dev/null 2>&1 && _run=true
  if [ "$1" = on ]; then
    [ "$_run" = true ] || { printf '{"ok":false,"running":%s}\n' "$_run"; return 1; }
  else
    [ "$_run" = false ] || { printf '{"ok":false,"running":%s}\n' "$_run"; return 1; }
  fi
  printf '{"ok":true,"running":%s}\n' "$_run"
}

# mihomo restart pin on/off
cmd_pin_set() {
  case "$1" in on|off) : ;; *) printf '{"ok":false,"error":"bad arg"}\n'; return 1;; esac
  if [ "$1" = on ]; then
    printf 'restarts forbidden by owner - VPN must stay up\n' > /root/.geomask-norestart
    printf '%s PIN set (mihomo --restart disabled)\n' "$(date '+%F %T')" >> "$LOG"
  else
    rm -f /root/.geomask-norestart
    printf '%s PIN removed (restarts allowed; be careful)\n' "$(date '+%F %T')" >> "$LOG"
  fi
  _p=false; [ -e /root/.geomask-norestart ] && _p=true
  printf '{"ok":true,"pin":%s}\n' "$_p"
}


# v1.13.0: lists template status (bundled MagiTrickle groups snapshot vs current config)
cmd_lists_get() {
  _tpl=/etc/geomask/lists/magitrickle-groups.yaml
  _cfg=/etc/magitrickle/state/config.yaml
  _tp=false; [ -s "$_tpl" ] && _tp=true
  _tg=$(grep -c '^- id:' "$_tpl" 2>/dev/null); [ -n "$_tg" ] || _tg=0
  _tr=$(grep -c '^  - id:' "$_tpl" 2>/dev/null); [ -n "$_tr" ] || _tr=0
  _cg=$(grep -c '^- id:' "$_cfg" 2>/dev/null); [ -n "$_cg" ] || _cg=0
  _cr=$(grep -c '^  - id:' "$_cfg" 2>/dev/null); [ -n "$_cr" ] || _cr=0
  _run=false; pgrep -f magitrickled >/dev/null 2>&1 && _run=true
  printf '{"ok":true,"template_present":%s,"template_version":"%s","template_groups":%s,"template_rules":%s,"current_groups":%s,"current_rules":%s,"magitrickle_running":%s}\n' \
    "$_tp" "$BACKEND_VERSION" "$_tg" "$_tr" "$_cg" "$_cr" "$_run"
}

# v1.13.0: restore MagiTrickle groups from the template (backup config.yaml, splice, restart+sync)
cmd_lists_seed() {
  _tpl=/etc/geomask/lists/magitrickle-groups.yaml
  _py=/usr/bin/geomask-seed-lists.py
  [ -s "$_tpl" ] || { printf '{"ok":false,"error":"template missing"}\n'; return 1; }
  [ -f "$_py" ] || { printf '{"ok":false,"error":"seeder missing"}\n'; return 1; }
  _require_backup || return 1
  # The daemon owns config.yaml. A failed stop means the write would race it, so nothing is
  # written and the caller is told (audit 14.11.2026, F19).
  if ! /etc/init.d/magitrickle stop >/dev/null 2>&1; then
    printf '{"ok":false,"error":"cannot stop magitrickle - config not written"}\n'; return 1
  fi
  _w=0
  while [ "$_w" -lt 5 ] && pgrep -f magitrickled >/dev/null 2>&1; do _w=$((_w+1)); sleep 1; done
  if pgrep -f magitrickled >/dev/null 2>&1; then
    /etc/init.d/magitrickle start >/dev/null 2>&1 || true
    printf '{"ok":false,"error":"magitrickled is still running - config not written"}\n'; return 1
  fi
  _out=$(python3 "$_py" 2>&1); _rc=$?
  _started=true
  /etc/init.d/magitrickle start >/dev/null 2>&1 || _started=false
  sleep 2
  pgrep -f magitrickled >/dev/null 2>&1 || _started=false
  _synced=true
  sh /root/sync_mtrickle_mihomo.sh >/dev/null 2>&1 || _synced=false
  controller_curl -s -X PUT http://127.0.0.1:9090/configs -H 'Content-Type: application/json' \
    -d '{"path":"/etc/mihomo/config.yaml"}' >/dev/null 2>&1 || true
  _ok=false
  [ "$_rc" = 0 ] && [ "$_started" = true ] && [ "$_synced" = true ] && \
    printf '%s' "$_out" | grep -q '^CHANGED' && _ok=true
  printf '{"ok":%s,"rc":%s,"running":%s,"synced":%s,"result":"%s"}\n' \
    "$_ok" "$_rc" "$_started" "$_synced" "$(json_esc "$_out")"
  [ "$_ok" = true ]
}

# v1.14.0: youtube edge watchdog - status / mode switch / manual release
YT_WATCH=/usr/bin/geomask-ytwatch.sh
YT_CONF=/etc/geomask-ytwatch.conf

cmd_yt_get() {
  [ -x "$YT_WATCH" ] || { printf '{"ok":false,"error":"ytwatch not installed"}\n'; return 1; }
  _j=$(sh "$YT_WATCH" --status 2>/dev/null)
  case "$_j" in '{'*'}'*) : ;; *) _j='{"error":"no status"}' ;; esac
  _mode=$(sed -n 's/^mode=//p' "$YT_CONF" 2>/dev/null | head -1)
  printf '%s\n' "$_j" | sed "s#\"mode\":[^,]*#\"mode\":\"${_mode:-auto}\",\"ok\":true#"
}

cmd_yt_mode() {
  case "$1" in auto|blanket) : ;; *) printf '{"ok":false,"error":"bad mode"}\n'; return 1 ;; esac
  [ -x "$YT_WATCH" ] || { printf '{"ok":false,"error":"ytwatch not installed"}\n'; return 1; }
  _out=$(sh "$YT_WATCH" --mode "$1" 2>&1); _rc=$?
  printf '%s\n' "$_out" | tail -1
  return $_rc
}

cmd_yt_reset() {
  [ -x "$YT_WATCH" ] || { printf '{"ok":false,"error":"ytwatch not installed"}\n'; return 1; }
  sh "$YT_WATCH" --reset >/dev/null 2>&1
  printf '{"ok":true}\n'
}

# v1.15.0: torrent admin (tracker ruleset + DIRECT listener)
TORRENT_SH=/usr/bin/geomask-torrent.sh

_torrent_missing() { printf '{"ok":false,"error":"torrent admin not installed"}\n'; }

cmd_torrent_get() {
  [ -x "$TORRENT_SH" ] || { _torrent_missing; return 1; }
  sh "$TORRENT_SH" --status 2>/dev/null
}
cmd_torrent_refresh() {
  [ -x "$TORRENT_SH" ] || { _torrent_missing; return 1; }
  sh "$TORRENT_SH" --refresh 2>/dev/null | tail -1
}
cmd_torrent_listener() {
  case "$1" in on|off) : ;; *) printf '{"ok":false,"error":"bad state"}\n'; return 1 ;; esac
  [ -x "$TORRENT_SH" ] || { _torrent_missing; return 1; }
  sh "$TORRENT_SH" --listener "$1" 2>/dev/null | tail -1
}
cmd_torrent_extra() {
  [ -x "$TORRENT_SH" ] || { _torrent_missing; return 1; }
  sh "$TORRENT_SH" --extra "$1" 2>/dev/null | tail -1
}

# Independent, report-only service-egress audit. Never calls the legacy mutating scan.
NODE_AUDIT=${GM_NODE_AUDIT:-/usr/lib/geomask/geomask-node-audit.py}
NODE_APPLY=${GM_NODE_APPLY:-/usr/lib/geomask/geomask-apply.py}
cmd_nodes_paths_get() {
  python3 "$NODE_AUDIT" get
}
cmd_nodes_path_save() {
  python3 "$NODE_AUDIT" save "$1" "$2" "$3"
}
cmd_nodes_path_check() {
  case "$1" in vpn|vpn_nop2p|ai) : ;; *) printf '{"ok":false,"error":"invalid path"}\n'; return 1 ;; esac
  [ -f "$NODE_AUDIT" ] || { printf '{"ok":false,"error":"audit module not installed"}\n'; return 1; }
  # Module validates and reserves synchronously, then forks with the job lock.
  python3 "$NODE_AUDIT" start "$1"
}
cmd_nodes_audit_events_get() {
  python3 "$NODE_AUDIT" audit-events "$1" "$2" "$3"
}
cmd_nodes_audit_job_get() {
  python3 "$NODE_AUDIT" audit-job "$1"
}
cmd_nodes_audit_cancel() {
  python3 "$NODE_AUDIT" audit-cancel "$1"
}
cmd_nodes_path_preview() {
  [ -f "$NODE_APPLY" ] || { printf '{"ok":false,"error":"apply module not installed"}\n'; return 1; }
  python3 "$NODE_APPLY" preview "$1" "$2" "$3" "$4"
}
cmd_nodes_path_apply() {
  [ -f "$NODE_APPLY" ] || { printf '{"ok":false,"error":"apply module not installed"}\n'; return 1; }
  python3 "$NODE_APPLY" apply "$1" "$2" "$3"
}
cmd_nodes_path_apply_status() {
  [ -f "$NODE_APPLY" ] || { printf '{"ok":false,"error":"apply module not installed"}\n'; return 1; }
  python3 "$NODE_APPLY" status "$1"
}
cmd_nodes_path_revoke() {
  [ -f "$NODE_APPLY" ] || { printf '{"ok":false,"error":"apply module not installed"}\n'; return 1; }
  python3 "$NODE_APPLY" revoke "$1"
}
cmd_mtrickle_sync() {
  python3 /usr/lib/geomask/geomask-sync-youtube.py mtrickle-sync
}
cmd_nodes_audit_schedule_get() {
  python3 "$NODE_AUDIT" schedule-get "$1"
}
cmd_nodes_audit_schedule_set() {
  python3 "$NODE_AUDIT" schedule-set "$1" "$2" "$3" "$4"
}

# v1.16.0: Policy management (paths, filters, subscriptions, preview)
cmd_policy_get() {
  _pol=/etc/geomask/policies.yaml
  [ -f "$_pol" ] || { printf '{"ok":false,"error":"policy file not found"}\n'; return 1; }
  cat "$_pol"
}

cmd_policy_preview() {
  _py=/usr/lib/geomask/policy_preview.py
  [ -f "$_py" ] || { printf '{"ok":false,"error":"preview engine not installed"}\n'; return 1; }
  _path_id="$2"
  _nodes_json="$3"
  if [ -z "$_path_id" ]; then
    printf '{"ok":false,"error":"path_id required"}\n'; return 1
  fi
  if [ -z "$_nodes_json" ]; then
    _nodes_json='[]'
  fi
  python3 "$_py" "$_path_id" --nodes-json "$_nodes_json"
}

cmd_policy_apply() {
  _pol=/etc/geomask/policies.yaml
  [ -f "$_pol" ] || { printf '{"ok":false,"error":"policy file not found"}\n'; return 1; }
  _new_policy="$2"
  if [ -z "$_new_policy" ]; then
    printf '{"ok":false,"error":"policy_json required"}\n'; return 1
  fi
  # Validate JSON structure
  echo "$_new_policy" | python3 -m json.tool >/dev/null 2>&1 || { printf '{"ok":false,"error":"invalid JSON"}\n'; return 1; }
  # Backup current policy
  _bak="/etc/geomask/policies.yaml.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$_pol" "$_bak"
  # Write new policy
  printf '%s' "$_new_policy" > "$_pol"
  # Verify the new policy is valid
  python3 -c "import yaml, sys; yaml.safe_load(open('/etc/geomask/policies.yaml'))" 2>/dev/null || {
    mv "$_bak" "$_pol"
    printf '{"ok":false,"error":"invalid policy structure"}\n'; return 1
  }
  printf '{"ok":true,"backup":"%s"}\n' "$(basename "$_bak")"
}

# Strict DNS with an emergency reserve (owner decision 14.11.2026). The policy itself lives in
# one script so the panel, the installer and a shell call all do the same thing.
DNSGUARD=/usr/lib/geomask/geomask-dnsguard.sh
cmd_dns_guard() { # cmd_dns_guard <status|apply|revert>
  case "${1:-status}" in
    status|apply|revert) ;;
    *) printf '{"error":"bad arg"}\n'; return 1 ;;
  esac
  [ "$1" = status ] || _require_backup || return 1
  [ -x "$DNSGUARD" ] || { printf '{"ok":false,"error":"dns guard not installed (/usr/lib/geomask/geomask-dnsguard.sh)"}\n'; return 1; }
  "$DNSGUARD" "$1"
}

# ---------------------------------------------------------------- console lane (v1.31.0)
# A console needs ONE egress for the whole session: auth, game and UDP must leave through the
# same node, or the service drops the session mid-match. Domain rules cannot promise that —
# the same CDN/API hosts serve every device — so the console gets its own tunnel and listener,
# exactly like the AI lane. The egress is chosen by the listener, not by a rule.
#
# `off` is the kill switch the owner asked for: it removes the ip rule and stops the tunnel, so
# the console falls back to the normal path immediately. The mihomo side stays in place, so
# switching back on is instant and does not reload mihomo.
CONSOLE_STATE=${GM_CONSOLE_STATE:-/etc/geomask/console.state}
CONSOLE_TUN_DEV=Console
CONSOLE_TUN_IP=198.21.0.1
CONSOLE_SOCKS_PORT=7893
CONSOLE_TABLE=102
CONSOLE_RULE_PREF=19903
CONSOLE_YML=${GM_CONSOLE_YML:-/etc/hev-socks5-tunnel/console.yml}
CONSOLE_INITD=${GM_CONSOLE_INITD:-/etc/init.d/hev-console}
CONSOLE_NFT=${GM_CONSOLE_NFT:-/usr/share/nftables.d/ruleset-post/99-console-accept.nft}
CONSOLE_PY=/usr/lib/geomask/geomask-console.py
# The switch writes the live config through a transaction (preimage, mihomo -t, isolated
# trial, atomic write, reload, rollback), never through a hand-written `cp`/`mv` pair.
TX_PY=${GM_TX:-/usr/lib/geomask/geomask-config-tx.py}
# One lock for `on`, `off` and the guard's console block: a rule must not come back after the
# kill switch removed it. `status` never takes it - the panel calls it on every render.
# The primitive is the shared kernel lock (geomask-lock.sh, console rank): a killed switch is
# released by the kernel, the lock file is never deleted and there is no PID to guess. The
# emergency path waits a bounded number of seconds and then fails; it never works unserialised.
CONSOLE_LOCK=${GM_CONSOLE_LOCK:-/tmp/geomask-locks/console.lock}

_console_get() { sed -n "s/^$1=//p" "$CONSOLE_STATE" 2>/dev/null | head -1; }

_console_lock() {
  command -v gm_lock_acquire >/dev/null 2>&1 || {
    echo "console: lock helper missing ($GM_LOCK_LIB) - refusing to switch the lane" >&2
    return 1
  }
  gm_lock_acquire console "${CONSOLE_LOCK_TRIES:-10}" "$CONSOLE_LOCK"
}

# Releases only the lock this process holds. The lock file stays: deleting or recreating it
# would give two writers two different inodes (the historical reclaim bug, R04).
_console_unlock() {
  command -v gm_lock_release >/dev/null 2>&1 || return 0
  gm_lock_release console
  return 0
}

_console_set() { # _console_set <key> <value>
  mkdir -p "$(dirname "$CONSOLE_STATE")" 2>/dev/null || return 1
  _ct_tmp="$CONSOLE_STATE.tmp.$$"
  if [ -f "$CONSOLE_STATE" ]; then
    grep -v "^$1=" "$CONSOLE_STATE" > "$_ct_tmp" 2>/dev/null || : > "$_ct_tmp"
  else
    : > "$_ct_tmp"
  fi
  printf '%s=%s\n' "$1" "$2" >> "$_ct_tmp" 2>/dev/null || { rm -f "$_ct_tmp"; return 1; }
  chmod 600 "$_ct_tmp" 2>/dev/null
  mv -f "$_ct_tmp" "$CONSOLE_STATE" 2>/dev/null
}

_console_tun_ensure() {
  mkdir -p /etc/hev-socks5-tunnel 2>/dev/null
  if [ ! -f "$CONSOLE_YML" ]; then
    {
      printf 'tunnel:\n'
      printf '  name: %s\n' "$CONSOLE_TUN_DEV"
      printf '  mtu: 8500\n  multi-queue: false\n'
      printf '  ipv4: %s\n' "$CONSOLE_TUN_IP"
      printf 'socks5:\n  port: %s\n  address: 127.0.0.1\n  udp: %s\n' \
        "$CONSOLE_SOCKS_PORT" "'udp'"
    } > "$CONSOLE_YML" || return 1
  fi
  if [ ! -x "$CONSOLE_INITD" ]; then
    {
      printf '#!/bin/sh /etc/rc.common\n'
      printf 'USE_PROCD=1\nSTART=99\n'
      printf 'start_service() {\n  procd_open_instance\n'
      printf '  procd_set_param command /usr/bin/hev-socks5-tunnel %s\n' "$CONSOLE_YML"
      printf '  procd_set_param file %s\n' "$CONSOLE_YML"
      printf '  procd_set_param limits core="unlimited"\n'
      printf '  procd_set_param limits nofile="1000000 1000000"\n'
      printf '  procd_set_param stdout 1\n  procd_set_param stderr 1\n'
      printf '  procd_set_param respawn\n  procd_close_instance\n}\n'
    } > "$CONSOLE_INITD" || return 1
    chmod 755 "$CONSOLE_INITD" 2>/dev/null
  fi
  "$CONSOLE_INITD" enable >/dev/null 2>&1
  # The tunnel is a procd service: run the init script without the lock descriptor, or the
  # daemon inherits it and holds the console lock until reboot.
  if command -v gm_lock_safe_exec >/dev/null 2>&1; then
    gm_lock_safe_exec "$CONSOLE_INITD" start >/dev/null 2>&1
  else
    "$CONSOLE_INITD" start >/dev/null 2>&1
  fi
  _i=0
  while [ "$_i" -lt 10 ] && ! ip link show "$CONSOLE_TUN_DEV" >/dev/null 2>&1; do
    sleep 1; _i=$((_i+1))
  done
  ip link show "$CONSOLE_TUN_DEV" >/dev/null 2>&1
}

_console_fw_ensure() {
  uci -q get network.Console >/dev/null 2>&1 || {
    uci set network.Console=interface
    uci set network.Console.proto='none'
    uci set network.Console.device="$CONSOLE_TUN_DEV"
    uci commit network
  }
  if ! uci -q show firewall 2>/dev/null | grep -q "name='Console'"; then
    _zn=''
    for _s in $(uci -q show firewall 2>/dev/null | sed -n 's/^\(firewall\.[^.]*\)=zone$/\1/p'); do
      [ "$(uci -q get "$_s.name")" = "Mihomo" ] && { _zn="$_s"; break; }
    done
    [ -n "$_zn" ] || return 1
    _new=$(uci add firewall zone)
    uci set "firewall.$_new.name=Console"
    for _o in input output forward masq mtu_fix; do
      uci set "firewall.$_new.$_o=$(uci -q get "$_zn.$_o")"
    done
    uci set "firewall.$_new.network=Console"
    for _f in $(uci -q show firewall 2>/dev/null | sed -n 's/^\(firewall\.[^.]*\)=forwarding$/\1/p'); do
      _src=$(uci -q get "$_f.src"); _dst=$(uci -q get "$_f.dest")
      if [ "$_src" = "lan" ] && [ "$_dst" = "Mihomo" ]; then
        _nf=$(uci add firewall forwarding); uci set "firewall.$_nf.src=lan"; uci set "firewall.$_nf.dest=Console"
      fi
      if [ "$_src" = "Mihomo" ] && [ "$_dst" = "lan" ]; then
        _nf=$(uci add firewall forwarding); uci set "firewall.$_nf.src=Console"; uci set "firewall.$_nf.dest=lan"
      fi
    done
    uci commit firewall
  fi
  # The TUN zone is a /32, so fw4 generates no accept for lan->Console: without this rule the
  # console's packets are dropped by the firewall even though routing is correct.
  _devs=$(_gm_lan_devices)
  _names=$(_gm_nft_names "$_devs")
  mkdir -p "$(dirname "$CONSOLE_NFT")" 2>/dev/null
  {
    printf '# geomask-lan: %s\n' "$_devs"
    printf 'table inet fw4 {\n    chain accept_to_Console {\n'
    [ -n "$_names" ] && printf '        iifname { %s } oifname "Console" counter accept comment "geomask-console: LAN to console tunnel"\n' "$_names"
    printf '        iifname "Wireguard" oifname "Console" counter accept comment "geomask-console: WG to console tunnel"\n'
    printf '    }\n}\n'
  } > "$CONSOLE_NFT" 2>/dev/null
  /etc/init.d/firewall reload >/dev/null 2>&1
}

_console_rule_on() { # _console_rule_on <ip>
  _gm_sync_routes "$CONSOLE_TABLE" || return 1
  ip route replace table "$CONSOLE_TABLE" default dev "$CONSOLE_TUN_DEV" 2>/dev/null || return 1
  while ip rule del from "$1" lookup "$CONSOLE_TABLE" 2>/dev/null; do :; done
  ip rule add pref "$CONSOLE_RULE_PREF" from "$1" lookup "$CONSOLE_TABLE" 2>/dev/null
}

_console_rule_off() { # remove only OUR rules: `from <ipv4> lookup 102`, never a bare pref
  _tmp="${TMPDIR:-/tmp}/geomask-console-rules.$$"
  ip rule show 2>/dev/null | grep -E 'lookup 102$' > "$_tmp" 2>/dev/null
  _had=0
  while IFS= read -r _line; do
    # `lookup 102` also matches tables 1020/1021 with a loose pattern, and deleting by
    # priority alone can drop somebody else's rule that happens to share the number.
    _from=$(printf '%s\n' "$_line" | sed -n 's/.* from \([0-9][0-9.]*\) lookup 102$/\1/p')
    [ -n "$_from" ] || continue
    ip rule del from "$_from" lookup 102 2>/dev/null && _had=1
  done < "$_tmp"
  rm -f "$_tmp" 2>/dev/null
  [ -n "${1:-}" ] && while ip rule del from "$1" lookup "$CONSOLE_TABLE" 2>/dev/null; do _had=1; done
  if [ "$_had" = 1 ] && [ -n "${1:-}" ]; then
    conntrack -D -s "$1" >/dev/null 2>&1
    conntrack -D -d "$1" >/dev/null 2>&1
  fi
  # The caller decides what to report: an unremoved rule is a failure, not a silent "ok".
  ip rule show 2>/dev/null | grep -qE 'lookup 102$' && return 1
  return 0
}

# Undo the console patch: put the pre-switch bytes back through the SAME transaction the switch
# used, then prove the live file is usable again. A restore that leaves mihomo unable to load
# the file, or the controller not answering, is not a restore.
#   _console_cfg_restore [preimage] -> rc 0 only when the live config is byte-identical to the
#   preimage, `mihomo -t` accepts it and the controller answers /version.
_console_cfg_restore() {
  _pre="${1:-${_orig:-}}"
  [ -n "$_pre" ] && [ -f "$_pre" ] || return 1
  # The CLI can refuse (lane busy, disk full, trial failed): its own rollback may already have
  # put the bytes back, so the byte comparison below decides, not its exit code.
  "$PYTHON_BIN" -B "$TX_PY" --commit "$MIHOMO_CONF" < "$_pre" >/dev/null 2>&1 || :
  cmp -s "$MIHOMO_CONF" "$_pre" || return 1
  if command -v mihomo >/dev/null 2>&1; then
    mihomo -d /etc/mihomo -f "$MIHOMO_CONF" -t >/dev/null 2>&1 || return 1
  fi
  _ver=$(controller_curl -s -o /dev/null -w '%{http_code}' -m10 "$MIHOMO_API/version" 2>/dev/null)
  case "$_ver" in 200|204) return 0 ;; *) return 1 ;; esac
}

# A failed switch: name the step, put the pre-switch config back and say honestly whether the
# live file really is the old one again. The preimage is kept only when the restore failed -
# then it is the only working copy left, and the answer has to say where it is.
_console_on_fail() { # _console_on_fail <step> <message>
  _restored=false
  if _console_cfg_restore "$_orig"; then
    _restored=true
    rm -f "$_orig" 2>/dev/null
  fi
  rm -f "$_cand" 2>/dev/null
  # Every failure path ends here; none of them may return with the lane lock still held.
  _console_unlock
  if [ "$_restored" = true ]; then
    printf '{"ok":false,"step":"%s","restored":true,"error":"%s - previous config restored"}\n' "$1" "$2"
  else
    # Not verified is not the same as "left patched": the transaction rolls back on its own, so
    # the bytes may already be the old ones while mihomo or the controller still refuses to
    # confirm it. Say what is certain and where the working copy is.
    printf '{"ok":false,"step":"%s","restored":false,"error":"%s - RESTORE NOT VERIFIED (mihomo -t or the controller did not confirm it); the live config may still be the patched one, the pre-switch copy is kept at %s"}\n' "$1" "$2" "$_orig"
  fi
  _console_unlock
  return 1
}

cmd_console() { # cmd_console <on|off|status> [ip] [sub-id] [countries]
  _act="$1"
  case "$_act" in
    on|off|status) ;;
    *) printf '{"error":"bad arg"}'; return 1 ;;
  esac
  _ip="${2:-$(_console_get ip)}"
  # The caller's fields are authoritative, EMPTY INCLUDED: that is how the panel asks for "no
  # country filter" and "subscription like the VPN again". The old `${4:-saved}` fallback read
  # an empty field as "keep the previous value", so neither reset was possible.
  _countries="${4-}"
  _sub="${3-}"
  if [ "$_act" = status ]; then
    # `status` reports what is stored instead of what the caller sent.
    _countries=$(_console_get countries)
    _sub=$(_console_get sub)
    _rule=0
    [ -n "$_ip" ] && ip rule show 2>/dev/null | grep -q "from $_ip lookup $CONSOLE_TABLE" && _rule=1
    _tun=0
    ip link show "$CONSOLE_TUN_DEV" >/dev/null 2>&1 && _tun=1
    _cfg='{}'
    [ -x "$CONSOLE_PY" ] && [ -f "$MIHOMO_CONF" ] && \
      _cfg=$("$PYTHON_BIN" -B "$CONSOLE_PY" status "$MIHOMO_CONF" 2>/dev/null)
    [ -n "$_cfg" ] || _cfg='{}'
    # `sub` is the registry id the panel restores into its field; empty means "as the VPN".
    printf '{"ok":true,"enabled":%s,"ip":"%s","sub":"%s","countries":"%s","rule":%s,"tun":%s,"config":%s}\n' \
      "$([ "$(_console_get enabled)" = 1 ] && echo true || echo false)" \
      "$_ip" "$_sub" "$_countries" "$_rule" "$_tun" "$_cfg"
    return 0
  fi
  # Serialise the lane before anything is changed: `off` must not interleave with a switch,
  # and the guard takes the same lock before it re-reads the state. The emergency path waits
  # for the lock (bounded), but never for a fresh backup.
  _console_lock || {
    printf '{"ok":false,"error":"console is busy: another switch holds the lane lock (%s)"}\n' "$CONSOLE_LOCK"
    return 1
  }
  if [ "$_act" = off ]; then
    # The kill switch must work when the router is already half-broken: no backup gate and no
    # silent success. State goes first (the watchdog reads it), then the service and the rules,
    # and every step is checked.
    _console_set enabled 0 || {
      _console_unlock
      printf '{"ok":false,"error":"cannot update the console state file"}\n'; return 1; }
    _rc=0
    if [ -x "$CONSOLE_INITD" ]; then
      "$CONSOLE_INITD" stop >/dev/null 2>&1 || _rc=1
      "$CONSOLE_INITD" disable >/dev/null 2>&1 || _rc=1
    fi
    _console_rule_off "$_ip" || _rc=1
    _console_rule_off "" || _rc=1
    _console_unlock
    if [ "$_rc" != 0 ]; then
      printf '{"ok":false,"enabled":false,"error":"console is marked off, but a rule or the tunnel did not stop - check ip rule and hev-console"}\n'
      return 1
    fi
    echo "$(date +%FT%T) CONSOLE off (ip ${_ip:-none}) - console back on the normal path" >> "$LOG"
    printf '{"ok":true,"enabled":false}\n'
    return 0
  fi
  _require_backup || { _console_unlock; return 1; }
  # --- on ---
  _ipv4_ok "$_ip" || { _console_unlock; printf '{"ok":false,"error":"bad or missing console IP"}\n'; return 1; }
  # A device already managed as ai/bypass gets its own rule at pref 19900, which outranks the
  # console rule (19903): the panel would say "on" while the traffic goes somewhere else.
  if [ -f "$DEVICES" ] && awk -v i="$_ip" '$1==i {found=1} END{exit found?0:1}' "$DEVICES" 2>/dev/null; then
    _console_unlock
    printf '{"ok":false,"error":"this IP is already a managed device (ai/bypass) - its rule would win; remove it from the device list first"}\n'
    return 1
  fi
  if [ ! -x "$CONSOLE_PY" ]; then
    _console_unlock
    printf '{"ok":false,"error":"console patcher not installed (/usr/lib/geomask/geomask-console.py)"}\n'; return 1
  fi
  _url=''
  if [ -n "${3:-}" ]; then
    _url=$(_find_sub_url "$3") || {
      _console_unlock
      printf '{"ok":false,"error":"subscription %s is not in the registry"}\n' "$3"; return 1
    }
  fi
  # Patch a CANDIDATE, validate it, and only then replace the live file: the first version
  # wrote the live config first and checked afterwards, so a rejected config was already on
  # disk and the next mihomo restart would fail.
  _cand="${TMPDIR:-/tmp}/geomask-console-candidate.$$.yaml"
  _orig="${TMPDIR:-/tmp}/geomask-console-orig.$$"
  cp -p "$MIHOMO_CONF" "$_cand" 2>/dev/null || {
    _console_unlock
    printf '{"ok":false,"error":"cannot read the mihomo config"}\n'; return 1
  }
  # The candidate is patched from THIS copy, so its digest is the base the transaction must
  # still find live at commit time. Without it a concurrent mtrickle_sync/apply commit would
  # be silently erased by a candidate built before it (report R06): the transaction refuses
  # with "changed since read" instead.
  _base_sha=$("$PYTHON_BIN" -B -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
    "$_cand" 2>/dev/null)
  [ -n "$_base_sha" ] || {
    rm -f "$_cand"; _console_unlock
    printf '{"ok":false,"error":"cannot fingerprint the mihomo config - live config untouched"}\n'; return 1
  }
  if [ -n "$_url" ]; then
    _res=$(printf '%s\n' "$_url" | "$PYTHON_BIN" -B "$CONSOLE_PY" apply "$_cand" --url - --countries "$_countries" 2>&1)
  else
    # No subscription chosen = "as the VPN": the patcher re-derives the URL from the VPN group
    # instead of keeping whatever the provider was pinned to earlier.
    _res=$("$PYTHON_BIN" -B "$CONSOLE_PY" apply "$_cand" --url default --countries "$_countries" 2>&1)
  fi
  case "$_res" in
    *'"ok": true'*|*'"ok":true'*) ;;
    *) rm -f "$_cand"
       _console_unlock
       printf '{"ok":false,"error":"mihomo config not patched: %s"}\n' "$(printf '%s' "$_res" | tail -1)"; return 1 ;;
  esac
  # A pre-check only: the transaction repeats the validation shaped for the trial, so this is
  # not duplicated work, it is the cheap "do not even start" gate.
  if command -v mihomo >/dev/null 2>&1; then
    mihomo -d /etc/mihomo -f "$_cand" -t >/dev/null 2>&1 || {
      rm -f "$_cand"
      _console_unlock
      printf '{"ok":false,"error":"mihomo -t rejected the patched config - live config untouched"}\n'; return 1
    }
  fi
  # Private copy of the live bytes. The transaction keeps its own preimage, but the rollback
  # below runs in this shell and must not depend on the tx work dir surviving. The old backup
  # copy is gone: it lived forever and no transaction owned it.
  ( umask 077; cp -p "$MIHOMO_CONF" "$_orig" ) 2>/dev/null || {
    rm -f "$_cand"
    _console_unlock
    printf '{"ok":false,"error":"cannot keep a private copy of the mihomo config - live config untouched"}\n'
    return 1
  }
  chmod 600 "$_orig" 2>/dev/null
  # The switch itself: preimage, mihomo -t, isolated trial, atomic write, reload and a rollback
  # if any of it fails. Nothing here changes the live file by hand.
  _txout=$("$PYTHON_BIN" -B "$TX_PY" --commit "$MIHOMO_CONF" --expect-sha256 "$_base_sha" < "$_cand" 2>&1)
  _txrc=$?
  if [ "$_txrc" != 0 ]; then
    _detail=$(printf '%s' "$_txout" | tail -1 | sed 's/["\\]//g' | cut -c1-300)
    _console_on_fail transaction "mihomo config transaction failed: ${_detail:-no output}"
    return 1
  fi
  # A new listener exists only after mihomo re-reads the file: force a reload and prove the
  # listener is really listening before any traffic is pointed at it.
  _code=$(controller_curl -s -o /dev/null -w '%{http_code}' -m15 -X PUT "$MIHOMO_API/configs?force=true" \
    -H 'Content-Type: application/json' -d "{\"path\":\"$MIHOMO_CONF\"}" 2>/dev/null)
  case "$_code" in
    200|204) ;;
    *) _console_on_fail reload "mihomo reload failed (HTTP ${_code:-none})"; return 1 ;;
  esac
  _i=0; _lst=0
  while [ "$_i" -lt 5 ]; do
    netstat -lnt 2>/dev/null | grep -q ":$CONSOLE_SOCKS_PORT " && { _lst=1; break; }
    sleep 1; _i=$((_i+1))
  done
  [ "$_lst" = 1 ] || {
    _console_on_fail listener "console listener did not appear on :$CONSOLE_SOCKS_PORT"; return 1
  }
  _console_tun_ensure || { _console_on_fail tun "console tunnel did not come up"; return 1; }
  _console_fw_ensure || { _console_on_fail firewall "console firewall zone not created"; return 1; }
  _console_rule_on "$_ip" || { _console_on_fail rule "console route rule not installed"; return 1; }
  rm -f "$_orig" "$_cand" 2>/dev/null
  # enabled is the COMMIT MARKER: written last, so a crash never leaves "on" without an address
  # for the watchdog and the kill switch to work with. `countries` and `sub` are written ALWAYS,
  # empty string included: an empty field is the reset the panel offers.
  _console_set countries "$_countries"
  _console_set sub "$_sub"
  _console_set ip "$_ip"
  _console_set enabled 1
  _console_unlock
  echo "$(date +%FT%T) CONSOLE on ip=$_ip countries=${_countries:-any} sub=${_sub:-vpn}" >> "$LOG"
  printf '{"ok":true,"enabled":true,"ip":"%s","sub":"%s","countries":"%s"}\n' "$_ip" "$_sub" "$_countries"
}

# Read-only twin of `cmd_console status`, registered in tools/geo-rpc.json as `console_status`
# with acl "read": the panel needs the state on every render, and a write-granted method there
# would let any panel reader change the lane.
cmd_console_status() { cmd_console status; }

# uci keeps the real credentials: WireGuard private_key, PPPoE password, provider tokens.
# `uci show network` and `uci show firewall` went into the bundle untouched, so the archive
# carried the owner's keys. The redactor is a shipped asset with the real `uci show` grammar
# (plus mihomo YAML, controller JSON and log text), so the masking is testable off-router.
DIAG_PY=${GM_DIAG_PY:-/usr/lib/geomask/geomask-diag-redact.py}
# R07: the bundle and its sidecar live in one private spool (0700, owned by us) instead of
# world-writable /tmp, and every read goes through the shipped helper - never through
# `wc`/`base64`, which follow symlinks.
DIAG_SPOOL=${GM_DIAG_SPOOL:-/tmp/geomask-diag}
DIAG_SPOOL_PY=${GM_DIAG_SPOOL_PY:-/usr/lib/geomask/geomask-diag-spool.py}

_console_diag_fail() { # $1 = stage that failed
  rm -rf "$_dir" 2>/dev/null
  printf '{"ok":false,"error":"redaction failed: %s"}\n' "$1"
  return 1
}

_console_diag_redact() { # $1 = uci|yaml|json|text|nft, $2 = stage, $3 = output file, stdin
  "$PYTHON_BIN" -B "$DIAG_PY" redact "$1" > "$3" 2>/dev/null
  _cdr=$?
  [ "$_cdr" = 0 ] || { _console_diag_fail "$2"; return 1; }
  return 0
}

# One archive with everything needed to diagnose the console path from a distance. The
# masking is done by the shipped redactor, and `verify` is the last gate: an unmasked bundle
# is never handed out (review 15.11, R02).
cmd_console_diag() {
  # R07: the archive lives in a private spool, never in world-writable /tmp. `init` fails
  # closed (must be a real directory, ours, 0700) and its JSON refusal is the answer.
  [ -f "$DIAG_SPOOL_PY" ] || {
    printf '{"ok":false,"error":"spool is not private: helper missing"}\n'; return 1
  }
  _diag_init=$("$PYTHON_BIN" -B "$DIAG_SPOOL_PY" init "$DIAG_SPOOL" 2>/dev/null)
  _diag_init_rc=$?
  if [ "$_diag_init_rc" != 0 ]; then
    case "$_diag_init" in
      '{"ok":false'*) printf '%s\n' "$_diag_init" ;;
      *) printf '{"ok":false,"error":"spool is not private: init failed"}\n' ;;
    esac
    return 1
  fi
  # One private directory per request: the fixed path plus `rm -rf` let two panel clicks delete
  # each other's half-built bundle (review 15.11, points 1 and 33).
  _tmpd=${TMPDIR:-/tmp}
  _dir=$(mktemp -d "$_tmpd/geomask-console-diag.XXXXXX" 2>/dev/null)
  [ -n "$_dir" ] || { printf '{"ok":false,"error":"cannot create a private bundle directory"}\n'; return 1; }
  chmod 700 "$_dir" 2>/dev/null
  # Nothing older than the retention window stays on the router: these archives carry the
  # masked-but-still-descriptive state of the console lane. The sidecar goes with the archive
  # or a missing archive would still look registered.
  find "$DIAG_SPOOL" -maxdepth 1 -name 'geomask-console-diag-*.tar.gz' \
    -mmin +"$((CONSOLE_DIAG_KEEP_H * 60))" -exec rm -f {} \; 2>/dev/null
  find "$DIAG_SPOOL" -maxdepth 1 -name 'geomask-console-diag-*.tar.gz.json' \
    -mmin +"$((CONSOLE_DIAG_KEEP_H * 60))" -exec rm -f {} \; 2>/dev/null
  # Without the redactor the bundle cannot be masked; an unmasked archive is worse than no
  # archive at all, so the request fails closed.
  [ -f "$DIAG_PY" ] || {
    rm -rf "$_dir" 2>/dev/null
    printf '{"ok":false,"error":"redaction failed: redactor missing"}\n'
    return 1
  }
  _ip=$(_console_get ip)
  {
    printf 'geomask console diag %s\n' "$(date '+%F %T %z')"
    printf 'backend %s\n' "$BACKEND_VERSION"
  } > "$_dir/summary.txt" 2>/dev/null
  [ -f /etc/geomask/version ] && cat /etc/geomask/version >> "$_dir/summary.txt" 2>&1
  ip -4 rule show > "$_dir/ip-rule.txt" 2>&1
  ip -4 route show table "$CONSOLE_TABLE" > "$_dir/route-table-$CONSOLE_TABLE.txt" 2>&1
  ip -4 addr show "$CONSOLE_TUN_DEV" > "$_dir/tun.txt" 2>&1
  uci -q show firewall 2>&1 | _console_diag_redact uci uci:firewall "$_dir/firewall.uci" || return 1
  uci -q show network 2>&1 | _console_diag_redact uci uci:network "$_dir/network.uci" || return 1
  nft list ruleset 2>&1 | _console_diag_redact nft nft:ruleset "$_dir/nft.txt" || return 1
  [ -n "$_ip" ] && conntrack -L -s "$_ip" > "$_dir/conntrack-console.txt" 2>&1
  logread 2>/dev/null | grep -iE 'mihomo|hev|geomask|console' | tail -400 \
    | _console_diag_redact text text:logread "$_dir/logread.txt" || return 1
  if [ -x "$CONSOLE_PY" ] && [ -f "$MIHOMO_CONF" ]; then
    "$PYTHON_BIN" -B "$CONSOLE_PY" status "$MIHOMO_CONF" 2>/dev/null \
      | _console_diag_redact json json:console-config "$_dir/console-config.json" || return 1
  fi
  controller_curl -s -m5 "$MIHOMO_API/proxies/CONSOLE" 2>/dev/null \
    | _console_diag_redact json json:group-CONSOLE "$_dir/group-CONSOLE.json" || return 1
  controller_curl -s -m5 "$MIHOMO_API/connections" 2>/dev/null \
    | _console_diag_redact json json:connections "$_dir/connections.json" || return 1
  # The mihomo config carries subscription URLs and the controller secret.
  if [ -f "$MIHOMO_CONF" ]; then
    _console_diag_redact yaml yaml:mihomo "$_dir/config.masked.yaml" < "$MIHOMO_CONF" || return 1
  fi
  # Last gate: unknown content must not be packed. `verify` prints file:line: kind.
  _diag_secret=$("$PYTHON_BIN" -B "$DIAG_PY" verify "$_dir" 2>&1)
  _diag_rc=$?
  if [ "$_diag_rc" != 0 ]; then
    _diag_detail=$(printf '%s\n' "$_diag_secret" | head -1)
    rm -rf "$_dir" 2>/dev/null
    printf '{"ok":false,"error":"bundle contains a secret (%s) - nothing was packed"}\n' \
      "$(json_esc "${_diag_detail:-verification failed}")"
    return 1
  fi
  _tgz="$DIAG_SPOOL/geomask-console-diag-$(date +%Y%m%d-%H%M%S).$$.tar.gz"
  ( umask 077; tar -czf "$_tgz" -C "$_tmpd" "$(basename "$_dir")" >/dev/null 2>&1 ) || {
    rm -rf "$_dir" 2>/dev/null
    rm -f "$_tgz" 2>/dev/null
    printf '{"ok":false,"error":"cannot pack the bundle"}\n'; return 1
  }
  chmod 600 "$_tgz" 2>/dev/null
  rm -rf "$_dir" 2>/dev/null
  # Registration records (dev, ino, link count, size, sha256) in <archive>.json; a failure
  # removes the archive so an unregistered bundle cannot linger in the spool.
  # Fixed creation/registration cap: 2 MiB, checked before SMB copy. Larger archives
  # are deleted, not left for SMB/SSH. GM_CONSOLE_DIAG_MAX only controls transport.
  _diag_reg=$("$PYTHON_BIN" -B "$DIAG_SPOOL_PY" register "$DIAG_SPOOL" "$_tgz" 2>/dev/null)
  _diag_reg_rc=$?
  if [ "$_diag_reg_rc" != 0 ]; then
    rm -f "$_tgz" 2>/dev/null
    case "$_diag_reg" in
      '{"ok":false'*) printf '%s\n' "$_diag_reg" ;;
      *) printf '{"ok":false,"error":"cannot register the bundle"}\n' ;;
    esac
    return 1
  fi
  # The helper measured the archive from its descriptor; the shell must not read it again.
  _diag_bytes=$(printf '%s\n' "$_diag_reg" | sed -n 's/.*"bytes":\([0-9][0-9]*\).*/\1/p')
  case "$_diag_bytes" in ''|*[!0-9]*) _diag_bytes=0 ;; esac
  # Into the share only when the owner opted in with the marker: before this the archive was
  # copied into a folder that may be published over SMB, without asking (review 15.11, #14).
  _shared=false
  if [ -f "$SMB_SHARE_ENABLE" ] && [ -d "$BACKUP_DIR" ] && [ -w "$BACKUP_DIR" ]; then
    cp -f "$_tgz" "$BACKUP_DIR/$(basename "$_tgz")" 2>/dev/null \
      && chmod 600 "$BACKUP_DIR/$(basename "$_tgz")" 2>/dev/null && _shared=true
  fi
  printf '{"ok":true,"file":"%s","bytes":%s,"shared":%s}\n' \
    "$_tgz" "$_diag_bytes" "$_shared"
}

# The panel used to get only a path on the router, so the owner had to find the archive by hand
# over SMB or SSH - and the file sat in /tmp with no way back (review 15.11, #15). The bytes now
# travel through the same authenticated ubus call as the rest of the panel: no world-readable
# URL, no published path. R07: the download path does not reopen the archive in shell - the
# helper refuses a symlink, a hard link, a swapped inode and a modified body, then base64s the
# bytes it hashed from one descriptor. GM_CONSOLE_DIAG_MAX is the configurable transport cap;
# it does not raise the fixed 2 MiB creation cap. Only successfully registered archives can
# remain in the spool or receive an opted-in SMB copy.
cmd_console_diag_get() {
  [ -f "$DIAG_SPOOL_PY" ] || {
    printf '{"ok":false,"error":"spool helper missing"}\n'; return 1
  }
  "$PYTHON_BIN" -B "$DIAG_SPOOL_PY" get "$DIAG_SPOOL" "${1:-}" \
    --max "${GM_CONSOLE_DIAG_MAX:-2097152}" --ttl "${CONSOLE_DIAG_KEEP_H:-24}"
  return $?
}

case "$1" in
  # >>> geo-rpc generated:dispatch (tools/geo-rpc.json) >>>
  status)                   cmd_status ;;
  leases)                   cmd_leases ;;
  log)                      cmd_log ;;
  backups)                  cmd_backups ;;
  stack)                    cmd_stack ;;
  nodescan)   # kick the node audit in background (returns immediately; panel re-reads stack)
    ( /usr/bin/geomask-node-scan.sh >/dev/null 2>&1 & )
    printf '{"ok":true,"started":true}\n' ;;
  nodes_get)                cmd_nodes_get ;;
  nodes_paths_get)          cmd_nodes_paths_get ;;
  nodes_path_save)          cmd_nodes_path_save "$2" "$3" "$4" ;;
  nodes_path_check)         cmd_nodes_path_check "$2" ;;
  nodes_audit_events_get)   cmd_nodes_audit_events_get "$2" "$3" "$4" ;;
  nodes_audit_job_get)      cmd_nodes_audit_job_get "$2" ;;
  nodes_audit_cancel)       cmd_nodes_audit_cancel "$2" ;;
  nodes_set)                cmd_nodes_set "$2" "$3" "$4" "$5" "$6" "$7" ;;
  sync_set)                 cmd_sync_set "$2" ;;
  syncd_set)                cmd_syncd_set "$2" ;;
  pin_set)                  cmd_pin_set "$2" ;;
  lists_get)                cmd_lists_get ;;
  lists_seed)               cmd_lists_seed ;;
  yt_get)                   cmd_yt_get ;;
  yt_mode)                  cmd_yt_mode "$2" ;;
  yt_reset)                 cmd_yt_reset ;;
  torrent_get)              cmd_torrent_get ;;
  torrent_refresh)          cmd_torrent_refresh ;;
  torrent_listener)         cmd_torrent_listener "$2" ;;
  torrent_extra)            cmd_torrent_extra "$2" ;;
  sync_lists)               cmd_sync_lists ;;
  aidns)                    cmd_aidns_apply ;;
  google)                   cmd_google ;;
  global)                   cmd_global "$2" ;;
  ipv6)                     cmd_ipv6 "$2" ;;
  dns_guard)                cmd_dns_guard "$2" ;;
  console)                  cmd_console "$2" "$3" "$4" "$5" ;;
  console_status)           cmd_console_status ;;
  console_diag)             cmd_console_diag "$2" ;;
  console_diag_get)         cmd_console_diag_get "$2" ;;
  dev_add)                  cmd_dev_add "$2" "$3" "$4" "$5" ;;
  dev_del)                  cmd_dev_del "$2" ;;
  ai_sub_get)               cmd_ai_sub_get ;;
  ai_sub_set)               cmd_ai_sub_set "$2" ;;
  subs_list)                cmd_subs_list ;;
  subs_seed)                cmd_subs_seed ;;
  sub_add)                  cmd_sub_add "$2" "$3" ;;
  sub_del)                  cmd_sub_del "$2" ;;
  ai_sub_use)               cmd_ai_sub_use "$2" ;;
  vpn_sub_use)              cmd_vpn_sub_use "$2" ;;
  vpnnp_sub_use)            cmd_vpnnp_sub_use "$2" ;;
  mod_list)                 cmd_mod_list ;;
  mod_check)                cmd_mod_check ;;
  mod_update)   # install one module's files from modules/index.json
    cmd_mod_update "$2" "$3" ;;
  diag_list)                cmd_diag_list ;;
  diag_run)                 cmd_diag_run "$2" ;;
  diag_log)                 cmd_diag_log "$2" ;;
  diag_domain)              cmd_diag_domain "$2" "$3" ;;
  upd_check)                cmd_upd_check "$2" ;;
  upd_status)               cmd_upd_status ;;
  upd_apply)                cmd_upd_apply ;;
  nodes_path_preview)       cmd_nodes_path_preview "$2" "$3" "$4" "$5" ;;
  nodes_path_apply)         cmd_nodes_path_apply "$2" "$3" "$4" ;;
  nodes_path_revoke)        cmd_nodes_path_revoke "$2" ;;
  nodes_path_apply_status)  cmd_nodes_path_apply_status "$2" ;;
  mtrickle_sync)            cmd_mtrickle_sync ;;
  nodes_audit_schedule_get) cmd_nodes_audit_schedule_get "$2" ;;
  backup)                   cmd_backup ;;
  nodes_audit_schedule_set) cmd_nodes_audit_schedule_set "$2" "$3" "$4" "$5" ;;
  # <<< geo-rpc generated:dispatch <<<
  *) printf '{"error":"usage"}'; exit 1 ;;
esac
