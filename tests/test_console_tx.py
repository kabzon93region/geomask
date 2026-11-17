"""Console switch-on is a transaction, not a hand-made copy of the live config.

Review 14-15.11.2026, point 4. `cmd_console on` patched a candidate in /tmp, validated it and
then did `cp` live -> .bak_console followed by `mv` candidate -> live. From that moment the old
bytes existed exactly once (`.bak_console`): a failed rollback left the patched file in place
while the answer still claimed "previous config restored", and the backup was never removed.

The switch now goes through `geomask-config-tx.py` (preimage, trial, reload, rollback), and the
rollback is the same CLI call with the pre-switch bytes. These tests run the real `cmd_console`
against one TemporaryDirectory: the config, the state file and the transaction journal all live
inside it. The only faked pieces are mihomo itself (a function that accepts everything) and the
controller HTTP (a function that answers with the status the test asks for).
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
CONSOLE_PY = ROOT / 'assets' / 'geomask-console.py'
TX_ASSET = ROOT / 'assets' / 'geomask-config-tx.py'

BACKEND_TEXT = BACKEND.read_text(encoding='utf-8')
# Only the console lane: the slot path has its own (unrelated) backup copy and must not be
# mistaken for a console one.
CONSOLE_SECTION = BACKEND_TEXT[BACKEND_TEXT.index('CONSOLE_PY='):
                               BACKEND_TEXT.index('\nDIAG_PY=')]

CONFIG = """\
mixed-port: 7890
mode: rule
proxy-providers:
  sub:
    type: http
    url: "https://example.invalid/sub"
    interval: 3600
proxy-groups:
  - name: VPN
    type: url-test
    use:
      - sub
rules:
  - MATCH,DIRECT
"""


def find_bash():
    import shutil
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def extract(name):
    """Whole function body, brace-balanced but heredoc-aware (same rule as test_backend_safety)."""
    lines = BACKEND_TEXT.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(name + '() {'):
            start = index
            break
    if start is None:
        raise AssertionError('function %s not found in the backend' % name)
    heredoc = None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        opener = re.search(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?", line)
        if opener is not None:
            heredoc = opener.group(1)
            continue
        if line.rstrip() == '}':
            return '\n'.join(lines[start:index + 1])
    raise AssertionError('function %s has no closing brace' % name)


def win(path):
    """A path both Git-bash and Windows Python accept: `C:/Users/...` (not `/c/Users/...`)."""
    return str(Path(path).resolve()).replace('\\', '/')


def sha(data):
    return hashlib.sha256(data).hexdigest()


# The transaction is real; only the two things a router supplies and a test cannot are taken
# out. Everything else - preimage, staging, atomic write, rollback - is the shipped code.
TX_STUB = '''\
"""Test double: the real transaction with mihomo and the controller replaced by no-ops."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types

spec = importlib.util.spec_from_file_location('geomask_config_tx', Path(%(asset)r))
tx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tx)

tx.ConfigTransaction.validate_candidate = lambda self, config_path=None: True
tx.ConfigTransaction.run_isolated_trial = lambda self, config_path=None, extra_listeners=None: True
tx.ConfigTransaction.reload_mihomo = lambda self, config_path: None
tx.ConfigTransaction.verify_runtime = lambda self, config_path, *a, **k: None

payload = sys.stdin.buffer.read()
log = os.environ.get('TX_CALLS')
if log:
    with open(log, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps({'argv': sys.argv[1:],
                                 'sha256': hashlib.sha256(payload).hexdigest(),
                                 'bytes': len(payload)}) + '\\n')
