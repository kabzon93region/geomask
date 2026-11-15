"""The writer's trial step: commands spelled out, no shell-executed strings in shipped code.

Review 14-15.11.2026, point 28 (T14). `assets/safe_write.sh` ran its per-resource trial step as
a string handed to the shell at run time. Every value was a literal, but the mihomo entry
interpolated the candidate path into that string, so a candidate file whose name contained `;`
would have executed the rest of the name - in the one script that rewrites configs on the
router. The old code is the reason both tests below exist: the first fails while any shipped
shell file contains that construct, the second drives the dispatcher with a hostile path.

The dispatcher is taken from the shipped file and run against stubs, so no config on this host
is read or written.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets'
SAFE_WRITE = ASSETS / 'safe_write.sh'
SOURCE = SAFE_WRITE.read_text(encoding='utf-8')


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def extract(name):
    lines = SOURCE.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(name + '() {')), None)
    if start is None:
        raise AssertionError('function %s not found in safe_write.sh' % name)
    for index in range(start + 1, len(lines)):
        if lines[index] == '}':
            return '\n'.join(lines[start:index + 1])
    raise AssertionError('function %s has no closing brace' % name)


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


def shipped_shell_files():
    """Every shell file the installer writes to the router (plus the installer itself)."""
    files = [path for path in ASSETS.glob('*.sh')]
    files += [path for path in ASSETS.glob('*.init')]
    files += [ASSETS / '99-default-vpn-route', ASSETS / '99-geomask-guard']
    files += [ROOT / 'geomask.sh.in']
    return [path for path in files if path.is_file()]


@unittest.skipIf(BASH is None, 'bash is required')
class SafeWriteTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.log = self.case / 'calls.log'
        self.log.write_text('', encoding='utf-8', newline='\n')

    def run_trial(self, resource, candidate='cand.yaml'):
        stubs = ('_rec() { printf "%%s\\n" "$*" >> "%s"; }\n' % posix(self.log)
                 + 'mihomo() { _rec "mihomo $*"; return 0; }\n'
                 + 'nft() { _rec "nft $*"; return 0; }\n'
                 + 'crontab() { _rec "crontab $*"; return 0; }\n'
                 + 'magitrickled() { _rec "magitrickled $*"; return 0; }\n')
        script = ('set -u\n%s\n%s\n_safe_write_trial "%s" "%s"\nprintf "RC=%%s\\n" "$?"\n'
                  % (stubs, extract('_safe_write_trial'), resource, candidate))
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    def calls(self):
        return [line for line in self.log.read_text(encoding='utf-8').splitlines() if line.strip()]

    # ---------------------------------------------------------------- no shell-executed strings
    def test_no_shipped_shell_file_hands_a_string_to_the_shell(self):
        """The construct that executed a candidate path is gone from every shipped script.

        Only the command-position token counts: the word also appears inside ordinary strings
        ("version source unreachable"), and a test that trips on prose gets switched off.
        """
        offenders = []
        for path in shipped_shell_files():
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                code = line.split('#', 1)[0]
                for part in code.replace(';', ' ').split():
                    if part == 'eval':
                        offenders.append('%s:%d: %s' % (path.name, number, line.strip()))
        self.assertEqual([], offenders,
                         'shipped shell code runs a string through the shell:\n'
                         + '\n'.join(offenders))

    # ---------------------------------------------------------------- the dispatcher
    def test_the_mihomo_trial_validates_the_candidate(self):
        result = self.run_trial('mihomo_config', '/tmp/cand.yaml')
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.calls(), ['mihomo -f /tmp/cand.yaml -d /etc/mihomo -t'])

    def test_a_ruleset_trial_validates_the_whole_config(self):
        result = self.run_trial('ruleset_ai', '/tmp/ai.yaml')
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.calls(), ['mihomo -f /etc/mihomo/config.yaml -d /etc/mihomo -t'])

    def test_a_config_only_resource_has_nothing_to_try(self):
        result = self.run_trial('nodes_conf', '/tmp/nodes.conf')
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_cron_trial_reads_the_crontab(self):
        result = self.run_trial('cron_root', '/tmp/cron')
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.calls(), ['crontab -l'])

    def test_an_unknown_resource_has_no_trial(self):
        """A typo must not mean "nothing to check": the dispatcher refuses what it does not know."""
        result = self.run_trial('mihomo_config_typo', '/tmp/cand.yaml')
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)

    def test_a_hostile_candidate_name_is_only_ever_an_argument(self):
        """The candidate path used to be interpolated into the trial string before execution."""
        marker = self.case / 'pwned'
        hostile = '%s/x; touch %s' % (posix(self.case), posix(marker))
        result = self.run_trial('mihomo_config', hostile)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertFalse(marker.exists(), 'the candidate name was executed as a command')
        self.assertEqual(self.calls(),
                         ['mihomo -f %s/x; touch %s -d /etc/mihomo -t' % (posix(self.case),
                                                                          posix(marker))])


if __name__ == '__main__':
    unittest.main()
