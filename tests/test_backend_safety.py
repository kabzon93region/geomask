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
            'AIDNS_UID_FILE="%s"\nAIDNS_OFF_FILE="%s"\nAIDNS_STATE="%s"\nNODES_LOCK="%s"\n'
            'BACKUP_OK="%s"\n'
            'GM_LOCK_DIR="%s"\nGM_LOCK_LIB="%s"\nDEVICES_LOCK="%s"\n'
            '. "$GM_LOCK_LIB"\n'
            'warn() { printf "WARN %%s\\n" "$*" >&2; }\n'
            % (posix(self.devices), posix(self.log), posix(self.case / 'subs.list'),
               posix(self.owned), posix(self.backups), posix(self.nodes),
               posix(self.case / 'config.yaml'), posix(self.case / 'geomask-aidns.uid'),
               posix(self.case / 'geomask-aidns.off'),
               posix(self.case / 'geomask-aidns.state'),
               posix(self.case / 'nodes.lock'),
               posix(self.case / 'backup-ok'),
               posix(self.case / 'locks'), posix(ROOT / 'assets' / 'geomask-lock.sh'),
               posix(self.case / 'devices.lock')))
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
                                             '_write_gate', '_devices_lock', '_devices_unlock',
                                             '_dhcp_owned_add'],
                                  extra=extra + '_apply_routing() { :; }\ncmd_status() { :; }\n'
                                                 'ip() { :; }\n')
        self.assertNotIn('DELETED', result.stdout)
        self.assertEqual(self.devices.read_text(encoding='utf-8'), '')

    def test_an_impossible_address_is_refused(self):
        result = self.run_backend('cmd_dev_add 999.1.1.1 ai "" test; printf "RC=%s\\n" "$?"',
                                  functions=['cmd_dev_add', '_ipv4_ok', '_require_backup',
                                             '_write_gate', '_devices_lock', '_devices_unlock',
                                             '_dhcp_owned_add'],
                                  extra='_apply_routing() { :; }\ncmd_status() { :; }\n')
        self.assertIn('RC=1', result.stdout)
        self.assertIn('bad ip', result.stdout)
        self.assertFalse(self.devices.exists() and self.devices.read_text(encoding='utf-8').strip())

    # ---------------------------------------------------------------- G03 dhcp lease
    def dhcp_fixture(self, set_fails=None, commit_fails=False):
        """Stateful uci stub with dhcp sections; `set_fails` is a substring that must fail."""
        db = self.case / 'uci-db2'
        db.mkdir(exist_ok=True)
        (db / 'dhcp_lan_ra').write_text('server', encoding='utf-8')
        calls = self.case / 'uci.calls'
        extra = r"""
uci() {
  printf '%s\n' "$*" >> '@CALLS@'
  _u_db='@DB@'
  [ "${1:-}" = "-q" ] && shift
  case "${1:-}" in
    show)
      printf "dhcp.lan.ra='server'\n"
      for _f in "$_u_db"/host_*_mac; do
        [ -f "$_f" ] || continue
        _s=$(basename "$_f" _mac)
        printf "dhcp.%s.mac='%s'\n" "$_s" "$(cat "$_f")"
        [ -f "$_u_db/${_s}_ip" ] && printf "dhcp.%s.ip='%s'\n" "$_s" "$(cat "$_u_db/${_s}_ip")"
      done
      return 0 ;;
    get)
      _k=${2#dhcp.}
      _s=${_k%%.*}; _o=${_k#*.}
      _f="$_u_db/${_s}_${_o}"
      [ -f "$_f" ] && { cat "$_f"; return 0; }
      return 1 ;;
    add)
      printf 'host-%s\n' "${RANDOM}${RANDOM}"
      return 0 ;;
    set)
      _assn=$2
      _key=${_assn%%=*}
      _val=${_assn#*=}
      _s=${_key%%.*}; _o=${_key#*.}
      case "$_val" in *'@FAIL@'*) return 1 ;; esac
      printf '%s' "$_val" > "$_u_db/${_s}_${_o}"
      return 0 ;;
    delete)
      rm -f "$_u_db/$(basename "${2:-x}" 2>/dev/null)_"* 2>/dev/null
      return 0 ;;
    commit)
      return @COMMIT@ ;;
  esac
  return 0
}
"""
        extra = (extra.replace('@CALLS@', posix(calls))
                 .replace('@DB@', posix(db))
                 .replace('@FAIL@', set_fails or 'NOPE-NOT-A-KEY')
                 .replace('@COMMIT@', '1' if commit_fails else '0'))
        return calls, db, extra

    def test_a_failed_lease_write_does_not_commit_or_claim_ownership(self):
        """G03: failed `uci set` calls were ignored, an empty host section was committed."""
        calls, db, extra = self.dhcp_fixture(set_fails='aa:bb:cc:dd:ee:ff')
        owned = self.case / 'dhcp-owned'
        extra += 'DHCP_OWNED="%s"\n' % posix(owned)
        result = self.run_backend(
            'cmd_dev_add 192.168.1.60 ai aa:bb:cc:dd:ee:ff tv; printf "\\nRC=%s\\n" "$?"',
            functions=['cmd_dev_add', '_ipv4_ok', '_require_backup', '_write_gate',
                       '_devices_lock', '_devices_unlock', '_dhcp_host_section',
                       '_dhcp_owned_add'],
            extra=extra + '_apply_routing() { :; }\ncmd_status() { :; }\n')
        output = result.stdout + result.stderr
        self.assertIn('RC=0', output, output)
        self.assertIn('static lease not created', output,
                      'a failed lease write was reported as done')
        self.assertFalse(owned.exists() and owned.read_text(encoding='utf-8').strip(),
                         'ownership was recorded for a lease that was never written')

    def test_an_existing_lease_for_the_mac_is_updated_in_place(self):
        """The old probe skipped a MAC that already had a lease with another IP."""
        calls, db, extra = self.dhcp_fixture()
        (db / 'host_abc_mac').write_text('aa:bb:cc:dd:ee:ff', encoding='utf-8')
        (db / 'host_abc_ip').write_text('192.168.1.10', encoding='utf-8')
        owned = self.case / 'dhcp-owned'
        extra += 'DHCP_OWNED="%s"\n' % posix(owned)
        result = self.run_backend(
            'cmd_dev_add 192.168.1.60 ai aa:bb:cc:dd:ee:ff tv; printf "\\nRC=%s\\n" "$?"',
            functions=['cmd_dev_add', '_ipv4_ok', '_require_backup', '_write_gate',
                       '_devices_lock', '_devices_unlock', '_dhcp_host_section',
                       '_dhcp_owned_add'],
            extra=extra + '_apply_routing() { :; }\ncmd_status() { :; }\n')
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr + '')
        self.assertEqual((db / 'host_abc_ip').read_text(encoding='utf-8'), '192.168.1.60',
                         'the existing lease kept the old address')
        calls_text = calls.read_text(encoding='utf-8')
        self.assertNotIn('add dhcp host', calls_text,
                         'a second host section was created for the same MAC')
        self.assertIn('192.168.1.60', owned.read_text(encoding='utf-8'))

    def test_a_mac_that_only_shares_a_prefix_is_not_ours(self):
        """`grep '.mac=<mac>'` matched a longer MAC; ownership must be exact."""
        calls, db, extra = self.dhcp_fixture()
        (db / 'host_other_mac').write_text('aa:bb:cc:dd:ee:ff:11', encoding='utf-8')
        owned = self.case / 'dhcp-owned'
        extra += 'DHCP_OWNED="%s"\n' % posix(owned)
        result = self.run_backend(
            'cmd_dev_add 192.168.1.60 ai aa:bb:cc:dd:ee:ff tv; printf "\\nRC=%s\\n" "$?"',
            functions=['cmd_dev_add', '_ipv4_ok', '_require_backup', '_write_gate',
                       '_devices_lock', '_devices_unlock', '_dhcp_host_section',
                       '_dhcp_owned_add'],
            extra=extra + '_apply_routing() { :; }\ncmd_status() { :; }\n')
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('add dhcp host', calls.read_text(encoding='utf-8'),
                      'a lookalike MAC was treated as our lease')

    # ---------------------------------------------------------------- AI-DNS
    def test_unknown_listen_address_fails_closed(self):
        dest = self.case / '98-geomask-ai-dns.nft'
        self.devices.write_text('192.168.1.50 ai tv\n', encoding='utf-8')
        (self.case / 'geomask-aidns.uid').write_text('65533\n', encoding='utf-8')
        fw = self.case / 'firewall-stub'
        fw.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        fw.chmod(0o755)
        # C04: reaching "rc=0" now also needs the reject rule to be live in the kernel.
        extra = ('nft() {\n'
                 '  case "$*" in\n'
                 '    *"list table inet geomask_ai_dns"*) printf "%s\\n" "ip saddr { 192.168.1.50 } counter reject" ;;\n'
                 '  esac\n'
                 '  return 0\n'
                 '}\n'
                 'AIDNS_NFT_DEST="' + posix(dest) + '"\n'
                 'AIDNS_DNS_PORT=5353\n'
                 'AIDNS_FW_INIT="' + posix(fw) + '"\n'
                 'AIDNS_PENDING="' + posix(self.case / 'aidns.pending') + '"\n'
                 '_gm_lan_ip() { printf "192.168.1.1"; }\n'
                 '_gm_aidns_ip() { printf ""; }\n')
        result = self.run_backend('_aidns_sync; printf "RC=%s\\n" "$?"',
                                  functions=self.AIDNS_FUNCS, extra=extra)
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)
        text = dest.read_text(encoding='utf-8')
        self.assertIn('counter reject', text)
        self.assertNotIn('dnat ip to', text)
        self.assertEqual((self.case / 'geomask-aidns.state').read_text(encoding='utf-8'), 'error')

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


    # ---------------------------------------------------------------- C04 reload proof
    def aidns_fixture(self, reload_ok, table_live):
        """A device list, a listen address and a firewall stub whose reload can fail."""
        dest = self.case / '98-geomask-ai-dns.nft'
        self.devices.write_text('192.168.1.50 ai tv\n', encoding='utf-8')
        (self.case / 'geomask-aidns.uid').write_text('65533\n', encoding='utf-8')
        fw = self.case / 'firewall-stub'
        fw.write_text('#!/bin/sh\nprintf "reload\\n" >> "%s/fw.calls"\nexit %d\n'
                      % (posix(self.case), 0 if reload_ok else 1),
                      encoding='utf-8', newline='\n')
        fw.chmod(0o755)
        table = 'dnat ip to 192.168.1.53:5353' if table_live else 'no such table'
        extra = ('nft() {\n'
                 '  case "$*" in\n'
                 '    *"list table inet geomask_ai_dns"*) printf "%s\\n" "' + table + '" ;;\n'
                 '  esac\n'
                 '  return 0\n'
                 '}\n'
                 'AIDNS_NFT_DEST="' + posix(dest) + '"\n'
                 'AIDNS_DNS_PORT=5353\n'
                 'AIDNS_FW_INIT="' + posix(fw) + '"\n'
                 'AIDNS_PENDING="' + posix(self.case / 'aidns.pending') + '"\n'
                 '_gm_lan_ip() { printf "192.168.1.1"; }\n'
                 '_gm_aidns_ip() { printf "192.168.1.53"; }\n')
        return dest, extra

    AIDNS_FUNCS = ['_aidns_sync', '_aidns_active', '_aidns_mark_pending',
                   '_aidns_clear_pending', '_aidns_reload_prove']

    def test_a_failed_reload_is_pending_and_not_reported_as_applied(self):
        """C04: the include was written, the reload failed, and the state still said ok."""
        dest, extra = self.aidns_fixture(reload_ok=False, table_live=False)
        result = self.run_backend('_aidns_sync; printf "RC=%s\\n" "$?"',
                                  functions=self.AIDNS_FUNCS, extra=extra)
        self.assertIn('RC=1', result.stdout,
                      'a write whose reload failed was reported as success')
        self.assertIn('dnat ip to 192.168.1.53:5353', dest.read_text(encoding='utf-8'))
        state = (self.case / 'geomask-aidns.state').read_text(encoding='utf-8')
        self.assertEqual(state, 'pending')
        marker = (self.case / 'aidns.pending').read_text(encoding='utf-8')
        self.assertIn('attempts=1', marker)

    def test_a_reload_that_does_not_load_the_table_is_still_pending(self):
        """The reload exit code is not proof: the table must be in the kernel."""
        dest, extra = self.aidns_fixture(reload_ok=True, table_live=False)
        result = self.run_backend('_aidns_sync; printf "RC=%s\\n" "$?"',
                                  functions=self.AIDNS_FUNCS, extra=extra)
        self.assertIn('RC=1', result.stdout,
                      'a reload that left the table absent was reported as applied')
        self.assertEqual((self.case / 'geomask-aidns.state').read_text(encoding='utf-8'),
                         'pending')

    def test_an_identical_run_retries_a_pending_reload(self):
        """The counterexample: same bytes, runtime stale - the next run must retry."""
        dest, extra = self.aidns_fixture(reload_ok=False, table_live=False)
        self.run_backend('_aidns_sync', functions=self.AIDNS_FUNCS, extra=extra)
        self.assertTrue((self.case / 'aidns.pending').exists())
        dest, extra_ok = self.aidns_fixture(reload_ok=True, table_live=True)
        result = self.run_backend('_aidns_sync; printf "RC=%s\\n" "$?"',
                                  functions=self.AIDNS_FUNCS, extra=extra_ok)
        self.assertIn('RC=0', result.stdout,
                      'an unchanged run did not retry the pending reload: ' + result.stdout)
        self.assertEqual((self.case / 'geomask-aidns.state').read_text(encoding='utf-8'), 'ok')
        self.assertFalse((self.case / 'aidns.pending').exists(),
                         'a successful retry left the pending marker behind')

    def test_an_unchanged_healthy_table_is_not_reloaded_again(self):
        dest, extra = self.aidns_fixture(reload_ok=True, table_live=True)
        self.run_backend('_aidns_sync', functions=self.AIDNS_FUNCS, extra=extra)
        calls = self.case / 'fw.calls'
        before = calls.read_text(encoding='utf-8') if calls.exists() else ''
        result = self.run_backend('_aidns_sync; printf "RC=%s\\n" "$?"',
                                  functions=self.AIDNS_FUNCS, extra=extra)
        after = calls.read_text(encoding='utf-8') if calls.exists() else ''
        self.assertIn('RC=0', result.stdout)
        self.assertEqual(before, after, 'a healthy unchanged table caused another reload')

    def test_the_rpc_answers_pending_instead_of_ok(self):
        dest, extra = self.aidns_fixture(reload_ok=False, table_live=False)
        extra += 'sha256sum() { printf "aaa  x\\n"; }\n'
        result = self.run_backend('cmd_aidns_apply; printf "\\nRC=%s\\n" "$?"',
                                  functions=['cmd_aidns_apply'] + self.AIDNS_FUNCS,
                                  extra=extra)
        self.assertIn('RC=1', result.stdout)
        self.assertIn('"state":"pending"', result.stdout)
        self.assertNotIn('"ok":true', result.stdout)

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
        # A stateful rule table: the kill switch must be able to read it and to see the rule
        # disappear, otherwise the fixture would only prove that an unreadable table fails.
        rules = self.case / 'rules'
        rules.write_text('19903:\tfrom 192.0.2.50 lookup 102\n', encoding='utf-8')
        return ('CONSOLE_STATE="%s"\nCONSOLE_INITD="%s"\nCONSOLE_TABLE=102\n'
                'CONSOLE_TUN_DEV=Console\nCONSOLE_RULE_PREF=19903\n'
                'CONSOLE_SOCKS_PORT=7893\nCONSOLE_TUN_IP=198.21.0.1\n'
                'CONSOLE_LOCK="%s"\nCONSOLE_LOCK_TRIES=1\nTMPDIR="%s"\n'
                'CONSOLE_PY=/nonexistent/geomask-console.py\n'
                '_require_backup() { return 0; }\n'
                'ip() {\n'
                '  printf "IP %%s\\n" "$*" >> "%s"\n'
                '  case "$1 $2" in\n'
                '    "rule show") cat "%s" 2>/dev/null; return 0 ;;\n'
                '    "rule del")\n'
                '      shift 2\n'
                '      _d_from=""; _d_tbl=""\n'
                '      while [ $# -gt 0 ]; do\n'
                '        case "$1" in from) _d_from=$2; shift 2 ;; lookup) _d_tbl=$2; shift 2 ;; *) shift ;; esac\n'
                '      done\n'
                '      grep -vE "from ${_d_from} lookup ${_d_tbl}$" "%s" > "%s.new" 2>/dev/null\n'
                '      mv -f "%s.new" "%s"; return 0 ;;\n'
                '  esac\n'
                '  return 1\n'
                '}\n'
                'conntrack() { printf "CT %%s\\n" "$*" >> "%s"; }\n'
                % (posix(self.case / 'console.state'), posix(initd),
                   posix(self.case / 'console.lock'), posix(self.case),
                   posix(self.case / 'ip-calls'), posix(rules),
                   posix(rules), posix(rules), posix(rules), posix(rules),
                   posix(self.case / 'ct-calls')))

    def test_console_off_is_a_real_kill_switch(self):
        (self.case / 'console.state').write_text('enabled=1\nip=192.0.2.50\n', encoding='utf-8')
        result = self.run_backend(
            'cmd_console off 192.0.2.50; printf "\\nRC=%s\\n" "$?"',
            functions=['_console_get', '_console_set', '_console_own_rules', '_console_rule_off',
                       '_console_lock', '_console_unlock', 'cmd_console'],
            extra=self.console_extra())
        self.assertIn('"enabled":false', result.stdout)
        self.assertIn('RC=0', result.stdout)
        state = (self.case / 'console.state').read_text(encoding='utf-8')
        self.assertIn('enabled=0', state)
        self.assertIn('ip=192.0.2.50', state)          # remembered for the next switch-on
        ip_calls = (self.case / 'ip-calls').read_text(encoding='utf-8')
        self.assertIn('rule del pref 19903 from 192.0.2.50 lookup 102', ip_calls)
        initd = (self.case / 'initd-calls').read_text(encoding='utf-8')
        self.assertIn('stop', initd)
        self.assertIn('disable', initd)
        self.assertFalse((self.case / 'console.lock').exists(), 'the kill switch kept the lock')

    def test_console_on_refuses_a_bad_address_before_touching_anything(self):
        before = 'enabled=1\nip=192.0.2.50\n'
        (self.case / 'console.state').write_text(before, encoding='utf-8')
        result = self.run_backend(
            'cmd_console on 999.1.1.1; printf "\\nRC=%s\\n" "$?"',
            functions=['_console_get', '_console_set', '_ipv4_ok',
                       '_console_lock', '_console_unlock', 'cmd_console'],
            extra=self.console_extra())
        self.assertIn('bad or missing console IP', result.stdout)
        self.assertIn('RC=1', result.stdout)
        self.assertEqual((self.case / 'console.state').read_text(encoding='utf-8'), before)
        self.assertFalse((self.case / 'console.lock').exists(), 'a refused switch kept the lock')

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


if __name__ == '__main__':
    unittest.main()
