#!/bin/sh
# geomask-routes.sh - mirror the LAN-reachable prefixes into a policy table.
#
# Why this file exists: the previous implementation copied EVERY non-default route from
# `main` into table 100/101 and deleted every entry of that table that was not in `main`.
# That made geomask the owner of routes it never created: a foreign PBR/WireGuard route was
# removed, and a public prefix that appeared on the WAN after the first setup was mirrored
# into the tunnel table, where longest-prefix match beats `default dev Mihomo` (leak or
# blackhole). When `ip route show table main` failed, the empty snapshot was taken as truth
# and every non-default route was deleted. Audit 14.11.2026, F01/F02.
#
# The contract now:
#   * a route is mirrored when its device carries a LAN-zone address AND it is either
#     connected (scope link) or reaches a gateway inside a connected LAN subnet (a second
#     router / routed subnet behind the LAN must stay reachable);
#   * a route through the WAN (public gateway) is never mirrored;
#   * only routes recorded as ours (the owned file, full route lines) are replaced or
#     removed; a foreign entry on the same prefix is left alone;
#   * the owned file is an ownership RECORD, not a write-ahead log: it lists the routes this
#     helper installed and that are still installed. A refused replace/delete keeps the old
#     record, so the next sync still recognises its own route and retries it; a route that is
#     no longer installed drops out of the record. Every phase compares ONE structural key
#     (prefix/via/dev/src, kernel decoration ignored), so a normalised installed form is the
#     same route for the occupant check, the delete and the record; a foreign route whose key
#     differs is never claimed or deleted, and only the indistinguishable same-key case is the
#     documented residual (R12).
#
# R05: the helper also owns the QUEUE between netifd and the writer. The ifup hotplug path
# must never wait for the route lock (it runs inside netifd's ifup and could stall PPPoE/WAN),
# so it only records `pending-<table>` and spawns one detached worker. The worker holds the
# `routes-worker` lock (rank 35, single flight), takes `route-<table>` (rank 40) per attempt
# with GM_ROUTE_LOCK_WAIT=2, and applies the default route/rules that used to live in the
# hotplug script. A busy or unreadable table is retried a bounded number of times; a failed
# mutation is never retried inside the worker. R12 owns ownership reconciliation: such an
# event is parked with a `retry-<table>` marker, and the next drain turns that marker into one
# new attempt (a stable link sends no ifup event that could re-arm it).
#
# Return codes of geomask_routes_sync (the reason the queue can decide retry vs park):
#   0  the table is consistent with `desired`
#   75 busy: another writer owns route-<table> (retryable)
#   76 retryable read/precondition failure BEFORE any mutation (retryable)
#   77 a mutation failed: replace/delete, the write of the owned record, the read-back of the
#      table after a mutation, or a foreign occupant blocked ours (NOT retried)
#   2  the lock primitive is unavailable (fail closed; the caller must refuse to mutate)
#   4  lock order inversion (caller bug; never retried)
# 1 is not used any more: a generic failure could not tell "retry" from "fail closed".
#
# State root: GM_ROUTE_STATE_DIR (default /tmp/geomask-routes), files:
#   pending-<table>  event written by hotplug (temp file + atomic rename), never by the worker
#   work-<table>     the event claimed by the running worker; removed only after success
#   error-<table>    last failure/timeout text (timestamp, rc, attempts); still read by the
#                    installer's step 7 check
#   parked-<table>   the worker gave up after the bounded retries; a NEW pending event for the
#                    same table re-arms it (gm_route_enqueue removes parked-<table>)
#   retry-<table>    R12: a park with rc=77 leaves it, and the next gm_route_drain converts it
#                    into one pending event. It is NOT a queue entry: _gm_route_has_queue
#                    ignores it, so an ifup never spawns a worker for a marker. A success or a
#                    new enqueue removes it.

GM_ROUTE_STATE_DIR=${GM_ROUTE_STATE_DIR:-/tmp/geomask-routes}

_gm_route_owned() { printf '%s/owned-%s' "$(_gm_route_dir)" "$1"; }

# The queue directory is always resolved here, never captured at source time: the installer
# and the tests inject GM_ROUTE_STATE_DIR after the helper has been sourced.
_gm_route_dir() { printf '%s' "${GM_ROUTE_STATE_DIR:-/tmp/geomask-routes}"; }

_gm_route_state_file() { printf '%s/%s-%s' "$(_gm_route_dir)" "$1" "$2"; }

_gm_route_networks() { # networks of the LAN firewall zone
  _rn=""
  for _rz in $(uci -q show firewall 2>/dev/null | sed -n 's/^firewall\.\([^.=]*\)=zone$/\1/p'); do
    [ "$(uci -q get "firewall.$_rz.name" 2>/dev/null)" = lan ] || continue
    for _rnet in $(uci -q get "firewall.$_rz.network" 2>/dev/null); do
      case " $_rn " in *" $_rnet "*) ;; *) _rn="$_rn $_rnet" ;; esac
    done
  done
  printf '%s' "$_rn"
}

_gm_route_devices() { # devices that may carry a LAN address
  _rdv=""
  for _rnet in $(_gm_route_networks); do
    _rdev=$(uci -q get "network.$_rnet.device" 2>/dev/null)
    [ -n "$_rdev" ] || _rdev=$(uci -q get "network.$_rnet.ifname" 2>/dev/null)
    case "$_rdev" in
      '') _rdev="br-$_rnet" ;;          # a bridge is named after its network
      *" "*) _rdev="br-$_rnet" ;;       # a list of ports is a bridge
    esac
    case " $_rdv " in *" $_rdev "*) ;; *) _rdv="$_rdv $_rdev" ;; esac
  done
  _rlan=$(uci -q get network.lan.device 2>/dev/null)
  [ -n "$_rlan" ] || _rlan=$(uci -q get network.lan.ifname 2>/dev/null)
  [ -n "$_rlan" ] || _rlan=br-lan
  case " $_rdv " in *" $_rlan "*) ;; *) _rdv="$_rdv $_rlan" ;; esac
  # WireGuard peers are remote LAN clients: their subnet must stay reachable in the table
  # as well, or every peer loses the local network the moment the tunnel table is used.
  if ip link show Wireguard >/dev/null 2>&1; then
    case " $_rdv " in *" Wireguard "*) ;; *) _rdv="$_rdv Wireguard" ;; esac
  fi
  printf '%s' "$_rdv"
}

