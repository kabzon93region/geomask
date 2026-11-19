#!/bin/sh
# geomask canary restore: put the saved archives back and remove files the archives do not
# contain. Runs ON THE ROUTER, called by the PC watchdog; rebooting is the caller's job.
#
# Usage: restore.sh <sysupgrade.tar.gz> <overlay.tar.gz> <restore-set.txt> <ref-file> [clean]
#   restore-set.txt - one absolute path per line, every file inside both archives
#   ref-file        - mtime marker created before the canary install started
#   clean           - also remove geomask files newer than ref-file that the archives lack
set -u
SYS=${1:-}
OVL=${2:-}
SET=${3:-}
REF=${4:-}
MODE=${5:-clean}

die() { printf 'RESTORE_FAIL %s\n' "$*" >&2; exit 1; }

[ -s "$SYS" ] || die "sysupgrade archive missing or empty"
[ -s "$OVL" ] || die "overlay archive missing or empty"
[ -s "$SET" ] || die "restore set missing or empty"
tar -tzf "$SYS" >/dev/null 2>&1 || die "sysupgrade archive is not a valid tar.gz"
tar -tzf "$OVL" >/dev/null 2>&1 || die "overlay archive is not a valid tar.gz"

if [ "$MODE" = clean ] && [ -f "$REF" ]; then
  for root in /etc/geomask /etc/mihomo /etc/init.d /etc/hotplug.d /usr/lib/geomask \
              /usr/bin /www/luci-static/resources; do
    [ -d "$root" ] || continue
    find "$root" -type f -newer "$REF" 2>/dev/null | while IFS= read -r p; do
      case "$p" in
        /etc/geomask/*|/etc/mihomo/*|/etc/init.d/geomask*|/etc/hotplug.d/*geomask*) ;;
        /usr/lib/geomask/*|/usr/bin/geomask*|/www/luci-static/resources/*geomask*) ;;
        *) continue ;;
      esac
      grep -qxF "$p" "$SET" && continue
      rm -f "$p" && printf 'REMOVED %s\n' "$p"
    done
  done
fi

tar -xzf "$SYS" -C / || die "extract of the sysupgrade archive failed"
tar -xzf "$OVL" -C / || die "extract of the overlay archive failed"
sync
printf 'RESTORE_OK\n'
exit 0
