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
#
# R10a contract of the WHOLE entrypoint (not only the console block):
#   0   healthy, or every needed repair succeeded
#   1   degraded / unrepaired: a mutation failed, or the AI/console tun is down
#   2   input or lock unknown: no readable device list, or no lock primitive
#   75  the console lane lock is busy (another writer owns the lane; retry later)
# A missing /etc/geo-devices.list is NOT an early exit any more: per-device state is
# reported unknown and the independent blocks (drain, base rules, table 101, console)
# still run. A list that exists but is not a readable regular file is fail-closed (2).
GUARD_VERSION="@@GUARD_VERSION@@"

log() { logger -t geomask-guard "$*"; }

RC_DEGRADED=0
RC_INPUT_UNKNOWN=0
RC_CONSOLE_BUSY=0

DEVS=${GM_DEVICES:-/etc/geo-devices.list}
CHAIN="ip mangle PREROUTING"
DEV_STATE=ok
if [ ! -e "$DEVS" ]; then
  DEV_STATE=missing
  RC_INPUT_UNKNOWN=1
  log "UNKNOWN: $DEVS is missing - per-device state is unknown (dev_add or an empty list will fix it)"
elif [ ! -f "$DEVS" ] || [ ! -r "$DEVS" ]; then
  # Fail closed: without the list we cannot tell our exclusions from somebody else's.
  log "FAIL: $DEVS is not a readable regular file - refusing to run without the device list"
  exit 2
fi

# ---------- (0) R05 route queue drain ----------
# The ifup hotplug only queues `pending-<table>`; its detached worker may find the route lock
# busy and leave the event for later. Cron is the backstop: apply what is queued, inline and
# bounded. This runs BEFORE the console block and OUTSIDE its lock - the drain takes
# routes-worker then route-<table>, one short try per table; a busy table stays pending and is
# retried by the next guard run, never held under the console lock.
CROUTES=${GM_ROUTES_LIB:-/usr/lib/geomask/geomask-routes.sh}
if [ -r "$CROUTES" ]; then
  ( GM_ROUTE_DRAIN=1; GM_ROUTE_TUN_WAIT=0
    export GM_ROUTE_DRAIN GM_ROUTE_TUN_WAIT
    . "$CROUTES" && gm_route_drain ) >/dev/null 2>&1 || true
fi

IPS=''
if [ "$DEV_STATE" = "ok" ]; then
  IPS=$(awk '$1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && ($2=="ai" || $2=="bypass") {print $1}' "$DEVS" | tr '\n' ' ')
fi
NEED_FLUSH=""

