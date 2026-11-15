"""Console diagnostic bundle: private directory, retention, and no silent copy into the share.

Review 14-15.11.2026, points 14 and 33. The bundle was assembled in a fixed
`/tmp/geomask-console-diag` that every request `rm -rf`'d first, so two panel clicks deleted
each other's half-built archive; and the result was copied into `BACKUP_DIR` - a folder that
may be published over SMB - without asking and without any retention.

The function is taken from the shipped backend and run against stubbed router commands; the
temporary directory, the share folder and the opt-in marker all live in the test's own sandbox.
"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
SOURCE = BACKEND.read_text(encoding='utf-8')


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def extract(name):
    """Whole function, brace-balanced at column 0 (no heredoc in these two)."""
    lines = SOURCE.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(name + '() {')), None)
    if start is None:
        raise AssertionError('function %s not found in the backend' % name)
    for index in range(start + 1, len(lines)):
        if lines[index] == '}':
            return '\n'.join(lines[start:index + 1])
    raise AssertionError('function %s has no closing brace' % name)


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


STUBS = r'''
uci() { return 0; }
nft() { return 0; }
logread() { printf 'mihomo line\n'; }
conntrack() { return 0; }
controller_curl() { return 0; }
ip() { printf '10: from 192.168.1.177 lookup 102\n'; return 0; }
'''


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleDiagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.spool = self.case / 'spool'
        self.spool.mkdir()
        self.share = self.case / 'share'
        self.share.mkdir()
        self.marker = self.case / 'backup-share.enable'
        self.state = self.case / 'console.state'
        self.state.write_text('enabled=1\nip=192.168.1.177\n', encoding='utf-8', newline='\n')
        (self.case / 'config.yaml').write_text('mixed-port: 7890\n', encoding='utf-8',
                                               newline='\n')

    def run_diag(self):
        script = '\n'.join([
            'set -u',
            'CONSOLE_STATE="%s"' % posix(self.state),
            'TMPDIR="%s"' % posix(self.spool),
            'BACKUP_DIR="%s"' % posix(self.share),
            'SMB_SHARE_ENABLE="%s"' % posix(self.marker),
            'CONSOLE_DIAG_KEEP_H=24',
            'CONSOLE_TABLE=102',
            'CONSOLE_TUN_DEV=Console',
            'BACKEND_VERSION=test',
            'MIHOMO_CONF="%s"' % posix(self.case / 'config.yaml'),
            'MIHOMO_API=http://127.0.0.1:9090',
            'CONSOLE_PY=/nonexistent/geomask-console.py',
            'PYTHON_BIN=/nonexistent/python3',
            STUBS,
            extract('_console_mask_stream'),
            extract('_console_get'),
            extract('cmd_console_diag'),
            'cmd_console_diag; printf "\\nRC=%s\\n" "$?"',
        ]) + '\n'
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=120)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        line = next(line for line in result.stdout.splitlines() if line.startswith('{"ok"'))
        return json.loads(line)

    def test_a_bundle_stays_out_of_the_share_without_the_marker(self):
        answer = self.run_diag()
        self.assertTrue(answer['ok'], answer)
        self.assertFalse(answer['shared'], 'the bundle went into the share without an opt-in')
        self.assertEqual(list(self.share.iterdir()), [])
        produced = Path(answer['file'].replace('/', str(self.case).replace('\\', '/')[0:2]))
        self.assertTrue(answer['file'].startswith(posix(self.spool)),
                        'the archive must sit in the temporary directory: %s' % answer['file'])
        self.assertGreater(answer['bytes'], 0)

    def test_an_opted_in_share_gets_a_copy(self):
        self.marker.write_text('', encoding='utf-8')
        answer = self.run_diag()
        self.assertTrue(answer['shared'], 'the opted-in share got no copy')
        names = [entry.name for entry in self.share.iterdir()]
        self.assertEqual(len(names), 1, names)
        self.assertIn('geomask-console-diag-', names[0])
        self.assertNotIn('console.state', names[0])

    def test_two_runs_do_not_share_a_directory_or_a_name(self):
        first = self.run_diag()
        second = self.run_diag()
        self.assertNotEqual(first['file'], second['file'],
                            'a fixed path would make the second run delete the first bundle')
        leftovers = [entry.name for entry in self.spool.iterdir()
                     if entry.name.startswith('geomask-console-diag.')]
        self.assertEqual(leftovers, [], 'a per-request directory was left behind')


if __name__ == '__main__':
    unittest.main()
