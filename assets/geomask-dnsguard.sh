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
#   geomask-dnsguard.sh status       -> JSON: is the policy applied, is DoH really answering
#   geomask-dnsguard.sh apply        -> put the DoH worker first and the reserve after it
#   geomask-dnsguard.sh revert       -> restore the exact list and strictorder that were there
#   geomask-dnsguard.sh revert --force -> drop an unusable record, live config untouched
#
# Safety rules this file follows, because the first version broke all three:
#   * the original type (list / string / absent), the ordered values, strictorder and a byte
#     preimage of /etc/config/dhcp are recorded BEFORE the first live write and never
#     overwritten while the policy is applied; without a usable record nothing is changed;
#   * the new list is built and verified in a private candidate copy first; only then is the
#     same sequence applied to the live file, and the live file itself is read back and
#     verified (never the private delta, which can disagree with what was committed);
#   * every failure says which stage failed. Before the live commit only our private delta is
#     dropped: the live file was not touched and is never "restored" from a stale preimage.
#     After a commit the preimage is only used when the live file is byte-identical to the
#     candidate we wrote; otherwise the file belongs to someone else and is left alone;
#   * a live restore is backed up first; if it cannot be reproduced exactly, that backup is
#     put back and the record is kept;
#   * "DoH is alive" is a real answer from the DoH worker, not the server's own address in
#     nslookup output (that mistake reported SERVFAIL as healthy);
#   * revert restores what was there, including "strictorder was not set at all" and
#     "server was a string / did not exist", and it still understands version 1 records.
#
# Why the list is written entry by entry (15.11.2026): dnsmasq reads `server` with
# config_list_foreach, so it needs server_LENGTH/server_ITEMn. `uci set ...server=...`
# allocates ONE UCI_TYPE_STRING option, and the configured upstreams disappear.
set -u
# Values are read line by line; a `*` upstream must stay `*`, not become a file list.
set -f

EMERGENCY="1.1.1.1 8.8.8.8"
STATE=${GM_DNSGUARD_STATE:-/etc/geomask/dnsguard.state}
CONFIG_DIR=${GM_UCI_CONFIG_DIR:-/etc/config}
LIVE_DHCP="$CONFIG_DIR/dhcp"
LIVE_DELTA=${GM_UCI_DELTA_DIR:-/tmp/.uci.geomask-dnsguard}
DNSMASQ_INIT=${GM_DNSMASQ_INIT:-/etc/init.d/dnsmasq}
PROBE_PY=${GM_PROBE_PY:-python3}
TEST_HOST=${GM_DNSGUARD_HOST:-example.com}
CAND=
NEW_VALUES=
SCOPED_VALUES=

say() { printf '%s\n' "$*"; }
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
fail() { say "{\"ok\":false,\"error\":\"$(json_escape "$1")\"}"; return 1; }

# The candidate and the value snapshots are private; a failed run must not leave a copy.
cleanup_private() {
  [ -n "${CAND:-}" ] && rm -rf "$CAND" 2>/dev/null
  CAND=
  rm -f "$STATE.values.$$" "$STATE.strict.$$" 2>/dev/null
}
trap 'cleanup_private' 0
trap 'cleanup_private; exit 130' 1 2 15

# `-c` names the config directory, `-t` the delta directory. Always pass both: without `-t`
# the shared /tmp/.uci is used and uncommitted candidate changes can leak into a live read.
uci_live() { uci -c "$CONFIG_DIR" -t "$LIVE_DELTA" "$@"; }
uci_cand() { uci -c "$CAND" -t "$CAND/delta" "$@"; }

live_revert() { uci_live -q revert dhcp 2>/dev/null || true; }
drop_live_delta() { rm -rf "$LIVE_DELTA" 2>/dev/null || true; }

