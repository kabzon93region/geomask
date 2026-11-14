#!/bin/sh
# geomask-dnsguard.sh - strict DNS with an emergency reserve.
#
# Owner policy (14.11.2026): resolution goes through the local DoH worker, and a plain public
# resolver is used ONLY when that worker fails. Two incidents shaped this:
#   1) a DoH proxy crashed in a loop while dnsmasq knew no other upstream - every device lost
#      DNS, which looks exactly like "the internet is gone";
#   2) a single upstream list had no order, so dnsmasq could answer from any server and the
#      "strict" promise was never true.
#
# The fallback is implemented by dnsmasq itself: `strictorder` makes it ask servers in the
# listed order and move on only when one fails. No watchdog race, no rewriting at runtime.
#
#   geomask-dnsguard.sh status   -> JSON: is the policy applied, is DoH really answering
#   geomask-dnsguard.sh apply    -> put the DoH worker first and the reserve after it
#   geomask-dnsguard.sh revert   -> restore the exact list and strictorder that were there
#
# Safety rules this file follows, because the first version broke all three:
#   * the original list and strictorder are recorded BEFORE the first write and never
#     overwritten while the policy is applied; without that record nothing is changed;
#   * every failure (add_list, commit, restart, a resolver that stops answering) puts the
#     recorded values back and says so - the router is never left with a half-applied policy;
#   * "DoH is alive" is a real answer from the DoH worker, not the server's own address in
#     nslookup output (that mistake reported SERVFAIL as healthy);
#   * revert restores what was there, including "strictorder was not set at all".
set -u

EMERGENCY="1.1.1.1 8.8.8.8"
STATE=${GM_DNSGUARD_STATE:-/etc/geomask/dnsguard.state}
DNSMASQ_INIT=${GM_DNSMASQ_INIT:-/etc/init.d/dnsmasq}
PROBE_PY=${GM_PROBE_PY:-python3}
TEST_HOST=${GM_DNSGUARD_HOST:-example.com}

say() { printf '%s\n' "$*"; }
fail() { say "{\"ok\":false,\"error\":\"$1\"}"; return 1; }

# ---------------------------------------------------------------- dnsmasq configuration
dnsmasq_list() { uci -q get dhcp.@dnsmasq[0].server 2>/dev/null; }
dnsmasq_strict() { uci -q get dhcp.@dnsmasq[0].strictorder 2>/dev/null; }

is_emergency() {
  case " $EMERGENCY " in *" $1 "*) return 0 ;; esac
  return 1
}

# A local DoH worker appears in the list as 127.0.0.1#PORT (or localhost#PORT).
local_doh_port() {
  for _s in $(dnsmasq_list); do
    case "$_s" in
      127.0.0.1#*|localhost#*) say "${_s#*#}"; return 0 ;;
    esac
  done
  return 1
}

# ---------------------------------------------------------------- probes
# Only an answer that follows the `Name:` line counts. The first `Address:` line of BusyBox
# nslookup is the address of the SERVER; reading it as an answer made SERVFAIL look healthy.
resolve_local() {
  _out=$(nslookup "$TEST_HOST" 127.0.0.1 2>/dev/null) || return 1
  printf '%s\n' "$_out" | awk '
    /^Name:/ { seen = 1; next }
    seen && match($0, /[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/) {
      ip = substr($0, RSTART, RLENGTH)
      if (ip != "0.0.0.0") { print ip; found = 1; exit }
    }
    END { exit found ? 0 : 1 }'
}

# Ask the DoH worker directly on its own port. rc 0 = it answered, 1 = it did not,
# 2 = cannot tell (no python3). Never guess in the caller.
doh_probe() {
  _port=$1
  command -v "$PROBE_PY" >/dev/null 2>&1 || return 2
  "$PROBE_PY" - "$_port" "$TEST_HOST" <<'PY' >/dev/null 2>&1
import random, socket, struct, sys
port, host = int(sys.argv[1]), sys.argv[2]
qid = random.randrange(0, 65536)
name = b''.join(bytes([len(p)]) + p.encode() for p in host.split('.')) + b'\0'
query = struct.pack('!HHHHHH', qid, 0x0100, 1, 0, 0, 0) + name + struct.pack('!HH', 1, 1)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
try:
    s.sendto(query, ('127.0.0.1', port))
    data, _ = s.recvfrom(512)
finally:
    s.close()
if len(data) < 12:
    sys.exit(1)
rid, flags, qd, an, ns, ar = struct.unpack('!HHHHHH', data[:12])
if rid != qid or not (flags & 0x8000) or (flags & 0x000F) != 0 or an < 1:
    sys.exit(1)
sys.exit(0)
PY
}

# ---------------------------------------------------------------- recorded original state
state_get() { sed -n "s/^$1=//p" "$STATE" 2>/dev/null | head -1; }

state_ok() {
  [ -f "$STATE" ] || return 1
  [ "$(state_get version)" = 1 ] || return 1
  [ "$(state_get saved)" = 1 ] || return 1
  case "$(state_get count)" in ''|*[!0-9]*) return 1 ;; esac
  return 0
}

