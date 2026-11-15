"""The release notes must not claim more than the tree can show.

Review 14-15.11.2026, point 31 (T10). The documentation still carried the "green numbers" of an
earlier tree (626 unit tests, 439 sandbox passes), a "G04 fixed" headline that was only ever
checked locally, an "instant re-enable" that a mihomo reload makes false, and a bundle
description that no longer matched the code. None of that is a code defect, and none of it is
visible to a test that only reads the repository - so this file checks the owner's documents by
the rules they have to satisfy.

The documents live beside the checkout (Y:\\openwrt\\docs), not in it: CI has no such directory
and skips these tests. Where they are present, a stale claim fails here instead of travelling
into a release note.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT.parent / 'docs'
FIXES = DOCS / 'FIXES-2026-11-14-v1.31.0.md'
HOWTO = DOCS / 'HOWTO-CONSOLE-TEST.md'
OWNERSHIP = DOCS / 'project' / 'OWNERSHIP.md'

# Claims the tree cannot back any more. Each entry is (needle, why it is false).
STALE = [
    ('626 тестов',
     'the count belongs to a tree from before the review; numbers must name the build they '
     'were measured on'),
    ('439 pass',
     'the sandbox suite was rewritten in T1 and is not measured by that number any more'),
    ('G04 исправлен.',
     'the IPv6 revert is covered by local tests only; on a router it has not been checked'),
    ('повторное включение мгновенное',
     'the re-enable reloads mihomo and takes seconds'),
    ('Архив появится в `/tmp`',
     'the bundle is built in a private temporary directory and now downloads from the panel'),
    ('в `/tmp` и в папку бэкапов, если шара включена',
     'the copy into the share needs the opt-in marker, and the panel no longer needs it at all'),
    ('архив **не попадают**',
     'the masking is a pattern filter; the document must not promise a clean archive'),
]

# A retired claim may be quoted as history; it must not be presented as the current state.
RETIRED = ('больше не', 'не действ', 'другое дерево', 'прежн', 'истори', 'отозван')


def read(path):
    if not path.is_file():
        return None
    return path.read_text(encoding='utf-8')


@unittest.skipIf(read(FIXES) is None and read(HOWTO) is None,
                 'the owner documentation tree is not next to this checkout')
class DocumentationClaimTests(unittest.TestCase):
    def documents(self):
        found = {}
        for path in (FIXES, HOWTO):
            text = read(path)
            if text is not None:
                found[path.name] = text
        return found

    def test_no_document_repeats_a_claim_the_tree_cannot_back(self):
        problems = []
        for name, text in self.documents().items():
            for number, line in enumerate(text.splitlines(), 1):
                for needle, why in STALE:
                    if needle not in line:
                        continue
                    if any(marker in line.lower() for marker in RETIRED):
                        continue      # quoted as retired history, not as the current state
                    problems.append('%s:%d says %r - %s' % (name, number, needle, why))
        self.assertEqual([], problems, '\n'.join(problems))

    def test_the_notes_say_what_was_not_checked(self):
        """A release note without an explicit "not verified" list reads as a guarantee."""
        for name, text in self.documents().items():
            if name.startswith('FIXES'):
                self.assertIn('Не проверено', text,
                              'the release notes must name what was not verified')
                self.assertIn('роутер', text.lower(),
                              'the notes must say the router was not touched')

    def test_the_console_howto_offers_the_download(self):
        text = read(HOWTO)
        self.assertIsNotNone(text)
        self.assertIn('Скачать', text,
                      'the how-to still tells the owner to fetch the archive by hand')


@unittest.skipIf(read(OWNERSHIP) is None, 'the ownership map is not next to this checkout')
class OwnershipMapTests(unittest.TestCase):
    def test_the_console_lane_has_a_named_owner(self):
        text = read(OWNERSHIP)
        for owner in ('console.state', 'таблиц', 'dnsguard', 'owned-'):
            self.assertIn(owner, text,
                          'the ownership map does not name the owner of %r' % owner)


if __name__ == '__main__':
    unittest.main()
