#!/usr/bin/env python3
"""Tests for Stage 2: honest diagnostics, status enum, admission service."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / 'tools'))

from diag_status import CheckStatus, CheckResult, DiagnosticSummary
from admission_service import (
    AdmissionProof, NodeAdmission, AdmissionCache,
    probe_service, run_full_admission, MANDATORY_SERVICES
)

# For admission service, status is string
from diag_status import CheckStatus as DiagCheckStatus


class CheckStatusTests(unittest.TestCase):
    def test_status_ordering(self):
        self.assertTrue(CheckStatus.FAIL > CheckStatus.UNKNOWN)  # for sorting if needed
        self.assertTrue(CheckStatus.OK.is_positive())
        self.assertFalse(CheckStatus.UNKNOWN.is_positive())
        self.assertTrue(CheckStatus.UNKNOWN.is_negative())
        self.assertTrue(CheckStatus.FAIL.is_negative())
        self.assertFalse(CheckStatus.OK.is_negative())

    def test_check_result_factory(self):
        ok = CheckResult.ok('test', 'all good', http=200)
        self.assertEqual(ok.status, CheckStatus.OK)
        self.assertEqual(ok.evidence['http'], 200)

        fail = CheckResult.fail('test', 'bad', code=500)
        self.assertEqual(fail.status, CheckStatus.FAIL)

        unk = CheckResult.unknown('test', 'timeout')
        self.assertEqual(unk.status, CheckStatus.UNKNOWN)

    def test_diagnostic_summary_overall(self):
        # All OK -> OK
        summary = DiagnosticSummary(checks=[
            CheckResult.ok('a'), CheckResult.ok('b')
        ])
        self.assertEqual(summary.overall, CheckStatus.OK)

        # One FAIL -> FAIL
        summary = DiagnosticSummary(checks=[
            CheckResult.ok('a'), CheckResult.fail('b', 'bad')
        ])
        self.assertEqual(summary.overall, CheckStatus.FAIL)

        # One UNKNOWN -> UNKNOWN (no FAIL)
        summary = DiagnosticSummary(checks=[
            CheckResult.ok('a'), CheckResult.unknown('b', 'timeout')
        ])
        self.assertEqual(summary.overall, CheckStatus.UNKNOWN)

        # FAIL beats UNKNOWN
        summary = DiagnosticSummary(checks=[
            CheckResult.fail('a'), CheckResult.unknown('b')
        ])
        self.assertEqual(summary.overall, CheckStatus.FAIL)


class AdmissionProofTests(unittest.TestCase):
    def test_proof_validity(self):
        proof = AdmissionProof(
            service='google',
            endpoint='https://www.google.com/generate_204',
            method='http_proxy',
            proxy='socks5h://127.0.0.1:7891',
            status='ok',
            evidence={'http': 204, 'latency_ms': 100},
            ts='2026-11-07T12:00:00Z',
            ttl_seconds=300,
            probe_version=1
        )
        # Valid within TTL - use a time close to the proof timestamp
        from datetime import datetime
        proof_ts = datetime.fromisoformat('2026-11-07T12:00:00+00:00').timestamp()
        self.assertTrue(proof.is_valid(proof_ts + 100))  # 100s after proof
        # Expired
        self.assertFalse(proof.is_valid(proof_ts + 400))  # 400s after proof (> 300 TTL)

    def test_proof_serialization(self):
        proof = AdmissionProof(
            service='google', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='ok',
            evidence={'http': 204}, ts='2026-11-07T12:00:00Z',
            ttl_seconds=300, probe_version=1
        )
        d = proof.to_dict()
        self.assertEqual(d['service'], 'google')
        restored = AdmissionProof.from_dict(d)
        self.assertEqual(restored.service, 'google')


class NodeAdmissionTests(unittest.TestCase):
    def setUp(self):
        now_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        self.ok_proof = AdmissionProof(
            service='google', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='ok',
            evidence={'http': 204}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )
        self.fail_proof = AdmissionProof(
            service='chatgpt', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='fail',
            evidence={'http': 403}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )
        self.unknown_proof = AdmissionProof(
            service='openrouter', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='unknown',
            evidence={'error': 'timeout'}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )

    def test_all_ok_true(self):
        # Use current time to ensure proofs are valid
        now_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        google_proof = AdmissionProof(
            service='google', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='ok',
            evidence={'http': 204}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )
        chatgpt_proof = AdmissionProof(
            service='chatgpt', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='ok',
            evidence={'loc': 'US'}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )
        openrouter_proof = AdmissionProof(
            service='openrouter', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='ok',
            evidence={'http': 200}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )
        node = NodeAdmission(
            node_id='test1', node_name='Test Node',
            proofs=[google_proof, chatgpt_proof, openrouter_proof],
            ts=now_ts, probe_version=1
        )
        self.assertTrue(node.all_ok())

    def test_all_ok_false_on_fail(self):
        node = NodeAdmission(
            node_id='test1', node_name='Test Node',
            proofs=[self.ok_proof, self.fail_proof, self.ok_proof],
            ts='2026-11-07T12:00:00Z', probe_version=1
        )
        self.assertFalse(node.all_ok())

    def test_all_ok_false_on_unknown(self):
        node = NodeAdmission(
            node_id='test1', node_name='Test Node',
            proofs=[self.ok_proof, self.unknown_proof, self.ok_proof],
            ts='2026-11-07T12:00:00Z', probe_version=1
        )
        self.assertFalse(node.all_ok())

    def test_all_ok_false_on_expired(self):
        expired_proof = AdmissionProof(
            service='chatgpt', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='ok',
            evidence={}, ts='2020-01-01T00:00:00Z',  # expired
            ttl_seconds=300, probe_version=1
        )
        node = NodeAdmission(
            node_id='test1', node_name='Test Node',
            proofs=[self.ok_proof, expired_proof, self.ok_proof],
            ts='2026-11-07T12:00:00Z', probe_version=1
        )
        self.assertFalse(node.all_ok())


class AdmissionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cache_path = Path(self.temp_dir) / 'admission-cache.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cache_save_load(self):
        cache = AdmissionCache(self.cache_path)
        now_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        node = NodeAdmission(
            node_id='n1', node_name='Test',
            proofs=[], ts=now_ts, probe_version=1
        )
        cache.set(node)
        self.assertTrue(self.cache_path.exists())

        # Load new cache instance
        cache2 = AdmissionCache(self.cache_path)
        self.assertIn('n1', cache2.data)
        self.assertEqual(cache2.data['n1'].node_name, 'Test')

    def test_cache_invalidate(self):
        cache = AdmissionCache(self.cache_path)
        now_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        node = NodeAdmission(
            node_id='n1', node_name='Test',
            proofs=[], ts=now_ts, probe_version=1
        )
        cache.set(node)
        cache.invalidate('n1')
        self.assertNotIn('n1', cache.data)

    def test_get_admitted_nodes(self):
        cache = AdmissionCache(self.cache_path)
        now_ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        google_ok = AdmissionProof(
            service='google', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='ok',
            evidence={'http': 204}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )
        chatgpt_ok = AdmissionProof(
            service='chatgpt', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='ok',
            evidence={'loc': 'US'}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )
        openrouter_ok = AdmissionProof(
            service='openrouter', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='ok',
            evidence={'http': 200}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )
        fail_proof = AdmissionProof(
            service='chatgpt', endpoint='url', method='http_proxy',
            proxy='socks5h://127.0.0.1:7891', status='fail',
            evidence={'http': 403}, ts=now_ts,
            ttl_seconds=300, probe_version=1
        )

        admitted = NodeAdmission('a', 'A',
            [google_ok, chatgpt_ok, openrouter_ok], now_ts, 1)
        rejected = NodeAdmission('b', 'B',
            [google_ok, fail_proof, openrouter_ok], now_ts, 1)
        cache.set(admitted)
        cache.set(rejected)

        admitted_nodes = cache.get_admitted_nodes()
        self.assertEqual(len(admitted_nodes), 1)
        self.assertEqual(admitted_nodes[0].node_id, 'a')


class ProbeServiceTests(unittest.TestCase):
    @patch('admission_service.urllib.request.build_opener')
    def test_probe_success_204(self, mock_opener):
        mock_resp = Mock()
        mock_resp.read.return_value = b''
        mock_resp.getcode.return_value = 204
        mock_opener.return_value.open.return_value.__enter__.return_value = mock_resp

        service = {'name': 'google', 'url': 'https://www.google.com/generate_204', 'expect_http': 204}
        proof = probe_service(service, 'socks5h://127.0.0.1:7891')

        self.assertEqual(proof.status, 'ok')
        self.assertEqual(proof.evidence['http'], 204)
        self.assertIn('latency_ms', proof.evidence)

    @patch('admission_service.urllib.request.build_opener')
    def test_probe_chatgpt_loc_us(self, mock_opener):
        mock_resp = Mock()
        mock_resp.read.return_value = b'loc=US\ncolo=EWR\n'
        mock_resp.getcode.return_value = 200
        mock_opener.return_value.open.return_value.__enter__.return_value = mock_resp

        service = {'name': 'chatgpt', 'url': 'https://chatgpt.com/cdn-cgi/trace', 'expect_field': 'loc=US'}
        proof = probe_service(service, 'socks5h://127.0.0.1:7891')

        self.assertEqual(proof.status, 'ok')
        self.assertEqual(proof.evidence.get('loc'), 'US')
        self.assertEqual(proof.evidence.get('colo'), 'EWR')

    @patch('admission_service.urllib.request.build_opener')
    def test_probe_timeout(self, mock_opener):
        import urllib.error
        mock_opener.return_value.open.side_effect = TimeoutError('timeout')
        service = {'name': 'google', 'url': 'https://www.google.com/generate_204', 'expect_http': 204}
        proof = probe_service(service, 'socks5h://127.0.0.1:7891')
        self.assertEqual(proof.status, 'unknown')
        self.assertIn('error', proof.evidence)

    @patch('admission_service.urllib.request.build_opener')
    def test_probe_http_error(self, mock_opener):
        import urllib.error
        mock_resp = Mock()
        mock_resp.code = 403
        mock_resp.read.return_value = b'forbidden'
        mock_opener.return_value.open.side_effect = urllib.error.HTTPError('url', 403, 'Forbidden', {}, None)
        service = {'name': 'google', 'url': 'https://www.google.com/generate_204', 'expect_http': 204}
        proof = probe_service(service, 'socks5h://127.0.0.1:7891')
        self.assertEqual(proof.status, 'fail')
        self.assertEqual(proof.evidence['http'], 403)


if __name__ == '__main__':
    unittest.main()