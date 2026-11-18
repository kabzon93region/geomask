#!/usr/bin/env python3
"""Advisory repo check for the stage-0 rule "author small sources" (0d).

The plan sets a soft ceiling of ~500 lines per *authored* file and says the check is
advisory during stage 0: it names what is still oversized so the next extraction is a
decision, not a surprise, but it must not break CI for the files the move has not reached
yet.

What counts as authored:
* delivery views of migrated targets (geomask.sh.in, assets/geo-backend.sh,
  assets/geo-sync-page.js) are GENERATED from their parts - they are reported as generated,
  never as oversized;
* the installer (geomask.sh) is the single generated artifact and is never counted;
* a file that is itself a part is counted per part, not by the original size;
* everything else under the tracked implementation paths is authored.

Usage:
    python -B tools/repo_check.py            # advisory report, exit 0
    python -B tools/repo_check.py --strict   # exit 1 when an authored file is oversized
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import compose_sources  # noqa: E402

SOFT_LIMIT = 500
GENERATED = {'geomask.sh'}

# Implementation paths the plan's inventory covers (tests/docs excluded on purpose).
SCAN_DIRS = ('', 'assets', 'tools', 'modules')
SCAN_SUFFIXES = ('.sh', '.py', '.js', '.json', '.in')


def oversized(root=ROOT, limit=SOFT_LIMIT):
    """[(relative path, lines, reason)] for authored files over the soft limit.

    `reason` is 'authored' (the file must be split) or 'generated' (reported, never fails).
    """
    def key(rel):
        return rel.replace(os.sep, '/')

    parts = set()
    generated = {key(name) for name in GENERATED}
    for target in compose_sources.TARGETS:
        manifest = compose_sources.load_manifest(target, root) or []
        for path in manifest:
            parts.add(key(os.path.relpath(path, root)))
        if manifest:
            generated.add(key(compose_sources.TARGETS[target]['output']))

    rows = []
    for sub in SCAN_DIRS:
        base = os.path.join(root, sub) if sub else root
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                continue
            rel = key(os.path.relpath(path, root))
            if not rel.endswith(SCAN_SUFFIXES):
                continue
            if rel in parts:
                continue
            try:
                with open(path, 'rb') as handle:
                    lines = handle.read().count(b'\n')
            except OSError:
                continue
            if lines <= limit:
                continue
            rows.append((rel, lines,
                         'generated' if rel in generated else 'authored'))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog='repo_check')
    parser.add_argument('--strict', action='store_true',
                        help='exit 1 when an authored file is over the soft limit')
    parser.add_argument('--limit', type=int, default=SOFT_LIMIT)
    parser.add_argument('--root', default=ROOT)
    args = parser.parse_args(argv)
    try:
        rows = oversized(args.root, args.limit)
    except compose_sources.ComposeError as exc:
        print('FAIL: cannot read the parts manifest: %s' % exc)
        return 1
    authored = [row for row in rows if row[2] == 'authored']
    generated = [row for row in rows if row[2] == 'generated']
    for rel, lines, kind in generated:
        label = ('the generated installer artifact' if rel == 'geomask.sh'
                 else 'view of its parts - not an authored source')
        print('generated %6d lines  %s (%s)' % (lines, rel, label))
    for rel, lines, _kind in authored:
        print('oversized %6d lines  %s' % (lines, rel))
    if not authored and not generated:
        print('OK: no authored file over %d lines' % args.limit)
        return 0
    print('summary: %d authored file(s) over %d lines, %d generated view(s); '
          'advisory during stage 0' % (len(authored), args.limit, len(generated)))
    return 1 if (args.strict and authored) else 0


if __name__ == '__main__':
    sys.exit(main())
