"""R10b: `status` is a read, not a health verdict.

The old payload ended with `rules_ok` (a count of `lookup main` rules) and the panel had
nothing else to judge. These tests run the two new sub-objects against fixtures:

  * `guard` separates the wanted base rules / table 101 default from what `ip` shows;
  * `dns` separates the recorded strict-DNS policy from the live dnsmasq section, WITHOUT
    probing the resolver (the panel renders status often);
  * `ok` on the top-level object means "the request ran"; health is guard.state/dns.state.

Also covers the "not installed" exit code of `aidns` (R10b item 4).
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

GUARD_FUNCTIONS = ['_gm_unknown', '_guard_status_json']
DNS_FUNCTIONS = ['json_esc', '_gm_unknown', '_dns_section_values', '_dns_status_json']


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def extract(name):
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
    "rule show")  cat "$CASE/rules";  return 0 ;;
    "route show") cat "$CASE/routes"; return 0 ;;
    "link show")  [ "${TUN_STATE:-1}" = 1 ] && return 0 || return 1 ;;
  esac
  return 1
}
_gm_lan_iface() { printf 'br-lan'; }
'''


@unittest.skipIf(BASH is None, 'bash is required')
class GuardStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.rules = self.case / 'rules'
        self.routes = self.case / 'routes'
        self.devices = self.case / 'geo-devices.list'
        self.devices.write_text('', encoding='utf-8', newline='\n')
        self.write_rules(['100: from all iif br-lan ipproto icmp lookup main',
                          '101: from all fwmark 0x64 lookup 101'])
        self.write_routes(['default dev AI'])
        self.minbin = self.case / 'minbin'

    def write_rules(self, rules):
        self.rules.write_text(''.join(line + '\n' for line in rules),
                              encoding='utf-8', newline='\n')

    def write_routes(self, routes):
        self.routes.write_text(''.join(line + '\n' for line in routes),
                               encoding='utf-8', newline='\n')

    def make_minbin(self):
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

    def run_guard(self, with_ip=True, tun=1, devices=True):
        if not devices:
            self.devices.unlink(missing_ok=True)
        header = (
            'set -u\n'
            'CASE=%s\nDEVICES=%s\nAI_TUN_DEV=AI\nTUN_STATE=%s\n'
            % (shlex.quote(posix(self.case)), shlex.quote(posix(self.devices)), tun)
        )
        body = IP_STUB if with_ip else ''
        if not with_ip:
            self.make_minbin()
            body += 'PATH=%s\nexport PATH\n' % shlex.quote(posix(self.minbin))
        script = header + body + '\n' + '\n'.join(
            extract(name) for name in GUARD_FUNCTIONS) + '\n_guard_status_json\n'
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8',
                                timeout=30)
        payloads = [line for line in result.stdout.splitlines() if line.lstrip().startswith('{')]
        self.assertTrue(payloads, 'no JSON:\n%s\n%s'
                        % (result.stdout[-800:], result.stderr[-800:]))
        return json.loads(payloads[-1]), result

    def test_a_matching_guard_contract_is_ok(self):
        data, result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data['state'], 'ok')
        self.assertEqual(data['runtime']['base_rules'], 2)
        self.assertEqual(data['runtime']['table101_default'], 'dev AI')

    def test_a_wan_default_in_table_101_is_drift(self):
        self.write_routes(['default via 192.168.1.1 dev eth0'])
        data, _ = self.run_guard()
        self.assertEqual(data['runtime']['table101_default'], 'foreign')
        self.assertEqual(data['state'], 'drift')

    def test_a_missing_base_rule_is_drift(self):
        self.write_rules(['101: from all fwmark 0x64 lookup 101'])
        data, _ = self.run_guard()
        self.assertEqual(data['runtime']['base_rules'], 1)
        self.assertEqual(data['state'], 'drift')

    def test_a_down_ai_tunnel_is_down(self):
        data, _ = self.run_guard(tun=0)
        self.assertEqual(data['runtime']['tun'], 'down')
        self.assertEqual(data['state'], 'down')

    def test_a_missing_device_list_is_unknown(self):
        data, _ = self.run_guard(devices=False)
        self.assertEqual(data['state'], 'unknown')
        self.assertIn('devices', data['unknown'])

    def test_no_ip_is_unknown_not_healthy(self):
        data, result = self.run_guard(with_ip=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data['state'], 'unknown')
        self.assertIn('ip', data['unknown'])


@unittest.skipIf(BASH is None, 'bash is required')
class DnsStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.state = self.case / 'dnsguard.state'
        self.dhcp = self.case / 'dhcp'

    def write_state(self, saved='1'):
        self.state.write_text('version=2\nsaved=%s\ncount=0\n' % saved,
                              encoding='utf-8', newline='\n')

    def write_dhcp(self, servers, strictorder=None):
        lines = ['config dnsmasq', "\toption domain 'lan'"]
        for server in servers:
            lines.append("\tlist server '%s'" % server)
        if strictorder is not None:
            lines.append("\toption strictorder '%s'" % strictorder)
        lines += ['', 'config dhcp lan', "\toption interface 'lan'"]
        self.dhcp.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')

    def run_dns(self):
        header = (
            'set -u\n'
            'CASE=%s\nDNSGUARD_STATE=%s\nDNSGUARD_DHCP=%s\n'
            % (shlex.quote(posix(self.case)), shlex.quote(posix(self.state)),
               shlex.quote(posix(self.dhcp)))
        )
        script = header + '\n' + '\n'.join(
            extract(name) for name in DNS_FUNCTIONS) + '\n_dns_status_json\n'
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8',
                                timeout=30)
        payloads = [line for line in result.stdout.splitlines() if line.lstrip().startswith('{')]
        self.assertTrue(payloads, 'no JSON:\n%s\n%s'
                        % (result.stdout[-800:], result.stderr[-800:]))
        return json.loads(payloads[-1]), result

    def strict_dhcp(self):
        self.write_dhcp(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strictorder='1')

    def test_a_recorded_and_live_strict_policy_is_ok(self):
        self.write_state()
        self.strict_dhcp()
        data, result = self.run_dns()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(data['desired']['mode'], 'strict')
        self.assertEqual(data['runtime']['mode'], 'strict')
        self.assertEqual(data['state'], 'ok')

    def test_a_recorded_policy_that_is_not_live_is_drift(self):
        self.write_state()
        self.write_dhcp(['9.9.9.9'], strictorder='0')
        data, _ = self.run_dns()
        self.assertEqual(data['desired']['mode'], 'strict')
        self.assertEqual(data['runtime']['mode'], 'off')
        self.assertEqual(data['state'], 'drift')

    def test_no_record_and_no_policy_is_off_and_ok(self):
        self.write_dhcp(['9.9.9.9'])
        data, _ = self.run_dns()
        self.assertEqual(data['desired']['mode'], 'off')
        self.assertEqual(data['runtime']['mode'], 'off')
        self.assertEqual(data['state'], 'ok')

    def test_a_live_policy_without_a_record_is_drift(self):
        self.strict_dhcp()
        data, _ = self.run_dns()
        self.assertEqual(data['desired']['mode'], 'off')
        self.assertEqual(data['runtime']['mode'], 'strict')
        self.assertEqual(data['state'], 'drift')

    def test_missing_dhcp_config_is_unknown(self):
        self.write_state()
        data, _ = self.run_dns()
        self.assertEqual(data['state'], 'unknown')
        self.assertIn('dhcp-config', data['unknown'])


@unittest.skipIf(BASH is None, 'bash is required')
class StatusEnvelopeTests(unittest.TestCase):
    def test_ok_means_the_request_ran_and_health_is_in_the_objects(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        case = Path(tmp.name)
        script = (
            'set -u\n'
            '_guard_status_json() { printf \'{"state":"drift"}\'; }\n'
            '_dns_status_json() { printf \'{"state":"ok"}\'; }\n'
            + extract('_status_attach_health') + '\n'
            '_status_attach_health \'{"vpn_node":"x"}\'\n'
        )
        path = case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(case),
                                capture_output=True, text=True, encoding='utf-8',
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(data['ok'])
        self.assertEqual(data['vpn_node'], 'x')
        self.assertEqual(data['guard']['state'], 'drift')
        self.assertEqual(data['dns']['state'], 'ok')


@unittest.skipIf(BASH is None, 'bash is required')
class DiagRunExitCodeTests(unittest.TestCase):
    def test_the_backend_propagates_a_failed_doctor_run(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        case = Path(tmp.name)
        bin_dir = case / 'bin'
        bin_dir.mkdir()
        stub = bin_dir / 'python3'
        stub.write_text('#!/bin/sh\nprintf \'{"ok":false,"state":"fail"}\\n\'\nexit 1\n',
                        encoding='utf-8', newline='\n')
        os.chmod(stub, 0o755)
        doctor = case / 'geomask-doctor.py'
        doctor.write_text('', encoding='utf-8')
        script = (
            'set -u\n'
            'CASE=%s\nDOCTOR=%s\nPATH=%s\nexport PATH\n'
            % (shlex.quote(posix(case)), shlex.quote(posix(doctor)),
               shlex.quote(posix(bin_dir)))
            + extract('cmd_diag_run') + '\n'
            + 'cmd_diag_run some_check\nprintf \'\\nRC=%s\\n\' "$?"\n'
        )
        path = case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(case),
                                capture_output=True, text=True, encoding='utf-8',
                                timeout=30)
        self.assertIn('RC=1', result.stdout)
        payload = json.loads(result.stdout.splitlines()[0])
        self.assertFalse(payload['ok'])


@unittest.skipIf(BASH is None, 'bash is required')
class AidnsExitCodeTests(unittest.TestCase):
    def test_a_missing_aidns_module_is_a_failure_not_a_silent_noop(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        case = Path(tmp.name)
        script = (
            'set -u\n'
            'AIDNS_OFF_FILE=%s\nAIDNS_UID_FILE=%s\nAIDNS_NFT_DEST=%s\nAIDNS_STATE=%s\n'
            'LOG=%s\n'
            % tuple(shlex.quote(posix(case / name)) for name in
                    ('off', 'uid', 'nft', 'state', 'log'))
            + extract('cmd_aidns_apply') + '\n'
            + 'cmd_aidns_apply; printf \'\\nRC=%s\\n\' "$?"\n'
        )
        path = case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(case),
                                capture_output=True, text=True, encoding='utf-8',
                                timeout=30)
        self.assertIn('RC=1', result.stdout)
        payload = json.loads(result.stdout.splitlines()[0])
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['state'], 'not_installed')


if __name__ == '__main__':
    unittest.main()
