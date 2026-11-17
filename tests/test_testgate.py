"""The gate itself: a green run must mean that tests actually executed (R06 M2).

These tests drive `tests/testgate.py` against throwaway one-module suites. The gate is the
thing that decides whether a run "passed", so its honesty is checked here instead of being
assumed: zero tests, all tests skipped, and fewer tests than the manifest must all be FAIL;
more tests than the manifest is allowed, because the manifest is a lower bound.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import testgate

PASSING = '''\
import unittest

class PassingTests(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)
'''

EMPTY = '''\
import unittest

class EmptyTests(unittest.TestCase):
    pass
'''

ALL_SKIPPED = '''\
import unittest

class SkippedTests(unittest.TestCase):
    @unittest.skip('posix-only: nothing was executed')
    def test_skipped(self):
        pass
'''

TWO_PASSING = PASSING.replace('    def test_one(self):\n        self.assertTrue(True)\n',
                              '    def test_one(self):\n        self.assertTrue(True)\n\n'
                              '    def test_two(self):\n        self.assertTrue(True)\n')


class GateTests(unittest.TestCase):
    def run_gate(self, modules, expected):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        suite = Path(tmp.name) / 'suite'
        suite.mkdir()
        for name, source in modules.items():
            (suite / name).write_text(source, encoding='utf-8', newline='\n')
        manifest = Path(tmp.name) / 'expected-counts.json'
        manifest.write_text(json.dumps(expected), encoding='utf-8')
        logs = Path(tmp.name) / 'logs'
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = testgate.run_modules(str(suite), str(logs), 60, sys.executable,
                                      str(manifest))
        summary = (logs / 'summary.txt').read_text(encoding='utf-8')
        return rc, output.getvalue(), summary

    def test_a_module_that_ran_no_tests_is_a_failure(self):
        rc, output, summary = self.run_gate({'test_empty.py': EMPTY},
                                            {'test_empty.py': 1})
        self.assertEqual(rc, 1)
        self.assertIn('verdict: FAIL', summary)
        self.assertIn('no module ran a test', output)

    def test_a_run_where_every_test_was_skipped_is_a_failure(self):
        rc, output, summary = self.run_gate({'test_skipped.py': ALL_SKIPPED},
                                            {'test_skipped.py': 1})
        self.assertEqual(rc, 1)
        self.assertIn('verdict: FAIL', summary)
        self.assertIn('no test actually executed', summary)

    def test_fewer_tests_than_the_manifest_is_a_failure(self):
        rc, output, summary = self.run_gate({'test_one.py': PASSING},
                                            {'test_one.py': 2})
        self.assertEqual(rc, 1)
        self.assertIn('verdict: FAIL', summary)
        self.assertIn('below manifest', summary)

    def test_more_tests_than_the_manifest_is_allowed(self):
        rc, output, summary = self.run_gate({'test_two.py': TWO_PASSING},
                                            {'test_two.py': 1})
        self.assertEqual(rc, 0)
        self.assertIn('verdict: PASS', summary)
        self.assertIn('tests actually executed: 2', summary)

    def test_a_manifest_module_missing_from_the_run_is_a_failure(self):
        rc, output, summary = self.run_gate({'test_one.py': PASSING},
                                            {'test_one.py': 1, 'test_deleted.py': 5})
        self.assertEqual(rc, 1)
        self.assertIn('verdict: FAIL', summary)
        self.assertIn('test_deleted.py', summary)
        self.assertIn('missing modules', summary)

    def test_a_utf8_bom_manifest_is_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        manifest = Path(tmp.name) / 'expected-counts.json'
        manifest.write_bytes(b'\xef\xbb\xbf{"test_one.py": 1}\n')
        expected, problem = testgate.load_expected(str(manifest))
        self.assertEqual('', problem)
        self.assertEqual({'test_one.py': 1}, expected)

    def test_missing_manifest_is_fail_closed(self):
        # run_gate always writes a manifest; point the gate at an absent one explicitly.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        suite = Path(tmp.name) / 'suite'
        suite.mkdir()
        (suite / 'test_one.py').write_text(PASSING, encoding='utf-8', newline='\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = testgate.run_modules(str(suite), str(Path(tmp.name) / 'logs'), 60,
                                      sys.executable, str(Path(tmp.name) / 'absent.json'))
        self.assertEqual(rc, 1)
        self.assertIn('cannot read expected counts', output.getvalue())


if __name__ == '__main__':
    unittest.main()
