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
#   * the original type (list / string / absent), the ordered values, strictorder and a byte
#     preimage of /etc/config/dhcp are recorded BEFORE the first write and never overwritten
#     while the policy is applied; without that record nothing is changed;
#   * every failure (candidate build, add_list, commit, live verification, restart, a resolver
#     that stops answering) puts the recorded values/types back and says so - the router is
#     never left with a half-applied policy;
#   * "DoH is alive" is a real answer from the DoH worker, not the server's own address in
#     nslookup output (that mistake reported SERVFAIL as healthy);
#   * revert restores what was there, including "strictorder was not set at all" and
#     "server was a string / did not exist".
#
# Why the list is written entry by entry (15.11.2026): dnsmasq reads `server` with
# config_list_foreach, so it needs server_LENGTH/server_ITEMn. `uci set ...server=...`
# allocates ONE UCI_TYPE_STRING option, and the configured upstreams disappear. The list is
# therefore built in a private candidate copy with `uci delete` + one `uci add_list` per value,
# verified there, and only then applied to the live config the same way.
set -u

EMERGENCY="1.1.1.1 8.8.8.8"
STATE=${GM_DNSGUARD_STATE:-/etc/geomask/dnsguard.state}
CONFIG_DIR=${GM_UCI_CONFIG_DIR:-/etc/config}
LIVE_DELTA=${GM_UCI_DELTA_DIR:-/tmp/.uci.geomask-dnsguard}
DNSMASQ_INIT=${GM_DNSMASQ_INIT:-/etc/init.d/dnsmasq}
PROBE_PY=${GM_PROBE_PY:-python3}
TEST_HOST=${GM_DNSGUARD_HOST:-example.com}
CAND=

say() { printf '%s\n' "$*"; }
fail() { say "{\"ok\":false,\"error\":\"$1\"}"; return 1; }

# The candidate is always private; a failed run must not leave a copy behind.
cleanup_candidate() { [ -n "${CAND:-}" ] && rm -rf "$CAND" 2>/dev/null; CAND=; }
trap 'cleanup_candidate' 0
trap 'cleanup_candidate; exit 130' 1 2 15

# `-c` names the config directory, `-t` the delta directory. Always pass both: without `-t`
# the shared /tmp/.uci is used and uncommitted candidate changes can leak into the live read.
uci_live() { uci -c "$CONFIG_DIR" -t "$LIVE_DELTA" "$@"; }
uci_cand() { uci -c "$CAND" -t "$CAND/delta" "$@"; }

# ---------------------------------------------------------------- dnsmasq configuration
dnsmasq_list() { uci_live -q get dhcp.@dnsmasq[0].server 2>/dev/null; }
dnsmasq_strict() { uci_live -q get dhcp.@dnsmasq[0].strictorder 2>/dev/null; }

live_revert() { uci_live -q revert dhcp 2>/dev/null || true; }

is_emergency() {
  case " $EMERGENCY " in *" $1 "*) return 0 ;; esac
  return 1
}

# Append to the desired list once; the order is the contract, duplicates are not.
add_new() {
  case " $_new " in *" $1 "*) return 0 ;; esac
  _new="$_new $1"
}

# A local DoH worker appears in the list as 127.0.0.1#PORT (or localhost#PORT).
doh_values() {
  for _s in $(dnsmasq_list); do
    case "$_s" in
      127.0.0.1#*|localhost#*) printf '%s\n' "$_s" ;;
    esac
  done
}

local_doh_port() {
  for _s in $(doh_values); do
    case "$_s" in
      127.0.0.1#*|localhost#*) say "${_s#*#}"; return 0 ;;
    esac
  done
  return 1
}

