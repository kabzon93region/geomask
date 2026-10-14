#!/bin/sh
# geomask.sh - geo-masking overlay + 2-subscription mihomo stack for OpenWrt
# Workflow: check -> geo overlay -> mihomo 2 subs -> magitrickle sync -> services -> verify
#
# Usage:
#   sh geomask.sh              interactive menu (status dashboard + actions loop)
#   sh geomask.sh --reinstall  delete -> install -> configure geomask (asks for 2 subscription URLs)
#   sh geomask.sh --backup     create a full settings backup (LuCI-restorable) and exit
#   sh geomask.sh --auto       all steps unattended: check, apply if needed, verify; stop on FAIL
#   sh geomask.sh --check      read-only report of every step (no changes, no downloads)
#   sh geomask.sh --step N     single step N interactively
#   sh geomask.sh --yes        never ask (with --auto/--step/--reinstall)
#   sh geomask.sh --restart    allow mihomo/magitrickle restarts where needed (drops VPN ~3s)
#   sh geomask.sh --vpn-url U --ai-url U    set subscription URLs (kept secret in config only)
#   sh geomask.sh --help
#
# Install (jsDelivr CDN - fast, shows download progress):
#   curl -fL -m 60 -o /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@master/geomask.sh' && sh /tmp/geomask.sh
# wget fallback:
#   wget -T 30 -O /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@master/geomask.sh' && sh /tmp/geomask.sh
# GitHub raw (can be VERY slow from some networks -> looks like a hang; prefer jsDelivr):
#   sh <(wget -qO - 'https://raw.githubusercontent.com/kabzon93region/geomask/master/geomask.sh')
#
# SAFETY: never reboots the router; never restarts working services (only starts missing ones);
#         never touches network/pppoe config; backups before every file write and before ANY
#         mihomo restart (full settings backup via sysupgrade -b -> /root/geomask-backups/);
#         mihomo restart = trial-run on shifted ports FIRST, restart only if the trial starts,
#         and automatic rollback to the previous config if the restarted service does not
#         answer. Hard pin: /root/.geomask-norestart disables --restart entirely.

set -u

GM_VERSION="1.5.3"
GM_PREFIX="${GM_PREFIX:-}"          # path prefix for tests (empty on real router)
GM_YES=0
GM_AUTO=0
GM_CHECK_ONLY=0
GM_RESTART=0
GM_BACKUP=0
GM_REINSTALL=0
GM_STEP=""
GM_VPN_URL=""
GM_AI_URL=""

# ---- embedded assets are injected by build.py into the heredocs below ----

PASS=0; FAILCNT=0; SKIP=0

log()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  [OK] %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  [FAIL] %s\n' "$*"; FAILCNT=$((FAILCNT+1)); }
warn() { printf '  [WARN] %s\n' "$*"; }
hdr()  { printf '\n== [%s] %s ==\n' "$1" "$2"; }

ask() { # ask "question" -> 0=yes  (auto/yes modes never block)
  [ "$GM_YES" = 1 ] && return 0
  [ "$GM_AUTO" = 1 ] && return 0
  printf '%s [y/N]: ' "$1"
  read -r _a || return 1
  case "$_a" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

P() { printf '%s%s' "$GM_PREFIX" "$1"; }

# extract provider url from mihomo config: _prov_url <file> <provider-name>
_prov_url() {
  awk -v p="^  $2:" '$0 ~ p {f=1;next} f && /^    url:/{gsub(/^[^"]*"/,"");gsub(/".*/,"");print;exit} f && /^  [A-Za-z0-9_-]+:/{f=0} f && /^[A-Za-z]/{f=0}' "$1" 2>/dev/null
}

save_stream() { # save_stream <target-path> <mode>  (content on stdin)
  _t="$(P "$1")"; _m="$2"
  mkdir -p "$(dirname "$_t")"
  _tmp="$_t.zmnew"
  cat > "$_tmp" || { rm -f "$_tmp"; return 1; }
  if [ -f "$_t" ]; then
    if cmp -s "$_tmp" "$_t"; then rm -f "$_tmp"; info "unchanged: $1"; return 0; fi
    cp -p "$_t" "$_t.bak_geomask_$(date +%s 2>/dev/null || echo bak)" 2>/dev/null || true
  fi
  chmod "$_m" "$_tmp" 2>/dev/null || true
  mv -f "$_tmp" "$_t" || return 1
  info "written: $1 (mode $_m)"
  return 0
}

have() { command -v "$1" >/dev/null 2>&1; }

svc_alive() { pidof "$1" >/dev/null 2>&1; }

initd() { # initd <svc> <action>  - respects GM_PREFIX for tests
  "$(P /etc/init.d/$1)" "$2" 2>/dev/null
}

# ============================================================ STEP 1: env
step1_check() {
  _need=0
  [ -f "$(P /etc/openwrt_release)" ] && info "OpenWrt: $(grep '^DISTRIB_RELEASE=' "$(P /etc/openwrt_release)" 2>/dev/null | cut -d"'" -f2)" || { bad "not OpenWrt (/etc/openwrt_release missing)"; _need=1; }
  if have apk; then info "pkg manager: apk"; elif have opkg; then info "pkg manager: opkg"; else bad "no apk/opkg"; _need=1; fi
  for c in python3 curl; do
    if have "$c"; then info "$c: present"; else warn "$c: MISSING (needed for magitrickle->mihomo sync)"; _need=1; fi
  done
  _tmp=$(df -m "$(P /tmp)" 2>/dev/null | awk 'NR==2{print $4}')
  if [ -n "${_tmp:-}" ] && [ "$_tmp" -ge 20 ] 2>/dev/null; then info "/tmp free: ${_tmp}MB"; else warn "/tmp free low or unknown"; _need=1; fi
  _root=$(df -m "$(P /)" 2>/dev/null | awk 'NR==2{print $4}')
  if [ -n "${_root:-}" ] && [ "$_root" -ge 30 ] 2>/dev/null; then info "root free: ${_root}MB"; else warn "root free low or unknown"; _need=1; fi
  _code=$(curl -m 5 -s -o /dev/null -w '%{http_code}' http://www.gstatic.com/generate_204 2>/dev/null)
  if [ "$_code" = "204" ]; then info "internet: OK (204 via router path)"; else warn "internet check: HTTP ${_code:-none} (WAN may be down)"; _need=1; fi
  return $_need
}
step1_apply() { warn "env issues cannot be auto-fixed here - fix manually, then re-run"; }
step1_verify() { step1_check; }

# ============================================================ STEP 2: geo overlay (ours)

# ============================================================ STEP 2: geo overlay (ours)
step2_check() {
  _need=0
  for f in /usr/bin/geo-backend.sh /usr/libexec/rpcd/geo-sync \
           /usr/share/rpcd/acl.d/luci-app-geo-sync.json \
           /usr/share/luci/menu.d/luci-app-geo-sync.json \
           /www/luci-static/resources/view/geo-sync/main.js; do
    if [ -s "$(P "$f")" ]; then info "present: $f"; else bad "missing: $f"; _need=1; fi
  done
  if have ubus && ubus list 2>/dev/null | grep -qx 'geo-sync'; then info "ubus geo-sync: registered"; else bad "ubus geo-sync not registered"; _need=1; fi
  return $_need
}
step2_apply() {
  save_stream /usr/bin/geo-backend.sh 755 <<'ASSET_EOF'
#!/bin/sh
# geo-backend.sh - backend for LuCI "Гео-маскировка и AI" panel (geo-sync ubus object).
# Commands:
#   status | leases | log | sync_lists | google
#   global <direct|vpn> | ipv6 <on|off>
#   dev_add <ip> <mode> [mac] [name] | dev_del <ip>
#     modes: bypass (real IP) | realtz (VPN, own tz) | ai (FULLY via AI subscription)
#   ai_sub_get | ai_sub_set <url>   (AI subscription / mihomo provider de5)
#   subs_list | sub_add <name> <url> | sub_del <id>
#   ai_sub_use <id> | vpn_sub_use <id>  (switch provider url from registry, no mihomo restart)
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
  curl -s -m3 "$MIHOMO_API/proxies/$1" | grep -oE '"now":"[^"]*"' | head -1 | sed 's/"now":"//; s/"$//'
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

cmd_status() {
  _migrate
  VPN_NODE=$(_grp_node VPN)
  AI_NODE=$(_grp_node AI)
  GLOBAL=$(curl -s -m3 "$MIHOMO_API/proxies/GLOBAL" | grep -oE '"now":"[^"]*"' | head -1 | sed 's/"now":"//; s/"$//')
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
  RULES=$(ip rule show | grep -c 'lookup main' || echo 0)
  _de5u=$(awk '/^  de5:/{f=1;next} f&&/^    url:/{gsub(/^[^"]*"/,"");gsub(/".*/,"");print;exit} f&&/^  [A-Za-z0-9_-]+:/{f=0} f&&/^[A-Za-z]/{f=0}' "$MIHOMO_CONF" 2>/dev/null)
  _subu=$(awk '/^  sub:/{f=1;next} f&&/^    url:/{gsub(/^[^"]*"/,"");gsub(/".*/,"");print;exit} f&&/^  [A-Za-z0-9_-]+:/{f=0} f&&/^[A-Za-z]/{f=0}' "$MIHOMO_CONF" 2>/dev/null)
  AI_SUB_ID=$(_sub_id_by_url "$_de5u" 2>/dev/null || echo '')
  VPN_SUB_ID=$(_sub_id_by_url "$_subu" 2>/dev/null || echo '')
  if ip link show "$AI_TUN_DEV" >/dev/null 2>&1; then AI_TUN=1; else AI_TUN=0; fi
  if netstat -lntp 2>/dev/null | grep -q ':7891'; then AI_LST=1; else AI_LST=0; fi
  printf '{"vpn_node":"%s","vpn_cc":"%s","ai_node":"%s","ai_cc":"%s","global":"%s","lcron":%s,"ipv6":%s,"doh":"%s","ai_rules":%s,"mt_rules":%s,"google":"%s","sync_at":"%s","devices":%s,"n_bypass":%s,"n_realtz":%s,"rules_ok":%s,"n_ai":%s,"ai_tun":%s,"ai_listener":%s,"ai_sub":"%s","vpn_sub":"%s","ai_url":"%s","vpn_url":"%s"}' \
    "$(json_esc "$VPN_NODE")" "$(_node_cc "$VPN_NODE")" \
    "$(json_esc "$AI_NODE")" "$(_node_cc "$AI_NODE")" \
    "$(json_esc "$GLOBAL")" "$LCRON" "$V6" "$(json_esc "$DOH")" \
    "$AI_RC" "$MT_RC" "$(json_esc "$GOOGLE_CC")" "$(json_esc "$SYNC_AT")" \
    "$DEV_LIST" "$BYPASS" "$REALTZ" "$RULES" \
    "$N_AI" "$AI_TUN" "$AI_LST" "$(json_esc "$AI_SUB_ID")" "$(json_esc "$VPN_SUB_ID")" \
    "$(json_esc "$_de5u")" "$(json_esc "$_subu")"
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
  curl -s -m5 -X PUT "$MIHOMO_API/proxies/GLOBAL" -H 'Content-Type: application/json' \
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
  ip rule del from "$IP" lookup main 2>/dev/null || true
  SEC=$(uci show dhcp | grep "\.ip='$IP'" | head -1 | sed "s/\.ip=.*//")
  if [ -n "$SEC" ]; then uci delete "$SEC"; uci commit dhcp; fi
  _apply_routing
  echo "$(date +%FT%T) DEV del $IP" >> "$LOG"
  cmd_status
}

# (re)apply routing for all managed devices:
#   bypass -> direct (32611) | realtz -> normal mihomo path | ai -> AI tunnel (32611 -> table 101 -> dev AI)
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
        ip rule show | grep -q "from $dip" || ip rule add priority 32611 from "$dip" lookup main
      elif [ "$dmode" = "ai" ]; then
        _ai_tun_ensure
           ip rule show | grep -q "from $dip" || ip rule add priority 32611 from "$dip" lookup 101
      fi
    done < "$DEVICES"
  fi
  _hotplug_sync
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
  # --- ICMP bypass: 32610 must stay BELOW the magitrickle fwmark rule base
  #     (~32645, grows upward with group count) or ping to a marked IP dies
  #     inside the TUN. Direct via pppoe-wan. ---
  ip rule show | grep -q '32610:' || ip rule add priority 32610 iif br-lan ipproto icmp lookup main
  # --- bypass-mode devices (32611 < magitrickle fwmark base ~32645, so marking
  #     cannot preempt a device rule; LAN stays local) ---
  if [ -f /etc/geo-devices.list ]; then
    while read -r dip dmode dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      [ "$dmode" = "bypass" ] || continue
      ip rule show | grep -q "from $dip" || ip rule add priority 32611 from "$dip" lookup main
    done < /etc/geo-devices.list
    # --- ai-mode devices (32611 < magitrickle fwmark base ~32645, so the whole
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
        ip rule show | grep -q "from $dip" || ip rule add priority 32611 from "$dip" lookup 101
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

# ---- AI subscription (mihomo provider "de5") ----
cmd_ai_sub_get() {
  u=$(awk '/^  de5:/{f=1;next} f&&/^    url:/{gsub(/^[^"]*"/,"");gsub(/".*/,"");print;exit} f&&/^  [A-Za-z0-9_-]+:/{f=0} f&&/^[A-Za-z]/{f=0}' "$MIHOMO_CONF" 2>/dev/null)
  printf '{"url":"%s"}' "$(json_esc "$u")"
}

# patch provider url in mihomo config + hot-refresh via API (no mihomo restart)
# _mihomo_set_provider <provider> <url>
_mihomo_set_provider() {
  PN="$1"; URL="$2"
  case "$URL" in
    http://*|https://*) ;;
    *) printf '{"error":"url must start with http:// or https://"}'; return 1 ;;
  esac
  if printf '%s' "$URL" | grep -qE '[[:space:]"]'; then
    printf '{"error":"url contains forbidden characters"}'; return 1
  fi
  [ -f "$MIHOMO_CONF" ] || { printf '{"error":"mihomo config missing"}'; return 1; }
  grep -q "^  $PN:" "$MIHOMO_CONF" || { printf '{"error":"provider %s not found in config"}' "$(json_esc "$PN")"; return 1; }
  cp -p "$MIHOMO_CONF" "$MIHOMO_CONF.bak_subswitch_$(date +%s)"
  TMPF="$MIHOMO_CONF.subsw"
  if ! awk -v pn="^  $PN:" -v nu="$URL" '
    BEGIN { f=0; done=0 }
    $0 ~ pn { f=1; print; next }
    f && /^    url:/ { print "    url: \"" nu "\""; done=1; next }
    f && /^  [A-Za-z0-9_-]+:/ { f=0 }
    f && /^[A-Za-z]/ { f=0 }
    { print }
    END { exit done ? 0 : 1 }
  ' "$MIHOMO_CONF" > "$TMPF"; then
    rm -f "$TMPF"
    printf '{"error":"failed to patch provider url"}'; return 1
  fi
  if mihomo -d /etc/mihomo -t >/dev/null 2>&1; then
    mv -f "$TMPF" "$MIHOMO_CONF"
  else
    rm -f "$TMPF"
    printf '{"error":"mihomo config test failed - old config kept"}'; return 1
  fi
  # apply to the RUNNING mihomo without restart:
  # PUT /configs with config path forces re-read of the config file,
  # which re-fetches the provider subscription (old node list replaced)
  if curl -s -m15 -f -X PUT "$MIHOMO_API/configs" \
      -H 'Content-Type: application/json' \
      -d "{\"path\":\"$MIHOMO_CONF\"}" >/dev/null 2>&1; then
    WARN=''
  else
    WARN='mihomo reload failed - reload via service/restart needed'
  fi
  # verify the running provider actually serves nodes after reload
  sleep 2
  _n=$(curl -s -m6 "$MIHOMO_API/providers/proxies/$PN" 2>/dev/null \
       | tr ',' '\n' | grep -c "\"provider-name\":\"$PN\"")
  case "${_n:-0}" in ''|*[!0-9]*) _n=0 ;; esac
  if [ "$_n" -eq 0 ]; then
    WARN='subscription returned no nodes - check the link, mihomo kept previous node list'
  fi
  echo "$(date +%FT%T) PROVIDER $PN <- $URL (nodes=$_n)" >> "$LOG"
  printf '{"ok":true,"provider":"%s","url":"%s","warn":"%s"}' \
    "$(json_esc "$PN")" "$(json_esc "$URL")" "$(json_esc "$WARN")"
}

cmd_ai_sub_set() {
  _mihomo_set_provider de5 "$1"
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
  _de5u=$(awk '/^  de5:/{f=1;next} f&&/^    url:/{gsub(/^[^"]*"/,"");gsub(/".*/,"");print;exit} f&&/^  [A-Za-z0-9_-]+:/{f=0} f&&/^[A-Za-z]/{f=0}' "$MIHOMO_CONF" 2>/dev/null)
  if [ -n "$_subu" ]; then
    _subs_json | grep -Fq "$_subu" || cmd_sub_add "VPN (текущая)" "$_subu" >/dev/null
  fi
  if [ -n "$_de5u" ]; then
    _subs_json | grep -Fq "$_de5u" || cmd_sub_add "AI (текущая)" "$_de5u" >/dev/null
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
  grep -v "^$ID|" "$SUBS_FILE" > "$SUBS_FILE.tmp" || true
  mv -f "$SUBS_FILE.tmp" "$SUBS_FILE"
  echo "$(date +%FT%T) SUB del $ID" >> "$LOG"
  printf '{"ok":true,"subs":%s}' "$(_subs_json)"
}

cmd_ai_sub_use() {
  ID="$1"
  URL=$(_find_sub_url "$ID") || { printf '{"error":"subscription not found"}'; return 1; }
  _mihomo_set_provider de5 "$URL"
  AI_NODE=$(_grp_node AI)
  printf '{"ok":true,"url":"%s","ai_node":"%s"}' "$(json_esc "$URL")" "$(json_esc "$AI_NODE")"
}

cmd_vpn_sub_use() {
  ID="$1"
  URL=$(_find_sub_url "$ID") || { printf '{"error":"subscription not found"}'; return 1; }
  _mihomo_set_provider sub "$URL"
  VPN_NODE=$(_grp_node VPN)
  printf '{"ok":true,"url":"%s","vpn_node":"%s"}' "$(json_esc "$URL")" "$(json_esc "$VPN_NODE")"
}

case "$1" in
  status)     cmd_status ;;
  leases)     cmd_leases ;;
  log)        cmd_log ;;
  sync_lists) cmd_sync_lists ;;
  google)     cmd_google ;;
  global)     cmd_global "$2" ;;
  ipv6)       cmd_ipv6 "$2" ;;
  dev_add)    cmd_dev_add "$2" "$3" "$4" "$5" ;;
  dev_del)    cmd_dev_del "$2" ;;
  ai_sub_get) cmd_ai_sub_get ;;
  ai_sub_set) cmd_ai_sub_set "$2" ;;
  subs_list)  cmd_subs_list ;;
  sub_add)    cmd_sub_add "$2" "$3" ;;
  sub_del)    cmd_sub_del "$2" ;;
  ai_sub_use)   cmd_ai_sub_use "$2" ;;
  vpn_sub_use)  cmd_vpn_sub_use "$2" ;;
  *) printf '{"error":"usage"}'; exit 1 ;;
