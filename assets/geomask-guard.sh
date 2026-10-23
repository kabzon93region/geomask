#!/bin/sh
# geomask-guard.sh - durable fix for the B-001 class bug (TD-01).
# (A) Source-IP exclusion for ai/bypass devices at the HEAD of ip mangle
#     PREROUTING (before magitrickle MT_* jumps) so their packets never get
#     a magitrickle fwmark.
# (A2) Mark-clear: own nft table (prerouting, priority -149) wipes ONLY
#     magitrickle-space marks (0x4d61xxxx) for those devices. Position-
#     independent: survives any magitrickle/fw4 PREROUTING rebuild, so fwmark
#     rules can NEVER hijack our devices even during the watchdog window.
# (B) ip-rule watchdog: our per-device/icmp/aidns rules must exist and sit
#     ABOVE (numerically below) the magitrickle fwmark base; re-seat them at
#     MT_MIN-100 and flush conntrack when violated.
# (C) Safety: table 101 must keep "default dev AI".
# Logs actions only via logger -t geomask-guard (silent when healthy, cron
# runs every 5 min - no syslog spam). Read: logread | grep geomask-guard
GUARD_VERSION="@@GUARD_VERSION@@"

log() { logger -t geomask-guard "$*"; }

DEVS=/etc/geo-devices.list
CHAIN="ip mangle PREROUTING"
[ -f "$DEVS" ] || { log "SKIP: $DEVS missing"; exit 0; }

IPS=$(awk '$1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && ($2=="ai" || $2=="bypass") {print $1}' "$DEVS" | tr '\n' ' ')
NEED_FLUSH=""

# ---------- (A) nft exclusion ----------
OUT=$(nft -a list chain $CHAIN 2>/dev/null)
if [ -z "$OUT" ]; then
  log "FAIL: cannot list $CHAIN"
else
  FIRST_MT=$(printf '%s\n' "$OUT" | grep -n 'jump MT_' | head -1 | cut -d: -f1)
  for ip in $IPS; do
    OURS=$(printf '%s\n' "$OUT" | grep -n "ip saddr $ip return" | head -1)
    if [ -z "$OURS" ]; then
      if nft insert rule $CHAIN ip saddr "$ip" return comment "geomask-excl" 2>/dev/null; then
        log "excl ADD $ip"
        NEED_FLUSH="1"
      else
        log "FAIL excl add $ip"
      fi
    else
      OUR_LINE=${OURS%%:*}
      if [ -n "$FIRST_MT" ] && [ "$OUR_LINE" -gt "$FIRST_MT" ]; then
        # our rule slid below the MT_ jumps (magitrickle re-seated) - rebuild at head
        printf '%s\n' "$OUT" | grep "ip saddr $ip return" | sed -n 's/.*handle \([0-9][0-9]*\).*/\1/p' | while read -r H; do
          nft delete rule $CHAIN handle "$H" 2>/dev/null
        done
        if nft insert rule $CHAIN ip saddr "$ip" return comment "geomask-excl" 2>/dev/null; then
          log "excl RESEAT $ip (was below MT_ jumps)"
          NEED_FLUSH="1"
        else
          log "FAIL excl reseat $ip"
        fi
      fi
    fi
  done
  # drop exclusions for devices no longer in the list (mode changed / removed)
  STALE=$(printf '%s\n' "$OUT" | grep 'geomask-excl')
  if [ -n "$STALE" ]; then
    OLDIFS2=$IFS
    IFS='
'
    for line in $STALE; do
      rip=$(printf '%s' "$line" | awk '{print $3}')
      case " $IPS " in
        *" $rip "*) ;;
        *)
          h=$(printf '%s' "$line" | sed -n 's/.*handle \([0-9][0-9]*\).*/\1/p')
          if [ -n "$h" ] && nft delete rule $CHAIN handle "$h" 2>/dev/null; then
            log "excl DEL stale $rip"
            NEED_FLUSH="1"
          fi
          ;;
      esac
    done
    IFS=$OLDIFS2
  fi
fi

# ---------- (A2) mark-clear table (survives PREROUTING rebuilds) ----------
# Own table is invisible to magitrickle (ip mangle) and fw4 (inet fw4).
# Hook at -149 = right after magitrickle's mangle (-150), before geomask
# AI-DNS mark 0x64 (-110). Clears ONLY 0x4d61xxxx marks - zapret/aidns marks
# are untouched.
MC_T=geomask_excl
nft list table ip "$MC_T" >/dev/null 2>&1 || nft add table ip "$MC_T" 2>/dev/null
if ! nft list chain ip "$MC_T" prerouting >/dev/null 2>&1; then
  nft add chain ip "$MC_T" prerouting '{ type filter hook prerouting priority -149; policy accept; }' 2>/dev/null \
    || log "FAIL mclear chain add"
