#!/usr/bin/env python3
"""Per-module updater for geomask.

The router has one installer (geomask.sh) and twelve module manifests. Until now the
only update path replaced everything at once, so a one-line fix in the YouTube
watchdog forced a full reinstall. This helper updates a single module: it reads
modules/index.json from the repository, compares the SHA-256 of every file the module
owns, downloads only those files, verifies them and writes them atomically.

Safety rules, in order:
  * a file edited locally is never overwritten without --force;
  * every download is verified against the index before anything is written;
  * all files are staged first, so a failed download leaves the box untouched;
  * originals are copied to a backup directory, and a write failure restores them.

Commands:
  status                 what is installed, and what differs from the index
  check [--force]        refresh the cached index and report available updates
  update <id> [--force]  install the module's files from the index
  verify <id>            re-hash installed files against the recorded state

A module the owner did not select at install time (/etc/geomask/modules.json) is
reported as `not-selected` and never updated here: changing which modules a router
carries is the installer's job, not the updater's.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.request

DEFAULT_REPO = 'kabzon93region/geomask'
DEFAULT_REF = 'master'
DEFAULT_CONFIG = '/etc/geomask/module-update.json'
DEFAULT_STATE = '/etc/geomask/modules-state.json'
DEFAULT_SELECTION = '/etc/geomask/modules.json'
CACHE = '/tmp/geomask-modules-index.json'
# The catalogue stores modes as zero-padded octal strings ("0755"). A JSON integer 755
# passed to os.chmod is 0o1363 - a world-writable executable - and 644 is 0o1204.
SAFE_MODES = {0o755, 0o750, 0o700, 0o644, 0o640, 0o600}
MIHOMO_CONF = '/etc/mihomo/config.yaml'


def parse_mode(value, path=''):
    """Mode from the catalogue, in octal, refusing anything outside the allow-list."""
    try:
        if isinstance(value, str):
            text = value.strip()
            mode = int(text, 8) if text else 0o644
        elif isinstance(value, int):
            # legacy catalogues stored a decimal integer: 755 meant "0755" to the author
            mode = int(str(value), 8) if value in (644, 755, 600, 640, 700, 750) else value
        else:
            mode = 0o644
    except ValueError:
        raise UpdateError('invalid mode %r for %s' % (value, path or '?')) from None
    if mode not in SAFE_MODES:
        raise UpdateError('refusing mode %04o for %s' % (mode, path or '?'))
    return mode


def provider_names(conf_path=None):
    """Providers declared in the mihomo config (proxy-providers section)."""
    conf_path = conf_path or MIHOMO_CONF
    names = set()
    section = ''
    try:
        with open(conf_path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                if line[:1] not in (' ', '\t', '\n'):
                    match = re.match(r'^([A-Za-z0-9_-]+):', line)
                    section = match.group(1) if match else ''
                    continue
                if section != 'proxy-providers':
                    continue
                match = re.match(r'^  ([A-Za-z0-9_-]+):\s*$', line.rstrip('\n'))
                if match:
                    names.add(match.group(1))
    except OSError:
        return set()
    return names


def capability_ready(dependency):
    """A non-module dependency such as `subscription:sub`.

    These used to be looked up in index.modules, where they never exist, so every path
    module reported "dependencies not ready" no matter how healthy the router was.
    """
    kind, _, name = dependency.partition(':')
    if kind != 'subscription':
        return False
    providers = provider_names()
    if not providers:
        return False
    if name in providers:
        return True
    # `de5` was retired in v1.23.0: AI and the restricted lane ride `sub` now, so a config
    # that still names de5 in a manifest is satisfied by any working provider.
    return name == 'de5' and 'sub' in providers
BACKUP_ROOT = '/tmp/geomask-module-backup'
RAW = 'https://raw.githubusercontent.com/{repo}/{ref}/{path}'
INDEX_PATH = 'modules/index.json'
TTL = 3600


class UpdateError(Exception):
    """A refusal or a failure that the caller must report, not swallow."""


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


# build.py stamps these markers while assembling geomask.sh, and the deploy tool stamps
# them while deploying, so the repository keeps the placeholders while the router gets
# the stamped bytes. The catalogue hashes the stamped bytes, so a download is stamped
# before it is verified - otherwise the panel and the backend would always fail the
# checksum. tools/build_module_index.py applies the same rule on the other side.
VERSION_MARKERS = ('@@PANEL_VERSION@@', '@@BACKEND_VERSION@@', '@@GUARD_VERSION@@')


def stamp_versions(data, version):
    for marker in VERSION_MARKERS:
        data = data.replace(marker.encode(), str(version).encode())
    return data


def digest_file(path):
    try:
        with open(path, 'rb') as handle:
            return digest_bytes(handle.read())
    except OSError:
        return None


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, payload):
    tmp = path + '.tmp'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write('\n')
    os.replace(tmp, path)


def version_tuple(text):
    parts = []
    for piece in str(text or '0').split('.'):
        digits = ''.join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def load_config(path=DEFAULT_CONFIG):
    config = read_json(path, {}) or {}
    return {
        'repo': config.get('repo') or DEFAULT_REPO,
        'ref': config.get('ref') or DEFAULT_REF,
        'ttl': int(config.get('ttl') or TTL),
    }


def fetch(url, timeout=20, max_bytes=4_000_000):
    request = urllib.request.Request(url, headers={'User-Agent': 'geomask-updater'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UpdateError('response too large')
    return data


def index_url(config):
    # raw.githubusercontent.com sits behind caches that can serve a stale catalogue for
    # minutes after a push; a changing query string keeps the fetch honest.
    base = RAW.format(repo=config['repo'], ref=config['ref'], path=INDEX_PATH)
    return '%s?t=%d' % (base, int(time.time()))


def load_index(config, force=False, fetcher=None):
    """Return (index, source). Cached for ttl seconds unless force."""
    fetcher = fetcher or fetch
    age = None
    if os.path.exists(CACHE):
        age = time.time() - os.path.getmtime(CACHE)
    if not force and age is not None and age < config['ttl']:
        cached = read_json(CACHE)
        if isinstance(cached, dict) and cached.get('schema') == 1:
            return cached, 'cache'
    try:
        raw = fetcher(index_url(config))
        index = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        cached = read_json(CACHE)
        if isinstance(cached, dict) and cached.get('schema') == 1:
            return cached, 'stale-cache'
        raise UpdateError('index unavailable (%s)' % type(exc).__name__) from None
    if not isinstance(index, dict) or index.get('schema') != 1:
        raise UpdateError('index has an unexpected schema')
    try:
        with open(CACHE + '.tmp', 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(index, handle, ensure_ascii=False, sort_keys=True)
        os.replace(CACHE + '.tmp', CACHE)
    except OSError:
        pass
    return index, 'network'


def load_state(path=DEFAULT_STATE):
    state = read_json(path, {}) or {}
    modules = state.get('modules')
    return state if isinstance(modules, dict) else {'schema': 1, 'modules': {}}


def load_selection(path=DEFAULT_SELECTION):
    """Modules this router is supposed to carry, or None when the file is absent.

    None means "no selection was ever recorded" - every module counts as selected, which
    is how routers installed before the selection existed keep behaving.
    """
    if not path or not os.path.exists(path):
        return None
    data = read_json(path, None)
    if not isinstance(data, dict):
        return None
    selected = data.get('selected')
    if not isinstance(selected, list):
        return None
    return [str(item) for item in selected]


def module_selected(selected, module_id):
    return True if selected is None else module_id in selected


def module_state(state, module_id):
    entry = (state.get('modules') or {}).get(module_id) or {}
    return {
        'version': entry.get('version', ''),
        'files': entry.get('files') if isinstance(entry.get('files'), dict) else {},
    }


def status(index, state, module_id=None, selected=None):
    """Compare the index with what is on disk. Never writes anything."""
    result = []
    for ident, module in sorted((index.get('modules') or {}).items()):
        if module_id and ident != module_id:
            continue
        recorded = module_state(state, ident)
        available = module.get('version', '0.0.0')
        installed = recorded['version'] or available
        chosen = module_selected(selected, ident)
        files = []
        modified = []
        missing = []
        stale = []
        for entry in module.get('files') or []:
            path = entry['path']
            actual = digest_file(path)
            if actual is None:
                missing.append(path)
            elif actual != entry['sha256']:
                recorded_hash = recorded['files'].get(path)
                if recorded_hash and actual != recorded_hash:
                    modified.append(path)
                elif recorded_hash and actual == recorded_hash:
                    # the release changed the file without bumping the version: the bytes on
                    # disk are the old release, not a local edit, so this is an update
                    stale.append(path)
                else:
                    modified.append(path)
            files.append({'path': path, 'sha256': entry['sha256'], 'installed': actual})
        if not chosen:
            state_name = 'not-selected'
        elif missing:
            state_name = 'incomplete'
        elif modified:
            state_name = 'modified'
        elif stale or version_tuple(available) > version_tuple(installed):
            state_name = 'update-available'
        else:
            state_name = 'current'
        result.append({
            'id': ident,
            'name': module.get('name', ident),
            'type': module.get('type', ''),
            'selected': chosen,
            'installed_version': installed,
            'available_version': available,
            'state': state_name,
            'files': len(files),
            'modified': modified,
            'missing': missing,
            'dependencies': module.get('dependencies') or [],
        })
    return result


def dependencies_ready(index, state, module_id):
    module = (index.get('modules') or {}).get(module_id) or {}
    problems = []
    for dependency in module.get('dependencies') or []:
        if ':' in dependency:
            if not capability_ready(dependency):
                problems.append('%s is not available on this router' % dependency)
            continue
        other = (index.get('modules') or {}).get(dependency)
        if other is None:
            problems.append('%s is not in the index' % dependency)
            continue
        installed = module_state(state, dependency)['version']
        if installed and version_tuple(installed) < version_tuple(other.get('version', '0')):
            problems.append('%s %s is older than %s'
                            % (dependency, installed, other.get('version')))
    return problems


def download_module(index, state, module_id, config, force=False, fetcher=None):
    """Stage every file in memory. Returns (files, changed) without writing."""
    fetcher = fetcher or fetch
    module = (index.get('modules') or {}).get(module_id)
    if module is None:
        raise UpdateError('module %s is not in the index' % module_id)
    problems = dependencies_ready(index, state, module_id)
    if problems:
        raise UpdateError('dependencies not ready: ' + '; '.join(problems))

    recorded = module_state(state, module_id)
    staged = []
    changed = False
    for entry in module.get('files') or []:
        path = entry['path']
        current = digest_file(path)
        if current is not None and current != entry['sha256']:
            known = recorded['files'].get(path)
            if entry.get('config') and not force:
                # an owner-edited config (nodes.conf, vpnwatch.conf, zapret lists) is kept by
                # the installer; the updater must not replace it just because the baseline
                # recorded those same bytes as "installed"
                raise UpdateError('%s is a configuration file; use --force to replace it'
                                  % path)
            if not force and current != known:
                raise UpdateError('%s was modified locally; use --force to overwrite' % path)
        url = RAW.format(repo=config['repo'], ref=config['ref'], path=entry['source'])
        data = stamp_versions(fetcher(url), index.get('core_version') or '0')
        actual = digest_bytes(data)
        if actual != entry['sha256']:
            raise UpdateError('%s: checksum mismatch (index %s, download %s)'
                              % (path, entry['sha256'][:12], actual[:12]))
        if current != actual:
            changed = True
        staged.append({'path': path, 'mode': parse_mode(entry.get('mode', '0644'), path),
                       'data': data, 'sha256': actual})
    return module, staged, changed


def backup_name(path):
    """Flatten an install path into one backup file name, on any platform."""
    return path.replace('\\', '/').lstrip('/').replace('/', '_').replace(':', '')


def install_staged(module_id, staged, state, version, state_path=DEFAULT_STATE):
    """Write staged files atomically, restoring originals if any write fails."""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = os.path.join(BACKUP_ROOT, '%s-%s' % (module_id, stamp))
    os.makedirs(backup, exist_ok=True)
    written = []
    try:
        for item in staged:
            path = item['path']
            if os.path.exists(path):
                keep = os.path.join(backup, backup_name(path))
                shutil.copy2(path, keep)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.geomask-new'
            with open(tmp, 'wb') as handle:
                handle.write(item['data'])
            os.chmod(tmp, item['mode'])
            os.replace(tmp, path)
            written.append(path)
    except OSError as exc:
        for path in written:
            keep = os.path.join(backup, backup_name(path))
            if os.path.exists(keep):
                try:
                    shutil.copy2(keep, path)
                except OSError:
                    pass
        raise UpdateError('write failed (%s); originals restored' % type(exc).__name__) from None

    modules = state.setdefault('modules', {})
    entry = modules.setdefault(module_id, {})
    entry['version'] = version or entry.get('version', '')
    entry['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    entry['files'] = {item['path']: item['sha256'] for item in staged}
    write_json(state_path, state)
    return backup


def seed_state(index, state, state_path=DEFAULT_STATE, versions=False):
    """Record what is on disk right now, so later edits are detectable.

    The installer calls this once with the index it shipped: without a baseline every
    untouched file would look locally modified on the first update.

    versions=False keeps the recorded version unless every file already matches the index.
    versions=True stamps the index version on every module whose files exist - that is
    what the installer and a deploy mean by "this release is what is on the router", and
    it must not be confused by files the release itself generates (mihomo rulesets,
    owner-edited configs) which legitimately differ from the shipped asset.
    """
    modules = state.setdefault('modules', {})
    seeded = 0
    for ident, module in (index.get('modules') or {}).items():
        hashes = {}
        expected = 0
        matched = True
        for entry in module.get('files') or []:
            expected += 1
            actual = digest_file(entry['path'])
            if actual is not None:
                hashes[entry['path']] = actual
            if actual != entry['sha256']:
                matched = False
        record = modules.setdefault(ident, {})
        if hashes:
            record['files'] = hashes
            seeded += 1
        if not expected:
            # a module without files (panel-only) ships with the release it names
            record['version'] = module.get('version', '0.0.0')
        elif versions and hashes:
            record['version'] = module.get('version', '0.0.0')
        elif matched and hashes:
            record['version'] = module.get('version', '0.0.0')
        else:
            record.setdefault('version', module.get('version', '0.0.0'))
    write_json(state_path, state)
    return seeded


def main(argv=None):
    parser = argparse.ArgumentParser(prog='geomask-module-update')
    parser.add_argument('command', choices=['status', 'check', 'update', 'verify', 'seed'])
    parser.add_argument('module', nargs='?')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--state', default=DEFAULT_STATE)
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    parser.add_argument('--selection', default=DEFAULT_SELECTION)
    parser.add_argument('--versions', action='store_true')
    parser.add_argument('--index-file', default='')
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        state = load_state(args.state)
        selected = load_selection(args.selection)
        if args.command == 'seed':
            if args.index_file:
                with open(args.index_file, encoding='utf-8') as handle:
                    index = json.load(handle)
                source = 'file'
            else:
                index, source = load_index(config)
            seeded = seed_state(index, state, args.state, versions=args.versions)
            print(json.dumps({'ok': True, 'seeded': seeded, 'source': source},
                             ensure_ascii=False))
            return 0
        if args.command == 'status':
            index, source = load_index(config)
            print(json.dumps({'ok': True, 'source': source,
                              'modules': status(index, state, selected=selected)},
                             ensure_ascii=False))
            return 0
        if args.command == 'check':
            index, source = load_index(config, force=True)
            print(json.dumps({'ok': True, 'source': source,
                              'modules': status(index, state, selected=selected)},
                             ensure_ascii=False))
            return 0
        if args.command == 'verify':
            index, source = load_index(config)
            if not args.module:
                raise UpdateError('verify needs a module id')
            report = status(index, state, args.module, selected=selected)
            if not report:
                raise UpdateError('unknown module %s' % args.module)
            print(json.dumps({'ok': report[0]['state'] in ('current', 'update-available',
                                                           'not-selected'),
                              'module': report[0]}, ensure_ascii=False))
            return 0
        if args.command == 'update':
            if not args.module:
                raise UpdateError('update needs a module id')
            if not module_selected(selected, args.module):
                raise UpdateError(
                    '%s is not part of this install (not selected in %s); '
                    'change the selection with the installer: sh geomask.sh --modules=%s'
                    % (args.module, args.selection, args.module))
            index, source = load_index(config)
            module, staged, changed = download_module(index, state, args.module, config,
                                                      force=args.force)
            if not changed:
                # A manifest-only module (files: []) never stages anything, so its version
                # used to stay at the old value and the panel offered the same update forever.
                recorded = module_state(state, args.module)['version']
                if version_tuple(module.get('version')) > version_tuple(recorded):
                    install_staged(args.module, [], state, module.get('version', ''), args.state)
                    print(json.dumps({'ok': True, 'module': args.module, 'changed': False,
                                      'version': module.get('version'), 'source': source,
                                      'metadata': True}, ensure_ascii=False))
                    return 0
                print(json.dumps({'ok': True, 'module': args.module, 'changed': False,
                                  'version': module.get('version'), 'source': source},
                                 ensure_ascii=False))
                return 0
            backup = install_staged(args.module, staged, state, module.get('version', ''), args.state)
            print(json.dumps({'ok': True, 'module': args.module, 'changed': True,
                              'version': module.get('version'), 'files': len(staged),
                              'backup': backup, 'source': source}, ensure_ascii=False))
            return 0
    except UpdateError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 1
    return 2


if __name__ == '__main__':
    sys.exit(main())
