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
# read-only subscription-assignment resolver (config + audit state + registry, never writes)
SUB_STATUS_PY=/usr/lib/geomask/geomask-subscription-status.py
# Interpreter for the python helpers; GM_PYTHON only lets tests point at their own python.
PYTHON_BIN=${GM_PYTHON:-python3}
SUB_STATUS_PYTHON=$PYTHON_BIN
UPD_CHECK=$UPD_DIR/check.json
UPD_STATUS=$UPD_DIR/status.json
UPD_PIDF=$UPD_DIR/pid
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
  [ -f "$1" ] && grep -cE '^  - ' "$1" 2>/dev/null || echo 0
}

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
m=re.search(r"(?m)^  - name: %s\s*\n(?:^(?:    .*|\s*)\n)*" % re.escape(sys.argv[1]), t)
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
  # working = ip rule routes marked DNS to the AI tunnel AND the AI DNS chain listens
  if [ -f /etc/geomask-aidns.off ] || [ ! -f /etc/geomask-aidns.uid ]; then AIDNS=0
  elif ip route get 1.1.1.1 mark 0x64 2>/dev/null | grep -q 'dev AI' \
       && netstat -ltnpu 2>/dev/null | grep -q '192.168.0.53:5353 '; then AIDNS=1
  else AIDNS=2; fi
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
    "$(json_esc "$_sub2u")" "$(json_esc "$_aiu")" "$(json_esc "$_subu")" "$AIDNS")
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
  printf '{"log":"%s"}' "$(json_esc "$(tail -30 "$LOG" 2>/dev/null)")"
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

