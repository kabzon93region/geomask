#!/usr/bin/env python3
"""Policy model: versioned dataclasses, validation, serialization for geomask paths."""
import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
from enum import Enum


class PathType(str, Enum):
    VPN = "vpn"
    VPN_NO_P2P = "vpn_nop2p"
    AI = "ai"
    BYPASS = "bypass"
    DIRECT = "direct"


@dataclass
class Subscription:
    id: str
    name: str
    url: str
    type: str = "http"
    p2p_allowed: bool = False
    health_check_url: Optional[str] = None


@dataclass
class FilterRule:
    """Single filter rule for a named filter."""
    type: str  # DOMAIN-SUFFIX, GEOIP, DOMAIN, etc.
    value: str
    action: str  # DIRECT, REJECT, PROXY, etc.
    comment: str = ""


@dataclass
class NamedFilter:
    id: str
    name: str
    rules: List[FilterRule] = field(default_factory=list)


@dataclass
class AdmissionConfig:
    required: bool = False
    services: List[str] = field(default_factory=lambda: ["google", "chatgpt", "openrouter"])
    ttl_seconds: int = 300


@dataclass
class PathPolicy:
    """Single path (VPN, VPN-noP2P, AI, etc.)"""
    id: str
    name: str
    path_type: PathType
    interface: str = "mihomo"
    subscription_id: str = ""
    p2p_allowed: bool = False
    countries_allow: List[str] = field(default_factory=list)
    countries_deny: List[str] = field(default_factory=list)
    asns_allow: List[str] = field(default_factory=list)
    asns_deny: List[str] = field(default_factory=list)
    filter_id: str = ""
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    enabled: bool = True

    def __post_init__(self):
        # Convert admission dict to AdmissionConfig if needed
        if isinstance(self.admission, dict):
            self.admission = AdmissionConfig(**self.admission)
        if isinstance(self.path_type, str):
            self.path_type = PathType(self.path_type)

    def validate(self, all_subscriptions: Dict[str, Subscription], all_filters: Dict[str, 'NamedFilter']) -> List[str]:
        errors = []
        if not self.id or not re.fullmatch(r'[a-z0-9_-]+', self.id):
            errors.append(f"Invalid path id: {self.id}")
        if self.subscription_id and self.subscription_id not in all_subscriptions:
            errors.append(f"Path {self.id}: unknown subscription {self.subscription_id}")
        if self.filter_id and self.filter_id not in all_filters:
            errors.append(f"Path {self.id}: unknown filter {self.filter_id}")
        # Countries: 2-letter codes
        for cc in self.countries_allow + self.countries_deny:
            if not re.fullmatch(r'[A-Z]{2}', cc):
                errors.append(f"Path {self.id}: invalid country code {cc}")
        # ASNs
        for asn in self.asns_allow + self.asns_deny:
            if not re.fullmatch(r'(AS)?\d+', asn):
                errors.append(f"Path {self.id}: invalid ASN {asn}")
        if self.admission.required:
            if not self.admission.services:
                errors.append(f"Path {self.id}: admission required but no services listed")
        return errors