# ---------- (A) nft exclusion ----------
# Per-device state needs the list: with a missing list nothing is added or deleted here, so an
# empty IPS can never be mistaken for "no devices are excluded".
if [ "$DEV_STATE" = "ok" ]; then
  OUT=$(nft -a list chain $CHAIN 2>/dev/null)
  if [ -z "$OUT" ]; then
    RC_DEGRADED=1
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
          RC_DEGRADED=1
          log "FAIL excl add $ip"
        fi
      else
        OUR_LINE=${OURS%%:*}
        if [ -n "$FIRST_MT" ] && [ "$OUR_LINE" -gt "$FIRST_MT" ]; then
          # our rule slid below the MT_ jumps (magitrickle re-seated) - rebuild at head
          for H in $(printf '%s\n' "$OUT" | grep "ip saddr $ip return" | sed -n 's/.*handle \([0-9][0-9]*\).*/\1/p'); do
            nft delete rule $CHAIN handle "$H" 2>/dev/null \
              || { RC_DEGRADED=1; log "FAIL excl delete handle $H"; }
          done
          if nft insert rule $CHAIN ip saddr "$ip" return comment "geomask-excl" 2>/dev/null; then
            log "excl RESEAT $ip (was below MT_ jumps)"
            NEED_FLUSH="1"
          else
            RC_DEGRADED=1
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
            if [ -n "$h" ]; then
              if nft delete rule $CHAIN handle "$h" 2>/dev/null; then
                log "excl DEL stale $rip"
                NEED_FLUSH="1"
              else
                RC_DEGRADED=1
                log "FAIL excl del stale $rip"
              fi
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
  if ! nft list table ip "$MC_T" >/dev/null 2>&1; then
    nft add table ip "$MC_T" 2>/dev/null \
      || { RC_DEGRADED=1; log "FAIL mclear table add"; }
  fi
  if ! nft list chain ip "$MC_T" prerouting >/dev/null 2>&1; then
    nft add chain ip "$MC_T" prerouting '{ type filter hook prerouting priority -149; policy accept; }' 2>/dev/null \
      || { RC_DEGRADED=1; log "FAIL mclear chain add"; }
  fi
  if ! nft list set ip "$MC_T" excl_ips >/dev/null 2>&1; then
    nft add set ip "$MC_T" excl_ips '{ type ipv4_addr; }' 2>/dev/null \
      || { RC_DEGRADED=1; log "FAIL mclear set add"; }
  fi
  if ! nft list chain ip "$MC_T" prerouting 2>/dev/null | grep -q '@excl_ips'; then
    if nft add rule ip "$MC_T" prerouting ip saddr @excl_ips \
        meta mark and 0xffff0000 eq 0x4d610000 counter meta mark set 0 \
        comment "geomask-mclear" 2>/dev/null; then
      log "mclear rule installed"
    else
      RC_DEGRADED=1
      log "FAIL mclear rule add"
    fi
  fi
  MC_CUR=$(nft list set ip "$MC_T" excl_ips 2>/dev/null | grep -oE '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' | tr '\n' ' ')
  for ip in $IPS; do
    case " $MC_CUR " in
      *" $ip "*) ;;
      *)
        if nft add element ip "$MC_T" excl_ips "{ $ip }" 2>/dev/null; then
          log "mclear ADD $ip"
        else
          RC_DEGRADED=1
          log "FAIL mclear add $ip"
        fi
        ;;
    esac
  done
  for rip in $MC_CUR; do
    case " $IPS " in
      *" $rip "*) ;;
      *)
        if nft delete element ip "$MC_T" excl_ips "{ $rip }" 2>/dev/null; then
          log "mclear DEL $rip"
        else
          RC_DEGRADED=1
          log "FAIL mclear del $rip"
        fi
        ;;
    esac
  done
else
  log "skip per-device exclusions: no device list to trust"
fi

# ---------- (B) ip-rule watchdog ----------
# The LAN input device comes from UCI (what netifd/hotplug actually named the bridge), not a
# hardcoded br-lan: on a system with br-lan.1 the old check re-seated the rules every 5 minutes.
LAN_DEV=$(uci get network.lan.device 2>/dev/null)
[ -n "$LAN_DEV" ] || LAN_DEV=$(uci get network.lan.ifname 2>/dev/null)
for _gm_lan in $LAN_DEV; do LAN_DEV=$_gm_lan; break; done
[ -n "$LAN_DEV" ] || LAN_DEV=br-lan

RULES_NOW=$(ip rule show 2>/dev/null)
MT_MIN=$(printf '%s\n' "$RULES_NOW" | grep 'fwmark 0x4d61' | awk -F: '{print $1}' | sort -n | head -1)

CHECKLIST="iif $LAN_DEV ipproto icmp lookup main
fwmark 0x64 lookup 101"
if [ "$DEV_STATE" = "ok" ]; then
  for ip in $IPS; do
    mode=$(awk -v i="$ip" '$1==i {print $2; exit}' "$DEVS")
    case "$mode" in
      ai) CHECKLIST="$CHECKLIST
from $ip lookup 101" ;;
      bypass) CHECKLIST="$CHECKLIST
from $ip lookup main" ;;
    esac
  done
fi