esac

ASSET_EOF
  save_stream /usr/libexec/rpcd/geo-sync 755 <<'ASSET_EOF'
#!/bin/sh
# rpcd plugin for geo-sync (LuCI "Гео-маскировка и AI")
# NOTE: router timezone commands removed - tz is per-PC (GeoSync.ps1), not needed on the router.

. /usr/share/libubox/jshn.sh

BACKEND="/usr/bin/geo-backend.sh"

list_methods() {
	json_init
	json_add_object "status";     json_close_object
	json_add_object "leases";     json_close_object
	json_add_object "log";        json_close_object
	json_add_object "sync_lists"; json_close_object
	json_add_object "google";     json_close_object
	json_add_object "global";     json_add_string "mode" "string"; json_close_object
	json_add_object "ipv6";       json_add_string "state" "string"; json_close_object
	json_add_object "dev_add";    json_add_string "ip" "string"; json_add_string "mode" "string"; json_add_string "mac" "string"; json_add_string "name" "string"; json_close_object
	json_add_object "dev_del";    json_add_string "ip" "string"; json_close_object
	json_add_object "ai_sub_get"; json_close_object
	json_add_object "ai_sub_set"; json_add_string "url" "string"; json_close_object
	json_add_object "subs_list";  json_close_object
	json_add_object "subs_seed"; json_close_object
	json_add_object "sub_add";    json_add_string "name" "string"; json_add_string "url" "string"; json_close_object
	json_add_object "sub_del";    json_add_string "id" "string"; json_close_object
	json_add_object "ai_sub_use";   json_add_string "id" "string"; json_close_object
	json_add_object "vpn_sub_use";  json_add_string "id" "string"; json_close_object
	json_dump
}

call_method() {
	local input method
	method="$1"
	input="$(cat)"
	json_load "$input" 2>/dev/null
	case "$method" in
		status)     "$BACKEND" status ;;
		leases)     "$BACKEND" leases ;;
		log)        "$BACKEND" log ;;
		sync_lists) "$BACKEND" sync_lists ;;
		google)     "$BACKEND" google ;;
		global)     json_get_var mode mode;   "$BACKEND" global "$mode" ;;
		ipv6)       json_get_var state state; "$BACKEND" ipv6 "$state" ;;
		dev_add)    json_get_var ip ip; json_get_var mode mode; json_get_var mac mac; json_get_var name name; "$BACKEND" dev_add "$ip" "$mode" "$mac" "$name" ;;
		dev_del)    json_get_var ip ip; "$BACKEND" dev_del "$ip" ;;
		ai_sub_get) "$BACKEND" ai_sub_get ;;
		ai_sub_set) json_get_var url url; "$BACKEND" ai_sub_set "$url" ;;
		subs_list)  "$BACKEND" subs_list ;;
		subs_seed)  "$BACKEND" subs_seed ;;
		sub_add)    json_get_var name name; json_get_var url url; "$BACKEND" sub_add "$name" "$url" ;;
		sub_del)    json_get_var id id; "$BACKEND" sub_del "$id" ;;
		ai_sub_use)   json_get_var id id; "$BACKEND" ai_sub_use "$id" ;;
		vpn_sub_use)  json_get_var id id; "$BACKEND" vpn_sub_use "$id" ;;
		*) echo '{"error":"unknown method"}'; return 1 ;;
	esac
}

case "$1" in
	list) list_methods ;;
	call)
		t="/tmp/geo-rpc.$$"
		call_method "$2" > "$t" 2>/dev/null
		sz="$(wc -c < "$t" 2>/dev/null | tr -d ' ')"
		if [ "${sz:-0}" -gt 400000 ]; then
			echo '{"error":"too big"}'
		else
			cat "$t"
		fi
		rm -f "$t"
		;;
	*) echo '{"error":"usage: geo-sync {list|call <method>}"}'; exit 1 ;;
esac

ASSET_EOF
  save_stream /usr/share/rpcd/acl.d/luci-app-geo-sync.json 644 <<'ASSET_EOF'
{
	"luci-app-geo-sync": {
		"description": "Grant access to geo masking + AI routing panel",
		"read": {
			"ubus": {
				"geo-sync": [ "status", "leases", "log", "ai_sub_get", "subs_list" ],
				"system": [ "info" ]
			}
		},
		"write": {
			"ubus": {
				"geo-sync": [ "sync_lists", "google", "global", "ipv6", "dev_add", "dev_del", "ai_sub_set", "sub_add", "sub_del", "ai_sub_use", "vpn_sub_use", "subs_seed" ]
			}
		}
	}
}

ASSET_EOF
  save_stream /usr/share/luci/menu.d/luci-app-geo-sync.json 644 <<'ASSET_EOF'
{
	"admin/services/geo-sync": {
		"title": "Гео-маскировка и AI",
		"order": 59,
		"action": {
			"type": "view",
			"path": "geo-sync/main"
		},
		"depends": {
			"acl": [ "luci-app-geo-sync" ]
		}
	}
}

ASSET_EOF
  save_stream /www/luci-static/resources/view/geo-sync/main.js 644 <<'ASSET_EOF'
'use strict';
'require view';
'require rpc';
'require ui';

var callStatus = rpc.declare({ object: 'geo-sync', method: 'status', expect: {} });
var callLeases = rpc.declare({ object: 'geo-sync', method: 'leases', expect: {} });
var callLog    = rpc.declare({ object: 'geo-sync', method: 'log',    expect: {} });
var callLists  = rpc.declare({ object: 'geo-sync', method: 'sync_lists', expect: {} });
var callGoogle = rpc.declare({ object: 'geo-sync', method: 'google',  expect: {} });
/* params as OBJECT (not array): array form expects positional args and would
   wrap {mode:...} into {"mode":{...}} breaking rpcd json_get_var */
var callGlobal = rpc.declare({ object: 'geo-sync', method: 'global',  params: { mode: null }, expect: {} });
var callIpv6   = rpc.declare({ object: 'geo-sync', method: 'ipv6',    params: { state: null }, expect: {} });
var callDevAdd = rpc.declare({ object: 'geo-sync', method: 'dev_add', params: { ip: null, mode: null, mac: null, name: null }, expect: {} });
var callDevDel = rpc.declare({ object: 'geo-sync', method: 'dev_del', params: { ip: null }, expect: {} });
var callAiSubGet = rpc.declare({ object: 'geo-sync', method: 'ai_sub_get', expect: {} });
var callAiSubSet = rpc.declare({ object: 'geo-sync', method: 'ai_sub_set', params: { url: null }, expect: {} });
var callSubsList = rpc.declare({ object: 'geo-sync', method: 'subs_list', expect: {} });
var callSubsSeed = rpc.declare({ object: 'geo-sync', method: 'subs_seed', expect: {} });
var callSubAdd   = rpc.declare({ object: 'geo-sync', method: 'sub_add',   params: { name: null, url: null }, expect: {} });
var callSubDel   = rpc.declare({ object: 'geo-sync', method: 'sub_del',   params: { id: null }, expect: {} });
var callAiSubUse   = rpc.declare({ object: 'geo-sync', method: 'ai_sub_use',   params: { id: null }, expect: {} });
var callVpnSubUse  = rpc.declare({ object: 'geo-sync', method: 'vpn_sub_use',  params: { id: null }, expect: {} });

