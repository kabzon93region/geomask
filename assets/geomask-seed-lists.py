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
#   --check-empty   exit 0 if the live config has 0 groups (safe to auto-seed), 1 if it has
#                   groups, 2 if it cannot be verified (unreadable YAML, duplicate keys,
#                   non-list `groups`). Anything but 0 means "do not auto-seed".
#   (default)       write template groups into config (backup first), print CHANGED summary.
#   --force         same as default; used by the shell to express intent (this tool always overwrites).
#   --template P    template path (default /etc/geomask/lists/magitrickle-groups.yaml)
#   --config P      config path  (default /etc/magitrickle/state/config.yaml)
#   --wan-iface N   interface to write instead of the template's pppoe-wan (DHCP WAN, etc.)
#
# PyYAML is required. The previous line-scan fallback read a non-empty `groups:` list without
# `- id:` lines as "empty" and then replaced the owner's groups, and `groups: [...]` made it
# append a second top-level key. Refusing without a real parser is the safe answer: the shell
# treats a non-zero --check-empty as "configured" and never overwrites.
import os
import re
import stat
import sys
import time

try:  # the honest parser is the only accepted one
    import yaml
except Exception:
    yaml = None

TEMPLATE = '/etc/geomask/lists/magitrickle-groups.yaml'
CONFIG = '/etc/magitrickle/state/config.yaml'

EXIT_EMPTY = 0
EXIT_CONFIGURED = 1
EXIT_UNVERIFIABLE = 2


class SeedError(Exception):
    pass


class UniqueKeyLoader(yaml.SafeLoader if yaml is not None else object):
    """SafeLoader that refuses duplicate mapping keys instead of silently keeping the last."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, 'duplicate key: %r' % (key,), key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


if yaml is not None:
    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


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


def read_text(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def load_document(text, label):
    """Parse a YAML document with duplicate-key detection; raise SeedError on any problem."""
    if yaml is None:
        raise SeedError('PyYAML (python3-yaml) is required: refusing to rewrite the lists '
                        'without a real parser')
    try:
        data = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise SeedError('%s is not valid YAML: %s' % (label, exc))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SeedError('%s is not a YAML mapping' % label)
    return data


def groups_of(data, label):
    """The `groups` value as a list; raise when it is present but not a list."""
    groups = data.get('groups')
    if groups is None:
        return []
    if not isinstance(groups, list):
        raise SeedError('%s has a non-list `groups` value' % label)
    return groups


def groups_region(lines):
    """Return (gi, si): the top-level `groups:` line and the next top-level key (or len).

    Matches the inline form too (`groups: []`), which the old exact-line check missed and
    then appended a second `groups:` key after.
    """
    gi = None
    for i, line in enumerate(lines):
        if re.match(r'^groups:(\s|$)', line):
            gi = i
            break
    if gi is None:
        return None, None
    si = len(lines)
    for i in range(gi + 1, len(lines)):
        line = lines[i]
        if re.match(r'^(---|\.\.\.)\s*$', line) or re.match(r'^[A-Za-z_][A-Za-z0-9_-]*\s*:', line):
            si = i
            break
    return gi, si


def remap_wan(lines, wan_iface):
    """Point the template's direct groups at the interface this router actually has."""
    if not wan_iface:
        return lines
    return [re.sub(r'^(\s*interface:\s*)pppoe-wan\s*$', r'\g<1>' + wan_iface, line)
            for line in lines]


def template_block(template_text, wan):
    """The template's own `groups:` block only - never its trailing keys."""
    lines = template_text.split('\n')
    gi, si = groups_region(lines)
    if gi is None:
        raise SeedError('template has no top-level `groups:` section')
    block = remap_wan(lines[gi:si], wan)
    while block and block[-1].strip() == '':
        block.pop()
    if not any(re.match(r'^\s*-\s', line) for line in block):
        raise SeedError('template groups block is empty')
    return block


def write_file(path, text, mode):
    tmp = path + '.gm.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as handle:
        handle.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def main():
    mode, tpl, cfg, wan = parse_args()
    if not os.path.exists(tpl):
        print('ERR template missing: %s' % tpl)
        return EXIT_UNVERIFIABLE
    try:
        tpl_text = read_text(tpl)
        template_doc = load_document(tpl_text, 'template')
        template_groups = groups_of(template_doc, 'template')
        if not template_groups:
            raise SeedError('template groups block is empty')
        block = template_block(tpl_text, wan)

        cur_text = read_text(cfg) if os.path.exists(cfg) else None
        cur_doc = None
        current = 0
        if cur_text is not None:
            cur_doc = load_document(cur_text, 'live config')
            current = len(groups_of(cur_doc, 'live config'))

        if mode == 'check':
            return EXIT_EMPTY if current == 0 else EXIT_CONFIGURED

        if cur_doc is None:
            print('ERR live config missing (%s); start magitrickle first' % cfg)
            return EXIT_UNVERIFIABLE
        if current and '--force' not in sys.argv:
            raise SeedError('live config already has %d groups; refusing to replace them '
                            'without --force' % current)

        newline = '\r\n' if '\r\n' in cur_text else '\n'
        cur_lines = cur_text.split('\n')
        if newline == '\r\n':
            cur_lines = [line.rstrip('\r') for line in cur_lines]
        gi, si = groups_region(cur_lines)
        if gi is None:
            new_lines = cur_lines + [''] + block
        else:
            new_lines = cur_lines[:gi] + block + cur_lines[si:]
        new_text = newline.join(new_lines).rstrip('\r\n') + newline

        result_doc = load_document(new_text, 'result')
        result_groups = groups_of(result_doc, 'result')
        if len(result_groups) != len(template_groups):
            raise SeedError('result has %d groups, template has %d - not writing'
                            % (len(result_groups), len(template_groups)))
        before_rest = {k: v for k, v in cur_doc.items() if k != 'groups'}
        after_rest = {k: v for k, v in result_doc.items() if k != 'groups'}
        if before_rest != after_rest:
            raise SeedError('result changed keys other than `groups` - not writing')

        mode_bits = stat.S_IMODE(os.stat(cfg).st_mode)
        stamp = time.strftime('%Y%m%d%H%M%S')
        backup = cfg + '.bak_geomask_' + stamp
        write_file(backup, cur_text, mode_bits)
        write_file(cfg, new_text, mode_bits)
        rules = sum(1 for line in block if re.match(r'^\s*-\s', line))
        print('CHANGED before=%d after=%d entries=%d backup=%s'
              % (current, len(result_groups), rules, os.path.basename(backup)))
        return 0
    except SeedError as exc:
        print('ERR %s' % exc)
        return EXIT_UNVERIFIABLE


if __name__ == '__main__':
    sys.exit(main())