RULES_OK=1
OLDIFS=$IFS
IFS='
'
for pat in $CHECKLIST; do
  [ -z "$pat" ] && continue
  LINE=$(printf '%s\n' "$RULES_NOW" | grep -F "$pat" | head -1)
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
  RESEAT_OK=1
  for ip in $IPS; do
    mode=$(awk -v i="$ip" '$1==i {print $2; exit}' "$DEVS")
    case "$mode" in
      ai)
        while ip rule del from "$ip" lookup 101 2>/dev/null; do :; done
        while ip rule del from "$ip" lookup main 2>/dev/null; do :; done
        ip rule add priority "$BASE" from "$ip" lookup 101 2>/dev/null \
          || { RESEAT_OK=0; RC_DEGRADED=1; log "FAIL ip rule add from $ip lookup 101"; }
        ;;
      bypass)
        while ip rule del from "$ip" lookup main 2>/dev/null; do :; done
        while ip rule del from "$ip" lookup 101 2>/dev/null; do :; done
        ip rule add priority "$BASE" from "$ip" lookup main 2>/dev/null \
          || { RESEAT_OK=0; RC_DEGRADED=1; log "FAIL ip rule add from $ip lookup main"; }
        ;;
    esac
  done
  while ip rule del iif "$LAN_DEV" ipproto icmp lookup main 2>/dev/null; do :; done
  ip rule add priority $((BASE + 1)) iif "$LAN_DEV" ipproto icmp lookup main 2>/dev/null \
    || { RESEAT_OK=0; RC_DEGRADED=1; log "FAIL ip rule add iif $LAN_DEV ipproto icmp lookup main"; }
  while ip rule del fwmark 0x64 table 101 2>/dev/null; do :; done
  ip rule add priority $((BASE + 2)) fwmark 0x64 table 101 2>/dev/null \
    || { RESEAT_OK=0; RC_DEGRADED=1; log "FAIL ip rule add fwmark 0x64 table 101"; }
  if [ "$RESEAT_OK" = "1" ]; then
    NEED_FLUSH="1"
    log "ip rules re-seated at $BASE (magitrickle base: ${MT_MIN:-none})"
  fi
fi

# ---------- (C) table 101 safety ----------
# `grep '^default'` accepted the WAN default netifd restores for every table: the AI lane was
# silently bypassed while the guard reported success. The default must point at the AI tun.
T101=$(ip route show table 101 2>/dev/null)
if printf '%s\n' "$T101" | awk '
    $1 == "default" { for (i = 2; i < NF; i++) if ($i == "dev" && $(i + 1) == "AI") ours = 1 }
    END { exit(ours ? 0 : 1) }'; then
  :
else
  if ip route replace default dev AI table 101 2>/dev/null; then
    log "table 101 default restored (dev AI)"
  else
    # The AI tun is absent. A foreign/WAN default here is worse than none: delete it and say
    # that the lane is down instead of leaving traffic on the real address.
    if ip route del default table 101 2>/dev/null; then
      log "table 101 default removed (AI tun down) - AI-DNS runtime=down"
    else
      log "FAIL table 101 default could not be removed - AI-DNS runtime=down"
    fi
    RC_DEGRADED=1
  fi
fi

# ---------- (E) console lane watchdog ----------
# A flushed ip rule silently puts the console back on the normal path: the owner sees "it
# stopped working" and has nothing to look at. Only our own rule and our own table are
# touched, and only while the lane is switched on in the panel.
#
# The kill switch and this block must not interleave: `off` writes enabled=0 and removes the
# rule, so a watchdog that had read the state BEFORE that would put the rule straight back
# after the owner switched the lane off. The state is therefore re-read INSIDE the same lock
# that `on`/`off` hold for their whole operation. The lock is the kernel flock of
# geomask-lock.sh: a killed holder is released by the kernel, the file is never deleted and
# there is no PID to guess. A busy lane means "skip this run", never "work without it".
CS=${GM_CONSOLE_STATE:-/etc/geomask/console.state}
CLOCK=${GM_CONSOLE_LOCK:-/tmp/geomask-locks/console.lock}
# The kill switch and this block hold the same kernel lock (geomask-lock.sh, console rank).
# The watchdog is a cron/hotplug background run: it takes the lock with a single non-blocking
# attempt and skips the run when the lane is busy - it never waits, never reclaims a lock and
# never deletes the lock file (a killed holder is released by the kernel).
GM_LOCK_LIB=${GM_LOCK_LIB:-/usr/lib/geomask/geomask-lock.sh}
if ! command -v gm_lock_acquire >/dev/null 2>&1 && [ -r "$GM_LOCK_LIB" ]; then
  . "$GM_LOCK_LIB"