# ---------------------------------------------------------------- uci export parsing
# Emit normalized lines for the FIRST dnsmasq section only:
#   server=<value> / strictorder=<value> / has_option_server=1 / server_type=... / strictorder_type=...
# The type comes from `uci export` (option vs list), not from `uci show`.
parse_export() {
  awk '
    function decode(s,   t, q) {
      q = sprintf("%c", 39)
      t = s
      sub(/^[ \t]*/, "", t)
      if (substr(t, 1, 1) == q) {
        t = substr(t, 2)
        if (substr(t, length(t), 1) == q) t = substr(t, 1, length(t) - 1)
        gsub(q "\\\\" q q, q, t)
      } else {
        sub(/[ \t]*$/, "", t)
      }
      return t
    }
    /^config[ \t]+dnsmasq([ \t]|$)/ { if (seen) exit; seen = 1; next }
    seen && /^config[ \t]/ { exit }
    seen {
      line = $0
      sub(/^[ \t]+/, "", line)
      if (line ~ /^list[ \t]+server[ \t]/) {
        v = line; sub(/^list[ \t]+server[ \t]+/, "", v)
        print "server=" decode(v)
        stype = "list"
        next
      }
      if (line ~ /^option[ \t]+server[ \t]/) {
        v = line; sub(/^option[ \t]+server[ \t]+/, "", v)
        print "server=" decode(v)
        print "has_option_server=1"
        if (stype == "") stype = "string"
        next
      }
      if (line ~ /^list[ \t]+strictorder[ \t]/) {
        v = line; sub(/^list[ \t]+strictorder[ \t]+/, "", v)
        print "strictorder=" decode(v)
        sstype = "list"
        next
      }
      if (line ~ /^option[ \t]+strictorder[ \t]/) {
        v = line; sub(/^option[ \t]+strictorder[ \t]+/, "", v)
        print "strictorder=" decode(v)
        if (sstype == "") sstype = "string"
        next
      }
    }
    END {
      print "server_type=" (stype == "" ? "absent" : stype)
      print "strictorder_type=" (sstype == "" ? "absent" : sstype)
    }
  '
}

dhcp_scan_live() {
  _dhcp_export=$(uci_live export dhcp 2>/dev/null) || return 1
  printf '%s\n' "$_dhcp_export" | parse_export
}

dhcp_scan_dir() {
  _dhcp_export=$(uci -c "$1" -t "$1/delta" export dhcp 2>/dev/null) || return 1
  printf '%s\n' "$_dhcp_export" | parse_export
}

scan_field() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -1; }
scan_values() { printf '%s\n' "$1" | sed -n "s/^$2=//p"; }
scan_lines() { printf '%s\n' "$1" | sed -n "/^$2=/p"; }
scan_has() { printf '%s\n' "$1" | grep -qx "$2"; }
scoped_from_scan() { printf '%s\n' "$1" | sed -n 's/^server=\(\/.*\)$/\1/p'; }

# ---------------------------------------------------------------- recorded original state
state_get() { sed -n "s/^$1=//p" "$STATE" 2>/dev/null | head -1; }
state_get_all() { sed -n "s/^$1=//p" "$STATE" 2>/dev/null; }
state_servers() { state_get_all server; }

state_ok() {
  [ -f "$STATE" ] || return 1
  [ "$(state_get saved)" = 1 ] || return 1
  case "$(state_get count)" in ''|*[!0-9]*) return 1 ;; esac
  [ "$(state_get count)" = "$(state_servers | wc -l | tr -d ' ')" ] || return 1
  case "$(state_get version)" in
    1)
      # v1 (the previous release) recorded the values and `strictorder=absent|value`, but neither
      # the option type nor a preimage. A complete v1 record is still a record: without it a
      # router that already has the policy applied could never turn it off after this update.
      _v1_so=$(state_get_all strictorder)
      [ -n "$_v1_so" ] || return 1
      [ "$(printf '%s\n' "$_v1_so" | wc -l | tr -d ' ')" = 1 ] || return 1
      return 0
      ;;
    2)
      case "$(state_get type)" in list|string|absent) ;; *) return 1 ;; esac
      case "$(state_get strictorder_type)" in list|string|absent) ;; *) return 1 ;; esac
      _state_pre=$(state_get preimage)
      [ -n "$_state_pre" ] && [ -f "$_state_pre" ] || return 1
      _state_sha=$(state_get preimage_sha256)
      [ -n "$_state_sha" ] || return 1
      [ "$(sha256sum "$_state_pre" 2>/dev/null | awk '{print $1}')" = "$_state_sha" ] || return 1
      return 0
      ;;
    *) return 1 ;;
  esac
}

