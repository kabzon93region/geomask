"""R03 phase 1: the manifest gate.

`tests/reinstall_write_set.json` must describe the ACTUAL writer set of the transactional
reinstall: every writer site in `geomask.sh.in` and its transitive callees is covered by a
manifest entry, and every manifest entry points back at real evidence. A mismatch - a new
writer nobody declared, or a declared object nobody writes - fails the gate.

The mutation tests prove the gate itself works: dropping one entry or adding one writer to
the source is caught.

Safety: static text analysis only; mutated sources live in a throwaway temp directory.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import reinstall_manifest as rm


class ManifestGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = rm.load_manifest()
        cls.source = rm.INSTALLER.read_text(encoding='utf-8')

    # ------------------------------------------------------------- the gate itself
    def test_manifest_matches_the_actual_writer_set(self):
        problems = rm.check(self.manifest)
        self.assertEqual([], problems, 'manifest does not match the real writers:\n'
                         + '\n'.join(problems))

    def test_manifest_covers_the_product_preflight_table(self):
        problems = rm.check_preflight_table(self.manifest)
        self.assertEqual([], problems, '\n'.join(problems))

    def test_service_table_matches_the_manifest(self):
        problems = rm.check_service_table(self.manifest)
        self.assertEqual([], problems, '\n'.join(problems))

    def test_gate_catches_a_service_missing_from_the_product_table(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['services'] = [entry for entry in manifest['services']
                                if entry['id'] != 'svc.hev-nop2p']
        problems = rm.check_service_table(manifest)
        self.assertTrue(any('svc.hev-nop2p' in problem for problem in problems),
                        'the gate did not notice a manifest service outside the product table')

    def test_gate_catches_a_service_field_drift(self):
        """pid/initd/observe are the contract, not only the id (round-2 review)."""
        text = self.source.replace(
            'svc.hev-nop2p|service|hev-nop2p|hev-socks5-tunnel|pidof',
            'svc.hev-nop2p|service|hev-nop2p|WRONG-BINARY|pidof')
        self.assertNotEqual(text, self.source, 'product service row not found')
        problems = rm.check_service_table(self.manifest, text=text)
        self.assertTrue(any('hev-nop2p' in problem for problem in problems),
                        'the gate did not notice a changed service pid')

    def test_gate_catches_a_missing_uci_option_row(self):
        """Dropping network|lan|option|ip6assign left the old check green."""
        text = self.source.replace('network|lan|option|ip6assign\n', '')
        self.assertNotEqual(text, self.source, 'product uci row not found')
        problems = rm.check_uci_table(self.manifest, text=text)
        self.assertTrue(any('ip6assign' in problem for problem in problems),
                        'the gate did not notice a dropped uci option row')

    def test_uci_table_matches_the_manifest(self):
        problems = rm.check_uci_table(self.manifest)
        self.assertEqual([], problems, '\n'.join(problems))

    def test_gate_catches_a_manifest_uci_entry_without_product_rows(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['uci'] = [entry for entry in manifest['uci']
                           if entry['id'] != 'uci.ksmbd']
        problems = rm.check_uci_table(manifest)
        self.assertTrue(any('ksmbd' in problem for problem in problems),
                        'the gate did not notice a dropped uci.ksmbd declaration')

    def test_snapshot_and_restore_codes_are_documented(self):
        problems = rm.check_legend(self.manifest)
        self.assertEqual([], problems, '\n'.join(problems))

    def test_every_declared_callee_is_scanned(self):
        labels = set(rm.callee_texts(self.manifest))
        expected = {entry['file'] for entry in self.manifest['callees']}
        for source_file in expected:
            self.assertTrue(any(label.startswith(source_file) for label in labels),
                            'callee not scanned: ' + source_file)

    # ------------------------------------------------------------- the gate catches omissions
    def test_gate_catches_a_writer_missing_from_the_manifest(self):
        """RED contract: a save_stream target the manifest does not know must fail."""
        manifest = copy.deepcopy(self.manifest)
        manifest['files'] = [entry for entry in manifest['files']
                             if entry['path'] != '/usr/bin/geo-backend.sh']
        problems = rm.check(manifest)
        self.assertTrue(any('geo-backend.sh' in problem for problem in problems),
                        'the gate did not notice a missing manifest entry')

    def test_gate_catches_a_new_writer_in_the_source(self):
        """RED contract: a brand-new writer in geomask.sh.in must fail until declared."""
        augmented = self.source + (
            "\nextra_r03_writer() {\n"
            "  save_stream /usr/lib/geomask/brand-new-r03.py 755 <<'ASSET_EOF'\n"
            "@@ASSET:x@@\nASSET_EOF\n}\n")
        original = rm.INSTALLER
        handle = tempfile.NamedTemporaryFile('w', suffix='.sh.in', delete=False,
                                             encoding='utf-8', newline='\n')
        try:
            handle.write(augmented)
            handle.close()
            rm.INSTALLER = Path(handle.name)
            problems = rm.check(self.manifest)
            self.assertTrue(any('brand-new-r03.py' in problem for problem in problems),
                            'the gate did not notice a new writer:\n' + '\n'.join(problems))
        finally:
            rm.INSTALLER = original
            Path(handle.name).unlink(missing_ok=True)

    def test_gate_catches_a_new_dynamic_writer_in_an_existing_function(self):
        """A new unresolvable redirect inside save_stream must hit the declared dynamic class.

        The dynamic entry for save_stream declares its exact temp/backup shapes; a new
        temp file with a different name is not silently absorbed.
        """
        augmented = self.source.replace(
            '  _tmp=$(mktemp "$_t.zmnew.XXXXXX" 2>/dev/null) || _tmp="$_t.zmnew.$$"\n',
            '  _tmp=$(mktemp "$_t.zmnew.XXXXXX" 2>/dev/null) || _tmp="$_t.zmnew.$$"\n'
            '  cat > "$_t.whole-new-shape" 2>/dev/null\n', 1)
        self.assertNotEqual(augmented, self.source, 'injection point not found')
        original = rm.INSTALLER
        handle = tempfile.NamedTemporaryFile('w', suffix='.sh.in', delete=False,
                                             encoding='utf-8', newline='\n')
        try:
            handle.write(augmented)
            handle.close()
            rm.INSTALLER = Path(handle.name)
            problems = rm.check(self.manifest)
            self.assertTrue(any('whole-new-shape' in problem for problem in problems),
                            'the gate absorbed an undeclared dynamic writer:\n'
                            + '\n'.join(problems))
        finally:
            rm.INSTALLER = original
            Path(handle.name).unlink(missing_ok=True)

    def test_gate_catches_a_missing_forbidden_effect_guard(self):
        """apk/opkg during reinstall must be declared forbidden AND actually guarded."""
        manifest = copy.deepcopy(self.manifest)
        manifest['forbidden'][0]['guards'] = ['GM_TOTALLY_ABSENT_GUARD']
        problems = rm.check(manifest)
        self.assertTrue(any('GM_TOTALLY_ABSENT_GUARD' in problem for problem in problems))

    def test_gate_catches_a_stale_dynamic_entry(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['dynamic'].append({
            'id': 'dyn.stale', 'functions': ['no_such_function'],
            'effects': ['redirect'], 'site_regex': 'nothing-matches-this',
        })
        problems = rm.check(manifest)
        self.assertTrue(any('dyn.stale' in problem for problem in problems))

    # ------------------------------------------------------------- content contracts
    def test_manifest_records_absence_symlink_and_type_handling(self):
        """The manifest must classify every object's type and say how absence is handled."""
        for entry in self.manifest['files']:
            self.assertIn(entry['type'], ('regular', 'symlink', 'dir', 'absent', 'dynamic'),
                          entry['id'])
            self.assertIn(entry['owner'], ('ours', 'shared'), entry['id'])
            self.assertTrue(entry.get('snapshot'), entry['id'])
            self.assertTrue(entry.get('restore'), entry['id'])

    def test_shared_files_never_claim_full_byte_restore(self):
        """A shared file must not declare plain byte restore - that erases foreign edits."""
        for entry in self.manifest['files']:
            if entry['owner'] != 'shared':
                continue
            if entry['restore'] == 'NR':
                continue
            self.assertIn(entry['restore'], ('RT', 'RU', 'RK', 'RS'),
                          '%s: shared file with restore %s' % (entry['id'], entry['restore']))

    def test_runtime_objects_include_services_cron_rc_uci_sysctl(self):
        kinds = {entry['kind'] for entry in self.manifest['preflight_objects']}
        for required in ('service', 'crontab', 'links', 'uci', 'ip-rule', 'nft-table',
                         'alias', 'sysctl', 'file-shared', 'file', 'dirs', 'dir'):
            self.assertIn(required, kinds, 'preflight objects miss kind ' + required)

    def test_package_calls_are_declared_forbidden_in_reinstall(self):
        forbidden = self.manifest['forbidden']
        self.assertTrue(any('apk' in entry['call'] for entry in forbidden))
        for entry in forbidden:
            self.assertIn('reinstall', entry['policy'],
                          'the forbidden policy must name the reinstall path')
            self.assertTrue(entry.get('guards'), 'forbidden effect without enforcement guard')

    def test_zapret_and_magitrickle_state_is_declared(self):
        paths = {entry['path'] for entry in self.manifest['files']}
        for required in ('/opt/zapret/ipset/zapret-hosts-user-exclude.txt',
                         '/opt/zapret/ipset/zapret-hosts-user.txt',
                         '/etc/geomask/zapret-force-keep.txt',
                         '/etc/geomask/zapret-always-exclude.txt',
                         '/etc/magitrickle/state/config.yaml'):
            self.assertIn(required, paths)

    def test_passwd_and_group_append_is_declared_shared(self):
        by_path = {entry['path']: entry for entry in self.manifest['files']}
        for path in ('/etc/passwd', '/etc/group'):
            entry = by_path[path]
            self.assertEqual('shared', entry['owner'])
            self.assertEqual('RT', entry['restore'], path + ' must restore by targeted line')

    def test_ipv6_backend_writers_are_declared(self):
        paths = {entry['path'] for entry in self.manifest['files']}
        for required in ('/etc/geomask/ipv6.state',
                         '/etc/sysctl.d/99-disable-ipv6.conf',
                         '/etc/nftables.d/99-geomask-no-v6.nft',
                         '/proc/sys/net/ipv6/conf'):
            self.assertIn(required, paths)
        kinds = {entry['kind'] for entry in self.manifest['net_runtime']}
        self.assertIn('sysctl-kernel', kinds)

    def test_sysctl_restore_is_per_interface(self):
        entry = next(item for item in self.manifest['net_runtime']
                     if item['kind'] == 'sysctl-kernel')
        self.assertEqual('RK', entry['restore'])
        self.assertIn('interface', entry['detail'])

    # --------------------------------------------------- callee path policy (U01)
    def test_callee_path_policy_is_declared_with_reasons(self):
        policy = self.manifest.get('callee_path_policy') or []
        self.assertTrue(policy, 'the updater install-root policy is not declared')
        self.assertIn('/etc/geomask/cache', {entry['path'] for entry in policy})
        for entry in policy:
            self.assertTrue(entry.get('reason'), 'policy entry without a reason: %s'
                            % entry.get('id'))

    def test_gate_rejects_a_policy_entry_without_a_reason(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['callee_path_policy'][0]['reason'] = ''
        problems = rm.check(manifest)
        self.assertTrue(any('without a reason' in problem for problem in problems),
                        'a policy entry without a reason passed the gate')

    def test_gate_rejects_a_stale_policy_entry(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['callee_path_policy'].append({'id': 'stale', 'path': '/etc/not-in-any-callee/',
                                               'reason': 'stale declaration'})
        problems = rm.check(manifest)
        self.assertTrue(any('without any literal' in problem for problem in problems),
                        'a policy entry that names no real literal passed the gate')

    def test_a_policy_path_does_not_excuse_an_installer_write_site(self):
        # Policy entries classify literals inside a callee. If they also covered the
        # installer's own writer sites, a save_stream aimed at a bare root would pass.
        self.assertFalse(rm._covered_by_files(self.manifest, '/etc/geomask/'))
        self.assertTrue(rm._covered_by_files(self.manifest, '/etc/geomask/', allow_policy=True))


if __name__ == '__main__':
    unittest.main()
