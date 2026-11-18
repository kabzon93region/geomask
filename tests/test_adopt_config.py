"""I05: adopting a foreign mihomo config is a real, validated step - or an honest refusal.

The audit found that `--adopt-config` passed the guard and then did nothing: the in-place
patch only rewrites geomask-shaped files, so the run "succeeded" without adopting and the
next run refused the same file again. These tests drive the merge tool directly: a merged
candidate is produced in a separate file, the owner's lines stay byte-identical, and an
ambiguous config is refused before anything is written.
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / 'assets' / 'geomask-mihomo-adopt.py'
VPN_URL = 'https://vpn.example/sub?token=SENTINEL'

FOREIGN = """\
mixed-port: 7890
secret: "owner-secret-SENTINEL"
mode: rule
tun:
  enable: true
  stack: system
  device: utun-owner
dns:
  enable: true
  nameserver:
    - 1.1.1.1
listeners:
  - name: ai-devices
    type: mixed
    port: 7891
    listen: 127.0.0.1
    proxy: AI
  - name: torrent-direct
    type: mixed
    port: 7894
    listen: 192.168.1.1
    proxy: DIRECT
proxies:
  - name: owner-node
    type: ss
    server: 203.0.113.10
    port: 8388
    cipher: aes-128-gcm
    password: "sentinel"
proxy-groups:
  - name: owner-group
    type: select
    proxies:
      - owner-node
rules:
  - DOMAIN-SUFFIX,example.com,owner-group
  - MATCH,owner-group