var CCNAME = {
	DE: 'Германия', NL: 'Нидерланды', FI: 'Финляндия', LV: 'Латвия', LT: 'Литва',
	EE: 'Эстония', PL: 'Польша', DK: 'Дания', SE: 'Швеция', NO: 'Норвегия',
	US: 'США', TR: 'Турция', AL: 'Албания', FR: 'Франция', IT: 'Италия',
	ES: 'Испания', AT: 'Австрия', CH: 'Швейцария', GB: 'Великобритания'
};

var STYLE = [
	'.gs-wrap{display:flex;flex-direction:column;gap:14px;max-width:960px}',
	'.gs-hero{background:linear-gradient(135deg,#0d419d22,#1f6feb11);border:1px solid #1f6feb55;border-radius:12px;padding:18px}',
	'.gs-hero h2{margin:0 0 4px;font-size:17px}',
	'.gs-hero .gs-sub{color:#8b949e;font-size:13px}',
	'.gs-duo{display:flex;gap:12px;flex-wrap:wrap;margin-top:14px}',
	'.gs-sub-card{flex:1;min-width:280px;background:var(--bg-color,#161b22);border:1px solid var(--border-color,#30363d);border-radius:10px;padding:14px 16px}',
	'.gs-sub-card .gs-ent{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e}',
	'.gs-sub-card .gs-node{font-size:15px;font-weight:600;margin:6px 0 2px}',
	'.gs-sub-card .gs-meta{font-size:12px;color:#8b949e}',
	'.gs-cc{display:inline-block;min-width:30px;text-align:center;font-size:11px;font-weight:700;letter-spacing:.4px;color:#58a6ff;background:#1f6feb22;border:1px solid #1f6feb55;border-radius:6px;padding:2px 7px;margin-right:8px;vertical-align:1px}',
	'.gs-counters{display:flex;gap:12px;flex-wrap:wrap;margin-top:12px}',
	'.gs-counter{flex:1;min-width:150px;background:#0d1117;border:1px solid #30363d;border-radius:9px;padding:12px 14px}',
	'.gs-counter .gs-num{font-size:26px;font-weight:700;line-height:1.1}',
	'.gs-counter .gs-cap{font-size:12px;color:#8b949e;margin-top:3px}',
	'.gs-card{background:var(--bg-color,#161b22);border:1px solid var(--border-color,#30363d);border-radius:10px;padding:16px}',
	'.gs-card h3{margin:0 0 10px;font-size:15px}',
	'.gs-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 0;flex-wrap:wrap}',
	'.gs-label{color:var(--text-color,#c9d1d9)}',
	'.gs-val{font-weight:600}',
	'.gs-badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}',
	'.gs-on{background:#1f6feb33;color:#58a6ff;border:1px solid #1f6feb66}',
	'.gs-ok{background:#23863633;color:#3fb950;border:1px solid #23863666}',
	'.gs-off{background:#f8514922;color:#f85149;border:1px solid #f8514966}',
	'.gs-warn{background:#d2992222;color:#d29922;border:1px solid #d2992266}',
	'.gs-seg{display:inline-flex;border:1px solid #30363d;border-radius:8px;overflow:hidden}',
	'.gs-seg div{padding:5px 14px;cursor:pointer;font-size:13px}',
	'.gs-seg .gs-active{background:#1f6feb;color:#fff}',
	'.gs-btn{padding:6px 16px;border-radius:7px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;cursor:pointer;font-size:13px}',
	'.gs-btn:hover{background:#30363d}',
	'.gs-btn-primary{background:#1f6feb;border-color:#1f6feb;color:#fff}',
	'.gs-btn-primary:hover{background:#388bfd}',
	'.gs-btn-warn{border-color:#f8514966;color:#f85149}',
	'.gs-btn:disabled{opacity:.55;cursor:default}',
	'.gs-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}',
	'.gs-log{background:#0d1117;color:#8b949e;font-family:monospace;font-size:12px;padding:10px;border-radius:7px;max-height:220px;overflow:auto;white-space:pre-wrap}',
	'.gs-table{width:100%;border-collapse:collapse;font-size:13px}',
	'.gs-table td,.gs-table th{padding:6px 8px;border-bottom:1px solid #30363d;text-align:left}',
	'.gs-subs{width:100%;table-layout:fixed}',
	'.gs-subs td{overflow-wrap:anywhere;vertical-align:middle}',
	'.gs-subs td:first-child{width:110px;font-weight:600}',
	'.gs-subs td:nth-child(2){font-family:monospace;font-size:12px;color:#8b949e}',
	'.gs-subs td:nth-child(3){width:110px}',
	'.gs-subs td:last-child{width:200px;white-space:nowrap}',
	'.gs-subs .gs-btn-use{background:#21262d;border-color:#30363d;color:#c9d1d9;padding:5px 10px;margin-right:4px}',
	'.gs-btn-vpn-on{background:#1f6feb;border-color:#58a6ff;color:#fff}',
	'.gs-btn-vpn-on:hover{background:#388bfd}',
	'.gs-btn-ai-on{background:#238636;border-color:#3fb950;color:#fff}',
	'.gs-btn-ai-on:hover{background:#2ea043}',
	'.gs-subs .gs-btn-use:hover{background:#30363d}',
	'.gs-subs .gs-btn-warn{padding:5px 10px}',
	'.gs-input{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px 9px;font-size:13px}',
	'.gs-hint{color:#8b949e;font-size:12px;margin-top:6px}',
	'.gs-out{background:#0d1117;color:#8b949e;font-family:monospace;font-size:12px;padding:9px 11px;border-radius:7px;margin-top:10px;white-space:pre-wrap;max-height:130px;overflow:auto}'
].join('\n');

function injectStyle() {
	if (document.getElementById('gs-style')) return;
	var s = document.createElement('style');
	s.id = 'gs-style';
	s.textContent = STYLE;
	document.head.appendChild(s);
}

function badge(on, yes, no) {
	return E('span', { 'class': 'gs-badge ' + (on ? 'gs-on' : 'gs-off') }, on ? yes : no);
}

function badgeKlass(on, okCls, offCls, yes, no) {
	return E('span', { 'class': 'gs-badge ' + (on ? okCls : offCls) }, on ? yes : no);
}

/* Strip emoji from mihomo node names (flags, pictographs, VS16, ZWJ) - LuCI font
   renders them as tofu boxes, so we show clean text instead. */
function cleanName(s) {
	if (!s) return '';
	var out = String(s)
		.replace(/[\u{1F1E6}-\u{1F1FF}]/gu, '')   /* regional indicators (flag halves) */
		.replace(/[\u{1F300}-\u{1FAFF}]/gu, '')   /* pictographs: gamepad, rockets etc */
		.replace(/[\u{2600}-\u{27BF}]/gu, '')     /* misc symbols & dingbats */
		.replace(/[\u{FE0E}\u{FE0F}\u{200D}\u{200B}]/gu, '') /* VS15/VS16, ZWJ, ZWSP */
		.replace(/\s{2,}/g, ' ')
		.trim();
	return out || String(s).trim();
}

/* Country badge as plain text (code chip) - no flag emoji, renders everywhere. */
function ccBadge(cc) {
	return E('span', { 'class': 'gs-cc' }, cc || '—');
}

var E = (function(raw) {
	return function() {
		var a = Array.prototype.slice.call(arguments), i = a.length - 1;
		if (i >= 1 && (typeof a[i] === 'string' || typeof a[i] === 'number')) a[i] = [ String(a[i]) ];
		return raw.apply(null, a);
	};
})(window.E);

