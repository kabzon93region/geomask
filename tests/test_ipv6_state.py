"""IPv6 off/on must put the kernel and firewall back exactly as they were.

Defect (plan 19): `ipv6.state` recorded only the four uci values (`ip6assign`, `ra`,
`dhcpv6`, `ndp`). The sysctl file, the per-interface `disable_ipv6` bits and the presence of
our own nft guard were not recorded, so `on` wrote `0` everywhere and deleted the guard: a
router that had IPv6 disabled before geomask (or carried the guard file) came back with a
different kernel state than the one it went in with.

The helper block is taken from the shipped backend and run against file-backed `uci`,
`sysctl` and `ip` stubs, so nothing outside the temporary case directory is touched.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
SOURCE = BACKEND.read_text(encoding='utf-8')
START = 'IPV6_STATE=/etc/geomask/ipv6.state'
END = 'cmd_ipv6() {'
BLOCK = SOURCE[SOURCE.index(START):SOURCE.index(END)]


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()

STUBS = r'''uci() {
  case "$1" in -q) shift ;; esac
  case "$1" in
    get) _k=$(printf '%s' "$2" | tr '.' '_')
         sed -n "s/^$_k=//p" "$CASE/uci" 2>/dev/null | head -1 ;;
    set) _k=${2%%=*}; _v=${2#*=}
         _k=$(printf '%s' "$_k" | tr '.' '_')
         grep -v "^$_k=" "$CASE/uci" > "$CASE/uci.new" 2>/dev/null
         printf '%s=%s\n' "$_k" "$_v" >> "$CASE/uci.new"
         mv -f "$CASE/uci.new" "$CASE/uci" ;;
    delete) _k=$(printf '%s' "$2" | tr '.' '_')
            grep -v "^$_k=" "$CASE/uci" > "$CASE/uci.new" 2>/dev/null
            mv -f "$CASE/uci.new" "$CASE/uci" ;;
    show) case "$2" in
            network) sed -n 's/^network_\([A-Za-z0-9_]*\)_\([A-Za-z0-9_]*\)=\(.*\)$/network.\1.\2=\3/p' \
                       "$CASE/uci" ;;
          esac ;;
    commit) return 0 ;;
  esac
  return 0
}
sysctl() { printf '%s\n' "$*" >> "$CASE/sysctl.calls"; return "${SYSCTL_RC:-0}"; }
ip() { case "$*" in "-6 addr show"*|"-6 addr"*) cat "$CASE/ipv6addrs" 2>/dev/null ;; esac; return 0; }
'''


@unittest.skipIf(BASH is None, 'bash is required')
class Ipv6StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'stubs.sh').write_text(STUBS, encoding='utf-8', newline='\n')
        (self.case / 'ipv6addrs').write_text('', encoding='utf-8')
        (self.case / 'sysctl.calls').write_text('', encoding='utf-8')
        (self.case / 'proc' / 'all').mkdir(parents=True)
        (self.case / 'proc' / 'default').mkdir(parents=True)
        self.set_proc('all', 0)
        self.set_proc('default', 0)
        self.state = self.case / 'ipv6.state'
        self.nft = self.case / '99-geomask-no-v6.nft'
        self.sysctl_conf = self.case / '99-disable-ipv6.conf'

    def set_proc(self, which, value):
        (self.case / 'proc' / which / 'disable_ipv6').write_text('%d\n' % value,
                                                                encoding='utf-8', newline='\n')

    def proc(self, which):
        return (self.case / 'proc' / which / 'disable_ipv6').read_text(
            encoding='utf-8').strip()

    def set_uci(self, values):
        (self.case / 'uci').write_text(
            ''.join('%s=%s\n' % (key.replace('.', '_'), value) for key, value in values.items()),
            encoding='utf-8', newline='\n')

    def uci(self, key):
        lines = [line for line in (self.case / 'uci').read_text(encoding='utf-8').splitlines()
                 if line.startswith(key.replace('.', '_') + '=')]
        return lines[0].split('=', 1)[1] if lines else ''

    def state_lines(self):
        return self.state.read_text(encoding='utf-8').splitlines()

    def run_block(self, body):
        script = '\n'.join([
            'set -u',
            'CASE="%s"' % self.case.as_posix(),
            'export CASE',
            '. "$CASE/stubs.sh"',
            BLOCK,
            'IPV6_STATE="$CASE/ipv6.state"',
            'IPV6_NFT="$CASE/99-geomask-no-v6.nft"',
            'IPV6_SYSCTL="$CASE/99-disable-ipv6.conf"',
            'IPV6_PROC="$CASE/proc"',
            body,
        ]) + '\n'
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, path.as_posix()], cwd=str(self.case),
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    ORIGINAL = {'network.lan.ip6assign': '56', 'dhcp.lan.ra': 'server',
                'dhcp.lan.dhcpv6': 'server', 'dhcp.lan.ndp': 'hybrid'}

    def test_the_original_kernel_and_guard_state_come_back(self):
        """IPv6 was already disabled and our guard file was there before geomask touched it."""
        self.set_uci(self.ORIGINAL)
        self.set_proc('all', 1)
        self.set_proc('default', 1)
        self.nft.write_text('table inet fw4 {\n}\n', encoding='utf-8', newline='\n')
        out = self.run_block('_ipv6_apply_off && echo OFF-OK\n_ipv6_apply_on && echo ON-OK')
        self.assertIn('OFF-OK', out)
        self.assertIn('ON-OK', out)
        self.assertEqual(self.uci('network.lan.ip6assign'), '56')
        self.assertEqual(self.uci('dhcp.lan.ra'), 'server')
        self.assertEqual(self.proc('all'), '1', 'the kernel switch was recorded and came back')
        self.assertEqual(self.proc('default'), '1')
        self.assertTrue(self.nft.exists(), 'the nft guard that existed before must come back')

    def test_the_record_holds_the_kernel_state_and_survives_a_second_off(self):
        """Two `off` runs must not replace the original record with our own zeros (G04)."""
        self.set_uci(self.ORIGINAL)
        out = self.run_block('_ipv6_apply_off\n_ipv6_apply_off\n'
                             'printf "IP6=%s\\n" "$(sed -n \'s/^ip6assign=//p\' "$IPV6_STATE")"\n'
                             '_ipv6_apply_on && echo ON-OK')
        self.assertIn('IP6=56', out, 'the second off overwrote the original record')
        self.assertIn('ON-OK', out)
        lines = self.state_lines() if self.state.exists() else []
        self.assertTrue(any(line.startswith('sysctl_all=') for line in lines),
                        'ipv6.state must carry the kernel switch: %r' % lines)
        self.assertTrue(any(line.startswith('sysctl_default=') for line in lines))
        self.assertTrue(any(line.startswith('nft=') for line in lines))
        self.assertEqual(self.uci('network.lan.ip6assign'), '56')
        self.assertEqual(self.proc('all'), '0')

    def test_a_legacy_record_still_turns_ipv6_back_on(self):
        """A record written by an older build has no kernel keys: enable instead of half-restore."""
        self.set_uci(self.ORIGINAL)
        self.state.write_text(''.join('%s=%s\n' % (key.split('.')[-1], value)
                                      for key, value in self.ORIGINAL.items()),
                              encoding='utf-8', newline='\n')
        self.set_proc('all', 1)
        self.set_proc('default', 1)
        self.nft.write_text('table inet fw4 {\n}\n', encoding='utf-8', newline='\n')
        out = self.run_block('_ipv6_apply_on && echo ON-OK')
        self.assertIn('ON-OK', out)
        self.assertEqual(self.uci('network.lan.ip6assign'), '56')
        self.assertEqual(self.proc('all'), '0', 'IPv6 must really be enabled again')
        self.assertFalse(self.nft.exists(), 'our drop rule must not stay when IPv6 is on')

    # --------------------------------------------------- C06/P06: per-interface restore
    def test_a_renamed_lan_section_is_restored_by_name(self):
        """The restore used to hardcode lan, so a lan1 router came back on the wrong section."""
        self.set_uci({'network.lan1.ip6assign': '56', 'dhcp.lan1.ra': 'server',
                      'dhcp.lan1.dhcpv6': 'server', 'dhcp.lan1.ndp': 'hybrid'})
        out = self.run_block('_ipv6_apply_off && echo OFF-OK\n'
                             'printf "OFF=%s\\n" "$(uci -q get network.lan1.ip6assign)"\n'
                             '_ipv6_apply_on && echo ON-OK')
        self.assertIn('OFF-OK', out)
        self.assertIn('OFF=0', out, 'the real LAN section was never disabled')
        self.assertIn('ON-OK', out)
        self.assertEqual(self.uci('network.lan1.ip6assign'), '56',
                         'the real LAN section was not restored')
        self.assertEqual(self.uci('dhcp.lan1.ra'), 'server')
        self.assertEqual(self.uci('network.lan.ip6assign'), '',
                         'our own network.lan key was left behind on a lan1 router')

    def test_a_disappeared_interface_is_not_recreated(self):
        self.set_uci({'network.lan.ip6assign': '56', 'dhcp.lan.ra': 'server',
                      'dhcp.lan.dhcpv6': 'server', 'dhcp.lan.ndp': 'hybrid'})
        self.state.write_text('ip6assign=56\nra=server\ndhcpv6=server\nndp=hybrid\n'
                              'sysctl_all=0\nsysctl_default=0\nnft=absent\n'
                              'ifv|wan9|ip6assign|56\n', encoding='utf-8', newline='\n')
        result_stdout = self.run_block('_ipv6_apply_on && echo ON-OK')
        self.assertIn('ON-OK', result_stdout)
        self.assertEqual(self.uci('network.wan9.ip6assign'), '',
                         'a vanished interface was re-created from the record')


if __name__ == '__main__':
    unittest.main()
