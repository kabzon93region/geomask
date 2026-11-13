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
#   --wan-iface N   interface to write instead of the template's pppoe-wan (DHCP WAN, etc.)
import sys, os, re, time

try:  # PyYAML is the honest parser; the line scan below is only a fallback
    import yaml
except Exception:
    yaml = None

TEMPLATE = '/etc/geomask/lists/magitrickle-groups.yaml'
CONFIG = '/etc/magitrickle/state/config.yaml'

def parse_args():
    mode = 'write'
    tpl, cfg, wan = TEMPLATE, CONFIG, ''
    for a in sys.argv[1:]:
        if a == '--check-empty': mode = 'check'
        elif a == '--force': mode = 'write'
        elif a.startswith('--template='): tpl = a.split('=', 1)[1]
        elif a.startswith('--config='): cfg = a.split('=', 1)[1]
        elif a.startswith('--wan-iface='): wan = a.split('=', 1)[1]
    return mode, tpl, cfg, wan

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
    """Groups in the `groups:` block, whatever the indentation.

    A line scan that only counted `- id:` in column 0 read a perfectly valid config with
    indented entries as "empty" and auto-seed then replaced the owner's groups with the
    template. The block is parsed as YAML when PyYAML is present; the scan below matches
    any indentation and refuses to guess when the file does not parse.
    """
    text = '\n'.join(lines)
    if yaml is not None:
        try:
            data = yaml.safe_load(text) or {}
        except Exception:
            # unreadable is not empty: never let auto-seed overwrite it
            return -1
        groups = data.get('groups') if isinstance(data, dict) else None
        return len(groups) if isinstance(groups, list) else 0
    gi, si = groups_region(lines)
    if gi is None:
        return 0
    return sum(1 for l in lines[gi + 1:si] if re.match(r'^\s*-\s*id:', l))


def remap_wan(lines, wan_iface):
    """Point the template's direct groups at the interface this router actually has."""
    if not wan_iface:
        return lines
    return [re.sub(r'^(\s*interface:\s*)pppoe-wan\s*$', r'\g<1>' + wan_iface, line)
            for line in lines]

def main():
    mode, tpl, cfg, wan = parse_args()
    if not os.path.exists(tpl):
        print('ERR template missing: %s' % tpl); return 2
    tlines = read_lines(tpl)
    ti, _ = groups_region(tlines)
    if ti is None:
        print('ERR template has no `groups:` section'); return 2
    groups_lines = remap_wan(tlines[ti:], wan)
    while groups_lines and groups_lines[-1].strip() == '':
        groups_lines.pop()
    if not any(l.startswith('- id:') for l in groups_lines):
        print('ERR template groups block is empty'); return 2

    cur = read_lines(cfg) if os.path.exists(cfg) else None
    ng = count_groups(cur) if cur else 0

    if mode == 'check':
        # exit 0 only when the live config is certainly empty; -1 (unparseable) is not empty
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
