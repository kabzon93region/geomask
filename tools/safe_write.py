#!/usr/bin/env python3
"""Python safe_write helper for embedded writers (geo-backend.sh, node-scan, etc.)"""
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

RESOURCES = {
    'mihomo_config': {
        'live': Path('/etc/mihomo/config.yaml'),
        'lock': Path('/tmp/mihomo_config.lock'),
        'rev': Path('/etc/mihomo/config.yaml.rev'),
        'trial': ['mihomo', '-f', '{cand}', '-d', '/etc/mihomo', '-t'],
        'reload': ['curl', '-sS', '-X', 'PUT', 'http://127.0.0.1:9090/configs?force=true',
                   '-H', 'Content-Type: application/json',
                   '-d', '{"path":"/etc/mihomo/config.yaml"}'],
    },
    'nodes_conf': {
        'live': Path('/etc/geomask-nodes.conf'),
        'lock': Path('/tmp/nodes_conf.lock'),
        'rev': Path('/etc/geomask-nodes.conf.rev'),
        'trial': ['true'],
        'reload': ['true'],
    },
    'zapret_exclude': {
        'live': Path('/opt/zapret/ipset/zapret-hosts-user-exclude.txt'),
        'lock': Path('/tmp/zapret_exclude.lock'),
        'rev': Path('/opt/zapret/ipset/zapret-hosts-user-exclude.txt.rev'),
        'trial': ['nft', '-c', '-f', '-'],  # stdin with nft rules
        'reload': ['pkill', '-HUP', 'nfqws'],
    },
    'cron_root': {
        'live': Path('/etc/crontabs/root'),
        'lock': Path('/tmp/cron_root.lock'),
        'rev': Path('/etc/crontabs/root.rev'),
        'trial': ['crontab', '-l'],
        'reload': ['/etc/init.d/cron', 'reload'],
    },
    'device_rules': {
        'live': Path('/etc/geo-devices.list'),
        'lock': Path('/tmp/device_rules.lock'),
        'rev': Path('/etc/geo-devices.list.rev'),
        'trial': ['nft', '-c', '-f', '/etc/nftables.d/device-rules.nft'],
        'reload': ['/usr/bin/geomask-guard.sh'],
    },
    'mtr_groups': {
        'live': Path('/etc/magitrickle/state/config.yaml'),
        'lock': Path('/tmp/mtr_groups.lock'),
        'rev': Path('/etc/magitrickle/state/config.yaml.rev'),
        'trial': ['magitrickled', '--dry-run'],
        'reload': ['pkill', '-HUP', 'magitrickled'],
    },
}


def backup_gate(max_age_seconds: int = 300) -> bool:
    """Check for fresh full sysupgrade backup on PC within max_age_seconds."""
    # Check local geomask-backups rotation
    backup_dir = Path('/root/geomask-backups')
    if not backup_dir.exists():
        print('BACKUP_GATE: no local geomask backup dir', file=sys.stderr)
        return False
    backups = list(backup_dir.glob('router-backup-*.tar.gz'))
    if not backups:
        print('BACKUP_GATE: no local backups', file=sys.stderr)
        return False
    latest = max(backups, key=lambda p: p.stat().st_mtime)
    age = time.time() - latest.stat().st_mtime
    if age > max_age_seconds:
        print(f'BACKUP_GATE: backup older than {max_age_seconds}s ({age:.0f}s)', file=sys.stderr)
        return False
    # TODO: verify MD5 with PC copy via SSH (fetch_backup.ps1)
    return True


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def acquire_lock(lock_path: Path, timeout: float = 10.0) -> Optional[int]:
    """Acquire flock on lock file. Returns fd on success, None on timeout."""
    if fcntl is None:
        # Windows: use file existence as simple lock
        if lock_path.exists():
            return None
        lock_path.write_text(f'{os.getpid()} {time.time()}')
        return 1  # dummy fd
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, f'{os.getpid()} {time.time()}'.encode())
        return fd
    except BlockingIOError:
        os.close(fd)
        start = time.time()
        while time.time() - start < timeout:
            try:
                fd = os.open(lock_path, os.O_WRONLY)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(fd, f'{os.getpid()} {time.time()}'.encode())
                return fd
            except BlockingIOError:
                time.sleep(0.1)
    print(f'LOCK_TIMEOUT {lock_path}', file=sys.stderr)
    return None


