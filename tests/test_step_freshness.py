import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'geomask.sh.in').read_text(encoding='utf-8')


def function(name):
    start = SOURCE.index('%s() {' % name)
    end = SOURCE.index('\n}\n', start) + 3
    return SOURCE[start:end]


class Step2FreshnessTests(unittest.TestCase):
    """I08: a presence-only check must not be able to skip the apply.

    The check knew 13 of the files step2 installs and nothing about the doctor, so a router
    with an old doctor/lock/updater asset passed "check: ALL GOOD" and step2_apply was
    skipped - the old bytes stayed installed.
    """

    def installed(self):
        body = function('step2_apply')
        return set(re.findall(r'save_stream (\S+) ', body))

    def listed(self):
        block = SOURCE.split('STEP2_FILES="', 1)[1].split('"', 1)[0]
        return {line.strip() for line in block.splitlines() if line.strip()}

    def test_the_check_lists_exactly_what_step2_installs(self):
        installed, listed = self.installed(), self.listed()
        self.assertEqual(installed - listed, set(),
                         'step2 installs files its check does not know')
        self.assertEqual(listed - installed, set(),
                         'the check looks for files step2 does not install')

    def test_the_doctor_asset_is_required(self):
        self.assertIn('/usr/lib/geomask/geomask-doctor.py', self.listed(),
                      'a missing doctor still passes the step-2 check')
        self.assertIn('/usr/lib/geomask/geomask-lock.sh', self.listed(),
                      'a missing writer lock still passes the step-2 check')

    def test_a_passing_check_does_not_skip_the_apply(self):
        self.assertIn('step_check_is_authoritative', function('run_step'),
                      'run_step still trusts the check alone')
        body = function('step_check_is_authoritative')
        self.assertRegex(body, r'2\)\s*return 1',
                         'step 2 is still allowed to skip its apply on presence')

    def test_other_steps_may_still_skip(self):
        body = function('step_check_is_authoritative')
        self.assertIn('return 0', body)


if __name__ == '__main__':
    unittest.main()