# ---------------------------------------------------------------- config file parsing
# Read the committed UCI file (not `uci export`, which overlays the private delta).
# Emit normalized lines for the FIRST dnsmasq section only:
#   server=<value> / strictorder=<value> / has_option_server=1 / server_type=... / strictorder_type=...
# The type comes from `option` vs `list`, as libuci's parser and dnsmasq see it.
parse_config_file() {
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

dhcp_scan_file() {
  [ -f "$1" ] || return 1
  _dhcp_text=$(cat "$1" 2>/dev/null) || return 1
  printf '%s\n' "$_dhcp_text" | parse_config_file
}

dhcp_scan_live() { dhcp_scan_file "$LIVE_DHCP"; }
dhcp_scan_dir() { dhcp_scan_file "$1/dhcp"; }

scan_field() { printf '%s\n' "$1" | sed -n "s/^$2=//p" | head -1; }
scan_values() { printf '%s\n' "$1" | sed -n "s/^$2=//p"; }
scan_has() { printf '%s\n' "$1" | grep -qx "$2"; }
scoped_from_scan() { printf '%s\n' "$1" | sed -n 's/^server=\(\/.*\)$/\1/p'; }

# ---------------------------------------------------------------- DoH helpers
# A string option from the previous release contains several whitespace-separated upstreams
# (dnsmasq ignores strings, but the old writer used one); a list item is one value and may
# itself contain a space, so only strings are tokenized.
value_tokens() {
  case "$(scan_field "$1" server_type)" in
    string) scan_values "$1" server | awk '{ for (i = 1; i <= NF; i++) print $i }' ;;
    *) scan_values "$1" server ;;
  esac
}

doh_values() {
  value_tokens "$1" | awk '/^127\.0\.0\.1#/ || /^localhost#/'
}

local_doh_port() {
  _port=$(doh_values "$1" | awk 'NR == 1 { sub(/^[^#]*#/, ""); print; exit }')
  [ -n "$_port" ] || return 1
  say "$_port"
}

is_emergency() {
  case " $EMERGENCY " in *" $1 "*) return 0 ;; esac
  return 1
}

# Append to the desired list once. The value is one line, so spaces and `*` survive.
add_new() {
  grep -Fqx -e "$1" "$NEW_VALUES" 2>/dev/null && return 0
  printf '%s\n' "$1" >> "$NEW_VALUES" || return 1
}

# ---------------------------------------------------------------- recorded original state
state_get() { sed -n "s/^$1=//p" "$STATE" 2>/dev/null | head -1; }
state_get_all() { sed -n "s/^$1=//p" "$STATE" 2>/dev/null; }
state_servers() { state_get_all server; }

# Values only: enough to revert. A missing/corrupt preimage must not make the policy impossible
# to turn off; the preimage is only needed for a byte-exact restore after a failed commit.
state_values_ok() {
  [ -f "$STATE" ] || return 1
  [ "$(state_get saved)" = 1 ] || return 1
  _count=$(state_get count)
  case "$_count" in ''|*[!0-9]*) return 1 ;; esac
  [ "$_count" = "$(state_servers | wc -l | tr -d ' ')" ] || return 1
  # A `server=` line with an empty value would restore `list server ''`, which is not a usable
  # upstream and not a faithful record of an option that was really there.
  if state_servers | grep -q '^$'; then
    return 1
  fi
  case "$(state_get version)" in
    1)
      # v1 (the previous release) recorded the values and `strictorder=absent|value`, but neither
      # the option type nor a preimage. A complete v1 record is still a record: without it a
      # router that already has the policy applied could never turn it off after this update.
      # count=0 is a valid v1 record (the server option was absent).
      _v1_so=$(state_get_all strictorder)
      [ -n "$_v1_so" ] || return 1
      [ "$(printf '%s\n' "$_v1_so" | wc -l | tr -d ' ')" = 1 ] || return 1
      return 0
      ;;
    2)
      _type=$(state_get type)
      _stype=$(state_get strictorder_type)
      case "$_type" in list|string|absent) ;; *) return 1 ;; esac
      case "$_stype" in list|string|absent) ;; *) return 1 ;; esac
      # Consistency between the recorded type and the count. A list is not a scalar, a string
      # is exactly one value, and an absent option has no values at all.
      case "$_type" in
        list) [ "$_count" -ge 1 ] || return 1 ;;
        string) [ "$_count" -eq 1 ] || return 1 ;;
        absent) [ "$_count" -eq 0 ] || return 1 ;;
      esac
      # A list record must not carry an option-typed value marker; the normal writer never emits
      # it, but a hand-edited/foreign record must not be trusted as a list.
      if [ "$_type" = list ] && [ "$(state_get has_option_server)" = 1 ]; then
        return 1
      fi
      # strictorder is an option: absent means no value lines, present means exactly one.
      _strict_lines=$(state_get_all strictorder | wc -l | tr -d ' ')
      case "$_stype" in
        absent) [ "$_strict_lines" = 0 ] || return 1 ;;
        *) [ "$_strict_lines" = 1 ] || return 1 ;;
      esac
      return 0
      ;;
    *) return 1 ;;
  esac
}

