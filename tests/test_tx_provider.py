"""Tests that the trial and the pre-flight validation agree about file providers.

Found by a real apply: `mihomo -t` rejected the candidate because a file provider pointed
at the live home while the check ran with `-d <workdir>`. Validation now uses the same
shaped config the trial runs, with provider files copied into the trial tree.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1] / 'assets'

def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

tx = _load('config_tx_provider', 'geomask-config-tx.py')

CONFIG = """\
mixed-port: 7890
secret: "s"
proxy-providers:
  audit-vpn-g1:
    type: file
    path: {pool}
  sub2:
    type: http
    url: "https://example.com/sub"
proxy-groups:
  - name: VPN
    type: url-test
    use: [audit-vpn-g1]
rules:
  - MATCH,VPN
"""


class TrialProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pool = self.root / 'live-pool.yaml'
        self.pool.write_text('proxies:\n  - {name: A, type: socks5, server: 1.1.1.1, port: 1}\n',
                             encoding='utf-8')
        self.config = self.root / 'config.yaml'
        self.config.write_text(CONFIG.format(pool=self.pool), encoding='utf-8')

        self.state = self.root / 'state'
        self.patchers = [
            patch.object(tx, 'TX_STATE_DIR', self.state),
            patch.object(tx, 'TX_PREIMAGES_DIR', self.state / 'pre'),
            patch.object(tx, 'TX_JOURNAL', self.state / 'journal.jsonl'),
            patch.object(tx, 'DEFAULT_CONFIG', self.config),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def instance(self):
        instance = tx.ConfigTransaction('provtest')
        instance.stage_file(self.config, self.config.read_bytes())
        return instance

    def test_trial_copies_file_providers_inside_its_own_home(self):
        instance = self.instance()
        work = instance._make_work_path(self.config)
        built = instance._build_trial_config(work, None)
        path = built['proxy-providers']['audit-vpn-g1']['path']
        self.assertNotEqual(path, str(self.pool))
        self.assertTrue(Path(path).is_file())
        self.assertIn(str(work.parent), path)
        self.assertEqual(Path(path).read_bytes(), self.pool.read_bytes())
        # An http provider is left alone.
        self.assertEqual(built['proxy-providers']['sub2']['type'], 'http')

    def test_trial_copies_file_backed_rule_providers_too(self):
        """The live config points rule providers at /etc/mihomo, outside the trial home."""
        ruleset = self.root / 'ruleset'
        ruleset.mkdir()
        (ruleset / 'torrentdirect.yaml').write_text('payload: []\n', encoding='utf-8')
        config = CONFIG.format(pool=self.pool) + (
            'rule-providers:\n'
            '  torrentdirect:\n'
            '    type: file\n'
            '    behavior: classical\n'
            '    format: yaml\n'
            '    path: %s\n' % (ruleset / 'torrentdirect.yaml'))
        self.config.write_text(config, encoding='utf-8')
        instance = self.instance()
        work = instance._make_work_path(self.config)
        built = instance._build_trial_config(work, None)
        path = built['rule-providers']['torrentdirect']['path']
        self.assertNotEqual(path, str(ruleset / 'torrentdirect.yaml'))
        self.assertTrue(Path(path).is_file())
        self.assertIn(str(work.parent), path)

    def test_trial_refuses_a_missing_rule_provider(self):
        self.config.write_text(
            CONFIG.format(pool=self.pool) + 'rule-providers:\n  gone:\n    type: file\n'
            '    path: %s\n' % (self.root / 'nope.yaml'), encoding='utf-8')
        instance = self.instance()
        work = instance._make_work_path(self.config)
        with self.assertRaises(tx.TxError) as ctx:
            instance._build_trial_config(work, None)
        self.assertIn('missing path', str(ctx.exception))

    def test_trial_refuses_a_missing_provider_file(self):
        self.pool.unlink()
        instance = self.instance()
        work = instance._make_work_path(self.config)
        with self.assertRaises(tx.TxError) as ctx:
            instance._build_trial_config(work, None)
        self.assertIn('missing path', str(ctx.exception))

    def test_validation_runs_the_trial_shape(self):
        instance = self.instance()
        captured = {}

        def fake_run(argv, **kwargs):
            captured['argv'] = argv
            captured['config'] = Path(argv[argv.index('-f') + 1]).read_text(encoding='utf-8')
            return Mock(returncode=0, stdout=b'ok', stderr=b'')

        with patch('subprocess.run', side_effect=fake_run):
            self.assertTrue(instance.validate_candidate(self.config))

        self.assertIn('-d', captured['argv'])
        home = captured['argv'][captured['argv'].index('-d') + 1]
        self.assertEqual(home, str(instance._make_work_path(self.config).parent))
        # The validated file must not carry the live provider path.
        self.assertNotIn(str(self.pool), captured['config'])

    def test_validation_failure_reports_the_reason(self):
        instance = self.instance()
        failure = Mock(returncode=1, stdout=b'', stderr=b'parse proxy provider error: nope')
        with patch('subprocess.run', return_value=failure):
            with self.assertRaises(tx.TxError) as ctx:
                instance.validate_candidate(self.config)
        self.assertIn('parse proxy provider error', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
