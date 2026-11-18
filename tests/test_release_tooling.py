"""R01: the release tooling may not claim more than it proved, nor skip the gate.

The audit found two things: publishing ran a raw `unittest discover` and a *mutating*
`check_build`, bypassing testgate's contract (per-module minimums, allowlisted skips,
timeouts), and `verify` without `--full` printed "installs and matches the build" after
only `sh -n` and `--list-modules` - nothing had been installed or even run.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELEASE = (ROOT / 'tools' / 'release.py').read_text(encoding='utf-8')


class ReleaseToolingTests(unittest.TestCase):
    def test_publish_uses_the_gate_not_a_raw_discovery_run(self):
        body = RELEASE
        self.assertIn("tests/testgate.py", body,
                      'publishing does not run the gate')
        self.assertNotIn("'-m', 'unittest', 'discover'", body,
                         'publishing still bypasses the gate with a raw discovery run')

    def test_readiness_does_not_rewrite_the_artifact(self):
        for match in re.finditer(r"tools/check_build\.py'", RELEASE):
            window = RELEASE[match.start():match.start() + 120]
            self.assertIn("--verify-only", window,
                          'a release check runs a mutating check_build')

    def test_verify_without_full_does_not_claim_installation(self):
        tail = RELEASE[RELEASE.index('def verify('):RELEASE.index('def drift(')]
        self.assertIn('установка НЕ проверялась', tail,
                      'verify without --full still claims the release installs')
        self.assertNotIn("'релиз %s устанавливается", tail)

    def test_the_full_path_names_the_sandbox_it_ran(self):
        tail = RELEASE[RELEASE.index('def verify('):RELEASE.index('def drift(')]
        self.assertIn('tests/run_tests.sh', tail)
        self.assertIn('if full:', tail)

    def test_release_notes_name_the_gate_contract(self):
        self.assertIn('testgate (counts/skips/timeouts)', RELEASE,
                      'the release notes do not say what publishing verified')


if __name__ == '__main__':
    unittest.main()