fi
# Table 102 is the only path the console device has, so both halves of it are watched: the LAN
# prefixes (mirrored through the same helper as table 101) and a default that really points at
# our tunnel. `grep '^default'` accepted the WAN default netifd restores for every table, so a
# reboot left the console leaving with the real address (review 15.11, #9).
CTUN=${GM_CONSOLE_TUN:-Console}
CTBL=${GM_CONSOLE_TABLE:-102}
# Kept here as well as at the drain above: the console block is exercised on its own by the
# tests, and a missing variable must not turn a watchdog run into a crash.
CROUTES=${GM_ROUTES_LIB:-/usr/lib/geomask/geomask-routes.sh}
# CONSOLE_LOCK_RC distinguishes the two refusals: 1 = another writer owns the lane (retry
# later, 75), anything else = no usable primitive (fail closed, 2). It is never a green SKIP.
CONSOLE_LOCK_RC=0
console_lock() { # 0 only with the kernel lock held; busy or no primitive = skip, never forced
  command -v gm_lock_acquire >/dev/null 2>&1 || { CONSOLE_LOCK_RC=2; return 1; }
  gm_lock_acquire console 0 "$CLOCK"
  CONSOLE_LOCK_RC=$?
  [ "$CONSOLE_LOCK_RC" = 0 ]
}
# Literal dotted quad only. An enabled lane with an empty or malformed ip= has no rule at
# all: that is a degraded state, never silence.
valid_ipv4() {
  case "$1" in
    ''|*[!0-9.]*|.*|*.|*..*) return 1 ;;
  esac
  _vi_o=$1
  _vi_n=0
  while [ -n "$_vi_o" ]; do
    _vi_p=${_vi_o%%.*}
    case "$_vi_p" in
      ''|*[!0-9]*) return 1 ;;
    esac
    [ "${#_vi_p}" -le 3 ] && [ "$_vi_p" -le 255 ] || return 1
    _vi_n=$((_vi_n + 1))
    [ "$_vi_n" -le 4 ] || return 1
    case "$_vi_o" in
      *.*) _vi_o=${_vi_o#*.} ;;
      *) _vi_o='' ;;
    esac
  done
  [ "$_vi_n" = 4 ]
}
# Only OUR pref-19903 rule counts: a foreign rule with the same from/lookup must never be
# reported as the console lane, and must never be deleted either.
console_rule_present() { # console_rule_present <ip>
  ip rule show 2>/dev/null | grep -q "^19903:.*from $1 lookup $CTBL"
}
if console_lock; then
  if [ -f "$CS" ] && [ -r "$CS" ]; then
    CEN=$(sed -n 's/^enabled=//p' "$CS" 2>/dev/null | head -1)
    CIP=$(sed -n 's/^ip=//p' "$CS" 2>/dev/null | head -1)
    if [ "$CEN" = "1" ]; then
      if [ -z "$CIP" ]; then
        RC_DEGRADED=1
        log "DEGRADED: console lane is enabled but ip= is empty - the lane has no rule"
      elif ! valid_ipv4 "$CIP"; then
        RC_DEGRADED=1
        log "DEGRADED: console lane is enabled with an invalid ip '$CIP' - the lane has no rule"
      else
        # The table first: the rule below sends the console into it, and a table without the LAN
        # prefixes hides the local network from the console. Sourced in a subshell so the shared
        # helper cannot leak its variables into this script. The route lock is a TRY-LOCK here:
        # this block already holds `console`, and waiting up to 20 s for `route-102` would make
        # `console off` fail for that whole window. A busy table is skipped and reported; the next
        # guard run (5 min) retries. No-wait in the subshell, without changing the default for
        # every other caller.
        if [ -r "$CROUTES" ]; then
          ( GM_ROUTE_LOCK_WAIT=0; . "$CROUTES" && geomask_routes_sync "$CTBL" ) >/dev/null 2>&1 \
            || { RC_DEGRADED=1; log "WARN console table $CTBL not synced"; }
        else
          RC_DEGRADED=1
          log "WARN console routes helper missing ($CROUTES)"
        fi
        if ! console_rule_present "$CIP"; then
          # Only our own stale 19903 rule may be removed. Deleting `from <ip> lookup 102`
          # without a pref also deleted a foreign rule (15000: from <ip> lookup 102) and
          # then claimed the lane was "re-seated" (R10a P3).
          while ip rule del pref 19903 from "$CIP" lookup "$CTBL" 2>/dev/null; do :; done
          if ip rule add pref 19903 from "$CIP" lookup "$CTBL" 2>/dev/null; then
            log "console rule re-seated for $CIP"
            conntrack -D -s "$CIP" >/dev/null 2>&1
            conntrack -D -d "$CIP" >/dev/null 2>&1
          else
            RC_DEGRADED=1
            log "FAIL console rule add $CIP"
          fi
        fi
        # A default through another device is not "a default": after a reboot netifd can leave
        # one via the WAN in every table, and the console would use the real address.
        if ! ip route show table "$CTBL" 2>/dev/null | grep -q "^default dev $CTUN"; then
          if ip route replace table "$CTBL" default dev "$CTUN" 2>/dev/null; then
            log "console table $CTBL default restored (dev $CTUN)"
          else
            RC_DEGRADED=1
            log "WARN console table $CTBL has no default (tunnel down?)"
          fi
        fi
      fi
    elif [ "$CEN" = "0" ]; then
      # The lane was switched off. Remove only the rule this guard owns (pref 19903): a
      # foreign rule with the same from/lookup is somebody else's and must survive.
      if [ -n "$CIP" ] && console_rule_present "$CIP"; then
        if ip rule del pref 19903 from "$CIP" lookup "$CTBL" 2>/dev/null; then
          log "console rule removed for $CIP (lane off)"
          conntrack -D -s "$CIP" >/dev/null 2>&1
          conntrack -D -d "$CIP" >/dev/null 2>&1
        else
          RC_DEGRADED=1
          log "FAIL console rule removal $CIP"
        fi
      fi
    else
      RC_DEGRADED=1
      log "DEGRADED: console state has an unknown enabled='$CEN' - not touching the lane"
    fi
  else
    # A missing state file is the normal "console lane was never configured" case. An
    # existing path that is not a readable regular file is different: the block used to
    # disappear silently and report rc=0 (R10a P3). Say it and do not touch the lane.
    if [ -e "$CS" ] || [ -L "$CS" ]; then
      RC_DEGRADED=1
      log "DEGRADED: console state unreadable ($CS) - not touching the lane"
    fi
  fi
  gm_lock_release console