# The type to restore. v1 did not record it: its writer always produced a string and its values
# were the owner's original entries, so the best reconstruction is a real list with those
# values (dnsmasq reads a one-item list exactly like the old string). count=0 means the option
# did not exist at all.
state_type() {
  if [ "$(state_get version)" = 1 ]; then
    if [ "$(state_get count)" = 0 ]; then printf 'absent\n'; else printf 'list\n'; fi
  else
    state_get type
  fi
}

state_stype() {
  if [ "$(state_get version)" = 1 ]; then
    if [ "$(state_get strictorder)" = absent ]; then printf 'absent\n'; else printf 'string\n'; fi
  else
    state_get strictorder_type
  fi
}

# Record the exact original: the option TYPE, the ordered values, the strictorder type/value,
# and a byte preimage of the dhcp file with its sha256. `saved=1` is written last and the
# state file is renamed into place only after the preimage exists, so a partial write is not
# a record and apply() changes nothing.
state_save() {
  # A v1 record with a broken count is not a record we can safely overwrite: its values may be
  # the only copy of the owner's original entries. Refuse instead of recording the applied state.
  if [ -f "$STATE" ] && [ "$(state_get version)" = 1 ] && ! state_ok; then
    return 1
  fi
  state_ok && return 0
  _state_dir=$(dirname "$STATE")
  mkdir -p "$_state_dir" 2>/dev/null || return 1
  _scan=$(dhcp_scan_live) || return 1
  _type=$(scan_field "$_scan" server_type)
  _stype=$(scan_field "$_scan" strictorder_type)
  case "$_type" in list|string|absent) ;; *) return 1 ;; esac
  case "$_stype" in list|string|absent) ;; *) return 1 ;; esac
  _pre="$STATE.preimage"
  _pre_tmp="$STATE.preimage.tmp.$$"
  cp -p "$CONFIG_DIR/dhcp" "$_pre_tmp" 2>/dev/null || { rm -f "$_pre_tmp"; return 1; }
  chmod 600 "$_pre_tmp" 2>/dev/null
  _sha=$(sha256sum "$_pre_tmp" 2>/dev/null | awk '{print $1}')
  [ -n "$_sha" ] || { rm -f "$_pre_tmp"; return 1; }
  _tmp="$STATE.tmp.$$"
  {
    printf 'version=2\n'
    printf 'type=%s\n' "$_type"
    scan_lines "$_scan" server
    printf 'strictorder_type=%s\n' "$_stype"
    scan_lines "$_scan" strictorder
    printf 'preimage=%s\n' "$_pre"
    printf 'preimage_sha256=%s\n' "$_sha"
    printf 'count=%s\n' "$(scan_values "$_scan" server | wc -l | tr -d ' ')"
    printf 'saved=1\n'
  } > "$_tmp" 2>/dev/null || { rm -f "$_tmp" "$_pre_tmp"; return 1; }
  chmod 600 "$_tmp" 2>/dev/null
  mv -f "$_pre_tmp" "$_pre" 2>/dev/null || { rm -f "$_tmp" "$_pre_tmp"; return 1; }
  chmod 600 "$_pre" 2>/dev/null
  mv -f "$_tmp" "$STATE" 2>/dev/null || { rm -f "$_tmp" "$_pre"; return 1; }
  state_ok
}

