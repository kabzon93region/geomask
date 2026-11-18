"""Policy rules: an apply that installed no rule must not report success.

Audit 18.11.2026, C05: `ip rule add` failed, every add was ignored and both applies returned 0,
so a worker could delete a queue event while LAN/AI traffic had no policy at all. The helper is
extracted from the shipped route writer and run against a stateful `ip` stub.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / 'assets' / 'geomask-routes.sh'
SOURCE = ROUTES.read_text(encoding='utf-8')

IP_STUB = r'''ip() {
  case "$1" in
    rule)
      case "$2" in
        show)
          [ "${FAIL_SHOW:-0}" = 1 ] && return 1
          cat "$CASE/rules" ;;
        add)
          [ "${FAIL_ADD:-0}" = 1 ] && return 1
          shift 2
          printf '%s\n' "$*" >> "$CASE/rules"
          printf '%s\n' "add $*" >> "$CASE/calls" ;;
        *) return 1 ;;
      esac ;;
    link) [ "$2" = show ] && [ "${WIREGUARD:-0}" = 1 ] && return 0; return 1 ;;
    route) return 0 ;;
    *) return 0 ;;
  esac
}
geomask_routes_sync() { return "${SYNC_RC:-0}"; }
_gm_route_mihomo_tun() { printf 'Mihomo'; }
_gm_route_wait_tun() { return "${TUN_RC:-0}"; }
_gm_route_lan_dev() { printf 'br-lan'; }
'''


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def extract_function(name):
    start = SOURCE.index(name + '() {')
    return SOURCE[start:].split('\n}\n', 1)[0] + '\n}\n'


@unittest.skipIf(BASH is None, 'bash is required')
class RouteRuleEnsureTests(unittest.TestCase):
    FUNCTIONS = ('_gm_route_rule_ensure', '_gm_route_apply_100')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'rules').write_text('', encoding='utf-8')
        (self.case / 'calls').write_text('', encoding='utf-8')
        self.devices = self.case / 'devices'
        self.devices.write_text('192.168.1.50 bypass tv\n', encoding='utf-8')

    def run_apply(self, env=None, body=None):
        environment = {
            'CASE': self.case.as_posix(),
            'DEVICES': self.devices.as_posix(),
            'GM_ROUTE_TUN_WAIT': '0',
            'GM_AIDNS_OFF': self.case.as_posix() + '/aidns.off',
            'GM_AIDNS_UID': self.case.as_posix() + '/aidns.uid',
        }
        environment.update(env or {})
        assignments = '\n'.join('%s="%s"' % item for item in environment.items())
        call = body or '_gm_route_apply_100; printf "RC=%s\\n" "$?"'
        script = ('set -u\n' + assignments + '\n' + IP_STUB
                  + '\n'.join(extract_function(name) for name in self.FUNCTIONS)
                  + '\n' + call + '\n')
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, path.as_posix()], cwd=str(self.case),
                              capture_output=True, text=True, encoding='utf-8', timeout=30)

    def rules(self):
        return [line for line in (self.case / 'rules').read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def test_a_clean_apply_installs_every_rule(self):
        result = self.run_apply()
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        joined = '\n'.join(self.rules())
        self.assertIn('32760', joined)
        self.assertIn('19901', joined)
        self.assertIn('19900 from 192.168.1.50 lookup main', joined)

    def test_a_refused_rule_fails_the_apply(self):
        result = self.run_apply(env={'FAIL_ADD': '1'})
        self.assertIn('RC=77', result.stdout, result.stdout + result.stderr)
        self.assertIn('could not be installed', result.stderr)
        self.assertEqual(self.rules(), [])

    def test_an_unreadable_rule_table_fails_the_apply(self):
        result = self.run_apply(env={'FAIL_SHOW': '1'})
        self.assertIn('RC=77', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.rules(), [])

    def test_a_rule_added_by_another_writer_is_not_a_failure(self):
        # The add fails, then the re-read proves the rule exists: that is success.
        body = ('printf "32760:\tfrom all iif br-lan lookup 100\\n" > "$CASE/rules"\n'
                '_gm_route_rule_ensure lan-100 "32760:.*iif br-lan lookup 100" '
                'priority 32760 iif br-lan lookup 100; printf "RC=%s\\n" "$?"')
        result = self.run_apply(body=body)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
