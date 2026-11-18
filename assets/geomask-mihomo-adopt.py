#!/usr/bin/env python3
"""Adopt a foreign mihomo config without losing the owner's settings (audit I05).

`--adopt-config` used to pass the guard and then do nothing: the in-place patch only
rewrites geomask-shaped files, so the run reported success without adopting anything and
the NEXT run refused the same file. This tool is the missing step: it produces a merged
candidate in a separate file, so the live config is untouched until `mihomo -t` passes.

The merge is additive and comment-preserving: only the pieces geomask needs are inserted
(the managed marker, `proxy-providers.sub`, the `VPN-noP2P`/`AI` groups and the
`ai-devices`/`torrent-direct` listeners). Every other line stays byte-identical, so the
owner keeps tun/dns/secret/rules/proxies as they are.

An ambiguous file is refused (rc 3) before anything is written: a half-merged config would
be worse than an honest refusal. Nothing here talks to the network or to the router.

    geomask-mihomo-adopt.py <src> <dst> --vpn-url URL --lan-ip IP [--hwid H]

rc 0 merged candidate written; rc 3 refused (reason on stderr); rc 4 PyYAML missing;
rc 5 the merged text does not parse or does not satisfy the shaped contract.
"""
import argparse
import os
import re
import sys

MARKER = '# geomask-managed: 1'
GROUP_NAMES = ('VPN-noP2P', 'AI')
KEY_RE = re.compile(r'^([A-Za-z0-9_.-]+):(?:\s|$)')
VPN_EXCLUDE = (
    'Россия|Мобильный|РФ|Авто|Russia|Mobile|Auto|Албания|Хорватия|Литва|Латвия|Дания|'
    'Финляндия|Нидерланды-(5|13|16|18)($|[^0-9])|Германия-[234]($|[^0-9])|'
    'США-1($|[^0-9])|Турция'
)


def refuse(message):
    print(message, file=sys.stderr)
    return 3


def load_yaml(text):
    try:
        import yaml
    except Exception:
        return None, 4, 'PyYAML is missing: adoption needs the parser before any write'
    try:
        return yaml.safe_load(text), 0, ''
    except Exception as exc:
        return None, 3, 'config is not valid YAML: %s' % exc


def section_bounds(lines, key):
    """Return (start, end) of a top-level key, or None. `end` trims trailing blanks."""
    start = None
    for index, line in enumerate(lines):
        match = KEY_RE.match(line)
        if match and match.group(1) == key:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if KEY_RE.match(lines[index]):
            end = index
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def provider_block(url, hwid):
    out = ['  sub:', '    type: http', '    proxy: DIRECT']
    if hwid:
        out += ['    header:', '      x-hwid:', '        - "%s"' % hwid]
    out += ['    url: "%s"' % url, '    interval: 3600']
    return out


def group_lines():
    return [
        '  - name: VPN-noP2P',
        '    type: url-test',
        '    use:',
        '      - sub',
        '    exclude-filter: "%s"' % VPN_EXCLUDE,
        '    url: "https://www.gstatic.com/generate_204"',
        '    interval: 180',
        '    tolerance: 99999',
        '    lazy: false',
        '  - name: AI',
        '    type: url-test',
        '    use:',
        '      - sub',
        '    filter: "USA"',
        '    exclude-filter: "HY2"',
        '    url: "https://www.gstatic.com/generate_204"',
        '    interval: 180',
        '    tolerance: 99999',
        '    lazy: false',
    ]


def listener_item(name, port, listen, proxy):
    return [
        '  - name: %s' % name,
        '    type: mixed',
        '    port: %d' % port,
        '    listen: %s' % listen,
        '    proxy: %s' % proxy,
    ]


