"""A per-module temporary home for product state that defaults to /etc/geomask.

The router is the only host where those defaults are writable by the product. A Linux CI
runner is not root, so a test that leaves the defaults alone fails with PermissionError
instead of testing its subject (CI-187, group A). Import this module and call
``isolate_audit_state()`` **before** the product module is loaded: the defaults are read at
import time, and ``setdefault`` keeps an explicit value a test set first.
"""
import os
import tempfile
from pathlib import Path

_VARIABLES = (
    'GM_AUDIT_STORE', 'GM_AUDIT_STATE', 'GM_AUDIT_EVENTS', 'GM_AUDIT_POOLS',
    'GM_AUDIT_REPORTS', 'GM_AUDIT_SNAPSHOTS', 'GM_AUDIT_CONFIG', 'GM_AUDIT_REGISTRY',
    'GM_AUDIT_RECEIPT',
)


def isolate_audit_state(root=None):
    """Point every audit default at a fresh temporary tree and return that tree."""
    root = Path(root) if root else Path(tempfile.mkdtemp(prefix='geomask-hermetic-'))
    values = {
        'GM_AUDIT_STORE': root / 'node-audit.json',
        'GM_AUDIT_STATE': root / 'state',
        'GM_AUDIT_EVENTS': root / 'events.jsonl',
        'GM_AUDIT_POOLS': root / 'pools',
        'GM_AUDIT_REPORTS': root / 'reports',
        'GM_AUDIT_SNAPSHOTS': root / 'snapshots',
        'GM_AUDIT_CONFIG': root / 'config.yaml',
        'GM_AUDIT_REGISTRY': root / 'subs.list',
        'GM_AUDIT_RECEIPT': root / 'receipt.json',
    }
    for name in _VARIABLES:
        os.environ.setdefault(name, str(values[name]))
    return root
