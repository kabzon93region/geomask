"""The config round-trip must not change the meaning of YAML 1.1 booleans.

A real apply failed with mihomo's "invalid find process mode": the live config contains
`find-process-mode: off`, PyYAML's default resolver read that as False, and re-dumping the
config wrote `false`, which mihomo rejects. These tests pin the loader that prevents it.
"""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parents[1] / 'assets'

def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

state = _load('audit_state_yaml', 'geomask-audit-state.py')
tx = _load('config_tx_yaml', 'geomask-config-tx.py')
apply = _load('apply_yaml', 'geomask-apply.py')

DOC = """\
find-process-mode: off
mixed-port: 7890
sniffer:
  enable: yes
  override-destination: no
tun:
  enable: false
  stack: system
"""


class Yaml12LoaderTests(unittest.TestCase):
    def test_mode_strings_stay_strings(self):
        loaded = state.yaml12_load(DOC)
        self.assertEqual(loaded['find-process-mode'], 'off')
        self.assertEqual(loaded['sniffer']['enable'], 'yes')
        self.assertEqual(loaded['sniffer']['override-destination'], 'no')

    def test_real_booleans_stay_booleans(self):
        loaded = state.yaml12_load(DOC)
        self.assertIs(loaded['tun']['enable'], False)
        self.assertIs(state.yaml12_load('x: true')['x'], True)
        self.assertIs(state.yaml12_load('x: TRUE')['x'], True)

    def test_round_trip_keeps_the_mode_readable(self):
        import yaml
        loaded = state.yaml12_load(DOC)
        dumped = yaml.safe_dump(loaded, allow_unicode=True, sort_keys=False)
        self.assertIn("'off'", dumped)
        again = state.yaml12_load(dumped)
        self.assertEqual(again['find-process-mode'], 'off')

    def test_plain_safe_load_would_break_it(self):
        """Documents the hazard this loader exists to avoid."""
        import yaml
        plain = yaml.safe_load(DOC)
        self.assertIs(plain['find-process-mode'], False)
        self.assertIn('false', yaml.safe_dump({'m': plain['find-process-mode']}))

    def test_all_three_modules_use_the_loader(self):
        self.assertEqual(apply.yaml_load(DOC)['find-process-mode'], 'off')
        self.assertEqual(tx.__dict__.get('_state') and
                         tx._state.yaml12_load(DOC)['find-process-mode'], 'off')

    def test_config_tx_reads_the_trial_config_with_it(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'config.yaml'
            cfg.write_text(DOC + 'proxy-groups:\n  - name: VPN\n    type: select\n'
                                 '    use: [sub2]\n', encoding='utf-8')
            state_dir = Path(tmp) / 'state'
            with patch.object(tx, 'TX_STATE_DIR', state_dir), \
                 patch.object(tx, 'TX_PREIMAGES_DIR', state_dir / 'pre'), \
                 patch.object(tx, 'TX_JOURNAL', state_dir / 'journal.jsonl'):
                instance = tx.ConfigTransaction('yaml12')
                instance.stage_file(cfg, cfg.read_bytes())
                work = instance._make_work_path(cfg)
                built = instance._build_trial_config(work, None)
            self.assertEqual(built['find-process-mode'], 'off')


if __name__ == '__main__':
    unittest.main()
