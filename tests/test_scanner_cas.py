"""Behavioural test for the scanner's config write: it must refuse to install a
candidate when the live config changed after the scan read it.

The logic lives inside the shell heredoc, so the helper block is extracted and executed
on its own rather than importing the whole nightly program (which probes the network).
Fixtures are written as bytes so the comparison is exact on every platform.
"""
import os
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1] / 'assets'
SCANNER = HERE / 'geomask-node-scan.sh'
START = 'TX_LOCK='
END = '\ncfgd={}'

ORIGINAL = b'proxy-groups: []\n'
CANDIDATE = b'proxy-groups: [new]\n'


def helper_source():
    text = SCANNER.read_text(encoding='utf-8')
    begin = text.index(START)
    finish = text.index(END, begin)
    return text[begin:finish]


def load_helper(config_path):
    env = {'GM_CONFIG_TX_LOCK': str(config_path.parent / '.config-tx.lock')}
    namespace = {'os': os, '_E': env, '__name__': 'scanner_helpers'}
    exec(compile(helper_source(), '<scanner-helpers>', 'exec'), namespace)
    namespace['CFG'] = str(config_path)
    return namespace


class ScannerConfigWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.yaml'
        self.config.write_bytes(ORIGINAL)
        self.helpers = load_helper(self.config)
        self.candidate = self.root / 'config.yaml.tmp'

    def install(self, expected):
        self.helpers['install_config_candidate'](str(self.candidate), expected)

    def test_installs_when_the_config_is_unchanged(self):
        self.candidate.write_bytes(CANDIDATE)
        self.install(ORIGINAL.decode())
        self.assertEqual(self.config.read_bytes(), CANDIDATE)
        self.assertFalse(self.candidate.exists())

    def test_refuses_when_the_config_moved(self):
        self.candidate.write_bytes(CANDIDATE)
        committed = b'proxy-groups: [committed-by-tx]\n'
        self.config.write_bytes(committed)
        with self.assertRaises(self.helpers['ConfigWriteRefused']):
            self.install(ORIGINAL.decode())
        self.assertEqual(self.config.read_bytes(), committed)
        self.assertTrue(self.candidate.exists(), 'a refused candidate must not be installed')

    def test_refuses_when_the_config_was_removed(self):
        self.candidate.write_bytes(CANDIDATE)
        self.config.unlink()
        with self.assertRaises(self.helpers['ConfigWriteRefused']):
            self.install(ORIGINAL.decode())
        self.assertFalse(Path(self.helpers['CFG']).exists())

    def test_accepts_bytes_as_well_as_text(self):
        self.candidate.write_bytes(CANDIDATE)
        self.install(ORIGINAL)
        self.assertEqual(self.config.read_bytes(), CANDIDATE)

    def test_scan_uses_the_checked_write_at_both_sites(self):
        text = SCANNER.read_text(encoding='utf-8')
        self.assertEqual(text.count('install_config_candidate('), 3,
                         'defined once and called at both write sites')
        self.assertNotIn('\nos.replace(CFG+', text,
                         'no write path may bypass the checked install')


if __name__ == '__main__':
    unittest.main()
