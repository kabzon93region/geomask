"""R10a / D1-D8: the guard entrypoint must run every independent block and tell the truth.

`geomask-guard.sh:20-22` exited 0 before anything else when `/etc/geo-devices.list` was
missing; on a fresh system the installer does not create it, so the console watchdog, table
101 and the base rules never ran (P1: rc=0, zero `ip`/`nft` calls). The rest of the defects
follow the same pattern: table 101 was "healthy" on any `^default` line (a WAN default passed,
P3), the LAN device was hardcoded `br-lan` (P4 re-seated rules on every run of a `br-lan.1`
system), every failure still ended in `exit 0`, and a missing lock primitive was a green SKIP.

These tests drive the WHOLE script (not the `(E)` block) through PATH stubs, with every host
path injected: GM_DEVICES, GM_CONSOLE_STATE, GM_CONSOLE_LOCK, GM_LOCK_DIR,
GM_ROUTE_STATE_DIR, GM_ROUTES_LIB, GM_CONSOLE_TABLE/TUN. No `/etc`, no host `/tmp`, no
router access. Exit contract under test: 0 healthy/repaired, 1 degraded/unrepaired (TUN down
included), 2 input/lock unknown, 75 console lane busy.
"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'assets' / 'geomask-guard.sh'
LOCKLIB = ROOT / 'assets' / 'geomask-lock.sh'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def win(path):
    return str(Path(path).resolve()).replace('\\', '/')


IP_STUB = r'''#!/bin/sh
printf '%s\n' "$*" >> "$CASE/ip.calls"
case "$1 $2" in
  "rule show")
    cat "$CASE/rules" 2>/dev/null
    exit 0 ;;
  "rule add")
    shift 2
    _pref=''
    case "${1:-}" in
      priority|pref) _pref=$2; shift 2 ;;
    esac
    _rest="$*"
    if [ -f "$CASE/fail-rule-add" ] && grep -qF "$_rest" "$CASE/fail-rule-add" 2>/dev/null; then
      exit 1
    fi
    _norm=$(printf '%s' "$_rest" | sed 's/ table / lookup /g')
    printf '%s:\t%s\n' "${_pref:-0}" "$_norm" >> "$CASE/ip.adds"
    printf '%s:\t%s\n' "${_pref:-0}" "$_norm" >> "$CASE/rules"
    exit 0 ;;
  "rule del")
    shift 2
    _pref=''
    if [ "${1:-}" = pref ]; then _pref=$2; shift 2; fi
    _rest=$(printf '%s' "$*" | sed 's/ table / lookup /g')
    awk -v pref="$_pref" -v pat="$_rest" '
      BEGIN { n=split(pat, t, " "); done=0 }
      {
        hit=1
        if (pref != "" && index($0, pref ":") != 1) hit=0
        for (i=1; i<=n; i++) if (hit && index($0, t[i]) == 0) hit=0
        if (hit && !done) { done=1; next }
        print
      }
      END { exit(done ? 0 : 1) }
    ' "$CASE/rules" > "$CASE/rules.new"
    _rc=$?
    [ -f "$CASE/rules.new" ] && mv -f "$CASE/rules.new" "$CASE/rules"
    exit "$_rc" ;;
  "route show")
    case "$*" in
      *"table 101"*) cat "$CASE/tbl-101" 2>/dev/null ;;
      *"table 102"*) cat "$CASE/tbl-102" 2>/dev/null ;;
      *) cat "$CASE/route-default" 2>/dev/null ;;
    esac
    exit 0 ;;
  "route replace")
    shift 2
    _tbl=main
    _rest=''
    while [ $# -gt 0 ]; do
      case "$1" in
        table) _tbl=$2; shift 2 ;;
        *) _rest="${_rest:+$_rest }$1"; shift ;;
      esac
    done
    [ -f "$CASE/fail-route-$_tbl" ] && exit 1
    _pfx=${_rest%% *}
    if [ -f "$CASE/tbl-$_tbl" ]; then
      awk -v p="$_pfx" '$1 != p' "$CASE/tbl-$_tbl" > "$CASE/tbl-$_tbl.new" \
        && mv -f "$CASE/tbl-$_tbl.new" "$CASE/tbl-$_tbl"
    fi
    printf '%s\n' "$_rest" >> "$CASE/tbl-$_tbl"
    exit 0 ;;
  "route del")
    shift 2
    _tbl=main
    _rest=''
    while [ $# -gt 0 ]; do
      case "$1" in
        table) _tbl=$2; shift 2 ;;
        *) _rest="${_rest:+$_rest }$1"; shift ;;
      esac
    done
    _pfx=${_rest%% *}
    if [ -f "$CASE/tbl-$_tbl" ] \
       && awk -v p="$_pfx" '$1 == p { found=1 } END { exit(found ? 0 : 1) }' "$CASE/tbl-$_tbl"; then
      awk -v p="$_pfx" '$1 != p' "$CASE/tbl-$_tbl" > "$CASE/tbl-$_tbl.new" \
        && mv -f "$CASE/tbl-$_tbl.new" "$CASE/tbl-$_tbl"
      exit 0
    fi
    exit 1 ;;
esac
exit 0
'''

NFT_STUB = r'''#!/bin/sh
printf '%s\n' "$*" >> "$CASE/nft.calls"
case "$*" in
  "-a list chain ip mangle PREROUTING") cat "$CASE/nft-chain" 2>/dev/null ;;
  "list chain ip mangle PREROUTING") cat "$CASE/nft-chain" 2>/dev/null ;;
  "list table ip geomask_excl") exit 0 ;;
  "list chain ip geomask_excl prerouting")
    printf 'chain prerouting {\n  ip saddr @excl_ips counter meta mark set 0\n}\n' ;;
  "list set ip geomask_excl excl_ips") cat "$CASE/nft-set" 2>/dev/null ;;
esac
exit 0
'''

UCI_STUB = r'''#!/bin/sh
printf '%s\n' "$*" >> "$CASE/uci.calls"
case "$*" in
  "get network.lan.device")
    [ -f "$CASE/lan.device" ] || exit 1
    cat "$CASE/lan.device"; exit 0 ;;
  "get network.lan.ifname") exit 1 ;;
esac
exit 1
'''

LOGGER_STUB = r'''#!/bin/sh
printf '%s\n' "$*" >> "$CASE/logger.log"
'''

CONNTRACK_STUB = r'''#!/bin/sh
printf '%s\n' "$*" >> "$CASE/conntrack.calls"
exit 0
'''

ROUTES_STUB = r'''gm_route_drain() {
  printf 'drain\n' >> "$CASE/drain.log"
}
geomask_routes_sync() {
  printf '%s\n' "$1" >> "$CASE/sync.log"
  return 0
}
'''

BASE_RULES = ('19901:\tiif br-lan ipproto icmp lookup main\n'
              '19902:\tfwmark 0x64 lookup 101\n')


@unittest.skipIf(BASH is None, 'no-bash')
class GuardEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.bin = self.case / 'bin'
        self.bin.mkdir()
        for name, body in (('ip', IP_STUB), ('nft', NFT_STUB), ('uci', UCI_STUB),
                           ('logger', LOGGER_STUB), ('conntrack', CONNTRACK_STUB)):
            self.write_bin(name, body)
        self.routes = self.write('routes-stub.sh', ROUTES_STUB)
        self.devices = self.write('geo-devices.list', '')
        self.console = self.write('console.state', 'enabled=0\nip=\n')
        self.write('rules', BASE_RULES)
        self.write('tbl-101', 'default dev AI\n')
        self.write('tbl-102', 'default dev Console\n')
        self.write('nft-chain', 'chain PREROUTING {\n  jump MT_abc\n}\n')
        self.write('nft-set', 'elements = { }\n')
        self.write('lan.device', 'br-lan\n')
        for name in ('logger.log', 'ip.calls', 'ip.adds', 'nft.calls',
                     'conntrack.calls', 'uci.calls', 'drain.log', 'sync.log'):
            self.write(name, '')

    def write(self, name, text):
        path = self.case / name
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def write_bin(self, name, text):
        path = self.bin / name
        path.write_text(text, encoding='utf-8', newline='\n')
        os.chmod(path, 0o755)
        return path

    def read(self, name):
        path = self.case / name
        if not path.exists():
            return ''
        return path.read_text(encoding='utf-8', errors='replace')

    def lines(self, name):
        return [line for line in self.read(name).splitlines() if line.strip()]

    def environment(self, **overrides):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('GM_')}
        env.pop('MSYS', None)
        env['CASE'] = win(self.case)
        env['PATH'] = str(self.bin) + os.pathsep + env.get('PATH', '')
        env['GM_DEVICES'] = win(self.devices)
        env['GM_CONSOLE_STATE'] = win(self.console)
        env['GM_CONSOLE_LOCK'] = win(self.case / 'locks' / 'console.lock')
        env['GM_LOCK_DIR'] = win(self.case / 'locks')
        env['GM_LOCK_LIB'] = win(LOCKLIB)
        env['GM_ROUTES_LIB'] = win(self.routes)
        env['GM_ROUTE_STATE_DIR'] = win(self.case / 'route-state')
        env['GM_CONSOLE_TUN'] = 'Console'
        env['GM_CONSOLE_TABLE'] = '102'
        env.update(overrides)
        return env

    def run_guard(self, **overrides):
        return subprocess.run([BASH, win(GUARD)], cwd=str(self.case),
                              env=self.environment(**overrides),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding='utf-8', errors='replace',
                              timeout=180)

    def hold_console_lock(self):
        holder = self.write('holder.sh', (
            '#!/bin/sh\n'
            'GM_LOCK_DIR="%s"\n'
            'GM_LOCK_LIB="%s"\n'
            '. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire console 0 "%s" || exit 9\n'
            ': > "%s"\n'
            'sleep 30\n'
            % (win(self.case / 'locks'), win(LOCKLIB),
               win(self.case / 'locks' / 'console.lock'), win(self.case / 'held'))))
        proc = subprocess.Popen([BASH, win(holder)], cwd=str(self.case),
                                env=self.environment())
        self.addCleanup(self.reap, proc)
        for _ in range(200):
            if (self.case / 'held').exists():
                return
            time.sleep(0.05)
        self.fail('the console lock holder never started')

    @staticmethod
    def reap(proc):
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass

    # ---------------------------------------------------------------- D1: no list
    def test_console_is_repaired_without_a_device_list(self):
        self.devices.unlink()
        self.write('console.state', 'enabled=1\nip=10.20.30.40\n')
        result = self.run_guard()
        calls = '\n'.join(self.lines('ip.calls'))
        self.assertIn('rule add pref 19903 from 10.20.30.40 lookup 102', calls,
                      'the console rule was not repaired without a device list:\n'
                      + result.stdout + '\n' + self.read('logger.log'))
        self.assertIn('UNKNOWN', self.read('logger.log'),
                      'a missing device list was not reported as unknown')
        self.assertTrue(self.lines('ip.calls'),
                        'the independent base-rule block did not run')
        self.assertIn('drain', self.read('drain.log'),
                      'the R05 drain did not run without a device list')
        self.assertEqual(result.returncode, 2, result.stdout + self.read('logger.log'))

    def test_empty_device_list_is_healthy(self):
        result = self.run_guard()
        self.assertEqual(result.returncode, 0,
                         result.stdout + '\n' + self.read('logger.log'))
        self.assertNotIn('DEGRADED', self.read('logger.log'))

    # ---------------------------------------------------------------- D1: unreadable
    def test_unreadable_device_list_fails_closed(self):
        directory = self.case / 'devices-as-directory'
        directory.mkdir()
        result = self.run_guard(GM_DEVICES=win(directory))
        self.assertEqual(result.returncode, 2, result.stdout + self.read('logger.log'))
        self.assertIn('readable regular file', self.read('logger.log'))
        self.assertEqual(self.lines('ip.adds'), [],
                         'the guard mutated routing without a readable device list')
        self.assertFalse((self.case / 'ip.calls').exists()
                         and self.read('ip.calls').strip(),
                         'the guard called ip without a readable device list')

    # ---------------------------------------------------------------- D2: table 101
    def test_a_wan_default_in_table_101_is_replaced(self):
        self.write('geo-devices.list', '10.0.0.5 ai phone\n')
        self.write('tbl-101', 'default via 192.168.1.1 dev pppoe-wan\n')
        result = self.run_guard()
        self.assertEqual(self.lines('tbl-101'), ['default dev AI'],
                         'the WAN default survived: the AI lane is still bypassed')
        self.assertIn('table 101 default restored', self.read('logger.log'))
        self.assertEqual(result.returncode, 0, result.stdout + self.read('logger.log'))

    def test_missing_ai_default_is_deleted_and_reported_down(self):
        self.write('geo-devices.list', '10.0.0.5 ai phone\n')
        self.write('tbl-101', 'default via 192.168.1.1 dev pppoe-wan\n')
        (self.case / 'fail-route-101').write_text('1\n', encoding='utf-8')
        result = self.run_guard()
        self.assertEqual(self.lines('tbl-101'), [],
                         'a foreign default was left in table 101 while the AI tun is down')
        self.assertIn('runtime=down', self.read('logger.log'))
        self.assertEqual(result.returncode, 1, result.stdout + self.read('logger.log'))

    # ---------------------------------------------------------------- D3: LAN device
    def test_lan_device_comes_from_uci(self):
        self.write('lan.device', 'br-lan.1\n')
        self.write('rules', '')
        result = self.run_guard()
        adds = '\n'.join(self.lines('ip.adds'))
        self.assertIn('iif br-lan.1 ipproto icmp lookup main', adds,
                      'the hardcoded br-lan was used instead of the UCI device:\n' + adds)
        self.assertNotIn('iif br-lan ipproto', adds)
        self.assertEqual(result.returncode, 0, result.stdout + self.read('logger.log'))

    # ---------------------------------------------------------------- D5: rc of mutation
    def test_failed_console_rule_add_is_degraded_and_not_logged_as_success(self):
        self.write('console.state', 'enabled=1\nip=10.20.30.40\n')
        self.write('fail-rule-add', 'from 10.20.30.40 lookup 102\n')
        result = self.run_guard()
        log = self.read('logger.log')
        self.assertIn('FAIL console rule add', log, log)
        self.assertNotIn('console rule re-seated', log,
                         'a failed ip rule add was logged as a success')
        self.assertEqual(result.returncode, 1, result.stdout + log)

    # ---------------------------------------------------------------- lock contract
    def test_busy_console_lock_returns_75(self):
        self.hold_console_lock()
        result = self.run_guard()
        self.assertEqual(result.returncode, 75, result.stdout + self.read('logger.log'))
        self.assertIn('busy', self.read('logger.log'))

    def test_missing_lock_primitive_returns_2(self):
        self.write('console.state', 'enabled=1\nip=10.20.30.40\n')
        result = self.run_guard(GM_LOCK_LIB=win(self.case / 'no-such-lock.sh'))
        self.assertEqual(result.returncode, 2, result.stdout + self.read('logger.log'))
        self.assertIn('lock primitive', self.read('logger.log'))
        self.assertEqual(self.lines('ip.adds'), [],
                         'the guard mutated the console rule without a lock primitive')

    # ---------------------------------------------------------------- D6/D7: console
    def test_enabled_console_with_empty_ip_is_degraded(self):
        self.write('console.state', 'enabled=1\nip=\n')
        result = self.run_guard()
        self.assertIn('DEGRADED', self.read('logger.log'),
                      'an enabled console with no address was silently ignored')
        self.assertEqual(result.returncode, 1, result.stdout + self.read('logger.log'))
        self.assertNotIn('pref 19903', '\n'.join(self.lines('ip.adds')))

    def test_console_off_removes_only_the_owned_rule(self):
        self.write('console.state', 'enabled=0\nip=10.20.30.40\n')
        self.write('rules', BASE_RULES
                   + '15000:\tfrom 10.20.30.40 lookup 102\n'
                   + '19903:\tfrom 10.20.30.40 lookup 102\n')
        result = self.run_guard()
        rules = '\n'.join(self.lines('rules'))
        self.assertNotIn('19903:', rules,
                         'the owned console rule survived enabled=0')
        self.assertIn('15000:', rules,
                      'a foreign rule with the same from/lookup was removed')
        self.assertIn('console rule removed', self.read('logger.log'))
        self.assertEqual(result.returncode, 0, result.stdout + self.read('logger.log'))

    def test_console_default_must_point_at_the_console_tun(self):
        self.write('console.state', 'enabled=1\nip=10.20.30.40\n')
        self.write('tbl-102', 'default via 192.168.1.1 dev br-lan\n')
        result = self.run_guard()
        self.assertEqual(self.lines('tbl-102'), ['default dev Console'],
                         'the console table kept a default through another device')
        self.assertIn('default restored', self.read('logger.log'))
        self.assertEqual(result.returncode, 0, result.stdout + self.read('logger.log'))

    def test_console_tun_down_returns_1(self):
        self.write('console.state', 'enabled=1\nip=10.20.30.40\n')
        self.write('tbl-102', '')
        (self.case / 'fail-route-102').write_text('1\n', encoding='utf-8')
        result = self.run_guard()
        self.assertEqual(result.returncode, 1,
                         'a down console tun was reported as healthy:\n'
                         + result.stdout + self.read('logger.log'))
        self.assertIn('tunnel down', self.read('logger.log'))


if __name__ == '__main__':
    unittest.main()
