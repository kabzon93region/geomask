"""Installer behaviour that the sandbox could not see (audit 14.11.2026, B1-B11).

Each test extracts the real function from `geomask.sh.in`, runs it in bash against a
throwaway root, and checks the bytes and the exit code. Nothing here touches the router:
`P` is prefixed, the network tools are stubs, and `mihomo -t` is replaced by a real YAML
parser so a malformed template cannot pass.
"""
import gzip
import json
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import win_symlink  # noqa: E402  (test-support helper in the same directory)
TEMPLATE = ROOT / 'geomask.sh.in'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()
SOURCE = TEMPLATE.read_text(encoding='utf-8')


REINSTALL_TABLES = ['REINSTALL_PREFLIGHT_OBJECTS', 'REINSTALL_PREFLIGHT_BYTE_FILES',
                    'REINSTALL_SHARED_APPEND', 'REINSTALL_UCI_OBJECTS',
                    'REINSTALL_LIST_DIRS', 'REINSTALL_TEMP_SUFFIXES',
                    'REINSTALL_NR_STATE']


def reinstall_tables():
    """Product-side manifest tables, straight from the template (not a test copy)."""
    out = ''
    for name in REINSTALL_TABLES:
        match = re.search(r'^' + name + r'="[^"]*"', SOURCE, re.MULTILINE | re.DOTALL)
        if match:
            out += match.group(0) + '\n'
    for match in re.finditer(r'^(AIDNS_[A-Z_]+=.*)$', SOURCE, re.MULTILINE):
        out += match.group(1) + '\n'
    return out


def reinstall_function_names():
    found = re.findall(r'^(_rs_[A-Za-z0-9_]+|_reinstall_(?:save|snapshot|restore))\(\) \{',
                       SOURCE, re.MULTILINE)
    names = [name for name in found if extract(name)]
    # The cron lock helpers are part of every restore path but are not _rs_*/_reinstall_*.
    return names + ['_cron_begin', '_cron_end']


def install_safety_stubs():
    """Injected runtime adapters for the reinstall snapshot/restore tests."""
    return r'''
RUNTIME='@RUNTIME@'
# The cron lock helpers keep their nesting depth in this variable; a fixture that extracts the
# helpers without the product's top-level assignment would trip `set -u`.
_CRON_LOCK_DEPTH=0
# The fixture root has no shared append files and no uci: phase 2b covers both with the
# dedicated fixture suite, this file keeps testing the byte/links/services contract.
REINSTALL_SHARED_APPEND=''
REINSTALL_UCI_OBJECTS=''
module_selected() { return 0; }
have() {
  case "$1" in
    pidof|pgrep|netstat|crontab|uci|ip|nft|sysctl) return 0 ;;
  esac
  command -v "$1" >/dev/null 2>&1
}
ip() { return 0; }
nft() { return 1; }
uci() { return 1; }
sysctl() { return 0; }
pidof() { [ -f "$RUNTIME/$1" ] && return 0; return 1; }
# pgrep covers script-based services and must follow the fixture, not the host: the reinstall
# test stops the service (its pid file goes away with /etc/init.d) and expects the rollback to
# start it again. A stub that always answers "running" would silently stop testing that path.
pgrep() {
  case "$*" in
    *geomask-syncd.sh*) [ -f "$RUNTIME/geomask-syncd" ] && return 0 ;;
  esac
  return 1
}
netstat() { return 1; }
'''