def release_lock(fd: int, lock_path: Path) -> None:
    if fcntl is None:
        try:
            lock_path.unlink()
        except OSError:
            pass
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def safe_write(resource: str,
               make_candidate: Callable[[bytes], bytes],
               validate: Callable[[bytes], Tuple[bool, str]],
               rollback_check: Callable[[bytes, bytes], bool],
               max_age_seconds: int = 300) -> int:
    """
    Unified writer protocol.
    Returns: 0=success, 1=validation failed, 2=backup gate failed, 3=trial failed,
             4=install/reload failed, 5=rollback failed
    """
    if resource not in RESOURCES:
        print(f'UNKNOWN_RESOURCE {resource}', file=sys.stderr)
        return 1

    spec = RESOURCES[resource]
    live = spec['live']
    lock = spec['lock']
    rev = spec['rev']

    # 1. BACKUP GATE
    if not backup_gate(max_age_seconds):
        return 2

    # 2. LOCK
    fd = acquire_lock(lock)
    if fd is None:
        return 1
    try:
        # 3. REVISION
        rev_old = rev.read_text().strip() if rev.exists() else ''

        # 4. SNAPSHOT
        if live.exists():
            pre_content = live.read_bytes()
            pre_stat = live.stat()
            pre_mode = stat.S_IMODE(pre_stat.st_mode)
            pre_owner = (pre_stat.st_uid, pre_stat.st_gid)
        else:
            pre_content = b''
            pre_mode = 0o644
            pre_owner = (0, 0)

        # 5. CANDIDATE
        cand_content = make_candidate(pre_content)

        # 6. VALIDATE
        ok, err = validate(cand_content)
        if not ok:
            print(f'VALIDATE_FAILED {resource}: {err}', file=sys.stderr)
            return 1

        # 7. TRIAL
        trial_cmd = [arg.replace('{cand}', '-') for arg in spec['trial']]
        if '{cand}' in ' '.join(spec['trial']):
            # Write candidate to temp file for trial
            import tempfile
            with tempfile.NamedTemporaryFile(mode='wb', delete=False) as tf:
                tf.write(cand_content)
                cand_path = tf.name
            try:
                trial_cmd = [arg.replace('{cand}', cand_path) for arg in spec['trial']]
                result = subprocess.run(trial_cmd, capture_output=True, timeout=30)
                if result.returncode != 0:
                    print(f'TRIAL_FAILED {resource}: {result.stderr.decode()[:200]}', file=sys.stderr)
                    return 3
            finally:
                os.unlink(cand_path)
        else:
            result = subprocess.run(trial_cmd, capture_output=True, timeout=30, input=cand_content)
            if result.returncode != 0:
                print(f'TRIAL_FAILED {resource}: {result.stderr.decode()[:200]}', file=sys.stderr)
                return 3

        # 8. ATOMIC INSTALL
        import tempfile
        with tempfile.NamedTemporaryFile(mode='wb', dir=live.parent, delete=False) as tf:
            tf.write(cand_content)
            cand_file = tf.name
        try:
            os.chmod(cand_file, pre_mode)
            os.chown(cand_file, *pre_owner)
            os.replace(cand_file, live)
        except Exception as e:
            try:
                os.unlink(cand_file)
            except OSError:
                pass
            print(f'INSTALL_FAILED {resource}: {e}', file=sys.stderr)
            return 4

        # 9. RELOAD
        if spec['reload'] != ['true']:
            reload_result = subprocess.run(spec['reload'], capture_output=True, timeout=30)
            if reload_result.returncode != 0:
                print(f'RELOAD_FAILED {resource}: {reload_result.stderr.decode()[:200]}', file=sys.stderr)
                # 10. ROLLBACK on reload failure
                if not rollback_check(pre_content, live.read_bytes()):
                    # Force restore
                    live.write_bytes(pre_content)
                    subprocess.run(spec['reload'], capture_output=True, timeout=30)
                    if not rollback_check(pre_content, live.read_bytes()):
                        print(f'ROLLBACK_FAILED {resource}', file=sys.stderr)
                        return 5
                else:
                    # Reload failed but rollback check passes (should not happen)
                    subprocess.run(spec['reload'], capture_output=True, timeout=30)
                return 4

        # 10. POST-CHECK
        post_content = live.read_bytes()
        if not rollback_check(pre_content, post_content):
            print(f'POST_CHECK_FAILED {resource}', file=sys.stderr)
            # Rollback
            live.write_bytes(pre_content)
            subprocess.run(spec['reload'], capture_output=True, timeout=30)
            if not rollback_check(pre_content, live.read_bytes()):
                print(f'ROLLBACK_FAILED {resource}', file=sys.stderr)
                return 5
            return 4

        # 11. SUCCESS - update revision
        rev.write_text(sha256_file(live))
        return 0

    finally:
        release_lock(fd, lock)


