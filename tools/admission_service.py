#!/usr/bin/env python3
"""Admission proof service: cache, TTL, invalidation for AI node admission."""
import hashlib
import json
import os
import time
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
import ssl

CACHE_PATH = Path('/tmp/geomask-admission-cache.json')
DEFAULT_TTL = 300  # seconds
MANDATORY_SERVICES = [
    {'name': 'google', 'url': 'https://www.google.com/generate_204', 'expect_http': 204},
    {'name': 'chatgpt', 'url': 'https://chatgpt.com/cdn-cgi/trace', 'expect_field': 'loc=US'},
    {'name': 'openrouter', 'url': 'https://openrouter.ai/', 'expect_http': 200},
]


@dataclass
class AdmissionProof:
    """Proof of admission for a single service."""
    service: str
    endpoint: str
    method: str                    # "http_proxy"
    proxy: str                     # "socks5h://127.0.0.1:7891"
    status: str                    # "ok" | "fail" | "unknown"
    evidence: Dict[str, Any]
    ts: str                        # RFC3339
    ttl_seconds: int
    probe_version: int             # для инвалидации кэша

    def is_valid(self, now: float = None) -> bool:
        now = now or time.time()
        try:
            proof_ts = datetime.fromisoformat(self.ts.replace('Z', '+00:00')).timestamp()
            return (now - proof_ts) < self.ttl_seconds
        except Exception:
            return False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'AdmissionProof':
        return cls(**d)


@dataclass
class NodeAdmission:
    """Complete admission state for one node."""
    node_id: str                   # mihomo proxy name
    node_name: str                 # display name
    proofs: List[AdmissionProof]
    ts: str                        # RFC3339 last update
    probe_version: int             # инкрементируется при каждом полном цикле

    def all_ok(self) -> bool:
        """AND of all mandatory services."""
        mandatory_names = {s['name'] for s in MANDATORY_SERVICES}
        for name in mandatory_names:
            proof = next((p for p in self.proofs if p.service == name), None)
            if not proof or proof.status != 'ok' or not proof.is_valid():
                return False
        return True

    def get_proof(self, service: str) -> Optional[AdmissionProof]:
        return next((p for p in self.proofs if p.service == service), None)

    def to_dict(self) -> dict:
        return {
            'node_id': self.node_id,
            'node_name': self.node_name,
            'proofs': [p.to_dict() for p in self.proofs],
            'ts': self.ts,
            'probe_version': self.probe_version,
            'admitted': self.all_ok()
        }


class AdmissionCache:
    """Persistent cache of node admissions."""

    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self.data: Dict[str, NodeAdmission] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding='utf-8'))
                for node_id, node_data in raw.items():
                    self.data[node_id] = NodeAdmission(
                        node_id=node_data['node_id'],
                        node_name=node_data['node_name'],
                        proofs=[AdmissionProof.from_dict(p) for p in node_data['proofs']],
                        ts=node_data['ts'],
                        probe_version=node_data['probe_version']
                    )
            except Exception:
                self.data = {}

    def save(self):
        out = {nid: node.to_dict() for nid, node in self.data.items()}
        # atomic write
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def get(self, node_id: str) -> Optional[NodeAdmission]:
        return self.data.get(node_id)

    def set(self, admission: NodeAdmission):
        self.data[admission.node_id] = admission
        self.save()

    def invalidate(self, node_id: str = None):
        if node_id:
            self.data.pop(node_id, None)
        else:
            self.data.clear()
        self.save()

    def get_admitted_nodes(self) -> List[NodeAdmission]:
        return [n for n in self.data.values() if n.all_ok()]


def probe_service(service: dict, proxy: str, timeout: int = 10) -> AdmissionProof:
    """Execute a single service probe via SOCKS5h proxy."""
    url = service['url']
    method = 'http_proxy'
    evidence = {}
    status = 'unknown'

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({'http': proxy, 'https': proxy}),
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context())
    )

    start = time.time()
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'curl/8'})
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(4000).decode('utf-8', 'replace')
            http_code = resp.getcode()
            latency_ms = int((time.time() - start) * 1000)
            evidence = {'http': http_code, 'latency_ms': latency_ms}

            if 'expect_http' in service:
                if http_code == service['expect_http']:
                    status = 'ok'
                else:
                    status = 'fail'
                    evidence['expected_http'] = service['expect_http']
            elif 'expect_field' in service:
                # parse chatgpt trace: loc=US
                expected = service['expect_field']
                if expected in body:
                    status = 'ok'
                    # extract loc/colo
                    for line in body.splitlines():
                        if line.startswith('loc='):
                            evidence['loc'] = line.split('=', 1)[1]
                        elif line.startswith('colo='):
                            evidence['colo'] = line.split('=', 1)[1]
                else:
                    status = 'fail'
                    evidence['body_sample'] = body[:200]
            else:
                if 200 <= http_code < 300:
                    status = 'ok'
                else:
                    status = 'fail'

    except urllib.error.HTTPError as e:
        status = 'fail'
        evidence = {'http': e.code, 'error': str(e)}
    except Exception as e:
        status = 'unknown'
        evidence = {'error': str(e), 'type': type(e).__name__}

    return AdmissionProof(
        service=service['name'],
        endpoint=url,
        method=method,
        proxy=proxy,
        status=status,
        evidence=evidence,
        ts=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        ttl_seconds=DEFAULT_TTL,
        probe_version=0  # будет установлен при сохранении
    )


def run_full_admission(node_id: str, node_name: str, proxy: str, cache: AdmissionCache) -> NodeAdmission:
    """Run full admission cycle for a node."""
    proofs = []
    for svc in MANDATORY_SERVICES:
        proof = probe_service(svc, proxy)
        proofs.append(proof)

    # Get current probe_version
    existing = cache.get(node_id)
    probe_version = (existing.probe_version + 1) if existing else 1

    admission = NodeAdmission(
        node_id=node_id,
        node_name=node_name,
        proofs=proofs,
        ts=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        probe_version=probe_version
    )
    # Update probe_version in proofs
    for p in admission.proofs:
        p.probe_version = probe_version

    cache.set(admission)
    return admission


def main():
    """CLI: python admission_service.py <node_id> <node_name> [--proxy socks5h://...]"""
    import sys
    if len(sys.argv) < 3:
        print("Usage: admission_service.py <node_id> <node_name> [--proxy socks5h://...]")
        sys.exit(1)

    node_id = sys.argv[1]
    node_name = sys.argv[2]
    proxy = 'socks5h://127.0.0.1:7891'
    if '--proxy' in sys.argv:
        proxy = sys.argv[sys.argv.index('--proxy') + 1]

    cache = AdmissionCache()
    admission = run_full_admission(node_id, node_name, proxy, cache)
    print(json.dumps(admission.to_dict(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()