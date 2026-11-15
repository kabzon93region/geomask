"""Strict DNS with an emergency reserve: the uci TYPE, the order and the rollback.

The runner puts a real file-backed ``uci`` stub (``tests/uci_stub.py``, invoked through a
generated shell wrapper on PATH), an ``nslookup`` stub and a DoH probe stub in front of the
real script.  Nothing outside the temporary directory is read or written.

``GM_DNSGUARD_SCRIPT`` points the tests at another copy of the asset (used for the red proof
against the previous release); it defaults to ``assets/geomask-dnsguard.sh``.
"""
import os
from pathlib import Path
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / 'tests' / 'uci_stub.py'


def script_path():
    override = os.environ.get('GM_DNSGUARD_SCRIPT')
    return Path(override).resolve() if override else ROOT / 'assets' / 'geomask-dnsguard.sh'


SCRIPT = script_path()


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def msys_path(path):
    """A Git-Bash PATH entry (``/c/...``); Windows drive paths are not searched by bash."""
    text = Path(path).as_posix()
    match = re.match(r'^([A-Za-z]):/(.*)$', text)
    if match:
        return '/' + match.group(1).lower() + '/' + match.group(2)
    return text


BASH = find_bash()

UCI = r'''#!/bin/sh
exec %s %s "$@"
'''

NSLOOKUP = r'''#!/bin/sh
printf '%s\n' "$*" >> "$CASE/probes"
case "$DNS_MODE" in
  ok)
    printf 'Server:\t\t127.0.0.1\nAddress 1:\t127.0.0.1\n\nName:\t\texample.com\nAddress 1:\t93.184.216.34\n' ;;
  servfail)
    printf 'Server:\t\t127.0.0.1\nAddress 1:\t127.0.0.1\n\n** server can not find example.com: SERVFAIL\n'
    return 1 ;;
  blocked)
    printf 'Server:\t\t127.0.0.1\nAddress 1:\t127.0.0.1\n\nName:\t\texample.com\nAddress 1:\t0.0.0.0\n' ;;
  *)
    return 1 ;;
esac
'''

PROBE = r'''#!/bin/sh
cat >/dev/null
exit ${FAKE_DOH_RC:-0}
'''


