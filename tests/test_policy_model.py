#!/usr/bin/env python3
"""Tests for policy_model.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))

from policy_model import (
    PolicyManifest, PathPolicy, Subscription, NamedFilter, FilterRule,
    AdmissionConfig, PathType, create_default_policy
)


class PolicyModelTests(unittest.TestCase):
    def test_create_default_policy(self):
        policy = create_default_policy()
        self.assertEqual(policy.version, 1)
        self.assertEqual(policy.schema, "geomask-policy-v1")
        self.assertEqual(len(policy.paths), 3)
        self.assertEqual(len(policy.subscriptions), 2)
        self.assertEqual(len(policy.filters), 2)

    def test_policy_validation_ok(self):
        policy = create_default_policy()
        errors = policy.validate()
        self.assertEqual(errors, [])

    def test_policy_validation_duplicate_path_id(self):
        policy = create_default_policy()
        # Add duplicate
        policy.paths.append(PathPolicy(
            id="vpn", name="VPN dup", path_type=PathType.VPN,
            interface="mihomo", subscription_id="sub",
            filter_id="base_filter"
        ))
        errors = policy.validate()
        self.assertIn("Duplicate path IDs", errors)

    def test_path_validation_bad_country(self):
        path = PathPolicy(
            id="test", name="Test", path_type=PathType.VPN,
            interface="mihomo", subscription_id="sub",
            filter_id="base_filter",
            countries_allow=["INVALID"]
        )
        errors = path.validate({}, {})
        self.assertTrue(any("invalid country code" in e for e in errors))

    def test_path_validation_bad_asn(self):
        path = PathPolicy(
            id="test", name="Test", path_type=PathType.VPN,
            interface="mihomo", subscription_id="sub",
            filter_id="base_filter",
            asns_allow=["ASINVALID"]
        )
        errors = path.validate({}, {})
        self.assertTrue(any("invalid ASN" in e for e in errors))

    def test_admission_required_no_services(self):
        path = PathPolicy(
            id="test", name="Test", path_type=PathType.AI,
            interface="mihomo", subscription_id="sub",
            filter_id="ai_filter",
            admission=AdmissionConfig(required=True, services=[])
        )
        errors = path.validate({}, {})
        self.assertTrue(any("admission required but no services" in e for e in errors))

    def test_serialization_roundtrip(self):
        policy = create_default_policy()
        data = policy.to_dict()
        restored = PolicyManifest.from_dict(data)
        self.assertEqual(restored.version, policy.version)
        self.assertEqual(len(restored.paths), len(policy.paths))
        self.assertEqual(len(restored.subscriptions), len(policy.subscriptions))
        self.assertEqual(len(restored.filters), len(policy.filters))

    def test_sha256_consistency(self):
        policy = create_default_policy()
        hash1 = policy.sha256()
        hash2 = policy.sha256()
        self.assertEqual(hash1, hash2)

    def test_save_load(self):
        policy = create_default_policy()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            temp_path = Path(f.name)
        try:
            policy.save(temp_path)
            loaded = PolicyManifest.load(temp_path)
            self.assertEqual(loaded.version, policy.version)
            self.assertEqual(len(loaded.paths), len(policy.paths))
        finally:
            temp_path.unlink()

    def test_default_policy_structure(self):
        policy = create_default_policy()
        path_ids = {p.id for p in policy.paths}
        self.assertIn('vpn', path_ids)
        self.assertIn('vpn_nop2p', path_ids)
        self.assertIn('ai', path_ids)

        # Check AI path has admission required
        ai_path = next(p for p in policy.paths if p.id == 'ai')
        self.assertTrue(ai_path.admission.required)
        self.assertIn('google', ai_path.admission.services)
        self.assertIn('chatgpt', ai_path.admission.services)
        self.assertIn('openrouter', ai_path.admission.services)

        # Check filters exist
        self.assertIn('base_filter', policy.filters)
        self.assertIn('ai_filter', policy.filters)

    def test_subscription_p2p_allowed_default(self):
        policy = create_default_policy()
        for sub in policy.subscriptions.values():
            self.assertFalse(sub.p2p_allowed)


if __name__ == '__main__':
    unittest.main()