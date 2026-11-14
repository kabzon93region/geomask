"""Self-update backup transaction (audit 14.11.2026, blocker 4).

The updater's backup block lives inside the generated /tmp/geomask-upd/run.sh heredoc.
These tests extract that block verbatim and run it in bash against a throwaway tree, with
`/root/geomask-backups` and `/tmp` rewritten to temporary paths (nothing outside the
temporary directory is touched). `sysupgrade`, `mv` and `tar` are stubbed where the test
needs a failure; everything else is the real shell code.
"""
import gzip
import os
from pathlib import Path
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


def posix(path):
    text = str(Path(path).resolve()).replace('\\', '/')
    if text.startswith('/'):
        return text
    return '/' + text[0].lower() + text[2:]


def backup_block():
    """The updater's backup section, from the heredoc, verbatim."""
    text = BACKEND.read_text(encoding='utf-8')
    start = text.index("say backup \"creating full router backup")
    end_marker = 'echo "file=$BKF md5=$_md" > "$D/backup.info"'
    end = text.index(end_marker) + len(end_marker)
    return text[start:end]


@unittest.skipIf(BASH is None, 'bash is required')
class UpdaterBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.upd = self.root / 'upd'
        self.backups = self.root / 'backups'
        self.spool = self.root / 'spool'
        for path in (self.upd, self.backups, self.spool):
            path.mkdir()

    def run_block(self, prelude='', sysupgrade_body=None):
        block = backup_block()
        block = block.replace('/root/geomask-backups', posix(self.backups))
        block = block.replace('/tmp/router-backup-', posix(self.spool) + '/router-backup-')
        block = block.replace('mv -f "$_f" /tmp/', 'mv -f "$_f" ' + posix(self.spool) + '/')
        block = block.replace('D=/tmp/geomask-upd', '')
        default_sysupgrade = (
            'sysupgrade() {\n'
            '  _t=$(mktemp -d); mkdir -p "$_t/etc/config"; printf "x\\n" > "$_t/etc/config/network"\n'
            '  tar czf "$2" -C "$_t" etc 2>/dev/null; _rc=$?\n'
            '  rm -rf "$_t"; return $_rc\n'
            '}\n')
        script = (
            'set -u\n'
            'D=%s\n'
            'say() { printf "SAY %%s: %%s\\n" "$1" "$2"; }\n'
            '%s\n'
            '%s\n'
            '%s\n' % (posix(self.upd), sysupgrade_body or default_sysupgrade, prelude, block))
        path = self.root / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([BASH, posix(path)], cwd=str(self.root), capture_output=True,
                              text=True, encoding='utf-8', timeout=120)

    def make_archive(self, path, payload=b'old'):
        work = self.root / ('work-' + path.name)
        work.mkdir(exist_ok=True)
        (work / 'payload').write_bytes(payload)
        subprocess.run(['tar', 'czf', str(path), '-C', str(work), 'payload'], check=True)
        return path.read_bytes()

    def test_a_failed_store_keeps_the_previous_backup(self):
        old = self.backups / 'router-backup-20260101-000000.tar.gz'
        before = self.make_archive(old)
        # the new archive is fine, but moving it into the permanent directory fails.
        # `mv -f src dst`: the source is $2 and the destination $3 - the old injection
        # matched $2 and therefore failed while PARKING, never reaching the store (F25).
        prelude = ('mv() { if [ "$1" = "-f" ] && [ "$2" = "$BK" ] && [ "$3" = "$BKF" ]; '
                   'then return 1; fi; command mv "$@"; }')
        result = self.run_block(prelude=prelude)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(old.exists(), 'the permanent backup disappeared after a failed move')
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual([p.name for p in self.backups.iterdir()],
                         ['router-backup-20260101-000000.tar.gz'])
        # the new archive is a valid backup: it stays in the updater's working directory and
        # the message says where, so the owner can still copy it
        left = list(self.upd.glob('router-backup-*.tar.gz'))
        self.assertEqual(len(left), 1, 'the new archive was silently deleted')
        self.assertIn(left[0].name, result.stdout)

    def test_same_names_in_tmp_and_backup_dir_do_not_overwrite_each_other(self):
        name = 'router-backup-20260101-000000.tar.gz'
        permanent = self.backups / name
        spooled = self.spool / name
        permanent_before = self.make_archive(permanent, b'permanent')
        spool_before = self.make_archive(spooled, b'spooled')
        result = self.run_block(sysupgrade_body='sysupgrade() { return 1; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(permanent.exists())
        self.assertTrue(spooled.exists())
        self.assertEqual(permanent.read_bytes(), permanent_before)
        self.assertEqual(spooled.read_bytes(), spool_before)

    def test_a_gzip_stream_that_is_not_tar_is_not_a_backup(self):
        old = self.backups / 'router-backup-20260101-000000.tar.gz'
        before = self.make_archive(old)
        result = self.run_block(
            sysupgrade_body='sysupgrade() { printf "not a tar" | gzip > "$2"; return 0; }')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(old.exists())
        self.assertEqual(old.read_bytes(), before)
        self.assertIn('backup failed', result.stdout)

    def test_a_successful_backup_is_stored_and_old_copies_rotate_out(self):
        old = self.backups / 'router-backup-20260101-000000.tar.gz'
        self.make_archive(old)
        result = self.run_block()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        stored = list(self.backups.glob('router-backup-*.tar.gz'))
        # retention is "freshest one": the new archive replaces the old only after it is
        # verified and in place, and nothing is left in the temporary spool
        self.assertEqual(len(stored), 1, 'the new archive was not stored')
        self.assertNotEqual(stored[0].name, old.name)
        self.assertEqual(list(self.spool.glob('router-backup-*.tar.gz')), [])
        info = (self.upd / 'backup.info').read_text(encoding='utf-8')
        self.assertIn('file=', info)
        self.assertIn(posix(self.backups), info)


if __name__ == '__main__':
    unittest.main()
