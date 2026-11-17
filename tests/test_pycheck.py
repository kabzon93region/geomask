"""Regression tests for tools/pycheck.py (R06 follow-up).

The tool used to read a heredoc as "everything after <<'PY'", which pulled the rest of the
invocation line (`>/dev/null 2>&1`) into ast.parse and produced a false syntax error for
assets/geomask-dnsguard.sh. It also checked only the first block per file and always
exited 0. These tests pin the contract:

  * dnsguard parses OK;
  * a trailing redirection/arguments on the opener line are not part of the body;
  * PY, PYEOF, quoted and unquoted openers, multiple blocks per file;
  * a real syntax error gives rc 1 with file:line;
  * a file with no block gives rc 2 (the gate checked nothing);
  * pycheck and tests/pyblocks.py agree on every assets/*.sh.
"""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYCHECK = ROOT / 'tools' / 'pycheck.py'
PYBLOCKS = ROOT / 'tests' / 'pyblocks.py'
ASSETS = sorted((ROOT / 'assets').glob('*.sh'))
TOTAL = re.compile(r'total embedded blocks: (\d+)')
FILE_LINE = re.compile(r'^(?P<path>.+):(?P<line>\d+) \((?:PY|PYEOF)\): SYNTAX ERROR', re.M)


def run(script, paths):
    completed = subprocess.run(
        [sys.executable, '-B', str(script)] + [str(p) for p in paths],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    return completed.returncode, (completed.stdout or '') + (completed.stderr or '')


def block_count(output):
    match = TOTAL.search(output)
    return int(match.group(1)) if match else -1


class PycheckTests(unittest.TestCase):
    def _write(self, directory, name, text):
        path = Path(directory) / name
        path.write_text(text, encoding='utf-8')
        return path

    def test_dnsguard_parses_ok(self):
        path = ROOT / 'assets' / 'geomask-dnsguard.sh'
        rc, output = run(PYCHECK, [path])
        self.assertEqual(0, rc, output)
        self.assertIn('geomask-dnsguard.sh', output)
        self.assertIn('python parses OK', output)

    def test_trailing_redirection_is_not_part_of_the_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, 'tail.sh', (
                '#!/bin/sh\n'
                '"$PY" - "$@" <<\'PY\' >/dev/null 2>&1\n'
                'value = 1 + 1\n'
                'PY\n'))
            rc, output = run(PYCHECK, [path])
            self.assertEqual(0, rc, output)
            self.assertIn('python parses OK', output)

    def test_both_markers_and_multiple_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, 'multi.sh', (
                '#!/bin/sh\n'
                'python3 - <<\'PY\'\n'
                'first = 1\n'
                'PY\n'
                'python3 - <<\'PYEOF\' >/dev/null\n'
                'second = 2\n'
                'PYEOF\n'))
            rc, output = run(PYCHECK, [path])
            self.assertEqual(0, rc, output)
            self.assertEqual(2, block_count(output), output)

    def test_unquoted_and_dash_openers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, 'unquoted.sh', (
                '#!/bin/sh\n'
                'python3 - <<PY\n'
                'value = 3\n'
                'PY\n'
                'python3 - <<-"PY" \n'
                'value = 4\n'
                'PY\n'))
            rc, output = run(PYCHECK, [path])
            self.assertEqual(0, rc, output)
            self.assertEqual(2, block_count(output), output)

    def test_real_syntax_error_is_reported_with_file_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, 'broken.sh', (
                '#!/bin/sh\n'
                'python3 - <<\'PY\'\n'
                'def broken(:\n'
                '    pass\n'
                'PY\n'))
            rc, output = run(PYCHECK, [path])
            self.assertEqual(1, rc, output)
            self.assertIn('SYNTAX ERROR', output)
            match = FILE_LINE.search(output)
            self.assertIsNotNone(match, output)
            self.assertEqual(str(path), match.group('path'))
            self.assertEqual('3', match.group('line'))

    def test_file_without_a_block_returns_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, 'plain.sh', '#!/bin/sh\necho no python here\n')
            rc, output = run(PYCHECK, [path])
            self.assertEqual(2, rc, output)
            self.assertIn('no embedded python block', output)
            self.assertEqual(0, block_count(output), output)

    def test_pycheck_and_pyblocks_agree_on_every_asset(self):
        self.assertGreater(len(ASSETS), 0)
        check_rc, check_output = run(PYCHECK, ASSETS)
        blocks_rc, blocks_output = run(PYBLOCKS, ASSETS)
        self.assertEqual(0, check_rc, check_output)
        self.assertEqual(check_rc, blocks_rc, blocks_output)
        self.assertGreater(block_count(check_output), 0)
        self.assertEqual(block_count(check_output), block_count(blocks_output))


if __name__ == '__main__':
    unittest.main()
