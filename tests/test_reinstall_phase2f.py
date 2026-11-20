"""R03 phase 2f: symlinked init scripts, pin lifecycle, cross-module leak.

RED first on 8057ce9eec75a99a (before the fix).
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
class Phase2fTest(unittest.TestCase):

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

    def assertSnapshotKept(self):
        self.h.assertSnapshotKept()

    def _symlink_init(self, name='network'):
        mnt = self.case / 'mnt'
        mnt.mkdir(exist_ok=True)
        (self.case / 'etc/init.d' / name).rename(mnt / name)
        (mnt / name).chmod(0o755)
        try:
            (self.case / 'etc/init.d' / name).symlink_to(str(mnt / name))
        except (OSError, NotImplementedError) as exc:
            self.skipTest('host cannot create an absolute native symlink: %s' % exc)

    def _initd_extra(self):
        return 'warn() { printf "WARN %s\\n" "$*" >&2; }\n' + t.extract('initd')

    # ------------------------------------------------ A: full restore with symlink init
    @requires_symlink_layout
    def test_symlinked_init_script_full_restore(self):
        self._symlink_init('network')
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'),
                         result.stdout + result.stderr + self.logs())
        restore = self.script(self.restore())
        self.assertEqual(0, self.rc_of(restore, 'RESTORE_RC'),
                         restore.stdout + restore.stderr + self.logs())

    @requires_symlink_layout
    def test_symlinked_firewall_init_script_restores(self):
        self._symlink_init('firewall')
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'),
                         result.stdout + result.stderr + self.logs())
        restore = self.script(self.restore())
        self.assertEqual(0, self.rc_of(restore, 'RESTORE_RC'),
                         restore.stdout + restore.stderr + self.logs())

    # ------------------------------------------------ B: swap before first initd
    @requires_symlink_layout
    def test_init_script_swap_before_first_initd_is_refused(self):
        self._symlink_init('mihomo')
        evil = self.case / 'mnt/mihomo'
        marker = self.case / 'mnt/EVIL'
        body = (self.snap() + '\n'
                + 'rm -f "%s"\n' % t.posix(evil)
                + 'printf "#!/bin/sh\\nprintf evil > \\"%s\\"\\n" > "%s"\n'
                  % (t.posix(marker), t.posix(evil))
                + 'initd mihomo start; printf "INITD_RC=%s\\n" "$?"\n'
                + self.restore())
        result = self.script(body, extra=self._initd_extra())
        self.assertIn('INITD_RC=1', result.stdout, result.stdout + result.stderr)
        self.assertFalse(marker.exists(), 'a swapped init script was executed')
        self.assertEqual(1, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertSnapshotKept()

    @requires_symlink_layout
    def test_initd_works_after_successful_snapshot_cleanup(self):
        self._symlink_init('mihomo')
        body = (self.snap() + '\n'
                + 'rm -rf "%s"\n' % t.posix(self.snapshot_dir)
                + 'initd mihomo stop; printf "INITD_RC=%s\\n" "$?"\n')
        result = self.script(body, extra=self._initd_extra())
        self.assertIn('INITD_RC=0', result.stdout, result.stdout + result.stderr)

    @requires_symlink_layout
    def test_snapshot_failure_resets_pin_mode(self):
        self._symlink_init('mihomo')
        body = ('GM_AUTO_LANE=1\n' + self.snap() + '\n'
                + 'initd mihomo stop; printf "INITD_RC=%s\\n" "$?"\n')
        result = self.script(body, extra=self._initd_extra())
        self.assertEqual(1, self.rc_of(result, 'SNAP_RC'), result.stdout + result.stderr)
        self.assertIn('INITD_RC=0', result.stdout, result.stdout + result.stderr)


    # ------------------------------------------------ same logic without exec
    @requires_symlink_layout
    def test_initd_guard_refuses_untrusted_file_without_exec(self):
        outside = self.case.parent / 'guard-outside'
        outside.mkdir(exist_ok=True)
        marker = outside / 'EXECUTED'
        target = outside / 'evil.sh'
        target.write_text('#!/bin/sh\nprintf evil > "%s"\n' % t.posix(marker),
                          encoding='utf-8', newline='\n')
        target.chmod(0o755)
        (self.case / 'etc/init.d/guard-untrusted').symlink_to(str(target))
        result = self.script('initd guard-untrusted start; printf "INITD_RC=%s\\n" "$?"',
                             extra=self._initd_extra())
        self.assertIn('INITD_RC=1', result.stdout, result.stdout + result.stderr)
        self.assertFalse(marker.exists(), 'untrusted symlinked init script was executed')

    @requires_symlink_layout
    def test_pin_state_without_exec(self):
        self._symlink_init('mihomo')
        body = (self.snap() + '\n'
                + 'printf "M1=%s E1=%s\\n" "$_rs_pin_mode" "${_rs_pin_enforce:-0}"\n'
                + 'rm -rf "%s"\n' % t.posix(self.snapshot_dir)
                + 'printf "M2=%s E2=%s\\n" "$_rs_pin_mode" "${_rs_pin_enforce:-0}"\n'
                + '_rs_safe_exec /etc/init.d/mihomo; printf "SAFE2=%s\\n" "$?"\n')
        result = self.script(body)
        self.assertIn('M1=0 E1=1', result.stdout, result.stdout + result.stderr)
        self.assertIn('M2=0 E2=1', result.stdout, result.stdout + result.stderr)
        self.assertIn('SAFE2=0', result.stdout, result.stdout + result.stderr)

    @requires_symlink_layout
    def test_snapshot_failure_pin_state_without_exec(self):
        self._symlink_init('mihomo')
        body = ('GM_AUTO_LANE=1\n' + self.snap() + '\n'
                + 'printf "M=%s E=%s\\n" "$_rs_pin_mode" "${_rs_pin_enforce:-0}"\n'
                + '_rs_safe_exec /etc/init.d/mihomo; printf "SAFE=%s\\n" "$?"\n')
        result = self.script(body)
        self.assertIn('M=0 E=0', result.stdout, result.stdout + result.stderr)
        self.assertIn('SAFE=0', result.stdout, result.stdout + result.stderr)

    @requires_symlink_layout
    def test_pin_swap_detected_without_exec(self):
        self._symlink_init('mihomo')
        target = self.case / 'mnt/mihomo'
        marker = self.case / 'mnt/EVIL'
        body = (self.snap() + '\n'
                # Create the replacement while the pinned inode still exists, then rename it
                # over the target: rm+create can reuse the freed inode, and then the pin
                # legitimately matches the swapped file (CI-187 group E).
                + 'printf "#!/bin/sh\\nprintf evil > \\"%s\\"\\n" > "%s.new"\n'
                  % (t.posix(marker), t.posix(target))
                + 'mv -f "%s.new" "%s"\n' % (t.posix(target), t.posix(target))
                + 'printf "ENFORCE=%s MODE=%s LINK=%s\\n" "${_rs_pin_enforce:-0}" '
                  '"${_rs_pin_mode:-0}" "$(readlink -f /etc/init.d/mihomo 2>/dev/null)"\n'
                + '_rs_safe_exec /etc/init.d/mihomo; printf "SAFE=%s\\n" "$?"\n'
                + '_rs_verify_runtime "%s"; printf "VERIFY=%%s\\n" "$?"\n'
                  % t.posix(self.snapshot_dir))
        result = self.script(body)
        print('DIAG 2f:', result.stdout, result.stderr)
        print('DIAG 2f symlinks:', (self.snapshot_dir / 'symlinks').read_text(
            encoding='utf-8', errors='replace'))
        self.assertIn('SAFE=1', result.stdout, result.stdout + result.stderr)
        self.assertIn('VERIFY=1', result.stdout, result.stdout + result.stderr)
        self.assertFalse(marker.exists(), 'swapped init script was executed')

    @requires_symlink_layout
    def test_symlinked_init_restore_state_without_exec(self):
        self._symlink_init('network')
        body = (self.snap() + '\n' + self.restore())
        result = self.script(body, extra='initd() { :; }\n')
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'),
                         result.stdout + result.stderr + self.logs())
        self.assertEqual(0, self.rc_of(result, 'RESTORE_RC'),
                         result.stdout + result.stderr + self.logs())

    # ------------------------------------------------ host-independent path forms
    def test_drive_path_is_normalized_before_policy(self):
        body = ('_rs_posix_path "Y:\\openwrt\\x"; printf "P1=%s\\n" "$?"\n'
                '_rs_posix_path "//?/Y:/openwrt/x"; printf "P2=%s\\n" "$?"\n')
        result = self.script(body)
        self.assertIn('/y/openwrt/x', result.stdout, result.stdout + result.stderr)

    @requires_symlink_layout
    def test_absolute_windows_target_is_accepted(self):
        target = self.case / 'mnt/abs-target'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('#!/bin/sh\n', encoding='utf-8', newline='\n')
        target.chmod(0o755)
        link = self.case / 'etc/init.d/abs-link'
        try:
            link.symlink_to(str(target))
        except (OSError, NotImplementedError) as exc:
            self.skipTest('host cannot create an absolute native symlink: %s' % exc)
        result = self.script(self.snap())
        self.assertEqual(0, self.rc_of(result, 'SNAP_RC'),
                         result.stdout + result.stderr + self.logs())

    def test_pin_lifecycle_without_symlinks(self):
        """Pin mode/enforce reset is proven on ordinary paths (no host symlink layout)."""
        body = (self.snap() + '\n'
                + 'printf "M1=%s E1=%s\\n" "$_rs_pin_mode" "${_rs_pin_enforce:-0}"\n'
                + 'rm -rf "%s"\n' % t.posix(self.snapshot_dir)
                + 'printf "M2=%s E2=%s\\n" "$_rs_pin_mode" "${_rs_pin_enforce:-0}"\n'
                + '_rs_safe_exec /etc/init.d/network; printf "SAFE=%s\\n" "$?"\n')
        result = self.script(body)
        self.assertIn('M1=0 E1=1', result.stdout, result.stdout + result.stderr)
        self.assertIn('M2=0 E2=1', result.stdout, result.stdout + result.stderr)
        self.assertIn('SAFE=0', result.stdout, result.stdout + result.stderr)

    # ------------------------------------------------ cross-module state leak
    @requires_symlink_layout
    def test_phase2d_then_phase2e_in_one_process(self):
        """The pin lifecycle must not leak between modules in one unittest process."""
        loader = unittest.TestLoader()
        suite = unittest.TestSuite([
            loader.loadTestsFromName(
                'test_reinstall_phase2d.Phase2dTest.test_symlinked_init_d_is_refused_by_the_snapshot'),
            loader.loadTestsFromName(
                'test_reinstall_phase2e.Phase2eTest.test_extroot_opt_symlink_is_accepted'),
            loader.loadTestsFromName(
                'test_reinstall_phase2e.Phase2eTest.'
                'test_symlink_target_swap_between_snapshot_and_restore_is_incomplete'),
        ])
        result = unittest.TestResult()
        suite.run(result)
        problems = ['%s: %s' % (test, detail)
                    for test, detail in result.failures + result.errors]
        self.assertFalse(problems, '\n'.join(problems))


if __name__ == '__main__':
    unittest.main()