state_preimage_ok() {
  [ "$(state_get version)" = 2 ] || return 1
  _pre=$(state_get preimage)
  [ -n "$_pre" ] && [ -f "$_pre" ] || return 1
  _sha=$(state_get preimage_sha256)
  [ -n "$_sha" ] || return 1
  [ "$(sha256sum "$_pre" 2>/dev/null | awk '{print $1}')" = "$_sha" ] || return 1
  return 0
}

# The type to restore. v1 did not record it: the original type is unrecoverable, so the safe
# reconstruction is a real list with the recorded values (dnsmasq does not read a string option
# at all; a list is what config_list_foreach needs). count=0 means the option did not exist.
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

# Record the exact original from the snapshot `$1`: the option TYPE, ordered values, strictorder
# type/value, and (when possible) a byte preimage with its sha256. `saved=1` is last and the state
# file is renamed into place only after the preimage, so a partial write is not a record.
# A values-valid record is never overwritten, with or without a preimage.
state_save() {
  _save_scan=$1
  if [ -f "$STATE" ] && ! state_values_ok; then
    return 1
  fi
  state_values_ok && return 0
  _state_dir=$(dirname "$STATE")
  mkdir -p "$_state_dir" 2>/dev/null || return 1
  _type=$(scan_field "$_save_scan" server_type)
  _stype=$(scan_field "$_save_scan" strictorder_type)
  case "$_type" in list|string|absent) ;; *) return 1 ;; esac
  case "$_stype" in list|string|absent) ;; *) return 1 ;; esac
  _pre="$STATE.preimage"
  _pre_tmp="$STATE.preimage.tmp.$$"
  _sha=
  if cp -p "$LIVE_DHCP" "$_pre_tmp" 2>/dev/null; then
    chmod 600 "$_pre_tmp" 2>/dev/null
    _sha=$(sha256sum "$_pre_tmp" 2>/dev/null | awk '{print $1}')
  fi
  [ -n "$_sha" ] || { _pre=; _sha=; rm -f "$_pre_tmp"; }
  _tmp="$STATE.tmp.$$"
  {
    printf 'version=2\n'
    printf 'type=%s\n' "$_type"
    scan_values "$_save_scan" server | sed 's/^/server=/'
    printf 'strictorder_type=%s\n' "$_stype"
    scan_values "$_save_scan" strictorder | sed 's/^/strictorder=/'
    if [ -n "$_pre" ]; then
      printf 'preimage=%s\n' "$_pre"
      printf 'preimage_sha256=%s\n' "$_sha"
    fi
    printf 'count=%s\n' "$(scan_values "$_save_scan" server | wc -l | tr -d ' ')"
    printf 'saved=1\n'
  } > "$_tmp" 2>/dev/null || { rm -f "$_tmp" "$_pre_tmp"; return 1; }
  chmod 600 "$_tmp" 2>/dev/null
  if [ -n "$_pre" ]; then
    mv -f "$_pre_tmp" "$_pre" 2>/dev/null || { rm -f "$_tmp" "$_pre_tmp"; return 1; }
    chmod 600 "$_pre" 2>/dev/null
  else
    rm -f "$_pre_tmp"
  fi
  mv -f "$_tmp" "$STATE" 2>/dev/null || { rm -f "$_tmp"; return 1; }
  if state_values_ok; then
    return 0
  fi
  # The new record itself is unusable (e.g. an empty upstream): do not leave it behind to block
  # the next apply or revert with a misleading record.
  rm -f "$STATE" "$STATE.preimage" 2>/dev/null
  return 1
}

