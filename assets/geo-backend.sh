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
UPD_CHECK=$UPD_DIR/check.json
UPD_STATUS=$UPD_DIR/status.json
UPD_PIDF=$UPD_DIR/pid
UPD_TTL=3600
UPD_REPO=kabzon93region/geomask
DEPLOY_JS=/www/luci-static/resources/view/geo-sync/main.js
DEPLOY_GUARD=/usr/bin/geomask-guard.sh
DEPLOY_BACKEND=/usr/bin/geo-backend.sh

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
  # AI-DNS (no DNS leak for AI devices): 0=off/absent, 1=working, 2=partial
  # working = ip rule routes marked DNS to the AI tunnel AND the AI DNS chain listens
  if [ -f /etc/geomask-aidns.off ] || [ ! -f /etc/geomask-aidns.uid ]; then AIDNS=0
  elif ip route get 1.1.1.1 mark 0x64 2>/dev/null | grep -q 'dev AI' \
       && netstat -ltnpu 2>/dev/null | grep -q '192.168.0.53:5353 '; then AIDNS=1
  else AIDNS=2; fi
  printf '{"vpn_node":"%s","vpn_cc":"%s","ai_node":"%s","ai_cc":"%s","global":"%s","lcron":%s,"ipv6":%s,"doh":"%s","ai_rules":%s,"mt_rules":%s,"google":"%s","sync_at":"%s","devices":%s,"n_bypass":%s,"n_realtz":%s,"rules_ok":%s,"n_ai":%s,"ai_tun":%s,"ai_listener":%s,"ai_sub":"%s","vpn_sub":"%s","ai_url":"%s","vpn_url":"%s","aidns":%s}' \
    "$(json_esc "$VPN_NODE")" "$(_node_cc "$VPN_NODE")" \
    "$(json_esc "$AI_NODE")" "$(_node_cc "$AI_NODE")" \
    "$(json_esc "$GLOBAL")" "$LCRON" "$V6" "$(json_esc "$DOH")" \
    "$AI_RC" "$MT_RC" "$(json_esc "$GOOGLE_CC")" "$(json_esc "$SYNC_AT")" \
    "$DEV_LIST" "$BYPASS" "$REALTZ" "$RULES" \
    "$N_AI" "$AI_TUN" "$AI_LST" "$(json_esc "$AI_SUB_ID")" "$(json_esc "$VPN_SUB_ID")" \
    "$(json_esc "$_de5u")" "$(json_esc "$_subu")" "$AIDNS"
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
  _hwid=$(grep -A1 'x-hwid' "$MIHOMO_CONF" 2>/dev/null | sed -n 's/^[[:space:]]*-[[:space:]]*"\(.*\)"/\1/p' | head -1)
  if ! awk -v pn="^  $PN:" -v nu="$URL" -v hwid="$_hwid" '
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
  if [ -n "$_hwid" ]; then
    awk -v pn="^  $PN:" -v hwid="$_hwid" '
      BEGIN { f=0; added=0 }
      $0 ~ pn { f=1; print; next }
      f && !added && /^    url:/ { print; print "    header:"; print "      x-hwid:"; print "        - \"" hwid "\""; added=1; next }
      f && /^  [A-Za-z0-9_-]+:/ { f=0 }
      f && /^[A-Za-z]/ { f=0 }
      { print }
    ' "$TMPF" > "$TMPF.hwid" && mv -f "$TMPF.hwid" "$TMPF"
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

# ---------------- self-update (v1.7.0): check / status / apply ----------------
# check -> $UPD_CHECK (cache TTL), apply -> detached runner writes $UPD_STATUS.
# Flow: full backup -> download release artifact -> sh -n + version + sha256
# -> "sh new --reinstall --yes" (module files only, no service restarts) -> rpcd reload.

_upd_esc() { sed 's/\\/\\\\/g; s/"/\\"/g'; }   # stdin -> json-safe

_upd_mark() { # $1=file, $2=marker name; supports NAME="v" and NAME = 'v'
  [ -f "$1" ] || { printf 'none'; return 0; }
  sed -n "s/.*$2[[:space:]]*=[[:space:]]*[\"']\([^\"']*\)[\"'].*/\1/p" "$1" 2>/dev/null | head -1
}

