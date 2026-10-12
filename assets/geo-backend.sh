#!/bin/sh
# geo-backend.sh - backend for LuCI "Гео-маскировка и AI" panel (geo-sync ubus object).
# Commands:
#   status | leases | log | sync_lists | google
#   global <direct|vpn> | ipv6 <on|off>
#   dev_add <ip> <mode> [mac] [name] | dev_del <ip>
# Device modes (per-device geo policy):
#   bypass  - device routes DIRECT (real IP), beats fwmark+Mihomo; real tz (GeoSync off on PC)
#   realtz  - device routes via mihomo (masked IP), but PC keeps real tz (GeoSync off on PC)
# NOTE: router timezone is NOT used for geo-masking (Google reads the PC browser tz), so no
#       tz commands here. PC-side tz is handled by GeoSync.ps1 on each PC.
# Output: JSON on stdout. Exit 0.

DEVICES=/etc/geo-devices.list
EXCL_OLD=/etc/geo-exclude.list   # legacy (ip name) -> migrated to DEVICES as bypass
LOG=/root/geo-sync.log
MIHOMO_API=http://127.0.0.1:9090
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
  RULES=$(ip rule show | grep -c 'lookup main' || echo 0)
  printf '{"vpn_node":"%s","vpn_cc":"%s","ai_node":"%s","ai_cc":"%s","global":"%s","lcron":%s,"ipv6":%s,"doh":"%s","ai_rules":%s,"mt_rules":%s,"google":"%s","sync_at":"%s","devices":%s,"n_bypass":%s,"n_realtz":%s,"rules_ok":%s}' \
    "$(json_esc "$VPN_NODE")" "$(_node_cc "$VPN_NODE")" \
    "$(json_esc "$AI_NODE")" "$(_node_cc "$AI_NODE")" \
    "$(json_esc "$GLOBAL")" "$LCRON" "$V6" "$(json_esc "$DOH")" \
    "$AI_RC" "$MT_RC" "$(json_esc "$GOOGLE_CC")" "$(json_esc "$SYNC_AT")" \
    "$DEV_LIST" "$BYPASS" "$REALTZ" "$RULES"
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

# quick Google geo-check through the tunnel (AI group handles google via RuleSet(ai))
cmd_google() {
  CC=$(curl -s -m25 -x "$TUN" \
    -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36' \
    -H 'Accept-Language: en-US,en;q=0.9' -b 'CONSENT=YES+' \
    'https://www.google.com/?hl=en' 2>/dev/null | grep -oE 'class="O3yKUb">[^<]+' | head -1 | sed 's/.*>//')
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
  case "$MODE" in bypass|realtz) ;; *) printf '{"error":"bad mode"}'; return 1 ;; esac
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

# (re)apply routing for all managed devices: bypass -> direct (32694), realtz -> via mihomo (no rule)
_apply_routing() {
  # drop stale per-device direct rules first (we re-add for bypass)
  if [ -f "$DEVICES" ]; then
    while read -r dip dmode dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      ip rule del from "$dip" lookup main 2>/dev/null || true
    done < "$DEVICES"
    while read -r dip dmode dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      if [ "$dmode" = "bypass" ]; then
        ip rule show | grep -q "from $dip" || ip rule add priority 32694 from "$dip" lookup main
      fi
    done < "$DEVICES"
  fi
  _hotplug_sync
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
  # --- bypass-mode devices (32694 beats fwmark rules 32695+ and 32760) ---
  if [ -f /etc/geo-devices.list ]; then
    while read -r dip dmode dname; do
      case "$dip" in ''|'#'*) continue ;; esac
      [ "$dmode" = "bypass" ] || continue
      ip rule show | grep -q "from $dip" || ip rule add priority 32694 from "$dip" lookup main
    done < /etc/geo-devices.list
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
  *) printf '{"error":"usage"}'; exit 1 ;;
esac
