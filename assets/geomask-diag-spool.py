#!/usr/bin/env python3
"""Registered diagnostic downloads. Linux uses pinned, no-follow directory descriptors.

Windows tests exercise content/identity rejection, not Linux kernel/0700 guarantees.
No fallback exporter may reopen a verified archive by pathname.
"""
import base64
import hashlib
import json
import math
import os
import secrets
import stat
import sys
import time

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


class Refused(Exception):
    """Fail closed with a JSON error."""


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


def private(st):
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != EUID:
        refuse('spool is not private: not our directory')
    if POSIX and stat.S_IMODE(st.st_mode) & 0o077:
        refuse('spool is not private: group/other permissions')


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
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.fd = walk_directory(self.path)
        try:
            self.st = os.fstat(self.fd) if POSIX else os.lstat(self.path)
            private(self.st)
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
            private(st)
            if identity(st) != identity(self.st):
                refuse('spool identity changed')
            if POSIX:
                private(os.fstat(self.fd))
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


def check_file(st, what):
    if not stat.S_ISREG(st.st_mode) or st.st_uid != EUID or st.st_nlink != 1:
        refuse('%s must be our regular single-link file' % what)
    if st.st_size <= 0:
        refuse('%s is empty' % what)


def read_all(fd, maximum):
    """Read at most maximum+1 bytes even if a same-inode writer grows the file."""
    chunks = []
    total = 0
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, min(262144, maximum - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            refuse('file exceeds bounded read limit')
        digest.update(chunk)
        chunks.append(chunk)
    return digest.hexdigest(), total, b''.join(chunks)


def read_sidecar(spool, name):
    try:
        fd = spool.open(name + '.json')
    except FileNotFoundError:
        refuse('архива нет: not a registered bundle')
    try:
        before = os.fstat(fd)
        check_file(before, 'sidecar')
        if before.st_size > SIDECAR_LIMIT:
            refuse('sidecar is oversized')
        _, total, payload = read_all(fd, before.st_size)
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


def cmd_register(path, archive):
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
            write_sidecar(spool, name, record)
            spool.check()
        finally:
            os.close(fd)
    return {'ok': True, 'name': name, 'bytes': total, 'sha256': digest}


def cmd_get(path, name, maximum, ttl):
    maximum = MAXIMUM if maximum is None else maximum
    if type(maximum) is not int or maximum < 0:
        refuse('invalid maximum')
    if ttl is not None and (not math.isfinite(ttl) or ttl < 0):
        refuse('invalid ttl')
    with Spool(path) as spool:
        if not name:
            candidates = []
            for entry in spool.names():
                if entry.endswith('.json') and valid_name(entry[:-5]):
                    try:
                        record = read_sidecar(spool, entry[:-5])
                        candidates.append((record['created_ns'], entry[:-5]))
                    except (Refused, OSError):
                        continue
            if not candidates:
                refuse('архива нет: no registered bundle')
            name = max(candidates)[1]
        if not valid_name(name):
            refuse('not a diagnostic bundle name')
        record = read_sidecar(spool, name)
        if ttl is not None and record['created'] + ttl * 3600 < time.time():
            refuse('bundle expired')
        fd = spool.open(name)
        try:
            before = os.fstat(fd)
            check_file(before, 'bundle')
            if identity(before) != (record['dev'], record['ino']) or before.st_uid != record['uid']:
                refuse('bundle identity changed')
            if before.st_size != record['bytes']:
                refuse('bundle size changed')
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


def cli(args):
    if len(args) == 2 and args[0] == 'init':
        return cmd_init(args[1])
    if len(args) == 3 and args[0] == 'register':
        return cmd_register(args[1], args[2])
    if len(args) >= 3 and args[0] == 'get':
        maximum, ttl = MAXIMUM, 24.0
        options = args[3:]
        if len(options) % 2:
            refuse('invalid options')
        for key, value in zip(options[::2], options[1::2]):
            if key == '--max':
                maximum = int(value)
            elif key == '--ttl':
                ttl = float(value)
            else:
                refuse('unknown option')
        return cmd_get(args[1], args[2], maximum, ttl)
    refuse('usage: init spool | register spool path | get spool name [--max N] [--ttl H]')


def main():
    try:
        answer = cli(sys.argv[1:])
        rc = 0
    except (Refused, OSError, ValueError) as exc:
        answer, rc = {'ok': False, 'error': str(exc)}, 1
    sys.stdout.buffer.write((json.dumps(answer, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
    return rc


if __name__ == '__main__':
    sys.exit(main())
