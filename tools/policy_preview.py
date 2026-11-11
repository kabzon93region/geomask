#!/usr/bin/env python3
"""Policy preview: shows admitted/rejected nodes for a path based on current policies."""
import json
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional

sys.path.insert(0, str(Path(__file__).parent))
from policy_model import (
    PolicyManifest, PathPolicy, Subscription, AdmissionConfig,
    PathType
)


class Node:
    """Represents a proxy node from a subscription."""
    def __init__(self, name: str, country: str, asn: str = '', proxy_id: str = ''):
        self.name = name
        self.country = country.upper() if country else ''
        self.asn = asn.upper() if asn else ''
        self.proxy_id = proxy_id

    @classmethod
    def from_clash_meta(cls, proxy: dict) -> 'Node':
        name = proxy.get('name', '')
        # Extract country from name (emoji or text)
        country = ''
        for k, v in COUNTRY_MAP.items():
            if k in name:
                return cls(name=name, country=v)
        # Try to extract from proxy fields
        if 'country' in proxy:
            return cls(name=name, country=proxy['country'])
        return cls(name=name, country='')

    def matches_path(self, path_policy) -> dict:
        """Check if node matches path policy. Returns dict with result details."""
        reasons = []
        ok = True

        # Country allow/deny
        if path_policy.countries_allow:
            if self.country not in path_policy.countries_allow:
                return {'ok': False, 'reason': f'country {self.country} not in allow list'}
        if path_policy.countries_deny:
            if self.country in path_policy.countries_deny:
                return {'ok': False, 'reason': f'country {self.country} in deny list'}

        # ASN allow/deny (if we had ASN info)
        # TODO: implement ASN matching when we have ASN data

        return {'ok': True, 'reason': 'passed geo filter'}


# Emoji to country code mapping
COUNTRY_MAP = {
    '🇳🇱': 'NL', '🇩🇪': 'DE', '🇫🇷': 'FR', '🇸🇪': 'SE',
    '🇵🇱': 'PL', '🇺🇸': 'US', '🇬🇧': 'GB', '🇹🇷': 'TR',
    '🇷🇺': 'RU', '🇧🇾': 'BY', '🇨🇳': 'CN', '🇮🇷': 'IR',
    '🇯🇵': 'JP', '🇭🇰': 'HK', '🇸🇬': 'SG', '🇨🇭': 'CH',
    '🇦🇹': 'AT', '🇧🇪': 'BE', '🇨🇿': 'CZ', '🇩🇰': 'DK',
    '🇫🇮': 'FI', '🇮🇪': 'IE', '🇮🇹': 'IT', '🇱🇺': 'LU',
    '🇳🇴': 'NO', '🇦🇹': 'AT', '🇵🇹': 'PT', '🇷🇴': 'RO',
    '🇸🇰': 'SK', '🇸🇮': 'SI', '🇪🇸': 'ES', '🇨🇭': 'CH',
}


class PreviewEngine:
    """Generates preview for a path based on current policies and live nodes."""

    def __init__(self, policy_path: str = '/etc/geomask/policies.yaml'):
        self.policy = PolicyManifest.load(Path(policy_path))
        self.nodes_cache: Dict[str, List[dict]] = {}

    def get_subscription_nodes(self, sub_id: str) -> List[dict]:
        """Fetch nodes from subscription. In production, this would call mihomo API."""
        # For local preview, return mock data or read from cached mihomo state
        # In production: curl mihomo API /proxies/{group}
        return []

    def run_preview(self, path_id: str, nodes: List[dict] = None) -> dict:
        """Generate preview for a path."""
        path_policy = self.policy.get_path(path_id)
        if not path_policy:
            return {'error': f'Path {path_id} not found'}

        sub = self.policy.get_subscription(path_policy.subscription_id)
        if not sub:
            return {'error': f'Subscription {path_policy.subscription_id} not found'}

        # Get nodes (mock for local, real would fetch from mihomo)
        if nodes is None:
            nodes = self._get_mock_nodes()

        admitted = []
        rejected = {
            'wrong_country': [],
            'admission_fail': [],
            'wrong_asn': [],
        }

        for node_data in nodes:
            node = Node.from_clash_meta(node_data)
            geo_result = node.matches_path(self.policy.get_path(path_id))
            if not geo_result['ok']:
                rejected['wrong_country'].append({
                    'name': node.name,
                    'country': node.country,
                    'reason': geo_result['reason'],
                })
                continue

            # For AI path, check admission
            if path_policy.admission.required:
                # In real preview, would check admission cache
                # For now, assume all pass
                pass

            admitted.append({
                'name': node_data.get('name', ''),
                'country': node_data.get('country', ''),
            })

        return {
            'path': path_policy.id,
            'subscription': path_policy.subscription_id,
            'total_nodes': len(nodes),
            'admitted': len(admitted),
            'admitted_details': admitted,
            'rejected': rejected,
            'missing_countries': self._missing_countries(nodes),
            'unknown_results': 0,
            'preview_only': True,
        }

    def _missing_countries(self, nodes: List[dict]) -> List[str]:
        """Countries from allow list that have no nodes."""
        present = {self._extract_country(n) for n in nodes}
        # This would need the path policy's allow list
        return []

    def _extract_country(self, node_data: dict) -> str:
        name = node_data.get('name', '')
        for k, v in COUNTRY_MAP.items():
            if k in name:
                return v
        return ''


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Policy preview for geomask paths')
    parser.add_argument('path_id', help='Path ID (vpn, vpn_nop2p, ai, etc.)')
    parser.add_argument('--policy', default='/etc/geomask/policies.yaml', help='Policy file path')
    parser.add_argument('--nodes-json', help='JSON file with nodes for preview')
    parser.add_argument('--json', action='store_true', help='Output JSON')
    args = parser.parse_args()

    if args.nodes_json:
        with open(args.nodes_json) as f:
            nodes = json.load(f)
    else:
        nodes = None

    engine = PreviewEngine(args.policy)
    result = engine.run_preview(args.path_id, nodes)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Path: {result.get('path', 'unknown')}")
        print(f"Subscription: {result.get('subscription', 'unknown')}")
        print(f"Total nodes: {result.get('total_nodes', 0)}")
        print(f"Admitted: {result.get('admitted', 0)}")
        print(f"Rejected: {sum(len(v) for v in result.get('rejected', {}).values())}")
        for cat, items in result.get('rejected', {}).items():
            if items:
                print(f"  {cat}: {len(items)}")
        print(f"Missing countries: {result.get('missing_countries', [])}")


if __name__ == '__main__':
    main()