# Compare the live FILE with the recorded type/values. Line-based, so spaces and `*` compare
# exactly; no word joining and no `uci export` (which would overlay the private delta).
state_verify() {
  state_values_ok || return 1
  _scan=$(dhcp_scan_live) || return 1
  [ "$(scan_field "$_scan" server_type)" = "$(state_type)" ] || return 1
  [ "$(scan_values "$_scan" server)" = "$(state_servers)" ] || return 1
  [ "$(scan_field "$_scan" strictorder_type)" = "$(state_stype)" ] || return 1
  # v1 wrote the literal marker `strictorder=absent`; there is no value to compare in that case.
  _state_strict=$(state_get_all strictorder)
  if [ "$(state_get version)" = 1 ] && [ "$(state_stype)" = absent ]; then
    _state_strict=
  fi
  [ "$(scan_values "$_scan" strictorder)" = "$_state_strict" ] || return 1
  return 0
}

# Byte-exact restore for a failed commit. Only valid when the preimage itself is intact.
restore_preimage() {
  state_values_ok || return 1
  state_preimage_ok || return 1
  _pre=$(state_get preimage)
  _sha=$(state_get preimage_sha256)
  _restore_tmp="$CONFIG_DIR/.dhcp.restore.$$"
  cp -p "$_pre" "$_restore_tmp" 2>/dev/null || { rm -f "$_restore_tmp"; return 1; }
  [ "$(sha256sum "$_restore_tmp" 2>/dev/null | awk '{print $1}')" = "$_sha" ] || {
    rm -f "$_restore_tmp"
    return 1
  }
  mv -f "$_restore_tmp" "$LIVE_DHCP" 2>/dev/null || {
    rm -f "$_restore_tmp"
    return 1
  }
  [ "$(sha256sum "$LIVE_DHCP" 2>/dev/null | awk '{print $1}')" = "$_sha" ] || return 1
  return 0
}

# Private backup of the live file before a live restore, and an atomic put-back. If the restore
# cannot reproduce the record exactly, the applied policy is still there instead of a half-write.
live_backup() {
  _backup="$STATE.rollback"
  cp -p "$LIVE_DHCP" "$_backup" 2>/dev/null || return 1
  return 0
}

live_backup_restore() {
  [ -f "$_backup" ] || return 1
  _rb_tmp="$CONFIG_DIR/.dhcp.rollback.$$"
  cp -p "$_backup" "$_rb_tmp" 2>/dev/null || { rm -f "$_rb_tmp"; return 1; }
  mv -f "$_rb_tmp" "$LIVE_DHCP" 2>/dev/null || { rm -f "$_rb_tmp"; return 1; }
  rm -f "$_backup"
  return 0
}

# Returns 0 when the record was reproduced; on failure `_restore_backup_ok` says whether the
# pre-restore live file was put back.
safe_state_restore() {
  _restore_backup_ok=0
  live_backup || return 1
  if state_restore; then
    rm -f "$_backup"
    return 0
  fi
  if live_backup_restore; then
    _restore_backup_ok=1
  fi
  return 1
}