fi
if ! nft list set ip "$MC_T" excl_ips >/dev/null 2>&1; then
  nft add set ip "$MC_T" excl_ips '{ type ipv4_addr; }' 2>/dev/null || log "FAIL mclear set add"
fi
if ! nft list chain ip "$MC_T" prerouting 2>/dev/null | grep -q '@excl_ips'; then
  nft add rule ip "$MC_T" prerouting ip saddr @excl_ips \
      meta mark and 0xffff0000 eq 0x4d610000 counter meta mark set 0 \
      comment "geomask-mclear" 2>/dev/null || log "FAIL mclear rule add"
  log "mclear rule installed"
fi
MC_CUR=$(nft list set ip "$MC_T" excl_ips 2>/dev/null | grep -oE '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' | tr '\n' ' ')
for ip in $IPS; do
  case " $MC_CUR " in
    *" $ip "*) ;;
    *) nft add element ip "$MC_T" excl_ips "{ $ip }" 2>/dev/null && log "mclear ADD $ip" ;;
  esac
done
for rip in $MC_CUR; do
  case " $IPS " in
    *" $rip "*) ;;
    *) nft delete element ip "$MC_T" excl_ips "{ $rip }" 2>/dev/null && log "mclear DEL $rip" ;;
  esac
done

# ---------- (B) ip-rule watchdog ----------
MT_MIN=$(ip rule show 2>/dev/null | grep 'fwmark 0x4d61' | awk -F: '{print $1}' | sort -n | head -1)

CHECKLIST="iif br-lan ipproto icmp lookup main
fwmark 0x64 lookup 101"
for ip in $IPS; do
  mode=$(awk -v i="$ip" '$1==i {print $2}' "$DEVS")
  case "$mode" in
    ai) CHECKLIST="$CHECKLIST
from $ip lookup 101" ;;
    bypass) CHECKLIST="$CHECKLIST
from $ip lookup main" ;;
  esac
done

RULES_OK=1
OLDIFS=$IFS
IFS='
'
for pat in $CHECKLIST; do
  [ -z "$pat" ] && continue
  LINE=$(ip rule show | grep -F "$pat" | head -1)
  if [ -z "$LINE" ]; then RULES_OK=0; break; fi
  PREF=${LINE%%:*}
  case "$PREF" in
    ''|*[!0-9]*) RULES_OK=0; break ;;
  esac
  if [ -n "$MT_MIN" ] && [ "$PREF" -ge "$MT_MIN" ]; then RULES_OK=0; break; fi
done
IFS=$OLDIFS

if [ "$RULES_OK" = "0" ]; then
  if [ -n "$MT_MIN" ] && [ "$MT_MIN" -gt 100 ]; then
    BASE=$((MT_MIN - 100))
  else
    BASE=19900
  fi
  for ip in $IPS; do
    mode=$(awk -v i="$ip" '$1==i {print $2}' "$DEVS")
    case "$mode" in
      ai)
        while ip rule del from "$ip" lookup 101 2>/dev/null; do :; done
        while ip rule del from "$ip" lookup main 2>/dev/null; do :; done
        ip rule add priority "$BASE" from "$ip" lookup 101
        ;;
      bypass)
        while ip rule del from "$ip" lookup main 2>/dev/null; do :; done
        while ip rule del from "$ip" lookup 101 2>/dev/null; do :; done
        ip rule add priority "$BASE" from "$ip" lookup main
        ;;
    esac
  done
  while ip rule del iif br-lan ipproto icmp lookup main 2>/dev/null; do :; done
  ip rule add priority $((BASE + 1)) iif br-lan ipproto icmp lookup main
  while ip rule del fwmark 0x64 table 101 2>/dev/null; do :; done
  ip rule add priority $((BASE + 2)) fwmark 0x64 table 101
  NEED_FLUSH="1"
  log "ip rules re-seated at $BASE (magitrickle base: ${MT_MIN:-none})"
fi

# ---------- (C) table 101 safety ----------
if ! ip route show table 101 2>/dev/null | grep -q '^default'; then
  if ip route replace default dev AI table 101 2>/dev/null; then
    log "table 101 default restored (dev AI)"
  else
    log "WARN: table 101 has no default route (AI tun down?)"
  fi
fi

# ---------- conntrack flush when something changed ----------
if [ -n "$NEED_FLUSH" ]; then
  for ip in $IPS; do
    conntrack -D -s "$ip" >/dev/null 2>&1
    conntrack -D -d "$ip" >/dev/null 2>&1
  done
  log "conntrack flushed for: $IPS"
fi

exit 0
