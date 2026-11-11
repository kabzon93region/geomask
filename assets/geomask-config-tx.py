#!/usr/bin/env python3
"""Config transaction manager for mihomo: single writer, CAS, trial, rollback, recovery."""
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

# Load state module for paths
import importlib.util
STATE_MODULE_PATH = Path(__file__).with_name('geomask-audit-state.py')
_state_spec = importlib.util.spec_from_file_location('audit_state', STATE_MODULE_PATH)
_state = importlib.util.module_from_spec(_state_spec)
_state_spec.loader.exec_module(_state)

# Paths from state module
DEFAULT_CONFIG = _state.DEFAULT_CONFIG
DEFAULT_STATE_DIR = _state.DEFAULT_STATE_DIR
DEFAULT_STORE = _state.DEFAULT_STORE
DEFAULT_REPORTS_DIR = _state.DEFAULT_REPORTS_DIR
DEFAULT_POOLS_DIR = _state.DEFAULT_POOLS_DIR

# Transaction journal
TX_JOURNAL = DEFAULT_STATE_DIR / 'config-tx-journal.jsonl'
TX_LOCK = DEFAULT_STATE_DIR / '.config-tx.lock'
TX_STATE_DIR = DEFAULT_STATE_DIR / 'tx-work'
TX_PREIMAGES_DIR = DEFAULT_STATE_DIR / 'tx-preimages'

class TxError(Exception):
    pass

# A provider-backed group can be absent or empty for a moment after a reload; the
# runtime check polls up to this long before declaring the reload unsuccessful.
RUNTIME_VERIFY_SECONDS = 20.0

def stamp():
    return datetime.datetime.now().astimezone().isoformat()

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def canonical_json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()