cmd_ipv6() {
  case "$1" in
    off)
      cp -n /etc/config/network "/etc/config/network.bak_v6off_$(date +%s)" 2>/dev/null || true
      echo 'net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1' > /etc/sysctl.d/99-disable-ipv6.conf
      sysctl -p /etc/sysctl.d/99-disable-ipv6.conf >/dev/null 2>&1
      for d in /proc/sys/net/ipv6/conf/*; do echo 1 > "$d/disable_ipv6" 2>/dev/null || true; done
      uci set network.lan.ip6assign='0'; uci commit network
      reload_config 2>/dev/null || ubus call network reload >/dev/null 2>&1
      ;;
    on)
      rm -f /etc/sysctl.d/99-disable-ipv6.conf
      for d in /proc/sys/net/ipv6/conf/*; do echo 0 > "$d/disable_ipv6" 2>/dev/null || true; done
      uci set network.lan.ip6assign='60'; uci commit network
      reload_config 2>/dev/null || ubus call network reload >/dev/null 2>&1
      ;;
    *) printf '{"error":"bad arg"}'; return 1 ;;
  esac
  sleep 1
  cmd_status
}

# AI-DNS on demand: regenerate the nft DNAT list for ai-mode devices + reload firewall.
# Installs nothing (that is the installer's aidns step); only re-syncs the device list.
cmd_aidns_apply() {
  if [ -f /etc/geomask-aidns.off ]; then
    printf '{"ok":true,"warn":"AI-DNS disabled (/etc/geomask-aidns.off)"}'
    return 0
  fi
  if [ ! -f /etc/geomask-aidns.uid ]; then
    printf '{"ok":false,"warn":"AI-DNS not installed - run the aidns step of geomask"}'
    return 0
  fi
  _before=$(cat /tmp/geomask-aidns-last 2>/dev/null)
  _aidns_sync
  _after=$(cat /tmp/geomask-aidns-last 2>/dev/null)
  if [ "$_before" != "$_after" ]; then
    echo "$(date +%FT%T) AI-DNS apply (manual)" >> "$LOG"
    printf '{"ok":true,"warn":""}'
  else
    printf '{"ok":true,"warn":"no changes"}'
  fi
  return 0
}

# add/update a device geo-policy: dev_add <ip> <mode> [mac] [name]
cmd_dev_add() {
  IP="$1"; MODE="$2"; MAC="$3"; NAME="${4:-$IP}"
  case "$IP" in ''|*[!0-9.]*) printf '{"error":"bad ip"}'; return 1 ;; esac
  echo "$IP" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || { printf '{"error":"bad ip"}'; return 1; }
  case "$MODE" in bypass|realtz|ai) ;; *) printf '{"error":"bad mode"}'; return 1 ;; esac
  NAME=$(printf '%s' "$NAME" | tr -d '"\\"\n\r\t' | head -c 64)
  mkdir -p "$(dirname "$DEVICES")"; touch "$DEVICES"
  # replace existing entry for this ip
  grep -v "^$IP " "$DEVICES" > "$DEVICES.tmp" 2>/dev/null || true
  mv -f "$DEVICES.tmp" "$DEVICES"
  echo "$IP $MODE $NAME" >> "$DEVICES"
  # stable lease if mac given (strict MAC format only)
  if [ -n "$MAC" ] && echo "$MAC" | grep -qE '^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$'; then
    if ! uci show dhcp | grep -q "\.mac='$MAC'"; then
      SEC="dhcp.$(uci add dhcp host)"
      uci set "$SEC.mac=$MAC"; uci set "$SEC.ip=$IP"; uci set "$SEC.name=$NAME"
      uci commit dhcp
      /etc/init.d/dnsmasq restart >/dev/null 2>&1 || true
    fi
  fi
  _apply_routing
  echo "$(date +%FT%T) DEV add $IP mode=$MODE ($NAME)" >> "$LOG"
  cmd_status
}

cmd_dev_del() {
  IP="$1"
  case "$IP" in ''|*[!0-9.]*) printf '{"error":"bad ip"}'; return 1 ;; esac
  echo "$IP" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || { printf '{"error":"bad ip"}'; return 1; }
  [ -f "$DEVICES" ] && sed -i "\|^$IP |d" "$DEVICES"
  # remove rules for this ip explicitly per table (bypass -> main, ai -> 101).
  # NEVER delete by pref alone: all per-device rules share priority 19900, so
  # `ip rule del pref` would drop another device's rule (and _apply_routing re-adds it)
  ip rule del from "$IP" lookup main 2>/dev/null || true
  ip rule del from "$IP" lookup 101 2>/dev/null || true
  SEC=$(uci show dhcp | grep "\.ip='$IP'" | head -1 | sed "s/\.ip=.*//")
  if [ -n "$SEC" ]; then uci delete "$SEC"; uci commit dhcp; fi
  _apply_routing
  echo "$(date +%FT%T) DEV del $IP" >> "$LOG"
  cmd_status
}

# AI-DNS: keep the nft DNAT list in sync with the ai-mode devices of /etc/geo-devices.list.
# No-op when the step was not installed (/etc/geomask-aidns.uid absent) or disabled
# (/etc/geomask-aidns.off present). Firewall is reloaded only when the list changes.
# NOTE: the AI dnsmasq listens on 192.168.0.53:5353 (NOT :53) - the main dnsmasq
# rebinds every interface address including our alias, so :53 there is taken.
_aidns_sync() {
  [ -f /etc/geomask-aidns.uid ] || return 0
  [ -f /etc/geomask-aidns.off ] && return 0
  _uid=$(cat /etc/geomask-aidns.uid 2>/dev/null | tr -dc '0-9')
  [ -n "$_uid" ] || return 0
  _ips=$(awk '$2=="ai" && $1 !~ /^#/ {print $1}' "$DEVICES" 2>/dev/null)
  _list=""
  for _ip in $_ips; do _list="$_list $_ip,"; done
  _list=${_list%,}
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
      printf 'add rule inet geomask_ai_dns pre_nat ip saddr { %s } meta l4proto { tcp, udp } th dport 53 dnat ip to 192.168.0.53:5353\n' "$_list"
    fi
  } > /tmp/geomask-aidns.nft 2>/dev/null || return 0
  if ! nft -c -f /tmp/geomask-aidns.nft >/dev/null 2>&1; then
    rm -f /tmp/geomask-aidns.nft
    echo "$(date +%FT%T) AI-DNS: generated rules failed syntax check" >> "$LOG"
    return 0
  fi
  cp /tmp/geomask-aidns.nft /usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft 2>/dev/null
  rm -f /tmp/geomask-aidns.nft
  if [ "${_list}" != "$(cat /tmp/geomask-aidns-last 2>/dev/null)" ]; then
    printf '%s' "$_list" > /tmp/geomask-aidns-last
    /etc/init.d/firewall reload >/dev/null 2>&1 || true
    echo "$(date +%FT%T) AI-DNS: device list updated -> firewall reloaded" >> "$LOG"
  fi
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
      mkdir -p /usr/share/nftables.d/ruleset-post
      cat > /usr/share/nftables.d/ruleset-post/99-ai-accept.nft <<'EOF'
