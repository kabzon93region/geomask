"""ytwatch: stalled-edge classifier (v1.15.2).

DPI can kill a YouTube stream AFTER the TCP handshake succeeds. mihomo then logs no dial
error, so the log scan cannot see it. The controller API still shows the connection as
"alive a long time, almost nothing downloaded" - that is what is_stalled() detects.
"""
import calendar
import re
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'assets' / 'geomask-ytwatch.sh').read_text(encoding='utf-8')
BODY = SRC.split("<<'PYEOF'\n", 1)[1].rsplit('\nPYEOF', 1)[0]


def load_funcs(names, **cfg):
    """extract selected top-level defs from the embedded script and exec them"""
    ns = {'calendar': calendar, 'time': time, 're': re}
    ns.update(cfg)
    for n in names:
        m = re.search(r'(?ms)^def %s\(.*?(?=^\S|\Z)' % re.escape(n), BODY)
        if not m:
            raise AssertionError('function %s not found in the embedded script' % n)
        exec(m.group(0), ns)
    return ns


NOW = calendar.timegm(time.strptime('2026-10-31T11:00:00', '%Y-%m-%dT%H:%M:%S'))


def conn(host='rr7---sn-xguxaxjvh-bvwe.googlevideo.com', age=60, download=1200, chains=('DIRECT',)):
    start = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(NOW - age)) + '.029566064Z'
    return {'start': start, 'download': download, 'upload': 900,
            'chains': list(chains), 'metadata': {'host': host}}


class StallClassifierTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_funcs(['utc_age', 'is_stalled'], stall_age=25, stall_bytes=10000)
        self.is_stalled = self.ns['is_stalled']

    def test_utc_age_parses_fractional_z(self):
        c = conn(age=90)
        self.assertAlmostEqual(self.ns['utc_age'](c['start'], NOW), 90, delta=1)

    def test_stalled_edge_is_flagged(self):
        self.assertTrue(self.is_stalled(conn(), NOW))

    def test_fresh_connection_is_not_stalled(self):
        self.assertFalse(self.is_stalled(conn(age=5), NOW))

    def test_connection_with_traffic_is_not_stalled(self):
        self.assertFalse(self.is_stalled(conn(download=4_000_000), NOW))

    def test_non_direct_chain_is_ignored(self):
        # already off DIRECT - pinning it again would be pointless
        self.assertFalse(self.is_stalled(conn(chains=('AI[USA 6]',)), NOW))
        self.assertFalse(self.is_stalled(conn(chains=('GLOBAL',)), NOW))

    def test_other_hosts_are_ignored(self):
        self.assertFalse(self.is_stalled(conn(host='www.youtube.com'), NOW))
        self.assertFalse(self.is_stalled(conn(host='i.ytimg.com'), NOW))

    def test_missing_fields_do_not_crash(self):
        self.assertFalse(self.is_stalled({}, NOW))
        self.assertFalse(self.is_stalled({'metadata': {}}, NOW))


class WiringTests(unittest.TestCase):
    def test_thresholds_are_configurable(self):
        self.assertIn('stall_min_age', BODY)
        self.assertIn('stall_max_bytes', BODY)
        self.assertIn('stall_threshold', BODY)
        conf = (ROOT / 'assets' / 'geomask-ytwatch.conf').read_text(encoding='utf-8')
        for k in ('stall_min_age=', 'stall_max_bytes=', 'stall_threshold='):
            self.assertIn(k, conf)

    def test_consecutive_sighting_tracking_exists(self):
        # one sample must not pin an edge permanently: require stall_threshold samples
        self.assertIn("st['stall_seen']", BODY)
        self.assertRegex(BODY, r'for h,n in fresh\.items\(\)')

    def test_status_exposes_stalled_for_the_panel(self):
        self.assertRegex(BODY, r"out\['stalled'\]")
        page = (ROOT / 'assets' / 'geo-sync-page.js').read_text(encoding='utf-8')
        self.assertIn('yt.stalled', page)
        self.assertIn('Зависшие рёбра', page)

    def test_reset_clears_stall_counters(self):
        self.assertRegex(BODY, r"st\['stall_seen'\]=\{\}")


if __name__ == '__main__':
    unittest.main()
