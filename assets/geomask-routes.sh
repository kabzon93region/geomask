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
#   * the owned file is rewritten from what is really installed, even after a partial
#     failure, so no entry stays in the table without an owner;
#   * a failed read of `main` falls back to the addresses of the LAN devices; if even that
#     fails, nothing is changed and the caller is told (rc=1).

GM_ROUTE_STATE_DIR=${GM_ROUTE_STATE_DIR:-/tmp/geomask-routes}

_gm_route_owned() { printf '%s/owned-%s' "$GM_ROUTE_STATE_DIR" "$1"; }

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
# with a default route must keep the LAN reachable.
_gm_route_local_lines() { # prints "<network/len> dev <device>"
  for _rp_dev in $(_gm_route_devices); do
    ip -4 addr show "$_rp_dev" 2>/dev/null \
      | sed -n 's/.*inet \([0-9][0-9.]*\/[0-9][0-9]*\).*/\1/p' \
      | _gm_route_normalize \
      | while IFS= read -r _rp; do
          [ -n "$_rp" ] && printf '%s dev %s\n' "$_rp" "$_rp_dev"
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
# cleaned up again (review 15.11, #22). BusyBox has no flock everywhere, so the lock is a
# directory with the owner pid inside. Waiting is bounded: a lock that stays busy means "do not
# touch this table now", never "work without it".
GM_ROUTE_LOCK_WAIT=${GM_ROUTE_LOCK_WAIT:-20}

_gm_route_lock() { # _gm_route_lock <table> -> 0 only when this process owns the table
  _gl="$GM_ROUTE_STATE_DIR/lock-$1"
  _gt=0
  while [ "$_gt" -lt "$GM_ROUTE_LOCK_WAIT" ]; do
    if mkdir "$_gl" 2>/dev/null; then
      if printf '%s\n' "$$" > "$_gl/pid" 2>/dev/null; then
        return 0
      fi
      rm -rf "$_gl" 2>/dev/null     # cannot name the owner: do not keep a half lock
      return 1
    fi
    _gt=$((_gt+1))
    _go=''
    [ -f "$_gl/pid" ] && _go=$(cat "$_gl/pid" 2>/dev/null)
    if [ -z "$_go" ] && [ "$_gt" -eq 1 ]; then
      # The owner may be between `mkdir` and writing its pid: look again before breaking it.
      sleep 1
    elif [ -n "$_go" ] && kill -0 "$_go" 2>/dev/null; then
      sleep 1
    else
      rm -rf "$_gl" 2>/dev/null     # ownerless or dead owner: a killed sync
    fi
  done
  return 1
}

_gm_route_unlock() { # never release a lock another process owns
  _gu="$GM_ROUTE_STATE_DIR/lock-$1"
  _gp=$(cat "$_gu/pid" 2>/dev/null)
  [ -z "$_gp" ] || [ "$_gp" = "$$" ] || return 0
  rm -rf "$_gu" 2>/dev/null
  return 0
}

geomask_routes_sync() { # geomask_routes_sync <table> -> 0 only when the table is consistent
  _rt=$1
  case "$_rt" in ''|*[!0-9]*) return 1 ;; esac
  mkdir -p "$GM_ROUTE_STATE_DIR" 2>/dev/null || return 1
  _gm_route_lock "$_rt" || return 1
  _owned=$(_gm_route_owned "$_rt")
  _main="$GM_ROUTE_STATE_DIR/main-$_rt.$$"
  _want="$GM_ROUTE_STATE_DIR/want-$_rt.$$"
  _new="$GM_ROUTE_STATE_DIR/new-$_rt.$$"
  _cur="$GM_ROUTE_STATE_DIR/cur-$_rt.$$"
  _occ="$GM_ROUTE_STATE_DIR/occ-$_rt.$$"

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
    _gm_route_cleanup "$_main" "$_want" "$_new" "$_cur" "$_occ"
    _gm_route_unlock "$_rt"
    return 1
  fi
  if [ ! -s "$_want" ]; then
    _gm_route_cleanup "$_main" "$_want" "$_new" "$_cur" "$_occ"
    _gm_route_unlock "$_rt"
    return 1
  fi

  # What is installed now. Ownership is decided against this snapshot, and a failed read
  # means nothing is touched at all: a blind `replace` could take a route somebody else owns.
  if ! ip route show table "$_rt" > "$_cur" 2>/dev/null; then
    _gm_route_cleanup "$_main" "$_want" "$_new" "$_cur" "$_occ"
    _gm_route_unlock "$_rt"
    return 1
  fi

  _fail=0
  : > "$_new"
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
      _foreign=0
      while IFS= read -r _c; do
        grep -qxF "$_c" "$_owned" 2>/dev/null || _foreign=1
      done < "$_occ"
      if [ "$_foreign" = 1 ]; then
        # A foreign entry on this prefix stays. rc=1: the table is not what `desired` wants.
        _fail=1
        continue
      fi
    fi
    if ip route replace table "$_rt" $_r 2>/dev/null; then
      printf '%s\n' "$_r" >> "$_new"
    else
      _fail=1
    fi
  done < "$_want"

  # Remove only recorded routes that are no longer wanted, and only by their full line: an
  # entry of ours may have been replaced by somebody else since, and `ip route del <prefix>`
  # cannot distinguish the two. A record that is no longer installed is not a failure.
  if [ -f "$_owned" ]; then
    while IFS= read -r _o; do
      [ -n "$_o" ] || continue
      grep -qxF "$_o" "$_want" && continue
      grep -qxF "$_o" "$_cur" || continue
      ip route del table "$_rt" $_o 2>/dev/null || _fail=1
    done < "$_owned"
  fi

  # Record what is really installed, even after a partial failure: an entry left in the table
  # without an owner could never be cleaned up later.
  cp -f "$_new" "$_owned" 2>/dev/null || _fail=1
  _gm_route_cleanup "$_main" "$_want" "$_new" "$_cur" "$_occ"
  _gm_route_unlock "$_rt"
  return "$_fail"
}
