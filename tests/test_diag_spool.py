"""Hermetic diagnostic spool adversarial tests; no router/backend execution."""
import base64
import contextlib
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('diag_spool', ROOT / 'assets/geomask-diag-spool.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spool = self.root / 'spool'
        m.cmd_init(str(self.spool))
        self.name = 'geomask-console-diag-test.tar.gz'
        self.path = self.spool / self.name
        self.payload = b'normal archive bytes\x00\xff'
        self.path.write_bytes(self.payload)
        m.cmd_register(str(self.spool), str(self.path))

    def get(self, name=None, maximum=1024, ttl=24):
        return m.cmd_get(str(self.spool), self.name if name is None else name, maximum, ttl)

    def refused(self, call=None):
        with self.assertRaises((m.Refused, OSError)):
            (call or self.get)()

    def symlink(self, target, link):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as exc:
            self.skipTest('symlink capability unavailable: %s' % exc)

    def test_normal_exact_bytes_sha_and_newest(self):
        answer = self.get('')
        self.assertEqual(base64.b64decode(answer['data']), self.payload)
        self.assertEqual(answer['sha256'], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(answer['bytes'], len(self.payload))
        self.assertEqual(set(answer), {'ok', 'name', 'bytes', 'sha256', 'data'})

    def test_private_sentinel_symlink_unregistered(self):
        sentinel = self.root / 'private.txt'
        sentinel.write_bytes(b'SYNTHETIC-PRIVATE-SENTINEL')
        name = 'geomask-console-diag-sentinel.tar.gz'
        self.symlink(sentinel, self.spool / name)
        self.refused(lambda: self.get(name))

    def test_registered_symlink_replacement(self):
        self.path.unlink()
        sentinel = self.root / 'private.txt'
        sentinel.write_bytes(b'SYNTHETIC-PRIVATE-SENTINEL')
        self.symlink(sentinel, self.path)
        self.refused()

    def test_hardlink(self):
        os.link(self.path, self.root / 'second-link')
        self.refused()

    def test_inode_rename_swap(self):
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.payload)
        os.replace(replacement, self.path)
        self.refused()

    def test_same_inode_tamper(self):
        self.path.write_bytes(b'x' * len(self.payload))
        self.refused()

    def test_unregistered_missing_expired_oversized(self):
        unregistered = self.spool / 'geomask-console-diag-unregistered.tar.gz'
        unregistered.write_bytes(b'unregistered bytes')
        self.refused(lambda: self.get(unregistered.name))
        self.refused(lambda: self.get('geomask-console-diag-absent.tar.gz'))
        self.refused(lambda: self.get(maximum=1))
        self.refused(lambda: self.get(ttl=0))
        self.path.unlink()
        self.refused()

    def test_malformed_sidecar(self):
        self.path.with_name(self.name + '.json').write_text('{bad', encoding='utf-8')
        self.refused()
        self.refused(lambda: self.get(''))

    def test_symlink_sidecar(self):
        sidecar = self.path.with_name(self.name + '.json')
        target = self.root / 'sidecar.json'
        sidecar.rename(target)
        self.symlink(target, sidecar)
        self.refused()
        self.refused(lambda: self.get(''))

    def test_hardlink_sidecar(self):
        sidecar = self.path.with_name(self.name + '.json')
        os.link(sidecar, self.root / 'second-sidecar')
        self.refused()

    def test_init_symlink_directory(self):
        link = self.root / 'link'
        self.symlink(self.spool, link)
        self.refused(lambda: m.cmd_init(str(link)))

    def test_symlink_ancestor(self):
        link = self.root / 'link'
        self.symlink(self.root, link)
        self.refused(lambda: m.cmd_get(str(link / 'spool'), self.name, 1024, 24))

    @unittest.skipUnless(os.name == 'posix', 'POSIX privacy mode assertion; Windows ACL not proven')
    def test_init_nonprivate_directory(self):
        self.spool.chmod(0o755)
        self.refused(lambda: m.cmd_init(str(self.spool)))

    @unittest.skipUnless(os.name == 'posix', 'POSIX FIFO only')
    def test_fifo_nonblocking(self):
        self.path.unlink()
        os.mkfifo(self.path)
        self.refused()
        sidecar = self.path.with_name(self.name + '.json')
        sidecar.unlink()
        os.mkfifo(sidecar)
        self.refused()

    def test_concurrent_get_replace_never_serves_replacement(self):
        stop = threading.Event()
        errors = []
        def replace():
            try:
                for index in range(100):
                    if stop.is_set():
                        break
                    other = self.spool / 'replacement'
                    other.write_bytes(b'SENTINEL-REPLACEMENT')
                    os.replace(other, self.path)
            except OSError as exc:
                # Windows may deny rename while a reader holds the file.
                if os.name != 'nt':
                    errors.append(exc)
        thread = threading.Thread(target=replace)
        thread.start()
        try:
            for _ in range(100):
                try:
                    answer = self.get()
                except (m.Refused, OSError):
                    continue
                self.assertEqual(base64.b64decode(answer['data']), self.payload)
        finally:
            stop.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_spool_identity_swap_is_refused(self):
        with m.Spool(str(self.spool)) as pinned:
            moved = self.root / 'old-spool'
            self.spool.rename(moved)
            self.spool.mkdir(mode=0o700)
            self.refused(pinned.check)
            self.refused(lambda: pinned.open(self.name))

    def test_register_rejects_symlink_hardlink_and_oversized(self):
        linked = self.spool / 'geomask-console-diag-link.tar.gz'
        os.link(self.path, linked)
        self.refused(lambda: m.cmd_register(str(self.spool), str(linked)))
        linked.unlink()
        linked.write_bytes(b'x' * (m.MAXIMUM + 1))
        self.refused(lambda: m.cmd_register(str(self.spool), str(linked)))
        linked.unlink()
        self.symlink(self.path, linked)
        self.refused(lambda: m.cmd_register(str(self.spool), str(linked)))

    def test_sidecar_oversized_and_invalid_records(self):
        sidecar = self.path.with_name(self.name + '.json')
        original = sidecar.read_text(encoding='utf-8')
        sidecar.write_bytes(b'x' * (m.SIDECAR_LIMIT + 1))
        self.refused()
        for key, value in [('schema', True), ('schema', 1.0),
                           ('bytes', True), ('sha256', 'g' * 64), ('created', 'bad'),
                           ('name', 'other'), ('uid', -1)]:
            record = json.loads(original)
            record[key] = value
            sidecar.write_text(json.dumps(record), encoding='utf-8')
            self.refused()

    def test_spool_swap_during_read_fails_closed(self):
        original = m.read_all
        original_lstat = m.os.lstat
        swapped = [False]
        other = self.root / 'foreign-spool'
        other.mkdir(mode=0o700)
        def observed_lstat(path, *args, **kwargs):
            # Windows denies renaming a directory containing an open CRT file. Inject
            # the changed identity observation there; Linux performs the real rename.
            if os.name == 'nt' and swapped[0] and os.path.abspath(path) == str(self.spool):
                return original_lstat(other, *args, **kwargs)
            return original_lstat(path, *args, **kwargs)
        def read_and_swap(fd, maximum, digest=True):
            answer = original(fd, maximum, digest)
            if maximum == len(self.payload) and not swapped[0]:
                swapped[0] = True
                if os.name == 'posix':
                    self.spool.rename(self.root / 'old-spool')
                    self.spool.mkdir(mode=0o700)
            return answer
        m.read_all = read_and_swap
        m.os.lstat = observed_lstat
        try:
            self.refused()
        finally:
            m.read_all = original
            m.os.lstat = original_lstat
        self.assertTrue(swapped[0])

    def test_bounded_read_during_growth(self):
        original = m.os.read
        count = [0]
        def growing(fd, size):
            count[0] += 1
            if count[0] > 20:
                raise AssertionError('unbounded read')
            return b'x' * size
        m.os.read = growing
        try:
            self.refused(lambda: m.read_all(0, 32))
        finally:
            m.os.read = original


class DiagCase(unittest.TestCase):
    """Shared sandbox: a private spool plus helpers for archives, ages and grace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spool = self.root / 'spool'
        m.cmd_init(str(self.spool))

    def bundle(self, tag):
        return 'geomask-console-diag-%s.tar.gz' % tag

    def make(self, name, payload=b'x' * 1024, mtime=None):
        path = self.spool / name
        path.write_bytes(payload)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def register(self, path, **kwargs):
        return m.cmd_register(str(self.spool), str(path), **kwargs)

    def sidecar(self, path):
        return self.spool / (path.name + '.json')

    def age(self, path, seconds):
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    @contextlib.contextmanager
    def grace(self, value):
        previous = m.GRACE
        m.GRACE = value
        try:
            yield
        finally:
            m.GRACE = previous

    def archives(self):
        return sorted(name for name in os.listdir(self.spool)
                      if m.valid_name(name)
                      and stat.S_ISREG(os.lstat(self.spool / name).st_mode))


class RetentionTests(DiagCase):
    """retain: one lstat pass, strict phase order, foreign names invisible."""

    def retained(self, **kwargs):
        options = dict(keep_hour=24, quota=8 * 1024 * 1024, files=10)
        options.update(kwargs)
        return m.cmd_retain(str(self.spool), **options)

    def test_summary_shape(self):
        answer = self.retained()
        self.assertEqual(set(answer), {'ok', 'kept', 'files', 'bytes', 'removed', 'orphans'})
        self.assertTrue(answer['ok'])
        self.assertEqual((answer['kept'], answer['files'], answer['bytes']), (0, 0, 0))

    def test_age_removes_the_archive_and_its_sidecar_together(self):
        old = self.make(self.bundle('20260101-000000.111'), b'o' * 512)
        fresh = self.make(self.bundle('20260102-000000.222'), b'f' * 512)
        self.register(old)
        self.register(fresh)
        self.age(old, 25 * 3600)
        self.age(self.sidecar(old), 25 * 3600)
        answer = self.retained()
        self.assertFalse(old.exists())
        self.assertFalse(self.sidecar(old).exists(), 'the sidecar outlived its archive')
        self.assertTrue(fresh.exists())
        self.assertTrue(self.sidecar(fresh).exists())
        self.assertEqual(answer['kept'], 1)

    def test_orphan_sidecar_and_stale_temporary_files(self):
        orphan = self.spool / (self.bundle('20260103-000000.333') + '.json')
        orphan.write_text('{"schema":1}\n', encoding='utf-8')
        self.age(orphan, 400)
        fresh_orphan = self.spool / (self.bundle('20260104-000000.444') + '.json')
        fresh_orphan.write_text('{"schema":1}\n', encoding='utf-8')
        stale = self.spool / '.sidecar-0123456789abcdef'
        stale.write_bytes(b'x')
        self.age(stale, 3700)
        fresh_part = self.spool / '.part-0123456789abcdef'
        fresh_part.write_bytes(b'x')
        answer = self.retained()
        self.assertFalse(orphan.exists())
        self.assertTrue(fresh_orphan.exists(), 'a just-orphaned sidecar was deleted early')
        self.assertFalse(stale.exists())
        self.assertTrue(fresh_part.exists(), 'a fresh temporary file was deleted')
        self.assertEqual(answer['orphans'], 1)

    def test_staging_directories_are_swept_by_age(self):
        old = self.spool / 'geomask-console-diag.AbC123'
        (old / 'bundle').mkdir(parents=True)
        (old / 'bundle' / 'summary.txt').write_text('state')
        self.age(old, 8000)
        fresh = self.spool / 'geomask-console-diag.ZzZ999'
        (fresh / 'bundle').mkdir(parents=True)
        self.retained()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists(), 'a fresh staging directory was swept')

    def test_staging_sweep_outside_the_spool_is_bounded_by_name_and_age(self):
        staging = self.root / 'tmp'
        staging.mkdir()
        old = staging / 'geomask-console-diag.Old123'
        old.mkdir()
        (old / 'member').write_text('state')
        self.age(old, 8000)
        fresh = staging / 'geomask-console-diag.New123'
        fresh.mkdir()
        foreign = staging / 'geomask-console-diag-keep.txt'
        foreign.write_text('not ours')
        answer = self.retained(stage_dir=str(staging))
        self.assertFalse(old.exists(), 'the TMPDIR staging directory was not swept')
        self.assertTrue(fresh.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(answer['removed'], 1)

    def test_staging_sweep_ignores_a_stage_owned_by_another_uid(self):
        # TMPDIR is 1777: a local user can create the name mask. Only a directory owned by
        # this euid is ours; the sweep must leave the other one (and its contents) alone.
        staging = self.root / 'tmp'
        staging.mkdir()
        ours = staging / 'geomask-console-diag.Ours01'
        (ours / 'member').mkdir(parents=True)
        foreign = staging / 'geomask-console-diag.Othe01'
        (foreign / 'member').mkdir(parents=True)
        self.age(ours, 8000)
        self.age(foreign, 8000)
        original_lstat = m.os.lstat
        euid = m.EUID

        def foreign_uid(path, *args, **kwargs):
            st = original_lstat(path, *args, **kwargs)
            if os.path.basename(os.fspath(path)) == foreign.name:
                values = list(st)
                values[4] = euid + 1
                return os.stat_result(values)
            return st

        m.os.lstat = foreign_uid
        try:
            answer = self.retained(stage_dir=str(staging))
        finally:
            m.os.lstat = original_lstat
        self.assertFalse(ours.exists(), 'our own staging directory was not swept')
        self.assertTrue(foreign.exists(), 'a staging directory of a foreign uid was removed')
        self.assertTrue((foreign / 'member').exists())
        self.assertEqual(answer['removed'], 1)

    def test_a_hardlinked_bundle_is_not_unlinked(self):
        # POSIX-only per the task: on Windows the guard's nlink semantics are not a claim.
        if os.name != 'posix':
            self.skipTest('posix-only: hardlink st_nlink is the property under test')
        archive = self.make(self.bundle('20260109-000000.777'), b'h' * 512)
        self.register(archive)
        os.link(archive, self.root / 'second-link')
        self.age(archive, 25 * 3600)
        self.age(self.sidecar(archive), 25 * 3600)
        answer = self.retained(keep_hour=24)
        self.assertTrue(archive.exists(), 'a hardlinked bundle was unlinked')
        self.assertTrue(self.sidecar(archive).exists(),
                        'the passport of a protected bundle was removed')
        self.assertEqual(answer['removed'], 0)

    @unittest.skipUnless(os.name == 'posix', 'posix-only: symlink inside a staging directory')
    def test_staging_removal_does_not_follow_a_link(self):
        outside = self.root / 'outside.txt'
        outside.write_text('keep')
        stage = self.spool / 'geomask-console-diag.LnK123'
        stage.mkdir()
        (stage / 'link').symlink_to(outside)
        self.age(stage, 8000)
        self.retained()
        self.assertFalse(stage.exists())
        self.assertTrue(outside.exists(), 'sweeping a staging directory followed a symlink')

    def test_foreign_names_are_neither_counted_nor_removed(self):
        backup = self.spool / 'router-backup-20260101-000000.tar.gz'
        backup.write_bytes(b'b' * 100000)
        notes = self.spool / 'notes.txt'
        notes.write_text('keep me')
        first = self.make(self.bundle('20260105-000000.555'), b'a' * 1024)
        second = self.make(self.bundle('20260106-000000.666'), b'b' * 1024)
        self.register(first)
        self.register(second)
        answer = self.retained(quota=8 * 1024 * 1024, files=10)
        expected = (2048 + self.sidecar(first).stat().st_size
                    + self.sidecar(second).stat().st_size)
        self.assertEqual(answer['files'], 2, 'a foreign file was counted as a bundle')
        self.assertEqual(answer['bytes'], expected,
                         'foreign bytes leaked into the quota accounting')
        self.assertTrue(backup.exists())
        self.assertTrue(notes.exists())
        self.assertTrue(first.exists() and second.exists())

    def test_retention_never_hashes(self):
        first = self.make(self.bundle('20260107-000000.777'), b'a' * 1024)
        second = self.make(self.bundle('20260108-000000.888'), b'b' * 1024)
        self.register(first)
        self.register(second)
        original = m.hashlib.sha256

        def forbidden(*args, **kwargs):
            raise AssertionError('retention must not hash a bundle')

        m.hashlib.sha256 = forbidden
        self.addCleanup(lambda: setattr(m.hashlib, 'sha256', original))
        answer = self.retained(quota=1200, files=1)
        self.assertTrue(answer['ok'])
        self.assertEqual(answer['kept'], 1, 'the quota phase did not evict under the patch')

    def test_missing_directory_is_refused(self):
        with self.assertRaises((m.Refused, OSError)):
            m.cmd_retain(str(self.root / 'absent'))


class QuotaTests(DiagCase):
    """register: quota is enforced there and only there, oldest-first, never the new one."""

    def test_byte_quota_evicts_the_oldest(self):
        with self.grace(0):
            first = self.make(self.bundle('20260101-000000.111'), b'a' * 1024)
            self.register(first, quota=2048, files=8)
            second = self.make(self.bundle('20260102-000000.222'), b'b' * 1024)
            self.register(second, quota=2048, files=8)
            third = self.make(self.bundle('20260103-000000.333'), b'c' * 1024)
            self.register(third, quota=2048, files=8)
        self.assertEqual(self.archives(), [third.name])
        self.assertFalse(self.sidecar(first).exists())
        self.assertFalse(self.sidecar(second).exists())
        self.assertTrue(self.sidecar(third).exists())
        self.assertEqual(m.cmd_get(str(self.spool), '', 4096, 24)['name'], third.name)

    def test_file_quota_evicts_the_oldest(self):
        with self.grace(0):
            made = []
            for index in range(5):
                path = self.make(self.bundle('2026010%d-000000.%d' % (index, index)),
                                 bytes([65 + index]) * 512)
                self.register(path, quota=8 * 1024 * 1024, files=2)
                made.append(path)
        self.assertEqual(self.archives(), [made[-2].name, made[-1].name])

    def test_eviction_order_uses_created_not_the_name(self):
        with self.grace(0):
            older = self.make(self.bundle('20990101-000000.111'), b'a' * 512)
            self.register(older, quota=8 * 1024 * 1024, files=1)
            newer = self.make(self.bundle('20000101-000000.222'), b'b' * 512)
            self.register(newer, quota=8 * 1024 * 1024, files=1)
        self.assertEqual(self.archives(), [newer.name],
                         'eviction followed the file name instead of creation time')

    def test_the_bundle_being_registered_is_never_evicted(self):
        with self.grace(0):
            first = self.make(self.bundle('20260101-000000.111'), b'a' * 512)
            self.register(first, quota=8 * 1024 * 1024, files=1)
            second = self.make(self.bundle('20260102-000000.222'), b'b' * 512)
            self.register(second, quota=8 * 1024 * 1024, files=1)
            self.assertTrue(second.exists())
            self.assertTrue(self.register_sidecar_ok(second))

    def register_sidecar_ok(self, path):
        try:
            with m.Spool(str(self.spool)) as spool:
                m.read_sidecar(spool, path.name)
            return True
        except (m.Refused, OSError):
            return False

    def test_fresh_archives_are_protected_by_the_grace_window(self):
        made = []
        for index in range(4):
            path = self.make(self.bundle('2026010%d-000000.%d' % (index, index)), b'x' * 512)
            self.register(path, quota=8 * 1024 * 1024, files=4)
            made.append(path)
        overflow = self.make(self.bundle('20260109-000000.999'), b'y' * 512)
        with self.assertRaises(m.Refused) as caught:
            self.register(overflow, quota=8 * 1024 * 1024, files=4)
        self.assertIn('не помещается', str(caught.exception))
        self.assertFalse(overflow.exists(), 'the refused archive stayed in the spool')
        self.assertFalse(self.sidecar(overflow).exists())
        self.assertEqual(self.archives(), [path.name for path in made])

    def test_an_archive_that_cannot_fit_is_removed_and_named(self):
        archive = self.make(self.bundle('20260101-000000.111'), b'z' * 1024)
        with self.assertRaises(m.Refused) as caught:
            self.register(archive, quota=200, files=4)
        text = str(caught.exception)
        self.assertIn('1024', text)
        self.assertIn('200', text)
        self.assertFalse(archive.exists())
        self.assertFalse(self.sidecar(archive).exists())

    def test_the_quota_includes_the_passport_bytes(self):
        # The archive alone fits, the pair does not: the exact pair size passes and one
        # byte less is refused with the archive and the temporary cleaned up.
        archive = self.make(self.bundle('20260101-000000.222'), b'p' * 100)
        self.register(archive, quota=1 << 20, files=8)
        sidecar = self.sidecar(archive)
        pair = archive.stat().st_size + sidecar.stat().st_size
        sidecar.unlink()
        with self.grace(0):
            self.register(archive, quota=pair, files=8)
        self.assertTrue(archive.exists())
        self.assertTrue(sidecar.exists())
        self.assertEqual(archive.stat().st_size + sidecar.stat().st_size, pair)
        sidecar.unlink()
        with self.assertRaises(m.Refused):
            self.register(archive, quota=pair - 1, files=8)
        self.assertFalse(archive.exists(), 'the refused archive stayed in the spool')
        self.assertFalse(sidecar.exists(), 'a passport survived a refused registration')
        leftovers = [name for name in os.listdir(self.spool) if name.startswith('.sidecar-')]
        self.assertEqual(leftovers, [])

    def test_sidecar_enospc_removes_the_archive_and_leaves_no_temporary(self):
        archive = self.make(self.bundle('20260101-000000.111'), b'z' * 512)
        original = m.os.write

        def no_space(fd, data):
            raise OSError(errno.ENOSPC, 'no space left on device')

        m.os.write = no_space
        try:
            with self.assertRaises(OSError):
                self.register(archive)
        finally:
            m.os.write = original
        self.assertFalse(archive.exists(), 'a registered-looking archive survived ENOSPC')
        self.assertFalse(self.sidecar(archive).exists())
        leftovers = [name for name in os.listdir(self.spool) if name.startswith('.sidecar-')]
        self.assertEqual(leftovers, [])

    def test_concurrent_registers_stay_within_the_quota(self):
        errors = []

        def worker(index):
            path = self.spool / self.bundle('20260101-0000%02d.%d' % (index, index))
            path.write_bytes(bytes([65 + index]) * 512)
            try:
                m.cmd_register(str(self.spool), str(path), quota=8 * 1024 * 1024,
                               files=3, grace=0)
            except (m.Refused, OSError):
                pass
            finally:
                try:
                    if path.exists() and not self.sidecar(path).exists():
                        path.unlink()
                except OSError as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive(), 'a registrar deadlocked')
        self.assertEqual(errors, [])
        kept = self.archives()
        self.assertLessEqual(len(kept), 3, kept)
        self.assertGreaterEqual(len(kept), 1, 'no concurrent registration succeeded: %r' % kept)
        spool = m.Spool(str(self.spool))
        try:
            for name in kept:
                record = m.read_sidecar(spool, name)
                self.assertEqual(record['name'], name)
        finally:
            spool.__exit__()
        orphans = [name for name in os.listdir(self.spool)
                   if name.endswith('.json') and name[:-5] not in kept
                   and m.valid_name(name[:-5])]
        self.assertEqual(orphans, [], 'a concurrent register left an orphan sidecar')


class ShareTests(DiagCase):
    """copy-share: temp + rename, 0600, source mtime, machine-readable failure."""

    def setUp(self):
        super().setUp()
        self.share = self.root / 'share'
        self.share.mkdir()

    def registered(self, payload=b'copy me' * 128, mtime=None):
        archive = self.make(self.bundle('share'), payload)
        self.register(archive)
        if mtime is not None:
            os.utime(archive, (mtime, mtime))
        return archive

    def test_copy_is_complete_private_and_keeps_the_source_mtime(self):
        stamp = time.time() - 3600
        archive = self.registered(mtime=stamp)
        answer = m.cmd_copy_share(str(archive), str(self.share), archive.name)
        self.assertTrue(answer['ok'])
        copied = self.share / archive.name
        self.assertTrue(copied.exists())
        self.assertEqual(copied.read_bytes(), archive.read_bytes())
        self.assertEqual(copied.stat().st_mtime_ns, archive.stat().st_mtime_ns,
                         'the share copy got a newer mtime than the bundle')
        self.assertEqual([entry.name for entry in self.share.iterdir()], [archive.name],
                         'a .part-* temporary file survived the copy')
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)

    def test_enospc_leaves_no_partial_file_under_the_final_name(self):
        archive = self.registered()
        original = m.os.write

        def no_space(fd, data):
            raise OSError(errno.ENOSPC, 'no space left on device')

        m.os.write = no_space
        try:
            with self.assertRaises(m.ShareRefused) as caught:
                m.cmd_copy_share(str(archive), str(self.share), archive.name)
        finally:
            m.os.write = original
        self.assertEqual(caught.exception.reason, 'no-space')
        self.assertEqual(list(self.share.iterdir()), [])

    def test_missing_source_and_missing_destination_are_machine_readable(self):
        archive = self.registered()
        with self.assertRaises(m.ShareRefused) as caught:
            m.cmd_copy_share(str(self.root / 'absent.tar.gz'), str(self.share), archive.name)
        self.assertEqual(caught.exception.reason, 'missing')
        with self.assertRaises(m.ShareRefused) as caught:
            m.cmd_copy_share(str(archive), str(self.root / 'absent'), archive.name)
        self.assertEqual(caught.exception.reason, 'missing')

    @unittest.skipUnless(os.name == 'posix', 'posix-only: directory mode')
    def test_world_writable_share_is_refused(self):
        os.chmod(self.share, 0o777)
        archive = self.registered()
        with self.assertRaises(m.ShareRefused) as caught:
            m.cmd_copy_share(str(archive), str(self.share), archive.name)
        self.assertEqual(caught.exception.reason, 'mode')
        self.assertEqual(list(self.share.iterdir()), [])

    def test_share_retention_keeps_router_backup_and_foreign_files(self):
        for index in range(4):
            path = self.share / self.bundle('2026010%d-000000.%d' % (index, index))
            path.write_bytes(bytes([65 + index]) * 512)
            self.age(path, 3600 * (5 - index))
        backup = self.share / 'router-backup-20260101-000000.tar.gz'
        backup.write_bytes(b'b' * 4096)
        notes = self.share / 'notes.txt'
        notes.write_text('keep')
        answer = m.cmd_retain(str(self.share), keep_hour=24, quota=8 * 1024 * 1024,
                              files=2, share=True)
        self.assertEqual(answer['kept'], 2)
        self.assertTrue(backup.exists())
        self.assertTrue(notes.exists())
        remaining = sorted(entry.name for entry in self.share.iterdir()
                           if entry.name.startswith('geomask-console-diag-'))
        self.assertEqual(remaining, [self.bundle('20260102-000000.2'),
                                     self.bundle('20260103-000000.3')])

    @unittest.skipUnless(os.name == 'posix', 'posix-only: directory mode')
    def test_share_retention_refuses_a_world_writable_directory(self):
        os.chmod(self.share, 0o777)
        with self.assertRaises(m.Refused):
            m.cmd_retain(str(self.share), keep_hour=24, quota=1024, files=2, share=True)


class DownloadFallbackTests(DiagCase):
    """get: an orphan sidecar is skipped, a gone archive is a Russian error, no deletion."""

    def register_pair(self):
        older = self.make(self.bundle('20260101-000000.111'), b'older bundle bytes')
        time.sleep(0.01)
        newer = self.make(self.bundle('20260102-000000.222'), b'newer bundle bytes')
        self.register(older)
        self.register(newer)
        return older, newer

    def test_empty_name_skips_an_orphan_sidecar(self):
        older, newer = self.register_pair()
        newer.unlink()
        answer = m.cmd_get(str(self.spool), '', 4096, 24)
        self.assertEqual(answer['name'], older.name,
                         'the newest VALID bundle was not selected')
        self.assertEqual(base64.b64decode(answer['data']), b'older bundle bytes')
        self.assertEqual(set(answer), {'ok', 'name', 'bytes', 'sha256', 'data'})

    def test_explicit_name_with_a_gone_archive_is_a_russian_error(self):
        older, newer = self.register_pair()
        newer.unlink()
        with self.assertRaises(m.Refused) as caught:
            m.cmd_get(str(self.spool), newer.name, 4096, 24)
        text = str(caught.exception)
        self.assertIn('архива нет', text)
        self.assertIn('файл удалён', text)
        self.assertNotIsInstance(caught.exception, OSError, 'a raw errno reached the panel')

    def test_expired_bundle_message_is_russian(self):
        older, newer = self.register_pair()
        with self.assertRaises(m.Refused) as caught:
            m.cmd_get(str(self.spool), newer.name, 4096, 0)
        self.assertIn('просрочен', str(caught.exception))

    def test_get_never_deletes_anything(self):
        older, newer = self.register_pair()
        original = m.Spool.unlink

        def forbidden(*args, **kwargs):
            raise AssertionError('a read-only download must not delete')

        m.Spool.unlink = forbidden
        try:
            self.assertEqual(m.cmd_get(str(self.spool), '', 4096, 24)['name'], newer.name)
            self.assertEqual(m.cmd_get(str(self.spool), older.name, 4096, 24)['name'],
                             older.name)
            for call in (lambda: m.cmd_get(str(self.spool), older.name, 4096, 0),
                         lambda: m.cmd_get(str(self.spool),
                                           self.bundle('20260103-000000.333'), 4096, 24),
                         lambda: m.cmd_get(str(self.spool), newer.name, 1, 24),
                         lambda: m.cmd_get(str(self.spool), '', 1, 24)):
                with self.assertRaises((m.Refused, OSError)):
                    call()
        finally:
            m.Spool.unlink = original
        self.assertTrue(older.exists() and newer.exists())
        self.assertTrue(self.sidecar(older).exists() and self.sidecar(newer).exists())


class WiringTests(unittest.TestCase):
    def test_helper_is_shipped_and_owned_by_core(self):
        index = json.loads((ROOT / 'modules/index.json').read_text(encoding='utf-8'))
        matches = [(owner, entry) for owner, module in index['modules'].items()
                   for entry in module['files']
                   if entry['path'] == '/usr/lib/geomask/geomask-diag-spool.py']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0], 'core')
        self.assertEqual(matches[0][1]['source'], 'assets/geomask-diag-spool.py')
        self.assertEqual(matches[0][1]['mode'], '0755')
        template = (ROOT / 'geomask.sh.in').read_text(encoding='utf-8')
        uninstall = template.split('geomask_uninstall() {', 1)[1].split('\n}', 1)[0]
        self.assertIn('geomask-diag-spool.py', uninstall)


if __name__ == '__main__':
    unittest.main()
