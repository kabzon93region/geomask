"""Console diagnostic bundle: private directory, retention, no silent copy into the share,
and a download the panel can actually offer.

Review 14-15.11.2026, points 14, 15 and 33. The bundle was assembled in a fixed
`/tmp/geomask-console-diag` that every request `rm -rf`'d first, so two panel clicks deleted
each other's half-built archive; the result was copied into `BACKUP_DIR` - a folder that may be
published over SMB - without asking and without any retention; and the panel got only a path,
so the owner had to hunt for the file by hand.

The functions are taken from the shipped backend and run against stubbed router commands; the
temporary directory, the share folder and the opt-in marker all live in the test's own sandbox.
"""
from pathlib import Path
import base64
import hashlib
import json
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
    """Whole function, brace-balanced at column 0 (no heredoc in these)."""
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

    def run_body(self, body, functions=('cmd_console_diag',), maximum=None):
        lines = [
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
        ]
        if maximum is not None:
            lines.append('GM_CONSOLE_DIAG_MAX=%s' % maximum)
        lines += [STUBS]
        lines += [extract('_console_mask_stream'), extract('_console_get')]
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
        # The exit code has to agree with the answer: a JSON {"ok": false} with rc 0 is how a
        # failed download used to look successful to the panel.
        expected = 'RC=0' if answer.get('ok') else 'RC=1'
        self.assertIn(expected, result.stdout, result.stdout + result.stderr)
        return answer

    def run_diag(self):
        return self.run_body('cmd_console_diag; printf "\\nRC=%s\\n" "$?"')

    def get(self, name, maximum=None):
        return self.run_body('cmd_console_diag_get \'%s\'; printf "\\nRC=%%s\\n" "$?"' % name,
                             functions=('cmd_console_diag_get',), maximum=maximum)

    def bundle_name(self, answer):
        return str(answer['file']).rsplit('/', 1)[-1]

    # ---------------------------------------------------------------- the bundle itself
    def test_a_bundle_stays_out_of_the_share_without_the_marker(self):
        answer = self.run_diag()
        self.assertTrue(answer['ok'], answer)
        self.assertFalse(answer['shared'], 'the bundle went into the share without an opt-in')
        self.assertEqual(list(self.share.iterdir()), [])
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

    # ---------------------------------------------------------------- #15: the download
    def test_the_bytes_the_panel_receives_are_the_archive(self):
        built = self.run_diag()
        name = self.bundle_name(built)
        answer = self.get(name)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['name'], name)
        raw = base64.b64decode(answer['data'])
        self.assertEqual(raw, (self.spool / name).read_bytes(),
                         'the panel got something other than the archive')
        self.assertEqual(answer['bytes'], len(raw))
        self.assertEqual(answer['sha256'], hashlib.sha256(raw).hexdigest())

    def test_the_newest_bundle_can_be_fetched_without_a_name(self):
        self.run_diag()
        second = self.run_diag()
        answer = self.get('')
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['name'], self.bundle_name(second),
                         'the download did not pick the newest archive')

    def test_a_path_is_not_a_bundle_name(self):
        for name in ('../console.state', '/etc/shadow', 'etc/shadow',
                     'geomask-console-diag-..%2F..%2Fetc.tar.gz', 'other.tar.gz'):
            answer = self.get(name)
            self.assertFalse(answer['ok'], 'the backend accepted the name %r' % name)

    def test_a_missing_bundle_is_an_error_not_an_empty_file(self):
        answer = self.get('geomask-console-diag-20000101-000000.99999.tar.gz')
        self.assertFalse(answer['ok'], answer)
        self.assertIn('архива нет', answer['error'])

    def test_a_bundle_over_the_limit_is_refused_with_the_reason(self):
        """rpcd has to carry the bytes, so an oversized archive is a refusal, not a hang."""
        fat = self.spool / 'geomask-console-diag-20260101-000000.1234.tar.gz'
        fat.write_bytes(b'x' * 64)
        answer = self.get(fat.name, maximum=32)
        self.assertFalse(answer['ok'], answer)
        self.assertIn('64', answer['error'])
        self.assertIn('32', answer['error'])


if __name__ == '__main__':
    unittest.main()
