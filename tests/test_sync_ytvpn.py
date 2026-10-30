"""Sync script: ytvpn.yaml must be writable to an empty list (v1.14.1).

v1.13.3 added "only write when the group produced something" to survive transient parse
failures, but that also froze the legitimate "auto mode, nothing broken" state: after the
watchdog removed the blanket rule the file kept pointing every googlevideo edge at the VPN.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'assets' / 'sync_mtrickle_mihomo.sh').read_text(encoding='utf-8')


class SyncYtvpnTests(unittest.TestCase):
    def test_group_presence_drives_write(self):
        body = SRC.split("<<'PYEOF'\n", 1)[1].rsplit('\nPYEOF', 1)[0]
        # the group must be tracked as present, and presence (not payload) drives the write
        self.assertIn('yt_group_found = False', body)
        self.assertIn('yt_group_found = True', body)
        self.assertRegex(body, r'if yt_group_found or ytvpn_payload:')

    def test_empty_payload_is_valid_yaml(self):
        body = SRC.split("<<'PYEOF'\n", 1)[1].rsplit('\nPYEOF', 1)[0]
        self.assertRegex(body, r"payload: \[\]\\n' if not payload")

    def test_mtrickle_and_ai_still_guarded(self):
        # the original protection (never blank the main rulesets) must stay
        self.assertRegex(SRC, r'if not mt_payload or not ai_payload')


if __name__ == '__main__':
    unittest.main()
