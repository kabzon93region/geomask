# geomask-lock.sh - the ONE inter-process lock helper of geomask (R04).
#
# Contract
#   * One persistent lock file per name. The file is opened, never deleted and never
#     recreated while a lock is in use: the same inode is locked every time, so a lock
#     released by one process is exactly the lock the next process takes.
#   * The lock itself is kernel flock(2) on that file: BusyBox `flock` (the applet is part of
#     the upstream OpenWrt 24.10 busybox config) and Python `fcntl.flock` on the same path
#     (geomask-config-tx.py) are the same primitive, so shell and Python exclude each other.
#   * No PID is stored, no owner is guessed, no stale lock is reclaimed. A holder that dies -
#     including SIGKILL - has its lock released by the kernel when the last descriptor of
#     that open file description is closed. There is therefore no ownerless window (nothing
#     is created before the lock is held) and no unlock-after-takeover (the file is never
#     removed, so a slow release cannot hand the lock to two writers).
#   * Fixed lock order, enforced at run time: a process may only take locks with a strictly
#     higher rank than every lock it already holds (gm_lock_rank). Downward nesting is
#     refused with rc=4 instead of deadlocking. The order is:
#       maintenance(10) -> console(20) -> cron(25) -> config-tx(30) -> route-<table>(40)
#       -> sync(45) -> devices(60) -> subs(65)
#   cron sits before config-tx because the uninstall (under maintenance) removes crontab lines
#   and then asks the lane helper to rewrite the mihomo config; the reverse nesting never
#   happens (the transaction and the lane manager never touch cron).
#   * Waiting is bounded and explicit: `gm_lock_acquire <name> [seconds] [path]`. 0 means a
#     single non-blocking attempt (hotplug, guard watchdog, single-instance entrypoints).
#     BusyBox flock has no -w, so a bounded wait is `flock -n` plus sleep 1 up to the budget.
#   * Critical sections stay short and contain no long-running external call. Commands that
#     may leave a daemon behind (init scripts, services) MUST be run through
#     gm_lock_safe_exec, which closes the reserved descriptors in the child: otherwise the
#     daemon inherits the descriptor and keeps the lock after the writer has exited.
#   * Fail-closed on the target: if `flock` cannot be found on Linux, every acquisition
#     returns rc=2 and the caller must refuse to mutate. There is NO mkdir/O_EXCL fallback on
#     Linux - that would bring back the reclaim race R04 is about.
#
# Non-Linux hosts (the Windows-hosted test suite) have neither flock(2) nor /proc. There the
# helper uses an atomic O_EXCL file (noclobber) created once and removed only by its owner.
# This backend exists so that caller logic can be exercised off-router; it is never selected
# on Linux, it does not release on SIGKILL (documented Linux-only test gap) and it is not a
# supported runtime.
#
# Environment (overridable for injected test roots only, never for skipping the lock):
#   GM_LOCK_DIR      directory of the lock files (default /tmp/geomask-locks, mode 0700)
#   GM_CONFIG_TX_LOCK  CANONICAL path of the config-transaction lock, shared with
#                      geomask-config-tx.py, geomask-node-scan.sh and geomask-torrent.sh
#   GM_LOCK_TX_FILE    legacy alias of GM_CONFIG_TX_LOCK (test roots only)
#   GM_LOCK_FDS      reserved descriptors, highest first (default "9 8 7 6 5")

GM_LOCK_DIR=${GM_LOCK_DIR:-/tmp/geomask-locks}
GM_LOCK_TX_FILE=${GM_CONFIG_TX_LOCK:-${GM_LOCK_TX_FILE:-/etc/geomask/node-audit/state/.config-tx.lock}}
GM_LOCK_FDS=${GM_LOCK_FDS:-9 8 7 6 5}
GM_LOCK_MAX_RANK=${GM_LOCK_MAX_RANK:-0}
GM_LOCK_STACK=${GM_LOCK_STACK:-}
GM_LOCK_FDS_USED=${GM_LOCK_FDS_USED:-}

# The rank table is the lock order. Unknown names get 90 so a new writer is the outermost -
# an earlier rank could otherwise turn a lawful sequence into a refused one silently.
gm_lock_rank() {
  case "$1" in
    maintenance) printf '10' ;;
    console)     printf '20' ;;
    config-tx)   printf '30' ;;
    route-*)     printf '40' ;;
    sync)        printf '45' ;;
    cron)        printf '25' ;;
    devices)     printf '60' ;;
    subs)        printf '65' ;;
    *)           printf '90' ;;
  esac
}