return view.extend({
	load: function() {
		injectStyle();
		return Promise.all([ callStatus(), callLeases(), callLog(), callAiSubGet(), callSubsList() ]);
	},

	render: function(all) {
		var st = all[0] || {}, leases = (all[1] || {}).leases || [], lg = (all[2] || {}).log || '';
		var aicfg = all[3] || {};
		var subs = (all[4] || {}).subs || [];
		var view = this;
		var wrap = E('div', { 'class': 'gs-wrap' });

		/* ===================== HERO: dual subscription ===================== */
		var aiCc = st.ai_cc || '', vpnCc = st.vpn_cc || '';
		var aiSub = E('div', { 'class': 'gs-sub-card' }, [
			E('div', { 'class': 'gs-ent' }, 'ИИ · Google / OpenAI / Claude → AI-подписка'),
			E('div', { 'class': 'gs-node' }, [ ccBadge(aiCc), cleanName(st.ai_node) || '—' ]),
			E('div', { 'class': 'gs-meta' }, 'Подписка de5 · выход ' + ((CCNAME[aiCc] || aiCc) ? (CCNAME[aiCc] || aiCc) : '—') +
				(st.google ? ' · Google видит: ' + st.google : ''))
		]);
		var vpnSub = E('div', { 'class': 'gs-sub-card' }, [
			E('div', { 'class': 'gs-ent' }, 'Общая VPN · всё магитрикле (не-ИИ)'),
			E('div', { 'class': 'gs-node' }, [ ccBadge(vpnCc), cleanName(st.vpn_node) || '—' ]),
			E('div', { 'class': 'gs-meta' }, 'Подписка vlessi · выход ' + ((CCNAME[vpnCc] || vpnCc) ? (CCNAME[vpnCc] || vpnCc) : '—'))
		]);

		var counters = E('div', { 'class': 'gs-counters' }, [
			E('div', { 'class': 'gs-counter' }, [ E('div', { 'class': 'gs-num' }, String(st.ai_rules != null ? st.ai_rules : '—')),
				E('div', { 'class': 'gs-cap' }, 'правил ИИ-пути (ai.yaml → AI)') ]),
			E('div', { 'class': 'gs-counter' }, [ E('div', { 'class': 'gs-num' }, String(st.mt_rules != null ? st.mt_rules : '—')),
				E('div', { 'class': 'gs-cap' }, 'правил общего пути (mtrickle.yaml → VPN)') ]),
			E('div', { 'class': 'gs-counter' }, [ E('div', { 'class': 'gs-num' }, String(st.n_bypass != null ? st.n_bypass : (st.devices || []).length)),
				E('div', { 'class': 'gs-cap' }, 'мимо VPN (реальный IP)') ])
		]);

		var hero = E('div', { 'class': 'gs-hero' }, [
			E('h2', {}, 'Гео-маскировка и маршрутизация AI'),
			E('div', { 'class': 'gs-sub' }, 'Google и ИИ-сервисы идут через AI-подписку (какую страну видит Google — показывает блок выше), всё остальное — через общую VPN.'),
			E('div', { 'class': 'gs-duo' }, [ aiSub, vpnSub ]),
			counters
		]);
		wrap.appendChild(hero);

		/* ===================== Subscriptions registry ===================== */
		function maskUrl(u) {
			if (!u) return '—';
			return String(u).replace(/\/[^/]*$/, '/...');
		}
		function subRow(s) {
			var isVpn = !!(st.vpn_sub && st.vpn_sub === s.id);
			var isAi = !!(st.ai_sub && st.ai_sub === s.id);
			var dest = '—';
			if (isVpn && isAi) dest = E('span', { 'class': 'gs-badge gs-on' }, 'VPN + AI');
			else if (isVpn) dest = E('span', { 'class': 'gs-badge gs-on' }, 'VPN');
			else if (isAi) dest = E('span', { 'class': 'gs-badge gs-ok' }, 'AI');
			var bV = { 'class': 'gs-btn gs-btn-use' + (isVpn ? ' gs-btn-vpn-on' : ''), 'title': isVpn ? 'Эта подписка используется для общего VPN' : 'Переключить общий VPN на эту подписку', 'click': function() { view.act(callVpnSubUse, { id: s.id }, 'Подписка применена для VPN'); } };
			var bA = { 'class': 'gs-btn gs-btn-use' + (isAi ? ' gs-btn-ai-on' : ''), 'title': isAi ? 'Эта подписка используется для AI-маршрутов' : 'Переключить AI-маршруты на эту подписку', 'click': function() { view.act(callAiSubUse, { id: s.id }, 'Подписка применена для AI'); } };
			var bD = { 'class': 'gs-btn gs-btn-warn', 'title': 'Убрать из реестра', 'click': function() { view.act(callSubDel, { id: s.id }, 'Подписка удалена'); } };
			return E('tr', {}, [
				E('td', {}, s.name || s.id),
				E('td', { 'class': 'gs-subs-url' }, maskUrl(s.url)),
				E('td', {}, dest),
				E('td', { 'class': 'gs-subs-actions' }, [
					E('button', bV, '→ VPN'),
					E('button', bA, '→ AI'),
					E('button', bD, 'Удалить')
				])
			]);
		}
		var subNameIn = E('input', { 'class': 'gs-input', 'style': 'width:150px', 'placeholder': 'Например: США' });
		var subUrlIn  = E('input', { 'class': 'gs-input', 'style': 'flex:1;min-width:260px', 'placeholder': 'https://... ссылка подписки' });
		var btnSubAdd = E('button', { 'class': 'gs-btn gs-btn-primary',
			'click': function() {
				var u = (subUrlIn.value || '').trim();
				if (!/^https?:\/\//.test(u)) {
					ui.addNotification(null, E('p', {}, 'Ссылка должна начинаться с http:// или https://'), 'warning');
					return;
				}
				btnSubAdd.disabled = true; btnSubAdd.textContent = 'Добавление…';
				callSubAdd({ name: subNameIn.value || '', url: u }).then(function(res) {
					btnSubAdd.disabled = false; btnSubAdd.textContent = 'Добавить';
					if (res && res.error) { ui.addNotification(null, E('p', {}, 'Ошибка: ' + res.error), 'error'); return; }
					subNameIn.value = ''; subUrlIn.value = '';
					ui.addNotification(null, E('p', {}, 'Подписка добавлена в реестр'), 'info');
					return view.reload();
				}).catch(function(e) {
					btnSubAdd.disabled = false; btnSubAdd.textContent = 'Добавить';
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
				});
			} }, 'Добавить');
		var btnSubSeed = E('button', { 'class': 'gs-btn',
			'title': 'Импортировать текущие подписки из конфига mihomo в реестр',
			'click': function() {
				btnSubSeed.disabled = true; btnSubSeed.textContent = 'Импорт…';
				callSubsSeed().then(function(res) {
					btnSubSeed.disabled = false; btnSubSeed.textContent = 'Подтянуть текущие из конфига';
					if (res && res.error) { ui.addNotification(null, E('p', {}, 'Ошибка: ' + res.error), 'error'); return; }
					ui.addNotification(null, E('p', {}, 'Текущие подписки добавлены в реестр'), 'info');
					return view.reload();
				}).catch(function(e) {
					btnSubSeed.disabled = false; btnSubSeed.textContent = 'Подтянуть текущие из конфига';
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
				});
			} }, 'Подтянуть текущие из конфига');
		var subsCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Подписки'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 8px' },
				'Реестр подписок. В колонке «Назначение» показано, чем подписка занята СЕЙЧАС; кнопки «→ VPN»/«→ AI» переключают соответствующий маршрут на эту подписку (активная кнопка неактивна). Переключение — через API mihomo, без перезапуска и обрыва соединений.'),
			E('table', { 'class': 'gs-table gs-subs' },
				[ E('tr', {}, [ E('th', {}, 'Имя'), E('th', {}, 'Ссылка'), E('th', {}, 'Назначение'), E('th', {}, '') ]) ]
					.concat(subs.map(subRow))),
			E('div', { 'class': 'gs-row', 'style': 'margin-top:10px' }, [ subNameIn, subUrlIn, btnSubAdd ]),
			E('div', { 'class': 'gs-row' }, [ btnSubSeed ])
		]);
		wrap.appendChild(subsCard);

		/* ===================== ACTIONS: sync / google check ===================== */
		var listsOut = E('div', {});
		var btnLists = E('button', { 'class': 'gs-btn gs-btn-primary',
			'click': function() {
				btnLists.disabled = true; btnLists.textContent = 'Синхронизация…';
				callLists().then(function(res) {
					btnLists.disabled = false; btnLists.textContent = 'Синхронизировать списки сейчас';
					var out = (res && res.output) ? res.output : '(нет вывода)';
					listsOut.textContent = out;
					ui.addNotification(null, E('p', {}, 'Списки синхронизированы: ИИ=' +
						(res && res.ai_rules) + ', VPN=' + (res && res.mt_rules)), 'info');
					return view.reload();
				}).catch(function(e) {
					btnLists.disabled = false; btnLists.textContent = 'Синхронизировать списки сейчас';
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
				});
			} }, 'Синхронизировать списки сейчас');

		var btnGoogle = E('button', { 'class': 'gs-btn',
			'click': function() {
				btnGoogle.disabled = true; btnGoogle.textContent = 'Проверка Google…';
				callGoogle().then(function(res) {
					btnGoogle.disabled = false; btnGoogle.textContent = 'Что видит Google?';
					var cc = res && res.google ? res.google : '(нет ответа)';
					ui.addNotification(null, E('p', {}, 'Google видит: ' + cc), 'info');
					return view.reload();
				}).catch(function(e) {
					btnGoogle.disabled = false; btnGoogle.textContent = 'Что видит Google?';
					ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
				});
			} }, 'Что видит Google?');

		var syncCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Синхронизация и проверка'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 6px' },
				'Списки «Google+AI to DE» в магитрикле автоматически уходят в mihomo каждые 5 минут. Кнопка ниже применяет изменения сразу, не дожидаясь cron.'),
			E('div', { 'class': 'gs-actions' }, [ btnLists, btnGoogle ]),
			listsOut
		]);
		wrap.appendChild(syncCard);

		/* ===================== Routing ===================== */
		var globalSeg = E('div', { 'class': 'gs-seg' }, [
			E('div', { 'class': st.global === 'DIRECT' ? 'gs-active' : '',
				'click': function() { view.act(callGlobal, { mode: 'DIRECT' }); } }, 'DIRECT'),
			E('div', { 'class': st.global === 'VPN' ? 'gs-active' : '',
				'click': function() { view.act(callGlobal, { mode: 'VPN' }); } }, 'VPN')
		]);
		var routeCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Маршрутизация'),
			E('div', { 'class': 'gs-row' }, [ E('span', { 'class': 'gs-label' }, 'Глобальный режим (трафик мимо списков)'),
				globalSeg ]),
			E('div', { 'class': 'gs-hint' }, 'DIRECT — мимо списков напрямую (локальные сервисы свободны). VPN — весь остаток через общую подписку.')
		]);
		wrap.appendChild(routeCard);

		/* ===================== IPv6 ===================== */
		var v6Card = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'IPv6'),
			E('div', { 'class': 'gs-row' }, [
				E('span', { 'class': 'gs-label' }, 'IPv6-стек (утечка реального IP мимо VPN)'),
				badgeKlass(st.ipv6 === 1, 'gs-warn', 'gs-ok', 'включён — УТЕЧКА', 'выключен'),
				E('button', { 'class': 'gs-btn' + (st.ipv6 ? ' gs-btn-warn' : ''),
					'click': function() { view.act(callIpv6, { state: st.ipv6 ? 'off' : 'on' }); } },
					st.ipv6 ? 'Отключить' : 'Включить')
			]),
			E('div', { 'class': 'gs-hint' }, 'Отключение снимает глобальные IPv6-адреса с роутера и LAN-устройств, утечка закрывается полностью.')
		]);
		wrap.appendChild(v6Card);

		/* ===================== Per-device geo policy ===================== */
		var MODEINFO = {
			bypass: { label: 'Мимо VPN', klass: 'gs-warn',
				hint: 'Реальный IP и часовой пояс ПК. Трафик напрямую или через zapret — mihomo не используется.' },
			realtz: { label: 'Через VPN, часовой пояс ПК свой', klass: 'gs-ok',
				hint: 'IP маскирован через mihomo, но часовой пояс самого ПК не трогается (GeoSync.ps1 на этом ПК выключен).' },
			ai: { label: 'Через AI-подписку', klass: 'gs-on',
				hint: 'ВЕСЬ трафик устройства идёт через AI-подписку (сейчас: Германия). Отдельный туннель AI, VPN не прерывается.' }
		};
		var modeSel = E('select', { 'class': 'gs-input', 'style': 'width:220px' }, [
			E('option', { 'value': 'bypass' }, 'Мимо VPN (реальный IP)'),
			E('option', { 'value': 'realtz' }, 'Через VPN, часовой пояс ПК свой'),
			E('option', { 'value': 'ai' }, 'Через AI-подписку (весь трафик)')
		]);
		var ipIn = E('input', { 'class': 'gs-input', 'style': 'width:130px', 'placeholder': 'IP' });
		var nameIn = E('input', { 'class': 'gs-input', 'style': 'width:150px', 'placeholder': 'Имя' });
		var leaseSel = E('select', { 'class': 'gs-input',
			'change': function(ev) {
				var parts = ev.target.value.split('|');
				if (parts[0]) ipIn.value = parts[0];
				if (parts[1]) nameIn.value = parts[2] || parts[1];
			} },
			[ E('option', { 'value': '' }, '— из DHCP-leases —') ].concat(
				leases.map(function(l) { return E('option', { 'value': l.ip + '|' + l.mac + '|' + (l.name || '') }, l.ip + '  ' + (l.name || l.mac)); })));
		var devRows = (st.devices || []).map(function(x) {
			var mi = MODEINFO[x.mode] || { label: x.mode || '—', klass: 'gs-badge', hint: '' };
			return E('tr', {}, [
				E('td', {}, x.ip),
				E('td', {}, x.name || ''),
				E('td', {}, E('span', { 'class': 'gs-badge ' + mi.klass, 'title': mi.hint }, mi.label)),
				E('td', {}, E('button', { 'class': 'gs-btn gs-btn-warn',
					'click': function() { view.act(callDevDel, { ip: x.ip }); } }, 'Убрать'))
			]);
		});
		var devTable = E('table', { 'class': 'gs-table' },
			[ E('tr', {}, [ E('th', {}, 'IP'), E('th', {}, 'Имя'), E('th', {}, 'Режим'), E('th', {}, '') ]) ].concat(devRows));
		var devCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Персональные режимы устройств'),
			E('div', { 'class': 'gs-hint', 'style': 'margin:0 0 8px' },
				'По умолчанию все устройства маскируются: IP идёт через mihomo, а часовой пояс каждого ПК настраивает его ОС. ' +
				'Здесь задаются исключения: мимо VPN / через VPN / весь трафик через AI-подписку' +
				(st.n_ai != null ? ' (сейчас через AI: ' + st.n_ai + ', туннель AI: ' + (st.ai_tun ? 'поднят' : 'нет') + ')' : '') + '.'),
			(st.ai_tun && !st.ai_listener) ? E('div', { 'class': 'gs-hint', 'style': 'color:#d29922' },
				'Внимание: слушатель AI-устройств (порт 7891) не активен — перезапустите mihomo один раз, когда будет удобно: sh /etc/init.d/mihomo restart. До этого устройства с режимом «Через AI» не получат трафик.') : E('div', {}),
			devTable,
			E('div', { 'class': 'gs-row', 'style': 'margin-top:10px' }, [ leaseSel, modeSel ]),
			E('div', { 'class': 'gs-row' }, [ ipIn, nameIn,
				E('button', { 'class': 'gs-btn', 'click': function() {
					if (!ipIn.value) { ui.addNotification(null, E('p', {}, 'Укажите IP'), 'warning'); return; }
					view.act(callDevAdd, { ip: ipIn.value, mode: modeSel.value, mac: (leaseSel.value.split('|')[1] || ''), name: nameIn.value || ipIn.value });
					ipIn.value = ''; nameIn.value = ''; leaseSel.value = '';
				} }, 'Добавить')
			])
		]);
		wrap.appendChild(devCard);

		/* ===================== Log ===================== */
		var logCard = E('div', { 'class': 'gs-card' }, [
			E('h3', {}, 'Журнал'),
			E('pre', { 'class': 'gs-log' }, lg || '(пусто)')
		]);
		wrap.appendChild(logCard);

		return wrap;
	},

	act: function(rpcFn, params, okMsg) {
		var view = this;
		return rpcFn(params).then(function(res) {
			if (res && res.error) { ui.addNotification(null, E('p', {}, 'Ошибка: ' + res.error), 'error'); return; }
			if (okMsg) ui.addNotification(null, E('p', {}, okMsg), 'info');
			return view.reload();
		}).catch(function(e) {
			ui.addNotification(null, E('p', {}, 'Ошибка: ' + e), 'error');
		});
	},

	reload: function() {
		var view = this;
		return Promise.all([ callStatus(), callLeases(), callLog(), callAiSubGet(), callSubsList() ]).then(function(all) {
			var newView = view.render(all);
			var old = document.querySelector('.gs-wrap');
			if (old && old.parentNode) old.parentNode.replaceChild(newView, old);
		});
	}
});

ASSET_EOF
  info "reloading rpcd + luci menu..."
  initd rpcd reload 2>/dev/null || true
  sleep 1
  return 0
}
step2_verify() { step2_check; }

# ============================================================ STEP 3: mihomo 2-subscription config
MIHOMO_CONF=/etc/mihomo/config.yaml