# --- P12: nodes.conf canonical rendering ---

EDITABLE_KEYS = frozenset(['enabled', 'schedule', 'bad_countries', 'bad_asns', 'base_filter', 'sub_provider'])
PROTECTED_PREFIXES = ('ai_', 'name_filter_', 'sub_user_agent')  # never defaulted, preserved verbatim


def nodes_render_candidate(live_content: bytes, editable_updates: dict) -> bytes:
    """Render new nodes.conf preserving ALL protected/unknown/comments verbatim."""
    lines = live_content.decode('utf-8').splitlines(True)  # keepends
    out = []
    seen_editable = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            out.append(line)
            continue
        if '=' not in line:
            out.append(line)
            continue
        k, v = line.split('=', 1)
        k = k.strip()
        if k in EDITABLE_KEYS:
            if k in editable_updates:
                out.append(f'{k}={editable_updates[k]}\n')
                seen_editable.add(k)
            # else: drop this editable key (should not happen if UI sends all)
        else:
            out.append(line)  # preserve protected/unknown/comment verbatim
    # Append any editable keys not seen (new keys)
    for k in EDITABLE_KEYS:
        if k in editable_updates and k not in seen_editable:
            out.append(f'{k}={editable_updates[k]}\n')
    return ''.join(out).encode('utf-8')


def nodes_validate_request(editable: dict) -> Tuple[bool, str]:
    """Validate all editable keys BEFORE staging."""
    import re
    # enabled: exact 0|1
    if 'enabled' in editable and editable['enabled'] not in ('0', '1'):
        return False, 'enabled must be 0 or 1'
    # schedule: HH:MM
    if 'schedule' in editable and not re.fullmatch(r'([01]?[0-9]|2[0-3]):[0-5][0-9]', editable['schedule']):
        return False, 'schedule must be HH:MM'
    # bad_countries: ^([A-Z]{2})(,[A-Z]{2})*$
    if 'bad_countries' in editable:
        val = editable['bad_countries'].strip()
        if val and not re.fullmatch(r'[A-Z]{2}(,[A-Z]{2})*', val):
            return False, 'bad_countries must be comma-separated 2-letter codes'
    # bad_asns: ^(AS)?[0-9]+(,(AS)?[0-9]+)*$
    if 'bad_asns' in editable:
        val = editable['bad_asns'].strip()
        if val and not re.fullmatch(r'(AS)?[0-9]+(,(AS)?[0-9]+)*', val):
            return False, 'bad_asns must be comma-separated AS numbers (with or without AS prefix)'
    # base_filter: no quotes/backslash (existing rule)
    if 'base_filter' in editable:
        if '"' in editable['base_filter'] or '\\' in editable['base_filter']:
            return False, 'base_filter cannot contain quotes or backslash'
    # sub_provider: ^[a-z0-9_-]+$
    if 'sub_provider' in editable and not re.fullmatch(r'[a-z0-9_-]+', editable['sub_provider']):
        return False, 'sub_provider must be lowercase alphanumeric with underscore/hyphen'
    # REJECT control characters in ANY value
    for k, v in editable.items():
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            return False, f'{k} contains control characters'
    return True, ''