table inet fw4 {
    chain accept_to_AI {
        iifname "br-lan" oifname "AI" counter accept comment "custom: LAN clients to AI tunnel"
        iifname "Wireguard" oifname "AI" counter accept comment "custom: WG peers to AI tunnel"
    }
}
EOF
      /etc/init.d/firewall reload >/dev/null 2>&1
    fi
  fi
  # v1.27.10: the accept drop-in above was written only when the AI zone was first
  # created. On a box where the zone already existed (ours), a WireGuard peer routed
  # by MagiTrickle into the AI tunnel hit forward_wg with no matching accept, so fw4
  # answered the phone with a TCP reset and YouTube stalled after the first second.
  # Ensure the WG rule on every run, not just at creation time.
  _ai_drop=/usr/share/nftables.d/ruleset-post/99-ai-accept.nft
  if [ ! -s "$_ai_drop" ] || ! grep -q 'iifname "Wireguard" oifname "AI"' "$_ai_drop" 2>/dev/null; then
    mkdir -p /usr/share/nftables.d/ruleset-post
    cat > "$_ai_drop" <<'EOF'
table inet fw4 {
    chain accept_to_AI {
        iifname "br-lan" oifname "AI" counter accept comment "custom: LAN clients to AI tunnel"
        iifname "Wireguard" oifname "AI" counter accept comment "custom: WG peers to AI tunnel"
    }
}
EOF
    /etc/init.d/firewall reload >/dev/null 2>&1
  fi
  # table 101: LAN/host routes must stay local, only internet goes to AI tunnel
  ip route show table main | grep -v '^default' | while IFS= read -r _r; do
    ip route replace table 101 $_r 2>/dev/null
  done
  ip route replace table 101 default dev "$AI_TUN_DEV" 2>/dev/null
  echo "$(date +%FT%T) AI_TUN provisioned" >> "$LOG"
}

