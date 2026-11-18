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
  * originals are copied to a backup directory, and a write failure restores them;
  * one install runs at a time: a lock serialises updaters, and the caller's state is
    updated only after the state file has been written.

Commands:
  status                 what is installed, and what differs from the index (cache only)
  check [--force]        refresh the cached index and report available updates
  update <id> [--force]  install the module's files from the index
  verify <id>            re-hash installed files against the recorded state

A module the owner did not select at install time (/etc/geomask/modules.json) is
reported as `not-selected` and never updated here: changing which modules a router
carries is the installer's job, not the updater's.
"""
import argparse
import contextlib
import copy
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
import time
import urllib.request

try:
    import fcntl
except ImportError:  # not on Windows: the router runs Linux, the test host may not
    fcntl = None

DEFAULT_REPO = 'kabzon93region/geomask'
DEFAULT_REF = 'master'
DEFAULT_CONFIG = '/etc/geomask/module-update.json'
DEFAULT_STATE = '/etc/geomask/modules-state.json'
DEFAULT_SELECTION = '/etc/geomask/modules.json'
# The cached catalogue decides which paths a root-owned update writes. In /tmp any local
# user could plant one (audit U01), so it lives in a private root-owned directory and is
# rejected when a link, a foreign owner or a writable mode shows it may have been replaced.
CACHE_ROOT = '/etc/geomask/cache'
CACHE = os.path.join(CACHE_ROOT, 'modules-index.json')
# Destinations the release is allowed to own. A catalogue - even a substituted one - cannot
# authorise a write anywhere else, so a compromised cache cannot reach an arbitrary path.
INSTALL_ROOTS = (
    '/etc/geomask/',
    '/etc/geomask-nodes.conf',
    '/etc/geomask-ytwatch.conf',
    '/etc/hotplug.d/',
    '/etc/init.d/',
    '/etc/mihomo/',
    '/root/sync_mtrickle_mihomo.sh',
    '/usr/bin/',
    '/usr/lib/',
    '/usr/libexec/',
    '/usr/share/',
    '/www/luci-static/',
)
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
LOCK_PATH = '/tmp/geomask-module-update.lock'
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


def _normalized(path):
    """The canonical form of a catalogue destination, on the host's own rules."""
    if path.startswith('/'):
        return posixpath.normpath(path)
    return os.path.normpath(path)


def destination_problem(path):
    """Why a catalogue destination is outside what the release may own, or ''.

    The catalogue is the only source of destination paths, so it also decides where root
    writes land: a cache that names `/tmp/x` used to be executed as-is. The shipped policy
    is a fixed list of roots; anything else, a relative path or a `..` detour is refused
    before the first write.
    """
    if not isinstance(path, str) or not path:
        return 'it is not a path'
    absolute = path.startswith('/') or (os.name == 'nt' and len(path) > 2 and path[1] == ':')
    if not absolute:
        return 'it is not an absolute path'
    if _normalized(path) != path:
        return 'the path is not normalized'
    for root in INSTALL_ROOTS:
        if root.endswith(('/', os.sep)):
            if path.startswith(root) and len(path) > len(root):
                return ''
        elif path == root:
            return ''
    return 'it is outside the shipped install roots'


def _first_symlink(path):
    """The first existing component of `path` that is a symlink, or ''.

    Writing through a symlink replaces the linked file and keeps the link, so the previous
    rollback "restored originals" while a foreign file had been overwritten (audit U03).
    """
    current = os.path.abspath(path)
    chain = []
    while True:
        chain.append(current)
        head, tail = os.path.split(current)
        if not tail or head == current:
            break
        current = head
    for candidate in reversed(chain):
        try:
            if os.path.islink(candidate):
                return candidate
        except OSError:
            continue
    return ''


def _unsafe_owner_or_mode(st, what):
    """POSIX ownership problems; Windows has no comparable model and is skipped."""
    if os.name != 'posix' or not hasattr(os, 'geteuid'):
        return ''
    euid = os.geteuid()
    if st.st_uid != euid:
        return '%s is owned by uid %d, not %d' % (what, st.st_uid, euid)
    if st.st_mode & 0o022:
        return '%s is writable by group or others' % what
    return ''


