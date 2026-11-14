"""Strict DNS with an emergency reserve: the order and the rollback are the whole point.

The runner puts a stateful `uci` stub, a `nslookup` stub and a probe stub in front of the real
script. Nothing outside the temporary directory is read or written.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'assets' / 'geomask-dnsguard.sh'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()

STUB = r'''#!/bin/sh
# stateful uci stub: one file per option, list options are one value per line
uci() {
  action="$1"; shift
  if [ "$action" = "-q" ]; then action="$1"; shift; fi
  key="${1:-}"
  case "$action" in
    get)
      case "$key" in
        'dhcp.@dnsmasq[0].server') tr '\n' ' ' < "$CASE/servers" ;;
        'dhcp.@dnsmasq[0].strictorder') cat "$CASE/strictorder" 2>/dev/null ;;
        *) return 1 ;;
      esac ;;
    delete)
      case "$key" in
        'dhcp.@dnsmasq[0].server') : > "$CASE/servers" ;;
        'dhcp.@dnsmasq[0].strictorder') rm -f "$CASE/strictorder" ;;
        *) return 1 ;;
      esac ;;
    add_list)
      [ "$FAIL_ADD" = 1 ] && return 1
      value=${key#*=}
      printf '%s\n' "$value" >> "$CASE/servers" ;;
    set)
      value=${key#*=}
      case "$key" in
        'dhcp.@dnsmasq[0].server='*)
          [ "$FAIL_SET" = 1 ] && return 1
          : > "$CASE/servers"
          for word in $value; do printf '%s\n' "$word" >> "$CASE/servers"; done ;;
        'dhcp.@dnsmasq[0].strictorder='*)
          [ "$FAIL_SET" = 1 ] && return 1
          printf '%s' "$value" > "$CASE/strictorder" ;;
        *) return 1 ;;
      esac ;;
    commit)
      [ "$FAIL_COMMIT" = 0 ] ;;
    *) return 1 ;;
  esac
}
nslookup() {
  printf '%s\n' "$*" >> "$CASE/probes"
  case "$DNS_MODE" in
    ok) printf 'Server:\t\t127.0.0.1\nAddress 1:\t127.0.0.1\n\nName:\t\texample.com\nAddress 1:\t93.184.216.34\n' ;;
    servfail) printf 'Server:\t\t127.0.0.1\nAddress 1:\t127.0.0.1\n\n** server can not find example.com: SERVFAIL\n'; return 1 ;;
    blocked) printf 'Server:\t\t127.0.0.1\nAddress 1:\t127.0.0.1\n\nName:\t\texample.com\nAddress 1:\t0.0.0.0\n' ;;
    *) return 1 ;;
  esac
}
sleep() { :; }
sleep() { :; }
'''


@unittest.skipIf(BASH is None, 'bash is required')
class DnsGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'servers').write_text('', encoding='utf-8', newline='\n')
        self.init = self.case / 'dnsmasq-init'
        self.probe = self.case / 'probe'
        self.probe.write_text('#!/bin/sh\ncat >/dev/null\nexit ${FAKE_DOH_RC:-0}\n',
                              encoding='utf-8', newline='\n')
        os.chmod(self.probe, 0o755)

    # ---------------------------------------------------------------- helpers
    def set_list(self, servers, strict=None):
        (self.case / 'servers').write_text(''.join(s + '\n' for s in servers),
                                           encoding='utf-8', newline='\n')
        path = self.case / 'strictorder'
        if strict is not None:
            path.write_text(strict, encoding='utf-8')
        elif path.exists():
            path.unlink()

    def servers(self):
        return [line for line in (self.case / 'servers').read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def strict(self):
        path = self.case / 'strictorder'
        return path.read_text(encoding='utf-8') if path.exists() else None

    def run_guard(self, action, dns='ok', doh_rc=0, fail_set=0, fail_commit=0, restart_rc=0):
        self.init.write_text('#!/bin/sh\nexit %d\n' % restart_rc, encoding='utf-8', newline='\n')
        os.chmod(self.init, 0o755)
        header = ('CASE="%s"\nDNS_MODE=%s\nFAKE_DOH_RC=%s\nFAIL_SET=%s\nFAIL_COMMIT=%s\n'
                  'GM_DNSMASQ_INIT="%s"\nGM_PROBE_PY="%s"\nGM_DNSGUARD_STATE="%s"\n'
                  % (self.case.as_posix(), dns, doh_rc, fail_set, fail_commit,
                     self.init.as_posix(), self.probe.as_posix(),
                     (self.case / 'state').as_posix()))
        path = self.case / 'run.sh'
        path.write_text(header + STUB + SCRIPT.read_text(encoding='utf-8'),
                        encoding='utf-8', newline='\n')
        env = dict(os.environ, CASE=str(self.case), DNS_MODE=dns, FAKE_DOH_RC=str(doh_rc),
                   FAIL_SET=str(fail_set), FAIL_ADD='0', FAIL_COMMIT=str(fail_commit))
        return subprocess.run([BASH, path.as_posix(), action], cwd=str(self.case),
                              capture_output=True, text=True, encoding='utf-8', timeout=60,
                              env=env)

    # ---------------------------------------------------------------- apply
    def test_apply_puts_doh_first_then_the_reserve(self):
        self.set_list(['9.9.9.9', '127.0.0.1#5053', '/corp/10.0.0.53'])
        result = self.run_guard('apply')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"mode":"strict"', result.stdout)
        self.assertEqual(self.servers(),
                         ['127.0.0.1#5053', '1.1.1.1', '8.8.8.8', '9.9.9.9', '/corp/10.0.0.53'])
        self.assertEqual(self.strict(), '1')

    def test_servfail_is_not_reported_as_a_working_resolver(self):
        self.set_list(['127.0.0.1#5053'])
        result = self.run_guard('apply', dns='servfail')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('previous DNS settings restored', result.stdout)
        self.assertEqual(self.servers(), ['127.0.0.1#5053'])
        self.assertIsNone(self.strict())

    def test_a_dead_doh_worker_is_reported_as_emergency(self):
        self.set_list(['127.0.0.1#5053'])
        result = self.run_guard('apply', doh_rc=1)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('"mode":"emergency"', result.stdout)

    def test_an_unprobeable_worker_is_not_called_strict(self):
        self.set_list(['127.0.0.1#5053'])
        result = self.run_guard('apply', doh_rc=2)
        self.assertIn('"mode":"applied"', result.stdout)

    def test_apply_refuses_without_a_local_doh_upstream(self):
        self.set_list(['8.8.8.8'])
        result = self.run_guard('apply')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('nothing to trust', result.stdout)
        self.assertEqual(self.servers(), ['8.8.8.8'])

    def test_a_failed_list_write_restores_the_original(self):
        self.set_list(['127.0.0.1#5053', '/corp/10.0.0.53'])
        result = self.run_guard('apply', fail_set=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.servers(), ['127.0.0.1#5053', '/corp/10.0.0.53'])

    def test_a_failed_restart_restores_the_original(self):
        self.set_list(['127.0.0.1#5053'], strict='0')
        result = self.run_guard('apply', restart_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.servers(), ['127.0.0.1#5053'])
        self.assertEqual(self.strict(), '0')

    def test_a_second_apply_keeps_the_first_record(self):
        self.set_list(['127.0.0.1#5053'])
        self.run_guard('apply')
        self.run_guard('apply')
        result = self.run_guard('revert')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.servers(), ['127.0.0.1#5053'])
        self.assertIsNone(self.strict())

    # ---------------------------------------------------------------- revert
    def test_revert_restores_the_owner_list_and_strictorder(self):
        self.set_list(['127.0.0.1#5053', '/corp/10.0.0.53', '9.9.9.9'], strict='1')
        self.run_guard('apply')
        result = self.run_guard('revert')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.servers(), ['127.0.0.1#5053', '/corp/10.0.0.53', '9.9.9.9'])
        self.assertEqual(self.strict(), '1')

    def test_revert_without_a_record_changes_nothing(self):
        self.set_list(['1.1.1.1'])
        result = self.run_guard('revert')
        self.assertEqual(result.returncode, 0)
        self.assertIn('nothing to revert', result.stdout)
        self.assertEqual(self.servers(), ['1.1.1.1'])

    def test_revert_keeps_the_record_when_the_resolver_stays_dead(self):
        self.set_list(['127.0.0.1#5053'])
        self.run_guard('apply')
        result = self.run_guard('revert', dns='servfail')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.case / 'state').exists())

    # ---------------------------------------------------------------- status
    def test_status_off_without_the_policy(self):
        self.set_list(['127.0.0.1#5053'])
        result = self.run_guard('status')
        self.assertIn('"mode":"off"', result.stdout)

    def test_status_emergency_when_the_worker_is_dead(self):
        self.set_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        result = self.run_guard('status', doh_rc=1)
        self.assertIn('"mode":"emergency"', result.stdout)
        self.assertIn('"doh_probe":"fail"', result.stdout)

    def test_status_degraded_when_nothing_resolves(self):
        self.set_list(['127.0.0.1#5053', '1.1.1.1', '8.8.8.8'], strict='1')
        result = self.run_guard('status', dns='servfail')
        self.assertIn('"mode":"degraded"', result.stdout)
        self.assertIn('"resolution":false', result.stdout)


if __name__ == '__main__':
    unittest.main()
