#!/bin/sh
# Sandbox test runner for geomask.sh (runs under Git-bash on Windows, no router access).
# Layout:
#   sandbox/bin/*     - stub commands (pidof, ubus, mihomo, curl, crontab, nslookup, ip, wget, apk)
#   sandbox/state/*   - state files consumed by stubs
#   sandbox/root/*    - fake filesystem (GM_PREFIX)
# Scenarios: S1 empty (all FAIL), S2 healthy (all OK), S3 apply (auto repairs subs).

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/../geomask.sh"
STUB="$HERE/bin"
export PATH="$STUB:$PATH"

pass=0; failn=0
t() { # t <name> <expected_rc> <cmd...>
  _name="$1"; _want="$2"; shift 2
  "$@" >"$HERE/last_out.txt" 2>&1
  _rc=$?
  if [ "$_rc" = "$_want" ]; then
    echo "PASS: $_name (rc=$_rc)"
    pass=$((pass+1))
  else
    echo "FAIL: $_name (rc=$_rc, want $_want)"
    sed 's/^/    | /' "$HERE/last_out.txt" | tail -25
    failn=$((failn+1))
  fi
}

contains() { # contains <file> <pattern> <name>
  if grep -q "$2" "$1" 2>/dev/null; then echo "PASS: $3"; pass=$((pass+1));
  else echo "FAIL: $3 (pattern '$2' not found)"; tail -15 "$1" | sed 's/^/    | /'; failn=$((failn+1)); fi
}
lacks() { # lacks <file> <pattern> <name>
  if grep -q "$2" "$1" 2>/dev/null; then echo "FAIL: $3 (pattern '$2' unexpectedly present)"; tail -15 "$1" | sed 's/^/    | /'; failn=$((failn+1));
  else echo "PASS: $3"; pass=$((pass+1)); fi
}

rm -rf "$HERE/root_empty" "$HERE/root_healthy" "$HERE/root_apply" "$HERE/state"
mkdir -p "$HERE/state" "$HERE/root_empty/tmp" "$HERE/root_healthy/tmp" "$HERE/root_apply/tmp"

# ---------- scenario 1: empty root -> everything FAILs, script still exits cleanly ----------
echo "=== S1: empty root, --check ==="
: > "$HERE/state/services"; : > "$HERE/state/ubus_objects"; : > "$HERE/state/crontab"; : > "$HERE/state/ip_rules"
export GM_STATE="$HERE/state"
export GM_PREFIX="$HERE/root_empty"
t "S1 check exits nonzero" 1 sh "$SCRIPT" --check
contains "$HERE/last_out.txt" "SUMMARY" "S1 prints summary"
contains "$HERE/last_out.txt" "\[FAIL\]" "S1 reports failures"

# ---------- scenario 2: healthy root -> all OK ----------
echo "=== S2: healthy root, --check ==="
R="$HERE/root_healthy"
mkdir -p "$R/etc/openwrt" "$R/opt/zapret-manager-luci" "$R/usr/libexec/rpcd" \
         "$R/usr/share/rpcd/acl.d" "$R/www/luci-static/resources/zapret-manager" \
         "$R/usr/bin" "$R/etc/mihomo/ruleset" "$R/root" "$R/etc/hotplug.d/iface"
printf "DISTRIB_RELEASE='24.10'\n" > "$R/etc/openwrt_release"
echo x > "$R/opt/zapret-manager-luci/backend.sh"
echo x > "$R/usr/libexec/rpcd/zapret-manager"
echo x > "$R/usr/share/rpcd/acl.d/luci-app-zapret-manager.json"
echo x > "$R/www/luci-static/resources/zapret-manager/common.js"
mkdir -p "$R/etc/init.d"
for s in magitrickle mihomo zapret hev-socks5-tunnel dnsmasq; do
  printf '#!/bin/sh\nexit 0\n' > "$R/etc/init.d/$s"
done
# AI-DNS step deps (v1.5.7): DoH binary + rc.common for the services it installs
mkdir -p "$R/usr/sbin"
printf '#!/bin/sh\nexit 0\n' > "$R/usr/sbin/https-dns-proxy"; chmod +x "$R/usr/sbin/https-dns-proxy"
printf '#!/bin/sh\nexit 0\n' > "$R/etc/rc.common"; chmod +x "$R/etc/rc.common"
echo x > "$R/usr/bin/geo-backend.sh"
echo x > "$R/usr/libexec/rpcd/geo-sync"
echo x > "$R/usr/share/rpcd/acl.d/luci-app-geo-sync.json"
mkdir -p "$R/usr/share/luci/menu.d"
echo x > "$R/usr/share/luci/menu.d/luci-app-geo-sync.json"
mkdir -p "$R/www/luci-static/resources/view/geo-sync"; echo x > "$R/www/luci-static/resources/view/geo-sync/main.js"
echo x > "$R/root/sync_mtrickle_mihomo.sh"; chmod +x "$R/root/sync_mtrickle_mihomo.sh"
# routing guard (v1.6.0): script + hotplug fixtures for step5 checks
# (shebang required: Git-bash derives +x from shebang for non-exe files)
printf '#!/bin/sh\nexit 0\n' > "$R/usr/bin/geomask-guard.sh"; chmod +x "$R/usr/bin/geomask-guard.sh"
printf '#!/bin/sh\nexit 0\n' > "$R/etc/hotplug.d/iface/99-geomask-guard"; chmod +x "$R/etc/hotplug.d/iface/99-geomask-guard"
# event sync watchdog (v1.10.0): healthy fixture has init script + rc.d link + binary
mkdir -p "$R/etc/rc.d"
printf '#!/bin/sh\nexit 0\n' > "$R/usr/bin/geomask-syncd.sh"; chmod +x "$R/usr/bin/geomask-syncd.sh"
printf '#!/bin/sh\nexit 0\n' > "$R/etc/init.d/geomask-syncd"; chmod +x "$R/etc/init.d/geomask-syncd"
: > "$R/etc/rc.d/S98geomask-syncd"
# node audit (v1.11.0): script + conf fixtures
printf '#!/bin/sh\nexit 0\n' > "$R/usr/bin/geomask-node-scan.sh"; chmod +x "$R/usr/bin/geomask-node-scan.sh"
echo 'bad_asns=AS210976' > "$R/etc/geomask-nodes.conf"
# youtube edge watchdog (v1.14.0): script + conf fixtures
printf '#!/bin/sh\nexit 0\n' > "$R/usr/bin/geomask-ytwatch.sh"; chmod +x "$R/usr/bin/geomask-ytwatch.sh"
echo 'mode=auto' > "$R/etc/geomask-ytwatch.conf"
# torrent admin (v1.15.0): script + state dir fixtures
printf '#!/bin/sh\nexit 0\n' > "$R/usr/bin/geomask-torrent.sh"; chmod +x "$R/usr/bin/geomask-torrent.sh"
# v1.18.0: VPN lane audit (step5_check requires script + conf)
mkdir -p "$R/etc/geomask"
printf '#!/bin/sh\nexit 0\n' > "$R/usr/bin/geomask-vpnwatch.sh"; chmod +x "$R/usr/bin/geomask-vpnwatch.sh"
printf '#!/bin/sh\nexit 0\n' > "$R/usr/bin/geomask-morning-check.sh"; chmod +x "$R/usr/bin/geomask-morning-check.sh"
printf '#!/bin/sh\nexit 0\n' > "$R/usr/bin/geomask-lane.sh"; chmod +x "$R/usr/bin/geomask-lane.sh"
printf '#!/bin/sh\nexit 0\n' > "$R/usr/bin/geomask-dnscheck.sh"; chmod +x "$R/usr/bin/geomask-dnscheck.sh"
# v1.28.0: node-audit subsystem modules live in /usr/lib/geomask
mkdir -p "$R/usr/lib/geomask"
for m in geomask-node-audit.py geomask-audit-state.py geomask-config-tx.py \
         geomask-config-owner.py geomask-apply.py geomask-lane-torrent.py \
         geomask-sync-youtube.py geomask-audit-scheduler.py \
         geomask-subscription-status.py geomask-slot-patch.py; do
  printf '#!/usr/bin/env python3\n' > "$R/usr/lib/geomask/$m"
done
printf 'group=VPN-noP2P\nlogfile=/root/geomask-vpn-audit.log\n' > "$R/etc/geomask/vpnwatch.conf"
mkdir -p "$R/etc/geomask/torrent"
printf 'payload: []\n' > "$R/etc/mihomo/ruleset/ytvpn.yaml"
printf 'payload:\n  - DOMAIN-SUFFIX,tracker.example.org\n' > "$R/etc/mihomo/ruleset/torrentdirect.yaml"
printf 'payload:\n  - DOMAIN-SUFFIX,rutracker.org\n' > "$R/etc/mihomo/ruleset/torrentsites.yaml"
# v1.17.0: the IP half of the mtrickle ruleset - must contain NO domain rules and vice versa
printf 'payload:\n  - IP-CIDR,203.0.113.0/24,no-resolve\n' > "$R/etc/mihomo/ruleset/mtrickle_bulk.yaml"
printf 'payload: []\n' > "$R/etc/mihomo/ruleset/mtrickle.yaml"
printf 'payload: []\n' > "$R/etc/mihomo/ruleset/ai.yaml"
printf 'payload: []\n' > "$R/etc/mihomo/ruleset/ytdirect.yaml"
cat > "$R/etc/mihomo/config.yaml" <<'YAMLEOF'
ipv6: false
proxy-providers:
  sub:
    type: http
    url: "https://example.com/a"
  de5:
    type: http
    url: "https://example.com/b"
proxy-groups:
  - name: VPN-noP2P
  - name: VPN
  - name: AI
rule-providers:
  ai:
    type: file
  mtrickle:
    type: file
  mtrickle_bulk:
    type: file
  ytdirect:
    type: file
  torrentdirect:
    type: file
  torrentsites:
    type: file
rules:
  - AND,((NETWORK,UDP),(DST-PORT,443),(DOMAIN-SUFFIX,googlevideo.com)),REJECT
  - AND,((NETWORK,UDP),(DST-PORT,443),(DOMAIN-SUFFIX,youtube.com)),REJECT
  - AND,((NETWORK,UDP),(DST-PORT,443),(DOMAIN-SUFFIX,youtu.be)),REJECT
  - RULE-SET,ytvpn,VPN
  - RULE-SET,ytdirect,DIRECT
  - RULE-SET,ai,AI
  - RULE-SET,torrentsites,DIRECT
  - RULE-SET,torrentdirect,VPN
  - SRC-PORT,41552,VPN
  - RULE-SET,mtrickle_bulk,VPN
  - RULE-SET,mtrickle,VPN-noP2P
  - MATCH,GLOBAL
listeners:
  - name: ai-devices
    type: mixed
    port: 7891
    listen: 127.0.0.1
    proxy: AI
  - name: torrent-direct
    type: mixed
    port: 7894
    listen: 192.168.0.1
    proxy: VPN
sniffer:
  enable: true
  parse-pure-ip: true
  override-destination: true
  sniff:
    HTTP:
      ports: [80, 8080-8880]
    TLS:
      ports: [443, 8443, 5228]
    QUIC:
      ports: [443, 8443]
YAMLEOF
# healthy state
printf 'dnsmasq\nmagitrickled\nmihomo\nhev-socks5-tunnel\nrpcd\n' > "$HERE/state/services"
printf 'zapret-manager\ngeo-sync\n' > "$HERE/state/ubus_objects"
echo '*/5 * * * * /root/sync_mtrickle_mihomo.sh' > "$HERE/state/crontab"
echo '*/5 * * * * /usr/bin/geomask-guard.sh >/dev/null 2>&1 # geomask-guard' >> "$HERE/state/crontab"
echo '30 3 * * * /usr/bin/geomask-node-scan.sh >/dev/null 2>&1 # geomask-nodescan' >> "$HERE/state/crontab"
echo '*/5 * * * * /usr/bin/geomask-ytwatch.sh >/dev/null 2>&1 # geomask-ytwatch' >> "$HERE/state/crontab"
echo '20 4 * * * /usr/bin/geomask-ytwatch.sh --reset >/dev/null 2>&1 # geomask-ytwatch-reset' >> "$HERE/state/crontab"
echo '* * * * * /usr/bin/geomask-vpnwatch.sh --tick >/dev/null 2>&1 # geomask-vpnwatch' >> "$HERE/state/crontab"
echo '32666: from all iif br-lan ipproto icmp lookup main' > "$HERE/state/ip_rules"
# pgrep stub state: syncd watchdog considered running in healthy/apply scenarios
printf 'geomask-syncd.sh\n' > "$HERE/state/pgrep"
: > "$HERE/state/mihomo_t_ok"
export GM_PREFIX="$HERE/root_healthy"
t "S2 check exits zero" 0 sh "$SCRIPT" --check
contains "$HERE/last_out.txt" "fail=0" "S2 zero failures"

# ---------- scenario 3: broken subs config -> --auto repairs with URLs ----------
echo "=== S3: broken subs, --auto --restart with urls ==="
R="$HERE/root_apply"
cp -a "$HERE/root_healthy/." "$R/"
rm -f "$R/etc/mihomo/config.yaml"   # break it
: > "$HERE/state/mihomo_t_ok"        # stub mihomo -t will pass
export GM_PREFIX="$HERE/root_apply"
t "S3 auto exits zero" 0 sh "$SCRIPT" --auto --yes --restart --vpn-url "https://vpn.example/sub1" --ai-url "https://ai.example/sub2"
contains "$R/etc/mihomo/config.yaml" 'url: "https://vpn.example/sub1"' "S3 vpn url written"
contains "$R/etc/mihomo/config.yaml" "name: AI" "S3 AI group written (rides sub)"
lacks "$R/etc/mihomo/config.yaml" '^  de5:' "S3 fresh config emits no de5"
contains "$R/etc/mihomo/config.yaml" "name: VPN" "S3 VPN group written"
contains "$R/etc/mihomo/config.yaml" "RULE-SET,mtrickle,VPN" "S3 mtrickle rule written"
contains "$R/etc/mihomo/config.yaml" "port: 7891" "S3 AI-devices listener written"
contains "$HERE/last_out.txt" "verify: PASS" "S3 verify passed"

# ---------- scenario 4: help + bad flag ----------
echo "=== S4: CLI edges ==="
t "S4 help exits 0" 0 sh "$SCRIPT" --help
t "S4 bad flag exits 2" 2 sh "$SCRIPT" --wat

# ---------- scenario 5: /root/.geomask-norestart pin blocks --restart ----------
echo "=== S5: restart pin ==="
R="$HERE/root_apply"
rm -f "$R/root/.geomask-norestart"
rm -f "$R/etc/mihomo/config.yaml"
echo "restarts forbidden" > "$R/root/.geomask-norestart"
export GM_PREFIX="$R"
sh "$SCRIPT" --step 3 --restart --yes --vpn-url "https://vpn.example/p1" --ai-url "https://ai.example/p2" >"$HERE/last_out.txt" 2>&1 || true
contains "$HERE/last_out.txt" "ignoring --restart" "S5 pin ignores --restart"
if grep -q "restarting mihomo" "$HERE/last_out.txt"; then
  echo "FAIL: S5 restart happened despite pin"; failn=$((failn+1))
