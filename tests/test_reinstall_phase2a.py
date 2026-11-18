"""R03 phase 2a: gate/preflight regressions for the review gaps A-G.

Scope (review-r03-phase1): the manifest gate and the reinstall preflight only -
snapshot/restore themselves are phase 2b and untouched here.

  A  callee_texts must really scan the geo-backend.sh scope {from,to} (IPv6 cluster);
     a path inside the scope that the manifest does not classify fails the gate.
  B  geomask-lane.sh (step5, GM_AUTO_LANE) is a declared transitive callee with all of
     its objects; a new object in the lane callee fails the gate; an invoked-but-undeclared
     script fails the gate (undeclared callee must not stay invisible).
  C  the transient /etc/geomask/ipv6.state.tmp (_ipv6_save) is declared in the manifest.
  D  dynamic classes may not absorb new unresolvable writers behind catch-all site_regex.
  E  uci/net/process coverage is concrete: a new uci key, ip rule or killed process fails.
  F  meta.contract documents the ACTUAL NR semantics (NR alone is not a preflight refusal).
  G  preflight classifies every byte-snapshot write-set object (symlink there = refusal)
     and the remaining boundary is documented honestly.

Safety: static text analysis and throwaway fixture roots with injected commands only;
no live system path is read or written.
"""
import copy
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reinstall_manifest as rm  # noqa: E402
import test_reinstall_preflight as tp  # noqa: E402
import win_symlink  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def clone_callees(root: Path, manifest: dict, extra=()):
    """Copy every declared callee asset under root so rm.ROOT can be swapped safely."""
    files = [callee['file'] for callee in manifest['callees']] + list(extra)
    for file in files:
        src = ROOT / file
        dst = root / file
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)


class GateHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = rm.load_manifest()
        cls.source = rm.INSTALLER.read_text(encoding='utf-8')

    def problems_with(self, installer_text=None, mutate=None, mutate_file=None):
        """Run rm.check with an optionally mutated installer and/or cloned callee root."""
        original_installer, original_root = rm.INSTALLER, rm.ROOT
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        try:
            if installer_text is not None:
                handle = Path(tmp.name) / 'geomask.sh.in'
                handle.write_text(installer_text, encoding='utf-8', newline='\n')
                rm.INSTALLER = handle
            if mutate is not None:
                root = Path(tmp.name) / 'root'
                clone_callees(root, self.manifest, extra=(mutate_file,))
                target = root / mutate_file
                text = target.read_text(encoding='utf-8')
                target.write_text(mutate(text), encoding='utf-8', newline='\n')
                rm.ROOT = root
            return rm.check(self.manifest)
        finally:
            rm.INSTALLER, rm.ROOT = original_installer, original_root


# ---------------------------------------------------------------- A: scope scanning


class ScopeTests(GateHarness):
    def test_a1_geo_backend_scope_body_is_really_scanned(self):
        bodies = rm.callee_texts(self.manifest)
        labels = [label for label in bodies if label.startswith('assets/geo-backend.sh')]
        self.assertTrue(labels, 'geo-backend.sh callee is not scanned at all')
        body = bodies[labels[0]]
        self.assertIn('cmd_ipv6', body,
                      'scope body is empty: the IPv6 cluster is not scanned by the gate')
        self.assertIn('IPV6_STATE', body)
        self.assertGreaterEqual(len(rm.path_literals(body)), 4,
                                'no path literals extracted from the IPv6 cluster')

    def test_a2_unclassified_path_inside_the_scope_fails_the_gate(self):
        def mutate(text):
            needle = 'IPV6_STATE=/etc/geomask/ipv6.state\n'
            self.assertIn(needle, text)
            return text.replace(
                needle, needle + 'cp /etc/config/network /usr/lib/geomask/scope-hidden-r03.bak '
                '2>/dev/null\n', 1)
        problems = self.problems_with(mutate=mutate, mutate_file='assets/geo-backend.sh')
        self.assertTrue(any('unclassified path in callee' in problem
                            and 'scope-hidden-r03' in problem for problem in problems),
                        'the gate did not notice an unclassified path inside the scope:\n'
                        + '\n'.join(problems))

    def test_a3_scope_paths_are_classified_on_the_real_sources(self):
        bodies = rm.callee_texts(self.manifest)
        labels = [label for label in bodies if label.startswith('assets/geo-backend.sh')]
        for path in sorted(rm.path_literals(bodies[labels[0]])):
            self.assertTrue(rm._covered_by_files(self.manifest, path),
                            'IPv6-cluster path not classified: ' + path)


