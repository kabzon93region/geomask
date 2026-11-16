"""R03 phase 2d: systemic path safety + runtime precision (round-2 review).

Blockers reproduced here:
  1. /etc/rc.d replaced by a symlink: restore wrote autostart links into the foreign
     directory and returned a "verified" rc=0.
  2. /etc/init.d (and /etc/config) symlink: rollback EXECUTED the foreign init script /
     reloaded through the foreign config, even though the file phase already saw the
     symlink. The rule is systemic: never read or execute through a symlink component, and
     stop BEFORE external actions.
  3. ip rule: `lookup`/table was not part of the recorded state, so pref+mark with a wrong
     table verified as success.

Must-fix false incompletes:
  4. nft live counters made a clean rollback look incomplete (exact text compare).
  5. network.lanai option recorded as <absent> was never deleted.

Gate:
  6. service/UCI cross-checks compared IDs only; pid/initd/observe and per-key UCI rows were
     invisible. The mutation tests below mutate the product text itself.

Same throwaway fixture as phase2b/2c; the harness sets the native-symlink environment and
skips explicitly when the host cannot represent symlinks.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import win_symlink  # noqa: E402
import test_reinstall_phase2b as t  # noqa: E402


REAL_INITD = 'initd() { "$(P /etc/init.d/$1)" "$2" 2>/dev/null; }\n'


@unittest.skipUnless(win_symlink.NATIVE_SYMLINKS, win_symlink.SYMLINK_REASON)
class Phase2dTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.h = t.Phase2bTest('test_snapshot_records_absent_objects')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    # ---------------------------------------------------------------- shortcuts
    @property
    def case(self):
        return self.h.case

    @property
    def fixture(self):
        return self.h.fixture

    @property
    def snapshot_dir(self):
        return self.h.snapshot_dir

    def script(self, body, extra=''):
        return self.h.script(body, extra=extra)

    def snap(self):
        return self.h.snap()

    def restore(self):
        return self.h.restore()

    def mutation(self, boundary):
        return self.h.mutation(boundary)

    def rc_of(self, result, marker):
        return self.h.rc_of(result, marker)

    def logs(self):
        return self.h.logs()

    def assertIncomplete(self, result):
        self.h.assertIncomplete(result)

    def assertSnapshotKept(self):
        self.h.assertSnapshotKept()

    # ---------------------------------------------------------------- 1. rc.d
    def test_rc_d_symlink_is_refused_and_writes_nothing_outside(self):
        outside = self.case.parent / 'rc-outside'
        outside.mkdir()
        body_mutation = ('rm -rf "%s"\n' % t.posix(self.case / 'etc/rc.d')
                         + 'ln -s "%s" "%s"\n'
                         % (t.posix(outside), t.posix(self.case / 'etc/rc.d')))
        result = self.script(self.snap() + '\n' + body_mutation + '\n' + self.restore())
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertEqual([], sorted(entry.name for entry in outside.iterdir()),
                         'the restore created rc.d links through a symlink')

    def test_rc_d_symlink_is_refused_by_the_snapshot(self):
        outside = self.case.parent / 'rc-outside'
        outside.mkdir()
        (self.case / 'etc/rc.d').rename(self.case / 'etc/rc.d.real')
        os.symlink(str(outside), self.case / 'etc/rc.d')
        result = self.script(self.snap())
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        self.assertIn('symlink', self.logs())

    # ---------------------------------------------------------------- 2. exec/read
    def test_symlinked_init_d_is_never_executed(self):
        outside = self.case.parent / 'init-outside'
        outside.mkdir()
        marker = outside / 'EXECUTED'
        script = outside / 'network'
        script.write_text('#!/bin/sh\nprintf "executed\\n" > "%s"\n' % t.posix(marker),
                          encoding='utf-8', newline='\n')
        script.chmod(0o755)
        body_mutation = ('rm -rf "%s"\n' % t.posix(self.case / 'etc/init.d')
                         + 'ln -s "%s" "%s"\n'
                         % (t.posix(outside), t.posix(self.case / 'etc/init.d')))
        result = self.script(self.snap() + '\n' + body_mutation + '\n' + self.restore(),
                             extra=REAL_INITD)
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertFalse(marker.exists(),
                         'rollback executed a script through a symlinked /etc/init.d')

    def test_symlinked_etc_config_is_not_read_by_reload(self):
        outside = self.case.parent / 'config-outside'
        outside.mkdir()
        marker = self.case / 'reload-marker'
        (outside / 'network').write_text("config interface 'lan'\n", encoding='utf-8')
        body_mutation = ('rm -rf "%s"\n' % t.posix(self.case / 'etc/config')
                         + 'ln -s "%s" "%s"\n'
                         % (t.posix(outside), t.posix(self.case / 'etc/config')))
        # the dispatcher only records the reload: does the rollback still run external
        # actions after /etc/config turned into a symlink?
        extra = ('initd() { printf "%%s %%s\\n" "$1" "$2" >> "%s"; }\n'
                 % t.posix(marker))
        result = self.script(self.snap() + '\n' + body_mutation + '\n' + self.restore(),
                             extra=extra)
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        executed = marker.read_text(encoding='utf-8') if marker.exists() else ''
        self.assertNotIn('network reload', executed,
                         'rollback ran an external reload through a symlinked /etc/config')

    def test_symlinked_init_d_is_refused_by_the_snapshot(self):
        outside = self.case.parent / 'init-outside'
        outside.mkdir()
        (self.case / 'etc/init.d').rename(self.case / 'etc/init.d.real')
        os.symlink(str(outside), self.case / 'etc/init.d')
        result = self.script(self.snap())
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        self.assertIn('symlink', self.logs())

    # ---------------------------------------------------------------- 3. ip rule table
    def test_ip_rule_table_is_part_of_the_state(self):
        rules = self.fixture.state / 'ip_rules'
        rules.write_text('19902:\tfrom all fwmark 0x64 lookup 101\n', encoding='utf-8')
        body_mutation = ('printf "19902:\\tfrom all fwmark 0x64 lookup 999\\n" > "%s"\n'
                         % t.posix(rules))
        result = self.script(self.snap() + '\n' + body_mutation + '\n' + self.restore())
        # Phase 2e: pref 19902 identifies OUR rule, so a changed lookup is repaired from
        # the snapshot instead of being reported as a foreign conflict.
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertIn('lookup 101', rules.read_text(encoding='utf-8'))
        self.assertFalse(self.snapshot_dir.exists())

    # ---------------------------------------------------------------- 4. nft counters
    def test_nft_live_counters_do_not_fake_incomplete(self):
        state = self.fixture.state
        content = ('table inet geomask_ai_dns {\n'
                   '\tchain pre_nat {\n'
                   '\t\tcounter packets 5 bytes 300 reject\n'
                   '\t}\n'
                   '}\n')
        (state / 'nft_tables').write_text('inet geomask_ai_dns\n', encoding='utf-8')
        (state / 'nft_ai_content').write_text(content, encoding='utf-8', newline='\n')
        template = self.case / 'nft-template.txt'
        template.write_text(content, encoding='utf-8', newline='\n')
        include = self.case / 'usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft'
        include.write_text('add table inet geomask_ai_dns\n', encoding='utf-8', newline='\n')
        body = (self.snap() + '\n'
                + 'NFT_COUNTERS_BUMP=1\nNFT_RELOAD_CONTENT="%s"\n' % t.posix(template)
                + self.restore())
        result = self.script(body)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertFalse(self.snapshot_dir.exists())

    def test_nft_structure_change_is_still_incomplete(self):
        state = self.fixture.state
        (state / 'nft_tables').write_text('inet geomask_ai_dns\n', encoding='utf-8')
        (state / 'nft_ai_content').write_text('table inet geomask_ai_dns {\n}\n',
                                              encoding='utf-8', newline='\n')
        template = self.case / 'nft-template.txt'
        template.write_text('table inet geomask_ai_dns {\n\tchain other {\n\t}\n}\n',
                            encoding='utf-8', newline='\n')
        include = self.case / 'usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft'
        include.write_text('add table inet geomask_ai_dns\n', encoding='utf-8', newline='\n')
        body = (self.snap() + '\n'
                + 'NFT_RELOAD_CONTENT="%s"\n' % t.posix(template)
                + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    # ---------------------------------------------------------------- 5. lanai absent option
    def test_lanai_option_absent_in_preimage_is_deleted(self):
        net = self.case / 'etc/config/network'
        net.write_text(
            "config interface 'lan'\n"
            "\toption device 'br-lan'\n"
            "\toption proto 'static'\n"
            "\toption ipaddr '192.168.1.1'\n"
            "\toption netmask '255.255.255.0'\n"
            "\toption ip6assign '60'\n"
            "config interface 'lanai'\n"
            "\toption proto 'static'\n"
            "\toption device 'br-lan'\n"
            "\toption ipaddr '192.168.1.254'\n", encoding='utf-8', newline='\n')
        result = self.script(self.snap() + '\n'
                             + 'uci set network.lanai.netmask=255.255.255.0\n'
                             + 'uci commit network\n' + self.restore())
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        text = net.read_text(encoding='utf-8')
        lanai = text.split("config interface 'lanai'", 1)[1]
        self.assertNotIn('netmask', lanai,
                         'a lanai option recorded as <absent> was not deleted')


    # ------------------------------------------------ 4b. nft representation
    def _nft_probe(self, include_body):
        state = self.fixture.state
        (state / 'nft_tables').write_text('inet geomask_ai_dns\n', encoding='utf-8')
        (state / 'nft_ai_content').write_text(
            'table inet geomask_ai_dns {\n'
            '\tchain pre_nat {\n'
            '\t\tcounter packets 5 bytes 100 reject\n'
            '\t}\n'
            '}\n', encoding='utf-8', newline='\n')
        include = self.case / 'usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft'
        include.write_text(include_body, encoding='utf-8', newline='\n')
        return self.script(self.snap() + '\n' + self.restore())

    def test_nft_list_and_script_formats_are_equivalent(self):
        """The live side may be script-form (add/flush/add rule): same semantics."""
        result = self._nft_probe(
            'add table inet geomask_ai_dns\n'
            'flush table inet geomask_ai_dns\n'
            'add rule inet geomask_ai_dns pre_nat counter reject\n')
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertFalse(self.snapshot_dir.exists())

    def test_nft_script_structure_change_is_still_incomplete(self):
        """A different rule expression is a real structure change, not formatting."""
        result = self._nft_probe(
            'add table inet geomask_ai_dns\n'
            'flush table inet geomask_ai_dns\n'
            'add rule inet geomask_ai_dns pre_nat counter drop\n')
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertIn('structure differs', self.logs())


    def _nft_realistic(self, priority):
        state = self.fixture.state
        listing = (
            'table inet geomask_ai_dns {\n'
            '\tchain pre_nat {\n'
            '\t\ttype nat hook prerouting priority -110; policy accept;\n'
            '\t\tip saddr { 192.168.1.10, 192.168.1.11 } meta l4proto { tcp, udp } '
            'th dport 53 counter packets 5 bytes 100 dnat ip to 192.168.1.254:5353\n'
            '\t}\n'
            '\tchain out_mark {\n'
            '\t\ttype route hook output priority -150; policy accept;\n'
            '\t\tmeta skuid 65533 mark set 0x64\n'
            '\t}\n'
            '}\n')
        (state / 'nft_tables').write_text('inet geomask_ai_dns\n', encoding='utf-8')
        (state / 'nft_ai_content').write_text(listing, encoding='utf-8', newline='\n')
        include = self.case / 'usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft'
        include.write_text(
            'add table inet geomask_ai_dns\n'
            'flush table inet geomask_ai_dns\n'
            'add chain inet geomask_ai_dns pre_nat { type nat hook prerouting priority %s; '
            'policy accept; }\n'
            'add chain inet geomask_ai_dns out_mark { type route hook output priority -150; '
            'policy accept; }\n'
            'add rule inet geomask_ai_dns out_mark meta skuid 65533 mark set 0x64\n'
            'add rule inet geomask_ai_dns pre_nat ip saddr { 192.168.1.10, 192.168.1.11 } '
            'meta l4proto { tcp, udp } th dport 53 counter packets 0 bytes 0 '
            'dnat ip to 192.168.1.254:5353\n' % priority,
            encoding='utf-8', newline='\n')
        return self.script(self.snap() + '\n' + self.restore())

    def test_nft_realistic_table_is_equal_across_representations(self):
        result = self._nft_realistic('-110')
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())

    def test_nft_chain_priority_change_is_still_incomplete(self):
        result = self._nft_realistic('-100')
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertIn('structure differs', self.logs())


if __name__ == '__main__':
    unittest.main()
