"""Network-facing helpers: AI-DNS address, LAN device discovery, route sync, SMB share.

Every test extracts the real function from the template or the backend and runs it in bash
with stubbed `ip`/`uci`, so no interface, route or UCI setting outside the temporary
directory is touched.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'geomask.sh.in'
BACKEND = ROOT / 'assets' / 'geo-backend.sh'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


def extract(path, name):
    text = path.read_text(encoding='utf-8')
    match = re.search(r'^' + re.escape(name) + r'\(\) \{.*?^\}\s*$', text,
                      re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError('%s not found in %s' % (name, path.name))
    return match.group(0)


@unittest.skipIf(BASH is None, 'bash is required')
class NetworkSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = self.root / 'case'
        self.case.mkdir()

    def run_script(self, body, functions, source=TEMPLATE, extra=''):
        script = ('set -u\n'
                  "P() { printf '%s%s' " + repr(posix(self.case)) + ' "$1"; }\n'
                  'info() { printf "INFO %s\\n" "$*"; }\n'
                  'warn() { printf "WARN %s\\n" "$*"; }\n'
                  'bad() { printf "FAIL %s\\n" "$*"; }\n'
                  'sleep() { :; }\n'
                  'have() { command -v "$1" >/dev/null 2>&1; }\n'
                  'lan_ip() { printf "%s" "${TEST_LAN_IP:-192.168.1.1}"; }\n'
                  'lan_iface() { printf "br-lan"; }\n'
                  'SMB_SHARE_NAME=geomask-backups\n'
                  'BACKUP_DIR=/root/geomask-backups\n'
                  'SMB_SHARE_ENABLE=/etc/geomask/backup-share.enable\n'
                  + extra + '\n'
                  + '\n'.join(extract(source, name) for name in functions) + '\n' + body)
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, posix(path)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', timeout=60)

    # ------------------------------------------------------------ AI-DNS address
    IP_STUB = r'''
ip() {
  case "$*" in
    *"addr show br-lan"*) printf '    inet @CIDR@ brd 10.0.0.255 scope global br-lan\n' ;;
    *"addr show "*) return 0 ;;
    *"neigh show "*) return 0 ;;
    *) return 0 ;;
  esac
}
uci() { return 1; }
'''

    def aidns(self, cidr, extra=''):
        address = cidr.split('/')[0]
        stub = self.IP_STUB.replace('@CIDR@', cidr)
        stub += '\nlan_ip() { printf "%s" "' + address + '"; }\n'
        return self.run_script('_a=$(aidns_ip); printf "IP=[%s]\\nRC=%s\\n" "$_a" "$?"',
                               functions=['aidns_ip', '_aidns_usable', '_aidns_taken'],
                               extra=stub + extra)

    def test_a_slash_24_lan_gets_dot_53(self):
        result = self.aidns('192.168.1.1/24')
        self.assertIn('192.168.1.53', result.stdout)

    def test_a_slash_25_lan_does_not_get_an_address_outside_the_subnet(self):
        # 10.0.0.53 is outside 10.0.0.128/25: the old code pointed DNS there anyway
        result = self.aidns('10.0.0.129/25')
        self.assertNotIn('10.0.0.53', result.stdout)
        self.assertIn('RC=0', result.stdout)

    def test_a_router_whose_own_address_ends_in_53_gets_no_alias(self):
        result = self.aidns('10.0.0.53/24')
        self.assertNotIn('10.0.0.53', result.stdout)

    def test_a_small_subnet_has_no_safe_dot_53(self):
        result = self.aidns('10.0.0.1/30')
        self.assertNotIn('10.0.0.53', result.stdout)

    def test_an_explicit_override_wins(self):
        target = self.case / 'etc/geomask-aidns.ip'
        target.parent.mkdir(parents=True)
        target.write_text('192.168.1.99\n', encoding='utf-8')
        result = self.aidns('192.168.1.1/24')
        self.assertIn('192.168.1.99', result.stdout)

    def test_an_override_outside_the_subnet_is_refused(self):
        target = self.case / 'etc/geomask-aidns.ip'
        target.parent.mkdir(parents=True)
        target.write_text('10.9.9.9\n', encoding='utf-8')
        result = self.aidns('192.168.1.1/24')
        self.assertIn('IP=[]', result.stdout)
        self.assertIn('RC=0', result.stdout)

    def test_an_override_with_invalid_octets_is_refused(self):
        target = self.case / 'etc/geomask-aidns.ip'
        target.parent.mkdir(parents=True)
        target.write_text('999.1.1.1\n', encoding='utf-8')
        result = self.aidns('192.168.1.1/24')
        self.assertIn('IP=[]', result.stdout)
        self.assertIn('RC=0', result.stdout)

    def test_an_override_equal_to_the_router_is_refused(self):
        target = self.case / 'etc/geomask-aidns.ip'
        target.parent.mkdir(parents=True)
        target.write_text('192.168.1.1\n', encoding='utf-8')
        result = self.aidns('192.168.1.1/24')
        self.assertIn('IP=[]', result.stdout)
        self.assertIn('RC=0', result.stdout)

    def test_an_override_that_is_already_in_use_is_refused(self):
        target = self.case / 'etc/geomask-aidns.ip'
        target.parent.mkdir(parents=True)
        target.write_text('192.168.1.50\n', encoding='utf-8')
        stub = self.IP_STUB.replace('@CIDR@', '192.168.1.1/24')
        stub += ('\nip() { case "$*" in\n'
                 '  *"addr show br-lan"*) printf "    inet 192.168.1.1/24 brd 192.168.1.255 scope global br-lan\\n" ;;\n'
                 '  *"addr show"*) printf "    inet 192.168.1.50/24 scope global br-other\\n" ;;\n'
                 '  *"neigh show "*) return 0 ;;\n'
                 '  *) return 0 ;;\n'
                 'esac; }\n')
        result = self.run_script('aidns_ip; printf "\\nRC=%s\\n" "$?"',
                                 functions=['aidns_ip', '_aidns_usable', '_aidns_taken'],
                                 extra=stub)
        self.assertNotIn('192.168.1.50', result.stdout)
        self.assertIn('RC=0', result.stdout)

    def test_an_existing_alias_is_kept_on_a_rerun(self):
        # .77 is NOT the default candidate (.53), so this test proves the alias branch is used
        stub = self.IP_STUB + '\nuci() { [ "$*" = "-q get network.lanai.ipaddr" ] && printf "192.168.1.77"; return 0; }\n'
        result = self.run_script('aidns_ip', functions=['aidns_ip', '_aidns_usable', '_aidns_taken'],
                                 extra=stub)
        self.assertIn('192.168.1.77', result.stdout)

    def test_a_stale_alias_outside_the_subnet_is_replaced(self):
        stub = self.IP_STUB + '\nuci() { [ "$*" = "-q get network.lanai.ipaddr" ] && printf "10.9.9.9"; return 0; }\n'
        result = self.run_script('aidns_ip; printf "\\nRC=%s\\n" "$?"',
                                 functions=['aidns_ip', '_aidns_usable', '_aidns_taken'],
                                 extra=stub)
        self.assertIn('192.168.1.53', result.stdout)
        self.assertNotIn('10.9.9.9', result.stdout)
    def test_prefix_to_dotted_netmask(self):
        result = self.run_script('_prefix_to_mask 24; printf "\\n"; _prefix_to_mask 25; printf "\\n"',
                                 functions=['_prefix_to_mask'])
        self.assertIn('255.255.255.0', result.stdout)
        self.assertIn('255.255.255.128', result.stdout)

    # ------------------------------------------------------------ WAN device
    def test_wan_ignores_a_default_route_through_the_tunnel(self):
        extra = r'''
ip() { case "$*" in "route show default") printf 'default dev Mihomo scope link\n' ;; *) return 0 ;; esac; }
uci() { [ "$*" = "-q get network.wan.device" ] && printf 'eth1'; return 0; }
mihomo_tun() { printf 'Mihomo'; }
'''
        result = self.run_script('wan_iface; printf "\\n"', functions=['wan_iface'], extra=extra)
        self.assertIn('eth1', result.stdout)
        self.assertNotIn('Mihomo', result.stdout)

    def test_wan_keeps_a_physical_default_route(self):
        extra = r'''
ip() { case "$*" in "route show default") printf 'default via 10.0.0.1 dev pppoe-wan proto static\n' ;; *) return 0 ;; esac; }
uci() { return 1; }
mihomo_tun() { printf 'Mihomo'; }
'''
        result = self.run_script('wan_iface; printf "\\n"', functions=['wan_iface'], extra=extra)
        self.assertIn('pppoe-wan', result.stdout)

    # ------------------------------------------------------------ LAN devices
    def test_lan_devices_come_from_the_firewall_zone_not_a_fixed_list(self):
        extra = r'''
uci() {
  case "$*" in
    "-q show firewall") printf "firewall.@zone[0]=zone\nfirewall.@zone[1]=zone\n" ;;
    "-q get firewall.@zone[0].name") printf 'wan' ;;
    "-q get firewall.@zone[1].name") printf 'lan' ;;
    "-q get firewall.@zone[1].network") printf 'lan guest' ;;
    "-q get network.lan.device") printf 'br-lan' ;;
    "-q get network.guest.device") printf 'br-guest' ;;
    "-q get network.lan.ifname") return 1 ;;
    *) return 1 ;;
  esac
}
ip() { case "$*" in "link show "*) return 0 ;; esac; return 0; }
'''
        result = self.run_script('_gm_lan_devices; printf "\\n"',
                                 functions=['_gm_lan_iface', '_gm_lan_devices'],
                                 source=BACKEND, extra=extra)
        self.assertIn('br-lan', result.stdout)
        self.assertIn('br-guest', result.stdout)

    def test_nft_names_are_quoted(self):
        result = self.run_script('_gm_nft_names "br-lan br-guest"; printf "\\n"',
                                 functions=['_gm_nft_names'], source=BACKEND)
        self.assertIn('"br-lan", "br-guest"', result.stdout)

    # ------------------------------------------------------------ route sync
    ROUTE_SOURCE = ROOT / 'assets' / 'geomask-routes.sh'

    def route_sync(self, main, table, owned=None, fail_main=False):
        state = self.case / 'routes'
        state.mkdir(exist_ok=True)
        if owned is not None:
            (state / 'owned-100').write_text('\n'.join(owned) + ('\n' if owned else ''),
                                             encoding='utf-8', newline='\n')

        def q(lines):
            return ' '.join("'" + line.replace("'", "'\\''") + "'" for line in lines) or "''"

        stub = ('ip() {\n'
                '  case "$*" in\n'
                '    "route show table main")\n'
                + ('      return 2 ;;\n' if fail_main
                   else '      printf "%s\\n" ' + q(main) + ' ;;\n')
                + '    "route show table 100 exact "*) return 1 ;;\n'
                '    "route show table 100") printf "%s\\n" ' + q(table) + ' ;;\n'
                '    "route replace table 100 "*) printf "REPLACE %s\\n" "$*" ;;\n'
                '    "route del table 100 "*) printf "DEL %s\\n" "$*" ;;\n'
                '    "-4 addr show br-lan") printf "    inet 10.0.0.1/24 brd 10.0.0.255 scope global br-lan\\n" ;;\n'
                '    "link show Wireguard") return 1 ;;\n'
                '    *) return 0 ;;\n'
                '  esac\n'
                '}\n'
                'uci() { case "$*" in\n'
                '  "-q show firewall") printf "firewall.@zone[0]=zone\\n" ;;\n'
                '  "-q get firewall.@zone[0].name") printf "lan" ;;\n'
                '  "-q get firewall.@zone[0].network") printf "lan" ;;\n'
                '  "-q get network.lan.device") printf "br-lan" ;;\n'
                '  *) return 1 ;;\n'
                'esac; }\n'
                'GM_ROUTE_STATE_DIR=' + posix(state) + '\n'
                # R04: the route table is now serialised by the shared kernel lock helper.
                'GM_LOCK_DIR=' + posix(self.case / 'locks') + '\n'
                'GM_LOCK_LIB=' + posix(ROOT / 'assets' / 'geomask-lock.sh') + '\n'
                '. "$GM_LOCK_LIB"\n')
        return self.run_script('geomask_routes_sync 100; printf "\\nRC=%s\\n" "$?"',
                               functions=['_gm_route_owned', '_gm_route_networks',
                                          '_gm_route_devices', '_gm_route_normalize',
                                          '_gm_route_local_lines', '_gm_route_in_prefix',
                                          '_gm_route_cleanup', '_gm_route_lock',
                                          '_gm_route_unlock', 'geomask_routes_sync'],
                               source=self.ROUTE_SOURCE, extra=stub)

    def test_only_lan_connected_routes_are_mirrored(self):
        """A public prefix that appears on the WAN must never enter the tunnel table."""
        main = ['10.0.0.0/24 dev br-lan proto kernel scope link src 10.0.0.1',
                '203.0.113.0/24 via 10.0.0.1 dev pppoe-wan',
                'default via 10.0.0.1 dev pppoe-wan']
        result = self.route_sync(main, [])
        self.assertIn('REPLACE route replace table 100 10.0.0.0/24', result.stdout)
        self.assertNotIn('203.0.113.0/24', result.stdout)
        self.assertNotIn('default', result.stdout)

    def test_a_foreign_route_in_the_table_is_never_removed(self):
        main = ['10.0.0.0/24 dev br-lan scope link']
        table = ['172.16.0.0/24 dev wg0 scope link']
        result = self.route_sync(main, table, owned=[])
        self.assertNotIn('DEL', result.stdout)
        self.assertIn('RC=0', result.stdout)

    def test_a_stale_route_we_installed_is_removed(self):
        main = ['10.0.0.0/24 dev br-lan scope link']
        table = ['10.0.0.0/24 dev br-lan scope link', '172.16.0.0/24 dev br-lan scope link']
        # `owned` keeps the FULL route line: a record that is not installed verbatim any more is
        # no longer ours, and deleting it by prefix could take a foreign entry with it.
        result = self.route_sync(main, table, owned=table)
        self.assertIn('DEL route del table 100 172.16.0.0/24', result.stdout)
        self.assertNotIn('DEL route del table 100 10.0.0.0/24', result.stdout)

    def test_a_failed_main_read_still_keeps_the_lan_reachable(self):
        """Without the routing table the connected LAN prefixes are mirrored from the addresses.

        The old code treated the empty snapshot as truth and deleted every entry; now the
        policy table keeps the LAN reachable and foreign entries stay untouched.
        """
        table = ['172.16.0.0/24 dev wg0 scope link']
        result = self.route_sync([], table, owned=[], fail_main=True)
        self.assertIn('REPLACE route replace table 100 10.0.0.0/24', result.stdout)
        self.assertNotIn('DEL', result.stdout)
        self.assertIn('RC=0', result.stdout)

    def test_hotplug_hands_the_event_to_the_shared_helper(self):
        """R05: ifup only queues; the shared helper owns the ip/uci work."""
        text = (ROOT / 'assets' / '99-default-vpn-route').read_text(encoding='utf-8')
        self.assertIn('gm_route_enqueue', text)
        self.assertIn('GM_ROUTES_LIB', text)
        self.assertIn('gm_route_worker', text)
        self.assertNotIn('geomask_routes_sync', text,
                         'the netifd path still runs the route writer itself')
        self.assertNotIn('sync_table', text)
        self.assertNotIn('apply_routes', text)

    def test_hotplug_without_the_helper_changes_nothing(self):
        """No route helper = no lock primitive and no queue: exit 2, nothing written."""
        state = self.case / 'routes-state'
        result = self.run_script(
            'ACTION=ifup\n'
            '. ' + repr(posix(ROOT / 'assets' / '99-default-vpn-route')) + '\n'
            'printf "AFTER\\n"\n',
            functions=[],
            extra='GM_ROUTES_LIB=/nonexistent/geomask-routes.sh\n'
                  'GM_ROUTE_STATE_DIR=' + posix(state) + '\n'
                  'export GM_ROUTES_LIB GM_ROUTE_STATE_DIR\n'
                  'logger() { :; }\n')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn('AFTER', result.stdout, 'the entrypoint continued without a helper')
        self.assertFalse(state.exists(), 'a missing helper still created route state')

    # ------------------------------------------------------------ SMB share
    SMB_FUNCS = ['_smb_sections', '_smb_ours', 'ensure_smb_share']

    def smb(self, extra=''):
        initd = self.case / 'etc/init.d/ksmbd'
        initd.parent.mkdir(parents=True, exist_ok=True)
        initd.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        initd.chmod(0o755)
        conf = self.case / 'etc/ksmbd/ksmbd.conf'
        conf.parent.mkdir(parents=True, exist_ok=True)
        if not conf.exists():
            conf.write_text('[geomask-backups]\n', encoding='utf-8')
        return self.run_script('ensure_smb_share; printf "\\nRC=%s\\n" "$?"',
                               functions=self.SMB_FUNCS, extra=extra)

    def test_the_share_is_not_created_without_an_explicit_opt_in(self):
        extra = r'''
uci() { printf 'UCI %s\n' "$*"; return 0; }
'''
        result = self.smb(extra=extra)
        self.assertIn('RC=0', result.stdout)
        self.assertNotIn('UCI set', result.stdout)
        self.assertNotIn('UCI add', result.stdout)
        self.assertNotIn('UCI delete', result.stdout)
        self.assertIn('не публикуется', result.stdout)

    def test_an_opted_in_share_is_authenticated_and_has_no_fixed_account(self):
        marker = self.case / 'etc/geomask/backup-share.enable'
        marker.parent.mkdir(parents=True)
        marker.write_text('', encoding='utf-8')
        extra = r'''
uci() {
  printf 'UCI %s\n' "$*"
  case "$*" in
    "add ksmbd share") printf 'cfg123' ;;
    "-q show ksmbd") printf 'ksmbd.cfg123=share\n' ;;
    "-q get ksmbd.cfg123.name") printf 'geomask-backups' ;;
    *) return 0 ;;
  esac
}
ksmbd.adduser() { printf 'ADDUSER %s\n' "$*"; return 0; }
'''
        result = self.smb(extra=extra)
        self.assertIn('guest_ok=no', result.stdout)
        self.assertIn('read_only=yes', result.stdout)
        self.assertNotIn('guest_ok=yes', result.stdout)
        self.assertNotIn('ADDUSER', result.stdout)
        self.assertNotIn('backup -p backup', result.stdout)

    def test_a_duplicate_of_our_share_is_removed_and_a_foreign_share_kept(self):
        """The dedupe loop must re-read uci: a cached @share[N] would hit the wrong section."""
        marker = self.case / 'etc/geomask/backup-share.enable'
        marker.parent.mkdir(parents=True)
        marker.write_text('', encoding='utf-8')
        sections = self.case / 'sections'
        sections.write_text('geomask-backups|/root/geomask-backups\n'
                            'photos|/srv/photos\n'
                            'geomask-backups|/root/geomask-backups\n', encoding='utf-8')
        extra = r'''
SECTIONS='@SECT@'
uci() {
  case "$*" in
    "-q show ksmbd")
      _i=0
      while IFS='|' read -r _n _p; do printf 'ksmbd.@share[%s]=share\n' "$_i"; _i=$((_i+1)); done < "$SECTIONS"
      ;;
    "-q get ksmbd.@share["*"].name")
      _i=$(printf '%s' "$*" | sed 's/.*\[\([0-9]*\)\].*/\1/')
      sed -n "$((_i+1))p" "$SECTIONS" | cut -d'|' -f1
      ;;
    "-q get ksmbd.@share["*"].path")
      _i=$(printf '%s' "$*" | sed 's/.*\[\([0-9]*\)\].*/\1/')
      sed -n "$((_i+1))p" "$SECTIONS" | cut -d'|' -f2
      ;;
    "-q get ksmbd.@share["*"].read_only"|"-q get ksmbd.@share["*"].guest_ok") printf 'yes' ;;
    "-q get ksmbd.@share["*"].force_root") printf '1' ;;
    "delete "*)
      printf 'DELETE %s\n' "$2"
      _i=$(printf '%s' "$*" | sed 's/.*\[\([0-9]*\)\].*/\1/')
      sed -i "$((_i+1))d" "$SECTIONS"
      ;;
    *) return 0 ;;
  esac
}
'''.replace('@SECT@', posix(sections))
        result = self.smb(extra=extra)
        self.assertIn('DELETE ksmbd.@share[2]', result.stdout)
        self.assertNotIn('DELETE ksmbd.@share[0]', result.stdout)
        left = sections.read_text(encoding='utf-8').splitlines()
        self.assertEqual(left, ['geomask-backups|/root/geomask-backups', 'photos|/srv/photos'])
        self.assertIn('RC=0', result.stdout)

    def test_removing_a_duplicate_never_repoints_a_foreign_share(self):
        """A saved @share[N] must not survive a delete: uci renumbers anonymous sections.

        The old code remembered `@share[1]` before deleting the unnamed `@share[0]`, then
        wrote our path and force_root into what had become the owner's `photos` share.
        """
        marker = self.case / 'etc/geomask/backup-share.enable'
        marker.parent.mkdir(parents=True)
        marker.write_text('', encoding='utf-8')
        extra = r'''
uci() {
  case "$*" in
    "-q show ksmbd") printf 'ksmbd.@share[0]=share\nksmbd.@share[1]=share\nksmbd.@share[2]=share\n' ;;
    "-q get ksmbd.@share[0].name") return 0 ;;
    "-q get ksmbd.@share[1].name") printf 'geomask-backups' ;;
    "-q get ksmbd.@share[2].name") printf 'photos' ;;
    "-q get ksmbd.@share[1].path") printf '/root/geomask-backups' ;;
    "-q get ksmbd.@share[1].read_only") printf 'yes' ;;
    "-q get ksmbd.@share[1].guest_ok") printf 'no' ;;
    "-q get ksmbd.@share[1].force_root") printf '1' ;;
    "-q get ksmbd.@share[2].path") printf '/srv/photos' ;;
    "delete "*) printf 'DELETE %s\n' "$*" ;;
    *) return 0 ;;
  esac
}
'''
        result = self.smb(extra=extra)
        # no duplicate of OUR name exists here, so nothing may be deleted or rewritten
        self.assertNotIn('DELETE', result.stdout)
        self.assertNotIn('ksmbd.@share[2].path', result.stdout)
        self.assertNotIn('photos', result.stdout)
        self.assertIn('RC=0', result.stdout)

    def test_the_share_restarts_the_file_server_inside_the_prefix(self):
        """A bare /etc/init.d/<svc> restart reaches the host, not the tree under test.

        Review 15.11, point 1: the template started the real init script of whatever machine ran
        the tests. The recording init script under `P /etc/init.d` proves which path was used.
        """
        calls = self.case / 'initd-calls'
        initd = self.case / 'etc/init.d/ksmbd'
        initd.parent.mkdir(parents=True, exist_ok=True)
        initd.write_text('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\n' % calls.as_posix(),
                         encoding='utf-8', newline='\n')
        initd.chmod(0o755)
        conf = self.case / 'etc/ksmbd/ksmbd.conf'
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text('[geomask-backups]\n', encoding='utf-8')
        marker = self.case / 'etc/geomask/backup-share.enable'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('', encoding='utf-8')
        # force_root is empty in the found section, so the helper has to change it - and a change
        # is what makes it restart the file server.
        extra = r'''
uci() {
  case "$*" in
    "-q show ksmbd") printf 'ksmbd.cfg1=share\n' ;;
    "-q get ksmbd.cfg1.name") printf 'geomask-backups' ;;
    "-q get ksmbd.cfg1.path") printf '/root/geomask-backups' ;;
    "-q get ksmbd.cfg1.read_only") printf 'yes' ;;
    "-q get ksmbd.cfg1.guest_ok") printf 'no' ;;
    "-q get ksmbd.cfg1.force_root") printf '' ;;
    "set "*) printf 'SET %s\n' "$2" ;;
    *) return 0 ;;
  esac
}
'''
        result = self.run_script('ensure_smb_share; printf "\\nRC=%s\\n" "$?"',
                                 functions=self.SMB_FUNCS, extra=extra)
        self.assertIn('SET ksmbd.cfg1.force_root=1', result.stdout)
        self.assertTrue(calls.exists(), 'the file server was never restarted from the prefix')
        self.assertIn('restart', calls.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