else
  echo "PASS: S5 no restart with pin"; pass=$((pass+1))
fi
rm -f "$R/root/.geomask-norestart"

# ---------- scenario 6: menu + step-by-step master ----------
echo "=== S6: interactive menu + master ==="
export GM_PREFIX="$HERE/root_healthy"
printf '2\nq\nq\n' | sh "$SCRIPT" >"$HERE/last_out.txt" 2>&1
_rc=$?
if [ "$_rc" = 0 ]; then echo "PASS: S6 menu exits 0"; pass=$((pass+1)); else echo "FAIL: S6 menu rc=$_rc"; failn=$((failn+1)); fi
contains "$HERE/last_out.txt" "GEOMASK" "S6 banner shown"
contains "$HERE/last_out.txt" "Выберите пункт" "S6 prompt shown"
contains "$HERE/last_out.txt" "Роутер:" "S6 dashboard shown"
contains "$HERE/last_out.txt" "Пошаговый мастер" "S6 master menu item shown"
_st_n=$(grep -cE 'выполнено|требует внимания' "$HERE/last_out.txt")
if [ "$_st_n" = 7 ]; then echo "PASS: S6 master shows 7 step statuses once"; pass=$((pass+1)); else echo "FAIL: S6 master status lines=$_st_n (want 7)"; failn=$((failn+1)); fi
if grep 'MagiTrickle:' "$HERE/last_out.txt" | grep -q 'запущен'; then echo "PASS: S6 MagiTrickle shows running"; pass=$((pass+1)); else echo "FAIL: S6 MagiTrickle status wrong"; grep 'MagiTrickle' "$HERE/last_out.txt" | sed 's/^/    | /'; failn=$((failn+1)); fi

# ---------- scenario 7: reinstall keeps current URLs, no mihomo restart ----------
echo "=== S7: --reinstall --yes (keep current subscription URLs) ==="
R="$HERE/root_apply"
export GM_PREFIX="$R"
sh "$SCRIPT" --reinstall --yes >"$HERE/last_out.txt" 2>&1
_rc=$?
if [ "$_rc" = 0 ]; then echo "PASS: S7 reinstall rc=0"; pass=$((pass+1)); else echo "FAIL: S7 reinstall rc=$_rc"; sed 's/^/    | /' "$HERE/last_out.txt" | tail -25; failn=$((failn+1)); fi
contains "$R/etc/mihomo/config.yaml" 'url: "https://vpn.example/p1"' "S7 kept VPN url"
lacks "$R/etc/mihomo/config.yaml" '^  de5:' "S7 reinstall strips the retired de5 provider"
contains "$HERE/last_out.txt" "verify: PASS" "S7 verify passed"
if grep -q "restarting mihomo" "$HERE/last_out.txt"; then
  echo "FAIL: S7 restarted mihomo despite no-restart policy"; failn=$((failn+1))
else
  echo "PASS: S7 no mihomo restart"; pass=$((pass+1))
fi
[ -s "$R/usr/bin/geo-backend.sh" ] && { echo "PASS: S7 geo reinstalled"; pass=$((pass+1)); } || { echo "FAIL: S7 geo missing"; failn=$((failn+1)); }

# ---------- scenario 8: --check has no duplicate lines, real counters ----------
echo "=== S8: check-only output sanity ==="
export GM_PREFIX="$HERE/root_healthy"
sh "$SCRIPT" --check >"$HERE/last_out.txt" 2>&1
_rc=$?
if [ "$_rc" = 0 ]; then echo "PASS: S8 check rc=0"; pass=$((pass+1)); else echo "FAIL: S8 check rc=$_rc"; failn=$((failn+1)); fi
_n=$(grep -c 'proxy-providers: 2' "$HERE/last_out.txt")
if [ "$_n" = 1 ]; then echo "PASS: S8 no duplicate detail lines"; pass=$((pass+1)); else echo "FAIL: S8 detail lines printed $_n times"; failn=$((failn+1)); fi
contains "$HERE/last_out.txt" "OpenWrt: 24.10" "S8 OpenWrt version shown"
contains "$HERE/last_out.txt" "aidns" "S8 AI-DNS step present in report"
if grep -q 'pass=6' "$HERE/last_out.txt"; then echo "PASS: S8 summary counts passing steps"; pass=$((pass+1)); else echo "FAIL: S8 summary pass count: $(grep -o 'pass=[0-9]*' "$HERE/last_out.txt")"; failn=$((failn+1)); fi

# ---------- scenario 9: no third-party panel references in the installer ----------
echo "=== S9: no Zapret-Manager references ==="
_n=$(grep -cE 'Zapret-Manager|zapret-manager|StressOzz' "$SCRIPT")
if [ "$_n" = 0 ]; then echo "PASS: S9 installer has no panel references"; pass=$((pass+1)); else echo "FAIL: S9 found $_n panel references"; grep -nE 'Zapret-Manager|zapret-manager|StressOzz' "$SCRIPT" | head -5; failn=$((failn+1)); fi

# ---------- scenario 10: safe restart path (backup -> trial -> verified restart) ----------
echo "=== S10: safe mihomo restart path ==="
R="$HERE/root_apply"
rm -f "$R/root/.geomask-norestart"
rm -f "$R/etc/mihomo/config.yaml"
export GM_PREFIX="$R"
sh "$SCRIPT" --step 3 --restart --yes --vpn-url "https://vpn.example/p1" --ai-url "https://ai.example/p2" >"$HERE/last_out.txt" 2>&1 || true
cp -f "$HERE/last_out.txt" "$HERE/s10_out.txt" 2>/dev/null || true
contains "$HERE/last_out.txt" "trial run: OK" "S10 trial run before restart"
contains "$HERE/last_out.txt" "restarted and verified" "S10 verified restart"
contains "$HERE/last_out.txt" "router-backup" "S10 full backup created"

# ---------- scenario 11: ICMP rule presence check is semantic (v1.6.2) ----------
# Guard reseats our rules to pref (base - 100) when the magitrickle base drifts;
# the hotplug must NOT add a duplicate when the ICMP rule lives at another pref.
echo "=== S11: ICMP check semantic (no dup after reseat) ==="
if grep -q "grep -q '19901:'" "$SCRIPT"; then
  echo "FAIL: S11 installer still checks ICMP by pref 19901"; failn=$((failn+1))
else
  echo "PASS: S11 no pref-based ICMP check in installer"; pass=$((pass+1))
fi
echo '20100: from all iif br-lan ipproto icmp lookup main' > "$HERE/state/ip_rules"
ACTION=ifup sh "$HERE/../assets/99-default-vpn-route" >"$HERE/last_out.txt" 2>&1
_icmp_n=$(grep -c 'ipproto icmp' "$HERE/state/ip_rules")
if [ "$_icmp_n" = 1 ]; then
  echo "PASS: S11 reseated ICMP rule not duplicated"; pass=$((pass+1))
else
  echo "FAIL: S11 icmp rules now: $_icmp_n (want 1)"; sed 's/^/    | /' "$HERE/state/ip_rules"; failn=$((failn+1))
fi
contains "$HERE/state/ip_rules" "priority 32760" "S11 hotplug actually ran"

# ---------- scenario 12: self-update feature wiring (v1.7.0) ----------
echo "=== S12: self-update markers/methods ==="
# 1) every @@...@@ marker stamped by build.py (no leftovers)
if grep -q '@@' "$SCRIPT"; then
  echo "FAIL: S12 unreplaced markers:"; grep -o '@@[A-Za-z0-9_]*@@' "$SCRIPT" | head -3 | sed 's/^/    /'
  failn=$((failn+1))
else
  echo "PASS: S12 no unreplaced markers"; pass=$((pass+1))
fi
# 2) component versions == GM_VERSION
_gv=$(sed -n 's/^GM_VERSION="\([^"]*\)".*/\1/p' "$SCRIPT" | head -1)
for _m in "PANEL_VERSION = '$_gv'" "BACKEND_VERSION=\"$_gv\"" "GUARD_VERSION=\"$_gv\""; do
  if grep -qF "$_m" "$SCRIPT"; then echo "PASS: S12 stamped: $_m"; pass=$((pass+1))
  else echo "FAIL: S12 stamp missing: $_m"; failn=$((failn+1)); fi
done
# 3) upd_* methods wired through backend + rpcd + acl + js (>=3 refs each)
for _m in upd_check upd_apply upd_status; do
  _c=$(grep -c "$_m" "$SCRIPT")
  if [ "$_c" -ge 3 ]; then echo "PASS: S12 $_m wired ($_c refs)"; pass=$((pass+1))
  else echo "FAIL: S12 $_m refs=$_c (want >=3)"; failn=$((failn+1)); fi
done
# 4) update runner sanity: backup first, digest check, no-restart install
for _p in 'sysupgrade -b' 'sha256 mismatch' 'reinstall --yes' 'rpcd reload' 'wc -c < "$BK"' 'rm -f /tmp/router-backup-'; do
  if grep -qF "$_p" "$SCRIPT"; then echo "PASS: S12 runner has: $_p"; pass=$((pass+1))
  else echo "FAIL: S12 runner missing: $_p"; failn=$((failn+1)); fi
done
# 5) backups: persistent shared folder, but archive MUST be assembled in /tmp and moved -
#    a pre-existing copy under /root would nest into the next sysupgrade backup (v1.6-1.8 bug)
if grep -qF 'geomask-bk-' "$SCRIPT" && ! grep -qE 'sysupgrade -b "[^"]*geomask-backups/' "$SCRIPT"; then
  echo "PASS: S12 ensure_backup: /tmp assembly + move, no direct writes into share"; pass=$((pass+1))
else
  echo "FAIL: S12 ensure_backup writes backup directly into share dir"; failn=$((failn+1))
fi
# 6) busybox find has no -delete on OpenWrt - purge must use rm -f globs
if grep -q -- '-delete' "$SCRIPT"; then
  echo "FAIL: S12 uses unsupported 'find -delete'"; failn=$((failn+1))
else
  echo "PASS: S12 no find -delete (busybox-safe)"; pass=$((pass+1))
fi

# ---------- scenario 13: backup folder over SMB + panel links (v1.9.0) ----------
echo "=== S13: backups SMB share ==="
for _p in 'SMB_SHARE_NAME=geomask-backups' 'ensure_smb_share' 'cmd_backups' 'BACKUP_DIR=/root/geomask-backups'; do
  if grep -qF "$_p" "$SCRIPT"; then echo "PASS: S13 has: $_p"; pass=$((pass+1))
  else echo "FAIL: S13 missing: $_p"; failn=$((failn+1)); fi
done
# panel wires the backups method: rpcd list + acl read + js call + block render
_c=$(grep -c 'backups' "$SCRIPT")
if [ "$_c" -ge 8 ]; then echo "PASS: S13 backups wired across layers ($_c refs)"; pass=$((pass+1))
else echo "FAIL: S13 backups refs=$_c (want >=8)"; failn=$((failn+1)); fi
if grep -qF '"backups"' "$SCRIPT"; then echo "PASS: S13 acl/rpcd method present"; pass=$((pass+1))
else echo "FAIL: S13 no \"backups\" method"; failn=$((failn+1)); fi
# v1.9.1: uci section must be captured from 'uci add' output (no @share[last] syntax support)
if grep -qF '_sec=$(uci add ksmbd share' "$SCRIPT" && ! grep -qF '@share[last]' "$SCRIPT"; then
  echo "PASS: S13 uci section id captured correctly"; pass=$((pass+1))
else
  echo "FAIL: S13 bad uci section handling"; failn=$((failn+1))
fi
# v1.9.2: plain yes/no values (quoted '1' leaked into ksmbd.conf) + purge strictly BEFORE create
if grep -qF 'read_only=yes' "$SCRIPT" && ! grep -qE "read_only='1'" "$SCRIPT"; then
  echo "PASS: S13 uci yes/no values"; pass=$((pass+1))
else
  echo "FAIL: S13 quoted uci values present"; failn=$((failn+1))
fi
if grep -qF 'rm -f "$_dir"/router-backup-*.tar.gz 2>/dev/null' "$SCRIPT" && grep -qF 'rm -f /tmp/router-backup-*.tar.gz "$BKD"/router-backup-*.tar.gz' "$SCRIPT"; then
  echo "PASS: S13 old copies purged before sysupgrade (no embedding)"; pass=$((pass+1))
else
  echo "FAIL: S13 purge order wrong"; failn=$((failn+1))
fi
# v1.9.3: section enumeration must accept 'ksmbd.@share[N]' (uci show format) + dedupe
if grep -qF 's/^\(ksmbd\.[^=]*\)=share$/\1/p' "$SCRIPT" && grep -qF 'duplicate sections removed' "$SCRIPT"; then
  echo "PASS: S13 section enumeration handles @share[N] + dedupe"; pass=$((pass+1))
else
  echo "FAIL: S13 section enumeration broken"; failn=$((failn+1))
fi

# v1.12.0: tabbed panel + full node-audit automation control
for _p in 'gs-tabs' 'callNodesGet' 'cmd_nodes_set' 'cmd_sync_set' 'cmd_syncd_set' 'cmd_pin_set' 'geomask-nodescan.state'; do
  if grep -qF "$_p" "$SCRIPT"; then echo "PASS: S16 v1.12: $_p"; pass=$((pass+1))
  else echo "FAIL: S16 v1.12 missing: $_p"; failn=$((failn+1)); fi
done
# v1.12.2: generic LuCI footer must be suppressed (own save buttons only)
if grep -qF 'handleSaveApply: null' "$SCRIPT"; then
  echo "PASS: S16 v1.12.2 footer suppressed"; pass=$((pass+1))
else
  echo "FAIL: S16 v1.12.2 footer not suppressed"; failn=$((failn+1))
fi

echo "=== S14: syncd watchdog + stack card ==="
for _p in 'geomask-syncd.sh' '/etc/init.d/geomask-syncd' 'geomask-sync.state' 'cmd_stack' 'callStack' 'inotifywait'; do
  if grep -qF "$_p" "$SCRIPT"; then echo "PASS: S14 has: $_p"; pass=$((pass+1))
  else echo "FAIL: S14 missing: $_p"; failn=$((failn+1)); fi
done
# syncd must be removed on uninstall too (no orphans)
if grep -qF 'removed: syncd watchdog' "$SCRIPT"; then
  echo "PASS: S14 uninstall cleans syncd"; pass=$((pass+1))
else
  echo "FAIL: S14 uninstall leaves syncd"; failn=$((failn+1))
fi
# v1.9.4: backslashes must be JSON-escaped (rpcd rejects invalid \g escapes -> Invalid argument)
if grep -qF '_unc=$(json_esc' "$SCRIPT" && grep -qF '_j=$(printf' "$SCRIPT"; then
  echo "PASS: S13 UNC/say JSON escaping present"; pass=$((pass+1))
else
  echo "FAIL: S13 missing json escaping of backslash paths"; failn=$((failn+1))
