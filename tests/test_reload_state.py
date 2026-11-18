"""C07/C04: a failed reload must leave a pending marker, and the next run must retry it.

The audit's counterexample: sync wrote the rulesets, the controller reload failed, the state
file still said everything was fine, and the identical next run saw "nothing changed" and did
not retry - so the persisted bytes were never the applied runtime.

These tests drive the helper against a real local HTTP server: the pending marker, the retry
on an unchanged run, the clear on success and the exit codes are observed, not inferred.
"""
import http.server
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / 'assets' / 'geomask-reload-state.py'


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200
    calls = []
    seen = threading.Event()

    def do_PUT(self):  # noqa: N802 - http.server API
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length).decode('utf-8', 'replace')
        type(self).calls.append({'body': body, 'headers': dict(self.headers)})
        type(self).seen.set()
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, *args):
        pass


class ReloadStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(('127.0.0.1', 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.status = 200
        _Handler.calls = []
        _Handler.seen.clear()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='gm-reload-'))
        self.pending = self.tmp / 'reload.pending'
        self.state = self.tmp / 'sync.state'
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, *extra):
        return subprocess.run(
            [sys.executable, str(TOOL), '--url',
             'http://127.0.0.1:%d/configs?force=true' % self.port,
             '--pending', str(self.pending), '--state', str(self.state)] + list(extra),
            capture_output=True, text=True, encoding='utf-8', timeout=60)

    def result(self, completed):
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_a_successful_reload_leaves_no_pending_marker(self):
        completed = self.run_tool('--needed', '--reason', 'rulesets changed')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(self.result(completed)['ok'])
        self.assertFalse(self.pending.exists())
        self.assertEqual(_Handler.calls[0]['body'], '{"path": "/etc/mihomo/config.yaml"}')
        state = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertEqual(state['reload'], 'ok')
        self.assertFalse(state['pending'])

    def test_a_failed_reload_records_pending_and_exits_nonzero(self):
        _Handler.status = 500
        completed = self.run_tool('--needed', '--reason', 'rulesets changed')
        self.assertEqual(completed.returncode, 1,
                         'a failed reload was reported as success: ' + completed.stdout)
        payload = self.result(completed)
        self.assertTrue(payload['pending'])
        self.assertEqual(payload['attempts'], 1)
        marker = json.loads(self.pending.read_text(encoding='utf-8'))
        self.assertEqual(marker['attempts'], 1)
        self.assertEqual(marker['reason'], 'rulesets changed')
        state = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertEqual(state['reload'], 'failed')
        self.assertTrue(state['pending'])

    def test_an_unchanged_run_retries_while_the_marker_is_there(self):
        """The counterexample from the audit: identical bytes, but runtime not applied."""
        _Handler.status = 500
        self.run_tool('--needed', '--reason', 'first')
        self.assertTrue(self.pending.exists())
        _Handler.status = 200
        _Handler.calls = []
        completed = self.run_tool()  # nothing changed this run
        payload = self.result(completed)
        self.assertTrue(payload['attempted'], 'an unchanged run ignored the pending reload')
        self.assertTrue(payload['ok'])
        self.assertFalse(self.pending.exists())
        self.assertEqual(len(_Handler.calls), 1)
        self.assertEqual(completed.returncode, 0)

    def test_nothing_to_do_makes_no_request(self):
        completed = self.run_tool()
        payload = self.result(completed)
        self.assertFalse(payload['attempted'])
        self.assertEqual(_Handler.calls, [])
        self.assertEqual(completed.returncode, 0)
        state = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertIsNone(state['reload'])

    def test_attempts_keep_growing_while_the_runtime_stays_stale(self):
        _Handler.status = 500
        self.run_tool('--needed')
        self.run_tool()
        self.run_tool()
        marker = json.loads(self.pending.read_text(encoding='utf-8'))
        self.assertEqual(marker['attempts'], 3)
        self.assertTrue(marker['since'], 'the marker lost the moment the reload first failed')

    def test_a_damaged_marker_is_treated_as_pending(self):
        self.pending.write_text('{not json', encoding='utf-8')
        completed = self.run_tool()
        payload = self.result(completed)
        self.assertTrue(payload['attempted'],
                        'a damaged marker was read as "nothing pending"')
        self.assertTrue(payload['ok'])

    def test_controller_secret_header_is_sent(self):
        completed = self.run_tool('--needed', '--header', 'Authorization: Bearer SENTINEL')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        headers = _Handler.calls[0]['headers']
        self.assertEqual(headers.get('Authorization'), 'Bearer SENTINEL')

    def test_an_unreachable_controller_is_pending_not_success(self):
        completed = subprocess.run(
            [sys.executable, str(TOOL), '--url', 'http://127.0.0.1:9/configs',
             '--pending', str(self.pending), '--needed', '--timeout', '1'],
            capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(self.pending.exists())

    def test_a_slow_controller_times_out_into_pending(self):
        import time

        class Slow(_Handler):
            def do_PUT(self):  # noqa: N802
                time.sleep(3)
                super().do_PUT()

        server = http.server.HTTPServer(('127.0.0.1', 0), Slow)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            completed = subprocess.run(
                [sys.executable, str(TOOL), '--url', 'http://127.0.0.1:%d/configs' % port,
                 '--pending', str(self.pending), '--needed', '--timeout', '0.3'],
                capture_output=True, text=True, encoding='utf-8', timeout=60)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(self.pending.exists())

    def test_state_merge_keeps_the_other_fields(self):
        self.state.write_text(json.dumps({'changed': True, 'mt': 5, 'ai': 2}), encoding='utf-8')
        self.run_tool('--needed')
        state = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertTrue(state['changed'])
        self.assertEqual(state['mt'], 5)


if __name__ == '__main__':
    unittest.main()