# Restore the recorded TYPE, then commit and verify:
#   list   -> delete + one add_list per value
#   string -> set server=<value>
#   absent -> delete server
# strictorder is handled the same way. Values are read line by line.
state_restore() {
  state_values_ok || return 1
  _type=$(state_type)
  _current=$(dhcp_scan_live) || return 1
  _cur_type=$(scan_field "$_current" server_type)
  case "$_type" in
    list)
      if [ "$_cur_type" != absent ]; then
        uci_live -q delete dhcp.@dnsmasq[0].server 2>/dev/null || return 1
      fi
      state_servers > "$STATE.values.$$" 2>/dev/null || return 1
      while IFS= read -r _v; do
        uci_live -q add_list "dhcp.@dnsmasq[0].server=$_v" 2>/dev/null || return 1
      done < "$STATE.values.$$"
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
      state_get_all strictorder > "$STATE.strict.$$" 2>/dev/null || return 1
      while IFS= read -r _v; do
        uci_live -q add_list "dhcp.@dnsmasq[0].strictorder=$_v" 2>/dev/null || return 1
      done < "$STATE.strict.$$"
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
  _vwant=$2
  _vscoped=$3
  [ "$(scan_field "$_vscan" server_type)" = list ] || {
    _verr="server is not a list option"
    return 1
  }
  if scan_has "$_vscan" has_option_server=1; then
    _verr="an option server line is still present"
    return 1
  fi
  [ "$(scan_values "$_vscan" server)" = "$(cat "$_vwant")" ] || {
    _verr="server values are not exactly the requested list"
    return 1
  }
  [ "$(scoped_from_scan "$_vscan")" = "$(cat "$_vscoped")" ] || {
    _verr="scoped zone entries changed"
    return 1
  }
  [ "$(scan_field "$_vscan" strictorder_type)" = string ] || {
    _verr="strictorder is not a string option"
    return 1
  }
  [ "$(scan_values "$_vscan" strictorder)" = 1 ] || {
    _verr="strictorder is not 1"
    return 1
  }
  return 0
}

verify_config() {
  _vscan=$(dhcp_scan_dir "$CAND") || { _verr="cannot read the candidate configuration"; return 1; }
  verify_scan "$_vscan" "$NEW_VALUES" "$SCOPED_VALUES"
}

verify_live() {
  _vscan=$(dhcp_scan_live) || { _verr="cannot read the live configuration"; return 1; }
  verify_scan "$_vscan" "$NEW_VALUES" "$SCOPED_VALUES"
}

# Build the new list in the private copy: delete, one add_list per value, strictorder, commit,
# then read the candidate FILE back and verify it.
candidate_build() {
  _cand_err=
  _stage=
  _cscan=$(dhcp_scan_dir "$CAND") || { _cand_err="cannot read the candidate copy"; return 1; }
  if [ "$(scan_field "$_cscan" server_type)" != absent ]; then
    _stage=delete
    uci_cand -q delete dhcp.@dnsmasq[0].server 2>/dev/null || {
      _cand_err="uci delete failed"
      return 1
    }
  fi
  _stage=add_list
  while IFS= read -r _v; do
    uci_cand -q add_list "dhcp.@dnsmasq[0].server=$_v" 2>/dev/null || {
      _cand_err="uci add_list $_v failed"
      return 1
    }
  done < "$NEW_VALUES"
  _stage=set
  uci_cand -q set dhcp.@dnsmasq[0].strictorder=1 2>/dev/null || {
    _cand_err="uci set strictorder failed"
    return 1
  }
  _stage=commit
  uci_cand -q commit dhcp 2>/dev/null || { _cand_err="uci commit failed"; return 1; }
  _stage=verify
  verify_config || return 1
  _stage=
  return 0
}

# The live sequence is the same. `_live_stage` records how far the live file was touched:
#   none   -> only our private delta exists, the live file is untouched
#   commit -> a commit was attempted
#   verify -> the commit returned 0, verification is in progress
apply_live() {
  _stage=
  _live_stage=none
  _lscan=$(dhcp_scan_live) || { _stage=read; return 1; }
  if [ "$(scan_field "$_lscan" server_type)" != absent ]; then
    _stage=delete
    uci_live -q delete dhcp.@dnsmasq[0].server 2>/dev/null || return 1
  fi
  _stage=add_list
  while IFS= read -r _v; do
    uci_live -q add_list "dhcp.@dnsmasq[0].server=$_v" 2>/dev/null || return 1
  done < "$NEW_VALUES"
  _stage=set
  uci_live -q set dhcp.@dnsmasq[0].strictorder=1 2>/dev/null || return 1
  _stage=commit
  _live_stage=commit
  uci_live -q commit dhcp 2>/dev/null || return 1
  _live_stage=verify
  _stage=verify
  verify_live || return 1
  _live_stage=done
  _stage=
  return 0
}

