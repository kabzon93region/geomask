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
from pathlib import Path
import tempfile
import unittest
from unittest import mock

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


class ModuleUpdaterSafetyTests(unittest.TestCase):
    """Regressions for the audit of 13.11.2026 (F09, F10, F14)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

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
        forced = updater.download_module(index, state, 'core', {'repo': 'r', 'ref': 'm'},
                                         force=True,
                                         fetcher=lambda url: body if url.endswith('.conf') else script)[1]
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

    def test_unknown_module_is_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater.download_module(self.index(), self.state('1.0.0'), 'nope',
                                    self.config, fetcher=self.fetcher())

    def test_version_compare_handles_padding(self):
        self.assertGreater(updater.version_tuple('1.10.0'), updater.version_tuple('1.9.9'))
        self.assertEqual(updater.version_tuple('1.27'), (1, 27, 0))


if __name__ == '__main__':
    unittest.main()
