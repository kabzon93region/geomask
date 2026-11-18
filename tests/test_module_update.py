"""Per-module updates: status comparison, refusal on local edits, atomic apply.

The helper is driven with an injected fetcher, so no network is used and the module
index format is exercised exactly as the router will see it. Repository content and
on-disk content are kept separate on purpose: several rules only show up when the two
differ (a hand-edited file, a stale checksum).
"""
import hashlib
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from win_symlink import NATIVE_SYMLINKS, SYMLINK_REASON

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'assets' / 'geomask-module-update.py'


def load_helper():
    spec = importlib.util.spec_from_file_location('module_update', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


updater = load_helper()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def keep_writes_in(test, root):
    """Point every host path the updater uses at the test's own tree.

    LOCK_PATH defaults to /tmp/geomask-module-update.lock and is opened by every
    state-changing command, so a test that only patches the state file would still write to
    the host's /tmp on a POSIX run (review 15.11, points 1 and 25). CACHE_ROOT and
    INSTALL_ROOTS are the trust boundary introduced for the 18.11 audit (U01): the cache
    must live in the private root, and a destination must be inside the shipped roots - a
    sandbox test declares its own temporary root as both.
    """
    for name, value in (('LOCK_PATH', str(root / 'install.lock')),
                        ('MAINTENANCE_LOCK', str(root / 'maintenance.lock')),
                        ('BACKUP_ROOT', str(root / 'backups')),
                        ('CACHE_ROOT', str(root)),
                        ('CACHE', str(root / 'index-cache.json')),
                        ('INSTALL_ROOTS', (str(root) + os.sep,))):
        patcher = mock.patch.object(updater, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)


class ModuleUpdaterSafetyTests(unittest.TestCase):
    """Regressions for the audit of 13.11.2026 (F09, F10, F14)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        keep_writes_in(self, self.root)

    def test_modes_from_the_catalogue_are_octal(self):
        # JSON 755 as a decimal int is 0o1363 - a world-writable root script
        self.assertEqual(updater.parse_mode('0755'), 0o755)
        self.assertEqual(updater.parse_mode('0644'), 0o644)
        self.assertEqual(updater.parse_mode(755), 0o755)   # legacy decimal catalogue
        self.assertEqual(updater.parse_mode(644), 0o644)
        with self.assertRaises(updater.UpdateError):
            updater.parse_mode('0777')
        with self.assertRaises(updater.UpdateError):
            updater.parse_mode(0o777)

    def test_the_shipped_catalogue_stores_octal_modes(self):
        index = json.loads((ROOT / 'modules/index.json').read_text(encoding='utf-8'))
        modes = {entry['mode'] for module in index['modules'].values()
                 for entry in module.get('files') or []}
        self.assertTrue(modes)
        for mode in modes:
            self.assertRegex(mode, r'^0[0-7]{3}$')

    def test_a_config_file_is_skipped_not_aborted(self):
        # aborting the whole module because one config differs made every core update
        # impossible without --force; the file is now left alone and reported
        conf = self.root / 'geomask-nodes.conf'
        conf.write_bytes(b'ai_expect=DE\n')
        other = self.root / 'script.sh'
        other.write_bytes(b'old\n')
        body = b'ai_expect=US\n'
        script = b'new\n'
        index = {'modules': {'core': {'version': '2', 'dependencies': [], 'files': [
            {'path': str(conf), 'source': 'assets/geomask-nodes.conf', 'mode': '0644',
             'config': True, 'sha256': updater.digest_bytes(body)},
            {'path': str(other), 'source': 'assets/script.sh', 'mode': '0755',
             'sha256': updater.digest_bytes(script)}]}}}
        state = {'modules': {'core': {'version': '1', 'files': {
            str(conf): updater.digest_bytes(body), str(other): updater.digest_bytes(b'old\n')}}}}
        skipped = []
        _module, staged, changed = updater.download_module(
            index, state, 'core', {'repo': 'r', 'ref': 'm'},
            fetcher=lambda url: body if url.endswith('.conf') else script, skipped=skipped)
        self.assertEqual(skipped, [str(conf)])
        self.assertTrue(changed)
        self.assertEqual([item['path'] for item in staged], [str(other)])
        def forced_fetch(url):
            return body if url.endswith('.conf') else script

        forced = updater.download_module(index, state, 'core', {'repo': 'r', 'ref': 'm'},
                                         force=True, fetcher=forced_fetch)[1]
        self.assertEqual(sorted(item['path'] for item in forced), sorted([str(conf), str(other)]))
        self.assertEqual(forced[0]['mode'] if forced[0]['path'] == str(conf) else forced[1]['mode'],
                         0o644)

    def test_subscription_dependencies_are_capabilities_not_modules(self):
        conf = self.root / 'config.yaml'
        conf.write_text('proxy-providers:\n  sub:\n    type: http\n    url: "https://x"\n',
                        encoding='utf-8')
        index = {'modules': {'core': {'version': '1', 'files': [], 'dependencies': []},
                             'path_vpn': {'version': '1', 'files': [],
                                          'dependencies': ['core', 'subscription:sub']}}}
        state = {'modules': {'core': {'version': '1', 'files': {}}}}
        with mock.patch.object(updater, 'MIHOMO_CONF', str(conf)):
            self.assertEqual(updater.dependencies_ready(index, state, 'path_vpn'), [])
            self.assertEqual(updater.provider_names(), {'sub'})
        with mock.patch.object(updater, 'MIHOMO_CONF', str(self.root / 'missing.yaml')):
            problems = updater.dependencies_ready(index, state, 'path_vpn')
            self.assertTrue(problems)
            self.assertIn('subscription:sub', problems[0])

    def test_changed_bytes_with_the_same_version_are_an_update(self):
        script = self.root / 'script.sh'
        old, new = b'old\n', b'new\n'
        script.write_bytes(old)
        index = {'modules': {'core': {'version': '1', 'files': [
            {'path': str(script), 'source': 's', 'mode': '0755',
             'sha256': updater.digest_bytes(new)}]}}}
        state = {'modules': {'core': {'version': '1',
                                      'files': {str(script): updater.digest_bytes(old)}}}}
        report = updater.status(index, state)[0]
        self.assertEqual(report['state'], 'update-available')
        self.assertEqual(report['modified'], [])

    def test_a_local_edit_is_still_modified(self):
        script = self.root / 'script.sh'
        script.write_bytes(b'edited\n')
        index = {'modules': {'core': {'version': '1', 'files': [
            {'path': str(script), 'source': 's', 'mode': '0755',
             'sha256': updater.digest_bytes(b'shipped\n')}]}}}
        state = {'modules': {'core': {'version': '1',
                                      'files': {str(script): updater.digest_bytes(b'baseline\n')}}}}
        self.assertEqual(updater.status(index, state)[0]['state'], 'modified')


class ModuleUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_path = self.root / 'state.json'
        self.config = {'repo': 'owner/repo', 'ref': 'master', 'ttl': 3600}
        self.repo = {}      # asset name -> bytes served by the fetcher
        self.disk = {}      # install path -> (bytes currently on disk, mode)
        # install_staged writes backups and takes a lock: keep both inside the temp tree
        self.backup_root = self.root / 'backups'
        for name, value in (('BACKUP_ROOT', str(self.backup_root)),
                            ('LOCK_PATH', str(self.root / 'install.lock')),
                            ('CACHE_ROOT', str(self.root)),
                            ('CACHE', str(self.root / 'index-cache.json')),
                            ('INSTALL_ROOTS', (str(self.root) + os.sep,))):
            patcher = mock.patch.object(updater, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- fixtures ---------------------------------------------------------
    def put(self, install_path, content, mode=755, on_disk=None):
        name = Path(install_path).name
        self.repo[name] = content
        self.disk[install_path] = (content if on_disk is None else on_disk, mode)
        target = Path(install_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.disk[install_path][0])
        target.chmod(mode)
        return install_path

    def index(self, version='1.1.0'):
        files = []
        for path, (_disk, mode) in self.disk.items():
            name = Path(path).name
            files.append({'path': path, 'source': 'assets/' + name, 'mode': mode,
                          'sha256': sha(self.repo[name])})
        return {'schema': 1, 'core_version': '1.27.10',
                'modules': {'core': {'version': version, 'type': 'core', 'name': 'Core',
                                     'dependencies': [], 'files': files}}}

    def fetcher(self, payload=None):
        payload = payload or self.index()

        def call(url):
            if url.endswith('modules/index.json'):
                return json.dumps(payload).encode()
            name = url.rsplit('/', 1)[-1]
            if name in self.repo:
                return self.repo[name]
            raise AssertionError('unexpected download ' + url)
        return call

    def state(self, version, files=None):
        return {'schema': 1, 'modules': {'core': {'version': version,
                                                  'files': files or {}}}}

    def index_two(self):
        """core (present on disk) plus a feature module that is not installed."""
        payload = self.index('1.1.0')
        payload['modules']['feature'] = {
            'version': '1.0.0', 'type': 'feature', 'name': 'Feature',
            'dependencies': ['core'],
            'files': [{'path': str(self.root / 'opt' / 'feature.sh'),
                       'source': 'assets/feature.sh', 'mode': 755,
                       'sha256': sha(b'feature\n')}],
        }
        return payload

    def selection(self, selected):
        path = self.root / 'modules.json'
        path.write_text(json.dumps({'selected': selected}), encoding='utf-8')
        return str(path)

    # --- status -----------------------------------------------------------
    def test_status_reports_update_available(self):
        self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n')
        report = updater.status(self.index('1.1.0'), self.state('1.0.0'))
        self.assertEqual(report[0]['state'], 'update-available')
        self.assertEqual(report[0]['installed_version'], '1.0.0')
        self.assertEqual(report[0]['available_version'], '1.1.0')

    def test_status_current_when_hashes_and_versions_match(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n')
        report = updater.status(self.index('1.0.0'),
                                self.state('1.0.0', {path: sha(b'body\n')}))
        self.assertEqual(report[0]['state'], 'current')

    def test_status_flags_locally_modified_file(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n',
                        on_disk=b'hand edit\n')
        report = updater.status(self.index('1.1.0'), self.state('1.0.0'))
        self.assertEqual(report[0]['state'], 'modified')
        self.assertIn(path, report[0]['modified'])

    def test_status_flags_missing_file(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n')
        Path(path).unlink()
        report = updater.status(self.index('1.0.0'), self.state('1.0.0'))
        self.assertEqual(report[0]['state'], 'incomplete')
        self.assertIn(path, report[0]['missing'])

    # --- verify -----------------------------------------------------------
    def test_verify_exits_one_when_the_module_is_modified(self):
        # F20: {"ok": false} used to exit 0, so a caller checking the code saw success
        self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n', on_disk=b'edited\n')
        self.state_path.write_text(json.dumps(self.state('1.0.0')), encoding='utf-8')
        out = io.StringIO()
        with mock.patch.object(updater, 'load_index',
                               return_value=(self.index('1.1.0'), 'cache')):
            with contextlib.redirect_stdout(out):
                rc = updater.main(['verify', 'core', '--state', str(self.state_path),
                                   '--selection', str(self.root / 'absent.json')])
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['module']['state'], 'modified')

    def test_verify_exits_zero_when_the_module_is_current(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n')
        self.state_path.write_text(
            json.dumps(self.state('1.0.0', {path: sha(b'body\n')})), encoding='utf-8')
        out = io.StringIO()
        with mock.patch.object(updater, 'load_index',
                               return_value=(self.index('1.0.0'), 'cache')):
            with contextlib.redirect_stdout(out):
                rc = updater.main(['verify', 'core', '--state', str(self.state_path),
                                   '--selection', str(self.root / 'absent.json')])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())['ok'])

    # --- update -----------------------------------------------------------
    def test_update_refuses_when_file_edited_locally(self):
        self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n', on_disk=b'hand edit\n')
        with self.assertRaises(updater.UpdateError) as caught:
            updater.download_module(self.index('1.1.0'), self.state('1.0.0'), 'core',
                                    self.config, fetcher=self.fetcher())
        self.assertIn('modified locally', str(caught.exception))

    def test_force_overrides_local_edit(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n',
                        on_disk=b'hand edit\n')
        module, staged, changed = updater.download_module(
            self.index('1.1.0'), self.state('1.0.0'), 'core', self.config,
            force=True, fetcher=self.fetcher())
        self.assertTrue(changed)
        updater.install_staged('core', staged, self.state('1.0.0'), '1.1.0',
                               str(self.state_path))
        self.assertEqual(Path(path).read_bytes(), b'body\n')

    def test_update_stages_and_writes_verified_files(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'new\n', on_disk=b'old\n')
        module, staged, changed = updater.download_module(
            self.index('1.1.0'), self.state('1.0.0', {path: sha(b'old\n')}), 'core',
            self.config, fetcher=self.fetcher())
        self.assertTrue(changed)
        self.assertEqual(len(staged), 1)
        updater.install_staged('core', staged, self.state('1.0.0'), '1.1.0',
                               str(self.state_path))
        self.assertEqual(Path(path).read_bytes(), b'new\n')
        state = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertEqual(state['modules']['core']['version'], '1.1.0')
        self.assertEqual(state['modules']['core']['files'][path], sha(b'new\n'))

    def test_checksum_mismatch_writes_nothing(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n', on_disk=b'old\n')
        broken = self.index('1.1.0')
        broken['modules']['core']['files'][0]['sha256'] = '0' * 64
        # The recorded hash matches the disk, so this is not a local edit: the
        # refusal must come from the checksum, not from the modified-file guard.
        with self.assertRaises(updater.UpdateError) as caught:
            updater.download_module(broken, self.state('1.0.0', {path: sha(b'old\n')}),
                                    'core', self.config, fetcher=self.fetcher())
        self.assertIn('checksum mismatch', str(caught.exception))
        self.assertEqual(Path(path).read_bytes(), b'old\n')

    def test_no_change_means_no_write(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'same\n')
        module, staged, changed = updater.download_module(
            self.index('1.0.0'), self.state('1.0.0', {path: sha(b'same\n')}), 'core',
            self.config, fetcher=self.fetcher())
        self.assertFalse(changed)

    def test_failed_write_restores_original(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n', on_disk=b'old\n')
        module, staged, changed = updater.download_module(
            self.index('1.1.0'), self.state('1.0.0', {path: sha(b'old\n')}), 'core',
            self.config, fetcher=self.fetcher())
        # A directory where the file should be makes the replace fail.
        Path(path).unlink()
        Path(path).mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.install_staged('core', staged, self.state('1.0.0'), '1.1.0',
                                   str(self.state_path))
        self.assertTrue(Path(path).is_dir())

    def test_failed_second_write_removes_a_newly_created_file(self):
        """A file that did not exist has no backup: rollback must delete it.

        The previous version only restored files it had copied and still reported that the
        originals were restored.
        """
        first = self.root / 'bin' / 'brand-new'
        second = self.root / 'bin' / 'second'
        second.parent.mkdir(parents=True, exist_ok=True)
        staged = [
            {'path': str(first), 'mode': 0o644, 'data': b'new\n', 'sha256': sha(b'new\n')},
            {'path': str(second), 'mode': 0o644, 'data': b'new\n', 'sha256': sha(b'new\n')},
        ]
        real_replace = updater.os.replace

        def fail_second(src, dst):
            if str(dst) == str(second):
                raise OSError('mocked write failure')
            return real_replace(src, dst)

        with mock.patch.object(updater.os, 'replace', side_effect=fail_second):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.install_staged('core', staged, {}, '1.0.0', str(self.state_path))
        self.assertFalse(first.exists(), 'the new file was left behind after a failed update')
        self.assertFalse(second.exists())
        leftovers = [name for name in os.listdir(str(second.parent))
                     if name.startswith('.geomask-new')]
        self.assertEqual(leftovers, [], 'a temp file survived the failed write')
        self.assertIn('originals restored', str(caught.exception))

    def test_state_write_failure_rolls_back_the_files(self):
        """The state file is part of the transaction: a failed commit must undo the writes."""
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'new\n', on_disk=b'old\n')
        module, staged, changed = updater.download_module(
            self.index('1.1.0'), self.state('1.0.0', {path: sha(b'old\n')}), 'core',
            self.config, fetcher=self.fetcher())
        with mock.patch.object(updater, 'write_json', side_effect=OSError('mocked state failure')):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.install_staged('core', staged, self.state('1.0.0'), '1.1.0',
                                       str(self.state_path))
        self.assertEqual(Path(path).read_bytes(), b'old\n')
        self.assertFalse(self.state_path.exists(), 'a failed transaction wrote state anyway')
        self.assertIn('originals restored', str(caught.exception))

    def test_duplicate_destination_paths_are_refused(self):
        # F21: two index entries aimed at one path wrote the file twice; the last write
        # silently became the installed content
        target = self.root / 'bin' / 'tool.sh'
        first = {'path': str(target), 'mode': 0o644, 'data': b'one\n',
                 'sha256': sha(b'one\n')}
        second = {'path': str(self.root / 'bin' / '..' / 'bin' / 'tool.sh'),
                  'mode': 0o644, 'data': b'two\n', 'sha256': sha(b'two\n')}
        with self.assertRaises(updater.UpdateError) as caught:
            updater.install_staged('core', [first, second], {}, '1.0.0',
                                   str(self.state_path))
        self.assertIn('duplicate destination path', str(caught.exception))
        self.assertFalse(target.exists())
        self.assertFalse(self.state_path.exists())

    def test_a_failed_transaction_leaves_the_caller_state_unchanged(self):
        # F21: the caller's dict was mutated before the state file was written, so a
        # failed install left memory claiming an update that never happened
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'new\n', on_disk=b'old\n')
        _module, staged, _changed = updater.download_module(
            self.index('1.1.0'), self.state('1.0.0', {path: sha(b'old\n')}), 'core',
            self.config, fetcher=self.fetcher())
        state = self.state('1.0.0', {path: sha(b'old\n')})
        snapshot = json.loads(json.dumps(state))
        with mock.patch.object(updater, 'write_json',
                               side_effect=OSError('mocked state failure')):
            with self.assertRaises(updater.UpdateError):
                updater.install_staged('core', staged, state, '1.1.0',
                                       str(self.state_path))
        self.assertEqual(state, snapshot)
        self.assertEqual(Path(path).read_bytes(), b'old\n')

    def test_a_successful_install_updates_the_caller_state(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'new\n', on_disk=b'old\n')
        _module, staged, _changed = updater.download_module(
            self.index('1.1.0'), self.state('1.0.0', {path: sha(b'old\n')}), 'core',
            self.config, fetcher=self.fetcher())
        state = self.state('1.0.0', {path: sha(b'old\n')})
        updater.install_staged('core', staged, state, '1.1.0', str(self.state_path))
        self.assertEqual(state['modules']['core']['version'], '1.1.0')
        self.assertEqual(state['modules']['core']['files'][path], sha(b'new\n'))

    def test_the_install_lock_wraps_the_whole_transaction(self):
        # F21: the state write must happen while the inter-process lock is held
        events = []

        class FakeFcntl:
            LOCK_EX = 1
            LOCK_UN = 2
            LOCK_NB = 4

            @staticmethod
            def flock(_fd, operation):
                events.append('unlock' if operation & 2 else 'lock')

        real_write_json = updater.write_json

        def write_while_locked(path, payload):
            events.append('state-write')
            real_write_json(path, payload)

        staged = [{'path': str(self.root / 'bin' / 'tool.sh'), 'mode': 0o644,
                   'data': b'body\n', 'sha256': sha(b'body\n')}]
        with mock.patch.object(updater, 'fcntl', FakeFcntl):
            with mock.patch.object(updater, 'write_json', side_effect=write_while_locked):
                updater.install_staged('core', staged, {}, '1.0.0',
                                       str(self.state_path))
        self.assertEqual(events, ['lock', 'state-write', 'unlock'])

    def test_backup_directories_are_unique(self):
        # a fixed module-stamp name made two installs in one second share a backup
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'new\n', on_disk=b'old\n')
        _module, staged, _changed = updater.download_module(
            self.index('1.1.0'), self.state('1.0.0', {path: sha(b'old\n')}), 'core',
            self.config, fetcher=self.fetcher())
        state = self.state('1.0.0', {path: sha(b'old\n')})
        first = updater.install_staged('core', staged, state, '1.1.0',
                                       str(self.state_path))
        second = updater.install_staged('core', staged, state, '1.1.0',
                                        str(self.state_path))
        self.assertNotEqual(first, second)
        self.assertEqual(os.path.dirname(first), str(self.backup_root))
        self.assertTrue(os.path.isdir(first))
        self.assertTrue(os.path.isdir(second))

    # --- selection --------------------------------------------------------
    def test_status_marks_a_deselected_module(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n')
        report = updater.status(self.index_two(),
                                self.state('1.1.0', {path: sha(b'body\n')}),
                                selected=['core'])
        by_id = {item['id']: item for item in report}
        self.assertEqual(by_id['core']['state'], 'current')
        self.assertTrue(by_id['core']['selected'])
        # Not selected wins over "missing": the files are absent on purpose.
        self.assertEqual(by_id['feature']['state'], 'not-selected')
        self.assertFalse(by_id['feature']['selected'])

    def test_deselected_module_is_reported_not_hidden(self):
        self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n')
        report = updater.status(self.index_two(), self.state('1.0.0'), selected=['core'])
        self.assertEqual([item['id'] for item in report], ['core', 'feature'])

    def test_no_selection_file_means_every_module_is_selected(self):
        self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n')
        report = updater.status(self.index_two(), self.state('1.0.0'))
        self.assertTrue(all(item['selected'] for item in report))
        self.assertNotIn('not-selected', [item['state'] for item in report])

    def test_load_selection_reads_the_installer_state(self):
        path = self.selection(['core', 'youtube'])
        self.assertEqual(updater.load_selection(path), ['core', 'youtube'])
        self.assertIsNone(updater.load_selection(str(self.root / 'absent.json')))
        broken = self.root / 'broken.json'
        broken.write_text('{"selected": "core"}', encoding='utf-8')
        self.assertIsNone(updater.load_selection(str(broken)))

    def test_update_refuses_a_module_that_is_not_selected(self):
        selection = self.selection(['core'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = updater.main(['update', 'feature', '--selection', selection,
                               '--state', str(self.state_path)])
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload['ok'])
        self.assertIn('not part of this install', payload['error'])

    def test_seed_records_the_version_when_the_files_match(self):
        path = self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n')
        state = {'schema': 1, 'modules': {}}
        updater.seed_state(self.index('1.1.0'), state, str(self.state_path))
        saved = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['modules']['core']['version'], '1.1.0')
        self.assertEqual(saved['modules']['core']['files'][path], sha(b'body\n'))

    def test_seed_keeps_an_old_version_when_the_bytes_differ(self):
        self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n', on_disk=b'older\n')
        state = {'schema': 1, 'modules': {'core': {'version': '0.9.0', 'files': {}}}}
        updater.seed_state(self.index('1.1.0'), state, str(self.state_path))
        saved = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['modules']['core']['version'], '0.9.0')

    def test_seed_versions_flag_trusts_the_installer(self):
        # the installer wrote this release; a generated ruleset is not a local edit
        self.put(str(self.root / 'bin' / 'tool.sh'), b'body\n', on_disk=b'generated\n')
        state = {'schema': 1, 'modules': {'core': {'version': '0.9.0', 'files': {}}}}
        updater.seed_state(self.index('1.1.0'), state, str(self.state_path), versions=True)
        saved = json.loads(self.state_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['modules']['core']['version'], '1.1.0')

    # --- guards -----------------------------------------------------------
    def test_dependency_must_be_new_enough(self):
        index = {'schema': 1, 'modules': {
            'core': {'version': '2.0.0', 'files': []},
            'feature': {'version': '1.0.0', 'dependencies': ['core'], 'files': []}}}
        problems = updater.dependencies_ready(index, self.state('1.0.0'), 'feature')
        self.assertTrue(problems)
        self.assertIn('core', problems[0])

    def test_a_dependency_without_a_version_is_not_ready(self):
        # F20: installed == '' skipped the version comparison, so a module that was never
        # installed counted as ready and its dependent updated over a missing base
        index = {'schema': 1, 'modules': {
            'core': {'version': '2.0.0', 'files': []},
            'feature': {'version': '1.0.0', 'dependencies': ['core'], 'files': []}}}
        problems = updater.dependencies_ready(index, self.state(''), 'feature')
        self.assertIn('core is not installed', problems)

    def test_a_dependency_with_missing_files_is_not_ready(self):
        base = self.root / 'lib' / 'base.sh'
        index = {'schema': 1, 'modules': {
            'core': {'version': '1.0.0', 'files': [
                {'path': str(base), 'source': 'assets/base.sh', 'mode': '0644',
                 'sha256': sha(b'base\n')}]},
            'feature': {'version': '1.0.0', 'dependencies': ['core'], 'files': []}}}
        state = self.state('1.0.0')
        problems = updater.dependencies_ready(index, state, 'feature')
        self.assertIn('core is missing 1 file', problems[0])
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_bytes(b'base\n')
        self.assertEqual(updater.dependencies_ready(index, state, 'feature'), [])

    def test_unknown_module_is_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater.download_module(self.index(), self.state('1.0.0'), 'nope',
                                    self.config, fetcher=self.fetcher())

    def test_version_compare_handles_padding(self):
        self.assertGreater(updater.version_tuple('1.10.0'), updater.version_tuple('1.9.9'))
        self.assertEqual(updater.version_tuple('1.27'), (1, 27, 0))


class UpdateLockScopeTests(unittest.TestCase):
    """#25: the lock covers the read of the state, not only the final write.

    `install_staged` took flock around its own writes, but `load_state` and `download_module`
    ran before it. Two updaters therefore read the same baseline, and the slower one wrote its
    state over the faster one's install: the faster files stayed on disk while the state
    claimed the old versions, which the panel then reported as "locally modified".

    The ordering is asserted with a recording fcntl, because the Windows test host has none and
    a real two-process race would be both flaky and invisible there.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        keep_writes_in(self, self.root)
        self.state_path = self.root / 'state.json'
        self.state_path.write_text(json.dumps({'schema': 1, 'modules': {}}), encoding='utf-8')
        self.index_path = self.root / 'index.json'
        self.index_path.write_text(json.dumps({'schema': 1, 'modules': {}}), encoding='utf-8')

    def fake_fcntl(self, events):
        class FakeFcntl:
            LOCK_EX = 1
            LOCK_UN = 2
            LOCK_NB = 4

            @staticmethod
            def flock(_fd, operation):
                events.append('unlock' if operation & 2 else 'lock')
        return FakeFcntl

    def common_argv(self, command):
        return [command, '--state', str(self.state_path),
                '--config', str(self.root / 'absent.json'),
                '--selection', str(self.root / 'absent.json')]

    def run_main(self, argv, events, **patches):
        out = io.StringIO()
        readers = patches.pop('readers', {})
        with mock.patch.object(updater, 'fcntl', self.fake_fcntl(events)):
            with contextlib.ExitStack() as stack:
                for name, action in readers.items():
                    stack.enter_context(mock.patch.object(updater, name, action))
                for name, value in patches.items():
                    stack.enter_context(mock.patch.object(updater, name, value))
                with contextlib.redirect_stdout(out):
                    rc = updater.main(argv)
        return rc, out.getvalue()

    def recording_read_state(self, events):
        def load_state(path=None):
            events.append('state-read')
            return {'schema': 1, 'modules': {}}
        return load_state

    def test_the_state_is_read_under_the_lock(self):
        events = []
        rc, _out = self.run_main(['seed', '--index-file', str(self.index_path)] +
                                 self.common_argv('seed')[1:], events,
                                 readers={'load_state': self.recording_read_state(events),
                                          'load_selection': lambda path=None: None})
        self.assertEqual(rc, 0)
        self.assertEqual(events[0], 'lock',
                         'the state was read before the lock was taken: %r' % (events,))
        self.assertIn('state-read', events)
        self.assertLess(events.index('lock'), events.index('state-read'))
        self.assertEqual(events[-1], 'unlock')

    def test_the_writer_takes_the_maintenance_lane_first(self):
        """P03: an update must not replace files under an installer that owns `maintenance`."""
        events = []
        rc, _out = self.run_main(['seed', '--index-file', str(self.index_path)] +
                                 self.common_argv('seed')[1:], events,
                                 readers={'load_state': self.recording_read_state(events),
                                          'load_selection': lambda path=None: None})
        self.assertEqual(rc, 0)
        locks = [index for index, event in enumerate(events) if event == 'lock']
        self.assertGreaterEqual(len(locks), 2,
                                'the updater did not take the maintenance lane: %r' % (events,))
        self.assertLess(locks[0], events.index('state-read'))

    @unittest.skipIf(updater.fcntl is None, 'posix-only')
    def test_the_maintenance_lock_refuses_instead_of_queueing(self):
        path = self.root / 'lane.lock'
        holder = open(path, 'a+')
        updater.fcntl.flock(holder.fileno(), updater.fcntl.LOCK_EX)
        try:
            with self.assertRaises(updater.UpdateError) as caught:
                with updater.maintenance_lock(path=str(path)):
                    pass
            self.assertIn('maintenance lock', str(caught.exception))
        finally:
            updater.fcntl.flock(holder.fileno(), updater.fcntl.LOCK_UN)
            holder.close()
        with updater.maintenance_lock(path=str(path)):
            pass

    def test_the_activation_contract_names_a_restart(self):
        self.assertEqual(updater.activation_required([]), 'none')
        self.assertEqual(updater.activation_required([{'path': '/usr/lib/geomask/x.py'}]),
                         'none')
        self.assertEqual(updater.activation_required(
            [{'path': '/usr/lib/geomask/x.py'}, {'path': '/etc/init.d/geomask-syncd'}]),
            'restart-required',
            'a module that ships an init script must not be reported as live')

    def test_a_read_only_command_never_waits_for_the_lock(self):
        # `status` is the panel's poll: if it queued behind an update, the panel would freeze
        # for the length of a download.
        events = []
        rc, payload = self.run_main(self.common_argv('status'), events,
                                    load_index=lambda *_a, **_k: ({'modules': {}}, 'cache'))
        self.assertEqual(rc, 0, payload)
        self.assertEqual(events, [], 'a read-only command took the update lock')

    def test_nested_locks_take_exactly_one_real_lock(self):
        events = []
        with mock.patch.object(updater, 'fcntl', self.fake_fcntl(events)):
            with updater.install_lock():
                with updater.install_lock():
                    events.append('body')
        self.assertEqual(events, ['lock', 'body', 'unlock'],
                         'the commit would block on the lock its own command holds')


class OfflineIndexTests(unittest.TestCase):
    """Regressions for the audit of 14.11.2026 (F28): status must not use the network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / 'index-cache.json'
        self.state_path = self.root / 'state.json'
        self.config = {'repo': 'owner/repo', 'ref': 'master', 'ttl': 3600}
        keep_writes_in(self, self.root)
        self.state_path.write_text(json.dumps({'schema': 1, 'modules': {}}),
                                   encoding='utf-8')

    @staticmethod
    def no_network(url):
        raise AssertionError('a network fetch was attempted: ' + url)

    def write_cache(self, payload=None):
        payload = payload if payload is not None else {'schema': 1, 'modules': {}}
        self.cache.write_text(json.dumps(payload), encoding='utf-8')

    def run_main(self, command):
        out = io.StringIO()
        with mock.patch.object(updater, 'fetch', self.no_network):
            with contextlib.redirect_stdout(out):
                rc = updater.main([command, '--state', str(self.state_path),
                                   '--selection', str(self.root / 'absent.json'),
                                   '--config', str(self.root / 'absent.json')])
        return rc, json.loads(out.getvalue())

    def test_offline_without_a_cache_is_a_refusal_not_a_download(self):
        with self.assertRaises(updater.UpdateError) as caught:
            updater.load_index(self.config, offline=True, fetcher=self.no_network)
        self.assertIn('index cache is empty', str(caught.exception))
        self.assertFalse(self.cache.exists())

    def test_offline_returns_a_fresh_cache(self):
        self.write_cache()
        index, source = updater.load_index(self.config, offline=True,
                                           fetcher=self.no_network)
        self.assertEqual(source, 'cache')
        self.assertEqual(index, {'schema': 1, 'modules': {}})

    def test_offline_returns_a_stale_cache(self):
        self.write_cache()
        old = time.time() - self.config['ttl'] - 60
        os.utime(str(self.cache), (old, old))
        _index, source = updater.load_index(self.config, offline=True,
                                            fetcher=self.no_network)
        self.assertEqual(source, 'stale-cache')

    def test_status_reads_the_cache_and_never_downloads(self):
        self.write_cache()
        rc, payload = self.run_main('status')
        self.assertEqual(rc, 0)
        self.assertEqual(payload['source'], 'cache')
        self.assertEqual(payload['modules'], [])

    def test_status_without_a_cache_fails_without_a_download(self):
        rc, payload = self.run_main('status')
        self.assertEqual(rc, 1)
        self.assertFalse(payload['ok'])
        self.assertIn('index cache is empty', payload['error'])
        self.assertFalse(self.cache.exists())

    def test_check_still_refreshes_the_cache_from_the_network(self):
        calls = []
        payload = {'schema': 1, 'modules': {}}

        def fetcher(url):
            calls.append(url)
            return json.dumps(payload).encode('utf-8')

        out = io.StringIO()
        with mock.patch.object(updater, 'fetch', fetcher):
            with contextlib.redirect_stdout(out):
                rc = updater.main(['check', '--state', str(self.state_path),
                                   '--selection', str(self.root / 'absent.json'),
                                   '--config', str(self.root / 'absent.json')])
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.cache.exists())
        self.assertEqual(json.loads(out.getvalue())['source'], 'network')


class UpdaterTrustTests(unittest.TestCase):
    """U01-U03 of the independent audit of 18.11.2026.

    U01: the catalogue cache sat in a predictable world-writable directory and every
    per-module destination came from the catalogue itself. A substituted cache plus a
    matching download therefore authorised a root write outside the shipped tree.
    U02: the original file was compared with the catalogue while staging the download, but
    not again under the write lock, so an owner edit made between the two was overwritten.
    U03: a symlink at a destination was dereferenced - the linked file was replaced and the
    call still reported "originals restored".
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.private = self.root / 'private'
        self.private.mkdir()
        self.cache = self.private / 'modules-index.json'
        self.state_path = self.root / 'state.json'
        self.config = {'repo': 'owner/repo', 'ref': 'master', 'ttl': 3600}
        self.dest = self.root / 'bin' / 'tool.sh'
        self.dest.parent.mkdir()
        for name, value in (('BACKUP_ROOT', str(self.root / 'backups')),
                            ('LOCK_PATH', str(self.root / 'install.lock')),
                            ('CACHE_ROOT', str(self.private)),
                            ('CACHE', str(self.cache)),
                            ('INSTALL_ROOTS', (str(self.root) + os.sep,))):
            # create=True: on the pre-fix code these names do not exist, and the point of the
            # tests is a behavioural RED (root write refused), not a setUp AttributeError.
            patcher = mock.patch.object(updater, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- fixtures ---------------------------------------------------------
    def catalogue(self, path, payload, mode='0644'):
        return {'schema': 1, 'core_version': '1.27.10', 'modules': {'core': {
            'version': '2.0.0', 'type': 'core', 'name': 'Core', 'dependencies': [],
            'files': [{'path': str(path), 'source': 'assets/tool.sh', 'mode': mode,
                       'sha256': sha(payload)}]}}}

    def fetch_body(self, payload):
        def call(url):
            if url.endswith('modules/index.json'):
                raise AssertionError('the cache path must decide the source: ' + url)
            return payload
        return call

    def state_with(self, payload):
        """Baseline equal to what is on disk, so the local-edit guard is not the one firing."""
        return {'modules': {'core': {'version': '1.0.0',
                                     'files': {str(self.dest): sha(payload)}}}}

    def no_network(self, url):
        raise AssertionError('a network fetch was attempted: ' + url)

    # --- U01: catalogue trust and destination policy ----------------------
    def test_a_regular_cache_inside_the_private_root_is_used(self):
        self.cache.write_text(json.dumps({'schema': 1, 'modules': {}}), encoding='utf-8')
        index, source = updater.load_index(self.config, offline=True,
                                           fetcher=self.no_network)
        self.assertEqual(source, 'cache')
        self.assertEqual(index, {'schema': 1, 'modules': {}})

    def test_a_cache_outside_the_private_root_is_refused(self):
        # /tmp was the old location: any local user could plant a catalogue there
        planted = self.root / 'tmp-cache.json'
        planted.write_text(json.dumps({'schema': 1, 'modules': {}}), encoding='utf-8')
        with mock.patch.object(updater, 'CACHE', str(planted)):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.load_index(self.config, offline=True, fetcher=self.no_network)
        self.assertIn('not trusted', str(caught.exception))

    def test_a_substituted_cache_is_never_used_and_never_overwritten(self):
        planted = self.root / 'planted.json'
        planted.write_text(json.dumps({'schema': 1, 'modules': {}}), encoding='utf-8')
        with mock.patch.object(updater, 'CACHE', str(planted)), \
                mock.patch.object(updater, 'CACHE_ROOT', str(self.root / 'other')):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.load_index(self.config, force=True, fetcher=self.fetch_body(b'{}'))
        self.assertIn('not trusted', str(caught.exception))
        self.assertEqual(json.loads(planted.read_text(encoding='utf-8')),
                         {'schema': 1, 'modules': {}})

    @unittest.skipUnless(NATIVE_SYMLINKS, 'native symlinks unavailable: ' + SYMLINK_REASON)
    def test_a_cache_symlink_is_refused(self):
        decoy = self.root / 'decoy.json'
        decoy.write_text(json.dumps({'schema': 1, 'modules': {}}), encoding='utf-8')
        os.symlink(str(decoy), str(self.cache))
        with self.assertRaises(updater.UpdateError) as caught:
            updater.load_index(self.config, offline=True, fetcher=self.no_network)
        self.assertIn('not trusted', str(caught.exception))

    def test_a_cache_that_is_not_a_regular_file_is_refused(self):
        self.cache.mkdir()
        with self.assertRaises(updater.UpdateError) as caught:
            updater.load_index(self.config, offline=True, fetcher=self.no_network)
        self.assertIn('not trusted', str(caught.exception))

    def test_a_planted_catalogue_cannot_authorise_a_destination_outside_the_policy(self):
        outside = self.root / 'pwn.sh'
        with mock.patch.object(updater, 'INSTALL_ROOTS', ('/usr/bin/',)):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.download_module(self.catalogue(outside, b'x\n'), {'modules': {}},
                                        'core', self.config,
                                        fetcher=self.fetch_body(b'x\n'))
        self.assertIn('install root', str(caught.exception))
        self.assertFalse(outside.exists())

    def test_a_traversing_destination_is_refused(self):
        index = self.catalogue('/usr/bin/../tmp/pwn', b'x\n')
        with self.assertRaises(updater.UpdateError) as caught:
            updater.download_module(index, {'modules': {}}, 'core', self.config,
                                    fetcher=self.fetch_body(b'x\n'))
        self.assertIn('normalized', str(caught.exception))

    def test_a_relative_destination_is_refused(self):
        index = self.catalogue('bin/tool.sh', b'x\n')
        with self.assertRaises(updater.UpdateError) as caught:
            updater.download_module(index, {'modules': {}}, 'core', self.config,
                                    fetcher=self.fetch_body(b'x\n'))
        self.assertIn('absolute', str(caught.exception))

    def test_the_shipped_policy_accepts_every_catalogue_destination(self):
        # The allow-list must cover the real catalogue, otherwise a legal module update breaks
        # on every router until a full reinstall. This uses the shipped constant, not a patch.
        spec = importlib.util.spec_from_file_location('module_update_fresh', HELPER)
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        index = json.loads((ROOT / 'modules' / 'index.json').read_text(encoding='utf-8'))
        paths = [entry['path'] for module in index['modules'].values()
                 for entry in module.get('files') or []]
        self.assertTrue(paths)
        for path in paths:
            self.assertEqual('', fresh.destination_problem(path),
                             'the shipped policy refuses a real catalogue path: %s' % path)

    @unittest.skipUnless(NATIVE_SYMLINKS, 'native symlinks unavailable: ' + SYMLINK_REASON)
    def test_a_symlinked_cache_directory_is_refused(self):
        # The directory decides who may replace the cache file, so it is checked too.
        real = self.root / 'real-cache'
        real.mkdir()
        link = self.root / 'cache-link'
        os.symlink(str(real), str(link))
        with mock.patch.object(updater, 'CACHE_ROOT', str(link), create=True), \
                mock.patch.object(updater, 'CACHE', str(link / 'modules-index.json')):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.load_index(self.config, offline=True, fetcher=self.no_network)
        self.assertIn('not trusted', str(caught.exception))

    # --- U02: the commit re-validates what it overwrites ------------------
    def test_owner_edit_between_stage_and_commit_is_not_lost(self):
        self.dest.write_bytes(b'old\n')
        index = self.catalogue(self.dest, b'new\n')
        _module, staged, _changed = updater.download_module(
            index, self.state_with(b'old\n'), 'core', self.config, fetcher=self.fetch_body(b'new\n'))
        self.dest.write_bytes(b'owner edit\n')      # happens after staging, before commit
        with self.assertRaises(updater.UpdateError) as caught:
            updater.install_staged('core', staged, {'modules': {}}, '2.0.0',
                                   str(self.state_path))
        self.assertIn('changed while the update was staged', str(caught.exception))
        self.assertEqual(self.dest.read_bytes(), b'owner edit\n')
        self.assertFalse(self.state_path.exists(), 'a refused commit wrote state anyway')

    def test_force_still_allows_the_confirmable_overwrite(self):
        self.dest.write_bytes(b'old\n')
        index = self.catalogue(self.dest, b'new\n')
        _module, staged, _changed = updater.download_module(
            index, self.state_with(b'old\n'), 'core', self.config, fetcher=self.fetch_body(b'new\n'))
        self.dest.write_bytes(b'owner edit\n')
        updater.install_staged('core', staged, {'modules': {}}, '2.0.0',
                               str(self.state_path), force=True)
        self.assertEqual(self.dest.read_bytes(), b'new\n')

    def test_a_deleted_original_blocks_the_commit(self):
        self.dest.write_bytes(b'old\n')
        index = self.catalogue(self.dest, b'new\n')
        _module, staged, _changed = updater.download_module(
            index, self.state_with(b'old\n'), 'core', self.config, fetcher=self.fetch_body(b'new\n'))
        self.dest.unlink()
        with self.assertRaises(updater.UpdateError) as caught:
            updater.install_staged('core', staged, {'modules': {}}, '2.0.0',
                                   str(self.state_path))
        self.assertIn('changed while the update was staged', str(caught.exception))
        self.assertFalse(self.dest.exists(), 'a refused commit recreated the file')

    def test_a_file_created_after_staging_is_not_overwritten(self):
        # A module may add a new file; while the rest of the module downloads, the owner (or
        # the installer) can create that path. The commit must not silently replace it.
        index = self.catalogue(self.dest, b'new\n')
        _module, staged, _changed = updater.download_module(
            index, {'modules': {}}, 'core', self.config, fetcher=self.fetch_body(b'new\n'))
        self.dest.write_bytes(b'created meanwhile\n')
        with self.assertRaises(updater.UpdateError) as caught:
            updater.install_staged('core', staged, {'modules': {}}, '2.0.0',
                                   str(self.state_path))
        self.assertIn('changed while the update was staged', str(caught.exception))
        self.assertEqual(self.dest.read_bytes(), b'created meanwhile\n')

    # --- U03: symlink identity -------------------------------------------
    @unittest.skipUnless(NATIVE_SYMLINKS, 'native symlinks unavailable: ' + SYMLINK_REASON)
    def test_a_symlinked_destination_is_refused_before_any_write(self):
        real = self.root / 'real-target'
        real.write_bytes(b'keep\n')
        os.symlink(str(real), str(self.dest))
        with self.assertRaises(updater.UpdateError) as caught:
            updater.download_module(self.catalogue(self.dest, b'new\n'), {'modules': {}},
                                    'core', self.config, fetcher=self.fetch_body(b'new\n'))
        self.assertIn('symlink', str(caught.exception))
        self.assertTrue(self.dest.is_symlink(), 'the link was replaced by a regular file')
        self.assertEqual(real.read_bytes(), b'keep\n')

    @unittest.skipUnless(NATIVE_SYMLINKS, 'native symlinks unavailable: ' + SYMLINK_REASON)
    def test_a_symlinked_parent_is_refused_before_any_write(self):
        real = self.root / 'real-dir'
        real.mkdir()
        link = self.root / 'linked-dir'
        os.symlink(str(real), str(link))
        with self.assertRaises(updater.UpdateError) as caught:
            updater.install_staged(
                'core', [{'path': str(link / 'tool.sh'), 'mode': 0o644, 'data': b'new\n',
                          'sha256': sha(b'new\n')}], {'modules': {}}, '2.0.0',
                str(self.state_path))
        self.assertIn('symlink', str(caught.exception))
        self.assertEqual(list(real.iterdir()), [], 'a file was written through the link')

    def test_a_destination_that_is_a_directory_is_refused(self):
        self.dest.mkdir()
        with self.assertRaises(updater.UpdateError) as caught:
            updater.install_staged(
                'core', [{'path': str(self.dest), 'mode': 0o644, 'data': b'new\n',
                          'sha256': sha(b'new\n')}], {'modules': {}}, '2.0.0',
                str(self.state_path))
        self.assertIn('regular file', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
