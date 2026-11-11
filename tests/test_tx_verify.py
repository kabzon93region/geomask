"""Tests for the runtime postcheck: it must wait for async provider load, and it must
treat an empty pinned group as a failure rather than a success.
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

tx = _load('config_tx_verify', 'geomask-config-tx.py')

CONFIG = """\
secret: "s3cret"
proxy-groups:
  - name: VPN
    type: url-test
    use: [sub]
  - name: AI
    type: url-test
    use: [slot]
"""


class RuntimeVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.yaml'
        self.config.write_text(CONFIG, encoding='utf-8')
        self.tx = tx.ConfigTransaction('verify1')
        self.tx.config_digest_after = tx.digest(self.config.read_bytes())

    def _responses(self, sequence):
        """Return a urlopen stand-in that pops one response per /proxies/ call."""
        queue = list(sequence)

        def fake(req, *args, **kwargs):
            url = getattr(req, 'full_url', None) or req.get_full_url()
            resp = Mock()
            resp.__enter__ = Mock(return_value=resp)
            resp.__exit__ = Mock(return_value=False)
            if url.endswith('/version'):
                resp.read.return_value = b'{"version":"1"}'
                return resp
            name = url.rsplit('/', 1)[-1]
            if not queue:
                resp.read.return_value = json.dumps({'all': ['x'], 'now': 'x'}).encode()
                return resp
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            resp.read.return_value = json.dumps(item).encode()
            return resp
        return fake

    def test_waits_for_a_group_that_appears_late(self):
        """A group absent right after reload must be polled, not declared missing."""
        sequence = [
            Exception('404'),                      # VPN not there yet
            Exception('404'),                      # AI not there yet
            {'all': ['a'], 'now': 'a'},
            {'all': ['b'], 'now': 'b'},
        ]
        with patch('urllib.request.urlopen', side_effect=self._responses(sequence)), \
             patch.object(tx.time, 'sleep', return_value=None):
            self.tx.verify_runtime(self.config)

    def _by_name(self, table):
        """urlopen stand-in that answers per group name for the whole poll window."""
        def fake(req, *args, **kwargs):
            url = getattr(req, 'full_url', None) or req.get_full_url()
            resp = Mock()
            resp.__enter__ = Mock(return_value=resp)
            resp.__exit__ = Mock(return_value=False)
            if url.endswith('/version'):
                resp.read.return_value = b'{"version":"1"}'
                return resp
            name = url.rsplit('/', 1)[-1]
            item = table[name]
            if isinstance(item, Exception):
                raise item
            resp.read.return_value = json.dumps(item).encode()
            return resp
        return fake

    def test_empty_pinned_group_is_a_failure(self):
        """A pinned group that stays empty is a failed apply, not a quiet success."""
        with patch('urllib.request.urlopen',
                   side_effect=self._by_name({'VPN': {'all': [], 'now': None},
                                              'AI': {'all': ['b'], 'now': 'b'}})), \
             patch.object(tx.time, 'sleep', return_value=None), \
             patch.object(tx, 'RUNTIME_VERIFY_SECONDS', 0.0):
            with self.assertRaises(tx.TxError) as ctx:
                self.tx.verify_runtime(self.config, require_nonempty=['VPN'])
        self.assertIn('empty pool', str(ctx.exception))

    def test_empty_group_is_accepted_when_not_required(self):
        sequence = [
            {'all': [], 'now': None},
            {'all': ['b'], 'now': 'b'},
        ]
        with patch('urllib.request.urlopen', side_effect=self._responses(sequence)), \
             patch.object(tx.time, 'sleep', return_value=None):
            self.tx.verify_runtime(self.config)          # no requirement -> fine

    def test_group_that_never_appears_times_out(self):
        def always_404(req, *args, **kwargs):
            url = getattr(req, 'full_url', None) or req.get_full_url()
            if url.endswith('/version'):
                resp = Mock()
                resp.__enter__ = Mock(return_value=resp)
                resp.__exit__ = Mock(return_value=False)
                resp.read.return_value = b'{}'
                return resp
            raise Exception('404')
        with patch('urllib.request.urlopen', side_effect=always_404), \
             patch.object(tx.time, 'sleep', return_value=None), \
             patch.object(tx, 'RUNTIME_VERIFY_SECONDS', 0.0):
            with self.assertRaises(tx.TxError) as ctx:
                self.tx.verify_runtime(self.config)
        self.assertIn('missing from the live runtime', str(ctx.exception))

    def test_config_tampering_after_commit_is_detected(self):
        self.config.write_text(CONFIG + '\n# edited behind our back\n', encoding='utf-8')
        sequence = [{'all': ['a'], 'now': 'a'}, {'all': ['b'], 'now': 'b'}]
        with patch('urllib.request.urlopen', side_effect=self._responses(sequence)), \
             patch.object(tx.time, 'sleep', return_value=None):
            with self.assertRaises(tx.TxError) as ctx:
                self.tx.verify_runtime(self.config)
        self.assertIn('changed after commit', str(ctx.exception))


class TrialIsolationTests(unittest.TestCase):
    def test_trial_moves_every_inbound_port_and_disables_tun(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / 'c.yaml'
        cfg.write_text(
            'port: 7890\nsocks-port: 7891\nmixed-port: 7892\nredir-port: 7893\n'
            'tproxy-port: 7894\nexternal-controller: 0.0.0.0:9090\n'
            'tun:\n  enable: true\n  stack: system\n'
            'allow-lan: true\n',
            encoding='utf-8')
        instance = tx.ConfigTransaction('trial1')
        built = instance._build_trial_config(cfg, None)
        for key in ('port', 'socks-port', 'mixed-port', 'redir-port', 'tproxy-port'):
            self.assertEqual(built[key], 0, '%s must be disabled in the trial' % key)
        self.assertFalse(built['tun']['enable'])
        self.assertFalse(built['allow-lan'])
        self.assertEqual(built['bind-address'], '127.0.0.1')
        self.assertNotIn(':9090', built['external-controller'])
        self.assertNotEqual(built['secret'], 's3cret')


if __name__ == '__main__':
    unittest.main()
