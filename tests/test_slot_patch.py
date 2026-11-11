"""Switching a slot must work on whatever YAML shape the config currently has.

The panel lost the ability to switch the AI slot because the patcher rewrote config text
with a regex that expected `  - name: GROUP`. Applying a pool re-serialises the config with
PyYAML (`- name:` at column 0), so every switch answered "no AI group anchor" although the
group was present. These tests drive the real patcher module on both shapes.
"""
import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

HERE = Path(__file__).resolve().parents[1] / 'assets'
PATCHER = HERE / 'geomask-slot-patch.py'

SUB_OLD = 'https://old.example.invalid/sub'
SUB_NEW = 'https://new.example.invalid/sub'


def _load():
    spec = importlib.util.spec_from_file_location('slot_patch', PATCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


patcher = _load()


LEGACY = '''mixed-port: 7890
find-process-mode: off
proxy-providers:
  sub2:
    type: http
    url: "https://old.example.invalid/sub"
    interval: 3600
  slot:
    type: http
    url: "https://new.example.invalid/sub"
    interval: 3600
proxy-groups:
  - name: VPN
    type: url-test
    use:
      - sub2
  - name: VPN-noP2P
    type: url-test
    use:
      - sub2
  - name: AI
    type: url-test
    use:
      - slot
rules:
  - RULE-SET,mtrickle,VPN-noP2P
  - MATCH,RESERVE
'''


def legacy_text(doc):
    """The historical hand-written shape: two-space indented list, quoted scalars."""
    return LEGACY


def base_config():
    return {
        'mixed-port': 7890,
        'find-process-mode': 'off',
        'proxy-providers': {
            'sub2': {'type': 'http', 'url': SUB_OLD, 'interval': 3600,
                     'header': {'x-hwid': ['abc123']}},
            'slot': {'type': 'http', 'url': SUB_NEW, 'interval': 3600},
        },
        'proxy-groups': [
            {'name': 'VPN', 'type': 'url-test', 'use': ['sub2']},
            {'name': 'VPN-noP2P', 'type': 'url-test', 'use': ['sub2']},
            {'name': 'AI', 'type': 'url-test', 'use': ['slot']},
        ],
        'rules': ['RULE-SET,mtrickle,VPN-noP2P', 'MATCH,RESERVE'],
    }


class SlotPatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.yaml'

    def run_patcher(self, text, group='AI', url=SUB_OLD):
        self.path.write_text(text, encoding='utf-8')
        result = subprocess.run([sys.executable, '-B', str(PATCHER), str(self.path), group, url],
                                capture_output=True, text=True, timeout=60)
        body = self.path.read_text(encoding='utf-8')
        return result, body

    def group(self, body, name):
        doc = yaml.safe_load(body)
        return next(g for g in doc['proxy-groups'] if g['name'] == name)

    def test_reserialised_config_switches_the_ai_slot(self):
        text = yaml.safe_dump(base_config(), allow_unicode=True, sort_keys=False)
        self.assertIn('\n- name: AI', text)  # the shape that used to break every switch
        result, body = self.run_patcher(text, 'AI', SUB_OLD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('updated:slot', result.stdout)
        self.assertEqual(self.group(body, 'AI')['use'], ['slot'])
        doc = yaml.safe_load(body)
        self.assertEqual(doc['proxy-providers']['slot']['url'], SUB_OLD)

    def test_legacy_indented_config_still_switches(self):
        result, body = self.run_patcher(legacy_text(base_config()), 'AI', SUB_OLD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.group(body, 'AI')['use'], ['slot'])
        self.assertEqual(yaml.safe_load(body)['proxy-providers']['slot']['url'], SUB_OLD)

    def test_yaml_12_booleans_survive_the_round_trip(self):
        result, body = self.run_patcher(
            yaml.safe_dump(base_config(), sort_keys=False), 'AI', SUB_OLD)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(yaml.safe_load(body)['find-process-mode'], 'off')

    def test_shared_provider_is_isolated_instead_of_moved(self):
        doc = base_config()
        doc['proxy-groups'] = [
            {'name': 'VPN', 'type': 'url-test', 'use': ['sub2']},
            {'name': 'VPN-noP2P', 'type': 'url-test', 'use': ['sub2']},
            {'name': 'AI', 'type': 'url-test', 'use': ['sub2']},
        ]
        result, body = self.run_patcher(
            yaml.safe_dump(doc, sort_keys=False), 'AI', SUB_NEW)
        self.assertEqual(result.returncode, 0, result.stderr)
        # base_config already defines "slot", so the isolated provider takes the next name.
        self.assertTrue(result.stdout.startswith('created:slot2'))
        parsed = yaml.safe_load(body)
        self.assertEqual(self.group(body, 'AI')['use'], ['slot2'])
        self.assertEqual(self.group(body, 'VPN')['use'], ['sub2'])
        self.assertEqual(self.group(body, 'VPN-noP2P')['use'], ['sub2'])
        self.assertEqual(parsed['proxy-providers']['sub2']['url'], SUB_OLD)
        self.assertEqual(parsed['proxy-providers']['slot2']['header'], {'x-hwid': ['abc123']})

    def test_managed_pool_provider_is_never_rewritten(self):
        doc = base_config()
        doc['proxy-providers']['audit-ai-g3'] = {
            'type': 'file', 'path': '/etc/mihomo/geomask-pools/ai-g3.yaml'}
        doc['proxy-groups'] = [
            {'name': 'VPN', 'type': 'url-test', 'use': ['sub2']},
            {'name': 'VPN-noP2P', 'type': 'url-test', 'use': ['sub2']},
            {'name': 'AI', 'type': 'url-test', 'use': ['audit-ai-g3']},
        ]
        result, body = self.run_patcher(yaml.safe_dump(doc, sort_keys=False), 'AI', SUB_NEW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('created:'))
        parsed = yaml.safe_load(body)
        self.assertEqual(parsed['proxy-providers']['audit-ai-g3'],
                         {'type': 'file', 'path': '/etc/mihomo/geomask-pools/ai-g3.yaml'})
        self.assertEqual(self.group(body, 'AI')['use'], [result.stdout.strip().split(':', 1)[1]])

    def test_absent_group_is_created_after_the_ai_anchor(self):
        doc = base_config()
        doc['proxy-groups'] = [g for g in doc['proxy-groups'] if g['name'] != 'VPN']
        result, body = self.run_patcher(yaml.safe_dump(doc, sort_keys=False), 'VPN', SUB_NEW)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = yaml.safe_load(body)
        names = [g['name'] for g in parsed['proxy-groups']]
        self.assertEqual(names, ['VPN-noP2P', 'AI', 'VPN'])
        vpn = self.group(body, 'VPN')
        self.assertEqual(vpn['use'], [result.stdout.strip().split(':', 1)[1]])
        self.assertIn('RULE-SET,mtrickle_bulk,VPN', parsed['rules'])
        self.assertIn('MATCH,VPN', parsed['rules'])
        self.assertNotIn('MATCH,RESERVE', parsed['rules'])

    def test_group_with_inline_proxies_is_refused(self):
        doc = base_config()
        doc['proxy-groups'][2] = {'name': 'AI', 'type': 'select', 'proxies': ['a', 'b']}
        before = yaml.safe_dump(doc, sort_keys=False)
        result, body = self.run_patcher(before, 'AI', SUB_NEW)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('inline proxies', result.stderr)
        self.assertEqual(body, before, 'a refused switch must not touch the config')

    def test_missing_provider_is_refused_without_writing(self):
        doc = base_config()
        doc['proxy-groups'][2] = {'name': 'AI', 'type': 'url-test', 'use': ['gone']}
        before = yaml.safe_dump(doc, sort_keys=False)
        result, body = self.run_patcher(before, 'AI', SUB_NEW)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing', result.stderr)
        self.assertEqual(body, before)

    def test_sibling_groups_keep_their_sources(self):
        doc = base_config()
        result, body = self.run_patcher(yaml.safe_dump(doc, sort_keys=False), 'AI', SUB_OLD)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = yaml.safe_load(body)
        self.assertEqual(self.group(body, 'VPN')['use'], ['sub2'])
        self.assertEqual(self.group(body, 'VPN-noP2P')['use'], ['sub2'])
        self.assertEqual(parsed['proxy-providers']['sub2']['url'], SUB_OLD)


if __name__ == '__main__':
    unittest.main()
