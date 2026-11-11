#!/usr/bin/env python3
"""Optional agent-side backup before changing audit settings over SSH.
LuCI saves autonomously; this tool is not a runtime prerequisite.
--prepare-only only creates/verifies a full PC backup, without issuing receipts.
Never applies desired filters to mihomo; never uploads subscription secrets.
"""
import argparse
import datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time

MODULE = '/usr/lib/geomask/geomask-node-audit.py'

def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=180, **kwargs)
    if result.returncode:
        raise RuntimeError('command failed: ' + Path(args[0]).name + '; no settings mutation authorized')
    return result.stdout

def archive_check(path, require_store=False):
    # Full decompression proves gzip CRC, not only a readable tar header.
    with gzip.open(path, 'rb') as stream:
        while stream.read(1024 * 1024):
            pass
    with tarfile.open(path, 'r:gz') as archive:
        members = set(name.lstrip('./') for name in archive.getnames())
        required = {'etc/config/network', 'etc/config/firewall', 'etc/mihomo/config.yaml'}
        if require_store:
            required.add('etc/geomask/node-audit.json')
        if not required <= members:
            raise RuntimeError('backup missing critical settings')
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', choices=['vpn', 'vpn_nop2p', 'ai'])
    parser.add_argument('countries')
    parser.add_argument('--router', default='root@192.168.0.1')
    parser.add_argument('--key', default=str(Path.home() / '.ssh/openwrt_final'))
    parser.add_argument('--backup-dir', default='Y:/openwrt/backup')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Z]{2}(?:,[A-Z]{2})*', args.countries):
        raise RuntimeError('countries must be uppercase ISO-2 tokens')
    ssh = ['ssh', '-i', args.key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', args.router]
    def remote(command, **kwargs):
        return run(ssh + [command], **kwargs)
    settings = json.loads(remote('python3 ' + MODULE + ' get'))
    if not settings.get('ok'):
        raise RuntimeError('cannot read audit settings')
    command = 'python3 ' + MODULE + ' prepare-save ' + shlex.quote(args.path) + ' ' + shlex.quote(args.countries) + ' ' + settings['revision']
    prepared = json.loads(remote(command))
    if not prepared.get('ok'):
        raise RuntimeError('candidate validation failed')
    if not prepared['changed']:
        print(json.dumps({'ok': True, 'changed': False, 'applied': False}))
        return
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    basename = 'router-backup-' + stamp + '.tar.gz'
    remote_path = '/tmp/' + basename
    local = Path(args.backup_dir).resolve() / basename
    local.parent.mkdir(parents=True, exist_ok=True)
    if local.exists():
        raise RuntimeError('refusing to overwrite backup')
    print('[1/4] Standard sysupgrade settings backup', flush=True)
    remote('sysupgrade -b ' + remote_path)
    print('[2/4] Download and verify complete archive on PC', flush=True)
    run(['scp', '-i', args.key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', args.router + ':' + remote_path, str(local)])
    local_hash = archive_check(local, require_store=settings['revision'] != hashlib.sha256(b'').hexdigest())
    expected = remote('sha256sum ' + remote_path).split()[0]
    if local_hash != expected:
        raise RuntimeError('backup checksum mismatch; router archive retained')
    # Recheck candidate/boot after potentially long backup, before authorizing one write.
    current = json.loads(remote(command))
    if current != prepared:
        raise RuntimeError('candidate changed while backup was collected; retry')
    print('[3/4] Full PC backup verified; no runtime receipt is needed', flush=True)
    if args.prepare_only:
        print('Backup completed. LuCI Save is autonomous and has no PC dependency.')
    else:
        result = json.loads(remote('python3 ' + MODULE + ' save ' + shlex.quote(args.path) + ' ' + shlex.quote(args.countries) + ' ' + settings['revision']))
        if not result.get('ok'):
            raise RuntimeError('save rejected; reload current settings and inspect validation/revision')
        print(json.dumps(result))
    # Literal generated /tmp basename is checked, and the PC archive verified first.
    if not re.fullmatch(r'/tmp/router-backup-[0-9-]+\.tar\.gz', remote_path):
        raise RuntimeError('unexpected cleanup target')
    remote('rm -f ' + remote_path)
    print('[4/4] PC backup retained: ' + str(local) + '; SHA256=' + local_hash, flush=True)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        sys.exit(1)
