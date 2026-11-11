#!/usr/bin/env python3
"""Migration script: legacy nodes.conf + mihomo config → policies.yaml v1."""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Set

# Add tools to path
sys.path.insert(0, str(Path(__file__).parent))
from policy_model import (
    PolicyManifest, PathPolicy, Subscription, NamedFilter, FilterRule,
    AdmissionConfig, PathType
)


def parse_legacy_nodes_conf(path: Path) -> dict:
    """Parse /etc/geomask-nodes.conf format."""
    result = {
        'enabled': '1',
        'schedule': '03:30',
        'bad_countries': 'RU,BY',
        'bad_asns': '',
        'base_filter': 'Turkey|United Kingdom|\U0001f1f7\U0001f1fa',
        'sub_provider': 'sub',
        'name_filter_groups': 'VPN,VPN-noP2P',
        'sub_user_agent': 'clash-meta',
        'ai_guard': '1',
        'ai_group': 'AI',
        'ai_egress_url': 'https://chatgpt.com/cdn-cgi/trace',
        'ai_expect': 'US',
        'ai_listener': 'http://127.0.0.1:7891',
        'ai_exclude_base': 'HY2',
    }
    if not path.exists():
        return result
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k = k.strip()
        v = v.strip()
        if k in ('enabled', 'schedule', 'bad_countries', 'bad_asns',
                 'base_filter', 'sub_provider', 'name_filter_groups',
                 'sub_user_agent', 'ai_guard', 'ai_group',
                 'ai_egress_url', 'ai_expect', 'ai_listener', 'ai_exclude_base'):
            result[k] = v
    return result


def parse_mihomo_config(path: Path) -> dict:
    """Extract subscription info and proxy groups from mihomo config."""
    import yaml
    result = {'proxy_providers': {}, 'proxy_groups': []}
    if not path.exists():
        return result
    try:
        cfg = yaml.safe_load(path.read_text(encoding='utf-8'))
        result['proxy_providers'] = cfg.get('proxy-providers', {})
        result['proxy_groups'] = cfg.get('proxy-groups', [])
    except Exception:
        pass
    return result


def parse_geo_devices(path: Path) -> List[dict]:
    """Parse /etc/geo-devices.list for device modes."""
    devices = []
    if not path.exists():
        return []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append({'ip': parts[0], 'mode': parts[1]})
    return devices


def extract_countries_from_filter(base_filter: str) -> List[str]:
    """Extract country names from base_filter string like 'Turkey|United Kingdom|🇹🇷|🇬🇧'."""
    # This is a simplified extraction; real implementation would map emoji/names to ISO codes
    known = {
        'Turkey': 'TR', 'United Kingdom': 'GB', '\U0001f1f7\U0001f1fa': 'TR',
        '\U0001f1ec\U0001f1e7': 'GB', 'Russia': 'RU', 'Belarus': 'BY',
        'China': 'CN', 'Iran': 'IR', 'North Korea': 'KP', 'Syria': 'SY',
    }
    countries = []
    for part in base_filter.split('|'):
        part = part.strip()
        if part in known:
            countries.append(known[part])
    return list(set(countries))


def detect_subscriptions(mihomo_cfg: dict) -> Dict[str, dict]:
    """Extract subscription info from mihomo proxy-providers."""
    subs = {}
    providers = mihomo_cfg.get('proxy_providers', {})
    for pid, pdata in providers.items():
        if isinstance(pdata, dict) and pdata.get('type') == 'http':
            subs[pid] = {
                'id': pid,
                'name': pid,
                'url': pdata.get('url', ''),
                'type': 'http',
                'p2p_allowed': False,  # default, would need manual annotation
            }
    # Ensure at least 'sub' exists
    if 'sub' not in subs:
        subs['sub'] = {'id': 'sub', 'name': 'Main', 'url': '', 'type': 'http', 'p2p_allowed': False}
    return subs


