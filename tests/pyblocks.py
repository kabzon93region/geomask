"""Syntax-check every python heredoc embedded in a geomask shell wrapper.

Test-side extractor shared with `tools/pycheck.py`. The old `tools/pycheck.py` split at the
first `<<'PY'` and passed the rest of the invocation line (for example ` >/dev/null 2>&1`)
into `ast.parse`, so `assets/geomask-dnsguard.sh` was reported as a syntax error even though
its block is valid.

Supported forms: `<<'PY'`, `<<"PY"`, `<<PY`, `<<-'PY'` (and the same with PYEOF), any
trailing redirection/arguments on the opener line, and more than one block per file.

Usage: python -B tests/pyblocks.py assets/*.sh
Exit: 0 all blocks parse, 1 a block does not parse, 2 no blocks were found at all.
"""
import ast
import re
import sys

MARKERS = ('PYEOF', 'PY')


def _opener(marker):
    """Match any shell heredoc opener for marker; the body starts after the newline."""
    escaped = re.escape(marker)
    return re.compile(
        r"<<-?\s*(?:'%s'|\"%s\"|%s(?![A-Za-z0-9_]))[^\n]*\n" % (escaped, escaped, escaped))


def _closer(marker):
    # `<<-` allows tabs before the terminator; trailing spaces are harmless either way.
    return re.compile(r'(?m)^[ \t]*%s[ \t]*$' % re.escape(marker))


_OPEN = {marker: _opener(marker) for marker in MARKERS}
_CLOSE = {marker: _closer(marker) for marker in MARKERS}


def blocks(text):
    """Return [(marker, opener_line, body)] for every embedded python heredoc."""
    found = []
    for marker in MARKERS:
        pos = 0
        while True:
            match = _OPEN[marker].search(text, pos)
            if match is None:
                break
            close = _CLOSE[marker].search(text, match.end())
            if close is None:
                break
            # close.start() is an absolute offset into text (search started at match.end()).
            body = text[match.end():close.start()]
            found.append((marker, text.count('\n', 0, match.start()) + 1, body))
            pos = close.end()
    found.sort(key=lambda item: item[1])
    return found


def main(argv):
    rc = 0
    total = 0
    for path in argv:
        try:
            text = open(path, encoding='utf-8').read()
        except OSError as exc:
            print('%s: cannot read (%s)' % (path, exc))
            rc = 1
            continue
        found = list(blocks(text))
        if not found:
            print('%s: no embedded python block' % path)
            continue
        for marker, line, body in found:
            total += 1
            try:
                ast.parse(body)
            except SyntaxError as exc:
                print('%s:%s (%s): SYNTAX ERROR: %s (line %s)'
                      % (path, line, marker, exc.msg, exc.lineno))
                rc = 1
            else:
                print('%s:%s (%s): python parses OK (%d chars)'
                      % (path, line, marker, len(body)))
    print('total embedded blocks: %d' % total)
    if total == 0:
        return 2
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
