#!/usr/bin/env python3
"""Build modules/index.json - the machine-readable catalogue for per-module updates.

The router cannot parse the monolithic installer, so the catalogue must be derived
here: it maps every install path to the repository file that produces it, with the
mode and the SHA-256 the router will verify after downloading.

Sources of truth, none duplicated:
  geomask.sh.in   save_stream <path> <mode> + @@ASSET:<name>@@   (install layout)
  build.py        MAP: asset name -> repository file              (asset origins)
  modules/*.json  owned_resources[type=file]                      (module ownership)

Run from the repository root:  python3 tools/build_module_index.py
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INSTALLER = os.path.join(ROOT, 'geomask.sh.in')
BUILD = os.path.join(ROOT, 'build.py')
MODULES_DIR = os.path.join(ROOT, 'modules')
OUT = os.path.join(MODULES_DIR, 'index.json')

STREAM = re.compile(
    r"save_stream\s+(/\S+)\s+(\d{3,4})\s+<<'ASSET_EOF'\s*\n\s*@@ASSET:([^@]+)@@\s*\n\s*ASSET_EOF")
MAP_LINE = re.compile(r'^\s*"([^"]+)"\s*:\s*"([^"]+)"\s*,?\s*$')


def asset_map():
    """Parse build.py's MAP without importing it (importing would run a build)."""
    mapping = {}
    inside = False
    with open(BUILD, encoding='utf-8') as handle:
        for line in handle:
            if line.startswith('MAP = {'):
                inside = True
                continue
            if inside and line.startswith('}'):
                break
            if inside:
                match = MAP_LINE.match(line)
                if match:
                    mapping[match.group(1)] = match.group(2)
    return mapping


def install_layout():
    """install path -> (mode, asset name) as the installer writes them."""
    with open(INSTALLER, encoding='utf-8') as handle:
        text = handle.read()
    layout = {}
    for path, mode, asset in STREAM.findall(text):
        layout[path] = ('%04o' % int(mode, 8), asset.strip())
    return layout


def source_path(repo_file):
    """Where the repository keeps the file that becomes an asset."""
    for candidate in (os.path.join(ROOT, 'assets', repo_file),
                      os.path.join(ROOT, repo_file),
                      os.path.join(ROOT, 'tools', repo_file),
                      os.path.join(ROOT, 'modules', repo_file)):
        if os.path.isfile(candidate):
            return candidate
    return None


# build.py stamps these markers while assembling geomask.sh, and scripts/router/
# deploy_luci_fix.py stamps them while deploying, so the bytes that land on the router
# are NOT the bytes in the repository. The catalogue hashes what the router gets -
# otherwise the panel and the backend would look locally modified after every install.
VERSION_MARKERS = ('@@PANEL_VERSION@@', '@@BACKEND_VERSION@@', '@@GUARD_VERSION@@')

# Files the installer deliberately keeps when they already exist (owner settings, not code).
# The updater needs the same rule, or a reinstall + core update silently resets them.
CONFIG_PATHS = (
    '/etc/geomask-nodes.conf',
    '/etc/geomask/vpnwatch.conf',
    '/etc/geomask-ytwatch.conf',
    '/etc/geomask/zapret-force-keep.txt',
    '/etc/geomask/zapret-always-exclude.txt',
)

# Files the release does not own the bytes of: sync/scripts rewrite them, and a difference
# from the shipped asset is normal, not a pending update.
GENERATED_PREFIXES = ('/etc/mihomo/ruleset/',)


def sha256_installed(path, version):
    """SHA-256 of the file as the installer writes it (markers stamped, LF line endings).

    build.py reads every asset as text (universal newlines) and the heredoc adds one final
    newline, so a CRLF file in the working tree is installed with LF. Hashing the raw bytes
    made such a file differ from its own catalogue entry by one byte per line, and every
    module then looked "изменён локально" after an official update.
    """
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    data = (text.rstrip('\n') + '\n').encode('utf-8')
    for marker in VERSION_MARKERS:
        data = data.replace(marker.encode(), version.encode())
    return hashlib.sha256(data).hexdigest()


def load_modules():
    modules = {}
    for name in sorted(os.listdir(MODULES_DIR)):
        if not name.endswith('.json') or name == 'index.json':
            continue
        with open(os.path.join(MODULES_DIR, name), encoding='utf-8') as handle:
            manifest = json.load(handle)
        modules[manifest['id']] = manifest
    return modules


def owners(modules):
    """install path -> module id, from owned_resources."""
    result = {}
    for module_id, manifest in modules.items():
        for resource in manifest.get('owned_resources') or []:
            if resource.get('type') == 'file' and str(resource.get('path', '')).startswith('/'):
                result.setdefault(resource['path'], module_id)
    return result


def core_version():
    with open(INSTALLER, encoding='utf-8') as handle:
        match = re.search(r'^GM_VERSION="([^"]+)"', handle.read(), re.M)
    return match.group(1) if match else ''


def main():
    layout = install_layout()
    assets = asset_map()
    modules = load_modules()
    owner = owners(modules)
    version = core_version()

    catalogue = {}
    problems = []
    for path, (mode, asset) in sorted(layout.items()):
        if path.endswith('/modules-index.json'):
            # The catalogue cannot describe itself: its hash changes with every build.
            continue
        if path.startswith('/tmp/'):
            # Transient staging file, not a module artifact.
            continue
        if path.endswith('/geomask-module-update.py'):
            # The updater cannot describe itself: it is replaced by the installer, and
            # a manual redeploy would make core look locally modified forever.
            continue
        # Files nobody claims belong to core: the panel, backend and base layout are
        # core infrastructure, and leaving them out would make them un-updatable.
        module_id = owner.get(path, 'core')
        repo_file = assets.get(asset)
        if repo_file is None:
            problems.append('%s: asset %s is not in build.py MAP' % (path, asset))
            continue
        source = source_path(repo_file)
        if source is None:
            problems.append('%s: repository file %s missing' % (path, repo_file))
            continue
        entry = catalogue.setdefault(module_id, {'files': [], 'restart': []})
        record = {
            'path': path,
            'source': os.path.relpath(source, ROOT).replace(os.sep, '/'),
            'mode': mode,
            'sha256': sha256_installed(source, version),
        }
        if path in CONFIG_PATHS:
            record['config'] = True
        elif path.startswith(GENERATED_PREFIXES):
            record['generated'] = True
        entry['files'].append(record)

    index = {
        'schema': 1,
        'core_version': core_version(),
        'modules': {},
    }
    for module_id in sorted(modules):
        manifest = modules[module_id]
        files = catalogue.get(module_id, {}).get('files') or []
        index['modules'][module_id] = {
            'version': manifest.get('version', '0.0.0'),
            'type': manifest.get('type', ''),
            'name': manifest.get('name', module_id),
            'dependencies': [d.get('module_id') for d in manifest.get('dependencies') or []
                             if d.get('required')],
            'files': sorted(files, key=lambda item: item['path']),
        }

    body = json.dumps(index, indent=2, ensure_ascii=False, sort_keys=False) + '\n'
    with open(OUT, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(body)

    covered = sum(len(module['files']) for module in index['modules'].values())
    print('wrote %s: %d modules, %d files, core %s'
          % (os.path.relpath(OUT, ROOT), len(index['modules']), covered, index['core_version']))
    for module_id in sorted(index['modules']):
        count = len(index['modules'][module_id]['files'])
        if count == 0:
            print('  note: module %s owns no updatable files' % module_id)
    for problem in problems:
        print('  WARN ' + problem)
    return 0


if __name__ == '__main__':
    sys.exit(main())