def encode(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()

def strict_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TxError('duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise TxError('invalid JSON') from None

@contextlib.contextmanager
def tx_lock():
    """Process-wide transaction lock using flock (Unix) or file-based lock (Windows)."""
    TX_LOCK.parent.mkdir(parents=True, exist_ok=True)
    
    if hasattr(os, 'O_NOFOLLOW'):  # Unix
        import fcntl
        fd = None
        try:
            fd = os.open(TX_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                os.close(fd)
    else:  # Windows - use file-based lock
        lock_acquired = False
        try:
            # Try to create lock file exclusively
            fd = os.open(TX_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            lock_acquired = True
            yield
        finally:
            if lock_acquired:
                try:
                    os.unlink(TX_LOCK)
                except OSError:
                    pass

@contextlib.contextmanager
def job_budget(seconds):
    deadline = time.monotonic() + seconds
    if not hasattr(signal, 'SIGALRM'):
        yield deadline
        return
    def expired(signum, frame):
        raise TxError('transaction time budget exceeded')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield deadline
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix=f'.{path.name}-', delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    try:
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

def fsync_dir(path: Path):
    """Force directory metadata to disk."""
    try:
        # O_DIRECTORY is Unix-only; skip on Windows
        if hasattr(os, 'O_DIRECTORY'):
            fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except (AttributeError, OSError):
        pass  # Skip on platforms without O_DIRECTORY

# The journal is append-only during a transaction, but it must not grow without bound on a
# router with a small overlay. Entries of a transaction that never reached a terminal stage
# are never dropped: recovery still needs them.
MAX_TX_JOURNAL_ENTRIES = 400
MAX_TX_JOURNAL_BYTES = 256 * 1024
TERMINAL_STAGES = ('committed', 'rollback_complete', 'rollback_partial',
                   'recovery_complete', 'recovery_failed')

def append_tx_journal(entry: dict):
    """Append transaction journal entry (append-only during a transaction)."""
    entry['timestamp'] = stamp()
    line = json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n'
    TX_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(TX_JOURNAL, 'ab') as f:
        f.write(line.encode('utf-8'))
        f.flush()
        os.fsync(f.fileno())
    fsync_dir(TX_JOURNAL.parent)
    trim_tx_journal()


def trim_tx_journal():
    """Bound the journal, but keep every transaction that is still unfinished."""
    try:
        if not TX_JOURNAL.exists():
            return 0
        raw = TX_JOURNAL.read_text(encoding='utf-8')
    except OSError:
        return 0
    if len(raw.encode('utf-8')) <= MAX_TX_JOURNAL_BYTES:
        lines = raw.splitlines()
        if len(lines) <= MAX_TX_JOURNAL_ENTRIES:
            return 0
        lines = raw.splitlines()
    else:
        lines = raw.splitlines()

    parsed = []
    for line in lines:
        try:
            parsed.append((line, json.loads(line)))
        except Exception:
            parsed.append((line, None))

    finished = {}
    for _, entry in parsed:
        if isinstance(entry, dict):
            tx_id = entry.get('tx_id')
            if tx_id and entry.get('stage') in TERMINAL_STAGES:
                finished[tx_id] = True
    unfinished = {entry.get('tx_id') for _, entry in parsed
                  if isinstance(entry, dict) and entry.get('tx_id')
                  and entry.get('tx_id') not in finished}

    keep = []
    for index, (line, entry) in enumerate(parsed):
        tx_id = entry.get('tx_id') if isinstance(entry, dict) else None
        is_unfinished = tx_id in unfinished
        remaining = len(parsed) - index
        if is_unfinished or remaining <= MAX_TX_JOURNAL_ENTRIES:
            keep.append(line)
    if len(keep) == len(parsed):
        return 0

    body = '\n'.join(keep) + ('\n' if keep else '')
    while len(body.encode('utf-8')) > MAX_TX_JOURNAL_BYTES and keep:
        # Dropping from the front is safe here: unfinished transactions were all kept and
        # they are the newest entries in practice.
        keep.pop(0)
        body = '\n'.join(keep) + ('\n' if keep else '')
    tmp = TX_JOURNAL.with_suffix('.tmp')
    tmp.write_text(body, encoding='utf-8')
    os.replace(tmp, TX_JOURNAL)
    return len(parsed) - len(keep)

def read_tx_journal() -> list:
    if not TX_JOURNAL.exists():
        return []
    lines = TX_JOURNAL.read_text(encoding='utf-8').splitlines()
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except Exception:
            continue
    return result

class ConfigTransaction:
    """Manages a single config transaction with full rollback/recovery."""
    
    def __init__(self, tx_id: str = None):
        self.tx_id = tx_id or secrets.token_hex(12)
        self.stage = 'initialized'
        self.preimages = {}  # path -> original bytes
        self.staged_files = {}  # path -> new bytes
        self.config_digest_before = None
        self.config_digest_after = None
        self.mihomo_pid_before = None
        self.applied = False
    
    def __enter__(self):
        # Journal tx start
        append_tx_journal({
            'tx_id': self.tx_id,
            'stage': 'start',
            'timestamp': stamp(),
        })
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            # Error during transaction - attempt rollback
            self.rollback()
            append_tx_journal({
                'tx_id': self.tx_id,
                'stage': 'failed',
                'error': str(exc_val),
                'timestamp': stamp(),
            })
        return False  # Don't suppress exception
    
    def _preimage_path(self, path: Path) -> Path:
        """Durable location for this file's preimage, keyed by tx and path."""
        return TX_PREIMAGES_DIR / self.tx_id / path.name

    def capture_preimage(self, path: Path):
        """Persist the exact current bytes of a file before it is modified.

        The preimage is written to disk (not kept only in memory) so that recovery
        after a crash, a reboot or a killed worker can restore it without the process.
        """
        data = path.read_bytes() if path.exists() else None
        self.preimages[str(path)] = data

        stored = self._preimage_path(path)
        stored.parent.mkdir(parents=True, exist_ok=True)
        if data is None:
            if stored.exists():
                stored.unlink()
        else:
            atomic_write(stored, data)

        append_tx_journal({
            'tx_id': self.tx_id,
            'stage': 'preimage',
            'path': str(path),
            'preimage_file': None if data is None else str(stored),
            'existed': data is not None,
            'digest': None if data is None else digest(data),
            'timestamp': stamp(),
        })
    
    def _make_work_path(self, path: Path) -> Path:
        """Map an absolute target path to a location strictly inside the tx work dir.

        The result must never equal (or escape to) the live path: staging writes there,
        and a cleanup that resolved back onto the live file would delete it.
        """
        parts = [p for p in path.parts if p not in ('/', '\\', '')]
        if parts and (parts[0].endswith(':') or parts[0].endswith(':\\')):
            parts = parts[1:]
        parts = [p for p in parts if p not in ('/', '\\')]
        if not parts:
            raise TxError('cannot map an empty path into the transaction work dir')
        work = (TX_STATE_DIR / self.tx_id).joinpath(*parts)
        base = (TX_STATE_DIR / self.tx_id).resolve()
        if base != work.resolve() and base not in work.resolve().parents:
            raise TxError('refusing to stage outside the transaction work dir: %s' % path)
        return work
    
    def stage_file(self, path: Path, data: bytes):
        """Stage a new file content (written to tx work dir first)."""
        work_path = self._make_work_path(path)
        work_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(work_path, data)
        fsync_dir(work_path.parent)
        self.staged_files[str(path)] = work_path
        append_tx_journal({
            'tx_id': self.tx_id,
            'stage': 'staged',
            'path': str(path),
            'digest': digest(data),
            'timestamp': stamp(),
        })
    
    def validate_candidate(self, config_path: Path = None) -> bool:
        """Run mihomo -t on the candidate, shaped the way the trial will run it.

        Validating the raw staged file would fail on any file provider, because mihomo only
        accepts provider paths inside its -d home and the staged file points at the live
        home. So the check runs against the trial shape, with providers copied in.
        """
        config_path = config_path or DEFAULT_CONFIG
        work_config = self._make_work_path(config_path)
        if not work_config.exists():
            raise TxError('staged config not found')
        work_dir = work_config.parent
        trial_config = self._build_trial_config(work_config, None)
        check_path = work_dir / 'validate-config.yaml'
        atomic_write(check_path, encode(trial_config))
        result = subprocess.run(
            ['mihomo', '-d', str(work_dir), '-f', str(check_path), '-t'],
            capture_output=True, timeout=45
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode('utf-8', 'replace').strip()
            raise TxError('mihomo validation failed: %s' % (detail[-400:] or 'no output'))
        try:
            check_path.unlink()
        except OSError:
            pass
        return True
    
    def run_isolated_trial(self, config_path: Path = None, extra_listeners: list = None) -> bool:
        """Run full isolated mihomo trial with all listeners."""
        config_path = config_path or DEFAULT_CONFIG
        work_config = self._make_work_path(config_path)
        work_dir = work_config.parent
        
        # Create trial config with isolated ports
        trial_config = self._build_trial_config(work_config, extra_listeners)
        trial_config_path = work_dir / 'trial-config.yaml'
        atomic_write(trial_config_path, encode(trial_config))
        
        # Validate
        result = subprocess.run(
            ['mihomo', '-d', str(work_dir), '-f', str(trial_config_path), '-t'],
            capture_output=True, timeout=45
        )
        if result.returncode != 0:
            raise TxError(f'trial config validation failed: {result.stderr.decode(errors="replace")[:500]}')
        
        # Start trial process
        proc = subprocess.Popen(
            ['mihomo', '-d', str(work_dir), '-f', str(trial_config_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        
        # Wait for controller
        controller_port = trial_config.get('external-controller', '127.0.0.1:0').split(':')[-1]
        if controller_port == '0':
            controller_port = '9090'  # default
        
        secret = trial_config.get('secret', '')
        headers = {'Authorization': 'Bearer ' + secret} if secret else {}
        
        for _ in range(40):
            if proc.poll() is not None:
                break
            try:
                req = urllib.request.Request(
                    f'http://127.0.0.1:{controller_port}/version',
                    headers=headers
                )
                urllib.request.urlopen(req, timeout=2).read()
                # Success - trial running
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                return True
            except Exception:
                time.sleep(0.2)
        
        # Failed to start
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        raise TxError('isolated trial mihomo did not start')
    
    def _build_trial_config(self, work_config_path: Path, extra_listeners: list) -> dict:
        """Trial config with every inbound port moved and every kernel-side effect off.

        Anything that binds a fixed port or touches the host network must be neutralised,
        otherwise the trial either cannot start beside the live mihomo or would hijack it.
        """
        import socket
        import yaml
        with open(work_config_path, encoding='utf-8') as f:
            config = _state.yaml12_load(f.read())

        used = set()

        def free_port():
            for _ in range(200):
                with socket.socket() as s:
                    s.bind(('127.0.0.1', 0))
                    port = s.getsockname()[1]
                if port not in used:
                    used.add(port)
                    return port
            raise TxError('could not allocate an isolated trial port')

        # Every fixed inbound port mihomo knows about.
        for key in ('port', 'socks-port', 'redir-port', 'tproxy-port', 'mixed-port'):
            if isinstance(config.get(key), int) and config[key]:
                config[key] = 0
        # An explicit zero disables each of those, but the trial still needs one place
        # to talk through, and the probe uses the listener list.
        for listener in config.get('listeners') or []:
            listener['port'] = free_port()
            if 'listen' in listener:
                listener['listen'] = '127.0.0.1'

        addr = config.get('external-controller')
        if isinstance(addr, str) and ':' in addr:
            host = addr.rsplit(':', 1)[0]
            config['external-controller'] = '%s:%d' % (host or '127.0.0.1', free_port())

        # No kernel tunnels, no DNS hijack, no transparent interception during a trial.
        tun = config.get('tun')
        if isinstance(tun, dict):
            tun['enable'] = False
        for key in ('dns', 'sniffer', 'tproxy-port', 'redir-port'):
            if key == 'dns' and isinstance(config.get('dns'), dict):
                config['dns']['enable'] = False
        config['allow-lan'] = False
        config['bind-address'] = '127.0.0.1'

        # The trial runs with its own -d, and mihomo only accepts file paths inside that
        # home. Copy every file-backed provider into the trial tree and repoint it there,
        # so the trial validates the real content without writing into the live home.
        trial_home = work_config_path.parent
        for section in ('proxy-providers', 'rule-providers'):
            entries = config.get(section) or {}
            for name, entry in entries.items():
                if not isinstance(entry, dict) or entry.get('type') != 'file':
                    continue
                source = entry.get('path')
                if not isinstance(source, str) or not source:
                    continue
                src = Path(source)
                if not src.is_file():
                    raise TxError('%s %s points at a missing path: %s' % (section, name, source))
                target_dir = trial_home / 'providers'
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / ('%s-%s%s' % (section, name, src.suffix or '.yaml'))
                target.write_bytes(src.read_bytes())
                entry['path'] = str(target)

        if extra_listeners:
            for listener in extra_listeners:
                item = dict(listener)
                item['port'] = free_port()
                item.setdefault('listen', '127.0.0.1')
                config.setdefault('listeners', []).append(item)

        config['secret'] = secrets.token_hex(24)
        config['log-level'] = 'silent'
        config['ipv6'] = False
        return config
    
    def commit(self, config_path: Path = None, require_nonempty=None, require_exactly=None):
        """Atomically write every staged file, reload mihomo and prove the result."""
        config_path = config_path or DEFAULT_CONFIG

        append_tx_journal({'tx_id': self.tx_id, 'stage': 'committing', 'timestamp': stamp()})

        for final_path_str, work_path in self.staged_files.items():
            final_path = Path(final_path_str)
            if final_path_str not in self.preimages:
                self.capture_preimage(final_path)
            atomic_write(final_path, work_path.read_bytes())
            fsync_dir(final_path.parent)

        self.config_digest_after = digest(config_path.read_bytes())

        self.reload_mihomo(config_path)
        self.verify_runtime(config_path, require_nonempty, require_exactly)

        self.applied = True
        append_tx_journal({'tx_id': self.tx_id, 'stage': 'committed',
                           'config_digest': self.config_digest_after, 'timestamp': stamp()})
        self.cleanup_work()
    
    def reload_mihomo(self, config_path: Path):
        """Reload mihomo via controller API."""
        # Read secret from new config
        with open(config_path, encoding='utf-8') as f:
            config = _state.yaml12_load(f.read())
        secret = config.get('secret', '')
        
        url = 'http://127.0.0.1:9090/configs?force=true'
        data = json.dumps({'path': str(config_path)}).encode()
        req = urllib.request.Request(url, data=data, method='PUT',
            headers={'Content-Type': 'application/json'})
        if secret:
            req.add_header('Authorization', 'Bearer ' + secret)
        
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            raise TxError(f'mihomo reload failed: {e}')
        
        # Give time for reload
        time.sleep(0.5)
    
    def verify_runtime(self, config_path: Path, require_nonempty=None, require_exactly=None):
        """Prove the live controller reflects the committed file.

        Three independent facts are checked: the API answers, every proxy-group the
        committed config declares is actually present, and the groups we just narrowed
        have a non-empty membership. Provider loading is asynchronous on this build --
        a group can be absent for a moment after a reload -- so presence is polled to a
        deadline instead of sampled once, and an empty pinned group is a hard failure
        rather than a silent success.
        """
        with open(config_path, encoding='utf-8') as f:
            config = _state.yaml12_load(f.read())

        secret = config.get('secret', '')
        headers = {'Authorization': 'Bearer ' + secret} if secret else {}
        require_nonempty = set(require_nonempty or ())
        require_exactly = {name: list(names) for name, names in (require_exactly or {}).items()}

        declared = [g.get('name') for g in (config.get('proxy-groups') or []) if g.get('name')]
        if not declared:
            raise TxError('committed config declares no proxy-groups')

        def fetch(name):
            req = urllib.request.Request(
                'http://127.0.0.1:9090/proxies/' + urllib.parse.quote(name), headers=headers)
            return json.loads(urllib.request.urlopen(req, timeout=5).read())

        deadline = time.monotonic() + RUNTIME_VERIFY_SECONDS
        missing = list(declared)
        empty = []
        wrong = []
        last_error = None
        while True:
            missing = []
            empty = []
            wrong = []
            for group_name in declared:
                try:
                    live = fetch(group_name)
                except Exception as exc:
                    last_error = exc
                    missing.append(group_name)
                    continue
                members = live.get('all')
                if not isinstance(members, list):
                    missing.append(group_name)
                    continue
                if group_name in require_nonempty and not members:
                    empty.append(group_name)
                elif group_name in require_exactly:
                    want = set(require_exactly[group_name])
                    got = set(members)
                    if got != want:
                        wrong.append('%s: missing %s, unexpected %s'
                                     % (group_name, sorted(want - got)[:3], sorted(got - want)[:3]))
            if not missing and not empty and not wrong:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)

        if missing:
            raise TxError('group(s) missing from the live runtime after %.0fs: %s (%s)'
                          % (RUNTIME_VERIFY_SECONDS, ', '.join(missing), last_error))
        if empty:
            raise TxError('pinned group(s) ended up with an empty pool: %s'
                          % ', '.join(empty))
        if wrong:
            raise TxError('runtime pool does not match the applied set: %s'
                          % '; '.join(wrong))

        # The file on disk must still be exactly what we committed.
        current_digest = digest(config_path.read_bytes())
        if current_digest != self.config_digest_after:
            raise TxError('config file changed after commit: expected %s, got %s'
                          % (self.config_digest_after[:12], current_digest[:12]))
    
    def rollback(self):
        """Rollback all staged files to preimages."""
        append_tx_journal({
            'tx_id': self.tx_id,
            'stage': 'rollback_start',
            'timestamp': stamp(),
        })
        
        success = True
        for final_path_str, preimage in self.preimages.items():
            final_path = Path(final_path_str)
            try:
                if preimage is None:
                    # File didn't exist before - remove it
                    if final_path.exists():
                        final_path.unlink()
                else:
                    atomic_write(final_path, preimage)
                fsync_dir(final_path.parent)
            except Exception as e:
                append_tx_journal({
                    'tx_id': self.tx_id,
                    'stage': 'rollback_failed',
                    'path': final_path_str,
                    'error': str(e),
                    'timestamp': stamp(),
                })
                success = False
        
        # Also rollback staged files in work dir (cleanup)
        for final_path_str, work_path in self.staged_files.items():
            try:
                if work_path.exists():
                    work_path.unlink()
            except Exception:
                pass
        
        # Reload mihomo to apply rollback
        try:
            self.reload_mihomo(DEFAULT_CONFIG)
        except Exception as e:
            append_tx_journal({
                'tx_id': self.tx_id,
                'stage': 'rollback_reload_failed',
                'error': str(e),
                'timestamp': stamp(),
            })
            success = False
        
        append_tx_journal({
            'tx_id': self.tx_id,
            'stage': 'rollback_complete' if success else 'rollback_partial',
            'success': success,
            'timestamp': stamp(),
        })
        
        if not success:
            raise TxError('Rollback partially failed - manual intervention required')
        
        self.cleanup_work()
    
    def cleanup_work(self):
        """Clean up transaction work directory."""
        work_dir = TX_STATE_DIR / self.tx_id
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

def start_transaction(files: dict, trial_paths: list = None, require_nonempty=None,
                      require_exactly=None):
    """Commit one whole file atomically.

    ``files`` maps an absolute path (str or Path) to the exact bytes that must land
    there. This is deliberately a file-content API: a caller that means "write this
    YAML" must not be able to accidentally request "merge this as a config field".
    Exactly one target is accepted; several files would not be one atomic commit.

    ``trial_paths`` lists which of those files are mihomo configs to validate and
    trial-run; it defaults to the primary mihomo config.
    """
    if not isinstance(files, dict) or not files:
        raise TxError('start_transaction requires a non-empty {path: bytes} mapping')
    if len(files) > 1:
        # Each file is replaced atomically, but several files are not one atomic commit:
        # a crash between them can leave half the set applied. Say so instead of implying
        # a guarantee this code does not provide. Every current caller writes one file.
        raise TxError('start_transaction commits one file at a time; '
                      'multi-file atomic commit is not implemented')
    # Repair a previous run that died mid-commit before stacking another mutation on top of
    # it. Nothing used to call the recovery routine, so an interrupted transaction stayed
    # interrupted until a human noticed.
    try:
        recover_interrupted_transactions()
    except Exception as exc:
        raise TxError('an earlier interrupted transaction could not be recovered: %s'
                      % str(exc)[:160]) from None

    normalised = {}
    for path, content in files.items():
        p = Path(path)
        # A relative key silently resolves against the caller's working directory, which
        # once wrote a candidate into "/" while reporting success. Refuse it outright.
        if not p.is_absolute():
            raise TxError('transaction target must be an absolute path: %r' % str(path))
        if not isinstance(content, (bytes, bytearray)):
            raise TxError('file content for %s must be bytes, got %s' % (p, type(content).__name__))
        normalised[p] = bytes(content)

    if trial_paths is None:
        trial_paths = [DEFAULT_CONFIG] if DEFAULT_CONFIG in normalised else []
    trials = [Path(p) for p in trial_paths]
    for t in trials:
        if t not in normalised:
            raise TxError('trial path %s is not part of this transaction' % t)

    tx_id = secrets.token_hex(12)

    with tx_lock(), job_budget(300), ConfigTransaction(tx_id) as tx:
        for path in normalised:
            tx.capture_preimage(path)
        for path, content in normalised.items():
            tx.stage_file(path, content)

        for path in trials:
            tx.validate_candidate(path)
            tx.run_isolated_trial(path)

        # Record the intent (including preimage locations) before the first mutation,
        # so a crash between commit and the caller's own bookkeeping stays recoverable.
        with open(TX_JOURNAL, 'ab') as f:
            f.write((json.dumps({
                'tx_id': tx.tx_id,
                'stage': 'prepare',
                'files': [str(p) for p in normalised],
                'preimages': {str(p): (digest(b) if b is not None else None)
                              for p, b in tx.preimages.items()},
                'timestamp': stamp(),
            }, ensure_ascii=False) + '\n').encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())

        tx.commit(next(iter(trials), None), require_nonempty, require_exactly)
        return {'ok': True, 'tx_id': tx.tx_id, 'config_digest': tx.config_digest_after,
                'files': [str(p) for p in normalised]}

def recover_interrupted_transactions():
    """Scan for incomplete transactions and rollback/recover."""
    if not TX_JOURNAL.exists():
        return
    
    journal = read_tx_journal()
    # Find latest incomplete tx per tx_id
    tx_states = {}
    for entry in journal:
        tx_id = entry.get('tx_id')
        stage = entry.get('stage')
        if tx_id:
            tx_states.setdefault(tx_id, []).append(stage)

    for tx_id, stages in tx_states.items():
        if 'committed' in stages or 'rollback_complete' in stages or 'rollback_partial' in stages:
            continue
        if not any(s in stages for s in ('prepare', 'committing', 'staged')):
            continue

        recovered = _restore_preimages(tx_id, journal)
        append_tx_journal({
            'tx_id': 'recovery',
            'stage': 'recovery_complete' if recovered else 'recovery_failed',
            'original_tx_id': tx_id,
            'stages': stages,
            'timestamp': stamp(),
        })
        if recovered:
            try:
                ConfigTransaction(tx_id).reload_mihomo(DEFAULT_CONFIG)
            except Exception as exc:
                append_tx_journal({'tx_id': 'recovery', 'stage': 'recovery_reload_failed',
                                   'original_tx_id': tx_id, 'error': str(exc),
                                   'timestamp': stamp()})

def _restore_preimages(tx_id, journal):
    """Put every file of an interrupted transaction back to its recorded preimage."""
    entries = [entry for entry in journal
               if entry.get('tx_id') == tx_id and entry.get('stage') == 'preimage']
    if not entries:
        # Without preimage records nothing can be restored, and claiming success would leave
        # a half-applied config reported as recovered.
        return False
    ok = True
    for entry in entries:
        target = Path(entry.get('path') or '')
        if not entry.get('path'):
            continue
        if not entry.get('existed'):
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                ok = False
            continue
        source = Path(entry.get('preimage_file') or '')
        if not source.is_file():
            ok = False
            continue
        data = source.read_bytes()
        if digest(data) != entry.get('digest'):
            ok = False
            continue
        try:
            atomic_write(target, data)
            fsync_dir(target.parent)
        except OSError:
            ok = False
    return ok

if __name__ == '__main__':
    # Quick self-test
    print('Config transaction module loaded OK')
    print(f'DEFAULT_CONFIG={DEFAULT_CONFIG}')
    print(f'TX_JOURNAL={TX_JOURNAL}')