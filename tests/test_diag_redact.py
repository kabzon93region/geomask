"""The diagnostic bundle redactor, run against the reviewer's real producer formats.

The old backend filter understood `option private_key '...'` - a form neither `uci show`
nor mihomo ever prints. These tests feed the shipped asset the shapes the router actually
produces: `uci show` with WireGuard/PPPoE keys, mihomo YAML with list/inline/header
credentials, controller JSON and log lines. Every sentinel has to disappear; structure and
harmless fields have to survive.
"""
from pathlib import Path
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REDACT = ROOT / 'assets' / 'geomask-diag-redact.py'

# The CLI helpers below exercise the shipped interface; this module load lets a
# few tests assert on the redactor's internal classification directly.
_spec = importlib.util.spec_from_file_location('_diag_redact', REDACT)
_redactor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_redactor)
m_is_placeholder = _redactor._is_placeholder

UCI_NETWORK = """network.wg0=interface
network.wg0.proto='wireguard'
network.wg0.private_key='UCI-WG-PRIV-SENTINEL'
network.wg0.device='wg0'
network.wg0.addresses='10.66.0.2/32' 'fd00::2/128'
network.wg0.dns='1.1.1.1' '8.8.8.8'
network.@wireguard_wg0[0]=wireguard_wg0
network.@wireguard_wg0[0].preshared_key='UCI-PSK-SENTINEL'
network.@wireguard_wg0[0].public_key='PUBKEY-visible'
network.@wireguard_wg0[0].endpoint_host='vpn.example.com'
network.wan=interface
network.wan.proto='pppoe'
network.wan.device='eth0.2'
network.wan.username='UCI-PPPOE-USER-SENTINEL'
network.wan.password='UCI-PPPOE-PASS-SENTINEL'
network.@device[0]=device
network.@device[0].name='br-lan'
network.mywg=custom
network.mywg.foo='UCI-UNKNOWN-VALUE-SENTINEL'
network.late.password='UCI-LATE-SENTINEL'
network.late=interface
"""

UCI_SENTINELS = (
    'UCI-WG-PRIV-SENTINEL',
    'UCI-PSK-SENTINEL',
    'UCI-PPPOE-USER-SENTINEL',
    'UCI-PPPOE-PASS-SENTINEL',
    'UCI-UNKNOWN-VALUE-SENTINEL',
    'UCI-LATE-SENTINEL',
)

YAML_SAMPLE = """mixed-port: 7890
allow-lan: true
secret: YAML-CONTROLLER-SENTINEL
proxy-providers:
  sub:
    type: http
    url: "https://sub.example.com/link?token=YAML-SUB-TOKEN-SENTINEL"
    username: YAML-USER-SENTINEL
    password: YAML-PASSWORD-SENTINEL
proxies:
  - name: "DE | node-visible"
    type: ss
    server: node.example.com
    port: 8388
    password: YAML-SS-PASS-SENTINEL
    private-key: YAML-PRIVKEY-SENTINEL
    uuid: YAML-UUID-SENTINEL
    sni: sni.example.com
  - {name: inline-visible, type: ss, server: inline.example.com, password: YAML-INLINE-PASS-SENTINEL}
  - name: list-visible
    type: http
    authentication:
      - alice:YAML-BASIC-SENTINEL
  - name: headers-visible
    type: vmess
    headers:
      Authorization: ["Bearer YAML-BEARER-SENTINEL"]
  - name: password-list
    type: ss
    password:
      - YAML-LIST-PASS-SENTINEL
"""

YAML_SENTINELS = (
    'YAML-CONTROLLER-SENTINEL',
    'YAML-SUB-TOKEN-SENTINEL',
    'YAML-USER-SENTINEL',
    'YAML-PASSWORD-SENTINEL',
    'YAML-SS-PASS-SENTINEL',
    'YAML-PRIVKEY-SENTINEL',
    'YAML-UUID-SENTINEL',
    'YAML-INLINE-PASS-SENTINEL',
    'YAML-BASIC-SENTINEL',
    'YAML-BEARER-SENTINEL',
    'YAML-LIST-PASS-SENTINEL',
)

