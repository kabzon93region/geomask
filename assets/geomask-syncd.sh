#!/bin/sh
# geomask-syncd.sh - event watchdog for magitrickle -> mihomo list sync (v1.10.0).
# Runs the idempotent sync (which reloads mihomo only on real change) a few seconds
# after magitrickle touches /etc/magitrickle/state, so the panel button and the */5
# cron become a backup, not the primary path.
# Prefers inotifywait (package inotifywait); falls back to a 10s mtime poll.
DIR=/etc/magitrickle/state
SYNC=/root/sync_mtrickle_mihomo.sh
DEB=4
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
    inotifywait -qq -e close_write,moved_to,create,delete "$DIR" 2>/dev/null
    sleep "$DEB"
    run
  else
    sleep 10
    _c=$(ls -l "$DIR" 2>/dev/null | md5sum)
    if [ -n "$_prev" ] && [ "$_c" != "$_prev" ]; then run; fi
    _prev=$_c
  fi
done
