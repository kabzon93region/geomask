"""R10a / G01: the DNS audit must not report OK from an answer it never got.

The shipped check read the FIRST `Address:` line of `nslookup` output - that is the SERVER
line - and ignored the exit code. A SERVFAIL therefore printed "OK dnsmasq answers on
127.0.0.1:53" (results2.txt, P7/P8), and the LAN address was hardcoded `192.168.0.1`.
`--quick` promised "no outbound probes" but still ran two nslookups; a missing `python3` or
`/etc/mihomo/config.yaml` silently turned checks into OKs.

Contract proved here (Windows/Git Bash, stubs only):

* GNU space-formatted SERVFAIL is never OK;
* BusyBox tab-formatted SERVFAIL is never OK, BusyBox `Address 1:` is a real answer;
* the answer is read only AFTER `Name:`, never the server line;
* the LAN resolver address comes from UCI `network.lan.device` + `ip -4 addr show`
  (fallback `br-lan`), not a hardcoded `192.168.0.1`;
* `--quick` performs zero nslookup calls;
* missing `python3` / missing mihomo config is UNKNOWN with rc 2, never a silent OK;
* a non-zero nslookup rc is not OK even when the body contains an address.

No `/etc`, no `/tmp`, no network: every command is a PATH stub in one TemporaryDirectory and
every host path is injected (GM_MIHOMO_CONFIG, GM_HOSTS, GM_NET_DIR, GM_RULESET_DIR,
GM_PYTHON).
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DNSCHECK = ROOT / 'assets' / 'geomask-dnscheck.sh'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def win(path):
    return str(Path(path).resolve()).replace('\\', '/')


GNU_SERVFAIL = '''Server:\t\t127.0.0.1
Address: 127.0.0.1:53

** server can't find yandex.ru: SERVFAIL
'''

BUSYBOX_SERVFAIL = '''Server:\t\t127.0.0.1
Address:\t127.0.0.1:53

** server can't find yandex.ru: SERVFAIL
'''

BUSYBOX_OK = '''Server:\t\t127.0.0.1
Address:\t127.0.0.1:53

Name:\tyandex.ru
Address 1: 5.255.255.242
'''

GNU_OK = '''Server:\t\t127.0.0.1
Address:\t127.0.0.1:53

Non-authoritative answer:
Name:\tyandex.ru
Address: 5.255.255.242
'''


IP_STUB = '''#!/bin/sh
printf '%s\\n' "$*" >> "$CASE/ip.calls"
case "$*" in
  *"addr show"*)
    printf '    inet 10.10.0.1/24 brd 10.10.0.255 scope global br-lan.1\\n' ;;
esac
exit 0
'''

UCI_STUB = '''#!/bin/sh
printf '%s\\n' "$*" >> "$CASE/uci.calls"
case "$*" in
  "get network.lan.device")
    [ -f "$CASE/lan.device" ] || exit 1
    cat "$CASE/lan.device"; exit 0 ;;
  "get network.lan.ifname")
    [ -f "$CASE/lan.ifname" ] || exit 1
    cat "$CASE/lan.ifname"; exit 0 ;;
esac
exit 1
'''

NETSTAT_STUB = '''#!/bin/sh
printf 'tcp        0      0 0.0.0.0:53              0.0.0.0:*               LISTEN\\n'
printf 'udp        0      0 0.0.0.0:53              0.0.0.0:*\\n'
'''

PGREP_STUB = '''#!/bin/sh
exit 0
'''

NFT_STUB = '''#!/bin/sh
exit 0
'''


@unittest.skipIf(BASH is None, 'no-bash')
class DnscheckContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.bin = self.case / 'bin'
        self.bin.mkdir()
        self.write_bin('ip', IP_STUB)
        self.write_bin('uci', UCI_STUB)
        self.write_bin('netstat', NETSTAT_STUB)
        self.write_bin('pgrep', PGREP_STUB)
        self.write_bin('nft', NFT_STUB)
        self.write('lan.device', 'br-lan\n')
        self.write('mihomo.yaml', 'rules:\n  - MATCH,DIRECT\n')
        self.write('hosts', '127.0.0.1 localhost\n')
        self.net = self.case / 'net'
        for tunnel in ('Mihomo', 'AI', 'VPN-noP2P'):
            (self.net / tunnel).mkdir(parents=True)
        self.ruleset = self.case / 'ruleset'
        self.ruleset.mkdir()
        self.nslookup(GNU_OK, 0)
        for name in ('nslookup.calls', 'ip.calls', 'uci.calls'):
            self.write(name, '')
        self.calls = self.case / 'nslookup.calls'

    def write(self, name, text):
        path = self.case / name
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def write_bin(self, name, text):
        path = self.bin / name
        path.write_text(text, encoding='utf-8', newline='\n')
        os.chmod(path, 0o755)
        return path

    def nslookup(self, body, rc):
        self.write_bin('nslookup', (
            '#!/bin/sh\n'
            'printf \'%s\\n\' "$*" >> "$CASE/nslookup.calls"\n'
            "cat <<'NS_OUT'\n"
        ) + body + 'NS_OUT\n' + 'exit %d\n' % rc)

    def environment(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('GM_')}
        env.pop('MSYS', None)
        env['CASE'] = win(self.case)
        env['PATH'] = str(self.bin) + os.pathsep + env.get('PATH', '')
        env['GM_MIHOMO_CONFIG'] = win(self.case / 'mihomo.yaml')
        env['GM_HOSTS'] = win(self.case / 'hosts')
        env['GM_NET_DIR'] = win(self.net)
        env['GM_RULESET_DIR'] = win(self.ruleset)
        env['GM_PYTHON'] = sys.executable.replace('\\', '/')
        return env

    def run_check(self, *args, **overrides):
        env = self.environment()
        env.update(overrides)
        result = subprocess.run([BASH, win(DNSCHECK), *args], cwd=str(self.case),
                                env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding='utf-8', errors='replace', timeout=180)
        return result

    def calls_made(self):
        if not self.calls.exists():
            return []
        return [line for line in self.calls.read_text(encoding='utf-8').splitlines()
                if line.strip()]

    # ------------------------------------------------------------------ parser
    def test_space_formatted_servfail_is_not_ok(self):
        self.nslookup(GNU_SERVFAIL, 0)
        result = self.run_check()
        self.assertNotIn('dnsmasq answers', result.stdout,
                         'the server Address line was accepted as the answer:\n' + result.stdout)
        self.assertIn('does NOT answer', result.stdout, result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_busybox_tab_servfail_is_not_ok(self):
        self.nslookup(BUSYBOX_SERVFAIL, 0)
        result = self.run_check()
        self.assertNotIn('dnsmasq answers', result.stdout, result.stdout)
        self.assertIn('does NOT answer', result.stdout, result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_busybox_address_number_is_a_real_answer(self):
        self.nslookup(BUSYBOX_OK, 0)
        result = self.run_check()
        self.assertIn('dnsmasq answers on 127.0.0.1:53 (5.255.255.242)',
                      result.stdout, result.stdout)
        self.assertNotIn('(127.0.0.1:53)', result.stdout,
                         'the server line was printed as the answer')
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_nonzero_nslookup_rc_is_not_ok_even_with_an_address(self):
        self.nslookup(GNU_OK, 1)
        result = self.run_check()
        self.assertIn('does NOT answer', result.stdout, result.stdout)
        self.assertNotIn('dnsmasq answers', result.stdout, result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout)

    # ------------------------------------------------------------------ LAN address
    def test_lan_address_comes_from_uci_and_ip_addr(self):
        self.write('lan.device', 'br-lan.1\n')
        self.nslookup(GNU_OK, 0)
        result = self.run_check()
        calls = self.calls_made()
        self.assertIn('yandex.ru 10.10.0.1', calls,
                      'the LAN probe did not use the UCI/ip address: %r' % calls)
        self.assertIn('yandex.ru 127.0.0.1', calls)
        self.assertIn('answers on 10.10.0.1', result.stdout, result.stdout)
        self.assertNotIn('192.168.0.1', result.stdout + '\n'.join(calls),
                         'the hardcoded 192.168.0.1 is still used')
        ip_calls = (self.case / 'ip.calls').read_text(encoding='utf-8')
        self.assertIn('addr show dev br-lan.1', ip_calls, ip_calls)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_lan_device_falls_back_to_br_lan(self):
        (self.case / 'lan.device').unlink()
        self.nslookup(GNU_OK, 0)
        result = self.run_check()
        ip_calls = (self.case / 'ip.calls').read_text(encoding='utf-8')
        self.assertIn('addr show dev br-lan', ip_calls, ip_calls)
        self.assertIn('yandex.ru 10.10.0.1', self.calls_made())
        self.assertEqual(result.returncode, 0, result.stdout)

    # ------------------------------------------------------------------ quick mode
    def test_quick_makes_no_network_calls(self):
        result = self.run_check('--quick')
        self.assertEqual(self.calls_made(), [],
                         '--quick still called nslookup: %r' % self.calls_made())
        self.assertIn('SKIP', result.stdout, result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout)

    # ------------------------------------------------------------------ UNKNOWN
    def test_missing_python3_is_unknown(self):
        result = self.run_check(
            GM_PYTHON=win(self.case / 'no-python3'))
        self.assertIn('UNKNOWN', result.stdout, result.stdout)
        self.assertNotIn('RESULT: DNS looks healthy', result.stdout)
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_missing_mihomo_config_is_unknown(self):
        result = self.run_check(
            GM_MIHOMO_CONFIG=win(self.case / 'absent.yaml'))
        self.assertIn('UNKNOWN', result.stdout, result.stdout)
        self.assertNotIn('mihomo uses the system resolver', result.stdout,
                         'a missing config was reported as a clean system resolver')
        self.assertEqual(result.returncode, 2, result.stdout)

    # ------------------------------------------------------------------ warning precedence
    def test_warnings_do_not_mask_a_failure(self):
        empty = self.case / 'empty-net'
        empty.mkdir()
        self.nslookup(GNU_SERVFAIL, 0)
        result = self.run_check(GM_NET_DIR=win(empty))
        self.assertIn('tunnel Mihomo missing', result.stdout, result.stdout)
        self.assertIn('problem(s)', result.stdout,
                      'a warning hid the resolver failure: ' + result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_warnings_do_not_mask_unknown(self):
        empty = self.case / 'empty-net'
        empty.mkdir()
        result = self.run_check(GM_NET_DIR=win(empty),
                                GM_MIHOMO_CONFIG=win(self.case / 'absent.yaml'))
        self.assertIn('tunnel Mihomo missing', result.stdout, result.stdout)
        self.assertIn('RESULT: UNKNOWN', result.stdout,
                      'a warning hid the unknown check: ' + result.stdout)
        self.assertEqual(result.returncode, 2, result.stdout)


if __name__ == '__main__':
    unittest.main()
