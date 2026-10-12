#!/bin/sh
# Sandbox test runner for zm-setup.sh (runs under Git-bash on Windows, no router access).
# Layout:
#   sandbox/bin/*     - stub commands (pidof, ubus, mihomo, curl, crontab, nslookup, ip, wget, apk)
#   sandbox/state/*   - state files consumed by stubs
#   sandbox/root/*    - fake filesystem (ZM_PREFIX)
# Scenarios: S1 empty (all FAIL), S2 healthy (all OK), S3 apply (auto repairs subs).

set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/../zm-setup.sh"
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
export ZM_STATE="$HERE/state"
export ZM_PREFIX="$HERE/root_empty"
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
echo x > "$R/usr/bin/geo-backend.sh"
echo x > "$R/usr/libexec/rpcd/geo-sync"
echo x > "$R/usr/share/rpcd/acl.d/luci-app-geo-sync.json"
mkdir -p "$R/usr/share/luci/menu.d"
echo x > "$R/usr/share/luci/menu.d/luci-app-geo-sync.json"
mkdir -p "$R/www/luci-static/resources/view/geo-sync"; echo x > "$R/www/luci-static/resources/view/geo-sync/main.js"
echo x > "$R/root/sync_mtrickle_mihomo.sh"; chmod +x "$R/root/sync_mtrickle_mihomo.sh"
printf 'payload: []\n' > "$R/etc/mihomo/ruleset/mtrickle.yaml"
printf 'payload: []\n' > "$R/etc/mihomo/ruleset/ai.yaml"
printf 'payload: []\n' > "$R/etc/mihomo/ruleset/ytdirect.yaml"
cat > "$R/etc/mihomo/config.yaml" <<'YAMLEOF'
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
  - RULE-SET,ytdirect,DIRECT
  - RULE-SET,ai,AI
  - RULE-SET,mtrickle,VPN
  - MATCH,GLOBAL
YAMLEOF
# healthy state
printf 'dnsmasq\nmagitrickled\nmihomo\nhev-socks5-tunnel\nrpcd\n' > "$HERE/state/services"
printf 'zapret-manager\ngeo-sync\n' > "$HERE/state/ubus_objects"
echo '*/5 * * * * /root/sync_mtrickle_mihomo.sh' > "$HERE/state/crontab"
echo '32666: from all iif br-lan ipproto icmp lookup main' > "$HERE/state/ip_rules"
: > "$HERE/state/mihomo_t_ok"
export ZM_PREFIX="$HERE/root_healthy"
t "S2 check exits zero" 0 sh "$SCRIPT" --check
contains "$HERE/last_out.txt" "fail=0" "S2 zero failures"

# ---------- scenario 3: broken subs config -> --auto repairs with URLs ----------
echo "=== S3: broken subs, --auto --restart with urls ==="
R="$HERE/root_apply"
cp -a "$HERE/root_healthy/." "$R/"
rm -f "$R/etc/mihomo/config.yaml"   # break it
: > "$HERE/state/mihomo_t_ok"        # stub mihomo -t will pass
export ZM_PREFIX="$HERE/root_apply"
t "S3 auto exits zero" 0 sh "$SCRIPT" --auto --yes --restart --vpn-url "https://vpn.example/sub1" --ai-url "https://ai.example/sub2"
contains "$R/etc/mihomo/config.yaml" 'url: "https://vpn.example/sub1"' "S3 vpn url written"
contains "$R/etc/mihomo/config.yaml" 'url: "https://ai.example/sub2"' "S3 ai url written"
contains "$R/etc/mihomo/config.yaml" "name: VPN" "S3 VPN group written"
contains "$R/etc/mihomo/config.yaml" "RULE-SET,mtrickle,VPN" "S3 mtrickle rule written"
contains "$HERE/last_out.txt" "verify: PASS" "S3 verify passed"

# ---------- scenario 4: help + bad flag ----------
echo "=== S4: CLI edges ==="
t "S4 help exits 0" 0 sh "$SCRIPT" --help
t "S4 bad flag exits 2" 2 sh "$SCRIPT" --wat

echo ""
echo "== SANDBOX RESULT: pass=$pass fail=$failn =="
[ "$failn" -eq 0 ]