def cache_problem(path=None):
    """Why the catalogue cache may not be trusted, or '' when it may be read and written."""
    path = path or CACHE
    root = os.path.abspath(CACHE_ROOT)
    target = os.path.abspath(path)
    if not (target == root or target.startswith(root + os.sep)):
        return 'it is outside the private cache directory %s' % root
    # The directory decides who may replace the file, so it is part of the trust boundary: a
    # symlinked or writable CACHE_ROOT makes the file check above moot (TOCTOU).
    if os.path.islink(root):
        return 'the cache directory %s is a symlink' % root
    if os.path.exists(root):
        if not os.path.isdir(root):
            return 'the cache directory %s is not a directory' % root
        reason = _unsafe_owner_or_mode(os.stat(root), 'the cache directory %s' % root)
        if reason:
            return reason
    if os.path.islink(target):
        return 'it is a symlink'
    if os.path.exists(target):
        if not os.path.isfile(target):
            return 'it is not a regular file'
        reason = _unsafe_owner_or_mode(os.stat(target), 'the cache file')
        if reason:
            return reason
    directory = os.path.dirname(target) or '.'
    while directory and directory != root:
        if os.path.islink(directory):
            return 'the cache directory %s is a symlink' % directory
        if os.path.isdir(directory):
            reason = _unsafe_owner_or_mode(os.stat(directory),
                                           'the cache directory %s' % directory)
            if reason:
                return reason
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return ''


def write_cache(index):
    """Publish the catalogue under an unpredictable name in the private directory."""
    os.makedirs(CACHE_ROOT, mode=0o700, exist_ok=True)
    directory = os.path.dirname(CACHE) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.modules-index-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(index, handle, ensure_ascii=False, sort_keys=True)
            handle.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, CACHE)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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


def load_index(config, force=False, fetcher=None, offline=False):
    """Return (index, source). Cached for ttl seconds unless force.

    offline=True never touches the network: the cached catalogue is all that is read, and
    an empty cache is a refusal. The panel polls status continuously, and a dead uplink
    used to turn every poll into a fresh download attempt.
    """
    fetcher = fetcher or fetch
    problem = cache_problem(CACHE)
    cached = None if problem else read_json(CACHE)
    usable = isinstance(cached, dict) and cached.get('schema') == 1
    age = None
    if usable and os.path.exists(CACHE):
        age = time.time() - os.path.getmtime(CACHE)
    if usable and not force and age is not None and age < config['ttl']:
        return cached, 'cache'
    if offline:
        if problem:
            raise UpdateError('index cache is not trusted (%s)' % problem)
        if usable:
            return cached, 'stale-cache'
        raise UpdateError('index cache is empty')
    if problem:
        # Refuse instead of overwriting: an untrusted cache is evidence of interference,
        # and writing over it would destroy the very object a review needs.
        raise UpdateError('index cache is not trusted (%s); remove it or fix its owner and mode'
                          % problem)
    try:
        raw = fetcher(index_url(config))
        index = json.loads(raw.decode('utf-8'))
    except Exception as exc:
        if usable:
            return cached, 'stale-cache'
        raise UpdateError('index unavailable (%s)' % type(exc).__name__) from None
    if not isinstance(index, dict) or index.get('schema') != 1:
        raise UpdateError('index has an unexpected schema')
    try:
        write_cache(index)
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
            elif actual != entry['sha256'] and not (entry.get('config')
                                                    or entry.get('generated')):
                # configs and sync-generated rulesets are expected to differ from the asset
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
        if not installed:
            # an entry without a version is not an install. Comparing '' with the index
            # skipped the version check, so a dependent module updated over a missing base.
            problems.append('%s is not installed' % dependency)
            continue
        missing = [entry['path'] for entry in other.get('files') or []
                   if digest_file(entry['path']) is None]
        if missing:
            problems.append('%s is missing %d file(s), including %s'
                            % (dependency, len(missing), missing[0]))
            continue
        if version_tuple(installed) < version_tuple(other.get('version', '0')):
            problems.append('%s %s is older than %s'
                            % (dependency, installed, other.get('version')))
    return problems


def download_module(index, state, module_id, config, force=False, fetcher=None,
                    skipped=None):
    """Stage every file in memory. Returns (module, files, changed) without writing.

    Files the release does not own the bytes of (owner configs, sync-generated rulesets)
    are left alone and reported through `skipped`: aborting the whole module because one
    config differs made every core update impossible without --force.
    """
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
        problem = destination_problem(path)
        if problem:
            raise UpdateError('%s: %s' % (path, problem))
        link = _first_symlink(path)
        if link:
            raise UpdateError('%s: a symlink is on the install path (%s); '
                              'refusing to write through it' % (path, link))
        if os.path.exists(path) and not os.path.isfile(path):
            raise UpdateError('%s: the destination is not a regular file' % path)
        current = digest_file(path)
        if current is not None and current != entry['sha256']:
            known = recorded['files'].get(path)
            if (entry.get('config') or entry.get('generated')) and not force:
                # an owner-edited config (nodes.conf, vpnwatch.conf, zapret lists) or a
                # ruleset the sync rewrites is kept as-is; the rest of the module still
                # updates. --force is the explicit way to replace it.
                if skipped is not None:
                    skipped.append(path)
                continue
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
                       'data': data, 'sha256': actual, 'before': current})
    return module, staged, changed