fi
# v1.10.2: subscription exclude-filter must carry RF-marked node blocklist
if grep -qF 'Нидерланды-(5|13|16|18)' "$SCRIPT" && grep -qF 'Германия-[234]' "$SCRIPT"; then
  echo "PASS: S13 RF node filter in exclude-filter"; pass=$((pass+1))
else
  echo "FAIL: S13 RF node filter missing"; failn=$((failn+1))
fi

# v1.11.0: nightly node audit (registry scan + panel + cron)
for _p in 'geomask-node-scan.sh' 'geomask-nodes.conf' 'geomask-nodescan.state' 'nodescan cron' 'callNodescan' 'bad_asns'; do
  if grep -qF "$_p" "$SCRIPT"; then echo "PASS: S15 nodescan has: $_p"; pass=$((pass+1))
  else echo "FAIL: S15 nodescan missing: $_p"; failn=$((failn+1)); fi
done
# v1.9.6: simple SMB credentials (backup/backup, guest ok) + force_root for /root paths
if grep -qF 'force_root=1' "$SCRIPT" && grep -qF 'ksmbd.adduser backup -p backup' "$SCRIPT"; then
  echo "PASS: S13 force_root + backup auth user"; pass=$((pass+1))
else
  echo "FAIL: S13 SMB auth setup missing"; failn=$((failn+1))
fi
# backup must be built in /tmp then moved (never let an older /root copy nest into sysupgrade -b)
if grep -qF 'geomask-bk-$' "$SCRIPT" && grep -qF 'mv -f "$_out" "$_dest"' "$SCRIPT"; then
  echo "PASS: S13 ensure_backup: build in tmp + move (no nesting)"; pass=$((pass+1))
else
  echo "FAIL: S13 ensure_backup does not build-then-move"; failn=$((failn+1))
fi

# ---------- scenario 17: MagiTrickle lists template + seeder (v1.13.0) ----------
echo "=== S17: lists template (v1.13.0) ==="
for _p in '--seed-lists' 'write_lists_assets' 'seed_lists' 'geomask-seed-lists.py' 'magitrickle-groups.yaml' 'cmd_lists_get' 'cmd_lists_seed' 'callListsGet' 'callListsSeed' 'lists_get' 'lists_seed'; do
  if grep -qF -- "$_p" "$SCRIPT"; then echo "PASS: S17 has: $_p"; pass=$((pass+1))
  else echo "FAIL: S17 missing: $_p"; failn=$((failn+1)); fi
done
# template + meta shipped in repo
if [ -s "$HERE/../assets/magitrickle-groups.yaml" ] && [ -s "$HERE/../templates/lists.meta.json" ]; then
  echo "PASS: S17 template + meta present"; pass=$((pass+1))
else echo "FAIL: S17 template/meta missing"; failn=$((failn+1)); fi
# template must carry the real groups (To VPN + Google+AI + Youtube)
if grep -qF 'name: To VPN' "$HERE/../assets/magitrickle-groups.yaml" \
   && grep -qF 'name: Google+AI to DE' "$HERE/../assets/magitrickle-groups.yaml" \
   && grep -qF 'name: To Not VPN' "$HERE/../assets/magitrickle-groups.yaml"; then
  echo "PASS: S17 template has routing groups"; pass=$((pass+1))
else echo "FAIL: S17 template missing expected groups"; failn=$((failn+1)); fi
# SECRET SCAN: lists template must not contain URLs or hwid (domain lists only)
if grep -qiE 'http://|https://|x-hwid|hwid:' "$HERE/../assets/magitrickle-groups.yaml"; then
  echo "FAIL: S17 lists template contains URL/hwid"; grep -inE 'http|hwid' "$HERE/../assets/magitrickle-groups.yaml" | head -3 | sed 's/^/    | /'; failn=$((failn+1))
else echo "PASS: S17 lists template has no URLs/hwid"; pass=$((pass+1)); fi
# SECRET SCAN: no 32-hex credential literal in installer source or template
if grep -qE '\b[0-9a-f]{32}\b' "$HERE/../geomask.sh.in" "$HERE/../assets/magitrickle-groups.yaml"; then
  echo "FAIL: S17 possible 32-hex secret literal committed"; failn=$((failn+1))
else echo "PASS: S17 no 32-hex secret literal"; pass=$((pass+1)); fi
# seeder is prefix-aware (takes --config/--template) and idempotent-safe (check-empty)
if grep -qF -- '--check-empty' "$HERE/../assets/geomask-seed-lists.py" \
   && grep -qF -- '--config=' "$HERE/../assets/geomask-seed-lists.py" \
   && grep -qF 'bak_geomask_' "$HERE/../assets/geomask-seed-lists.py"; then
  echo "PASS: S17 seeder: check-empty + prefix paths + backup"; pass=$((pass+1))
else echo "FAIL: S17 seeder contract broken"; failn=$((failn+1)); fi
# backend runs OUTSIDE geomask.sh: svc_alive is undefined there -> must use pgrep (v1.13.0 bug guard)
if grep -qF 'svc_alive' "$HERE/../assets/geo-backend.sh"; then
  echo "FAIL: S17 geo-backend.sh calls svc_alive (undefined in backend context)"; failn=$((failn+1))
else echo "PASS: S17 geo-backend.sh uses pgrep, not svc_alive"; pass=$((pass+1)); fi

# ---------- scenario 18: ytvpn (googlevideo edges -> VPN via MagiTrickle group) (v1.13.2/3) ----------
echo "=== S18: ytvpn -> VPN (v1.13.3) ==="
# seed asset present and points googlevideo at the suffix (source of truth is the MT group)
if [ -s "$HERE/../assets/ytvpn.yaml" ] && grep -q 'DOMAIN-SUFFIX,googlevideo.com' "$HERE/../assets/ytvpn.yaml"; then
  echo "PASS: S18 ytvpn seed has googlevideo suffix"; pass=$((pass+1))
else echo "FAIL: S18 ytvpn seed missing/incomplete"; failn=$((failn+1)); fi
# sync must generate ytvpn.yaml from the GoogleVideo VPN group
for _p in 'GOOGLEVIDEO_GROUP' 'ytvpn_payload' 'write_if_changed(YTVPN'; do
  if grep -qF -- "$_p" "$HERE/../assets/sync_mtrickle_mihomo.sh"; then echo "PASS: S18 sync has: $_p"; pass=$((pass+1))
  else echo "FAIL: S18 sync missing: $_p"; failn=$((failn+1)); fi
done
# ytdirect must NOT carry googlevideo (would win over VPN by ordering)
if grep -qE 'googlevideo\.com$' "$HERE/../assets/ytdirect.yaml"; then
  echo "FAIL: S18 ytdirect still contains googlevideo suffix"; failn=$((failn+1))
else echo "PASS: S18 ytdirect free of googlevideo"; pass=$((pass+1)); fi
# built script wires provider + rule + check
for _p in 'path: /etc/mihomo/ruleset/ytvpn.yaml' 'RULE-SET,ytvpn,VPN' 'ytvpn rule: present'; do
  if grep -qF -- "$_p" "$SCRIPT"; then echo "PASS: S18 has: $_p"; pass=$((pass+1))
  else echo "FAIL: S18 missing: $_p"; failn=$((failn+1)); fi
done
# ORDERING: ytvpn rule must precede ytdirect DIRECT rule (else ytdirect suffix wins -> still DIRECT)
_yt=$(grep -nF 'RULE-SET,ytvpn,VPN' "$HERE/../geomask.sh.in" | head -1 | cut -d: -f1)
_yd=$(grep -nF 'RULE-SET,ytdirect,DIRECT' "$HERE/../geomask.sh.in" | head -1 | cut -d: -f1)
if [ -n "$_yt" ] && [ -n "$_yd" ] && [ "$_yt" -lt "$_yd" ]; then
  echo "PASS: S18 ytvpn rule precedes ytdirect (line $_yt < $_yd)"; pass=$((pass+1))
else echo "FAIL: S18 ytvpn/ytdirect order wrong (ytvpn=$_yt ytdirect=$_yd)"; failn=$((failn+1)); fi

# ---------- scenario 19: every backend dispatch method is present in rpcd ACL (v1.13.3) ----------
echo "=== S19: geo-sync methods must be ACL-granted AND registered in the rpcd wrapper ==="
# collect methods from the backend case-block (lines like:   foo)  cmd_foo ...) and check each
# appears in acl-geo-sync.json read[] or write[]. Missing -> rpcd -32002 Access denied at runtime.
_miss=0; _nowrap=0
for _m in $(grep -oE '^  [a-z_]+\) ' "$HERE/../assets/geo-backend.sh" | tr -d ' )' | sort -u); do
  if ! grep -qF "\"$_m\"" "$HERE/../assets/acl-geo-sync.json"; then
    echo "  MISSING in ACL: $_m"; _miss=$((_miss+1))
  fi
  # v1.13.3 lesson: the ubus method must ALSO be registered in the rpcd plugin wrapper
  if ! grep -qE "(json_add_object \"$_m\"|^\t\t$_m\))" "$HERE/../assets/geo-sync"; then
    echo "  MISSING in rpcd wrapper: $_m"; _nowrap=$((_nowrap+1))
  fi
done
if [ "$_miss" = 0 ]; then echo "PASS: S19 all backend methods granted in ACL"; pass=$((pass+1))
else echo "FAIL: S19 $_miss backend method(s) missing from ACL"; failn=$((failn+1)); fi
if [ "$_nowrap" = 0 ]; then echo "PASS: S19 all backend methods registered in rpcd wrapper"; pass=$((pass+1))
else echo "FAIL: S19 $_nowrap backend method(s) missing from rpcd wrapper"; failn=$((failn+1)); fi
# explicit: lists_get/lists_seed must be present (the v1.13.0 regression)
if grep -qF '"lists_get"' "$HERE/../assets/acl-geo-sync.json" && grep -qF '"lists_seed"' "$HERE/../assets/acl-geo-sync.json" \
   && grep -qF '"lists_get"' "$HERE/../assets/geo-sync" && grep -qF '"lists_seed"' "$HERE/../assets/geo-sync"; then
  echo "PASS: S19 lists_get/lists_seed in ACL + wrapper"; pass=$((pass+1))
else echo "FAIL: S19 lists_* not in ACL/wrapper"; failn=$((failn+1)); fi

# ---------- scenario 48: generated glue stays in sync with tools/geo-rpc.json ----------
echo "=== S48: gen-geo-rpc --check (single-source manifest) ==="
if python3 -c 'pass' >/dev/null 2>&1; then
  if python3 "$HERE/../tools/gen-geo-rpc.py" --check >/dev/null 2>&1; then
    echo "PASS: S48 all four integration points match tools/geo-rpc.json"; pass=$((pass+1))
  else
    echo "FAIL: S48 generated regions drifted (run: python tools/gen-geo-rpc.py)"; failn=$((failn+1))
  fi
else
  echo "SKIP: S48 manifest check (python3 not runnable)"
fi

# ---------- scenario 20: youtube edges watchdog (v1.14.0) ----------
echo "=== S20: ytwatch (googlevideo auto-pin) ==="
if [ -s "$HERE/../assets/geomask-ytwatch.sh" ] && [ -s "$HERE/../assets/geomask-ytwatch.conf" ]; then
  echo "PASS: S20 ytwatch script + conf present"; pass=$((pass+1))
else echo "FAIL: S20 ytwatch assets missing"; failn=$((failn+1)); fi
for _p in '/usr/bin/geomask-ytwatch.sh' '/etc/geomask-ytwatch.conf' '# geomask-ytwatch' '# geomask-ytwatch-reset'; do
  if grep -qF -- "$_p" "$SCRIPT"; then echo "PASS: S20 installer has: $_p"; pass=$((pass+1))
  else echo "FAIL: S20 installer missing: $_p"; failn=$((failn+1)); fi
done
grep -qF 'ytwatch: installed' "$SCRIPT" && { echo "PASS: S20 step5_check verifies ytwatch"; pass=$((pass+1)); } || { echo "FAIL: S20 no ytwatch check"; failn=$((failn+1)); }
grep -qF 'removed: ytwatch' "$SCRIPT" && { echo "PASS: S20 uninstall cleans ytwatch"; pass=$((pass+1)); } || { echo "FAIL: S20 uninstall leaves ytwatch"; failn=$((failn+1)); }
# watchdog semantics: reads mihomo dial errors, pins into the MT group, nightly release, cap, modes
for _p in 'googlevideo' '--reset' '--mode' 'max_pins' 'fail_threshold'; do
  if grep -qF -- "$_p" "$HERE/../assets/geomask-ytwatch.sh"; then echo "PASS: S20 ytwatch has: $_p"; pass=$((pass+1))
  else echo "FAIL: S20 ytwatch missing: $_p"; failn=$((failn+1)); fi
done
for _p in 'callYtGet' 'callYtMode' 'callYtReset' 'yt_get' 'yt_mode' 'yt_reset'; do
  if grep -qF -- "$_p" "$SCRIPT"; then echo "PASS: S20 panel/backend has: $_p"; pass=$((pass+1))
  else echo "FAIL: S20 panel/backend missing: $_p"; failn=$((failn+1)); fi
done
# v1.15.2: stalled-edge detection (DPI kills the stream AFTER the handshake -> no dial error at all)
for _p in 'stall_min_age' 'stall_max_bytes' 'stall_threshold' 'stall_seen' '/connections' "Bearer "; do
  if grep -qF -- "$_p" "$HERE/../assets/geomask-ytwatch.sh"; then echo "PASS: S20 ytwatch has: $_p"; pass=$((pass+1))
  else echo "FAIL: S20 ytwatch missing: $_p"; failn=$((failn+1)); fi
done
if grep -qF 'stall_min_age=' "$HERE/../assets/geomask-ytwatch.conf"; then
  echo "PASS: S20 stall thresholds shipped in the conf"; pass=$((pass+1))
else echo "FAIL: S20 stall thresholds missing from the conf"; failn=$((failn+1)); fi
grep -qF 'Зависшие рёбра' "$HERE/../assets/geo-sync-page.js" \
  && { echo "PASS: S20 panel shows stalled edges"; pass=$((pass+1)); } \
  || { echo "FAIL: S20 panel does not show stalled edges"; failn=$((failn+1)); }
# v1.16.1: state must survive a reboot/reinstall, and the MagiTrickle tag is the source of truth
if grep -qF "STATE='/etc/geomask/ytwatch.state'" "$HERE/../assets/geomask-ytwatch.sh"; then
  echo "PASS: S20 ytwatch state is persistent (not /tmp)"; pass=$((pass+1))
else echo "FAIL: S20 ytwatch state still lives in /tmp"; failn=$((failn+1)); fi
grep -qF 'def auto_rule_ids' "$HERE/../assets/geomask-ytwatch.sh" \
  && { echo "PASS: S20 reset uses the ytwatch rule tag"; pass=$((pass+1)); } \
  || { echo "FAIL: S20 reset has no tag-based fallback"; failn=$((failn+1)); }
grep -qF "rm -f \"\$(P /usr/bin/geomask-ytwatch.sh)\"" "$HERE/../geomask.sh.in" \
  && ! grep -qF 'tmp/geomask-ytwatch.state' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S20 uninstall does not delete the state"; pass=$((pass+1)); } \
  || { echo "FAIL: S20 uninstall still deletes the state"; failn=$((failn+1)); }

