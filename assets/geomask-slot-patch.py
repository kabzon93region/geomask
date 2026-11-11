#!/usr/bin/env python3
"""Structural slot patcher: point a mihomo group at a subscription URL.

Historically this was a text rewriter that expected `  - name: GROUP` and a double-quoted
provider URL. Applying a verified pool re-serialises the whole config via PyYAML, which
writes `- name:` at column 0 and bare scalars, so every rewrite failed - switching the AI
slot answered "no AI group anchor" although the group was right there.

Read and write the YAML structure instead, with YAML 1.2 boolean semantics so a round trip
cannot turn `off` into `false`. Prints one machine-readable line: updated:<provider> or
created:<provider>.
"""
import copy
import re
import sys

import yaml

GROUPS = ('VPN', 'VPN-noP2P', 'AI')
GROUP_TEMPLATE = {
    'type': 'url-test',
    'url': 'https://www.gstatic.com/generate_204',
    'interval': 180,
    'tolerance': 99999,
    'lazy': False,
}


def loader():
    """PyYAML follows YAML 1.1, where off/on/yes/no are booleans; mihomo uses 1.2."""
    class Loader(yaml.SafeLoader):
        pass

    resolutions = {}
    for first, rules in yaml.SafeLoader.yaml_implicit_resolvers.items():
        kept = [(tag, regexp) for tag, regexp in rules
                if tag not in ('tag:yaml.org,2002:bool', 'tag:yaml.org,2002:timestamp')]
        if kept:
            resolutions[first] = kept
    Loader.yaml_implicit_resolvers = resolutions
    bool_re = re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$')
    for first in 'tTfF':
        Loader.add_implicit_resolver('tag:yaml.org,2002:bool', bool_re, [first])
    return Loader


def find_group(doc, name):
    for entry in doc.get('proxy-groups') or []:
        if isinstance(entry, dict) and entry.get('name') == name:
            return entry
    return None


def uses_provider(doc, name, provider):
    group = find_group(doc, name)
    if not group:
        return False
    return provider in (group.get('use') or [])


def free_provider_name(providers):
    name = 'slot'
    index = 2
    while name in providers:
        name = 'slot%d' % index
        index += 1
    return name


def new_provider(doc, url):
    """A fresh http provider; copy the header block (e.g. x-hwid) from an existing one.

    No health-check options on purpose: mihomo drops nodes that fail one from the group,
    which silently empties a lane and turns every rule using it into 502s.
    """
    spec = {'type': 'http', 'url': url, 'interval': 3600}
    for existing in (doc.get('proxy-providers') or {}).values():
        if isinstance(existing, dict) and isinstance(existing.get('header'), dict) \
                and existing['header']:
            # Deep-copy: a shared dict would be dumped as a YAML anchor/alias, coupling
            # the new provider to the one it was copied from.
            spec['header'] = copy.deepcopy(existing['header'])
            break
    return spec


def rewrite_vpn_rules(doc):
    """A VPN lane needs the IP half of the ruleset and must not end in a RESERVE anchor."""
    rules = doc.get('rules')
    if not isinstance(rules, list):
        return
    has_bulk = any(isinstance(rule, str) and rule.startswith('RULE-SET,mtrickle_bulk')
                   for rule in rules)
    if not has_bulk:
        for index, rule in enumerate(rules):
            if isinstance(rule, str) and rule.startswith('RULE-SET,mtrickle,VPN-noP2P'):
                rules.insert(index, 'RULE-SET,mtrickle_bulk,VPN')
                break
    doc['rules'] = [rule.replace(',RESERVE', ',VPN')
                    if isinstance(rule, str) and rule.endswith(',RESERVE') else rule
                    for rule in rules]


def main(argv):
    if len(argv) != 4:
        sys.stderr.write('usage: slot patcher <config> <group> <url>\n')
        return 2
    config_path, group, url = argv[1], argv[2], argv[3]
    with open(config_path, encoding='utf-8') as handle:
        doc = yaml.load(handle, Loader=loader())
    if not isinstance(doc, dict):
        sys.stderr.write('mihomo config is not a mapping\n')
        return 2
    providers = doc.get('proxy-providers')
    if not isinstance(providers, dict):
        sys.stderr.write('no proxy-providers section\n')
        return 2

    target = find_group(doc, group)
    if target is not None:
        use = target.get('use')
        if not isinstance(use, list) or not use:
            sys.stderr.write('group %s is fed by inline proxies, not a provider\n' % group)
            return 2
        current = use[0]
        spec = providers.get(current)
        # A managed pool (audit-*) is a file provider built from verified nodes: never
        # rewrite it into an http subscription, and never leave the group half-switched.
        managed = str(current).startswith('audit-') or \
            (isinstance(spec, dict) and spec.get('type') == 'file')
        shared = any(other != group and uses_provider(doc, other, current) for other in GROUPS)
        if not shared and not managed:
            if not isinstance(spec, dict):
                sys.stderr.write('provider %s is missing from the config\n' % current)
                return 2
            spec['url'] = url
            spec.setdefault('type', 'http')
            action = 'updated:' + current
        else:
            created = free_provider_name(providers)
            providers[created] = new_provider(doc, url)
            target['use'] = [created if item == current else item for item in use]
            action = 'created:' + created
    else:
        created = free_provider_name(providers)
        providers[created] = new_provider(doc, url)
        entry = {'name': group}
        entry.update(GROUP_TEMPLATE)
        entry['use'] = [created]
        groups = doc.get('proxy-groups')
        if not isinstance(groups, list):
            groups = []
            doc['proxy-groups'] = groups
        anchor = next((index for index, item in enumerate(groups)
                       if isinstance(item, dict) and item.get('name') == 'AI'), None)
        if anchor is None:
            groups.append(entry)
        else:
            groups.insert(anchor + 1, entry)
        if group == 'VPN':
            rewrite_vpn_rules(doc)
        action = 'created:' + created

    body = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    with open(config_path, 'w', encoding='utf-8', newline='') as handle:
        handle.write(body)
    print(action)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
