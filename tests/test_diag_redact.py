"""The diagnostic bundle redactor, run against the reviewer's real producer formats.

The old backend filter understood `option private_key '...'` - a form neither `uci show`
nor mihomo ever prints. These tests feed the shipped asset the shapes the router actually
produces: `uci show` with WireGuard/PPPoE keys, mihomo YAML with list/inline/header
credentials, controller JSON and log lines. Every sentinel has to disappear; structure and
harmless fields have to survive.
"""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REDACT = ROOT / 'assets' / 'geomask-diag-redact.py'

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


if __name__ == '__main__':
    unittest.main()