step3_check() {
  _need=0
  if ! have mihomo; then bad "mihomo binary missing"; return 1; fi
  _c="$(P $MIHOMO_CONF)"
  if [ ! -s "$_c" ]; then bad "$MIHOMO_CONF missing"; return 1; fi
  _np=$(grep -c 'type: http' "$_c" 2>/dev/null || echo 0)
  if [ "$_np" -ge 2 ] 2>/dev/null; then info "proxy-providers: $_np (need >=2)"; else bad "proxy-providers: $_np (need 2: VPN + AI)"; _need=1; fi
  for g in VPN AI; do
    if grep -q "name: $g$" "$_c" 2>/dev/null; then info "group $g: present"; else bad "group $g: missing"; _need=1; fi
  done
  grep -q 'RULE-SET,ai,AI' "$_c" 2>/dev/null && info "rule ai->AI: present" || { bad "rule ai->AI missing"; _need=1; }
  grep -q 'RULE-SET,mtrickle,VPN' "$_c" 2>/dev/null && info "rule mtrickle->VPN: present" || { bad "rule mtrickle->VPN missing"; _need=1; }
  grep -q 'ai-devices' "$_c" 2>/dev/null && info "listeners (ai-devices): present" || { bad "listeners (ai-devices:7891) missing"; _need=1; }
  for rf in /etc/mihomo/ruleset/mtrickle.yaml /etc/mihomo/ruleset/ai.yaml /etc/mihomo/ruleset/ytdirect.yaml; do
    if [ -s "$(P "$rf")" ]; then info "ruleset: $rf ($(grep -c 'DOMAIN-SUFFIX' "$(P "$rf")" 2>/dev/null || echo '?') suffixes)"; else warn "ruleset empty/missing: $rf (sync step will create)"; fi
  done
  if mihomo -d /etc/mihomo -t >/dev/null 2>&1; then info "mihomo -t: PASS"; else bad "mihomo -t: FAIL"; _need=1; fi
  return $_need
}
step3_apply() {
  _c="$(P $MIHOMO_CONF)"
  _vpnu="$GM_VPN_URL"; _aiu="$GM_AI_URL"
  if [ -s "$_c" ]; then
    [ -z "$_vpnu" ] && _vpnu=$(_prov_url "$_c" sub)
    [ -z "$_aiu" ]  && _aiu=$(_prov_url "$_c" de5)
  fi
  if [ -z "$_vpnu" ] || [ -z "$_aiu" ]; then
    bad "subscription URLs unknown - pass --vpn-url and --ai-url (or keep existing config)"
    return 1
  fi
  info "VPN provider URL: $(_maskurl "$_vpnu")"
  info "AI  provider URL: $(_maskurl "$_aiu")"
  ask "  Write mihomo config with these 2 providers (backup kept)?" || { warn "skipped"; return 1; }
  write_mihomo_config "$_vpnu" "$_aiu" || return 1
  if [ "$GM_RESTART" = 1 ]; then
    safe_mihomo_restart || true
  elif api_refresh_provider sub "$_vpnu" && api_refresh_provider de5 "$_aiu"; then
    info "providers refreshed via API (no restart, VPN stays up)"
  elif svc_alive mihomo; then
    warn "mihomo is running: restart later to apply (rerun with --restart)"
  else
    info "starting mihomo..."
    initd mihomo start >/dev/null 2>&1 || true
  fi
  return 0
}
step3_verify() { step3_check; }

# write a fresh 2-provider mihomo config: write_mihomo_config <vpn_url> <ai_url>
# validates with `mihomo -t`, keeps backup of the previous config; no restart here.
write_mihomo_config() {
  _c="$(P $MIHOMO_CONF)"
  _vpnu="$1"; _aiu="$2"
  # --- existing 2-subscription config: keep it as-is (custom rules, headers, sniffer)
  #     and only patch provider urls / ensure the ai-devices listener ---
  if [ -s "$_c" ] && grep -q '^  sub:' "$_c" && grep -q '^  de5:' "$_c" \
     && grep -q 'name: VPN' "$_c" && grep -q 'name: AI' "$_c"; then
    _curv=$(_prov_url "$_c" sub)
    _cura=$(_prov_url "$_c" de5)
    if [ -n "${_vpnu:-}" ] && [ "$_vpnu" != "$_curv" ]; then _swap=1; fi
    if [ -n "${_aiu:-}" ] && [ "$_aiu" != "$_cura" ]; then _swap=1; fi
    if [ "${_swap:-0}" = 1 ]; then
      cp -p "$_c" "$_c.bak_geomask_$(date +%s)" || true
      if ! awk -v pn="^  $([ "$_vpnu" != "$_curv" ] && echo sub || echo de5):" -v nu="$([ "$_vpnu" != "$_curv" ] && printf '%s' "$_vpnu" || printf '%s' "$_aiu")" '
        BEGIN{f=0;done=0}
        $0 ~ pn {f=1; print; next}
        f && /^    url:/{print "    url: \"" nu "\""; done=1; next}
        f && /^  [A-Za-z0-9_-]+:/{f=0}
        f && /^[A-Za-z]/{f=0}
        {print}
        END{exit done?0:1}' "$_c" > "$_c.urlnew"; then
        rm -f "$_c.urlnew"; bad "provider url patch failed"; return 1
      fi
      mihomo -d /etc/mihomo -t >/dev/null 2>&1 && mv -f "$_c.urlnew" "$_c" || { rm -f "$_c.urlnew"; bad "mihomo -t failed - config kept"; return 1; }
      info "provider url(s) updated in place (sub=$( [ "$_vpnu" != "$_curv" ] && echo sub || echo de5))"
    fi
    if ! grep -q 'ai-devices' "$_c"; then
      cp -p "$_c" "$_c.bak_geomask_$(date +%s)" || true
      awk 'BEGIN{d=0}
        /^proxy-providers:/ && !d {print "listeners:";print "  - name: ai-devices";print "    type: mixed";print "    port: 7891";print "    listen: 127.0.0.1";print "    proxy: AI";print "";d=1}
        {print}' "$_c" > "$_c.zmnew" || return 1
      if mihomo -d /etc/mihomo -t >/dev/null 2>&1; then
        mv -f "$_c.zmnew" "$_c"; info "listeners block added to existing config (custom rules preserved)"
      else
        rm -f "$_c.zmnew"; bad "mihomo -t failed after adding listeners - config kept"; return 1
      fi
    else
      info "existing 2-subscription config kept as-is (ai-devices already present)"
    fi
    return 0
  fi
  _hwid=$(grep -A1 'x-hwid' "$_c" 2>/dev/null | sed -n 's/^[[:space:]]*-[[:space:]]*"\(.*\)"/\1/p' | head -1)
  _tmpconf="$_c.zmnew"
  {
    printf '%s\n' 'mixed-port: 7890'
    printf '%s\n' 'allow-lan: true'
    printf '%s\n' 'bind-address: "0.0.0.0"'
    printf '%s\n' 'tcp-concurrent: true'
    printf '%s\n' 'mode: rule'
    printf '%s\n' 'log-level: info'
    printf '%s\n' 'ipv6: true'
    printf '%s\n' 'external-controller: 0.0.0.0:9090'
    printf '%s\n' 'external-ui: ui'
    printf '%s\n' 'unified-delay: true'
    printf '%s\n' 'find-process-mode: off'
    printf '%s\n' 'profile:'
    printf '%s\n' '  store-selected: true'
    printf '%s\n' '  store-fake-ip: true'
    printf '\n'
    printf '%s\n' 'listeners:'
    printf '%s\n' '  - name: ai-devices'
    printf '%s\n' '    type: mixed'
    printf '%s\n' '    port: 7891'
    printf '%s\n' '    listen: 127.0.0.1'
    printf '%s\n' '    proxy: AI'
    printf '\n'
    printf '%s\n' 'proxy-providers:'
    printf '%s\n' '  sub:'
    printf '%s\n' '    type: http'
    printf '%s\n' '    proxy: DIRECT'
    if [ -n "${_hwid:-}" ]; then
      printf '%s\n' '    header:'
      printf '%s\n' '      x-hwid:'
      printf '%s\n' "        - \"$_hwid\""
    fi
    printf '%s\n' "    url: \"$_vpnu\""
    printf '%s\n' '    interval: 3600'
    printf '%s\n' '    health-check:'
    printf '%s\n' '      enable: true'
    printf '%s\n' '      interval: 180'
    printf '%s\n' '      url: "https://www.gstatic.com/generate_204"'
    printf '%s\n' '      expected-status: 204'
    printf '%s\n' '  de5:'
    printf '%s\n' '    type: http'
    printf '%s\n' '    proxy: DIRECT'
    printf '%s\n' "    url: \"$_aiu\""
    printf '%s\n' '    interval: 3600'
    printf '%s\n' '    health-check:'
    printf '%s\n' '      enable: true'
    printf '%s\n' '      interval: 180'
    printf '%s\n' '      url: "https://www.gstatic.com/generate_204"'
    printf '%s\n' '      expected-status: 204'
    printf '\n'
    printf '%s\n' 'proxy-groups:'
    printf '%s\n' '  - name: VPN'
    printf '%s\n' '    type: url-test'
    printf '%s\n' '    use:'
    printf '%s\n' '      - sub'
    printf '%s\n' '    exclude-filter: "Россия|Мобильный|РФ|Авто|Russia|Mobile|Auto|Албания|Хорватия|Литва|Латвия|Дания"'
    printf '%s\n' '    url: "https://www.gstatic.com/generate_204"'
    printf '%s\n' '    interval: 180'
    printf '%s\n' '    tolerance: 50'
    printf '%s\n' '    lazy: false'
    printf '%s\n' '  - name: AI'
    printf '%s\n' '    type: url-test'
    printf '%s\n' '    use:'
    printf '%s\n' '      - de5'
    printf '%s\n' '    url: "https://www.gstatic.com/generate_204"'
    printf '%s\n' '    interval: 180'
    printf '%s\n' '    tolerance: 50'
    printf '%s\n' '    lazy: false'
    printf '\n'
    printf '%s\n' 'rule-providers:'
    printf '%s\n' '  ai:'
    printf '%s\n' '    type: file'
    printf '%s\n' '    behavior: classical'
    printf '%s\n' '    path: /etc/mihomo/ruleset/ai.yaml'
    printf '%s\n' '  mtrickle:'
    printf '%s\n' '    type: file'
    printf '%s\n' '    behavior: classical'
    printf '%s\n' '    path: /etc/mihomo/ruleset/mtrickle.yaml'
    printf '%s\n' '  ytdirect:'
    printf '%s\n' '    type: file'
    printf '%s\n' '    behavior: classical'
    printf '%s\n' '    path: /etc/mihomo/ruleset/ytdirect.yaml'
    printf '\n'
    printf '%s\n' 'rules:'
    printf '%s\n' '  # AUTO-SYNC from magitrickle: /root/sync_mtrickle_mihomo.sh - DO NOT EDIT MANUALLY'
    printf '%s\n' '  - RULE-SET,ytdirect,DIRECT'
    printf '%s\n' '  - DOMAIN-SUFFIX,cloudflare-ech.com,AI'
    printf '%s\n' '  - RULE-SET,ai,AI'
    printf '%s\n' '  - RULE-SET,mtrickle,VPN'
    printf '%s\n' '  - MATCH,GLOBAL'
    printf '\n'
    printf '%s\n' 'sniffer:'
    printf '%s\n' '  enable: true'
    printf '%s\n' '  parse-pure-ip: true'
    printf '%s\n' '  override-destination: true'
    printf '%s\n' '  sniff:'
    printf '%s\n' '    HTTP:'
    printf '%s\n' '      ports: [80, 8080-8880]'
    printf '%s\n' '    TLS:'
    printf '%s\n' '      ports: [443, 8443]'
    printf '%s\n' '    QUIC:'
    printf '%s\n' '      ports: [443, 8443]'
  } > "$_tmpconf" || { bad "cannot write $_tmpconf"; return 1; }
  # ruleset files must exist before mihomo -t
  mkdir -p "$(P /etc/mihomo/ruleset)"
  for rf in mtrickle ai ytdirect; do
    [ -s "$(P /etc/mihomo/ruleset/$rf.yaml)" ] || printf 'payload: []\n' > "$(P /etc/mihomo/ruleset/$rf.yaml)"
  done
  if mihomo -d /etc/mihomo -t >/dev/null 2>&1; then
    info "mihomo -t on new config: PASS"
  else
    bad "mihomo -t FAILED on new config - keeping old"; rm -f "$_tmpconf"; return 1
  fi
  if [ -f "$_c" ]; then cp -p "$_c" "$_c.bak_geomask_$(date +%s 2>/dev/null || echo bak)"; fi
  mv -f "$_tmpconf" "$_c" || { bad "cannot install new config"; return 1; }
  info "config installed"
  return 0
}

# runtime provider refresh through mihomo API - NO restart, VPN stays up
api_refresh_provider() { # api_refresh_provider <name> <url>
  [ -n "${2:-}" ] || return 1
  # re-read config from file (re-fetches provider subscription)
  curl -s -m15 -f -X PUT "http://127.0.0.1:9090/configs" \
    -H 'Content-Type: application/json' \
    -d "{\"path\":\"/etc/mihomo/config.yaml\"}" >/dev/null 2>&1
}

