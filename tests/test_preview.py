#!/usr/bin/env python3
"""Tests for policy_preview.py"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

from policy_model import (
    PolicyManifest, PathPolicy, Subscription, AdmissionConfig,
    PathType, create_default_policy
)
from policy_preview import PreviewEngine, Node, COUNTRY_MAP


class PreviewEngineTests(unittest.TestCase):
    def setUp(self):
        self.policy = create_default_policy()
        self.temp_dir = tempfile.mkdtemp()
        self.policy_path = Path(self.temp_dir) / 'policies.yaml'
        self.policy.save(self.policy_path)
        self.engine = PreviewEngine(str(self.policy_path))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_node_country_extraction(self):
        # Test emoji extraction
        node = Node.from_clash_meta({'name': '🇺🇸 USA 1'})
        self.assertEqual(node.country, 'US')

        node = Node.from_clash_meta({'name': '🇳🇱 Netherlands'})
        self.assertEqual(node.country, 'NL')

        node = Node.from_clash_meta({'name': '🇩🇪 Germany 1'})
        self.assertEqual(node.country, 'DE')

    def test_node_matches_path_vpn(self):
        vpn_path = self.policy.get_path('vpn')
        node = Node(name='🇺🇸 USA 1', country='US', asn='', proxy_id='')
        result = node.matches_path(self.policy.get_path('vpn'))
        # US not in VPN allow list (NL,DE,FR,SE)
        self.assertFalse(result['ok'])

        node_nl = Node(name='🇳🇱 NL 1', country='NL', asn='', proxy_id='')
        result = node_nl.matches_path(self.policy.get_path('vpn'))
        self.assertTrue(result['ok'])

    def test_ai_path_requires_us(self):
        ai_path = self.policy.get_path('ai')
        node_us = Node(name='🇺🇸 USA 1', country='US', asn='', proxy_id='')
        result = node_us.matches_path(self.policy.get_path('ai'))
        self.assertTrue(result['ok'])

        node_nl = Node(name='🇳🇱 NL 1', country='NL', asn='', proxy_id='')
        result = node_nl.matches_path(self.policy.get_path('ai'))
        self.assertFalse(result['ok'])

    def test_vpn_nop2p_only_de_pl(self):
        vpn_nop2p = self.policy.get_path('vpn_nop2p')
        node_de = Node(name='🇩🇪 DE 1', country='DE', asn='', proxy_id='')
        result = node_de.matches_path(self.policy.get_path('vpn_nop2p'))
        self.assertTrue(result['ok'])

        node_fr = Node(name='🇫🇷 FR 1', country='FR', asn='', proxy_id='')
        result = node_fr.matches_path(self.policy.get_path('vpn_nop2p'))
        self.assertFalse(result['ok'])

    def test_preview_engine_runs(self):
        nodes = [
            {'name': '🇺🇸 USA 1', 'country': 'US'},
            {'name': '🇳🇱 NL 1', 'country': 'NL'},
            {'name': '🇩🇪 DE 1', 'country': 'DE'},
            {'name': '🇫🇷 FR 1', 'country': 'FR'},
            {'name': '🇯🇵 JP 1', 'country': 'JP'},
        ]
        result = self.engine.run_preview('vpn', nodes)
        self.assertEqual(result['path'], 'vpn')
        self.assertEqual(result['total_nodes'], 5)
        # VPN allows NL, DE, FR, SE
        self.assertEqual(result['admitted'], 3)  # NL, DE, FR
        self.assertEqual(len(result['rejected']['wrong_country']), 2)  # US, JP

    def test_ai_preview(self):
        nodes = [
            {'name': '🇺🇸 USA 1', 'country': 'US'},
            {'name': '🇩🇪 DE 1', 'country': 'DE'},
        ]
        result = self.engine.run_preview('ai', nodes)
        self.assertEqual(result['path'], 'ai')
        self.assertEqual(result['admitted'], 1)  # only US
        self.assertEqual(len(result['rejected']['wrong_country']), 1)  # DE


class CountryMapTests(unittest.TestCase):
    def test_emoji_to_code(self):
        self.assertEqual(COUNTRY_MAP['🇺🇸'], 'US')
        self.assertEqual(COUNTRY_MAP['🇳🇱'], 'NL')
        self.assertEqual(COUNTRY_MAP['🇩🇪'], 'DE')
        self.assertEqual(COUNTRY_MAP['🇫🇷'], 'FR')
        self.assertEqual(COUNTRY_MAP['🇸🇪'], 'SE')
        self.assertEqual(COUNTRY_MAP['🇵🇱'], 'PL')
        self.assertEqual(COUNTRY_MAP['🇬🇧'], 'GB')
        self.assertEqual(COUNTRY_MAP['🇹🇷'], 'TR')
        self.assertEqual(COUNTRY_MAP['🇷🇺'], 'RU')
        self.assertEqual(COUNTRY_MAP['🇧🇾'], 'BY')


if __name__ == '__main__':
    unittest.main()