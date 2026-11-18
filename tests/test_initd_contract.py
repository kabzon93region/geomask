"""D18: the procd init scripts and the sync watchdog must fail loudly, not silently.

The R10 analysis showed that a missing `/usr/bin/geomask-syncd.sh` was `return 0`, that
`inotifywait`'s exit code was ignored (a failed watch made the loop spin every 4 seconds),
that `dnsmasq-ai` respawned forever without `/etc/dnsmasq-ai.conf`, and that neither AI
service kept its stderr. This is a static contract: the scripts are read, never executed and
never require `procd`, `ip` or a router. Real procd/ifup behaviour is Linux-only.

Run: python -B -m unittest test_initd_contract  (from tests/, PYTHONPATH=tests)
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets'


def asset(name):
    path = ASSETS / name
    if not path.is_file():
        raise AssertionError('missing asset %s' % path)
    return path.read_text(encoding='utf-8')


class SyncdInitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = asset('geomask-syncd.init')

    def test_a_missing_watchdog_script_is_logged_and_fails(self):
        match = re.search(
            r'\[\s*-x\s+/usr/bin/geomask-syncd\.sh\s*\]\s*\|\|\s*\{(?P<body>[^}]*)\}',
            self.text, re.S)
        self.assertIsNotNone(
            match, 'geomask-syncd.init no longer guards /usr/bin/geomask-syncd.sh with '
                   '`[ -x ... ] || { ... }`')
        body = match.group('body')
        self.assertIn('logger -t geomask-syncd', self.text,
                      'the watchdog must log through `logger -t geomask-syncd`')
        self.assertIn('log "', body,
                      'the missing-script branch must write to syslog: %r' % body)
        self.assertIn('return 1', body,
                      'the missing-script branch must fail, not report success: %r' % body)
        self.assertNotIn('return 0', body,
                         'the missing-script branch still returns success: %r' % body)
        self.assertNotIn('|| return 0', self.text,
                         'a missing watchdog script must not be a silent success')

    def test_the_instance_keeps_stderr_in_syslog(self):
        self.assertIn('procd_set_param stderr 1', self.text,
                      'procd_set_param stderr 1 is what makes the watchdog errors visible')


class DnsmasqAiInitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = asset('dnsmasq-ai.init')

    def test_a_missing_config_is_logged_and_fails(self):
        self.assertIn('CONF=/etc/dnsmasq-ai.conf', self.text,
                      'dnsmasq-ai.init must name the config file it needs')
        match = re.search(
            r'\[\s*-f\s+"\$CONF"\s*\]\s*\|\|\s*\{(?P<body>[^}]*)\}', self.text, re.S)
        self.assertIsNotNone(
            match, 'dnsmasq-ai.init must check `[ -f "$CONF" ]` before starting procd')
        body = match.group('body')
        self.assertIn('logger -t dnsmasq-ai', self.text,
                      'dnsmasq-ai must log through `logger -t dnsmasq-ai`')
        self.assertIn('log "', body,
                      'the missing-config branch must write to syslog: %r' % body)
        self.assertIn('return 1', body,
                      'the missing-config branch must fail instead of a silent respawn: %r'
                      % body)
        self.assertNotIn('procd_open_instance', body,
                         'procd must not be opened without a config file')

    def test_the_config_is_tracked_and_stderr_is_kept(self):
        self.assertIn('procd_set_param file "$CONF"', self.text,
                      'a rewritten /etc/dnsmasq-ai.conf must restart the instance')
        self.assertIn('procd_set_param stderr 1', self.text,
                      'the dnsmasq-ai stderr must reach syslog')

    def test_the_check_runs_before_procd_open_instance(self):
        self.assertIn('logger -t dnsmasq-ai', self.text)
        self.assertIn('procd_open_instance', self.text)
        self.assertLess(self.text.index('logger -t dnsmasq-ai'),
                        self.text.index('procd_open_instance'),
                        'the config check must run before the instance is declared')


class AiDohInitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = asset('ai-doh.init')

    def test_the_ai_tunnel_and_listener_are_checked(self):
        self.assertIn('AI_TUN=AI', self.text)
        self.assertIn('/sys/class/net/$AI_TUN', self.text,
                      'the AI TUN must be checked (sysfs) before the worker is declared')
        self.assertIn('127.0.0.1:7891', self.text,
                      'the worker depends on mihomo ai-devices on 127.0.0.1:7891')
        self.assertIn('logger -t ai-doh', self.text,
                      'a missing dependency must be visible in syslog')

    def test_the_dependency_check_runs_at_start(self):
        self.assertIn('check_deps', self.text)
        self.assertIn('procd_open_instance', self.text)
        self.assertLess(self.text.index('check_deps\n'),
                        self.text.index('procd_open_instance'),
                        'check_deps() must be called before procd_open_instance')

    def test_stderr_is_kept(self):
        self.assertIn('procd_set_param stderr 1', self.text,
                      'the DoH worker errors must reach syslog')


class SyncdWatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = asset('geomask-syncd.sh')

    def test_inotifywait_exit_code_is_checked(self):
        match = re.search(
            r'_iw_out=\$\(inotifywait\b[^)]*\)\s*_iw_rc=\$\?', self.text)
        self.assertIsNotNone(
            match, 'geomask-syncd.sh must keep the exit code of inotifywait '
                   '(`_iw_out=$(inotifywait ...)` followed by `_iw_rc=$?`)')
        self.assertIn('[ "$_iw_rc" -ne 0 ]', self.text,
                      'the saved inotifywait rc is never compared')

    def test_a_failed_watch_backs_off_instead_of_spinning(self):
        match = re.search(
            r'if \[ "\$_iw_rc" -ne 0 \]; then(?P<body>.*?)\n    fi', self.text, re.S)
        self.assertIsNotNone(match,
                             'the inotifywait failure branch is missing or its shape changed')
        body = match.group('body')
        self.assertIn('log "inotifywait rc=', body,
                      'a failed watch must be logged: %r' % body)
        self.assertIn('logger -t geomask-syncd', self.text,
                      'the watchdog must log through `logger -t geomask-syncd`')
        self.assertIn('sleep "$BACKOFF"', body,
                      'a failed watch must sleep a backoff, not retry at once: %r' % body)
        self.assertIn('continue', body,
                      'a failed watch must skip the sync/sleep-4s path: %r' % body)
        self.assertNotIn('sleep "$DEB"', body,
                         'the failed-watch branch must not use the normal 4s debounce')
        self.assertIn('BACKOFF=15', self.text)
        self.assertRegex(self.text, r'BACKOFF\s*\*\s*2',
                         'the backoff must grow, otherwise a broken watch is a 15s spin')

    def test_a_successful_watch_resets_the_backoff(self):
        self.assertIn('BACKOFF=15', self.text,
                      'the watchdog must define the initial backoff')
        first = self.text.index('BACKOFF=15')
        after = self.text[first + len('BACKOFF=15'):]
        self.assertIn('BACKOFF=15', after,
                      'BACKOFF must be reset after a successful inotifywait')

    def test_startup_reconciles_before_waiting_for_an_event(self):
        """P11: an already-up router had no event to wait for."""
        self.assertIn('run_catchup', self.text)
        startup = self.text.split('watchdog started', 1)[1].split('while true', 1)[0]
        self.assertIn('run_catchup', startup,
                      'the watchdog waits for an event before it ever syncs')

    def test_a_write_during_the_sync_is_applied_without_a_second_event(self):
        """P11: the blind window - the change landed while the sync was reading."""
        self.assertIn('_before=$(digest)', self.text)
        self.assertIn('_after=$(digest)', self.text)
        self.assertIn('CATCHUP_MAX', self.text)
        self.assertIn('state changed during the sync', self.text)

    def test_the_fallback_digest_sees_content_not_mtime(self):
        """`ls -l` cannot see a same-size rewrite in the same minute."""
        self.assertIn('md5sum', self.text)
        self.assertNotIn('ls -l "$DIR"', self.text)

if __name__ == '__main__':
    unittest.main()