candidate_matches_live() {
  [ -f "$CAND/dhcp" ] || return 1
  [ -f "$LIVE_DHCP" ] || return 1
  [ "$(sha256sum "$LIVE_DHCP" 2>/dev/null | awk '{print $1}')" = \
    "$(sha256sum "$CAND/dhcp" 2>/dev/null | awk '{print $1}')" ] || return 1
  return 0
}

# A preimage from an earlier apply is stale: restoring those bytes would erase edits made
# between the two applies. Only the snapshot this candidate was built from is byte-restorable.
preimage_matches_base() {
  state_preimage_ok || return 1
  [ -f "$CAND/base" ] || return 1
  [ "$(sha256sum "$(state_get preimage)" 2>/dev/null | awk '{print $1}')" = \
    "$(sha256sum "$CAND/base" 2>/dev/null | awk '{print $1}')" ] || return 1
  return 0
}

live_write_failed() {
  _why=$1
  live_revert
  case "$_live_stage" in
    none)
      fail "$_why at the ${_stage:-unknown} stage - live config untouched"
      ;;
    commit|verify)
      if candidate_matches_live; then
        if preimage_matches_base && restore_preimage; then
          restart_dnsmasq >/dev/null 2>&1 || true
          fail "$_why at the ${_stage:-unknown} stage - the preimage was restored"
        elif safe_state_restore; then
          restart_dnsmasq >/dev/null 2>&1 || true
          fail "$_why at the ${_stage:-unknown} stage - the recorded values were restored"
        elif [ "${_restore_backup_ok:-0}" = 1 ]; then
          restart_dnsmasq >/dev/null 2>&1 || true
          fail "$_why at the ${_stage:-unknown} stage - live restored to the pre-revert state, record kept"
        else
          fail "$_why at the ${_stage:-unknown} stage - could not restore, check $LIVE_DHCP"
        fi
      else
        fail "$_why at the ${_stage:-unknown} stage - the live file is not the one we wrote; record kept"
      fi
      ;;
    *)
      fail "$_why at the ${_stage:-unknown} stage - live config untouched"
      ;;
  esac
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
  if safe_state_restore; then
    restart_dnsmasq >/dev/null 2>&1 || true
    fail "$1 - previous DNS settings restored"
  elif [ "${_restore_backup_ok:-0}" = 1 ]; then
    restart_dnsmasq >/dev/null 2>&1 || true
    fail "$1 - live restored to the pre-revert state, record kept"
  else
    fail "$1 - could not restore the previous DNS settings, record kept"
  fi
}

