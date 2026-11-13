"""Installer behaviour that the sandbox could not see (audit 14.11.2026, B1-B11).

Each test extracts the real function from `geomask.sh.in`, runs it in bash against a
throwaway root, and checks the bytes and the exit code. Nothing here touches the router:
`P` is prefixed, the network tools are stubs, and `mihomo -t` is replaced by a real YAML
parser so a malformed template cannot pass.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'geomask.sh.in'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()
SOURCE = TEMPLATE.read_text(encoding='utf-8')


def extract(name):
    match = re.search(r'^' + re.escape(name) + r'\(\) \{.*?^\}\s*$', SOURCE,
                      re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError('function %s not found in the template' % name)
    return match.group(0)


SUPPORT = r'''set -u
PASS=0; FAILCNT=0; SKIP=0
GM_YES=1; GM_AUTO=1; GM_RESTART=0; GM_CHECK_ONLY=0; GM_ADOPT=0
GM_VPN_URL=''; GM_MODULES=''
GM_PREFIX='@PREFIX@'
MIHOMO_CONF=/etc/mihomo/config.yaml
BACKUP_KEEP=1
GM_REQUIRED_MODULES="core path_vpn"
GM_ALL_MODULES="core luci guard path_vpn path_vpn_nop2p path_ai ai_dns youtube torrent diagnostic doctor device_modes backup_share self_update admission_service"
P() { printf '%s%s' "$GM_PREFIX" "$1"; }
info() { printf 'INFO %s\n' "$*"; }
ok() { printf 'OK %s\n' "$*"; }
warn() { printf 'WARN %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILCNT=$((FAILCNT+1)); }
ask() { return 0; }
sleep() { :; }
have() { command -v "$1" >/dev/null 2>&1; }
lan_ip() { printf '192.168.1.1'; }
lan_iface() { printf 'br-lan'; }
mihomo_tun() { printf 'Mihomo'; }
uci() { return 1; }
'''


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    # already a POSIX path (Linux CI): C:\x -> /c/x, but /tmp/x must stay /tmp/x
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


@unittest.skipIf(BASH is None, 'bash is required')
class InstallerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = self.root / 'case'
        self.case.mkdir()

    def run_script(self, body, functions=(), extra=''):
        script = (SUPPORT.replace('@PREFIX@', posix(self.case)) + extra + '\n'
                  + '\n'.join(extract(name) for name in functions) + '\n' + body)
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    # ---------------------------------------------------------------- B1
    def test_the_fresh_template_is_valid_yaml(self):
        """The template used to emit `listen: %s` and a separate line with the IP."""
        block = re.search(r'  _tmpconf="\$_c\.zmnew"\n(.*?)\n  \} > "\$_tmpconf"',
                          SOURCE, re.DOTALL)
        self.assertIsNotNone(block, 'template block not found')
        body = ('_lanip=192.168.1.1\n_vpnu="https://example.invalid/sub"\n_hwid=""\n'
                '_tmpconf="%s/config.yaml.zmnew"\n' % posix(self.case)
                + block.group(1) + '\n} > "$_tmpconf"\n'
                + 'printf "RC=0\\n"')
        result = self.run_script(body)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        data = yaml.safe_load((self.case / 'config.yaml.zmnew').read_text(encoding='utf-8'))
        listeners = {item['name']: item for item in data['listeners']}
        self.assertEqual(listeners['torrent-direct']['listen'], '192.168.1.1')
        self.assertEqual(str(listeners['torrent-direct']['port']), '7894')
        # every rule target must exist as a group (GLOBAL is built into mihomo)
        groups = {group['name'] for group in data['proxy-groups']}
        for rule in data['rules']:
            parts = rule if isinstance(rule, str) else str(rule)
            target = parts.rsplit(',', 1)[-1].strip()
            if target in ('DIRECT', 'REJECT', 'PASS', 'GLOBAL'):
                continue
            self.assertIn(target, groups, 'rule points at a missing group: ' + parts)

    # ---------------------------------------------------------------- B2
    def test_yaml_package_is_requested_when_python3_is_absent(self):
        extra = r'''
have() {
  case "$1" in
    python3) [ -f python-present ] ;;
    apk|curl) return 0 ;;
    *) return 1 ;;
  esac
}
python3() { [ -f python-present ] && [ -f yaml-present ]; }
apk() {
  printf 'APK %s\n' "$*"
  case "$1" in
    add) for pkg in "$@"; do
           case "$pkg" in
             python3) : > python-present ;;
             python3-yaml) : > yaml-present ;;
           esac
         done ;;
  esac
}
'''
        result = self.run_script('step1_apply; printf "RC=%s\\n" "$?"',
                                 functions=['step1_apply'], extra=extra)
        self.assertIn('python3-yaml', result.stdout)
        self.assertIn('RC=0', result.stdout)
        self.assertTrue((self.case / 'yaml-present').exists())

    # ---------------------------------------------------------------- B5
    def test_a_foreign_config_is_never_overwritten(self):
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('mixed-port: 7891\nsecret: "owner"\n', encoding='utf-8')
        before = conf.read_bytes()
        extra = r'''
geomask_config_shaped() {
  grep -q '^proxy-providers:' "$1" 2>/dev/null || return 1
  grep -qE '^  - name: (AI|VPN-noP2P)$' "$1" 2>/dev/null || return 1
  return 0
}
_prov_url() { printf ''; }
any_provider_url() { printf ''; }
mihomo_validate() { return 0; }
module_selected() { return 0; }
'''
        result = self.run_script('write_mihomo_config "https://example.invalid/x" ""; '
                                 'printf "RC=%s\\n" "$?"',
                                 functions=['write_mihomo_config'], extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('свой конфиг mihomo', result.stdout)
        self.assertEqual(conf.read_bytes(), before)

    # ---------------------------------------------------------------- B9
    def test_save_stream_repairs_a_lost_mode(self):
        target = self.case / 'usr/bin/tool.sh'
        target.parent.mkdir(parents=True)
        target.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        target.chmod(0o644)
        body = ('printf "#!/bin/sh\\nexit 0\\n" | save_stream /usr/bin/tool.sh 755\n'
                'printf "RC=%s MODE=%s\\n" "$?" "$(stat -c %a ' + posix(target) + ')"')
        result = self.run_script(body, functions=['save_stream'],
                                 extra='module_skip_path() { return 1; }\n')
        self.assertIn('RC=0', result.stdout)
        self.assertIn('MODE=755', result.stdout)

    # ---------------------------------------------------------------- B10
    def test_a_failed_trial_restores_the_previous_config(self):
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('BROKEN-CANDIDATE\n', encoding='utf-8')
        pre = self.case / 'pre.yaml'
        pre.write_text('GOOD-ORIGINAL\n', encoding='utf-8')
        extra = r'''
GM_PRE_CONFIG='@PRE@'
GM_BACKUP_PATH=/tmp/backup.tar.gz
GM_RESTART=1
backup_gate() { return 0; }
mihomo_trial_run() { return 1; }
mihomo_restart_verified() { return 1; }
'''.replace('@PRE@', posix(pre))
        result = self.run_script('safe_mihomo_restart; printf "RC=%s\\n" "$?"',
                                 functions=['safe_mihomo_restart'], extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertEqual(conf.read_text(encoding='utf-8'), 'GOOD-ORIGINAL\n')

    # ---------------------------------------------------------------- B4
    def test_a_garbage_archive_is_not_a_backup(self):
        garbage = self.case / 'garbage.tar.gz'
        garbage.write_text('not a gzip stream\n', encoding='utf-8')
        good = self.case / 'good.tar.gz'
        payload = self.case / 'payload.txt'
        payload.write_text('x\n', encoding='utf-8')
        subprocess.run(['tar', 'czf', str(good), '-C', str(self.case), 'payload.txt'],
                       check=True)
        result = self.run_script(
            'archive_ok %s && printf "GOOD=1\\n" || printf "GOOD=0\\n"\n'
            'archive_ok %s && printf "BAD=1\\n" || printf "BAD=0\\n"'
            % (posix(good), posix(garbage)),
            functions=['archive_ok'])
        self.assertIn('GOOD=1', result.stdout)
        self.assertIn('BAD=0', result.stdout)

    def test_a_failed_full_backup_keeps_the_previous_copy(self):
        backups = self.case / 'root/geomask-backups'
        backups.mkdir(parents=True)
        old = backups / 'router-backup-20260101-000000.tar.gz'
        subprocess.run(['tar', 'czf', str(old), '-C', str(self.case), '.'], check=True)
        extra = r'''
have() { case "$1" in sysupgrade|tar|gzip) return 0 ;; esac; command -v "$1" >/dev/null 2>&1; }
sysupgrade() { printf 'garbage' > "$2"; return 0; }
'''
        result = self.run_script('ensure_backup full; printf "RC=%s\\n" "$?"',
                                 functions=['archive_ok', 'ensure_backup'], extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertTrue(old.exists(), 'the previous backup was deleted by a failed run')
        self.assertEqual(sorted(p.name for p in backups.iterdir()),
                         ['router-backup-20260101-000000.tar.gz'])

    # ---------------------------------------------------------------- B11
    def test_required_modules_are_added_to_a_cli_selection(self):
        body = r'''
GM_MODULES="luci"
_valid=""
for _m in $GM_MODULES; do
  case " $GM_ALL_MODULES " in
    *" $_m "*) _valid="$_valid $_m" ;;
  esac
done
GM_MODULES=$(printf '%s' "$_valid" | sed 's/^ *//;s/  */ /g')
for _m in $GM_REQUIRED_MODULES; do
  case " $GM_MODULES " in *" $_m "*) ;; *) GM_MODULES="$GM_MODULES $_m" ;; esac
done
GM_MODULES=$(printf '%s' "$GM_MODULES" | sed 's/^ *//;s/  */ /g')
printf 'MODULES=%s\n' "$GM_MODULES"
'''
        # the snippet above must stay identical to the installer's own block
        installer_block = re.search(
            r'  # core and path_vpn are not optional.*?  GM_MODULES=\$\(printf \'%s\' "\$GM_MODULES" \| sed \'s/\^ \*//;s/  \*/ /g\'\)\n',
            SOURCE, re.DOTALL)
        self.assertIsNotNone(installer_block, 'required-module block not found')
        self.assertIn('GM_REQUIRED_MODULES', installer_block.group(0))
        result = self.run_script(body)
        self.assertIn('MODULES=luci core path_vpn', result.stdout)


if __name__ == '__main__':
    unittest.main()