# ============================================================ SAFETY layer
# full settings backup (restorable via LuCI) - MANDATORY before any router change
ensure_backup() { # -> path on stdout, rc=1 on failure
  _ts=$(date +%Y%m%d-%H%M%S 2>/dev/null || echo bak)
  _bkdir="$(P /root/geomask-backups)"
  mkdir -p "$_bkdir" 2>/dev/null
  _out="$_bkdir/router-backup-$_ts.tar.gz"
  if have sysupgrade; then
    if ! sysupgrade -b "$_out" >/dev/null 2>&1 || [ ! -s "$_out" ]; then _out=""; fi
  else
    _out=""
  fi
  if [ -z "$_out" ] || [ ! -s "$_out" ]; then
    _out="$_bkdir/router-backup-$_ts-min.tar.gz"
    tar czf "$_out" -C "$(P /)" etc 2>/dev/null || { _out=""; }
  fi
  if [ -z "$_out" ] || [ ! -s "$_out" ]; then
    bad "cannot create backup"
    return 1
  fi
  printf '%s' "$_out"
  return 0
}

# dry-run: start a COPY of the mihomo config on shifted ports in a temp dir.
# rc=0 only if mihomo actually starts with the new config. Never touches live service.
mihomo_trial_run() {
  _c="$(P $MIHOMO_CONF)"
  [ -s "$_c" ] || { bad "config missing for trial"; return 1; }
  _td="$(P /tmp)/gm-trial-$$"
  rm -rf "$_td"; mkdir -p "$_td"
  cp -a "$(dirname "$_c")/." "$_td/" 2>/dev/null || cp -a "$(P /etc/mihomo)" "$_td" 2>/dev/null || {
    warn "trial: cannot copy mihomo dir"; rm -rf "$_td"; return 1; }
  sed -i -e 's/^mixed-port:.*/mixed-port: 17890/' \
         -e 's/^external-controller:.*/external-controller: 0.0.0.0:19090/' \
         -e 's/^[[:space:]]*port: 7891/    port: 17901/' \
         -e "s|path: /etc/mihomo/|path: $_td/|g" "$_td/config.yaml" 2>/dev/null
  mihomo -d "$_td" -f config.yaml > "$_td/trial.log" 2>&1 &
  _tp=$!
  sleep 3
  if kill -0 "$_tp" 2>/dev/null; then
    info "trial run: OK (config starts, no restart done)"
    kill "$_tp" 2>/dev/null
    wait "$_tp" 2>/dev/null
    rm -rf "$_td"
    return 0
  fi
  bad "trial run FAILED - live mihomo NOT touched"
  sed 's/^/    | /' "$_td/trial.log" 2>/dev/null | tail -5
  rm -rf "$_td"
  return 1
}

# restart mihomo and verify it really came back (pid + API). NO silent failures.
mihomo_restart_verified() {
  initd mihomo restart >/dev/null 2>&1
  sleep 3
  if ! svc_alive mihomo; then
    bad "mihomo did NOT come back after restart!"
    return 1
  fi
  if ! curl -s -m3 "http://127.0.0.1:9090/version" 2>/dev/null | grep -qi 'mihomo\|meta'; then
    bad "mihomo API not responding after restart!"
    return 1
  fi
  info "mihomo restarted and verified (pid $(pidof mihomo))"
  return 0
}

# full safe mihomo restart: backup -> trial -> restart -> verify -> rollback on failure
safe_mihomo_restart() {
  _c="$(P $MIHOMO_CONF)"
  if [ "$GM_RESTART" != 1 ]; then
    warn "restart skipped (no --restart); will apply at next mihomo start"
    return 0
  fi
  _bk=$(ensure_backup) || { bad "no backup - restart ABORTED"; return 1; }
  info "backup: $_bk"
  cp -p "$_c" "$_c.pre-restart" 2>/dev/null || true
  if ! mihomo_trial_run; then
    bad "trial failed - live mihomo NOT restarted; config left for manual review"
    return 1
  fi
  if mihomo_restart_verified; then
    info "restart verified OK"
    return 0
  fi
  bad "restart FAILED - rolling back config and re-starting"
  if [ -s "$_c.pre-restart" ]; then
    cp -p "$_c.pre-restart" "$_c"
    /etc/init.d/mihomo restart >/dev/null 2>&1
    sleep 3
    if svc_alive mihomo; then
      info "rollback OK: previous config restored, mihomo running"
    else
      bad "rollback FAILED - RESTORE FROM BACKUP: $_bk"
    fi
  else
    bad "no pre-restart copy; RESTORE FROM BACKUP: $_bk"
  fi
  return 1
}

# ============================================================ STEP 4: magitrickle -> mihomo sync
step4_check() {
  _need=0
  _s="$(P /root/sync_mtrickle_mihomo.sh)"
  if [ -s "$_s" ]; then
    info "sync script: present"
    [ -x "$_s" ] || warn "sync script not executable (chmod +x on router)"
  else
    bad "sync script missing: /root/sync_mtrickle_mihomo.sh"; _need=1
  fi
  if crontab -l 2>/dev/null | grep -q 'sync_mtrickle_mihomo'; then info "cron */5: present"; else bad "cron entry missing"; _need=1; fi
  svc_alive magitrickled && info "magitrickle: running" || { bad "magitrickle not running"; _need=1; }
  return $_need
}
step4_apply() {
  save_stream /root/sync_mtrickle_mihomo.sh 755 <<'ASSET_EOF'
#!/bin/sh
# Sync magitrickle (enabled groups with interface Mihomo) -> TWO mihomo rule-provider files:
#   /etc/mihomo/ruleset/mtrickle.yaml  (non-AI groups)  -> group VPN (sub #1 vlessi)
#   /etc/mihomo/ruleset/ai.yaml        (Google/AI)      -> group AI (sub #2 de5, Germany)
# AI detection: whole groups by name + domain patterns (AI domains live inside Geoblock too).
# Reloads mihomo only on change.
exec python3 - <<'PYEOF'
import os, re, json, sys, urllib.request

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
    try:
        req = urllib.request.Request(
            'http://127.0.0.1:9090/configs?force=true',
            data=json.dumps({'path': '/etc/mihomo/config.yaml'}).encode(),
            method='PUT', headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5).read()
        print('mihomo reloaded')
    except Exception as e:
        print('reload FAILED: %s (restart needed)' % e)
PYEOF

ASSET_EOF
  save_stream /etc/mihomo/ruleset/ytdirect.yaml 644 <<'ASSET_EOF'
# AUTO-GENERATED from magitrickle Youtube group - YouTube DIRECT ruleset - DO NOT EDIT
payload:
  - DOMAIN-SUFFIX,cdn.youtube.com
  - DOMAIN-SUFFIX,googlevideo.com
  - DOMAIN-SUFFIX,i.ytimg.com
  - DOMAIN-SUFFIX,i9.ytimg.com
  - DOMAIN-SUFFIX,kids.youtube.com
  - DOMAIN-SUFFIX,m.youtube.com
  - DOMAIN-SUFFIX,manifest.googlevideo.com
  - DOMAIN-SUFFIX,music.youtube.com
  - DOMAIN-SUFFIX,nhacmp3youtube.com
  - DOMAIN-SUFFIX,returnyoutubedislikeapi.com
  - DOMAIN-SUFFIX,s.ytimg.com
  - DOMAIN-SUFFIX,signaler-pa.youtube.com
  - DOMAIN-SUFFIX,studio.youtube.com
  - DOMAIN-SUFFIX,tv.youtube.com
  - DOMAIN-SUFFIX,wide-youtube.l.google.com
  - DOMAIN-SUFFIX,withyoutube.com
  - DOMAIN-SUFFIX,youtu.be
  - DOMAIN-SUFFIX,youtube-nocookie.com
  - DOMAIN-SUFFIX,youtube-ui.l.google.com
  - DOMAIN-SUFFIX,youtube.ae
  - DOMAIN-SUFFIX,youtube.al
  - DOMAIN-SUFFIX,youtube.am
  - DOMAIN-SUFFIX,youtube.at
  - DOMAIN-SUFFIX,youtube.az
  - DOMAIN-SUFFIX,youtube.ba
  - DOMAIN-SUFFIX,youtube.be
  - DOMAIN-SUFFIX,youtube.bg
  - DOMAIN-SUFFIX,youtube.bh
  - DOMAIN-SUFFIX,youtube.bo
  - DOMAIN-SUFFIX,youtube.by
  - DOMAIN-SUFFIX,youtube.ca
  - DOMAIN-SUFFIX,youtube.cat
  - DOMAIN-SUFFIX,youtube.ch
  - DOMAIN-SUFFIX,youtube.cl
  - DOMAIN-SUFFIX,youtube.co
  - DOMAIN-SUFFIX,youtube.co.ae
  - DOMAIN-SUFFIX,youtube.co.at
  - DOMAIN-SUFFIX,youtube.co.cr
  - DOMAIN-SUFFIX,youtube.co.hu
  - DOMAIN-SUFFIX,youtube.co.id
  - DOMAIN-SUFFIX,youtube.co.il
  - DOMAIN-SUFFIX,youtube.co.in
  - DOMAIN-SUFFIX,youtube.co.jp
  - DOMAIN-SUFFIX,youtube.co.ke
  - DOMAIN-SUFFIX,youtube.co.kr
  - DOMAIN-SUFFIX,youtube.co.ma
  - DOMAIN-SUFFIX,youtube.co.nz
  - DOMAIN-SUFFIX,youtube.co.th
  - DOMAIN-SUFFIX,youtube.co.tz
  - DOMAIN-SUFFIX,youtube.co.ug
  - DOMAIN-SUFFIX,youtube.co.uk
  - DOMAIN-SUFFIX,youtube.co.ve
  - DOMAIN-SUFFIX,youtube.co.za
  - DOMAIN-SUFFIX,youtube.co.zw
  - DOMAIN-SUFFIX,youtube.com
  - DOMAIN-SUFFIX,youtube.com.ar
  - DOMAIN-SUFFIX,youtube.com.au
  - DOMAIN-SUFFIX,youtube.com.az
  - DOMAIN-SUFFIX,youtube.com.bd
  - DOMAIN-SUFFIX,youtube.com.bh
  - DOMAIN-SUFFIX,youtube.com.bo
  - DOMAIN-SUFFIX,youtube.com.br
  - DOMAIN-SUFFIX,youtube.com.by
  - DOMAIN-SUFFIX,youtube.com.co
  - DOMAIN-SUFFIX,youtube.com.do
  - DOMAIN-SUFFIX,youtube.com.ec
  - DOMAIN-SUFFIX,youtube.com.ee
  - DOMAIN-SUFFIX,youtube.com.eg
  - DOMAIN-SUFFIX,youtube.com.es
  - DOMAIN-SUFFIX,youtube.com.gh
  - DOMAIN-SUFFIX,youtube.com.gr
  - DOMAIN-SUFFIX,youtube.com.gt
  - DOMAIN-SUFFIX,youtube.com.hk
  - DOMAIN-SUFFIX,youtube.com.hn
  - DOMAIN-SUFFIX,youtube.com.hr
  - DOMAIN-SUFFIX,youtube.com.jm
  - DOMAIN-SUFFIX,youtube.com.jo
  - DOMAIN-SUFFIX,youtube.com.kw
  - DOMAIN-SUFFIX,youtube.com.lb
  - DOMAIN-SUFFIX,youtube.com.lv
  - DOMAIN-SUFFIX,youtube.com.ly
  - DOMAIN-SUFFIX,youtube.com.mk
  - DOMAIN-SUFFIX,youtube.com.mt
  - DOMAIN-SUFFIX,youtube.com.mx
  - DOMAIN-SUFFIX,youtube.com.my
  - DOMAIN-SUFFIX,youtube.com.ng
  - DOMAIN-SUFFIX,youtube.com.ni
  - DOMAIN-SUFFIX,youtube.com.om
  - DOMAIN-SUFFIX,youtube.com.pa
  - DOMAIN-SUFFIX,youtube.com.pe
  - DOMAIN-SUFFIX,youtube.com.ph
  - DOMAIN-SUFFIX,youtube.com.pk
  - DOMAIN-SUFFIX,youtube.com.pt
  - DOMAIN-SUFFIX,youtube.com.py
  - DOMAIN-SUFFIX,youtube.com.qa
  - DOMAIN-SUFFIX,youtube.com.ro
  - DOMAIN-SUFFIX,youtube.com.sa
  - DOMAIN-SUFFIX,youtube.com.sg
  - DOMAIN-SUFFIX,youtube.com.sv
  - DOMAIN-SUFFIX,youtube.com.tn
  - DOMAIN-SUFFIX,youtube.com.tr
  - DOMAIN-SUFFIX,youtube.com.tw
  - DOMAIN-SUFFIX,youtube.com.ua
  - DOMAIN-SUFFIX,youtube.com.uy
  - DOMAIN-SUFFIX,youtube.com.ve
  - DOMAIN-SUFFIX,youtube.cr
  - DOMAIN-SUFFIX,youtube.cz
  - DOMAIN-SUFFIX,youtube.de
  - DOMAIN-SUFFIX,youtube.dk
  - DOMAIN-SUFFIX,youtube.ee
  - DOMAIN-SUFFIX,youtube.es
  - DOMAIN-SUFFIX,youtube.fi
  - DOMAIN-SUFFIX,youtube.fr
  - DOMAIN-SUFFIX,youtube.ge
  - DOMAIN-SUFFIX,youtube.googleapis.com
  - DOMAIN-SUFFIX,youtube.gr
  - DOMAIN-SUFFIX,youtube.gt
  - DOMAIN-SUFFIX,youtube.hk
  - DOMAIN-SUFFIX,youtube.hr
  - DOMAIN-SUFFIX,youtube.hu
  - DOMAIN-SUFFIX,youtube.ie
  - DOMAIN-SUFFIX,youtube.in
  - DOMAIN-SUFFIX,youtube.iq
  - DOMAIN-SUFFIX,youtube.is
  - DOMAIN-SUFFIX,youtube.it
  - DOMAIN-SUFFIX,youtube.jo
  - DOMAIN-SUFFIX,youtube.jp
  - DOMAIN-SUFFIX,youtube.kr
  - DOMAIN-SUFFIX,youtube.kz
  - DOMAIN-SUFFIX,youtube.la
  - DOMAIN-SUFFIX,youtube.lk
  - DOMAIN-SUFFIX,youtube.lt
  - DOMAIN-SUFFIX,youtube.lu
  - DOMAIN-SUFFIX,youtube.lv
  - DOMAIN-SUFFIX,youtube.ly
  - DOMAIN-SUFFIX,youtube.ma
  - DOMAIN-SUFFIX,youtube.md
  - DOMAIN-SUFFIX,youtube.me
  - DOMAIN-SUFFIX,youtube.mk
  - DOMAIN-SUFFIX,youtube.mn
  - DOMAIN-SUFFIX,youtube.mx
  - DOMAIN-SUFFIX,youtube.my
  - DOMAIN-SUFFIX,youtube.ng
  - DOMAIN-SUFFIX,youtube.ni
  - DOMAIN-SUFFIX,youtube.nl
  - DOMAIN-SUFFIX,youtube.no
  - DOMAIN-SUFFIX,youtube.pa
  - DOMAIN-SUFFIX,youtube.pe
  - DOMAIN-SUFFIX,youtube.ph
  - DOMAIN-SUFFIX,youtube.pk
  - DOMAIN-SUFFIX,youtube.pl
  - DOMAIN-SUFFIX,youtube.pr
  - DOMAIN-SUFFIX,youtube.pt
  - DOMAIN-SUFFIX,youtube.qa
  - DOMAIN-SUFFIX,youtube.ro
  - DOMAIN-SUFFIX,youtube.rs
  - DOMAIN-SUFFIX,youtube.ru
  - DOMAIN-SUFFIX,youtube.sa
  - DOMAIN-SUFFIX,youtube.se
  - DOMAIN-SUFFIX,youtube.sg
  - DOMAIN-SUFFIX,youtube.si
  - DOMAIN-SUFFIX,youtube.sk
  - DOMAIN-SUFFIX,youtube.sn
  - DOMAIN-SUFFIX,youtube.soy
  - DOMAIN-SUFFIX,youtube.sv
  - DOMAIN-SUFFIX,youtube.tn
  - DOMAIN-SUFFIX,youtube.tv
  - DOMAIN-SUFFIX,youtube.ua
  - DOMAIN-SUFFIX,youtube.ug
  - DOMAIN-SUFFIX,youtube.uy
  - DOMAIN-SUFFIX,youtube.vn
  - DOMAIN-SUFFIX,youtubeeducation.com
  - DOMAIN-SUFFIX,youtubeembeddedplayer.googleapis.com
  - DOMAIN-SUFFIX,youtubefanfest.com
  - DOMAIN-SUFFIX,youtubegaming.com
  - DOMAIN-SUFFIX,youtubego.co.id
  - DOMAIN-SUFFIX,youtubego.co.in
  - DOMAIN-SUFFIX,youtubego.com
  - DOMAIN-SUFFIX,youtubego.com.br
  - DOMAIN-SUFFIX,youtubego.id
  - DOMAIN-SUFFIX,youtubego.in
  - DOMAIN-SUFFIX,youtubei.googleapis.com
  - DOMAIN-SUFFIX,youtubei.youtube.com
  - DOMAIN-SUFFIX,youtubekids.com
  - DOMAIN-SUFFIX,youtubemobilesupport.com
  - DOMAIN-SUFFIX,yt.be
  - DOMAIN-SUFFIX,yt3.ggpht.com
  - DOMAIN-SUFFIX,yt3.googleusercontent.com
  - DOMAIN-SUFFIX,yt4.ggpht.com
  - DOMAIN-SUFFIX,ytimg.com
  - DOMAIN-SUFFIX,ytimg.l.google.com

ASSET_EOF
  save_stream /etc/hotplug.d/iface/99-default-vpn-route 755 <<'ASSET_EOF'
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
  # --- ICMP bypass: 32610 must stay BELOW the magitrickle fwmark rule base
  #     (~32645, grows upward with group count) or ping to a marked IP dies
  #     inside the TUN. Direct via pppoe-wan. ---
  ip rule show | grep -q '32610:' || ip rule add priority 32610 iif br-lan ipproto icmp lookup main
  # --- per-device rules: 32611 < magitrickle fwmark base ~32645, so a device
  #     rule is never preempted by magitrickle marking; LAN stays local ---
  # --- bypass-mode devices (32611 vs fwmark rules 32645+) ---
  if [ -f /etc/geo-devices.list ]; then
    while read -r dip dmode dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      [ "$dmode" = "bypass" ] || continue
      ip rule show | grep -q "from $dip" || ip rule add priority 32611 from "$dip" lookup main
    done < /etc/geo-devices.list
    # --- ai-mode devices (32611 vs fwmark rules 32645+; LAN stays local in table 101) ---
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
           ip rule show | grep -q "from $dip" || ip rule add priority 32611 from "$dip" lookup 101
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

ASSET_EOF
  if crontab -l 2>/dev/null | grep -q 'sync_mtrickle_mihomo'; then
    info "cron already present"
  else
    ( crontab -l 2>/dev/null; echo '*/5 * * * * /root/sync_mtrickle_mihomo.sh' ) | crontab - 2>/dev/null && info "cron added: */5 sync" || warn "cannot set crontab"
  fi
  if ! svc_alive magitrickled; then
    info "starting magitrickle..."
    initd magitrickle start >/dev/null 2>&1 || true
  fi
  if [ "$GM_RESTART" = 1 ]; then
    info "running sync once (--restart)..."
    sh "$(P /root/sync_mtrickle_mihomo.sh)" 2>&1 | sed 's/^/  | /' || true
  else
    warn "sync not run (it may restart mihomo). Run later: sh /root/sync_mtrickle_mihomo.sh  or use --restart"
  fi
  return 0
}
step4_verify() { step4_check; }