# Echo the file a lock name uses. An explicit path wins (callers keep their historical
# env-overridable path, e.g. GM_CONSOLE_LOCK, so an injected test root never touches /tmp).
gm_lock_path() { # gm_lock_path <name> [explicit-path]
  if [ -n "${2:-}" ]; then
    printf '%s' "$2"
  elif [ "$1" = config-tx ]; then
    printf '%s' "$GM_LOCK_TX_FILE"
  else
    printf '%s/%s.lock' "$GM_LOCK_DIR" "$1"
  fi
}

# flock | portable | missing. "missing" is a refusal on Linux (the caller must fail closed).
gm_lock_backend() {
  if command -v flock >/dev/null 2>&1; then
    printf 'flock'
    return 0
  fi
  case "$(uname -s 2>/dev/null)" in
    Linux) printf 'missing' ;;
    '')    printf 'missing' ;;
    *)     printf 'portable' ;;
  esac
}

_gm_lock_var() { # _gm_lock_var <prefix> <name> -> variable name, '-' and '/' become '_'
  printf '%s_%s' "$1" "$(printf '%s' "$2" | tr -c 'A-Za-z0-9' '_')"
}

_gm_lock_fd_of() { # echoes the descriptor held for <name>, empty when not held here
  _gm_v=$(_gm_lock_var _GM_LOCK_FD "$1")
  eval "printf '%s' \"\${$_gm_v:-}\""
}

_gm_lock_held() { # 0 when this shell holds <name>
  [ -n "$(_gm_lock_fd_of "$1")" ]
}

_gm_lock_track() { # _gm_lock_track <name> <fd> <path>
  eval "$(_gm_lock_var _GM_LOCK_FD "$1")='$2'"
  eval "$(_gm_lock_var _GM_LOCK_PATH "$1")='$3'"
  GM_LOCK_FDS_USED="$GM_LOCK_FDS_USED $2"
  GM_LOCK_STACK="$GM_LOCK_STACK $1"
  _gm_r=$(gm_lock_rank "$1")
  [ "$_gm_r" -gt "$GM_LOCK_MAX_RANK" ] && GM_LOCK_MAX_RANK=$_gm_r
  return 0
}

_gm_lock_forget() { # _gm_lock_forget <name> <fd>
  eval "$(_gm_lock_var _GM_LOCK_FD "$1")=''"
  eval "$(_gm_lock_var _GM_LOCK_PATH "$1")=''"
  _gm_used=''
  for _gm_u in $GM_LOCK_FDS_USED; do
    [ "$_gm_u" = "$2" ] || _gm_used="$_gm_used $_gm_u"
  done
  GM_LOCK_FDS_USED=$_gm_used
  _gm_new=''
  for _gm_s in $GM_LOCK_STACK; do
    [ "$_gm_s" = "$1" ] || _gm_new="$_gm_new $_gm_s"
  done
  GM_LOCK_STACK=$_gm_new
  GM_LOCK_MAX_RANK=0
  for _gm_s in $GM_LOCK_STACK; do
    _gm_r=$(gm_lock_rank "$_gm_s")
    [ "$_gm_r" -gt "$GM_LOCK_MAX_RANK" ] && GM_LOCK_MAX_RANK=$_gm_r
  done
  return 0
}

_gm_lock_free_fd() { # echoes the first reserved descriptor not used by this shell
  for _gm_c in $GM_LOCK_FDS; do
    case " $GM_LOCK_FDS_USED " in
      *" $_gm_c "*) continue ;;
    esac
    printf '%s' "$_gm_c"
    return 0
  done
  return 1
}