else
  case "$CONSOLE_LOCK_RC" in
    1)
      RC_CONSOLE_BUSY=1
      log "SKIP console watchdog: lane lock is busy - another writer owns it" ;;
    *)
      RC_INPUT_UNKNOWN=1
      log "UNKNOWN: the console lock primitive is unavailable - refusing to touch the console lane" ;;
  esac
fi

# ---------- (D) AI-DNS self-heal ----------
# A transient failure (alias not up yet, ip unavailable) must not leave the AI clients with a
# permanent reject rule until the owner re-runs the panel step. The sync is cheap when nothing
# changed (it compares the generated include and reloads the firewall only on a difference).
[ -x /usr/bin/geo-backend.sh ] && ( /usr/bin/geo-backend.sh aidns >/dev/null 2>&1 & )

# ---------- conntrack flush when something changed ----------
if [ -n "$NEED_FLUSH" ] && [ -n "$IPS" ]; then
  for ip in $IPS; do
    conntrack -D -s "$ip" >/dev/null 2>&1
    conntrack -D -d "$ip" >/dev/null 2>&1
  done
  log "conntrack flushed for: $IPS"
fi

# Input/lock unknown is the most serious answer: nothing may be claimed healthy.
if [ "$RC_INPUT_UNKNOWN" = "1" ]; then
  exit 2
fi
if [ "$RC_CONSOLE_BUSY" = "1" ]; then
  exit 75
fi
if [ "$RC_DEGRADED" = "1" ]; then
  exit 1
fi
exit 0
