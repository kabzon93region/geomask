"""R03 phase 1: fail-closed preflight of the transactional reinstall.

The preflight runs BEFORE the first mutation and refuses whenever the promised rollback
cannot be proven: missing dependencies (never auto-installed), unobservable or unrestorable
runtime objects (tri-state enabled/running/unknown - unknown is an error), forbidden
effects (package installs in the reinstall path), and broken snapshot observers.

Every test runs the real extracted functions against a throwaway fixture root with
injected commands. Nothing here touches a live system path.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import win_symlink  # noqa: E402  (test-support helper in the same directory)
TEMPLATE = ROOT / 'geomask.sh.in'


def find_bash():
    for candidate in (r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe', shutil.which('bash')):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()
SOURCE = TEMPLATE.read_text(encoding='utf-8')

PREFLIGHT_FUNCTIONS = ['_pf_reason', '_reinstall_obj_type', '_pf_svc_enabled',
                       '_pf_svc_running', 'reinstall_preflight', 'pkg_install']


def extract(name):
    match = re.search(r'^' + re.escape(name) + r'\(\) \{.*?^\}\s*$', SOURCE,
                      re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError('function %s not found in the template' % name)
    return match.group(0)


def objects_table():
    match = re.search(r'^REINSTALL_PREFLIGHT_OBJECTS="([^"]*)"', SOURCE, re.M | re.S)
    if not match:
        raise AssertionError('REINSTALL_PREFLIGHT_OBJECTS not found in the template')
    return match.group(0) + '\n'


SUPPORT = r'''set -u
PASS=0; FAILCNT=0; SKIP=0; SAVE_FAIL=0
GM_YES=1; GM_AUTO=1; GM_RESTART=0; GM_CHECK_ONLY=0
GM_MODULES=''
GM_REINSTALL_NO_PKG="${GM_REINSTALL_NO_PKG:-0}"
GM_IPV6="${GM_IPV6:-off}"
GM_PREFIX='@PREFIX@'
PKG_LOG='@PKGLOG@'
P() { printf '%s%s' "$GM_PREFIX" "$1"; }
info() { printf 'INFO %s\n' "$*" >&2; }
ok()   { printf 'OK %s\n' "$*" >&2; }
warn() { printf 'WARN %s\n' "$*" >&2; }
bad()  { printf 'FAIL %s\n' "$*" >&2; FAILCNT=$((FAILCNT+1)); }
have() {
  case " ${PF_MISSING:-} " in
    *" $1 "*) return 1 ;;
  esac
  case "$1" in
    https-dns-proxy) [ -e '@PREFIX@/usr/sbin/https-dns-proxy' ] && return 0; return 1 ;;
  esac
  return 0
}
module_selected() { case " ${PF_MODULES:-} " in *" $1 "*) return 0 ;; esac; return 1; }
python3() { [ "${PF_PY_OK:-1}" = 1 ] || return 1; [ "${PF_YAML_OK:-1}" = 1 ] || return 1; return 0; }
crontab() {
  if [ "${1:-}" = "-l" ]; then
    case "${PF_CRONTAB_RC:-0}" in
      0) printf '* * * * * /bin/true\n'; return 0 ;;
      *) return "$PF_CRONTAB_RC" ;;
    esac
  fi
  return 0
}
pidof()  { return "${PIDOF_RC:-1}"; }
pgrep()  { return "${PGREP_RC:-1}"; }
netstat() { return 1; }
nft()    { return "${NFT_RC:-1}"; }
uci()    { return "${UCI_RC:-0}"; }
ip()     { return 0; }
apk()    { printf 'APK %s\n' "$*" >> "$PKG_LOG"; }
opkg()   { printf 'OPKG %s\n' "$*" >> "$PKG_LOG"; }
'''


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


@unittest.skipIf(BASH is None, 'bash is required')
class ReinstallPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = self.root / 'case'
        self.case.mkdir()
        self.pkg_log = self.root / 'pkg.log'
        self.pkg_log.write_text('', encoding='utf-8')
        self.build_fixture()

    # ------------------------------------------------------------------ fixture
    def write(self, relative, body='x\n'):
        path = self.case / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8', newline='\n')
        return path

    def build_fixture(self):
        (self.case / 'tmp').mkdir(exist_ok=True)
        self.write('etc/openwrt_release', "DISTRIB_RELEASE='24.10'\n")
        for service in ('dnsmasq', 'mihomo', 'magitrickle', 'hev-socks5-tunnel', 'rpcd',
                        'network', 'firewall', 'geomask-syncd', 'ai-doh', 'dnsmasq-ai',
                        'ksmbd'):
            self.write('etc/init.d/' + service, '#!/bin/sh\nexit 0\n')
        for link in ('S50mihomo', 'S19firewall', 'S20network', 'S60dnsmasq', 'S60rpcd',
                     'S99geomask-syncd'):
            self.write('etc/rc.d/' + link, '')
        self.write('etc/passwd', 'root:x:0:0:root:/root:/bin/ash\n')
        self.write('etc/group', 'root:x:0:\n')
        self.write('etc/mihomo/config.yaml', '# geomask-managed: 1\nsecret: "test"\n')
        self.write('etc/mihomo/ruleset/ai.yaml', 'payload: []\n')
        self.write('etc/magitrickle/state/config.yaml', 'groups:\n  - name: GoogleVideo VPN\n')
        self.write('opt/zapret/ipset/zapret-hosts-user-exclude.txt', 'ours.example\n')
        self.write('opt/zapret/ipset/zapret-hosts-user.txt', 'kept.example\n')
        self.write('usr/lib/geomask/geomask-tool.sh', '#!/bin/sh\n')
        self.write('usr/bin/geomask-tool.sh', '#!/bin/sh\n')
        for key in ('all', 'default', 'br-lan'):
            self.write('proc/sys/net/ipv6/conf/%s/disable_ipv6' % key, '0\n')
        self.write('usr/sbin/https-dns-proxy', '#!/bin/sh\n')

    def run_preflight(self, extra='', body=None):
        body = body or 'reinstall_preflight; printf "RC=%s\\n" "$?"; printf "NOPKG=%s\\n" "$GM_REINSTALL_NO_PKG"'
        script = (SUPPORT.replace('@PREFIX@', posix(self.case))
                  .replace('@PKGLOG@', posix(self.pkg_log))
                  + objects_table()
                  + '\n'.join(extract(name) for name in PREFLIGHT_FUNCTIONS) + '\n'
                  + extra + '\n' + body)
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    def assertRefused(self, result, needle):
        output = result.stdout + result.stderr
        self.assertIn('RC=1', result.stdout, output[-1500:])
        self.assertIn('reinstall not started: unprovable state', result.stdout, output[-1500:])
        self.assertIn(needle, output, output[-1500:])

    def pkg_calls(self):
        return self.pkg_log.read_text(encoding='utf-8')

    # ------------------------------------------------------------------ happy path
    def test_preflight_passes_on_a_healthy_fixture(self):
        result = self.run_preflight()
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('"ok":true', result.stdout)

    def test_preflight_arms_the_no_package_install_guard(self):
        result = self.run_preflight()
        self.assertIn('NOPKG=1', result.stdout, result.stdout + result.stderr)

    # ------------------------------------------------------------------ dependencies
    def test_preflight_refuses_the_auto_lane_branch(self):
        """GM_AUTO_LANE lets a callee create uci/service ids the snapshot cannot journal."""
        result = self.run_preflight(extra='GM_AUTO_LANE=1\n')
        self.assertRefused(result, 'GM_AUTO_LANE')

    def test_preflight_refuses_a_missing_dependency(self):
        result = self.run_preflight(extra='PF_MISSING="crontab"\n')
        self.assertRefused(result, 'dependency missing: crontab')

    def test_preflight_never_installs_a_missing_dependency(self):
        result = self.run_preflight(extra='PF_MISSING="python3"\n')
        self.assertRefused(result, 'dependency missing')
        self.assertEqual('', self.pkg_calls(),
                         'preflight touched apk/opkg while checking dependencies')

    def test_preflight_refuses_when_python_yaml_is_missing(self):
        result = self.run_preflight(extra='PF_YAML_OK=0\n')
        self.assertRefused(result, 'python3-yaml')

    def test_preflight_refuses_auto_package_install_of_https_dns_proxy(self):
        """ai_dns in scope without the worker: step4 would apk-add it - forbidden here."""
        (self.case / 'usr/sbin/https-dns-proxy').unlink()
        result = self.run_preflight(extra='PF_MODULES="ai_dns"\n')
        self.assertRefused(result, 'https-dns-proxy')
        self.assertEqual('', self.pkg_calls(),
                         'preflight attempted a package install')

    def test_preflight_requires_the_nft_toolchain_only_for_ai_dns(self):
        result = self.run_preflight(extra='PF_MODULES="ai_dns"\nPF_MISSING="nft"\n')
        self.assertRefused(result, 'dependency missing: nft')

    # ------------------------------------------------------------------ runtime tri-state
    def test_preflight_refuses_unknown_runtime_state(self):
        """Neither pidof nor pgrep: running state is unknown - unknown is an error."""
        result = self.run_preflight(extra='PF_MISSING="pidof pgrep"\n')
        self.assertRefused(result, 'unobservable')

    def test_preflight_refuses_when_rc_d_is_unobservable(self):
        shutil.rmtree(self.case / 'etc/rc.d')
        result = self.run_preflight()
        self.assertRefused(result, 'rc.d')

    def test_preflight_records_a_determinate_stopped_state_as_fine(self):
        # PIDOF_RC=1 (stopped) is observable - the fixture is healthy with it
        result = self.run_preflight(extra='PIDOF_RC=1\n')
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)

    # ------------------------------------------------------------------ objects
    def test_preflight_refuses_an_unobservable_object(self):
        """Injected observer failure: one tracked object cannot be classified at all."""
        extra = '''
_reinstall_obj_type() {
  case "$1" in
    /etc/passwd) printf 'unknown' ;;
    /etc/group) printf 'regular' ;;
    /etc/mihomo/config.yaml) printf 'regular' ;;
    /etc/magitrickle/state/config.yaml) printf 'regular' ;;
    /opt/zapret/ipset/zapret-hosts-user-exclude.txt) printf 'regular' ;;
    /opt/zapret/ipset/zapret-hosts-user.txt) printf 'regular' ;;
    *) printf 'absent' ;;
  esac
}
'''
        result = self.run_preflight(extra=extra)
        self.assertRefused(result, 'unobservable')

    @unittest.skipUnless(win_symlink.NATIVE_SYMLINKS, win_symlink.SYMLINK_REASON)
    def test_preflight_refuses_a_symlink_where_a_regular_file_is_tracked(self):
        config = self.case / 'etc/mihomo/config.yaml'
        config.unlink()
        os.symlink('missing-target.yaml', config)
        result = self.run_preflight()
        self.assertRefused(result, 'symlink')

    @unittest.skipUnless(win_symlink.NATIVE_SYMLINKS, win_symlink.SYMLINK_REASON)
    def test_preflight_refuses_a_symlink_escaping_the_root(self):
        passwd = self.case / 'etc/passwd'
        passwd.unlink()
        outside = self.root / 'outside.txt'
        outside.write_text('secret\n', encoding='utf-8')
        os.symlink(str(outside), passwd)
        result = self.run_preflight()
        self.assertRefused(result, 'symlink')

    def test_preflight_refuses_unobservable_crontab_state(self):
        result = self.run_preflight(extra='PF_CRONTAB_RC=2\n')
        self.assertRefused(result, 'crontab')

    def test_preflight_refuses_unobservable_kernel_sysctl_state(self):
        shutil.rmtree(self.case / 'proc/sys/net/ipv6')
        result = self.run_preflight()
        self.assertRefused(result, 'sysctl')

    def test_preflight_refuses_unprovable_magitrickle_seed(self):
        """An empty MagiTrickle config would be rewritten by the seeder - no proven rollback."""
        self.write('etc/magitrickle/state/config.yaml', 'groups: []\n')
        result = self.run_preflight()
        self.assertRefused(result, 'magitrickle')

    def test_preflight_accepts_a_configured_magitrickle_state(self):
        result = self.run_preflight()      # fixture has groups
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)

    # ------------------------------------------------------------------ forbidden effects
    def test_pkg_install_is_forbidden_once_armed(self):
        body = ('GM_REINSTALL_NO_PKG=1\n'
                'pkg_install inotifywait; printf "RC=%s\\n" "$?"')
        result = self.run_preflight(body=body)
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)
        self.assertEqual('', self.pkg_calls(), 'apk/opkg ran although reinstall was armed')

    def test_pkg_install_still_works_on_the_normal_install_path(self):
        body = ('pkg_install inotifywait; printf "RC=%s\\n" "$?"')
        result = self.run_preflight(body=body)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('APK add inotifywait', self.pkg_calls())

    # ------------------------------------------------------------------ JSON contract
    def test_refusal_json_error_is_exact(self):
        result = self.run_preflight(extra='PF_MISSING="crontab"\n')
        match = re.search(r'^\{.*\}$', result.stdout, re.M)
        self.assertIsNotNone(match, result.stdout)
        import json
        payload = json.loads(match.group(0))
        self.assertFalse(payload['ok'])
        self.assertEqual('reinstall not started: unprovable state', payload['error'])
        self.assertTrue(payload['reasons'])

    def test_success_json_reports_the_armed_guard(self):
        result = self.run_preflight()
        import json
        match = re.search(r'^\{.*\}$', result.stdout, re.M)
        self.assertIsNotNone(match, result.stdout)
        payload = json.loads(match.group(0))
        self.assertTrue(payload['ok'])
        self.assertEqual('no-package-install', payload.get('armed'))


@unittest.skipIf(BASH is None, 'bash is required')
class ReinstallWiringTests(unittest.TestCase):
    """menu_reinstall must refuse BEFORE the first mutation when preflight says no."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name) / 'case'
        self.case.mkdir()
        (self.case / 'tmp').mkdir()

    def test_menu_reinstall_refuses_before_any_mutation(self):
        extra = r'''
MIHOMO_CONF=/etc/mihomo/config.yaml
GM_VPN_URL=''
GM_ADOPT=0
GM_REPLACE=0
hdr() { printf 'HDR %s\n' "$*" >&2; }
warn() { :; }
bad() { printf 'FAIL %s\n' "$*" >&2; }
info() { :; }
ask() { return 0; }
df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/x 100000 1 90000 1%% /\n'; }
geomask_config_shaped() { return 0; }
# N04: ownership is decided by the tri-state verdict now; the fixture must answer it.
geomask_config_verdict() { return 0; }
_mihomo_guard() { return 0; }
_mihomo_py() { :; }
_prov_url() { printf 'https://example.invalid/sub'; }
_py_yaml_ok() { return 0; }
modules_maybe_prompt() { :; }
prompt_url() { printf 'https://example.invalid/sub'; }
backup_gate() { printf 'BACKUP_GATE\n'; }
mktemp() { printf 'MUTATION_MKTEMP\n'; }
geomask_uninstall() { printf 'UNINSTALL\n'; }
_reinstall_snapshot() { printf 'SNAPSHOT\n'; }
_reinstall_steps() { printf 'STEPS\n'; }
_reinstall_restore() { printf 'RESTORE\n'; }
reinstall_preflight() {
  printf '{"ok":false,"error":"reinstall not started: unprovable state","reasons":["fixture"]}\n'
  return 1
}
'''
        script = (SUPPORT.replace('@PREFIX@', posix(self.case))
                  .replace('@PKGLOG@', posix(self.case / 'pkg.log')) + extra + '\n'
                  + extract('menu_reinstall') + '\n'
                  + 'menu_reinstall; printf "RC=%s\\n" "$?"')
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=60)
        output = result.stdout + result.stderr
        self.assertIn('RC=1', result.stdout, output[-1500:])
        for marker in ('UNINSTALL', 'BACKUP_GATE', 'MUTATION_MKTEMP', 'SNAPSHOT', 'STEPS'):
            self.assertNotIn(marker, output, 'mutation happened although preflight refused')
        self.assertIn('reinstall not started: unprovable state', result.stdout)


if __name__ == '__main__':
    unittest.main()
