#!/usr/bin/env python3
"""Point module manifests at the files the installer actually writes.

The manifests were written before the install layout was final, so most paths in
owned_resources did not exist on a real box and every file fell back to core.
This maps the real paths to the module that should own them, keeping any
non-file resources (cron, filter, service) untouched.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = os.path.join(ROOT, 'modules')

OWNERSHIP = {
    'youtube': [
        ('/usr/bin/geomask-ytwatch.sh', 'stalled-edge watchdog'),
        ('/etc/geomask-ytwatch.conf', 'watchdog configuration'),
        ('/etc/mihomo/ruleset/ytvpn.yaml', 'domains pinned to the VPN lane'),
        ('/etc/mihomo/ruleset/ytdirect.yaml', 'domains served directly'),
    ],
    'torrent': [
        ('/usr/bin/geomask-torrent.sh', 'listener and ruleset manager'),
        ('/etc/mihomo/ruleset/torrentdirect.yaml', 'tracker and peer ruleset'),
    ],
    'diagnostic': [
        ('/usr/bin/geomask-dnscheck.sh', 'DNS safety audit'),
        ('/usr/bin/geomask-morning-check.sh', 'pre-torrent check'),
        ('/usr/bin/geomask-vpnwatch.sh', 'P2P attempt watcher'),
        ('/etc/geomask/vpnwatch.conf', 'watcher configuration'),
    ],
}


def main():
    for module_id, files in OWNERSHIP.items():
        path = os.path.join(MODULES, module_id + '.json')
        with open(path, encoding='utf-8') as handle:
            manifest = json.load(handle)
        resources = [r for r in manifest.get('owned_resources') or []
                     if r.get('type') != 'file']
        for install_path, description in files:
            resources.append({'type': 'file', 'path': install_path, 'description': description})
        manifest['owned_resources'] = resources
        with open(path, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
            handle.write('\n')
        print('updated %s: %d file resources' % (module_id, len(files)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