_hotplug_sync() {
  cat > /etc/hotplug.d/iface/99-default-vpn-route <<'EOF'
#!/bin/sh
# Route unmarked LAN traffic to Mihomo TUN; magitrickle marks take precedence.
# Devices from /etc/geo-devices.list with mode=bypass go DIRECT (real IP), never via VPN.
# Devices with mode=realtz follow normal mihomo routing (masked IP), only tz is real (PC-side).
# NOTE: runs inside netifd's ifup path -> MUST NOT block here (would stall PPPoE auth / WAN).
[ "$ACTION" = "ifup" ] || exit 0

apply_routes() {
  if ! ip route show table 100 | grep -q '^default'; then
    ip route show table main | grep -v '^default' | while IFS= read -r r; do
      ip route replace table 100 $r 2>/dev/null
    done
    ip route replace table 100 default dev Mihomo 2>/dev/null
  fi
  ip rule show | grep -q '32760:' || ip rule add priority 32760 iif br-lan lookup 100
  # --- ICMP bypass: 19901 stays ABOVE the magitrickle fwmark rule base
  #     (dynamic, observed 32558..32609) so ping to a marked IP never dies
  #     inside the TUN. Direct via pppoe-wan. Guard reseats the rule to
  #     (base - 100) when the base drifts, so presence is checked by SEMANTICS,
  #     never by the 19901 prefix (v1.6.2: no duplicates after a reseat). ---
  ip rule show | grep -q 'iif br-lan ipproto icmp lookup main' || ip rule add priority 19901 iif br-lan ipproto icmp lookup main
  # --- bypass-mode devices (19900 is safely above the magitrickle fwmark base,
  #     so marking cannot preempt a device rule; LAN stays local) ---
  if [ -f /etc/geo-devices.list ]; then
    while read -r dip dmode dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      [ "$dmode" = "bypass" ] || continue
      ip rule show | grep -q "from $dip" || ip rule add priority 19900 from "$dip" lookup main
    done < /etc/geo-devices.list
    # --- ai-mode devices (19900 is safely above the magitrickle fwmark base, so the whole
    #     device goes through the AI tunnel; LAN routes stay local in table 101) ---
    if grep -qE '^[0-9.]+[[:space:]]+ai[[:space:]]' /etc/geo-devices.list; then
      if ip link show AI >/dev/null 2>&1; then
        if ! ip route show table 101 | grep -q '^default'; then
          ip route show table main | grep -v '^default' | while IFS= read -r r; do
            ip route replace table 101 $r 2>/dev/null
          done
          ip route replace table 101 default dev AI 2>/dev/null
        fi
        while read -r dip dmode dname; do
          case "$dip" in ''|'#'*) continue ;; esac
          [ "$dmode" = "ai" ] || continue
        ip rule show | grep -q "from $dip" || ip rule add priority 19900 from "$dip" lookup 101
        done < /etc/geo-devices.list
      fi
    fi
  fi
}

if ip link show Mihomo >/dev/null 2>&1; then
  apply_routes
else
  # Mihomo TUN not up yet: retry in BACKGROUND so netifd is never stalled (fixes PPPoE hangs).
  ( i=0; while [ $i -lt 20 ]; do
      ip link show Mihomo >/dev/null 2>&1 && { sleep 1; apply_routes; exit 0; }
      i=$((i+1)); sleep 1
    done ) >/dev/null 2>&1 &
fi
exit 0
EOF
  sh -n /etc/hotplug.d/iface/99-default-vpn-route 2>/dev/null || \
    echo '{"error":"hotplug syntax"}'
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
  cp -p "$MIHOMO_CONF" "$MIHOMO_CONF.bak_slot" 2>/dev/null
  _res=$("$PYTHON_BIN" -B "$SLOT_PATCH_PY" "$MIHOMO_CONF" "$_g" "$URL" 2>&1)
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
  echo "$(date +%FT%T) SLOT $_g <- $URL ($_res)" >> "$LOG"
  printf '{"ok":true,"slot":"%s","change":"%s","url":"%s","node":"%s"}' \
    "$_g" "$(json_esc "$_res")" "$(json_esc "$URL")" "$(json_esc "$(_grp_node "$_g")")"
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
      "$(json_esc "${sid:-}")" "$(json_esc "${sname:-}")" "$(json_esc "${surl:-}")"
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
    _subs_json | grep -Fq "$_subu" || cmd_sub_add "VPN (текущая)" "$_subu" >/dev/null
  fi
  echo "$(date +%FT%T) SUB seed (auto)" >> "$LOG"
}

cmd_subs_seed() {
  _subs_seed_internal
  printf '{"ok":true,"subs":%s}' "$(_subs_json)"
}