# Compare the live config with the recorded type/values (used after every restore).
state_verify() {
  state_ok || return 1
  _scan=$(dhcp_scan_live) || return 1
  [ "$(scan_field "$_scan" server_type)" = "$(state_type)" ] || return 1
  [ "$(scan_values "$_scan" server | tr '\n' ' ' | sed 's/ $//')" = \
    "$(state_servers | tr '\n' ' ' | sed 's/ $//')" ] || return 1
  [ "$(scan_field "$_scan" strictorder_type)" = "$(state_stype)" ] || return 1
  # v1 wrote the literal marker `strictorder=absent`; there is no value to compare in that case.
  _state_strict=$(state_get_all strictorder | tr '\n' ' ' | sed 's/ $//')
  if [ "$(state_get version)" = 1 ] && [ "$(state_stype)" = absent ]; then
    _state_strict=
  fi
  [ "$(scan_values "$_scan" strictorder | tr '\n' ' ' | sed 's/ $//')" = "$_state_strict" ] \
    || return 1
  return 0
}

# Byte-exact restore for failures in the middle of the live write/commit. The temp file sits in
# the config directory so the rename is atomic; a torn copy must never become the live config.
restore_preimage() {
  state_ok || return 1
  _pre=$(state_get preimage)
  [ -f "$_pre" ] || return 1
  _sha=$(state_get preimage_sha256)
  [ "$(sha256sum "$_pre" 2>/dev/null | awk '{print $1}')" = "$_sha" ] || return 1
  _restore_tmp="$CONFIG_DIR/.dhcp.restore.$$"
  cp -p "$_pre" "$_restore_tmp" 2>/dev/null || { rm -f "$_restore_tmp"; return 1; }
  chmod 600 "$_restore_tmp" 2>/dev/null
  [ "$(sha256sum "$_restore_tmp" 2>/dev/null | awk '{print $1}')" = "$_sha" ] || {
    rm -f "$_restore_tmp"
    return 1
  }
  mv -f "$_restore_tmp" "$CONFIG_DIR/dhcp" 2>/dev/null || {
    rm -f "$_restore_tmp"
    return 1
  }
  [ "$(sha256sum "$CONFIG_DIR/dhcp" 2>/dev/null | awk '{print $1}')" = "$_sha" ] || return 1
  return 0
}

# Restore the recorded TYPE, then commit and verify:
#   list   -> delete + one add_list per value
#   string -> set server=<value>
#   absent -> delete server
# strictorder is handled the same way.
state_restore() {
  state_ok || return 1
  _type=$(state_type)
  _current=$(dhcp_scan_live) || return 1
  _cur_type=$(scan_field "$_current" server_type)
  case "$_type" in
    list)
      if [ "$_cur_type" != absent ]; then
        uci_live -q delete dhcp.@dnsmasq[0].server 2>/dev/null || return 1
      fi
      for _v in $(state_servers); do
        uci_live -q add_list "dhcp.@dnsmasq[0].server=$_v" 2>/dev/null || return 1
      done
      ;;
    string)
      _v=$(state_get_all server | head -1)
      uci_live -q set "dhcp.@dnsmasq[0].server=$_v" 2>/dev/null || return 1
      ;;
    absent)
      if [ "$_cur_type" != absent ]; then
        uci_live -q delete dhcp.@dnsmasq[0].server 2>/dev/null || return 1
      fi
      ;;
    *) return 1 ;;
  esac
  _stype=$(state_stype)
  _cur_stype=$(scan_field "$_current" strictorder_type)
  case "$_stype" in
    list)
      if [ "$_cur_stype" != absent ]; then
        uci_live -q delete dhcp.@dnsmasq[0].strictorder 2>/dev/null || return 1
      fi
      for _v in $(state_get_all strictorder); do
        uci_live -q add_list "dhcp.@dnsmasq[0].strictorder=$_v" 2>/dev/null || return 1
      done
      ;;
    string)
      _v=$(state_get_all strictorder | head -1)
      uci_live -q set "dhcp.@dnsmasq[0].strictorder=$_v" 2>/dev/null || return 1
      ;;
    absent)
      if [ "$_cur_stype" != absent ]; then
        uci_live -q delete dhcp.@dnsmasq[0].strictorder 2>/dev/null || return 1
      fi
      ;;
    *) return 1 ;;
  esac
  uci_live -q commit dhcp 2>/dev/null || return 1
  state_verify
}

