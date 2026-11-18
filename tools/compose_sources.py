#!/usr/bin/env python3
"""Compose the three authored delivery targets from ordered source parts (stage 0).

Stage 0 of the readiness plan moves the big authored sources (the installer template, the
backend and the LuCI page) into small parts under `src/<target>/`, while the delivery paths
keep their current names: the composition result is byte-identical to the file the build,
the catalogue and the tests already read. Nothing here runs at install time - parts exist
only for development and the build.

The composer is deliberately dumb and total:

* a manifest is a fixed ordered list of part files inside the target directory
  (`src/installer/order.json`), never an include graph, so a cycle cannot exist;
* parts are concatenated as raw bytes with no separator, wrapper, comment or source map -
  a part boundary is not visible in the result, which is what keeps shell globals, traps,
  `$0`, heredocs and lock file descriptors unchanged;
* a part must be a regular file inside the target root, listed once, and every listed part
  must exist; anything else is refused before any output is touched;
* `--check` is read-only and reports drift with the part that changed; `--write` publishes
  only after the whole composition validated.

Usage:
    python -B tools/compose_sources.py --check [--target installer]
    python -B tools/compose_sources.py --write [--target installer]
    python -B tools/compose_sources.py --list
    python -B tools/compose_sources.py --inventory

Targets without a manifest are reported as "monolith" (stage 0a/0b are delivered, the move
of a target happens in 0v): `--check` accepts them, so the tool can land before the split.
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The three delivery targets stage 0 may compose. Delivery path -> part directory.
TARGETS = {
    'installer': {'output': 'geomask.sh.in', 'parts': 'src/installer'},
    'backend': {'output': 'assets/geo-backend.sh', 'parts': 'src/backend'},
    'panel': {'output': 'assets/geo-sync-page.js', 'parts': 'src/panel'},
}

# The authored sources the plan's inventory calls oversized (>=500 lines) or generated.
INVENTORY = [
    ('geomask.sh', 'generated'),
    ('geomask.sh.in', 'installer'),
    ('assets/geo-backend.sh', 'backend'),
    ('assets/geo-sync-page.js', 'panel'),
    ('assets/geomask-doctor.py', ''),
    ('assets/geomask-diag-spool.py', ''),
    ('assets/geomask-config-tx.py', ''),
    ('assets/geomask-node-audit.py', ''),
    ('assets/geomask-routes.sh', ''),
    ('assets/geomask-dnsguard.sh', ''),
    ('assets/geomask-apply.py', ''),
    ('assets/geomask-diag-redact.py', ''),
    ('assets/geomask-module-update.py', ''),
    ('tools/module_manifest.py', ''),
    ('assets/geomask-audit-state.py', ''),
    ('assets/geomask-node-scan.sh', ''),
    ('tools/safe_write.py', ''),
]


class ComposeError(Exception):
    """A manifest or part that must not be published."""


def _read_bytes(path):
    with open(path, 'rb') as handle:
        return handle.read()


def load_manifest(target, root=ROOT):
    """Ordered part paths for a target, validated; [] means "not migrated yet"."""
    spec = TARGETS.get(target)
    if spec is None:
        raise ComposeError('unknown target %r (known: %s)'
                           % (target, ', '.join(sorted(TARGETS))))
    parts_dir = os.path.join(root, spec['parts'])
    manifest_path = os.path.join(parts_dir, 'order.json')
    if not os.path.exists(manifest_path):
        return []
    try:
        data = json.loads(_read_bytes(manifest_path).decode('utf-8'))
    except (OSError, ValueError) as exc:
        raise ComposeError('cannot read %s: %s' % (manifest_path, exc)) from None
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ComposeError('%s must be a schema-1 object' % manifest_path)
    if data.get('target') not in (None, target):
        raise ComposeError('%s declares target %r, not %r'
                           % (manifest_path, data.get('target'), target))
    raw_parts = data.get('parts')
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ComposeError('%s must list at least one part' % manifest_path)
    parts = []
    seen = set()
    parts_real = os.path.realpath(parts_dir)
    for entry in raw_parts:
        rel = entry.get('path') if isinstance(entry, dict) else entry
        if not isinstance(rel, str) or not rel:
            raise ComposeError('%s has a part without a path' % manifest_path)
        joined = os.path.join(parts_dir, rel)
        if os.path.islink(joined):
            # Before realpath: resolving first would look like a path outside the root and
            # hide the real reason (a symlink is not an authored part).
            raise ComposeError('part %r is a symlink - parts are authored files' % rel)
        candidate = os.path.realpath(joined)
        if os.path.commonpath([candidate, parts_real]) != parts_real:
            raise ComposeError('part %r is outside %s' % (rel, spec['parts']))
        if candidate in seen:
            raise ComposeError('part %r is listed twice' % rel)
        seen.add(candidate)
        if not os.path.isfile(candidate):
            raise ComposeError('part %r does not exist' % rel)
        parts.append(candidate)
    unlisted = sorted(
        os.path.relpath(os.path.join(base, name), parts_dir)
        for base, _dirs, files in os.walk(parts_dir) for name in files
        if name != 'order.json' and not name.startswith('.'))
    known = {os.path.relpath(path, parts_dir) for path in parts}
    extra = [name for name in unlisted if name not in known]
    if extra:
        raise ComposeError('part(s) not listed in %s: %s'
                           % (manifest_path, ', '.join(extra)))
    return parts


def compose_target(target, root=ROOT):
    """Exact bytes of a target: the parts concatenated in order, nothing else."""
    parts = load_manifest(target, root)
    if not parts:
        return None
    return b''.join(_read_bytes(path) for path in parts)


def output_path(target, root=ROOT):
    spec = TARGETS.get(target)
    if spec is None:
        raise ComposeError('unknown target %r' % target)
    return os.path.join(root, spec['output'])


def target_for_output(output_name, root=ROOT):
    """Which target publishes this delivery file (None when it is not a target)."""
    for name, spec in TARGETS.items():
        if os.path.normpath(spec['output']) == os.path.normpath(output_name):
            return name
    return None


def write_target_for_asset(output_name, root=ROOT):
    """Refresh the view of the asset a generator just wrote a part for.

    Returns the number of bytes published, or None when the file is not a migrated target
    (the caller then keeps writing the asset itself, as before the move).
    """
    target = target_for_output(output_name, root)
    if target is None or compose_target(target, root) is None:
        return None
    return write_target(target, root)


def check_target(target, root=ROOT):
    """(status, detail): status is 'monolith', 'ok' or 'drift'."""
    composed = compose_target(target, root)
    if composed is None:
        return 'monolith', '%s has no parts yet (%s/order.json)' % (target, TARGETS[target]['parts'])
    path = output_path(target, root)
    try:
        current = _read_bytes(path)
    except OSError as exc:
        return 'drift', 'cannot read %s: %s' % (path, exc)
    if composed == current:
        return 'ok', '%s: %d bytes from %d parts' % (target, len(composed),
                                                     len(load_manifest(target, root)))
    parts = load_manifest(target, root)
    offset = 0
    first = ''
    for path_part in parts:
        data = _read_bytes(path_part)
        if current[offset:offset + len(data)] != data:
            first = os.path.relpath(path_part, root)
            break
        offset += len(data)
    if not first and len(current) != len(composed):
        first = 'end of composition (%d bytes composed, %d installed)' % (len(composed),
                                                                         len(current))
    return 'drift', '%s differs from its parts (first difference: %s)' % (target, first or '?') \
        if first else '%s differs from its parts' % target


def write_target(target, root=ROOT):
    composed = compose_target(target, root)
    if composed is None:
        raise ComposeError('%s has no parts - nothing to compose' % target)
    # Validate every target before publishing any of them: a half-published set is worse
    # than a refusal, because the build reads all three.
    for name in TARGETS:
        try:
            compose_target(name, root)
        except ComposeError as exc:
            raise ComposeError('%s is not composable, nothing published: %s' % (name, exc)) from None
    path = output_path(target, root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        raise ComposeError('cannot create %s: %s' % (os.path.dirname(path), exc)) from None
    tmp = '%s.compose.%d' % (path, os.getpid())
    try:
        with open(tmp, 'wb') as handle:
            handle.write(composed)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ComposeError('cannot publish %s: %s' % (path, exc)) from None
    return len(composed)


def inventory(root=ROOT):
    rows = []
    for rel, role in INVENTORY:
        path = os.path.join(root, rel)
        try:
            with open(path, 'rb') as handle:
                data = handle.read()
        except OSError:
            rows.append((rel, role, None, None))
            continue
        rows.append((rel, role, data.count(b'\n') + (0 if data.endswith(b'\n') or not data else 1),
                     hashlib.sha256(data).hexdigest()))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog='compose_sources')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true')
    group.add_argument('--write', action='store_true')
    group.add_argument('--list', action='store_true')
    group.add_argument('--inventory', action='store_true')
    parser.add_argument('--target', action='append', default=[])
    parser.add_argument('--root', default=ROOT)
    args = parser.parse_args(argv)
    targets = args.target or list(TARGETS)
    try:
        if args.inventory:
            for rel, role, lines, digest in inventory(args.root):
                if lines is None:
                    print('%-40s MISSING' % rel)
                    continue
                print('%-40s %6s lines  %s  %s' % (rel, lines, digest[:16], role or ''))
            return 0
        if args.list:
            for name in targets:
                parts = load_manifest(name, args.root)
                print('%s: %d parts' % (name, len(parts)))
                for path in parts:
                    print('  %s' % os.path.relpath(path, args.root))
            return 0
        if args.write:
            for name in targets:
                size = write_target(name, args.root)
                print('%s: published %d bytes' % (name, size))
            return 0
        rc = 0
        for name in targets:
            status, detail = check_target(name, args.root)
            print('%s: %s - %s' % (name, status, detail))
            if status == 'drift':
                rc = 1
        return rc
    except ComposeError as exc:
        print('refused: %s' % exc, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