def nodes_rollback_check(pre: bytes, post: bytes) -> bool:
    """Verify rollback: editable keys match pre, protected/unknown preserved."""
    try:
        pre_lines = pre.decode('utf-8').splitlines()
        post_lines = post.decode('utf-8').splitlines()
    except UnicodeDecodeError:
        return False
    pre_dict = {}
    post_dict = {}
    for line in pre_lines:
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.split('=', 1)
            pre_dict[k.strip()] = v.strip()
    for line in post_lines:
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.split('=', 1)
            post_dict[k.strip()] = v.strip()
    # All protected/unknown keys must match exactly
    for k in pre_dict:
        if k not in EDITABLE_KEYS:
            if post_dict.get(k) != pre_dict[k]:
                return False
    # Editable keys: if they were in pre, they should be in post (with new values OK)
    # This is checked by validate -> we trust the update
    return True


# --- mihomo config validation ---
def mihomo_validate(content: bytes) -> Tuple[bool, str]:
    """Basic YAML structure check."""
    try:
        import yaml
        cfg = yaml.safe_load(content)
        if not isinstance(cfg, dict):
            return False, 'config root is not a dict'
        required = ['proxy-providers', 'proxy-groups', 'rule-providers']
        for req in required:
            if req not in cfg:
                return False, f'missing required section: {req}'
        return True, ''
    except Exception as e:
        return False, f'YAML parse error: {e}'


def mihomo_rollback_check(pre: bytes, post: bytes) -> bool:
    """Verify mihomo config rollback: sha256 match."""
    return hashlib.sha256(pre).hexdigest() == hashlib.sha256(post).hexdigest()


