"""The real backend must keep every assignment after an apply.

This drives assets/geo-backend.sh itself (sourced, stubbed PATH) instead of grepping it:
the defect was a text parser that agreed with the tests but disagreed with PyYAML output.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml

HERE = Path(__file__).resolve().parents[1] / 'assets'
BACKEND = HERE / 'geo-backend.sh'

SUB_A = 'https://a.example.invalid/sub'
SUB_B = 'https://b.example.invalid/sub'
SUB_C = 'https://c.example.invalid/sub'

STUB = '#!/bin/sh\nexit 1\n'


def find_bash():
    for candidate in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe',
                      r'C:\Program Files\Git\usr\bin\bash.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


class BackendStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('curl', 'ip', 'netstat', 'crontab', 'uci', 'pidof', 'mihomo', 'logger'):
            path = self.bin / name
            path.write_text(STUB, encoding='utf-8', newline='\n')
            path.chmod(0o755)
        # The backend calls `python3` from PATH (rpcd's PATH has /usr/bin on the router).
        self.python3 = self.bin / 'python3'
        interpreter = (shutil.which('python') or sys.executable).replace('\\', '/')
        self.python3.write_text('#!/bin/sh\nexec %s "$@"\n' % interpreter, encoding='utf-8', newline='\n')
        self.python3.chmod(0o755)
        self.config = self.root / 'config.yaml'
        self.registry = self.root / 'subs.list'
        self.store = self.root / 'store.json'
        self.helper = self.root / 'geomask-subscription-status.py'
        shutil.copyfile(HERE / 'geomask-subscription-status.py', self.helper)
        # In production both modules sit in /usr/lib/geomask; the resolver imports the state
        # module for the YAML 1.2 loader, so it has to be next to it here too.
        shutil.copyfile(HERE / 'geomask-audit-state.py', self.root / 'geomask-audit-state.py')
        self.registry.write_text('100|A|%s\n200|B|%s\n300|C|%s\n' % (SUB_A, SUB_B, SUB_C),
                                 encoding='utf-8')
        self.bash = find_bash()
        # Source only the definitions: the file's tail is the RPC dispatcher, which would
        # run something (or exit) on source. The functions themselves are the real code.
        self.defs = self.root / 'backend-defs.sh'
        self.defs.write_text(BACKEND.read_text(encoding='utf-8').split('\ncase "$1" in')[0],
                             encoding='utf-8', newline='\n')

    def write_config(self, managed=True):
        providers = {
            'sub': {'type': 'http', 'url': SUB_B, 'interval': 3600},
            'slot': {'type': 'http', 'url': SUB_C, 'interval': 3600},
            'sub2': {'type': 'http', 'url': SUB_A, 'interval': 3600},
        }
        use_vpn = ['sub2']
        if managed:
            providers['audit-vpn-g8'] = {'type': 'file',
                                         'path': str(self.root / 'pool.yaml')}
            use_vpn = ['audit-vpn-g8']
        config = {
            'proxy-providers': providers,
            'proxy-groups': [
                {'name': 'VPN', 'type': 'url-test', 'use': use_vpn},
                {'name': 'VPN-noP2P', 'type': 'url-test', 'use': ['sub']},
                {'name': 'AI', 'type': 'url-test', 'use': ['slot']},
            ],
        }
        # This is exactly what an apply stores: PyYAML's own formatting.
        self.config.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                               encoding='utf-8')
        if managed:
            self.store.write_text(json.dumps({'schema': 2, 'paths': {'vpn': {'applied': {
                'status': 'applied', 'provider_id': 'audit-vpn-g8', 'group': 'VPN',
                'previous_use': ['sub2']}}}}), encoding='utf-8')

    def run_backend(self):
        self.assertIsNotNone(self.bash, 'bash is required to source the backend')
        script = '\n'.join([
            "set -- ''",
            '. ' + repr(str(self.defs)),
            'MIHOMO_CONF=' + repr(str(self.config)),
            'SUBS_FILE=' + repr(str(self.registry)),
            'SUB_STATUS_PY=' + repr(str(self.helper)),
            'LOG=' + repr(str(self.root / 'geo-sync.log')),
            'DEVICES=' + repr(str(self.root / 'devices.list')),
            'EXCL_OLD=' + repr(str(self.root / 'exclude.list')),
            'AI_RULES=' + repr(str(self.root / 'ai.yaml')),
            'MT_RULES=' + repr(str(self.root / 'mt.yaml')),
            'cmd_status',
        ])
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
                   GM_PYTHON=(shutil.which('python') or sys.executable),
                   MIHOMO_API='http://127.0.0.1:1',
                   GM_SUBSTATUS_CONFIG=str(self.config),
                   GM_SUBSTATUS_STORE=str(self.store),
                   GM_SUBSTATUS_REGISTRY=str(self.registry))
        result = subprocess.run([self.bash, '-c', script], capture_output=True, timeout=120,
                                text=True, encoding='utf-8', errors='replace', env=env)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        return json.loads(result.stdout)

    def test_apply_does_not_erase_the_other_assignments(self):
        self.write_config(managed=True)
        payload = self.run_backend()
        bindings = payload['subscription_bindings']
        self.assertEqual(bindings['vpn']['subscription_ids'], ['100'])
        self.assertEqual(bindings['vpn']['via'], 'managed')
        self.assertEqual(bindings['vpn_nop2p']['subscription_ids'], ['200'])
        self.assertEqual(bindings['ai']['subscription_ids'], ['300'])
        self.assertEqual(payload['vpn_sub'], '100')
        self.assertEqual(payload['vpn_nop2p_sub'], '200')
        self.assertEqual(payload['ai_sub'], '300')

    def test_counters_are_preserved_alongside_the_new_field(self):
        self.write_config(managed=True)
        payload = self.run_backend()
        for key in ('vpn_node', 'ai_rules', 'mt_rules', 'devices', 'aidns', 'lcron'):
            self.assertIn(key, payload, key)

    def test_a_direct_config_still_resolves(self):
        self.write_config(managed=False)
        payload = self.run_backend()
        bindings = payload['subscription_bindings']
        self.assertEqual(bindings['vpn']['subscription_ids'], ['100'])
        self.assertEqual(bindings['vpn']['via'], 'direct')

    def test_missing_helper_keeps_a_valid_status_payload(self):
        self.write_config(managed=True)
        self.helper.unlink()
        payload = self.run_backend()
        self.assertNotIn('subscription_bindings', payload)
        self.assertIn('vpn_node', payload)

    def test_reading_status_creates_no_files(self):
        self.write_config(managed=True)
        before = sorted(p.relative_to(self.root).as_posix()
                        for p in self.root.rglob('*') if p.is_file())
        self.run_backend()
        after = sorted(p.relative_to(self.root).as_posix()
                       for p in self.root.rglob('*') if p.is_file())
        self.assertEqual(before, after)

    def test_ai_sub_get_keeps_the_actual_provider(self):
        self.assertIsNotNone(self.bash, 'bash is required to source the backend')
        self.write_config(managed=True)
        script = '\n'.join([
            "set -- ''",
            '. ' + repr(str(self.defs)),
            'MIHOMO_CONF=' + repr(str(self.config)),
            'SUBS_FILE=' + repr(str(self.registry)),
            'SUB_STATUS_PY=' + repr(str(self.helper)),
            'cmd_ai_sub_get',
        ])
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
                   GM_PYTHON=(shutil.which('python') or sys.executable),
                   MIHOMO_API='http://127.0.0.1:1',
                   GM_SUBSTATUS_CONFIG=str(self.config),
                   GM_SUBSTATUS_STORE=str(self.store),
                   GM_SUBSTATUS_REGISTRY=str(self.registry))
        result = subprocess.run([self.bash, '-c', script], capture_output=True, timeout=120,
                                text=True, encoding='utf-8', errors='replace', env=env)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        payload = json.loads(result.stdout)
        self.assertEqual(payload['provider'], 'slot')
        self.assertEqual(payload['source_subscription_ids'], ['300'])


if __name__ == '__main__':
    unittest.main()
