"""Installer behaviour that the sandbox could not see (audit 14.11.2026, B1-B11).

Each test extracts the real function from `geomask.sh.in`, runs it in bash against a
throwaway root, and checks the bytes and the exit code. Nothing here touches the router:
`P` is prefixed, the network tools are stubs, and `mihomo -t` is replaced by a real YAML
parser so a malformed template cannot pass.
"""
import gzip
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
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
SAVE_FAIL=0
GM_YES=1; GM_AUTO=1; GM_RESTART=0; GM_CHECK_ONLY=0; GM_ADOPT=0
GM_VPN_URL=''; GM_MODULES=''
GM_PREFIX='@PREFIX@'
GM_PYTHON=''
# The router has python3; a Windows test host may only expose `python`.
if ! command -v python3 >/dev/null 2>&1 && command -v python >/dev/null 2>&1; then
  GM_PYTHON=python
fi
MIHOMO_CONF=/etc/mihomo/config.yaml
BACKUP_DIR=/root/geomask-backups
BACKUP_KEEP=1
BACKUP_RECOVERY=/etc/geomask/backup-recovery
SMB_SHARE_NAME=geomask-backups
SMB_SHARE_ENABLE=/etc/geomask/backup-share.enable
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
        # /tmp always exists on a router; the fake root needs it for the backup park
        (self.case / 'tmp').mkdir()

    def run_script(self, body, functions=(), extra=''):
        script = (SUPPORT.replace('@PREFIX@', posix(self.case))
                  .replace('@PY@', posix(sys.executable)) + extra + '\n'
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
        # a controller on 0.0.0.0 without a secret let any LAN device switch nodes
        self.assertEqual(data['external-controller'], '127.0.0.1:9090')
        # every rule target must exist as a group (GLOBAL is built into mihomo)
        groups = {group['name'] for group in data['proxy-groups']}
        for rule in data['rules']:
            parts = rule if isinstance(rule, str) else str(rule)
            target = parts.rsplit(',', 1)[-1].strip()
            if target in ('DIRECT', 'REJECT', 'PASS', 'GLOBAL'):
                continue
            self.assertIn(target, groups, 'rule points at a missing group: ' + parts)

    # ---------------------------------------------------------------- IPv6 default
    def _fake_backend(self, answer):
        log = self.case / 'backend-calls'
        be = self.case / 'usr' / 'bin' / 'geo-backend.sh'
        be.parent.mkdir(parents=True, exist_ok=True)
        text = ('#!/bin/sh\n'
                'printf "%s\\n" "$*" >> "@LOG@"\n'
                'printf \'@ANSWER@\\n\'\n').replace('@LOG@', posix(log)).replace('@ANSWER@', answer)
        be.write_text(text, encoding='utf-8', newline='\n')
        os.chmod(be, 0o755)
        return log

    def test_ipv6_is_disabled_by_default(self):
        log = self._fake_backend('{"ok":true}')
        result = self.run_script('apply_ipv6_default; printf "RC=%s\\n" "$?"',
                                 functions=['apply_ipv6_default'], extra='GM_IPV6=off\n')
        self.assertIn('RC=0', result.stdout)
        self.assertEqual(log.read_text(encoding='utf-8').strip(), 'ipv6 off')

    def test_ipv6_keep_never_calls_the_backend(self):
        log = self._fake_backend('{"ok":true}')
        result = self.run_script('apply_ipv6_default; printf "RC=%s\\n" "$?"',
                                 functions=['apply_ipv6_default'], extra='GM_IPV6=keep\n')
        self.assertIn('RC=0', result.stdout)
        self.assertFalse(log.exists())

    def test_a_refused_ipv6_disable_is_reported_as_failure(self):
        self._fake_backend('{"ok":false,"error":"uci write failed"}')
        result = self.run_script('apply_ipv6_default; printf "RC=%s\\n" "$?"',
                                 functions=['apply_ipv6_default'], extra='GM_IPV6=off\n')
        self.assertIn('RC=1', result.stdout)
        self.assertIn('uci write failed', result.stdout)

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
_prov_url() { printf ''; }
any_provider_url() { printf ''; }
mihomo_validate() { return 0; }
module_selected() { return 0; }
'''
        result = self.run_script('write_mihomo_config "https://example.invalid/x" ""; '
                                 'printf "RC=%s\\n" "$?"',
                                 functions=['_mihomo_py', 'geomask_config_shaped', 'write_mihomo_config'],
                                 extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('свой конфиг mihomo', result.stdout)
        self.assertEqual(conf.read_bytes(), before)

    def test_replace_config_rewrites_a_foreign_file(self):
        """--replace-config is the owner's explicit decision: it must pass the guard (F16)."""
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('mixed-port: 7891\nsecret: "owner"\n', encoding='utf-8')
        extra = r'''
_prov_url() { printf ''; }
any_provider_url() { printf ''; }
mihomo_validate() { return 0; }
module_selected() { return 0; }
GM_REPLACE=1
'''
        result = self.run_script('write_mihomo_config "https://example.invalid/x" ""; '
                                 'printf "RC=%s\\n" "$?"',
                                 functions=['_mihomo_py', 'geomask_config_shaped',
                                            'write_mihomo_config'],
                                 extra=extra)
        self.assertIn('RC=0', result.stdout)
        data = conf.read_text(encoding='utf-8')
        self.assertIn('# geomask-managed: 1', data)
        self.assertNotIn('secret: "owner"', data)
        self.assertIn('https://example.invalid/x', data)

    def test_a_shaped_config_is_not_replaced_by_the_template_without_the_flag(self):
        """A config can pass the ownership check and still not fit the in-place branch.

        Falling through to the template used to drop tun/dns/secret even though the file
        was recognised as geomask's own.
        """
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('# geomask-managed: 1\nmixed-port: 7890\n'
                        'tun:\n  enable: true\ndns:\n  enable: true\nsecret: "keep-me"\n',
                        encoding='utf-8')
        before = conf.read_bytes()
        extra = r'''
_prov_url() { printf 'https://example.invalid/old'; }
any_provider_url() { printf 'https://example.invalid/old'; }
mihomo_validate() { return 0; }
module_selected() { return 0; }
GM_ADOPT=1
'''
        result = self.run_script('write_mihomo_config "https://example.invalid/x" ""; '
                                 'printf "RC=%s\\n" "$?"',
                                 functions=['_mihomo_py', 'geomask_config_shaped', 'write_mihomo_config'],
                                 extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('--replace-config', result.stdout)
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
        payload = self.case / 'etc/config/network'
        payload.parent.mkdir(parents=True)
        payload.write_text('x\n', encoding='utf-8')
        subprocess.run(['tar', 'czf', str(good), '-C', str(self.case), 'etc'], check=True)
        result = self.run_script(
            'archive_ok %s && printf "GOOD=1\\n" || printf "GOOD=0\\n"\n'
            'archive_ok %s && printf "BAD=1\\n" || printf "BAD=0\\n"'
            % (posix(good), posix(garbage)),
            functions=['archive_ok'])
        self.assertIn('GOOD=1', result.stdout)
        self.assertIn('BAD=0', result.stdout)

    def test_a_gzip_stream_that_is_not_tar_is_not_a_backup(self):
        """`gzip -t` only proves the stream decompresses; it says nothing about the format.

        The old archive_ok fell back to it after tar failed, so arbitrary compressed bytes
        replaced the last good backup.
        """
        fake = self.case / 'not-tar.tar.gz'
        fake.write_bytes(gzip.compress(b'this is not a tar archive'))
        result = self.run_script(
            'archive_ok %s && printf "FAKE=1\\n" || printf "FAKE=0\\n"' % posix(fake),
            functions=['archive_ok'])
        self.assertIn('FAKE=0', result.stdout)

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
                                 functions=['_backup_park_restore', 'archive_ok', 'ensure_backup',
                                            '_backup_recovery_write', '_backup_recovery_park',
                                            '_backup_recovery_clear', '_backup_keep_failed',
                                            '_bnote', '_bwarn', '_bbad'],
                                 extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertTrue(old.exists(), 'the previous backup was deleted by a failed run')
        self.assertEqual(sorted(p.name for p in backups.iterdir()),
                         ['router-backup-20260101-000000.tar.gz'])

    def test_a_second_attempt_keeps_the_parked_copy(self):
        """Two runs in one shell used to `rm -rf` the same park at the start (F03)."""
        backups = self.case / 'root/geomask-backups'
        backups.mkdir(parents=True)
        old = backups / 'router-backup-20260101-000000.tar.gz'
        subprocess.run(['tar', 'czf', str(old), '-C', str(self.case), '.'], check=True)
        before = old.read_bytes()
        extra = r'''
have() { case "$1" in sysupgrade|tar|gzip) return 0 ;; esac; command -v "$1" >/dev/null 2>&1; }
sysupgrade() { return 1; }
mv() {
  case "$2" in @BKD@/*) command mv "$@"; return ;; esac
  case "$3" in @BKD@/*) return 1 ;; esac
  command mv "$@"
}
'''.replace('@BKD@', posix(backups))
        body = ('ensure_backup full; printf "FIRST=%s\\n" "$?"\n'
                'ensure_backup full; printf "SECOND=%s\\n" "$?"\n')
        result = self.run_script(body,
                                 functions=['_backup_park_restore', 'archive_ok', 'ensure_backup',
                                            '_backup_recovery_write', '_backup_recovery_park',
                                            '_backup_recovery_clear', '_backup_keep_failed',
                                            '_bnote', '_bwarn', '_bbad'],
                                 extra=extra)
        self.assertIn('FIRST=1', result.stdout)
        self.assertIn('SECOND=1', result.stdout)
        parks = list((self.case / 'tmp').glob('geomask-bk-park-*'))
        self.assertEqual(len(parks), 1, 'the park directory was recreated or deleted')
        kept = list(parks[0].glob('*.tar.gz'))
        self.assertEqual(len(kept), 1, 'the only backup copy was destroyed')
        self.assertEqual(kept[0].read_bytes(), before)
        # the second attempt must say where the copy is, not just "failed"
        self.assertIn(posix(parks[0]), result.stdout + result.stderr)

    # ---------------------------------------------------------------- B5b
    def test_both_yaml_list_styles_count_as_a_geomask_config(self):
        """mihomo and panel tools write groups at column 0; the template uses two spaces."""
        indented = self.case / 'indented.yaml'
        indented.write_text('proxy-providers:\n  sub:\n    type: http\n'
                            'proxy-groups:\n  - name: AI\n  - name: VPN-noP2P\n',
                            encoding='utf-8')
        flat = self.case / 'flat.yaml'
        flat.write_text('proxy-providers:\n  sub:\n    type: http\n'
                        'proxy-groups:\n- name: AI\n- name: VPN-noP2P\n', encoding='utf-8')
        foreign = self.case / 'foreign.yaml'
        foreign.write_text('proxy-providers:\n  mine:\n    type: http\n'
                           'proxy-groups:\n- name: MyGroup\n', encoding='utf-8')
        result = self.run_script(
            'for f in %s %s %s; do '
            'geomask_config_shaped "$f" && printf "%%s SHAPED\\n" "$f" '
            '|| printf "%%s FOREIGN\\n" "$f"; done'
            % (posix(indented), posix(flat), posix(foreign)),
            functions=['_mihomo_py', 'geomask_config_shaped'])
        self.assertIn('indented.yaml SHAPED', result.stdout)
        self.assertIn('flat.yaml SHAPED', result.stdout)
        self.assertIn('foreign.yaml FOREIGN', result.stdout)

    # ---------------------------------------------------------------- B11
    def test_required_modules_are_added_to_a_cli_selection(self):
        """The installer's own normalize_modules, not a copy of its logic."""
        result = self.run_script('printf "MODULES=%s\\n" "$(normalize_modules luci)"',
                                 functions=['normalize_modules'])
        self.assertIn('MODULES=luci core path_vpn', result.stdout)

    def test_an_unknown_module_is_refused(self):
        result = self.run_script('normalize_modules "luci nope"; printf "RC=%s\\n" "$?"',
                                 functions=['normalize_modules'])
        self.assertIn('RC=2', result.stdout)
        self.assertIn('unknown module: nope', result.stdout + result.stderr)

    # ---------------------------------------------------------------- T05
    def test_reinstall_snapshot_restores_removed_components(self):
        """The rollback path must put back exactly what uninstall removed."""
        tool = self.case / 'usr/bin/geomask-tool.sh'
        tool.parent.mkdir(parents=True)
        tool.write_text('#!/bin/sh\necho tool\n', encoding='utf-8', newline='\n')
        tool.chmod(0o755)
        backend = self.case / 'usr/bin/geo-backend.sh'
        backend.write_text('OLD_BACKEND\n', encoding='utf-8')
        sync = self.case / 'root/sync_mtrickle_mihomo.sh'
        sync.parent.mkdir(parents=True)
        sync.write_text('OLD_SYNC\n', encoding='utf-8')
        initd = self.case / 'etc/init.d/geomask-syncd'
        initd.parent.mkdir(parents=True)
        initd.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        rc = self.case / 'etc/rc.d/S99geomask-syncd'
        rc.parent.mkdir(parents=True)
        rc.write_text('', encoding='utf-8')
        cron = self.case / 'cron.txt'
        cron.write_text('*/5 * * * * /usr/bin/geomask-tool.sh\n', encoding='utf-8')
        extra = r'''
REINSTALL_TRACKED='/etc/init.d/geomask-syncd /usr/bin/geo-backend.sh /root/sync_mtrickle_mihomo.sh'
CRON_FILE='@CRON@'
INITD_LOG='@INITD@'
crontab() {
  if [ "$1" = "-l" ]; then cat "$CRON_FILE" 2>/dev/null; else cp "$1" "$CRON_FILE"; fi
}
initd() { printf '%s\n' "$*" >> "$INITD_LOG"; }
'''.replace('@CRON@', posix(cron)).replace('@INITD@', posix(self.case / 'initd.log'))
        body = ('snap="%s/snap"; rm -rf "$snap"\n'
                '_reinstall_snapshot "$snap" || { printf "SNAP_FAIL\\n"; exit 1; }\n'
                'rm -rf "%s/usr" "%s/etc/init.d" "%s/etc/rc.d"\n'
                'rm -f "%s/root/sync_mtrickle_mihomo.sh"\n'
                'rm -f "$CRON_FILE"\n'
                'mkdir -p "%s/usr/bin"; printf "new\\n" > "%s/usr/bin/geomask-leftover"\n'
                'printf "NEW_BACKEND\\n" > "%s/usr/bin/geo-backend.sh"\n'
                '_reinstall_restore "$snap"; printf "RC=%%s\\n" "$?"\n'
                % (posix(self.case), posix(self.case), posix(self.case), posix(self.case),
                   posix(self.case), posix(self.case), posix(self.case), posix(self.case)))
        result = self.run_script(body,
                                 functions=['_reinstall_save', '_reinstall_snapshot',
                                            '_reinstall_restore'],
                                 extra=extra)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertTrue(tool.exists(), 'the tool script was not restored')
        self.assertEqual(tool.read_text(encoding='utf-8'), '#!/bin/sh\necho tool\n')
        self.assertEqual(backend.read_text(encoding='utf-8'), 'OLD_BACKEND\n',
                         'the previous backend was not restored')
        self.assertEqual(sync.read_text(encoding='utf-8'), 'OLD_SYNC\n',
                         'the sync script was not restored')
        self.assertTrue(initd.exists())
        self.assertTrue(rc.exists())
        self.assertIn('geomask-tool.sh', cron.read_text(encoding='utf-8'))
        self.assertIn('geomask-syncd start',
                      (self.case / 'initd.log').read_text(encoding='utf-8'))
        self.assertFalse((self.case / 'usr/bin/geomask-leftover').exists(),
                         'a file created by the failed install survived the rollback')
        self.assertFalse((self.case / 'snap').exists(), 'the snapshot was not cleaned up')

    def test_reinstall_refuses_a_foreign_config_before_removing_anything(self):
        """The old order uninstalled the panel/cron and only then refused the config."""
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('mixed-port: 7891\nsecret: "owner"\n', encoding='utf-8')
        extra = r'''
hdr() { :; }
warn() { :; }
ask() { return 0; }
backup_gate() { return 0; }
modules_maybe_prompt() { :; }
prompt_url() { printf 'https://example.invalid/sub'; }
_prov_url() { printf ''; }
geomask_uninstall() { printf 'UNINSTALLED\n'; }
df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/x 100000 1 90000 1%% /\n'; }
'''
        result = self.run_script('menu_reinstall; printf "RC=%s\\n" "$?"',
                                 functions=['_mihomo_py', 'geomask_config_shaped', 'menu_reinstall'],
                                 extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertNotIn('UNINSTALLED', result.stdout)
        self.assertIn('свой конфиг mihomo', result.stdout)

    # ---------------------------------------------------------------- T13
    def test_a_failed_restart_fails_the_step(self):
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('# geomask-managed: 1\nproxy-providers:\n  sub:\n'
                        'proxy-groups:\n  - name: VPN-noP2P\n  - name: AI\n', encoding='utf-8')
        extra = r'''
GM_RESTART=1
GM_VPN_URL="https://example.invalid/sub"
ask() { return 0; }
write_mihomo_config() { return 0; }
safe_mihomo_restart() { return 1; }
api_refresh_provider() { return 0; }
'''
        result = self.run_script('step3_apply; printf "RC=%s\\n" "$?"',
                                 functions=['geomask_config_shaped', 'step3_apply'], extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('шаг 3 не завершён', result.stdout)


if __name__ == '__main__':
    unittest.main()

