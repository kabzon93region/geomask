"""R03 phase 2c: regressions for the holes found by the full independent review.

Each test reproduces one concrete defect of the phase-2b rollback, on the same throwaway
fixture harness as tests/test_reinstall_phase2b.py (no live paths, injected commands only):

  * ksmbd: an EXISTING managed share with force_root must come back unchanged; an absent
    ksmbd package must not make the rollback fail with `uci commit`;
  * GM_AUTO_LANE is refused before mutation even when the lane script is not installed yet
    (the old check looked at `-x` before step2 writes it - a documented bypass);
  * zapret-force: a managed line that sync returned to the active list must be re-commented;
  * symlink substitution: a directory object (or a predictable rebuild temp) replaced by a
    symlink must never be followed;
  * a snapshot without the shared preimage is refused, not silently "restored";
  * an absent shared file is a valid state (zapret not installed), not a permanent failure;
  * the ip rule check is exact (pref/mark/table), not just "some rule with the mark exists";
  * the nft table/chain content is compared, not only its presence;
  * transaction temp names (.bak_v6off_*, .gm.tmp) are pruned only when we created them.

The fixture harness sets the native-symlink environment itself (tests/win_symlink.py); when
the host cannot represent symlinks the class is skipped explicitly.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import win_symlink  # noqa: E402
import test_reinstall_phase2b as t  # noqa: E402


@unittest.skipUnless(win_symlink.NATIVE_SYMLINKS, win_symlink.SYMLINK_REASON)
class Phase2cTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        # composition, not inheritance: inheriting Phase2bTest would collect its 44 tests
        # again. The harness instance owns the fixture, the bash adapters and the snapshot.
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

    def run_rollback(self, body_mutation):
        body = self.snap() + '\n' + body_mutation + '\n' + self.restore()
        result = self.script(body)
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        return result

    # ---------------------------------------------------------------- helpers
    def ksmbd_shares(self):
        sections = self.fixture.uci('ksmbd') or []
        return [dict(options) for _name, kind, options in sections
                if kind == 'share' and dict(options).get('name') == 'geomask-backups']

    def write_ksmbd(self, extra_options):
        options = ("\toption name 'geomask-backups'\n"
                   "\toption path '/root/geomask-backups'\n"
                   "\toption read_only '1'\n"
                   "\toption guest_ok '0'\n") + extra_options
        (self.case / 'etc/config/ksmbd').write_text(
            "config globals\n\toption workgroup 'HOME'\n"
            "config share 'old'\n" + options, encoding='utf-8', newline='\n')

    # ---------------------------------------------------------------- 1. ksmbd
    def test_existing_ksmbd_share_with_force_root_survives(self):
        """A share that existed before the run must come back unchanged, force_root included."""
        self.write_ksmbd("\toption force_root '1'\n")
        before = self.ksmbd_shares()
        self.assertEqual(1, len(before))
        self.assertEqual('1', before[0].get('force_root'))
        body_mutation = ('uci set ksmbd.old.path=/tmp/other\n'
                         'uci set ksmbd.old.read_only=0\n'
                         'uci set ksmbd.old.guest_ok=1\n'
                         'uci commit ksmbd\n')
        result = self.run_rollback(body_mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        after = self.ksmbd_shares()
        self.assertEqual(before, after,
                         'the managed share was not restored exactly (force_root/path/...)')
        self.assertEqual('1', after[0].get('force_root'))
        self.assertFalse(self.snapshot_dir.exists(), 'verified rollback kept the snapshot')
        globals_section = [options for name, kind, options in self.fixture.uci('ksmbd')
                           if kind == 'globals'][0]
        self.assertEqual('HOME', dict(globals_section).get('workgroup'))

    def test_ksmbd_absent_package_rolls_back_cleanly(self):
        """No /etc/config/ksmbd: nothing to restore, so nothing may fail or be committed."""
        (self.case / 'etc/config/ksmbd').unlink()
        result = self.run_rollback('')
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertFalse((self.case / 'etc/config/ksmbd').exists())
        self.assertNotIn('uci commit ksmbd failed', self.logs())
        self.assertFalse(self.snapshot_dir.exists())

    # ---------------------------------------------------------------- 2. lane
    def test_auto_lane_is_refused_before_mutation_without_lane_script(self):
        """step2 installs lane later; the snapshot must refuse the lane branch regardless."""
        self.assertFalse((self.case / 'usr/bin/geomask-lane.sh').exists())
        result = self.script('GM_AUTO_LANE=1\n' + self.snap())
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertIn('GM_AUTO_LANE', self.logs())
        self.assertFalse((self.case / 'usr/bin/geomask-lane.sh').exists(),
                         'the refused snapshot still let the lane branch write files')

    # ---------------------------------------------------------------- 3. zapret-force
    def test_zapret_force_managed_line_returned_to_active_is_commented_again(self):
        target = self.case / 'opt/zapret/ipset/zapret-hosts-user.txt'
        before = ('kept.example\n'
                  '# geomask: tunneled, DPI removed tunneled.example\n')
        target.write_text(before, encoding='utf-8', newline='\n')
        body_mutation = ('printf "kept.example\\ntunneled.example\\n" > "%s"\n'
                         % t.posix(target))
        result = self.run_rollback(body_mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertEqual(before, target.read_text(encoding='utf-8'),
                         'a managed line returned to the active list was not reverted')

    def test_zapret_force_active_line_still_becomes_managed_again(self):
        target = self.case / 'opt/zapret/ipset/zapret-hosts-user.txt'
        before = 'kept.example\ntunneled.example\n'
        target.write_text(before, encoding='utf-8', newline='\n')
        body_mutation = ('sed -i "s/^tunneled.example$/# geomask: tunneled, DPI removed '
                         'tunneled.example/" "%s"\n' % t.posix(target))
        result = self.run_rollback(body_mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertEqual(before, target.read_text(encoding='utf-8'))

    # ---------------------------------------------------------------- 4. symlinks
    def test_directory_object_replaced_by_a_symlink_is_refused(self):
        evil = self.case / 'outside-target'
        evil.mkdir()
        body_mutation = ('rm -rf "%s"\n' % t.posix(self.case / 'etc/mihomo')
                         + 'ln -s "%s" "%s"\n'
                         % (t.posix(evil), t.posix(self.case / 'etc/mihomo')))
        result = self.run_rollback(body_mutation)
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertFalse((evil / 'config.yaml').exists(),
                         'the restore wrote through a symlinked directory')
        self.assertFalse((evil / 'ruleset').exists(),
                         'the restore created a directory through a symlink')

    def test_predictable_rebuild_temp_symlink_is_not_followed(self):
        outside = self.case / 'outside-passwd'
        outside.write_text('outside\n', encoding='utf-8', newline='\n')
        passwd = self.case / 'etc/passwd'
        original = passwd.read_text(encoding='utf-8')
        body_mutation = ('printf "dnsai:x:65533:65533::/:/bin/false\\n" >> "%s"\n'
                         % t.posix(passwd)
                         + 'ln -s "%s" "%s.rsnew"\n'
                         % (t.posix(outside), t.posix(passwd)))
        result = self.run_rollback(body_mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertEqual('outside\n', outside.read_text(encoding='utf-8'),
                         'the rebuild temp symlink was followed and wrote outside')
        self.assertFalse(passwd.is_symlink())
        self.assertEqual(original, passwd.read_text(encoding='utf-8'))

    # ---------------------------------------------------------------- 5. preimage
    def test_missing_shared_preimage_refuses_the_restore(self):
        passwd = self.case / 'etc/passwd'
        passwd.write_text(passwd.read_text(encoding='utf-8')
                          + 'dnsai:x:65533:65533::/:/bin/false\n', encoding='utf-8')
        body = (self.snap() + '\n'
                + 'rm -f "%s"/shared.*.pre\n' % t.posix(self.snapshot_dir)
                + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertIn('shared', self.logs())

    # ---------------------------------------------------------------- 6. absent shared
    def test_absent_shared_file_is_a_valid_state(self):
        (self.case / 'opt/zapret/ipset/zapret-hosts-user.txt').unlink()
        result = self.run_rollback('')
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertFalse((self.case / 'opt/zapret/ipset/zapret-hosts-user.txt').exists())
        self.assertFalse(self.snapshot_dir.exists())

    def test_absent_shared_file_that_appeared_is_incomplete(self):
        target = self.case / 'opt/zapret/ipset/zapret-hosts-user.txt'
        target.unlink()
        body_mutation = 'printf "new.example\\n" > "%s"\n' % t.posix(target)
        result = self.run_rollback(body_mutation)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    # ---------------------------------------------------------------- 7. ip rule exact
    def test_our_ip_rule_replaced_by_another_pref_is_a_conflict(self):
        rules = self.fixture.state / 'ip_rules'
        rules.write_text('19902:\tfrom all fwmark 0x64 lookup 101\n', encoding='utf-8')
        body_mutation = ('printf "20000:\\tfrom all fwmark 0x64 lookup 101\\n" > "%s"\n'
                         % t.posix(rules))
        result = self.run_rollback(body_mutation)
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    def test_our_ip_rule_added_after_snapshot_is_removed(self):
        body_mutation = 'ip rule add pref 19902 fwmark 0x64 table 101\n'
        result = self.run_rollback(body_mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertEqual('', (self.fixture.state / 'ip_rules').read_text(
            encoding='utf-8').strip())

    def test_foreign_rule_with_the_same_mark_is_not_touched(self):
        rules = self.fixture.state / 'ip_rules'
        rules.write_text('20000:\tfrom all fwmark 0x64 lookup 101\n', encoding='utf-8')
        result = self.run_rollback('')
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertIn('20000:', rules.read_text(encoding='utf-8'))

    # ---------------------------------------------------------------- 8. nft content
    def _write_ai_include(self, marker):
        include = self.case / 'usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft'
        include.write_text(
            'add table inet geomask_ai_dns\n'
            'flush table inet geomask_ai_dns\n'
            'add rule inet geomask_ai_dns out_mark meta skuid 65533 mark set 0x64 %s\n'
            % marker, encoding='utf-8', newline='\n')
        return include

    def test_nft_table_content_is_compared_not_only_presence(self):
        self._write_ai_include('# preimage')
        body = ('initd firewall reload\n' + self.snap() + '\n'
                + 'printf "add table inet geomask_ai_dns\\nadd rule inet geomask_ai_dns '
                  'out_mark meta skuid 65533 mark set 0x64 # changed\\n" > "%s"\n'
                  % t.posix(self.case / 'usr/share/nftables.d/ruleset-post/'
                            '98-geomask-ai-dns.nft')
                + 'initd firewall reload\n'
                + 'FIREWALL_RELOAD_NOOP=1\n'
                + self.restore())
        result = self.script(body)
        self.assertIncomplete(result)
        self.assertSnapshotKept()
        self.assertIn('nft', self.logs())

    def test_nft_table_content_matches_after_a_clean_rollback(self):
        include = self._write_ai_include('# preimage')
        body = ('initd firewall reload\n' + self.snap() + '\n'
                + 'printf "add table inet geomask_ai_dns\\nadd rule inet geomask_ai_dns '
                  'out_mark meta skuid 65533 mark set 0x64 # changed\\n" > "%s"\n'
                  % t.posix(include)
                + 'initd firewall reload\n' + self.restore())
        result = self.script(body)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertFalse(self.snapshot_dir.exists())

    # ---------------------------------------------------------------- 9. temp pruning
    def test_backup_network_temp_created_by_the_run_is_pruned(self):
        bak = self.case / 'etc/config/network.bak_v6off_1700000000'
        body_mutation = 'printf "x\\n" > "%s"\n' % t.posix(bak)
        result = self.run_rollback(body_mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertFalse(bak.exists(), 'our .bak_v6off_* backup survived the rollback')

    def test_preexisting_backup_network_temp_is_kept(self):
        bak = self.case / 'etc/config/network.bak_v6off_1600000000'
        bak.write_text('owner\n', encoding='utf-8', newline='\n')
        result = self.run_rollback('')
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertEqual('owner\n', bak.read_text(encoding='utf-8'))

    def test_seed_lists_temp_is_pruned(self):
        tmp = self.case / 'etc/magitrickle/state/config.yaml.gm.tmp'
        body_mutation = 'printf "half\\n" > "%s"\n' % t.posix(tmp)
        result = self.run_rollback(body_mutation)
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertFalse(tmp.exists(), 'the seeder temp file survived the rollback')


if __name__ == '__main__':
    unittest.main()
