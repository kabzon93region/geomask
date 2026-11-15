"""Console diagnostic bundle: private directory, retention, no silent copy into the share,
and a download the panel can actually offer.

Review 14-15.11.2026, points 14, 15 and 33. The bundle was assembled in a fixed
`/tmp/geomask-console-diag` that every request `rm -rf`'d first, so two panel clicks deleted
each other's half-built archive; the result was copied into `BACKUP_DIR` - a folder that may be
published over SMB - without asking and without any retention; and the panel got only a path,
so the owner had to hunt for the file by hand.

The functions are taken from the shipped backend and run against stubbed router commands; the
temporary directory, the share folder and the opt-in marker all live in the test's own sandbox.
"""
from pathlib import Path
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'
SOURCE = BACKEND.read_text(encoding='utf-8')
REDACT = ROOT / 'assets' / 'geomask-diag-redact.py'


def find_bash():
    # Never select Windows' optional WSL launcher from PATH.
    candidates = ((r'C:\Program Files\Git\bin\bash.exe',) if sys.platform == 'win32'
                  else (shutil.which('bash'),))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def extract(name):
    """Whole function, brace-balanced at column 0 (no heredoc in these)."""
    lines = SOURCE.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(name + '() {')), None)
    if start is None:
        raise AssertionError('function %s not found in the backend' % name)
    for index in range(start + 1, len(lines)):
        if lines[index] == '}':
            # Map the sole absolute version read before any extracted function runs.
            return '\n'.join(lines[start:index + 1]).replace(
                '/etc/geomask/version', '${TEST_VERSION_PATH}')
    raise AssertionError('function %s has no closing brace' % name)


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


STUBS = r'''
uci() { return 0; }
nft() { return 0; }
logread() { printf 'mihomo line\n'; }
conntrack() { return 0; }
controller_curl() { return 0; }
ip() { printf '10: from 192.168.1.177 lookup 102\n'; return 0; }
'''