_gm_route_normalize() { # stdin: a.b.c.d/len -> network/len
  awk -F'[./]' '
    NF >= 5 {
      a=$1*16777216+$2*65536+$3*256+$4; p=$5+0
      if (p < 1 || p > 32) next
      size=2^(32-p); net=int(a/size)*size
      printf "%d.%d.%d.%d/%d\n", int(net/16777216)%256, int(net/65536)%256, int(net/256)%256, net%256, p
    }'
}

# Connected prefixes of the LAN devices, derived from the addresses themselves. Used for the
# gateway check and as the fallback when the routing table cannot be read: a policy table
# with a default route must keep the LAN reachable. The line names `src` so the fallback
# `desired` line is structurally identical to the installed form (R12 round 2: without it
# every later sync would call our own normalised route foreign).
_gm_route_local_lines() { # prints "<network/len> dev <device> src <address>"
  for _rp_dev in $(_gm_route_devices); do
    _rp_seen=""
    for _rp in $(ip -4 addr show "$_rp_dev" 2>/dev/null \
      | sed -n 's/.*inet \([0-9][0-9.]*\/[0-9][0-9]*\).*/\1/p'); do
      _rnet=$(printf '%s\n' "$_rp" | _gm_route_normalize)
      [ -n "$_rnet" ] || continue
      # One line per prefix, with the FIRST address as src (the primary one on OpenWrt): an
      # alias on the same prefix must not become the source address of the desired route.
      case " $_rp_seen " in *" $_rnet "*) continue ;; esac
      _rp_seen="$_rp_seen $_rnet"
      printf '%s dev %s src %s\n' "$_rnet" "$_rp_dev" "${_rp%%/*}"
    done
  done
}

_gm_route_in_prefix() { # _gm_route_in_prefix <ip> <prefix/cidr> -> 0 when inside
  awk -v ip="$1" -v net="$2" 'BEGIN{
    n=split(ip,A,"."); split(net,B,"/");
    m=split(B[1],C,"."); p=B[2]+0;
    if (n!=4 || m!=4) exit 1;
    for (i=1;i<=4;i++){ if (A[i]!~/^[0-9]+$/ || A[i]>255) exit 1; if (C[i]!~/^[0-9]+$/ || C[i]>255) exit 1 }
    a=((A[1]*256+A[2])*256+A[3])*256+A[4];
    b=((C[1]*256+C[2])*256+C[3])*256+C[4];
    size=2^(32-p); exit ((int(a/size)==int(b/size)) ? 0 : 1)
  }'
}

_gm_route_cleanup() { rm -f "$@" 2>/dev/null; }

# One writer per table. The hotplug path and the panel can fire together (a reconnect while the
# owner presses "apply"), and both read the owned file and then rewrite it: the slower one
# erased the faster one's prefixes, and a prefix left in the table without an owner can never be
# cleaned up again (review 15.11, #22). The lock is the kernel lock of geomask-lock.sh - one
# persistent inode, no PID, no reclaim; a killed sync is released by the kernel. Waiting is
# bounded and every caller of the QUEUE path sets GM_ROUTE_LOCK_WAIT explicitly: a busy table
# means "try again shortly", never "work without it" and never a stalled ifup.
GM_LOCK_LIB=${GM_LOCK_LIB:-/usr/lib/geomask/geomask-lock.sh}
if ! command -v gm_lock_acquire >/dev/null 2>&1 && [ -r "$GM_LOCK_LIB" ]; then
  . "$GM_LOCK_LIB"
fi
GM_ROUTE_LOCK_WAIT=${GM_ROUTE_LOCK_WAIT:-20}
# Worker bounds (R05). The backoff and the deadline are environment-overridable so the
# off-router tests can exercise the retry/park path without sleeping for half a minute; the
# defaults are the shipped contract.
GM_ROUTE_TUN_WAIT=${GM_ROUTE_TUN_WAIT:-20}
GM_ROUTE_WORKER_CAP=${GM_ROUTE_WORKER_CAP:-30}
GM_ROUTE_ATTEMPTS=${GM_ROUTE_ATTEMPTS:-5}
GM_ROUTE_ACTIVATIONS=${GM_ROUTE_ACTIVATIONS:-3}
GM_ROUTE_BACKOFF=${GM_ROUTE_BACKOFF:-1 2 4 8}
GM_ROUTE_WORKER_LOCK=${GM_ROUTE_WORKER_LOCK:-routes-worker}
GM_ROUTE_DRAIN=${GM_ROUTE_DRAIN:-0}

_gm_route_lock() { # _gm_route_lock <table> -> 0, 75, 76, 77, 2 or 4 (see the header)
  command -v gm_lock_acquire >/dev/null 2>&1 || {
    echo "geomask-routes: lock helper missing ($GM_LOCK_LIB) - refusing to touch table $1" >&2
    return 2
  }
  # A malformed budget is a caller bug, not a transient error (the helper refuses it).
  case "${GM_ROUTE_LOCK_WAIT:-}" in
    ''|*[!0-9]*) return 2 ;;
  esac
  # rc=2 has two very different meanings in the helper: the primitive is missing (terminal,
  # fail closed) or the lock directory/file could not be created/opened (ENOSPC/EACCES: a
  # transient condition that deserves a retry, not a park). Ask the backend first.
  if command -v gm_lock_backend >/dev/null 2>&1; then
    _gl_backend=$(gm_lock_backend 2>/dev/null)
    [ -n "$_gl_backend" ] || _gl_backend=missing
  else
    _gl_backend=missing
  fi
  [ "$_gl_backend" = missing ] && return 2
  gm_lock_acquire "route-$1" "$GM_ROUTE_LOCK_WAIT"
  case "$?" in
    0) return 0 ;;
    1) return 75 ;;      # busy: another writer owns this table right now
    4) return 4 ;;       # rank inversion: a caller bug, never retryable
    3) return 4 ;;       # re-entrant acquire: same class of caller bug
    *) return 76 ;;      # backend exists: directory/open failure is transient (retryable)
  esac
}

_gm_route_unlock() { # releases only our own kernel lock; nothing is deleted
  command -v gm_lock_release >/dev/null 2>&1 || return 0
  gm_lock_release "route-$1"
  return 0
}

