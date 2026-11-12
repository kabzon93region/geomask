#!/usr/bin/env python3
"""Lane and Torrent operations using config transaction system."""
import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

from pathlib import Path

# Load config-tx module
TX_MODULE_PATH = Path(__file__).with_name('geomask-config-tx.py')
_tx_spec = importlib.util.spec_from_file_location('config_tx', TX_MODULE_PATH)
_tx = importlib.util.module_from_spec(_tx_spec)
_tx_spec.loader.exec_module(_tx)

# Paths
CFG = Path('/etc/mihomo/config.yaml')

class LaneTorrentError(Exception):
    pass

def _read_config():
    """Read and parse mihomo config."""
    with open(CFG, encoding='utf-8') as f:
        return f.read()

def _validate_config(text=None):
    """Validate mihomo config."""
    if text is None:
        result = subprocess.run(['mihomo', '-t', '-d', '/etc/mihomo'],
                               capture_output=True, timeout=60)
    else:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(text)
            tmp = f.name
        try:
            result = subprocess.run(['mihomo', '-t', '-d', '/etc/mihomo', '-f', tmp],
                                   capture_output=True, timeout=60)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    return result.returncode == 0

def _reload_mihomo():
    """Reload mihomo via controller API."""
    text = _read_config()
    secret = ''
    for line in text.splitlines():
        m = re.match(r'^secret:\s*["\']?([^"\'\s]+)', line)
        if m:
            secret = m.group(1)
            break
    
    url = 'http://127.0.0.1:9090/configs?force=true'
    req = urllib.request.Request(url,
                                data=json.dumps({'path': '/etc/mihomo/config.yaml'}).encode(),
                                method='PUT',
                                headers={'Content-Type': 'application/json'})
    if secret:
        req.add_header('Authorization', 'Bearer ' + secret)
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception as e:
        raise LaneTorrentError(f'mihomo reload failed: {e}')

# ============================================================================
# LANE OPERATIONS
# ============================================================================

def _lane_install_changes(lane_name='VPN-noP2P', lane_ip='198.20.0.1', lane_port=7892, lane_group='VPN-noP2P'):
    """Build config changes for lane install."""
    config = _read_config()
    
    # Check group exists
    if f'name: {lane_group}' not in config:
        raise LaneTorrentError(f'mihomo group {lane_group} not found')
    
    # Check if listener already exists
    listener_name = f'lane-{lane_name}'
    if f'name: {listener_name}' in config:
        return {}  # Already exists
    
    # Build listener block
    listener_block = f'''  - name: {listener_name}
    type: mixed
    port: {lane_port}
    listen: 127.0.0.1
    proxy: {lane_group}'''
    
    # Insert into listeners block
    if 'listeners:' in config:
        # Insert at end of listeners block
        import re
        m = re.search(r'(?m)^listeners:\s*$(.*?)(?=^(?=[a-z]))', config, re.M | re.S)
        if m:
            body = m.group(1)
            tail = len(body) - len(body.rstrip('\n'))
            new_body = body.rstrip('\n') + '\n' + listener_block + '\n' + '\n' * (1 if tail else 0)
            new_config = config[:m.start(1)] + new_body + config[m.end(1):]
            return {'config.yaml': new_config.encode('utf-8')}
    
    raise LaneTorrentError('Could not find listeners block in config')

def _tx_files(payload):
    """Wrap a config payload for the transaction, keyed by its absolute path.

    The transaction refuses relative keys, and a bare 'config.yaml' used to be resolved
    against the caller's working directory.
    """
    return {str(CFG): payload}

def lane_install(lane_name='VPN-noP2P', lane_ip='198.20.0.1', lane_port=7892, lane_group='VPN-noP2P'):
    """Install a new lane using transaction."""
    changes = _lane_install_changes(lane_name, lane_ip, lane_port, lane_group)
    if not changes:
        return {'ok': True, 'message': 'Lane already installed'}

    return _tx.start_transaction(_tx_files(changes['config.yaml']))

def lane_remove(lane_name='VPN-noP2P'):
    """Remove a lane using transaction."""
    config = _read_config()
    listener_name = f'lane-{lane_name}'
    
    if f'name: {listener_name}' not in config:
        return {'ok': True, 'message': 'Lane not installed'}
    
    # Remove listener block
    import re
    pattern = rf'(?m)^  - name: {re.escape(listener_name)}\n(?:[ ]{{4}}.*\n)+'
    new_config = re.sub(pattern, '', config, count=1)
    
    if new_config == config:
        return {'ok': True, 'message': 'Lane not found in config'}
    
    return _tx.start_transaction(_tx_files(new_config.encode('utf-8')))

def lane_status(lane_name='VPN-noP2P'):
    """Get lane status."""
    config = _read_config()
    listener_name = f'lane-{lane_name}'
    listener_present = f'name: {listener_name}' in config
    netdev_present = os.path.exists(f'/sys/class/net/{lane_name}')
    service_exists = os.path.exists(f'/etc/init.d/hev-{lane_name}')
    
    return {
        'ok': True,
        'lane_name': lane_name,
        'listener_present': listener_present,
        'netdev_present': netdev_present,
        'service_exists': service_exists,
    }

# ============================================================================
# TORRENT OPERATIONS
# ============================================================================