cmd_sub_add() {
  NAME="$1"; URL="$2"
  case "$URL" in
    http://*|https://*) ;;
    *) printf '{"error":"url must start with http:// or https://"}'; return 1 ;;
  esac
  if printf '%s' "$URL" | grep -qE '[[:space:]"]'; then
    printf '{"error":"url contains forbidden characters"}'; return 1
  fi
  [ -n "$NAME" ] || NAME=$(printf '%s' "$URL" | sed 's|^https\?://||; s|/.*||')
  NAME=$(printf '%s' "$NAME" | tr -d '|' | head -c 48)
  ID=$(date +%s | tail -c 9)
  while grep -q "^$ID|" "$SUBS_FILE" 2>/dev/null; do ID=$((ID+1)); done
  echo "$ID|$NAME|$URL" >> "$SUBS_FILE"
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
    USED=$(python3 "$NODE_AUDIT" source-in-use "$URL" 2>/dev/null)
    case "$USED" in
      *'"in_use": true'*|*'"in_use":true'*)
        printf '{"error":"subscription is used by a path; switch the path to another subscription before deleting it"}'
        return 1 ;;
    esac
  fi
  grep -v "^$ID|" "$SUBS_FILE" > "$SUBS_FILE.tmp" || true
  mv -f "$SUBS_FILE.tmp" "$SUBS_FILE"
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
  [ -f "$UPD_PIDF" ] || return 1
  _p=$(cat "$UPD_PIDF" 2>/dev/null)
  [ -n "$_p" ] && kill -0 "$_p" 2>/dev/null
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
  DIR=/root/geomask-backups
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
  # v1.9.6: simple credentials (owner request); guest/no-password also works, share is read-only
  printf '{"ok":true,"dir":"%s","unc":"%s","smb":"%s","user":"backup","pass":"backup","share_ok":%s,"files":[%s]}\n' \
    "$DIR" "$_unc" "$_smbu" "$_sh" "$_files"
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
trap 'rm -f "$D/pid"' EXIT
TAG="$1"
say queued "update to $TAG started"
say backup "creating full router backup (sysupgrade -b)..."
TS=$(date +%Y%m%d-%H%M%S)
BKD=/root/geomask-backups
BK=/tmp/geomask-bk-$TS.tar.gz
# purge BEFORE creating: files under /root are embedded by sysupgrade -b (nesting),
# and /tmp legacy copies bloat tmpfs. Retention on router: only the freshest backup.
mkdir -p "$BKD" 2>/dev/null
rm -f /tmp/router-backup-*.tar.gz "$BKD"/router-backup-*.tar.gz 2>/dev/null
if ! sysupgrade -b "$BK" >/dev/null 2>&1 || [ ! -s "$BK" ]; then
  say error "backup failed - update aborted"; exit 1
fi
SZ=$(wc -c < "$BK")
_md=$(md5sum "$BK" 2>/dev/null | awk '{print $1}')
BKF="$BKD/router-backup-$TS.tar.gz"
mv -f "$BK" "$BKF" 2>/dev/null || BKF="$BK"
[ -s "$BKF" ] || { say error "backup vanished after create - update aborted"; exit 1; }
echo "file=$BKF md5=$_md" > "$D/backup.info"
LANIP=$(ip -4 addr show br-lan 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -1)
say backup "backup OK: $BKF ($SZ bytes, md5 $_md)"
say backup "SMB (retention: freshest one): \\\\$LANIP\\geomask-backups\\${BKF##*/} - Windows: net use /user:backup backup (or no password), read-only; PC history: fetch_backup.ps1"
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
if [ -n "$DIG" ]; then
  ACT="sha256:$(sha256sum "$D/geomask.sh.new" | awk '{print $1}')"
  [ "$ACT" = "$DIG" ] || { say error "sha256 mismatch"; exit 1; }
  say validate "version $NEWV ok, sha256 matches release digest"
else
  say validate "version $NEWV ok (digest check skipped - no cached digest)"