geomask_routes_sync() { # geomask_routes_sync <table> -> rc per the header
  _rt=$1
  case "$_rt" in ''|*[!0-9]*) return 77 ;; esac
  ( umask 077; mkdir -p "$(_gm_route_dir)" ) 2>/dev/null || return 76
  _gm_route_lock "$_rt"; _rl=$?
  [ "$_rl" = 0 ] || return "$_rl"
  _owned=$(_gm_route_owned "$_rt")
  _state=$(_gm_route_dir)
  _main="$_state/main-$_rt.$$"
  _want="$_state/want-$_rt.$$"
  _cur="$_state/cur-$_rt.$$"
  _occ="$_state/occ-$_rt.$$"
  _after="$_state/after-$_rt.$$"
  # R12: the record is written through a per-process temp and an atomic rename in the same
  # directory, so a refused or killed write can never truncate the file that is still valid.
  _owned_new="$_state/owned-$_rt.tmp.$$"
  _post="$_state/post-$_rt.$$"
  _k_owned="$_state/kown-$_rt.$$"
  _k_want="$_state/kwant-$_rt.$$"
  _foreign="$_state/foreign-$_rt.$$"
  _del="$_state/del-$_rt.$$"

  _devs=$(_gm_route_devices)
  _local_lines=$(_gm_route_local_lines)
  _locals=$(printf '%s\n' "$_local_lines" | awk 'NF >= 3 {print $1}')

  : > "$_want"
  if ip route show table main > "$_main" 2>/dev/null && [ -s "$_main" ]; then
    while IFS= read -r _r; do
      case "$_r" in ''|default*) continue ;; esac
      _rdev=$(printf '%s\n' "$_r" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
      [ -n "$_rdev" ] || continue
      case " $_devs " in *" $_rdev "*) ;; *) continue ;; esac
      case "$_r" in
        *" scope link"*) printf '%s\n' "$_r" >> "$_want"; continue ;;
      esac
      # A route through a gateway is mirrored only when the gateway lives inside a connected
      # LAN subnet (second router / routed subnet); a WAN gateway is not our business.
      _rgw=$(printf '%s\n' "$_r" | sed -n 's/.* via \([^ ]*\).*/\1/p')
      [ -n "$_rgw" ] || continue
      for _lp in $_locals; do
        if _gm_route_in_prefix "$_rgw" "$_lp"; then
          printf '%s\n' "$_r" >> "$_want"
          break
        fi
      done
    done < "$_main"
    rm -f "$_main"
  elif [ -n "$_local_lines" ]; then
    # No readable table: mirror at least the connected LAN prefixes, or the policy table
    # would send the LAN into the tunnel.
    printf '%s\n' "$_local_lines" >> "$_want"
  else
    _gm_route_cleanup "$_main" "$_want" "$_cur" "$_occ"
    _gm_route_unlock "$_rt"
    return 76
  fi
  if [ ! -s "$_want" ]; then
    _gm_route_cleanup "$_main" "$_want" "$_cur" "$_occ"
    _gm_route_unlock "$_rt"
    return 76
  fi

  # What is installed now. Ownership is decided against this snapshot, and a failed read
  # means nothing is touched at all: a blind `replace` could take a route somebody else owns.
  if ! ip route show table "$_rt" > "$_cur" 2>/dev/null; then
    _gm_route_cleanup "$_main" "$_want" "$_cur" "$_occ"
    _gm_route_unlock "$_rt"
    return 76
  fi

  # A record that exists but cannot be read would make every occupant look foreign (no
  # mutation, no loss) yet would also drop our entries from the reconciled file. Fail closed
  # before the first mutation: a directory in place of the file, a broken mode.
  if [ -e "$_owned" ] && { [ ! -f "$_owned" ] || [ ! -r "$_owned" ]; }; then
    _gm_route_cleanup "$_main" "$_want" "$_cur" "$_occ"
    _gm_route_unlock "$_rt"
    return 76
  fi

  # R12 (round 2): ONE structural key, used by every phase - not only for the read-back.
  #   key = prefix|via|dev|src, where a missing src is "-" (so a line that carries a foreign
  #   src never matches a line without one), the destination may follow a route type word, and
  #   kernel decoration (proto, metric, expires, linkdown, mtu, scope, table) is not part of
  #   the key. An occupant is ours when its key is in the record or in `desired`; a recorded
  #   line is installed when its key is in the fresh table; the new record keeps installed
  #   lines whose key is in the old record or in `desired`. A stale/kernel-normalised record
  #   therefore stays useful in Phase 1 and Phase 3 as well, and a foreign route whose
  #   prefix/via/dev/src differs is never ours, never deleted and never recorded.
  _route_awk='
    function key(s,   n, a, i, via, dev, src, pfx, first) {
      n = split(s, a, " ")
      if (n == 0) return ""
      first = a[1]
      pfx = first
      if (first == "unicast" || first == "blackhole" || first == "unreachable" || first == "prohibit" || first == "throw" || first == "local" || first == "broadcast" || first == "multicast" || first == "anycast" || first == "nat") pfx = a[2]
      via = "-"; dev = "-"; src = "-"
      for (i = 2; i <= n; i++) {
        if (a[i] == "via") via = a[i+1]
        else if (a[i] == "dev") dev = a[i+1]
        else if (a[i] == "src") src = a[i+1]
      }
      return pfx "|" via "|" dev "|" src
    }
    FILENAME == of { okeys[$0] = 1; next }
    FILENAME == wf { wkeys[$0] = 1; next }
    FILENAME == df { dkeys[$0] = 1; next }
    mode == "keys" { if (NF > 0) print key($0); next }
    mode == "foreign" { if ((key($0) in okeys) || (key($0) in wkeys)) next; print; next }
    mode == "delete" { if (key($0) in wkeys) next; if (key($0) in okeys) print; next }
    mode == "record" { if ((key($0) in okeys) || (key($0) in dkeys)) print; next }
  '
  _prep=0
  : > "$_k_owned" 2>/dev/null || _prep=1
  : > "$_k_want" 2>/dev/null || _prep=1
  if [ "$_prep" = 0 ] && [ -f "$_owned" ]; then
    awk -v mode=keys "$_route_awk" "$_owned" > "$_k_owned" 2>/dev/null || _prep=1
  fi
  if [ "$_prep" = 0 ]; then
    awk -v mode=keys "$_route_awk" "$_want" > "$_k_want" 2>/dev/null || _prep=1
  fi
  if [ "$_prep" != 0 ]; then
    _gm_route_cleanup "$_main" "$_want" "$_cur" "$_occ" "$_k_owned" "$_k_want"
    _gm_route_unlock "$_rt"
    return 76
  fi

  _fail=0
  _wrote=""
  # Phase 1+2 (R12): authorise the prefix, then replace it.
  while IFS= read -r _r; do
    [ -n "$_r" ] || continue
    _pfx=${_r%% *}
    # Only a route we recorded may be replaced, and the occupants of the prefix - not the
    # desired line - decide ownership. Comparing `desired` with `owned` skipped every
    # replacement (on A->B, B is not in `owned` yet, so B was dropped and A removed), and the
    # removal then went by prefix, which cannot tell two entries of one prefix apart. N01.
    # The prefix may sit at any field (`10.0.0.0/24 dev ...`, `blackhole 10.0.0.0/24`), so the
    # match is by whole word, not by the first column; a typed route is foreign by definition.
    awk -v p="$_pfx" '{ for (i = 1; i <= NF; i++) if ($i == p) { print; break } }' "$_cur" > "$_occ"
    if [ -s "$_occ" ]; then
      # An occupant is ours when its structural key is in the record OR equals a desired line:
      # that accepts the decorated/kernel-normalised installed form of our own route (a stale
      # raw record, a lost record after a wiped /tmp, a refused write of the record) and still
      # refuses a route whose prefix/via/dev/src differ. The old byte-identical test missed the
      # normalised form and called our own route foreign (R12 review, round 2).
      : > "$_foreign"
      _fo=0
      awk -v mode=foreign -v of="$_k_owned" -v wf="$_k_want" "$_route_awk" \
        "$_k_owned" "$_k_want" "$_occ" > "$_foreign" 2>/dev/null || _fo=1
      if [ "$_fo" != 0 ] || [ -s "$_foreign" ]; then
        # A foreign entry on this prefix stays, and our own entries on it are left alone too:
        # rc=77, because the table is not what `desired` wants and repeating the same replace
        # cannot fix an ownership conflict. The old record keeps our line, so a later sync
        # still recognises it (R12: it used to drop the line and lock the prefix as foreign).
        _fail=1
        continue
      fi
    fi
    if ip route replace table "$_rt" $_r 2>/dev/null; then
      _wrote="$_wrote $_pfx"
    else
      # R12: the old line (if any) stays installed and keeps its record; the next sync sees it
      # as ours and retries instead of declaring it foreign.
      _fail=1
    fi
  done < "$_want"

  # Phase 3 (R12): remove recorded routes that are no longer wanted. The delete names the line
  # that is ACTUALLY installed (the kernel may print a decorated/normalised form) and is
  # matched by structural key against the record, so a foreign route that only shares the
  # prefix is not deleted. The snapshot is taken AFTER Phase 2: a successful `replace` has
  # already changed the old line, and deleting that old text would fail on a real kernel
  # (R12 review, round 2). A refused delete leaves the line installed; Phase 4 keeps its
  # record, so the next sync retries the delete (it used to become a silent orphan, rc 0).
  if [ -s "$_k_owned" ]; then
    if ! ip route show table "$_rt" > "$_post" 2>/dev/null; then
      _fail=1
    else
      : > "$_del"
      _dbo=0
      awk -v mode=delete -v of="$_k_owned" -v wf="$_k_want" "$_route_awk" \
        "$_k_owned" "$_k_want" "$_post" > "$_del" 2>/dev/null || _dbo=1
      if [ "$_dbo" != 0 ]; then
        _fail=1
      else
        while IFS= read -r _d; do
          [ -n "$_d" ] || continue
          ip route del table "$_rt" $_d 2>/dev/null || _fail=1
        done < "$_del"
      fi
    fi
  fi

  # Phase 4 (R12): the record must describe what is installed now, and only what is ours.
  # `_new` (the lines this run managed to replace) was the old source of the record: a refused
  # replace or delete dropped a live route from it, so the next sync declared that route
  # foreign and never retried it (refused replace) or never removed it (refused delete).
  #
  # Exactly two kinds of installed lines may be recorded:
  #   (a) a line whose structural key is in the OLD record (a refused delete leaves the line
  #       installed; a stale or kernel-normalised record still recognises it);
  #   (b) a line whose structural key equals a desired line (a byte-identical desired line
  #       returns to the record even when a foreign neighbour blocked its replace).
  # `_route_awk` is the one key used here, in Phase 1 and in Phase 3: prefix/via/dev/src with
  # kernel decoration ignored. A prefix alone is NOT a reason to record a line - after
  # `replace` another writer can add a route on the same prefix before the read-back, and the
  # old `prefix in _wrote` rule recorded that foreign line (R12 review, D1). A route that
  # disappeared on its own drops out instead of being trusted blindly.
  if ! ip route show table "$_rt" > "$_after" 2>/dev/null; then
    # The mutations already happened, so this is a mutation-reporting failure (77), never the
    # retryable pre-mutation read (76): a 76 would make the caller repeat the mutations. The
    # old record is the conservative claim and is left untouched.
    _fail=1
  else
    _wf=0
    if [ ! -s "$_after" ] && { [ -s "$_owned" ] || [ -n "$_wrote" ]; }; then
      # A successful read that returns NOTHING while we own or wrote routes is not an empty
      # table: recording it would erase live ownership. Keep the old record and report 77.
      _wf=1
    elif ! awk -v mode=record -v of="$_k_owned" -v df="$_k_want" "$_route_awk" \
         "$_k_owned" "$_k_want" "$_after" > "$_owned_new" 2>/dev/null; then
      _wf=1
    fi
    if [ "$_wf" != 0 ]; then
      # The filtered record could not be written completely (or the read-back was empty):
      # keep the old file - a truncated record would lose routes forever - and report 77.
      _fail=1
    else
      chmod 600 "$_owned_new" 2>/dev/null
      # A rename in the same directory is the write: the old record survives any failure,
      # including ENOSPC/EROFS and a killed sync (R12: `cp -f` truncated it first).
      mv -f "$_owned_new" "$_owned" 2>/dev/null || _fail=1
    fi
  fi

  _gm_route_cleanup "$_main" "$_want" "$_cur" "$_occ" "$_post" "$_after" \
    "$_owned_new" "$_k_owned" "$_k_want" "$_foreign" "$_del"
  _gm_route_unlock "$_rt"
  if [ "$_fail" = 0 ]; then
    # R12 (D5): a successful sync proves the table is consistent again, so a retry marker left
    # by an earlier hard failure has done its job. This also covers a direct sync from the
    # console/panel, which never goes through the worker's own success branch.
    rm -f "$_state/retry-$_rt" 2>/dev/null
    return 0
  fi
  return 77
}

