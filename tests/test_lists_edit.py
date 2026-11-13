"""Editing MagiTrickle lists: add, remove, and refuse to break the file.

Every test runs against a fixture shaped like the live config (groups at column 0, rules
indented by four spaces), because the indentation is what a text edit gets wrong first.
"""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'assets' / 'geomask-lists-edit.py'

FIXTURE = '''configVersion: 0.8.2
app:
  logLevel: error
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
- id: 4e8ce948
  name: GoogleVideo VPN
  color: '#0e7490'
  interface: Mihomo
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
- id: 145328f2
  name: Empty
  interface: pppoe-wan
  enable: false
  rules:
subscriptions: []
'''


def load_tool():
    spec = importlib.util.spec_from_file_location('lists_edit', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SeederSafetyTests(unittest.TestCase):
    """Auto-seed must never read a valid config as empty (audit F11)."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'seed_lists', ROOT / 'assets' / 'geomask-seed-lists.py')
        cls.seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.seed)

    def test_an_indented_group_is_counted(self):
        lines = ['groups:', '  - id: custom', '    name: mine', '    rules: []', '']
        self.assertEqual(self.seed.count_groups(lines), 1)

    def test_an_empty_block_is_empty(self):
        self.assertEqual(self.seed.count_groups(['groups:', '']), 0)

    def test_an_unparseable_config_is_not_treated_as_empty(self):
        lines = ['groups:', '  - id: [broken', '   name: x']
        self.assertEqual(self.seed.count_groups(lines), -1)

    def test_the_wan_interface_is_remapped(self):
        lines = ['groups:', '  - id: a', '    interface: pppoe-wan', '  - id: b']
        out = self.seed.remap_wan(lines, 'eth1')
        self.assertIn('    interface: eth1', out)
        self.assertNotIn('    interface: pppoe-wan', out)
        self.assertEqual(self.seed.remap_wan(lines, ''), lines)


class ListsEditTests(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.yaml'
        self.path.write_text(FIXTURE, encoding='utf-8')
        self.tool.CONFIG = str(self.path)
        self.tool.BACKUP = str(Path(self.tmp.name) / 'bak.yaml')

    def read(self):
        return self.path.read_text(encoding='utf-8')

    def test_groups_are_listed_with_interface_and_counts(self):
        blocks = self.tool.group_blocks(self.tool.read_lines())
        self.assertEqual([b['name'] for b in blocks], ['Torrents', 'GoogleVideo VPN', 'Empty'])
        self.assertEqual([len(b['rules']) for b in blocks], [1, 2, 0])
        self.assertEqual(blocks[2]['interface'], 'pppoe-wan')

    def test_add_inserts_a_rule_with_the_group_indentation(self):
        lines, added = self.tool.plan_add(self.tool.read_lines(), 'GoogleVideo VPN',
                                          ['plati.market', 'digiseller.ru'])
        self.assertEqual(added, ['plati.market', 'digiseller.ru'])
        text = ''.join(lines)
        # same indentation as the rules already in that group (two spaces here)
        self.assertIn('  - id: ', text)
        self.assertIn('    rule: plati.market', text)
        blocks = self.tool.group_blocks(lines)
        self.assertEqual([len(b['rules']) for b in blocks], [1, 4, 0])
        self.tool.validate(lines, 3, 5)

    def test_add_is_idempotent_for_a_listed_domain(self):
        lines, added = self.tool.plan_add(self.tool.read_lines(), 'Torrents',
                                          ['youtube.com', 'plati.market'])
        self.assertEqual(added, ['plati.market'])

    def test_add_refuses_an_unknown_group_and_names_the_real_ones(self):
        with self.assertRaises(self.tool.EditError) as caught:
            self.tool.plan_add(self.tool.read_lines(), 'Nope', ['plati.market'])
        self.assertIn('GoogleVideo VPN', str(caught.exception))

    def test_add_refuses_when_everything_is_already_listed(self):
        with self.assertRaises(self.tool.EditError):
            self.tool.plan_add(self.tool.read_lines(), 'Torrents', ['youtube.com'])

    def test_add_works_into_a_group_with_an_empty_rules_list(self):
        lines, added = self.tool.plan_add(self.tool.read_lines(), 'Empty', ['plati.market'])
        self.assertEqual(added, ['plati.market'])
        text = ''.join(lines)
        self.assertIn('  rules:\n    - id: ', text)
        self.tool.validate(lines, 3, 4)

    def test_remove_deletes_the_whole_rule_block(self):
        lines, removed = self.tool.plan_remove(self.tool.read_lines(), ['youtube.com'])
        self.assertEqual(removed, ['youtube.com'])
        text = ''.join(lines)
        self.assertNotIn('youtube.com', text)
        self.assertNotIn('8a7e8701', text)
        self.tool.validate(lines, 3, 2)

    def test_remove_refuses_a_domain_that_is_not_listed(self):
        with self.assertRaises(self.tool.EditError):
            self.tool.plan_remove(self.tool.read_lines(), ['plati.market'])

    def test_validate_catches_a_broken_edit(self):
        lines = self.tool.read_lines()
        # dropping a rule's `- id:` line makes the file parse with one rule less
        index = next(i for i, line in enumerate(lines) if line.strip() == '- id: 8a7e8701')
        del lines[index]
        with self.assertRaises(self.tool.EditError) as caught:
            self.tool.validate(lines, 3, 3)
        self.assertIn('rules changed', str(caught.exception))

    def test_move_takes_the_domain_out_of_the_old_group(self):
        lines, moved = self.tool.plan_move(self.tool.read_lines(), 'Torrents', ['youtube.com'])
        self.assertEqual(moved, ['youtube.com'])
        text = ''.join(lines)
        blocks = self.tool.group_blocks(lines)
        by_name = {b['name']: b for b in blocks}
        self.assertEqual(len(by_name['Torrents']['rules']), 2)
        self.assertEqual(len(by_name['GoogleVideo VPN']['rules']), 1)
        self.assertIn('rule: youtube.com', text)
        self.tool.validate(lines, 3, 3)

    def test_move_into_the_same_group_changes_nothing(self):
        with self.assertRaises(self.tool.EditError) as caught:
            self.tool.plan_move(self.tool.read_lines(), 'GoogleVideo VPN', ['youtube.com'])
        self.assertIn('already in', str(caught.exception))

    def test_move_adds_a_domain_that_is_nowhere(self):
        lines, moved = self.tool.plan_move(self.tool.read_lines(), 'Empty', ['plati.market'])
        self.assertEqual(moved, ['plati.market'])
        self.tool.validate(lines, 3, 4)

    def test_dry_run_writes_nothing(self):
        before = self.read()
        lines, added = self.tool.plan_add(self.tool.read_lines(), 'Torrents', ['plati.market'])
        message = self.tool.apply(lines, 4, dry_run=True)
        self.assertIn('dry-run', message)
        self.assertEqual(self.read(), before)


if __name__ == '__main__':
    unittest.main()
