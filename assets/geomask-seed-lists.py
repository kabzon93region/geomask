#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# geomask-seed-lists.py - restore MagiTrickle domain lists from the geomask template.
#
# The template ships the full `groups:` block (25 groups / ~3300 rules, no secrets).
# This tool splices that block into the live MagiTrickle config, preserving the host's
# `app:` settings and trailing keys. magitrickled must be STOPPED while it writes
# (the daemon owns the file); the geomask shell wrapper handles stop/start + sync.
#
# Modes:
#   --check-empty   exit 0 if the live config has 0 groups (safe to auto-seed), else 1. No writes.
#   (default)       write template groups into config (backup first), print CHANGED summary.
#   --force         same as default; used by the shell to express intent (this tool always overwrites).
#   --template P    template path (default /etc/geomask/lists/magitrickle-groups.yaml)
#   --config P      config path  (default /etc/magitrickle/state/config.yaml)
import sys, os, re, time

TEMPLATE = '/etc/geomask/lists/magitrickle-groups.yaml'
CONFIG = '/etc/magitrickle/state/config.yaml'

def parse_args():
    mode = 'write'
    tpl, cfg = TEMPLATE, CONFIG
    for a in sys.argv[1:]:
        if a == '--check-empty': mode = 'check'
        elif a == '--force': mode = 'write'
        elif a.startswith('--template='): tpl = a.split('=', 1)[1]
        elif a.startswith('--config='): cfg = a.split('=', 1)[1]
    return mode, tpl, cfg

def read_lines(p):
    with open(p, encoding='utf-8') as f:
        return f.read().split('\n')

def groups_region(lines):
    """Return (gi, si): indices of the `groups:` line and the next top-level key (or len)."""
    gi = None
    for i, l in enumerate(lines):
        if l == 'groups:' and gi is None:
            gi = i
            break
    if gi is None:
        return None, None
    si = len(lines)
    for i in range(gi + 1, len(lines)):
        if re.match(r'^[A-Za-z]', lines[i]):  # next top-level key (subscriptions:, etc.)
            si = i
            break
    return gi, si

def count_groups(lines):
    gi, si = groups_region(lines)
    if gi is None:
        return 0
    return sum(1 for l in lines[gi + 1:si] if l.startswith('- id:'))

def main():
    mode, tpl, cfg = parse_args()
    if not os.path.exists(tpl):
        print('ERR template missing: %s' % tpl); return 2
    tlines = read_lines(tpl)
    ti, _ = groups_region(tlines)
    if ti is None:
        print('ERR template has no `groups:` section'); return 2
    groups_lines = tlines[ti:]
    while groups_lines and groups_lines[-1].strip() == '':
        groups_lines.pop()
    if not any(l.startswith('- id:') for l in groups_lines):
        print('ERR template groups block is empty'); return 2

    cur = read_lines(cfg) if os.path.exists(cfg) else None
    ng = count_groups(cur) if cur else 0

    if mode == 'check':
        # exit 0 if empty (auto-seed is safe), 1 if it already has groups
        sys.exit(0 if ng == 0 else 1)

    if cur is None:
        print('ERR live config missing (%s); start magitrickle first' % cfg); return 2

    gi, si = groups_region(cur)
    if gi is None:
        new = cur + groups_lines
    else:
        new = cur[:gi] + groups_lines + cur[si:]

    ts = time.strftime('%Y%m%d%H%M%S')
    bak = cfg + '.bak_geomask_' + ts
    with open(bak, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(cur).rstrip('\n') + '\n')

    tmp = cfg + '.gm.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(new).rstrip('\n') + '\n')
    os.replace(tmp, cfg)

    ng2 = count_groups(new)
    nrules = sum(1 for l in groups_lines if l.startswith('  - id:'))
    print('CHANGED before=%d after=%d rules=%d backup=%s' % (ng, ng2, nrules, os.path.basename(bak)))
    return 0

if __name__ == '__main__':
    sys.exit(main())
