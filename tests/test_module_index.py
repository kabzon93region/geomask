"""The module catalogue must describe the bytes the router actually receives.

`build.py` stamps the component versions while assembling the installer, and the deploy
tool stamps them while deploying, so three files in the repository still carry
`@@PANEL_VERSION@@` / `@@BACKEND_VERSION@@` / `@@GUARD_VERSION@@` placeholders. The
catalogue therefore hashes the *stamped* content. If either side drifts, the router
either sees a file it calls locally modified or refuses a correct download with a
checksum mismatch - both were observed before this test existed.
"""
import hashlib
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'modules' / 'index.json'
INSTALLER = ROOT / 'geomask.sh.in'
MARKERS = (b'@@PANEL_VERSION@@', b'@@BACKEND_VERSION@@', b'@@GUARD_VERSION@@')


def stamped(data, version):
    for marker in MARKERS:
        data = data.replace(marker, version.encode())
    return data


def installer_version():
    text = INSTALLER.read_text(encoding='utf-8')
    return re.search(r'(?m)^GM_VERSION="([^"]+)"', text).group(1)


class ModuleIndexTests(unittest.TestCase):
    def setUp(self):
        self.index = json.loads(INDEX.read_text(encoding='utf-8'))
        self.version = installer_version()

    def entries(self):
        for module in self.index['modules'].values():
            for entry in module.get('files') or []:
                yield entry

    def test_every_source_file_exists(self):
        for entry in self.entries():
            self.assertTrue((ROOT / entry['source']).is_file(), entry['source'])

    def test_hashes_match_the_stamped_assets(self):
        for entry in self.entries():
            data = (ROOT / entry['source']).read_bytes()
            actual = hashlib.sha256(stamped(data, self.version)).hexdigest()
            self.assertEqual(entry['sha256'], actual, entry['path'])

    def test_core_version_matches_the_installer(self):
        self.assertEqual(self.index['core_version'], self.version)

    def test_both_sides_stamp_the_same_markers(self):
        updater = (ROOT / 'assets' / 'geomask-module-update.py').read_text(encoding='utf-8')
        catalogue = (ROOT / 'tools' / 'build_module_index.py').read_text(encoding='utf-8')
        for marker in MARKERS:
            self.assertIn(marker.decode(), updater)
            self.assertIn(marker.decode(), catalogue)

    def test_router_side_files_are_not_catalogued(self):
        # These cannot describe themselves without changing on every build.
        paths = {entry['path'] for entry in self.entries()}
        self.assertNotIn('/etc/geomask/modules-index.json', paths)
        self.assertNotIn('/usr/lib/geomask/geomask-module-update.py', paths)


if __name__ == '__main__':
    unittest.main()
