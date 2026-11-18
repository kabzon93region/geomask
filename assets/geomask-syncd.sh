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
#
# P11 (audit 18.11.2026): two holes in the same loop.
#   * No initial reconcile: an already-running router whose state was written while the
#     daemon was down had no event to wait for, so the rulesets stayed stale until the
#     next cron run or a manual sync.
#   * Blind window: the hash was compared before the run, so a write that landed while
#     the sync was reading was applied by nobody - the writer does not fire a second event.
#   * The fallback digest was `ls -l`, which cannot see a same-size rewrite in the same
#     minute.
# The watchdog now reconciles at startup, digests file CONTENT, and re-runs (bounded) when
# the state changed during the run.
DIR=/etc/magitrickle/state
SYNC=/root/sync_mtrickle_mihomo.sh
DEB=4
BACKOFF=15
BACKOFF_MAX=60
CATCHUP_MAX=3
log() { logger -t geomask-syncd "$1" 2>/dev/null; }
run() {
  [ -x "$SYNC" ] || return 0
  _o=$("$SYNC" 2>&1); _r=$?
  [ $_r -ne 0 ] && log "sync rc=$_r: $(printf '%s' "$_o" | tail -1)"
}
# Content digest of the watched directory: a same-size rewrite in the same minute is
# invisible to `ls -l`, and that is exactly the case the blind window missed.
digest() {
  find "$DIR" -maxdepth 1 -type f -print 2>/dev/null | LC_ALL=C sort \
    | while IFS= read -r _f; do md5sum "$_f" 2>/dev/null; done
}
# One sync, then catch up while the state keeps changing under it (bounded: a writer that
# never stops must not turn the watchdog into a spin, the cron remains the backstop).
run_catchup() {
  _pass=0
  while [ "$_pass" -lt "$CATCHUP_MAX" ]; do
    _pass=$((_pass + 1))
    _before=$(digest)
    run
    _after=$(digest)
    [ "$_before" = "$_after" ] && return 0
    log "state changed during the sync - catching up (pass $_pass)"
  done
  log "state is still changing after $CATCHUP_MAX passes - the */5 cron will catch up"
  return 0
}
log "watchdog started (dir=$DIR, inotify=$(command -v inotifywait >/dev/null 2>&1 && echo yes || echo no))"
# Initial reconcile: the router may already be up with state written while we were away.
if [ -d "$DIR" ]; then run_catchup; fi
_prev=$(digest)
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
    run_catchup
    _prev=$(digest)
  else
    sleep 10
    _c=$(digest)
    if [ -n "$_prev" ] && [ "$_c" != "$_prev" ]; then
      run_catchup
      _c=$(digest)
    fi
    _prev=$_c
  fi
done
