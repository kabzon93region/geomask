#!/usr/bin/env python3
"""Registered diagnostic downloads: private spool, retention, quota and share copies.

Linux uses pinned, no-follow directory descriptors. Windows tests exercise content,
identity, quota, retention and share-copy algorithms - not Linux kernel, 0700, flock(2),
fsync, fs-error or ksmbd guarantees. No fallback exporter may reopen a verified archive
by pathname.
"""
import base64
import errno
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # Windows test hosts have no flock(2); see Locked.
    fcntl = None

EUID = os.geteuid() if hasattr(os, 'geteuid') else 0
PREFIX = 'geomask-console-diag-'
SUFFIX = '.tar.gz'
SIDECAR_LIMIT = 65536
# Fixed creation/registration bound, independent of configurable get --max transport cap.
# Larger archives are refused and removed by the collector before any SMB copy.
CREATION_MAXIMUM = 2097152
MAXIMUM = CREATION_MAXIMUM  # Default transport limit; get --max may be lower or higher.
SCHEMA = 1
POSIX = os.name == 'posix'

# Retention/quota defaults (R08). The shell clamps its environment into these ranges and
# passes the result on the command line; the helper only rejects impossible values, so the
# algorithm stays testable with tiny fixtures.
QUOTA = 8 * 1024 * 1024
FILES = 4
SHARE_QUOTA = 6 * 1024 * 1024
SHARE_FILES = 3
KEEP_HOURS = 24
KEEP_MAXIMUM = 720
QUOTA_MINIMUM = 2 * 1024 * 1024
QUOTA_MAXIMUM = 64 * 1024 * 1024
FILES_MAXIMUM = 32
SHARE_FILES_MAXIMUM = 16
# Age windows in seconds.
GRACE = 120            # a just-created bundle is not a candidate for eviction
ORPHAN_AGE = 300       # sidecar whose archive is gone
TEMPORARY_AGE = 3600   # .sidecar-<hex> / .part-<hex> left by a killed writer
STAGE_AGE = 7200       # geomask-console-diag.XXXXXX staging directory
STAGE_PATTERN = re.compile(r'^geomask-console-diag\.[A-Za-z0-9]{6}$')
TEMPORARY_PREFIXES = ('.sidecar-', '.part-')
PROTECTED_PREFIXES = ('router-backup-',)
COPY_CHUNK = 262144
_LOCK = threading.RLock()


class Refused(Exception):
    """Fail closed with a JSON error."""


class NotPrivate(Refused):
    """Refusal that is a permission policy decision, not a missing target."""


class ShareRefused(Exception):
    """A share copy that did not happen; the caller only needs the machine reason."""

    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


def refuse(message):
    raise Refused(message)


def identity(st):
    return st.st_dev, st.st_ino


def signature(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_uid, st.st_nlink,
            st.st_mtime_ns, st.st_ctime_ns)


def valid_name(name):
    return (isinstance(name, str) and name.startswith(PREFIX) and name.endswith(SUFFIX)
            and len(name) > len(PREFIX) + len(SUFFIX) and '..' not in name
            and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-'
                    for c in name))


def protected_name(name):
    """Names retention must never touch, even if a future pattern would match them."""
    return isinstance(name, str) and name.startswith(PROTECTED_PREFIXES)


def private(st):
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != EUID:
        refuse('spool is not private: not our directory')
    if POSIX and stat.S_IMODE(st.st_mode) & 0o077:
        refuse('spool is not private: group/other permissions')


def shared(st):
    """The backup folder may be 0755: it is published by ksmbd. Group/other write is not."""
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != EUID:
        refuse('share is not our directory')
    if POSIX and stat.S_IMODE(st.st_mode) & 0o022:
        raise NotPrivate('share is group/other writable')


def staging(st):
    """TMPDIR is 1777 on OpenWrt: only the strict name pattern makes a child ours."""
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != EUID:
        refuse('staging directory is not ours')


