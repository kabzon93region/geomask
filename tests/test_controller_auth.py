"""Targeted controller authentication regression tests (no router or shell needed)."""
import ast
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_headers(source):
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'controller_headers')
    namespace = {'json': json, 're': __import__('re')}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<headers>', 'exec'), namespace)
    return namespace['controller_headers']


class ControllerAuthTests(unittest.TestCase):
    def test_headers(self):
        installer = (ROOT / 'geomask.sh').read_text(encoding='utf-8')
        helper = installer.split("<<'GM_CONTROLLER_PY'\n", 1)[1].split('\nGM_CONTROLLER_PY', 1)[0]
        sync = (ROOT / 'assets/sync_mtrickle_mihomo.sh').read_text(encoding='utf-8')
        embedded = installer.split("save_stream /root/sync_mtrickle_mihomo.sh 755 <<'ASSET_EOF'\n", 1)[1].split('\nASSET_EOF', 1)[0]
        self.assertEqual(sync.rstrip(), embedded.rstrip())
        for source in (helper, sync.split("<<'PYEOF'\n", 1)[1].rsplit('\nPYEOF', 1)[0]):
            headers = load_headers(source)
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'config.yaml'
                for scalar, expected in [('abc # comment', 'abc'), ('"a#b\\\"c"', 'a#b"c'),
                                         ("'a''b # c' # comment", "a'b # c"), ('""', ''), ('', '')]:
                    config.write_text('secret: ' + scalar + '\n', encoding='utf-8')
                    self.assertEqual(headers(str(config)), {'Authorization': 'Bearer ' + expected} if expected else {})
                config.write_text('proxy-providers:\n  secret: nested\n', encoding='utf-8')
                self.assertEqual(headers(str(config)), {})
                for scalar in ['"bad\\nheader"', '|', '"unterminated']:
                    config.write_text('secret: ' + scalar + '\n', encoding='utf-8')
                    with self.assertRaises(ValueError):
                        headers(str(config))

    def test_all_embedded_python_compiles(self):
        import re
        installer = (ROOT / 'geomask.sh').read_text(encoding='utf-8')
        for delimiter in ('GM_CONTROLLER_PY', 'PYEOF'):
            for source in re.findall(r"<<'" + delimiter + r"'\n(.*?)\n" + delimiter,
                                     installer, re.S):
                compile(source, '<embedded>', 'exec')

    def test_backend_controller_calls_use_helper(self):
        source = (ROOT / 'assets/geo-backend.sh').read_text(encoding='utf-8')
        helper = source.split("<<'GM_CONTROLLER_PY'\n", 1)[1].split('\nGM_CONTROLLER_PY', 1)[0]
        load_headers(helper)
        for line in source.splitlines():
            if ('$MIHOMO_API/' in line or '9090/' in line) and 'curl ' in line:
                self.assertIn('controller_curl ', line)

    def test_installer_controller_calls_use_helper(self):
        source = (ROOT / 'geomask.sh').read_text(encoding='utf-8')
        # Remove deployed asset heredocs; those have independent runtimes.
        source = __import__('re').sub(r"<<'ASSET_EOF'\n.*?\nASSET_EOF", '', source, flags=__import__('re').S)
        for line in source.splitlines():
            if '9090/' in line and 'curl ' in line:
                self.assertIn('controller_curl ', line)


if __name__ == '__main__':
    unittest.main()