JSON_SAMPLE = """{
  "ok": true,
  "password": "JSON-PASSWORD-SENTINEL",
  "token": "JSON-TOKEN-SENTINEL",
  "group": {"name": "CONSOLE", "type": "select", "now": "DE | node-visible"},
  "meta": {"note": "Authorization: Bearer JSON-BEARER-SENTINEL"}
}"""

JSON_SENTINELS = (
    'JSON-PASSWORD-SENTINEL',
    'JSON-TOKEN-SENTINEL',
    'JSON-BEARER-SENTINEL',
)

TEXT_SAMPLE = """2026/11/15 09:12:44 mihomo: group CONSOLE selected DE | node-visible
2026/11/15 09:12:45 controller: Authorization: Bearer TEXT-BEARER-SENTINEL
2026/11/15 09:12:46 api: password=TEXT-PASSWORD-SENTINEL user=admin
2026/11/15 09:12:47 sub: url=https://alice:TEXT-URL-PASS-SENTINEL@sub.example.com/link?token=TEXT-QUERY-SENTINEL
2026/11/15 09:12:48 nft: hash=0123456789abcdef0123456789abcdef
"""

TEXT_SENTINELS = (
    'TEXT-BEARER-SENTINEL',
    'TEXT-PASSWORD-SENTINEL',
    'TEXT-URL-PASS-SENTINEL',
    'TEXT-QUERY-SENTINEL',
    '0123456789abcdef0123456789abcdef',
)


def redact(mode, text):
    return subprocess.run(
        [sys.executable, '-B', str(REDACT), 'redact', mode],
        input=text.encode('utf-8'), capture_output=True, timeout=120)


def verify(directory):
    return subprocess.run(
        [sys.executable, '-B', str(REDACT), 'verify', str(directory)],
        capture_output=True, timeout=120)


def out_of(result):
    return result.stdout.decode('utf-8')


class RedactorTestCase(unittest.TestCase):
    def assert_redacted(self, result, sentinels):
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        text = out_of(result)
        for sentinel in sentinels:
            self.assertNotIn(sentinel, text,
                             'the redactor kept %r in its output' % sentinel)
        return text


class UciTests(RedactorTestCase):
    def test_uci_show_secrets_are_masked_and_safe_fields_survive(self):
        text = self.assert_redacted(redact('uci', UCI_NETWORK), UCI_SENTINELS)
        for keep in ("network.wg0=interface",
                     "network.wg0.device='wg0'",
                     "network.wg0.proto='wireguard'",
                     "network.wg0.addresses='10.66.0.2/32' 'fd00::2/128'",
                     "network.wg0.dns='1.1.1.1' '8.8.8.8'",
                     "network.@wireguard_wg0[0]=wireguard_wg0",
                     "network.@wireguard_wg0[0].public_key='PUBKEY-visible'",
                     "network.@wireguard_wg0[0].endpoint_host='vpn.example.com'",
                     "network.@device[0].name='br-lan'"):
            self.assertIn(keep, text)

    def test_unknown_section_and_option_keep_the_name_only(self):
        text = self.assert_redacted(redact('uci', UCI_NETWORK), UCI_SENTINELS)
        self.assertIn("network.mywg.foo='…'", text)
        self.assertIn("network.late.password='…'", text)
        self.assertNotIn('UCI-UNKNOWN-VALUE-SENTINEL', text)

    def test_empty_uci_output_is_not_an_error(self):
        result = redact('uci', '')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(out_of(result), '')


class YamlTests(RedactorTestCase):
    def test_yaml_secrets_are_masked_and_structure_survives(self):
        text = self.assert_redacted(redact('yaml', YAML_SAMPLE), YAML_SENTINELS)
        for keep in ('mixed-port: 7890', 'port: 8388', 'server: node.example.com',
                     'sni: sni.example.com', 'node-visible', 'inline-visible',
                     'list-visible', 'headers-visible'):
            self.assertIn(keep, text)
        # the document is still YAML and keeps the harmless shape
        import yaml
        loaded = yaml.safe_load(text)
        self.assertEqual(loaded['mixed-port'], 7890)
        self.assertEqual(loaded['proxies'][0]['port'], 8388)

    def test_yaml_parse_error_fails_closed(self):
        result = redact('yaml', 'proxies: [\n  - name: oops\n')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'', 'a broken document leaked onto stdout')
        error = result.stderr.decode('utf-8')
        self.assertIn('yaml parse failed', error)
        self.assertEqual(json.loads(error)['ok'], False)

    def test_empty_yaml_is_not_an_error(self):
        result = redact('yaml', '')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(out_of(result), '')