# ---------------------------------------------------------------- B: lane callee


class LaneTests(GateHarness):
    LANE_OBJECTS = ('/etc/hev-socks5-tunnel/nop2p.yml',
                    '/etc/init.d/hev-nop2p',
                    '/usr/share/nftables.d/ruleset-post/99-lane-accept.nft',
                    '/etc/mihomo/config.yaml.bak_lane')

    def test_b1_lane_callee_is_declared_and_scanned(self):
        callees = {entry['file'] for entry in self.manifest['callees']}
        self.assertIn('assets/geomask-lane.sh', callees,
                      'geomask-lane.sh is not a declared transitive callee')
        bodies = rm.callee_texts(self.manifest)
        labels = [label for label in bodies if 'geomask-lane.sh' in label]
        self.assertTrue(labels, 'lane callee body is not scanned')
        self.assertIn('_provision_netfw', bodies[labels[0]])

    def test_b2_new_object_in_the_lane_callee_fails_the_gate(self):
        def mutate(text):
            self.assertIn('  echo "=== 1) tunnel config ==="', text)
            return text.replace(
                '  echo "=== 1) tunnel config ==="',
                '  touch /usr/lib/geomask/lane-hidden-r03.tmp 2>/dev/null\n'
                '  echo "=== 1) tunnel config ==="', 1)
        problems = self.problems_with(mutate=mutate, mutate_file='assets/geomask-lane.sh')
        self.assertTrue(any('unclassified path in callee' in problem
                            and 'lane-hidden-r03' in problem for problem in problems),
                        'the gate absorbed a new lane object:\n' + '\n'.join(problems))

    def test_b3_invoking_an_undeclared_script_fails_the_gate(self):
        augmented = self.source + (
            "\nextra_r03_invoke() {\n"
            "  \"$(P /usr/bin/geomask-brand-new.sh)\" install >/dev/null 2>&1 || true\n}\n")
        problems = self.problems_with(installer_text=augmented)
        self.assertTrue(any('geomask-brand-new.sh' in problem
                            and 'declared' in problem for problem in problems),
                        'an invoked but undeclared script stayed invisible:\n'
                        + '\n'.join(problems))

    def test_b4_lane_objects_and_uci_keys_are_declared(self):
        paths = {entry['path'] for entry in self.manifest['files']}
        for required in self.LANE_OBJECTS:
            self.assertIn(required, paths, 'lane object missing from the manifest: ' + required)
        patterns = [pattern for entry in self.manifest['uci']
                    for pattern in (entry.get('key_patterns') or [])]
        self.assertTrue(any(re.search(pattern, 'network.$NETID') for pattern in patterns),
                        'lane uci network keys are not declared')
        self.assertTrue(any(re.search(pattern, 'firewall.$NEW.name') for pattern in patterns),
                        'lane uci firewall keys are not declared')
        self.assertTrue(any(re.search(pattern, 'add:firewall zone') for pattern in patterns),
                        'lane uci firewall sections are not declared')


# ---------------------------------------------------------------- C: transient object


class TransientTests(GateHarness):
    def test_c1_ipv6_state_tmp_is_declared(self):
        paths = {entry['path'] for entry in self.manifest['files']}
        self.assertIn('/etc/geomask/ipv6.state.tmp', paths,
                      'transient _ipv6_save object /etc/geomask/ipv6.state.tmp is undeclared')


# ---------------------------------------------------------------- D: dynamic precision


