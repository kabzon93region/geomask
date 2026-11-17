"""Syntax-check the python blocks embedded in geomask shell wrappers.

Usage:
  python tools/pycheck.py assets/geomask-node-scan.sh [...]

The wrappers differ ("exec python3 - <<'PYEOF'", 'exec "$_py" - "$@" <<'PYEOF', etc.),
so instead of matching the invocation line this tool extracts every heredoc block by its
marker. Extraction is shared with `tests/pyblocks.py` (one implementation, no drift):
it ignores the rest of the opener line (`>/dev/null 2>&1`, `"$@"`, ...), supports both
`PY` and `PYEOF`, quoted and unquoted openers, and multiple blocks per file.

Exit status:
  0  every embedded block was found and parses
  1  a block has a real syntax error (reported as file:line) or a file cannot be read
  2  no embedded blocks were found at all (the gate checked nothing)
"""
import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYBLOCKS = ROOT / 'tests' / 'pyblocks.py'


def _load_extractor():
    """Load tests/pyblocks.py without making tools/ depend on a package layout."""
    spec = importlib.util.spec_from_file_location('geomask_pyblocks', PYBLOCKS)
    if spec is None or spec.loader is None:
        raise ImportError('cannot load %s' % PYBLOCKS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.blocks


def main(argv=None):
    paths = list(sys.argv[1:] if argv is None else argv)
    if not paths:
        print('usage: python tools/pycheck.py <shell-wrapper> [...]')
        return 2
    try:
        blocks = _load_extractor()
    except Exception as exc:  # a broken extractor must not look like a clean pass
        print('pycheck: cannot load %s: %s' % (PYBLOCKS, exc))
        return 1

    rc = 0
    total = 0
    for path in paths:
        try:
            text = open(path, encoding='utf-8').read()
        except OSError as exc:
            print('%s: cannot read (%s)' % (path, exc))
            rc = 1
            continue
        found = blocks(text)
        if not found:
            print('%s: no embedded python block' % path)
            continue
        for marker, line, body in found:
            total += 1
            try:
                ast.parse(body)
            except SyntaxError as exc:
                # body line 1 is the line after the opener line.
                absolute = line + (exc.lineno or 1)
                print('%s:%d (%s): SYNTAX ERROR: %s (line %s in block)'
                      % (path, absolute, marker, exc.msg, exc.lineno))
                rc = 1
            else:
                print('%s:%d (%s): python parses OK (%d chars)'
                      % (path, line, marker, len(body)))
    print('total embedded blocks: %d' % total)
    if total == 0:
        print('pycheck: no embedded python blocks found - the gate checked nothing')
        return 2
    return rc


if __name__ == '__main__':
    sys.exit(main())
