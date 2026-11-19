#!/bin/sh
# geomask canary health probe: read-only, prints key=value lines for the PC watchdog.
# Installed to /tmp/canary/probe.sh by tools/canary/run-canary.ps1; never mutates anything.
export LC_ALL=C

ver=$(sed -n '1s/^[[:space:]]*//p' /etc/geomask/version 2>/dev/null | head -1)
[ -n "$ver" ] || ver=unknown
printf 'version=%s\n' "$ver"

printf 'uptime=%s\n' "$(cut -d. -f1 /proc/uptime 2>/dev/null)"

if pidof mihomo >/dev/null 2>&1; then printf 'mihomo=1\n'; else printf 'mihomo=0\n'; fi

tuns=$(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | cut -d@ -f1 \
  | grep -E '^(Mihomo|AI|Console)$' | paste -sd, - 2>/dev/null)
printf 'tun=%s\n' "${tuns:-none}"

rules=$(ip rule show 2>/dev/null | sed -n 's/.*lookup \(10[0-9]\).*/\1/p' \
  | sort -u | paste -sd, - 2>/dev/null)
printf 'rules=%s\n' "${rules:-none}"

nft=$(nft list tables 2>/dev/null | grep -c 'inet geomask')
printf 'nft=%s\n' "${nft:-0}"

dns=0
if command -v nslookup >/dev/null 2>&1; then
  nslookup example.com 127.0.0.1 >/dev/null 2>&1 && dns=1
elif command -v host >/dev/null 2>&1; then
  host example.com 127.0.0.1 >/dev/null 2>&1 && dns=1
fi
printf 'dns=%s\n' "$dns"

inet=0
if command -v wget >/dev/null 2>&1; then
  wget -q -T 6 -O /dev/null http://connectivitycheck.gstatic.com/generate_204 2>/dev/null && inet=1
elif command -v curl >/dev/null 2>&1; then
  curl -fsS -m 6 -o /dev/null http://connectivitycheck.gstatic.com/generate_204 2>/dev/null && inet=1
fi
printf 'inet=%s\n' "$inet"

[ -f /etc/geomask/backup-ok ] && printf 'backupok=1\n' || printf 'backupok=0\n'
[ -f /etc/geomask/modules.json ] && printf 'modules=1\n' || printf 'modules=0\n'
printf 'load=%s\n' "$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"
exit 0
