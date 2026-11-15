"""Console lane: an empty field must be a reset, not "keep the previous value".

Review 14-15.11.2026, point 11. The backend wrote `countries`/`sub` to the state file only when
they were non-empty, so after a first switch-on an empty panel field silently kept the old
filter and the old subscription -- "как у VPN" could not be restored. `--url default` is the
explicit way to say "point the console provider at the subscription the VPN uses".

The shell part runs the real `cmd_console` extracted from the shipped asset against stub `ip`
and a state file inside the temporary directory: no router, no `/etc`, no `/tmp`.
"""
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'assets' / 'geomask-console.py'
BACKEND = ROOT / 'assets' / 'geo-backend.sh'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()
BACKEND_TEXT = BACKEND.read_text(encoding='utf-8')

PINNED = """\
mixed-port: 7890
mode: rule
proxy-providers:
  sub:
    type: http
    url: "https://example.invalid/restricted"
    interval: 3600
  tolerant:
    type: http
    url: "https://example.invalid/tolerant"
    interval: 3600
    header:
      User-Agent: "tolerant-agent"
  console:
    type: http
    url: "https://example.invalid/pinned"
    interval: 3600
    header:
      User-Agent: "pinned-agent"
proxy-groups:
  - name: VPN
    type: url-test
    use:
      - tolerant
  - name: AI
    type: url-test
    use:
      - sub
  - name: CONSOLE
    type: select
    use:
      - console
    filter: "Турция"
rules:
  - MATCH,DIRECT
"""


def load_module():
    spec = importlib.util.spec_from_file_location('geomask_console_reset', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def extract(name):
    """Whole function body, brace-balanced but heredoc-aware (same rule as test_backend_safety)."""
    lines = BACKEND_TEXT.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(name + '() {'):
            start = index
            break
    if start is None:
        raise AssertionError('function %s not found in the backend' % name)
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
        if line.rstrip() == '}':
            return '\n'.join(lines[start:index + 1])
    raise AssertionError('function %s has no closing brace' % name)


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


class ConsoleProviderResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.config = self.case / 'config.yaml'
        self.config.write_text(PINNED, encoding='utf-8')
        self.module = load_module()

    def run_main(self, *args):
        return self.module.main(list(args))

    def doc(self):
        return yaml.safe_load(self.config.read_text(encoding='utf-8'))

    def test_an_empty_subscription_repoints_the_console_provider(self):
        # `--url default` means "as the VPN": the pinned URL and the header copied with it are
        # dropped and both are re-derived from the group the VPN rides.
        code = self.run_main('apply', str(self.config), '--url', 'default')
        self.assertEqual(code, 0)
        provider = self.doc()['proxy-providers']['console']
        self.assertEqual(provider['url'], 'https://example.invalid/tolerant')
        self.assertEqual(provider['header'], {'User-Agent': 'tolerant-agent'})

    def test_the_reset_accepts_the_equals_form(self):
        # `--url=default` is the same instruction as `--url default`.
        code = self.run_main('apply', str(self.config), '--url=default')
        self.assertEqual(code, 0)
        provider = self.doc()['proxy-providers']['console']
        self.assertEqual(provider['url'], 'https://example.invalid/tolerant')

    def test_a_plain_apply_keeps_the_pinned_subscription(self):
        # The reset must stay opt-in: `on` without a choice keeps what is configured.
        code = self.run_main('apply', str(self.config))
        self.assertEqual(code, 0)
        provider = self.doc()['proxy-providers']['console']
        self.assertEqual(provider['url'], 'https://example.invalid/pinned')
        self.assertEqual(provider['header'], {'User-Agent': 'pinned-agent'})

    def test_an_empty_country_list_clears_the_filter(self):
        # Shell side: the state is written even when the value is empty, and the patcher is
        # asked for `--url default` when no subscription was chosen.
        self.assertNotIn('[ -n "$_countries" ] && _console_set countries', BACKEND_TEXT)
        self.assertNotIn('[ -n "${3:-}" ] && _console_set sub', BACKEND_TEXT)
        self.assertRegex(BACKEND_TEXT, r'(?m)^\s*_console_set countries "\$_countries"$')
        self.assertRegex(BACKEND_TEXT, r'(?m)^\s*_console_set sub "\$_sub"$')
        self.assertIn('apply "$_cand" --url default --countries "$_countries"', BACKEND_TEXT)
        # Patcher side: an empty list removes the filter it installed earlier.
        code = self.run_main('apply', str(self.config), '--countries', '')
        self.assertEqual(code, 0)
        group = next(g for g in self.doc()['proxy-groups'] if g['name'] == 'CONSOLE')
        self.assertNotIn('filter', group)
        self.assertEqual(group['use'], ['console'])


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.state = self.case / 'console.state'

    def run_backend(self, body):
        header = (
            'set -u\n'
            'CONSOLE_STATE="%s"\nCONSOLE_TABLE=102\nCONSOLE_TUN_DEV=Console\n'
            'CONSOLE_RULE_PREF=19903\nCONSOLE_SOCKS_PORT=7893\nCONSOLE_TUN_IP=198.21.0.1\n'
            'CONSOLE_PY=/nonexistent/geomask-console.py\n'
            'PYTHON_BIN=/nonexistent/python3\n'
            'LOG="%s"\n'
            'ip() { return 0; }\n'
            % (posix(self.state), posix(self.case / 'geo-sync.log')))
        script = (header + '\n'.join(extract(name) for name in
                                     ('_console_get', '_console_set', 'cmd_console'))
                  + '\n' + body)
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    def test_console_status_reports_the_saved_subscription(self):
        # Point 11: the panel restores the subscription field from `status`, so the stored id
        # has to be in the answer (it was written to the state file but never reported).
        self.state.write_text('enabled=1\nip=192.0.2.50\nsub=7\ncountries=TR\n',
                              encoding='utf-8', newline='\n')
        result = self.run_backend('cmd_console status "" "" ""; printf "\\nRC=%s\\n" "$?"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=0', result.stdout)
        self.assertIn('"sub":"7"', result.stdout)
        self.assertIn('"countries":"TR"', result.stdout)
        self.assertIn('"ip":"192.0.2.50"', result.stdout)

    def test_console_status_reports_an_empty_subscription_as_empty(self):
        self.state.write_text('enabled=1\nip=192.0.2.50\nsub=\ncountries=\n',
                              encoding='utf-8', newline='\n')
        result = self.run_backend('cmd_console status "" "" ""')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"sub":""', result.stdout)
        self.assertIn('"countries":""', result.stdout)


if __name__ == '__main__':
    unittest.main()