def backup_name(path):
    """Flatten an install path into one backup file name, on any platform."""
    return path.replace('\\', '/').lstrip('/').replace('/', '_').replace(':', '')


def rollback_files(written, backup):
    """Put the tree back the way it was; returns a list of problems, empty on success.

    `written` holds (path, existed_before). A file that did not exist has no backup copy,
    so restoring "originals" means removing it - the previous version kept it and still
    claimed that the originals were restored.
    """
    problems = []
    for path, existed in reversed(written):
        if existed:
            keep = os.path.join(backup, backup_name(path))
            try:
                shutil.copy2(keep, path)
            except OSError:
                problems.append('could not restore %s (copy kept in %s)' % (path, keep))
        else:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                problems.append('could not remove new file %s' % path)
    return problems


# One real flock per process: the command takes the lock before it reads the state and
# install_staged takes it again for the write. A second flock on a fresh descriptor would block
# on its own lock, so the nested acquisition is counted instead.
_LOCK_DEPTH = {}


@contextlib.contextmanager
def install_lock(path=None):
    """Serialise an update from its first read to its last write; no-op without fcntl.

    Two updaters launching together used to share one backup directory and one temp file
    name, so the slower process could replace the other's bytes mid-write. The router is
    Linux, where flock is cheap; only the Windows test host takes the fallback.

    The lock is re-entrant within one process: `run_command` holds it for the whole
    read-modify-write cycle and `install_staged` re-enters it for the commit.
    """
    path = path or LOCK_PATH
    if fcntl is None:
        yield
        return
    if _LOCK_DEPTH.get(path):
        _LOCK_DEPTH[path] += 1
        try:
            yield
        finally:
            _LOCK_DEPTH[path] -= 1
        return
    try:
        handle = open(path, 'a+')
    except OSError as exc:
        raise UpdateError('could not open lock %s (%s)'
                          % (path, type(exc).__name__)) from None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        handle.close()
        raise UpdateError('could not lock %s (%s)'
                          % (path, type(exc).__name__)) from None
    _LOCK_DEPTH[path] = 1
    try:
        yield
    finally:
        _LOCK_DEPTH.pop(path, None)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass    # closing the descriptor releases the lock anyway
        finally:
            handle.close()


def preflight_staged(staged, force=False):
    """Refuse a commit whose world changed after staging, before the first write.

    The download step compares the file with the catalogue, but the catalogue was fetched
    over the network and the owner may edit the file in between: the commit then overwrote
    bytes the catalogue never saw (audit U02). Everything is re-checked under the write
    lock, and a symlink or a non-regular destination is refused rather than dereferenced.
    """
    seen = set()
    for item in staged:
        path = item['path']
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            raise UpdateError('duplicate destination path in the index: %s' % path)
        seen.add(key)
        problem = destination_problem(path)
        if problem:
            raise UpdateError('%s: %s' % (path, problem))
        link = _first_symlink(path)
        if link:
            raise UpdateError('%s: a symlink is on the install path (%s); '
                              'refusing to write through it' % (path, link))
        if os.path.exists(path) and not os.path.isfile(path):
            raise UpdateError('%s: the destination is not a regular file' % path)
        before = item.get('before')
        if not force:
            now = digest_file(path)
            # Re-committing bytes that are already installed is harmless; anything else
            # means the file moved under us after staging - an owner edit, a deletion or a
            # file created at a path that did not exist yet - and must not be overwritten
            # from a stale snapshot. `before` is None for a path that did not exist then, so
            # the comparison is `now != before` for that case too.
            if now != before and now != item['sha256']:
                raise UpdateError('%s changed while the update was staged; '
                                  're-run the update' % path)


