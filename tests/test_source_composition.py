"""Stage 0b: the composer is exact, total and refuses before it publishes.

The delivery paths keep their names and bytes while the authored sources move to ordered
parts. These tests fix the contract: raw concatenation of the listed parts, no separators, a
fixed order, and a refusal (without touching the output) for every malformed manifest.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import compose_sources as composer  # noqa: E402


class ComposerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='gm-compose-'))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.root = self.tmp
        for name, spec in composer.TARGETS.items():
            (self.root / spec['parts']).mkdir(parents=True, exist_ok=True)

    def manifest(self, target, parts, extra=None):
        data = {'schema': 1, 'target': target, 'parts': parts}
        if extra:
            data.update(extra)
        path = self.root / composer.TARGETS[target]['parts'] / 'order.json'
        path.write_text(json.dumps(data, indent=2), encoding='utf-8', newline='\n')
        return path

    def part(self, target, name, payload):
        path = self.root / composer.TARGETS[target]['parts'] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def output(self, target, payload):
        path = pathlib.Path(composer.output_path(target, str(self.root)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    # ---------------------------------------------------------------- composition
    def test_parts_are_concatenated_in_order_without_separators(self):
        self.part('installer', 'a.sh.part', b'first\n')
        self.part('installer', 'b.sh.part', b'second')
        self.manifest('installer', ['a.sh.part', 'b.sh.part'])
        composed = composer.compose_target('installer', str(self.root))
        self.assertEqual(composed, b'first\nsecond',
                         'the composer added or lost bytes at a part boundary')

    def test_a_changed_part_makes_check_fail_without_rewriting(self):
        self.part('backend', 'main.sh.part', b'#!/bin/sh\necho one\n')
        self.manifest('backend', ['main.sh.part'])
        target = self.output('backend', b'#!/bin/sh\necho one\n')
        self.assertEqual(composer.check_target('backend', str(self.root))[0], 'ok')
        self.part('backend', 'main.sh.part', b'#!/bin/sh\necho two\n')
        status, detail = composer.check_target('backend', str(self.root))
        self.assertEqual(status, 'drift')
        self.assertIn('main.sh.part', detail)
        self.assertEqual(target.read_bytes(), b'#!/bin/sh\necho one\n',
                         '--check rewrote the delivery file')

    def test_write_publishes_the_composition_after_validation(self):
        self.part('panel', 'view.js.part', b'/* part */\n')
        self.manifest('panel', ['view.js.part'])
        self.output('panel', b'old')
        self.assertEqual(composer.write_target('panel', str(self.root)), 11)
        self.assertEqual(pathlib.Path(composer.output_path('panel', str(self.root))).read_bytes(),
                         b'/* part */\n')

    def test_a_target_without_parts_is_a_monolith_not_an_error(self):
        status, detail = composer.check_target('installer', str(self.root))
        self.assertEqual(status, 'monolith')
        self.assertIn('no parts', detail)
        self.assertFalse((self.root / composer.TARGETS['installer']['parts'] / 'order.json').exists())

    # ---------------------------------------------------------------- refusals
    def test_an_unknown_target_is_refused(self):
        with self.assertRaises(composer.ComposeError):
            composer.compose_target('rpc', str(self.root))

    def test_a_missing_part_is_refused(self):
        self.manifest('installer', ['gone.sh.part'])
        with self.assertRaises(composer.ComposeError) as caught:
            composer.compose_target('installer', str(self.root))
        self.assertIn('does not exist', str(caught.exception))

    def test_a_duplicate_part_is_refused(self):
        self.part('installer', 'a.sh.part', b'x')
        self.manifest('installer', ['a.sh.part', 'a.sh.part'])
        with self.assertRaises(composer.ComposeError) as caught:
            composer.compose_target('installer', str(self.root))
        self.assertIn('twice', str(caught.exception))

    def test_an_unlisted_part_is_refused(self):
        self.part('installer', 'a.sh.part', b'x')
        self.part('installer', 'forgotten.sh.part', b'y')
        self.manifest('installer', ['a.sh.part'])
        with self.assertRaises(composer.ComposeError) as caught:
            composer.compose_target('installer', str(self.root))
        self.assertIn('not listed', str(caught.exception))

    def test_a_part_outside_the_target_root_is_refused(self):
        outside = self.root / 'elsewhere.sh'
        outside.write_bytes(b'x')
        self.manifest('installer', ['../elsewhere.sh'])
        with self.assertRaises(composer.ComposeError) as caught:
            composer.compose_target('installer', str(self.root))
        self.assertIn('outside', str(caught.exception))

    def test_a_symlinked_part_is_refused(self):
        real = self.root / 'real.sh'
        real.write_bytes(b'x')
        link = self.root / composer.TARGETS['installer']['parts'] / 'link.sh.part'
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest('posix-only')
        self.manifest('installer', ['link.sh.part'])
        with self.assertRaises(composer.ComposeError) as caught:
            composer.compose_target('installer', str(self.root))
        self.assertIn('symlink', str(caught.exception))

    def test_a_broken_manifest_is_refused(self):
        path = self.root / composer.TARGETS['installer']['parts'] / 'order.json'
        path.write_text('{not json', encoding='utf-8')
        with self.assertRaises(composer.ComposeError):
            composer.compose_target('installer', str(self.root))

    def test_a_manifest_for_another_target_is_refused(self):
        self.part('backend', 'a.sh.part', b'x')
        path = self.manifest('backend', ['a.sh.part'])
        data = json.loads(path.read_text(encoding='utf-8'))
        data['target'] = 'installer'
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(composer.ComposeError):
            composer.compose_target('backend', str(self.root))

    def test_write_refuses_before_publishing_any_target(self):
        self.part('installer', 'a.sh.part', b'ok')
        self.manifest('installer', ['a.sh.part'])
        self.manifest('backend', ['missing.sh.part'])
        before = pathlib.Path(composer.output_path('installer', str(self.root)))
        before.parent.mkdir(parents=True, exist_ok=True)
        before.write_bytes(b'original')
        with self.assertRaises(composer.ComposeError):
            composer.write_target('installer', str(self.root))
        self.assertEqual(before.read_bytes(), b'original',
                         'a published target came from a set that failed validation')

    # ---------------------------------------------------------------- CLI
    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(ROOT / 'tools' / 'compose_sources.py')]
                              + list(args), capture_output=True, text=True, encoding='utf-8')

    def test_cli_check_is_read_only_and_names_the_drift(self):
        self.part('backend', 'main.sh.part', b'one')
        self.manifest('backend', ['main.sh.part'])
        target = self.output('backend', b'one')
        result = self.run_cli('--check', '--root', str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.part('backend', 'main.sh.part', b'two')
        result = self.run_cli('--check', '--root', str(self.root))
        self.assertEqual(result.returncode, 1)
        self.assertIn('drift', result.stdout)
        self.assertEqual(target.read_bytes(), b'one')

    def test_cli_write_publishes(self):
        self.part('panel', 'view.js.part', b'bytes')
        self.manifest('panel', ['view.js.part'])
        result = self.run_cli('--write', '--root', str(self.root), '--target', 'panel')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(pathlib.Path(composer.output_path('panel', str(self.root))).read_bytes(),
                         b'bytes')

    def test_cli_refusal_is_not_a_publication(self):
        self.manifest('installer', ['nope.sh.part'])
        result = self.run_cli('--write', '--root', str(self.root), '--target', 'installer')
        self.assertEqual(result.returncode, 2)
        self.assertIn('refused', result.stderr)
        self.assertFalse(pathlib.Path(composer.output_path('installer', str(self.root))).exists())

    def test_cli_inventory_reports_the_frozen_baseline_rows(self):
        result = self.run_cli('--inventory')
        self.assertEqual(result.returncode, 0)
        self.assertIn('geomask.sh.in', result.stdout)
        self.assertIn('generated', result.stdout)

    def test_the_real_tree_is_not_migrated_yet_and_check_says_so(self):
        result = self.run_cli('--check')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in composer.TARGETS:
            self.assertIn('%s: monolith' % name, result.stdout)


if __name__ == '__main__':
    unittest.main()
