"""Router-only fixes must survive source rebuilds, without live router tests."""
import ast
import re
import shlex
import types
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SCAN = (ROOT / 'assets' / 'geomask-node-scan.sh').read_text(encoding='utf-8')
BODY = SCAN.split("<<'PYEOF'\n", 1)[1].rsplit('\nPYEOF', 1)[0]
TREE = ast.parse(BODY)
GUARD = next(node for node in TREE.body if isinstance(node, ast.If)
             and "'--ai-guard'" in ast.unparse(node.test))


def compile_nodes(nodes, namespace):
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<isolated-guard>', 'exec'), namespace)


def init_command(source):
    match = re.search(r"save_stream /etc/init.d/ai-doh 755 <<'ASSET_EOF'\n(.*?)\nASSET_EOF", source, re.S)
    if not match:
        raise AssertionError('AI-DoH service heredoc not found')
    line = next(line for line in match.group(1).splitlines()
                if 'procd_set_param command ' in line)
    return shlex.split(line)[2:]


class AiGuardCarryoverTests(unittest.TestCase):
    def request_urls(self, base):
        opener = Mock()
        opener.open.return_value.read.return_value = b'loc=US\n'
        request = types.SimpleNamespace(Request=urllib.request.Request,
                                         ProxyHandler=Mock(), HTTPSHandler=Mock(),
                                         build_opener=Mock(return_value=opener))
        namespace = {'urllib': types.SimpleNamespace(request=request),
                     'ssl': types.SimpleNamespace(_create_unverified_context=Mock()),
                     'listener': 'http://127.0.0.1:7891', 'egress_url': base,
                     '_t': types.SimpleNamespace(time=Mock(side_effect=[1000.1, 1001.2]))}
        function = next(node for node in GUARD.body if isinstance(node, ast.FunctionDef)
                        and node.name == '_egress')
        compile_nodes([function], namespace)
        self.assertEqual(namespace['_egress'](), 'loc=US\n')
        self.assertEqual(namespace['_egress'](), 'loc=US\n')
        calls = opener.open.call_args_list
        for call in calls:
            self.assertEqual(call.kwargs['timeout'], 18)
            self.assertEqual(call.args[0].get_header('User-agent'), 'curl/8')
        return [call.args[0].full_url for call in calls]

    def test_egress_cache_bust_without_query(self):
        urls = self.request_urls('https://chatgpt.com/cdn-cgi/trace')
        self.assertNotEqual(urls[0], urls[1])
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query),
                         {'_': ['1000']})

    def test_egress_cache_bust_preserves_existing_query(self):
        urls = self.request_urls('https://example.test/trace?keep=one')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
        self.assertEqual(query, {'keep': ['one'], '_': ['1000']})
        self.assertEqual(urls[0].count('?'), 1)
        self.assertNotEqual(urls[0], urls[1])

    def test_guard_waits_after_select_before_probe(self):
        events = []
        namespace = {'members': ['test-node'], 'ai_group': 'AI', 'expect': 'US',
                     'liars': [], 're': re, '_adm_ok': set(),
                     '_api': lambda *args: events.append(('select', args)),
                     '_t': types.SimpleNamespace(sleep=lambda value: events.append(('sleep', value))),
                     '_egress': lambda: (events.append(('probe',)), 'loc=US\n')[1],
                     'log': lambda *args: None}
        loop = next(node for node in GUARD.body if isinstance(node, ast.For)
                    and isinstance(node.target, ast.Name) and node.target.id == 'm')
        compile_nodes([loop], namespace)
        self.assertEqual(events, [('select', ('/proxies/AI', {'name': 'test-node'})),
                                  ('sleep', 3.0), ('probe',)])
        self.assertEqual(namespace['liars'], [])

    def test_local_log_levels_are_preserved(self):
        function = next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'log')
        self.assertEqual(function.args.args[-1].arg, 'lv')
        self.assertIn('[%s]', ast.unparse(function))


class AiDohCarryoverTests(unittest.TestCase):
    def check_command(self, source):
        command = init_command(source)
        self.assertEqual(command[0], '/usr/sbin/https-dns-proxy')
        self.assertEqual(command[1::2], ['-a', '-p', '-r', '-t', '-b', '-u', '-g'])
        self.assertEqual(dict(zip(command[1::2], command[2::2])), {
            '-a': '127.0.0.1', '-p': '5055',
            '-r': 'https://cloudflare-dns.com/dns-query',
            '-t': 'socks5h://127.0.0.1:7891', '-b': '8.8.8.8,1.1.1.1',
            '-u': 'dnsai', '-g': 'dnsai'})

    def test_template_keeps_ai_socks_and_bootstrap(self):
        self.check_command((ROOT / 'geomask.sh.in').read_text(encoding='utf-8'))

    def test_built_installer_keeps_ai_socks_and_bootstrap(self):
        self.check_command((ROOT / 'geomask.sh').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