# ============================================================ STEP 5: services (start missing only)
SVC_LIST="dnsmasq magitrickled mihomo hev-socks5-tunnel rpcd"
step5_check() {
  _need=0
  for s in $SVC_LIST; do
    case "$s" in
      magitrickled) _p=magitrickled ;;
      *) _p=$s ;;
    esac
    svc_alive "$_p" && info "$s: running" || { warn "$s: NOT running"; _need=1; }
  done
  return $_need
}
step5_apply() {
  for s in $SVC_LIST; do
    _p=$s; [ "$s" = magitrickled ] && _p=magitrickled
    if svc_alive "$_p"; then info "$s: already running - untouched"; continue; fi
    _init=$s; [ "$s" = magitrickled ] && _init=magitrickle
    info "starting $_init (it was down)..."
    initd "$_init" start >/dev/null 2>&1 || warn "cannot start $_init"
  done
  return 0
}
step5_verify() { step5_check; }

# ============================================================ STEP 6: end-to-end verify (read-only)
step6_check() {
  _need=0
  _r=$(nslookup ya.ru 127.0.0.1 2>/dev/null | awk '/^Address: /{print $2}' | tail -1)
  if [ -n "$_r" ] && [ "$_r" != "127.0.0.1" ]; then info "DNS normal (ya.ru -> $_r)"; else bad "DNS broken for normal domains"; _need=1; fi
  _b=$(nslookup doubleclick.net 127.0.0.1 2>/dev/null | awk '/^Address: /{print $2}' | tail -1)
  case "$_b" in
    ""|0.0.0.0|::) info "DNS adblock (doubleclick.net -> ${_b:-NXDOMAIN})" ;;
    *) warn "adblock: doubleclick.net resolves to $_b (adblock-fast may be off)" ;;
  esac
  _code=$(curl -m 5 -s -o /dev/null -w '%{http_code}' http://www.gstatic.com/generate_204 2>/dev/null)
  [ "$_code" = "204" ] && info "connectivity: 204 OK" || { bad "connectivity: HTTP ${_code:-none}"; _need=1; }
  _px=$(curl -m 5 -s http://127.0.0.1:9090/proxies 2>/dev/null)
  printf '%s' "$_px" | grep -q '"VPN"' && info "mihomo group VPN: in API" || { bad "mihomo group VPN missing"; _need=1; }
  printf '%s' "$_px" | grep -q '"AI"' && info "mihomo group AI: in API" || { bad "mihomo group AI missing"; _need=1; }
  if have ubus; then
    ubus list 2>/dev/null | grep -qx 'geo-sync' && info "geo ubus: OK" || { bad "geo ubus: missing"; _need=1; }
  fi
  if ip rule show 2>/dev/null | grep -q '32666'; then info "ICMP bypass rule: present"; else warn "ICMP bypass rule (32666) absent - LAN ping may die in TUN"; fi
  return $_need
}
step6_apply() { warn "verify-only step - nothing to apply"; return 0; }
step6_verify() { step6_check; }

# ============================================================ runner
STEP_NAMES="env geo subs sync services verify"

step_fn() { # step_fn N action -> call stepN_action
  _n=$1; _a=$2
  case "$_n-$_a" in
    1-check) step1_check ;; 1-apply) step1_apply ;; 1-verify) step1_verify ;;
    2-check) step2_check ;; 2-apply) step2_apply ;; 2-verify) step2_verify ;;
    3-check) step3_check ;; 3-apply) step3_apply ;; 3-verify) step3_verify ;;
    4-check) step4_check ;; 4-apply) step4_apply ;; 4-verify) step4_verify ;;
    5-check) step5_check ;; 5-apply) step5_apply ;; 5-verify) step5_verify ;;
    6-check) step6_check ;; 6-apply) step6_apply ;; 6-verify) step6_verify ;;
    *) bad "unknown step $_n"; return 1 ;;
  esac
}

run_step() { # run_step N  (interactive/auto common logic)
  _n=$1
  _name=$(echo "$STEP_NAMES" | cut -d' ' -f"$_n")
  hdr "$_n/6" "$_name"
  _fc_before=$FAILCNT
  if step_fn "$_n" check; then
    info "check: ALL GOOD"
    PASS=$((PASS+1))
    SKIP=$((SKIP+1))
    return 0
  fi
  info "check: issues found - apply needed"
  if [ "$GM_CHECK_ONLY" = 1 ]; then
    info "(check-only mode: not applying)"
    return 1
  fi
  if [ "$GM_AUTO" = 1 ] || [ "$GM_YES" = 1 ] || ask "Apply step $_n ($_name)?"; then
    if step_fn "$_n" apply; then
      info "--- verify ---"
      if step_fn "$_n" verify; then
        info "verify: PASS"
        # check-phase failures were fixed by apply - drop them from the global count
        FAILCNT=$_fc_before
        return 0
      else
        bad "verify: FAILED after apply"
        return 1
      fi
    else
      bad "apply failed or was skipped"
      return 1
    fi
  else
    info "skipped by user"
    SKIP=$((SKIP+1))
    return 2
  fi
}

# ============================================================ terminal UI
if [ -t 1 ]; then
  C_R="$(printf '\033[1;31m')"; C_G="$(printf '\033[1;32m')"; C_Y="$(printf '\033[1;33m')"
  C_B="$(printf '\033[1;36m')"; C_M="$(printf '\033[1;35m')"; C_DIM="$(printf '\033[38;5;244m')"
  NC="$(printf '\033[0m')"
else
  C_R=""; C_G=""; C_Y=""; C_B=""; C_M=""; C_DIM=""; NC=""
fi

clr()   { [ -t 1 ] && command -v clear >/dev/null 2>&1 && clear; return 0; }
PAUSE() { [ "$GM_YES" = 1 ] && return 0; [ -t 0 ] || return 0; printf '%sНажмите Enter...%s' "$C_DIM" "$NC"; read -r _p || true; echo; }