fi
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
  echo $! > "$UPD_PIDF"
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
cmd_nodes_set() {
  _en=$1; _sched=$2; _cc=$3; _as=$4; _base=$5; _prov=$6
  case "$_en" in 0|1) : ;; *) printf '{"ok":false,"error":"bad enabled"}\n'; return 1;; esac
  printf '%s' "$_sched" | grep -qE '^([01][0-9]|2[0-3]):[0-5][0-9]$' || { printf '{"ok":false,"error":"bad schedule (HH:MM)"}\n'; return 1; }
  [ -z "$_cc" ] || printf '%s' "$_cc" | grep -qE '^([A-Z][A-Z],)*[A-Z][A-Z]$' || { printf '{"ok":false,"error":"bad countries (AA,BB)"}\n'; return 1; }
  [ -z "$_as" ] || printf '%s' "$_as" | grep -qE '^(AS?[0-9]+,)*AS?[0-9]+$' || { printf '{"ok":false,"error":"bad asns"}\n'; return 1; }
  printf '%s' "$_base" | grep -qE '["'"'"'\\]' && { printf '{"ok":false,"error":"base filter: no quotes/backslashes"}\n'; return 1; }
  [ -z "$_prov" ] && _prov=sub
  printf '%s' "$_prov" | grep -qE '^[a-z0-9_-]+$' || { printf '{"ok":false,"error":"bad provider name"}\n'; return 1; }
  # write conf (full template; keep-as-is logic preserved, user values)
  cat > "$NODESCONF" <<EOF
# geomask node-scan configuration (edited via panel $(date '+%F %T'))
bad_countries=$_cc
bad_asns=$_as
base_filter=$_base
sub_provider=$_prov
EOF
  _hr=${_sched%:*}; _min=${_sched#*:}
  # cron line (hour/min: cron wants numbers; strip leading zeros to be safe)
  _hrn=$(printf '%s' "$_hr" | sed 's/^0//'); _minn=$(printf '%s' "$_min" | sed 's/^0//')
  [ -n "$_hrn" ] || _hrn=0
  _tmp=/tmp/gm-cron.$$
  crontab -l 2>/dev/null | grep -vF "$NODESCAN_TAG" > "$_tmp" || true
  if [ "$_en" = 1 ]; then
    printf '%s %s * * * /usr/bin/geomask-node-scan.sh >/dev/null 2>&1 %s\n' "$_minn" "$_hrn" "$NODESCAN_TAG" >> "$_tmp" 2>/dev/null
  fi
  crontab "$_tmp" && rm -f "$_tmp" || { printf '{"ok":false,"error":"crontab write failed"}\n'; return 1; }
  printf '%s NODESCAN cfg saved via panel (enabled=%s sched=%s)\n' "$(date '+%F %T')" "$_en" "$_sched" >> "$LOG"
  printf '{"ok":true,"enabled":"%s","schedule":"%s"}\n' "$_en" "$_sched"
}

# sync insurance cron interval: */N minutes (N: 1,2,3,5,10,15,30)
cmd_sync_set() {
  case "$1" in 1|2|3|5|10|15|30) : ;; *) printf '{"ok":false,"error":"bad interval"}\n'; return 1;; esac
  _tmp=/tmp/gm-cron.$$
  crontab -l 2>/dev/null | grep -vF "/root/sync_mtrickle_mihomo.sh" > "$_tmp" || true
  printf '*/%s * * * * %s # %s\n' "$1" "$SYNCLISTS" "$SYNC_TAG" >> "$_tmp"
  crontab "$_tmp" && rm -f "$_tmp" || { printf '{"ok":false,"error":"crontab write failed"}\n'; return 1; }
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
  /etc/init.d/magitrickle stop >/dev/null 2>&1 || true
  _out=$(python3 "$_py" 2>&1); _rc=$?
  /etc/init.d/magitrickle start >/dev/null 2>&1 || true
  sleep 2
  sh /root/sync_mtrickle_mihomo.sh >/dev/null 2>&1 || true
  controller_curl -s -X PUT http://127.0.0.1:9090/configs -H 'Content-Type: application/json' \
    -d '{"path":"/etc/mihomo/config.yaml"}' >/dev/null 2>&1 || true
  _ok=false; printf '%s' "$_out" | grep -q '^CHANGED' && _ok=true
  printf '{"ok":%s,"rc":%s,"result":"%s"}\n' "$_ok" "$_rc" "$(json_esc "$_out")"
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
  nodes_audit_schedule_set) cmd_nodes_audit_schedule_set "$2" "$3" "$4" "$5" ;;
  # <<< geo-rpc generated:dispatch <<<
  *) printf '{"error":"usage"}'; exit 1 ;;
esac