# ---------------------------------------------------------------- candidate + verification
verify_scan() {
  _vscan=$1
  shift
  [ "$(scan_field "$_vscan" server_type)" = list ] || {
    _verr="server is not a list option"
    return 1
  }
  if scan_has "$_vscan" has_option_server=1; then
    _verr="an option server line is still present"
    return 1
  fi
  [ "$(scan_values "$_vscan" server | tr '\n' ' ' | sed 's/ $//')" = "$*" ] || {
    _verr="server values are not exactly the requested list"
    return 1
  }
  [ "$(scoped_from_scan "$_vscan")" = "${_expected_scoped:-}" ] || {
    _verr="scoped zone entries changed"
    return 1
  }
  [ "$(scan_field "$_vscan" strictorder_type)" = string ] || {
    _verr="strictorder is not a string option"
    return 1
  }
  [ "$(scan_values "$_vscan" strictorder | tr '\n' ' ' | sed 's/ $//')" = 1 ] || {
    _verr="strictorder is not 1"
    return 1
  }
  return 0
}

verify_config() {
  _vdir=$1
  shift
  _vscan=$(dhcp_scan_dir "$_vdir") || { _verr="cannot read the candidate configuration"; return 1; }
  verify_scan "$_vscan" "$@"
}

verify_live() {
  _vscan=$(dhcp_scan_live) || { _verr="cannot read the live configuration"; return 1; }
  verify_scan "$_vscan" "$@"
}

# Build and verify the new list in a private copy before the live file is touched.
candidate_build() {
  _cand_err=
  _stage=
  CAND=$(mktemp -d "${TMPDIR:-/tmp}/geomask-dnsguard.XXXXXX") || {
    _cand_err="cannot create a private candidate directory"
    return 1
  }
  mkdir -p "$CAND/delta" 2>/dev/null || {
    _cand_err="cannot create the candidate delta directory"
    return 1
  }
  cp -p "$CONFIG_DIR/dhcp" "$CAND/dhcp" 2>/dev/null || {
    _cand_err="cannot copy the live configuration"
    return 1
  }
  _cscan=$(dhcp_scan_dir "$CAND") || { _cand_err="cannot read the candidate copy"; return 1; }
  if [ "$(scan_field "$_cscan" server_type)" != absent ]; then
    _stage=delete
    uci_cand -q delete dhcp.@dnsmasq[0].server 2>/dev/null || {
      _cand_err="uci delete failed"
      return 1
    }
  fi
  _stage=add_list
  for _v in "$@"; do
    uci_cand -q add_list "dhcp.@dnsmasq[0].server=$_v" 2>/dev/null || {
      _cand_err="uci add_list $_v failed"
      return 1
    }
  done
  _stage=set
  uci_cand -q set dhcp.@dnsmasq[0].strictorder=1 2>/dev/null || {
    _cand_err="uci set strictorder failed"
    return 1
  }
  _stage=commit
  uci_cand -q commit dhcp 2>/dev/null || { _cand_err="uci commit failed"; return 1; }
  _stage=verify
  verify_config "$CAND" "$@" || return 1
  _stage=
  return 0
}

