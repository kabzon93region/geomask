"""R10b: `console_status` must separate desired, runtime and "cannot check".

The old status was three boolean substrings:

  * `grep "from $ip lookup 102"` matched a longer address (192.168.1.1770) and table 1020;
  * `grep '^default'` accepted a WAN default in table 102 as "the tunnel is fine";
  * `ip link show Console` was the only tunnel answer, so "rule=true, tun=true" could sit
    on top of traffic that left through the WAN.

These tests run the real status branch against a stateful `ip` stub. Nothing touches a
router: the state file, the rule/route snapshots and the LAN expected lines are fixtures.
The exact-token checks are POSIX awk and are covered on Windows; the real `ip`/kernel
semantics stay Linux-only.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
SOURCE = BACKEND.read_text(encoding='utf-8')

FUNCTIONS = [
    'json_esc', '_ipv4_ok', '_console_get', '_console_rule_exact',
    '_console_rule_any_table', '_console_default_kind', '_console_lan_present',
    '_console_status_unknown', '_console_status_state', 'cmd_console',
]


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def extract(name):
    """Whole function body, brace-balanced but heredoc-aware (the status helper has one)."""
    lines = SOURCE.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(name + '() {'):
            start = index
            break
    if start is None:
        raise AssertionError('function %s not found in the backend' % name)
    heredoc = None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        opener = None
        marker = line.find("<<")
        if marker >= 0:
            tail = line[marker + 2:].strip()
            if tail.startswith('-'):
                tail = tail[1:].strip()
            opener = tail.strip("'\"")
            if opener and ' ' not in opener:
                heredoc = opener
                continue
        if line.rstrip() == '}':
            return '\n'.join(lines[start:index + 1])
    raise AssertionError('function %s has no closing brace' % name)


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


IP_STUB = r'''ip() {
  case "$1 $2" in
    "rule show")  cat "$CASE/rules";  return "${RULES_RC:-0}" ;;
    "route show") cat "$CASE/routes"; return "${ROUTES_RC:-0}" ;;
    "link show")  [ "${TUN_STATE:-1}" = 1 ] && return 0 || return 1 ;;
  esac
  return 1
}
'''


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleStatusContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.state = self.case / 'console.state'
        self.rules = self.case / 'rules'
        self.routes = self.case / 'routes'
        self.routes_lib = self.case / 'routes-lib.sh'
        self.minbin = self.case / 'minbin'
        self.write_rules([])
        self.write_routes([])
        self.write_lan([])
        self.write_state()

    # ---------------------------------------------------------------- fixtures
    def write_state(self, enabled=1, ip='192.168.1.177', present=True):
        if not present:
            self.state.unlink(missing_ok=True)
            return
        self.state.write_text('enabled=%s\nip=%s\nsub=\ncountries=\n' % (enabled, ip),
                              encoding='utf-8', newline='\n')

    def write_rules(self, rules):
        self.rules.write_text(''.join(line + '\n' for line in rules),
                              encoding='utf-8', newline='\n')

    def write_routes(self, routes):
        self.routes.write_text(''.join(line + '\n' for line in routes),
                               encoding='utf-8', newline='\n')

    def write_lan(self, lines):
        body = ''.join('  printf \'%%s\\n\' %s\n' % shlex.quote(line) for line in lines)
        self.routes_lib.write_text('_gm_route_local_lines() {\n%s}\n' % body,
                                   encoding='utf-8', newline='\n')

    def make_minbin(self):
        """sed/head/awk only: `command -v ip` must really fail."""
        self.minbin.mkdir(exist_ok=True)
        for tool in ('sed', 'head', 'awk'):
            candidates = [shutil.which(tool),
                          r'C:\Program Files\Git\usr\bin\%s.exe' % tool,
                          '/usr/bin/' + tool, '/bin/' + tool]
            real = next((path for path in candidates
                         if path and Path(path).exists()), None)
            self.assertIsNotNone(real, tool)
            wrapper = self.minbin / tool
            wrapper.write_text('#!/bin/sh\nexec %s "$@"\n'
                               % shlex.quote(str(real).replace('\\', '/')),
                               encoding='utf-8', newline='\n')
            os.chmod(wrapper, 0o755)

    def run_status(self, with_ip=True, minimal_path=False, tun=1, routes_rc=0):
        header = (
            'set -u\n'
            'CASE=%s\nCONSOLE_STATE=%s\nCONSOLE_TUN_DEV=Console\n'
            'CONSOLE_RULE_PREF=19903\nCONSOLE_TABLE=102\n'
            'CONSOLE_PY=%s\nMIHOMO_CONF=%s\nGM_ROUTES_LIB=%s\n'
            'TUN_STATE=%s\nROUTES_RC=%s\n'
            % (shlex.quote(posix(self.case)), shlex.quote(posix(self.state)),
               shlex.quote(posix(self.case / 'no-console.py')),
               shlex.quote(posix(self.case / 'no-config.yaml')),
               shlex.quote(posix(self.routes_lib)), tun, routes_rc)
        )
        body = IP_STUB if with_ip else ''
        if minimal_path:
            self.make_minbin()
            body += 'PATH=%s\nexport PATH\n' % shlex.quote(posix(self.minbin))
        script = header + body + '\n' + '\n'.join(
            extract(name) for name in FUNCTIONS) + '\ncmd_console status\n'
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8',
                                timeout=30)
        payloads = [line for line in result.stdout.splitlines() if line.lstrip().startswith('{')]
        self.assertTrue(payloads, 'no JSON status:\n%s\n%s'
                        % (result.stdout[-1000:], result.stderr[-1000:]))
        return json.loads(payloads[-1]), result

    # ---------------------------------------------------------------- exactness
    def test_a_wan_default_is_drift_even_with_the_rule_and_tunnel(self):
        self.write_rules(['19903:\tfrom 192.168.1.177 lookup 102'])
        self.write_routes(['default via 192.168.1.1 dev eth0',
                           '192.168.1.0/24 dev br-lan scope link src 192.168.1.1'])
        self.write_lan(['192.168.1.0/24 dev br-lan src 192.168.1.1'])
        data, result = self.run_status()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(data['rule'])
        self.assertTrue(data['tun'])
        self.assertEqual(data['runtime']['default'], 'foreign')
        self.assertEqual(data['state'], 'drift')
        # old fields survive, so a panel that predates `state` still renders
        for key in ('ok', 'enabled', 'ip', 'sub', 'countries', 'rule', 'tun', 'config'):
            self.assertIn(key, data)

    def test_a_default_on_the_tunnel_device_with_a_gateway_is_foreign(self):
        self.write_rules(['19903:\tfrom 192.168.1.177 lookup 102'])
        self.write_routes(['default via 10.0.0.1 dev Console'])
        data, _ = self.run_status()
        self.assertEqual(data['runtime']['default'], 'foreign')
        self.assertEqual(data['state'], 'drift')

    def test_a_wrong_preference_is_not_the_console_rule(self):
        self.write_rules(['19900:\tfrom 192.168.1.177 lookup 102'])
        self.write_routes(['default dev Console'])
        data, _ = self.run_status()
        self.assertFalse(data['rule'])
        self.assertEqual(data['state'], 'drift')

    def test_a_longer_address_or_table_1020_does_not_match(self):
        self.write_rules(['19903:\tfrom 192.168.1.1770 lookup 1020'])
        self.write_routes(['default dev Console'])
        data, _ = self.run_status()
        self.assertFalse(data['rule'])
        self.assertEqual(data['state'], 'drift')

    def test_a_missing_lan_prefix_in_table_102_is_drift(self):
        self.write_rules(['19903:\tfrom 192.168.1.177 lookup 102'])
        self.write_routes(['default dev Console'])
        self.write_lan(['192.168.1.0/24 dev br-lan src 192.168.1.1'])
        data, _ = self.run_status()
        self.assertEqual(data['state'], 'drift')
        self.assertEqual(data['runtime']['lan_missing'], ['192.168.1.0/24'])

    def test_a_matching_lane_is_ok(self):
        self.write_rules(['19903:\tfrom 192.168.1.177 lookup 102'])
        self.write_routes(['default dev Console',
                           '192.168.1.0/24 dev br-lan scope link src 192.168.1.1'])
        self.write_lan(['192.168.1.0/24 dev br-lan src 192.168.1.1'])
        data, _ = self.run_status()
        self.assertEqual(data['state'], 'ok')
        self.assertEqual(data['unknown'], [])
        self.assertEqual(data['runtime']['default'], 'dev Console')

    def test_a_down_tunnel_is_named_down(self):
        self.write_rules(['19903:\tfrom 192.168.1.177 lookup 102'])
        self.write_routes(['default dev Console'])
        data, _ = self.run_status(tun=0)
        self.assertEqual(data['state'], 'down')
        self.assertFalse(data['tun'])

    # ---------------------------------------------------------------- disabled lane
    def test_enabled_zero_with_a_leftover_rule_is_drift(self):
        self.write_state(enabled=0)
        self.write_rules(['19903:\tfrom 192.168.1.177 lookup 102'])
        data, _ = self.run_status()
        self.assertEqual(data['state'], 'drift')
        self.assertFalse(data['rule'])

    def test_enabled_zero_without_a_console_rule_is_ok(self):
        self.write_state(enabled=0)
        self.write_rules(['32766:\tfrom all lookup main'])
        data, _ = self.run_status()
        self.assertEqual(data['state'], 'ok')
        self.assertEqual(data['runtime']['default'], 'not_checked')

    # ---------------------------------------------------------------- unknown
    def test_no_ip_is_unknown_not_drift(self):
        self.write_state(enabled=1)
        data, result = self.run_status(with_ip=False, minimal_path=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data['state'], 'unknown')
        self.assertIn('ip', data['unknown'])
        self.assertFalse(data['rule'])
        self.assertFalse(data['tun'])

    def test_a_missing_state_file_is_unknown(self):
        self.write_state(present=False)
        self.write_rules(['19903:\tfrom 192.168.1.177 lookup 102'])
        self.write_routes(['default dev Console'])
        data, _ = self.run_status()
        self.assertEqual(data['state'], 'unknown')
        self.assertIn('state', data['unknown'])

    def test_a_failed_route_read_is_unknown(self):
        self.write_rules(['19903:\tfrom 192.168.1.177 lookup 102'])
        self.write_routes(['default dev Console'])
        data, result = self.run_status(routes_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data['state'], 'unknown')
        self.assertIn('ip-route', data['unknown'])

    def test_the_rule_must_be_the_exact_five_token_rule(self):
        self.write_rules(['19903:\tfrom 192.168.1.177 lookup 102 extra'])
        self.write_routes(['default dev Console'])
        data, _ = self.run_status()
        self.assertFalse(data['rule'])
        self.assertEqual(data['state'], 'drift')


if __name__ == '__main__':
    unittest.main()