class DynamicTests(GateHarness):
    def _assert_uncovered(self, problems, needle):
        self.assertTrue(any('uncovered' in problem and needle in problem
                            for problem in problems),
                        'a new unresolvable writer was absorbed by a dynamic class:\n'
                        + '\n'.join(problems))

    def test_d1_new_unresolvable_writer_in_save_stream_is_not_absorbed(self):
        # The temp name is mkstemp-unique now, so the anchor is the mktemp line itself.
        augmented = self.source.replace(
            '  _tmp=$(mktemp "$_t.zmnew.XXXXXX" 2>/dev/null) || _tmp="$_t.zmnew.$$"\n',
            '  _tmp=$(mktemp "$_t.zmnew.XXXXXX" 2>/dev/null) || _tmp="$_t.zmnew.$$"\n'
            '  cat > "$_r03_brand_new_shape" 2>/dev/null\n', 1)
        self.assertNotEqual(augmented, self.source, 'injection point not found')
        self._assert_uncovered(self.problems_with(installer_text=augmented),
                               'r03_brand_new_shape')

    def test_d2_new_unresolvable_writer_in_the_snapshot_is_not_absorbed(self):
        augmented = self.source.replace(
            '  : > "$1/present"; : > "$1/absent"; : > "$1/rc.d"\n',
            '  : > "$1/present"; : > "$1/absent"; : > "$1/rc.d"\n'
            "  printf '%s\\n' x >> \"$1/r03-brand-new-snap\" 2>/dev/null\n", 1)
        self.assertNotEqual(augmented, self.source, 'injection point not found')
        self._assert_uncovered(self.problems_with(installer_text=augmented),
                               'r03-brand-new-snap')

    def test_d3_new_script_writer_is_not_absorbed(self):
        augmented = self.source.replace(
            "mod.schedule_set(ident, 'true', when, 'check-only')\n",
            "mod.schedule_set(ident, 'true', when, 'check-only')\n"
            "open('/tmp/r03-new-script-writer','w').write('x')\n", 1)
        self.assertNotEqual(augmented, self.source, 'injection point not found')
        self._assert_uncovered(self.problems_with(installer_text=augmented),
                               'r03-new-script-writer')

    def test_d4_no_dynamic_entry_uses_a_catch_all_site_regex(self):
        for entry in self.manifest['dynamic']:
            pattern = entry.get('site_regex')
            self.assertTrue(pattern,
                            '%s must declare a concrete site_regex' % entry['id'])
            self.assertNotIn(pattern, ('.*', '(.*)', '^.*$', '.*$', '^.*', ''),
                             '%s uses a catch-all site_regex' % entry['id'])


# ---------------------------------------------------------------- E: effect precision


class EffectTests(GateHarness):
    ANCHOR = "    uci set network.lanai.proto='static'\n"

    def test_e1_new_uci_key_fails_the_gate(self):
        augmented = self.source.replace(
            self.ANCHOR, self.ANCHOR + "    uci set network.r03_sneaky_key='1' 2>/dev/null\n", 1)
        self.assertNotEqual(augmented, self.source, 'injection point not found')
        problems = self.problems_with(installer_text=augmented)
        self.assertTrue(any('uncovered uci site' in problem and 'r03_sneaky_key' in problem
                            for problem in problems),
                        'a new uci key was absorbed by a broad package class:\n'
                        + '\n'.join(problems))

    def test_e2_new_ip_rule_fails_the_gate(self):
        augmented = self.source.replace(
            self.ANCHOR, self.ANCHOR + '    ip rule add pref 424242 table 55 2>/dev/null\n', 1)
        self.assertNotEqual(augmented, self.source, 'injection point not found')
        problems = self.problems_with(installer_text=augmented)
        self.assertTrue(any('uncovered net site' in problem and '424242' in problem
                            for problem in problems),
                        'a new ip rule was absorbed by the ip-* wildcard:\n'
                        + '\n'.join(problems))

    def test_e3_new_process_signal_fails_the_gate(self):
        augmented = self.source.replace(
            self.ANCHOR, self.ANCHOR + '    pkill -f r03-brand-new-process 2>/dev/null || true\n', 1)
        self.assertNotEqual(augmented, self.source, 'injection point not found')
        problems = self.problems_with(installer_text=augmented)
        self.assertTrue(any('uncovered process site' in problem
                            and 'r03-brand-new-process' in problem for problem in problems),
                        'a new process kill was absorbed by the process wildcard:\n'
                        + '\n'.join(problems))

    def test_e4_effect_declarations_are_concrete(self):
        for entry in self.manifest['uci']:
            self.assertNotEqual('*', entry.get('package'),
                                '%s declares a catch-all package' % entry['id'])
            patterns = entry.get('key_patterns') or []
            self.assertTrue(patterns, '%s has no key_patterns' % entry['id'])
            for pattern in patterns:
                self.assertNotEqual('.*', pattern, '%s has a catch-all key pattern' % entry['id'])
        for entry in self.manifest['net_runtime']:
            if entry.get('object') in ('rule', 'nft', 'sysctl'):
                self.assertTrue(entry.get('pattern'), '%s has no pattern' % entry['id'])
        for entry in self.manifest['processes']:
            self.assertTrue(entry.get('pattern'), '%s has no pattern' % entry['id'])
            self.assertNotEqual('.*', entry.get('pattern'),
                                '%s has a catch-all pattern' % entry['id'])