_upd_core_ver() { # installed installer copy (may be absent)
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

_upd_check_now() { # $1 = "force" bypasses the TTL cache
  mkdir -p "$UPD_DIR" 2>/dev/null
  _age=$(( $(date +%s) - $(date -r "$UPD_CHECK" +%s 2>/dev/null || echo 0) ))
  if [ "$1" != "force" ] && [ -s "$UPD_CHECK" ] && [ "$_age" -lt "$UPD_TTL" ]; then
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
  _core=$(_upd_core_ver)
  _panel=$(_upd_mark "$DEPLOY_JS" PANEL_VERSION)
  _guard=$(_upd_mark "$DEPLOY_GUARD" GUARD_VERSION)
  _backend=$(_upd_mark "$DEPLOY_BACKEND" BACKEND_VERSION)
  [ -n "$_panel" ] || _panel=none
  [ -n "$_guard" ] || _guard=none
  [ -n "$_backend" ] || _backend=none
  _need=false
  if [ "$_core" = "none" ]; then _need=true
  elif _upd_newer "$_ver" "$_core"; then _need=true
  elif [ "$_core" = "$_ver" ] && { [ "$_panel" != "$_ver" ] || [ "$_guard" != "$_ver" ] || [ "$_backend" != "$_ver" ]; }; then
    _need=true   # partial/misaligned deploy of the same release -> repair
  fi
  _now=$(date '+%F %T')
  printf '{"ok":true,"checked_at":"%s","src":"%s","local":{"core":"%s","panel":"%s","guard":"%s","backend":"%s"},"latest":{"tag":"%s","version":"%s","digest":"%s","published":"%s"},"need":%s,"stack":%s}\n' \
    "$_now" "$_src" "$_core" "$_panel" "$_guard" "$_backend" \
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

cmd_upd_status() {
  mkdir -p "$UPD_DIR" 2>/dev/null
  if [ -s "$UPD_STATUS" ]; then cat "$UPD_STATUS"; else printf '{"phase":"idle"}'; fi
  printf '\n'
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
D=/tmp/geomask-upd
ST=$D/status.json
say() { printf '{"phase":"%s","msg":"%s","ts":"%s"}' "$1" "$2" "$(date '+%F %T')" > "$ST.tmp"; mv -f "$ST.tmp" "$ST"; }
trap 'rm -f "$D/pid"' EXIT
TAG="$1"
say backup "creating full router backup..."
TS=$(date +%Y%m%d-%H%M%S)
BK=/tmp/router-backup-$TS.tar.gz
if ! sysupgrade -b "$BK" >/dev/null 2>&1 || [ ! -s "$BK" ]; then
  say error "backup failed - update aborted"; exit 1
fi
echo "file=$BK md5=$(md5sum "$BK" | awk '{print $1}')" > "$D/backup.info"
say download "downloading geomask $TAG..."
rm -f "$D/geomask.sh.new"
if ! curl -fsSL -m 90 -o "$D/geomask.sh.new" "https://cdn.jsdelivr.net/gh/kabzon93region/geomask@${TAG}/geomask.sh" && \
   ! curl -fsSL -m 90 -o "$D/geomask.sh.new" "https://github.com/kabzon93region/geomask/releases/download/${TAG}/geomask.sh"; then
  say error "download failed (jsdelivr + github)"; exit 1
fi
say validate "validating download..."
if ! sh -n "$D/geomask.sh.new"; then say error "syntax check failed"; exit 1; fi
NEWV=$(sed -n 's/^GM_VERSION="\([^"]*\)".*/\1/p' "$D/geomask.sh.new" | head -1)
if [ -z "$NEWV" ] || [ "v$NEWV" != "$TAG" ]; then
  say error "version mismatch (file=${NEWV:-none} tag=$TAG)"; exit 1
fi
DIG=$(sed -n 's/.*"digest":"\(sha256:[0-9a-f]*\)".*/\1/p' "$D/check.json" | head -1)
if [ -n "$DIG" ]; then
  ACT="sha256:$(sha256sum "$D/geomask.sh.new" | awk '{print $1}')"
  [ "$ACT" = "$DIG" ] || { say error "sha256 mismatch"; exit 1; }
fi
say apply "installing modules and panel (no service restarts)..."
if ! sh "$D/geomask.sh.new" --reinstall --yes >> "$D/upd.log" 2>&1; then
  say error "install failed - see $D/upd.log"; exit 1
fi
cp -f "$D/geomask.sh.new" /tmp/geomask.sh 2>/dev/null && chmod +x /tmp/geomask.sh 2>/dev/null
/etc/init.d/rpcd reload >/dev/null 2>&1 || true
sleep 1
say done "updated to $NEWV - refresh the panel page (Ctrl+F5)"
RUNEOF
  printf '{"phase":"queued","msg":"starting update to %s","ts":"%s"}\n' "$_tag" "$(date '+%F %T')" > "$UPD_STATUS"
  sh "$UPD_DIR/run.sh" "$_tag" >/dev/null 2>&1 &
  echo $! > "$UPD_PIDF"
  printf '{"ok":true,"to":"%s"}\n' "$_tag"
}

case "$1" in
  status)     cmd_status ;;
  leases)     cmd_leases ;;
  log)        cmd_log ;;
  sync_lists) cmd_sync_lists ;;
  aidns)      cmd_aidns_apply ;;
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
  upd_check)    cmd_upd_check "$2" ;;
  upd_status)   cmd_upd_status ;;
  upd_apply)    cmd_upd_apply ;;
  *) printf '{"error":"usage"}'; exit 1 ;;
esac