def wanted_listeners(lan_ip):
    return [
        ('ai-devices', 7891, '127.0.0.1', 'AI', listener_item('ai-devices', 7891, '127.0.0.1', 'AI')),
        ('torrent-direct', 7894, lan_ip, 'DIRECT', listener_item('torrent-direct', 7894, lan_ip, 'DIRECT')),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument('src')
    parser.add_argument('dst')
    parser.add_argument('--vpn-url', default='')
    parser.add_argument('--lan-ip', default='')
    parser.add_argument('--hwid', default='')
    args = parser.parse_args(argv)

    try:
        with open(args.src, encoding='utf-8') as handle:
            text = handle.read()
    except OSError as exc:
        return refuse('cannot read %s: %s' % (args.src, exc))
    if not text.strip():
        return refuse('config is empty - nothing to adopt')

    data, rc, message = load_yaml(text)
    if rc:
        return refuse(message)
    if not isinstance(data, dict):
        return refuse('config is not a YAML mapping - refusing to guess its structure')

    providers = data.get('proxy-providers')
    if providers is not None and not isinstance(providers, dict):
        return refuse('proxy-providers is not a mapping - refusing to merge into it')
    groups = data.get('proxy-groups')
    if groups is not None:
        if not isinstance(groups, list):
            return refuse('proxy-groups is not a list - refusing to merge into it')
        names = []
        for item in groups:
            if not isinstance(item, dict):
                return refuse('proxy-groups contains a non-mapping item - refusing')
            name = item.get('name')
            if isinstance(name, str):
                names.append(name)
        clash = [name for name in GROUP_NAMES if name in names]
        if clash:
            return refuse('the config already has %s with its own meaning - '
                          'rename it or use --replace-config' % ', '.join(clash))
    if isinstance(providers, dict) and 'sub' in providers:
        return refuse('proxy-providers.sub already exists with another meaning - '
                      'use --replace-config instead of adoption')
    proxies = data.get('proxies')
    if not proxies and not providers:
        return refuse('the config has no proxies and no proxy-providers - '
                      'adoption would leave nothing to route')
    listeners = data.get('listeners')
    if listeners is not None:
        if not isinstance(listeners, list):
            return refuse('listeners is not a list - refusing to merge into it')
        for item in listeners:
            if not isinstance(item, dict):
                return refuse('listeners contains a non-mapping item - refusing')
            name = item.get('name')
            for want, _port, _listen, proxy, _block in wanted_listeners(args.lan_ip):
                if name != want:
                    continue
                if item.get('proxy') != proxy:
                    return refuse('listener %s already exists with proxy %r, geomask needs %s'
                                  % (want, item.get('proxy'), proxy))

    eol = '\r\n' if '\r\n' in text else '\n'
    lines = text.replace('\r\n', '\n').split('\n')
    while lines and lines[-1] == '':
        lines.pop()

    added = []
    if MARKER not in [line.strip() for line in lines]:
        lines[0:0] = [MARKER]
        added.append('marker')

    sub_block = provider_block(args.vpn_url, args.hwid)
    bounds = section_bounds(lines, 'proxy-providers')
    if bounds is None:
        lines += ['', 'proxy-providers:'] + sub_block
        added.append('proxy-providers.sub')
    else:
        lines[bounds[0] + 1:bounds[0] + 1] = sub_block
        added.append('proxy-providers.sub')

    have_groups = set()
    bounds = section_bounds(lines, 'proxy-groups')
    if bounds is not None:
        for line in lines[bounds[0]:bounds[1]]:
            match = re.match(r'^\s*-\s*name:\s*(\S+)\s*$', line)
            if match:
                have_groups.add(match.group(1))
    missing = [name for name in GROUP_NAMES if name not in have_groups]
    if missing:
        block = group_lines()
        if len(missing) == 1:
            block = block[10:] if missing[0] == 'AI' else block[:10]
        bounds = section_bounds(lines, 'proxy-groups')
        if bounds is None:
            lines += ['', 'proxy-groups:'] + block
        else:
            lines[bounds[1]:bounds[1]] = block
        added.append('groups:%s' % ','.join(missing))

    bounds = section_bounds(lines, 'listeners')
    live = set()
    if bounds is not None:
        for line in lines[bounds[0]:bounds[1]]:
            match = re.match(r'^\s*-\s*name:\s*(\S+)\s*$', line)
            if match:
                live.add(match.group(1))
    for name, _port, _listen, _proxy, block in wanted_listeners(args.lan_ip):
        if name in live:
            continue
        bounds = section_bounds(lines, 'listeners')
        if bounds is None:
            lines += ['', 'listeners:'] + block
        else:
            lines[bounds[1]:bounds[1]] = block
        added.append('listener:%s' % name)

    merged = eol.join(lines) + eol
    data, rc, message = load_yaml(merged)
    if rc:
        return refuse('the merged candidate does not parse (%s) - nothing written' % message)
    providers = data.get('proxy-providers') or {}
    groups = data.get('proxy-groups') or []
    names = {item.get('name') for item in groups if isinstance(item, dict)}
    if 'sub' not in providers or not set(GROUP_NAMES) <= names:
        return refuse('the merged candidate is still not geomask-shaped - nothing written')

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        handle = os.open(args.dst, flags, 0o600)
    except OSError as exc:
        return refuse('cannot create %s: %s' % (args.dst, exc))
    with os.fdopen(handle, 'w', encoding='utf-8', newline='') as out:
        out.write(merged)
    print('adopted: %s' % ', '.join(added))
    return 0


if __name__ == '__main__':
    sys.exit(main())
