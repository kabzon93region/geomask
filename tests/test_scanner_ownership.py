"""The nightly scanner must not touch a group owned by the node-audit coordinator.

Found live: the router runs the scanner with `sub_provider=all`, so once an apply creates
`audit-vpn-gN`, the scanner enumerates it and writes an exclude-filter into the very group
that was just pinned and verified.
"""
import os
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parents[1] / 'assets'
SCANNER = HERE / 'geomask-node-scan.sh'

CONFIG = """\
proxy-providers:
  sub:
    type: http
    url: "https://example.com/sub"
  audit-vpn-g6:
    type: file
    path: /etc/mihomo/geomask-pools/vpn-g6.yaml
proxy-groups:
  - name: VPN
    type: url-test
    use:
      - audit-vpn-g6
  - name: VPN-noP2P
    type: url-test
    use:
      - sub
"""


def helpers():
    """Load only the definitions under test from the embedded program.

    Executing the whole program would run its top-level config read and network work, so
    the needed functions are sliced out by indentation instead.
    """
    text = SCANNER.read_text(encoding='utf-8')

    def function(name):
        lines = text.splitlines(keepends=True)
        start = next(i for i, line in enumerate(lines) if line.startswith('def %s(' % name))
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() and not line[:1].isspace():
                break
            end += 1
        return ''.join(lines[start:end])

    constants = "MANAGED_PREFIX='audit-'\nimport re\n"
    body = constants + ''.join(function(n) for n in
                               ('group_blocks', 'provider_of_block', 'apply_filter_all'))
    namespace = {'os': os, '__name__': 'scanner_helpers'}
    exec(compile(body, '<scanner-helpers>', 'exec'), namespace)
    return namespace, text


class ManagedGroupGuardTests(unittest.TestCase):
    def test_scanner_refuses_a_managed_provider(self):
        ns, _ = helpers()
        out, changed, matched, touched = ns['apply_filter_all'](
            CONFIG, 'Россия|Финляндия', 'audit-vpn-g6', ['VPN', 'VPN-noP2P'])
        self.assertFalse(changed)
        self.assertEqual(matched, 0)
        self.assertEqual(touched, 0)
        self.assertEqual(out, CONFIG)

    def test_scanner_still_edits_its_own_provider(self):
        ns, _ = helpers()
        out, changed, matched, touched = ns['apply_filter_all'](
            CONFIG, 'Россия', 'sub', ['VPN', 'VPN-noP2P'])
        self.assertTrue(changed)
        self.assertEqual(matched, 1)
        self.assertIn("exclude-filter: 'Россия'", out)
        # The managed group is untouched even when another provider is being scanned.
        vpn_block = out.split('  - name: VPN\n')[1].split('  - name: VPN-noP2P')[0]
        self.assertNotIn('exclude-filter', vpn_block)

    def test_enumeration_skips_managed_providers(self):
        _, text = helpers()
        self.assertIn('not p.startswith(MANAGED_PREFIX)', text)

    def test_prefix_is_defined_once(self):
        _, text = helpers()
        self.assertEqual(text.count("MANAGED_PREFIX='audit-'"), 1)


if __name__ == '__main__':
    unittest.main()
