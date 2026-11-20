"""R03 phase 2e: multiple ip rules with our mark, symlink policy, restore var.

RED first: every test below fails on the phase-2d/fix2 product for the stated reason.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import win_symlink  # noqa: E402
import test_reinstall_phase2b as t  # noqa: E402

SYMLINK_LAYOUT_OK, SYMLINK_LAYOUT_REASON = win_symlink.layout_status()
requires_symlink_layout = unittest.skipUnless(SYMLINK_LAYOUT_OK,
                                              SYMLINK_LAYOUT_REASON)


@unittest.skipUnless(win_symlink.NATIVE_SYMLINKS, win_symlink.SYMLINK_REASON)
class Phase2eTest(unittest.TestCase):

    def setUp(self):
        self.h = t.Phase2bTest('test_snapshot_records_absent_objects')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

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

    def rc_of(self, result, marker):
        return self.h.rc_of(result, marker)

    def logs(self):
        return self.h.logs()

    def assertIncomplete(self, result):
        self.h.assertIncomplete(result)

    def assertSnapshotKept(self):
        self.h.assertSnapshotKept()

    # ------------------------------------------------------------ B1: ip rule set
    def _rules(self):
        return self.fixture.state / 'ip_rules'

    def test_multiple_same_mark_rules_restore_ours(self):
        self._rules().write_text(
            '10000:\tfrom all fwmark 0x64 lookup 101\n'
            '19902:\tfrom all fwmark 0x64 lookup 101\n', encoding='utf-8')
        result = self.script(
            self.snap() + '\n'
            + 'grep -v "^19902:" "%s" > "%s.new"\n' % (t.posix(self._rules()),
                                                        t.posix(self._rules()))
            + 'mv -f "%s.new" "%s"\n' % (t.posix(self._rules()), t.posix(self._rules()))
            + self.restore())
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        after = self._rules().read_text(encoding='utf-8')
        self.assertIn('19902', after, 'our rule was not restored')
        self.assertIn('10000', after, 'a foreign rule was touched')

    def test_rule_added_by_the_run_is_removed_again(self):
        self._rules().write_text(
            '10000:\tfrom all fwmark 0x64 lookup 101\n', encoding='utf-8')
        result = self.script(
            self.snap() + '\n'
            + 'printf "19902:\\tfrom all fwmark 0x64 lookup 101\\n" >> "%s"\n'
              % t.posix(self._rules())
            + self.restore())
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        after = self._rules().read_text(encoding='utf-8')
        self.assertNotIn('19902', after, 'our rule added by the run was not removed')
        self.assertIn('10000', after, 'a foreign rule was touched')

    def test_foreign_same_mark_change_is_incomplete(self):
        self._rules().write_text(
            '10000:\tfrom all fwmark 0x64 lookup 101\n'
            '19902:\tfrom all fwmark 0x64 lookup 101\n', encoding='utf-8')
        result = self.script(
            self.snap() + '\n'
            + 'printf "10000:\\tfrom all fwmark 0x64 lookup 999\\n" > "%s.new"\n'
              % t.posix(self._rules())
            + 'grep "^19902:" "%s" >> "%s.new"\n' % (t.posix(self._rules()),
                                                       t.posix(self._rules()))
            + 'mv -f "%s.new" "%s"\n' % (t.posix(self._rules()), t.posix(self._rules()))
            + self.restore())
        self.assertIncomplete(result)
        self.assertSnapshotKept()

    # ------------------------------------------------------------ M1: symlink policy
    @requires_symlink_layout
    def test_extroot_opt_symlink_is_accepted(self):
        mnt = self.case / 'mnt'
        mnt.mkdir()
        (self.case / 'opt').rename(mnt / 'opt')
        (self.case / 'opt').symlink_to('mnt/opt', target_is_directory=True)
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'),
                         result.stdout + result.stderr + self.logs())
        restore = self.script(self.restore())
        self.assertEqual(0, self.rc_of(restore, 'RESTORE_RC'),
                         restore.stdout + restore.stderr + self.logs())

    @requires_symlink_layout
    def test_symlink_target_swap_between_snapshot_and_restore_is_incomplete(self):
        mnt = self.case / 'mnt'
        mnt.mkdir()
        (self.case / 'opt').rename(mnt / 'opt')
        (self.case / 'opt').symlink_to('mnt/opt', target_is_directory=True)
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'),
                         result.stdout + result.stderr + self.logs())
        (mnt / 'opt').rename(mnt / 'opt-old')
        (mnt / 'opt').mkdir()
        (self.case / 'opt').unlink()
        (self.case / 'opt').symlink_to('mnt/opt', target_is_directory=True)
        restore = self.script(self.restore())
        self.assertIncomplete(restore)
        self.assertSnapshotKept()

    # ------------------------------------------------------------ M3: initd guard itself
    def _initd_extra(self):
        return 'warn() { printf "WARN %s\\n" "$*" >&2; }\n' + t.extract('initd')

    @unittest.skipUnless(win_symlink.EXEC_SYMLINK, win_symlink.EXEC_REASON)
    @requires_symlink_layout
    def test_initd_guard_allows_trusted_root_owned_file_symlink(self):
        outside = self.case / 'mnt/init-outside'
        outside.mkdir(parents=True)
        marker = outside / 'EXECUTED'
        target = outside / 'guard-ok'
        target.write_text('#!/bin/sh\nprintf "ok\\n" > "%s"\n' % t.posix(marker),
                          encoding='utf-8', newline='\n')
        target.chmod(0o755)
        (self.case / 'etc/init.d/guard-ok').symlink_to(target)
        result = self.script('initd guard-ok start; printf "INITD_RC=%s\\n" "$?"',
                             extra=self._initd_extra())
        self.assertIn('INITD_RC=0', result.stdout, result.stdout + result.stderr)
        self.assertTrue(marker.exists(), 'guard refused a legitimate symlinked init script')

    @requires_symlink_layout
    def test_initd_guard_refuses_a_symlink_to_a_directory(self):
        outside = self.case / 'mnt/init-bad'
        outside.mkdir(parents=True)
        (self.case / 'etc/init.d/guard-bad').symlink_to('../../mnt/init-bad', target_is_directory=True)
        result = self.script('initd guard-bad start; printf "INITD_RC=%s\\n" "$?"',
                             extra=self._initd_extra())
        self.assertIn('INITD_RC=1', result.stdout, result.stdout + result.stderr)

    @requires_symlink_layout
    def test_symlinked_etc_mihomo_directory_is_supported(self):
        mnt = self.case / 'mnt'
        mnt.mkdir()
        (self.case / 'etc/mihomo').rename(mnt / 'mihomo')
        (self.case / 'etc/mihomo').symlink_to('../mnt/mihomo', target_is_directory=True)
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'),
                         result.stdout + result.stderr + self.logs())
        # Diagnostics for the Linux-only run: the restore refuses a symlinked /etc/mihomo,
        # so print the pin and the individual policy answers instead of guessing.
        print('DIAG symlinks:', (self.snapshot_dir / 'symlinks').read_text(
            encoding='utf-8', errors='replace'))
        print('DIAG objects:', '\n'.join(
            line for line in (self.snapshot_dir / 'objects').read_text(
                encoding='utf-8', errors='replace').splitlines()
            if line.startswith(('dirlink', 'dir|'))))
        probe = self.script(
            '_rs_pin_enforce=1; _rs_snap="%s"\n'
            '_rs_pin_mode=0\n'
            '_rs_link_ok /etc/mihomo "%s/etc/mihomo" dir; printf "LINK_RC=%%s\\n" "$?"\n'
            '_rs_trusted_link "$(readlink -f "%s/etc/mihomo")"; printf "TRUST_RC=%%s\\n" "$?"\n'
            '_rs_uid "$(readlink -f "%s/etc/mihomo")"; printf "UID_RC=%%s SELF=%%s\\n" "$?" "$(id -u)"\n'
            % (t.posix(self.snapshot_dir), t.posix(self.case), t.posix(self.case),
               t.posix(self.case)))
        print('DIAG probe:', probe.stdout, probe.stderr)
        restore = self.script(self.restore())
        self.assertEqual(0, self.rc_of(restore, 'RESTORE_RC'),
                         restore.stdout + restore.stderr + self.logs())
        self.assertTrue((self.case / 'etc/mihomo').is_symlink(),
                        'the trusted directory symlink was replaced by a real directory')

    # ------------------------------------------------------------ M2: restore shared
    def test_restore_shared_path_refusal_sets_restore_rc(self):
        snap = self.case / 'snap-shared'
        snap.mkdir()
        (snap / 'shared').write_text(
            'dnsai|/etc/passwd|1|regular|deadbeef|1:2|644\n', encoding='utf-8')
        (snap / 'shared.1.pre').write_text('', encoding='utf-8')
        extra = ('_rs_snap="%s"\n_rr=0\n_rs_vr=0\n'
                 'bad() { printf "BAD %%s\\n" "$*"; }\n'
                 '_rs_safe_path() { return 1; }\n'
                 '_rs_safe_file() { return 1; }\n'
                 '_rs_restore_shared "$_rs_snap"\n'
                 'printf "RR=%%s VR=%%s\\n" "$_rr" "$_rs_vr"\n' % t.posix(snap))
        result = self.script(extra)
        self.assertIn('RR=1', result.stdout, result.stdout + result.stderr)
        self.assertIn('BAD rollback:', result.stdout, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
