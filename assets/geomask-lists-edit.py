#!/usr/bin/env python3
"""Edit MagiTrickle domain lists safely: add or remove one domain at a time.

MagiTrickle decides the direction of every domain on this router, and magitrickled owns
its config file. A blind text edit there can leave the router without lists at all, so
this tool:

  * refuses to work on a file it cannot parse back (groups and rules are counted before
    and after the change);
  * keeps a pre-image and restores it if the service does not come back;
  * stops magitrickled around the write, waits for it, then runs the mihomo sync so the
    change is visible to both halves of the routing.

Commands:
  groups                                  list groups with interface and rule counts
  add --group NAME --domain D [--domain D2 ...] [--dry-run]
  remove --domain D [--dry-run]

The interface of the group decides the path: `Mihomo` means "into the tunnel" (mihomo
then applies its own rules), `VPN-noP2P`/`AI` send the domain straight to that tunnel,
`pppoe-wan` (or any physical device) sends it out directly.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

CONFIG = '/etc/magitrickle/state/config.yaml'
SYNC = '/root/sync_mtrickle_mihomo.sh'
SERVICE = 'magitrickle'
BACKUP = '/tmp/geomask-lists-edit.bak'
RULE_ID = re.compile(r'^[0-9a-f]{8}$')


class EditError(Exception):
    """A refusal or a failure the caller must report."""


# --------------------------------------------------------------------------- parsing
def read_lines(path=None):
    path = path or CONFIG
    try:
        with open(path, encoding='utf-8') as handle:
            return handle.read().splitlines(True)
    except OSError as exc:
        raise EditError('cannot read %s: %s' % (path, exc))


def group_blocks(lines):
    """[(name, interface, start, end, rule_lines)] for every group at column 0."""
    blocks = []
    for index, line in enumerate(lines):
        if not line.startswith('- id:'):
            continue
        end = len(lines)
        for probe in range(index + 1, len(lines)):
            if lines[probe].startswith('- id:') or (lines[probe].strip()
                                                    and not lines[probe].startswith(' ')):
                end = probe
                break
        name = interface = ''
        rules = []
        for probe in range(index + 1, end):
            text = lines[probe].strip()
            if text.startswith('name:') and not name:
                value = text.split(':', 1)[1].strip().strip('\'"')
                if value:
                    name = value
            elif text.startswith('interface:'):
                interface = text.split(':', 1)[1].strip().strip('\'"')
            elif re.match(r'^-\s*id:', text):
                rules.append(probe)
        blocks.append({'name': name, 'interface': interface, 'start': index,
                       'end': end, 'rules': rules})
    return blocks


def rule_indent(lines, block):
    """Indentation of a rule entry inside this block (2 extra spaces vs the group)."""
    for index in block['rules']:
        match = re.match(r'^(\s*)-\s*id:', lines[index])
        if match:
            return match.group(1)
    return '    '


def domain_of(lines, rule_index):
    for probe in range(rule_index + 1, min(rule_index + 6, len(lines))):
        text = lines[probe].strip()
        if text.startswith('rule:'):
            return text.split(':', 1)[1].strip().strip('\'"')
        if re.match(r'^-\s*id:', text):
            break
    return ''


def find_domain(lines, domain):
    domain = domain.lower()
    for block in group_blocks(lines):
        for index in block['rules']:
            if domain_of(lines, index).lower() == domain:
                return block, index
    return None, None


def validate(lines, expected_groups, expected_rules):
    """The file must still parse into the same groups with the expected rule count."""
    blocks = group_blocks(lines)
    total = sum(len(block['rules']) for block in blocks)
    if len(blocks) != expected_groups:
        raise EditError('groups changed: %d -> %d' % (expected_groups, len(blocks)))
    if total != expected_rules:
        raise EditError('rules changed: %d -> %d' % (expected_rules, total))
    ids = []
    for index, line in enumerate(lines):
        match = re.match(r'^\s*-\s*id:\s*"?([0-9a-f]{8})"?\s*$', line)
        if match:
            ids.append(match.group(1))
    if len(ids) != len(set(ids)):
        raise EditError('duplicate rule/group ids after the edit')


def next_id(lines):
    used = {m.group(1) for m in
            (re.match(r'^\s*-\s*id:\s*"?([0-9a-f]{8})"?\s*$', line) for line in lines) if m}
    for _ in range(1000):
        candidate = os.urandom(4).hex()
        if candidate not in used:
            return candidate
    raise EditError('cannot allocate a rule id')


# --------------------------------------------------------------------------- edits
def plan_add(lines, group_name, domains):
    blocks = group_blocks(lines)
    block = next((b for b in blocks if b['name'] == group_name), None)
    if block is None:
        names = ', '.join(sorted(b['name'] for b in blocks if b['name'])) or '(нет групп)'
        raise EditError('group %r not found; available: %s' % (group_name, names))
    indent = rule_indent(lines, block)
    if not block['rules']:
        # the group has an empty rules: list - insert right after that line
        rules_line = next((i for i in range(block['start'], block['end'])
                           if lines[i].strip() == 'rules:'), None)
        if rules_line is None:
            raise EditError('group %r has no rules: section' % group_name)
        position = rules_line + 1
    else:
        last = block['rules'][-1]
        position = last + 1
        while position < block['end'] and not re.match(r'^\s*-\s*id:', lines[position]) \
                and lines[position].startswith(' '):
            position += 1
    new_lines = []
    added = []
    for domain in domains:
        existing_group, _ = find_domain(lines, domain)
        if existing_group is not None:
            continue
        new_lines.extend([
            '%s- id: %s\n' % (indent, next_id(lines + new_lines)),
            '%s  name: ""\n' % indent,
            '%s  type: namespace\n' % indent,
            '%s  rule: %s\n' % (indent, domain),
            '%s  enable: true\n' % indent,
        ])
        added.append(domain)
    if not new_lines:
        raise EditError('nothing to add: already listed')
    return lines[:position] + new_lines + lines[position:], added


def plan_remove(lines, domains):
    for domain in domains:
        block, index = find_domain(lines, domain)
        if index is None:
            raise EditError('%s is not listed' % domain)
        end = index + 1
        while end < len(lines) and lines[end].startswith(' ') and not re.match(r'^\s*-\s*id:', lines[end]):
            end += 1
        del lines[index:end]
    return lines, list(domains)


# --------------------------------------------------------------------------- service
def run(cmd, timeout=30):
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, ''
    except subprocess.TimeoutExpired:
        return 124, 'timeout'
    return proc.returncode, ((proc.stdout or b'') + (proc.stderr or b'')).decode('utf-8', 'replace')


def service_alive():
    rc, _ = run(['pidof', 'magitrickled'])
    return rc == 0


def write_config(lines):
    with open(CONFIG + '.new', 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(''.join(lines))
    os.replace(CONFIG + '.new', CONFIG)


def apply(lines, expected_rules, dry_run=False):
    """Write the file with the service stopped, verify it, restore on any failure."""
    before = read_lines()
    before_groups = len(group_blocks(before))
    before_rules = sum(len(block['rules']) for block in group_blocks(before))
    # the caller changed exactly this many rules; anything else means a broken edit
    validate(lines, before_groups, expected_rules)
    if dry_run:
        return 'dry-run: %d line(s) differ, rules %d -> %d' % (
            len(lines) - len(before), before_rules, expected_rules)
    shutil.copy2(CONFIG, BACKUP)
    was_alive = service_alive()
    if was_alive:
        run(['/etc/init.d/%s' % SERVICE, 'stop'], timeout=60)
    try:
        write_config(lines)
        validate(read_lines(), before_groups, expected_rules)
    except Exception:
        shutil.copy2(BACKUP, CONFIG)
        if was_alive:
            run(['/etc/init.d/%s' % SERVICE, 'start'], timeout=60)
        raise
    run(['/etc/init.d/%s' % SERVICE, 'start'], timeout=60)
    for _ in range(15):
        if service_alive():
            break
        time.sleep(1)
    if not service_alive():
        shutil.copy2(BACKUP, CONFIG)
        run(['/etc/init.d/%s' % SERVICE, 'start'], timeout=60)
        raise EditError('magitrickled did not come back; config restored')
    rc, out = run(['sh', SYNC], timeout=120)
    tail = out.strip().splitlines()[-1] if out.strip() else ''
    return 'applied; sync rc=%d %s' % (rc, tail)


# --------------------------------------------------------------------------- cli
def main(argv=None):
    parser = argparse.ArgumentParser(prog='geomask-lists-edit')
    parser.add_argument('command', choices=['groups', 'add', 'remove'])
    parser.add_argument('--group', default='')
    parser.add_argument('--domain', action='append', default=[])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)

    try:
        lines = read_lines()
        blocks = group_blocks(lines)
        if args.command == 'groups':
            for block in blocks:
                print('%-30s %-12s %d правил'
                      % (block['name'] or '(без имени)', block['interface'], len(block['rules'])))
            return 0
        domains = [d.strip().lower() for d in args.domain if d.strip()]
        if not domains:
            raise EditError('--domain is required')
        for domain in domains:
            if not re.match(r'^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$', domain):
                raise EditError('not a domain: %r' % domain)
        before_rules = sum(len(block['rules']) for block in blocks)
        if args.command == 'add':
            if not args.group:
                raise EditError('--group is required')
            updated, changed = plan_add(lines, args.group, domains)
            print('add to %r: %s' % (args.group, ', '.join(changed)))
            expected = before_rules + len(changed)
        else:
            updated, changed = plan_remove(lines, domains)
            print('remove: %s' % ', '.join(changed))
            expected = before_rules - len(changed)
        print(apply(updated, expected, args.dry_run))
        return 0
    except EditError as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