# ---------------------------------------------------------------- R05 queue and worker

_gm_route_log() {
  command -v logger >/dev/null 2>&1 && logger -t geomask "$*" 2>/dev/null
  return 0
}

# Remove stale per-process temp files (a writer killed between create and rename leaves
# `pending-<t>.<pid>`, a killed sync leaves `want-<t>.<pid>` etc.). Only files older than a
# minute are touched: a live enqueue may legitimately hold its temp for a few milliseconds.
_gm_route_prune_temps() {
  _pt_dir=$(_gm_route_dir)
  [ -d "$_pt_dir" ] || return 0
  find "$_pt_dir" -type f -name '*.[0-9]*' -mmin +1 \
    -exec rm -f {} \; 2>/dev/null
  return 0
}

# Record an ifup event. Temp file + rename: a concurrent reader never sees a partial file, and
# a failed write never leaves a pending event behind. rc: 0 accepted, 1 not written,
# 2 no lock primitive (an event nobody could ever apply must not be reported as accepted).
gm_route_enqueue() { # gm_route_enqueue <table>
  _ge_t=$1
  case "$_ge_t" in ''|*[!0-9]*) return 1 ;; esac
  command -v gm_lock_backend >/dev/null 2>&1 || return 2
  case "$(gm_lock_backend)" in missing) return 2 ;; esac
  _ge_dir=$(_gm_route_dir)
  ( umask 077; mkdir -p "$_ge_dir" ) 2>/dev/null || return 1
  _ge_tmp="$_ge_dir/pending-$_ge_t.$$"
  # The generation is timestamp + pid without spaces: the worker uses it to tell a NEW event
  # for an already-processed table from a deferred one it must not retry in the same drain.
  if ! printf '%s.%s\n' "$(date +%s 2>/dev/null)" "$$" > "$_ge_tmp" 2>/dev/null; then
    rm -f "$_ge_tmp" 2>/dev/null
    return 1
  fi
  chmod 600 "$_ge_tmp" 2>/dev/null
  mv -f "$_ge_tmp" "$_ge_dir/pending-$_ge_t" 2>/dev/null || {
    rm -f "$_ge_tmp" 2>/dev/null
    return 1
  }
  # A new event re-arms a table that the worker had parked (it is also the trigger R12 needs,
  # so the retry marker of a previous hard failure has done its job).
  rm -f "$_ge_dir/parked-$_ge_t" 2>/dev/null
  rm -f "$_ge_dir/retry-$_ge_t" 2>/dev/null
  return 0
}