_maskurl()  { printf '%s' "${1:-}" | sed 's|/[^/]*$|/...|'; }
_row()      { printf '%s%-20s%s %s\n' "$C_Y" "$1:" "$NC" "$2"; }
_api_node() { curl -s -m2 "http://127.0.0.1:9090/proxies/$1" 2>/dev/null | grep -oE '"now":"[^"]*"' | head -1 | sed 's/"now":"//;s/"$//'; }

_svc_row() { # _svc_row <label> <initd> <pidof-name>
  if [ ! -f "$(P /etc/init.d/$2)" ]; then _row "$1" "${C_DIM}не установлен${NC}"; return; fi
  if pidof "${3:-$2}" >/dev/null 2>&1; then _row "$1" "${C_G}запущен${NC}"; else _row "$1" "${C_R}остановлен${NC}"; fi
}

dashboard() {
  _model=$(cat "$(P /tmp/sysinfo/model)" 2>/dev/null)
  _rel=$(grep '^DISTRIB_RELEASE=' "$(P /etc/openwrt_release)" 2>/dev/null | cut -d"'" -f2)
  _row "Роутер" "${_model:-OpenWrt}${_rel:+ ${C_DIM}/ OpenWrt $_rel${NC}}"
  _svc_row "Zapret" zapret nfqws
  _svc_row "Mihomo" mihomo
  _svc_row "MagiTrickle" magitrickle magitrickled
  _svc_row "HevSocks5" hev-socks5-tunnel
  _svc_row "Dnsmasq" dnsmasq
  if [ -x "$(P /etc/init.d/adblock-fast)" ]; then
    _ab=$(grep -c 'server=/' "$(P /var/run/adblock-fast/dnsmasq.servers)" 2>/dev/null)
    _row "Adblock" "${C_G}${_ab:-0} доменов${NC}"
  fi
  _vpn=$(_api_node VPN); _ai=$(_api_node AI)
  _row "VPN узел" "${_vpn:-${C_DIM}нет ответа API${NC}}"
  _row "AI узел"  "${_ai:-${C_DIM}нет ответа API${NC}}"
  if [ -s "$(P /usr/bin/geo-backend.sh)" ] && ubus list 2>/dev/null | grep -qx 'geo-sync'; then
    _row "Geo LuCI" "${C_G}установлена${NC}"
  else
    _row "Geo LuCI" "${C_R}не установлена${NC}"
  fi
  if ip link show AI >/dev/null 2>&1; then _ait="${C_G}поднят${NC}"; else _ait="${C_DIM}нет${NC}"; fi
  _naic=$(grep -c ' ai ' "$(P /etc/geo-devices.list)" 2>/dev/null)
  _row "AI-туннель" "$_ait${_naic:+ · устройств: ${_naic:-0}}"
  _mt=$(grep -c 'DOMAIN-SUFFIX' "$(P /etc/mihomo/ruleset/mtrickle.yaml)" 2>/dev/null)
  _row "Sync правил" "${_mt:-0} в mtrickle.yaml"
  if [ -f "$(P /root/.geomask-norestart)" ]; then
    _row "VPN-рестарты" "${C_G}запрещены (pin)${NC}"
  else
    _row "VPN-рестарты" "${C_Y}разрешены${NC}"
  fi
}

print_banner() {
  printf '%s+========================================+%s\n' "$C_M" "$NC"
  printf '%s|%s      %sGEOMASK%s  -  geo-masking stack   %s|%s\n' "$C_M" "$NC" "$C_B" "$NC" "$C_M" "$NC"
  printf '%s+========================================+%s %sv%s%s\n' "$C_M" "$NC" "$C_DIM" "$GM_VERSION" "$NC"
}

show_menu() {
  echo
  printf '%s1)%s %sБыстрая диагностика%s %s(только чтение)%s\n'    "$C_B" "$NC" "$C_G" "$NC" "$C_DIM" "$NC"
  printf '%s2)%s %sПошаговый мастер%s %s(статус шагов, запуск любого отдельно)%s\n' "$C_B" "$NC" "$C_G" "$NC" "$C_DIM" "$NC"
  printf '%s3)%s %sПанель Гео%s %s(переустановить / обновить)%s\n'  "$C_B" "$NC" "$C_G" "$NC" "$C_DIM" "$NC"
  printf '%sf)%s %sУдалить -> установить -> настроить geomask%s\n'  "$C_B" "$NC" "$C_Y" "$NC"
  printf '%s0)%s Выход\n'                                          "$C_B" "$NC"
}

diag_quick() {
  PASS=0; FAILCNT=0; SKIP=0
  GM_CHECK_ONLY=1
  for n in 1 2 3 4 5 6; do run_step "$n" || true; done
  GM_CHECK_ONLY=0
  echo
  log "${C_DIM}== ДИАГНОСТИКА: pass=$PASS fail=$FAILCNT ==$NC"
}

# step-by-step master: show status of every step, let the user run any single step
menu_master() {
  while :; do
    _n=1
    for _nm in $STEP_NAMES; do
      if step_fn "$_n" check >/dev/null 2>&1; then
        printf '  %s%d)%s %-14s %sвыполнено%s\n' "$C_B" "$_n" "$NC" "$_nm" "$C_G" "$NC"
      else
        printf '  %s%d)%s %-14s %sтребует внимания%s\n' "$C_B" "$_n" "$NC" "$_nm" "$C_R" "$NC"
      fi
      _n=$((_n+1))
    done
    echo
    printf 'Номер шага для запуска (Enter - назад): '
    read -r _sel || return
    case "$_sel" in
      ''|q|Q) return ;;
      [1-6]) run_step "$_sel" || true; PAUSE ;;
      *) printf '%sнет такого шага%s\n' "$C_R" "$NC"; sleep 1 ;;
    esac
  done
}

# prompt for a subscription URL; keeps current value on Enter; honors flags/--yes
prompt_url() { # prompt_url <label> <current> <flag-value>  -> prints url, rc=1 if none
  _lbl="$1"; _cur="$2"; _flag="$3"
  if [ -n "$_flag" ]; then printf '%s' "$_flag"; return 0; fi
  if [ -n "$_cur" ]; then
    if [ "$GM_YES" = 1 ] || [ ! -t 0 ]; then printf '%s' "$_cur"; return 0; fi
    printf '%s [Enter = оставить %s]: ' "$_lbl" "$(_maskurl "$_cur")"
    read -r _v || _v=""
    printf '%s' "${_v:-$_cur}"
    return 0
  fi
  if [ "$GM_YES" = 1 ] || [ ! -t 0 ]; then return 1; fi
  while :; do
    printf '%s: ' "$_lbl"
    read -r _v || return 1
    case "$_v" in
      http://*|https://*) printf '%s' "$_v"; return 0 ;;
      '') return 1 ;;
      *) printf '  URL должен начинаться с http:// или https://\n' ;;
    esac
  done
}

# remove geomask parts (geo overlay, sync script, cron); keeps mihomo config
geomask_uninstall() {
  for f in /usr/bin/geo-backend.sh /usr/libexec/rpcd/geo-sync \
           /usr/share/rpcd/acl.d/luci-app-geo-sync.json \
           /usr/share/luci/menu.d/luci-app-geo-sync.json; do
    if [ -f "$(P "$f")" ]; then rm -f "$(P "$f")"; info "removed: $f"; fi
  done
  rm -f "$(P /www/luci-static/resources/view/geo-sync/main.js)" 2>/dev/null || true
  rmdir "$(P /www/luci-static/resources/view/geo-sync)" 2>/dev/null || true
  if [ -f "$(P /root/sync_mtrickle_mihomo.sh)" ]; then rm -f "$(P /root/sync_mtrickle_mihomo.sh)"; info "removed: sync script"; fi
  if crontab -l 2>/dev/null | grep -q 'sync_mtrickle_mihomo'; then
    crontab -l 2>/dev/null | grep -v 'sync_mtrickle_mihomo' | crontab - 2>/dev/null && info "cron entry removed"
  fi
  initd rpcd reload 2>/dev/null || true
  return 0
}

# full flow: uninstall -> install geo -> rewrite mihomo 2-subs -> sync+cron -> services -> verify
menu_reinstall() {
  hdr "REINSTALL" "Удалить -> установить -> настроить geomask"
  warn "Будут удалены и переустановлены: гео-оверлей LuCI, sync-скрипт, cron."
  warn "Mihomo конфиг будет перезаписан шаблоном 2-подписок (старый — в бэкапе)."
  warn "Mihomo НЕ перезапускается: подписки применяются через API без обрыва VPN."
  _c="$(P $MIHOMO_CONF)"
  _curv=""; _cura=""
  if [ -s "$_c" ]; then
    _curv=$(_prov_url "$_c" sub)
    _cura=$(_prov_url "$_c" de5)
  fi
  ask "Продолжить пересстановку?" || { warn "отменено"; return 1; }
  _vpn=$(prompt_url "Ссылка на ОБЩУЮ VPN-подписку" "$_curv" "$GM_VPN_URL") || { bad "URL подписки не задан"; return 1; }
  _ai=$(prompt_url "Ссылка на AI-подписку (Германия)" "$_cura" "$GM_AI_URL") || { bad "URL AI-подписки не задан"; return 1; }
  hdr "1/5" "удаление geomask"; geomask_uninstall || true
  hdr "2/5" "гео-оверлей LuCI"; step2_apply || true
  hdr "3/5" "mihomo: 2 подписки"
  if write_mihomo_config "$_vpn" "$_ai"; then
    api_refresh_provider sub "$_vpn" && info "provider sub refreshed (no restart)" || warn "provider sub: refresh failed, применится при старте mihomo"
    api_refresh_provider de5 "$_ai" && info "provider de5 refreshed (no restart)" || warn "provider de5: refresh failed, применится при старте mihomo"
  else
    bad "конфиг mihomo не записан"; return 1
  fi
  hdr "4/5" "синхронизация + роутинг"; step4_apply || true
  hdr "5/5" "сервисы"; step5_apply || true
  hdr "VERIFY" "итоговая проверка"
  if step6_check; then info "verify: PASS"; else bad "verify: FAIL"; return 1; fi
  log "Пересстановка geomask завершена."
  return 0
}

menu_loop() {
  while :; do
    clr
    print_banner
    echo
    dashboard
    show_menu
    printf '\n%sВыберите пункт:%s ' "$C_Y" "$NC"
    read -r _c || exit 0
    case "$_c" in
      1) diag_quick; PAUSE ;;
      2) menu_master || true; PAUSE ;;
      3) hdr "PANEL" "Панель Гео: переустановка/обновление"
         warn "Обновляет файлы панели (backend/rpcd/acl/страница); mihomo не трогается."
         run_step 2 || true; PAUSE ;;
      f|F) menu_reinstall || true; PAUSE ;;
      0|q|Q|"") exit 0 ;;
      *) printf '%sнеизвестный пункт%s\n' "$C_R" "$NC"; sleep 1 ;;
    esac
  done
}

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; }

# ---- args ----
while [ $# -gt 0 ]; do
  case "$1" in
    --auto) GM_AUTO=1 ;;
    --check) GM_CHECK_ONLY=1 ;;
    --yes|-y) GM_YES=1 ;;
    --restart) GM_RESTART=1 ;;
    --backup) GM_BACKUP=1 ;;
    --reinstall) GM_REINSTALL=1 ;;
    --step) shift; GM_STEP="${1:-}" ;;
    --vpn-url) shift; GM_VPN_URL="${1:-}" ;;
    --ai-url) shift; GM_AI_URL="${1:-}" ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'unknown option: %s (see --help)\n' "$1"; exit 2 ;;
  esac
  shift
done

# hard pin: router owner may forbid restarts permanently via flag file
if [ "$GM_RESTART" = 1 ] && [ -f "$(P /root/.geomask-norestart)" ]; then
  log "NOTE: /root/.geomask-norestart present - ignoring --restart (owner forbids VPN interruption)."
  GM_RESTART=0
fi

RC=0
if [ "$GM_BACKUP" = 1 ]; then
  _b=$(ensure_backup) && { info "full backup created: $_b"; RC=0; } || RC=1
elif [ "$GM_REINSTALL" = 1 ]; then
  menu_reinstall || RC=1
elif [ -n "$GM_STEP" ]; then
  run_step "$GM_STEP" || RC=1
elif [ "$GM_AUTO" = 1 ] || [ "$GM_CHECK_ONLY" = 1 ]; then
  log "geomask v$GM_VERSION  mode: $([ "$GM_CHECK_ONLY" = 1 ] && echo check || echo auto)"
  for n in 1 2 3 4 5 6; do
    if [ "$GM_CHECK_ONLY" = 1 ]; then
      run_step "$n" || true
    else
      run_step "$n" || { [ $? -eq 1 ] && { RC=1; bad "aborting on step $n failure"; break; }; }
    fi
  done
else
  menu_loop
fi

log ""
log "${C_DIM}== SUMMARY: pass=$PASS fail=$FAILCNT skipped_steps=$SKIP ==$NC"
[ "$FAILCNT" -eq 0 ] || RC=1
exit $RC
