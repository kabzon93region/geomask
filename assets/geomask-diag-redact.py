#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Redact the console diagnostic bundle before it is packed.

The bundle is handed to the owner as-is, so masking has to happen here, on real producer
output - not on the config-file form the backend used to know:

    redact uci    `uci show firewall|network` (pkg.sec=type, pkg.sec.opt='value', lists)
    redact yaml   /etc/mihomo/config.yaml (mihomo's own YAML)
    redact json   console-config.json, group-CONSOLE.json, connections.json
    redact text   logread / nft output (log lines and API payloads)

Every mode reads stdin and writes stdout.  `verify <dir>` is the last gate before tar:
it walks every regular file and fails on anything that still looks like a real secret.

Fail closed: an unparsable YAML/JSON document prints nothing of the input, writes
{"ok":false,"error":"yaml parse failed"} (or json) to stderr and exits 1.  No colours,
no timestamps, no --help: the backend owns stages, messages and exit codes.
"""

import json
import os
import re
import sys

import yaml

PLACEHOLDER = '…'


class RedactionError(Exception):
    """A document could not be parsed; nothing of it may reach stdout."""


# ---------------------------------------------------------------- helpers

def read_stdin():
    return sys.stdin.buffer.read().decode('utf-8', 'replace')


def emit(text):
    sys.stdout.buffer.write(text.encode('utf-8'))
    sys.stdout.buffer.flush()


def fail(message):
    sys.stderr.buffer.write((message + '\n').encode('utf-8'))
    sys.stderr.buffer.flush()
    return 1


# ---------------------------------------------------------------- uci show

# Allowlists are the point of `redact uci`: an option that is not known to be harmless is
# masked, never printed.  Values of these options are addresses, ports, booleans, names and
# list names - useful in a diagnosis, useless to an attacker.
_UCI_INTERFACE = {
    'device', 'proto', 'ipaddr', 'netmask', 'gateway', 'dns', 'metric', 'mtu',
    'disabled', 'ifname', 'type', 'name', 'addresses', 'auto', 'peerdns', 'defaultroute',
    'delegate', 'ip6assign', 'ip6hint', 'ip6prefix', 'ip6addr', 'ip6gw',
    'ip6table', 'ip4table', 'sourcefilter', 'force_link', 'hostname',
    'macaddr', 'reqprefix', 'reqaddress', 'ttl',
}
_UCI_DEVICE = {
    'name', 'type', 'mtu', 'macaddr', 'ifname', 'ports', 'bridge_empty',
    'igmp_snooping', 'promisc', 'rxpause', 'txpause',
}
_UCI_ZONE = {'name', 'network', 'input', 'output', 'forward', 'masq', 'mtu_fix', 'family'}
_UCI_RULE = {
    'name', 'src', 'dest', 'src_ip', 'dest_ip', 'proto', 'src_port', 'dest_port',
    'port', 'target', 'family', 'src_mac', 'dest_mac', 'icmp_type', 'extra',
    'helper', 'mark', 'limit', 'in', 'out', 'lookup', 'invert',
    'suppress_prefixlength',
}
_UCI_DNSMASQ = {
    'server', 'strictorder', 'noresolv', 'no-poll', 'domainneeded', 'boguspriv',
    'listen_address', 'port', 'domain', 'local_service', 'cachesize',
    'rebind_protection', 'localise_queries',
}
_UCI_DEFAULTS = {
    'input', 'output', 'forward', 'syn_flood', 'flow_offloading',
    'flow_offloading_hw',
}
_UCI_WIREGUARD = {
    'description', 'public_key', 'endpoint_host', 'endpoint_port',
    'persistent_keepalive', 'allowed_ips', 'route_allowed_ips', 'fwmark',
}
_UCI_BRIDGE_VLAN = {'device', 'ports', 'vlan'}
_UCI_GLOBALS = {'ula_prefix', 'packet_steering'}
_UCI_ROUTE = {
    'interface', 'target', 'netmask', 'gateway', 'metric', 'source', 'table',
    'type', 'onlink',
}

_UCI_ALLOW = {
    'interface': _UCI_INTERFACE,
    'device': _UCI_DEVICE,
    'zone': _UCI_ZONE,
    'rule': _UCI_RULE,
    'redirect': _UCI_RULE,
    'dnsmasq': _UCI_DNSMASQ,
    'defaults': _UCI_DEFAULTS,
    'wireguard_*': _UCI_WIREGUARD,
    'bridge-vlan': _UCI_BRIDGE_VLAN,
    'globals': _UCI_GLOBALS,
    'route': _UCI_ROUTE,
}

_UCI_SECTION = re.compile(r'^([A-Za-z0-9_-]+)\.([^.=]+)=([A-Za-z0-9_-]+)$')
_UCI_OPTION = re.compile(r'^([A-Za-z0-9_-]+)\.([^.=]+)\.([A-Za-z0-9_-]+)=(.*)$')


def _uci_allowed(section_type, option):
    if not section_type:
        return False
    allowed = _UCI_ALLOW.get(section_type)
    if allowed is None and section_type.startswith('wireguard_'):
        allowed = _UCI_ALLOW['wireguard_*']
    return allowed is not None and option in allowed


def redact_uci(text):
    """Structural pass over `uci show`: types first, then every option line."""
    sections = {}
    records = []
    for line in text.split('\n'):
        section = _UCI_SECTION.match(line)
        if section:
            sections[(section.group(1), section.group(2))] = section.group(3)
            records.append(('section', line, None))
            continue
        option = _UCI_OPTION.match(line)
        if option:
            records.append(('option', line, option))
            continue
        records.append(('other', line, None))
    out = []
    for kind, line, match in records:
        if kind == 'section':
            out.append(line)
            continue
        if kind == 'other':
            # uci's own error message or a stray line: keep it, but never a credential
            # that happens to sit on it.
            out.append(redact_text_line(line))
            continue
        pkg, sec, option, value = match.groups()
        prefix = line[:match.start(4)]
        if _uci_allowed(sections.get((pkg, sec)), option):
            out.append(prefix + (value if value else "''"))
        else:
            # Unknown section/option: the name is kept, the value is not.
            out.append(prefix + "'" + PLACEHOLDER + "'")
    return '\n'.join(out)


# ---------------------------------------------------------------- yaml / json

# Exact (normalised) key names whose value is a credential.  `sni` is deliberately absent:
# the host name is what makes a diagnosis possible and it is not a secret.
_SECRET_KEYS = {
    'password', 'passwd', 'secret', 'private-key', 'preshared-key', 'psk',
    'token', 'uuid', 'auth', 'authentication', 'authorization', 'header',
    'headers', 'username', 'user', 'x-hwid', 'url', 'subscribe-url',
    'obfs-password', 'key',
}


def _normalise_key(name):
    return re.sub(r'[^a-z0-9]+', '-', str(name).strip().lower()).strip('-')


def _key_is_secret(name):
    normalised = _normalise_key(name)
    if normalised in _SECRET_KEYS:
        return True
    # `header.Authorization`, `provider.headers.Authorization`: a dotted key carries the
    # secret in its last component even when the whole string is not in the set.
    for part in re.split(r'[.\s]+', str(name).strip()):
        if _normalise_key(part) in _SECRET_KEYS:
            return True
    return False


_LONG_TOKEN = re.compile(r'(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{32,}(?![A-Za-z0-9+/=])')
_JWT = re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')
_BEARER_ANY = re.compile(r'(?i)\bbearer\s+\S')
_USERINFO_ANY = re.compile(r'(?i)[a-z][a-z0-9+.-]*://[^/\s@:]+:[^/\s@]+@')
_SECRET_QUERY = re.compile(
    r'(?i)[?&](token|key|password|passwd|secret|uuid|auth|signature|sig)=')


def _string_is_secret(value):
    if _BEARER_ANY.search(value):
        return True
    if _USERINFO_ANY.search(value):
        return True
    if _SECRET_QUERY.search(value):
        return True
    if _JWT.search(value):
        return True
    if _LONG_TOKEN.search(value):
        return True
    return False


def _redact_tree(node):
    if isinstance(node, dict):
        clean = {}
        for key, value in node.items():
            if _key_is_secret(key):
                clean[key] = PLACEHOLDER
            else:
                clean[key] = _redact_tree(value)
        return clean
    if isinstance(node, list):
        return [_redact_tree(item) for item in node]
    if isinstance(node, str) and _string_is_secret(node):
        return PLACEHOLDER
    return node


def redact_yaml(text):
    if not text.strip():
        return ''
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise RedactionError('yaml parse failed')
    if data is None:
        # comments only: nothing to leak, nothing useful to keep
        return ''
    clean = _redact_tree(data)
    return yaml.safe_dump(clean, allow_unicode=True, sort_keys=False)


def redact_json(text):
    if not text.strip():
        return ''
    try:
        data = json.loads(text)
    except ValueError:
        raise RedactionError('json parse failed')
    clean = _redact_tree(data)
    return json.dumps(clean, ensure_ascii=False, indent=2) + '\n'


# ---------------------------------------------------------------- text

_BEARER = re.compile(r'(?i)(\bbearer\s+)\S+')
_URL = re.compile(r'(?i)\bhttps?://[^\s"\'<>]+')
_KEYVAL = re.compile(
    r'(?i)(?<![A-Za-z0-9_-])'
    r'(password|passwd|secret|private[_-]?key|preshared[_-]?key|psk|token|uuid|'
    r'x-hwid|authorization|authentication|auth|header|headers|username|user|'
    r'obfs-password|key)'
    r'(\s*[:=]\s*)'
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s,;&}\]]+)')
_PLACEHOLDERS = {'', '…', "'…'", '"…"', '[redacted]', '***', 'null', 'none', '~'}


def _is_placeholder(value):
    token = value.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        token = token[1:-1].strip()
    return token.lower() in _PLACEHOLDERS


def _mask_token(token):
    if _is_placeholder(token):
        return token
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[0] + PLACEHOLDER + token[0]
    return PLACEHOLDER


def _url_without_credentials(match):
    url = match.group(0)
    parsed = re.match(r'(?i)^(https?)://(?:[^/@\s]*@)?([^/?#\s]+)', url)
    if not parsed:
        return PLACEHOLDER
    host = parsed.group(2).rstrip('.,;:)]}')
    return parsed.group(1) + '://' + host


def redact_text_line(line):
    line = _BEARER.sub(lambda m: m.group(1) + PLACEHOLDER, line)
    line = _URL.sub(_url_without_credentials, line)
    line = _KEYVAL.sub(lambda m: m.group(1) + m.group(2) + _mask_token(m.group(3)), line)
    line = _JWT.sub(PLACEHOLDER, line)
    line = _LONG_TOKEN.sub(PLACEHOLDER, line)
    return line


def redact_text(text):
    return '\n'.join(redact_text_line(line) for line in text.split('\n'))


# ---------------------------------------------------------------- verify

_VERIFY_KV = re.compile(
    r'(?i)(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)\s*[:=]\s*'
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s,;&}\]]+)')
_VERIFY_BEARER = re.compile(r'(?i)\bbearer\s+(\S+)')
_VERIFY_URL_USERINFO = re.compile(r'(?i)[a-z][a-z0-9+.-]*://[^/\s@:]+:[^/\s@]+@')


def _plain_host_url(value):
    token = value.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        token = token[1:-1].strip()
    return bool(re.match(r'(?i)^https?://[^/\s?#@]+$', token))


def _verify_text(filename, text):
    findings = []
    for number, line in enumerate(text.split('\n'), 1):
        for match in _VERIFY_KV.finditer(line):
            key, value = match.group(1), match.group(2)
            if not _key_is_secret(key):
                continue
            if _normalise_key(key) in ('url', 'subscribe-url') and _plain_host_url(value):
                # `redact text` keeps scheme://host only, so a bare host is the safe form.
                continue
            if not _is_placeholder(value):
                findings.append('%s:%d: %s' % (filename, number, _normalise_key(key)))
        for match in _VERIFY_BEARER.finditer(line):
            if not _is_placeholder(match.group(1)):
                findings.append('%s:%d: bearer' % (filename, number))
        if _VERIFY_URL_USERINFO.search(line):
            findings.append('%s:%d: url-userinfo' % (filename, number))
        for match in _LONG_TOKEN.finditer(line):
            if not _is_placeholder(match.group(0)):
                findings.append('%s:%d: long-token' % (filename, number))
        if _JWT.search(line):
            findings.append('%s:%d: long-token' % (filename, number))
    return findings


def verify_directory(directory):
    findings = []
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            relative = os.path.relpath(path, directory).replace(os.sep, '/')
            try:
                with open(path, 'rb') as handle:
                    data = handle.read()
            except OSError as exc:
                findings.append('%s:0: unreadable (%s)' % (relative, exc.strerror or exc))
                continue
            findings.extend(_verify_text(relative, data.decode('utf-8', 'replace')))
    return findings


# ---------------------------------------------------------------- main

_REDACTORS = {
    'uci': redact_uci,
    'yaml': redact_yaml,
    'json': redact_json,
    'text': redact_text,
}


def main(argv):
    if len(argv) < 2:
        return fail('usage: geomask-diag-redact.py redact <uci|yaml|json|text> | verify <dir>')
    command = argv[1]
    if command == 'redact':
        if len(argv) != 3 or argv[2] not in _REDACTORS:
            return fail('usage: geomask-diag-redact.py redact <uci|yaml|json|text>')
        mode = argv[2]
        text = read_stdin()
        try:
            result = _REDACTORS[mode](text)
        except RedactionError as exc:
            return fail('{"ok":false,"error":"%s"}' % exc)
        emit(result)
        return 0
    if command == 'verify':
        if len(argv) != 3:
            return fail('usage: geomask-diag-redact.py verify <dir>')
        directory = argv[2]
        if not os.path.isdir(directory):
            return fail('verify: not a directory: %s' % directory)
        findings = verify_directory(directory)
        if findings:
            emit('\n'.join(findings) + '\n')
            return 1
        return 0
    return fail('unknown command: %s' % command)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
