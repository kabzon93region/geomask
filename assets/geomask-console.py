#!/usr/bin/env python3
"""Console lane for geomask: a fourth tunnel that belongs to one game console.

Why a separate lane and not just rules: a console needs ONE stable egress for the whole
session (auth + game + UDP). Rules keyed on domain names cannot promise that — the console
talks to CDN and API hosts that also belong to every other device. The AI lane solved the
same problem with a dedicated hev tunnel + a listener pinned to one group; this does the
same for a console, so the egress is chosen by the listener and no rule can steal it.

Commands:
  apply  <config> [--url URL|default|-] [--countries TR,DE] [--port N]   create or update
  remove <config>                                                remove what we own
  status <config>                                                JSON state, read-only

Ownership: the provider is named `console`, the group `CONSOLE`, the listener `console`.
Anything with those exact names is ours; nothing else is touched. `remove` restores the
config to the state it had before `apply`, which is what the panel's kill switch calls.
"""
import json
import copy
import os
import re
import sys

import yaml

PROVIDER = 'console'
GROUP = 'CONSOLE'
LISTENER = 'console'
DEFAULT_PORT = 7893
# `--url default` is not a URL: it means "as the VPN" and discards the pinned subscription.
RESET_URL = 'default'

# Node names in this subscription carry the country in Russian and/or English. The filter is a
# regex over names, so a country is the union of the spellings actually seen in the wild.
COUNTRIES = {
    'TR': ('Турция', 'Turkey'),
    'DE': ('Германия', 'Germany'),
    'NL': ('Нидерланды', 'Netherlands', 'Holland'),
    'FI': ('Финляндия', 'Finland'),
    'SE': ('Швеция', 'Sweden'),
    'NO': ('Норвегия', 'Norway'),
    'DK': ('Дания', 'Denmark'),
    'PL': ('Польша', 'Poland'),
    'CZ': ('Чехия', 'Czech'),
    'AT': ('Австрия', 'Austria'),
    'CH': ('Швейцария', 'Switzerland'),
    'FR': ('Франция', 'France'),
    'IT': ('Италия', 'Italy'),
    'ES': ('Испания', 'Spain'),
    'GB': ('Великобритания', 'United Kingdom', 'Britain'),
    'IE': ('Ирландия', 'Ireland'),
    'US': ('США', 'USA', 'United States'),
    'CA': ('Канада', 'Canada'),
    'JP': ('Япония', 'Japan'),
    'KR': ('Корея', 'Korea'),
    'SG': ('Сингапур', 'Singapore'),
    'HK': ('Гонконг', 'Hong Kong'),
    'AE': ('ОАЭ', 'Emirates', 'UAE'),
    'IL': ('Израиль', 'Israel'),
    'KZ': ('Казахстан', 'Kazakhstan'),
    'AM': ('Армения', 'Armenia'),
    'GE': ('Грузия', 'Georgia'),
    'MD': ('Молдова', 'Moldova'),
    'RS': ('Сербия', 'Serbia'),
    'BG': ('Болгария', 'Bulgaria'),
    'RO': ('Румыния', 'Romania'),
    'HU': ('Венгрия', 'Hungary'),
    'GR': ('Греция', 'Greece'),
    'PT': ('Португалия', 'Portugal'),
    'LV': ('Латвия', 'Latvia'),
    'LT': ('Литва', 'Lithuania'),
    'EE': ('Эстония', 'Estonia'),
    'BR': ('Бразилия', 'Brazil'),
    'IN': ('Индия', 'India'),
    'AU': ('Австралия', 'Australia'),
}


class ConsoleError(Exception):
    pass


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


def dumper():
    """The matching dumper: without this, `ipv6: off` is written as `ipv6: 'off'`.

    PyYAML quotes any string that its YAML 1.1 resolvers would read back as a boolean, and a
    quoted `'off'` is not the boolean mihomo expects. Removing the same resolvers on the way
    out keeps the file byte-stable across a round trip.
    """
    class Dumper(yaml.SafeDumper):
        pass

    resolutions = {}
    for first, rules in yaml.SafeDumper.yaml_implicit_resolvers.items():
        kept = [(tag, regexp) for tag, regexp in rules
                if tag not in ('tag:yaml.org,2002:bool', 'tag:yaml.org,2002:timestamp')]
        if kept:
            resolutions[first] = kept
    Dumper.yaml_implicit_resolvers = resolutions
    bool_re = re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$')
    for first in 'tTfF':
        Dumper.add_implicit_resolver('tag:yaml.org,2002:bool', bool_re, [first])
    return Dumper


