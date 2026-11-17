"""R09: one diagnostics archive with every saved per-check log, built without a probe.

The owner asked for "all check logs in one button". The console card already assembled a safe
bundle (R02 masking, R07 private spool and NOFOLLOW download, R08 retention/quota), so the
answer must reuse that exporter rather than grow a second one: `cmd_diag_bundle` takes the same
collector/packer, adds `diagnostics/index.json` plus the logs `diag_list` already points at, and
the panel downloads the result through `console_diag_get`.

These tests run the shipped shell functions against a fake Doctor module and stubbed router
commands. No router, no network, and the fake `run` subcommand writes a marker: if the bundle
ever calls a check, the marker proves it.
"""
from pathlib import Path
import base64
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
SOURCE = BACKEND.read_text(encoding='utf-8')
REDACT = ROOT / 'assets' / 'geomask-diag-redact.py'
SPOOL = ROOT / 'assets' / 'geomask-diag-spool.py'


def find_bash():
    # Never select Windows' optional WSL launcher from PATH.
    candidates = ((r'C:\Program Files\Git\bin\bash.exe',) if sys.platform == 'win32'
                  else (shutil.which('bash'),))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def extract(name):
    """Whole function, brace-balanced and heredoc-aware (the index builder has a heredoc)."""
    lines = SOURCE.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(name + '() {')), None)
    if start is None:
        raise AssertionError('function %s not found in the backend' % name)
    first = lines[start]
    if first.rstrip().endswith('}') and first.count('{') == first.count('}'):
        return first
    heredoc = None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        opener = re.search(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?", line)
        if opener is not None:
            heredoc = opener.group(1)
            continue
        if line == '}':
            return '\n'.join(lines[start:index + 1])
    raise AssertionError('function %s has no closing brace' % name)


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


def native(path):
    """A path native Python can open when it arrives through JSON, not through argv."""
    return str(Path(path).resolve()).replace('\\', '/')


STUBS = r'''
uci() { return 0; }
nft() { return 0; }
logread() { printf 'mihomo line\n'; }
conntrack() { return 0; }
controller_curl() { return 0; }
ip() { printf '10: from 192.168.1.177 lookup 102\n'; return 0; }
'''


@unittest.skipIf(BASH is None, 'bash is required')
class DiagBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.spool = self.case / 'spool'
        self.spool.mkdir(mode=0o700)
        self.share = self.case / 'share'
        self.share.mkdir()
        self.state = self.case / 'console.state'
        self.state.write_text('enabled=1\nip=192.168.1.177\n', encoding='utf-8',
                              newline='\n')
        (self.case / 'config.yaml').write_text('mixed-port: 7890\n', encoding='utf-8',
                                               newline='\n')
        self.logs = self.case / 'logs'
        self.logs.mkdir()
        self.probe = self.case / 'probe-ran'
        self.doctor = self.case / 'fake-doctor.py'
        self.checks = [
            {
                'id': 'router_system',
                'area': 'router',
                'title': 'Система и аптайм',
                'live': False,
                'last_state': 'ok',
                'last_summary': 'всё в порядке',
                'last_at': '2026-11-17T10:00:00+0300',
                'log': native(self.logs / 'router_system.log'),
            },
            {
                'id': 'net_dns',
                'area': 'network',
                'title': 'DNS и блокировка рекламы',
                'live': False,
                'last_state': 'fail',
                'last_summary': 'резолвер молчит',
                'last_at': '2026-11-17T10:00:00+0300',
                'log': native(self.logs / 'net_dns.log'),
            },
            {
                'id': 'net_mtu',
                'area': 'network',
                'title': 'MTU до провайдера',
                'live': True,
                'last_state': '',
                'last_summary': '',
                'last_at': '',
                'log': native(self.logs / 'net_mtu.log'),
            },
            {
                'id': 'path_ai',
                'area': 'paths',
                'title': 'Путь AI',
                'live': False,
                'last_state': '',
                'last_summary': '',
                'last_at': '',
                'log': native(self.logs / 'path_ai.log'),
            },
        ]
        (self.logs / 'router_system.log').write_text(
            '=== 2026-11-16T09:00:00+0300 ok ===\nold block\n'
            '=== 2026-11-17T10:00:00+0300 ok ===\nsystem is visible\n',
            encoding='utf-8', newline='\n')
        (self.logs / 'net_dns.log').write_text(
            '=== 2026-11-17T10:00:00+0300 fail ===\n'
            'resolver refused; Authorization: Bearer BUNDLE-SECRET-TOKEN\n'
            'mihomo: password=BUNDLE-PASS-123\n'
            'mihomo: sub url=https://alice:BUNDLE-URL-456@sub.example.com/link?token=BUNDLE-QS-789\n',
            encoding='utf-8', newline='\n')
        # `path_ai` has a log file but never wrote a block; `net_mtu` has no file at all.
        (self.logs / 'path_ai.log').write_text('', encoding='utf-8')
        self.write_doctor()

    def write_doctor(self):
        data = json.dumps(self.checks, ensure_ascii=False)
        self.doctor.write_text(
            'import json, sys\n'
            'CHECKS = %r\n'
            'PROBE = %r\n'
            'if sys.argv[1:2] == ["list"]:\n'
            '    body = {"ok": True, "version": "test", "checks": json.loads(CHECKS)}\n'
            '    sys.stdout.buffer.write(json.dumps(body, ensure_ascii=False).encode("utf-8") + b"\\n")\n'
            '    sys.exit(0)\n'
            'if sys.argv[1:2] == ["run"]:\n'
            '    open(PROBE, "w").write("probe")\n'
            '    sys.exit(1)\n'
            'sys.exit(2)\n' % (data, native(self.probe)),
            encoding='utf-8', newline='\n')

    def run_body(self, body, functions=('cmd_diag_bundle',), env='', spool_py=None):
        lines = [
            'set -u',
            'CONSOLE_STATE="%s"' % posix(self.state),
            'export TMPDIR="%s"' % posix(self.spool),
            'export TMP="%s" TEMP="%s"' % (posix(self.case), posix(self.case)),
            'DIAG_SPOOL="%s"' % posix(self.spool),
            'DIAG_SPOOL_PY="%s"' % posix(spool_py or SPOOL),
            'BACKUP_DIR="%s"' % posix(self.share),
            'SMB_SHARE_ENABLE="%s"' % posix(self.case / 'backup-share.enable'),
            'CONSOLE_DIAG_KEEP_H=24',
            'CONSOLE_TABLE=102',
            'CONSOLE_TUN_DEV=Console',
            'BACKEND_VERSION=test',
            'TEST_VERSION_PATH="%s"' % posix(self.case / 'absent-version'),
            'MIHOMO_CONF="%s"' % posix(self.case / 'config.yaml'),
            'MIHOMO_API=http://127.0.0.1:9090',
            'CONSOLE_PY=/nonexistent/geomask-console.py',
            'PYTHON_BIN="%s"' % posix(sys.executable),
            'GM_DIAG_PY="%s"' % posix(REDACT),
            'DIAG_PY="${GM_DIAG_PY:-/usr/lib/geomask/geomask-diag-redact.py}"',
            'DOCTOR="%s"' % posix(self.doctor),
        ]
        if env:
            lines.append(env)
        lines.append(STUBS)
        lines += [extract('_diag_missing'), extract('_diag_list_json'),
                  extract('_console_diag_fail'), extract('_console_diag_redact'),
                  extract('_console_diag_json'), extract('_diag_uint'),
                  extract('_diag_run'), extract('_console_get'), extract('json_esc'),
                  extract('_diag_index_json'), extract('_diag_bundle_entries'),
                  extract('_console_diag_build')]
        lines += [extract(name) for name in functions]
        lines += [body]
        path = self.case / 'run.sh'
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=120)
        line = next((line for line in result.stdout.splitlines()
                     if line.startswith('{"ok"')), None)
        self.assertIsNotNone(line, result.stdout + result.stderr)
        answer = json.loads(line)
        expected = 'RC=0' if answer.get('ok') else 'RC=1'
        self.assertIn(expected, result.stdout, result.stdout + result.stderr)
        return answer

    def run_bundle(self, **kwargs):
        return self.run_body('cmd_diag_bundle; printf "\\nRC=%s\\n" "$?"', **kwargs)

    def bundle_name(self, answer):
        return str(answer['name'])

    def members(self, answer):
        found = {}
        with tarfile.open(str(self.spool / self.bundle_name(answer)), 'r:gz') as bundle:
            for member in bundle.getmembers():
                if member.isfile():
                    found[member.name.rsplit('/', 1)[-1]] = bundle.extractfile(member).read()
        return found

    # ---------------------------------------------------------------- contract and entries
    def test_rpc_is_declared_in_manifest_and_acl(self):
        manifest = json.loads((ROOT / 'tools' / 'geo-rpc.json').read_text(encoding='utf-8'))
        method = next(item for item in manifest['methods'] if item['name'] == 'diag_bundle')
        self.assertEqual(method['acl'], 'write')
        self.assertEqual(method['args'], [])
        acl = json.loads((ROOT / 'assets' / 'acl-geo-sync.json').read_text(encoding='utf-8'))
        role = acl['luci-app-geo-sync']
        self.assertIn('diag_bundle', role['write']['ubus']['geo-sync'])
        self.assertNotIn('diag_bundle', role['read']['ubus']['geo-sync'])
        self.assertIn('diag_bundle)', SOURCE.split('# >>> geo-rpc generated:dispatch', 1)[1])

    def test_every_listed_check_gets_an_entry_with_state_and_log_state(self):
        answer = self.run_bundle()
        self.assertTrue(answer['ok'], answer)
        self.assertGreater(answer['bytes'], 0)
        self.assertEqual(answer['checks'], 4)
        self.assertEqual(answer['not_run'], 2)
        self.assertNotIn('/', answer['name'], 'the answer must be a name, not a path')
        self.assertNotIn('data', answer, 'the build RPC must not carry base64')
        members = self.members(answer)
        index = json.loads(members['index.json'].decode('utf-8'))
        self.assertRegex(index['generated_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
        entries = {entry['id']: entry for entry in index['entries']}
        self.assertEqual(set(entries), {'router_system', 'net_dns', 'net_mtu', 'path_ai'})
        for entry in entries.values():
            self.assertEqual(set(entry),
                             {'id', 'title', 'area', 'state', 'last_state_at', 'log_state'})
        self.assertEqual(entries['router_system']['state'], 'ok')
        self.assertEqual(entries['router_system']['log_state'], 'included')
        self.assertEqual(entries['router_system']['last_state_at'],
                         '2026-11-17T10:00:00+0300')
        self.assertEqual(entries['net_dns']['state'], 'fail')
        self.assertEqual(entries['net_dns']['log_state'], 'included')
        self.assertEqual(entries['net_mtu']['state'], 'not_run')
        self.assertEqual(entries['net_mtu']['log_state'], 'not_run')
        self.assertEqual(entries['net_mtu']['last_state_at'], '')
        self.assertEqual(entries['path_ai']['state'], 'not_run')
        self.assertEqual(entries['path_ai']['log_state'], 'empty')
        self.assertIn('router_system.log', members)
        self.assertIn('net_dns.log', members)
        self.assertNotIn('net_mtu.log', members)
        self.assertNotIn('path_ai.log', members)
        # The same archive already contains the console system set (reused, not copied code).
        for expected in ('summary.txt', 'firewall.uci', 'network.uci', 'nft.txt',
                         'logread.txt', 'group-CONSOLE.json', 'connections.json',
                         'config.masked.yaml', 'ip-rule.txt', 'tun.txt',
                         'route-table-102.txt'):
            self.assertIn(expected, members, 'the console collector was not reused')

    def test_building_the_archive_never_runs_a_check(self):
        answer = self.run_bundle()
        self.assertTrue(answer['ok'], answer)
        self.assertFalse(self.probe.exists(), 'the bundle called the run/probe path')
        bundle_code = '\n'.join((extract('cmd_diag_bundle'), extract('_diag_bundle_entries'),
                                 extract('_console_diag_build')))
        self.assertNotIn('cmd_diag_run', bundle_code)
        self.assertNotIn('run all', bundle_code)

    def test_secrets_in_saved_logs_are_masked_before_packing(self):
        answer = self.run_bundle()
        self.assertTrue(answer['ok'], answer)
        sentinels = ('BUNDLE-SECRET-TOKEN', 'BUNDLE-PASS-123', 'BUNDLE-URL-456',
                     'BUNDLE-QS-789')
        with tarfile.open(str(self.spool / self.bundle_name(answer)), 'r:gz') as bundle:
            for member in bundle.getmembers():
                content = member.name.encode('utf-8')
                if member.isfile():
                    content += bundle.extractfile(member).read()
                for sentinel in sentinels:
                    self.assertNotIn(sentinel.encode('utf-8'), content, member.name)
        members = self.members(answer)
        self.assertIn('resolver refused', members['net_dns.log'].decode('utf-8'))

    def test_the_verify_gate_refuses_an_unmasked_diagnostic_log(self):
        passthrough = self.case / 'passthrough-redact.py'
        passthrough.write_text(
            'import subprocess, sys\n'
            'REAL = %r\n'
            'if len(sys.argv) > 1 and sys.argv[1] == "redact":\n'
            '    sys.stdout.buffer.write(sys.stdin.buffer.read())\n'
            'else:\n'
            '    sys.exit(subprocess.run([sys.executable, REAL] + sys.argv[1:]).returncode)\n'
            % native(REDACT), encoding='utf-8')
        answer = self.run_body(
            'GM_DIAG_PY="%s"; DIAG_PY="$GM_DIAG_PY"\n'
            'cmd_diag_bundle; printf "\\nRC=%%s\\n" "$?"' % native(passthrough))
        self.assertFalse(answer['ok'], answer)
        self.assertIn('bundle contains a secret', answer['error'])
        leftovers = [entry.name for entry in self.spool.iterdir()
                     if entry.name.startswith('geomask-console-diag')]
        self.assertEqual(leftovers, [])

    # ---------------------------------------------------------------- spool registration
    def test_archive_is_registered_into_the_private_spool_with_the_shell_quota(self):
        calls = self.case / 'spool-calls'
        wrapper = self.case / 'spool-wrapper.py'
        wrapper.write_text(
            'import runpy, sys\n'
            'REAL = %r\n'
            'CALLS = %r\n'
            'with open(CALLS, "a", encoding="utf-8") as handle:\n'
            '    handle.write(" ".join(sys.argv[1:]) + "\\n")\n'
            'runpy.run_path(REAL, run_name="__main__")\n'
            % (native(SPOOL), native(calls)), encoding='utf-8')
        answer = self.run_body(
            'cmd_diag_bundle; printf "\\nRC=%s\\n" "$?"',
            env='GM_CONSOLE_DIAG_QUOTA=2\nGM_CONSOLE_DIAG_FILES=4',
            spool_py=wrapper)
        self.assertTrue(answer['ok'], answer)
        text = calls.read_text(encoding='utf-8')
        self.assertIn('init ', text)
        register = next(line for line in text.splitlines() if line.startswith('register '))
        self.assertIn('--quota 2097152', register)
        self.assertIn('--files 4', register)
        name = self.bundle_name(answer)
        sidecar = self.spool / (name + '.json')
        self.assertTrue(sidecar.is_file(), 'the archive was not registered in the spool')
        record = json.loads(sidecar.read_text(encoding='utf-8'))
        self.assertEqual(record['name'], name)
        self.assertEqual(record['bytes'], answer['bytes'])
        self.assertEqual(record['schema'], 1)

    def test_download_only_goes_through_console_diag_get(self):
        answer = self.run_bundle()
        name = self.bundle_name(answer)
        got = self.run_body(
            'cmd_console_diag_get \'%s\'; printf "\\nRC=%%s\\n" "$?"' % name,
            functions=('cmd_console_diag_get',))
        self.assertTrue(got['ok'], got)
        self.assertEqual(got['name'], name)
        self.assertEqual(base64.b64decode(got['data']),
                         (self.spool / name).read_bytes())
        getter = extract('cmd_console_diag_get')
        self.assertIn('DIAG_SPOOL_PY', getter)
        # No second download path: the bundle RPC returns a registered name and no bytes.
        builder = extract('cmd_diag_bundle') + extract('_console_diag_build')
        self.assertNotIn('base64', builder)
        self.assertNotIn('"data"', builder)

    # ---------------------------------------------------------------- UI (static, no browser)
    def test_the_diagnostics_tab_has_exactly_one_archive_button(self):
        page = (ROOT / 'assets' / 'geo-sync-page.js').read_text(encoding='utf-8')
        self.assertEqual(page.count('Скачать архив диагностики'), 1)
        self.assertIn('callDiagBundle()', page)
        self.assertIn('callConsoleDiagGet({ name: name })', page)
        self.assertNotIn('Собрать и скачать логи', page,
                         'the console card still offers a second archive flow')
        self.assertNotIn('conDiagBuild', page)
        self.assertNotIn('conDiagSaveLast', page)
        # The object URL is built from downloaded bytes; a network answer is never a link.
        self.assertIn('var url = URL.createObjectURL(blob)', page)
        self.assertNotRegex(page, r"href:\s*(?:res|got)\b")
        self.assertNotIn('location.href =', page)

    def test_the_bundle_reuses_one_collector_and_one_packer(self):
        self.assertEqual(SOURCE.count('_console_diag_build() {'), 1)
        self.assertEqual(SOURCE.count('cmd_console_diag() { _console_diag_build console; }'), 1)
        self.assertEqual(SOURCE.count('cmd_diag_bundle() { _console_diag_build bundle; }'), 1)
        self.assertEqual(SOURCE.count('tar -czf "$_tgz"'), 1,
                         'a second archive exporter appeared')
        self.assertEqual(SOURCE.count('"$DIAG_SPOOL_PY" register'), 1,
                         'a second registration path appeared')


if __name__ == '__main__':
    unittest.main()