# Record the exact original: the ordered server list (repeated `server=` lines) and the
# strictorder value, including the fact that it was absent. A partial write is not a record.
state_save() {
  state_ok && return 0
  mkdir -p "$(dirname "$STATE")" 2>/dev/null || return 1
  _tmp="$STATE.tmp.$$"
  _count=0
  {
    printf 'version=1\nsaved=1\n'
    _so=$(dnsmasq_strict)
    if [ -n "$_so" ]; then printf 'strictorder=%s\n' "$_so"; else printf 'strictorder=absent\n'; fi
  } > "$_tmp" 2>/dev/null || { rm -f "$_tmp"; return 1; }
  for _s in $(dnsmasq_list); do
    printf 'server=%s\n' "$_s" >> "$_tmp" 2>/dev/null || { rm -f "$_tmp"; return 1; }
    _count=$((_count + 1))
  done
  printf 'count=%s\n' "$_count" >> "$_tmp" 2>/dev/null || { rm -f "$_tmp"; return 1; }
  chmod 600 "$_tmp" 2>/dev/null
  mv -f "$_tmp" "$STATE" 2>/dev/null || { rm -f "$_tmp"; return 1; }
  state_ok
}

state_servers() { sed -n 's/^server=//p' "$STATE" 2>/dev/null; }

# Replace the whole list in ONE uci write. The first version deleted the option and then
# added the entries back; when a later add failed (full disk, refused commit) the original
# list was already gone and the record was the only copy left. A single `uci set` has no
# window in between: either the option holds the new list or it still holds the old one.
list_write() { # list_write <space separated values>
  if [ -n "$1" ]; then
    uci -q set "dhcp.@dnsmasq[0].server=$1" 2>/dev/null
  else
    uci -q delete dhcp.@dnsmasq[0].server 2>/dev/null
  fi
}

state_restore() {
  state_ok || return 1
  set -- $(state_servers)
  _orig="$*"
  list_write "$_orig" || return 1
  _so=$(state_get strictorder)
  if [ "$_so" = absent ] || [ -z "$_so" ]; then
    uci -q delete dhcp.@dnsmasq[0].strictorder 2>/dev/null
  else
    uci -q set "dhcp.@dnsmasq[0].strictorder=$_so" 2>/dev/null || return 1
  fi
  uci -q commit dhcp 2>/dev/null || return 1
  return 0
}

restart_dnsmasq() {
  [ -x "$DNSMASQ_INIT" ] || return 1
  "$DNSMASQ_INIT" restart >/dev/null 2>&1
}

# Undo a half-applied policy and report it. Used by every failure path in apply().
rollback() {
  if state_restore; then
    restart_dnsmasq >/dev/null 2>&1 || true
    fail "$1 - previous DNS settings restored"
  else
    fail "$1 - could not restore the previous DNS settings, check /etc/config/dhcp"
  fi
}

