"""R03 phase 2b: manifest-driven reinstall snapshot/restore + rollback fault matrix.

Scope (plan R03 phase 2): `_reinstall_snapshot` / `_reinstall_restore` must cover the exact
manifest write-set (tests/reinstall_write_set.json): bytes/absence/identity of the owned
files, the managed subset of the shared files (passwd/group dnsai line, zapret markers,
crontab, uci keys), the rc.d links with their real targets and the tri-state
enabled/running of every service in scope, and the per-interface IPv6 sysctl values.

Restore must VERIFY instead of trusting exit codes: any unverified / failed object keeps
the snapshot, returns non-zero and says "rollback incomplete / recovery required".
A concurrent write after the snapshot is a conflict, never a success.

Safety: throwaway fixture root, injected commands only (no live /etc, /tmp, init.d,
network, firewall, sysctl). Fault injection happens through those injected commands.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import win_symlink  # noqa: E402  (test-support helper in the same directory)
TEMPLATE = ROOT / 'geomask.sh.in'
MANIFEST = json.loads((Path(__file__).resolve().parent / 'reinstall_write_set.json')
                      .read_text(encoding='utf-8'))


def find_bash():
    for candidate in (r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe', shutil.which('bash')):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()
SOURCE = TEMPLATE.read_text(encoding='utf-8')

FUNCTIONS = [
    '_reinstall_save', '_reinstall_snapshot', '_reinstall_restore',
]

# The extracted restore functions call the one-line product helpers _cron_begin/_cron_end,
# which the discover-by-name rule (`_rs_*`) does not pick up. The harness emulates the installed
# system: the same semantics, using the same kernel helper. Without them the restore printed
# "command not found" and its `&&`-guarded release masked the failure.
HARNESS_STUBS = '''
_cron_begin() { command -v gm_lock_acquire >/dev/null 2>&1 || return 1; gm_lock_acquire cron 20; }
_cron_end() { command -v gm_lock_release >/dev/null 2>&1 && gm_lock_release cron; return 0; }
'''


def reinstall_functions():
    """Every real snapshot/restore helper the template defines, in file order.

    The list is discovered instead of hard-coded so a new helper cannot silently stay
    outside the fixture: the product body calls it and the shell fails loudly.
    """
    names = re.findall(r'^(_rs_[A-Za-z0-9_]+|_reinstall_(?:save|snapshot|restore))\(\) \{',
                       SOURCE, re.MULTILINE)
    return [name for name in names if extract(name)]

TABLES = ['REINSTALL_PREFLIGHT_OBJECTS', 'REINSTALL_PREFLIGHT_BYTE_FILES',
          'REINSTALL_SHARED_APPEND', 'REINSTALL_UCI_OBJECTS', 'REINSTALL_LIST_DIRS',
          'REINSTALL_TEMP_SUFFIXES', 'REINSTALL_NR_STATE']


def extract(name):
    match = re.search(r'^' + re.escape(name) + r'\(\) \{.*?^\}\s*$', SOURCE,
                      re.MULTILINE | re.DOTALL)
    return match.group(0) if match else None


def template_tables():
    text = ''
    for name in TABLES:
        match = re.search(r'^' + name + r'="[^"]*"', SOURCE, re.MULTILINE | re.DOTALL)
        if match:
            text += match.group(0) + '\n'
    for match in re.finditer(r'^(AIDNS_[A-Z_]+=.*)$', SOURCE, re.MULTILINE):
        text += match.group(1) + '\n'
    return text


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


GIT_ROOT = ''
if BASH:
    GIT_ROOT = str(Path(BASH).resolve().parents[1]).replace('\\', '/')


def posix_link(target):
    """The POSIX target a Git-Bash symlink stands for on this host.

    Git Bash stores native Windows symlinks: relative targets get backslashes and an
    absolute POSIX target is mapped under the Git installation root. On the router both are
    plain POSIX strings; Windows only lets us compare the equivalent form.
    """
    text = target.replace('\\', '/')
    if text.startswith('//?/'):
        text = text[4:]
    if GIT_ROOT and text.startswith(GIT_ROOT + '/'):
        return text[len(GIT_ROOT):]
    if GIT_ROOT and text == GIT_ROOT:
        return '/'
    return text


"""Fixture command adapters (uci/ip/nft/fw4/svc) for test_reinstall_phase2b.

