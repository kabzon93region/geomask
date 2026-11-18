"""Stage 0d: the repo check names oversized authored files and never fails CI advisory.

During stage 0 the plan wants a report, not a gate: most candidates are extracted next to
their repair task, and a hard failure would block CI for files the move has not reached.
Generated views and the single generated installer are never counted.
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import repo_check  # noqa: E402


class RepoCheckTests(unittest.TestCase):
    def test_the_real_tree_reports_authored_and_generated_separately(self):
        rows = repo_check.oversized(str(ROOT))
        kinds = {kind for _rel, _lines, kind in rows}
        self.assertIn('authored', kinds)
        self.assertIn('generated', kinds,
                      'the migrated delivery views are not reported as generated')
        generated = [rel for rel, _l, kind in rows if kind == 'generated']
        self.assertIn('assets/geo-backend.sh', generated)
        self.assertIn('geomask.sh.in', generated)

    def test_the_installer_is_reported_as_generated_not_oversized(self):
        rows = repo_check.oversized(str(ROOT))
        kinds = {rel: kind for rel, _l, kind in rows}
        self.assertEqual(kinds.get('geomask.sh'), 'generated')

    def test_parts_are_not_counted_as_their_original_file(self):
        rows = [row for row in repo_check.oversized(str(ROOT)) if row[2] == 'authored']
        paths = [rel for rel, _l, _k in rows]
        self.assertNotIn('assets/geo-sync-page.js', paths)
        self.assertFalse([p for p in paths if p.startswith('src/')],
                         'a part is counted per part, not as a monolith')

    def test_advisory_and_strict_agree_when_only_exceptions_remain(self):
        """After 0g every oversized file is either split or an explained exception."""
        advisory = subprocess.run([sys.executable, '-B', 'tools/repo_check.py'],
                                  cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(advisory.returncode, 0, advisory.stdout + advisory.stderr)
        self.assertIn('advisory during stage 0', advisory.stdout)
        self.assertIn('explained exception', advisory.stdout)
        strict = subprocess.run([sys.executable, '-B', 'tools/repo_check.py', '--strict'],
                                cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(strict.returncode, 0,
                         'an explained exception must not fail strict mode: ' + strict.stdout)

    def test_an_unexplained_oversized_file_fails_strict_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / 'big.sh').write_text('x\n' * 600, encoding='utf-8')
            strict = subprocess.run([sys.executable, '-B', 'tools/repo_check.py', '--strict',
                                     '--root', tmp], cwd=str(ROOT), capture_output=True,
                                    text=True, encoding='utf-8')
            self.assertEqual(strict.returncode, 1)
            self.assertIn('oversized', strict.stdout)

    def test_a_clean_small_tree_is_green_in_strict_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / 'tiny.sh').write_text('echo hi\n', encoding='utf-8')
            result = subprocess.run([sys.executable, '-B', 'tools/repo_check.py', '--strict',
                                     '--root', tmp], cwd=str(ROOT), capture_output=True,
                                    text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('OK: no authored file over', result.stdout)

    def test_a_broken_parts_manifest_is_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            parts = pathlib.Path(tmp) / 'src' / 'installer'
            parts.mkdir(parents=True)
            (parts / 'order.json').write_text('{oops', encoding='utf-8')
            result = subprocess.run([sys.executable, '-B', 'tools/repo_check.py', '--root', tmp],
                                    cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 1)
            self.assertIn('FAIL: cannot read the parts manifest', result.stdout)


if __name__ == '__main__':
    unittest.main()
