"""R04 review round 2: the fixes for the blockers and cheap defects.

Covered here (each test fails on the pre-review worktree):
  * the installer's live-config rewrite participates in the transaction lock (blocker 1);
  * the scanner keeps ONE backup with an atomic replace, not one file per scan (blocker 3);
  * the guard does not hold `console` while waiting 20 s for the route table (defect 5);
  * a fresh install refuses without the flock primitive (defect 6);
  * a non-numeric wait is refused instead of looping forever (defect 8);
  * `revoke_applied_pool` reads the live config once (defect 4);
  * the torrent lock is bounded (defect 7);
  * one canonical transaction lock name, guard comment, per-PID installer temps (9-11).
Kernel semantics (SIGKILL release, inode identity, Python<->shell exclusion) stay Linux-only and
are covered by test_lock_protocol.py / the stand.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / 'geomask.sh.in'
HELPER = ROOT / 'assets' / 'geomask-lock.sh'
ROUTES = ROOT / 'assets' / 'geomask-routes.sh'
GUARD = ROOT / 'assets' / 'geomask-guard.sh'
NODESCAN = ROOT / 'assets' / 'geomask-node-scan.sh'
TORRENT = ROOT / 'assets' / 'geomask-torrent.sh'
TX = ROOT / 'assets' / 'geomask-config-tx.py'
APPLY = ROOT / 'assets' / 'geomask-apply.py'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


BASH = find_bash()


def p(value):
    raw = Path(value).as_posix()
    match = re.match(r'^([A-Za-z]):/(.*)$', raw)
    return '/' + match.group(1).lower() + '/' + match.group(2) if match else raw


def shq(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def func(name, source=None):
    src = TPL.read_text(encoding='utf-8') if source is None else source
    lines = src.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(name + '() {'))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == '}')
    return '\n'.join(lines[start:end + 1])


@unittest.skipIf(BASH is None, 'bash is required')
class ReviewRoundTwoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'locks').mkdir()

    def write(self, name, body):
        path = self.case / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8', newline='\n')
        return path

    def run_sh(self, path, timeout=10):
        env = dict(os.environ)
        env.pop('MSYS', None)
        return subprocess.run([BASH, p(path)], cwd=str(self.case), capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=timeout,
                              env=env)

    def hold(self, name, explicit=None, seconds=6):
        target = explicit or (self.case / 'locks' / (name + '.lock'))
        script = self.write('holder.sh', (
            'GM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\n. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire %s 0 %s || exit 9\nprintf held > %s\nsleep %d\n'
            % (shq(p(self.case / 'locks')), shq(p(HELPER)), name, shq(p(target)),
               shq(p(self.case / 'held')), seconds)))
        proc = subprocess.Popen([BASH, p(script)], cwd=str(self.case))
        self.addCleanup(self.reap, proc)
        for _ in range(200):
            if (self.case / 'held').exists():
                return proc
            time.sleep(0.05)
        self.fail('the lock holder never started')

    @staticmethod
    def reap(proc):
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass

    # ---------------------------------------------------------------- blocker 1
    def test_installer_config_write_waits_for_the_transaction_lock(self):
        cfg = self.write('config.yaml', 'mixed-port: 7890\nproxy-providers:\n  sub:\n    url: "old"\n')
        original = cfg.read_bytes()
        txlock = self.case / 'tx.lock'
        self.hold('config-tx', txlock, seconds=8)
        src = TPL.read_text(encoding='utf-8')
        funcs = func('write_mihomo_config', src)
        if '_write_mihomo_config_unlocked' in src:
            funcs += '\n' + func('_write_mihomo_config_unlocked', src)
        script = self.write('installer.sh', (
            'set -u\nGM_PREFIX=""\nMIHOMO_CONF=%s\nGM_PRE_CONFIG=%s/pre\n'
            'GM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\nGM_CONFIG_TX_LOCK=%s\n. "$GM_LOCK_LIB"\n'
            'P() { printf "%%s%%s" "${GM_PREFIX:-}" "$1"; }\n'
            'have() { command -v "$1" >/dev/null 2>&1; }\n'
            'bad() { printf "BAD %%s\\n" "$*" >&2; }\ninfo() { :; }\nwarn() { :; }\n'
            'lan_ip() { printf 192.168.1.1; }\nlan_iface() { printf br-lan; }\n'
            'geomask_config_shaped() { return 0; }\n_prov_url() { printf "old"; }\n'
            'mihomo_validate() { return 0; }\n%s\n'
            'write_mihomo_config "https://new.example/sub" ""; printf "WRC=%%s\\n" "$?"\n'
            % (shq(p(cfg)), shq(p(self.case)), shq(p(self.case / 'locks')), shq(p(HELPER)),
               shq(p(txlock)), funcs)))
        try:
            result = subprocess.run([BASH, p(script)], cwd=str(self.case), capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=2.5)
            out = result.stdout + result.stderr
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b'').decode('utf-8', 'replace') if isinstance(exc.stdout, bytes) else (exc.stdout or '')
        self.assertEqual(cfg.read_bytes(), original,
                         'the installer rewrote the live config while config-tx was held')
        self.assertNotIn('WRC=0', out, 'write_mihomo_config reported success on a busy lock')

    # ---------------------------------------------------------------- blocker 3
    def test_scanner_keeps_one_backup(self):
        cfg = self.write('config.yaml', 'proxy-groups: []\n')
        src = NODESCAN.read_text(encoding='utf-8')
        block = src[src.index('applied=False\nif changed:'):src.index('\n# remember list')]
        if 'def save_backup' in src:
            sb = src.index('def save_backup')
            block = src[sb:src.index('\n# Provider ids', sb)] + '\n' + block
        driver = self.write('driver.py', (
            'import json, os, re, sys, types\n'
            'CASE = sys.argv[1]\nCFG = os.path.join(CASE, "config.yaml")\n'
            'TMP_CFG = CFG + ".tmp.%d" % os.getpid()\n_PID = os.getpid()\n'
            'text = open(CFG, encoding="utf-8", errors="replace").read()\n'
            'out = text + "# patched\\n"\nchanged = True\ntotal = 1\nblocked = []\nerrs = 0\n'
            'provider = "sub"\nmatched = 1\n'
            'def log(*a, **k):\n    pass\n'
            'class ConfigWriteRefused(Exception):\n    pass\n'
            'def install_config_candidate(candidate, expected):\n    os.replace(candidate, CFG)\n'
            'def _run(*a, **k):\n    return types.SimpleNamespace(returncode=0)\n'
            'subprocess = types.SimpleNamespace(run=_run)\n'
            'class _R(object):\n'
            '    Request = staticmethod(lambda *a, **k: None)\n'
            '    @staticmethod\n    def urlopen(*a, **k):\n        raise OSError("no controller")\n'
            'urllib = types.SimpleNamespace(request=_R)\n'
            + block + '\n'))
        for _ in range(2):
            result = subprocess.run([sys.executable, '-B', str(driver), str(self.case)],
                                    capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-400:])
        backups = list(self.case.glob('config.yaml.bak_nodescan*'))
        self.assertLessEqual(len(backups), 1,
                             'one backup per scan accumulates in /etc/mihomo: %s'
                             % [item.name for item in backups])
        self.assertEqual(list(self.case.glob('config.yaml.tmp.*')), [])

    # ---------------------------------------------------------------- defect 5
    def test_guard_does_not_wait_for_the_route_table(self):
        state = self.write('console.state', 'enabled=1\nip=192.168.1.177\n')
        self.hold('route-102', self.case / 'locks' / 'route-102.lock', seconds=10)
        src = GUARD.read_text(encoding='utf-8')
        block = src[src.index('# ---------- (E) console lane watchdog ----------'):
                    src.index('# ---------- (D) AI-DNS self-heal ----------')]
        script = self.write('guard.sh', (
            'set -u\nCASE=%s\nGM_CONSOLE_STATE=%s\nGM_CONSOLE_LOCK=%s\n'
            'GM_ROUTES_LIB=%s\nGM_ROUTE_STATE_DIR=%s\nGM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\n'
            '. "$GM_LOCK_LIB"\n'
            'log() { printf "%%s\\n" "$*" >> "$CASE/logger"; }\n'
            'conntrack() { return 0; }\n'
            'ip() { case "$1 $2" in "rule show") return 0 ;; "rule del") return 1 ;; '
            '"route show") printf "default dev Console\\n" ;; *) return 0 ;; esac; }\n'
            '%s\n'
            % (shq(p(self.case)), shq(p(state)), shq(p(self.case / 'locks' / 'console.lock')),
               shq(p(ROUTES)), shq(p(self.case / 'state')), shq(p(self.case / 'locks')),
               shq(p(HELPER)), block)))
        started = time.monotonic()
        # The product's bad case is a 20 s wait for route-102 while holding console. Measure in
        # Python (not shell integer seconds) and keep a wide margin for a loaded CI host: 10 s
        # still proves "it did not wait for the route lock", 20 s would be the old behaviour.
        result = self.run_sh(script, timeout=20)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertLess(elapsed, 10,
                        'the guard held console %.1fs while waiting for route-102' % elapsed)
        self.assertIn('not synced', (self.case / 'logger').read_text(encoding='utf-8'))

    # ---------------------------------------------------------------- round 4 step gate
    def test_step_mode_refuses_without_flock(self):
        """`--step 2` runs in a process where step1_check never ran: probe directly."""
        stub = self.case / 'stub-step'
        stub.mkdir()
        uname = stub / 'uname'
        uname.write_text('#!/bin/sh\nprintf "Linux\\n"\n', encoding='utf-8', newline='\n')
        uname.chmod(0o755)
        src = TPL.read_text(encoding='utf-8')
        funcs = func('lock_primitive_missing', src) + '\n' + func('run_step', src)
        script = self.write('step2.sh', (
            'set -u\nPATH=%s\nCASE=%s\nSTEP_NAMES="env geo"\n'
            'FAILCNT=0 PASS=0 SKIP=0 GM_CHECK_ONLY=0 GM_AUTO=1 GM_STEP1_FATAL=0\n'
            'hdr() { :; }\ninfo() { :; }\nbad() { printf "BAD %%s\\n" "$*" >&2; }\n'
            'cut() { printf geo; }\n'
            'step_fn() { case "$2" in check) return 1 ;; apply) '
            ': > "$CASE/APPLIED"; return 0 ;; verify) return 0 ;; esac; }\n'
            'ask() { return 0; }\nbackup_gate() { return 0; }\nmodules_maybe_prompt() { :; }\n'
            'have() { command -v "$1" >/dev/null 2>&1; }\nP() { printf "%%s" "$1"; }\n'
            '%s\n'
            'run_step 2; printf "RC=%%s\\n" "$?"\n'
            % (shq(p(stub)), shq(p(self.case)), funcs)))
        result = self.run_sh(script)
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)
        self.assertFalse((self.case / 'APPLIED').exists(),
                         'step 2 was applied although the lock primitive is missing')
        self.assertIn('refusing to apply', result.stdout + result.stderr)

    # ---------------------------------------------------------------- defect 6
    def test_fresh_install_refuses_without_flock(self):
        stub = self.case / 'stub'
        stub.mkdir()
        uname = stub / 'uname'
        uname.write_text('#!/bin/sh\nprintf "Linux\\n"\n', encoding='utf-8', newline='\n')
        uname.chmod(0o755)
        src = TPL.read_text(encoding='utf-8')
        funcs = func('lock_primitive_missing', src) + '\n' + func('preflight', src)
        script = self.write('preflight.sh', (
            'set -u\nPATH=%s\nCASE=%s\nGM_VPN_URL="https://x/sub"\nMIHOMO_CONF="$CASE/mihomo.yaml"\n'
            'have() { command -v "$1" >/dev/null 2>&1; }\nP() { printf "%%s" "$1"; }\n'
            '_prov_url() { :; }\nany_provider_url() { :; }\ngeomask_config_shaped() { return 1; }\n'
            'lan_ip() { printf 192.168.1.1; }\nlan_iface() { printf br-lan; }\n'
            'bad() { printf "BAD %%s\\n" "$*" >&2; }\ninfo() { :; }\nwarn() { :; }\n%s\n'
            'preflight; printf "RC=%%s\\n" "$?"\n'
            % (shq(p(stub)), shq(p(self.case)), funcs)))
        result = self.run_sh(script)
        self.assertIn('RC=1', result.stdout, result.stdout + result.stderr)
        self.assertIn('flock', result.stdout + result.stderr)

    # ---------------------------------------------------------------- defect 8
    def test_a_non_numeric_wait_is_refused(self):
        self.hold('console', self.case / 'locks' / 'console.lock')
        script = self.write('wait.sh', (
            'GM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\n. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire console abc\nexit $?\n'
            % (shq(p(self.case / 'locks')), shq(p(HELPER)))))
        result = self.run_sh(script, timeout=5)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)


class ReviewRoundTwoStaticTests(unittest.TestCase):
    def test_apply_revoke_reads_the_config_once(self):
        src = APPLY.read_text(encoding='utf-8')
        body = src[src.index('def revoke_applied_pool'):src.index('\ndef ', src.index('def revoke_applied_pool') + 10)]
        reads = len(re.findall(r'DEFAULT_CONFIG\.read_bytes\(\)', body)) + \
            len(re.findall(r'open\(DEFAULT_CONFIG', body))
        self.assertEqual(1, reads, 'the CAS base and the candidate must come from one read')

    def test_torrent_lock_is_bounded(self):
        src = TORRENT.read_text(encoding='utf-8')
        body = src[src.index('def tx_lock'):src.index('\ndef ', src.index('def tx_lock') + 10)]
        self.assertIn('LOCK_NB', body)
        self.assertIn('time.monotonic', body)
        self.assertNotRegex(body, r'fcntl\.flock\(fd,\s*fcntl\.LOCK_EX\)')

    def test_one_canonical_transaction_lock_name(self):
        helper = HELPER.read_text(encoding='utf-8')
        tx = TX.read_text(encoding='utf-8')
        self.assertIn('GM_CONFIG_TX_LOCK', helper)
        self.assertIn('GM_CONFIG_TX_LOCK', tx)
        for name in ('geomask-node-scan.sh', 'geomask-torrent.sh'):
            text = (ROOT / 'assets' / name).read_text(encoding='utf-8')
            self.assertIn('/etc/geomask/node-audit/state/.config-tx.lock', text)

    def test_guard_no_longer_documents_the_removed_protocol(self):
        src = GUARD.read_text(encoding='utf-8')
        self.assertNotIn('directory with the owner pid', src)
        self.assertNotIn('stale lock (owner gone) is broken', src)
        self.assertIn('GM_ROUTE_LOCK_WAIT=0', src)

    def test_installer_temps_are_per_pid_and_pruned(self):
        src = TPL.read_text(encoding='utf-8')
        self.assertRegex(src, r'\$_c\.urlnew\.\$')
        self.assertIn('REINSTALL_TEMP_GLOBS', src)
        self.assertIn('gm_lock_acquire config-tx', src)
        step1 = src[src.index('step1_check() {'):src.index('\n}', src.index('step1_check() {')) + 2]
        self.assertIn('lock_primitive_missing', step1)

    # ---------------------------------------------------------------- round 4
    def test_transaction_lock_path_is_prefixed_with_the_install_root(self):
        src = TPL.read_text(encoding='utf-8')
        self.assertIn('GM_CONFIG_TX_LOCK=${GM_CONFIG_TX_LOCK:-$(P /etc/geomask/node-audit/state/.config-tx.lock)}',
                      src)
        # must be set before the helper is sourced: it captures the canonical path at source time
        self.assertLess(src.index('GM_CONFIG_TX_LOCK=${GM_CONFIG_TX_LOCK:-$(P '),
                        src.index('@@ASSET:geomask-lock.sh@@'))

    def test_mihomo_preimage_restore_is_serialised(self):
        src = TPL.read_text(encoding='utf-8')
        helper = func('_mihomo_restore_preimage', src)
        self.assertIn('gm_lock_acquire config-tx', helper)
        restart = func('safe_mihomo_restart', src)
        self.assertNotIn('cp -p "$_pre" "$_c"', restart,
                         'safe_mihomo_restart still writes config.yaml without config-tx')
        self.assertIn('_mihomo_restore_preimage "$_pre" "$_c"', restart)

    def test_verify_files_honours_its_argument(self):
        body = func('_rs_verify_files')
        self.assertIn('_rs_snap=${1:-$_rs_snap}', body)

    def test_scanner_backup_failure_is_visible(self):
        src = NODESCAN.read_text(encoding='utf-8')
        body = src[src.index('def save_backup'):src.index('\n# Provider ids',
                                                          src.index('def save_backup'))]
        self.assertIn('NOT saved', body)
        self.assertIn('return False', body)

    def test_prune_does_not_touch_generic_tmp_names(self):
        src = TPL.read_text(encoding='utf-8')
        line = next(item for item in src.splitlines()
                    if item.startswith('REINSTALL_TEMP_GLOBS='))
        self.assertNotIn('.tmp.*', line,
                         'a generic .tmp.* prune can delete another writer\'s live temporary')

    def test_step1_lock_refusal_blocks_the_wizard(self):
        src = TPL.read_text(encoding='utf-8')
        step1 = src[src.index('step1_check() {'):src.index('\n}', src.index('step1_check() {')) + 2]
        self.assertIn('GM_STEP1_FATAL=1', step1)
        runner = func('run_step', src)
        self.assertIn('GM_STEP1_FATAL', runner)
        self.assertIn('refusing to apply', runner)


@unittest.skipIf(BASH is None, 'bash is required')
class RollbackSerialisationTests(unittest.TestCase):
    """Round 3: the rollback of byte objects must serialise with the live config transaction.

    rpcd is already reloaded when a failed reinstall rolls back, so the panel can commit
    config.yaml through geomask-config-tx.py. `_rs_restore_files` writes that same file and
    used to do it outside the transaction lock: a live commit could be overwritten or land
    between the copy and the verification.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case = Path(self.tmp.name)
        (self.case / 'locks').mkdir()
        self.snap = self.case / 'snap'
        self.target = self.case / 'config.yaml'
        self.target.write_text('LIVE-NEWER-COMMIT\n', encoding='utf-8', newline='\n')
        copy = self.snap / 'files' / 'config.yaml'
        copy.parent.mkdir(parents=True)
        copy.write_text('SNAPSHOT-CONFIG\n', encoding='utf-8', newline='\n')
        (self.snap / 'objects').write_text('file|/config.yaml|644|abc|||\n', encoding='utf-8',
                                           newline='\n')

    def write(self, name, body):
        path = self.case / name
        path.write_text(body, encoding='utf-8', newline='\n')
        return path

    def restore_script(self):
        src = TPL.read_text(encoding='utf-8')
        funcs = func('_rs_restore_files', src)
        if '_rs_restore_files_unlocked' in src:
            funcs += '\n' + func('_rs_restore_files_unlocked', src) + '\n' + func('_rs_verify_files', src)
        return self.write('restore.sh', (
            'set -u\nCASE=%s\nGM_PREFIX="$CASE"\nP() { printf "%%s%%s" "$GM_PREFIX" "$1"; }\n'
            'bad() { printf "BAD %%s\\n" "$*" >&2; }\n_rr=0\n'
            '_rs_safe_path() { return 0; }\n_rs_safe_file() { return 0; }\n'
            '_rs_mode() { stat -c %%a "$1" 2>/dev/null; }\n'
            'GM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\nGM_CONFIG_TX_LOCK=%s\n. "$GM_LOCK_LIB"\n'
            '_rs_snap=%s\n%s\n'
            '_rs_restore_files "$_rs_snap"\nprintf "RRC=%%s RR=%%s\\n" "$?" "$_rr"\n'
            % (shq(p(self.case)), shq(p(self.case / 'locks')), shq(p(HELPER)),
               shq(p(self.case / 'tx.lock')), shq(p(self.snap)), funcs)))

    def hold(self):
        script = self.write('holder.sh', (
            'GM_LOCK_DIR=%s\nGM_LOCK_LIB=%s\nGM_CONFIG_TX_LOCK=%s\n. "$GM_LOCK_LIB"\n'
            'gm_lock_acquire config-tx 0 %s || exit 9\nprintf held > %s\nsleep 8\n'
            % (shq(p(self.case / 'locks')), shq(p(HELPER)), shq(p(self.case / 'tx.lock')),
               shq(p(self.case / 'tx.lock')), shq(p(self.case / 'held')))))
        proc = subprocess.Popen([BASH, p(script)], cwd=str(self.case))
        self.addCleanup(self.reap, proc)
        for _ in range(200):
            if (self.case / 'held').exists():
                return proc
            time.sleep(0.05)
        self.fail('the lock holder never started')

    @staticmethod
    def reap(proc):
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass

    def release_emulated(self):
        # The portable O_EXCL backend (Windows test host) cannot release a killed holder;
        # flock(2) releases the lock in the kernel. Test-host concession, documented.
        (self.case / 'tx.lock').unlink(missing_ok=True)

    def test_rollback_does_not_overwrite_a_live_commit(self):
        script = self.restore_script()
        self.hold()
        try:
            subprocess.run([BASH, p(script)], cwd=str(self.case), capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=3)
        except subprocess.TimeoutExpired:
            pass                       # waiting for the bounded budget is allowed
        self.assertEqual(self.target.read_text(encoding='utf-8'), 'LIVE-NEWER-COMMIT\n',
                         'the rollback overwrote the live config while config-tx was held')

    def test_rollback_succeeds_after_release_and_keeps_one_shot(self):
        script = self.restore_script()
        result = subprocess.run([BASH, p(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=30)
        self.assertIn('RRC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.target.read_text(encoding='utf-8'), 'SNAPSHOT-CONFIG\n')
        again = subprocess.run([BASH, p(script)], cwd=str(self.case), capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=30)
        self.assertIn('RRC=0', again.stdout,
                      'the rollback kept config-tx after finishing: ' + again.stdout + again.stderr)

    def test_never_a_mixed_state_with_a_live_commit(self):
        script = self.restore_script()
        holder = self.hold()
        try:
            try:
                subprocess.run([BASH, p(script)], cwd=str(self.case), capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=3)
            except subprocess.TimeoutExpired:
                pass
            self.assertEqual(self.target.read_text(encoding='utf-8'), 'LIVE-NEWER-COMMIT\n')
        finally:
            self.reap(holder)
        if os.name == 'nt':
            self.release_emulated()
        result = subprocess.run([BASH, p(script)], cwd=str(self.case), capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=30)
        self.assertIn('RRC=0', result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.target.read_text(encoding='utf-8'), 'SNAPSHOT-CONFIG\n',
                         'final state is neither the commit nor the snapshot')

    def test_bytes_are_verified_inside_the_lock(self):
        body = func('_rs_restore_files')
        self.assertIn('gm_lock_acquire config-tx', body)
        acquire = body.index('gm_lock_acquire config-tx')
        verify = body.find('_rs_verify_files')
        release = body.find('gm_lock_release config-tx')
        self.assertGreater(verify, acquire, 'no in-lock verification of the restored bytes')
        self.assertGreater(release, verify, 'the lock is released before the byte verification')

    def test_other_restore_objects_are_out_of_transaction_scope(self):
        src = TPL.read_text(encoding='utf-8')
        for name in ('_rs_restore_uci', '_rs_restore_shared', '_rs_restore_sysctl',
                     '_rs_restore_links'):
            self.assertNotIn('gm_lock_acquire config-tx', func(name, src),
                             '%s takes config-tx although the transaction does not write it' % name)
        verify = src[src.index('_rs_restore_verify() {'):
                     src.index('\n}', src.index('_rs_restore_verify() {'))]
        self.assertIn('concurrent writer', verify)


if __name__ == '__main__':
    unittest.main()
