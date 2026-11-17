"""The G/N reconciliation table must not claim a fix the tree cannot show.

R10 (item 6) requires one row per legacy finding (`G01-G06`, `N00-N12`) with the current
file:line fact, a status and evidence. A row marked FIXED is only credible when it names a
test, a document or a commit hash; a row marked OPEN must name the R-task that owns the
remaining work. This test reads the document only - no network, no `ip`, no router.

Run: python -B -m unittest test_gn_mapping  (from tests/, PYTHONPATH=tests)
"""
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'docs' / 'GN-MAPPING.md'

IDS = ['G%02d' % n for n in range(1, 7)] + ['N%02d' % n for n in range(0, 13)]
STATUSES = ('FIXED', 'OPEN', 'OUT-OF-SCOPE')

ID_RE = re.compile(r'^(?:G0[1-6]|N(?:0[0-9]|1[0-2]))$')
FACT_RE = re.compile(r'[A-Za-z0-9_./-]+:\d+')
OWNER_RE = re.compile(r'R(?:0[1-9]|1[0-2])(?![A-Za-z0-9_])')
COMMIT_RE = re.compile(r'(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])')


def split_row(line):
    """Cells of one markdown table row, or None when the line is not a 4-column row."""
    text = line.strip()
    if not text.startswith('|'):
        return None
    cells = [cell.strip() for cell in text.strip('|').split('|')]
    if len(cells) != 4:
        return None
    return cells


def data_rows(text):
    """[(line_number, cells)] for the data rows of the mapping table."""
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        cells = split_row(line)
        if cells is None:
            continue
        if cells[0].upper() == 'ID':
            continue
        if all(set(cell) <= set('-: ') for cell in cells):
            continue
        rows.append((number, cells))
    return rows


def status_of(cell):
    cleaned = cell.strip('`* ')
    return cleaned.split(' ', 1)[0].split('(', 1)[0].strip()


class GnMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exists = DOC.is_file()
        cls.text = DOC.read_text(encoding='utf-8') if cls.exists else ''
        cls.rows = data_rows(cls.text)

    def ensure_document(self):
        self.assertTrue(self.exists,
                        'missing %s - R10 requires the G/N table in the repository' % DOC)

    def test_the_mapping_table_exists_and_has_rows(self):
        self.ensure_document()
        self.assertTrue(self.rows, '%s has no 4-column table rows' % DOC.name)

    def test_every_legacy_id_has_exactly_one_row(self):
        self.ensure_document()
        seen = {}
        for number, cells in self.rows:
            ident = cells[0].strip('`* ')
            if not ID_RE.match(ident):
                self.fail('%s:%d has an unknown ID %r (expected G01-G06 or N00-N12)'
                          % (DOC.name, number, ident))
            seen.setdefault(ident, []).append(number)
        duplicated = ['%s (lines %s)' % (ident, ', '.join(map(str, lines)))
                      for ident, lines in seen.items() if len(lines) > 1]
        missing = ['%s' % ident for ident in IDS if ident not in seen]
        problems = []
        if missing:
            problems.append('missing IDs: %s' % ', '.join(missing))
        if duplicated:
            problems.append('IDs present more than once: %s' % '; '.join(duplicated))
        unknown = sorted(set(seen) - set(IDS))
        if unknown:
            problems.append('unexpected IDs: %s' % ', '.join(unknown))
        self.assertEqual([], problems, '%s: %s' % (DOC.name, '; '.join(problems)))
        self.assertEqual(sorted(IDS), sorted(seen),
                         '%s must list exactly the 19 legacy IDs' % DOC.name)

    def test_every_row_has_a_current_file_line_fact(self):
        self.ensure_document()
        problems = []
        for number, cells in self.rows:
            if not cells[1]:
                problems.append('line %d: empty fact column' % number)
            elif not FACT_RE.search(cells[1]):
                problems.append('line %d (%s): fact has no file:line reference: %r'
                                % (number, cells[0], cells[1][:80]))
        self.assertEqual([], problems, '\n'.join(problems))

    def test_every_status_is_known(self):
        self.ensure_document()
        problems = []
        for number, cells in self.rows:
            status = status_of(cells[2])
            if status not in STATUSES:
                problems.append('line %d (%s): unknown status %r (allowed: %s)'
                                % (number, cells[0], cells[2], ', '.join(STATUSES)))
        self.assertEqual([], problems, '\n'.join(problems))

    def test_fixed_rows_cite_a_test_document_or_commit(self):
        self.ensure_document()
        problems = []
        for number, cells in self.rows:
            if status_of(cells[2]) != 'FIXED':
                continue
            evidence = cells[3]
            if 'tests/' in evidence or 'docs/' in evidence or COMMIT_RE.search(evidence):
                continue
            problems.append('line %d (%s): FIXED without a tests/, docs/ or commit reference: %r'
                            % (number, cells[0], evidence[:100]))
        self.assertEqual([], problems,
                         'a FIXED status must name its proof:\n' + '\n'.join(problems))

    def test_open_rows_name_the_owning_r_task(self):
        self.ensure_document()
        problems = []
        for number, cells in self.rows:
            if status_of(cells[2]) != 'OPEN':
                continue
            if OWNER_RE.search(cells[2]) or OWNER_RE.search(cells[3]):
                continue
            problems.append('line %d (%s): OPEN without an R-task owner (R01-R12): %r'
                            % (number, cells[0], cells[2]))
        self.assertEqual([], problems,
                         'an OPEN status must name its owner:\n' + '\n'.join(problems))

    def test_out_of_scope_rows_say_why(self):
        self.ensure_document()
        problems = []
        for number, cells in self.rows:
            if status_of(cells[2]) != 'OUT-OF-SCOPE':
                continue
            if len(cells[3]) < 20:
                problems.append('line %d (%s): OUT-OF-SCOPE without a reason' % (number, cells[0]))
        self.assertEqual([], problems, '\n'.join(problems))


if __name__ == '__main__':
    unittest.main()