def read_config(path):
    try:
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
    except OSError as exc:
        raise ConsoleError('cannot read %s: %s' % (path, exc))
    doc = yaml.load(text, Loader=loader())
    if not isinstance(doc, dict):
        raise ConsoleError('mihomo config is not a mapping')
    return doc


def write_config(path, doc):
    """Atomic replace, same permissions: a half-written config stops mihomo on restart."""
    tmp = '%s.console-tmp.%d' % (path, os.getpid())
    body = yaml.dump(doc, Dumper=dumper(), allow_unicode=True, sort_keys=False)
    with open(tmp, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(body)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def country_filter(codes):
    """Regex for the node filter: every spelling of every allowed country."""
    parts = []
    for code in codes:
        names = COUNTRIES.get(code)
        if not names:
            raise ConsoleError('unknown country code: %s' % code)
        parts.extend(re.escape(name) for name in names)
    # `filter` is a regexp2 pattern; alternation without anchors matches anywhere in the name.
    return '|'.join(parts)


def find_group(doc):
    for entry in doc.get('proxy-groups') or []:
        if isinstance(entry, dict) and entry.get('name') == GROUP:
            return entry
    return None


def find_listener(doc):
    for entry in doc.get('listeners') or []:
        if isinstance(entry, dict) and entry.get('name') == LISTENER:
            return entry
    return None


def default_provider(doc):
    """The provider the VPN really rides on, else the legacy sub/slot/sub2 order.

    Point 10 of the 15.11 review: the console borrowed the first provider named `sub`, so on a
    split registry it silently left through the restricted (VPN-noP2P) subscription while the
    console needs the one the VPN group uses. `VPN` wins over `VPN-noP2P`; the first provider
    of that group's `use` list that actually has a URL is the answer.
    """
    providers = doc.get('proxy-providers')
    if not isinstance(providers, dict):
        return None
    groups = doc.get('proxy-groups')
    if isinstance(groups, list):
        for name in ('VPN', 'VPN-noP2P'):
            group = next((g for g in groups
                          if isinstance(g, dict) and g.get('name') == name), None)
            if group is None:
                continue
            for provider_name in group.get('use') or []:
                candidate = providers.get(provider_name)
                if isinstance(candidate, dict) and candidate.get('url'):
                    return candidate
    # No VPN lane on this config (or it has no usable provider): keep the old behaviour, or the
    # very first switch-on fails on a router that is already working.
    for name in ('sub', 'slot', 'sub2'):
        candidate = providers.get(name)
        if isinstance(candidate, dict) and candidate.get('url'):
            return candidate
    return None


def default_provider_url(doc):
    provider = default_provider(doc)
    return provider.get('url') if provider else None


def ensure_provider(doc, url):
    providers = doc.get('proxy-providers')
    if not isinstance(providers, dict):
        providers = {}
        doc['proxy-providers'] = providers
    current = providers.get(PROVIDER)
    spec = {'type': 'http', 'interval': 3600}
    if isinstance(current, dict):
        # keep whatever the owner's provider block already had (header, proxy, interval)
        spec = dict(current)
    if url == RESET_URL:
        # "As the VPN again": drop the pinned URL and the header that was copied with it, then
        # re-derive both below. Without the reset an explicit `--url` could never be undone.
        url = ''
        spec.pop('url', None)
        spec.pop('header', None)
    if url:
        spec['url'] = url
    if not spec.get('url'):
        source = default_provider(doc)
        if source is not None:
            spec['url'] = source['url']
            if isinstance(source.get('header'), dict) and source['header']:
                spec['header'] = copy.deepcopy(source['header'])
    if not spec.get('url'):
        raise ConsoleError('no subscription URL: the console provider has none and none was given')
    # A provider-level health check makes mihomo drop nodes from the group; the console lane
    # must keep its node even when one check fails mid-match.
    for key in ('health-check', 'lazy'):
        spec.pop(key, None)
    providers[PROVIDER] = spec
    return spec


def ensure_group(doc, countries, port):
    groups = doc.get('proxy-groups')
    if not isinstance(groups, list):
        groups = []
        doc['proxy-groups'] = groups
    group = find_group(doc)
    if group is None:
        group = {'name': GROUP}
        groups.append(group)
    group['type'] = 'select'
    group['use'] = [PROVIDER]
    if countries:
        group['filter'] = country_filter(countries)
    else:
        group.pop('filter', None)
    # A select group needs no health-check settings; leaving stale ones behind would make the
    # next reader think the lane is auto-switching.
    for key in ('url', 'interval', 'tolerance', 'lazy', 'expected-status'):
        group.pop(key, None)
    return group


def ensure_listener(doc, port):
    listeners = doc.get('listeners')
    if not isinstance(listeners, list):
        listeners = []
        doc['listeners'] = listeners
    entry = find_listener(doc)
    if entry is None:
        entry = {'name': LISTENER, 'type': 'mixed'}
        listeners.append(entry)
    entry.update({'type': 'mixed', 'port': int(port), 'listen': '127.0.0.1', 'proxy': GROUP})
    return entry


def apply(args):
    path, url, countries, port = args['config'], args['url'], args['countries'], args['port']
    doc = read_config(path)
    if url == '-':
        url = sys.stdin.readline().strip()
    ensure_provider(doc, url)
    ensure_group(doc, countries, port)
    ensure_listener(doc, port)
    write_config(path, doc)
    return {'ok': True, 'group': GROUP, 'provider': PROVIDER, 'listener': LISTENER,
            'port': int(port), 'countries': countries}


def remove(args):
    path = args['config']
    doc = read_config(path)
    changed = []
    groups = doc.get('proxy-groups')
    if isinstance(groups, list):
        kept = [g for g in groups if not (isinstance(g, dict) and g.get('name') == GROUP)]
        if len(kept) != len(groups):
            doc['proxy-groups'] = kept
            changed.append('group')
    listeners = doc.get('listeners')
    if isinstance(listeners, list):
        kept = [l for l in listeners if not (isinstance(l, dict) and l.get('name') == LISTENER)]
        if len(kept) != len(listeners):
            doc['listeners'] = kept
            changed.append('listener')
    providers = doc.get('proxy-providers')
    if isinstance(providers, dict) and PROVIDER in providers:
        providers.pop(PROVIDER, None)
        changed.append('provider')
    if changed:
        write_config(path, doc)
    return {'ok': True, 'removed': changed}


def status(args):
    doc = read_config(args['config'])
    group = find_group(doc)
    listener = find_listener(doc)
    providers = doc.get('proxy-providers') or {}
    provider = providers.get(PROVIDER) if isinstance(providers, dict) else None
    return {'ok': True,
            'present': bool(group and listener and provider),
            'group': {'type': (group or {}).get('type'),
                      'filter': (group or {}).get('filter'),
                      'use': (group or {}).get('use')} if group else None,
            'listener': {'port': (listener or {}).get('port'),
                         'proxy': (listener or {}).get('proxy')} if listener else None,
            'provider': {'has_url': bool((provider or {}).get('url'))} if provider else None}


def parse(argv):
    if not argv:
        raise ConsoleError('usage: geomask-console.py apply|remove|status <config> [options]')
    command, rest = argv[0], argv[1:]
    config = None
    url = ''
    countries = []
    port = DEFAULT_PORT
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == '--url':
            index += 1
            url = rest[index] if index < len(rest) else ''
        elif item.startswith('--url='):
            url = item.split('=', 1)[1]
        elif item == '--countries':
            index += 1
            value = rest[index] if index < len(rest) else ''
            countries = [part.strip().upper() for part in value.split(',') if part.strip()]
        elif item.startswith('--countries='):
            value = item.split('=', 1)[1]
            countries = [part.strip().upper() for part in value.split(',') if part.strip()]
        elif item == '--port':
            index += 1
            port = int(rest[index])
        elif item.startswith('--port='):
            port = int(item.split('=', 1)[1])
        elif config is None:
            config = item
        else:
            raise ConsoleError('unexpected argument: %s' % item)
        index += 1
    if not config:
        raise ConsoleError('config path is required')
    return command, {'config': config, 'url': url, 'countries': countries, 'port': port}


def main(argv):
    try:
        command, args = parse(argv)
        handler = {'apply': apply, 'remove': remove, 'status': status}.get(command)
        if handler is None:
            raise ConsoleError('unknown command: %s' % command)
        result = handler(args)
    except ConsoleError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(json.dumps({'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
