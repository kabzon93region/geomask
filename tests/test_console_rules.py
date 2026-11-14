"""Console kill switch: remove our rules only, and never report a silent success.

The helper is extracted from the shipped backend and run against a stateful `ip` stub, so the
test proves the exact selectors that get deleted.
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
        show) cat "$CASE/rules" ;;
        del)
          [ "$FAIL_DEL" = 1 ] && return 1
          shift 2
          grep -qF "$*" "$CASE/rules" 2>/dev/null || return 1
          printf '%s\n' "$*" >> "$CASE/deleted"
          grep -vF "$*" "$CASE/rules" > "$CASE/rules.new" 2>/dev/null
          mv -f "$CASE/rules.new" "$CASE/rules"
          ;;
        *) return 1 ;;
      esac ;;
    *) return 0 ;;
  esac
}
conntrack() { printf '%s\n' "$*" >> "$CASE/conntrack"; return 0; }
'''


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleRuleOffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'deleted').write_text('', encoding='utf-8')
        (self.case / 'conntrack').write_text('', encoding='utf-8')

    def run_off(self, rules, arg='', fail_del=0):
        (self.case / 'rules').write_text(''.join(r + '\n' for r in rules),
                                         encoding='utf-8', newline='\n')
        script = ('set -u\nCASE="%s"\nFAIL_DEL=%s\nCONSOLE_TABLE=102\nTMPDIR="%s"\n'
                  % (self.case.as_posix(), fail_del, self.case.as_posix())
                  + IP_STUB + SOURCE[SOURCE.index('_console_rule_off()'):]
                  .split('\n}\n', 1)[0] + '\n}\n'
                  + '_console_rule_off "%s"; printf "RC=%%s\\n" "$?"\n' % arg)
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, path.as_posix()], cwd=str(self.case),
                              capture_output=True, text=True, encoding='utf-8', timeout=30)

    def remaining(self):
        return [line for line in (self.case / 'rules').read_text(encoding='utf-8').splitlines()
                if line.strip()]

    def deleted(self):
        return (self.case / 'deleted').read_text(encoding='utf-8').split('\n')

    def test_only_the_console_rule_is_removed(self):
        rules = ['19903:\tfrom 192.168.1.177 lookup 102',
                 '19903:\tfrom 10.0.0.5 lookup 1020',
                 '19900:\tfrom 192.168.1.50 lookup 101']
        result = self.run_off(rules, arg='192.168.1.177')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('RC=0', result.stdout)
        self.assertEqual(self.remaining(), rules[1:])
        # deletion is expressed by the full selector, never by a bare priority
        self.assertNotIn('pref', ' '.join(self.deleted()))

    def test_a_rule_that_cannot_be_deleted_is_reported(self):
        rules = ['19903:\tfrom 192.168.1.177 lookup 102']
        result = self.run_off(rules, arg='192.168.1.177', fail_del=1)
        self.assertIn('RC=1', result.stdout)
        self.assertEqual(self.remaining(), rules)

    def test_conntrack_is_flushed_only_when_something_was_removed(self):
        rules = ['19903:\tfrom 192.168.1.177 lookup 102']
        self.run_off(rules, arg='192.168.1.177')
        self.assertIn('192.168.1.177', (self.case / 'conntrack').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