# Queue entries only: pending and work. `retry-<table>` (R12) is deliberately NOT one - the
# hotplug path must not spawn a worker for a marker; only a drain turns it into work.
_gm_route_has_queue() {
  _hq_dir=$(_gm_route_dir)
  for _hq_f in "$_hq_dir"/pending-* "$_hq_dir"/work-*; do
    [ -f "$_hq_f" ] || continue
    _hq_t=${_hq_f##*-}
    case "$_hq_t" in ''|*[!0-9]*) continue ;; esac
    return 0
  done
  return 1
}

# Generation of an event file (first line). Older events have a bare timestamp; both forms are
# compared as opaque strings, so no parsing is needed.
_gm_route_gen_of() {
  _gg=$(sed -n '1p' "$1" 2>/dev/null)
  printf '%s' "${_gg:-0}"
}

# Drain fairness: a table generation already processed in THIS drain must not be claimed again
# (a deferred event stays pending with the same generation; a new ifup writes a new one).
_gm_route_done_has() {
  case " $_gw_done " in
    *" $1:$2 "*) return 0 ;;
  esac
  return 1
}

# Claim one event: a leftover work-<table> first (a killed worker), then the pending file,
# renamed atomically. Sets _gw_t, _gw_path and _gw_gen. The caller holds `routes-worker`.
# In drain mode a generation already processed in this drain is skipped, so one busy/deferred
# table cannot starve the others and cannot be retried in a tight loop.
_gm_route_claim() {
  _gc_dir=$(_gm_route_dir)
  for _gc_f in "$_gc_dir"/work-*; do
    [ -f "$_gc_f" ] || continue
    _gc_t=${_gc_f##*-}
    case "$_gc_t" in ''|*[!0-9]*) continue ;; esac
    _gc_gen=$(_gm_route_gen_of "$_gc_f")
    if [ "$_gw_drain" = 1 ] && _gm_route_done_has "$_gc_t" "$_gc_gen"; then continue; fi
    _gw_t=$_gc_t
    _gw_path=$_gc_f
    _gw_gen=$_gc_gen
    return 0
  done
  for _gc_f in "$_gc_dir"/pending-*; do
    [ -f "$_gc_f" ] || continue
    _gc_t=${_gc_f##*-}
    case "$_gc_t" in ''|*[!0-9]*) continue ;; esac
    _gc_gen=$(_gm_route_gen_of "$_gc_f")
    if [ "$_gw_drain" = 1 ] && _gm_route_done_has "$_gc_t" "$_gc_gen"; then continue; fi
    _gw_path="$_gc_dir/work-$_gc_t"
    mv -f "$_gc_f" "$_gw_path" 2>/dev/null || continue
    # A pending event is newer than any parked generation of the same table.
    rm -f "$_gc_dir/parked-$_gc_t" 2>/dev/null
    _gw_t=$_gc_t
    _gw_gen=$_gc_gen
    return 0
  done
  return 1
}

_gm_route_backoff() { # _gm_route_backoff <failed-attempt-number> -> seconds
  _gb_n=0
  for _gb_s in $GM_ROUTE_BACKOFF; do
    _gb_n=$((_gb_n+1))
    if [ "$_gb_n" = "$1" ]; then printf '%s' "$_gb_s"; return 0; fi
  done
  printf '0'
}

# Sleep, but never past the worker deadline (best effort: the shell clock has 1 s steps).
_gm_route_pause() {
  _gp=$1
  case "$_gp" in ''|*[!0-9]*) return 0 ;; esac
  _gp_left=$((_gw_deadline - $(date +%s)))
  [ "$_gp_left" -lt "$_gp" ] && _gp=$_gp_left
  [ "$_gp" -gt 0 ] && sleep "$_gp"
  return 0
}