# The live sequence is the same: delete, one add_list per value, strictorder, commit, verify.
apply_live() {
  _stage=
  _lscan=$(dhcp_scan_live) || { _stage=read; return 1; }
  if [ "$(scan_field "$_lscan" server_type)" != absent ]; then
    _stage=delete
    uci_live -q delete dhcp.@dnsmasq[0].server 2>/dev/null || return 1
  fi
  _stage=add_list
  for _v in "$@"; do
    uci_live -q add_list "dhcp.@dnsmasq[0].server=$_v" 2>/dev/null || return 1
  done
  _stage=set
  uci_live -q set dhcp.@dnsmasq[0].strictorder=1 2>/dev/null || return 1
  _stage=commit
  uci_live -q commit dhcp 2>/dev/null || return 1
  _stage=verify
  verify_live "$@" || return 1
  _stage=
  return 0
}

live_write_failed() {
  _why=$1
  live_revert
  if restore_preimage && state_verify; then
    restart_dnsmasq >/dev/null 2>&1 || true
    fail "$_why at the ${_stage:-unknown} stage - the preimage was restored"
  elif state_restore; then
    restart_dnsmasq >/dev/null 2>&1 || true
    fail "$_why at the ${_stage:-unknown} stage - the recorded values were restored"
  else
    fail "$_why at the ${_stage:-unknown} stage - could not restore, check $CONFIG_DIR/dhcp"
  fi
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

restart_dnsmasq() {
  [ -x "$DNSMASQ_INIT" ] || return 1
  "$DNSMASQ_INIT" restart >/dev/null 2>&1
}

# Undo a half-applied policy and report it. Used by every failure path in apply().
rollback() {
  live_revert
  if state_restore; then
    restart_dnsmasq >/dev/null 2>&1 || true
    fail "$1 - previous DNS settings restored"
  else
    fail "$1 - could not restore the previous DNS settings, check $CONFIG_DIR/dhcp"
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
  mkdir -p "$LIVE_DELTA" 2>/dev/null || true
  live_revert
  _doh_port=$(local_doh_port 2>/dev/null) || {
    fail "no local DoH upstream in dnsmasq - strict mode would have nothing to trust"
    return 1
  }
  state_save || { fail "cannot record the current DNS settings - nothing changed"; return 1; }

  # Order: DoH worker, then the emergency reserve, then anything else the owner had,
  # without duplicates; the domain-scoped zones stay last (they never take part in the
  # default order) and must survive exactly.
  _new=
  for _s in $(doh_values); do add_new "$_s"; done
  for _e in $EMERGENCY; do add_new "$_e"; done
  for _s in $(dnsmasq_list); do
    case "$_s" in
      127.0.0.1#*|localhost#*) continue ;;
      /*) continue ;;
    esac
    is_emergency "$_s" && continue
    add_new "$_s"
  done
  for _s in $(dnsmasq_list); do
    case "$_s" in /*) add_new "$_s" ;; esac
  done
  _expected_scoped=$(dnsmasq_list | tr ' ' '\n' | grep '^/' || true)

  set -- $_new
  if ! candidate_build "$@"; then
    fail "candidate configuration rejected at the ${_stage:-unknown} stage - live config untouched${_cand_err:+: $_cand_err}"
    return 1
  fi
  if ! apply_live "$@"; then
    live_write_failed "cannot rewrite the dnsmasq server list"
    return 1
  fi

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
  mkdir -p "$LIVE_DELTA" 2>/dev/null || true
  live_revert
  # A v1 file that does not describe a complete record is not "no record": say so and leave both
  # the live policy and the file untouched, so the values are not lost.
  if [ -f "$STATE" ] && [ "$(state_get version)" = 1 ] && ! state_ok; then
    fail "the version 1 DNS record is incomplete - nothing changed, record kept"
    return 1
  fi
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
  rm -f "$STATE" "$STATE.preimage" 2>/dev/null
  say '{"ok":true,"mode":"off"}'
}

case "${1:-status}" in
  status) status ;;
  apply) apply ;;
  revert) revert ;;
  *) say '{"ok":false,"error":"usage: geomask-dnsguard.sh status|apply|revert"}'; exit 2 ;;
esac