def walk_directory(path):
    """Never follow any ancestor symlink. Return a pinned Linux directory fd."""
    path = os.path.abspath(path)
    if POSIX:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open('/', flags)
        try:
            for part in path.split('/')[1:]:
                if not part:
                    continue
                other = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = other
            return fd
        except BaseException:
            os.close(fd)
            raise
    # Windows has no openat/O_NOFOLLOW; this is not a POSIX privacy proof.
    current = os.path.splitdrive(path)[0] + os.sep
    for part in path[len(current):].split(os.sep):
        if not part:
            continue
        current = os.path.join(current, part)
        st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_reparse_tag', 0):
            refuse('spool ancestor is a symlink/reparse point')
        if not stat.S_ISDIR(st.st_mode):
            refuse('spool ancestor is not a directory')
    return None


class Spool:
    """A pinned directory: the 0700 spool, the published share or a staging folder."""

    POLICIES = {'private': private, 'share': shared, 'staging': staging}

    def __init__(self, path, kind='private'):
        if kind not in self.POLICIES:
            refuse('unknown directory kind')
        self.kind = kind
        self.path = os.path.abspath(path)
        self.fd = walk_directory(self.path)
        try:
            self.st = os.fstat(self.fd) if POSIX else os.lstat(self.path)
            self.POLICIES[kind](self.st)
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
            raise

    def __enter__(self):
        return self

    def check(self):
        fd = walk_directory(self.path)
        try:
            st = os.fstat(fd) if POSIX else os.lstat(self.path)
            self.POLICIES[self.kind](st)
            if identity(st) != identity(self.st):
                refuse('spool identity changed')
            if POSIX:
                self.POLICIES[self.kind](os.fstat(self.fd))
        finally:
            if fd is not None:
                os.close(fd)

    def __exit__(self, *unused):
        if self.fd is not None:
            os.close(self.fd)

    def open(self, name, flags=os.O_RDONLY, mode=0o600):
        self.check()
        flags |= getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
        if POSIX:
            return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=self.fd)
        path = os.path.join(self.path, name)
        before = None
        if not flags & os.O_CREAT:
            before = os.lstat(path)
            if stat.S_ISLNK(before.st_mode) or getattr(before, 'st_reparse_tag', 0):
                refuse('file is a symlink/reparse point')
        fd = os.open(path, flags, mode)
        try:
            if before and identity(before) != identity(os.fstat(fd)):
                refuse('file identity changed at open')
            self.check()
            return fd
        except BaseException:
            os.close(fd)
            raise

    def names(self):
        self.check()
        return os.listdir(self.fd if POSIX else self.path)

    def lstat(self, name):
        try:
            if POSIX:
                return os.lstat(name, dir_fd=self.fd)
            return os.lstat(os.path.join(self.path, name))
        except (FileNotFoundError, NotADirectoryError):
            return None

    def replace(self, source, target):
        self.check()
        if POSIX:
            os.replace(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            os.replace(os.path.join(self.path, source), os.path.join(self.path, target))

    def unlink(self, name):
        if POSIX:
            os.unlink(name, dir_fd=self.fd)
        else:
            os.unlink(os.path.join(self.path, name))


class Locked:
    """Serialise inventory -> evict -> register on the pinned directory descriptor.

    POSIX takes flock(2) on the directory descriptor, so two backend processes cannot both
    see room for their bundle. Windows test hosts have no fcntl: the module mutex keeps the
    same algorithm linear there, which is the only guarantee claimed off Linux.
    """

    def __init__(self, spool):
        self.spool = spool
        self.flocked = False

    def __enter__(self):
        _LOCK.acquire()
        try:
            if fcntl is not None and POSIX and self.spool.fd is not None:
                fcntl.flock(self.spool.fd, fcntl.LOCK_EX)
                self.flocked = True
        except BaseException:
            _LOCK.release()
            raise
        return self

    def __exit__(self, *unused):
        try:
            if self.flocked:
                fcntl.flock(self.spool.fd, fcntl.LOCK_UN)
        finally:
            _LOCK.release()


def check_file(st, what):
    if not stat.S_ISREG(st.st_mode) or st.st_uid != EUID or st.st_nlink != 1:
        refuse('%s must be our regular single-link file' % what)
    if st.st_size <= 0:
        refuse('%s is empty' % what)


def read_all(fd, maximum, digest=True):
    """Read at most maximum+1 bytes even if a same-inode writer grows the file."""
    chunks = []
    total = 0
    hasher = hashlib.sha256() if digest else None
    while True:
        chunk = os.read(fd, min(262144, maximum - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            refuse('file exceeds bounded read limit')
        if hasher is not None:
            hasher.update(chunk)
        chunks.append(chunk)
    return (hasher.hexdigest() if hasher is not None else None), total, b''.join(chunks)


def read_sidecar(spool, name, digest=True):
    try:
        fd = spool.open(name + '.json')
    except FileNotFoundError:
        refuse('архива нет: not a registered bundle')
    try:
        before = os.fstat(fd)
        check_file(before, 'sidecar')
        if before.st_size > SIDECAR_LIMIT:
            refuse('sidecar is oversized')
        _, total, payload = read_all(fd, before.st_size, digest)
        if total != before.st_size or signature(before) != signature(os.fstat(fd)):
            refuse('sidecar changed while read')
    finally:
        os.close(fd)
    try:
        record = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        refuse('malformed sidecar')
    if (not isinstance(record, dict) or type(record.get('schema')) is not int
            or record['schema'] != SCHEMA or record.get('name') != name):
        refuse('invalid sidecar schema/name')
    for key in ('dev', 'ino', 'bytes', 'uid', 'created', 'created_ns'):
        if type(record.get(key)) is not int or not 0 <= record[key] < 2 ** 64:
            refuse('invalid sidecar ' + key)
    digest = record.get('sha256')
    if (record['bytes'] <= 0 or not isinstance(digest, str) or len(digest) != 64
            or any(c not in '0123456789abcdef' for c in digest)):
        refuse('invalid sidecar size/hash')
    return record


def write_sidecar(spool, name, record):
    temporary = '.sidecar-' + secrets.token_hex(16)
    fd = spool.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        if POSIX:
            os.fchmod(fd, 0o600)
        payload = (json.dumps(record, separators=(',', ':')) + '\n').encode('utf-8')
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                refuse('short sidecar write')
            payload = payload[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        spool.replace(temporary, name + '.json')
    finally:
        if fd is not None:
            os.close(fd)
        try:
            spool.unlink(temporary)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------- retention inventory
def scan(directory):
    """lstat-only inventory. Never hashes, never opens a bundle, never follows a link.

    Returns (archives, sidecars, orphans, temps, stages):
      archives  name -> lstat of a regular file with a valid bundle name;
      sidecars  bundle name -> lstat of its .json (any type: read_sidecar still decides);
      orphans   [(sidecar name, lstat)] whose archive is not a regular file here;
      temps     [(name, lstat)] .sidecar-* / .part-* leftovers;
      stages    [(name, lstat)] geomask-console-diag.XXXXXX directories.
    Foreign names (router-backup-*.tar.gz, notes.txt, ...) stay out of every list.
    """
    entries = {}
    for name in directory.names():
        st = directory.lstat(name)
        if st is not None:
            entries[name] = st
    archives, sidecars, stages, temps = {}, {}, [], []
    for name in sorted(entries):
        st = entries[name]
        if valid_name(name) and stat.S_ISREG(st.st_mode):
            archives[name] = st
        elif name.endswith('.json') and valid_name(name[:-5]):
            sidecars[name[:-5]] = st
        elif STAGE_PATTERN.match(name) and stat.S_ISDIR(st.st_mode):
            stages.append((name, st))
        elif name.startswith(TEMPORARY_PREFIXES) and not stat.S_ISDIR(st.st_mode):
            temps.append((name, st))
    orphans = [(name + '.json', st) for name, st in sidecars.items() if name not in archives]
    return archives, sidecars, orphans, temps, stages


def measure(archives, sidecars):
    """(archives, bytes): sidecar bodies belong to the budget, foreign names do not."""
    total = 0
    for name, st in archives.items():
        total += st.st_size
        shadow = sidecars.get(name)
        if shadow is not None and stat.S_ISREG(shadow.st_mode):
            total += shadow.st_size
    return len(archives), total


def _created_ns(directory, name, st):
    """Eviction order follows the registered creation time, with mtime as fallback.

    The sidecar is bounded to 64 KiB and is read without hashing: retention costs lstats
    plus a few small JSON parses, never a sha256 pass over the archives.
    """
    try:
        return read_sidecar(directory, name, digest=False)['created_ns']
    except (Refused, OSError):
        return st.st_mtime_ns


def _order(directory, archives):
    return sorted(archives, key=lambda name: (_created_ns(directory, name, archives[name]), name))


def _unlink(directory, name):
    if protected_name(name):
        return False
    try:
        directory.unlink(name)
        return True
    except FileNotFoundError:
        return False
    except (OSError, Refused):
        return False


def _unlink_pair(directory, name):
    """Archive first, then its sidecar: the reverse order would hide a 2 MiB orphan."""
    removed = 1 if _unlink(directory, name) else 0
    if _unlink(directory, name + '.json'):
        removed += 1
    return removed


def _evict(directory, archives, sidecars, order, keep_name, grace, files, quota, now, floor=1):
    """Evict oldest-first until both limits hold; never the registering bundle."""
    removed = 0
    while len(archives) > files or measure(archives, sidecars)[1] > quota:
        if len(archives) <= floor:
            break
        victim = None
        for name in order:
            if name not in archives or name == keep_name:
                continue
            if grace and now - archives[name].st_mtime < grace:
                continue
            victim = name
            break
        if victim is None:
            break
        gone = _unlink_pair(directory, victim)
        if gone == 0 and directory.lstat(victim) is not None:
            # Windows may deny unlink while another registrar is hashing the file: stop
            # instead of spinning, and let the caller decide what does not fit.
            break
        removed += gone
        archives.pop(victim, None)
        sidecars.pop(victim, None)
    return removed


def _remove_tree_fd(parent_fd, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent_fd)
    except OSError:
        return False
    try:
        for child in os.listdir(fd):
            try:
                child_st = os.lstat(child, dir_fd=fd)
            except OSError:
                continue
            if stat.S_ISDIR(child_st.st_mode):
                _remove_tree_fd(fd, child)
            else:
                try:
                    os.unlink(child, dir_fd=fd)
                except OSError:
                    pass
    finally:
        os.close(fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
        return True
    except OSError:
        return False


def _remove_tree_path(path, depth=0):
    if depth > 32:
        return False
    try:
        names = os.listdir(path)
    except OSError:
        return False
    for child in names:
        full = os.path.join(path, child)
        try:
            child_st = os.lstat(full)
        except OSError:
            continue
        if (stat.S_ISDIR(child_st.st_mode) and not stat.S_ISLNK(child_st.st_mode)
                and not getattr(child_st, 'st_reparse_tag', 0)):
            _remove_tree_path(full, depth + 1)
        else:
            try:
                os.unlink(full)
            except OSError:
                pass
    try:
        os.rmdir(path)
        return True
    except OSError:
        return False


def _remove_tree(directory, name):
    """Remove one staging directory without following any link inside it."""
    if POSIX:
        return _remove_tree_fd(directory.fd, name)
    return _remove_tree_path(os.path.join(directory.path, name))


def _retain(directory, keep_hour, quota, files):
    archives, sidecars, orphans, temps, stages = scan(directory)
    before, _ = measure(archives, sidecars)
    now = time.time()
    removed = 0
    orphaned = 0
    # 1. Age, archive and sidecar as one pair.
    if keep_hour:
        for name in sorted(archives):
            if now - archives[name].st_mtime > keep_hour * 3600:
                removed += _unlink_pair(directory, name)
                archives.pop(name, None)
                sidecars.pop(name, None)
    # 2. Sidecars whose archive was deleted by hand or by an earlier pass.
    for name, st in orphans:
        if now - st.st_mtime > ORPHAN_AGE and _unlink(directory, name):
            removed += 1
            orphaned += 1
    # 3. Temporary files of a killed writer.
    for name, st in temps:
        if now - st.st_mtime > TEMPORARY_AGE and _unlink(directory, name):
            removed += 1
    # 4. Staging directories of a killed export (inside the spool only in tests; the
    #    backend passes TMPDIR as --stage-dir, which is swept by sweep_stages()).
    for name, st in stages:
        if now - st.st_mtime > STAGE_AGE and _remove_tree(directory, name):
            removed += 1
    # 5. Count, then 6. bytes: the old goes before the merely extra.
    order = _order(directory, archives)
    removed += _evict(directory, archives, sidecars, order, None, 0, files, quota, now)
    kept, total = measure(archives, sidecars)
    return {'kept': kept, 'files': before, 'bytes': total,
            'removed': removed, 'orphans': orphaned}


def sweep_stages(path):
    """Sweep geomask-console-diag.XXXXXX directories in a non-private TMPDIR."""
    removed = 0
    try:
        with Spool(path, 'staging') as staging_dir:
            with Locked(staging_dir):
                now = time.time()
                for name, st in scan(staging_dir)[4]:
                    if now - st.st_mtime > STAGE_AGE and _remove_tree(staging_dir, name):
                        removed += 1
    except (Refused, OSError):
        return 0
    return removed


def cmd_retain(path, keep_hour=None, quota=None, files=None, share=False, stage_dir=None):
    keep_hour = KEEP_HOURS if keep_hour is None else keep_hour
    quota = QUOTA if quota is None else quota
    files = FILES if files is None else files
    if type(keep_hour) is not int or not 0 <= keep_hour <= KEEP_MAXIMUM:
        refuse('invalid keep hours')
    if type(quota) is not int or quota < 1:
        refuse('invalid quota')
    if type(files) is not int or files < 1:
        refuse('invalid files')
    kind = 'share' if share else 'private'
    with Spool(path, kind) as spool:
        with Locked(spool):
            summary = _retain(spool, keep_hour, quota, files)
        # The staging sweep runs after the spool lock is released: with TMPDIR inside the
        # spool the two directories are the same, and a second flock on another descriptor
        # of that directory would deadlock (the spool pass already swept it).
        if stage_dir and (os.path.normcase(os.path.abspath(stage_dir))
                          != os.path.normcase(spool.path)):
            summary['removed'] += sweep_stages(stage_dir)
    return {'ok': True, 'kept': summary['kept'], 'files': summary['files'],
            'bytes': summary['bytes'], 'removed': summary['removed'],
            'orphans': summary['orphans']}


# ------------------------------------------------------------------- share copy
def _share_reason(exc):
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return 'missing'
    if getattr(exc, 'errno', None) == errno.ENOTDIR:
        # POSIX hits this when the destination is a plain file, not a directory.
        return 'missing'
    if isinstance(exc, PermissionError):
        return 'perm'
    if getattr(exc, 'errno', None) in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1),
                                       errno.EFBIG):
        return 'no-space'
    return 'perm'


def _write_all(fd, chunk):
    view = chunk
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.ENOSPC, 'short copy write')
        view = view[written:]


def _copy_share(share, source, name):
    try:
        source_fd = os.open(source, os.O_RDONLY | getattr(os, 'O_BINARY', 0)
                            | getattr(os, 'O_NONBLOCK', 0)
                            | (os.O_NOFOLLOW if POSIX else 0))
    except FileNotFoundError:
        raise ShareRefused('missing')
    except PermissionError:
        raise ShareRefused('perm')
    temporary = '.part-' + secrets.token_hex(16)
    target_fd = None
    try:
        source_st = os.fstat(source_fd)
        if (not stat.S_ISREG(source_st.st_mode) or source_st.st_uid != EUID
                or source_st.st_nlink != 1 or source_st.st_size <= 0
                or source_st.st_size > CREATION_MAXIMUM):
            raise ShareRefused('missing')
        try:
            target_fd = share.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            if POSIX:
                os.fchmod(target_fd, 0o600)
            copied = 0
            while copied < source_st.st_size:
                chunk = os.read(source_fd, min(COPY_CHUNK, source_st.st_size - copied))
                if not chunk:
                    raise OSError(errno.EIO, 'short source read')
                _write_all(target_fd, chunk)
                copied += len(chunk)
            if copied != source_st.st_size or os.fstat(target_fd).st_size != source_st.st_size:
                raise OSError(errno.ENOSPC, 'short copy')
            os.fsync(target_fd)
            os.close(target_fd)
            target_fd = None
            share.replace(temporary, name)
            try:
                if POSIX:
                    os.utime(name, ns=(source_st.st_mtime_ns, source_st.st_mtime_ns),
                             dir_fd=share.fd, follow_symlinks=False)
                else:
                    os.utime(os.path.join(share.path, name),
                             ns=(source_st.st_mtime_ns, source_st.st_mtime_ns))
            except OSError:
                _unlink(share, name)
                raise ShareRefused('perm')
        except ShareRefused:
            raise
        except PermissionError:
            raise ShareRefused('perm')
        except OSError as exc:
            raise ShareRefused(_share_reason(exc))
    finally:
        os.close(source_fd)
        if target_fd is not None:
            try:
                os.close(target_fd)
            except OSError:
                pass
        try:
            share.unlink(temporary)
        except OSError:
            pass
    return {'ok': True, 'name': name, 'bytes': source_st.st_size}


def cmd_copy_share(source, destination, name):
    """Copy one registered bundle into the published folder: temp -> fsync -> rename.

    The reader over SMB sees either nothing or a complete 0600 file whose mtime is the
    bundle's own, so the share and the spool expire together.
    """
    if not valid_name(name):
        raise ShareRefused('missing')
    try:
        share = Spool(destination, 'share')
    except NotPrivate as exc:
        raise ShareRefused('mode' if 'writable' in str(exc) else 'missing')
    except FileNotFoundError:
        raise ShareRefused('missing')
    except PermissionError:
        raise ShareRefused('perm')
    except Refused:
        raise ShareRefused('missing')
    except OSError as exc:
        raise ShareRefused(_share_reason(exc))
    with share:
        try:
            return _copy_share(share, os.path.abspath(source), name)
        except FileNotFoundError:
            raise ShareRefused('missing')
        except PermissionError:
            raise ShareRefused('perm')
        except OSError as exc:
            raise ShareRefused(_share_reason(exc))


# ------------------------------------------------------------------- operations
def cmd_init(path):
    # Pin and validate the parent before creation; do not chmod an existing directory.
    absolute = os.path.abspath(path)
    parent, name = os.path.split(absolute)
    fd = walk_directory(parent)
    try:
        try:
            if POSIX:
                os.mkdir(name, 0o700, dir_fd=fd)
            else:
                os.mkdir(absolute, 0o700)
        except FileExistsError:
            pass
        with Spool(absolute) as spool:
            spool.check()
    finally:
        if fd is not None:
            os.close(fd)
    return {'ok': True, 'spool': path}


def cmd_register(path, archive, quota=None, files=None, grace=None):
    quota = QUOTA if quota is None else quota
    files = FILES if files is None else files
    grace = GRACE if grace is None else grace
    if type(quota) is not int or quota < 1:
        refuse('invalid quota')
    if type(files) is not int or files < 1:
        refuse('invalid files')
    absolute = os.path.abspath(archive)
    if os.path.normcase(os.path.dirname(absolute)) != os.path.normcase(os.path.abspath(path)):
        refuse('archive is not directly inside spool')
    name = os.path.basename(absolute)
    if not valid_name(name):
        refuse('not a diagnostic bundle name')
    with Spool(path) as spool:
        fd = spool.open(name)
        try:
            before = os.fstat(fd)
            check_file(before, 'bundle')
            # Registration is bounded too; never hash arbitrary-size input into memory.
            if before.st_size > CREATION_MAXIMUM:
                refuse('bundle exceeds %s-byte creation/registration cap' % CREATION_MAXIMUM)
            digest, total, _ = read_all(fd, before.st_size)
            if total != before.st_size or signature(before) != signature(os.fstat(fd)):
                refuse('bundle changed during registration')
            record = dict(schema=SCHEMA, name=name, dev=before.st_dev, ino=before.st_ino,
                          bytes=total, uid=before.st_uid, sha256=digest, created=int(time.time()),
                          created_ns=time.time_ns())
        finally:
            os.close(fd)
        # Quota is enforced here and only here: the archive is closed and hashed, and the
        # whole measure -> evict -> accept step runs under one lock, so a parallel exporter
        # cannot slip between "there is room" and "write the sidecar" (R08).
        with Locked(spool):
            archives, sidecars, _, _, _ = scan(spool)
            if name not in archives:
                refuse('bundle changed during registration')
            _evict(spool, archives, sidecars, _order(spool, archives), name, grace,
                   files, quota, time.time())
            count, taken = measure(archives, sidecars)
            if count > files:
                _unlink_pair(spool, name)
                refuse('архив не помещается: квота %d файлов, свежие архивы защищены %d с'
                       % (files, grace))
            if taken > quota:
                _unlink_pair(spool, name)
                refuse('архив %d байт не помещается в квоту %d' % (record['bytes'], quota))
            try:
                write_sidecar(spool, name, record)
            except BaseException:
                # ENOSPC here must not leave a registered-looking pair behind: the archive
                # is removed as well (its sidecar temp is cleaned by write_sidecar).
                _unlink_pair(spool, name)
                raise
            spool.check()
    return {'ok': True, 'name': name, 'bytes': record['bytes'], 'sha256': digest}


def _open_registered(spool, record):
    """Open the archive and prove identity/size. None means 'candidate is gone'."""
    try:
        fd = spool.open(record['name'])
    except (FileNotFoundError, Refused):
        return None
    try:
        st = os.fstat(fd)
        if (identity(st) != (record['dev'], record['ino']) or st.st_uid != record['uid']
                or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                or st.st_size != record['bytes'] or st.st_size <= 0):
            os.close(fd)
            return None
        return fd
    except BaseException:
        os.close(fd)
        raise


def _newest_valid(spool):
    """Newest structurally valid registered bundle; an orphan sidecar is skipped."""
    candidates = []
    for entry in spool.names():
        if not entry.endswith('.json'):
            continue
        base = entry[:-5]
        if not valid_name(base):
            continue
        try:
            candidates.append(read_sidecar(spool, base))
        except (Refused, OSError):
            continue
    for record in sorted(candidates, key=lambda item: (item['created_ns'], item['name']),
                         reverse=True):
        fd = _open_registered(spool, record)
        if fd is not None:
            return record, fd
    refuse('архива нет: no registered bundle')


def cmd_get(path, name, maximum, ttl):
    maximum = MAXIMUM if maximum is None else maximum
    if type(maximum) is not int or maximum < 0:
        refuse('invalid maximum')
    if ttl is not None and (not math.isfinite(ttl) or ttl < 0):
        refuse('invalid ttl')
    with Spool(path) as spool:
        if not name:
            record, fd = _newest_valid(spool)
            name = record['name']
        else:
            if not valid_name(name):
                refuse('not a diagnostic bundle name')
            record = read_sidecar(spool, name)
            try:
                fd = spool.open(name)
            except FileNotFoundError:
                refuse('архива нет: файл удалён')
        try:
            before = os.fstat(fd)
            check_file(before, 'bundle')
            if identity(before) != (record['dev'], record['ino']) or before.st_uid != record['uid']:
                refuse('bundle identity changed')
            if before.st_size != record['bytes']:
                refuse('bundle size changed')
            if ttl is not None and record['created'] + ttl * 3600 < time.time():
                refuse('архив просрочен: старше %g ч — соберите новый' % ttl)
            if before.st_size > maximum:
                refuse('bundle is %s bytes, over the %s-byte download cap' % (before.st_size, maximum))
            digest, total, payload = read_all(fd, before.st_size)
            after = os.fstat(fd)
            check_file(after, 'bundle')
            if total != before.st_size or signature(before) != signature(after):
                refuse('bundle changed while read')
            if digest != record['sha256']:
                refuse('bundle sha256 mismatch')
            spool.check()
            return dict(ok=True, name=name, bytes=total, sha256=digest,
                        data=base64.b64encode(payload).decode('ascii'))
        finally:
            os.close(fd)


def _option_uint(text, what, minimum=1):
    if not isinstance(text, str) or not text.isdigit():
        refuse('invalid ' + what)
    value = int(text)
    if value < minimum:
        refuse('invalid ' + what)
    return value


def _options(pairs, allowed):
    values = {}
    if len(pairs) % 2:
        refuse('invalid options')
    for key, value in zip(pairs[::2], pairs[1::2]):
        if key not in allowed:
            refuse('unknown option')
        values[key] = value
    return values


def cli(args):
    if len(args) == 2 and args[0] == 'init':
        return cmd_init(args[1])
    if len(args) >= 3 and args[0] == 'register':
        values = _options(args[3:], ('--quota', '--files'))
        quota = _option_uint(values['--quota'], 'quota') if '--quota' in values else None
        files = _option_uint(values['--files'], 'files') if '--files' in values else None
        return cmd_register(args[1], args[2], quota, files)
    if len(args) >= 2 and args[0] == 'retain':
        options = list(args[2:])
        keep_hour, quota, files, share, stage_dir = None, None, None, False, None
        index = 0
        while index < len(options):
            key = options[index]
            if key == '--share':
                share = True
                index += 1
                continue
            if index + 1 >= len(options):
                refuse('invalid options')
            value = options[index + 1]
            if key == '--keep-h':
                keep_hour = _option_uint(value, 'keep hours', 0)
            elif key == '--quota':
                quota = _option_uint(value, 'quota')
            elif key == '--files':
                files = _option_uint(value, 'files')
            elif key == '--stage-dir':
                stage_dir = value
            else:
                refuse('unknown option')
            index += 2
        return cmd_retain(args[1], keep_hour, quota, files, share, stage_dir)
    if len(args) == 4 and args[0] == 'copy-share':
        return cmd_copy_share(args[1], args[2], args[3])
    if len(args) >= 3 and args[0] == 'get':
        maximum, ttl = MAXIMUM, 24.0
        values = _options(args[3:], ('--max', '--ttl'))
        if '--max' in values:
            maximum = int(values['--max'])
        if '--ttl' in values:
            ttl = float(values['--ttl'])
        return cmd_get(args[1], args[2], maximum, ttl)
    refuse('usage: init spool | register spool path [--quota N] [--files N] '
           '| get spool name [--max N] [--ttl H] '
           '| retain dir [--keep-h H] [--quota N] [--files N] [--share] [--stage-dir D] '
           '| copy-share src dir name')


def main():
    try:
        answer = cli(sys.argv[1:])
        rc = 0
    except ShareRefused as exc:
        answer, rc = {'ok': False, 'reason': exc.reason}, 1
    except (Refused, OSError, ValueError) as exc:
        answer, rc = {'ok': False, 'error': str(exc)}, 1
    sys.stdout.buffer.write((json.dumps(answer, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
    return rc


if __name__ == '__main__':
    sys.exit(main())
