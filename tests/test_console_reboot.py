"""The console lane after a reboot: LAN prefixes in table 102 and a default that is ours.

Review 14-15.11.2026, point 9. The policy rule sends the console device into table 102, and
after a reboot that table starts empty. The guard put back the rule and *a* default, but never
mirrored the LAN prefixes into the table, and its check was `grep '^default'`: the WAN default
netifd restores for every table (via 192.168.1.1, dev br-lan) passed it, so the console kept
leaving with the real address and lost the local network at the same time.

Both halves of the lane are covered here: the ifup hotplug script (so the table is usable
before the guard runs) and the guard's own console block. Every command is a stub inside the
test's temporary directory - no router, no `/etc`, no host `/tmp`.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'assets' / 'geomask-guard.sh'
HOTPLUG = ROOT / 'assets' / '99-default-vpn-route'
GUARD_TEXT = GUARD.read_text(encoding='utf-8')
HOTPLUG_TEXT = HOTPLUG.read_text(encoding='utf-8')

GUARD_START = '# ---------- (E) console lane watchdog ----------'
GUARD_END = '# ---------- (D) AI-DNS self-heal ----------'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def win(path):
    return str(Path(path).resolve()).replace('\\', '/')


def hotplug_function(name):
    """Whole function from the hotplug script, braces at column 0."""
    lines = HOTPLUG_TEXT.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(name + '() {')), None)
    if start is None:
        raise AssertionError('function %s not found in the hotplug script' % name)
    for index in range(start + 1, len(lines)):
        if lines[index] == '}':
            return '\n'.join(lines[start:index + 1])
    raise AssertionError('function %s has no closing brace' % name)


def guard_block():
    start = GUARD_TEXT.index(GUARD_START)
    end = GUARD_TEXT.index(GUARD_END, start)
    return GUARD_TEXT[start:end]


IP_STUB = r'''ip() {
  printf '%s\n' "$*" >> "$CASE/calls.log"
  case "$1 $2" in
    'link show')
      case "$3" in Wireguard|AI|pppoe-wan) return 1 ;; *) return 0 ;; esac ;;
    'rule show')
      cat "$CASE/rules" 2>/dev/null; return 0 ;;
    'rule add')
      printf '%s\n' "$*" >> "$CASE/rules"; return 0 ;;
    'rule del')
      return 1 ;;
    'route show')
      _tbl=main
      [ "${3:-}" = table ] && _tbl=$4
      cat "$CASE/tbl-$_tbl" 2>/dev/null
      return 0 ;;
    'route replace')
      shift 2
      _tbl=main
      if [ "${1:-}" = table ]; then _tbl=$2; shift 2; fi
      _line="$*"
      _pfx=${_line%% *}
      if [ -f "$CASE/tbl-$_tbl" ]; then
        awk -v p="$_pfx" '$1 != p' "$CASE/tbl-$_tbl" > "$CASE/tbl-$_tbl.new"
        mv -f "$CASE/tbl-$_tbl.new" "$CASE/tbl-$_tbl"
      fi
      printf '%s\n' "$_line" >> "$CASE/tbl-$_tbl"
      return 0 ;;
    *) return 0 ;;
  esac
}
'''

# The shared route helper, replaced by a recorder: this test is about *whether* the table is
# synced, not about the helper's ownership rules (tests/test_routes_ownership.py owns those).
ROUTES_STUB = r'''geomask_routes_sync() {
  printf '%s\n' "$1" >> "$CASE/syncs"
  return 0
}
'''


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleRebootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        for name in ('calls.log', 'rules', 'syncs', 'logger.log'):
            (self.case / name).write_text('', encoding='utf-8', newline='\n')
        self.state = self.case / 'console.state'
        self.state.write_text('enabled=1\nip=192.168.1.177\n', encoding='utf-8', newline='\n')
        self.lib = self.case / 'routes-stub.sh'
        self.lib.write_text(ROUTES_STUB, encoding='utf-8', newline='\n')

    def write(self, name, text):
        path = self.case / name
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def environment(self):
        return (
            'CASE="%s"\n'
            'GM_CONSOLE_STATE="%s"\n'
            'GM_CONSOLE_LOCK="%s/console.lock"\n'
            'GM_ROUTES_LIB="%s"\n'
            'GM_CONSOLE_TUN="Console"\n'
            'export CASE GM_CONSOLE_STATE GM_CONSOLE_LOCK GM_ROUTES_LIB GM_CONSOLE_TUN\n'
            'logger() { printf "%%s\n" "$*" >> "$CASE/logger.log"; }\n'
            'conntrack() { return 0; }\n'
            % (win(self.case), win(self.state), win(self.case), win(self.lib)))

    def run_script(self, body, prefix=''):
        script = self.write('run.sh', 'set -u\n' + self.environment() + IP_STUB
                            + prefix + '\n' + body + '\n')
        return subprocess.run([BASH, win(script)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=120)

    def calls(self):
        return [line for line in (self.case / 'calls.log').read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def syncs(self):
        return [line for line in (self.case / 'syncs').read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def table(self, number):
        path = self.case / ('tbl-%s' % number)
        if not path.exists():
            return []
        return [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    # ---------------------------------------------------------------- ifup hotplug
    def hotplug_body(self):
        """The shipped functions plus the two values the file derives before calling them.

        `TUN_DEV` comes from the mihomo config and `LAN_DEV` from uci in the real script, so the
        test supplies them exactly as the file would. `CONSOLE_TABLE`/`CONSOLE_TUN_DEV` are left
        out on purpose: their defaults live in `apply_routes` and must be the ones under test.
        """
        return ('TUN_DEV=Mihomo\nLAN_DEV=br-lan\n'
                + '\n'.join(hotplug_function(name) for name in ('sync_table', 'apply_routes')))

    def test_ifup_gives_the_console_table_its_lan_prefixes(self):
        """Table 102 is empty after a reboot; the rule points the console straight into it."""
        self.write('tbl-100', '')
        self.write('tbl-102', '')
        result = self.run_script('apply_routes; printf "RC=%s\\n" "$?"',
                                 prefix=self.hotplug_body())
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('102', self.syncs(),
                      'the console table was never mirrored on ifup: %r' % (self.syncs(),))
        self.assertEqual(self.table(102), ['default dev Console'],
                         'the console table has no default through its own tunnel')
        self.assertEqual(self.table(100), ['default dev Mihomo'],
                         'the normal lane regressed')

    def test_ifup_leaves_the_console_table_alone_while_the_lane_is_off(self):
        self.state.write_text('enabled=0\nip=192.168.1.177\n', encoding='utf-8', newline='\n')
        self.write('tbl-100', '')
        result = self.run_script('apply_routes; printf "RC=%s\\n" "$?"',
                                 prefix=self.hotplug_body())
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertNotIn('102', self.syncs(), 'a switched-off lane still synced its table')
        self.assertEqual(self.table(102), [], 'a switched-off lane still got a default route')
        self.assertNotIn('table 102', '\n'.join(self.calls()))

    # ---------------------------------------------------------------- guard block
    def test_the_guard_restores_the_lan_prefixes_in_table_102(self):
        self.write('tbl-102', 'default dev Console')
        result = self.run_script(guard_block(), prefix='log() { logger -t geomask-guard "$*"; }')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('102', self.syncs(),
                      'the watchdog re-seated the rule but left the table without the LAN')

    def test_the_guard_replaces_a_wan_default_in_table_102(self):
        """`grep '^default'` accepted the WAN default netifd restores; it is not our tunnel."""
        self.write('tbl-102', 'default via 192.168.1.1 dev br-lan')
        result = self.run_script(guard_block(), prefix='log() { logger -t geomask-guard "$*"; }')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.table(102), ['default dev Console'],
                         'the WAN default survived: the console leaves with the real address')
        self.assertIn('console table 102 default restored',
                      (self.case / 'logger.log').read_text(encoding='utf-8'))

    def test_the_guard_keeps_a_correct_default_and_never_replaces_it(self):
        self.write('tbl-102', 'default dev Console')
        result = self.run_script(guard_block(), prefix='log() { logger -t geomask-guard "$*"; }')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('route replace table 102', '\n'.join(self.calls()),
                         'the correct default was rewritten on every watchdog run')


if __name__ == '__main__':
    unittest.main()
