#!/bin/sh
# geomask-syncd.sh - event watchdog for magitrickle -> mihomo list sync (v1.10.0).
# Runs the idempotent sync (which reloads mihomo only on real change) a few seconds
# after magitrickle touches /etc/magitrickle/state, so the panel button and the */5
# cron become a backup, not the primary path.
# Prefers inotifywait (package inotifywait); falls back to a 10s mtime poll.
#
# R10b (D18): the exit code of inotifywait used to be ignored. When the watch dies
# (directory replaced, inotify limit, permission) the loop still slept DEB=4 s, ran the
# sync and retried at once - a 4-second spin with no line in syslog. A failed watch now
# is logged and backs off exponentially up to a minute, and a successful watch resets it.
DIR=/etc/magitrickle/state
SYNC=/root/sync_mtrickle_mihomo.sh
DEB=4
BACKOFF=15
BACKOFF_MAX=60
log() { logger -t geomask-syncd "$1" 2>/dev/null; }
run() {
  [ -x "$SYNC" ] || return 0
  _o=$("$SYNC" 2>&1); _r=$?
  [ $_r -ne 0 ] && log "sync rc=$_r: $(printf '%s' "$_o" | tail -1)"
}
log "watchdog started (dir=$DIR, inotify=$(command -v inotifywait >/dev/null 2>&1 && echo yes || echo no))"
while true; do
  if ! [ -d "$DIR" ]; then sleep 15; continue; fi
  if command -v inotifywait >/dev/null 2>&1; then
    _iw_out=$(inotifywait -qq -e close_write,moved_to,create,delete "$DIR" 2>&1)
    _iw_rc=$?
    if [ "$_iw_rc" -ne 0 ]; then
      log "inotifywait rc=$_iw_rc on $DIR ($(printf '%s' "$_iw_out" | tail -1)); backing off ${BACKOFF}s"
      sleep "$BACKOFF"
      BACKOFF=$((BACKOFF * 2))
      [ "$BACKOFF" -le "$BACKOFF_MAX" ] || BACKOFF=$BACKOFF_MAX
      continue
    fi
    BACKOFF=15
    sleep "$DEB"
    run
  else
    sleep 10
    _c=$(ls -l "$DIR" 2>/dev/null | md5sum)
    if [ -n "$_prev" ] && [ "$_c" != "$_prev" ]; then run; fi
    _prev=$_c
  fi
done