sys.stdin = types.SimpleNamespace(buffer=io.BytesIO(payload))
sys.exit(tx.main(sys.argv[1:]))
'''

HEADER = '''\
set -u
CASE="@CASE@"
MIHOMO_CONF="@CONFIG@"
MIHOMO_API=http://127.0.0.1:9090
CONSOLE_STATE="@STATE@"
CONSOLE_INITD="@INITD@"
CONSOLE_TABLE=102
CONSOLE_TUN_DEV=Console
CONSOLE_TUN_IP=198.21.0.1
CONSOLE_SOCKS_PORT=7893
CONSOLE_RULE_PREF=19903
CONSOLE_YML="@CASE@/console.yml"
CONSOLE_NFT="@CASE@/console.nft"
CONSOLE_PY="@CONSOLE_PY@"
TX_PY="@TX_PY@"
PYTHON_BIN="@PYTHON@"
CONSOLE_LOCK="@LOCK@"
CONSOLE_LOCK_TRIES=3
# R04: the backend holds the shared kernel lock through the shipped helper. The test host has
# no flock(2), so the helper selects its documented portable (O_EXCL) backend; the lock dir is
# inside the case tree like every other path here.
GM_LOCK_DIR="@CASE@/locks"
GM_LOCK_LIB="@LOCKLIB@"
. "$GM_LOCK_LIB"
LOG="@LOG@"
DEVICES="@CASE@/geo-devices.list"
TMPDIR="@CASE@"
_require_backup() { return 0; }
_console_tun_ensure() { return "${TUN_RC:-0}"; }
_console_fw_ensure() { return "${FW_RC:-0}"; }
_console_rule_on() { return "${RULE_RC:-0}"; }
ip() { return 0; }
conntrack() { return 0; }
mihomo() { return 0; }
netstat() { printf 'tcp 0 0 127.0.0.1:7893 0.0.0.0:* LISTEN\\n'; }
# The reload under test is the `PUT /configs?force=true` the switch issues after the commit.
controller_curl() {
  case "$*" in
    *"-X PUT"*) printf '%s' "${RELOAD_CODE:-200}" ;;
    *) printf '%s' "${VERSION_CODE:-200}" ;;
  esac
}
'''


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleSwitchTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.config = self.case / 'config.yaml'
        self.config.write_text(CONFIG, encoding='utf-8', newline='\n')
        self.state = self.case / 'console.state'
        self.state.write_text('enabled=0\nip=\n', encoding='utf-8', newline='\n')
        self.calls = self.case / 'tx-calls.jsonl'
        self.calls.write_text('', encoding='utf-8', newline='\n')
        self.tx_stub = self.case / 'tx-stub.py'
        self.tx_stub.write_text(TX_STUB % {'asset': str(TX_ASSET)}, encoding='utf-8',
                                newline='\n')

    def header(self):
        values = {
            'CASE': win(self.case),
            'CONFIG': win(self.config),
            'STATE': win(self.state),
            'INITD': win(self.case / 'hev-console'),
            'CONSOLE_PY': win(CONSOLE_PY),
            'TX_PY': win(self.tx_stub),
            'PYTHON': win(sys.executable),
            'LOCK': win(self.case / 'console.lock'),
            'LOCKLIB': win(ROOT / 'assets' / 'geomask-lock.sh'),
            'LOG': win(self.case / 'geo-sync.log'),
        }
        text = HEADER
        for key, value in values.items():
            text = text.replace('@%s@' % key, value)
        return text

    def run_backend(self, body, env=None):
        script = (self.header()
                  + '\n'.join(extract(name) for name in
                              ('_sha_file', '_console_get', '_console_set', '_ipv4_ok',
                               '_console_file_sha', '_console_cfg_restore',
                               '_console_on_fail', '_console_lock', '_console_unlock',
                               'cmd_console'))
                  + '\n' + body)
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        environment = dict(os.environ)
        environment.update({
            'GM_AUDIT_STATE': str(self.case / 'tx-state'),
            'GM_AUDIT_CONFIG': str(self.config),
            'TX_CALLS': str(self.calls),
        })
        environment.update(env or {})
        return subprocess.run([BASH, win(path)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=120, env=environment)

    def switch_on(self, env=None):
        return self.run_backend('cmd_console on 192.168.1.177 "" ""; printf "\\nRC=%s\\n" "$?"',
                                env=env)

    def recorded(self):
        return [json.loads(line) for line in
                self.calls.read_text(encoding='utf-8').splitlines() if line.strip()]

    def test_a_failed_switch_leaves_the_original_bytes(self):
        # The reload answers 500: the switch must put the pre-switch bytes back, prove it, and
        # leave nothing of its own behind - no .bak_console, no candidate, no preimage.
        original = self.config.read_bytes()
        result = self.switch_on({'RELOAD_CODE': '500'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=1', result.stdout)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.case / 'config.yaml.bak_console').exists())
        self.assertEqual(list(self.case.glob('geomask-console-orig.*')), [])
        self.assertEqual(list(self.case.glob('geomask-console-candidate.*')), [])
        self.assertEqual(self.state.read_text(encoding='utf-8'), 'enabled=0\nip=\n')
        self.assertIn('"ok":false', result.stdout)
        self.assertIn('"step":"reload"', result.stdout)
        self.assertIn('"restored":true', result.stdout)
        # The rollback is the same transaction CLI, fed with the pre-switch bytes.
        calls = self.recorded()
        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            self.assertEqual(call['argv'][:2], ['--commit', win(self.config)])
        # CAS on both directions (C01): the forward commit carries the digest of the live
        # bytes it was built from, and the rollback carries the digest of OUR post-image -
        # not "whatever is there now", which may be another writer's commit.
        for call in calls:
            self.assertEqual(len(call['argv']), 4, call['argv'])
            self.assertEqual(call['argv'][2], '--expect-sha256')
        self.assertNotEqual(calls[0]['sha256'], sha(original))   # the patched candidate went in
        self.assertEqual(calls[1]['sha256'], sha(original))      # the original bytes came back
        self.assertEqual(calls[1]['argv'][3], calls[0]['sha256'],
                         'the rollback is not tied to the bytes this switch installed')

    def test_a_second_on_keeps_the_same_bytes(self):
        # The acceptance criterion for the transaction: a repeated `on` is idempotent, so a
        # retried button does not rewrite the live config with a new digest.
        first = self.switch_on()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn('RC=0', first.stdout)
        after_first = self.config.read_bytes()
        self.assertNotEqual(after_first, CONFIG.encode())
        self.assertIn('enabled=1', self.state.read_text(encoding='utf-8'))

        second = self.switch_on()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('RC=0', second.stdout)
        self.assertEqual(self.config.read_bytes(), after_first)
        self.assertFalse((self.case / 'config.yaml.bak_console').exists())
        self.assertEqual(list(self.case.glob('geomask-console-orig.*')), [])
        calls = self.recorded()
        self.assertEqual(len(calls), 2, calls)
        # Patching an already patched config is a no-op: the second candidate is the live file.
        self.assertEqual(calls[1]['sha256'], sha(after_first))

    def test_a_failing_step_names_itself_and_restores(self):
        # The same rollback must cover the steps after the reload (TUN/firewall/rule), and the
        # answer must say which one failed instead of a generic "previous config restored".
        original = self.config.read_bytes()
        result = self.switch_on({'RULE_RC': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('"step":"rule"', result.stdout)
        self.assertIn('"restored":true', result.stdout)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.case / 'config.yaml.bak_console').exists())

    def test_a_late_foreign_commit_survives_the_rollback(self):
        # C01 of the 18.11 audit: our switch had already installed its candidate, then a panel
        # commit wrote new bytes, and only then the reload failed. The rollback used to write
        # the pre-switch copy unconditionally, destroying the newer commit.
        original = self.config.read_bytes()
        foreign = b'# committed by the panel after the console switch\n' + original
        foreign_path = self.case / 'foreign.yaml'
        foreign_path.write_bytes(foreign)
        body = (
            'controller_curl() {\n'
            '  case "$*" in\n'
            '    *"-X PUT"*) cp -p "%s" "$MIHOMO_CONF"; printf 500 ;;\n'
            '    *) printf 200 ;;\n'
            '  esac\n'
            '}\n'
            'cmd_console on 192.168.1.177 "" ""; printf "\\nRC=%%s\\n" "$?"'
            % win(foreign_path))
        result = self.run_backend(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=1', result.stdout)
        self.assertEqual(self.config.read_bytes(), foreign,
                         'the rollback overwrote a commit made by another writer')
        self.assertIn('"restored":false', result.stdout)
        self.assertIn('"conflict":true', result.stdout)
        self.assertEqual(len(list(self.case.glob('geomask-console-orig.*'))), 1,
                         'the only working copy was deleted after a refused restore')

    def test_the_restore_refuses_a_live_file_it_did_not_write(self):
        # No-mutation case: the transaction refused on its own, the live bytes belong to
        # somebody else. There is nothing of ours to undo, so nothing may be written.
        pre = self.case / 'pre.yaml'
        pre.write_bytes(b'pre: 1\n')
        cand = self.case / 'cand.yaml'
        cand.write_bytes(b'cand: 2\n')
        foreign = b'foreign: 3\n'
        self.config.write_bytes(foreign)
        body = ('_orig="%s"; _cand="%s"; _console_cfg_restore "$_orig"; '
                'printf "\\nRC=%%s\\n" "$?"' % (win(pre), win(cand)))
        result = self.run_backend(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=2', result.stdout)
        self.assertEqual(self.config.read_bytes(), foreign)
        self.assertEqual(self.recorded(), [], 'the restore committed bytes anyway')

    def test_the_switch_writes_only_through_the_transaction_cli(self):
        # Static: no `cp` of the live config into a backup, no `mv` of the candidate over it.
        # `assertFalse(x in text)` keeps the failure message readable (the text is 2000 lines).
        self.assertFalse('.bak_console' in CONSOLE_SECTION,
                         'the console switch still keeps a .bak_console copy')
        self.assertFalse('mv -f "$_cand" "$MIHOMO_CONF"' in CONSOLE_SECTION,
                         'the candidate is still moved over the live config by hand')
        self.assertFalse('cp -p "$MIHOMO_CONF" "$MIHOMO_CONF' in CONSOLE_SECTION,
                         'the live config is still copied by hand before the switch')
        self.assertTrue('TX_PY=${GM_TX:-/usr/lib/geomask/geomask-config-tx.py}' in BACKEND_TEXT,
                        'the transaction CLI is not wired into the backend')
        self.assertTrue('--commit "$MIHOMO_CONF" --expect-sha256 "$_base_sha" < "$_cand"'
                        in CONSOLE_SECTION,
                        'the candidate is not installed through the transaction CLI with its CAS base')
        # The rollback is the same CLI, fed with the private preimage the switch kept, and it
        # is bound to the bytes this lane installed (C01): a live file that changed elsewhere
        # is refused instead of overwritten.
        self.assertTrue('--commit "$MIHOMO_CONF" --expect-sha256 "$_live_sha" \\' in CONSOLE_SECTION
                        or '--commit "$MIHOMO_CONF" --expect-sha256 "$_live_sha"' in CONSOLE_SECTION,
                        'the rollback does not go through the same CLI with a CAS base')
        self.assertTrue('_console_file_sha' in CONSOLE_SECTION,
                        'the restore does not compare the live bytes with its own post-image')
        self.assertTrue('return 2' in CONSOLE_SECTION,
                        'a foreign commit is not distinguished from a failed restore')
        self.assertTrue('_console_cfg_restore "$_orig"' in CONSOLE_SECTION,
                        'a failed step does not restore the preimage it kept')
        # The preimage is private (0600) and named the way the plan asks for.
        self.assertTrue('geomask-console-orig.$$' in CONSOLE_SECTION,
                        'the preimage path is not /tmp/geomask-console-orig.$$')
        self.assertTrue('umask 077' in CONSOLE_SECTION, 'the preimage is not created under umask 077')
        self.assertTrue('chmod 600 "$_orig"' in CONSOLE_SECTION, 'the preimage is not 0600')


class TransactionCliTests(unittest.TestCase):
    """`--commit`/`--recover`: the shell can only rely on a documented JSON + rc contract."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location('geomask_config_tx_cli', TX_ASSET)
        cls.tx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tx)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.target = self.case / 'config.yaml'
        self.target.write_bytes(b'old: bytes\n')

    def run_main(self, argv, stdin=b'new: bytes\n'):
        import contextlib
        import io
        out = io.StringIO()
        with patch.object(sys, 'stdin', type('In', (), {'buffer': io.BytesIO(stdin)})), \
             contextlib.redirect_stdout(out):
            code = self.tx.main(argv)
        return code, out.getvalue()

    def test_commit_hands_the_bytes_to_start_transaction(self):
        with patch.object(self.tx, 'start_transaction',
                          return_value={'ok': True, 'tx_id': 'deadbeef'}) as fake:
            code, out = self.run_main(['--commit', str(self.target)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {'ok': True, 'tx_id': 'deadbeef'})
        files = fake.call_args.args[0]
        self.assertEqual([str(key) for key in files], [str(self.target)])
        self.assertEqual(list(files.values()), [b'new: bytes\n'])

    def test_a_refused_commit_is_json_and_a_non_zero_exit(self):
        with patch.object(self.tx, 'start_transaction',
                          side_effect=self.tx.TxError('mihomo reload failed: HTTP 500')):
            code, out = self.run_main(['--commit', str(self.target)])
        self.assertEqual(code, 1)
        answer = json.loads(out)
        self.assertFalse(answer['ok'])
        self.assertIn('HTTP 500', answer['error'])

    def test_an_empty_candidate_is_refused_before_the_writer_runs(self):
        # An empty stdin would replace the live config with nothing: refuse it, do not write.
        with patch.object(self.tx, 'start_transaction') as fake:
            code, out = self.run_main(['--commit', str(self.target)], stdin=b'')
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)['ok'])
        fake.assert_not_called()

    def test_recover_runs_the_recovery_routine(self):
        with patch.object(self.tx, 'recover_interrupted_transactions') as fake:
            code, out = self.run_main(['--recover'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)['ok'])
        fake.assert_called_once_with()

    def test_an_unknown_argument_is_refused(self):
        code, out = self.run_main(['--frobnicate'])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)['ok'])

    def test_no_arguments_keeps_the_self_test(self):
        code, out = self.run_main([], stdin=b'')
        self.assertEqual(code, 0)
        self.assertIn('Config transaction module loaded OK', out)


if __name__ == '__main__':
    unittest.main()