def install_staged(module_id, staged, state, version, state_path=DEFAULT_STATE, force=False):
    """Write staged files atomically, restoring originals if any write fails.

    The state file is part of the same transaction: writing it outside the rollback block
    left new files on disk with the old state, which then reported them as locally modified
    and blocked every later update. The caller's dict is a copy until the state file has
    been written, so a failed install leaves memory and disk agreeing.
    """
    # Mutate a copy: the caller must not learn about an install that never committed.
    updated = copy.deepcopy(state)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    with install_lock():
        preflight_staged(staged, force=force)
        backup = ''
        written = []
        temps = []
        try:
            os.makedirs(BACKUP_ROOT, exist_ok=True)
            # mkdtemp keeps the backup name unpredictable: a fixed module-stamp directory
            # let two installs of one module in the same second overwrite each other.
            backup = tempfile.mkdtemp(prefix='%s-%s-%d-' % (module_id, stamp, os.getpid()),
                                      dir=BACKUP_ROOT)
            for item in staged:
                path = item['path']
                existed = os.path.exists(path)
                if existed:
                    keep = os.path.join(backup, backup_name(path))
                    shutil.copy2(path, keep)
                directory = os.path.dirname(path) or '.'
                os.makedirs(directory, exist_ok=True)
                # mkstemp writes the bytes under an unguessable name next to the target;
                # path + '.geomask-new' was predictable and shared between processes.
                fd, tmp = tempfile.mkstemp(prefix='.geomask-new-', dir=directory)
                temps.append(tmp)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(item['data'])
                os.chmod(tmp, item['mode'])
                os.replace(tmp, path)
                written.append((path, existed))

            modules = updated.setdefault('modules', {})
            entry = modules.setdefault(module_id, {})
            entry['version'] = version or entry.get('version', '')
            entry['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            # merge, never replace: a skipped config keeps the baseline that makes a later
            # local edit detectable
            entry.setdefault('files', {}).update({item['path']: item['sha256']
                                                  for item in staged})
            write_json(state_path, updated)
        except (OSError, ValueError) as exc:
            for tmp in temps:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            problems = rollback_files(written, backup)
            if problems:
                detail = 'rollback incomplete: ' + '; '.join(problems)
            else:
                detail = 'originals restored'
            raise UpdateError('write failed (%s); %s' % (type(exc).__name__, detail)) from None
        if isinstance(state, dict):
            state.clear()
            state.update(updated)
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


def run_command(args):
    """One parsed command; the caller holds the update lock for state-changing ones."""
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
        # status is the panel's poll: it reports the cached catalogue and never waits
        # for the uplink. `check` is the command that refreshes the cache.
        index, source = load_index(config, offline=True)
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
        ok = report[0]['state'] in ('current', 'update-available', 'not-selected')
        print(json.dumps({'ok': ok, 'module': report[0]}, ensure_ascii=False))
        # a JSON {"ok": false} used to exit 0, so callers that check the code saw a
        # clean verification of a module whose files are missing or edited
        return 0 if ok else 1
    if args.command == 'update':
        if not args.module:
            raise UpdateError('update needs a module id')
        if not module_selected(selected, args.module):
            raise UpdateError(
                '%s is not part of this install (not selected in %s); '
                'change the selection with the installer: sh geomask.sh --modules=%s'
                % (args.module, args.selection, args.module))
        index, source = load_index(config)
        skipped = []
        module, staged, changed = download_module(index, state, args.module, config,
                                                  force=args.force, skipped=skipped)
        if not changed:
            # A manifest-only module (files: []) never stages anything, so its version
            # used to stay at the old value and the panel offered the same update forever.
            recorded = module_state(state, args.module)['version']
            if version_tuple(module.get('version')) > version_tuple(recorded):
                install_staged(args.module, [], state, module.get('version', ''), args.state,
                               force=args.force)
                print(json.dumps({'ok': True, 'module': args.module, 'changed': False,
                                  'version': module.get('version'), 'source': source,
                                  'metadata': True}, ensure_ascii=False))
                return 0
            print(json.dumps({'ok': True, 'module': args.module, 'changed': False,
                              'version': module.get('version'), 'source': source,
                              'skipped': skipped}, ensure_ascii=False))
            return 0
        backup = install_staged(args.module, staged, state, module.get('version', ''),
                                args.state, force=args.force)
        print(json.dumps({'ok': True, 'module': args.module, 'changed': True,
                          'version': module.get('version'), 'files': len(staged),
                          'backup': backup, 'source': source,
                          'skipped': skipped}, ensure_ascii=False))
        return 0
    return 2


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
        # The state file is a read-modify-write record. Taking the lock only in install_staged
        # left the read outside it: two updaters computed from one baseline and the slower one
        # wrote its state over the faster one's install (review 15.11, #25). Writing commands
        # take the lock BEFORE their first read; read-only ones never take it, so the panel's
        # poll cannot queue behind a long download.
        if args.command in ('seed', 'update'):
            with install_lock():
                return run_command(args)
        return run_command(args)
    except UpdateError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
