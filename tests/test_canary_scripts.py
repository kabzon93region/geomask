"""The canary tooling is executed against a live router, so its safety properties are tested.

The scripts themselves run on the PC and on the router from /tmp; they are not shipped by the
installer. What is checked here is exactly what would be expensive to get wrong in a run:
ASCII-only scripts (project rule), shell syntax, the probe/watchdog key contract, host-key
verification on every ssh/scp, and a watchdog that installs nothing unless asked.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / 'tools' / 'canary'
PROBE = CANARY / 'probe.sh'
RESTORE = CANARY / 'restore.sh'
WATCHDOG = CANARY / 'run-canary.ps1'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def text(path):
    return path.read_text(encoding='utf-8')


def posix(path):
    value = str(Path(path).resolve()).replace('\\', '/')
    if value.startswith('/'):
        return value
    return '/' + value[0].lower() + value[2:]


class CanaryToolingTests(unittest.TestCase):
    def test_the_scripts_are_ascii_only(self):
        for path in (PROBE, RESTORE, WATCHDOG):
            self.assertTrue(path.exists(), 'missing %s' % path.name)
            bad = [(index, ch) for index, ch in enumerate(text(path)) if ord(ch) > 0x7E]
            self.assertEqual(bad, [], '%s contains non-ASCII characters' % path.name)

    @unittest.skipIf(BASH is None, 'bash is required')
    def test_the_shell_scripts_have_valid_syntax(self):
        for path in (PROBE, RESTORE):
            result = subprocess.run([BASH, '-n', posix(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, '%s: %s' % (path.name, result.stderr))

    def test_the_probe_reports_every_key_the_watchdog_reads(self):
        probe = text(PROBE)
        keys = ['version', 'uptime', 'mihomo', 'tun', 'rules', 'nft', 'dns', 'inet',
                'backupok', 'modules', 'load']
        for key in keys:
            self.assertIn("printf '%s=" % key, probe, 'probe does not print %s=' % key)
        watchdog = text(WATCHDOG)
        for key in ('mihomo', 'inet', 'tun', 'rules', 'dns', 'nft', 'backupok'):
            self.assertIn("'%s'" % key, watchdog,
                          'the watchdog never reads the %s probe key' % key)

    def test_every_ssh_and_scp_pins_the_host_key(self):
        watchdog = text(WATCHDOG)
        self.assertNotIn('StrictHostKeyChecking=no', watchdog)
        self.assertNotIn('StrictHostKeyChecking accept-new', watchdog)
        self.assertNotIn('UserKnownHostsFile', watchdog, 'known_hosts must stay the default')
        self.assertGreaterEqual(watchdog.count("'StrictHostKeyChecking=yes'"), 3,
                                'each ssh/scp call site must pin the host key')
        self.assertGreaterEqual(watchdog.count("'-o', 'BatchMode=yes'"), 3)
        self.assertIn('BatchMode=yes', watchdog)

    def test_the_watchdog_installs_only_when_explicitly_asked(self):
        watchdog = text(WATCHDOG)
        self.assertIn('[switch]$Execute', watchdog)
        self.assertIn('if (-not $Execute)', watchdog)
        # The install command must be the only place that runs --auto on the router.
        self.assertEqual(watchdog.count('geomask.sh --auto --yes --restart'), 1)
        # A rollback is never triggered by a single bad round.
        self.assertIn('[int]$FailThreshold = 3', watchdog)
        self.assertIn('$fails -ge $FailThreshold', watchdog)

    @unittest.skipIf(BASH is None, 'bash is required')
    def test_restore_refuses_without_its_archives(self):
        result = subprocess.run([BASH, posix(RESTORE)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('RESTORE_FAIL', result.stderr)

    def test_restore_touches_only_geomask_paths_missing_from_the_set(self):
        restore = text(RESTORE)
        self.assertIn('grep -qxF "$p" "$SET" && continue', restore)
        for pattern in ('/etc/geomask/*', '/etc/mihomo/*', '/etc/init.d/geomask*',
                        '/usr/lib/geomask/*', '/www/luci-static/resources/*geomask*'):
            self.assertIn(pattern, restore, 'cleanup lost the %s allow-list entry' % pattern)
        self.assertNotIn('/etc/config/*', restore,
                         'cleanup must never delete files inside /etc/config')


if __name__ == '__main__':
    unittest.main()
