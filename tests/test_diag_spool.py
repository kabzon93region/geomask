"""Hermetic diagnostic spool adversarial tests; no router/backend execution."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
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
        def read_and_swap(fd, maximum):
            answer = original(fd, maximum)
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