class JsonTests(RedactorTestCase):
    def test_json_secrets_are_masked_and_safe_fields_survive(self):
        text = self.assert_redacted(redact('json', JSON_SAMPLE), JSON_SENTINELS)
        loaded = json.loads(text)
        self.assertEqual(loaded['ok'], True)
        self.assertEqual(loaded['group']['name'], 'CONSOLE')
        self.assertEqual(loaded['group']['now'], 'DE | node-visible')

    def test_json_parse_error_fails_closed(self):
        result = redact('json', '{"password": "JSON-LEAK-SENTINEL",')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'', 'a broken document leaked onto stdout')
        error = result.stderr.decode('utf-8')
        self.assertIn('json parse failed', error)
        self.assertEqual(json.loads(error)['ok'], False)

    def test_empty_json_is_not_an_error(self):
        # controller_curl writes an empty file when the controller is down; the bundle
        # keeps the empty member instead of aborting on it.
        result = redact('json', '')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(out_of(result), '')


class TextTests(RedactorTestCase):
    def test_log_lines_lose_credentials_but_stay_useful(self):
        text = self.assert_redacted(redact('text', TEXT_SAMPLE), TEXT_SENTINELS)
        for keep in ('mihomo', 'CONSOLE', 'node-visible', 'Authorization', 'password=',
                     'sub.example.com'):
            self.assertIn(keep, text)
        self.assertIn('user=…', text)


class VerifyTests(RedactorTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)

    def write(self, name, text):
        path = self.case / name
        path.write_text(text, encoding='utf-8', newline='\n')
        return path

    def test_verify_catches_an_unredacted_uci_file(self):
        self.write('leak.uci', "network.wg0=interface\n"
                               "network.wg0.private_key='VERIFY-LEAK-SENTINEL'\n")
        result = verify(self.case)
        self.assertEqual(result.returncode, 1)
        text = out_of(result)
        self.assertIn('leak.uci:2:', text)
        self.assertIn('private-key', text)

    def test_verify_catches_a_bearer_token_and_a_long_token(self):
        self.write('bearer.txt', 'Authorization: Bearer VERIFY-BEARER-SENTINEL\n')
        self.write('token.txt', 'payload=0123456789abcdef0123456789abcdef\n')
        self.write('url.txt', 'proxy socks5://alice:VERIFY-URL-SENTINEL@host:1080\n')
        result = verify(self.case)
        self.assertEqual(result.returncode, 1)
        text = out_of(result)
        self.assertIn('bearer', text)
        self.assertIn('long-token', text)
        self.assertIn('url-userinfo', text)

    def test_the_redacted_forms_pass_verify(self):
        files = {
            'network.uci': redact('uci', UCI_NETWORK),
            'config.masked.yaml': redact('yaml', YAML_SAMPLE),
            'console-config.json': redact('json', JSON_SAMPLE),
            'logread.txt': redact('text', TEXT_SAMPLE),
        }
        for name, result in files.items():
            self.assertEqual(result.returncode, 0, result.stderr)
            self.write(name, out_of(result))
        result = verify(self.case)
        self.assertEqual(result.returncode, 0, out_of(result))
        self.assertEqual(out_of(result), '')


