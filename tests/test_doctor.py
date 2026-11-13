"""The diagnostics engine: parsers, the rule walk, logs and the domain trace.

Everything here is driven by fixture text, because the router's files are the only real
input and the checks must keep working when a file is missing or malformed.
"""
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / 'assets' / 'geomask-doctor.py'
BACKEND = ROOT / 'assets' / 'geo-backend.sh'


def load_doctor():
    spec = importlib.util.spec_from_file_location('geomask_doctor', DOCTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doctor = load_doctor()

MT_CONFIG = '''configVersion: 0.8.2
app:
  httpWeb:
    enabled: true
groups:
- id: d5fb0e47
  name: Torrents
  color: '#903a67'
  interface: Mihomo
  enable: true
  rules:
  - id: ffd41513
    name: ""
    type: namespace
    rule: thepiratebay.org
    enable: true
  - id: d1ee7f53
    name: ""
    type: namespace
    rule: rutracker.org
    enable: true
- id: 22cf88fd
  name: GoogleVideo VPN
  color: '#3e738a'
  interface: VPN-noP2P
  enable: true
  rules:
  - id: 8a7e8700
    name: ""
    type: namespace
    rule: googlevideo.com
    enable: true
  - id: 8a7e8701
    name: ""
    type: namespace
    rule: youtube.com
    enable: true
- id: 33aa11bb
  name: Disabled group
  interface: AI
  enable: false
  rules:
  - id: 99aa11bb
    name: ""
    type: namespace
    rule: openai.com
    enable: true
'''

MIHOMO_CONFIG = '''mixed-port: 7890
secret: "s3cr3t"
rule-providers:
  ai:
    type: file
    behavior: classical
    path: %(ai)s
  mtrickle:
    type: file
    behavior: classical
    path: %(mt)s
proxy-groups:
  - name: AI
  - name: VPN
rules:
  - AND,((NETWORK,UDP),(DST-PORT,443),(DOMAIN-SUFFIX,googlevideo.com)),REJECT
  - RULE-SET,ai,AI
  - DOMAIN-SUFFIX,example.com,VPN
  - IP-CIDR,203.0.113.0/24,VPN-noP2P
  - RULE-SET,mtrickle,VPN-noP2P
  - MATCH,GLOBAL
'''


class DoctorParserTests(unittest.TestCase):
    def test_magitrickle_groups_are_parsed(self):
        groups = doctor.parse_magitrickle(MT_CONFIG)
        self.assertEqual([g['name'] for g in groups],
                         ['Torrents', 'GoogleVideo VPN', 'Disabled group'])
        self.assertEqual(groups[0]['interface'], 'Mihomo')
        self.assertEqual([r['rule'] for r in groups[0]['rules']],
                         ['thepiratebay.org', 'rutracker.org'])
        self.assertFalse(groups[2]['enable'])

    def test_route_picks_the_owning_group(self):
        doctor.MT_CONF = self.write('mt.yaml', MT_CONFIG)
        group, rule, _ = doctor.magitrickle_route('rr3---sn-abc.googlevideo.com')
        self.assertEqual(group['name'], 'GoogleVideo VPN')
        self.assertEqual(rule['rule'], 'googlevideo.com')

    def test_route_ignores_a_disabled_group(self):
        doctor.MT_CONF = self.write('mt.yaml', MT_CONFIG)
        self.assertIsNone(doctor.magitrickle_route('api.openai.com'))

    def test_a_disabled_rule_is_named_not_called_missing(self):
        # the live case: the domain was listed all along, the rule was switched off
        text = ('groups:\n- id: a\n  name: Shops\n  interface: Mihomo\n  enable: true\n'
                '  rules:\n  - id: r1\n    type: namespace\n    rule: plati.market\n'
                '    enable: false\n')
        doctor.MT_CONF = self.write('mt.yaml', text)
        self.assertIsNone(doctor.magitrickle_route('plati.market'))
        group, rule = doctor.magitrickle_disabled('plati.market')
        self.assertEqual(group['name'], 'Shops')
        self.assertEqual(rule['rule'], 'plati.market')

    def test_route_prefers_the_most_specific_rule(self):
        text = ('groups:\n- id: a\n  name: G\n  interface: Mihomo\n  enable: true\n'
                '  rules:\n  - id: r1\n    type: namespace\n    rule: com\n    enable: true\n'
                '  - id: r2\n    type: namespace\n    rule: youtube.com\n    enable: true\n')
        doctor.MT_CONF = self.write('mt.yaml', text)
        _, rule, _ = doctor.magitrickle_route('www.youtube.com')
        self.assertEqual(rule['rule'], 'youtube.com')

    def write(self, name, text):
        path = Path(tempfile.mkdtemp()) / name
        path.write_text(text, encoding='utf-8')
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return str(path)

    def test_mihomo_providers_and_rules_are_parsed(self):
        cfg = self.write('cfg.yaml', MIHOMO_CONFIG % {'ai': '/tmp/ai.yaml', 'mt': '/tmp/mt.yaml'})
        providers, rules = doctor.parse_mihomo(Path(cfg).read_text(encoding='utf-8'))
        self.assertEqual(providers['ai'], '/tmp/ai.yaml')
        self.assertEqual(len(rules), 6)
        self.assertTrue(rules[0].startswith('AND,'))

    def test_rule_walk_matches_a_domain_suffix(self):
        cfg = self.write('cfg.yaml', MIHOMO_CONFIG % {'ai': '/tmp/ai.yaml', 'mt': '/tmp/mt.yaml'})
        doctor.MIHOMO_CONF = cfg
        hit = doctor.mihomo_route('www.example.com', [])
        self.assertEqual(hit['policy'], 'VPN')
        self.assertEqual(hit['index'], 3)

    def test_rule_walk_follows_a_ruleset(self):
        ai = self.write('ai.yaml', 'payload:\n  - DOMAIN-SUFFIX,openai.com\n  - DOMAIN,claude.ai\n')
        cfg = self.write('cfg.yaml', MIHOMO_CONFIG % {'ai': ai, 'mt': '/tmp/mt.yaml'})
        doctor.MIHOMO_CONF = cfg
        hit = doctor.mihomo_route('chat.openai.com', [])
        self.assertEqual(hit['policy'], 'AI')
        self.assertIn('openai.com', hit['note'])

    def test_rule_walk_matches_an_ip_rule_by_resolved_address(self):
        cfg = self.write('cfg.yaml', MIHOMO_CONFIG % {'ai': '/tmp/ai.yaml', 'mt': '/tmp/mt.yaml'})
        doctor.MIHOMO_CONF = cfg
        hit = doctor.mihomo_route('no-such-domain.example', ['203.0.113.9'])
        self.assertEqual(hit['policy'], 'VPN-noP2P')
        self.assertIn('203.0.113.9', hit['note'])

    def test_rule_walk_falls_back_to_match(self):
        cfg = self.write('cfg.yaml', MIHOMO_CONFIG % {'ai': '/tmp/ai.yaml', 'mt': '/tmp/mt.yaml'})
        doctor.MIHOMO_CONF = cfg
        hit = doctor.mihomo_route('unlisted.example', [])
        self.assertEqual(hit['policy'], 'GLOBAL')

    def test_flow_style_rules_are_parsed(self):
        # the panel/mihomo rewrite emits `- RULE,...` at column 0; the installer writes
        # `  - RULE,...`. Both must be read, or every domain "falls through" to MATCH.
        flow = ('mixed-port: 7890\nrule-providers:\n  ai:\n    type: file\n'
                '    path: %s\nrules:\n- DOMAIN-SUFFIX,example.com,VPN\n'
                '- RULE-SET,ai,AI\n- MATCH,GLOBAL\n') % '/tmp/ai.yaml'
        cfg = self.write('flow.yaml', flow)
        providers, rules = doctor.parse_mihomo(Path(cfg).read_text(encoding='utf-8'))
        self.assertEqual(len(rules), 3)
        self.assertEqual(providers['ai'], '/tmp/ai.yaml')
        doctor.MIHOMO_CONF = cfg
        self.assertEqual(doctor.mihomo_route('www.example.com', [])['policy'], 'VPN')
        self.assertEqual(doctor.mihomo_route('other.example', [])['policy'], 'GLOBAL')

    def test_quic_reject_rule_is_recognised_for_a_video_domain(self):
        cfg = self.write('cfg.yaml', MIHOMO_CONFIG % {'ai': '/tmp/ai.yaml', 'mt': '/tmp/mt.yaml'})
        doctor.MIHOMO_CONF = cfg
        hit = doctor.mihomo_route('rr1.googlevideo.com', [])
        self.assertEqual(hit['policy'], 'REJECT')

    def test_listeners_are_parsed_with_their_real_address(self):
        text = ('listeners:\n'
                '- name: ai-devices\n  type: mixed\n  port: 7891\n  listen: 127.0.0.1\n  proxy: AI\n'
                '- name: torrent-direct\n  type: mixed\n  port: 7894\n  listen: 192.168.0.1\n  proxy: VPN\n')
        items = doctor.parse_listeners(text)
        self.assertEqual([i['name'] for i in items], ['ai-devices', 'torrent-direct'])
        self.assertEqual(items[1]['listen'], '192.168.0.1')
        self.assertEqual(items[1]['proxy'], 'VPN')

    def test_domain_matching_handles_suffixes_and_exact_names(self):
        self.assertTrue(doctor.domain_matches('a.b.example.com', 'example.com'))
        self.assertFalse(doctor.domain_matches('notexample.com', 'example.com'))
        self.assertTrue(doctor.domain_matches('claude.ai', 'claude.ai', 'domain'))
        self.assertFalse(doctor.domain_matches('api.claude.ai', 'claude.ai', 'domain'))
        self.assertTrue(doctor.domain_matches('api.claude.ai', 'claude', 'keyword'))


class DoctorCountryTests(unittest.TestCase):
    def test_russian_node_names_map_to_countries(self):
        self.assertEqual(doctor.node_country('США-1')[0], 'US')
        self.assertEqual(doctor.node_country('Нидерланды-3')[0], 'NL')
        self.assertEqual(doctor.node_country('Германия-2')[0], 'DE')
        self.assertEqual(doctor.node_country(''), ('', ''))

    def test_every_country_the_backend_maps_is_known_here(self):
        text = BACKEND.read_text(encoding='utf-8')
        block = text.split('_node_cc() {', 1)[1].split('}', 1)[0]
        pairs = re.findall(r'\*([^*]+)\*\)\s*echo\s+([A-Z]{2})', block)
        self.assertGreater(len(pairs), 10)
        for name, code in pairs:
            if not name or name.startswith('🇦') or any(ord(ch) > 127 and ord(ch) < 0x2000 for ch in name):
                continue  # emoji flags and other noise
            self.assertEqual(doctor.node_country(name + '-1')[0], code,
                             'node name %r must map to %s' % (name, code))


class DoctorLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        doctor.LOG_DIR = self.tmp.name

    def test_write_log_appends_a_block(self):
        doctor.write_log('demo', 'ok', ['строка'])
        text = (Path(self.tmp.name) / 'demo.log').read_text(encoding='utf-8')
        self.assertIn('=== ', text)
        self.assertIn('ok ===', text)
        self.assertIn('строка', text)

    def test_old_blocks_are_dropped_on_write(self):
        path = Path(self.tmp.name) / 'demo.log'
        path.write_text('=== 2020-01-01T00:00:00+0300 ok ===\nстарое\n', encoding='utf-8')
        doctor.write_log('demo', 'ok', ['свежее'])
        text = path.read_text(encoding='utf-8')
        self.assertNotIn('старое', text)
        self.assertIn('свежее', text)

    def test_log_is_capped(self):
        doctor.LOG_MAX_BYTES = 2000
        for index in range(40):
            doctor.write_log('big', 'ok', ['строка %d %s' % (index, 'x' * 100)])
        size = (Path(self.tmp.name) / 'big.log').stat().st_size
        self.assertLessEqual(size, 2000)
        self.assertIn('строка 39', (Path(self.tmp.name) / 'big.log').read_text(encoding='utf-8'))

    def test_log_tail_returns_the_last_lines(self):
        for index in range(5):
            doctor.write_log('tail', 'ok', ['запись %d' % index])
        text = doctor.log_tail('tail', 3)
        self.assertIn('запись 4', text)
        self.assertNotIn('запись 0', text)


class DoctorRegistryTests(unittest.TestCase):
    def setUp(self):
        # the registry is built from the shipped catalogue, not from the router's copy
        self._index = doctor.MODULE_INDEX
        doctor.MODULE_INDEX = str(ROOT / 'modules' / 'index.json')
        doctor.CHECKS = []
        self.addCleanup(lambda: (setattr(doctor, 'MODULE_INDEX', self._index),
                                 setattr(doctor, 'CHECKS', [])))
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        doctor.LOG_DIR = self._tmp.name

    def test_registry_has_every_area(self):
        checks = doctor.build_registry()
        areas = {check['area'] for check in checks}
        self.assertEqual(areas, {'modules', 'router', 'network', 'paths'})
        ids = {check['id'] for check in checks}
        for expected in ('router_resources', 'net_dns', 'path_vpn', 'path_ai', 'path_egress'):
            self.assertIn(expected, ids)

    def test_unknown_check_is_reported_not_raised(self):
        result = doctor.run_check('nope')
        self.assertEqual(result['state'], 'fail')
        self.assertIn('нет такой проверки', result['summary'])

    def test_domain_trace_rejects_a_non_domain(self):
        result = doctor.trace_domain('not a domain')
        self.assertFalse(result['ok'])
        self.assertIn('не домен', result['error'])

    def _route_fixture(self):
        mt = Path(tempfile.mkdtemp()) / 'mt.yaml'
        mt.write_text(MT_CONFIG, encoding='utf-8')
        ai = Path(tempfile.mkdtemp()) / 'ai.yaml'
        ai.write_text('payload:\n  - DOMAIN-SUFFIX,openai.com\n', encoding='utf-8')
        cfg = Path(tempfile.mkdtemp()) / 'cfg.yaml'
        cfg.write_text(MIHOMO_CONFIG % {'ai': str(ai), 'mt': '/tmp/mt.yaml'}, encoding='utf-8')
        doctor.MT_CONF = str(mt)
        doctor.MIHOMO_CONF = str(cfg)
        doctor.LOG_DIR = tempfile.mkdtemp()

    def test_domain_trace_renders_the_route(self):
        self._route_fixture()
        result = doctor.trace_domain('chat.openai.com')
        self.assertTrue(result['ok'])
        stages = [step['stage'] for step in result['steps']]
        self.assertEqual(stages[:3], ['DNS', 'MagiTrickle', 'Правило mihomo'])
        text = '\n'.join(result['lines'])
        self.assertIn('Правило mihomo', text)
        self.assertIn('AI', text)

    def test_domain_trace_treats_a_quic_reject_as_normal(self):
        self._route_fixture()
        result = doctor.trace_domain('rr1---sn-abc.googlevideo.com')
        exit_step = [step for step in result['steps'] if step['stage'] == 'Выход'][0]
        self.assertEqual(exit_step['verdict'], 'ok')
        self.assertIn('QUIC', exit_step['detail'])

    def test_domain_trace_names_a_missing_domain(self):
        self._route_fixture()
        original = doctor.run
        self.addCleanup(lambda: setattr(doctor, 'run', original))
        doctor.run = lambda cmd, timeout=8, stdin=None: (
            (0, '** server can\'t find nope.example: NXDOMAIN')
            if cmd[:1] == ['nslookup'] else (127, ''))
        result = doctor.trace_domain('nope.example')
        self.assertEqual(result['state'], 'fail')
        self.assertIn('не существует', result['steps'][0]['detail'])
        self.assertIn('написание', result['fix'])


class DoctorCheckTests(unittest.TestCase):
    """Checks are shell-driven, so the shell answers are injected here."""

    def setUp(self):
        self._run = doctor.run
        self._index = doctor.MODULE_INDEX
        self._updater = doctor.MODULE_UPDATER
        doctor.MODULE_INDEX = str(ROOT / 'modules' / 'index.json')
        doctor.MODULE_UPDATER = str(ROOT / 'assets' / 'geomask-module-update.py')
        doctor.CHECKS = []
        self.addCleanup(lambda: (setattr(doctor, 'run', self._run),
                                 setattr(doctor, 'MODULE_INDEX', self._index),
                                 setattr(doctor, 'MODULE_UPDATER', self._updater),
                                 setattr(doctor, 'CHECKS', [])))

    def fake(self, answers):
        def call(cmd, timeout=8, stdin=None):
            for prefix, value in answers:
                if cmd[:len(prefix)] == prefix:
                    return value
                # a probe with --proxy has the same first and last element as without it
                if prefix and cmd[0] == prefix[0] and cmd[-1] == prefix[-1]:
                    return value
            return 127, ''
        doctor.run = call

    def test_netfilter_accepts_the_live_magitrickle_shape(self):
        # MagiTrickle lives in table ip mangle as MT_<group> chains - not in its own table
        self.fake([
            (['nft', 'list', 'tables'], (0, 'table inet fw4\ntable ip mangle')),
            (['nft', 'list', 'table'], (0, 'jump MT_d5fb0e47\ncomment "geomask-excl"')),
            (['ip', '-4'], (0, '17840: from 192.168.0.125 lookup 101\n'
                               '17940: from all fwmark 0x4d616780 lookup 1298229120')),
        ])
        state, summary, details = doctor.check_router_netfilter()
        self.assertEqual(state, 'ok', details)
        self.assertIn('MagiTrickle: 1 цепочек', ' '.join(details))

    def test_netfilter_fails_without_magitrickle_chains(self):
        self.fake([
            (['nft', 'list', 'tables'], (0, 'table inet fw4')),
            (['nft', 'list', 'table'], (0, '')),
            (['ip', '-4'], (0, '0: from all lookup local')),
        ])
        state, _, details = doctor.check_router_netfilter()
        self.assertEqual(state, 'fail')

    def test_netfilter_fails_without_fw4(self):
        self.fake([(['nft', 'list', 'tables'], (0, 'table ip filter'))])
        state, _, _ = doctor.check_router_netfilter()
        self.assertEqual(state, 'fail')

    def test_a_failing_check_carries_a_remedy(self):
        doctor.LOG_DIR = tempfile.mkdtemp()
        result = doctor.run_check('router_backup')
        self.assertIn(result['state'], ('warn', 'fail'))
        self.assertTrue(result['fix'], 'a non-ok check must say what to do')
        self.assertIn('бэкап', result['fix'].lower())

    def test_a_healthy_check_carries_no_remedy(self):
        doctor.LOG_DIR = tempfile.mkdtemp()
        self.fake([(['nft', 'list', 'tables'], (0, 'table inet fw4\ntable ip mangle')),
                   (['nft', 'list', 'table'], (0, 'jump MT_abc')),
                   (['ip', '-4'], (0, '17840: from all lookup 101'))])
        result = doctor.run_check('router_netfilter')
        self.assertEqual(result['state'], 'ok')
        self.assertEqual(result['fix'], '')

    def test_module_check_remedy_depends_on_the_state(self):
        self._index = doctor.MODULE_INDEX
        doctor.MODULE_INDEX = str(ROOT / 'modules' / 'index.json')
        self.addCleanup(lambda: setattr(doctor, 'MODULE_INDEX', self._index))
        state, summary, details, fix = doctor.check_module('youtube')()
        self.assertIn(state, ('fail', 'warn', 'ok'))
        if state != 'ok':
            self.assertIn('Модули', fix)

    def test_verdict_summarises_the_run(self):
        healthy = doctor.verdict([{'state': 'ok'}, {'state': 'ok'}])
        self.assertEqual(healthy['state'], 'ok')
        self.assertIn('в порядке', healthy['text'])
        warned = doctor.verdict([{'state': 'ok'}, {'state': 'warn'}])
        self.assertEqual(warned['state'], 'warn')
        broken = doctor.verdict([{'state': 'ok'}, {'state': 'fail'}, {'state': 'warn'}])
        self.assertEqual(broken['state'], 'fail')
        self.assertEqual(broken['counts'], {'ok': 1, 'warn': 1, 'fail': 1})
        self.assertIn('что делать', broken['text'])

    def test_egress_country_survives_a_blocking_geo_service(self):
        # the AI lane's exit gets 403 from ipinfo.io; the next service must answer
        answers = [
            (['curl', '-m', '8', '-s', 'https://ipinfo.io/json'],
             (0, '<html><h1>Error: Forbidden</h1>')),
            (['curl', '-m', '8', '-s', 'https://api.myip.com'],
             (0, '{"ip":"1.2.3.4","country":"NL","cc":"NL"}')),
        ]
        self.fake(answers)
        self.assertEqual(doctor.fetch_country(None), 'NL')

    def test_egress_country_reads_iso_names(self):
        self.fake([(['curl', '-m', '8', '-s', 'https://ipinfo.io/json'],
                    (0, '{"country":"US"}'))])
        self.assertEqual(doctor.fetch_country('http://127.0.0.1:7891'), 'US')

    def test_egress_country_gives_up_cleanly(self):
        self.fake([(['curl', '-m', '8', '-s', 'https://ipinfo.io/json'], (0, 'nonsense'))])
        self.assertEqual(doctor.fetch_country(None), '')


class DoctorRelatedHostsTests(unittest.TestCase):
    """The page's own dependencies: redirect hops, links, forms and the certificate."""

    def setUp(self):
        self._saved = {name: getattr(doctor, name)
                       for name in ('run', 'read_text', 'magitrickle_route',
                                    'mihomo_route', 'group_state')}
        self.addCleanup(lambda: [setattr(doctor, name, value)
                                 for name, value in self._saved.items()])

    def test_host_of_handles_the_shapes_html_produces(self):
        cases = {
            'https://digiseller.ru/pay?x=1': 'digiseller.ru',
            '//cdn.example.com/a.js': 'cdn.example.com',
            'http://example.com:8080/x': 'example.com',
            'digiseller.ru': 'digiseller.ru',
            '/local/path': '',
            'mailto:help@example.com': '',
            '#anchor': '',
            '': '',
        }
        for url, expected in cases.items():
            self.assertEqual(doctor.host_of(url), expected, url)

    def test_related_hosts_reads_hops_links_and_certificate(self):
        page = Path(tempfile.mkdtemp()) / 'page.html'
        page.write_text(
            '<a href="https://digiseller.ru/cart">cart</a>'
            '<script src="//static.plati.io/app.js"></script>'
            '<form action="https://pay.plati.io/submit"></form>'
            '<a href="/local">local</a>'
            '<a href="https://plati.market/self">self</a>', encoding='utf-8')
        hops = Path(tempfile.mkdtemp()) / 'hops.txt'
        hops.write_text('HTTP/2 301\r\nlocation: https://www.plati.market/\r\n', encoding='utf-8')
        answers = {
            'curl': (0, ''),
            'sh': (0, 'X509v3 Subject Alternative Name:\n    DNS:plati.market, DNS:*.digiseller.ru, DNS:digiseller.com\n'),
        }

        def fake_run(cmd, timeout=8, stdin=None):
            if cmd[0] == 'curl' and '-D' in cmd:
                return 0, ''
            return answers.get(cmd[0], (0, ''))

        doctor.run = fake_run
        doctor.read_text = lambda path, default='': (
            page.read_text(encoding='utf-8') if path.endswith('.html')
            else hops.read_text(encoding='utf-8') if path.endswith('.txt')
            else default)
        hosts = {item['host']: item for item in doctor.related_hosts('plati.market')}
        self.assertIn('digiseller.ru', hosts)
        self.assertIn('a', hosts['digiseller.ru']['kinds'])
        self.assertIn('static.plati.io', hosts)
        self.assertIn('script', hosts['static.plati.io']['kinds'])
        self.assertIn('pay.plati.io', hosts)
        self.assertTrue(hosts['pay.plati.io']['session'])     # a form carries the session
        self.assertFalse(hosts['static.plati.io']['session'])  # a script does not
        self.assertIn('digiseller.com', hosts)                 # from the certificate
        self.assertNotIn('plati.market', hosts)                # the domain itself is not a dependency
        self.assertNotIn('www.plati.market', hosts)

    def test_http_probe_names_a_timeout_instead_of_rc_28(self):
        doctor.run = lambda cmd, timeout=8, stdin=None: (28, '')
        probe = doctor.http_probe('plati.market')
        self.assertFalse(probe['ok'])
        self.assertEqual(probe['kind'], 'timeout')
        self.assertIn('нет ответа за 20 с', probe['detail'])

    def test_http_probe_keeps_the_status_of_a_real_answer(self):
        # a 4xx is the site answering (404 on a game host is normal): the path works
        doctor.run = lambda cmd, timeout=8, stdin=None: (0, '403 0.4 1.2.3.4')
        probe = doctor.http_probe('plati.market')
        self.assertTrue(probe['ok'])
        self.assertEqual(probe['code'], '403')
        self.assertIn('1.2.3.4', probe['detail'])

    def test_a_server_error_is_not_a_working_path(self):
        doctor.run = lambda cmd, timeout=8, stdin=None: (0, '503 1.9 1.2.3.4')
        probe = doctor.http_probe('plati.market')
        self.assertFalse(probe['ok'])
        self.assertEqual(probe['kind'], 'http')

    def test_host_route_reports_a_direct_exit_as_russia(self):
        doctor.magitrickle_route = lambda host: None
        doctor.mihomo_route = lambda host, ips: {'policy': 'DIRECT', 'rule': 'MATCH'}
        info = doctor.host_route('example.com')
        self.assertEqual(info['policy'], 'DIRECT')
        self.assertEqual(info['cc'], 'RU')

    def test_host_route_follows_the_group_of_a_tunnelled_host(self):
        doctor.magitrickle_route = lambda host: ({'name': 'To VPN'}, {'rule': host}, '')
        doctor.mihomo_route = lambda host, ips: {'policy': 'VPN', 'rule': 'RULE-SET,mtrickle,VPN'}
        doctor.group_state = lambda name: {'now': 'NL-1', 'cc': 'NL', 'country': 'Нидерланды'}
        info = doctor.host_route('digiseller.ru')
        self.assertEqual(info['group'], 'To VPN')
        self.assertEqual(info['cc'], 'NL')
        self.assertEqual(info['country'], 'Нидерланды')


class DoctorStalePageTests(unittest.TestCase):
    """A failed fetch must not report the previous site's hosts as this domain's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.saved = {name: getattr(doctor, name) for name in ('run', 'PAGE_FILE', 'HOPS_FILE')}
        self.addCleanup(lambda: [setattr(doctor, name, value)
                                 for name, value in self.saved.items()])
        doctor.PAGE_FILE = str(base / 'page.html')
        doctor.HOPS_FILE = str(base / 'hops.txt')
        # what the previous trace left behind
        Path(doctor.PAGE_FILE).write_text(
            '<html><body><img src="https://shop.example.net/a.png">'
            '<a href="https://pay.example.net/">pay</a></body></html>', encoding='utf-8')
        Path(doctor.HOPS_FILE).write_text('HTTP/1.1 200 OK\n', encoding='utf-8')

    def test_a_failed_request_reports_no_hosts(self):
        doctor.run = lambda cmd, timeout=8, stdin=None: (6, '')
        self.assertEqual(doctor.related_hosts('nope-not-in-any-list.org'), [])

    def test_a_real_page_still_yields_its_hosts(self):
        def fake(cmd, timeout=8, stdin=None):
            if cmd[0] == 'curl':
                Path(doctor.PAGE_FILE).write_text(
                    '<html><body><img src="https://cdn.example.net/x.png"></body></html>',
                    encoding='utf-8')
                Path(doctor.HOPS_FILE).write_text('HTTP/1.1 200 OK\n', encoding='utf-8')
                return 0, ''
            return 0, ''
        doctor.run = fake
        hosts = doctor.related_hosts('shop.example.com')
        self.assertEqual([item['host'] for item in hosts], ['cdn.example.net'])


class DoctorZapretTests(unittest.TestCase):
    """Whether zapret (nfqws) processes a domain - the trace used to stop before this."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)

        def put(name, body):
            path = base / name
            path.write_text(body, encoding='utf-8')
            return str(path)

        self.saved = {name: getattr(doctor, name)
                      for name in ('run', 'ZAPRET_CONF', 'ZAPRET_HOSTLIST',
                                   'ZAPRET_EXCLUDE', 'ZAPRET_AUTO')}
        self.addCleanup(lambda: [setattr(doctor, name, value)
                                 for name, value in self.saved.items()])
        doctor.ZAPRET_CONF = put('config', 'MODE_FILTER=autohostlist\n')
        doctor.ZAPRET_HOSTLIST = put('hosts.txt', 'example.com\n'
                                     'live.wardogs.bulkhead.pragmaengine.com\n')
        doctor.ZAPRET_EXCLUDE = put('exclude.txt', '# nochange\nru\n')
        doctor.ZAPRET_AUTO = put('auto.txt', '')

    def running(self, pids='571 572'):
        doctor.run = lambda cmd, timeout=8, stdin=None: (
            (0, pids) if cmd[0] == 'pidof' else (127, ''))

    def test_an_exact_host_from_the_hostlist_is_processed(self):
        self.running()
        info = doctor.zapret_route('live.wardogs.bulkhead.pragmaengine.com')
        self.assertTrue(info['running'])
        self.assertEqual(info['mode'], 'autohostlist')
        self.assertEqual(info['listed'], 'live.wardogs.bulkhead.pragmaengine.com')

    def test_a_subdomain_matches_its_parent_line(self):
        self.running()
        self.assertEqual(doctor.zapret_route('cdn.example.com')['listed'], 'example.com')

    def test_a_tld_line_in_the_exclude_list_wins(self):
        # the owner excludes `ru` on purpose: every .ru domain skips the desync
        self.running()
        info = doctor.zapret_route('plati.ru')
        self.assertEqual(info['excluded'], 'ru')
        self.assertEqual(info['listed'], '')

    def test_an_unlisted_domain_is_reported_as_uncovered(self):
        self.running()
        info = doctor.zapret_route('nope.org')
        self.assertEqual((info['listed'], info['excluded'], info['auto']), ('', '', False))

    def test_stopped_nfqws_is_visible(self):
        doctor.run = lambda cmd, timeout=8, stdin=None: (1, '')
        self.assertFalse(doctor.zapret_route('example.com')['running'])


if __name__ == '__main__':
    unittest.main()