# ---------------------------------------------------------------- status
status() {
  _scan=$(dhcp_scan_live) || {
    fail "cannot read $LIVE_DHCP"
    return 1
  }
  _list=$(value_tokens "$_scan" | awk 'BEGIN { ORS = " " } { print }' | sed 's/ $//')
  _so=$(scan_field "$_scan" strictorder)
  _reserve=0
  for _e in $EMERGENCY; do
    if value_tokens "$_scan" | grep -Fqx -e "$_e"; then _reserve=1; fi
  done
  _resolved=0
  resolve_local >/dev/null 2>&1 && _resolved=1
  _doh=unknown
  _port=$(local_doh_port "$_scan" 2>/dev/null) && {
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
    "$(json_escape "$_list")"
}

# ---------------------------------------------------------------- apply / revert
apply() {
  mkdir -p "$LIVE_DELTA" 2>/dev/null || true
  live_revert
  CAND=$(mktemp -d "${TMPDIR:-/tmp}/geomask-dnsguard.XXXXXX") || {
    fail "cannot create a private candidate directory"
    return 1
  }
  mkdir -p "$CAND/delta" 2>/dev/null || {
    fail "cannot create the candidate delta directory"
    return 1
  }
  cp -p "$LIVE_DHCP" "$CAND/dhcp" 2>/dev/null || {
    fail "cannot read $LIVE_DHCP - nothing changed"
    return 1
  }
  cp -p "$CAND/dhcp" "$CAND/base" 2>/dev/null || {
    fail "cannot snapshot $LIVE_DHCP - nothing changed"
    return 1
  }
  _scan=$(dhcp_scan_dir "$CAND") || {
    fail "cannot parse $LIVE_DHCP - nothing changed"
    return 1
  }
  _doh_port=$(local_doh_port "$_scan" 2>/dev/null) || {
    fail "no local DoH upstream in dnsmasq - strict mode would have nothing to trust"
    return 1
  }

  NEW_VALUES="$CAND/new.values"
  SCOPED_VALUES="$CAND/scoped.values"
  : > "$NEW_VALUES" || { fail "cannot create the value snapshot"; return 1; }
  value_tokens "$_scan" | grep '^/' > "$SCOPED_VALUES" 2>/dev/null || true

  # Order: DoH worker, then the emergency reserve, then anything else the owner had, without
  # duplicates; the domain-scoped zones stay last and must survive exactly.
  doh_values "$_scan" > "$CAND/doh.values"
  while IFS= read -r _s; do add_new "$_s"; done < "$CAND/doh.values"
  for _e in $EMERGENCY; do add_new "$_e"; done
  value_tokens "$_scan" > "$CAND/all.values"
  while IFS= read -r _s; do
    case "$_s" in
      127.0.0.1#*|localhost#*) continue ;;
      /*) continue ;;
    esac
    is_emergency "$_s" && continue
    add_new "$_s"
  done < "$CAND/all.values"
  while IFS= read -r _s; do add_new "$_s"; done < "$SCOPED_VALUES"

  if ! candidate_build; then
    fail "candidate configuration rejected at the ${_stage:-unknown} stage - live config untouched${_cand_err:+: $_cand_err}"
    return 1
  fi
  # Record only after the candidate is known good, so a candidate failure leaves no record.
  state_save "$_scan" || {
    fail "cannot record the current DNS settings at $STATE - nothing changed; if the file is unusable run: geomask-dnsguard.sh revert --force"
    return 1
  }
  if ! apply_live; then
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

  drop_live_delta
  doh_probe "$_doh_port"
  case "$?" in
    0) say '{"ok":true,"mode":"strict"}' ;;
    1) say '{"ok":true,"mode":"emergency","warn":"DoH worker does not answer - resolution uses the emergency reserve"}' ;;
    *) say '{"ok":true,"mode":"applied","warn":"policy applied, but the DoH worker could not be probed"}' ;;
  esac
}

revert() {
  _force=${1:-}
  mkdir -p "$LIVE_DELTA" 2>/dev/null || true
  live_revert
  if [ -f "$STATE" ] && ! state_values_ok; then
    if [ "$_force" = --force ]; then
      rm -f "$STATE" "$STATE.preimage" "$STATE.rollback" 2>/dev/null
      say '{"ok":true,"mode":"off","note":"unusable record dropped, live config untouched"}'
      return 0
    fi
    fail "the DNS record at $STATE is incomplete - nothing changed; run: geomask-dnsguard.sh revert --force to drop it"
    return 1
  fi
  if ! state_values_ok; then
    say '{"ok":true,"mode":"off","note":"no recorded policy - nothing to revert"}'
    return 0
  fi
  _had_preimage=0
  state_preimage_ok && _had_preimage=1
  _was_v1=0
  [ "$(state_get version)" = 1 ] && _was_v1=1
  if ! safe_state_restore; then
    if [ "${_restore_backup_ok:-0}" = 1 ]; then
      fail "cannot restore the previous DNS settings - live restored to the pre-revert state, record kept"
    else
      fail "cannot restore the previous DNS settings - record kept"
    fi
    return 1
  fi
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
  rm -f "$STATE" "$STATE.preimage" "$STATE.rollback" 2>/dev/null
  drop_live_delta
  if [ "$_was_v1" = 1 ]; then
    say '{"ok":true,"mode":"off","note":"version 1 record upgraded and restored from its values"}'
  elif [ "$_had_preimage" = 0 ]; then
    say '{"ok":true,"mode":"off","note":"preimage missing - restored from the recorded values"}'
  else
    say '{"ok":true,"mode":"off"}'
  fi
}

case "${1:-status}" in
  status) status ;;
  apply) apply ;;
  revert) revert "${2:-}" ;;
  *) say '{"ok":false,"error":"usage: geomask-dnsguard.sh status|apply|revert [--force]"}'; exit 2 ;;
esac