class FollowupTests(RedactorTestCase):
    # Arbitrary short values prove classification is structural, not marker matching.
    CASES = (
        ('text', 'mihomo: response {"password":"SYNTH-pass-17","token":"SYNTH-token-18"}', ('SYNTH-pass-17', 'SYNTH-token-18')),
        ('text', "mihomo: 'password' = 'p7' \"token\":\"t8\" auth_str=au9 auth-string=au10 private_key=pk11", ('p7', 't8', 'au9', 'au10', 'pk11')),
        ('text', 'mihomo: Authorization: Basic YWxpY2U6cHc=', ('YWxpY2U6cHc=',)),
        ('text', 'mihomo: Authorization: Bearer "two words short"', ('two words short',)),
        ('text', 'mihomo: Authorization: Digest username="alice", response="p14"', ('alice', 'p14')),
        ('text', 'mihomo: Proxy-Authorization: Basic YWxpY2U6cHc=', ('YWxpY2U6cHc=',)),
        ('text', 'mihomo: Authorization: … YWxpY2U6cHc=', ('YWxpY2U6cHc=',)),
        ('json', json.dumps({'message': 'mihomo: {"password":"SYNTH-pass-19"}', 'ok': True}), ('SYNTH-pass-19',)),
        ('json', json.dumps({'message': json.dumps({'message': 'Authorization: Basic YWxpY2U6cHc='})}), ('YWxpY2U6cHc=',)),
        ('yaml', 'proxies: [{name: visible, type: hysteria, auth-str: SYNTH-auth-20, auth_str: a2, auth-string: a3}]', ('SYNTH-auth-20', 'a2', 'a3')),
        ('yaml', "message: |\n  mihomo: {\"password\":\"p12\"}\n  Authorization: Basic YWxpY2U6cHc=\n", ('p12', 'YWxpY2U6cHc=')),
        ('json', json.dumps({'message': 'socks5://alice:shortpw@host:1080'}), ('shortpw',)),
        ('yaml', 'message: "request?signature=shortsig"', ('shortsig',)),
        ('uci', "network.wan=interface\nnetwork.wan.password='firstline\nSYNTH-pass-21'\n", ('firstline', 'SYNTH-pass-21')),
        ('uci', "network.wan=interface\nnetwork.wan.name='visible\npassword=p13'\n", ('p13',)),
    )

    def test_new_shapes_redact_and_verify(self):
        import yaml
        for mode, source, secrets in self.CASES:
            with self.subTest(mode=mode, source=source):
                result = redact(mode, source)
                text = self.assert_redacted(result, secrets)
                if mode == 'json':
                    json.loads(text)
                if mode == 'yaml':
                    yaml.safe_load(text)
                with tempfile.TemporaryDirectory() as directory:
                    suffix = {'yaml': '.yaml', 'json': '.json', 'uci': '.uci'}.get(mode, '.txt')
                    path = Path(directory) / ('member' + suffix)
                    path.write_text(text, encoding='utf-8')
                    self.assertEqual(verify(directory).returncode, 0, text)

    def test_independent_verify_rejects_each_unredacted_shape(self):
        for mode, source, _ in self.CASES:
            with self.subTest(mode=mode, source=source), tempfile.TemporaryDirectory() as directory:
                suffix = {'yaml': '.yaml', 'json': '.json', 'uci': '.uci'}.get(mode, '.txt')
                (Path(directory) / ('member' + suffix)).write_text(source, encoding='utf-8')
                self.assertEqual(verify(directory).returncode, 1, source)

    def test_nested_serialized_json_remains_parseable(self):
        payload = json.dumps({'message': 'Authorization: Basic YWxpY2U6cHc=', 'count': 7})
        result = redact('json', json.dumps({'message': payload}))
        loaded = json.loads(self.assert_redacted(result, ('YWxpY2U6cHc=',)))
        inner = json.loads(loaded['message'])
        self.assertEqual(inner['count'], 7)
        self.assertIn('Authorization', inner['message'])

    def test_uci_escaped_quote_and_plain_sections(self):
        source = "network.wan=interface\nnetwork.wan.name='router'\\''s name'\nnetwork.wan.password='pw'\\''tail\nmore'\n"
        text = self.assert_redacted(redact('uci', source), ('pw', 'tail', 'more'))
        self.assertIn("network.wan.name='router'\\''s name'", text)
        self.assertIn('network.wan=interface', text)

    def test_uci_malformed_fails_closed(self):
        for source in ("network.wan.password='unterminated\nraw-private", 'unknown raw continuation\n', "network.wan.name='ok' junk\n"):
            with self.subTest(source=source):
                result = redact('uci', source)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b'')
                with tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / 'network.uci').write_text(source, encoding='utf-8')
                    self.assertEqual(verify(directory).returncode, 1)