# Acquire <name>. rc: 0 held, 1 busy/timeout, 2 primitive unavailable (fail closed),
# 3 already held by this shell, 4 lock order violated. On 1/2/4 nothing is held.
gm_lock_acquire() { # gm_lock_acquire <name> [wait-seconds] [path]
  _gm_name=$1
  _gm_wait=${2:-0}
  # A non-numeric budget is a caller bug, not "wait forever": refuse it before the deadline
  # comparison, otherwise `[ N -ge abc ]` errors and the retry loop never terminates.
  case "$_gm_wait" in
    ''|*[!0-9]*)
      echo "geomask-lock: wait budget for '$_gm_name' must be a non-negative integer, got '$_gm_wait'" >&2
      return 2 ;;
  esac
  [ "$_gm_wait" -gt 3600 ] && _gm_wait=3600
  _gm_path=$(gm_lock_path "$_gm_name" "${3:-}")
  _gm_rank=$(gm_lock_rank "$_gm_name")

  if _gm_lock_held "$_gm_name"; then
    echo "geomask-lock: '$GM_LOCK_STACK' already holds '$_gm_name' (re-entrant acquire refused)" >&2
    return 3
  fi
  if [ "$GM_LOCK_MAX_RANK" -ge "$_gm_rank" ]; then
    echo "geomask-lock: lock order violation: '$_gm_name' (rank $_gm_rank) requested while holding '$GM_LOCK_STACK' (max rank $GM_LOCK_MAX_RANK)" >&2
    return 4
  fi

  _gm_backend=$(gm_lock_backend)
  case "$_gm_backend" in
    missing)
      echo "geomask-lock: the flock primitive is missing on Linux ($_gm_name): refusing to mutate without a lock" >&2
      return 2 ;;
  esac

  _gm_dir=$(dirname "$_gm_path")
  ( umask 077; mkdir -p "$_gm_dir" ) 2>/dev/null || {
    echo "geomask-lock: cannot create the lock directory $_gm_dir - refusing to lock $_gm_name" >&2
    return 2
  }
  _gm_fd=$(_gm_lock_free_fd) || {
    echo "geomask-lock: no free reserved descriptor for '$_gm_name' (GM_LOCK_FDS='$GM_LOCK_FDS')" >&2
    return 2
  }

  _gm_try=0
  while :; do
    if [ "$_gm_backend" = portable ]; then
      # noclobber create: the file appears exactly once, atomically. Never reclaimed.
      if ( set -C; : > "$_gm_path" ) 2>/dev/null; then
        _gm_lock_track "$_gm_name" "$_gm_fd" "$_gm_path"
        return 0
      fi
    else
      if eval "exec $_gm_fd>>\"\$_gm_path\"" 2>/dev/null; then
        if flock -n "$_gm_fd" 2>/dev/null; then
          _gm_lock_track "$_gm_name" "$_gm_fd" "$_gm_path"
          return 0
        fi
        eval "exec $_gm_fd>&-"
      else
        echo "geomask-lock: cannot open the lock file $_gm_path - refusing to lock $_gm_name" >&2
        return 2
      fi
    fi
    if [ "$_gm_try" -ge "$_gm_wait" ]; then
      return 1
    fi
    _gm_try=$((_gm_try+1))
    sleep 1
  done
}

gm_lock_release() { # gm_lock_release <name>
  _gm_name=$1
  _gm_fd=$(_gm_lock_fd_of "$_gm_name")
  [ -n "$_gm_fd" ] || return 0
  _gm_pv=$(_gm_lock_var _GM_LOCK_PATH "$_gm_name")
  eval "_gm_path=\${$_gm_pv:-}"
  if [ "$(gm_lock_backend)" = portable ]; then
    [ -f "$_gm_path" ] && rm -f "$_gm_path" 2>/dev/null
  else
    flock -u "$_gm_fd" 2>/dev/null
    eval "exec $_gm_fd>&-"
  fi
  _gm_lock_forget "$_gm_name" "$_gm_fd"
  return 0
}

gm_lock_release_all() {
  for _gm_name in $GM_LOCK_STACK; do
    gm_lock_release "$_gm_name"
  done
}

# Report whether another process holds <name>: 0 busy, 1 free, 2 primitive unavailable.
gm_lock_busy() { # gm_lock_busy <name> [path]
  _gm_name=$1
  if gm_lock_acquire "$_gm_name" 0 "${2:-}"; then
    gm_lock_release "$_gm_name"
    return 1
  else
    _gm_rc=$?
    [ "$_gm_rc" = 1 ] && return 0
    return 2
  fi
}

# Close the reserved descriptors in this process WITHOUT unlocking: flock lives on the open
# file description, so another process (the updater runner) still holds the lock.
gm_lock_close_inherited() {
  for _gm_c in $GM_LOCK_FDS; do
    eval "exec $_gm_c>&-" 2>/dev/null
  done
  GM_LOCK_FDS_USED=''
  GM_LOCK_STACK=''
  GM_LOCK_MAX_RANK=0
  return 0
}

# Run a command (init script, service) so it does not inherit the lock descriptors. A daemon
# spawned with them would keep the lock after the writer exits and block every later writer.
gm_lock_safe_exec() {
  ( gm_lock_close_inherited; "$@" )
}
