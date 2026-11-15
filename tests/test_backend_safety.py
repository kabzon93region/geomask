"""Backend writes that must not damage the owner's configuration (audit 14.11.2026).

Every function is extracted from the shipped asset and run in bash with stub `uci`/`ip`/
`crontab` and a fake root inside a temporary directory. No router, no network.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'assets' / 'geo-backend.sh'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()
SOURCE = BACKEND.read_text(encoding='utf-8')

# The set of options the IPv6 switch records is a constant of the backend; take the real value
# instead of repeating it here, otherwise the tests would keep passing after the list changed.
_KEYS = re.search(r'^IPV6_KEYS="([^"]+)"', SOURCE, re.MULTILINE)
if _KEYS is None:
    raise AssertionError('IPV6_KEYS not found in the backend')
IPV6_KEYS = _KEYS.group(1)


def extract(name):
    """Whole function body, brace-balanced but heredoc-aware.

    The regex `^func() {.*?^}$` stopped at the first column-0 `}` — which is the closing brace
    of the nft table inside _ipv6_guard_install's heredoc, not the end of the function. The
    extracted fragment was then a syntax error, so every test that used it silently ran
    nothing.
    """
    lines = SOURCE.splitlines()
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


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


@unittest.skipIf(BASH is None, 'bash is required')
class BackendSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        self.devices = self.case / 'geo-devices.list'
        self.nodes = self.case / 'geomask-nodes.conf'
        self.owned = self.case / 'dhcp-hosts'
        self.log = self.case / 'geo-sync.log'
        self.backups = self.case / 'geomask-backups'
        self.backups.mkdir()
        # a fresh archive recorded as verified: the write gate accepts only what a backup
        # creator checked (a bare file with a fresh mtime proves nothing)
        archive = self.backups / 'router-backup-20260101-000000.tar.gz'
        archive.write_bytes(b'x')
        (self.case / 'backup-ok').write_text('file=%s\n' % posix(archive), encoding='utf-8')

    def run_backend(self, body, functions, extra=''):
        header = (
            'set -u\n'
            'DEVICES="%s"\nLOG="%s"\nSUBS_FILE="%s"\nDHCP_OWNED="%s"\n'
            'BACKUP_DIR="%s"\nNODESCONF="%s"\nNODESCAN_TAG="# geomask-nodescan"\n'
            'MIHOMO_CONF="%s"\nGM_ROUTES_LIB=/nonexistent\n'
            'AIDNS_UID_FILE="%s"\nAIDNS_OFF_FILE="%s"\nNODES_LOCK="%s"\n'
            'BACKUP_OK="%s"\n'
            'warn() { printf "WARN %%s\\n" "$*" >&2; }\n'
            % (posix(self.devices), posix(self.log), posix(self.case / 'subs.list'),
               posix(self.owned), posix(self.backups), posix(self.nodes),
               posix(self.case / 'config.yaml'), posix(self.case / 'geomask-aidns.uid'),
               posix(self.case / 'geomask-aidns.off'), posix(self.case / 'nodes.lock'),
               posix(self.case / 'backup-ok')))
        script = (header + extra + '\n'
                  + '\n'.join(extract(name) for name in functions) + '\n' + body)
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    # ---------------------------------------------------------------- nodes_set
    def test_nodes_set_keeps_fields_it_does_not_own(self):
        self.nodes.write_text('# owner settings\nbad_countries=RU\nai_guard=1\n'
                              'custom_field=keep\n', encoding='utf-8')
        extra = ('crontab() { if [ "$1" = "-l" ]; then printf "# owner cron\\n"; '
                 'else cp "$1" "$CRON_OUT"; fi; }\n'
                 'CRON_OUT="%s"\n' % posix(self.case / 'cron.txt'))
        result = self.run_backend(
            'cmd_nodes_set 1 03:30 RU AS123 Turkey sub; printf "RC=%s\\n" "$?"',
            functions=['_nconf', '_nodes_candidate', '_nodes_lock', '_nodes_unlock',
                       'cmd_nodes_set'],
            extra=extra)
        data = self.nodes.read_text(encoding='utf-8')
        self.assertIn('RC=0', result.stdout)
        self.assertIn('ai_guard=1', data)
        self.assertIn('custom_field=keep', data)
        self.assertIn('bad_countries=RU', data)
        self.assertIn('sub_provider=sub', data)
        self.assertIn('# owner cron', (self.case / 'cron.txt').read_text(encoding='utf-8'))

    def test_nodes_set_refuses_a_newline_in_a_value(self):
        before = 'bad_countries=RU\nai_guard=1\n'
        self.nodes.write_text(before, encoding='utf-8')
        result = self.run_backend(
            'cmd_nodes_set 1 03:30 RU AS123 "$(printf \'x\\nai_guard=0\')" sub; '
            'printf "RC=%s\\n" "$?"',
            functions=['_nconf', '_nodes_candidate', '_nodes_lock', '_nodes_unlock',
                       'cmd_nodes_set'],
            extra='crontab() { return 0; }\n')
        self.assertIn('RC=1', result.stdout)
        self.assertIn('control characters', result.stdout)
        self.assertEqual(self.nodes.read_text(encoding='utf-8'), before)

    def test_a_failed_cron_write_restores_the_settings(self):
        before = 'bad_countries=RU\nai_guard=1\n'
        self.nodes.write_text(before, encoding='utf-8')
        result = self.run_backend(
            'cmd_nodes_set 1 03:30 DE AS1 Turkey sub; printf "RC=%s\\n" "$?"',
            functions=['_nconf', '_nodes_candidate', '_nodes_lock', '_nodes_unlock',
                       'cmd_nodes_set'],
            extra='crontab() { if [ "$1" = "-l" ]; then printf "x\\n"; return 0; fi; return 1; }\n')
        self.assertIn('RC=1', result.stdout)
        self.assertEqual(self.nodes.read_text(encoding='utf-8'), before)

    # ---------------------------------------------------------------- devices
    def test_a_foreign_dhcp_lease_is_not_deleted(self):
        self.devices.write_text('192.168.1.2 bypass phone\n', encoding='utf-8')
        extra = r'''
uci() {
  case "$*" in
    "show dhcp") printf "dhcp.owner=host\ndhcp.owner.ip='192.168.1.2'\n" ;;
    "-q get dhcp.owner.ip") printf '192.168.1.2' ;;
    "-q get dhcp.owner.mac") printf 'aa:bb:cc:dd:ee:ff' ;;
    "delete "*) printf 'DELETED %s\n' "$*" ;;
    "commit "*) printf 'COMMIT %s\n' "$*" ;;
    *) return 0 ;;
  esac
}
'''
        result = self.run_backend('cmd_dev_del 192.168.1.2; printf "RC=%s\\n" "$?"',
                                  functions=['cmd_dev_del', '_ipv4_ok', '_require_backup',
                                             '_write_gate'],
                                  extra=extra + '_apply_routing() { :; }\ncmd_status() { :; }\n'
                                                 'ip() { :; }\n')
        self.assertNotIn('DELETED', result.stdout)
        self.assertEqual(self.devices.read_text(encoding='utf-8'), '')

    def test_an_impossible_address_is_refused(self):
        result = self.run_backend('cmd_dev_add 999.1.1.1 ai "" test; printf "RC=%s\\n" "$?"',
                                  functions=['cmd_dev_add', '_ipv4_ok', '_require_backup',
                                             '_write_gate'],
                                  extra='_apply_routing() { :; }\ncmd_status() { :; }\n')
        self.assertIn('RC=1', result.stdout)
        self.assertIn('bad ip', result.stdout)
        self.assertFalse(self.devices.exists() and self.devices.read_text(encoding='utf-8').strip())

    # ---------------------------------------------------------------- AI-DNS
    def test_unknown_listen_address_fails_closed(self):
        dest = self.case / '98-geomask-ai-dns.nft'
        self.devices.write_text('192.168.1.50 ai tv\n', encoding='utf-8')
        (self.case / 'geomask-aidns.uid').write_text('65533\n', encoding='utf-8')
        extra = ('nft() { return 0; }\n'
                 'AIDNS_NFT_DEST="%s"\nAIDNS_DNS_PORT=5353\n'
                 '_gm_lan_ip() { printf "192.168.1.1"; }\n'
                 '_gm_aidns_ip() { printf ""; }\n' % posix(dest))
        result = self.run_backend('_aidns_sync; printf "RC=%s\\n" "$?"',
                                  functions=['_aidns_sync'], extra=extra)
        self.assertIn('RC=0', result.stdout)
        text = dest.read_text(encoding='utf-8')
        self.assertIn('counter reject', text)
        self.assertNotIn('dnat ip to', text)

    def test_a_changed_address_rewrites_the_same_device_list(self):
        dest = self.case / '98-geomask-ai-dns.nft'
        self.devices.write_text('192.168.1.50 ai tv\n', encoding='utf-8')
        (self.case / 'geomask-aidns.uid').write_text('65533\n', encoding='utf-8')
        base = ('nft() { return 0; }\n'
                'AIDNS_NFT_DEST="%s"\nAIDNS_DNS_PORT=5353\n'
                '_gm_lan_ip() { printf "192.168.1.1"; }\n' % posix(dest))
        self.run_backend('_aidns_sync', functions=['_aidns_sync'],
                         extra=base + '_gm_aidns_ip() { printf "192.168.1.53"; }\n')
        first = dest.read_text(encoding='utf-8')
        self.assertIn('dnat ip to 192.168.1.53:5353', first)
        self.run_backend('_aidns_sync', functions=['_aidns_sync'],
                         extra=base + '_gm_aidns_ip() { printf "192.168.1.54"; }\n')
        second = dest.read_text(encoding='utf-8')
        self.assertIn('dnat ip to 192.168.1.54:5353', second)
        self.assertNotIn('192.168.1.53', second)


    # ---------------------------------------------------------------- IPv6 (F14/G04)
    def uci_db(self, **values):
        """Stateful uci stub: get/set/delete work on files, like the real command."""
        db = self.case / 'uci-db'
        db.mkdir(exist_ok=True)
        for key, value in values.items():
            name = key.replace('.', '_')
            if value is None:
                (db / name).unlink(missing_ok=True)
            else:
                (db / name).write_text(value, encoding='utf-8')
        return db

    def uci_extra(self, db):
        stub = '''IPV6_STATE="@STATE@"
IPV6_KEYS="@KEYS@"
IPV6_PROC="@PROC@"
IPV6_NFT="@NFT@"
IPV6_SYSCTL="@SYSCTL@"
UCI_DB="@DB@"
sysctl() { :; }
uci() {
  _a="$1"; shift
  [ "$_a" = "-q" ] && { _a="$1"; shift; }
  case "$_a" in
    get) _f="$UCI_DB/$(printf '%s' "$1" | tr '.' '_')"; [ -f "$_f" ] && cat "$_f" || return 1 ;;
    set) _kv="$1"; _k=${_kv%%=*}; _v=${_kv#*=}
         printf '%s' "$_v" > "$UCI_DB/$(printf '%s' "$_k" | tr '.' '_')" ;;
    delete) rm -f "$UCI_DB/$(printf '%s' "$1" | tr '.' '_')" ;;
    commit) : ;;
    *) return 0 ;;
  esac
}
'''
        return (stub.replace('@STATE@', posix(self.case / 'ipv6.state'))
                    .replace('@KEYS@', IPV6_KEYS)
                    .replace('@PROC@', posix(self.case / 'proc'))
                    .replace('@NFT@', posix(self.case / 'nft' / '99-geomask-no-v6.nft'))
                    .replace('@SYSCTL@', posix(self.case / 'sysctl' / '99-disable-ipv6.conf'))
                    .replace('@DB@', posix(db)))

    def test_ipv6_off_then_on_keeps_prefix_size_and_odhcpd_flags(self):
        # The regression G04: the second save (on the way back) recorded our own 0/disabled
        # values and the restore then "restored" them.
        db = self.uci_db(**{'network.lan.ip6assign': '56',
                            'dhcp.lan.ra': 'server',
                            'dhcp.lan.dhcpv6': 'server',
                            'dhcp.lan.ndp': 'hybrid'})
        body = ('_ipv6_save\n'
                'uci set network.lan.ip6assign=0\n'
                'uci set dhcp.lan.ra=disabled\n'
                'uci set dhcp.lan.dhcpv6=disabled\n'
                'uci set dhcp.lan.ndp=disabled\n'
                '_ipv6_save\n'
                '_ipv6_restore\n'
                'printf "ip6assign=%s\\n" "$(uci -q get network.lan.ip6assign)"\n'
                'printf "ra=%s\\n" "$(uci -q get dhcp.lan.ra)"\n'
                'printf "dhcpv6=%s\\n" "$(uci -q get dhcp.lan.dhcpv6)"\n'
                'printf "ndp=%s\\n" "$(uci -q get dhcp.lan.ndp)"\n')
        result = self.run_backend(body, functions=['_ipv6_state_get', '_ipv6_record_ok',
                                                   '_ipv6_read_kernel', '_ipv6_save',
                                                   '_ipv6_restore'],
                                  extra=self.uci_extra(db))
        self.assertIn('ip6assign=56', result.stdout)
        self.assertIn('ra=server', result.stdout)
        self.assertIn('dhcpv6=server', result.stdout)
        self.assertIn('ndp=hybrid', result.stdout)

    def test_ipv6_on_removes_options_that_did_not_exist_before(self):
        db = self.uci_db()
        body = ('_ipv6_save\n'
                'uci set network.lan.ip6assign=0\n'
                'uci set dhcp.lan.ra=disabled\n'
                '_ipv6_restore\n'
                'for k in network.lan.ip6assign dhcp.lan.ra dhcp.lan.dhcpv6 dhcp.lan.ndp; do\n'
                '  uci -q get "$k" >/dev/null 2>&1 && printf "STILL %s\\n" "$k"\n'
                'done\n'
                'printf "DONE\\n"\n')
        result = self.run_backend(body, functions=['_ipv6_state_get', '_ipv6_record_ok',
                                                   '_ipv6_read_kernel', '_ipv6_save',
                                                   '_ipv6_restore'],
                                  extra=self.uci_extra(db))
        self.assertNotIn('STILL', result.stdout)
        self.assertIn('DONE', result.stdout)

    def test_ipv6_off_then_on_restores_every_layer(self):
        # Three layers must move together: kernel switch, LAN/odhcpd options, forwarding guard.
        db = self.uci_db(**{'network.lan.ip6assign': '56',
                            'dhcp.lan.ra': 'server',
                            'dhcp.lan.dhcpv6': 'server',
                            'dhcp.lan.ndp': 'hybrid'})
        proc = self.case / 'proc'
        (proc / 'br-lan').mkdir(parents=True)
        iface = proc / 'br-lan' / 'disable_ipv6'
        iface.write_text('0', encoding='utf-8')
        sysctl_conf = self.case / 'sysctl' / '99-disable-ipv6.conf'
        guard = self.case / 'nft' / '99-geomask-no-v6.nft'
        extra = (self.uci_extra(db)
                 + 'IPV6_PROC="%s"\nIPV6_SYSCTL="%s"\nIPV6_NFT="%s"\n'
                   'sysctl() { :; }\n'
                 % (posix(proc), posix(sysctl_conf), posix(guard)))
        functions = ['_ipv6_state_get', '_ipv6_record_ok', '_ipv6_read_kernel', '_ipv6_save',
                     '_ipv6_restore', '_ipv6_kernel', '_ipv6_kernel_restore',
                     '_ipv6_guard_install', '_ipv6_guard_remove', '_ipv6_guard_restore',
                     '_ipv6_apply_off', '_ipv6_apply_on']
        off = self.run_backend('_ipv6_apply_off; printf "RC=%s\\n" "$?"',
                               functions=functions, extra=extra)
        self.assertIn('RC=0', off.stdout)
        self.assertEqual((db / 'network_lan_ip6assign').read_text(encoding='utf-8'), '0')
        self.assertEqual((db / 'dhcp_lan_ra').read_text(encoding='utf-8'), 'disabled')
        self.assertEqual((db / 'dhcp_lan_dhcpv6').read_text(encoding='utf-8'), 'disabled')
        self.assertEqual((db / 'dhcp_lan_ndp').read_text(encoding='utf-8'), 'disabled')
        self.assertEqual(iface.read_text(encoding='utf-8').strip(), '1')
        self.assertTrue(sysctl_conf.is_file())
        self.assertIn('disable_ipv6 = 1', sysctl_conf.read_text(encoding='utf-8'))
        self.assertTrue(guard.is_file())
        self.assertIn('meta nfproto ipv6', guard.read_text(encoding='utf-8'))
        self.assertIn('drop', guard.read_text(encoding='utf-8'))
        state = (self.case / 'ipv6.state').read_text(encoding='utf-8')
        self.assertIn('ip6assign=56', state)
        self.assertIn('ra=server', state)

        on = self.run_backend('_ipv6_apply_on; printf "RC=%s\\n" "$?"',
                              functions=functions, extra=extra)
        self.assertIn('RC=0', on.stdout)
        self.assertEqual((db / 'network_lan_ip6assign').read_text(encoding='utf-8'), '56')
        self.assertEqual((db / 'dhcp_lan_ra').read_text(encoding='utf-8'), 'server')
        self.assertEqual((db / 'dhcp_lan_dhcpv6').read_text(encoding='utf-8'), 'server')
        self.assertEqual((db / 'dhcp_lan_ndp').read_text(encoding='utf-8'), 'hybrid')
        self.assertEqual(iface.read_text(encoding='utf-8').strip(), '0')
        self.assertFalse(sysctl_conf.exists())
        self.assertFalse(guard.exists())

    def test_ipv6_refuses_a_partial_record(self):
        (self.case / 'ipv6.state').write_text('ip6assign=60abc\n', encoding='utf-8')
        db = self.uci_db(**{'network.lan.ip6assign': '0'})
        result = self.run_backend('_ipv6_restore; printf "RC=%s\\n" "$?"',
                                  functions=['_ipv6_state_get', '_ipv6_record_ok',
                                             '_ipv6_restore'],
                                  extra=self.uci_extra(db))
        self.assertIn('RC=1', result.stdout)
        self.assertEqual((db / 'network_lan_ip6assign').read_text(encoding='utf-8'), '0')

    def test_ipv6_save_failure_is_reported(self):
        blocker = self.case / 'blocked'
        blocker.write_text('x', encoding='utf-8')     # a file where a directory is needed
        result = self.run_backend('_ipv6_save; printf "RC=%s\\n" "$?"',
                                  functions=['_ipv6_state_get', '_ipv6_record_ok', '_ipv6_save'],
                                  extra='IPV6_STATE="%s"\n' % posix(blocker / 'ipv6.state'))
        self.assertIn('RC=1', result.stdout)

    # ---------------------------------------------------------------- console kill switch
    def console_extra(self):
        initd = self.case / 'hev-console'
        initd.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "%s"\n'
                         % ('%s', posix(self.case / 'initd-calls')), encoding='utf-8',
                         newline='\n')
        initd.chmod(0o755)
        return ('CONSOLE_STATE="%s"\nCONSOLE_INITD="%s"\nCONSOLE_TABLE=102\n'
                'CONSOLE_TUN_DEV=Console\nCONSOLE_RULE_PREF=19903\n'
                'CONSOLE_SOCKS_PORT=7893\nCONSOLE_TUN_IP=198.21.0.1\n'
                'CONSOLE_PY=/nonexistent/geomask-console.py\n'
                '_require_backup() { return 0; }\n'
                'ip() { printf "IP %%s\\n" "$*" >> "%s"; return 1; }\n'
                'conntrack() { printf "CT %%s\\n" "$*" >> "%s"; }\n'
                % (posix(self.case / 'console.state'), posix(initd),
                   posix(self.case / 'ip-calls'), posix(self.case / 'ct-calls')))

    def test_console_off_is_a_real_kill_switch(self):
        (self.case / 'console.state').write_text('enabled=1\nip=192.0.2.50\n', encoding='utf-8')
        result = self.run_backend(
            'cmd_console off 192.0.2.50; printf "\\nRC=%s\\n" "$?"',
            functions=['_console_get', '_console_set', '_console_rule_off', 'cmd_console'],
            extra=self.console_extra())
        self.assertIn('"enabled":false', result.stdout)
        self.assertIn('RC=0', result.stdout)
        state = (self.case / 'console.state').read_text(encoding='utf-8')
        self.assertIn('enabled=0', state)
        self.assertIn('ip=192.0.2.50', state)          # remembered for the next switch-on
        ip_calls = (self.case / 'ip-calls').read_text(encoding='utf-8')
        self.assertIn('rule del from 192.0.2.50 lookup 102', ip_calls)
        initd = (self.case / 'initd-calls').read_text(encoding='utf-8')
        self.assertIn('stop', initd)
        self.assertIn('disable', initd)

    def test_console_on_refuses_a_bad_address_before_touching_anything(self):
        before = 'enabled=1\nip=192.0.2.50\n'
        (self.case / 'console.state').write_text(before, encoding='utf-8')
        result = self.run_backend(
            'cmd_console on 999.1.1.1; printf "\\nRC=%s\\n" "$?"',
            functions=['_console_get', '_console_set', '_ipv4_ok', 'cmd_console'],
            extra=self.console_extra())
        self.assertIn('bad or missing console IP', result.stdout)
        self.assertIn('RC=1', result.stdout)
        self.assertEqual((self.case / 'console.state').read_text(encoding='utf-8'), before)

    def test_console_state_replaces_one_key_and_keeps_the_rest(self):
        state = self.case / 'console.state'
        state.write_text('enabled=1\nip=192.0.2.50\ncountries=TR\n', encoding='utf-8')
        result = self.run_backend(
            '_console_set countries "TR,DE"; _console_get countries',
            functions=['_console_get', '_console_set'], extra=self.console_extra())
        self.assertEqual(result.stdout.strip(), 'TR,DE')
        text = state.read_text(encoding='utf-8')
        self.assertEqual(text.count('countries='), 1)
        self.assertIn('ip=192.0.2.50', text)

    def test_the_console_bundle_never_carries_credentials(self):
        # The owner hands the bundle over as-is, so masking has to happen here: the path of a
        # subscription URL is the token, and `secret:` is the controller password.
        src = self.case / 'config.yaml'
        src.write_text('mixed-port: 7890\n'
                       'secret: "s3cr3t-token"\n'
                       'proxy-providers:\n'
                       '  sub:\n'
                       '    type: http\n'
                       '    url: "https://sub.example.com/-a9BmJwWGvh61Zg2"\n'
                       '    username: alice\n'
                       '    password: hunter2\n', encoding='utf-8')
        out = self.case / 'masked.yaml'
        self.run_backend('_console_mask_stream < "%s" > "%s"' % (posix(src), posix(out)),
                         functions=['_console_mask_stream'], extra='')
        text = out.read_text(encoding='utf-8')
        self.assertNotIn('a9BmJwWGvh61Zg2', text)
        self.assertNotIn('s3cr3t-token', text)
        self.assertNotIn('hunter2', text)
        self.assertNotIn('alice', text)
        self.assertIn('mixed-port: 7890', text)      # structure is kept for diagnosis
        self.assertIn('https://…', text)

    def test_uci_secrets_are_masked_too(self):
        # `uci show network/firewall` goes into the same bundle and carries the real keys.
        sample = ("config interface 'wan'\n"
                  "\toption password 'pppoe-secret'\n"
                  "config wireguard 'wg0'\n"
                  "\toption private_key 'PRIVKEY-abc'\n"
                  "\toption preshared_key 'PSK-xyz'\n"
                  "\tlist addresses '10.0.0.2/32'\n")
        src = self.case / 'network.uci'
        src.write_text(sample, encoding='utf-8', newline='\n')
        out = self.case / 'network.masked'
        self.run_backend('_console_mask_stream < "%s" > "%s"' % (posix(src), posix(out)),
                         functions=['_console_mask_stream'], extra='')
        text = out.read_text(encoding='utf-8')
        self.assertNotIn('pppoe-secret', text)
        self.assertNotIn('PRIVKEY-abc', text)
        self.assertNotIn('PSK-xyz', text)
        self.assertIn("list addresses '10.0.0.2/32'", text)

    # ---------------------------------------------------------------- credentials (F23)
    def test_a_subscription_url_never_shows_its_credentials(self):
        extra = 'URL="https://user:secret@example.com/sub?token=abcdef"\n'
        result = self.run_backend('_mask_url "$URL"', functions=['_mask_url'], extra=extra)
        self.assertEqual(result.stdout, 'https://example.com/…')
        self.assertNotIn('secret', result.stdout)
        self.assertNotIn('token', result.stdout)

    def test_a_plain_subscription_url_keeps_the_host_only(self):
        extra = 'URL="https://sub.example.org/api/v1/list?id=42"\n'
        result = self.run_backend('_mask_url "$URL"', functions=['_mask_url'], extra=extra)
        self.assertEqual(result.stdout, 'https://sub.example.org/…')


if __name__ == '__main__':
    unittest.main()