@unittest.skipIf(BASH is None, 'bash is required')
class ConsoleDiagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.spool = self.case / 'spool'
        self.spool.mkdir(mode=0o700)
        self.share = self.case / 'share'
        self.share.mkdir()
        self.marker = self.case / 'backup-share.enable'
        self.state = self.case / 'console.state'
        self.state.write_text('enabled=1\nip=192.168.1.177\n', encoding='utf-8', newline='\n')
        (self.case / 'config.yaml').write_text('mixed-port: 7890\n', encoding='utf-8',
                                               newline='\n')

    def run_body(self, body, functions=('cmd_console_diag',), maximum=None,
                 stubs=STUBS, diag_py=None):
        lines = [
            'set -u',
            'CONSOLE_STATE="%s"' % posix(self.state),
            'export TMPDIR="%s"' % posix(self.spool),
            'export TMP="%s" TEMP="%s"' % (posix(self.case), posix(self.case)),
            'DIAG_SPOOL="%s"' % posix(self.spool),
            'DIAG_SPOOL_PY="%s"' % posix(ROOT / 'assets' / 'geomask-diag-spool.py'),
            'BACKUP_DIR="%s"' % posix(self.share),
            'SMB_SHARE_ENABLE="%s"' % posix(self.marker),
            'CONSOLE_DIAG_KEEP_H=24',
            'CONSOLE_TABLE=102',
            'CONSOLE_TUN_DEV=Console',
            'BACKEND_VERSION=test',
            'TEST_VERSION_PATH="%s"' % posix(self.case / 'absent-version'),
            'MIHOMO_CONF="%s"' % posix(self.case / 'config.yaml'),
            'MIHOMO_API=http://127.0.0.1:9090',
            'CONSOLE_PY=/nonexistent/geomask-console.py',
            'PYTHON_BIN="%s"' % posix(sys.executable),
            'GM_DIAG_PY="%s"' % posix(diag_py or REDACT),
            'DIAG_PY="${GM_DIAG_PY:-/usr/lib/geomask/geomask-diag-redact.py}"',
        ]
        if maximum is not None:
            lines.append('GM_CONSOLE_DIAG_MAX=%s' % maximum)
        lines += [stubs]
        lines += [extract('_console_diag_fail'), extract('_console_diag_redact'),
                  extract('_console_get'), extract('json_esc')]
        lines += [extract(name) for name in functions]
        lines += [body]
        path = self.case / 'run.sh'
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
        result = subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', timeout=120)
        line = next((line for line in result.stdout.splitlines()
                     if line.startswith('{"ok"')), None)
        self.assertIsNotNone(line, result.stdout + result.stderr)
        answer = json.loads(line)
        # The exit code has to agree with the answer: a JSON {"ok": false} with rc 0 is how a
        # failed download used to look successful to the panel.
        expected = 'RC=0' if answer.get('ok') else 'RC=1'
        self.assertIn(expected, result.stdout, result.stdout + result.stderr)
        return answer

    def run_diag(self, **kwargs):
        return self.run_body('cmd_console_diag; printf "\\nRC=%s\\n" "$?"', **kwargs)

    def get(self, name, maximum=None):
        return self.run_body('cmd_console_diag_get \'%s\'; printf "\\nRC=%%s\\n" "$?"' % name,
                             functions=('cmd_console_diag_get',), maximum=maximum)

    def bundle_name(self, answer):
        return str(answer['file']).rsplit('/', 1)[-1]

    # ---------------------------------------------------------------- the bundle itself
    def test_a_bundle_stays_out_of_the_share_without_the_marker(self):
        answer = self.run_diag()
        self.assertTrue(answer['ok'], answer)
        self.assertFalse(answer['shared'], 'the bundle went into the share without an opt-in')
        self.assertEqual(list(self.share.iterdir()), [])
        self.assertTrue(answer['file'].startswith(posix(self.spool)),
                        'the archive must sit in the temporary directory: %s' % answer['file'])
        self.assertGreater(answer['bytes'], 0)

    def test_an_opted_in_share_gets_a_copy(self):
        self.marker.write_text('', encoding='utf-8')
        answer = self.run_diag()
        self.assertTrue(answer['shared'], 'the opted-in share got no copy')
        names = [entry.name for entry in self.share.iterdir()]
        self.assertEqual(len(names), 1, names)
        self.assertIn('geomask-console-diag-', names[0])
        self.assertNotIn('console.state', names[0])

    def test_two_runs_do_not_share_a_directory_or_a_name(self):
        first = self.run_diag()
        second = self.run_diag()
        self.assertNotEqual(first['file'], second['file'],
                            'a fixed path would make the second run delete the first bundle')
        leftovers = [entry.name for entry in self.spool.iterdir()
                     if entry.name.startswith('geomask-console-diag.')]
        self.assertEqual(leftovers, [], 'a per-request directory was left behind')

    # ---------------------------------------------------------------- R02: redaction
    E2E_SENTINELS = (
        'WG-PRIV-E2E', 'WG-PSK-E2E', 'PPPOE-PASS-E2E', 'FW-SENTINEL-E2E',
        'YAML-SECRET-E2E', 'YAML-QUERY-E2E', 'YAML-PASSWORD-E2E', 'YAML-BEARER-E2E',
        'GROUP-SENTINEL-E2E', 'CONN-SENTINEL-E2E',
        'LOG-BEARER-E2E', 'LOG-PASSWORD-E2E', 'LOG-URL-E2E', 'LOG-QUERY-E2E',
        'NFT-BEARER-E2E', '0123456789abcdef0123456789abcdef',
    )

    def sentinel_stubs(self):
        network = self.case / 'network.uci'
        network.write_text(
            "network.wg0=interface\n"
            "network.wg0.proto='wireguard'\n"
            "network.wg0.device='wg0'\n"
            "network.wg0.private_key='WG-PRIV-E2E'\n"
            "network.@wireguard_wg0[0]=wireguard_wg0\n"
            "network.@wireguard_wg0[0].preshared_key='WG-PSK-E2E'\n"
            "network.@wireguard_wg0[0].public_key='PUBKEY-visible'\n"
            "network.wan=interface\n"
            "network.wan.proto='pppoe'\n"
            "network.wan.password='PPPOE-PASS-E2E'\n",
            encoding='utf-8', newline='\n')
        firewall = self.case / 'firewall.uci'
        firewall.write_text(
            "firewall.@zone[0]=zone\n"
            "firewall.@zone[0].name='lan'\n"
            "firewall.@zone[0].network='lan'\n"
            "firewall.@zone[0].password='FW-SENTINEL-E2E'\n",
            encoding='utf-8', newline='\n')
        group = self.case / 'group.json'
        group.write_text(json.dumps({
            'name': 'CONSOLE', 'type': 'Selector', 'now': 'DE | node-visible',
            'secret': 'GROUP-SENTINEL-E2E'}), encoding='utf-8', newline='\n')
        connections = self.case / 'connections.json'
        connections.write_text(json.dumps({
            'downloadTotal': 1, 'uploadTotal': 2,
            'connections': [{
                'id': 'uuid-visible',
                'metadata': {'host': 'node.example.com', 'destinationIP': '1.2.3.4',
                             'sourceIP': '192.168.1.9'},
                'chains': ['CONSOLE'], 'rule': 'Match', 'rulePayload': '',
                'password': 'CONN-SENTINEL-E2E'}]}),
            encoding='utf-8', newline='\n')
        (self.case / 'config.yaml').write_text(
            'mixed-port: 7890\n'
            'secret: YAML-SECRET-E2E\n'
            'proxy-providers:\n'
            '  sub:\n'
            '    type: http\n'
            '    url: "https://sub.example.com/link?token=YAML-QUERY-E2E"\n'
            'proxies:\n'
            '  - name: "DE | node-visible"\n'
            '    type: ss\n'
            '    server: node.example.com\n'
            '    port: 8388\n'
            '    password: YAML-PASSWORD-E2E\n'
            '    headers:\n'
            '      Authorization: ["Bearer YAML-BEARER-E2E"]\n',
            encoding='utf-8', newline='\n')
        stubs = r'''
uci() {
  case "$*" in
    *network*) cat '@NETWORK@' ;;
    *firewall*) cat '@FIREWALL@' ;;
  esac
}
nft() { printf 'nft: Authorization: Bearer NFT-BEARER-E2E token=0123456789abcdef0123456789abcdef\n'; }
logread() {
  printf '%s\n' \
    'mihomo: group CONSOLE selected DE | node-visible' \
    'mihomo: controller: Authorization: Bearer LOG-BEARER-E2E' \
    'mihomo: api: password=LOG-PASSWORD-E2E' \
    'mihomo: sub: url=https://alice:LOG-URL-E2E@sub.example.com/link?token=LOG-QUERY-E2E'
}
conntrack() { return 0; }
controller_curl() {
  case "$*" in
    *proxies/CONSOLE*) cat '@GROUP@' ;;
    *connections*) cat '@CONNECTIONS@' ;;
  esac
}
ip() { printf '10: from 192.168.1.177 lookup 102\n'; return 0; }
'''
        for key, path in (('@NETWORK@', network), ('@FIREWALL@', firewall),
                          ('@GROUP@', group), ('@CONNECTIONS@', connections)):
            stubs = stubs.replace(key, posix(path))
        return stubs

    def assert_packs_nothing(self):
        leftovers = [entry.name for entry in self.spool.iterdir()
                     if entry.name.startswith('geomask-console-diag')]
        self.assertEqual(leftovers, [],
                         'a failed request left a private directory or an archive behind')
        self.assertEqual(list(self.share.iterdir()), [])

    def test_the_bundle_carries_no_sentinel_from_any_producer(self):
        answer = self.run_diag(stubs=self.sentinel_stubs())
        self.assertTrue(answer['ok'], answer)
        archive = self.spool / self.bundle_name(answer)
        members = {}
        with tarfile.open(str(archive), 'r:gz') as bundle:
            for member in bundle.getmembers():
                if not member.isfile():
                    continue
                data = bundle.extractfile(member)
                members[member.name.rsplit('/', 1)[-1]] = data.read().decode(
                    'utf-8', 'replace')
        for name, text in members.items():
            for sentinel in self.E2E_SENTINELS:
                self.assertNotIn(sentinel, text,
                                 '%s: sentinel %r survived the bundle' % (name, sentinel))
        for expected in ('firewall.uci', 'network.uci', 'nft.txt', 'logread.txt',
                         'group-CONSOLE.json', 'connections.json', 'config.masked.yaml'):
            self.assertIn(expected, members, 'the bundle lost its %s member' % expected)
        joined = '\n'.join(members.values())
        self.assertIn('network.wg0=interface', joined)
        self.assertIn("device='wg0'", joined)
        self.assertIn('mixed-port: 7890', joined)
        self.assertIn('node-visible', joined)

    def test_followup_bundle_scans_all_members_and_short_encoded_auth(self):
        stubs = self.sentinel_stubs()
        network = self.case / 'network.uci'
        with network.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write("network.wan.password='firstline\nSYNTH-pass-21'\n"
                         "network.wan.name='visible\npassword=allowed-short'\n")
        with (self.case / 'config.yaml').open('a', encoding='utf-8', newline='\n') as handle:
            handle.write('    auth-str: SYNTH-auth-20\n'
                         '    auth_str: alias-short\n'
                         '    auth-string: alias-other\n'
                         'message: \'mihomo: {"password":"scalar-short"}\'\n')
        group = self.case / 'group.json'
        group.write_text(json.dumps({'name': 'CONSOLE', 'message': json.dumps({
            'message': 'mihomo: {"token":"nested-short"}'})}), encoding='utf-8')
        logs = self.case / 'followup-logs.txt'
        logs.write_text('mihomo: response {"password":"SYNTH-pass-17","token":"SYNTH-token-18"}\n'
                        'mihomo: Authorization: Basic YWxpY2U6cHc=\n'
                        'mihomo: Authorization: Bearer "two words short"\n', encoding='utf-8')
        stubs += "\nlogread() { cat '%s'; }\n" % posix(logs)
        answer = self.run_diag(stubs=stubs)
        self.assertTrue(answer['ok'], answer)
        sentinels = self.E2E_SENTINELS + ('firstline', 'SYNTH-pass-21', 'allowed-short',
            'SYNTH-auth-20', 'alias-short', 'alias-other', 'scalar-short', 'nested-short',
            'SYNTH-pass-17', 'SYNTH-token-18', 'YWxpY2U6cHc=', 'two words short')
        with tarfile.open(self.spool / self.bundle_name(answer), 'r:gz') as bundle:
            for member in bundle.getmembers():
                # Scan metadata as well as every file, without extracting paths.
                content = member.name.encode('utf-8')
                if member.isfile():
                    content += bundle.extractfile(member).read()
                for sentinel in sentinels:
                    self.assertNotIn(sentinel.encode('utf-8'), content, member.name)

    def round3_stubs(self):
        stubs = self.sentinel_stubs()
        logs = self.case / 'round3-log.txt'
        logs.write_text(
            r'mihomo: {"pass\u0077ord":"p31!","visible":7}' + '\n' +
            r'mihomo: {" password ":"p32!","visible":7}' + '\n' +
            r'mihomo: [{"items":[{"to\u006ben":"t33!","visible":7}]}]' + '\n',
            encoding='utf-8', newline='\n')
        (self.case / 'group.json').write_text(json.dumps({
            'name': 'CONSOLE', 'message': json.dumps({'message':
                r'api: [{"auth\u005fstr":"a34!","private\u002dkey":"k35!"}]'}),
            'folded': 'Authorization:\n Basic YWxp\n Y2U6cHc='}), encoding='utf-8')
        with (self.case / 'config.yaml').open('a', encoding='utf-8', newline='\n') as handle:
            handle.write("message: |\n  mihomo: [{\" token \":\"t36!\"}]\n"
                         'folded: |\n  Authorization: Bearer\n    "b37! two"\n')
        return stubs + "\nlogread() { cat '%s'; }\n" % posix(logs)

    def test_round3_all_archive_members_lose_embedded_json_and_folded_auth(self):
        answer = self.run_diag(stubs=self.round3_stubs())
        self.assertTrue(answer['ok'], answer)
        sentinels = self.E2E_SENTINELS + ('p31!', 'p32!', 't33!', 'a34!', 'k35!',
                                        't36!', 'b37!', 'YWxpY2U6cHc=', 'YWxp', 'Y2U6cHc=')
        members = {}
        with tarfile.open(self.spool / self.bundle_name(answer), 'r:gz') as bundle:
            for member in bundle.getmembers():
                content = member.name.encode('utf-8')
                if member.isfile():
                    data = bundle.extractfile(member).read()
                    content += data
                    members[member.name.rsplit('/', 1)[-1]] = data.decode('utf-8')
                for secret in sentinels:
                    self.assertNotIn(secret.encode('utf-8'), content, member.name)
        # Serialized objects remain structured diagnostics, not whole-message deletion.
        for line in members['logread.txt'].splitlines():
            node = json.loads(line.split('mihomo: ', 1)[1])
            self.assertTrue(isinstance(node, (dict, list)))
        self.assertIn('visible', members['logread.txt'])
        json.loads(members['group-CONSOLE.json'])
        import yaml
        yaml.safe_load(members['config.masked.yaml'])

    def test_round3_ambiguous_folded_scalar_aborts_without_archive(self):
        stubs = self.round3_stubs()
        (self.case / 'group.json').write_text(json.dumps({
            'message': 'Authorization: Basic\nYWxpY2U6cHc='}), encoding='utf-8')
        self.marker.write_text('', encoding='utf-8')
        answer = self.run_diag(stubs=stubs)
        self.assertFalse(answer['ok'], answer)
        self.assert_packs_nothing()

    def test_round4_real_collector_preserves_nft_map_and_masks_payloads(self):
        stubs = self.sentinel_stubs()
        nft = self.case / 'round4-nft.txt'
        nft.write_text(
            'map ifname_to_mark { type ifname : mark; elements = { "br-lan" : 0x1 } }\n'
            + 'counter comment ' + json.dumps(r'{"pass\u0077ord":"n41!","visible":7}') + '\n'
            + r'# [{" token ":"n42!","visible":7}]' + '\n',
            encoding='utf-8', newline='\n')
        stubs += "\nnft() { cat '%s'; }\n" % posix(nft)
        answer = self.run_diag(stubs=stubs)
        self.assertTrue(answer['ok'], answer)
        nft_member = None
        with tarfile.open(self.spool / self.bundle_name(answer), 'r:gz') as bundle:
            for member in bundle.getmembers():
                content = member.name.encode('utf-8')
                if member.isfile():
                    raw = bundle.extractfile(member).read()
                    content += raw
                    if member.name.endswith('/nft.txt'):
                        nft_member = raw.decode('utf-8')
                for secret in self.E2E_SENTINELS + ('n41!', 'n42!'):
                    self.assertNotIn(secret.encode('utf-8'), content, member.name)
        self.assertIsNotNone(nft_member)
        self.assertIn('br-lan', nft_member)
        self.assertIn('0x1', nft_member)

    def test_round4_malformed_nft_payload_aborts_without_archive(self):
        nft = self.case / 'bad-nft.txt'
        nft.write_text(r'# {"to\u006ben":"n45!",', encoding='utf-8')
        self.marker.write_text('', encoding='utf-8')
        answer = self.run_diag(stubs=STUBS + "\nnft() { cat '%s'; }\n" % posix(nft))
        self.assertFalse(answer['ok'], answer)
        self.assert_packs_nothing()

    def test_malformed_uci_aborts_without_archive_or_shared_copy(self):
        stubs = self.sentinel_stubs()
        (self.case / 'network.uci').write_text(
            "network.wan=interface\nnetwork.wan.password='firstline\nunterminated",
            encoding='utf-8')
        self.marker.write_text('', encoding='utf-8')
        answer = self.run_diag(stubs=stubs)
        self.assertFalse(answer['ok'], answer)
        self.assertIn('uci:network', answer['error'])
        self.assert_packs_nothing()

    def test_creation_cap_refuses_before_opted_in_shared_copy(self):
        # Inject only tar's output, not registration: exercise actual collector cleanup.
        fat = self.case / 'fat.bin'
        fat.write_bytes(b'x' * (2097152 + 1))
        stubs = STUBS + "\ntar() { cp '%s' \"$2\"; }\n" % posix(fat)
        self.marker.write_text('', encoding='utf-8')
        answer = self.run_diag(stubs=stubs, maximum=4194304)
        self.assertFalse(answer['ok'], answer)
        self.assertIn('cap', answer['error'])
        self.assert_packs_nothing()

    def test_a_failed_redactor_aborts_and_packs_nothing(self):
        fake = self.case / 'fake-redact.py'
        fake.write_text('import sys\nsys.exit(1)\n', encoding='utf-8')
        answer = self.run_diag(diag_py=fake)
        self.assertFalse(answer['ok'], answer)
        self.assertIn('redaction failed', answer['error'])
        self.assert_packs_nothing()

    def test_the_verify_gate_refuses_an_unmasked_bundle(self):
        # A redactor that passes everything through must not be able to hand out a secret:
        # the last gate reads the files and refuses to pack.  The fake still delegates
        # `verify` to the shipped asset, because a real installer always ships the real one.
        passthrough = self.case / 'passthrough-redact.py'
        passthrough.write_text(
            'import subprocess, sys\n'
            'REAL = %r\n'
            'if len(sys.argv) > 1 and sys.argv[1] == "redact":\n'
            '    sys.stdout.buffer.write(sys.stdin.buffer.read())\n'
            'else:\n'
            '    sys.exit(subprocess.run([sys.executable, REAL] + sys.argv[1:]).returncode)\n'
            % str(REDACT), encoding='utf-8')
        answer = self.run_diag(stubs=self.sentinel_stubs(), diag_py=passthrough)
        self.assertFalse(answer['ok'], answer)
        self.assertIn('bundle contains a secret', answer['error'])
        self.assertIn('nothing was packed', answer['error'])
        self.assert_packs_nothing()

    def test_a_missing_redactor_aborts_and_packs_nothing(self):
        answer = self.run_diag(diag_py=self.case / 'missing-redact.py')
        self.assertFalse(answer['ok'], answer)
        self.assertIn('redactor missing', answer['error'])
        self.assert_packs_nothing()

    def test_an_unparsable_config_aborts_and_packs_nothing(self):
        (self.case / 'config.yaml').write_text('proxy-providers: [\n', encoding='utf-8')
        answer = self.run_diag()
        self.assertFalse(answer['ok'], answer)
        self.assertIn('yaml:mihomo', answer['error'])
        self.assert_packs_nothing()

    def test_download_manifest_remains_read_acl(self):
        manifest = json.loads((ROOT / 'tools' / 'geo-rpc.json').read_text(encoding='utf-8'))
        method = next(item for item in manifest['methods'] if item['name'] == 'console_diag_get')
        self.assertEqual(method['acl'], 'read')
        acl = json.loads((ROOT / 'assets' / 'acl-geo-sync.json').read_text(encoding='utf-8'))
        role = acl['luci-app-geo-sync']
        self.assertIn('console_diag_get', role['read']['ubus']['geo-sync'])
        self.assertNotIn('console_diag_get', role['write']['ubus']['geo-sync'])

    # ---------------------------------------------------------------- #15: the download
    def test_the_bytes_the_panel_receives_are_the_archive(self):
        built = self.run_diag()
        name = self.bundle_name(built)
        answer = self.get(name)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['name'], name)
        raw = base64.b64decode(answer['data'])
        self.assertEqual(raw, (self.spool / name).read_bytes(),
                         'the panel got something other than the archive')
        self.assertEqual(answer['bytes'], len(raw))
        self.assertEqual(answer['sha256'], hashlib.sha256(raw).hexdigest())

    def test_the_newest_bundle_can_be_fetched_without_a_name(self):
        self.run_diag()
        second = self.run_diag()
        answer = self.get('')
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['name'], self.bundle_name(second),
                         'the download did not pick the newest archive')

    def test_a_path_is_not_a_bundle_name(self):
        for name in ('../console.state', '/etc/shadow', 'etc/shadow',
                     'geomask-console-diag-..%2F..%2Fetc.tar.gz', 'other.tar.gz'):
            answer = self.get(name)
            self.assertFalse(answer['ok'], 'the backend accepted the name %r' % name)

    def test_a_missing_bundle_is_an_error_not_an_empty_file(self):
        answer = self.get('geomask-console-diag-20000101-000000.99999.tar.gz')
        self.assertFalse(answer['ok'], answer)
        self.assertIn('архива нет', answer['error'])

    def test_a_bundle_over_the_limit_is_refused_with_the_reason(self):
        """rpcd has to carry the bytes, so an oversized archive is a refusal, not a hang."""
        fat = self.spool / 'geomask-console-diag-20260101-000000.1234.tar.gz'
        fat.write_bytes(b'x' * 64)
        registered = subprocess.run([sys.executable, '-B', str(ROOT / 'assets' / 'geomask-diag-spool.py'),
                                     'register', str(self.spool), str(fat)],
                                    capture_output=True, timeout=10)
        self.assertEqual(registered.returncode, 0, registered.stdout + registered.stderr)
        answer = self.get(fat.name, maximum=32)
        self.assertFalse(answer['ok'], answer)
        self.assertIn('64', answer['error'])
        self.assertIn('32', answer['error'])


if __name__ == '__main__':
    unittest.main()