class EmbeddedJsonRound3Tests(RedactorTestCase):
    PAYLOADS = (
        (r'mihomo: {"pass\u0077ord":"p31!","visible":7}', ('p31!',)),
        (r'mihomo: {" password ":"p32!","visible":7}', ('p32!',)),
        (r'mihomo: [{"items":[{"to\u006ben":"t33!","visible":7}]}]', ('t33!',)),
        (r'mihomo: {"auth\u005fstr":"a34!","private\u002dkey":"k35!","visible":7}', ('a34!', 'k35!')),
        ('mihomo: ' + json.dumps({'message': r'api: [{" token ":"t36!","visible":7}]', 'visible': 7}), ('t36!',)),
    )

    def test_embedded_objects_arrays_and_messages_redact_and_verify(self):
        import yaml
        for payload, secrets in self.PAYLOADS:
            for mode, source, suffix in (
                ('text', payload, '.txt'),
                ('json', json.dumps({'message': payload, 'visible': 7}), '.json'),
                ('yaml', yaml.safe_dump({'message': payload, 'visible': 7}), '.yaml'),
            ):
                with self.subTest(mode=mode, payload=payload):
                    text = self.assert_redacted(redact(mode, source), secrets)
                    self.assertIn('visible', text)
                    if mode == 'json':
                        self.assertEqual(json.loads(text)['visible'], 7)
                    elif mode == 'yaml':
                        self.assertEqual(yaml.safe_load(text)['visible'], 7)
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / ('member' + suffix)
                        path.write_text(text, encoding='utf-8', newline='\n')
                        self.assertEqual(verify(directory).returncode, 0, text)

    def test_independent_verify_rejects_raw_embedded_json(self):
        import yaml
        for payload, _ in self.PAYLOADS:
            for suffix, source in (('.txt', payload),
                                   ('.json', json.dumps({'message': payload})),
                                   ('.yaml', yaml.safe_dump({'message': payload}))):
                with self.subTest(suffix=suffix, payload=payload), tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / ('raw' + suffix)).write_text(source, encoding='utf-8', newline='\n')
                    self.assertEqual(verify(directory).returncode, 1, source)

    def test_folded_headers_in_structured_multiline_scalars(self):
        import yaml
        for message in ('Authorization:\n Basic YWxp\n Y2U6cHc=',
                        'Authorization: Basic\n YWxpY2U6cHc=',
                        'Authorization: Bearer\r\n\t"b37! two"',
                        'Authorization: Basic YWxp\n Y2U6cHc=',
                        'Authorization: …\n YWxpY2U6cHc='):
            for mode, source in (('json', json.dumps({'message': message, 'visible': 7})),
                                 ('yaml', yaml.safe_dump({'message': message, 'visible': 7}, default_style='|'))):
                with self.subTest(mode=mode, message=message):
                    text = self.assert_redacted(redact(mode, source), ('YWxp', 'Y2U6cHc=', 'b37!'))
                    loaded = json.loads(text) if mode == 'json' else yaml.safe_load(text)
                    self.assertEqual(loaded['visible'], 7)
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / ('raw.' + mode)
                        path.write_text(source, encoding='utf-8', newline='\n')
                        self.assertEqual(verify(directory).returncode, 1)
                        path.write_text(text, encoding='utf-8', newline='\n')
                        self.assertEqual(verify(directory).returncode, 0, text)

    def test_non_json_nft_sets_and_log_timestamps_remain_useful(self):
        source = ('mihomo: [2026/11/15] selected node-visible\n'
                  'nft set domains { "example.invalid", "other.invalid" }\n')
        text = self.assert_redacted(redact('text', source), ())
        for useful in ('[2026/11/15]', 'node-visible', 'example.invalid', 'other.invalid'):
            self.assertIn(useful, text)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'nft.txt').write_text(text, encoding='utf-8')
            self.assertEqual(verify(directory).returncode, 0, text)

    def test_ambiguous_json_keys_or_truncated_objects_fail_closed(self):
        for source in (r'mihomo: {"pass\u0077ord":"p38!","password":"…"}',
                       r'mihomo: {"to\u006ben":"t39!",'):
            with self.subTest(source=source):
                result = redact('text', source)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b'')
                with tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / 'log.txt').write_text(source, encoding='utf-8')
                    self.assertEqual(verify(directory).returncode, 1)

    def test_ambiguous_auth_continuation_fails_closed(self):
        for message in ('Authorization: Basic\nYWxpY2U6cHc=',
                        'Authorization:\nBasic YWxpY2U6cHc='):
            with self.subTest(message=message):
                source = json.dumps({'message': message})
                result = redact('json', source)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b'')
                with tempfile.TemporaryDirectory() as directory:
                    (Path(directory) / 'raw.json').write_text(source, encoding='utf-8')
                    self.assertEqual(verify(directory).returncode, 1)