@unittest.skipIf(BASH is None, 'bash is required')
class DnsGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.config = self.case / 'etc' / 'config'
        self.config.mkdir(parents=True)
        self.delta = self.case / 'delta'
        self.delta.mkdir()
        (self.case / 'tmp').mkdir()
        self.bin = self.case / 'bin'
        self.bin.mkdir()
        self.dhcp = self.config / 'dhcp'
        self.state = self.case / 'dnsguard.state'
        self.counter = self.case / 'uci-stub-state'
        self.init = self.case / 'dnsmasq-init'
        self.probe = self.case / 'probe'
        self._write_wrappers()

    # ---------------------------------------------------------------- helpers
    def _write_wrappers(self):
        wrapper = UCI % (shlex.quote(sys.executable.replace('\\', '/')),
                         shlex.quote(STUB.as_posix()))
        uci = self.bin / 'uci'
        uci.write_text(wrapper, encoding='utf-8', newline='\n')
        nslookup = self.bin / 'nslookup'
        nslookup.write_text(NSLOOKUP, encoding='utf-8', newline='\n')
        self.probe.write_text(PROBE, encoding='utf-8', newline='\n')
        for path in (uci, nslookup, self.probe):
            os.chmod(path, 0o755)

    def write_dhcp(self, text):
        self.dhcp.write_text(text, encoding='utf-8', newline='\n')

    def dhcp_list(self, servers, strict=None):
        lines = ['config dnsmasq', "\toption domain 'lan'"]
        for value in servers:
            lines.append("\tlist server '%s'" % value)
        if strict is not None:
            lines.append("\toption strictorder '%s'" % strict)
        lines += ['', "config dhcp 'lan'", "\toption interface 'lan'", "\toption start '100'"]
        self.write_dhcp('\n'.join(lines) + '\n')

    def dhcp_string(self, value, strict=None):
        lines = ['config dnsmasq', "\toption domain 'lan'", "\toption server '%s'" % value]
        if strict is not None:
            lines.append("\toption strictorder '%s'" % strict)
        lines += ['', "config dhcp 'lan'", "\toption interface 'lan'"]
        self.write_dhcp('\n'.join(lines) + '\n')

    def read_dhcp(self):
        return self.dhcp.read_text(encoding='utf-8')

    def _entries(self, kind):
        pattern = re.compile(r"^\s*%s\s+server\s+'(.*)'\s*$" % kind, re.MULTILINE)
        return pattern.findall(self.read_dhcp())

    def server_lists(self):
        return self._entries('list')

    def server_options(self):
        return self._entries('option')

    def strict_option(self):
        match = re.search(r"^\s*option\s+strictorder\s+'(.*)'\s*$", self.read_dhcp(), re.MULTILINE)
        return match.group(1) if match else None

    def uci(self, *args):
        proc = subprocess.run([sys.executable, str(STUB), '-c', str(self.config),
                               '-t', str(self.delta), *args],
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=60)
        return proc

    def config_value(self, key):
        proc = self.uci('get', key)
        return proc.stdout.strip() if proc.returncode == 0 else ''

    def config_export(self):
        return self.uci('export', 'dhcp').stdout

    def state_value(self, key):
        if not self.state.exists():
            return ''
        for line in self.state.read_text(encoding='utf-8').splitlines():
            if line.startswith(key + '='):
                return line[len(key) + 1:]
        return ''

    def run_guard(self, action, dns='ok', doh_rc=0, restart_rc=0, fail_commit=None,
                  fail_addlist=None, fail_set=None, fail_delete=None, lie_commit=None,
                  extra_args=()):
        self.init.write_text('#!/bin/sh\nexit %d\n' % restart_rc, encoding='utf-8',
                             newline='\n')
        os.chmod(self.init, 0o755)
        env = dict(os.environ)
        for key in ('UCI_FAIL_COMMIT', 'UCI_FAIL_ADDLIST', 'UCI_FAIL_SET',
                    'UCI_FAIL_DELETE', 'UCI_LIE_COMMIT'):
            env.pop(key, None)
        env.update({
            'CASE': self.case.as_posix(),
            'DNS_MODE': dns,
            'FAKE_DOH_RC': str(doh_rc),
            'GM_DNSMASQ_INIT': self.init.as_posix(),
            'GM_PROBE_PY': self.probe.as_posix(),
            'GM_DNSGUARD_STATE': self.state.as_posix(),
            'GM_UCI_CONFIG_DIR': self.config.as_posix(),
            'GM_UCI_DELTA_DIR': self.delta.as_posix(),
            'UCI_CONFIG_DIR': self.config.as_posix(),
            'UCI_DELTA_DIR': self.delta.as_posix(),
            'UCI_STUB_STATE': self.counter.as_posix(),
            'TMPDIR': (self.case / 'tmp').as_posix(),
        })
        if fail_commit:
            env['UCI_FAIL_COMMIT'] = fail_commit
        if fail_addlist:
            env['UCI_FAIL_ADDLIST'] = fail_addlist
        if fail_set:
            env['UCI_FAIL_SET'] = fail_set
        if fail_delete:
            env['UCI_FAIL_DELETE'] = fail_delete
        if lie_commit:
            env['UCI_LIE_COMMIT'] = lie_commit
        inner = ('bin=$1; shift; export PATH="$bin:$PATH"; '
                 'runner=$1; shift; exec "$runner" "$0" "$@"')
        command = [BASH, '-c', inner, str(SCRIPT), msys_path(self.bin), BASH, action,
                   *extra_args]
        return subprocess.run(command, cwd=str(self.case), capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=120, env=env)

    def combined(self, result):
        return result.stdout + result.stderr

    # ---------------------------------------------------------------- apply: real list
    def test_apply_writes_real_list_entries_not_a_scalar(self):
        self.dhcp_list(['9.9.9.9', '127.0.0.1#5053', '/corp/10.0.0.53'])
        result = self.run_guard('apply')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertIn('"mode":"strict"', result.stdout)
        expected = ['127.0.0.1#5053', '1.1.1.1', '8.8.8.8', '9.9.9.9', '/corp/10.0.0.53']
        self.assertEqual(self.server_lists(), expected)
        self.assertEqual(self.server_options(), [],
                         'no option server line may exist after apply')
        exported = self.config_export()
        self.assertIn("list server '127.0.0.1#5053'", exported)
        self.assertIn("list server '/corp/10.0.0.53'", exported)
        self.assertNotIn('option server', exported)
        self.assertEqual(self.state_value('type'), 'list')
        self.assertEqual(self.state_value('version'), '2')
        self.assertEqual(self.state_value('count'), '3',
                         'the record describes the three original values, not the applied list')

    def test_state_records_a_verified_preimage_of_the_dhcp_file(self):
        self.dhcp_list(['127.0.0.1#5053'])
        preimage = self.dhcp.read_bytes()
        self.assertEqual(self.run_guard('apply').returncode, 0)
        recorded = self.state_value('preimage')
        self.assertTrue(recorded and Path(recorded).is_file(), recorded)
        self.assertEqual(Path(recorded).read_bytes(), preimage)
        self.assertEqual(len(self.state_value('preimage_sha256')), 64)

    def test_apply_leaves_no_candidate_and_only_the_dhcp_config(self):
        self.dhcp_list(['127.0.0.1#5053'])
        result = self.run_guard('apply')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(sorted(path.name for path in self.config.iterdir()), ['dhcp'])
        self.assertEqual(list((self.case / 'tmp').iterdir()), [],
                         'the private candidate directory must be removed')
        self.assertFalse(self.delta.exists(),
                         'the private live delta directory is removed after a successful commit')

    def test_the_asset_never_uses_one_directory_for_config_and_delta(self):
        # Real uci writes a pending delta to <savedir>/<package>; passing the same directory to
        # -c and -t would let the delta clobber the candidate copy before commit.
        text = SCRIPT.read_text(encoding='utf-8')
        pairs = re.findall(r'uci(?:\s+-q)?\s+-c\s+(\S+)\s+-t\s+(\S+)', text)
        self.assertGreaterEqual(len(pairs), 2, pairs)
        for config, delta in pairs:
            self.assertNotEqual(config.strip('"'), delta.strip('"'),
                                'uci -c and -t must name different directories: %s' % ((config, delta),))
        self.assertIn('$CAND/delta', text)
        self.assertNotIn('$1/delta', text)

    # ---------------------------------------------------------------- apply: rollbacks
    def test_a_failed_live_commit_restores_the_original_bytes(self):
        self.dhcp_list(['127.0.0.1#5053', '/corp/10.0.0.53'], strict='0')
        preimage = self.dhcp.read_bytes()
        result = self.run_guard('apply', fail_commit='dhcp:2')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertTrue(self.state.exists(), 'the record must survive a failed restore')
        self.assertIn('record kept', result.stdout)

    def test_a_failing_candidate_is_rejected_before_live_changes(self):
        self.dhcp_list(['127.0.0.1#5053', '/corp/10.0.0.53'], strict='0')
        preimage = self.dhcp.read_bytes()
        result = self.run_guard('apply', fail_addlist='1.1.1.1')
        output = self.combined(result)
        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertIn('candidate', output)
        self.assertIn('add_list', output)
        self.assertIn('live config untouched', output)
        self.assertFalse(self.state.exists(), 'a candidate failure must not record anything')

    def test_a_failing_candidate_commit_is_rejected_before_live_changes(self):
        self.dhcp_list(['127.0.0.1#5053'])
        preimage = self.dhcp.read_bytes()
        result = self.run_guard('apply', fail_commit='dhcp:1')
        output = self.combined(result)
        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertIn('candidate', output)
        self.assertIn('commit', output)

    def test_a_lying_live_commit_is_rejected_and_leaves_the_file(self):
        self.dhcp_list(['127.0.0.1#5053', '9.9.9.9'], strict='0')
        preimage = self.dhcp.read_bytes()
        result = self.run_guard('apply', lie_commit='dhcp:2')
        output = self.combined(result)
        self.assertNotEqual(result.returncode, 0, output)
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertTrue(self.state.exists())
        self.assertIn('not the one we wrote', output)

    def test_a_glob_value_is_preserved(self):
        self.dhcp_list(['*', '127.0.0.1#5053'])
        applied = self.run_guard('apply')
        self.assertEqual(applied.returncode, 0, self.combined(applied))
        self.assertEqual(self.server_lists(),
                         ['127.0.0.1#5053', '1.1.1.1', '8.8.8.8', '*'])
        reverted = self.run_guard('revert')
        self.assertEqual(reverted.returncode, 0, self.combined(reverted))
        self.assertEqual(self.server_lists(), ['*', '127.0.0.1#5053'])

    def test_a_value_with_a_space_is_preserved(self):
        self.dhcp_list(['weird value', '127.0.0.1#5053'])
        applied = self.run_guard('apply')
        self.assertEqual(applied.returncode, 0, self.combined(applied))
        self.assertEqual(self.server_lists(),
                         ['127.0.0.1#5053', '1.1.1.1', '8.8.8.8', 'weird value'])
        reverted = self.run_guard('revert')
        self.assertEqual(reverted.returncode, 0, self.combined(reverted))
        self.assertEqual(self.server_lists(), ['weird value', '127.0.0.1#5053'])

    def test_an_error_with_a_quote_is_valid_json(self):
        self.dhcp_list(['bad"value', '127.0.0.1#5053'])
        result = self.run_guard('apply', fail_addlist='bad"value')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        payload = json.loads(result.stdout)
        self.assertFalse(payload['ok'])
        self.assertIn('bad"value', payload['error'])

    def test_a_failed_restart_restores_the_original(self):
        self.dhcp_list(['127.0.0.1#5053'], strict='0')
        result = self.run_guard('apply', restart_rc=1)
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.server_lists(), ['127.0.0.1#5053'])
        self.assertEqual(self.server_options(), [])
        self.assertEqual(self.strict_option(), '0')

    def test_a_failed_live_delete_leaves_the_file_untouched(self):
        self.dhcp_list(['127.0.0.1#5053', '9.9.9.9'], strict='0')
        preimage = self.dhcp.read_bytes()
        result = self.run_guard('apply', fail_delete='dhcp.@dnsmasq[0].server:2')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertIn('live config untouched', result.stdout)

    def test_a_failed_live_add_list_leaves_the_file_untouched(self):
        self.dhcp_list(['127.0.0.1#5053', '9.9.9.9'], strict='0')
        preimage = self.dhcp.read_bytes()
        result = self.run_guard('apply', fail_addlist='1.1.1.1:2')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertIn('live config untouched', result.stdout)

    def test_a_failed_live_set_leaves_the_file_untouched(self):
        self.dhcp_list(['127.0.0.1#5053', '9.9.9.9'], strict='0')
        preimage = self.dhcp.read_bytes()
        result = self.run_guard('apply', fail_set='dhcp.@dnsmasq[0].strictorder:2')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertIn('live config untouched', result.stdout)

    def test_a_concurrent_guest_section_survives_a_failed_live_write(self):
        self.dhcp_list(['127.0.0.1#5053'])
        guest = "\nconfig dhcp 'guest'\n\toption interface 'guest'\n"

        def append_when_recorded():
            deadline = time.time() + 15
            while time.time() < deadline:
                if self.state.exists():
                    with open(self.dhcp, 'a', encoding='utf-8', newline='\n') as handle:
                        handle.write(guest)
                    return
                time.sleep(0.005)

        thread = threading.Thread(target=append_when_recorded, daemon=True)
        thread.start()
        result = self.run_guard('apply', fail_addlist='1.1.1.1:2')
        thread.join(20)
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        text = self.read_dhcp()
        self.assertIn("config dhcp 'guest'", text, 'an external edit must not be erased')
        self.assertEqual(self.server_lists(), ['127.0.0.1#5053'])
        self.assertIn('live config untouched', result.stdout)

    def test_a_manual_edit_survives_a_failed_second_apply(self):
        self.dhcp_list(['127.0.0.1#5053'])
        self.assertEqual(self.run_guard('apply').returncode, 0)
        applied = self.server_lists()
        guest = "\nconfig dhcp 'guest'\n\toption interface 'guest'\n"
        with open(self.dhcp, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(guest)
        result = self.run_guard('apply', fail_addlist='1.1.1.1:2')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertIn("config dhcp 'guest'", self.read_dhcp())
        self.assertEqual(self.server_lists(), applied)

    # ---------------------------------------------------------------- apply: behaviour
    def test_apply_puts_doh_first_then_the_reserve(self):
        self.dhcp_list(['9.9.9.9', '127.0.0.1#5053', '/corp/10.0.0.53'])
        result = self.run_guard('apply')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertIn('"mode":"strict"', result.stdout)
        self.assertEqual(self.server_lists(),
                         ['127.0.0.1#5053', '1.1.1.1', '8.8.8.8', '9.9.9.9',
                          '/corp/10.0.0.53'])
        self.assertEqual(self.strict_option(), '1')

    def test_servfail_is_not_reported_as_a_working_resolver(self):
        self.dhcp_list(['127.0.0.1#5053'])
        result = self.run_guard('apply', dns='servfail')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertIn('previous DNS settings restored', result.stdout)
        self.assertEqual(self.server_lists(), ['127.0.0.1#5053'])
        self.assertIsNone(self.strict_option())

    def test_a_dead_doh_worker_is_reported_as_emergency(self):
        self.dhcp_list(['127.0.0.1#5053'])
        result = self.run_guard('apply', doh_rc=1)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertIn('"mode":"emergency"', result.stdout)

    def test_an_unprobeable_worker_is_not_called_strict(self):
        self.dhcp_list(['127.0.0.1#5053'])
        result = self.run_guard('apply', doh_rc=2)
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertIn('"mode":"applied"', result.stdout)

    def test_apply_refuses_without_a_local_doh_upstream(self):
        self.dhcp_list(['8.8.8.8'])
        result = self.run_guard('apply')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertIn('nothing to trust', result.stdout)
        self.assertEqual(self.server_lists(), ['8.8.8.8'])
        self.assertFalse(self.state.exists(), 'a refused apply must not record anything')

    # ---------------------------------------------------------------- revert: type
    def test_revert_restores_a_previous_string_option_as_a_string(self):
        # A string option is invisible to dnsmasq, but the local DoH worker still has to be in
        # that string for the safety gate to pass; revert must keep the STRING type.
        value = '127.0.0.1#5053'
        self.dhcp_string(value)
        applied = self.run_guard('apply')
        self.assertEqual(applied.returncode, 0, self.combined(applied))
        self.assertEqual(self.server_lists()[:2], ['127.0.0.1#5053', '1.1.1.1'])
        self.assertEqual(self.server_options(), [])
        reverted = self.run_guard('revert')
        self.assertEqual(reverted.returncode, 0, self.combined(reverted))
        self.assertEqual(self.server_options(), [value])
        self.assertEqual(self.server_lists(), [])
        self.assertIn("option server '%s'" % value, self.config_export())

    def test_apply_migrates_a_previous_release_string_policy(self):
        # The previous release left one string with all upstreams; apply must find the DoH
        # worker inside it, replace it with a real list, and revert must restore the string.
        value = '127.0.0.1#5053 1.1.1.1 8.8.8.8'
        self.dhcp_string(value)
        applied = self.run_guard('apply')
        self.assertEqual(applied.returncode, 0, self.combined(applied))
        self.assertEqual(self.server_lists(), ['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'])
        self.assertEqual(self.server_options(), [])
        reverted = self.run_guard('revert')
        self.assertEqual(reverted.returncode, 0, self.combined(reverted))
        self.assertEqual(self.server_options(), [value])
        self.assertEqual(self.server_lists(), [])

    def test_revert_restores_the_absent_option(self):
        # apply cannot start from an absent list: with no server option there is no local DoH
        # worker and the safety gate refuses (test_apply_refuses_without_a_local_doh_upstream).
        # So this test supplies the record apply would have written for an absent option and
        # drives the restore path: revert must delete the applied list, not leave a string.
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        absent = '\n'.join(['config dnsmasq', "\toption domain 'lan'", '',
                            "config dhcp 'lan'", "\toption interface 'lan'"]) + '\n'
        preimage = self.case / 'absent.preimage'
        preimage.write_text(absent, encoding='utf-8', newline='\n')
        digest = hashlib.sha256(preimage.read_bytes()).hexdigest()
        self.state.write_text(
            'version=2\ntype=absent\nstrictorder_type=absent\npreimage=%s\n'
            'preimage_sha256=%s\ncount=0\nsaved=1\n'
            % (preimage.as_posix(), digest), encoding='utf-8', newline='\n')
        reverted = self.run_guard('revert')
        self.assertEqual(reverted.returncode, 0, self.combined(reverted))
        self.assertEqual(self.server_lists(), [])
        self.assertEqual(self.server_options(), [])
        self.assertIsNone(self.strict_option())
        self.assertNotIn('server', self.config_export())
        self.assertFalse(self.state.exists())

    def test_revert_upgrades_a_version_1_record(self):
        # The previous release recorded values as v1 and wrote a string. Upgrading must still
        # turn the policy off: restore the owner's values as a real list and their strictorder.
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        self.state.write_text(
            'version=1\nsaved=1\nstrictorder=absent\n'
            'server=127.0.0.1#5053\nserver=9.9.9.9\ncount=2\n',
            encoding='utf-8', newline='\n')
        result = self.run_guard('revert')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.server_lists(), ['127.0.0.1#5053', '9.9.9.9'])
        self.assertEqual(self.server_options(), [])
        self.assertIsNone(self.strict_option())
        self.assertFalse(self.state.exists())

    def test_a_version_1_record_without_its_values_is_refused(self):
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        preimage = self.dhcp.read_bytes()
        self.state.write_text(
            'version=1\nsaved=1\nstrictorder=absent\n'
            'server=127.0.0.1#5053\ncount=2\n',
            encoding='utf-8', newline='\n')
        result = self.run_guard('revert')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertTrue(self.state.exists())
        self.assertIn('incomplete', self.combined(result))
        self.assertIn(self.state.as_posix(), self.combined(result))
        self.assertIn('revert --force', self.combined(result))

    def test_a_v2_record_without_a_preimage_still_reverts(self):
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        self.state.write_text(
            'version=2\ntype=list\nstrictorder_type=absent\n'
            'server=127.0.0.1#5053\nserver=9.9.9.9\ncount=2\nsaved=1\n',
            encoding='utf-8', newline='\n')
        result = self.run_guard('revert')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.server_lists(), ['127.0.0.1#5053', '9.9.9.9'])
        self.assertIsNone(self.strict_option())
        self.assertIn('preimage missing', result.stdout)
        self.assertFalse(self.state.exists())

    def test_revert_force_drops_an_unusable_record_without_touching_live(self):
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        preimage = self.dhcp.read_bytes()
        self.state.write_text(
            'version=1\nsaved=1\nstrictorder=absent\n'
            'server=127.0.0.1#5053\ncount=2\n',
            encoding='utf-8', newline='\n')
        result = self.run_guard('revert', extra_args=['--force'])
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertFalse(self.state.exists())
        payload = json.loads(result.stdout)
        self.assertEqual(payload['note'], 'unusable record dropped, live config untouched')

    def test_an_empty_server_value_record_is_refused(self):
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        preimage = self.dhcp.read_bytes()
        self.state.write_text(
            'version=2\ntype=list\nstrictorder_type=string\nstrictorder=1\n'
            'server=\nserver=9.9.9.9\ncount=2\nsaved=1\n',
            encoding='utf-8', newline='\n')
        result = self.run_guard('revert')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.dhcp.read_bytes(), preimage)
        self.assertTrue(self.state.exists())
        self.assertIn('--force', self.combined(result))
        forced = self.run_guard('revert', extra_args=['--force'])
        self.assertEqual(forced.returncode, 0, self.combined(forced))
        self.assertFalse(self.state.exists())
        self.assertEqual(self.dhcp.read_bytes(), preimage)

    def test_inconsistent_v2_and_v1_records_are_refused(self):
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        preimage = self.dhcp.read_bytes()
        cases = {
            'v2 string with two values': (
                'version=2\ntype=string\nstrictorder_type=string\nstrictorder=1\n'
                'server=127.0.0.1#5053\nserver=9.9.9.9\ncount=2\nsaved=1\n'),
            'v2 absent with one value': (
                'version=2\ntype=absent\nstrictorder_type=string\nstrictorder=1\n'
                'server=9.9.9.9\ncount=1\nsaved=1\n'),
            'v2 list with no values': (
                'version=2\ntype=list\nstrictorder_type=string\nstrictorder=1\n'
                'count=0\nsaved=1\n'),
            'v2 list with an option marker': (
                'version=2\ntype=list\nhas_option_server=1\nstrictorder_type=string\n'
                'strictorder=1\nserver=9.9.9.9\ncount=1\nsaved=1\n'),
            'v1 strictorder absent plus a value': (
                'version=1\nsaved=1\nstrictorder=absent\nstrictorder=1\n'
                'server=9.9.9.9\ncount=1\n'),
        }
        for name, state in cases.items():
            with self.subTest(name):
                self.state.write_text(state, encoding='utf-8', newline='\n')
                result = self.run_guard('revert')
                self.assertNotEqual(result.returncode, 0, self.combined(result))
                self.assertEqual(self.dhcp.read_bytes(), preimage)
                self.assertTrue(self.state.exists())
                self.assertIn('--force', self.combined(result))
                forced = self.run_guard('revert', extra_args=['--force'])
                self.assertEqual(forced.returncode, 0, self.combined(forced))
                self.assertFalse(self.state.exists())
                self.assertEqual(self.dhcp.read_bytes(), preimage)

    def test_a_version_1_absent_record_still_reverts(self):
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1'], strict='1')
        self.state.write_text(
            'version=1\nsaved=1\nstrictorder=absent\ncount=0\n',
            encoding='utf-8', newline='\n')
        result = self.run_guard('revert')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.server_lists(), [])
        self.assertEqual(self.server_options(), [])
        self.assertIsNone(self.strict_option())
        self.assertFalse(self.state.exists())

    def test_revert_restores_the_owner_list_and_strictorder(self):
        self.dhcp_list(['127.0.0.1#5053', '/corp/10.0.0.53', '9.9.9.9'], strict='1')
        self.run_guard('apply')
        result = self.run_guard('revert')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.server_lists(), ['127.0.0.1#5053', '/corp/10.0.0.53', '9.9.9.9'])
        self.assertEqual(self.strict_option(), '1')

    def test_a_second_apply_keeps_the_first_record(self):
        self.dhcp_list(['127.0.0.1#5053'])
        self.assertEqual(self.run_guard('apply').returncode, 0)
        self.assertEqual(self.run_guard('apply').returncode, 0)
        result = self.run_guard('revert')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.server_lists(), ['127.0.0.1#5053'])
        self.assertIsNone(self.strict_option())

    def test_revert_without_a_record_changes_nothing(self):
        self.dhcp_list(['1.1.1.1'])
        result = self.run_guard('revert')
        self.assertEqual(result.returncode, 0, self.combined(result))
        self.assertIn('nothing to revert', result.stdout)
        self.assertEqual(self.server_lists(), ['1.1.1.1'])

    def test_revert_keeps_the_record_when_the_resolver_stays_dead(self):
        self.dhcp_list(['127.0.0.1#5053'])
        self.run_guard('apply')
        result = self.run_guard('revert', dns='servfail')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertTrue(self.state.exists())

    def test_a_failed_revert_puts_the_applied_policy_back(self):
        self.dhcp_list(['127.0.0.1#5053'])
        self.assertEqual(self.run_guard('apply').returncode, 0)
        applied = self.dhcp.read_bytes()
        result = self.run_guard('revert', fail_addlist='127.0.0.1#5053:1')
        self.assertNotEqual(result.returncode, 0, self.combined(result))
        self.assertEqual(self.dhcp.read_bytes(), applied,
                         'a failed restore must put the pre-revert policy back')
        self.assertTrue(self.state.exists())
        self.assertIn('pre-revert state', result.stdout)

    # ---------------------------------------------------------------- status
    def test_status_off_without_the_policy(self):
        self.dhcp_list(['127.0.0.1#5053'])
        result = self.run_guard('status')
        self.assertIn('"mode":"off"', result.stdout)

    def test_status_emergency_when_the_worker_is_dead(self):
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        result = self.run_guard('status', doh_rc=1)
        self.assertIn('"mode":"emergency"', result.stdout)
        self.assertIn('"doh_probe":"fail"', result.stdout)

    def test_status_degraded_when_nothing_resolves(self):
        self.dhcp_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        result = self.run_guard('status', dns='servfail')
        self.assertIn('"mode":"degraded"', result.stdout)
        self.assertIn('"resolution":false', result.stdout)


if __name__ == '__main__':
    unittest.main()