"""


class AdoptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='gm-adopt-'))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text, encoding='utf-8')
        return path

    def run_tool(self, src, *extra, dst_name='merged.yaml'):
        dst = self.tmp / dst_name
        if dst.exists():
            dst.unlink()
        result = subprocess.run(
            [sys.executable, str(TOOL), str(src), str(dst),
             '--vpn-url', VPN_URL, '--lan-ip', '192.168.1.1'] + list(extra),
            capture_output=True, text=True, encoding='utf-8')
        return result, dst

    def test_a_plain_foreign_config_is_adopted_and_keeps_the_owner_lines(self):
        src = self.write('config.yaml', FOREIGN)
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        merged = dst.read_text(encoding='utf-8')
        for line in FOREIGN.splitlines():
            self.assertIn(line, merged, 'the owner line was rewritten: %r' % line)
        data = yaml.safe_load(merged)
        self.assertEqual(data['secret'], 'owner-secret-SENTINEL')
        self.assertEqual(data['tun']['device'], 'utun-owner')
        self.assertEqual(data['rules'], yaml.safe_load(FOREIGN)['rules'])
        self.assertEqual(data['proxy-providers']['sub']['url'], VPN_URL)
        names = [group['name'] for group in data['proxy-groups']]
        self.assertEqual(names[:1], ['owner-group'])
        self.assertIn('VPN-noP2P', names)
        self.assertIn('AI', names)
        self.assertIn('# geomask-managed: 1', merged.splitlines())

    def test_the_merged_candidate_satisfies_the_shaped_contract(self):
        src = self.write('config.yaml', FOREIGN)
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = yaml.safe_load(dst.read_text(encoding='utf-8'))
        providers = data.get('proxy-providers')
        groups = data.get('proxy-groups')
        self.assertIsInstance(providers, dict)
        self.assertIn('sub', providers)
        names = {group.get('name') for group in groups if isinstance(group, dict)}
        self.assertTrue({'VPN-noP2P', 'AI'} <= names)

    def test_a_config_without_proxies_is_refused(self):
        src = self.write('config.yaml', 'mixed-port: 7890\nrules:\n  - MATCH,DIRECT\n')
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 3)
        self.assertIn('nothing to route', result.stderr)
        self.assertFalse(dst.exists(), 'a refused adoption wrote a candidate')

    def test_a_group_name_clash_is_refused(self):
        text = FOREIGN.replace('  - name: owner-group', '  - name: VPN-noP2P')
        src = self.write('config.yaml', text)
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 3)
        self.assertIn('VPN-noP2P', result.stderr)
        self.assertFalse(dst.exists())

    def test_an_existing_provider_sub_is_refused(self):
        text = ('mixed-port: 7890\n'
                'proxy-providers:\n'
                '  sub:\n'
                '    type: http\n'
                '    url: "https://other/sub"\n'
                'proxies:\n'
                '  - name: owner-node\n'
                '    type: ss\n'
                '    server: 203.0.113.10\n'
                '    port: 8388\n'
                '    cipher: aes-128-gcm\n'
                '    password: "sentinel"\n'
                'proxy-groups:\n'
                '  - name: owner-group\n'
                '    type: select\n'
                '    proxies:\n'
                '      - owner-node\n'
                'rules:\n'
                '  - MATCH,owner-group\n')
        src = self.write('config.yaml', text)
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 3)
        self.assertIn('already exists with another meaning', result.stderr)
        self.assertFalse(dst.exists())

    def test_a_listener_with_another_policy_is_refused(self):
        text = FOREIGN.replace('    proxy: AI\n', '    proxy: DIRECT\n', 1)
        src = self.write('config.yaml', text)
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 3)
        self.assertIn('ai-devices', result.stderr)
        self.assertFalse(dst.exists())

    def test_a_missing_listener_is_added_next_to_the_owner_ones(self):
        text = ('mixed-port: 7890\n'
                'listeners:\n'
                '  - name: ai-devices\n'
                '    type: mixed\n'
                '    port: 7891\n'
                '    listen: 127.0.0.1\n'
                '    proxy: AI\n'
                'proxies:\n'
                '  - name: owner-node\n'
                '    type: ss\n'
                '    server: 203.0.113.10\n'
                '    port: 8388\n'
                '    cipher: aes-128-gcm\n'
                '    password: "sentinel"\n'
                'rules:\n'
                '  - MATCH,DIRECT\n')
        src = self.write('config.yaml', text)
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = yaml.safe_load(dst.read_text(encoding='utf-8'))
        names = [item['name'] for item in data['listeners']]
        self.assertEqual(names, ['ai-devices', 'torrent-direct'])
        self.assertEqual(data['listeners'][1]['listen'], '192.168.1.1')

    def test_a_config_without_listeners_gets_the_block(self):
        text = ('mixed-port: 7890\n'
                'proxies:\n'
                '  - name: owner-node\n'
                '    type: ss\n'
                '    server: 203.0.113.10\n'
                '    port: 8388\n'
                '    cipher: aes-128-gcm\n'
                '    password: "sentinel"\n'
                'rules:\n'
                '  - MATCH,DIRECT\n')
        src = self.write('config.yaml', text)
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = yaml.safe_load(dst.read_text(encoding='utf-8'))
        self.assertEqual({item['name'] for item in data['listeners']},
                         {'ai-devices', 'torrent-direct'})

    def test_broken_yaml_is_refused(self):
        src = self.write('config.yaml', 'a: [1, 2\n')
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 3)
        self.assertIn('not valid YAML', result.stderr)
        self.assertFalse(dst.exists())

    def test_a_non_mapping_config_is_refused(self):
        src = self.write('config.yaml', '- just\n- a list\n')
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 3)
        self.assertIn('not a YAML mapping', result.stderr)
        self.assertFalse(dst.exists())

    def test_an_empty_config_is_refused(self):
        src = self.write('config.yaml', '\n\n')
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 3)
        self.assertIn('empty', result.stderr)
        self.assertFalse(dst.exists())

    def test_the_output_cannot_be_adopted_twice(self):
        src = self.write('config.yaml', FOREIGN)
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        second, second_dst = self.run_tool(dst, dst_name='twice.yaml')
        self.assertEqual(second.returncode, 3,
                         'a second adoption would duplicate the managed blocks')
        self.assertIn('--replace-config', second.stderr)
        self.assertFalse(second_dst.exists())

    def test_a_provider_that_is_not_a_mapping_is_refused(self):
        text = FOREIGN.replace('proxies:\n', 'proxy-providers: []\nproxies:\n')
        src = self.write('config.yaml', text)
        result, dst = self.run_tool(src)
        self.assertEqual(result.returncode, 3)
        self.assertIn('not a mapping', result.stderr)
        self.assertFalse(dst.exists())


if __name__ == '__main__':
    unittest.main()
