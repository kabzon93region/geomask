"""The console lane patcher: what it creates, what it removes, and what it must never touch."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'assets' / 'geomask-console.py'

BASE = """\
mixed-port: 7890
mode: rule
ipv6: false
profile:
  store-selected: true
listeners:
  - name: ai-devices
    type: mixed
    port: 7891
    listen: 127.0.0.1
    proxy: AI
proxy-providers:
  sub:
    type: http
    url: "https://example.invalid/sub"
    interval: 3600
proxy-groups:
  - name: VPN-noP2P
    type: url-test
    use:
      - sub
  - name: AI
    type: url-test
    use:
      - sub
    filter: 'USA'
rules:
  - MATCH,DIRECT
"""


def load_module():
    spec = importlib.util.spec_from_file_location('geomask_console', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConsolePatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.config = self.case / 'config.yaml'
        self.config.write_text(BASE, encoding='utf-8')
        self.module = load_module()

    def run_main(self, *args, stdin=None):
        old = sys.stdin
        if stdin is not None:
            import io
            sys.stdin = io.StringIO(stdin)
        try:
            return self.module.main(list(args))
        finally:
            sys.stdin = old

    def doc(self):
        return yaml.safe_load(self.config.read_text(encoding='utf-8'))

    def test_apply_creates_a_select_group_and_its_listener(self):
        code = self.run_main('apply', str(self.config), '--url',
                             'https://example.invalid/console', '--countries', 'TR,DE')
        self.assertEqual(code, 0)
        doc = self.doc()
        group = next(g for g in doc['proxy-groups'] if g['name'] == 'CONSOLE')
        self.assertEqual(group['type'], 'select')          # no health veto mid-match
        self.assertEqual(group['use'], ['console'])
        self.assertIn('Турция', group['filter'])
        self.assertIn('Germany', group['filter'])
        listener = next(l for l in doc['listeners'] if l['name'] == 'console')
        self.assertEqual(listener['proxy'], 'CONSOLE')
        self.assertEqual(listener['port'], 7893)
        self.assertEqual(doc['proxy-providers']['console']['url'],
                         'https://example.invalid/console')

    def test_apply_keeps_the_existing_provider_when_no_url_is_given(self):
        self.run_main('apply', str(self.config), '--url', 'https://example.invalid/first')
        code = self.run_main('apply', str(self.config), '--countries', 'NL')
        self.assertEqual(code, 0)
        provider = self.doc()['proxy-providers']['console']
        self.assertEqual(provider['url'], 'https://example.invalid/first')

    def test_apply_is_idempotent(self):
        self.run_main('apply', str(self.config), '--url', 'https://example.invalid/c')
        first = self.config.read_text(encoding='utf-8')
        self.run_main('apply', str(self.config), '--url', 'https://example.invalid/c')
        self.assertEqual(self.config.read_text(encoding='utf-8'), first)

    def test_url_from_stdin_keeps_the_token_out_of_argv(self):
        code = self.run_main('apply', str(self.config), '--url', '-',
                             stdin='https://example.invalid/secret-token\n')
        self.assertEqual(code, 0)
        self.assertEqual(self.doc()['proxy-providers']['console']['url'],
                         'https://example.invalid/secret-token')

    def test_remove_takes_away_exactly_what_we_added(self):
        self.run_main('apply', str(self.config), '--url', 'https://example.invalid/c')
        code = self.run_main('remove', str(self.config))
        self.assertEqual(code, 0)
        doc = self.doc()
        self.assertNotIn('console', [g['name'] for g in doc['proxy-groups']])
        self.assertNotIn('console', [l['name'] for l in doc['listeners']])
        self.assertNotIn('console', doc['proxy-providers'])
        # everything else survives untouched
        self.assertEqual([g['name'] for g in doc['proxy-groups']], ['VPN-noP2P', 'AI'])
        self.assertEqual([l['name'] for l in doc['listeners']], ['ai-devices'])
        self.assertEqual(doc['proxy-providers']['sub']['url'], 'https://example.invalid/sub')
        self.assertEqual(doc['rules'], ['MATCH,DIRECT'])

    def test_remove_on_a_clean_config_does_not_rewrite_it(self):
        before = self.config.read_text(encoding='utf-8')
        code = self.run_main('remove', str(self.config))
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text(encoding='utf-8'), before)

    def test_unknown_country_is_refused_and_the_file_is_untouched(self):
        before = self.config.read_text(encoding='utf-8')
        code = self.run_main('apply', str(self.config), '--url',
                             'https://example.invalid/c', '--countries', 'ZZ')
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_text(encoding='utf-8'), before)

    def test_a_missing_url_is_refused(self):
        # No URL anywhere: the provider cannot be created, and the file must stay untouched.
        self.config.write_text(BASE.replace('    url: "https://example.invalid/sub"\n', ''),
                               encoding='utf-8')
        before = self.config.read_text(encoding='utf-8')
        code = self.run_main('apply', str(self.config), '--countries', 'TR')
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_text(encoding='utf-8'), before)

    def test_first_switch_on_borrows_the_main_subscription(self):
        # Out of the box the console has no URL of its own; refusing would make the very first
        # switch-on fail on a working router.
        code = self.run_main('apply', str(self.config), '--countries', 'TR')
        self.assertEqual(code, 0)
        provider = self.doc()['proxy-providers']['console']
        self.assertEqual(provider['url'], 'https://example.invalid/sub')

    def test_yaml_12_booleans_survive_a_round_trip(self):
        self.config.write_text(BASE.replace('ipv6: false', 'ipv6: off'), encoding='utf-8')
        self.run_main('apply', str(self.config), '--url', 'https://example.invalid/c')
        text = self.config.read_text(encoding='utf-8')
        self.assertIn('ipv6: off', text)          # not "false"
        self.assertNotIn('ipv6: false', text)

    def test_status_reports_what_is_there(self):
        self.run_main('apply', str(self.config), '--url', 'https://example.invalid/c',
                      '--countries', 'TR')
        code = self.run_main('status', str(self.config))
        self.assertEqual(code, 0)
        doc = self.doc()
        self.assertIn('CONSOLE', [g['name'] for g in doc['proxy-groups']])


if __name__ == '__main__':
    unittest.main()