def torrent_status():
    """Get torrent status."""
    config = _read_config()
    ruleset_path = Path('/etc/mihomo/ruleset/torrentdirect.yaml')
    
    listener_present = 'torrent-direct' in config
    listener_policy = ''
    if listener_present:
        m = re.search(r'(?m)^  - name: torrent-direct\n(?:[ ]{4}.*\n)*?[ ]{4}proxy:\s*(\S+)', config)
        if m:
            listener_policy = m.group(1)
    
    ruleset_text = ''
    if Path('/etc/mihomo/ruleset/torrentdirect.yaml').exists():
        ruleset_text = Path('/etc/mihomo/ruleset/torrentdirect.yaml').read_text()
    
    return {
        'ok': True,
        'listener_present': listener_present,
        'listener_policy': listener_policy,
        'listener_port': 7894,
        'ruleset_present': Path('/etc/mihomo/ruleset/torrentdirect.yaml').exists(),
        'domains': len(re.findall(r'DOMAIN-SUFFIX,', ruleset_text)),
        'raw_ips': len(re.findall(r'IP-CIDR,', ruleset_text)),
    }

def _torrent_listener_changes(enable):
    """Build config changes for torrent listener enable/disable."""
    config = _read_config()
    
    if enable:
        if 'torrent-direct' in config:
            # Already present, check if policy needs update
            m = re.search(r'(?m)^  - name: torrent-direct\n(?:[ ]{4}.*\n)*?[ ]{4}proxy:\s*(\S+)', config)
            current_policy = m.group(1) if m else ''
            # Should be VPN or DIRECT, never AI
            if current_policy in ('VPN', 'DIRECT'):
                return {}  # Already correct
            # Update policy
            new_config = re.sub(
                r'(?m)^(\s{4}proxy:\s*)\S+\s*$',
                rf'\1VPN',
                config
            )
            if new_config != config:
                return {'config.yaml': new_config.encode('utf-8')}
            return {}
        else:
            # Add listener before ai-devices
            listener_block = f'''  - name: torrent-direct
    type: mixed
    port: 7894
    listen: 192.168.0.1
    proxy: VPN'''
            if 'ai-devices' in config:
                # Insert before ai-devices
                idx = config.index('ai-devices')
                # Find the line start
                lines = config.splitlines(True)
                for i, line in enumerate(lines):
                    if 'ai-devices' in line and line.strip().startswith('- name:'):
                        lines.insert(i, listener_block + '\n')
                        return {str(DEFAULT_CONFIG if 'DEFAULT_CONFIG' in dir() else _tx.DEFAULT_CONFIG): ''.join(lines).encode('utf-8')}
            return {}
    else:
        # Disable listener
        if 'torrent-direct' not in config:
            return {}
        # Remove torrent-direct block
        new_config = re.sub(
            r'(?m)^  - name: torrent-direct\n(?:[ ]{4}.*\n)+',
            '',
            config
        )
        if new_config != config:
            return {'config.yaml': new_config.encode('utf-8')}
        return {}

def torrent_set_listener(enable):
    """Enable/disable torrent listener using transaction."""
    changes = _torrent_listener_changes(enable)
    if not changes:
        return {'ok': True, 'message': 'No change needed'}
    
    return _tx.start_transaction(_tx_files(changes['config.yaml']))

def torrent_refresh():
    """Refresh torrent ruleset."""
    # This is a complex operation - use the existing script for now
    # TODO: Implement in transaction system
    raise LaneTorrentError('Torrent refresh not yet implemented in transaction system')

def torrent_set_extra(hosts):
    """Set extra hosts for torrent ruleset."""
    raise LaneTorrentError('Torrent extra hosts not yet implemented in transaction system')

# ============================================================================
# MTRICKLE SYNC OPERATIONS
# ============================================================================

def mtrickle_sync_changes():
    """Build config changes for mtrickle sync."""
    # This is a complex operation that modifies rulesets, not the main config
    # For now, we just need to trigger reload if rulesets changed
    # The actual sync is done by the shell script
    return {}

def mtrickle_sync():
    """Trigger mtrickle sync and reload if needed."""
    # Run the sync script
    result = subprocess.run(['/root/sync_mtrickle_mihomo.sh'], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise LaneTorrentError(f'mtrickle sync failed: {result.stderr}')
    
    # Check if reload is needed (look for "reload needed" in output)
    if 'reload needed' in result.stdout or 'reload' in result.stdout.lower():
        _reload_mihomo()
    
    return {'ok': True, 'output': result.stdout}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['lane-status', 'torrent-status', 'lane-install', 'lane-remove', 'torrent-enable', 'torrent-disable', 'mtrickle-sync'])
    parser.add_argument('--lane', help='Lane name')
    parser.add_argument('--enable', help='Enable/disable')
    args = parser.parse_args()
    
    if args.command == 'lane-status':
        result = lane_status(args.lane or 'VPN-noP2P')
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'torrent-status':
        result = torrent_status()
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'lane-install':
        result = lane_install(args.lane or 'VPN-noP2P')
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'lane-remove':
        result = lane_remove(args.lane or 'VPN-noP2P')
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'torrent-enable':
        result = torrent_set_listener(True)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'torrent-disable':
        result = torrent_set_listener(False)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'mtrickle-sync':
        result = mtrickle_sync()
        print(json.dumps(result, ensure_ascii=False))