@dataclass
class PolicyManifest:
    version: int = 1
    schema: str = "geomask-policy-v1"
    paths: List[PathPolicy] = field(default_factory=list)
    subscriptions: Dict[str, Subscription] = field(default_factory=dict)
    filters: Dict[str, NamedFilter] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_path(self, path_id: str) -> Optional[PathPolicy]:
        for p in self.paths:
            if p.id == path_id:
                return p
        return None

    def get_subscription(self, sub_id: str) -> Optional[Subscription]:
        return self.subscriptions.get(sub_id)

    def get_filter(self, filter_id: str) -> Optional[NamedFilter]:
        return self.filters.get(filter_id)

    def validate(self) -> List[str]:
        errors = []
        # Unique path IDs
        path_ids = [p.id for p in self.paths]
        if len(path_ids) != len(set(path_ids)):
            errors.append("Duplicate path IDs")
        # Unique subscription IDs
        sub_ids = list(self.subscriptions.keys())
        if len(sub_ids) != len(set(sub_ids)):
            errors.append("Duplicate subscription IDs")
        # Unique filter IDs
        filter_ids = list(self.filters.keys())
        if len(filter_ids) != len(set(filter_ids)):
            errors.append("Duplicate filter IDs")
        # Validate each path
        for path in self.paths:
            errors.extend(path.validate(self.subscriptions, self.filters))
        return errors

    def to_dict(self) -> dict:
        return {
            'version': self.version,
            'schema': self.schema,
            'paths': [asdict(p) for p in self.paths],
            'subscriptions': {k: asdict(v) for k, v in self.subscriptions.items()},
            'filters': {k: asdict(v) for k, v in self.filters.items()},
            'metadata': self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'PolicyManifest':
        paths = [PathPolicy(**p) for p in data.get('paths', [])]
        subscriptions = {k: Subscription(**v) for k, v in data.get('subscriptions', {}).items()}
        filters = {}
        for k, v in data.get('filters', {}).items():
            rules = [FilterRule(**r) for r in v.get('rules', [])]
            filters[k] = NamedFilter(id=v.get('id', k), name=v.get('name', k), rules=rules)
        return cls(
            version=data.get('version', 1),
            schema=data.get('schema', 'geomask-policy-v1'),
            paths=paths,
            subscriptions=subscriptions,
            filters=filters,
            metadata=data.get('metadata', {}),
        )

    def sha256(self) -> str:
        """Content hash for revision tracking."""
        canon = json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canon.encode('utf-8')).hexdigest()

    def save(self, path: Path):
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding='utf-8')

    @classmethod
    def load(cls, path: Path) -> 'PolicyManifest':
        if not path.exists():
            return cls()
        return cls.from_dict(json.loads(path.read_text(encoding='utf-8')))


# --- Default policy for migration seed ---

def create_default_policy() -> PolicyManifest:
    """Create the default policy matching current production setup."""
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return PolicyManifest(
        version=1,
        schema="geomask-policy-v1",
        metadata={
            'created': now,
            'migrated_from': 'legacy nodes.conf + mihomo config',
        },
        paths=[
            PathPolicy(
                id="vpn",
                name="VPN",
                path_type=PathType.VPN,
                interface="mihomo",
                subscription_id="sub",
                p2p_allowed=False,
                countries_allow=["NL", "DE", "FR", "SE"],
                countries_deny=[],
                asns_allow=[],
                asns_deny=["AS210976"],
                filter_id="base_filter",
                admission=AdmissionConfig(required=False),
                enabled=True,
            ),
            PathPolicy(
                id="vpn_nop2p",
                name="VPN-noP2P",
                path_type=PathType.VPN_NO_P2P,
                interface="mihomo",
                subscription_id="de5",
                p2p_allowed=False,
                countries_allow=["DE", "PL"],
                countries_deny=[],
                asns_allow=[],
                asns_deny=["AS210976"],
                filter_id="base_filter",
                admission=AdmissionConfig(required=False),
                enabled=True,
            ),
            PathPolicy(
                id="ai",
                name="AI",
                path_type=PathType.AI,
                interface="mihomo",
                subscription_id="sub",
                p2p_allowed=False,
                countries_allow=["US"],
                countries_deny=[],
                asns_allow=[],
                asns_deny=[],
                filter_id="ai_filter",
                admission=AdmissionConfig(
                    required=True,
                    services=["google", "chatgpt", "openrouter"],
                    ttl_seconds=300,
                ),
                enabled=True,
            ),
        ],
        subscriptions={
            "sub": Subscription(id="sub", name="Main", url="https://...", type="http", p2p_allowed=False),
            "de5": Subscription(id="de5", name="Restricted", url="https://...", type="http", p2p_allowed=False),
        },
        filters={
            "base_filter": NamedFilter(
                id="base_filter",
                name="Base Filter",
                rules=[
                    FilterRule(type="DOMAIN-SUFFIX", value="google.com", action="DIRECT"),
                    FilterRule(type="GEOIP", value="RU", action="REJECT"),
                    FilterRule(type="GEOIP", value="BY", action="REJECT"),
                ],
            ),
            "ai_filter": NamedFilter(
                id="ai_filter",
                name="AI Filter (US only)",
                rules=[
                    FilterRule(type="DOMAIN-SUFFIX", value="google.com", action="DIRECT"),
                    FilterRule(type="GEOIP", value="US", action="PROXY"),
                    FilterRule(type="GEOIP", value="!", action="REJECT"),  # negative match
                ],
            ),
        },
    )


if __name__ == '__main__':
    import sys
    policy = create_default_policy()
    errors = policy.validate()
    if errors:
        print("VALIDATION ERRORS:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(policy.to_dict(), indent=2, ensure_ascii=False))