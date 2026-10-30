"""Regression tests for the node-scan subscription parser (v1.13.6).

The provider switched from base64/share-link payloads to a full YAML config where
`server:` is several lines below `name:`. The old regex matched almost nothing and the
scan silently rewrote exclude-filter with no exact-name bans.
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'assets' / 'geomask-node-scan.sh').read_text(encoding='utf-8')


def load_func(name):
    body = SRC.split("<<'PYEOF'\n", 1)[1].rsplit('\nPYEOF', 1)[0]
    tree = ast.parse(body)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {'re': re}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<f>', 'exec'), ns)
    return ns[name]


class NodeScanParserTests(unittest.TestCase):
    def setUp(self):
        self.parse = load_func('parse_yaml_items')

    def test_yaml_with_fields_between(self):
        sub = """proxies:
  - name: "\U0001F1F3\U0001F1F1 Netherlands"
    type: vless
    server: nl1.example.com
    port: 443
    uuid: abc
  - name: "\U0001F1F1\U0001F1FB Latvia"
    type: hysteria2
    server: lv2.example.com
    port: 443
"""
        got = self.parse(sub)
        self.assertEqual(len(got), 2)
        self.assertIn('Latvia', got[1][0])
        self.assertEqual(got[0][1], 'nl1.example.com')

    def test_flow_style(self):
        sub = """proxies:
  - {name: USA 1, type: vless, server: us.example.com, port: 443}
  - {name: 'UK fake', server: ae.example.com}
"""
        got = self.parse(sub)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1], ('UK fake', 'ae.example.com'))

    def test_groups_without_server_are_skipped(self):
        sub = """proxy-groups:
  - name: VPN
    type: url-test
    proxies:
      - a
proxies:
  - name: real
    type: ss
    server: h.example.com
"""
        got = self.parse(sub)
        self.assertEqual(got, [('real', 'h.example.com')])

    def test_safety_guard_exists(self):
        # a parse regression must refuse to rewrite the filter instead of wiping bans
        self.assertIn('MIN_NODES', SRC)
        self.assertRegex(SRC, r'refusing to rewrite filter')

    def test_subscription_client_identified(self):
        # v1.13.7: the panel serves a 1-node stub to unknown User-Agents -> scan must
        # send a known app UA (configurable) and retry.
        self.assertRegex(SRC, r"sub_user_agent','clash-meta'")
        self.assertRegex(SRC, r"h\['User-Agent'\]=sub_ua")
        self.assertRegex(SRC, r'for _try in range\(3\)')
        conf = (ROOT / 'assets' / 'geomask-nodes.conf').read_text(encoding='utf-8')
        self.assertIn('sub_user_agent=clash-meta', conf)

    def test_controller_reload_is_authenticated(self):
        # v1.13.5 fixed auth in installer/backend/sync; the scan had the same 401 bug
        self.assertRegex(SRC, r"add_header\('Authorization', 'Bearer ")

    def test_geo_policy_covers_found_leaks(self):
        conf = (ROOT / 'assets' / 'geomask-nodes.conf').read_text(encoding='utf-8')
        self.assertIn('TR', conf.split('bad_countries=')[1].split('\n')[0])
        self.assertIn('AS207847', conf.split('bad_asns=')[1].split('\n')[0])
        base = conf.split('base_filter=')[1].split('\n')[0]
        for needle in ('Turkey', 'United Kingdom', '\U0001F1F7\U0001F1FA'):
            self.assertIn(needle, base)


if __name__ == '__main__':
    unittest.main()
