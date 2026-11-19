#!/bin/sh
# geomask canary health probe: read-only, prints key=value lines for the PC watchdog.
# Installed to /tmp/canary/probe.sh by tools/canary/run-canary.ps1; never mutates anything.
export LC_ALL=C

ver=$(sed -n '1s/^[[:space:]]*//p' /etc/geomask/version 2>/dev/null | head -1)
[ -n "$ver" ] || ver=unknown
printf 'version=%s\n' "$ver"

printf 'uptime=%s\n' "$(cut -d. -f1 /proc/uptime 2>/dev/null)"

if pidof mihomo >/dev/null 2>&1; then printf 'mihomo=1\n'; else printf 'mihomo=0\n'; fi

# No paste(1) here: it is missing on some OpenWrt builds and silently produced "none".
tuns=$(ls /sys/class/net 2>/dev/null | grep -xE 'Mihomo|AI|Console|VPN-noP2P' | sort | tr '\n' ',')
tuns=${tuns%,}
printf 'tun=%s\n' "${tuns:-none}"

rules=$(ip rule show 2>/dev/null | grep -o 'lookup 10[0-9]' | awk '{print $2}' \
  | sort -u | tr '\n' ',')
rules=${rules%,}
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
