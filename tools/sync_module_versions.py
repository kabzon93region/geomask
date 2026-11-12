#!/usr/bin/env python3
"""Derive each module's version from the release that last changed it.

The module version means: "the geomask release in which this module last changed".
For a module with files that is the GM_VERSION of the newest commit touching any of its
source files; a module without files lives in the panel and ships with the current release.

    python3 tools/sync_module_versions.py            # print the mapping
    python3 tools/sync_module_versions.py --write    # rewrite modules/*.json
    python3 tools/sync_module_versions.py --bump     # +1 patch on modules changed since HEAD~1

Run from the repository root.
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODULES_DIR = os.path.join(ROOT, 'modules')
INDEX = os.path.join(MODULES_DIR, 'index.json')


def git(*args):
    r = subprocess.run(['git', '-C', ROOT] + list(args), capture_output=True,
                       encoding='utf-8', errors='replace')
    if r.returncode:
        raise SystemExit('git %s failed: %s' % (' '.join(args), (r.stderr or '').strip()[:200]))
    return r.stdout


def installer_version(ref='HEAD'):
    try:
        text = git('show', '%s:geomask.sh.in' % ref)
    except SystemExit:
        return ''
    match = re.search(r'(?m)^GM_VERSION="([^"]+)"', text)
    return match.group(1) if match else ''


def last_commit_for(paths):
    """Newest commit (sha, unix time) that touched any of the repository paths."""
    newest = None
    for path in paths:
        out = git('log', '-1', '--format=%H %ct', '--', path).strip()
        if not out:
            continue
        sha, stamp = out.split()
        if newest is None or int(stamp) > newest[1]:
            newest = (sha, int(stamp))
    return newest


def version_at(sha):
    """GM_VERSION as it was in that commit."""
    try:
        text = git('show', '%s:geomask.sh.in' % sha)
    except SystemExit:
        return ''
    match = re.search(r'(?m)^GM_VERSION="([^"]+)"', text)
    return match.group(1) if match else ''


def changed_since(ref):
    out = git('diff', '--name-only', ref, 'HEAD').splitlines()
    return {line.strip() for line in out if line.strip()}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='sync-module-versions')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--bump', action='store_true')
    parser.add_argument('--since', default='HEAD~1')
    args = parser.parse_args(argv)

    index = json.load(open(INDEX, encoding='utf-8'))
    current = installer_version()
    changed = changed_since(args.since) if args.bump else set()
    rows = []
    for name in sorted(os.listdir(MODULES_DIR)):
        if not name.endswith('.json') or name == 'index.json':
            continue
        path = os.path.join(MODULES_DIR, name)
        manifest = json.load(open(path, encoding='utf-8'))
        module_id = manifest['id']
        files = [entry['source'] for entry in
                 (index.get('modules', {}).get(module_id, {}).get('files') or [])]
        if not files:
            version = current
            basis = 'панель, текущий релиз'
        else:
            commit = last_commit_for(files)
            version = version_at(commit[0]) if commit else current
            basis = commit[0][:8] if commit else 'нет истории'
        if args.bump and files and changed.intersection(files):
            parts = [int(p) for p in re.findall(r'\d+', version or '0')] or [0]
            while len(parts) < 3:
                parts.append(0)
            parts[2] += 1
            version = '.'.join(str(p) for p in parts[:3])
            basis += ' (bump)'
        rows.append((module_id, manifest.get('version', '0.0.0'), version, basis))
        if args.write and version and manifest.get('version') != version:
            manifest['version'] = version
            with open(path, 'w', encoding='utf-8', newline='\n') as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.write('\n')

    width = max(len(r[0]) for r in rows) + 2
    for module_id, old, new, basis in rows:
        mark = '→' if old != new else ' '
        print('%-*s %-8s %s %-8s %s' % (width, module_id, old, mark, new, basis))
    if not args.write:
        print('\n(dry run; --write to apply)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
