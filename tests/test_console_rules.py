"""Console lane rules: exact ownership, one rule per switch, and honest failures.

The helpers are extracted from the shipped backend and run against a stateful `ip` stub, so the
tests prove the exact selectors that get deleted or added - not a copy of the logic.

Audit 18.11.2026: C03 (A->B left both rules, so the next owner of A's DHCP lease inherited the
policy), C08 (a foreign rule to table 102 was swept), C09 (a failed `ip rule show` counted as
"no rule left" and the kill switch reported success).
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
SOURCE = BACKEND.read_text(encoding='utf-8')


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()

IP_STUB = r'''ip() {
  case "$1" in
    rule)
      case "$2" in
        show)
          [ "${FAIL_SHOW:-0}" = 1 ] && return 1
          cat "$CASE/rules" ;;
        add)
          [ "${FAIL_ADD:-0}" = 1 ] && return 1
          shift 2
          _pref=""; _from=""; _tbl=""
          while [ $# -gt 0 ]; do
            case "$1" in
              pref) _pref=$2; shift 2 ;;
              from) _from=$2; shift 2 ;;
              lookup) _tbl=$2; shift 2 ;;
              *) shift ;;
            esac
          done
          printf '%s:\tfrom %s lookup %s\n' "$_pref" "$_from" "$_tbl" >> "$CASE/rules"
          printf '%s\n' "add pref $_pref from $_from lookup $_tbl" >> "$CASE/deleted" ;;
        del)
          [ "${FAIL_DEL:-0}" = 1 ] && return 1
          shift 2
          _pref=""; _from=""; _tbl=""
          while [ $# -gt 0 ]; do
            case "$1" in
              pref) _pref=$2; shift 2 ;;
              from) _from=$2; shift 2 ;;
              lookup) _tbl=$2; shift 2 ;;
              *) shift ;;
            esac
          done
          _key="from $_from lookup $_tbl"
          grep -qE "^${_pref}:[[:space:]]+$_key$" "$CASE/rules" 2>/dev/null || return 1
          printf '%s\n' "del pref $_pref $_key" >> "$CASE/deleted"
          grep -vE "^${_pref}:[[:space:]]+$_key$" "$CASE/rules" > "$CASE/rules.new" 2>/dev/null
          mv -f "$CASE/rules.new" "$CASE/rules" ;;
        *) return 1 ;;
      esac ;;
    route) return 0 ;;
    *) return 0 ;;
  esac
}
conntrack() { printf '%s\n' "$*" >> "$CASE/conntrack"; return 0; }
_gm_sync_routes() { return "${SYNC_RC:-0}"; }
'''


def extract_function(name):
    start = SOURCE.index(name + '() {')
    return SOURCE[start:].split('\n}\n', 1)[0] + '\n}\n'


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleRuleTests(unittest.TestCase):
    FUNCTIONS = ('_console_own_rules', '_console_rule_off', '_console_rule_on')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'deleted').write_text('', encoding='utf-8')
        (self.case / 'conntrack').write_text('', encoding='utf-8')

    def run_fn(self, rules, call, env=None):
        (self.case / 'rules').write_text(''.join(r + '\n' for r in rules),
                                         encoding='utf-8', newline='\n')
        environment = {
            'CASE': self.case.as_posix(),
            'CONSOLE_TABLE': '102',
            'CONSOLE_RULE_PREF': '19903',
            'CONSOLE_TUN_DEV': 'Console',
            'CONSOLE_ROUTE_WAIT': '2',
            'TMPDIR': self.case.as_posix(),
        }
        environment.update(env or {})
        assignments = '\n'.join('%s="%s"' % item for item in environment.items())
        script = ('set -u\n' + assignments
                  + '\n' + IP_STUB
                  + '\n'.join(extract_function(name) for name in self.FUNCTIONS)
                  + '\n' + call + '\n')
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, path.as_posix()], cwd=str(self.case),
                              capture_output=True, text=True, encoding='utf-8', timeout=30)

    def remaining(self):
        return [line for line in (self.case / 'rules').read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def deleted(self):
        return (self.case / 'deleted').read_text(encoding='utf-8').split('\n')

    # ---------------------------------------------------------------- ownership
    def test_only_our_pref_rule_is_removed(self):
        rules = ['19903:\tfrom 192.168.1.177 lookup 102',
                 '19903:\tfrom 10.0.0.5 lookup 1020',
                 '19900:\tfrom 192.168.1.50 lookup 101']
        result = self.run_fn(rules, '_console_rule_off "192.168.1.177"; printf "RC=%s\\n" "$?"')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=0', result.stdout)
        self.assertEqual(self.remaining(), rules[1:])
        deleted = ' '.join(self.deleted())
        self.assertIn('pref 19903', deleted, 'the delete selector does not name ownership')
        self.assertIn('from 192.168.1.177', deleted)

    def test_a_foreign_rule_to_the_same_table_survives(self):
        # C08: a rule of another writer that points at table 102 is not ours to delete.
        rules = ['15000:\tfrom 10.9.9.9 lookup 102',
                 '19903:\tfrom 192.168.1.177 lookup 102']
        result = self.run_fn(rules, '_console_rule_off ""; printf "RC=%s\\n" "$?"')
        self.assertIn('RC=0', result.stdout, result.stdout)
        self.assertEqual(self.remaining(), ['15000:\tfrom 10.9.9.9 lookup 102'])

    def test_an_unreadable_rule_table_is_not_success(self):
        # C09: a failed `ip rule show` must not look like "no rule left".
        rules = ['19903:\tfrom 192.168.1.177 lookup 102']
        result = self.run_fn(rules, '_console_rule_off ""; printf "RC=%s\\n" "$?"',
                             env={'FAIL_SHOW': '1'})
        self.assertIn('RC=2', result.stdout, result.stdout)
        self.assertEqual(self.remaining(), rules)
        self.assertEqual(self.deleted(), [''])

    def test_a_rule_that_cannot_be_deleted_is_reported(self):
        rules = ['19903:\tfrom 192.168.1.177 lookup 102']
        result = self.run_fn(rules, '_console_rule_off "192.168.1.177"; printf "RC=%s\\n" "$?"',
                             env={'FAIL_DEL': '1'})
        self.assertIn('RC=1', result.stdout)
        self.assertEqual(self.remaining(), rules)

    def test_conntrack_is_flushed_only_when_something_was_removed(self):
        rules = ['19903:\tfrom 192.168.1.177 lookup 102']
        self.run_fn(rules, '_console_rule_off "192.168.1.177" >/dev/null 2>&1')
        self.assertIn('192.168.1.177', (self.case / 'conntrack').read_text(encoding='utf-8'))

    # ---------------------------------------------------------------- switching
    def test_a_switch_keeps_only_the_new_address(self):
        # C03: A->B used to leave both rules; the next owner of A's lease kept the policy.
        rules = ['19903:\tfrom 192.168.1.177 lookup 102',
                 '15000:\tfrom 10.9.9.9 lookup 102']
        result = self.run_fn(rules,
                             '_console_rule_on "192.168.1.200"; printf "RC=%s\\n" "$?"')
        self.assertIn('RC=0', result.stdout, result.stdout)
        self.assertEqual(self.remaining(),
                         ['15000:\tfrom 10.9.9.9 lookup 102',
                          '19903:\tfrom 192.168.1.200 lookup 102'])

    def test_a_switch_that_cannot_add_a_rule_is_reported(self):
        rules = ['19903:\tfrom 192.168.1.177 lookup 102']
        result = self.run_fn(rules, '_console_rule_on "192.168.1.200"; printf "RC=%s\\n" "$?"',
                             env={'FAIL_ADD': '1'})
        self.assertIn('RC=77', result.stdout, result.stdout)

    def test_a_switch_without_a_readable_table_writes_nothing(self):
        rules = ['19903:\tfrom 192.168.1.177 lookup 102']
        result = self.run_fn(rules, '_console_rule_on "192.168.1.200"; printf "RC=%s\\n" "$?"',
                             env={'FAIL_SHOW': '1'})
        self.assertIn('RC=2', result.stdout, result.stdout)
        self.assertEqual(self.remaining(), rules)
        self.assertEqual(self.deleted(), [''])


if __name__ == '__main__':
    unittest.main()