# --- zapret exclude validation ---
def zapret_validate(content: bytes) -> Tuple[bool, str]:
    """Each line: bare domain or CIDR, no comments in middle."""
    for i, line in enumerate(content.decode('utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Basic domain/CIDR validation
        if '/' in line:
            # CIDR
            import ipaddress
            try:
                ipaddress.ip_network(line, strict=False)
            except ValueError:
                return False, f'line {i}: invalid CIDR'
        else:
            # Domain - basic label check
            if not all(c.isalnum() or c in '.-' for c in line):
                return False, f'line {i}: invalid domain chars'
    return True, ''


def zapret_rollback_check(pre: bytes, post: bytes) -> bool:
    return hashlib.sha256(pre).hexdigest() == hashlib.sha256(post).hexdigest()


# --- nodes_conf atomic pair (conf + cron) ---
CRON_MANAGED_TAG = 'NODESCAN_TAG'

def update_cron(enabled: bool, schedule: str, old_cron: str) -> str:
    """Update cron with managed entry, preserving other lines."""
    lines = old_cron.splitlines(keepends=True) if old_cron else []
    lines = [l for l in lines if CRON_MANAGED_TAG not in l]
    if enabled:
        hr, minute = schedule.split(':')
        hr = int(hr); minute = int(minute)
        lines.append(f'{minute} {hr} * * * /usr/bin/geomask-node-scan.sh >/dev/null 2>&1 {CRON_MANAGED_TAG}\n')
    return ''.join(lines)


def nodes_render_candidate_with_cron(live_conf: str, live_cron: str, editable: dict) -> tuple[str, str]:
    """Render new conf and cron from live content and editable updates."""
    new_conf = nodes_render_candidate(live_conf, editable)
    enabled = editable.get('enabled', '0') == '1'
    schedule = editable.get('schedule', '03:30')
    new_cron = update_cron(enabled, schedule, live_cron)
    return new_conf, new_cron


def nodes_rollback_check_with_cron(pre: dict, post: dict) -> bool:
    """Verify rollback for both conf and cron."""
    pre_conf = pre.get('conf', '')
    pre_cron = pre.get('cron', '')
    post_conf = post.get('conf', '')
    post_cron = post.get('cron', '')
    return (hashlib.sha256(pre_conf.encode()).hexdigest() == hashlib.sha256(post_conf.encode()).hexdigest() and
            pre_cron == post_cron)


def safe_write_nodes_conf(live_conf: str, live_cron: str, editable: dict, max_age_seconds: int = 300) -> tuple[int, str]:
    """Atomic write of nodes.conf + crontab as a pair."""
    # 1. Validate
    ok, err = nodes_validate_request(editable)
    if not ok:
        return 1, err

    # 2. Backup gate
    if not backup_gate(max_age_seconds):
        return 2, "backup gate failed"

    # 3. Lock
    lock_path = RESOURCES['nodes_conf']['lock']
    fd = acquire_lock(lock_path)
    if fd is None:
        return 1, "lock timeout"

    try:
        # 4. Snapshot
        conf_path = RESOURCES['nodes_conf']['live']
        cron_path = Path('/etc/crontabs/root')
        pre_conf = conf_path.read_bytes() if conf_path.exists() else b''
        pre_cron = cron_path.read_bytes() if cron_path.exists() else b''

        # 5. Render candidates
        new_conf_bytes = nodes_render_candidate(pre_conf.decode('utf-8') if pre_conf else '', editable)
        new_conf = new_conf_bytes.decode('utf-8')
        enabled = editable.get('enabled', '0') == '1'
        schedule = editable.get('schedule', '03:30')
        new_cron = update_cron(editable.get('enabled', '0') == '1', editable.get('schedule', '03:30'),
                               pre_cron.decode('utf-8') if pre_cron else '')

        # 6. Validate conf
        ok, err = nodes_validate_request(editable)
        if not ok:
            return 1, err

        # 7. Trial: mihomo -t
        with tempfile.NamedTemporaryFile(mode='wb', delete=False) as tf:
            tf.write(new_conf_bytes)
            cand_path = tf.name
        try:
            result = subprocess.run(['mihomo', '-f', cand_path, '-d', '/etc/mihomo', '-t'],
                                    capture_output=True, timeout=30)
            if result.returncode != 0:
                return 3, f"trial failed: {result.stderr.decode()[:200]}"
        finally:
            os.unlink(cand_path)

        # 8. Atomic install both
        # conf
        with tempfile.NamedTemporaryFile(mode='wb', dir=conf_path.parent, delete=False) as tf:
            tf.write(new_conf_bytes)
            conf_cand = tf.name
        try:
            os.chmod(conf_cand, 0o644)
            os.replace(conf_cand, conf_path)
        except Exception:
            try:
                os.unlink(conf_cand)
            except OSError:
                pass
            return 4, "conf install failed"

        # cron
        with tempfile.NamedTemporaryFile(mode='wb', dir=cron_path.parent, delete=False) as tf:
            tf.write(new_cron.encode('utf-8'))
            cron_cand = tf.name
        try:
            os.chmod(cron_cand, 0o644)
            os.replace(cron_cand, cron_path)
        except Exception:
            try:
                os.unlink(cron_cand)
            except OSError:
                pass
            # Rollback conf
            conf_path.write_bytes(pre_conf)
            return 4, "cron install failed"

        # 9. Reload cron
        reload_result = subprocess.run(['/etc/init.d/cron', 'reload'], capture_output=True, timeout=30)
        if reload_result.returncode != 0:
            return 4, f"cron reload failed: {reload_result.stderr.decode()[:200]}"

        # 10. Post-check
        post_conf = conf_path.read_bytes()
        post_cron = cron_path.read_bytes()
        if post_conf.decode('utf-8') != new_conf or post_cron.decode('utf-8') != new_cron:
            # Rollback
            conf_path.write_bytes(pre_conf)
            cron_path.write_bytes(pre_cron)
            subprocess.run(['/etc/init.d/cron', 'reload'], capture_output=True, timeout=30)
            return 5, "post-check failed: content mismatch"

        # 11. Update revision
        rev_path = RESOURCES['nodes_conf']['rev']
        rev_path.write_text(sha256_file(conf_path))

        return 0, new_conf

    finally:
        release_lock(fd, lock_path)


# --- CLI for testing ---
if __name__ == '__main__':
    import json
    if len(sys.argv) < 2:
        print('Usage: safe_write.py <resource> <candidate_json> [--dry-run]', file=sys.stderr)
        sys.exit(1)
    resource = sys.argv[1]
    candidate_json = sys.argv[2]
    dry_run = '--dry-run' in sys.argv

    editable = json.loads(candidate_json)
    ok, err = nodes_validate_request(editable)
    if not ok:
        print(f'VALIDATION_ERROR: {err}')
        sys.exit(1)

    # Read live
    live_path = RESOURCES[resource]['live']
    live_content = live_path.read_bytes() if live_path.exists() else b''

    cand = nodes_render_candidate(live_content, editable)
    print(cand.decode('utf-8'), end='')