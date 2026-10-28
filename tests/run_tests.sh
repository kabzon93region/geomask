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
  - name: VPN
  - name: AI
rule-providers:
  ai:
    type: file
  mtrickle:
    type: file
  ytdirect:
    type: file
rules:
  - AND,((NETWORK,UDP),(DST-PORT,443),(DOMAIN-SUFFIX,googlevideo.com)),REJECT
  - AND,((NETWORK,UDP),(DST-PORT,443),(DOMAIN-SUFFIX,youtube.com)),REJECT
  - AND,((NETWORK,UDP),(DST-PORT,443),(DOMAIN-SUFFIX,youtu.be)),REJECT
  - RULE-SET,ytdirect,DIRECT
  - RULE-SET,ai,AI
  - RULE-SET,mtrickle,VPN
  - MATCH,GLOBAL
listeners:
  - name: ai-devices
    type: mixed
    port: 7891
    listen: 127.0.0.1
    proxy: AI
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
contains "$R/etc/mihomo/config.yaml" 'url: "https://ai.example/sub2"' "S3 ai url written"
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
contains "$R/etc/mihomo/config.yaml" 'url: "https://ai.example/p2"' "S7 kept AI url"
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

# ---------- scenario 14: event sync watchdog + stack state (v1.10.0) ----------
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

echo ""
echo "== SANDBOX RESULT: pass=$pass fail=$failn =="
[ "$failn" -eq 0 ]