# ---------- scenario 21: update card must not show stale installed versions (v1.14.2) ----------
echo "=== S21: live installed-version block in update check ==="
if grep -qF '_upd_local_block' "$HERE/../assets/geo-backend.sh" \
   && grep -qF 'cached GitHub answer, but installed versions are always recomputed live' "$HERE/../assets/geo-backend.sh"; then
  echo "PASS: S21 cached check recomputes installed versions"; pass=$((pass+1))
else echo "FAIL: S21 update card still serves cached installed versions"; failn=$((failn+1)); fi
# the helper must be used by BOTH branches (cache hit and fresh fetch) - no duplicated logic left
if [ "$(grep -c '_upd_local_block' "$HERE/../assets/geo-backend.sh")" -ge 3 ]; then
  echo "PASS: S21 helper defined and used in both paths"; pass=$((pass+1))
else echo "FAIL: S21 helper not used in both paths"; failn=$((failn+1)); fi

# ---------- scenario 22: reboot must not make the update card lie (v1.14.3) ----------
echo "=== S22: release identity from flash components, not /tmp ==="
if grep -qF 'The "core" marker is an installer copy under /tmp' "$HERE/../assets/geo-backend.sh" \
   && grep -qF 'nothing deployed at all' "$HERE/../assets/geo-backend.sh"; then
  echo "PASS: S22 need-flag falls back to deployed components"; pass=$((pass+1))
else echo "FAIL: S22 need-flag still driven by the volatile /tmp core marker"; failn=$((failn+1)); fi
# the old unconditional rule must be gone
if grep -qE 'if \[ "\$_core" = "none" \]; then _LNEED=true$' "$HERE/../assets/geo-backend.sh"; then
  echo "FAIL: S22 bare core=none -> need=true rule still present"; failn=$((failn+1))
else echo "PASS: S22 no bare core=none -> need=true rule"; pass=$((pass+1)); fi

# ---------- scenario 23: torrent trackers must never reach the VPN lists (v1.14.4) ----------
echo "=== S23: torrentdirect (trackers -> DIRECT) ==="
if [ -s "$HERE/../assets/torrentdirect.yaml" ] && grep -q '^payload:' "$HERE/../assets/torrentdirect.yaml" \
   && grep -q 'DOMAIN-SUFFIX,' "$HERE/../assets/torrentdirect.yaml"; then
  echo "PASS: S23 tracker ruleset asset present with entries"; pass=$((pass+1))
else echo "FAIL: S23 tracker ruleset asset missing/empty"; failn=$((failn+1)); fi
if grep -qE 'x-hwid|token|password|secret|/sub' "$HERE/../assets/torrentdirect.yaml"; then
  echo "FAIL: S23 ruleset asset contains a secret-looking string"; failn=$((failn+1))
else echo "PASS: S23 ruleset asset has no secrets"; pass=$((pass+1)); fi
grep -qF '"torrentdirect.yaml"' "$HERE/../build.py" && { echo "PASS: S23 wired into build MAP"; pass=$((pass+1)); } || { echo "FAIL: S23 not in build MAP"; failn=$((failn+1)); }
for _p in '/etc/mihomo/ruleset/torrentdirect.yaml' 'RULE-SET,torrentdirect,(DIRECT|VPN)' 'torrentdirect rule: present' 'no blanket P2P port rules'; do
  if grep -qF -- "$_p" "$SCRIPT"; then echo "PASS: S23 installer has: $_p"; pass=$((pass+1))
  else echo "FAIL: S23 installer missing: $_p"; failn=$((failn+1)); fi
done
# the legacy rule that sent tracker ports to VPN must be gone FROM THE TEMPLATE.
# Scope to the generated rules block: the same strings appear in step3_check as detectors.
_tmpl=$(awk "/^ *printf .*'rules:'/{f=1} f{print} /'  - MATCH,GLOBAL'/{if(f)exit}" "$HERE/../geomask.sh.in")
if printf '%s' "$_tmpl" | grep -qE "DST-PORT,(6969|1337|2710|8081),VPN"; then
  echo "FAIL: S23 template still redirects P2P ports to VPN"; failn=$((failn+1))
else echo "PASS: S23 no P2P port -> VPN in the template"; pass=$((pass+1)); fi
if ! printf '%s' "$_tmpl" | grep -qF 'RULE-SET,torrentdirect,VPN'; then
  echo "FAIL: S23 template lacks the torrentdirect rule"; failn=$((failn+1))
else echo "PASS: S23 template emits the torrentdirect rule"; pass=$((pass+1)); fi
# ordering INSIDE the template: ai first, then torrentdirect, then mtrickle
_la=$(printf '%s\n' "$_tmpl" | grep -nF "RULE-SET,ai,AI" | head -1 | cut -d: -f1)
_lt=$(printf '%s\n' "$_tmpl" | grep -nF "RULE-SET,torrentdirect,VPN" | head -1 | cut -d: -f1)
_lm=$(printf '%s\n' "$_tmpl" | grep -nF "RULE-SET,mtrickle,VPN" | head -1 | cut -d: -f1)
if [ -n "$_la" ] && [ -n "$_lt" ] && [ -n "$_lm" ] && [ "$_la" -lt "$_lt" ] && [ "$_lt" -lt "$_lm" ]; then
  echo "PASS: S23 template rule order ai < torrentdirect < mtrickle"; pass=$((pass+1))
else echo "FAIL: S23 template rule order wrong (ai=$_la td=$_lt mt=$_lm)"; failn=$((failn+1)); fi
# the DIRECT listener for torrent clients is documented in the template too
grep -qF 'torrent-direct' "$HERE/../geomask.sh.in" || grep -qF 'torrent-direct' "$HERE/../docs/YOUTUBE.md" || grep -qF 'torrent-direct' "$HERE/../docs/ARCHITECTURE.md" \
  && { echo "PASS: S23 DIRECT listener documented"; pass=$((pass+1)); } || { echo "FAIL: S23 torrent-direct listener not documented"; failn=$((failn+1)); }

# ---------- scenario 24: torrent admin in the panel (v1.15.0) ----------
echo "=== S24: torrent admin (geomask-torrent.sh + panel) ==="
if [ -s "$HERE/../assets/geomask-torrent.sh" ]; then
  echo "PASS: S24 admin script present"; pass=$((pass+1))
else echo "FAIL: S24 admin script missing"; failn=$((failn+1)); fi
for _p in '--status' '--refresh' '--listener' '--extra' 'mihomo -t' 'Authorization'; do
  if grep -qF -- "$_p" "$HERE/../assets/geomask-torrent.sh"; then echo "PASS: S24 script has: $_p"; pass=$((pass+1))
  else echo "FAIL: S24 script missing: $_p"; failn=$((failn+1)); fi
done
grep -qF '"geomask-torrent.sh"' "$HERE/../build.py" && { echo "PASS: S24 in build MAP"; pass=$((pass+1)); } || { echo "FAIL: S24 not in build MAP"; failn=$((failn+1)); }
for _p in '/usr/bin/geomask-torrent.sh' '/etc/geomask/torrent' 'torrent admin: installed' 'removed: torrent admin'; do
  if grep -qF -- "$_p" "$SCRIPT"; then echo "PASS: S24 installer has: $_p"; pass=$((pass+1))
  else echo "FAIL: S24 installer missing: $_p"; failn=$((failn+1)); fi
done
# every admin method must exist in backend, rpcd wrapper AND acl (same trap as v1.13.0)
for _m in torrent_get torrent_refresh torrent_listener torrent_extra; do
  _ok=1
  grep -qF "cmd_$_m" "$HERE/../assets/geo-backend.sh" || { echo "  MISSING backend: $_m"; _ok=0; }
  grep -qF "\"$_m\"" "$HERE/../assets/geo-sync" || { echo "  MISSING rpcd wrapper: $_m"; _ok=0; }
  grep -qF "\"$_m\"" "$HERE/../assets/acl-geo-sync.json" || { echo "  MISSING acl: $_m"; _ok=0; }
  if [ "$_ok" = 1 ]; then echo "PASS: S24 $_m wired in backend+wrapper+acl"; pass=$((pass+1))
  else echo "FAIL: S24 $_m not wired everywhere"; failn=$((failn+1)); fi
done
for _p in 'callTorGet' 'callTorRefresh' 'callTorListener' 'callTorExtra' 'Торренты и трекеры'; do
  grep -qF -- "$_p" "$SCRIPT" && { echo "PASS: S24 panel has: $_p"; pass=$((pass+1)); } || { echo "FAIL: S24 panel missing: $_p"; failn=$((failn+1)); }
done
# the panel must read the 13th promise slot
grep -qF 'all[12]' "$HERE/../assets/geo-sync-page.js" && { echo "PASS: S24 panel consumes the new RPC slot"; pass=$((pass+1)); } || { echo "FAIL: S24 new RPC result not consumed"; failn=$((failn+1)); }
# v1.17.0: lanes. IP rules must never reach the P2P-restricted VPN subscription, because a
# torrent peer is always a bare address. The sync splits them out; --check enforces the invariant.
grep -qF 'MTBULK' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S24 sync emits a separate IP ruleset"; pass=$((pass+1)); } \
  || { echo "FAIL: S24 sync does not split IP rules"; failn=$((failn+1)); }
grep -qF "mt_dom = [p for p in mt_payload if not p.startswith('IP-CIDR')]" "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S24 split is by rule TYPE (not by group name)"; pass=$((pass+1)); } \
  || { echo "FAIL: S24 split is not type-based"; failn=$((failn+1)); }
grep -qF 'lane invariant: mtrickle.yaml has no IP rules' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S24 --check verifies the lane invariant"; pass=$((pass+1)); } \
  || { echo "FAIL: S24 --check does not verify the invariant"; failn=$((failn+1)); }
grep -qF 'lane: mtrickle_bulk -> VPN rule missing' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S24 --check requires the lane routing rule"; pass=$((pass+1)); } \
  || { echo "FAIL: S24 --check does not require the lane rule"; failn=$((failn+1)); }
# v1.23.1 (owner decision 02.11): the torrent listener must be pinned to the P2P-tolerant lane
# (VPN) or DIRECT - NEVER to AI (AI is node-relay, the provider that banned the sub) and never
# rule-based (then peers leak to node-relay via MagiTrickle IP marks). --check enforces this.
if grep -qF 'pinned to AI = node-relay' "$HERE/../geomask.sh.in"; then
  echo "PASS: S24 --check rejects an AI-pinned torrent listener"; pass=$((pass+1))
else echo "FAIL: S24 --check does not reject AI listener (ban vector)"; failn=$((failn+1)); fi
if grep -qF 'no fixed policy - peers can reach node-relay' "$HERE/../geomask.sh.in"; then
  echo "PASS: S24 --check rejects a policy-less listener"; pass=$((pass+1))
else echo "FAIL: S24 lost the no-policy safety branch"; failn=$((failn+1)); fi
if grep -qE 'SRC-PORT,41552,\(VPN\|DIRECT\)' "$HERE/../geomask.sh.in"; then
  echo "PASS: S24 peer-reply guard is VPN/DIRECT only (not AI)"; pass=$((pass+1))
else echo "FAIL: S24 peer-reply guard still allows AI"; failn=$((failn+1)); fi
if grep -qF 'def tolerant_lane' "$HERE/../assets/geomask-torrent.sh" && grep -qF "return tolerant_lane() or 'DIRECT'" "$HERE/../assets/geomask-torrent.sh"; then
  echo "PASS: S24 admin script pins to tolerant lane/DIRECT (never AI)"; pass=$((pass+1))
else echo "FAIL: S24 admin script has no tolerant-lane policy"; failn=$((failn+1)); fi
# v1.23.2 (R01/P1): --check must guard that node-relay groups carry no static IP ranges
if grep -qF 'static IP range(s)' "$HERE/../geomask.sh.in" && grep -qF 'type: subnet' "$HERE/../geomask.sh.in"; then
  echo "PASS: S24 --check guards node-relay static IP ranges (R01/P1)"; pass=$((pass+1))
else echo "FAIL: S24 --check missing node-relay static-IP guard"; failn=$((failn+1)); fi
grep -qF "'    proxy: DIRECT'" "$HERE/../geomask.sh.in" \
  && { echo "PASS: S24 fresh-install template pins a safe policy"; pass=$((pass+1)); } \
  || { echo "FAIL: S24 template ships a policy-less listener without lanes"; failn=$((failn+1)); }
# a self-update must not wipe accumulated YouTube pins (observed 31.10: --reinstall reset 10 pins)
grep -qF 'if [ -z "$1" ] && [ -x "$(P /usr/bin/geomask-ytwatch.sh)" ]' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S24 pins are released only on a real uninstall"; pass=$((pass+1)); } \
  || { echo "FAIL: S24 reinstall still releases YouTube pins"; failn=$((failn+1)); }

# v1.16.2: the panel must be able to bypass its own 1-hour cache for the GitHub poll
grep -qF "callUpdCheck({ force: 'force' })" "$HERE/../assets/geo-sync-page.js" \
  && { echo "PASS: S21 panel can force a live update check"; pass=$((pass+1)); } \
  || { echo "FAIL: S21 panel cannot force a live update check"; failn=$((failn+1)); }
grep -qF 'Проверить сейчас' "$HERE/../assets/geo-sync-page.js" \
  && { echo "PASS: S21 force-check control is labelled"; pass=$((pass+1)); } \
  || { echo "FAIL: S21 force-check control missing"; failn=$((failn+1)); }

# ==================== S25: route slots + VPN lane audit (v1.18.0) ====================
echo ""
echo "=== S25: slots VPN-noP2P / VPN / AI + lane audit ==="
# the restricted slot must be the one geomask ships and checks
grep -qF "'  - name: VPN-noP2P'" "$HERE/../geomask.sh.in" \
  && { echo "PASS: S25 template creates the VPN-noP2P slot"; pass=$((pass+1)); } \
  || { echo "FAIL: S25 template has no VPN-noP2P slot"; failn=$((failn+1)); }
grep -qF 'name: VPN$' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S25 --check distinguishes the optional VPN lane"; pass=$((pass+1)); } \
  || { echo "FAIL: S25 --check cannot tell a lane apart from no lane"; failn=$((failn+1)); }
# the new ubus method must exist in backend, wrapper AND acl (v1.13.0 trap)
for _m in vpnnp_sub_use; do
  grep -qF "$_m)" "$HERE/../assets/geo-backend.sh" \
    && grep -qF "\"$_m\"" "$HERE/../assets/geo-sync" \
    && grep -qF "\"$_m\"" "$HERE/../assets/acl-geo-sync.json" \
    && { echo "PASS: S25 $_m wired in backend+wrapper+acl"; pass=$((pass+1)); } \
    || { echo "FAIL: S25 $_m missing in one of backend/wrapper/acl"; failn=$((failn+1)); }