_gm_route_wait_tun() { # <device> <seconds> -> 0 when the link exists
  _rwt=$2
  case "$_rwt" in ''|*[!0-9]*) _rwt=0 ;; esac
  # Never wait past the worker deadline: a deferred event must not turn the ~30 s cap into
  # "20 s of TUN wait per activation".
  if [ -n "${_gw_deadline:-}" ]; then
    _rwt_left=$((_gw_deadline - $(date +%s)))
    [ "$_rwt_left" -lt 0 ] && _rwt_left=0
    [ "$_rwt_left" -lt "$_rwt" ] && _rwt=$_rwt_left
  fi
  _rwi=0
  while [ "$_rwi" -lt "$_rwt" ]; do
    ip link show "$1" >/dev/null 2>&1 && return 0
    _rwi=$((_rwi+1))
    sleep 1
  done
  ip link show "$1" >/dev/null 2>&1
}

_gm_route_mihomo_tun() {
  _rmt=$(awk '/^tun:/{f=1;next} f&&/^[^ ]/{exit} f&&/^[[:space:]]*device:/{gsub(/^[[:space:]]*device:[[:space:]]*/,"");print;exit}' \
    "${MIHOMO_CONF:-/etc/mihomo/config.yaml}" 2>/dev/null)
  case "$_rmt" in ''|*[!a-zA-Z0-9._-]*) _rmt=Mihomo ;; esac
  printf '%s' "$_rmt"
}

_gm_route_lan_dev() {
  _rld=$(uci -q get network.lan.device 2>/dev/null)
  [ -n "$_rld" ] || _rld=$(uci -q get network.lan.ifname 2>/dev/null)
  [ -n "$_rld" ] || _rld=br-lan
  case "$_rld" in *" "*) _rld=br-lan ;; esac
  printf '%s' "$_rld"
}

# Every policy rule is installed by one helper, because ignoring the exit of `ip rule add`
# reported a healthy apply while no rule existed (audit C05). rc: 0 present/installed,
# 1 refused (the rule is still absent), 2 the table could not be read.
_gm_route_rule_ensure() { # <label> <grep-ere> <ip rule add args...>
  _rre_lbl=$1; _rre_pat=$2; shift 2
  _rre_rules=$(ip rule show 2>/dev/null) || return 2
  printf '%s\n' "$_rre_rules" | grep -qE "$_rre_pat" && return 0
  ip rule add "$@" 2>/dev/null && return 0
  # Another writer may have added the same rule between the read and the add: the state, not
  # the exit code, is what decides.
  _rre_rules=$(ip rule show 2>/dev/null) || return 2
  printf '%s\n' "$_rre_rules" | grep -qE "$_rre_pat" && return 0
  printf 'geomask-routes: rule %s could not be installed\n' "$_rre_lbl" >&2
  return 1
}

# Table 100: the normal LAN policy - LAN prefixes locally, everything else through Mihomo.
# The rules were moved here from the hotplug script (R05): the netifd path may not run ip.
_gm_route_apply_100() {
  _ra_tun=$(_gm_route_mihomo_tun)
  _gm_route_wait_tun "$_ra_tun" "$GM_ROUTE_TUN_WAIT" || return 79  # deferred, no mutation
  geomask_routes_sync 100; _ra_rc=$?
  [ "$_ra_rc" = 0 ] || return "$_ra_rc"
  ip route replace table 100 default dev "$_ra_tun" 2>/dev/null || return 77
  _ra_lan=$(_gm_route_lan_dev)
  _ra_fail=0
  _gm_route_rule_ensure lan-100 "32760:.*iif $_ra_lan lookup 100" \
    priority 32760 iif "$_ra_lan" lookup 100 || _ra_fail=1
  # WireGuard peers are remote LAN clients (10.10.0.0/24). Without this rule their traffic
  # falls through to main (real WAN IP). The rule needs the device to exist.
  if ip link show Wireguard >/dev/null 2>&1; then
    _gm_route_rule_ensure wg-100 'iif Wireguard lookup 100' \
      priority 32759 iif Wireguard lookup 100 || _ra_fail=1
  fi
  # ICMP bypass: above the dynamic magitrickle fwmark base; presence checked by semantics.
  _gm_route_rule_ensure icmp-bypass "iif $_ra_lan ipproto icmp lookup main" \
    priority 19901 iif "$_ra_lan" ipproto icmp lookup main || _ra_fail=1
  # AI-DNS: the DoH worker's sockets are marked 0x64 and sent to table 101.
  if [ ! -f "${GM_AIDNS_OFF:-/etc/geomask-aidns.off}" ] \
     && [ -f "${GM_AIDNS_UID:-/etc/geomask-aidns.uid}" ]; then
    _gm_route_rule_ensure aidns-101 'fwmark 0x64' \
      pref 19902 fwmark 0x64 table 101 || _ra_fail=1
  fi
  # Per-device bypass rules: a low pref keeps them above the magitrickle fwmark base.
  _ra_dev=${DEVICES:-/etc/geo-devices.list}
  if [ -f "$_ra_dev" ]; then
    while read -r _dip _dmode _dname; do
      case "$_dip" in ''|'#'*) continue ;; esac
      [ "$_dmode" = "bypass" ] || continue
      _gm_route_rule_ensure "bypass-$_dip" "from $_dip lookup main" \
        priority 19900 from "$_dip" lookup main || _ra_fail=1
    done < "$_ra_dev"
  fi
  [ "$_ra_fail" = 0 ] || return 77
  return 0
}

# Table 101: the AI lane. The default must NOT be installed when the table could not be
# filled: an AI device with a default but no LAN prefix is black-holed (R05 D7).
_gm_route_apply_101() {
  _rb_tun=${AI_TUN_DEV:-AI}
  _gm_route_wait_tun "$_rb_tun" "$GM_ROUTE_TUN_WAIT" || return 79
  geomask_routes_sync 101; _rb_rc=$?
  [ "$_rb_rc" = 0 ] || return "$_rb_rc"
  ip route replace table 101 default dev "$_rb_tun" 2>/dev/null || return 77
  _rb_dev=${DEVICES:-/etc/geo-devices.list}
  _rb_fail=0
  if [ -f "$_rb_dev" ] && grep -qE '^[0-9.]+[[:space:]]+ai[[:space:]]' "$_rb_dev" 2>/dev/null; then
    while read -r _dip _dmode _dname; do
      case "$_dip" in ''|'#'*) continue ;; esac
      [ "$_dmode" = "ai" ] || continue
      _gm_route_rule_ensure "ai-$_dip" "from $_dip lookup 101" \
        priority 19900 from "$_dip" lookup 101 || _rb_fail=1
    done < "$_rb_dev"
  fi
  [ "$_rb_fail" = 0 ] || return 77
  return 0
}