# ---------------------------------------------------------------- F: NR contract text


class ContractTests(GateHarness):
    def test_f_contract_matches_the_actual_nr_semantics(self):
        contract = ' '.join(self.manifest['meta']['contract'])
        nr = self.manifest['meta']['legend']['restore'].get('NR', '')
        self.assertNotIn('Любой объект с restore=NR', contract,
                         'the contract still claims that every NR object refuses the preflight')
        combined = contract + ' ' + nr
        for marker in ('NR', 'не является отказом', 'seed-risk', 'ненаблюдаем', 'symlink'):
            self.assertIn(marker, combined,
                          'the exact NR/refusal rule is not documented (missing %r)' % marker)
        for condition in ('зависимост', 'наблюдаем', 'восстановим', 'seed-risk'):
            self.assertIn(condition, combined,
                          'refusal conditions are not enumerated (missing %r)' % condition)


# ---------------------------------------------------------------- G: preflight coverage


def byte_table_rows(source: str):
    match = re.search(r'^REINSTALL_PREFLIGHT_BYTE_FILES="([^"]*)"', source, re.M | re.S)
    if not match:
        return None
    rows = {}
    for line in match.group(1).splitlines():
        if line.strip():
            path, expected = line.split('|', 1)
            rows[path] = expected
    return rows