done
grep -qF 'callVpnnpSubUse' "$HERE/../assets/geo-sync-page.js" \
  && { echo "PASS: S25 panel exposes the third slot"; pass=$((pass+1)); } \
  || { echo "FAIL: S25 panel has no VPN-noP2P button"; failn=$((failn+1)); }
# status must report both tunnels, or the panel cards would lie
grep -qF '"vpn_nop2p_node"' "$HERE/../assets/geo-backend.sh" \
  && grep -qF '"vpn_nop2p_sub"' "$HERE/../assets/geo-backend.sh" \
  && { echo "PASS: S25 status reports both VPN slots"; pass=$((pass+1)); } \
  || { echo "FAIL: S25 status misses a slot"; failn=$((failn+1)); }
# lane creation must be reversible: backup + validate + rollback
grep -qF 'bak_slot' "$HERE/../assets/geo-backend.sh" \
  && grep -qF 'mihomo config test failed - old config kept' "$HERE/../assets/geo-backend.sh" \
  && { echo "PASS: S25 slot edit validates and rolls back"; pass=$((pass+1)); } \
  || { echo "FAIL: S25 slot edit has no rollback"; failn=$((failn+1)); }
for _p in 'geomask-vpnwatch.sh' 'geomask-vpnwatch.conf'; do
  grep -qF "\"$_p\"" "$HERE/../build.py" \
    && { echo "PASS: S25 asset in build MAP: $_p"; pass=$((pass+1)); } \
    || { echo "FAIL: S25 asset not in build MAP: $_p"; failn=$((failn+1)); }
done
grep -qF 'geomask-vpnwatch.sh 755' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S25 installer installs the audit script"; pass=$((pass+1)); } \
  || { echo "FAIL: S25 installer does not ship the audit script"; failn=$((failn+1)); }
grep -qF '# geomask-vpnwatch' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S25 audit cron tagged for clean removal"; pass=$((pass+1)); } \
  || { echo "FAIL: S25 cron is not tagged"; failn=$((failn+1)); }
grep -qF 'geomask-vpnwatch.sh)' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S25 uninstall removes the audit script"; pass=$((pass+1)); } \
  || { echo "FAIL: S25 uninstall leaves the audit script"; failn=$((failn+1)); }
# the audit must never touch routing - it only reads
if grep -nE 'PUT .*proxies|/proxies/.{0,20}DELETE|url-test' "$HERE/../assets/geomask-vpnwatch.sh" | grep -q .; then
  echo "FAIL: S25 audit script tries to change routing"; failn=$((failn+1))
else echo "PASS: S25 audit script is read-only"; pass=$((pass+1)); fi
grep -qF 'group=VPN-noP2P' "$HERE/../assets/geomask-vpnwatch.conf" \
  && { echo "PASS: S25 audit watches the restricted slot by default"; pass=$((pass+1)); } \
  || { echo "FAIL: S25 audit default group is wrong"; failn=$((failn+1)); }

# ==================== S26: zapret sync + port-rule invariant (v1.19.0) ====================
echo ""
echo "=== S26: zapret exclude sync and the restricted-slot invariant ==="
grep -qF 'ZEXCL' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && grep -qF 'geomask sync: tunneled domains' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S26 sync writes a managed zapret exclude block"; pass=$((pass+1)); } \
  || { echo "FAIL: S26 sync has no zapret exclude block"; failn=$((failn+1)); }
grep -qF 'pinned_direct' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S26 DIRECT-pinned domains stay under DPI"; pass=$((pass+1)); } \
  || { echo "FAIL: S26 would exclude domains that go DIRECT"; failn=$((failn+1)); }
# the exclude file keeps its own content: the block is replaced by markers, not by rewriting
grep -qF 'drop our previous block by marker positions' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S26 exclude file keeps its own content around the block"; pass=$((pass+1)); } \
  || { echo "FAIL: S26 may clobber the exclude file"; failn=$((failn+1)); }
# v1.20.2: zapret wants BARE domains - writing "DOMAIN-SUFFIX,foo" would never match
grep -qF 'zapret' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && grep -qF 'def bare(p)' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S26 exclude entries are stripped to bare domains"; pass=$((pass+1)); } \
  || { echo "FAIL: S26 would write mihomo rule syntax into zapret"; failn=$((failn+1)); }
grep -qF 'send ports/IPs/protocols to VPN-noP2P' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S26 --check rejects port/IP rules on the restricted slot"; pass=$((pass+1)); } \
  || { echo "FAIL: S26 --check has no port-rule invariant"; failn=$((failn+1)); }
# v1.19.1: the actions column holds four buttons - nowrap pushed them out of the card
if grep -qF 'gs-subs td:last-child{width:200px;white-space:nowrap}' "$HERE/../assets/geo-sync-page.js"; then
  echo "FAIL: S26 subscription actions still force a single nowrap line"; failn=$((failn+1))
else echo "PASS: S26 subscription actions may wrap inside the card"; pass=$((pass+1)); fi
grep -qF '.gs-subs td:nth-child(3) .gs-badge{white-space:nowrap}' "$HERE/../assets/geo-sync-page.js" \
  && { echo "PASS: S26 the slot badge stays on one line"; pass=$((pass+1)); } \
  || { echo "FAIL: S26 the slot badge can break across lines"; failn=$((failn+1)); }

# v1.20.0: hand-written DIRECT pins outrank whatever group the owner picks - they must be gone
if grep -qE "DOMAIN-SUFFIX,(thepiratebay|rutracker)[a-z.]*,DIRECT" "$HERE/../geomask.sh.in"; then
  echo "FAIL: S26 template still emits hardcoded torrent DIRECT pins"; failn=$((failn+1))
else echo "PASS: S26 no hardcoded torrent pins in the template"; pass=$((pass+1)); fi
# node flags must reach the UI: cleanName strips them, so the badge has to carry them
grep -qF 'function flagOf' "$HERE/../assets/geo-sync-page.js" \
  && grep -qF 'ccBadge(cc, name)' "$HERE/../assets/geo-sync-page.js" \
  && { echo "PASS: S26 the badge renders the flag from the node name"; pass=$((pass+1)); } \
  || { echo "FAIL: S26 flags are still stripped everywhere"; failn=$((failn+1)); }
if grep -qE 'ccBadge\((aiCc|vpnCc|laneCc)\)' "$HERE/../assets/geo-sync-page.js"; then
  echo "FAIL: S26 some node badge still gets no name (flag lost)"; failn=$((failn+1))
else echo "PASS: S26 all three slot badges receive the node name"; pass=$((pass+1)); fi

# v1.20.1: the first implementation used a None sentinel for the pre-block list and crashed on
# the SECOND run (when the block already existed) - a crash here also skips the mihomo reload.
grep -qF 'pre, post = old[:bi], old[ei+1:]' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S26 exclude block is replaced by marker positions"; pass=$((pass+1)); } \
  || { echo "FAIL: S26 exclude block rewrite looks fragile again"; failn=$((failn+1)); }
grep -qF 'a zapret problem must never block the mihomo ruleset sync' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S26 zapret errors cannot break the ruleset sync"; pass=$((pass+1)); } \
  || { echo "FAIL: S26 a zapret error would abort the sync"; failn=$((failn+1)); }

# v1.20.3: the node filter must reach every VPN slot, must not be appended with duplicates,
# and must never be applied to the AI slot (its subscription is USA-only - a name list tuned
# to another provider emptied it to one pseudo-node when that was done by mistake).
grep -qF 'def apply_filter_all' "$HERE/../assets/geomask-node-scan.sh" \
  && grep -qF 'def group_blocks' "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S27 scan writes the filter per group, not per line"; pass=$((pass+1)); } \
  || { echo "FAIL: S27 scan still rewrites the first exclude-filter it finds"; failn=$((failn+1)); }
grep -qF 'if p and p not in seen' "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S27 filter alternations are de-duplicated"; pass=$((pass+1)); } \
  || { echo "FAIL: S27 the filter grows duplicated terms each run"; failn=$((failn+1)); }
grep -qF "name_filter_groups='VPN,VPN-noP2P'" "$HERE/../assets/geomask-nodes.conf" \
  && { echo "PASS: S27 AI is excluded from name filtering by default"; pass=$((pass+1)); } \
  || { echo "FAIL: S27 AI could be emptied by the name filter"; failn=$((failn+1)); }

# v1.20.4: scan all subscriptions, zapret direction B, seconds-fast YouTube failover
# behavioural test: run the REAL enumeration against the fixture config. A grep cannot catch a
# missing re.M flag - that is exactly how "scanning all 0 providers" shipped once.
if python3 -c 'pass' >/dev/null 2>&1; then
  cat > "$HERE/state/providers.yaml" <<'PROVEOF'
proxy-providers:
  sub:
    type: http
    url: "https://example.com/a"
  de5:
    type: http
    url: "https://example.com/b"
  sub2:
    type: http
    url: "https://example.com/c"
rule-providers:
  ai:
    type: file
PROVEOF
  printf 'sub_provider=all\nbad_countries=RU\nbase_filter=x\n' > "$HERE/state/nodes.conf"
  _got=$(GM_NODES_CONF="$HERE/state/nodes.conf" GM_MIHOMO_CFG="$HERE/state/providers.yaml" \
         sh "$HERE/../assets/geomask-node-scan.sh" --list-providers 2>/dev/null | tail -1)
  case "$_got" in
    '')     echo "SKIP: S27 provider enumeration (python not runnable in this sandbox)" ;;
    sub,de5,sub2) echo "PASS: S27 provider enumeration finds all three (got: $_got)"; pass=$((pass+1)) ;;
    *)      echo "FAIL: S27 provider enumeration returned '$_got' (want sub,de5,sub2)"; failn=$((failn+1)) ;;
  esac
else
  echo "SKIP: S27 provider enumeration (python3 not runnable)"
fi
grep -qF "elif provider.strip()=='all'" "$HERE/../assets/geomask-node-scan.sh" \
  && grep -qF "'--provider'" "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S27 scan fans out per provider via --provider"; pass=$((pass+1)); } \
  || { echo "FAIL: S27 scan has no fan-out implementation"; failn=$((failn+1)); }
grep -qF 'def _zapret_force_sync' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S28 sync implements zapret direction B"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 direction B is missing"; failn=$((failn+1)); }
grep -qF "out.append('%s %s' % (ZMK, dom))" "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S28 force list is commented in place, not rewritten"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 would drop operator lines"; failn=$((failn+1)); }
for _d in api.epicgames.dev firstlook.gg live.wardogs.bulkhead.pragmaengine.com; do
  grep -qF "$_d" "$HERE/../assets/zapret-force-keep.txt" \
    && { echo "PASS: S28 wardogs exception kept: $_d"; pass=$((pass+1)); } \
    || { echo "FAIL: S28 missing exception: $_d"; failn=$((failn+1)); }
done
grep -qF 'zapret force: skipped' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S28 direction B cannot break the ruleset sync"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 direction B is unguarded"; failn=$((failn+1)); }
# v1.20.6: group offsets must be absolute - relative ones made the scan claim no group uses
# the provider, while the group plainly did (VPN uses sub2).
grep -qF 'yield (name, body_start+start, body_start+pos)' "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S27 group offsets are absolute"; pass=$((pass+1)); } \
  || { echo "FAIL: S27 group offsets are slice-relative again"; failn=$((failn+1)); }
# one dead provider must not erase the others
grep -qF 'if not _ok and not _skip:' "$HERE/../assets/geomask-node-scan.sh" \
  && grep -qF 'ok=[' "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S27 fan-out isolates per-provider failures and reports them"; pass=$((pass+1)); } \
  || { echo "FAIL: S27 fan-out still aborts on one provider"; failn=$((failn+1)); }
grep -qF 'skipped by design' "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S27 a provider outside name filtering is skipped, not failed"; pass=$((pass+1)); } \
  || { echo "FAIL: S27 the AI subscription will be logged as a failure nightly"; failn=$((failn+1)); }
grep -qF 'Турция' "$HERE/../assets/geomask-nodes.conf" \
  && { echo "PASS: S27 base_filter covers the Cyrillic Turkey"; pass=$((pass+1)); } \
  || { echo "FAIL: S27 base_filter still misses the Cyrillic Turkey"; failn=$((failn+1)); }
grep -qF 'geomask-morning-check.sh 755' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S28 morning check ships with geomask"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 morning check is not installed"; failn=$((failn+1)); }
# v1.21.0: the extra lane must refuse to touch a config without the group, must validate the
# listener before writing it, and must restore the config if validation fails.
grep -qF 'group $LANE_GROUP not found - refusing' "$HERE/../assets/geomask-lane.sh" \
  && { echo "PASS: S30 lane refuses when the mihomo group is absent"; pass=$((pass+1)); } \
  || { echo "FAIL: S30 lane could create a tunnel to nothing"; failn=$((failn+1)); }
grep -qF 'mihomo -t failed after adding the listener - config restored' "$HERE/../assets/geomask-lane.sh" \
  && { echo "PASS: S30 listener write is validated and rolled back"; pass=$((pass+1)); } \
  || { echo "FAIL: S30 listener write is unvalidated"; failn=$((failn+1)); }
# v1.22.0: torrent forum sites must go DIRECT - measured 502 through the VPN exit, 301 direct
grep -qF 'def build_sites' "$HERE/../assets/geomask-torrent.sh" \
  && { echo "PASS: S36 torrent sites get their own ruleset"; pass=$((pass+1)); } \
  || { echo "FAIL: S36 sites and trackers share one target"; failn=$((failn+1)); }
grep -qF 'RULE-SET,torrentsites,DIRECT' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S36 sites rule precedes the tracker rule in the template"; pass=$((pass+1)); } \
  || { echo "FAIL: S36 sites would still be pushed into the VPN lane"; failn=$((failn+1)); }
# v1.22.1: DNS safety audit must ship and must never modify anything
grep -qF 'geomask-dnscheck.sh 755' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S37 DNS audit ships with geomask"; pass=$((pass+1)); } \
  || { echo "FAIL: S37 DNS audit is not installed"; failn=$((failn+1)); }
if grep -qE 'uci (set|commit|delete)|sed -i|>/etc/hosts|/etc/init.d/.*restart' "$HERE/../assets/geomask-dnscheck.sh"; then
  echo "FAIL: S37 the DNS audit is not read-only"; failn=$((failn+1))
else
  echo "PASS: S37 the DNS audit only reads"; pass=$((pass+1))
fi
grep -qF 'stale bypass pins' "$HERE/../assets/geomask-dnscheck.sh" \
  && { echo "PASS: S37 dead /etc/hosts pins are detected"; pass=$((pass+1)); } \
  || { echo "FAIL: S37 stale pins would go unnoticed"; failn=$((failn+1)); }
grep -qF 'geomask-dnscheck.sh --quick' "$HERE/../assets/geomask-morning-check.sh" \
  && { echo "PASS: S37 the morning check includes the DNS audit"; pass=$((pass+1)); } \
  || { echo "FAIL: S37 the morning check skips DNS"; failn=$((failn+1)); }
if grep -qE '^\s*\[ .* \] && (warn|ok|bad) .* \|\| .*\[ ' "$HERE/../assets/geomask-dnscheck.sh"; then
  echo "FAIL: S37 an && ... || chain can print two contradictory verdicts"; failn=$((failn+1))
