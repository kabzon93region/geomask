"""Offline tests of real per-path audit module; no router/network effects."""
import importlib.util
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[1] / 'assets' / 'geomask-node-audit.py'
spec = importlib.util.spec_from_file_location('node_audit', SOURCE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(audit, 'STORE', self.root / 'settings.json'),
                        patch.object(audit, 'STATE', self.root / 'state'),
                        patch.object(audit, 'CONFIG', self.root / 'live.yaml')]
        for item in self.patches: item.start()
        self.addCleanup(self.temp.cleanup)
        for item in self.patches: self.addCleanup(item.stop)

    def test_defaults_are_proposed_not_applied(self):
        result = audit.get()
        self.assertEqual([p['countries_allow'] for p in result['paths']], [['NL','DE','FR','SE'],['DE','PL'],['US']])
        self.assertTrue(all(not p['configured'] and not p['applied'] for p in result['paths']))
        self.assertFalse(audit.STORE.exists())

    def test_invalid_countries_fail(self):
        for value in ('', 'US\nDE', 'us', 'US,US', 'ZZ', 'US,,DE', None, ['US']):
            with self.subTest(value=value), self.assertRaises(audit.AuditError): audit.countries(value)

    def test_corrupt_existing_store_not_reset(self):
        audit.STORE.write_bytes(b'')
        with self.assertRaises(audit.AuditError): audit.get()
        audit.STORE.write_bytes(b'{"schema":1,"schema":1}')
        with self.assertRaises(audit.AuditError): audit.get()

    def test_render_preserves_neighbor(self):
        original = {'schema':1, 'generation':2, 'paths':{'ai':{'countries_allow':['US']}}}
        result = json.loads(audit.render_save(original, 'vpn', ['DE']))
        self.assertEqual(result['paths']['ai'], original['paths']['ai'])
        self.assertEqual(original['generation'], 2)
        self.assertEqual(result['generation'], 3)

    def test_shared_provider_resolves_per_path(self):
        config = {'proxy-groups':[{'name':'AI','use':['same']},{'name':'VPN','use':['same']}],
                  'proxy-providers':{'same':{'url':'https://secret.invalid/token'}}}
        self.assertEqual(audit.resolve_source(config,'ai'), ('AI',['same']))
        self.assertEqual(audit.resolve_source(config,'vpn'), ('VPN',['same']))

    def test_unknown_and_empty_pool_are_not_admitted(self):
        for probes in ({}, {'cloudflare':{'status':'ok','country':None}}):
            self.assertFalse(audit.classify('vpn',['DE'],probes)['admitted'])

    def test_country_filters_independent(self):
        probe = {'cloudflare':{'status':'ok','country':'PL'}}
        self.assertEqual(audit.classify('vpn',['DE'],probe)['status'],'excluded')
        self.assertEqual(audit.classify('vpn_nop2p',['DE','PL'],probe)['status'],'eligible')

    def test_ai_admission_no_longer_requires_google(self):
        # Superseded by the owner decision of 09.11.2026: Google cannot report a country, so
        # an unmeasured Google no longer blocks an otherwise admitted node.
        probe = {'cloudflare':{'status':'ok','country':'US'}, 'chatgpt':{'status':'ok','country':'US'},
                 'google':{'status':'unknown','availability':True,'country':None}}
        self.assertEqual(audit.classify('ai',['US'],probe)['status'],'eligible')
        probe['chatgpt']['country']='FI'
        self.assertEqual(audit.classify('ai',['US'],probe)['status'],'excluded')

    def test_ai_is_admitted_by_chatgpt_and_cloudflare(self):
        # Owner decision 09.11.2026: Google cannot report a country, so it does not gate.
        probe = {'cloudflare': {'status': 'ok', 'country': 'US'},
                 'chatgpt': {'status': 'ok', 'country': 'US'},
                 'google': {'status': 'unknown', 'availability': True, 'country': None}}
        result = audit.classify('ai', ['US'], probe)
        self.assertEqual(result['status'], 'eligible')
        self.assertTrue(result['admitted'])
        self.assertFalse(any(r.startswith('google') for r in result['reasons']))
        self.assertEqual(result['informational']['google'],
                         {'measured': False, 'country': None, 'policy': 'not-required'})

    def test_a_measured_google_country_is_reported_but_does_not_gate(self):
        probe = {'cloudflare': {'status': 'ok', 'country': 'US'},
                 'chatgpt': {'status': 'ok', 'country': 'US'},
                 'google': {'status': 'ok', 'country': 'NL'}}
        result = audit.classify('ai', ['US'], probe)
        self.assertEqual(result['status'], 'eligible')
        self.assertEqual(result['informational']['google']['country'], 'NL')
        self.assertTrue(result['informational']['google']['measured'])

    def test_chatgpt_still_decides_the_ai_path(self):
        probe = {'cloudflare': {'status': 'ok', 'country': 'US'},
                 'chatgpt': {'status': 'ok', 'country': 'FI'},
                 'google': {'status': 'ok', 'country': 'US'}}
        self.assertEqual(audit.classify('ai', ['US'], probe)['status'], 'excluded')

    def test_an_unmeasured_chatgpt_is_unknown_not_admitted(self):
        probe = {'cloudflare': {'status': 'ok', 'country': 'US'},
                 'chatgpt': {'status': 'unknown', 'country': None}}
        self.assertEqual(audit.classify('ai', ['US'], probe)['status'], 'unknown')

    def test_vpn_paths_have_no_informational_block(self):
        result = audit.classify('vpn', ['DE'], {'cloudflare': {'status': 'ok', 'country': 'DE'}})
        self.assertNotIn('informational', result)

    def test_the_probe_resolves_names_remotely_not_locally(self):
        # Country must be measured over the node's own DNS path: a different local resolver
        # must not be able to change the answer. socks5h sends the hostname to the proxy.
        instance = audit.IsolatedProbe([])
        result = type('Result', (), {'returncode': 0,
                                     'stdout': 'ip=23.95.1.1\nloc=US\ncolo=LAX\nhttp_code=200\n'})()
        with patch.object(instance, 'api'), patch.object(audit.subprocess, 'run', return_value=result) as run:
            instance.probe('USA', 'cloudflare')
        args = run.call_args[0][0]
        joined = ' '.join(args)
        self.assertIn('socks5h://127.0.0.1:', joined)
        self.assertNotIn('socks5://', joined)
        self.assertNotIn('--resolve', joined)
        self.assertNotIn('--doh-url', joined)

    def test_a_google_probe_never_claims_a_country(self):
        instance = audit.IsolatedProbe([])
        result = type('Result', (), {'returncode': 0, 'stdout': 'http_code=204\n'})()
        with patch.object(instance, 'api'), patch.object(audit.subprocess, 'run', return_value=result):
            proof = instance.probe('USA', 'google')
        self.assertEqual(proof['status'], 'unknown')
        self.assertIsNone(proof['country'])
        self.assertTrue(proof['availability'])
        self.assertEqual(proof['dns_context'], 'remote SOCKS5h')

    def test_tls_probe_arguments_do_not_disable_verification(self):
        instance = audit.IsolatedProbe([])
        result = type('Result', (), {'returncode':0,'stdout':'ip=23.95.1.1\nloc=US\ncolo=LAX\nhttp_code=200\n'})()
        with patch.object(instance,'api'), patch.object(audit.subprocess,'run',return_value=result) as run:
            proof = instance.probe('USA','chatgpt')
        self.assertTrue(proof['tls_verified'])
        arguments = run.call_args.args[0]
        self.assertNotIn('-k',arguments)
        self.assertTrue(any('socks5h://' in arg for arg in arguments))
        self.assertTrue(arguments[-1].startswith('https://chatgpt.com/'))

    def test_google_http_success_not_country(self):
        instance = audit.IsolatedProbe([])
        result = type('Result', (), {'returncode':0,'stdout':'http_code=204\n'})()
        with patch.object(instance,'api'), patch.object(audit.subprocess,'run',return_value=result):
            proof = instance.probe('USA','google')
        self.assertTrue(proof['availability'])
        self.assertIsNone(proof['country'])
        self.assertEqual(proof['status'],'unknown')

    def test_subscription_errors_do_not_expose_url(self):
        with patch.object(audit.socket,'getaddrinfo',return_value=[(2,1,6,'',('1.1.1.1',443))]), patch.object(audit.urllib.request,'build_opener',side_effect=ValueError('https://secret.invalid/token')):
            with self.assertRaises(audit.AuditError) as raised:
                audit.fetch_nodes({'url':'https://secret.invalid/token'})
        self.assertNotIn('token',str(raised.exception))

    def test_noop_save_never_requests_backup_or_writes(self):
        content = audit.encode({'schema':1,'generation':1,'paths':{'ai':{'countries_allow':['US']}}})
        audit.STORE.write_bytes(content)
        with patch.object(audit,'locked',return_value=contextlib.nullcontext()), patch.object(audit,'atomic', wraps=audit.atomic) as writer:
            result = audit.save('ai','US',audit.digest(content))
        self.assertFalse(result['changed'])
        self.assertEqual(audit.STORE.read_bytes(),content)
        writer.assert_not_called()

    def test_changed_save_works_without_pc_receipt_or_external_command(self):
        content = audit.encode({'schema':1,'generation':1,'paths':{'ai':{'countries_allow':['US']},'vpn':{'countries_allow':['DE']}}})
        audit.STORE.write_bytes(content)
        audit.CONFIG.write_bytes(b'production unchanged')
        with patch.object(audit,'locked',return_value=contextlib.nullcontext()), \
             patch.object(audit.subprocess,'run',side_effect=AssertionError('no PC/service dependency')), \
             patch.object(audit.urllib.request,'urlopen',side_effect=AssertionError('no network dependency')):
            result=audit.save('ai','DE',audit.digest(content))
        saved=json.loads(audit.STORE.read_bytes())
        self.assertTrue(result['changed'])
        self.assertFalse(result['applied'])
        self.assertEqual(saved['generation'],2)
        self.assertEqual(saved['paths']['ai']['countries_allow'],['DE'])
        self.assertEqual(saved['paths']['vpn']['countries_allow'],['DE'])
        self.assertEqual(audit.CONFIG.read_bytes(),b'production unchanged')
        self.assertFalse((self.root / 'receipt.json').exists())

    def test_stale_save_rejected_before_backup(self):
        with patch.object(audit,'locked',return_value=contextlib.nullcontext()), patch.object(audit,'atomic', wraps=audit.atomic) as writer:
            with self.assertRaises(audit.AuditError): audit.save('ai','US','0'*64)
        writer.assert_not_called()
        self.assertFalse(audit.STORE.exists())

    def test_raw_rpc_rejects_types_nul_and_duplicate_keys(self):
        for raw in ('[]', '{"path_id":null}', '{"path_id":["ai"]}',
                    '{"path_id":"ai\\\\u0000"}', '{"path_id":"ai","path_id":"vpn"}'):
            with self.subTest(raw=raw), self.assertRaises(audit.AuditError):
                audit.validate_rpc('nodes_path_check',raw)
        self.assertTrue(audit.validate_rpc('nodes_path_check','{"path_id":"ai"}')['ok'])

    def test_authenticated_transport_metadata_is_accepted_but_not_arbitrary_fields(self):
        for method, business in (('nodes_paths_get',{}),('nodes_path_check',{'path_id':'ai'})):
            request=dict(business,ubus_rpc_session='a'*32)
            self.assertTrue(audit.validate_rpc(method,json.dumps(request))['ok'])
            request['ubus_rpc_session']=None
            with self.assertRaises(audit.AuditError): audit.validate_rpc(method,json.dumps(request))
        with self.assertRaises(audit.AuditError):
            audit.validate_rpc('nodes_path_check',json.dumps({'path_id':'ai','unexpected':'x'}))

    def test_real_json_escaped_nul_and_oversize_rejected(self):
        with self.assertRaises(audit.AuditError):
            audit.validate_rpc('nodes_path_check',json.dumps({'path_id':'ai'+chr(0)}))
        with self.assertRaises(audit.AuditError):
            audit.validate_rpc('nodes_paths_get','{}'+' '*16384)

    def test_unchanged_unsorted_country_list_is_noop(self):
        content=audit.encode({'schema':1,'generation':1,'paths':{'vpn':{'countries_allow':['NL','DE']}}})
        audit.STORE.write_bytes(content)
        with patch.object(audit,'locked',return_value=contextlib.nullcontext()), patch.object(audit,'atomic', wraps=audit.atomic) as writer:
            result=audit.save('vpn','DE,NL',audit.digest(content))
        self.assertFalse(result['changed'])
        writer.assert_not_called()

    def test_render_preserves_future_path_fields(self):
        data={'schema':1,'generation':0,'paths':{'ai':{'countries_allow':['US'],'proofs':['chatgpt','google']}}}
        result=json.loads(audit.render_save(data,'ai',['DE']))
        self.assertEqual(result['paths']['ai']['proofs'],['chatgpt','google'])

    def test_boolean_schema_and_nonstring_country_are_invalid(self):
        for data in ({'schema':True,'generation':0,'paths':{}},
                     {'schema':1,'generation':0,'paths':{'ai':{'countries_allow':[1]}}}):
            audit.STORE.write_bytes(audit.encode(data))
            with self.assertRaises(audit.AuditError): audit.load_store()

    def test_failed_atomic_replace_preserves_previous_settings(self):
        content=audit.encode({'schema':1,'generation':1,'paths':{'ai':{'countries_allow':['US']}}})
        audit.STORE.write_bytes(content)
        with patch.object(audit,'locked',return_value=contextlib.nullcontext()), \
             patch.object(audit.os,'replace',side_effect=OSError('disk error')):
            with self.assertRaises(OSError): audit.save('ai','DE',audit.digest(content))
        self.assertEqual(audit.STORE.read_bytes(),content)
        self.assertEqual(list(self.root.glob('.settings.json.*')),[])

    def test_invalid_save_preserves_existing_settings(self):
        content=audit.encode({'schema':1,'generation':1,'paths':{'ai':{'countries_allow':['US']}}})
        audit.STORE.write_bytes(content)
        for ident,value in (('ai',''),('../ai','US'),('ai','US,US')):
            with self.assertRaises(audit.AuditError): audit.save(ident,value,audit.digest(content))
        self.assertEqual(audit.STORE.read_bytes(),content)

    def test_missing_target_rejected(self):
        with self.assertRaises(audit.AuditError): audit.resolve_source({},'vpn')
        with self.assertRaises(audit.AuditError): audit.resolve_source({},'../ai')

if __name__ == '__main__': unittest.main()