class NftProducerRound4Tests(RedactorTestCase):
    MAP = ('map ifname_to_mark { type ifname : mark; '
           'elements = { "br-lan" : 0x1, "eth0" : 0x2 } }\n')

    def test_nft_map_preserves_interfaces_marks_and_verifies(self):
        text = self.assert_redacted(redact('nft', self.MAP), ())
        for useful in ('ifname_to_mark', 'br-lan', 'eth0', '0x1', '0x2'):
            self.assertIn(useful, text)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nft.txt'
            path.write_text(text, encoding='utf-8')
            self.assertEqual(verify(directory).returncode, 0, text)

    def test_nft_strings_and_comments_still_redact_and_reject_raw(self):
        payloads = (
            r'{"pass\u0077ord":"n41!","visible":7}',
            r'[{"items":[{" token ":"n42!","visible":7}]}]',
            'Authorization: Basic YWxpY2U6cHc=',
            'Authorization:\n Basic YWxp\n Y2U6cHc=',
        )
        for payload in payloads:
            for source in (self.MAP + 'ip saddr 192.0.2.1 counter comment ' + json.dumps(payload) + '\n',
                           self.MAP + '# ' + payload.replace('\n', ' ') + '\n'):
                with self.subTest(source=source):
                    text = self.assert_redacted(redact('nft', source), ('n41!', 'n42!', 'YWxp', 'Y2U6cHc='))
                    self.assertIn('br-lan', text)
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / 'nft.txt'
                        path.write_text(source, encoding='utf-8', newline='\n')
                        self.assertEqual(verify(directory).returncode, 1, source)
                        path.write_text(text, encoding='utf-8', newline='\n')
                        self.assertEqual(verify(directory).returncode, 0, text)

    def test_nft_quoted_credential_map_keys_are_not_an_escape_hatch(self):
        source = self.MAP + r'map labels { elements = { "pass\u0077ord" : "n43!", " token " : "n44!" } }'
        text = self.assert_redacted(redact('nft', source), ('n43!', 'n44!'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nft.txt'
            path.write_text(source, encoding='utf-8')
            self.assertEqual(verify(directory).returncode, 1)
            path.write_text(text, encoding='utf-8')
            self.assertEqual(verify(directory).returncode, 0, text)

    def test_nft_payload_malformed_duplicate_json_and_quotes_fail_closed(self):
        for payload in (r'{"to\u006ben":"n45!",',
                        r'{"pass\u0077ord":"n46!","password":"…"}'):
            for mode, source, name in (
                ('text', 'mihomo: ' + payload, 'log.txt'),
                ('nft', self.MAP + '# ' + payload, 'nft.txt'),
                ('nft', self.MAP + 'comment ' + json.dumps(payload), 'nft.txt'),
            ):
                with self.subTest(mode=mode, source=source):
                    result = redact(mode, source)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, b'')
                    with tempfile.TemporaryDirectory() as directory:
                        (Path(directory) / name).write_text(source, encoding='utf-8')
                        self.assertEqual(verify(directory).returncode, 1)
        result = redact('nft', self.MAP + 'comment "unterminated n47!')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')


class ValueBoundaryRound4Tests(RedactorTestCase):
    """An unquoted secret value must not stop at the first space and leak its
    tail; a placeholder-looking first word must not let a live tail through."""

    CASES = (
        # (source, secret sentinels that must NOT survive, useful text kept)
        ('mihomo: password=hello world\n', ('world',), ('password=',)),
        ('mihomo: token: abc def\n', ('def',), ('token:',)),
        ('mihomo: password: null secret\n', ('secret',), ('password:',)),
        ('mihomo: password: null none\n', ('none',), ('password:',)),
        ('mihomo: password=foo region=eu\n', ('foo',), ('region=eu',)),
        ('mihomo: password=\nrawsecret\n', ('rawsecret',), ('password=',)),
        ('mihomo: auth Basic YWxp Y2U6cHc=\n', ('YWxp', 'Y2U6cHc='), ('Basic',)),
        # An indented obs-fold continues the value (like folded Authorization).
        ('mihomo: password=raw1\n raw2tail\n', ('raw2tail',), ('password=',)),
        # A trailing base64-padding token (uppercase / empty after `=`) is a
        # secret tail, never a new field, so it must not survive (b1/b2/b3/b4).
        ('mihomo: password=hello Y2U6cHc=\n', ('Y2U6cHc=',), ('password=',)),
        ('mihomo: token: abc YWxpY2U6cHc=\n', ('YWxpY2U6cHc=',), ('token:',)),
        ('mihomo: password=a Y2U6cHc= btail\n', ('Y2U6cHc=', 'btail'), ('password=',)),
        ('mihomo: password=raw1\n  raw2 Y2U6cHc=\n', ('Y2U6cHc=',), ('password=',)),
        # Bare-scheme auth credentials fold across indented lines (b5/b6).
        ('mihomo: auth Basic YWxp\n  Y2U6cHc=\n', ('YWxp', 'Y2U6cHc='), ('Basic',)),
        ('mihomo: auth Bearer tok1\n  tok2tail\n', ('tok1', 'tok2tail'), ('Bearer',)),
    )

    def test_unquoted_value_masks_the_whole_tail(self):
        for source, secrets, keeps in self.CASES:
            with self.subTest(source=source):
                text = self.assert_redacted(redact('text', source), secrets)
                for keep in keeps:
                    self.assertIn(keep, text)

    def test_raw_forms_are_refused_and_clean_forms_pass(self):
        for source, secrets, _ in self.CASES:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'log.txt'
                path.write_text(source, encoding='utf-8', newline='\n')
                self.assertEqual(verify(directory).returncode, 1, source)
                text = self.assert_redacted(redact('text', source), secrets)
                path.write_text(text, encoding='utf-8', newline='\n')
                self.assertEqual(verify(directory).returncode, 0, text)

    def test_real_redaction_marker_still_passes_verify(self):
        # A folded credential masked in pieces yields repeated ellipsis; that is
        # a real redaction, while `null none` is a live secret tail.
        self.assertTrue(m_is_placeholder('…'))
        self.assertTrue(m_is_placeholder('… …'))
        self.assertFalse(m_is_placeholder('null none'))
        self.assertFalse(m_is_placeholder('null secret'))

    def test_col0_next_line_is_a_record_boundary_not_a_value_tail(self):
        # An unindented next line is a new logical record (matching folded
        # Authorization handling): masking it would swallow the next log entry.
        # A real multi-line secret is quoted (handled structurally) or folded
        # with indentation (masked above). Documented boundary, not a leak.
        source = 'mihomo: password=raw1\n2026/11/15 sub: url=https://a\n'
        text = self.assert_redacted(redact('text', source), ('raw1',))
        self.assertIn('2026/11/15', text)
        self.assertIn('sub: url=https://a', text)


class ShortKeyRound5Tests(RedactorTestCase):
    """Audit 18.11.2026: short values under api_key/access_token/client_secret leaked.

    The key set only knew whole names, and the value heuristics need a long token, a URL or a
    bearer scheme - a synthetic short value under `api_key:` therefore stayed readable.
    """

    YAML = ('mixed-port: 7890\n'
            'api_key: short-api-1\n'
            'access_token: short-token-2\n'
            'client_secret: short-secret-3\n'
            'Api-Key: short-api-4\n'
            'clientSecret: short-secret-5\n'
            'public_key: PUBKEY-visible\n')
    YAML_SENTINELS = ('short-api-1', 'short-token-2', 'short-secret-3',
                      'short-api-4', 'short-secret-5')

    JSON = json.dumps({'api_key': 'short-api-6', 'accessToken': 'short-token-7',
                       'client_secret': 'short-secret-8', 'public_key': 'PUBKEY-visible'})
    JSON_SENTINELS = ('short-api-6', 'short-token-7', 'short-secret-8')

    TEXT = ('mihomo: api_key=short-api-9 token: short-token-10 '
            'client_secret=short-secret-11 public_key=PUBKEY-visible\n')

    def test_yaml_short_secret_names_are_masked(self):
        text = self.assert_redacted(redact('yaml', self.YAML), self.YAML_SENTINELS)
        self.assertIn('PUBKEY-visible', text, 'a public key is not a secret')

    def test_json_camel_case_secret_names_are_masked(self):
        text = self.assert_redacted(redact('json', self.JSON), self.JSON_SENTINELS)
        self.assertIn('PUBKEY-visible', text)

    def test_log_text_short_secret_names_are_masked(self):
        text = self.assert_redacted(redact('text', self.TEXT),
                                    ('short-api-9', 'short-token-10', 'short-secret-11'))
        self.assertIn('PUBKEY-visible', text)

    def test_verify_rejects_the_short_key_forms(self):
        for mode, source, _ in (('yaml', self.YAML, self.YAML_SENTINELS),
                                ('json', self.JSON, self.JSON_SENTINELS)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'bundle.txt'
                path.write_text(source, encoding='utf-8', newline='\n')
                self.assertEqual(verify(directory).returncode, 1, source)

    def test_a_query_string_short_key_is_masked(self):
        # The URL policy keeps scheme+host (that is what makes a diagnosis possible) and drops
        # the rest, so the credential cannot survive either way.
        source = 'GET https://api.example.com/v1?api_key=short-q-12&lang=en HTTP/1.1\n'
        text = self.assert_redacted(redact('text', source), ('short-q-12',))
        self.assertIn('api.example.com', text, 'the host was removed as well')


class BatchTests(RedactorTestCase):
    """One redactor process for all saved logs: 30 starts took 16 s of the 30 s ubus budget."""

    def run_batch(self, manifest):
        return subprocess.run(
            [sys.executable, '-B', str(REDACT), 'batch', 'text', str(manifest)],
            capture_output=True, timeout=120)

    def test_one_process_redacts_every_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            outputs = []
            lines = []
            for index in range(3):
                source = base / ('check%d.log' % index)
                source.write_text('ok line\nsecret %d: password=BATCH-SENTINEL-%d\n'
                                  % (index, index), encoding='utf-8')
                target = base / ('out%d.log' % index)
                outputs.append(target)
                lines.append('%s\t%s\t65536' % (source, target))
            manifest = base / 'manifest.txt'
            manifest.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            result = self.run_batch(manifest)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            self.assertIn(b'"files":3', result.stdout)
            for index, target in enumerate(outputs):
                text = target.read_text(encoding='utf-8')
                self.assertIn('ok line', text)
                self.assertNotIn('BATCH-SENTINEL-%d' % index, text)

    def test_batch_keeps_the_tail_and_refuses_a_missing_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / 'check.log'
            source.write_text('OLD-PREFIX\n' + 'x' * 4096 + '\nTAILMARK password=SECRET\n',
                              encoding='utf-8')
            target = base / 'out.log'
            manifest = base / 'manifest.txt'
            manifest.write_text('%s\t%s\t64\n' % (source, target), encoding='utf-8')
            result = self.run_batch(manifest)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            text = target.read_text(encoding='utf-8')
            self.assertNotIn('OLD-PREFIX', text)
            self.assertIn('TAILMARK', text)
            self.assertNotIn('SECRET', text)

            broken = base / 'broken.txt'
            broken.write_text('%s\t%s\t64\n' % (base / 'missing.log', base / 'x.log'),
                              encoding='utf-8')
            result = self.run_batch(broken)
            self.assertEqual(result.returncode, 1)
            self.assertIn(b'cannot read', result.stderr)


if __name__ == '__main__':
    unittest.main()