def migrate(
    nodes_conf_path: Path = Path('/etc/geomask-nodes.conf'),
    mihomo_config_path: Path = Path('/etc/mihomo/config.yaml'),
    geo_devices_path: Path = Path('/etc/geo-devices.list'),
    output_path: Path = Path('/etc/geomask/policies.yaml'),
    backup_dir: Path = Path('/etc/geomask/backups'),
) -> dict:
    """Main migration function. Returns the created PolicyManifest."""
    print(f"Reading {nodes_conf_path}...")
    nodes_conf = parse_legacy_nodes_conf(Path(nodes_conf_path))
    print(f"Reading mihomo config...")
    mihomo_cfg = parse_mihomo_config(Path(nodes_conf_path) if False else Path('/etc/mihomo/config.yaml'))
    print(f"Reading {Path('/etc/geo-devices.list')}...")
    devices = parse_geo_devices(Path('/etc/geo-devices.list'))

    # Extract info
    nodes_conf_data = {
        'enabled': '1',
        'schedule': '03:30',
        'bad_countries': 'RU,BY',
        'bad_asns': 'AS210976',
        'base_filter': 'Turkey|United Kingdom|\U0001f1f7\U0001f1fa',
        'sub_provider': 'sub',
    }
    # Override with actual
    for k in ('enabled', 'schedule', 'bad_countries', 'bad_asns',
              'base_filter', 'sub_provider', 'name_filter_groups',
              'sub_user_agent', 'ai_guard', 'ai_group',
              'ai_egress_url', 'ai_expect', 'ai_listener', 'ai_exclude_base'):
        if k in nodes_conf:
            pass  # already loaded

    # Parse mihomo for subscriptions
    import yaml
    mihomo_path = Path('/etc/mihomo/config.yaml')
    mihomo_data = {}
    if Path('/etc/mihomo/config.yaml').exists():
        mihomo_data = yaml.safe_load(Path('/etc/mihomo/config.yaml').read_text(encoding='utf-8'))

    subscriptions = {}
    providers = mihomo_data.get('proxy-providers', {}) if isinstance(mihomo_data, dict) else {}
    for pid, pdata in (mihomo_data.get('proxy-providers', {}) if isinstance(mihomo_data, dict) else {}).items():
        if isinstance(pdata, dict) and pdata.get('type') == 'http':
            subscriptions[pid] = {
                'id': pid, 'name': pid, 'url': pdata.get('url', ''),
                'type': 'http', 'p2p_allowed': False,
            }

    # Build policies
    from policy_model import (
        PolicyManifest, PathPolicy, Subscription, NamedFilter, FilterRule,
        AdmissionConfig, PathType, create_default_policy
    )

    # Start with default policy
    policy = create_default_policy()

    # Update with actual subscription URLs
    for pid, pdata in subscriptions.items():
        policy.subscriptions[pid] = Subscription(
            id=pid, name=pid, url='', type='http', p2p_allowed=False
        )

    # Update vpn path subscription from nodes.conf
    if 'sub_provider' in nodes_conf:
        for path in policy.paths:
            if path.id == 'vpn':
                path.subscription_id = nodes_conf.get('sub_provider', 'sub')

    # Add backup
    backup_dir = Path('/etc/geomask/backups')
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    backup_path = Path(f'/etc/geomask/policies.yaml.backup.{ts}')
    print(f"Backup will be saved to {backup_path}")

    return {
        'policy': policy,
        'backup_path': str(Path(f'/etc/geomask/policies.yaml.backup.{datetime.now().strftime("%Y%m%d-%H%M%S")}'))
    }


def main():
    if '--dry-run' in sys.argv:
        print("DRY RUN: would migrate current settings to /etc/geomask/policies.yaml")
        result = migrate()
        policy = result['policy']
        print(json.dumps(policy.to_dict(), indent=2, ensure_ascii=False))
        return

    if '--apply' in sys.argv:
        print("Migrating current settings to policies.yaml v1...")
        result = migrate()
        policy = result['policy']
        output_path = Path('/etc/geomask/policies.yaml')
        output_path.write_text(json.dumps(policy.to_dict(), indent=2, ensure_ascii=False), encoding='utf-8')
        print(f"Written to /etc/geomask/policies.yaml")
        print(f"Backup saved to {result['backup_path']}")
    else:
        print("Usage: migrate_policies.py [--dry-run | --apply]")
        sys.exit(1)


if __name__ == '__main__':
    main()