# Table 102: the console lane. Its rule belongs to the watchdog/panel, the table is filled here
# so the lane survives an ifup before the next guard run. The LAN prefixes are safe to install
# even while the console TUN is down (the original apply_routes did that too); only the default
# waits for the tunnel.
_gm_route_apply_102() {
  _rc_tun=${CONSOLE_TUN_DEV:-${GM_CONSOLE_TUN:-Console}}
  geomask_routes_sync 102; _rc_rc=$?
  [ "$_rc_rc" = 0 ] || return "$_rc_rc"
  _gm_route_wait_tun "$_rc_tun" "$GM_ROUTE_TUN_WAIT" || return 79
  ip route replace table 102 default dev "$_rc_tun" 2>/dev/null || return 77
  return 0
}

_gm_route_activate() { # <table> -> rc of the table's sync/default work
  case "$1" in
    100) _gm_route_apply_100 ;;
    101) _gm_route_apply_101 ;;
    102) _gm_route_apply_102 ;;
    *) return 77 ;;
  esac
}

# Try one table up to max_attempts times. Only 75 (busy) and 76 (retryable read) are retried;
# 77/2/4 are final for this event. Returns the last rc; _gm_route_attempts is the count.
_gm_route_apply_retry() {
  _gr_t=$1
  _gr_max=$2
  _gr_n=0
  _gr_rc=76
  _gm_route_attempts=0
  while [ "$_gr_n" -lt "$_gr_max" ]; do
    [ "$(date +%s)" -lt "$_gw_deadline" ] || break
    _gr_n=$((_gr_n+1))
    _gm_route_activate "$_gr_t"
    _gr_rc=$?
    _gm_route_attempts=$_gr_n
    [ "$_gr_rc" = 0 ] && return 0
    case "$_gr_rc" in
      75|76) ;;
      *) return "$_gr_rc" ;;
    esac
    [ "$_gr_n" -lt "$_gr_max" ] || break
    _gm_route_pause "$(_gm_route_backoff "$_gr_n")"
  done
  return "$_gr_rc"
}

# Give up on this event until a NEW pending event re-arms the table. work -> parked (a rename,
# not a deletion: the generation is still visible for diagnosis) plus a timestamped error file.
_gm_route_park() {
  _pk_t=$1
  _pk_path=$2
  _pk_rc=$3
  _pk_att=$4
  _pk_dir=$(_gm_route_dir)
  _pk_ts=$(date '+%F %T' 2>/dev/null)
  [ -n "$_pk_ts" ] || _pk_ts=$(date 2>/dev/null)
  _pk_tmp="$_pk_dir/error-$_pk_t.$$"
  if printf '%s rc=%s attempts=%s table=%s\n' "$_pk_ts" "$_pk_rc" "$_pk_att" "$_pk_t" \
       > "$_pk_tmp" 2>/dev/null; then
    mv -f "$_pk_tmp" "$_pk_dir/error-$_pk_t" 2>/dev/null
  fi
  rm -f "$_pk_tmp" 2>/dev/null
  # R12: a hard failure (77) leaves a retry marker. The next drain converts it into one more
  # attempt - on a stable link no new ifup arrives, so a parked event would otherwise never be
  # reconciled. 77 keeps its "not retried by the worker" contract: the next attempt happens in
  # a LATER drain run, never in this one. The marker must survive a hostile filesystem: when
  # the atomic rename is refused, write it in place instead of losing the retry (D4).
  if [ "$_pk_rc" = 77 ]; then
    _pk_rm="$_pk_dir/retry-$_pk_t.$$"
    _pk_gen=$(printf '%s.%s' "$(date +%s 2>/dev/null)" "$$")
    _pk_ok=0
    if printf '%s\n' "$_pk_gen" > "$_pk_rm" 2>/dev/null; then
      chmod 600 "$_pk_rm" 2>/dev/null
      mv -f "$_pk_rm" "$_pk_dir/retry-$_pk_t" 2>/dev/null && _pk_ok=1
    fi
    if [ "$_pk_ok" = 0 ]; then
      printf '%s\n' "$_pk_gen" > "$_pk_dir/retry-$_pk_t" 2>/dev/null \
        || _gm_route_log "route table $_pk_t: cannot leave a retry marker after rc=77"
    fi
    rm -f "$_pk_rm" 2>/dev/null
  fi
  mv -f "$_pk_path" "$_pk_dir/parked-$_pk_t" 2>/dev/null || rm -f "$_pk_path" 2>/dev/null
  _gm_route_log "route table $_pk_t parked (rc=$_pk_rc, attempts=$_pk_att) - waiting for a new ifup event"
  return 0
}