@unittest.skipIf(tp.BASH is None, 'bash is required')
class PreflightByteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = self.root / 'case'
        self.case.mkdir()
        self.pkg_log = self.root / 'pkg.log'
        self.pkg_log.write_text('', encoding='utf-8')
        self.build_fixture()

    def write(self, relative, body='x\n'):
        path = self.case / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8', newline='\n')
        return path

    def build_fixture(self):
        (self.case / 'tmp').mkdir(exist_ok=True)
        self.write('etc/openwrt_release', "DISTRIB_RELEASE='24.10'\n")
        for service in ('dnsmasq', 'mihomo', 'magitrickle', 'hev-socks5-tunnel', 'rpcd',
                        'network', 'firewall', 'geomask-syncd', 'ai-doh', 'dnsmasq-ai',
                        'ksmbd'):
            self.write('etc/init.d/' + service, '#!/bin/sh\nexit 0\n')
        for link in ('S50mihomo', 'S19firewall', 'S20network', 'S60dnsmasq', 'S60rpcd',
                     'S99geomask-syncd'):
            self.write('etc/rc.d/' + link, '')
        self.write('etc/passwd', 'root:x:0:0:root:/root:/bin/ash\n')
        self.write('etc/group', 'root:x:0:\n')
        self.write('etc/mihomo/config.yaml', '# geomask-managed: 1\nsecret: "test"\n')
        self.write('etc/mihomo/ruleset/ai.yaml', 'payload: []\n')
        self.write('etc/magitrickle/state/config.yaml', 'groups:\n  - name: GoogleVideo VPN\n')
        self.write('opt/zapret/ipset/zapret-hosts-user-exclude.txt', 'ours.example\n')
        self.write('opt/zapret/ipset/zapret-hosts-user.txt', 'kept.example\n')
        self.write('usr/lib/geomask/geomask-tool.sh', '#!/bin/sh\n')
        self.write('usr/bin/geomask-tool.sh', '#!/bin/sh\n')
        for key in ('all', 'default', 'br-lan'):
            self.write('proc/sys/net/ipv6/conf/%s/disable_ipv6' % key, '0\n')
        self.write('usr/sbin/https-dns-proxy', '#!/bin/sh\n')

    def run_preflight(self, extra='', body=None):
        body = body or ('reinstall_preflight; printf "RC=%s\\n" "$?"'
                        '; printf "NOPKG=%s\\n" "$GM_REINSTALL_NO_PKG"')
        match = re.search(r'^REINSTALL_PREFLIGHT_BYTE_FILES="([^"]*)"', tp.SOURCE, re.M | re.S)
        table = tp.objects_table()
        if match:
            table += match.group(0) + '\n'
        script = (tp.SUPPORT.replace('@PREFIX@', tp.posix(self.case))
                  .replace('@PKGLOG@', tp.posix(self.pkg_log))
                  + table
                  + '\n'.join(tp.extract(name) for name in tp.PREFLIGHT_FUNCTIONS) + '\n'
                  + extra + '\n' + body)
        path = self.case / 'run.sh'
        path.write_text(script, encoding='utf-8', newline='\n')
        return subprocess.run([tp.BASH, tp.posix(path)], cwd=str(self.case),
                              capture_output=True, text=True, encoding='utf-8', timeout=60)

    def assertRefused(self, result, needle):
        output = result.stdout + result.stderr
        self.assertIn('RC=1', result.stdout, output[-1500:])
        self.assertIn('reinstall not started: unprovable state', result.stdout, output[-1500:])
        self.assertIn(needle, output, output[-1500:])

    # ------------------------------------------------------------- G regressions
    @unittest.skipUnless(win_symlink.NATIVE_SYMLINKS, win_symlink.SYMLINK_REASON)
    def test_g1_preflight_refuses_a_symlink_where_the_snapshot_stores_bytes(self):
        """/usr/bin/geo-backend.sh is a byte-snapshot object (SB): a symlink must refuse."""
        os.symlink('missing-target.sh', self.case / 'usr/bin/geo-backend.sh')
        result = self.run_preflight()
        self.assertRefused(result, 'symlink')

    @unittest.skipUnless(win_symlink.NATIVE_SYMLINKS, win_symlink.SYMLINK_REASON)
    def test_g2_preflight_refuses_a_symlinked_tracked_directory(self):
        shutil.rmtree(self.case / 'etc/mihomo')
        os.symlink(str(self.root / 'outside-mihomo'), self.case / 'etc/mihomo')
        result = self.run_preflight()
        self.assertRefused(result, 'symlink')

    def test_g3_the_healthy_fixture_still_passes(self):
        result = self.run_preflight()
        self.assertIn('RC=0', result.stdout, result.stdout + result.stderr)

    def test_g4_product_byte_table_matches_the_manifest(self):
        source = tp.SOURCE
        self.assertIn('REINSTALL_PREFLIGHT_BYTE_FILES', source,
                      'the product has no byte-snapshot object table')
        rows = byte_table_rows(source)
        self.assertIsNotNone(rows, 'REINSTALL_PREFLIGHT_BYTE_FILES missing from geomask.sh.in')
        manifest = rm.load_manifest()
        expected = {}
        for entry in manifest['files']:
            if entry.get('snapshot') in ('SB', 'SBA', 'SAP'):
                expected[entry['path']] = ('dir-or-absent' if entry['type'] == 'dir'
                                           else 'regular-or-absent')
        self.assertEqual(expected, rows,
                         'product byte table and manifest byte-snapshot objects diverge')

    def test_g5_the_preflight_boundary_is_documented(self):
        meta = rm.load_manifest()['meta']
        self.assertIn('preflight_boundary', meta,
                      'the manifest does not document the preflight observability boundary')
        boundary = json.dumps(meta['preflight_boundary'], ensure_ascii=False)
        for marker in ('dynamic', 'NONE', 'symlink'):
            self.assertIn(marker, boundary, 'boundary misses %r' % marker)


if __name__ == '__main__':
    unittest.main()
