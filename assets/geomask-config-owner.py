#!/usr/bin/env python3
"""Config ownership tracker: coordinates slot setter, legacy scanner, audit, and tx ownership."""
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

# Load state module
import importlib.util
STATE_MODULE_PATH = Path(__file__).with_name('geomask-audit-state.py')
_state_spec = importlib.util.spec_from_file_location('audit_state', STATE_MODULE_PATH)
_state = importlib.util.module_from_spec(_state_spec)
_state_spec.loader.exec_module(_state)

# Load config-tx module
TX_MODULE_PATH = Path(__file__).with_name('geomask-config-tx.py')
_tx_spec = importlib.util.spec_from_file_location('config_tx', TX_MODULE_PATH)
_tx = importlib.util.module_from_spec(_tx_spec)
_tx_spec.loader.exec_module(_tx)

# Paths from state module
DEFAULT_CONFIG = _state.DEFAULT_CONFIG
DEFAULT_STATE_DIR = _state.DEFAULT_STATE_DIR

OWNERSHIP_FILE = DEFAULT_STATE_DIR / 'config-ownership.json'
_lock = threading.Lock()

class OwnershipError(Exception):
    pass

def stamp():
    return _state.stamp()

def digest(data: bytes) -> str:
    return _state.digest(data)

def canonical_json(obj) -> bytes:
    return _state.canonical_json(obj)

def encode(value) -> bytes:
    return _state.encode(value)

def strict_json(data: bytes):
    return _state.strict_json(data)

def atomic_write(path: Path, data: bytes):
    _state.atomic_write(path, data)

@contextlib.contextmanager
def ownership_lock():
    """File-based lock for ownership operations."""
    lock_path = DEFAULT_STATE_DIR / '.ownership.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_acquired = False
    try:
        # Windows-compatible lock using O_EXCL for atomic creation
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        lock_acquired = True
        try:
            yield
        finally:
            # Release lock by deleting the file
            if lock_acquired:
                try:
                    os.unlink(lock_path)
                except Exception:
                    pass
    except FileExistsError:
        raise OwnershipError('ownership lock held')
    except Exception:
        if lock_acquired:
            try:
                os.unlink(lock_path)
            except Exception:
                pass
        raise

def load_ownership():
    """Load ownership state."""
    if not OWNERSHIP_FILE.exists():
        return {'schema': 1, 'groups': {}}
    try:
        return _state.strict_json(OWNERSHIP_FILE.read_bytes())
    except Exception:
        return {'schema': 1, 'groups': {}}

def save_ownership(data: dict):
    """Save ownership state atomically."""
    atomic_write(OWNERSHIP_FILE, encode(data))

def _register_unlocked(data: dict, group_name: str, owner: str, provider_ids: list) -> dict:
    """Record ownership in an already-loaded store. Caller holds the lock."""
    entry = data.setdefault('groups', {}).get(group_name, {})
    data['groups'][group_name] = {
        'owner': owner,  # 'slot', 'scanner', 'audit', 'manual'
        'provider_ids': provider_ids,
        'registered_at': _state.stamp(),
        'version': int(entry.get('version', 0)) + 1,
    }
    return data

def register_owner(group_name: str, owner: str, provider_ids: list):
    """Register that a group is owned by a specific component."""
    with ownership_lock():
        save_ownership(_register_unlocked(load_ownership(), group_name, owner, provider_ids))

def unregister_owner(group_name: str, expected_owner: str = None):
    """Unregister ownership (when group is deleted or transferred)."""
    with ownership_lock():
        data = load_ownership()
        if group_name in data.get('groups', {}):
            current = data['groups'][group_name].get('owner')
            if expected_owner and current != expected_owner:
                raise OwnershipError(
                    f'group {group_name} owned by {current}, not {expected_owner}')
            del data['groups'][group_name]
            save_ownership(data)

def get_owner(group_name: str):
    """Get current owner of a group."""
    data = load_ownership()
    return data.get('groups', {}).get(group_name, {}).get('owner')

def can_modify_group(group_name: str, requester: str) -> bool:
    """Check if requester can modify the group."""
    owner = get_owner(group_name)
    return owner is None or owner == requester

def claim_group(group_name: str, requester: str, provider_ids: list) -> bool:
    """Claim ownership, or confirm the caller already owns it.

    Uses one lock acquisition and one write: calling register_owner() here would try to
    take the same lock again and deadlock.
    """
    with ownership_lock():
        data = load_ownership()
        current = data.get('groups', {}).get(group_name, {}).get('owner')
        if current is not None and current != requester:
            return False
        save_ownership(_register_unlocked(data, group_name, requester, provider_ids))
        return True

def check_group_ownership_before_write(group_name: str, requester: str):
    """Validate that requester owns the group before allowing config write."""
    owner = get_owner(group_name)
    if owner is not None and owner != requester:
        raise OwnershipError(f'group {group_name} is owned by {owner}, not {requester}')

def get_group_provider_ids(config: dict, group_name: str) -> list:
    """Get provider IDs used by a group."""
    groups = config.get('proxy-groups', [])
    for g in groups:
        if g.get('name') == group_name:
            return g.get('use', [])
    return []

def sync_ownership_with_config(config: dict):
    """Sync ownership registry with current config state."""
    with ownership_lock():
        data = load_ownership()
        # Find all groups in config
        config_groups = set()
        for g in config.get('proxy-groups', []):
            name = g.get('name')
            if name:
                config_groups.add(name)
        
        # Remove ownership for groups that no longer exist
        to_delete = [name for name in data.get('groups', {}) if name not in config_groups]
        for name in to_delete:
            del data['groups'][name]
        
        # Register ownership for new groups if unowned
        for g in config.get('proxy-groups', []):
            name = g.get('name')
            if name and name not in data.get('groups', {}):
                _register_unlocked(data, name, 'manual', g.get('use', []))

        save_ownership(data)

# CLI for testing
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['claim', 'check', 'sync', 'list', 'unregister'])
    parser.add_argument('--group', help='Group name')
    parser.add_argument('--owner', help='Owner name (slot, scanner, audit, manual)')
    parser.add_argument('--providers', help='Comma-separated provider IDs')
    args = parser.parse_args()
    
    if args.command == 'claim':
        if not args.group or not args.owner:
            print('ERROR: --group and --owner required for claim')
            sys.exit(1)
        providers = args.providers.split(',') if args.providers else []
        result = claim_group(args.group, args.owner, providers)
        print(json.dumps({'ok': True, 'claimed': result}))
    elif args.command == 'check':
        if not args.group:
            print('ERROR: --group required for check')
            sys.exit(1)
        owner = get_owner(args.group)
        print(json.dumps({'ok': True, 'group': args.group, 'owner': owner}))
    elif args.command == 'sync':
        config_path = _state.DEFAULT_CONFIG
        if config_path.exists():
            import yaml
            config = yaml.safe_load(config_path.read_text())
            sync_ownership_with_config(config)
        print(json.dumps({'ok': True, 'synced': True}))
    elif args.command == 'list':
        data = load_ownership()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif args.command == 'unregister':
        if not args.group:
            print('ERROR: --group required for unregister')
            sys.exit(1)
        try:
            unregister_owner(args.group, args.owner)
            print(json.dumps({'ok': True, 'unregistered': True}))
        except OwnershipError as e:
            print(json.dumps({'ok': False, 'error': str(e)}))
            sys.exit(1)
