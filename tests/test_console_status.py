"""Console lane: which subscription it borrows, and whether the panel reads its state.

Review 14-15.11.2026, points 10 and 12.

Point 10: with no URL of its own the console provider took the first provider named
`sub`/`slot`/`sub2`. On a split registry that is the restricted (VPN-noP2P) subscription,
while the console has to ride the subscription the VPN group actually uses.

Point 12: the panel never asked the router for the console state, so IP/countries/subscription
were empty after every reload, and the hint promised a switch-on that costs nothing -- `on`
always does `PUT /configs?force=true` and reloads mihomo.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'assets' / 'geomask-console.py'
PAGE = ROOT / 'assets' / 'geo-sync-page.js'
MANIFEST = ROOT / 'tools' / 'geo-rpc.json'
ACL = ROOT / 'assets' / 'acl-geo-sync.json'
RPC_PLUGIN = ROOT / 'assets' / 'geo-sync'
BACKEND = ROOT / 'assets' / 'geo-backend.sh'

# A split registry: `sub` is the restricted subscription (no P2P), `tolerant` is the one the
# VPN group rides. The console must borrow `tolerant`.
SPLIT = """\
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
proxy-groups:
  - name: VPN-noP2P
    type: url-test
    use:
      - sub
  - name: VPN
    type: url-test
    use:
      - tolerant
  - name: AI
    type: url-test
    use:
      - sub
rules:
  - MATCH,DIRECT
"""


def load_module():
    spec = importlib.util.spec_from_file_location('geomask_console_status', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DefaultSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.config = self.case / 'config.yaml'
        self.config.write_text(SPLIT, encoding='utf-8')
        self.module = load_module()

    def run_main(self, *args):
        return self.module.main(list(args))

    def doc(self):
        return yaml.safe_load(self.config.read_text(encoding='utf-8'))

    def test_default_subscription_follows_the_vpn_group(self):
        # No console provider yet and no --url: the URL must come from the group the VPN uses
        # (`VPN` first, then `VPN-noP2P`), not from whichever provider is named `sub`.
        code = self.run_main('apply', str(self.config), '--countries', 'TR')
        self.assertEqual(code, 0)
        provider = self.doc()['proxy-providers']['console']
        self.assertEqual(provider['url'], 'https://example.invalid/tolerant')
        # the header of the borrowed provider travels with its URL
        self.assertEqual(provider['header'], {'User-Agent': 'tolerant-agent'})
        # and that is exactly what the `--url default` reset resolves to as well
        self.assertEqual(self.module.default_provider_url(yaml.safe_load(SPLIT)),
                         'https://example.invalid/tolerant')


class PanelStateTests(unittest.TestCase):
    def block(self):
        """The console card source, so an unrelated card cannot satisfy the assertions."""
        text = PAGE.read_text(encoding='utf-8')
        start = text.index('Console lane')
        end = text.index('Per-device geo policy', start)
        return text[start:end]

    def test_the_panel_reads_console_status(self):
        block = self.block()
        self.assertIn('callConsoleStatus(', block)
        self.assertRegex(block, r'conIp\.value\s*=\s*[^;\n]*\.ip\b')
        self.assertRegex(block, r'conCc\.value\s*=\s*[^;\n]*\.countries\b')
        self.assertRegex(block, r'conSel\.value\s*=\s*[^;\n]*\.sub\b')
        # `on` always reloads mihomo: "старое включение мгновенное" was a lie.
        self.assertNotIn('мгновенное', PAGE.read_text(encoding='utf-8'))


class RpcManifestTests(unittest.TestCase):
    def test_console_status_is_a_read_only_method(self):
        manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
        names = [m['name'] for m in manifest['methods']]
        self.assertIn('console_status', names)
        method = manifest['methods'][names.index('console_status')]
        self.assertEqual(method['acl'], 'read')
        self.assertEqual(method['args'], [])
        self.assertEqual(method['js'], 'callConsoleStatus')
        # canonical order: right after `console`, before `console_diag`
        self.assertEqual(names.index('console_status'), names.index('console') + 1)
        self.assertLess(names.index('console_status'), names.index('console_diag'))

        acl = json.loads(ACL.read_text(encoding='utf-8'))['luci-app-geo-sync']
        self.assertIn('console_status', acl['read']['ubus']['geo-sync'])
        self.assertNotIn('console_status', acl['write']['ubus']['geo-sync'])

        plugin = RPC_PLUGIN.read_text(encoding='utf-8')
        self.assertIn('json_add_object "console_status"', plugin)
        self.assertIn('console_status)', plugin)

        backend = BACKEND.read_text(encoding='utf-8')
        self.assertIn('console_status)', backend)
        self.assertIn('cmd_console_status', backend)


if __name__ == '__main__':
    unittest.main()