Implemented in shell on purpose: every adapter call in the restore path would otherwise
spawn a Python interpreter (Windows Git Bash ~150 ms each), which made the fault matrix
unusable and also depended on `python3` being on PATH. These are strict stand-ins for the
OpenWrt tools on a throwaway fixture root - they never touch the host.
"""

SUPPORT = r'''set -u
PASS=0; FAILCNT=0; SKIP=0; SAVE_FAIL=0
GM_YES=1; GM_AUTO=1; GM_RESTART=0; GM_CHECK_ONLY=0; GM_ADOPT=0; GM_REPLACE=0
GM_VPN_URL=''; GM_MODULES='@MODULES@'
GM_IPV6='off'; GM_PREFIX='@PREFIX@'
STATE='@STATE@'; LOGS='@LOGS@'; ROOTDIR='@PREFIX@'
GM_RESTORE_VERIFY_TRIES=1
UCI_DELTA="$ROOTDIR/tmp/.uci"
export GM_PREFIX STATE
P() { printf '%s%s' "$GM_PREFIX" "$1"; }
info() { printf 'INFO %s\n' "$*" >> "$LOGS/info.log"; }
ok()   { printf 'OK %s\n' "$*" >> "$LOGS/info.log"; }
warn() { printf 'WARN %s\n' "$*" >> "$LOGS/info.log"; }
bad()  { printf 'BAD %s\n' "$*" >> "$LOGS/info.log"; FAILCNT=$((FAILCNT+1)); }
log()  { printf 'LOG %s\n' "$*" >> "$LOGS/info.log"; }
hdr()  { printf 'HDR %s\n' "$*" >> "$LOGS/info.log"; }
sleep() { :; }
have() {
  case " ${HAVE_MISSING:-} " in
    *" $1 "*) return 1 ;;
  esac
  case "$1" in
    pidof|pgrep|netstat|crontab|uci|ip|nft|sysctl|apk|opkg) return 0 ;;
    sha256sum|sha1sum|md5sum|stat|readlink|cmp|find|awk|sed|grep|cut|tr|head|tail|wc|date|df|cp|mv|rm|mkdir|chmod|ln|cat|sort|uniq|printf|ls|dirname|basename|touch|test) return 0 ;;
  esac
  command -v "$1" >/dev/null 2>&1
}
module_required() { case " $GM_REQUIRED_MODULES " in *" $1 "*) return 0 ;; esac; return 1; }
module_selected() { [ -z "$GM_MODULES" ] && return 0; case " $GM_MODULES " in *" $1 "*) return 0 ;; esac; return 1; }

# ------------------------------------------------------------------ uci stand-in
_u_file() {
  if [ -f "$UCI_DELTA/$1" ]; then printf '%s' "$UCI_DELTA/$1"; else printf '%s' "$ROOTDIR/etc/config/$1"; fi
}
_u_unq='function unq(x){ gsub(/'\''/, "", x); return x }'
_u_type() {
  _f=$(_u_file "$1"); [ -f "$_f" ] || return 1
  awk -v S="$2" "$_u_unq"'
    /^[ \t]*config[ \t]/ { if ($3 != "" && unq($3) == S) { print $2; found = 1; exit } }
    END { exit found ? 0 : 1 }' "$_f"
}
_u_get() {
  _f=$(_u_file "$1"); [ -f "$_f" ] || return 1
  if [ -z "${3:-}" ]; then _u_type "$1" "$2"; return $?; fi
  awk -v S="$2" -v O="$3" "$_u_unq"'
    /^[ \t]*config[ \t]/ { sec = ""; if ($3 != "") sec = unq($3); next }
    sec == S && $1 == "option" && unq($2) == O {
      v = $0; sub(/^[ \t]*option[ \t]+[^ \t]+[ \t]+/, "", v); gsub(/'\''/, "", v); print v; found = 1; exit }
    END { exit found ? 0 : 1 }' "$_f"
}
_u_rewrite() { mv -f "$1.new" "$1"; }
_u_mut() { # _u_mut <pkg> -> delta file, seeded from the committed config (libuci semantics)
  [ -f "$UCI_DELTA/$1" ] && { printf '%s' "$UCI_DELTA/$1"; return 0; }
  mkdir -p "$UCI_DELTA" || return 1
  if [ -f "$ROOTDIR/etc/config/$1" ]; then
    cp "$ROOTDIR/etc/config/$1" "$UCI_DELTA/$1" || return 1
  else
    : > "$UCI_DELTA/$1" || return 1
  fi
  printf '%s' "$UCI_DELTA/$1"
}
_u_set() {
  _kv=$1; _k=${_kv%%=*}; _v=${_kv#*=}
  _pkg=${_k%%.*}; _rest=${_k#*.}
  [ "$_rest" != "$_k" ] || return 2
  _sec=${_rest%%.*}
  if [ "$_sec" = "$_rest" ]; then _opt=""; else _opt=${_rest#*.}; fi
  _f=$(_u_mut "$_pkg") || return 2
  if [ ! -f "$_f" ]; then return 2; fi
  if [ -z "$_opt" ]; then
    awk -v S="$_sec" -v T="$_v" "$_u_unq"'
      /^[ \t]*config[ \t]/ { if ($3 != "" && unq($3) == S) { print "config " T " '\''" S "'\''"; done = 1; next } }
      { print }
      END { if (!done) print "config " T " '\''" S "'\''" }' "$_f" > "$_f.new" || return 2
    _u_rewrite "$_f"
    return 0
  fi
  if ! _u_type "$_pkg" "$_sec" >/dev/null 2>&1; then return 2; fi
  if _u_get "$_pkg" "$_sec" "$_opt" >/dev/null 2>&1; then
    awk -v S="$_sec" -v O="$_opt" -v V="$_v" "$_u_unq"'
      /^[ \t]*config[ \t]/ { if ($3 != "") sec = unq($3); else sec = ""; }
      sec == S && $1 == "option" && unq($2) == O { print "\toption " O " '\''" V "'\''"; next }
      { print }' "$_f" > "$_f.new" || return 2
  else
    awk -v S="$_sec" -v O="$_opt" -v V="$_v" "$_u_unq"'
      /^[ \t]*config[ \t]/ {
        if ($3 != "") sec = unq($3); else sec = ""
        print
        if (sec == S && !done) { print "\toption " O " '\''" V "'\''"; done = 1 }
        next }
      { print }
      END { if (!done) print "\toption " O " '\''" V "'\''" }' "$_f" > "$_f.new" || return 2
  fi
  _u_rewrite "$_f"
  return 0
}
_u_del() {
  _k=$1; _pkg=${_k%%.*}; _rest=${_k#*.}
  [ "$_rest" != "$_k" ] || return 2
  _sec=${_rest%%.*}
  if [ ! -f "$UCI_DELTA/$_pkg" ] && [ ! -f "$ROOTDIR/etc/config/$_pkg" ]; then return 0; fi
  _f=$(_u_mut "$_pkg") || return 2
  if [ "$_sec" = "$_rest" ]; then
    awk -v S="$_sec" "$_u_unq"'
      /^[ \t]*config[ \t]/ { drop = ($3 != "" && unq($3) == S) }
      !drop { print }' "$_f" > "$_f.new" || return 2
  else
    _opt=${_rest#*.}
    awk -v S="$_sec" -v O="$_opt" "$_u_unq"'
      /^[ \t]*config[ \t]/ { if ($3 != "") sec = unq($3); else sec = "" }
      sec == S && $1 == "option" && unq($2) == O { next }
      { print }' "$_f" > "$_f.new" || return 2
  fi
  _u_rewrite "$_f"
  return 0
}
uci() {
  _q=0
  while [ "${1:-}" = "-q" ]; do _q=1; shift; done
  _verb=${1:-}; shift || true
  case "$_verb" in
    get)
      [ $# -ge 1 ] || return 1
      if [ "${1#*.}" = "$1" ]; then return 1; fi
      if [ "$(printf '%s' "$1" | awk -F. '{print NF}')" -ge 3 ]; then
        _pkg=${1%%.*}; _rest=${1#*.}; _sec=${_rest%%.*}; _opt=${_rest#*.}
        _u_get "$_pkg" "$_sec" "$_opt"
      else
        _u_type "${1%%.*}" "${1#*.}"
      fi ;;
    set)
      [ $# -ge 1 ] || return 2
      _u_set "$1" ;;
    delete|del)
      [ $# -ge 1 ] || return 0
      _u_del "$1" ;;
    add)
      [ $# -ge 2 ] || return 2
      _pkg=$1; _type=$2
      _f=$(_u_mut "$_pkg") || return 2
      _n=$(awk '/^[ \t]*config[ \t]/{n++} END{print n+1}' "$_f")
      _sec=$(printf 'cfg%06d' "$_n")
      printf "config %s '%s'\n" "$_type" "$_sec" >> "$_f"
      printf '%s\n' "$_sec" ;;
    commit)
      _pkg=${1:-}
      [ "${UCI_COMMIT_FAIL:-0}" = 1 ] && return 1
      if [ ! -f "$UCI_DELTA/$_pkg" ]; then
        [ -f "$ROOTDIR/etc/config/$_pkg" ] && return 0
        return 1
      fi
      [ "${UCI_COMMIT_NOOP:-0}" = 1 ] && return 0
      mkdir -p "$ROOTDIR/etc/config"
      mv -f "$UCI_DELTA/$_pkg" "$ROOTDIR/etc/config/$_pkg" ;;
    show)
      _pkg=${1:-}; _f=$(_u_file "$_pkg"); [ -f "$_f" ] || return 1
      awk -v P="$_pkg" "$_u_unq"'
        /^[ \t]*config[ \t]/ {
          if ($3 != "") sec = unq($3); else sec = "@" $2
          print P "." sec "=" $2; next }
        $1 == "option" {
          v = $0; sub(/^[ \t]*option[ \t]+[^ \t]+[ \t]+/, "", v); gsub(/'\''/, "", v)
          print P "." sec "." unq($2) "='\''" v "'\''" }' "$_f" ;;
    *)
      return 2 ;;
  esac
}
uci_sections_of() { # uci_sections_of <pkg> <type> -> section names
  _f=$(_u_file "$1"); [ -f "$_f" ] || return 1
  awk -v T="$2" "$_u_unq"'
    /^[ \t]*config[ \t]/ { if ($2 == T && $3 != "") print unq($3) }' "$_f"
}

# ------------------------------------------------------------------ ip stand-in
_ip_addr() {
  awk -v WANT="${1:-}" "$_u_unq"'
    /^[ \t]*config[ \t]+interface/ {
      if (sec != "") out()
      sec = ($3 != "") ? unq($3) : ""; dev = ""; ip = ""; mask = ""; next }
    $1 == "option" && $2 == "device" { dev = unq($3) }
    $1 == "option" && $2 == "ipaddr" { ip = unq($3) }
    $1 == "option" && $2 == "netmask" { mask = unq($3) }
    function out() {
      if (ip == "") return
      if (WANT != "" && WANT != dev && WANT != sec) return
      printf "    inet %s/24 brd %s scope global %s\n", ip, ip, (dev != "" ? dev : sec) }
    END { if (sec != "") out() }' "$(_u_file network)"
}
ip() {
  _line="$*"
  case "$_line" in
    "rule show"*)
      [ -f "$STATE/ip_rules" ] && cat "$STATE/ip_rules"; return 0 ;;
    "rule add"*)
      _pref=$(printf '%s' "$_line" | sed -n 's/.*pref \([0-9]*\).*/\1/p')
      _mark=$(printf '%s' "$_line" | sed -n 's/.*fwmark \([^ ]*\).*/\1/p')
      _tbl=$(printf '%s' "$_line" | sed -n 's/.*table \([^ ]*\).*/\1/p')
      _r="$_pref:	from all fwmark $_mark lookup $_tbl"
      grep -qxF "$_r" "$STATE/ip_rules" 2>/dev/null || printf '%s\n' "$_r" >> "$STATE/ip_rules"
      return 0 ;;
    "rule del"*)
      _pref=$(printf '%s' "$_line" | sed -n 's/.*pref \([0-9]*\).*/\1/p')
      if [ -f "$STATE/ip_rules" ]; then
        grep -v "^$_pref:" "$STATE/ip_rules" > "$STATE/ip_rules.new" 2>/dev/null || :
        mv -f "$STATE/ip_rules.new" "$STATE/ip_rules"
      fi
      return 0 ;;
  esac
  case " $_line " in
    *" -4 addr show"*)
      _dev=""
      for _a in $_line; do case "$_a" in -4|addr|show) ;; *) _dev=$_a ;; esac; done
      _ip_addr "$_dev"
      return 0 ;;
  esac
  return 0
}

# ------------------------------------------------------------------ nft stand-in
nft() {
  _line="$*"
  case "$_line" in
    "list table "*)
      _spec=${_line#list table }
      if grep -qxF "$_spec" "$STATE/nft_tables" 2>/dev/null; then
        if [ -f "$STATE/nft_ai_content" ]; then
          if [ "${NFT_COUNTERS_BUMP:-0}" = 1 ]; then
            sed 's/counter packets 5 bytes 300/counter packets 6 bytes 301/g' "$STATE/nft_ai_content"
          else
            cat "$STATE/nft_ai_content"
          fi
        else
          printf 'table %s {\n}\n' "$_spec"
        fi
        return 0
      fi
      return 1 ;;
    "list chain "*)
      _spec=${_line#list chain }
      if grep -qxF "$_spec" "$STATE/nft_chains" 2>/dev/null; then
        if [ -f "$STATE/nft_v6_content" ]; then
          cat "$STATE/nft_v6_content"
        else
          printf 'chain %s {\n}\n' "$_spec"
        fi
        return 0
      fi
      return 1 ;;
    "delete table "*)
      _spec=${_line#delete table }
      grep -vxF "$_spec" "$STATE/nft_tables" > "$STATE/nft_tables.new" 2>/dev/null || :
      mv -f "$STATE/nft_tables.new" "$STATE/nft_tables"
      rm -f "$STATE/nft_ai_content"
      return 0 ;;
    "add table "*)
      _spec=${_line#add table }
      grep -qxF "$_spec" "$STATE/nft_tables" 2>/dev/null || printf '%s\n' "$_spec" >> "$STATE/nft_tables"
      return 0 ;;
  esac
  return 0
}
_firewall_reload() {
  : > "$STATE/nft_chains"
  rm -f "$STATE/nft_v6_content"
  for _d in "$ROOTDIR/etc/nftables.d" "$ROOTDIR/usr/share/nftables.d/ruleset-post"; do
    [ -d "$_d" ] || continue
    for _f in "$_d"/*.nft; do
      [ -f "$_f" ] || continue
      awk '/chain[ \t]+[A-Za-z0-9_]+[ \t]*\{/ { for (i = 1; i <= NF; i++) if ($i == "chain") { c = $(i+1); sub(/\{.*/, "", c); print "inet fw4 " c } }' "$_f" >> "$STATE/nft_chains" 2>/dev/null || :
      sed -n 's/^add table \([^ ]* [^ ]*\)$/\1/p' "$_f" >> "$STATE/nft_tables.new" 2>/dev/null || :
      if grep -q 'add table inet geomask_ai_dns' "$_f" 2>/dev/null; then
        if [ -n "${NFT_RELOAD_CONTENT:-}" ] && [ -f "$NFT_RELOAD_CONTENT" ]; then
          cp "$NFT_RELOAD_CONTENT" "$STATE/nft_ai_content"
        else
          cp "$_f" "$STATE/nft_ai_content"
        fi
      fi
      if grep -q 'chain geomask_no_v6' "$_f" 2>/dev/null; then
        cp "$_f" "$STATE/nft_v6_content"
      fi
    done
  done
  if [ -f "$STATE/nft_tables.new" ]; then
    cat "$STATE/nft_tables.new" "$STATE/nft_tables" 2>/dev/null | awk '!seen[$0]++' > "$STATE/nft_tables.merged"
    mv -f "$STATE/nft_tables.merged" "$STATE/nft_tables"
    rm -f "$STATE/nft_tables.new"
  fi
}
_svc_marker() {
  case "$1" in
    ai-doh) printf 'port5055' ;;
    dnsmasq-ai) printf 'port5353' ;;
    magitrickle) printf 'magitrickled' ;;
    hev-nop2p) printf 'hev-socks5-tunnel' ;;
    network) printf 'netifd' ;;
    firewall) printf 'fw4' ;;
    *) printf '%s' "$1" ;;
  esac
}
sysctl() {
  case "${1:-}" in
    -w)
      [ "${SYSCTL_WRITE_FAIL:-0}" = 1 ] && return 1
      _k=${2%%=*}; _v=${2#*=}
      printf '%s\n' "$_v" > "$(P "/proc/sys/$(printf '%s' "$_k" | tr '.' '/')")" || return 1
      return 0 ;;
    -p)
      [ -f "$2" ] || return 1
      while IFS='=' read -r _k _v; do
        _k=$(printf '%s' "$_k" | tr -d ' \t')
        _v=$(printf '%s' "$_v" | tr -d ' \t')
        [ -n "$_k" ] || continue
        printf '%s\n' "$_v" > "$(P "/proc/sys/$(printf '%s' "$_k" | tr '.' '/')")" 2>/dev/null || true
      done < "$2"
      return 0 ;;
  esac
  return 0
}
pidof() { [ -f "$STATE/run/$1" ] && { printf '4242\n'; return 0; }; return 1; }
pgrep() {
  case "$*" in
    *geomask-syncd.sh*) [ -f "$STATE/run/geomask-syncd" ] && return 0 ;;
  esac
  return 1
}
netstat() {
  [ -f "$STATE/run/port5055" ] && printf 'tcp 0 0 127.0.0.1:5055 0.0.0.0:* LISTEN\n'
  [ -f "$STATE/run/port5353" ] && printf 'udp 0 0 192.168.1.254:5353 0.0.0.0:* ESTABLISHED\n'
  return 0
}
crontab() {
  case "${1:-}" in
    -l) [ -f "$STATE/crontab" ] || return 1; cat "$STATE/crontab" ;;
    -)
      [ "${CRONTAB_WRITE_FAIL:-0}" = 1 ] && return 2
      cat > "$STATE/crontab.new" && mv -f "$STATE/crontab.new" "$STATE/crontab" ;;
    *)
      [ "${CRONTAB_WRITE_FAIL:-0}" = 1 ] && return 2
      cp "$1" "$STATE/crontab" ;;
  esac
}
initd() {
  printf '%s %s\n' "$1" "${2:-}" >> "$LOGS/initd.log"
  if [ "${SILENT_NOOP:-0}" = 1 ]; then return 0; fi
  if [ "${INITD_FAIL:-}" = "$1:$2" ]; then return 1; fi
  case "${2:-}" in
    enable)
      rm -f "$(P /etc/rc.d)"/*"$1" 2>/dev/null || true
      ln -s "../init.d/$1" "$(P "/etc/rc.d/S95$1")" ;;
    disable)
      rm -f "$(P /etc/rc.d)"/*"$1" 2>/dev/null || true
      ln -s "../init.d/$1" "$(P "/etc/rc.d/K01$1")" ;;
    start|restart|restart_verified)
      [ "${INITD_START_FAIL:-}" = "$1" ] && return 1
      : > "$STATE/run/$(_svc_marker "$1")" ;;
    stop)
      [ "${INITD_STOP_FAIL:-}" = "$1" ] && return 1
      rm -f "$STATE/run/$(_svc_marker "$1")" ;;
    reload)
      case "$1" in
        firewall) [ "${RELOAD_FAIL:-}" = firewall ] && return 1
                  [ "${FIREWALL_RELOAD_NOOP:-0}" = 1 ] && return 0
                  _firewall_reload ;;
        network)  [ "${RELOAD_FAIL:-}" = network ] && return 1 ;;
      esac ;;
  esac
  return 0
}
apk()  { printf 'APK %s\n' "$*" >> "$LOGS/pkg.log"; }
opkg() { printf 'OPKG %s\n' "$*" >> "$LOGS/pkg.log"; }
df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/x 100000 1 90000 1%% /\n'; }
'''


ARCHIVE = 'router-backup-20260101-000000.tar.gz'

FILES = {
    'etc/openwrt_release': "DISTRIB_RELEASE='24.10'\n",
    'etc/passwd': 'root:x:0:0:root:/root:/bin/ash\nnobody:x:65534:65534:nobody:/var:/bin/false\n',
    'etc/group': 'root:x:0:\nnogroup:x:65534:\n',
    'etc/config/network': (
        "config interface 'lan'\n"
        "\toption device 'br-lan'\n"
        "\toption proto 'static'\n"
        "\toption ipaddr '192.168.1.1'\n"
        "\toption netmask '255.255.255.0'\n"
        "\toption ip6assign '60'\n"),
    'etc/config/dhcp': (
        "config dnsmasq\n"
        "\toption domain 'lan'\n"
        "\toption server '1.1.1.1'\n"
        "config dhcp 'lan'\n"
        "\toption interface 'lan'\n"
        "\toption ra 'server'\n"
        "\toption dhcpv6 'server'\n"
        "\toption ndp 'server'\n"),
    'etc/config/ksmbd': "config globals\n\toption workgroup 'HOME'\n",
    'etc/config/firewall': "config zone\n\toption name 'lan'\n\toption network 'lan'\n",
    'etc/mihomo/config.yaml': 'mixed-port: 7890\n# geomask-managed: 1\n',
    'etc/mihomo/ruleset/ai.yaml': 'payload:\n  - OLD-AI\n',
    'etc/mihomo/ruleset/mtrickle.yaml': 'payload:\n  - OLD-MTRICKLE\n',
    'etc/magitrickle/state/config.yaml': 'groups:\n  - name: GoogleVideo VPN\n    enabled: true\n',
    'etc/geomask/version': 'v1.30.0\n',
    'etc/geomask/modules.json': '{"selected": ["core", "path_vpn"]}\n',
    'etc/geomask/lists/magitrickle-groups.yaml': 'groups:\n  - name: template\n',
    'etc/geomask/node-audit.json': '{"paths": []}\n',
    'opt/zapret/ipset/zapret-hosts-user-exclude.txt': '# nochange\nours.example\n',
    'opt/zapret/ipset/zapret-hosts-user.txt': 'kept.example\ntunneled.example\n',
    'usr/bin/geo-backend.sh': 'OLD BACKEND\n',
    'usr/bin/geomask-guard.sh': 'OLD GUARD\n',
    'usr/bin/geomask-syncd.sh': 'OLD SYNCD\n',
    'usr/lib/geomask/geomask-routes.sh': 'OLD ROUTES\n',
    'usr/libexec/rpcd/geo-sync': 'OLD RPC\n',
    'root/sync_mtrickle_mihomo.sh': 'OLD SYNC\n',
    'root/geomask-backups/' + ARCHIVE: 'archive-bytes\n',
}

SERVICES = ('dnsmasq', 'mihomo', 'magitrickle', 'hev-socks5-tunnel', 'rpcd',
            'network', 'firewall', 'ksmbd', 'geomask-syncd')
LINKS = {'S50mihomo': '../init.d/mihomo', 'S19firewall': '../init.d/firewall',
         'S20network': '../init.d/network', 'S60dnsmasq': '../init.d/dnsmasq',
         'S60rpcd': '../init.d/rpcd', 'S99geomask-syncd': '../init.d/geomask-syncd'}
RUNNING = ('dnsmasq', 'mihomo', 'magitrickled', 'rpcd', 'netifd', 'fw4')
SYSCTL = {'all': '0', 'default': '0', 'br-lan': '1', 'eth0': '0'}


def fixture_relative_paths():
    dirs = ['etc', 'tmp', 'proc/sys/net/ipv6/conf', 'usr/bin', 'usr/lib/geomask',
            'usr/libexec/rpcd', 'usr/share/rpcd/acl.d', 'usr/share/luci/menu.d',
            'usr/share/nftables.d/ruleset-post', 'www/luci-static/resources/view/geo-sync',
            'etc/init.d', 'etc/rc.d', 'etc/hotplug.d/iface', 'etc/geomask/lists',
            'etc/geomask/torrent', 'etc/mihomo/ruleset', 'etc/nftables.d', 'etc/sysctl.d',
            'etc/magitrickle/state', 'etc/hev-socks5-tunnel', 'opt/zapret/ipset',
            'root/geomask-backups']
    dirs += ['proc/sys/net/ipv6/conf/' + key for key in SYSCTL]
    return dirs


class Fixture:
    def __init__(self, case):
        self.case = case
        self.state = case / 'state'
        self.logs = case / 'logs'

    def build(self):
        for relative in fixture_relative_paths():
            (self.case / relative).mkdir(parents=True, exist_ok=True)
        # A router's storage dirs are not world-writable, and the symlink policy refuses a
        # target that is. GitHub runners use umask 000, so an unadjusted fixture produced 0777
        # directories and the policy correctly rejected them (CI-187 group E).
        for relative in ['.'] + fixture_relative_paths():
            try:
                (self.case / relative).chmod(0o755)
            except OSError:
                pass
        for relative, body in FILES.items():
            path = self.case / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding='utf-8', newline='\n')
        for relative in ('usr/bin/geo-backend.sh', 'usr/bin/geomask-guard.sh',
                         'usr/bin/geomask-syncd.sh',
                         'usr/lib/geomask/geomask-routes.sh',
                         'root/sync_mtrickle_mihomo.sh'):
            (self.case / relative).chmod(0o755)
        (self.case / 'etc/mihomo/config.yaml').chmod(0o600)
        for service in SERVICES:
            (self.case / 'etc/init.d' / service).write_text(
                '#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        for name, target in LINKS.items():
            os.symlink(target, self.case / 'etc/rc.d' / name)
        for key, value in SYSCTL.items():
            (self.case / 'proc/sys/net/ipv6/conf' / key / 'disable_ipv6').write_text(
                value + '\n', encoding='utf-8', newline='\n')
        self.state.mkdir(exist_ok=True)
        self.logs.mkdir(exist_ok=True)
        (self.state / 'run').mkdir(exist_ok=True)
        for marker in RUNNING:
            (self.state / 'run' / marker).write_text('1\n', encoding='utf-8')
        (self.state / 'crontab').write_text('* * * * * /usr/bin/existing-job\n',
                                            encoding='utf-8', newline='\n')
        for name in ('ip_rules', 'nft_tables', 'nft_chains'):
            (self.state / name).write_text('', encoding='utf-8')
        (self.logs / 'info.log').write_text('', encoding='utf-8')
        (self.logs / 'initd.log').write_text('', encoding='utf-8')
        (self.logs / 'pkg.log').write_text('', encoding='utf-8')

    def uci(self, pkg):
        path = self.case / 'etc/config' / pkg
        if not path.exists():
            return None
        sections = []
        for line in path.read_text(encoding='utf-8').splitlines():
            text = line.strip()
            if not text or text.startswith('#'):
                continue
            match = re.match(r"^config\s+(\S+)(?:\s+'([^']*)')?", text)
            if match:
                sections.append([match.group(2) or '', match.group(1), {}])
                continue
            match = re.match(r"^(?:option|list)\s+(\S+)\s+'(.*)'\s*$", text)
            if match and sections:
                sections[-1][2][match.group(1)] = match.group(2)
        return sections

    def capture(self):
        byte_paths = [entry['path'] for entry in MANIFEST['files']
                      if entry.get('snapshot') in ('SB', 'SBA', 'SAP')]
        state = {'bytes': {}, 'shared': {}, 'dirs': {}, 'cron': None, 'uci': {},
                 'links': {}, 'running': [], 'sysctl': {}, 'tables': None,
                 'chains': None, 'iprules': None}
        shared = ('/etc/passwd', '/etc/group',
                  '/opt/zapret/ipset/zapret-hosts-user-exclude.txt',
                  '/opt/zapret/ipset/zapret-hosts-user.txt')
        for path in byte_paths:
            local = self.case / path.lstrip('/')
            if local.is_dir() and not local.is_symlink():
                state['dirs'][path] = True
                continue
            if path in shared:
                state['shared'][path] = local.read_text(encoding='utf-8', errors='replace')
                continue
            state['bytes'][path] = local.read_bytes() if local.exists() else None
        cron = self.state / 'crontab'
        state['cron'] = cron.read_text(encoding='utf-8') if cron.exists() else None
        for pkg in ('network', 'dhcp', 'ksmbd', 'firewall'):
            state['uci'][pkg] = self.uci(pkg)
        for entry in sorted((self.case / 'etc/rc.d').iterdir()):
            # Git Bash creates Windows symlinks and stores the target with backslashes;
            # the POSIX contract is compared with / (the router only has /).
            state['links'][entry.name] = posix_link(os.readlink(entry))
        state['running'] = sorted(entry.name for entry in (self.state / 'run').iterdir())
        for key in SYSCTL:
            path = self.case / 'proc/sys/net/ipv6/conf' / key / 'disable_ipv6'
            state['sysctl'][key] = path.read_text(encoding='utf-8').strip() \
                if path.exists() else None
        for name, out in (('tables', 'nft_tables'), ('chains', 'nft_chains'),
                          ('iprules', 'ip_rules')):
            path = self.state / out
            state[name] = path.read_text(encoding='utf-8') if path.exists() else None
        return state


def normalise_cron(text):
    if not text:
        return []
    return [line for line in text.splitlines() if line.strip()]


def neutral_shared(path, text):
    """The part of a shared file geomask does not own (order preserved)."""
    if path in ('/etc/passwd', '/etc/group'):
        return [line for line in text.splitlines() if not line.startswith('dnsai:')]
    if path.endswith('zapret-hosts-user-exclude.txt'):
        out, inside = [], False
        for line in text.splitlines():
            if line.strip() == '# >>> geomask sync: tunneled domains (generated) >>>':
                inside = True
                continue
            if line.strip() == '# <<< geomask sync: tunneled domains <<<':
                inside = False
                continue
            if not inside:
                out.append(line)
        return out
    if path.endswith('zapret-hosts-user.txt'):
        prefix = '# geomask: tunneled, DPI removed '
        return [line[len(prefix):] if line.startswith(prefix) else line
                for line in text.splitlines()]
    return text.splitlines()


@unittest.skipUnless(win_symlink.NATIVE_SYMLINKS, win_symlink.SYMLINK_REASON)
class Phase2bTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = self.root / 'case'
        self.case.mkdir()
        self.fixture = Fixture(self.case)
        self.fixture.build()
        self.snapshot_dir = self.case / 'tmp' / 'geomask-reinstall-test'
        self.listing_before = self.fixture.capture()

    # ---------------------------------------------------------------- helpers
    def script(self, body, extra=''):
        parts = []
        for name in reinstall_functions():
            parts.append(extract(name))
        support = (SUPPORT.replace('@PREFIX@', posix(self.case))
                   .replace('@STATE@', posix(self.fixture.state))
                   .replace('@LOGS@', posix(self.fixture.logs))
                   .replace('@MODULES@', ''))
        # R04: the shipped rollback takes the kernel config-tx lock (and cron lock) through the
        # installed helper; the fixture root injects it exactly like the installed file.
        support += ('\nGM_LOCK_DIR="%s/locks"\nGM_CONFIG_TX_LOCK="%s/tx.lock"\n'
                    'GM_LOCK_LIB="%s"\n. "$GM_LOCK_LIB"\n'
                    % (posix(self.case), posix(self.case),
                       posix(ROOT / 'assets' / 'geomask-lock.sh')))
        text = (support + template_tables() + HARNESS_STUBS + '\n' + '\n'.join(parts) + '\n'
                + extra + '\n' + body)
        path = self.case / 'run.sh'
        path.write_text(text, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, posix(path)], cwd=str(self.case),
                              env=win_symlink.bash_env(),
                              capture_output=True, text=True, encoding='utf-8',
                              timeout=120)

    def logs(self):
        return (self.fixture.logs / 'info.log').read_text(encoding='utf-8',
                                                          errors='replace')

    def snap(self):
        return '_reinstall_snapshot "%s"; printf "SNAP_RC=%%s\\n" "$?"' % posix(self.snapshot_dir)

    def restore(self):
        return '_reinstall_restore "%s"; printf "RESTORE_RC=%%s\\n" "$?"' % posix(self.snapshot_dir)

    def run_rollback(self, mutation, extra=''):
        body = self.snap() + '\n' + mutation + '\n' + self.restore()
        return self.script(body, extra=extra)

    def rc_of(self, result, marker):
        match = re.search(r'^%s=(\d+)$' % marker, result.stdout, re.M)
        self.assertIsNotNone(match, result.stdout + result.stderr)
        return int(match.group(1))

    def assertSnapshotKept(self):
        self.assertTrue(self.snapshot_dir.exists(),
                        'a failed rollback deleted the only copy of the old state')

    def assertIncomplete(self, result):
        output = result.stdout + result.stderr + self.logs()
        self.assertEqual(1, self.rc_of(result, 'RESTORE_RC'), output[-2500:])
        self.assertIn('rollback incomplete', self.logs(), output[-2500:])
        self.assertIn('recovery required', self.logs(), output[-2500:])

    # ---------------------------------------------------------------- manifest coverage
    def test_snapshot_records_every_manifest_byte_object(self):
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        objects = (self.snapshot_dir / 'objects').read_text(encoding='utf-8')
        recorded = {line.split('|')[1] for line in objects.splitlines() if line}
        expected = {entry['path'] for entry in MANIFEST['files']
                    if entry.get('snapshot') in ('SB', 'SBA', 'SAP')}
        self.assertEqual(expected, recorded, 'snapshot does not cover the manifest objects')

    def test_snapshot_records_identity_mode_and_hash(self):
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        objects = (self.snapshot_dir / 'objects').read_text(encoding='utf-8')
        row = [line for line in objects.splitlines()
               if line.startswith('file|/usr/bin/geo-backend.sh|')][0]
        fields = row.split('|')
        self.assertEqual(7, len(fields), row)
        # Windows does not store POSIX exec bits (chmod only toggles read-only), so the
        # exact 755 is a Linux/router property; the octal shape and the restore
        # verification of the recorded mode are what this host can prove.
        self.assertRegex(fields[2], r'^[0-7]{3,4}$')
        self.assertRegex(fields[4], r'^\d+:\d+$')
        self.assertRegex(fields[5], r'^\d+$')
        self.assertRegex(fields[6], r'^(sha256|md5|sha1):[0-9a-f]+$')
        self.assertTrue((self.snapshot_dir / 'files/usr/bin/geo-backend.sh').exists())

    def test_snapshot_records_uci_runtime_cron_and_links(self):
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        links = (self.snapshot_dir / 'links').read_text(encoding='utf-8')
        self.assertIn('S99geomask-syncd|../init.d/geomask-syncd', links)
        services = (self.snapshot_dir / 'services').read_text(encoding='utf-8')
        self.assertIn('svc.dnsmasq|dnsmasq|dnsmasq|pidof|1|1', services)
        uci = (self.snapshot_dir / 'uci').read_text(encoding='utf-8')
        self.assertIn('network|lan|ip6assign|60', uci)
        self.assertIn('dhcp|lan|ra|server', uci)
        sysctl = (self.snapshot_dir / 'sysctl').read_text(encoding='utf-8')
        self.assertIn('br-lan|1', sysctl)
        self.assertIn('eth0|0', sysctl)
        cron = (self.snapshot_dir / 'cron').read_text(encoding='utf-8')
        self.assertIn('/usr/bin/existing-job', cron)

    def test_snapshot_records_absent_objects(self):
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        objects = (self.snapshot_dir / 'objects').read_text(encoding='utf-8')
        rows = [line for line in objects.splitlines() if line]
        self.assertTrue(any(line.startswith('absent|/etc/init.d/ai-doh|') for line in rows),
                        objects)
        self.assertTrue(any(line.startswith('absent|/etc/geomask-aidns.uid|') for line in rows),
                        objects)

    def test_snapshot_refuses_a_symlink_where_bytes_are_stored(self):
        target = self.case / 'usr/bin/geo-backend.sh'
        target.unlink()
        os.symlink('missing-backend.sh', target)
        result = self.script(self.snap())
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        self.assertIn('symlink', self.logs())

    def test_snapshot_refuses_an_unobservable_service_state(self):
        result = self.script(self.snap(), extra='HAVE_MISSING="pidof pgrep"\n')
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        self.assertIn('unobservable', self.logs())

    def test_snapshot_refuses_a_foreign_pending_uci_delta(self):
        delta = self.case / 'tmp/.uci'
        delta.mkdir(parents=True)
        (delta / 'network').write_text(
            "config interface 'lan'\n\toption ipaddr '10.0.0.1'\n", encoding='utf-8')
        result = self.script(self.snap())
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        self.assertIn('pending', self.logs())

    def test_snapshot_refuses_a_directory_where_a_shared_file_is_tracked(self):
        passwd = self.case / 'etc/passwd'
        passwd.unlink()
        passwd.mkdir()
        result = self.script(self.snap())
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)

    def test_snapshot_refuses_when_crontab_state_is_unknown(self):
        body = ('crontab() { if [ "$1" = "-l" ]; then return 2; fi; return 0; }\n'
                + self.snap())
        result = self.script(body)
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        self.assertIn('crontab', self.logs())

    # ---------------------------------------------------------------- boundaries
    def mutation(self, boundary):
        return MUTATION_TEMPLATE.replace('@BOUNDARY@', boundary)

    def assertStateRestored(self, before, result):
        after = self.fixture.capture()
        messages = []
        for path in sorted(set(before['bytes']) | set(after['bytes'])):
            if before['bytes'].get(path) != after['bytes'].get(path):
                messages.append('bytes differ: %s' % path)
        for path in sorted(before['shared']):
            if neutral_shared(path, before['shared'][path]) != \
                    neutral_shared(path, after['shared'][path]):
                messages.append('shared foreign part changed: %s' % path)
        if before['shared'] != after['shared']:
            messages.append('shared files not restored byte-for-byte')
        if normalise_cron(before['cron']) != normalise_cron(after['cron']):
            messages.append('crontab not restored: %r -> %r'
                            % (normalise_cron(before['cron']), normalise_cron(after['cron'])))
        for pkg in before['uci']:
            if before['uci'][pkg] != after['uci'][pkg]:
                messages.append('uci %s not restored: %r -> %r'
                                % (pkg, before['uci'][pkg], after['uci'][pkg]))
        if before['links'] != after['links']:
            messages.append('rc.d links differ: %r -> %r'
                            % (before['links'], after['links']))
        if before['running'] != after['running']:
            messages.append('running services differ: %r -> %r'
                            % (before['running'], after['running']))
        if before['sysctl'] != after['sysctl']:
            messages.append('per-interface sysctl not restored: %r -> %r'
                            % (before['sysctl'], after['sysctl']))
        for name in ('tables', 'chains', 'iprules'):
            if (before[name] or '').strip() != (after[name] or '').strip():
                messages.append('%s differ: %r -> %r' % (name, before[name], after[name]))
        if before['dirs'] != after['dirs']:
            messages.append('tracked directories differ')
        self.assertEqual([], messages,
                         'rollback did not restore the pre-install state:\n'
                         + '\n'.join(messages) + '\n--- output ---\n'
                         + result.stdout + result.stderr + '\n--- log ---\n' + self.logs())

    def test_step4_success_then_step5_failure_is_fully_rolled_back(self):
        before = self.listing_before
        result = self.run_rollback(self.mutation('after-step4'))
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertStateRestored(before, result)
        self.assertFalse(self.snapshot_dir.exists(),
                         'a verified rollback should release the snapshot')
        self.assertIn('verified', self.logs())

    def test_every_install_boundary_is_rolled_back(self):
        boundaries = ['before-uninstall', 'after-uninstall', 'after-step2-apply',
                      'after-step2-verify', 'after-mihomo-write', 'after-mihomo-api',
                      'after-step4-first-writer', 'after-step4-apply', 'after-step4-verify',
                      'after-step5-first-writer', 'after-step5-apply', 'after-step5-verify',
                      'after-step6', 'after-ipv6', 'after-step7']
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                shutil.rmtree(self.case)
                self.case.mkdir()
                self.fixture = Fixture(self.case)
                self.fixture.build()
                before = self.fixture.capture()
                result = self.run_rollback(self.mutation(boundary))
                self.assertEqual(0, self.rc_of(result, 'SNAP_RC'),
                                 result.stdout + result.stderr)
                self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                                 boundary + '\n' + result.stdout + result.stderr
                                 + '\n' + self.logs())
                self.assertStateRestored(before, result)
                self.assertFalse(self.snapshot_dir.exists(), boundary)

    # ---------------------------------------------------------------- verification vs rc
    def test_silent_success_without_change_is_caught(self):
        """uci commit returns 0 but applies nothing: trusting rc would be a fake success."""
        mutation = self.mutation('after-step4')
        body = (self.snap() + '\n' + mutation + '\n'
                + 'UCI_COMMIT_NOOP=1\n' + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_silent_service_success_without_change_is_caught(self):
        # the failed install stopped magitrickle; the rollback must start it again. SILENT_NOOP
        # makes every initd action report success without doing anything - a fake rollback.
        mutation = (self.mutation('after-step4')
                    + '\nrm -f "%s/run/magitrickled"\n' % posix(self.fixture.state))
        body = (self.snap() + '\n' + mutation + '\n'
                + 'SILENT_NOOP=1\n' + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_rollback_copy_failure_keeps_the_snapshot(self):
        mutation = self.mutation('after-step4')
        body = (self.snap() + '\n' + mutation + '\n'
                + 'cp() { case "$*" in *geo-backend.sh*) return 1 ;; esac; command cp "$@"; }\n'
                + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_rollback_chmod_failure_keeps_the_snapshot(self):
        mutation = self.mutation('after-step4')
        body = (self.snap() + '\n' + mutation + '\n'
                + 'chmod() { case "$*" in *geo-backend.sh*) return 1 ;; esac; command chmod "$@"; }\n'
                + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_rollback_link_failure_keeps_the_snapshot(self):
        mutation = self.mutation('after-step4')
        body = (self.snap() + '\n' + mutation + '\n'
                + 'ln() { return 1; }\n' + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_rollback_crontab_write_failure_keeps_the_snapshot(self):
        mutation = self.mutation('after-step5')
        body = (self.snap() + '\n' + mutation + '\n'
                + 'CRONTAB_WRITE_FAIL=1\n' + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_rollback_uci_commit_failure_keeps_the_snapshot(self):
        mutation = self.mutation('after-step4')
        body = (self.snap() + '\n' + mutation + '\n'
                + 'UCI_COMMIT_FAIL=1\n' + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_rollback_network_reload_failure_keeps_the_snapshot(self):
        mutation = self.mutation('after-step4')
        body = (self.snap() + '\n' + mutation + '\n'
                + 'RELOAD_FAIL=network\n' + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_rollback_firewall_reload_failure_keeps_the_snapshot(self):
        mutation = self.mutation('after-step4')
        body = (self.snap() + '\n' + mutation + '\n'
                + 'RELOAD_FAIL=firewall\n' + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_rollback_stop_failure_keeps_the_snapshot(self):
        mutation = self.mutation('after-step6')
        body = (self.snap() + '\n' + mutation + '\n'
                + 'INITD_STOP_FAIL=ai-doh\n' + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_incomplete_snapshot_is_refused_and_kept(self):
        mutation = self.mutation('after-step4')
        body = (self.snap() + '\n'
                + 'rm -rf "%s/files/usr/bin"\n' % posix(self.snapshot_dir)
                + mutation + '\n' + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    # ---------------------------------------------------------------- services tri-state
    def test_all_four_enabled_running_combinations_are_restored(self):
        combos = [('enabled', 'running'), ('enabled', 'stopped'),
                  ('disabled', 'running'), ('disabled', 'stopped')]
        for enabled, running in combos:
            with self.subTest(enabled=enabled, running=running):
                shutil.rmtree(self.case)
                self.case.mkdir()
                self.fixture = Fixture(self.case)
                self.fixture.build()
                link = self.case / 'etc/rc.d/S50mihomo'
                marker = self.fixture.state / 'run/mihomo'
                if enabled == 'disabled':
                    link.unlink()
                    os.symlink('../init.d/mihomo', self.case / 'etc/rc.d/K01mihomo')
                if running == 'stopped':
                    marker.unlink()
                before = self.fixture.capture()
                mutation = ('rm -f "%s/etc/rc.d"/*mihomo\n'
                            'ln -s "../init.d/mihomo" "%s/etc/rc.d/S50mihomo"\n'
                            ': > "%s/run/mihomo"\n'
                            % (posix(self.case), posix(self.case),
                               posix(self.fixture.state)))
                result = self.run_rollback(mutation)
                self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                                 result.stdout + result.stderr + self.logs())
                self.assertStateRestored(before, result)

    def test_initially_stopped_service_stays_stopped(self):
        before = self.listing_before
        mutation = ': > "%s/run/magitrickled"\n' % posix(self.fixture.state)
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertStateRestored(before, result)

    def test_a_service_the_install_started_is_stopped_again(self):
        marker = self.fixture.state / 'run/port5055'
        # step 4 installs the init script and then starts the worker: reproduce both
        mutation = ('printf "#!/bin/sh\\n" > "%s"\n'
                    ': > "%s"\n' % (posix(self.case / 'etc/init.d/ai-doh'), posix(marker)))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertFalse(marker.exists(), 'ai-doh was left running after the rollback')

    # ---------------------------------------------------------------- links
    def test_relative_absolute_and_dangling_links_are_restored_exactly(self):
        os.symlink('/etc/init.d/ksmbd', self.case / 'etc/rc.d/K01ksmbd')
        os.symlink('../init.d/gone-service', self.case / 'etc/rc.d/S97foreign')
        before = self.fixture.capture()
        mutation = ('rm -f "%s/etc/rc.d"/*geomask* "%s/etc/rc.d"/*ksmbd\n'
                    'ln -s "../init.d/geomask-syncd" "%s/etc/rc.d/S99geomask-syncd"\n'
                    % (posix(self.case), posix(self.case), posix(self.case)))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertStateRestored(before, result)

    def test_a_foreign_rc_link_is_not_deleted(self):
        foreign = self.case / 'etc/rc.d/S99foreign-daemon'
        mutation = ('rm -f "%s/etc/rc.d"/*geomask*\n' % posix(self.case)
                    + 'ln -s "../init.d/foreign-daemon" "%s"\n' % posix(foreign))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertTrue(foreign.is_symlink(),
                        'a foreign rc.d link was deleted by the rollback')

    # ---------------------------------------------------------------- foreign objects
    def test_a_foreign_file_in_a_shared_directory_survives(self):
        foreign = self.case / 'usr/bin/foreign-tool'
        mutation = ('printf "foreign\\n" > "%s"\n' % posix(foreign)
                    + self.mutation('after-step4'))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertTrue(foreign.exists(), 'a third-party file was deleted by the rollback')

    def test_a_new_file_with_our_prefix_but_no_record_survives(self):
        leftover = self.case / 'usr/bin/geomask-leftover'
        mutation = ('printf "new\\n" > "%s"\n' % posix(leftover)
                    + self.mutation('after-step4'))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertTrue(leftover.exists(),
                        'the rollback deleted a file it never recorded')

    def test_a_preexisting_backup_with_the_same_name_is_not_deleted(self):
        """A same-named .bak that existed before the run is not ours to delete.

        Its bytes are not part of the snapshot (only manifest objects are), so if the install
        overwrote it the rollback cannot bring the old content back - it must not delete the
        file either. That limitation is reported honestly, not hidden.
        """
        bak = self.case / 'etc/mihomo/config.yaml.bak_geomask_1000'
        bak.write_text('owner backup\n', encoding='utf-8', newline='\n')
        mutation = ('printf "new\\n" > "%s"\n' % posix(bak)
                    + self.mutation('after-step4'))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertTrue(bak.exists(),
                        'the rollback deleted a same-named backup it did not create')

    def test_temporary_files_created_by_the_transaction_are_removed(self):
        mutation = ('printf "half\\n" > "%s"\n'
                    'printf "half\\n" > "%s"\n'
                    % (posix(self.case / 'etc/mihomo/config.yaml.zmnew'),
                       posix(self.case / 'etc/mihomo/ruleset/ai.yaml.bak_geomask_9999'))
                    + self.mutation('after-step4'))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertFalse((self.case / 'etc/mihomo/config.yaml.zmnew').exists(),
                         'our half-written temp file survived the rollback')
        self.assertFalse((self.case / 'etc/mihomo/ruleset/ai.yaml.bak_geomask_9999').exists(),
                         'our transaction backup survived the rollback')

    # ---------------------------------------------------------------- conflicts
    def test_concurrent_passwd_change_is_a_conflict(self):
        mutation = (self.mutation('after-step4')
                    + '\nprintf "intruder:x:777:777::/:/bin/false\\n" >> "%s"\n'
                    % posix(self.case / 'etc/passwd'))
        result = self.run_rollback(mutation)
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertIn('intruder', (self.case / 'etc/passwd').read_text(encoding='utf-8'))

    def test_concurrent_cron_change_is_a_conflict(self):
        mutation = (self.mutation('after-step5')
                    + '\nprintf "* * * * * /opt/foreign.sh\\n" >> "%s/crontab"\n'
                    % posix(self.fixture.state))
        result = self.run_rollback(mutation)
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertIn('/opt/foreign.sh',
                      (self.fixture.state / 'crontab').read_text(encoding='utf-8'))

    def test_concurrent_zapret_exclude_change_is_a_conflict(self):
        mutation = (self.mutation('after-step5')
                    + '\nprintf "foreign.example\\n" >> "%s"\n'
                    % posix(self.case / 'opt/zapret/ipset/zapret-hosts-user-exclude.txt'))
        result = self.run_rollback(mutation)
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertIn('foreign.example',
                      (self.case / 'opt/zapret/ipset/zapret-hosts-user-exclude.txt')
                      .read_text(encoding='utf-8'))

    def test_pending_uci_delta_during_the_transaction_is_a_conflict(self):
        mutation = (self.mutation('after-step4')
                    + '\nmkdir -p "%s/tmp/.uci"; printf "staged\\n" > "%s/tmp/.uci/network"\n'
                    % (posix(self.case), posix(self.case)))
        result = self.run_rollback(mutation)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_a_foreign_uci_key_change_is_preserved(self):
        """Only our keys are reverted: a concurrent edit of network.lan.ipaddr survives."""
        mutation = (self.mutation('after-step4')
                    + '\nuci set network.lan.ipaddr=10.9.9.9\nuci commit network\n')
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        network = self.fixture.uci('network')
        lan = [options for name, _kind, options in network if name == 'lan'][0]
        lan = dict(lan)
        self.assertEqual('10.9.9.9', lan.get('ipaddr'),
                         'the rollback overwrote a foreign uci option')
        self.assertEqual('60', lan.get('ip6assign'), 'our ip6assign was not reverted')

    # ---------------------------------------------------------------- IPv6 per interface
    def test_per_interface_ipv6_values_are_restored_individually(self):
        before = self.listing_before
        mutation = ('for d in all default br-lan eth0; do '
                    'printf "1\\n" > "%s/proc/sys/net/ipv6/conf/$d/disable_ipv6"; done\n'
                    % posix(self.case)
                    + self.mutation('after-ipv6'))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertStateRestored(before, result)
        self.assertEqual('1', (self.case / 'proc/sys/net/ipv6/conf/br-lan/disable_ipv6')
                         .read_text(encoding='utf-8').strip())
        self.assertEqual('0', (self.case / 'proc/sys/net/ipv6/conf/eth0/disable_ipv6')
                         .read_text(encoding='utf-8').strip())

    def test_unreadable_per_interface_ipv6_value_refuses_the_snapshot(self):
        path = self.case / 'proc/sys/net/ipv6/conf/br-lan/disable_ipv6'
        path.unlink()
        path.mkdir()
        result = self.script(self.snap())
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        self.assertIn('sysctl', self.logs())

    def test_a_vanished_interface_makes_the_rollback_incomplete(self):
        mutation = (self.mutation('after-step4')
                    + '\nrm -rf "%s/proc/sys/net/ipv6/conf/eth0"\n' % posix(self.case))
        result = self.run_rollback(mutation)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    # ---------------------------------------------------------------- NR honesty
    def test_magitrickle_state_change_is_reported_not_claimed_as_rollback(self):
        mutation = (self.mutation('after-step5')
                    + '\nprintf "groups: []\\n" > "%s"\n'
                    % posix(self.case / 'etc/magitrickle/state/config.yaml'))
        result = self.run_rollback(mutation)
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertIn('MagiTrickle', self.logs())

    def test_witness_logs_are_not_rollback_failures(self):
        mutation = ('printf "event\\n" >> "%s"\n'
                    % posix(self.case / 'root/geo-sync.log')
                    + self.mutation('after-step4'))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertTrue((self.case / 'root/geo-sync.log').exists())
        self.assertIn('NR', self.logs())

    def test_provider_refresh_is_reported_as_not_restorable(self):
        before = self.listing_before
        result = self.run_rollback(self.mutation('after-mihomo-api'))
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertStateRestored(before, result)
        self.assertIn('provider', self.logs())

    def test_absent_crontab_restores_to_empty(self):
        (self.fixture.state / 'crontab').unlink()
        before = self.fixture.capture()
        mutation = ('printf "0 4 * * * /usr/bin/geomask-guard.sh >/dev/null 2>&1 # geomask-guard\\n" | crontab -\n'
                    + self.mutation('after-step4'))
        result = self.run_rollback(mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertEqual([], normalise_cron(
            (self.fixture.state / 'crontab').read_text(encoding='utf-8')
            if (self.fixture.state / 'crontab').exists() else None))
        self.assertStateRestored(before, result)

    def test_ksmbd_managed_section_is_removed_again(self):
        before = self.fixture.capture()
        result = self.run_rollback(self.mutation('after-step2-apply'))
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertStateRestored(before, result)


MUTATION_TEMPLATE = r'''# --- install simulator: realistic writer effects up to @BOUNDARY@
_sim_uninstall() {
  rm -f "$(P /usr/bin/geo-backend.sh)" "$(P /usr/libexec/rpcd/geo-sync)" \
        "$(P /usr/share/rpcd/acl.d/luci-app-geo-sync.json)" \
        "$(P /usr/share/luci/menu.d/luci-app-geo-sync.json)" \
        "$(P /www/luci-static/resources/view/geo-sync/main.js)" \
        "$(P /root/sync_mtrickle_mihomo.sh)" "$(P /usr/bin/geomask-guard.sh)" \
        "$(P /usr/bin/geomask-syncd.sh)" "$(P /usr/lib/geomask/geomask-routes.sh)"
  crontab -l 2>/dev/null | grep -v 'geomask' | crontab - 2>/dev/null || true
  initd geomask-syncd stop >/dev/null 2>&1 || true
  initd geomask-syncd disable >/dev/null 2>&1 || true
  rm -f "$(P /etc/init.d/geomask-syncd)"
}
_sim_step2() {
  printf '#!/bin/sh\n# new backend\n' > "$(P /usr/bin/geo-backend.sh)"
  printf '#!/bin/sh\n# new guard\n' > "$(P /usr/bin/geomask-guard.sh)"
  printf '#!/bin/sh\n# new syncd\n' > "$(P /usr/bin/geomask-syncd.sh)"
  printf '#!/bin/sh\n# new init\n' > "$(P /etc/init.d/geomask-syncd)"
  printf '#!/bin/sh\n# new rpc\n' > "$(P /usr/libexec/rpcd/geo-sync)"
  printf '{"new": 1}\n' > "$(P /usr/share/rpcd/acl.d/luci-app-geo-sync.json)"
  printf '{"new": 1}\n' > "$(P /usr/share/luci/menu.d/luci-app-geo-sync.json)"
  printf '// new\n' > "$(P /www/luci-static/resources/view/geo-sync/main.js)"
  printf '#!/bin/sh\n# new sync\n' > "$(P /root/sync_mtrickle_mihomo.sh)"
  printf 'v1.31.0\n' > "$(P /etc/geomask/version)"
  printf '{"selected": ["core"]}\n' > "$(P /etc/geomask/modules.json)"
  printf '{"modules": {"core": 1}}\n' > "$(P /etc/geomask/modules-state.json)"
  printf '{"modules-index": 1}\n' > "$(P /etc/geomask/modules-index.json)"
  printf '#!/bin/sh\n# new routes\n' > "$(P /usr/lib/geomask/geomask-routes.sh)"
  _sec=$(uci add ksmbd share)
  uci set "ksmbd.$_sec.name=geomask-backups"
  uci set "ksmbd.$_sec.path=/root/geomask-backups"
  uci set "ksmbd.$_sec.read_only=1"
  uci commit ksmbd
  initd rpcd reload >/dev/null 2>&1 || true
}
_sim_mihomo() {
  printf 'mixed-port: 7890\nnew: true\n' > "$(P /etc/mihomo/config.yaml)"
  printf 'old bytes\n' > "$(P "/etc/mihomo/config.yaml.bak_geomask_1700000000")"
  printf 'partial\n' > "$(P /etc/mihomo/config.yaml.zmnew)"
  printf 'partial\n' > "$(P /etc/mihomo/ruleset/ai.yaml.bak_geomask_1700000000)"
}
_sim_mihomo_api() { printf 'API PUT /configs\n' >> "$LOGS/api.log"; }
_sim_step4_first() {
  grep -q '^dnsai:' "$(P /etc/passwd)" || printf 'dnsai:x:65533:65533:geomask AI-DNS:/var/run/dnsai:/bin/false\n' >> "$(P /etc/passwd)"
  grep -q '^dnsai:' "$(P /etc/group)" || printf 'dnsai:x:65533:\n' >> "$(P /etc/group)"
  printf '65533\n' > "$(P /etc/geomask-aidns.uid)"
}
_sim_step4() {
  uci set network.lanai=interface
  uci set network.lanai.proto=static
  uci set network.lanai.device=br-lan
  uci set network.lanai.ipaddr=192.168.1.254
  uci set network.lanai.netmask=255.255.255.0
  uci set dhcp.lan.ra=disabled
  uci set dhcp.lan.dhcpv6=disabled
  uci set dhcp.lan.ndp=disabled
  uci commit network
  uci commit dhcp
  printf '#!/bin/sh\n# ai-doh\n' > "$(P /etc/init.d/ai-doh)"
  printf '#!/bin/sh\n# dnsmasq-ai\n' > "$(P /etc/init.d/dnsmasq-ai)"
  printf 'listen-address=192.168.1.254\nport=5353\n' > "$(P /etc/dnsmasq-ai.conf)"
  printf 'add table inet geomask_ai_dns\n' > "$(P /usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft)"
  ip rule add pref 19902 fwmark 0x64 table 101
  initd ai-doh enable >/dev/null 2>&1 || true
  initd dnsmasq-ai enable >/dev/null 2>&1 || true
  initd ai-doh restart >/dev/null 2>&1 || true
  initd dnsmasq-ai restart >/dev/null 2>&1 || true
  initd firewall reload >/dev/null 2>&1 || true
}
_sim_step5_first() {
  printf 'payload:\n  - NEW-AI\n' > "$(P /etc/mihomo/ruleset/ai.yaml)"
}
_sim_step5() {
  printf 'payload:\n  - NEW-MTRICKLE\n' > "$(P /etc/mihomo/ruleset/mtrickle.yaml)"
  printf 'payload:\n  - NEW-BULK\n' > "$(P /etc/mihomo/ruleset/mtrickle_bulk.yaml)"
  printf '#!/bin/sh\n# hotplug\n' > "$(P /etc/hotplug.d/iface/99-default-vpn-route)"
  printf '#!/bin/sh\n# hotplug guard\n' > "$(P /etc/hotplug.d/iface/99-geomask-guard)"
  printf '# >>> geomask sync: tunneled domains (generated) >>>\ntunneled.example\n# <<< geomask sync: tunneled domains <<<\n' >> "$(P /opt/zapret/ipset/zapret-hosts-user-exclude.txt)"
  sed -i 's/^tunneled.example$/# geomask: tunneled, DPI removed tunneled.example/' "$(P /opt/zapret/ipset/zapret-hosts-user.txt)"
  printf '{"paths": ["new"]}\n' > "$(P /etc/geomask/node-audit.json)"
  printf 'groups:\n  - name: template\n' > "$(P /etc/geomask/lists/magitrickle-groups.yaml)"
  ( crontab -l 2>/dev/null; echo '*/5 * * * * /root/sync_mtrickle_mihomo.sh' ) | crontab - 2>/dev/null || true
  ( crontab -l 2>/dev/null; echo '*/5 * * * * /usr/bin/geomask-guard.sh >/dev/null 2>&1 # geomask-guard' ) | crontab - 2>/dev/null || true
  initd magitrickle stop >/dev/null 2>&1 || true
  initd magitrickle start >/dev/null 2>&1 || true
  initd geomask-syncd enable >/dev/null 2>&1 || true
  initd geomask-syncd restart >/dev/null 2>&1 || true
}
_sim_step6() {
  initd dnsmasq start >/dev/null 2>&1 || true
  initd mihomo start >/dev/null 2>&1 || true
  initd rpcd start >/dev/null 2>&1 || true
  initd dnsmasq-ai start >/dev/null 2>&1 || true
}
_sim_ipv6() {
  printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n' > "$(P /etc/sysctl.d/99-disable-ipv6.conf)"
  for d in all default br-lan eth0; do printf '1\n' > "$(P "/proc/sys/net/ipv6/conf/$d/disable_ipv6")"; done
  printf 'table inet fw4 {\n    chain geomask_no_v6 {\n    }\n}\n' > "$(P /etc/nftables.d/99-geomask-no-v6.nft)"
  uci set network.lan.ip6assign=0; uci set dhcp.lan.ra=disabled
  uci set dhcp.lan.dhcpv6=disabled; uci set dhcp.lan.ndp=disabled
  uci commit network; uci commit dhcp
  initd network reload >/dev/null 2>&1 || true
  initd firewall reload >/dev/null 2>&1 || true
  printf 'ip6assign=60\nra=server\ndhcpv6=server\nndp=server\nsysctl_all=0\nsysctl_default=0\nnft=absent\n' > "$(P /etc/geomask/ipv6.state)"
}
_sim_run() {
  case "$1" in before-uninstall) return 1 ;; esac
  _sim_uninstall
  case "$1" in after-uninstall) return 1 ;; esac
  _sim_step2
  case "$1" in after-step2-apply) return 1 ;; esac
  case "$1" in after-step2-verify) return 1 ;; esac
  _sim_mihomo
  case "$1" in after-mihomo-write) return 1 ;; esac
  _sim_mihomo_api
  case "$1" in after-mihomo-api) return 1 ;; esac
  _sim_step4_first
  case "$1" in after-step4-first-writer) return 1 ;; esac
  _sim_step4
  case "$1" in after-step4-apply) return 1 ;; esac
  case "$1" in after-step4|after-step4-verify) return 1 ;; esac
  _sim_step5_first
  case "$1" in after-step5-first-writer) return 1 ;; esac
  _sim_step5
  case "$1" in after-step5-apply) return 1 ;; esac
  case "$1" in after-step5-verify) return 1 ;; esac
  _sim_step6
  case "$1" in after-step6) return 1 ;; esac
  _sim_ipv6
  case "$1" in after-ipv6) return 1 ;; esac
  case "$1" in after-step7) return 1 ;; esac
  return 1
}
_sim_run @BOUNDARY@
'''


if __name__ == '__main__':
    unittest.main()