def extract(name):
    """Whole function body; a one-line helper stays a one-liner.

    The old regex stopped at the first line that was exactly `}`. A one-line function such as
    `_cron_end() { ...; }` has no such line, so the match ran on into the next function and
    silently pulled unrelated code into the fixture. One-liners are returned as the single
    line they are; everything else keeps the documented "ends at a column-0 brace" rule.
    """
    lines = SOURCE.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(name + '() {'):
            start = index
            break
    if start is None:
        raise AssertionError('function %s not found in the template' % name)
    head = lines[start].rstrip()
    if head != name + '() {' and head.endswith('}'):
        return head
    for index in range(start + 1, len(lines)):
        if lines[index].rstrip() == '}':
            return '\n'.join(lines[start:index + 1])
    raise AssertionError('function %s has no closing brace' % name)


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
# Product constant used by the selection functions (I10); the harness extracts functions only.
MODULES_STATE=/etc/geomask/modules.json
# The product probes the interpreter; the fixture must offer the same helper.
_mihomo_py() { if [ -n "${GM_PYTHON:-}" ]; then "$GM_PYTHON" - "$@" 2>/dev/null; return; fi; python3 - "$@" 2>/dev/null; }
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
        (self.case / 'etc/rc.d').mkdir(parents=True)
        for key in ('all', 'default'):
            (self.case / 'proc/sys/net/ipv6/conf' / key).mkdir(parents=True)
            (self.case / 'proc/sys/net/ipv6/conf' / key / 'disable_ipv6').write_text(
                '0\n', encoding='utf-8', newline='\n')

    def run_script(self, body, functions=(), extra=''):
        names = list(functions)
        if 'write_mihomo_config' in names and '_write_mihomo_config_unlocked' not in names:
            # R04: the public writer is a thin config-tx lock wrapper around the old body.
            names.append('_write_mihomo_config_unlocked')
        if 'safe_mihomo_restart' in names and '_mihomo_restore_preimage' not in names:
            # R04 round 4: the preimage rollback is serialised through its own lock helper.
            names.append('_mihomo_restore_preimage')
        script = (SUPPORT.replace('@PREFIX@', posix(self.case))
                  .replace('@PY@', posix(sys.executable))
                  + '\nGM_LOCK_DIR="%s/locks"\nGM_CONFIG_TX_LOCK="%s/tx.lock"\n'
                    'GM_LOCK_LIB="%s"\n. "$GM_LOCK_LIB"\n'
                    % (posix(self.case), posix(self.case), posix(ROOT / 'assets' / 'geomask-lock.sh'))
                  + extra + '\n'
                  + '\n'.join(extract(name) for name in names) + '\n' + body)
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, posix(path)], cwd=str(self.case),
                              env=win_symlink.bash_env(), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    # ---------------------------------------------------------------- B1
    def test_the_fresh_template_is_valid_yaml(self):
        """The template used to emit `listen: %s` and a separate line with the IP."""
        block = re.search(r'  _tmpconf="\$_c\.zmnew\.\$_wpid"\n(.*?)\n  \} > "\$_tmpconf"',
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
                                 functions=['_mihomo_py', 'geomask_config_verdict', '_mihomo_guard',
                                            'geomask_config_shaped', 'write_mihomo_config'],
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
                                 functions=['_mihomo_py', 'geomask_config_verdict', '_mihomo_guard',
                                            'geomask_config_shaped', 'write_mihomo_config'],
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
                                 functions=['_mihomo_py', 'geomask_config_verdict', '_mihomo_guard',
                                            'geomask_config_shaped', 'write_mihomo_config'],
                                 extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('--replace-config', result.stdout)
        self.assertEqual(conf.read_bytes(), before)

    # ---------------------------------------------------------------- I05 adopt
    def adopt_fixture(self, text):
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(text, encoding='utf-8')
        tool = self.case / 'usr/lib/geomask/geomask-mihomo-adopt.py'
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text((ROOT / 'assets' / 'geomask-mihomo-adopt.py').read_text(encoding='utf-8'),
                        encoding='utf-8')
        return conf

    FOREIGN_CONF = ('mixed-port: 7890\n'
                    'secret: "owner"\n'
                    'tun:\n  enable: true\n  device: utun-owner\n'
                    'proxies:\n  - name: owner-node\n    type: ss\n'
                    '    server: 203.0.113.10\n    port: 8388\n'
                    '    cipher: aes-128-gcm\n    password: "sentinel"\n'
                    'proxy-groups:\n  - name: owner-group\n    type: select\n'
                    '    proxies:\n      - owner-node\n'
                    'rules:\n  - MATCH,owner-group\n')

    ADOPT_EXTRA = r"""
_prov_url() { printf ''; }
any_provider_url() { printf ''; }
mihomo_validate() { return 0; }
module_selected() { return 0; }
GM_ADOPT=1
"""

    def test_adopt_merges_a_foreign_config_and_keeps_the_owner_settings(self):
        """I05: --adopt-config must actually adopt, not pass the guard and do nothing."""
        conf = self.adopt_fixture(self.FOREIGN_CONF)
        result = self.run_script('write_mihomo_config "https://example.invalid/x" ""; '
                                 'printf "RC=%s\\n" "$?"',
                                 functions=['_mihomo_py', '_gm_py_script', 'geomask_config_verdict',
                                            '_mihomo_guard', 'geomask_config_shaped',
                                            'write_mihomo_config'],
                                 extra=self.ADOPT_EXTRA)
        self.assertIn('RC=0', result.stdout)
        data = conf.read_text(encoding='utf-8')
        self.assertIn('# geomask-managed: 1', data)
        self.assertIn('https://example.invalid/x', data)
        self.assertIn('secret: "owner"', data, 'adoption dropped the owner secret')
        self.assertIn('device: utun-owner', data, 'adoption dropped the owner tun block')
        self.assertIn('MATCH,owner-group', data, 'adoption dropped the owner rules')
        self.assertIn('VPN-noP2P', data)

    def test_adopt_refusal_leaves_the_live_config_untouched(self):
        """A config that cannot be merged is refused before the first write."""
        conf = self.adopt_fixture('mixed-port: 7890\nsecret: "owner"\n')
        before = conf.read_bytes()
        result = self.run_script('write_mihomo_config "https://example.invalid/x" ""; '
                                 'printf "RC=%s\\n" "$?"',
                                 functions=['_mihomo_py', '_gm_py_script', 'geomask_config_verdict',
                                            '_mihomo_guard', 'geomask_config_shaped',
                                            'write_mihomo_config'],
                                 extra=self.ADOPT_EXTRA)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('adopt', result.stdout)
        self.assertIn('--replace-config', result.stdout)
        self.assertEqual(conf.read_bytes(), before, 'a refused adoption rewrote the live config')
        leftovers = [p.name for p in conf.parent.iterdir() if '.adopt.' in p.name]
        self.assertEqual(leftovers, [], 'a refused adoption left a candidate behind')

    def test_adopt_does_not_replace_the_live_file_when_mihomo_rejects_the_candidate(self):
        conf = self.adopt_fixture(self.FOREIGN_CONF)
        before = conf.read_bytes()
        extra = self.ADOPT_EXTRA.replace('mihomo_validate() { return 0; }',
                                         'mihomo_validate() { return 1; }')
        result = self.run_script('write_mihomo_config "https://example.invalid/x" ""; '
                                 'printf "RC=%s\\n" "$?"',
                                 functions=['_mihomo_py', '_gm_py_script', 'geomask_config_verdict',
                                            '_mihomo_guard', 'geomask_config_shaped',
                                            'write_mihomo_config'],
                                 extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('mihomo -t', result.stdout)
        self.assertEqual(conf.read_bytes(), before,
                         'a candidate rejected by mihomo -t replaced the live config')
        leftovers = [p.name for p in conf.parent.iterdir() if '.adopt.' in p.name]
        self.assertEqual(leftovers, [], 'a rejected candidate was left behind')

    # ---------------------------------------------------------------- N04 ownership verdict
    def test_an_unreadable_config_is_not_treated_as_a_foreign_one(self):
        """N04: no PyYAML must refuse on its own terms, never offer --replace-config.

        A missing parser used to make every config "not shaped", so the operator was told the
        file was somebody else's and offered a destructive replacement of what may well be
        geomask's own config.
        """
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('mixed-port: 7890\nsecret: "owner"\n', encoding='utf-8')
        before = conf.read_bytes()
        extra = r"""
_mihomo_py() { return 2; }
_prov_url() { printf ''; }
any_provider_url() { printf ''; }
mihomo_validate() { return 0; }
module_selected() { return 0; }
"""
        result = self.run_script('write_mihomo_config "https://example.invalid/x" ""; '
                                 'printf "RC=%s\\n" "$?"',
                                 functions=['geomask_config_verdict', '_mihomo_guard',
                                            'geomask_config_shaped', 'write_mihomo_config'],
                                 extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('не читается', result.stdout)
        self.assertNotIn('--replace-config', result.stdout,
                         'an unreadable config was offered for replacement')
        self.assertEqual(conf.read_bytes(), before, 'an unreadable config was modified')

    def test_a_foreign_config_still_gets_the_ownership_message(self):
        """The tri-state verdict must not weaken the foreign-config guard."""
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('mixed-port: 7890\nsecret: "owner"\n', encoding='utf-8')
        before = conf.read_bytes()
        extra = r"""
_prov_url() { printf ''; }
any_provider_url() { printf ''; }
mihomo_validate() { return 0; }
module_selected() { return 0; }
"""
        result = self.run_script('write_mihomo_config "https://example.invalid/x" ""; '
                                 'printf "RC=%s\\n" "$?"',
                                 functions=['_mihomo_py', 'geomask_config_verdict',
                                            '_mihomo_guard', 'geomask_config_shaped',
                                            'write_mihomo_config'],
                                 extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('свой конфиг mihomo', result.stdout)
        self.assertIn('--replace-config', result.stdout)
        self.assertEqual(conf.read_bytes(), before)

    # ---------------------------------------------------------------- I11 selection prune
    def _catalogue(self, modules):
        path = self.case / 'etc/geomask/modules-index.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema': 1, 'modules': modules}, indent=2),
                        encoding='utf-8', newline='\n')
        return path

    def test_narrowing_the_selection_removes_the_dropped_module_runtime(self):
        """I11: a module dropped from the selection must not keep running behind the claim.

        The guard used to keep its hotplug hook and its 5-minute cron line, and AI-DNS kept its
        services and nft table, after `--modules` narrowed the selection.
        """
        self._catalogue({
            'core': {'id': 'core', 'owned_resources': []},
            'guard': {'id': 'guard', 'owned_resources': [
                {'type': 'file', 'path': '/usr/bin/geomask-guard.sh'},
                {'type': 'file', 'path': '/etc/hotplug.d/iface/99-geomask-guard'},
                {'type': 'cron', 'path': 'geomask-guard'}]},
            'ai_dns': {'id': 'ai_dns', 'owned_resources': [
                {'type': 'service', 'path': 'ai-doh'},
                {'type': 'file', 'path': '/etc/init.d/ai-doh'},
                {'type': 'nft', 'path': 'geomask_ai_dns'}]},
        })
        guard = self.case / 'usr/bin/geomask-guard.sh'
        hook = self.case / 'etc/hotplug.d/iface/99-geomask-guard'
        initd = self.case / 'etc/init.d/ai-doh'
        calls = self.case / 'initd.calls'
        for path in (guard, hook):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
            path.chmod(0o755)
        # The real init script is what the product runs: it must log stop+disable itself.
        initd.parent.mkdir(parents=True, exist_ok=True)
        initd.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "' + posix(calls) + '"\nexit 0\n',
                         encoding='utf-8', newline='\n')
        initd.chmod(0o755)
        cron_state = self.case / 'crontab.txt'
        cron_state.write_text('*/5 * * * * /usr/bin/geomask-guard.sh >/dev/null 2>&1\n'
                              '0 3 * * * /usr/bin/other-thing\n', encoding='utf-8', newline='\n')
        state = self.case / 'etc/geomask/modules.json'
        state.write_text(json.dumps({'selected': ['core', 'guard', 'ai_dns']}),
                         encoding='utf-8', newline='\n')
        extra = r"""
GM_MODULES="core"
crontab() {
  if [ "${1:-}" = "-l" ]; then cat "@CRON@"; else cat > "@CRON@"; fi
}
"""
        extra = extra.replace('@CRON@', posix(cron_state))
        body = ('modules_state_sync; printf "RC=%s\\n" "$?"'
                '\nprintf "STATE=%s\\n" "$(cat ' + posix(state) + ')"\n')
        result = self.run_script(
            body,
            functions=['_mihomo_py', 'modules_state_read', 'modules_state_write',
                       'module_effective_list', 'module_owned_resources',
                       'modules_remove_runtime', 'modules_prune_selection',
                       'modules_state_sync', '_cron_begin', '_cron_end'],
            extra=extra)
        self.assertIn('RC=0', result.stdout, result.stderr)
        self.assertFalse(guard.exists(), 'the guard script of a dropped module stayed')
        self.assertFalse(hook.exists(), 'the hotplug hook of a dropped module stayed')
        self.assertFalse(initd.exists(), 'the AI-DNS init script stayed')
        cron = cron_state.read_text(encoding='utf-8')
        self.assertNotIn('geomask-guard', cron, 'the guard cron line stayed')
        self.assertIn('other-thing', cron, 'an unrelated cron line was removed')
        service_calls = calls.read_text(encoding='utf-8')
        self.assertIn('stop', service_calls,
                      'the AI-DNS service was not stopped')
        self.assertIn('disable', service_calls,
                      'the AI-DNS service was not disabled (autostart link stayed)')
        self.assertIn('core', state.read_text(encoding='utf-8'))

    def test_a_still_selected_module_keeps_its_runtime(self):
        self._catalogue({
            'core': {'id': 'core', 'owned_resources': []},
            'guard': {'id': 'guard', 'owned_resources': [
                {'type': 'file', 'path': '/usr/bin/geomask-guard.sh'}]},
        })
        guard = self.case / 'usr/bin/geomask-guard.sh'
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        state = self.case / 'etc/geomask/modules.json'
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'selected': ['core', 'guard']}), encoding='utf-8', newline='\n')
        extra = 'GM_MODULES="core guard"\n'
        result = self.run_script('modules_state_sync; printf "RC=%s\\n" "$?"',
                                 functions=['_mihomo_py', 'modules_state_read',
                                            'modules_state_write', 'module_effective_list',
                                            'module_owned_resources', 'modules_remove_runtime',
                                            'modules_prune_selection', 'modules_state_sync',
                                            '_cron_begin', '_cron_end'],
                                 extra=extra)
        self.assertIn('RC=0', result.stdout, result.stderr)
        self.assertTrue(guard.exists(), 'a module that is still selected lost its files')


    # ---------------------------------------------------------------- P10 start order
    def test_the_ifup_hook_exists_before_mihomo_is_started(self):
        """P10: the first mihomo start ran before the hook that restores tables 100/102."""
        body = extract('step3_apply')
        self.assertIn('99-default-vpn-route', body,
                      'step3 starts mihomo without installing the ifup hook first')
        self.assertLess(body.index('99-default-vpn-route'), body.index('safe_mihomo_restart'),
                        'the ifup hook is installed only after mihomo is started')

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
        parks = list((self.case / 'etc/geomask').glob('.backup-park-*'))
        self.assertEqual(len(parks), 1, 'the park directory was recreated or deleted')
        kept = list(parks[0].glob('*.tar.gz'))
        self.assertEqual(len(kept), 1, 'the only backup copy was destroyed')
        self.assertEqual(kept[0].read_bytes(), before)
        # the second attempt must say where the copy is, not just "failed"
        self.assertIn(posix(parks[0]), result.stdout + result.stderr)

    def test_a_kill_after_parking_does_not_lose_the_only_backup(self):
        """P09: the park lived in /tmp (tmpfs) and had no marker until a restore failed.

        A power loss or SIGKILL between parking the only copy and publishing the new archive
        therefore lost it: /tmp is cleared on reboot and nothing recorded where the copy went.
        """
        backups = self.case / 'root/geomask-backups'
        backups.mkdir(parents=True)
        old = backups / 'router-backup-20260101-000000.tar.gz'
        subprocess.run(['tar', 'czf', str(old), '-C', str(self.case), 'etc'], check=True)
        before = old.read_bytes()
        funcs = ['_backup_park_restore', 'archive_ok', 'ensure_backup',
                 '_backup_recovery_write', '_backup_recovery_park',
                 '_backup_recovery_clear', '_backup_keep_failed',
                 '_bnote', '_bwarn', '_bbad']
        crash = r'''
have() { case "$1" in sysupgrade|tar|gzip) return 0 ;; esac; command -v "$1" >/dev/null 2>&1; }
sysupgrade() { kill -9 $$; }
'''
        self.run_script('ensure_backup full', functions=funcs, extra=crash)
        parks = list((self.case / 'etc/geomask').glob('.backup-park-*'))
        self.assertEqual(len(parks), 1, 'the parked copy is not in persistent storage')
        self.assertTrue(list(parks[0].glob('*.tar.gz')), 'the only backup was not parked')
        marker = self.case / 'etc/geomask/backup-recovery'
        self.assertTrue(marker.exists(), 'nothing says where the only copy is after a kill')
        self.assertIn(posix(parks[0]), marker.read_text(encoding='utf-8'))
        # the "next boot": the parked copy comes back before a new archive is created
        second = r'''
have() { case "$1" in sysupgrade|tar|gzip) return 0 ;; esac; command -v "$1" >/dev/null 2>&1; }
sysupgrade() { tar czf "$2" -C @CASE@ etc; }
BACKUP_KEEP=2
'''.replace('@CASE@', posix(self.case))
        result = self.run_script('ensure_backup full; printf "RC=%s\\n" "$?"',
                                 functions=funcs, extra=second)
        self.assertIn('RC=0', result.stdout)
        self.assertTrue(old.exists(), 'the parked copy did not come back')
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse(marker.exists(), 'the recovery marker was left behind')
        self.assertEqual(list((self.case / 'etc/geomask').glob('.backup-park-*')), [])

    def test_a_marker_whose_park_is_gone_refuses_instead_of_ignoring_it(self):
        """A park in /tmp did not survive a reboot of an older build: say the copy is lost.

        Creating a fresh archive anyway would silently pretend the previous one never existed
        and leave the marker behind forever.
        """
        backups = self.case / 'root/geomask-backups'
        backups.mkdir(parents=True)
        gone = self.case / 'tmp/geomask-bk-park-gone01'
        etc = self.case / 'etc/geomask'
        etc.mkdir(parents=True)
        (etc / 'backup-recovery').write_text('park=%s\nreason=parking\n' % posix(gone),
                                             encoding='utf-8')
        extra = r'''
have() { case "$1" in sysupgrade|tar|gzip) return 0 ;; esac; command -v "$1" >/dev/null 2>&1; }
sysupgrade() { tar czf "$2" -C @CASE@ etc; }
'''.replace('@CASE@', posix(self.case))
        result = self.run_script(
            'ensure_backup full; printf "RC=%s\\n" "$?"',
            functions=['_backup_park_restore', 'archive_ok', 'ensure_backup',
                       '_backup_recovery_write', '_backup_recovery_park',
                       '_backup_recovery_clear', '_backup_keep_failed',
                       '_bnote', '_bwarn', '_bbad'],
            extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn(posix(gone), result.stdout + result.stderr)
        self.assertIn('потеряна', result.stdout + result.stderr)
        self.assertEqual(list(backups.glob('*.tar.gz')), [],
                         'a new archive was created over a lost previous one')
        self.assertTrue((etc / 'backup-recovery').exists(),
                        'the marker was cleared without the copy')

    def test_the_ai_dns_mark_check_accepts_zero_padded_hex(self):
        """nft prints "mark set 0x00000064"; the installer greps for "mark set 0x64".

        On the live router the rule existed and step 4 still failed, aborting the install
        before steps 5-7. The check must compare the value, not the spelling.
        """
        body = r'''
padded='meta skuid 65533 meta mark set 0x00000064'
plain='meta skuid 65533 meta mark set 0x64'
decimal='meta skuid 65533 meta mark set 100'
other='meta skuid 65533 meta mark set 0x65'
none='meta skuid 65533 counter accept'
for _p in "padded:$padded" "plain:$plain" "decimal:$decimal" "other:$other" "none:$none"; do
  _name=${_p%%:*}
  _val=${_p#*:}
  _aidns_mark_present "$_val" 0x64 && printf '%s=1\n' "$_name" || printf '%s=0\n' "$_name"
done
'''
        result = self.run_script(body, functions=['_aidns_mark_present'])
        self.assertIn('padded=1', result.stdout)
        self.assertIn('plain=1', result.stdout)
        self.assertIn('decimal=1', result.stdout)
        self.assertIn('other=0', result.stdout)
        self.assertIn('none=0', result.stdout)

    def test_snapshot_metadata_works_without_stat(self):
        """This router's busybox has no stat(1); the snapshot then recorded empty metadata."""
        target = self.case / 'payload.bin'
        target.write_bytes(b'abcde')
        os.chmod(target, 0o640)
        t = posix(target)
        body = ('stat() { return 127; }\n'
                'printf "MODE=%s\\n" "$(_rs_mode ' + t + ')"\n'
                'printf "SIZE=%s\\n" "$(_rs_size ' + t + ')"\n'
                'printf "OWNER=%s\\n" "$(_rs_uid ' + t + ')"\n'
                'printf "DEVINO=%s\\n" "$(_rs_devino ' + t + ')"\n'
                'printf "FOUR=%s\\n" "$(_rs_stat4 ' + t + ')"\n')
        result = self.run_script(
            body,
            functions=['_rs_mode', '_rs_uid', '_rs_devino', '_rs_size', '_rs_stat4',
                       '_rs_ls_mode', '_rs_ls_uid', '_rs_ls_size', '_rs_ls_devino'])
        # Windows ignores group bits, so compare the shape, not a fixed 640.
        self.assertRegex(result.stdout, r'MODE=[0-7]{3}', result.stdout + result.stderr)
        self.assertIn('SIZE=5', result.stdout, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'OWNER=\d+')
        self.assertRegex(result.stdout, r'DEVINO=0:\d+')
        self.assertRegex(result.stdout, r'FOUR=[0-7]{3}\|\d+\|0:\d+\|5')

    def test_snapshot_metadata_of_a_directory_without_stat(self):
        """/etc/mihomo is a directory; `wc -c < dir` fails and aborted the reinstall."""
        target = self.case / 'state-dir'
        target.mkdir()
        t = posix(target)
        body = ('stat() { return 127; }\n'
                'printf "SIZE=%s\\n" "$(_rs_size ' + t + ')"\n'
                'printf "FOUR=%s\\n" "$(_rs_stat4 ' + t + ')"\n')
        result = self.run_script(
            body,
            functions=['_rs_size', '_rs_stat4', '_rs_mode', '_rs_uid', '_rs_devino',
                       '_rs_ls_mode', '_rs_ls_uid', '_rs_ls_size', '_rs_ls_devino'])
        self.assertRegex(result.stdout, r'SIZE=\d+', result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'FOUR=[0-7]{3}\|\d+\|0:\d+\|\d+')

    def test_an_empty_uci_delta_does_not_block_the_reinstall(self):
        """uci leaves an empty /tmp/.uci/<pkg> behind; only non-empty is a mid-edit."""
        for name in ('110-restore-snapshot-shared.sh.part', '130-restore-shared-uci.sh.part'):
            part = (TEMPLATE.parent / 'src' / 'installer' / name).read_text(encoding='utf-8')
            self.assertIn('if [ -s "$(P "/tmp/.uci/$_rs_up")" ]', part,
                          '%s still treats an empty delta as pending' % name)
            self.assertNotIn('if [ -f "$(P "/tmp/.uci/$_rs_up")" ]', part)

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
            functions=['_mihomo_py', 'geomask_config_verdict', 'geomask_config_shaped'])
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

    def test_selecting_ai_dns_pulls_the_tunnel_it_needs(self):
        """I07: ai_dns was installed without path_ai, which the manifest requires."""
        result = self.run_script('printf "MODULES=%s\\n" "$(normalize_modules ai_dns)"',
                                 functions=['module_deps', 'normalize_modules'])
        self.assertIn('ai_dns', result.stdout)
        self.assertIn('path_ai', result.stdout)
        self.assertIn('admission_service', result.stdout)
        self.assertIn('core', result.stdout)

    def test_the_installer_dependency_edges_match_the_manifests(self):
        """The shell edges are a copy of modules/*.json; a drift would break a clean install."""
        import json as _json
        result = self.run_script('for m in ai_dns path_ai; do printf "%s:" "$m"; '
                                 'module_deps "$m"; printf "\\n"; done',
                                 functions=['module_deps'])
        shell = {}
        for line in result.stdout.splitlines():
            if ':' in line:
                name, _, deps = line.partition(':')
                shell[name] = sorted(deps.split())
        self.assertTrue(shell)
        for module_id, deps in shell.items():
            manifest = _json.loads((ROOT / 'modules' / ('%s.json' % module_id))
                                   .read_text(encoding='utf-8'))
            # subscription:sub is a capability, not a module the installer can select.
            declared = sorted(dep['module_id'] for dep in manifest.get('dependencies') or []
                              if dep.get('required')
                              and not str(dep.get('module_id', '')).startswith('subscription:'))
            self.assertEqual(deps, declared, 'shell deps drifted for %s' % module_id)

    # ---------------------------------------------------------------- T05
    #
    # R03 phase 2b: the snapshot is manifest-driven, every restored object is verified and an
    # unverified rollback keeps the snapshot. These tests exercise the real functions against
    # injected adapters; the full fault matrix lives in tests/test_reinstall_phase2b.py.
    def reinstall_extra(self, runtime, tracked=''):
        cron = posix(self.case / 'cron.txt')
        return (reinstall_tables()
                + install_safety_stubs().replace('@RUNTIME@', posix(runtime))
                + "crontab() { if [ \"$1\" = \"-l\" ]; then cat \"%s\" 2>/dev/null; "
                  "else cp \"$1\" \"%s\"; fi; }\n" % (cron, cron)
                + ("initd() { printf '%%s\\n' \"$*\" >> \"%s\"; "
                   "case \"$2\" in start|restart|restart_verified) : > \"$RUNTIME/$1\" ;; "
                   "stop) rm -f \"$RUNTIME/$1\" ;; esac; }\n"
                   % posix(self.case / 'initd.log'))
                + tracked)

    def reinstall_runtime(self, running):
        runtime = self.case / 'runtime'
        runtime.mkdir(exist_ok=True)
        for name in running:
            (runtime / name).write_text('1\n', encoding='utf-8')
        return runtime

    @unittest.skipUnless(win_symlink.NATIVE_SYMLINKS, win_symlink.SYMLINK_REASON)
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
        rc.parent.mkdir(parents=True, exist_ok=True)
        os.symlink('../init.d/geomask-syncd', rc)
        cron = self.case / 'cron.txt'
        cron.write_text('*/5 * * * * /usr/bin/geomask-guard.sh >/dev/null 2>&1 # geomask-guard\n', encoding='utf-8')
        runtime = self.reinstall_runtime(('dnsmasq', 'mihomo', 'magitrickled', 'rpcd',
                                          'netifd', 'fw4', 'geomask-syncd'))
        extra = self.reinstall_extra(
            runtime,
            "REINSTALL_TRACKED='/etc/init.d/geomask-syncd /usr/bin/geo-backend.sh "
            "/usr/bin/geomask-tool.sh /root/sync_mtrickle_mihomo.sh'\n")
        body = ('snap="%s/snap"; rm -rf "$snap"\n'
                '_reinstall_snapshot "$snap" || { printf "SNAP_FAIL\\n"; exit 1; }\n'
                'rm -rf "%s/usr" "%s/etc/init.d"\n'
'rm -f "%s/etc/rc.d"/*\n'
                'rm -f "%s/root/sync_mtrickle_mihomo.sh"\n'
                "crontab -l 2>/dev/null | grep -v '# geomask-guard' | crontab - 2>/dev/null\n"
                # The uninstall stopped the service: its pid marker is gone with it, while the
                # rollback has to bring both the script and the running state back.
                'rm -f "%s/geomask-syncd"\n'
                'mkdir -p "%s/usr/bin"; printf "new\\n" > "%s/usr/bin/geomask-leftover"\n'
                'printf "NEW_BACKEND\\n" > "%s/usr/bin/geo-backend.sh"\n'
                '_reinstall_restore "$snap"; printf "RC=%%s\\n" "$?"\n'
                % (posix(self.case), posix(self.case), posix(self.case),
                   posix(self.case), posix(self.case), posix(runtime),
                   posix(self.case), posix(self.case), posix(self.case)))
        result = self.run_script(body, functions=reinstall_function_names(), extra=extra)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertTrue(tool.exists(), 'the tool script was not restored')
        self.assertEqual(tool.read_text(encoding='utf-8'), '#!/bin/sh\necho tool\n')
        self.assertEqual(backend.read_text(encoding='utf-8'), 'OLD_BACKEND\n',
                         'the previous backend was not restored')
        self.assertEqual(sync.read_text(encoding='utf-8'), 'OLD_SYNC\n',
                         'the sync script was not restored')
        self.assertTrue(initd.exists())
        self.assertTrue(rc.exists())
        self.assertIn('geomask-guard', cron.read_text(encoding='utf-8'))
        self.assertIn('geomask-syncd start',
                      (self.case / 'initd.log').read_text(encoding='utf-8'))
        # phase 2b: an unrecorded file (even with our prefix) is NOT deleted - a third party
        # may have created it. Only the transaction's own recorded objects are rolled back.
        self.assertTrue((self.case / 'usr/bin/geomask-leftover').exists(),
                        'the rollback deleted a file it never recorded')
        self.assertFalse((self.case / 'snap').exists(), 'the snapshot was not cleaned up')

    def reinstall_tracked(self):
        """The shipped snapshot list, straight from the template.

        Taking the test's own list would prove nothing: what the rollback covers is exactly
        the question, so the assertion has to run against the real variable.
        """
        match = re.search(r'(?m)^REINSTALL_TRACKED="(?:[^"]*)"$', SOURCE)
        self.assertIsNotNone(match, 'REINSTALL_TRACKED not found in the template')
        return match.group(0) + '\n'

    def reinstall_case(self):
        """Byte objects the reinstall flow rewrites: they must survive a rollback.

        /etc/config/network, dhcp and ksmbd are UCI objects in the manifest (restored by key,
        not by bytes), so they are covered by tests/test_reinstall_phase2b.py instead.
        """
        files = {
            'etc/mihomo/config.yaml': 'mixed-port: 7890\n# old config\n',
            'etc/mihomo/ruleset/ai.yaml': 'payload:\n  - OLD-RULESET\n',
            'etc/geomask/modules.json': '{"selected": ["core"]}\n',
            'etc/geomask/modules-state.json': '{"modules": {}}\n',
            'etc/geomask/version': 'v1.30.0\n',
        }
        for relative, body in files.items():
            path = self.case / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding='utf-8', newline='\n')
        return files

    def test_the_snapshot_covers_the_config_the_flow_rewrites(self):
        """#23: restoring the panel while leaving the new config is not a rollback.

        `_reinstall_steps` rewrites the mihomo config and the generated assets, so a snapshot
        of the panel files only could return "success" with the device on the new settings.
        """
        files = self.reinstall_case()
        runtime = self.reinstall_runtime(('dnsmasq', 'mihomo', 'magitrickled', 'rpcd',
                                          'netifd', 'fw4'))
        extra = self.reinstall_extra(runtime)
        rewrites = '\n'.join('printf "NEW\\n" > "%s"' % posix(self.case / relative)
                             for relative in files)
        rewrites += '\nrm -f "%s"' % posix(self.case / 'etc/mihomo/ruleset/ai.yaml')
        body = ('snap="%s/snap"; rm -rf "$snap"\n'
                '_reinstall_snapshot "$snap" || { printf "SNAP_FAIL\\n"; exit 1; }\n'
                '%s\n'
                '_reinstall_restore "$snap"; printf "RC=%%s\\n" "$?"\n'
                % (posix(self.case), rewrites))
        result = self.run_script(body, functions=reinstall_function_names(), extra=extra)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        for relative, expected in files.items():
            actual = (self.case / relative).read_text(encoding='utf-8')
            self.assertEqual(actual, expected,
                             '%s was not rolled back (the failed reinstall left its own bytes)'
                             % relative)

    def test_an_incomplete_snapshot_fails_the_restore(self):
        """A component recorded as present but missing from the copy tree must fail the rollback.

        The old restore copied whatever it found with `find` and returned 0, so a truncated
        snapshot looked like a successful rollback while the files stayed new.
        """
        self.reinstall_case()
        runtime = self.reinstall_runtime(('dnsmasq', 'mihomo', 'magitrickled', 'rpcd',
                                          'netifd', 'fw4'))
        extra = self.reinstall_extra(runtime)
        body = ('snap="%s/snap"; rm -rf "$snap"\n'
                '_reinstall_snapshot "$snap" || { printf "SNAP_FAIL\\n"; exit 1; }\n'
                'rm -rf "$snap/files/etc/geomask"\n'
                'printf "NEW\\n" > "%s"\n'
                '_reinstall_restore "$snap"; printf "RC=%%s\\n" "$?"\n'
                % (posix(self.case), posix(self.case / 'etc/geomask/modules.json')))
        result = self.run_script(body, functions=reinstall_function_names(), extra=extra)
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)
        self.assertEqual((self.case / 'etc/geomask/modules.json').read_text(encoding='utf-8'),
                         'NEW\n')
        self.assertIn('etc/geomask/modules.json', result.stdout + result.stderr)
        self.assertTrue((self.case / 'snap').exists(),
                        'the only copy of the old state was deleted by a failed rollback')

    # ---------------------------------------------------------------- step 6 gate
    def test_step_six_takes_the_backup_gate(self):
        """#24: step 6 starts services, so it writes to the router like steps 1-5 and 7.

        Without the gate the last writing step of a run could start mihomo, the tunnels and the
        web server with no verified backup behind it - the one rule the project never relaxes.
        """
        extra = r'''
STEP_NAMES="env geo subs aidns sync services verify"
hdr() { :; }
backup_gate() { printf 'GATE\n'; return 0; }
modules_maybe_prompt() { :; }
step_fn() { case "$2" in check) return 1 ;; *) return 0 ;; esac; }
'''
        result = self.run_script('run_step 6; printf "RC=%s\\n" "$?"',
                                 functions=['run_step'], extra=extra)
        self.assertIn('GATE', result.stdout,
                      'step 6 started services without asking the backup gate')
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)

    def test_a_refused_gate_stops_step_six(self):
        """The gate must be able to refuse, not just be called."""
        extra = r'''
STEP_NAMES="env geo subs aidns sync services verify"
hdr() { :; }
backup_gate() { printf 'GATE_REFUSED\n'; return 1; }
modules_maybe_prompt() { :; }
step_fn() { case "$2" in check) return 1 ;; apply) printf 'APPLIED\n' ;; esac; return 0; }
'''
        result = self.run_script('run_step 6; printf "RC=%s\\n" "$?"',
                                 functions=['run_step'], extra=extra)
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)
        self.assertNotIn('APPLIED', result.stdout,
                         'the services were started although the backup gate refused')

    def test_a_single_tunnel_install_passes_the_group_check(self):
        """I01 of the 18.11 audit: a fresh VPN-noP2P-only topology is not a broken install.

        The template creates VPN-noP2P and AI; `VPN` is the optional second tunnel the panel
        adds later, and verify used to fail on its absence alone.
        """
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('proxy-groups:\n  - name: VPN-noP2P\n  - name: AI\n', encoding='utf-8')
        extra = ('controller_curl() { printf \'{"VPN-noP2P":{},"AI":{}}\'; }\n')
        result = self.run_script('_step7_groups; printf "RC=%s\\n" "$?"',
                                 functions=['_step7_groups'], extra=extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=0', result.stdout, result.stdout)
        self.assertNotIn('group VPN missing', result.stdout)

    def test_a_missing_required_group_fails_the_group_check(self):
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('proxy-groups:\n  - name: VPN-noP2P\n', encoding='utf-8')
        extra = ('controller_curl() { printf \'{"VPN-noP2P":{}}\'; }\n')
        result = self.run_script('_step7_groups; printf "RC=%s\\n" "$?"',
                                 functions=['_step7_groups'], extra=extra)
        self.assertIn('RC=1', result.stdout, result.stdout)
        self.assertIn('group AI missing', result.stdout)

    def test_a_declared_vpn_group_missing_from_the_api_fails(self):
        conf = self.case / 'etc/mihomo/config.yaml'
        conf.parent.mkdir(parents=True)
        conf.write_text('proxy-groups:\n  - name: VPN-noP2P\n  - name: AI\n  - name: VPN\n',
                        encoding='utf-8')
        extra = ('controller_curl() { printf \'{"VPN-noP2P":{},"AI":{}}\'; }\n')
        result = self.run_script('_step7_groups; printf "RC=%s\\n" "$?"',
                                 functions=['_step7_groups'], extra=extra)
        self.assertIn('RC=1', result.stdout, result.stdout)
        self.assertIn('declared in the config but missing from the API', result.stdout)

    def test_prompt_stdout_is_only_the_url(self):
        """I06 of the 18.11 audit: the question leaked into the captured value."""
        functions = ['_prompt_tty', 'prompt_url']
        body = ('GM_YES=0\n'
                'url=$(printf "https://example.invalid/sub\\n" | GM_PROMPT_TTY=1 '
                'prompt_url "VPN URL" "" ""); printf "URL=[%s]\\n" "$url"\n'
                'cur=$(printf "\\n" | GM_PROMPT_TTY=1 '
                'prompt_url "VPN URL" "https://kept.invalid/s" ""); printf "CUR=[%s]\\n" "$cur"\n'
                'fix=$(printf "ftp://bad\\nhttps://good.invalid/s\\n" | GM_PROMPT_TTY=1 '
                'prompt_url "VPN URL" "" ""); printf "FIX=[%s]\\n" "$fix"')
        result = self.run_script(body, functions=functions)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('URL=[https://example.invalid/sub]', result.stdout, result.stdout)
        self.assertIn('CUR=[https://kept.invalid/s]', result.stdout, result.stdout)
        self.assertIn('FIX=[https://good.invalid/s]', result.stdout, result.stdout)
        self.assertNotIn('[VPN URL', result.stdout, 'the prompt went into the captured value')
        self.assertIn('VPN URL:', result.stderr, 'the prompt is not shown to the operator')
        self.assertIn('http:// или https://', result.stderr)

    def test_step5_apply_reports_write_and_seed_failures(self):
        """I12: the sync step returned 0 no matter what failed inside it."""
        body = ('SAVE_FAIL=0; _step5_apply_result 0 0 0 0; printf "OK=%s\\n" "$?"\n'
                'SAVE_FAIL=2; _step5_apply_result 1 0 0 0; printf "SAVE=%s\\n" "$?"\n'
                'SAVE_FAIL=0; _step5_apply_result 0 3 0 0; printf "SEED=%s\\n" "$?"\n'
                'SAVE_FAIL=0; _step5_apply_result 0 0 4 0; printf "LISTS=%s\\n" "$?"\n'
                'SAVE_FAIL=0; _step5_apply_result 0 0 0 5; printf "STATE=%s\\n" "$?"')
        result = self.run_script(body, functions=['_step5_apply_result'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for expected in ('OK=0', 'SAVE=1', 'SEED=1', 'LISTS=1', 'STATE=1'):
            self.assertIn(expected, result.stdout, result.stdout)
        self.assertIn('не все файлы записаны', result.stdout)
        self.assertIn('not seeded', result.stdout)

    def test_save_stream_reports_a_truncated_write(self):
        """The published bytes are checked, not the exit code of `mv`."""
        extra = ('mv() { command mv "$@" || return 1; : > "$3"; }\n'
                 'module_skip_path() { return 1; }\n'
                 'module_owns_path() { printf ""; }\n')
        body = ('save_stream /etc/geomask/x.conf 644 <<\'EOF\' >/dev/null 2>&1\n'
                'payload-line\n'
                'EOF\n'
                'printf "RC=%s SAVE=%s SIZE=%s\\n" "$?" "$SAVE_FAIL" '
                '"$(wc -c < "$(P /etc/geomask/x.conf)" | tr -d " ")"')
        result = self.run_script(body, extra=extra, functions=['save_stream'])
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)
        self.assertIn('SAVE=1', result.stdout)

    def test_a_damaged_selection_is_not_treated_as_all(self):
        """I10: 'no selection' means install everything, so a broken file must not look like it."""
        sel = self.case / 'etc/geomask/modules.json'
        sel.parent.mkdir(parents=True)
        sel.write_text('{not json', encoding='utf-8')
        result = self.run_script('modules_state_read; printf "RC=%s\\n" "$?"',
                                 functions=['modules_state_read'])
        self.assertIn('RC=2', result.stdout, result.stdout + result.stderr)
        self.assertEqual(sel.read_text(encoding='utf-8'), '{not json')

    def test_the_selection_sync_refuses_a_damaged_file(self):
        sel = self.case / 'etc/geomask/modules.json'
        sel.parent.mkdir(parents=True)
        sel.write_text('{not json', encoding='utf-8')
        body = ('GM_MODULES="core"; modules_state_sync; printf "RC=%s\\n" "$?"; '
                'cat "$(P /etc/geomask/modules.json)"')
        result = self.run_script(body, functions=['modules_state_read', 'modules_state_write',
                                                 'module_effective_list', 'modules_state_sync'])
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)
        self.assertIn('{not json', result.stdout, 'the damaged file was overwritten')
        self.assertIn('повреждён', result.stdout)

    def test_the_selection_is_published_atomically(self):
        body = ('modules_state_write core luci; printf "RC=%s\\n" "$?"; '
                'cat "$(P /etc/geomask/modules.json)"; '
                'ls "$(P /etc/geomask)" | tr "\\n" " "; printf "\\n"')
        result = self.run_script(body, functions=['modules_state_write'])
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('"core"', result.stdout)
        self.assertIn('"luci"', result.stdout)
        self.assertNotIn('.tmp', result.stdout.split('RC=0')[1],
                         'a temp file survived the publish')

    def test_core_owns_the_backend_so_a_headless_install_can_disable_ipv6(self):
        """I02: with core/path_vpn the backend was not installed, so IPv6-off never ran."""
        import json as _json
        index = _json.loads((ROOT / 'modules' / 'index.json').read_text(encoding='utf-8'))
        owner = {entry['path']: module_id
                 for module_id, module in index['modules'].items()
                 for entry in module.get('files') or []}
        self.assertEqual(owner.get('/usr/bin/geo-backend.sh'), 'core')

    def test_cron_ensure_without_a_lock_does_not_write(self):
        """I03 of the 18.11 audit: a refused lock must not still rewrite the crontab."""
        extra = r"""
module_selected() { return 0; }
CRONFILE="@CASE@/cron.txt"
printf 'MAILTO=""\n' > "$CRONFILE"
crontab() {
  case "${1:-}" in
    -l) cat "$CRONFILE" ;;
    -) cat > "$CRONFILE" ;;
    *) : ;;
  esac
}
gm_lock_acquire() { return 1; }
gm_lock_release() { return 0; }
_CRON_LOCK_DEPTH=0
""".replace('@CASE@', posix(self.case))
        body = ('cron_ensure youtube "# geomask-ytwatch$" '
                '"*/1 * * * * /usr/bin/geomask-ytwatch.sh >/dev/null 2>&1 # geomask-ytwatch"; '
                'printf "RC=%s\\n" "$?"; cat "$CRONFILE"')
        result = self.run_script(body, extra=extra,
                                 functions=['mod_skipped', 'cron_ensure', '_cron_begin',
                                            '_cron_end'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('geomask-ytwatch',
                         (self.case / 'cron.txt').read_text(encoding='utf-8'),
                         'the crontab was rewritten although the lock was refused')
        self.assertIn('RC=1', result.stdout, result.stdout)

    def test_the_uninstall_does_not_edit_cron_without_a_lock(self):
        extra = r"""
module_selected() { return 0; }
CRONFILE="@CASE@/cron.txt"
printf '0 0 * * * /root/keep-me\n*/5 * * * * /root/sync_mtrickle_mihomo.sh\n' > "$CRONFILE"
crontab() {
  case "${1:-}" in
    -l) cat "$CRONFILE" ;;
    -) cat > "$CRONFILE" ;;
    *) : ;;
  esac
}
gm_lock_acquire() { return 1; }
gm_lock_release() { return 0; }
initd() { return 0; }
pkill() { return 0; }
rmdir() { return 0; }
""".replace('@CASE@', posix(self.case))
        body = 'geomask_uninstall noreload; printf "RC=%s\\n" "$?"; cat "$CRONFILE"'
        result = self.run_script(body, extra=extra,
                                 functions=['_cron_begin', '_cron_end', 'geomask_uninstall'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('/root/keep-me', result.stdout)
        self.assertIn('sync_mtrickle_mihomo', result.stdout,
                      'the cron line was removed although the lock was refused')

    def test_the_cron_restore_takes_the_lock_before_it_reads(self):
        """I04/I03: the foreign-line compare must happen inside the lock, and no lock = no write."""
        snap = self.case / 'snap'
        snap.mkdir()
        (snap / 'cron').write_text('0 0 * * * /root/keep-me\n', encoding='utf-8')
        extra = r"""
CRONFILE="@CASE@/cron.txt"
printf '0 5 * * * /root/foreign-after-snapshot\n' > "$CRONFILE"
crontab() {
  case "${1:-}" in
    -l) cat "$CRONFILE" ;;
    -) cat > "$CRONFILE" ;;
    *) cp "$1" "$CRONFILE" ;;
  esac
}
gm_lock_acquire() { printf 'LOCK\n'; return 0; }
gm_lock_release() { printf 'UNLOCK\n'; return 0; }
_CRON_LOCK_DEPTH=0
""".replace('@CASE@', posix(self.case))
        body = ('_rr=0; _rs_restore_cron "%s"; printf "RC=%%s\\n" "$?"; cat "$CRONFILE"'
                % posix(snap))
        result = self.run_script(body, extra=extra,
                                 functions=['_cron_begin', '_cron_end', '_rs_restore_cron'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = result.stdout
        self.assertIn('LOCK', log)
        self.assertIn('RC=1', log, log)
        self.assertIn('foreign-after-snapshot', log,
                      'the restore overwrote a foreign crontab change')
        self.assertLess(log.index('LOCK'), log.index('conflict'),
                        'the crontab was read before the lock was taken')
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
                                 functions=['_mihomo_py', 'geomask_config_verdict', '_mihomo_guard',
                                            'geomask_config_shaped', 'menu_reinstall'],
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
                                 functions=['geomask_config_verdict', '_mihomo_guard',
                                            'geomask_config_shaped', 'step3_apply'],
                                 extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('шаг 3 не завершён', result.stdout)

    # ---------------------------------------------------------------- R10b device list
    def test_a_fresh_install_creates_an_empty_device_list(self):
        # The guard now treats a missing list as unknown (rc 2). The installer must create
        # an EMPTY 600 list, so "missing" means damage rather than a normal fresh system.
        result = self.run_script('ensure_devices_list; printf "RC=%s\\n" "$?"',
                                 functions=['ensure_devices_list'])
        self.assertIn('RC=0', result.stdout, result.stderr)
        path = self.case / 'etc/geo-devices.list'
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b'')
        self.assertIn('chmod 600 "$_edl"', SOURCE)
        if os.name != 'nt':
            import stat
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_an_existing_device_list_is_never_truncated(self):
        path = self.case / 'etc/geo-devices.list'
        path.parent.mkdir(parents=True, exist_ok=True)
        before = '10.0.0.5 ai phone\n192.168.1.50 bypass tv\n'
        path.write_text(before, encoding='utf-8', newline='\n')
        result = self.run_script('ensure_devices_list; printf "RC=%s\\n" "$?"',
                                 functions=['ensure_devices_list'])
        self.assertIn('RC=0', result.stdout, result.stderr)
        self.assertEqual(path.read_text(encoding='utf-8'), before)


if __name__ == '__main__':
    unittest.main()