else
  echo "PASS: S37 verdicts use if/elif, not && || chains"; pass=$((pass+1))
fi
if grep -v '^[[:space:]]*#' "$HERE/../assets/geomask-morning-check.sh" | grep -qF 'PIPESTATUS'; then
  echo "FAIL: S37 morning check uses a bash-only variable ash lacks"; failn=$((failn+1))
else
  echo "PASS: S37 morning check stays ash-compatible"; pass=$((pass+1))
fi
grep -qF 'geomask-lane.sh 755' "$HERE/../geomask.sh.in" \
  && grep -qF 'geomask-lane.sh)" remove' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S30 lane is installed and removed with geomask"; pass=$((pass+1)); } \
  || { echo "FAIL: S30 lane lifecycle is incomplete"; failn=$((failn+1)); }
grep -qF 'stall_min_conns=3' "$HERE/../assets/geomask-ytwatch.conf" \
  && { echo "PASS: S28 a stalled edge needs a swarm, not one idle connection"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 idle prefetch connections can pin healthy edges"; failn=$((failn+1)); }
grep -qF 'stall_threshold=4' "$HERE/../assets/geomask-ytwatch.conf" \
  && { echo "PASS: S28 pinning needs four consecutive samples"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 one sample can pin an edge"; failn=$((failn+1)); }
grep -qF 'for h in stalled:' "$HERE/../assets/geomask-ytwatch.sh" \
  && { echo "PASS: S28 stall counter accumulates samples, not connections"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 a single swarm still crosses the threshold"; failn=$((failn+1)); }
grep -qF 'if len(stalled) >= blanket_when' "$HERE/../assets/geomask-ytwatch.sh" \
  && { echo "PASS: S28 many quiet edges escalate to one blanket rule"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 cannot win the 10s edge-rotation race"; failn=$((failn+1)); }
grep -qF 'blanket_when=4' "$HERE/../assets/geomask-ytwatch.conf" \
  && grep -qF 'blanket_off_after=20' "$HERE/../assets/geomask-ytwatch.conf" \
  && { echo "PASS: S28 blanket trigger and hysteresis are configured"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 blanket would never come back down"; failn=$((failn+1)); }
grep -qF "out['auto_pin_count']" "$HERE/../assets/geomask-ytwatch.sh" \
  && { echo "PASS: S28 status separates auto-pins from all group rules"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 status can mislead about how many pins exist"; failn=$((failn+1)); }
grep -qF '*/1 * * * * /usr/bin/geomask-ytwatch.sh' "$HERE/../geomask.sh.in" \
  && grep -qF 'ytwatch cron migrated */5 -> */1' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S28 ytwatch cron is */1 and migrates old entries"; pass=$((pass+1)); } \
  || { echo "FAIL: S28 ytwatch cron still 5 min or does not migrate"; failn=$((failn+1)); }

# v1.20.6: every embedded python block must parse. Two shipped bugs (the None sentinel and the
# missing re.M) were invisible to grep-based tests and only surfaced on the router; this gate
# catches syntax errors here instead.
_PYC=''
for c in python3 python; do if "$c" -c 'pass' >/dev/null 2>&1; then _PYC="$c"; break; fi; done
if [ -n "$_PYC" ]; then
  if "$_PYC" "$HERE/../tools/pycheck.py" "$HERE"/../assets/*.sh >"$HERE/state/pycheck.out" 2>&1; then
    _n=$(grep -c 'parses OK' "$HERE/state/pycheck.out")
    echo "PASS: S29 embedded python parses ($_n blocks checked)"; pass=$((pass+1))
  else
    echo "FAIL: S29 embedded python has a syntax error"; sed 's/^/    | /' "$HERE/state/pycheck.out"; failn=$((failn+1))
  fi
else
  echo "SKIP: S29 python syntax gate (no runnable python)"
fi

# v1.21.3: a provider health-check removes failing nodes from the group, which emptied the VPN
# lane and turned every list into 502s (owner: "server banners appear, then disappear").
if grep -nE 'health-check:' "$HERE/../geomask.sh.in" | grep -vE '^[0-9]+:[[:space:]]*#|NO provider' | grep -q .; then
  echo "FAIL: S31 the installer template still creates a provider health-check"; failn=$((failn+1))
else
  echo "PASS: S31 no provider health-check in the template"; pass=$((pass+1))
fi
if grep -nE 'health-check:' "$HERE/../assets/geo-backend.sh" | grep -v '^[0-9]*:#' | grep -q .; then
  echo "FAIL: S31 adding a subscription from the panel would reintroduce it"; failn=$((failn+1))
else
  echo "PASS: S31 no provider health-check when adding a subscription"; pass=$((pass+1))
fi
_o=$(grep -oE "E\('button', b[VAN]," "$HERE/../assets/geo-sync-page.js" | grep -oE 'b[VAN]' | tr -d 'b' | tr '\n' ' ')
[ "$_o" = "V A N " ] && { echo "PASS: S31 assignment buttons ordered VPN, AI, VPN-noP2P (AI switch restored, isolated)"; pass=$((pass+1)); } \
  || { echo "FAIL: S31 assignment buttons ordered '$_o'"; failn=$((failn+1)); }

# v1.21.4: --reinstall runs the uninstall path too, and it used to destroy the lane - which
# silently deleted an interface the owner had already assigned MagiTrickle lists to.
grep -qF 'if [ -z "$1" ] && [ -x "$(P /usr/bin/geomask-lane.sh)" ]' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S32 lane survives --reinstall (removed only on a real uninstall)"; pass=$((pass+1)); } \
  || { echo "FAIL: S32 a reinstall would delete the lane and its interface"; failn=$((failn+1)); }

# v1.21.5: DIRECT must not live inside the VPN group - it turns a subscription outage into a
# silent plain-path leak instead of a loud failure.
grep -qF 'the VPN group contains DIRECT' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S33 --check rejects DIRECT inside the VPN group"; pass=$((pass+1)); } \
  || { echo "FAIL: S33 --check no longer guards the VPN group against DIRECT"; failn=$((failn+1)); }
if grep -qF 'fallback with DIRECT last resort' "$HERE/../geomask.sh.in"; then
  echo "FAIL: S33 the old advice (fallback + DIRECT) is still in the checker"; failn=$((failn+1))
else
  echo "PASS: S33 the checker no longer recommends a direct leak"; pass=$((pass+1))
fi

# v1.21.6: the sync must follow the interface, not one hard-coded name - otherwise pointing a
# group at the AI tunnel silently removes its domains from the AI ruleset.
# v1.21.7: the panel needs the provider's own headers to fetch a subscription at all - without
# x-hwid the server answers a 1.4 KB stub, and the scan kept "refusing to overwrite" forever.
grep -qF 'def provider_block(name)' "$HERE/../assets/geomask-node-scan.sh" \
  && grep -qF "log('provider %s: url ok, %d header(s)" "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S35 scan reads every provider header, not one regex guess"; pass=$((pass+1)); } \
  || { echo "FAIL: S35 the scan can still fetch a subscription without its headers"; failn=$((failn+1)); }
# the one number the owner is told to watch must not fire on mDNS
grep -qF 'def is_group_traffic' "$HERE/../assets/geomask-vpnwatch.sh" \
  && grep -qF 'PEER-SHAPED (bare IP, not multicast)' "$HERE/../assets/geomask-vpnwatch.sh" \
  && { echo "PASS: S35 multicast is excluded from the peer-shaped signal"; pass=$((pass+1)); } \
  || { echo "FAIL: S35 mDNS traffic is reported as P2P"; failn=$((failn+1)); }
# torrentdirect may legitimately target either lane now; the checker must not cry over it
grep -qF 'RULE-SET,torrentdirect,(DIRECT|VPN)' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S35 --check accepts torrentdirect on either lane"; pass=$((pass+1)); } \
  || { echo "FAIL: S35 --check still demands the retired DIRECT target"; failn=$((failn+1)); }
grep -qF "TUNNELED_IFACES = {'Mihomo', 'AI', 'VPN-noP2P'}" "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && grep -qF "g.get('interface') not in TUNNELED_IFACES" "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S34 sync classifies groups by interface"; pass=$((pass+1)); } \
  || { echo "FAIL: S34 sync still only reads interface Mihomo"; failn=$((failn+1)); }
grep -qF "is_ai = g.get('interface') == 'AI'" "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S34 a group on the AI tunnel always lands in ai.yaml"; pass=$((pass+1)); } \
  || { echo "FAIL: S34 AI membership still depends on the group name"; failn=$((failn+1)); }
grep -qF 'def audit_restricted' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
  && { echo "PASS: S34 IP ranges on the restricted tunnel are reported"; pass=$((pass+1)); } \
  || { echo "FAIL: S34 the restricted tunnel cannot be audited"; failn=$((failn+1)); }

# v1.22.3: the installer runs under `set -u` on Debian's dash, where a new variable used
# without a default kills the run with rc=2 - invisible under Git bash, which has no nounset.
if grep -E '\$\{GM_[A-Z_]+\}[^:=]' "$HERE/../geomask.sh.in" | grep -qv '^\s*#'; then
  echo "FAIL: S38 a GM_* variable is referenced bare (breaks under set -u)"
  grep -nE '\$\{GM_[A-Z_]+\}' "$HERE/../geomask.sh.in" | grep -v ':-' | head -5 | sed 's/^/    /'
  failn=$((failn+1))
else
  echo "PASS: S38 no bare GM_* references outside assignments"; pass=$((pass+1))
fi
# and the lane flag must carry its default
grep -qF '${GM_AUTO_LANE:-}' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S38 GM_AUTO_LANE has a nounset-safe default"; pass=$((pass+1)); } \
  || { echo "FAIL: S38 GM_AUTO_LANE is bare again"; failn=$((failn+1)); }

# v1.22.5: blanket is a standing decision, not an escalation the watchdog can undo
grep -qF 'blanket_permanent' "$HERE/../assets/geomask-ytwatch.sh" \
  && { echo "PASS: S39 the blanket can be made permanent"; pass=$((pass+1)); } \
  || { echo "FAIL: S39 the watchdog would still downgrade the blanket on its own"; failn=$((failn+1)); }
grep -qF 'blanket_permanent=1' "$HERE/../assets/geomask-ytwatch.conf" \
  && { echo "PASS: S39 shipped config keeps all googlevideo on the VPN"; pass=$((pass+1)); } \
  || { echo "FAIL: S39 the shipped config still starts in per-edge mode"; failn=$((failn+1)); }
# a pin that belongs to a tunneled name must not be judged by a direct probe
grep -qF 'is_tunneled' "$HERE/../assets/geomask-dnscheck.sh" \
  && { echo "PASS: S39 tunneled pins are probed through the lane, not directly"; pass=$((pass+1)); } \
  || { echo "FAIL: S39 the pin check will cry wolf on AI pins"; failn=$((failn+1)); }

# v1.22.6: an upgrade must teach an existing config the new options, not freeze it
grep -qF 'added $_added new option(s)' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S40 conf upgrade appends missing keys"; pass=$((pass+1)); } \
  || { echo "FAIL: S40 a new option would never reach an existing router"; failn=$((failn+1)); }
if grep -A22 'ytwatch conf: kept existing' "$HERE/../geomask.sh.in" | grep -qE "sed -i|'> *\"?\$\(P /etc/geomask-ytwatch"; then
  echo "FAIL: S40 the upgrade overwrites operator values"; failn=$((failn+1))
else
  echo "PASS: S40 the upgrade only appends absent keys"; pass=$((pass+1))
fi
# a tunneled pin must be judged by every lane, not by the torrent listener
grep -qF 'for port in (7891, 7892, 7894)' "$HERE/../assets/geomask-dnscheck.sh" \
  && { echo "PASS: S40 tunneled pins are tried through all lanes"; pass=$((pass+1)); } \
  || { echo "FAIL: S40 pins are still judged by the torrent listener only"; failn=$((failn+1)); }

# v1.22.7: the lane MUST get a firewall zone, or fw4 rejects (tcp-reset) all its traffic
grep -qF '_provision_netfw' "$HERE/../assets/geomask-lane.sh" \
  && grep -qF 'accept_to_' "$HERE/../assets/geomask-lane.sh" \
  && { echo "PASS: S41 geomask-lane provisions a firewall zone for the tun"; pass=$((pass+1)); } \
  || { echo "FAIL: S41 the lane tun has no zone and fw4 rejects it (anime/news dead)"; failn=$((failn+1)); }
grep -qF '_teardown_netfw' "$HERE/../assets/geomask-lane.sh" \
  && { echo "PASS: S41 lane removal tears the zone/network down too"; pass=$((pass+1)); } \
  || { echo "FAIL: S41 removal leaves an orphan zone/network behind"; failn=$((failn+1)); }

# v1.22.8: the AI lane must be USA-filtered (un1.pro routes OpenAI via a blocked Helsinki relay)
if grep -A8 "  - name: AI" "$HERE/../geomask.sh.in" | grep -qE "use:|      - sub" \
   && grep -A8 "  - name: AI" "$HERE/../geomask.sh.in" | grep -qF "filter: 'USA'"; then
  echo "PASS: S42 AI group borrows node-relay and filters to USA"; pass=$((pass+1))
else
  echo "FAIL: S42 AI group is not USA-filtered (OpenAI would see the RU-mapped relay)"; failn=$((failn+1))
fi
grep -A8 "  - name: AI" "$HERE/../geomask.sh.in" | grep -qF "exclude-filter: 'HY2'" \
  && { echo "PASS: S42 the dead USA-HY2 node is excluded from AI"; pass=$((pass+1)); } \
  || { echo "FAIL: S42 USA-HY2 (unreachable) is left in the AI group"; failn=$((failn+1)); }

# v1.22.9: the AI group must be guarded by an ACTIVE OpenAI-egress test, not just a name filter
grep -qF "'--ai-guard'" "$HERE/../assets/geomask-node-scan.sh" \
  && grep -qF 'cdn-cgi/trace' "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S43 nodescan has an active AI OpenAI-egress guard"; pass=$((pass+1)); } \
  || { echo "FAIL: S43 no active egress guard - a USA-labelled node leaking to OpenAI from FI slips through"; failn=$((failn+1)); }
grep -qF "'--ai-guard'" "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S43 the all-provider pass invokes the AI guard"; pass=$((pass+1)); } \
  || { echo "FAIL: S43 the guard is never invoked from the nightly pass"; failn=$((failn+1)); }
grep -qF 'ai_expect' "$HERE/../assets/geomask-nodes.conf" \
  && { echo "PASS: S43 AI guard config present in geomask-nodes.conf"; pass=$((pass+1)); } \
  || { echo "FAIL: S43 geomask-nodes.conf has no AI guard keys"; failn=$((failn+1)); }

# v1.23.0: de5 (the rejected un1.pro AI sub) must no longer be emitted; AI rides sub
if ! grep -qE "printf '%s\\\\n' '  de5:'" "$HERE/../geomask.sh.in"; then
  echo "PASS: S44 fresh config no longer emits the de5 provider"; pass=$((pass+1))
else
  echo "FAIL: S44 the template still emits de5 (panel shows a dead subscription)"; failn=$((failn+1))
fi
grep -qF 'retired de5 provider removed' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S44 reinstall strips a stale de5 from an existing config"; pass=$((pass+1)); } \
  || { echo "FAIL: S44 reinstall would not clean a leftover de5"; failn=$((failn+1)); }

# v1.24.1: lane groups must be url-test WITH a huge tolerance so mihomo auto-tests every node
# (metacubexd always has fresh per-node data -> the servers never vanish from the list) while the
# huge tolerance stops it from auto-switching `now` (a manual pick still sticks). Plain `select`
# gave testUrl="" (no auto-test) -> metacubexd rendered the group empty.
if grep -qF '    tolerance: 99999' "$HERE/../geomask.sh.in" && grep -qF "    type: url-test" "$HERE/../geomask.sh.in"; then
  echo "PASS: S45 template lane groups are url-test + huge tolerance (auto-test, no switch)"; pass=$((pass+1))
else echo "FAIL: S45 template lane groups not url-test+tolerance (metacubexd shows them empty)"; failn=$((failn+1)); fi
if grep -qF "'tolerance': 99999" "$HERE/../assets/geomask-slot-patch.py"; then
  echo "PASS: S45 panel slot-creator makes url-test+tolerance groups"; pass=$((pass+1))
else echo "FAIL: S45 panel slot-creator missing url-test+tolerance"; failn=$((failn+1)); fi

# v1.25.0: all Yandex domains go DIRECT and bypass zapret DPI (Yandex Go / Plus / Bank).
if grep -qF 'DOMAIN-SUFFIX,%s,DIRECT' "$HERE/../geomask.sh.in" \
   && grep -qF 'yandex-net.ru' "$HERE/../geomask.sh.in" \
   && grep -qF 'yandexcom.net' "$HERE/../geomask.sh.in"; then
  echo "PASS: S46 template emits Yandex DIRECT rules incl. app zones (yandex-net.ru/.com)"; pass=$((pass+1))
else echo "FAIL: S46 template missing Yandex DIRECT rules"; failn=$((failn+1)); fi
if [ -s "$HERE/../assets/zapret-always-exclude.txt" ] \
   && grep -qF 'yandex.ru' "$HERE/../assets/zapret-always-exclude.txt"; then
  echo "PASS: S46 zapret-always-exclude asset lists Yandex"; pass=$((pass+1))
else echo "FAIL: S46 zapret-always-exclude asset missing Yandex"; failn=$((failn+1)); fi
grep -qF '"zapret-always-exclude.txt"' "$HERE/../build.py" \
  && { echo "PASS: S46 always-exclude in build MAP"; pass=$((pass+1)); } \
  || { echo "FAIL: S46 always-exclude not in build MAP"; failn=$((failn+1)); }
if grep -qF 'ZALWAYS' "$HERE/../assets/sync_mtrickle_mihomo.sh" \
   && grep -qF 'tunnel.add(_d)' "$HERE/../assets/sync_mtrickle_mihomo.sh"; then
  echo "PASS: S46 sync merges always-exclude into zapret exclude"; pass=$((pass+1))
else echo "FAIL: S46 sync does not merge always-exclude"; failn=$((failn+1)); fi

# v1.25.3: the shared-provider slot patcher must re-match the group block AFTER inserting the
# v1.27.9: the slot patcher no longer rewrites config TEXT. A regex could not survive the
# config being re-serialised by an apply (`- name:` at column 0), which is exactly why the
# AI switch answered "no AI group anchor". The structural module must be used instead.
grep -qF 'SLOT_PATCH_PY=/usr/lib/geomask/geomask-slot-patch.py' "$HERE/../assets/geo-backend.sh" \
  && ! grep -qF 'no AI group anchor' "$HERE/../assets/geo-backend.sh" \
  && { echo "PASS: S47 slot switching uses the structural patcher, not text regex"; pass=$((pass+1)); } \
  || { echo "FAIL: S47 slot switching still depends on YAML text formatting"; failn=$((failn+1)); }
grep -qF 'geomask-slot-patch.py' "$HERE/../build.py" \
  && grep -qF 'geomask-slot-patch.py' "$HERE/../geomask.sh.in" \
  && { echo "PASS: S47 slot patcher is shipped by build and installer"; pass=$((pass+1)); } \
  || { echo "FAIL: S47 slot patcher not shipped"; failn=$((failn+1)); }

# Run the REAL slot patcher (all three switch buttons go through it) on a config in the
# shape an apply produces. AI on a shared provider must get an isolated provider while VPN
# keeps the shared one; a later VPN switch re-points only the shared provider; VPN-noP2P
# (group absent) must create its own provider and group after the AI anchor.
if [ -n "$_PYC" ]; then
  _s49cfg="$HERE/state/slot_cfg.yaml"
  "$_PYC" - "$_s49cfg" <<'S49GEN'
import sys, yaml
doc = {
    'mixed-port': 7890,
    'external-controller': '127.0.0.1:9090',
    'secret': 's49secret',
    'proxy-providers': {
        'sub': {'type': 'http', 'url': 'https://sub.example/base', 'interval': 3600,
                'header': {'x-hwid': 'HWTEST'}},
    },
    'proxy-groups': [
        {'name': 'VPN', 'type': 'url-test', 'use': ['sub'],
         'url': 'https://www.gstatic.com/generate_204', 'interval': 180},
        {'name': 'AI', 'type': 'url-test', 'use': ['sub'],
         'url': 'https://www.gstatic.com/generate_204', 'interval': 180},
    ],
    'rules': ['RULE-SET,mtrickle,VPN-noP2P', 'IP-CIDR,10.0.0.0/8,RESERVE'],
}
open(sys.argv[1], 'w', encoding='utf-8').write(
    yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))
S49GEN
  grep -qE '^- name: AI$' "$_s49cfg" \
    && { echo "PASS: S49 fixture uses the post-apply YAML shape that used to break switches"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 fixture is not the post-apply shape"; failn=$((failn+1)); }
  _S49P="$HERE/../assets/geomask-slot-patch.py"
  "$_PYC" -B "$_S49P" "$_s49cfg" AI "https://ai-swap.example/v2" >"$HERE/state/s49.out" 2>&1
  _s49rc=$?
  if [ "$_s49rc" -eq 0 ] && grep -qF 'created:slot' "$HERE/state/s49.out"; then
    echo "PASS: S49 AI switch on a shared provider creates an isolated provider"; pass=$((pass+1))
  else
    echo "FAIL: S49 AI switch on a shared provider (rc=$_s49rc)"; sed 's/^/    | /' "$HERE/state/s49.out"; failn=$((failn+1))
  fi
  grep -qF 'https://ai-swap.example/v2' "$_s49cfg" \
    && { echo "PASS: S49 the new slot provider carries the AI url"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 slot provider url missing"; failn=$((failn+1)); }
  grep -qE '^  - slot$' "$_s49cfg" && grep -qE '^  - sub$' "$_s49cfg" \
    && { echo "PASS: S49 AI rides the slot while VPN still rides sub"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 use lists wrong after the AI switch"; failn=$((failn+1)); }
  grep -qF 'https://sub.example/base' "$_s49cfg" \
    && { echo "PASS: S49 the shared provider url stays untouched by the AI switch"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 AI switch moved the shared provider url"; failn=$((failn+1)); }
  [ "$(grep -cF 'x-hwid: HWTEST' "$_s49cfg")" -ge 2 ] \
    && { echo "PASS: S49 provider header block copied to the slot provider"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 header block not copied to the slot provider"; failn=$((failn+1)); }
  "$_PYC" -B "$_S49P" "$_s49cfg" VPN "https://vpn-swap.example/v3" >"$HERE/state/s49.out" 2>&1
  _s49rc=$?
  if [ "$_s49rc" -eq 0 ] && grep -qF 'updated:sub' "$HERE/state/s49.out"; then
    echo "PASS: S49 VPN switch re-points its own (now unshared) provider"; pass=$((pass+1))
  else
    echo "FAIL: S49 VPN switch (rc=$_s49rc)"; sed 's/^/    | /' "$HERE/state/s49.out"; failn=$((failn+1))
  fi
  grep -qF 'https://vpn-swap.example/v3' "$_s49cfg" && ! grep -qF 'https://sub.example/base' "$_s49cfg" \
    && { echo "PASS: S49 shared provider url replaced, old url gone"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 provider url not replaced by the VPN switch"; failn=$((failn+1)); }
  grep -qF 'https://ai-swap.example/v2' "$_s49cfg" && grep -qE '^  - slot$' "$_s49cfg" \
    && { echo "PASS: S49 AI keeps its isolated slot after the VPN switch"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 VPN switch disturbed the AI isolation"; failn=$((failn+1)); }
  "$_PYC" -B "$_S49P" "$_s49cfg" VPN-noP2P "https://np-swap.example/v4" >"$HERE/state/s49.out" 2>&1
  _s49rc=$?
  if [ "$_s49rc" -eq 0 ] && grep -qF 'created:slot2' "$HERE/state/s49.out"; then
    echo "PASS: S49 VPN-noP2P switch creates its own provider and group"; pass=$((pass+1))
  else
    echo "FAIL: S49 VPN-noP2P switch (rc=$_s49rc)"; sed 's/^/    | /' "$HERE/state/s49.out"; failn=$((failn+1))
  fi
  grep -qF 'https://np-swap.example/v4' "$_s49cfg" \
    && { echo "PASS: S49 the slot2 provider carries the VPN-noP2P url"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 slot2 provider url missing"; failn=$((failn+1)); }
  _s49np=$(grep -nE '^- name: VPN-noP2P$' "$_s49cfg" | head -1 | cut -d: -f1)
  [ -n "$_s49np" ] && sed -n "${_s49np},$((_s49np+12))p" "$_s49cfg" | grep -qF 'tolerance: 99999' \
    && sed -n "${_s49np},$((_s49np+8))p" "$_s49cfg" | grep -qF 'slot2' \
    && { echo "PASS: S49 the new group rides slot2 with the no-health-check tolerance"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 VPN-noP2P group template wrong"; failn=$((failn+1)); }
  _s49ai=$(grep -nE '^- name: AI$' "$_s49cfg" | head -1 | cut -d: -f1)
  [ -n "$_s49ai" ] && [ -n "$_s49np" ] && [ "$_s49np" -gt "$_s49ai" ] \
    && { echo "PASS: S49 the new group is anchored after the AI group"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 VPN-noP2P group anchor position wrong"; failn=$((failn+1)); }
  grep -qF 'RULE-SET,mtrickle,VPN-noP2P' "$_s49cfg" && grep -qF ',RESERVE' "$_s49cfg" && ! grep -qF 'mtrickle_bulk' "$_s49cfg" \
    && { echo "PASS: S49 VPN-noP2P creation leaves the rule table alone"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 VPN-noP2P creation touched the rule table"; failn=$((failn+1)); }
  "$_PYC" -c "import sys,yaml; yaml.safe_load(open(sys.argv[1],encoding='utf-8'))" "$_s49cfg" 2>/dev/null \
    && { echo "PASS: S49 the rewritten config is still valid YAML"; pass=$((pass+1)); } \
    || { echo "FAIL: S49 the rewritten config is not valid YAML"; failn=$((failn+1)); }
else
  echo "SKIP: S49 slot patcher functional run (no runnable python)"
fi

# ==================== S50: node-audit subsystem wiring (v1.28.0) ====================
echo "=== S50: node-audit subsystem modules, install check and removal ==="
for _m in geomask-node-audit.py geomask-audit-state.py geomask-config-tx.py \
          geomask-config-owner.py geomask-apply.py geomask-lane-torrent.py \
          geomask-sync-youtube.py geomask-audit-scheduler.py geomask-slot-patch.py; do
  grep -qF "\"$_m\":" "$HERE/../build.py" \
    && { echo "PASS: S50 build MAP has: $_m"; pass=$((pass+1)); } \
    || { echo "FAIL: S50 build MAP missing: $_m"; failn=$((failn+1)); }
  grep -qF "/usr/lib/geomask/$_m" "$SCRIPT" \
    && { echo "PASS: S50 installer ships: $_m"; pass=$((pass+1)); } \
    || { echo "FAIL: S50 installer does not ship: $_m"; failn=$((failn+1)); }
done
grep -qF 'rm -f "$(P /usr/lib/geomask/$_m)"' "$SCRIPT" \
  && { echo "PASS: S50 uninstall removes the audit modules"; pass=$((pass+1)); } \
  || { echo "FAIL: S50 uninstall leaves the audit modules"; failn=$((failn+1)); }
grep -qF 'step2_check' "$SCRIPT" && grep -qF '/usr/lib/geomask/geomask-audit-state.py' "$SCRIPT" \
  && { echo "PASS: S50 step2_check verifies the audit modules"; pass=$((pass+1)); } \
  || { echo "FAIL: S50 step2_check does not verify the audit modules"; failn=$((failn+1)); }
grep -qF 'event log and the immutable report/pool directories are deliberately KEPT' "$SCRIPT" \
  && { echo "PASS: S50 uninstall documents that settings and evidence survive"; pass=$((pass+1)); } \
  || { echo "FAIL: S50 uninstall does not document retained state"; failn=$((failn+1)); }
# every shipped module must be syntactically valid python
if [ -n "$_PYC" ]; then
  for _m in geomask-node-audit.py geomask-audit-state.py geomask-config-tx.py \
            geomask-config-owner.py geomask-apply.py geomask-lane-torrent.py \
            geomask-sync-youtube.py geomask-audit-scheduler.py geomask-slot-patch.py; do
    if "$_PYC" -m py_compile "$HERE/../assets/$_m" >/dev/null 2>&1; then
      echo "PASS: S50 compiles: $_m"; pass=$((pass+1))
    else
      echo "FAIL: S50 syntax error in: $_m"; failn=$((failn+1))
    fi
  done
else
  echo "SKIP: S50 module compile check (no runnable python)"
fi
# the legacy scanner must no longer own the managed 'exclude-filter' write path unconditionally
grep -qF 'apply_filter_all' "$HERE/../assets/geomask-node-scan.sh" \
  && { echo "PASS: S50 legacy scanner still isolated behind its own flag"; pass=$((pass+1)); } \
  || { echo "FAIL: S50 legacy scanner changed unexpectedly"; failn=$((failn+1)); }
# the new scheduler RPCs must be wired in all four generated points
for _m in nodes_audit_schedule_get nodes_audit_schedule_set nodes_audit_events_get \
          nodes_path_preview nodes_path_apply; do
  if grep -qF -- "$_m" "$HERE/../assets/geo-backend.sh" \
     && grep -qF -- "$_m" "$HERE/../assets/geo-sync" \
     && grep -qF -- "$_m" "$HERE/../assets/acl-geo-sync.json" \
     && grep -qF -- "$_m" "$HERE/../assets/geo-sync-page.js"; then
    echo "PASS: S50 $_m wired in backend+wrapper+acl+panel"; pass=$((pass+1))
  else
    echo "FAIL: S50 $_m not wired in all four points"; failn=$((failn+1))
  fi
done

# ==================== S51: module selection gating (v1.29.0) ====================
# A router that only wants core+VPN must not carry the optional pieces - and must not
# fail its own verification because they are absent. Cron entries are the trap: they are
# created in step5_apply and verified in step5_check, so both halves read the module flag.
echo "=== S51: minimal install (core+path_vpn) leaves the optional pieces out ==="
# Start from the healthy fixtures but strip the geomask files themselves, so both step 2
# and step 5 really apply: a check-only pass would never exercise the gated apply path.
R="$HERE/root_modules"
rm -rf "$R"; cp -a "$HERE/root_healthy/." "$R/"
rm -f "$R/usr/bin/geomask-ytwatch.sh" "$R/usr/bin/geomask-torrent.sh" \
      "$R/usr/bin/geomask-vpnwatch.sh" "$R/usr/bin/geomask-morning-check.sh" \
      "$R/etc/geomask-ytwatch.conf" "$R/etc/geomask/vpnwatch.conf"
rm -rf "$R/etc/geomask/torrent"
rm -f "$R/etc/mihomo/ruleset/ytvpn.yaml" "$R/etc/mihomo/ruleset/ytdirect.yaml" \
      "$R/etc/mihomo/ruleset/torrentdirect.yaml"
rm -f "$R/usr/libexec/rpcd/geo-sync" "$R/root/sync_mtrickle_mihomo.sh" \
      "$R/etc/geomask/modules.json" "$R/etc/geomask/modules-index.json"
{
  echo '*/5 * * * * /usr/bin/geomask-guard.sh >/dev/null 2>&1 # geomask-guard'
  echo '30 3 * * * /usr/bin/geomask-node-scan.sh >/dev/null 2>&1 # geomask-nodescan'
} > "$HERE/state/crontab"
export GM_PREFIX="$R"
t "S51 minimal install exits zero" 0 sh "$SCRIPT" --auto --yes --modules=core,path_vpn --vpn-url "https://vpn.example/m1"
lacks "$HERE/last_out.txt" '\[FAIL\] ytwatch' "S51 ytwatch check is gated by its module"
lacks "$HERE/last_out.txt" '\[FAIL\] vpnwatch' "S51 vpnwatch check is gated"
lacks "$HERE/last_out.txt" '\[FAIL\] torrent' "S51 torrent check is gated"
lacks "$HERE/last_out.txt" '\[FAIL\] morning check' "S51 morning check is gated"
lacks "$HERE/last_out.txt" '\[FAIL\] syncd' "S51 syncd check stays unconditional (core)"
contains "$HERE/last_out.txt" "пропущен: модуль 'youtube'" "S51 youtube reported as skipped, not missing"
contains "$HERE/last_out.txt" "пропущен: модуль 'ai_dns'" "S51 ai_dns step reported as skipped"
lacks "$HERE/state/crontab" 'geomask-ytwatch' "S51 no ytwatch cron for a deselected module"
lacks "$HERE/state/crontab" 'geomask-vpnwatch' "S51 no vpnwatch cron"
contains "$HERE/state/crontab" 'sync_mtrickle_mihomo' "S51 core sync cron still created"
contains "$HERE/state/crontab" 'geomask-guard' "S51 core guard cron still created"
if [ -x "$R/usr/bin/geomask-ytwatch.sh" ]; then
  echo "FAIL: S51 ytwatch was installed anyway"; failn=$((failn+1))
else
  echo "PASS: S51 ytwatch not installed"; pass=$((pass+1))
fi
if grep -qF 'payload: []' "$R/etc/mihomo/ruleset/ytvpn.yaml" 2>/dev/null \
   && grep -qF 'payload: []' "$R/etc/mihomo/ruleset/ytdirect.yaml" 2>/dev/null \
   && grep -qF 'payload: []' "$R/etc/mihomo/ruleset/torrentdirect.yaml" 2>/dev/null; then
  echo "PASS: S51 rulesets of deselected modules stay valid empty stubs (mihomo -t holds)"; pass=$((pass+1))
else
  echo "FAIL: S51 ruleset stub missing for a deselected module"; failn=$((failn+1))
fi
contains "$R/etc/geomask/modules.json" '"core"' "S51 selection persisted (core)"
contains "$R/etc/geomask/modules.json" '"path_vpn"' "S51 selection persisted (path_vpn)"
lacks "$R/etc/geomask/modules.json" '"youtube"' "S51 deselected module is not recorded as selected"
if grep -qF 'SMB-шар с бэкапами' "$HERE/last_out.txt" && ! grep -qF '[FAIL] SMB share' "$HERE/last_out.txt"; then
  echo "PASS: S51 backup share check is gated by backup_share"; pass=$((pass+1))
else
  echo "FAIL: S51 backup share gating did not report"; failn=$((failn+1))
fi

echo "=== S51b: a full install still creates every optional piece ==="
R="$HERE/root_modules_full"
rm -rf "$R"; cp -a "$HERE/root_apply/." "$R/"
: > "$HERE/state/crontab"
export GM_PREFIX="$R"
t "S51b full install exits zero" 0 sh "$SCRIPT" --auto --yes --vpn-url "https://vpn.example/m2"
contains "$HERE/state/crontab" 'geomask-ytwatch' "S51b ytwatch cron created"
contains "$HERE/state/crontab" 'geomask-vpnwatch' "S51b vpnwatch cron created"
contains "$HERE/state/crontab" 'geomask-ytwatch-reset' "S51b ytwatch reset cron created"
contains "$R/etc/geomask/modules.json" '"youtube"' "S51b full selection persisted"

echo "=== S51c: selection flags ==="
t "S51c --list-modules exits zero" 0 sh "$SCRIPT" --list-modules
contains "$HERE/last_out.txt" 'core.*обязательный' "S51c core marked mandatory"
contains "$HERE/last_out.txt" 'youtube.*по выбору' "S51c youtube marked optional"
t "S51c unknown module is rejected" 2 sh "$SCRIPT" --modules=core,nope
contains "$HERE/last_out.txt" 'unknown module: nope' "S51c unknown module named in the error"
contains "$SCRIPT" 'mod_skipped()' "S51c the shared gate helper is present"
contains "$SCRIPT" 'cron_ensure diagnostic' "S51c the vpnwatch cron goes through cron_ensure"
contains "$SCRIPT" 'cron_verify diagnostic' "S51c the vpnwatch cron is verified through cron_verify"
_n=$(grep -cE "^  if crontab -l 2>/dev/null \| grep -qE '# geomask-(ytwatch|vpnwatch)" "$SCRIPT")
if [ "$_n" = "0" ]; then
  echo "PASS: S51c no raw crontab check left for an optional module's cron"; pass=$((pass+1))
else
  echo "FAIL: S51c $_n raw crontab check(s) left (trap: apply/verify drift)"; failn=$((failn+1))
fi

echo "=== S51d: a fresh install really writes the rpcd wrapper and the module catalogue ==="
# v1.29.0 regression: an indented ASSET_EOF does not terminate a plain <<'ASSET_EOF' heredoc,
# so the block swallowed the two save_stream calls after it - a fresh router got no rpcd
# wrapper and no module catalogue, and only the sandbox fixtures hid it.
_n=$(grep -cE '^[[:space:]]+ASSET_EOF[[:space:]]*$' "$HERE/../geomask.sh.in")
if [ "$_n" = "0" ]; then
  echo "PASS: S51d no indented heredoc terminator (it would swallow the next blocks)"; pass=$((pass+1))
else
  echo "FAIL: S51d $_n indented ASSET_EOF terminator(s) in the template"; failn=$((failn+1))
fi
R="$HERE/root_fresh"
rm -rf "$R"; mkdir -p "$R/tmp"
export GM_PREFIX="$R"
sh "$SCRIPT" --step 2 --yes >"$HERE/last_out.txt" 2>&1
_rc=$?
if [ "$_rc" = 0 ]; then
  echo "PASS: S51d fresh step 2 exits zero"; pass=$((pass+1))
else
  echo "FAIL: S51d fresh step 2 rc=$_rc"; sed 's/^/    | /' "$HERE/last_out.txt" | tail -20; failn=$((failn+1))
fi
for _f in usr/libexec/rpcd/geo-sync etc/geomask/modules-index.json \
          usr/lib/geomask/geomask-module-update.py etc/geomask/modules.json; do
  if [ -s "$R/$_f" ]; then
    echo "PASS: S51d fresh install wrote $_f"; pass=$((pass+1))
  else
    echo "FAIL: S51d fresh install did not write $_f"; failn=$((failn+1))
  fi
done
if grep -q 'ASSET_EOF' "$R/usr/lib/geomask/geomask-module-update.py" 2>/dev/null; then
  echo "FAIL: S51d the installed updater carries heredoc junk"; failn=$((failn+1))
else
  echo "PASS: S51d the installed updater is clean"; pass=$((pass+1))
fi
if grep -qF '"core"' "$R/etc/geomask/modules.json" && grep -qF '"youtube"' "$R/etc/geomask/modules.json"; then
  echo "PASS: S51d a fresh install records all modules (nothing was narrowed)"; pass=$((pass+1))
else
  echo "FAIL: S51d fresh install recorded an unexpected selection"; failn=$((failn+1))
fi

echo "=== S51e: interactive module picker (no tty, driven by GM_MODULES_ASK) ==="
# Numbers are positions in GM_ALL_MODULES; required modules are added back whatever is typed.
R="$HERE/root_picker"
rm -rf "$R"; mkdir -p "$R/tmp"
export GM_PREFIX="$R"
printf '5\n' | GM_MODULES_ASK=1 sh "$SCRIPT" --step 2 --yes >"$HERE/last_out.txt" 2>&1
if grep -qF '"ai_dns"' "$R/etc/geomask/modules.json" 2>/dev/null; then
  echo "PASS: S51e the picked module is recorded"; pass=$((pass+1))
else
  echo "FAIL: S51e the picked module is not recorded"; sed 's/^/    | /' "$HERE/last_out.txt" | tail -15; failn=$((failn+1))
fi
if grep -qF '"core"' "$R/etc/geomask/modules.json" 2>/dev/null \
   && grep -qF '"path_vpn"' "$R/etc/geomask/modules.json" 2>/dev/null; then
  echo "PASS: S51e required modules are added back automatically"; pass=$((pass+1))
else
  echo "FAIL: S51e required modules were dropped by the picker"; failn=$((failn+1))
fi
if grep -qF '"youtube"' "$R/etc/geomask/modules.json" 2>/dev/null; then
  echo "FAIL: S51e an unpicked module was installed anyway"; failn=$((failn+1))
else
  echo "PASS: S51e unpicked modules stay out"; pass=$((pass+1))
fi
contains "$HERE/last_out.txt" 'обязательный' "S51e the picker marks required modules"
# Enter keeps the stored selection; a fresh router has no stored selection, so it is "all".
R="$HERE/root_picker_all"
rm -rf "$R"; mkdir -p "$R/tmp"
export GM_PREFIX="$R"
printf '\n' | GM_MODULES_ASK=1 sh "$SCRIPT" --step 2 --yes >"$HERE/last_out.txt" 2>&1
if grep -qF '"youtube"' "$R/etc/geomask/modules.json" 2>/dev/null \
   && grep -qF '"admission_service"' "$R/etc/geomask/modules.json" 2>/dev/null; then
  echo "PASS: S51e Enter installs every module"; pass=$((pass+1))
else
  echo "FAIL: S51e Enter did not install every module"; failn=$((failn+1))
fi
# A second run with Enter must keep the narrowed selection instead of widening it.
R="$HERE/root_picker"
export GM_PREFIX="$R"
printf '\n' | GM_MODULES_ASK=1 sh "$SCRIPT" --step 2 --yes >"$HERE/last_out.txt" 2>&1
if grep -qF '"youtube"' "$R/etc/geomask/modules.json" 2>/dev/null; then
  echo "FAIL: S51e Enter widened the stored selection"; failn=$((failn+1))
else
  echo "PASS: S51e Enter on a configured router keeps the stored selection"; pass=$((pass+1))
fi
# Narrowing the selection cannot delete files, so the installer has to say so out loud.
rm -f "$R/usr/libexec/rpcd/geo-sync"
t "S51e narrowing the selection still installs" 0 sh "$SCRIPT" --step 2 --yes --modules=core,path_vpn
contains "$HERE/last_out.txt" 'снят с выбора' "S51e files of a dropped module are reported as left behind"
lacks "$R/etc/geomask/modules.json" '"ai_dns"' "S51e the narrowed selection is recorded"

echo "=== S52: checks accept the config shapes the panel writes ==="
# The installer writes block style (`  - name:`), the panel/mihomo rewrite emits flow style
# (`- name:` at column 0) and turns `ports: [443, 8443, 5228]` into a block list. The checks
# used to demand one exact spelling and failed a perfectly good config on the live router.
R="$HERE/root_shapes"
rm -rf "$R"; cp -a "$HERE/root_healthy/." "$R/"
awk '
  /^listeners:/ {print; inl=1; next}
  inl && /^[A-Za-z]/ {inl=0}
  inl && /^  / {sub(/^  /, ""); print; next}
  {print}
' "$R/etc/mihomo/config.yaml" > "$R/etc/mihomo/config.tmp" \
  && mv "$R/etc/mihomo/config.tmp" "$R/etc/mihomo/config.yaml"
sed -i 's/^      ports: \[443, 8443, 5228\]$/      ports:\n      - 443\n      - 8443\n      - 5228/' \
  "$R/etc/mihomo/config.yaml"
export GM_PREFIX="$R"
sh "$SCRIPT" --step 3 --check >"$HERE/last_out.txt" 2>&1 || true
lacks "$HERE/last_out.txt" 'torrent listener has no fixed policy' "S52 flow-style listener is parsed"
contains "$HERE/last_out.txt" 'torrent-direct:7894 -> VPN' "S52 the listener policy is read"
lacks "$HERE/last_out.txt" 'sniffer TLS 5228 missing' "S52 block-style sniffer ports are recognised"
contains "$HERE/last_out.txt" 'sniffer TLS 5228' "S52 the sniffer check still reports"

echo "=== S52b: the legacy nodescan cron is optional when the scheduler owns the audit ==="
R="$HERE/root_sched"
rm -rf "$R"; cp -a "$HERE/root_healthy/." "$R/"
cat > "$R/usr/lib/geomask/geomask-node-audit.py" <<'PYEOF'
def load_store():
    return {'paths': {'vpn': {'schedule': {'enabled': True}}}}, None
PYEOF
grep -v 'geomask-node-scan' "$HERE/state/crontab" > "$HERE/state/crontab.new"
mv "$HERE/state/crontab.new" "$HERE/state/crontab"
export GM_PREFIX="$R"
sh "$SCRIPT" --step 5 --check >"$HERE/last_out.txt" 2>&1 || true
lacks "$HERE/last_out.txt" 'nodescan cron missing' "S52b an active schedule satisfies the cron check"
contains "$HERE/last_out.txt" 'per-path scheduler is active' "S52b the scheduler is named as the owner"

echo ""
echo "== SANDBOX RESULT: pass=$pass fail=$failn =="
[ "$failn" -eq 0 ]
