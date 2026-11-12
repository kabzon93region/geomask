#!/usr/bin/env python3
"""Youtube watch and sync operations using config transaction system."""
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

class SyncYoutubeError(Exception):
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
    text = open(CFG, encoding='utf-8').read()
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
        raise SyncYoutubeError(f'mihomo reload failed: {e}')

# ============================================================================
# YOUTUBE WATCH OPERATIONS
# ============================================================================

def yt_status():
    """Get youtube watch status."""
    config = open(CFG, encoding='utf-8').read()
    
    # Check for ytwatch mode
    mode = 'off'
    if 'ytwatch' in config:
        m = re.search(r'(?m)^\s*ytwatch:\s*(\S+)', config)
        if m:
            mode = m.group(1)
    
    return {
        'ok': True,
        'mode': mode,
    }

def yt_mode_changes(mode):
    """Build config changes for ytwatch mode."""
    config = open(CFG, encoding='utf-8').read()
    
    if mode == 'off':
        # Remove ytwatch section
        if 'ytwatch:' in config:
            import re
            new_config = re.sub(r'(?m)^\s*ytwatch:.*\n(?:[ ]{2,4}.*\n)*', '', config, count=1)
            if new_config != config:
                return {str(DEFAULT_CONFIG if 'DEFAULT_CONFIG' in dir() else _tx.DEFAULT_CONFIG): new_config.encode('utf-8')}
        return {}
    else:
        # Add or update ytwatch section
        ytwatch_block = f'ytwatch: {mode}'
        if 'ytwatch:' in config:
            import re
            new_config = re.sub(r'(?m)^\s*ytwatch:.*', ytwatch_block, config, count=1)
            if new_config != config:
                return {str(DEFAULT_CONFIG if 'DEFAULT_CONFIG' in dir() else _tx.DEFAULT_CONFIG): new_config.encode('utf-8')}
        else:
            # Add after rules section
            if 'rules:' in config:
                lines = config.splitlines(True)
                for i, line in enumerate(lines):
                    if line.strip() == 'rules:':
                        # Find end of rules block
                        j = i + 1
                        while j < len(lines) and (lines[j].startswith('  ') or lines[j].strip() == ''):
                            j += 1
                        lines.insert(j, f'ytwatch: {mode}\n')
                        return {str(DEFAULT_CONFIG if 'DEFAULT_CONFIG' in dir() else _tx.DEFAULT_CONFIG): ''.join(lines).encode('utf-8')}
        return {}

def yt_mode(mode):
    """Set ytwatch mode."""
    if mode not in ('off', 'yt-dlp', 'yt-dlp-live', 'yt-dlp-audio'):
        raise SyncYoutubeError(f'Invalid yt mode: {mode}')
    
    changes = yt_mode_changes(mode)
    if not changes:
        return {'ok': True, 'message': f'ytwatch already set to {mode}'}

    # Absolute key: the transaction refuses relative targets.
    return _tx.start_transaction({str(CFG): changes['config.yaml']})

def yt_reset():
    """Reset ytwatch to off."""
    return yt_mode('off')

# ============================================================================
# SYNC OPERATIONS
# ============================================================================

def sync_mtrickle_changes():
    """Build config changes for mtrickle sync."""
    # This operation primarily modifies rulesets, not main config
    # It triggers reload if rulesets changed
    return {}

def sync_mtrickle():
    """Run mtrickle sync and reload if needed."""
    # Run the sync script
    result = subprocess.run(['/root/sync_mtrickle_mihomo.sh'], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise SyncYoutubeError(f'mtrickle sync failed: {result.stderr}')
    
    # Check if reload is needed
    if 'reload needed' in result.stdout or 'reload' in result.stdout.lower():
        _reload_mihomo()
    
    return {'ok': True, 'output': result.stdout}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['yt-status', 'yt-mode', 'yt-reset', 'mtrickle-sync'])
    parser.add_argument('--mode', help='ytwatch mode')
    args = parser.parse_args()
    
    if args.command == 'yt-status':
        result = yt_status()
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'yt-mode':
        if not args.mode:
            print(json.dumps({'ok': False, 'error': 'mode required'}))
            sys.exit(1)
        result = yt_mode(args.mode)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'yt-reset':
        result = yt_reset()
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'mtrickle-sync':
        result = sync_mtrickle()
        print(json.dumps(result, ensure_ascii=False))