# Apply what is already queued. Single-flight on `routes-worker`: a second worker exits at once
# and leaves the pending event to the owner. Every attempt re-sets GM_ROUTE_LOCK_WAIT=2, so a
# caller that exported 0 (hotplug, guard) cannot make the retry loop spin without a budget.
gm_route_worker() {
  # A detached child must not keep the console/updater lock of its parent: flock lives on the
  # open file description, not on the shell. Reset the in-memory stack as well, or the rank
  # check would see a lock this process no longer holds.
  command -v gm_lock_close_inherited >/dev/null 2>&1 && gm_lock_close_inherited
  GM_ROUTE_LOCK_WAIT=2
  export GM_ROUTE_LOCK_WAIT
  _gw_drain=${GM_ROUTE_DRAIN:-0}
  _gw_attempts=$GM_ROUTE_ATTEMPTS
  _gw_activations=$GM_ROUTE_ACTIVATIONS
  _gw_cap=$GM_ROUTE_WORKER_CAP
  case "$_gw_attempts" in ''|*[!0-9]*|0) _gw_attempts=5 ;; esac
  case "$_gw_activations" in ''|*[!0-9]*|0) _gw_activations=3 ;; esac
  case "$_gw_cap" in ''|*[!0-9]*) _gw_cap=30 ;; esac
  if [ "$_gw_drain" = 1 ]; then
    # Guard/cron/installer drain: ONE attempt per table, but EVERY pending table in this call
    # (100/101/102 and a generation that arrived during the sync). No TUN wait, no backoff.
    # A busy/unreadable table is returned to pending, a hard failure parks only that table and
    # the loop continues with the next one (fairness); a transient condition must never stop
    # the others or be parked by a cron run.
    _gw_attempts=1
    _gw_activations=${GM_ROUTE_DRAIN_ACTIVATIONS:-4}
    case "$_gw_activations" in ''|*[!0-9]*|0) _gw_activations=4 ;; esac
    GM_ROUTE_TUN_WAIT=0
    export GM_ROUTE_TUN_WAIT
  fi
  command -v gm_lock_acquire >/dev/null 2>&1 || return 2
  _gm_route_has_queue || return 0
  gm_lock_acquire "$GM_ROUTE_WORKER_LOCK" 0 || return 0   # another worker owns the queue
  _gw_deadline=$(( $(date +%s) + _gw_cap ))
  _gw_act=0
  _gw_rc=0
  _gw_fail=0
  _gw_stop=0
  _gw_attempts_total=0
  _gw_done=""
  while [ "$_gw_act" -lt "$_gw_activations" ] && [ "$_gw_stop" = 0 ]; do
    [ "$(date +%s)" -lt "$_gw_deadline" ] || break
    _gm_route_claim || break
    _gw_act=$((_gw_act+1))
    _gm_route_apply_retry "$_gw_t" "$_gw_attempts"
    _gw_rc=$?
    _gw_attempts_total=$((_gw_attempts_total + _gm_route_attempts))
    case "$_gw_rc" in
      0)
        rm -f "$_gw_path" 2>/dev/null
        rm -f "$(_gm_route_state_file error "$_gw_t")" 2>/dev/null
        rm -f "$(_gm_route_state_file retry "$_gw_t")" 2>/dev/null
        _gm_route_prune_temps
        _gw_rc=0
        _gw_stop=0 ;;
      79)
        # The tunnel is not up (yet). Keep the event, do not park it: a later drain or a new
        # ifup must be able to apply it. work -> pending, so the next worker sees it again.
        mv -f "$_gw_path" "$(_gm_route_state_file pending "$_gw_t")" 2>/dev/null \
          || rm -f "$_gw_path" 2>/dev/null
        [ "$_gw_drain" = 1 ] || _gm_route_log "route table $_gw_t deferred: tunnel is not up"
        _gw_rc=0
        # Drain: this generation is done for this call; continue with the other tables.
        _gw_stop=0
        [ "$_gw_drain" = 1 ] || _gw_stop=1 ;;
      75|76)
        # Retryable: the work file stays, so the next activation claims the SAME event again.
        # Up to _gw_activations rounds of <=5 attempts, never past the deadline. A drain makes
        # exactly one attempt and returns the event to pending - a transient busy must not park
        # it, or a guard run would stop the automation for a condition that clears by itself.
        if [ "$_gw_drain" = 1 ]; then
          mv -f "$_gw_path" "$(_gm_route_state_file pending "$_gw_t")" 2>/dev/null \
            || rm -f "$_gw_path" 2>/dev/null
          _gw_rc=0
          _gw_stop=0
        elif [ "$_gw_act" -lt "$_gw_activations" ] && [ "$(date +%s)" -lt "$_gw_deadline" ]; then
          _gw_stop=0
        else
          _gm_route_park "$_gw_t" "$_gw_path" "$_gw_rc" "$_gw_attempts_total"
          _gw_stop=1
        fi ;;
      *)
        _gm_route_park "$_gw_t" "$_gw_path" "$_gw_rc" "$_gw_attempts_total"
        [ "$_gw_drain" = 1 ] && _gw_fail=$_gw_rc
        [ "$_gw_drain" = 1 ] || _gw_stop=1 ;;
    esac
    # One generation per table per drain call: a deferred event stays pending but is skipped
    # until a NEW enqueue writes a different generation (generation recheck).
    if [ "$_gw_drain" = 1 ] && [ -n "${_gw_gen:-}" ]; then
      _gw_done="$_gw_done $_gw_t:$_gw_gen"
    fi
  done
  gm_lock_release "$GM_ROUTE_WORKER_LOCK"
  if [ "$_gw_drain" = 1 ] && [ "$_gw_fail" != 0 ]; then
    return "$_gw_fail"
  fi
  return "$_gw_rc"
}

# R12: `_gm_route_park` leaves `retry-<table>` after a hard failure (77). A stable PPPoE link
# sends no new ifup event, so the parked event alone would never be reconciled. One drain run
# converts each marker into a pending event (once: a failure during this very drain writes a
# NEW marker, which waits for the next run). A pending/work event for the same table already
# triggers it, so the marker is simply dropped there. It is deliberately NOT a queue entry:
# `_gm_route_has_queue` ignores it, or every ifup would spawn a worker for a marker.
# A refused conversion must KEEP the marker (D4): deleting it on ENOSPC/EACCES would leave the
# table parked with nothing to retry it until an unrelated ifup.
_gm_route_retry_all() {
  _ry_dir=$(_gm_route_dir)
  for _ry_f in "$_ry_dir"/retry-*; do
    [ -f "$_ry_f" ] || continue
    _ry_t=${_ry_f##*-}
    case "$_ry_t" in ''|*[!0-9]*) continue ;; esac
    if [ -f "$_ry_dir/pending-$_ry_t" ] || [ -f "$_ry_dir/work-$_ry_t" ]; then
      rm -f "$_ry_f" 2>/dev/null
      continue
    fi
    mv -f "$_ry_f" "$_ry_dir/pending-$_ry_t" 2>/dev/null \
      || _gm_route_log "route table $_ry_t: retry marker could not become a pending event"
  done
  return 0
}

# Backstop for the guard/cron and the installer: apply the queue inline, without spawning a
# detached worker (the installer holds `maintenance`, and an inline call keeps the result
# observable before it reads error-<table>). Every pending table is processed in one call.
gm_route_drain() {
  GM_ROUTE_DRAIN=1
  GM_ROUTE_TUN_WAIT=0
  export GM_ROUTE_DRAIN GM_ROUTE_TUN_WAIT
  _gm_route_prune_temps
  _gm_route_retry_all
  gm_route_worker
}