# ---------------------------------------------------------------- status
status() {
  _list=$(dnsmasq_list)
  _so=$(dnsmasq_strict)
  _reserve=0
  for _e in $EMERGENCY; do
    case " $_list " in *" $_e "*) _reserve=1 ;; esac
  done
  _resolved=0
  resolve_local >/dev/null 2>&1 && _resolved=1
  _doh=unknown
  _port=$(local_doh_port 2>/dev/null) && {
    doh_probe "$_port"
    case "$?" in 0) _doh=ok ;; 1) _doh=fail ;; *) _doh=unknown ;; esac
  }
  _mode=off
  if [ "$_reserve" = 1 ] && [ "$_so" = 1 ]; then
    if [ "$_resolved" = 0 ]; then _mode=degraded
    elif [ "$_doh" = ok ]; then _mode=strict
    elif [ "$_doh" = fail ]; then _mode=emergency
    else _mode=applied
    fi
  fi
  printf '{"ok":true,"mode":"%s","resolution":%s,"doh_probe":"%s","reserve":%s,"strictorder":%s,"upstreams":"%s"}\n' \
    "$_mode" "$([ "$_resolved" = 1 ] && echo true || echo false)" "$_doh" \
    "$([ "$_reserve" = 1 ] && echo true || echo false)" \
    "$([ "$_so" = 1 ] && echo true || echo false)" \
    "$(printf '%s' "$_list" | sed 's/"/\\"/g')"
}

# ---------------------------------------------------------------- apply / revert
apply() {
  _doh_port=$(local_doh_port 2>/dev/null) || {
    fail "no local DoH upstream in dnsmasq - strict mode would have nothing to trust"
    return 1
  }
  state_save || { fail "cannot record the current DNS settings - nothing changed"; return 1; }

  # Order: DoH worker, then the emergency reserve, then anything else the owner had, and the
  # domain-scoped zones last (they never take part in the default order).
  _new=""
  for _s in $(dnsmasq_list); do
    case "$_s" in
      127.0.0.1#*|localhost#*) _new="$_new $_s" ;;
    esac
  done
  for _e in $EMERGENCY; do _new="$_new $_e"; done
  for _s in $(dnsmasq_list); do
    case "$_s" in
      127.0.0.1#*|localhost#*) continue ;;
      /*) continue ;;
    esac
    is_emergency "$_s" && continue
    _new="$_new $_s"
  done
  for _s in $(dnsmasq_list); do
    case "$_s" in /*) _new="$_new $_s" ;; esac
  done

  set -- $_new
  _joined="$*"
  list_write "$_joined" || { rollback "cannot rewrite the dnsmasq server list"; return 1; }
  uci -q set dhcp.@dnsmasq[0].strictorder=1 2>/dev/null \
    || { rollback "cannot set strictorder"; return 1; }
  uci -q commit dhcp 2>/dev/null || { rollback "uci commit failed"; return 1; }

  restart_dnsmasq || { rollback "dnsmasq did not restart"; return 1; }
  _i=0
  while [ "$_i" -lt 5 ]; do
    resolve_local >/dev/null 2>&1 && break
    sleep 1; _i=$((_i + 1))
  done
  # A policy that leaves the router unable to resolve is worse than the state we found.
  resolve_local >/dev/null 2>&1 || {
    rollback "the resolver does not answer after the change"
    return 1
  }

  doh_probe "$_doh_port"
  case "$?" in
    0) say '{"ok":true,"mode":"strict"}' ;;
    1) say '{"ok":true,"mode":"emergency","warn":"DoH worker does not answer - resolution uses the emergency reserve"}' ;;
    *) say '{"ok":true,"mode":"applied","warn":"policy applied, but the DoH worker could not be probed"}' ;;
  esac
}

revert() {
  if ! state_ok; then
    say '{"ok":true,"mode":"off","note":"no recorded policy - nothing to revert"}'
    return 0
  fi
  state_restore || { fail "cannot restore the previous DNS settings - record kept"; return 1; }
  restart_dnsmasq || { fail "dnsmasq did not restart - previous list written, record kept"; return 1; }
  _i=0
  while [ "$_i" -lt 5 ]; do
    resolve_local >/dev/null 2>&1 && break
    sleep 1; _i=$((_i + 1))
  done
  resolve_local >/dev/null 2>&1 || {
    fail "the resolver does not answer after revert - record kept"
    return 1
  }
  rm -f "$STATE" 2>/dev/null
  say '{"ok":true,"mode":"off"}'
}

case "${1:-status}" in
  status) status ;;
  apply) apply ;;
  revert) revert ;;
  *) say '{"ok":false,"error":"usage: geomask-dnsguard.sh status|apply|revert"}'; exit 2 ;;
esac
