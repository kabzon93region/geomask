#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Redact the console diagnostic bundle before it is packed.

The bundle is handed to the owner as-is, so masking has to happen here, on real producer
output - not on the config-file form the backend used to know:

    redact uci    `uci show firewall|network` (pkg.sec=type, pkg.sec.opt='value', lists)
    redact yaml   /etc/mihomo/config.yaml (mihomo's own YAML)
    redact json   console-config.json, group-CONSOLE.json, connections.json
    redact text   logread (log lines and API payloads)
    redact nft    plain nft ruleset tokens, with strict string/comment payload handling

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


def _uci_records(text):
    """Parse uci_print_value grammar, not shell syntax. Never eval/source input.

    Values are single-quoted chunks, joined by literal \\' escapes, with spaces
    between list elements. Newlines inside quotes belong to the same assignment.
    Unknown lines, unquoted values and incomplete quotes are ambiguous: refuse all.
    """
    text = text.replace('\r\n', '\n')
    records = []
    pos = 0
    while pos < len(text):
        if text[pos] == '\n':
            pos += 1
            continue
        end = text.find('\n', pos)
        end = len(text) if end < 0 else end
        line = text[pos:end]
        section = _UCI_SECTION.fullmatch(line)
        if section:
            records.append(('section', section.groups(), line, []))
            pos = end + 1
            continue
        option = re.match(r'([A-Za-z0-9_-]+)\.([^.=\n]+)\.([A-Za-z0-9_-]+)=', text[pos:])
        if not option:
            raise RedactionError('uci parse failed')
        prefix = option.group(0)
        pos += option.end()
        values = []
        while True:
            if pos >= len(text) or text[pos] != "'":
                raise RedactionError('uci parse failed')
            value = ''
            while True:
                pos += 1
                close = text.find("'", pos)
                if close < 0:
                    raise RedactionError('uci parse failed')
                value += text[pos:close]
                pos = close + 1
                # uci_print_value emits closing quote + backslash quote + opening quote.
                if text.startswith("\\''", pos):
                    value += "'"
                    pos += 2
                    continue
                break
            values.append(value)
            if pos == len(text) or text[pos] == '\n':
                pos += 1
                break
            if text[pos] != ' ':
                raise RedactionError('uci parse failed')
            while pos < len(text) and text[pos] == ' ':
                pos += 1
        records.append(('option', option.groups(), prefix, values))
    return records


def _uci_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def redact_uci(text):
    records = _uci_records(text)
    sections = {(parts[0], parts[1]): parts[2]
                for kind, parts, _, _ in records if kind == 'section'}
    out = []
    for kind, parts, prefix, values in records:
        if kind == 'section':
            out.append(prefix)
            continue
        pkg, sec, option = parts
        if _uci_allowed(sections.get((pkg, sec)), option):
            clean = [redact_text(value) for value in values]
        else:
            clean = [PLACEHOLDER]
        out.append(prefix + ' '.join(_uci_quote(value) for value in clean))
    return '\n'.join(out) + ('\n' if text.endswith('\n') and out else '')


# ---------------------------------------------------------------- yaml / json

# Exact (normalised) key names whose value is a credential.  `sni` is deliberately absent:
# the host name is what makes a diagnosis possible and it is not a secret.
_SECRET_KEYS = {
    'password', 'passwd', 'secret', 'private-key', 'preshared-key', 'psk',
    'token', 'uuid', 'auth', 'authentication', 'authorization', 'header',
    'headers', 'username', 'user', 'x-hwid', 'url', 'subscribe-url',
    'obfs-password', 'key', 'auth-str', 'auth-string', 'proxy-authorization',
    # The audit of 18.11.2026 found short values under these names left untouched: the whole
    # key was not in the set, and the value was too short for the token heuristics.
    'api-key', 'apikey', 'access-key', 'secret-key', 'access-token',
    'refresh-token', 'session-token', 'auth-token', 'client-secret',
    'client-key', 'subscription-url', 'sub-url', 'hwid',
}

# A key is secret when its LAST component is one of these, or when its last two components
# form one of the pairs below. `public-key` stays visible on purpose (the UCI fixture asserts
# it), while `api_key`, `my access token` and `clientSecret` do not.
_SECRET_SUFFIXES = ('token', 'secret', 'password', 'passwd', 'credential', 'credentials')
_SECRET_PAIRS = {
    ('api', 'key'), ('access', 'key'), ('private', 'key'), ('secret', 'key'),
    ('auth', 'key'), ('client', 'key'), ('client', 'secret'),
    ('access', 'token'), ('refresh', 'token'), ('session', 'token'), ('auth', 'token'),
    ('subscription', 'url'), ('subscribe', 'url'), ('sub', 'url'),
}


def _normalise_key(name):
    # camelCase splits into components: `clientSecret` is the same key as `client-secret`.
    text = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '-', str(name).strip())
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _key_is_secret(name):
    normalised = _normalise_key(name)
    if normalised in _SECRET_KEYS:
        return True
    # `header.Authorization`, `provider.headers.Authorization`: a dotted key carries the
    # secret in its last component even when the whole string is not in the set.
    for part in re.split(r'[.\s]+', str(name).strip()):
        if _normalise_key(part) in _SECRET_KEYS:
            return True
    tokens = [token for token in normalised.split('-') if token]
    if tokens and tokens[-1] in _SECRET_SUFFIXES:
        return True
    if len(tokens) >= 2 and (tokens[-2], tokens[-1]) in _SECRET_PAIRS:
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


def _redact_tree(node, depth=0):
    if depth > _MAX_NESTING:
        raise RedactionError('document nesting limit')
    if isinstance(node, dict):
        clean = {}
        for key, value in node.items():
            if _key_is_secret(key):
                clean[key] = PLACEHOLDER
            else:
                clean[key] = _redact_tree(value, depth + 1)
        return clean
    if isinstance(node, list):
        return [_redact_tree(item, depth + 1) for item in node]
    if isinstance(node, str):
        clean = redact_text(node, depth + 1)
        # Retain the prior scalar shape policy (non-HTTP userinfo and secret query
        # strings too), without discarding useful nested JSON for ordinary key/value logs.
        if _USERINFO_ANY.search(clean):
            return PLACEHOLDER
        for match in _SECRET_QUERY.finditer(clean):
            value = re.match(_VALUE, clean[match.end():])
            if value and not _is_placeholder(value.group(0)):
                return PLACEHOLDER
        return clean
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
        data = json.loads(text, object_pairs_hook=_unique_json_object)
    except ValueError:
        raise RedactionError('json parse failed')
    clean = _redact_tree(data)
    return json.dumps(clean, ensure_ascii=False, indent=2) + '\n'


# ---------------------------------------------------------------- text

# One lexical key grammar; policy is _key_is_secret, not a second list of aliases.
# Quoted values include whitespace. Composite auth values consume scheme AND credential.
_QUOTED = r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\''
# An unquoted value keeps going across spaces AND across an indented obs-fold
# (like _HEADER) until a real `name=value` field begins, so `password=hello
# world` and a folded `password=raw1\n raw2tail` mask the whole tail while
# `user=admin` after a secret stays its own field. A col-0 (unindented) next
# line is a new logical record, not a value tail: masking it would swallow the
# next log entry, and real multi-line secrets are quoted (handled structurally).
# A field boundary needs a lowercase `name` (case-sensitive, even though the
# key grammar is case-insensitive) and a NON-EMPTY value: a trailing base64
# padding token like `Y2U6cHc=` (uppercase / empty after `=`) is secret tail,
# never a field, so it is masked rather than left to leak.
_FIELD_START = r'(?-i:[a-z][a-z0-9_.-]*)\s*[:=]\s*[^\s,;&}\]"\']'
_UNQUOTED = (r'[^\s,;&}\]"\']+(?:(?:[ \t]+|\r?\n[ \t]+)(?!'
             + _FIELD_START + r')[^\s,;&}\]"\']+)*')
_VALUE = r'(?:' + _QUOTED + r'|' + _UNQUOTED + r')'
# A Basic/Bearer credential is opaque (base64 may contain spaces and `=`, and
# may fold across indented lines); mask through the fold rather than leaving a
# folded tail to leak.
_AUTH_VALUE = r'(?:' + _QUOTED + r'|[^\r\n,;&}]+(?:\r?\n[ \t]+[^\r\n]*)*)'
_KEYVAL = re.compile(
    r'(?i)(?=(?<![A-Za-z0-9_.-])(["\']?)([A-Za-z0-9_.-]+)\1'
    r'(\s*[:=]\s*)(' + _VALUE + r'))')
_AUTH = re.compile(r'(?i)\b(basic|bearer)\s+(' + _AUTH_VALUE + r')')
_HEADER = re.compile(
    r'(?i)(?<![A-Za-z0-9_.-])(["\']?)(?:proxy-)?authorization\1'
    r'[ \t]*[:=][ \t]*(?:\r?\n[ \t]+)*('
    + _QUOTED + r'|[^\r\n}\]]+(?:\r?\n[ \t]+[^\r\n]*)*)')
_UNFRAMED_HEADER = re.compile(
    r'(?i)(?<![A-Za-z0-9_.-])(["\']?)(?:proxy-)?authorization\1'
    r'[ \t]*[:=][ \t]*\r?\n(?=[^ \t\r\n])')
_STRINGS = re.compile(_QUOTED, re.DOTALL)
_URL = re.compile(r'(?i)\bhttps?://[^\s"\'<>]+')
_MAX_NESTING = 32


def _decoded_quote(token):
    if token.startswith('"'):
        try:
            return json.loads(token)
        except ValueError:
            pass
    # Log/YAML single quotes: support doubled quotes without evaluating escape code.
    return token[1:-1].replace("''", "'")


_PLACEHOLDERS = {'', '…', "'…'", '"…"', '[redacted]', '***', 'null', 'none', '~'}


def _is_placeholder(value):
    token = value.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        token = token[1:-1].strip()
    if token.lower() in _PLACEHOLDERS:
        return True
    # A real redaction run is only the ellipsis marker (possibly repeated when a
    # folded credential is masked in pieces). Real placeholder *words* like
    # `null`/`none` in a multi-word value are a live secret tail, not redaction.
    pieces = token.split()
    return bool(pieces) and all(piece == '…' for piece in pieces)


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


def _unique_json_object(pairs):
    # Duplicate decoded keys are ambiguous (including escape-equivalent spellings).
    result = {}
    for key, value in pairs:
        if key in result:
            raise RedactionError('duplicate json key')
        result[key] = value
    return result


_JSON_DECODER = json.JSONDecoder(object_pairs_hook=_unique_json_object)
_JSON_OBJECT_START = re.compile(r'\s*(?:"(?:[^"\\]|\\.)*"\s*:|})')
_JSON_ARRAY_START = re.compile(r'\s*(?:[\[\]{"0-9-]|true\b|false\b|null\b)')


def _embedded_json(text):
    """Yield whole object/array spans. Never decode a key apart from its value.

    Quoted scalars are visited by the caller after decoding their complete string.
    Malformed object-shaped candidates and duplicate keys fail closed. Ordinary
    nft string sets and non-JSON bracket notation are not interpreted as JSON.
    """
    pos = 0
    while pos < len(text):
        quoted = _STRINGS.match(text, pos)
        if quoted:
            pos = quoted.end()
            continue
        char = text[pos]
        looks_json = ((char == '{' and _JSON_OBJECT_START.match(text, pos + 1))
                      or (char == '[' and _JSON_ARRAY_START.match(text, pos + 1)))
        if looks_json:
            try:
                node, end = _JSON_DECODER.raw_decode(text, pos)
            except ValueError:
                if char == '[':
                    # Bracketed timestamps/addresses are not necessarily arrays.
                    # Keep scanning so any inner JSON object is still classified.
                    pos += 1
                    continue
                raise RedactionError('embedded json parse failed')
            except RecursionError:
                raise RedactionError('embedded json nesting limit')
            yield pos, end, node
            pos = end
        else:
            pos += 1


def _ambiguous_auth_folding(text):
    spans = [match.span() for match in _STRINGS.finditer(text)]
    for match in _UNFRAMED_HEADER.finditer(text):
        if not any(start < match.start() < end for start, end in spans):
            return True
    for match in _HEADER.finditer(text):
        if any(start < match.start() < end for start, end in spans):
            continue
        # Obs-fold must start with SP/HTAB. A bare scheme followed by a physical
        # line without that framing cannot safely be separated from a credential.
        if (match.group(2).strip().lower() in ('basic', 'bearer')
                and (match.end() < len(text) and text[match.end()] in '\r\n')):
            return True
    return False


def redact_text_line(line):
    return redact_text(line)


def redact_text(text, depth=0, *, embedded=True):
    if depth > _MAX_NESTING:
        raise RedactionError('text nesting limit')
    # Parse whole embedded JSON before any lexical substitution can destroy its
    # key/value association or quoting. Arrays and decoded keys share tree policy.
    if embedded:
        pieces, previous = [], 0
        for start, end, node in _embedded_json(text):
            pieces.append(text[previous:start])
            pieces.append(json.dumps(_redact_tree(node, depth + 1), ensure_ascii=False))
            previous = end
        pieces.append(text[previous:])
        text = ''.join(pieces)
    # Work inside encoded log strings next, then serialize them back. This handles
    # escaped JSON embedded in JSON scalar messages without corrupting outer JSON.
    def quoted(match):
        token = match.group(0)
        decoded = _decoded_quote(token)
        clean = redact_text(decoded, depth + 1)
        if clean == decoded:
            return token
        if token[0] == '"':
            return json.dumps(clean, ensure_ascii=False)
        return "'" + clean.replace("'", "''") + "'"
    text = _STRINGS.sub(quoted, text)
    if _ambiguous_auth_folding(text):
        raise RedactionError('ambiguous authorization folding')
    # A header is a composite value (also Digest/custom schemes). Mask its whole
    # credential, including any tail after a placeholder from an older redactor.
    spans = [match.span() for match in _STRINGS.finditer(text)]
    def header(match):
        # Quoted contents were handled recursively; never eat their closing quote.
        if any(start < match.start() < end for start, end in spans):
            return match.group(0)
        return match.group(0)[:match.start(2) - match.start()] + _mask_token(match.group(2))
    text = _HEADER.sub(header, text)
    text = _AUTH.sub(lambda m: m.group(1) + ' ' + _mask_token(m.group(2)), text)
    text = _URL.sub(_url_without_credentials, text)
    replacements = []
    for match in _KEYVAL.finditer(text):
        _, key, _, value = match.groups()
        if not _key_is_secret(key):
            continue
        # A complete auth credential was masked above; leave its scheme useful.
        if (_normalise_key(key) in ('authorization', 'authentication', 'auth')
                and value.lower() in ('basic', 'bearer')):
            continue
        if _normalise_key(key) in ('url', 'subscribe-url') and _plain_host_url(value):
            continue
        start, end = match.span(4)
        # A parent secret value owns any nested credential spans.
        if replacements and start < replacements[-1][1]:
            continue
        replacements.append((start, end, _mask_token(value)))
    for start, end, value in reversed(replacements):
        text = text[:start] + value + text[end:]
    text = _JWT.sub(PLACEHOLDER, text)
    return _LONG_TOKEN.sub(PLACEHOLDER, text)


# ---------------------------------------------------------------- nft producer

def _nft_parts(text):
    """Lex strings and # comments, never evaluate nft syntax or require nft -j.

    Bare nft tokens/maps are not JSON. Quoted payloads must have an unambiguous
    supported string encoding; anything unsupported/unclosed fails without raw output.
    """
    parts = []
    pos = 0
    while pos < len(text):
        if text[pos] == '#':
            end = text.find('\n', pos)
            end = len(text) if end < 0 else end
            parts.append(('comment', pos, end, text[pos:end]))
            pos = end
        elif text[pos] in ('"', "'"):
            match = _STRINGS.match(text, pos)
            if not match:
                raise RedactionError('nft string parse failed')
            token = match.group(0)
            if token[0] == '"':
                try:
                    decoded = json.loads(token)
                except ValueError:
                    raise RedactionError('nft string escape unsupported')
            else:
                if '\\' in token:
                    raise RedactionError('nft string escape unsupported')
                decoded = token[1:-1]
            parts.append(('string', pos, match.end(), decoded))
            pos = match.end()
        else:
            pos += 1
    return parts


def _nft_key_values(text, parts):
    # Also bind credential-shaped quoted map keys to scalar values. Escaped/spaced
    # names must not turn nft.txt into a bypass for the existing normalized policy.
    for kind, _, end, key in parts:
        if kind != 'string' or not _key_is_secret(key):
            continue
        colon = re.match(r'\s*:\s*', text[end:])
        if not colon:
            continue
        start = end + colon.end()
        value = re.match(_VALUE, text[start:])
        if not value or text[start:start + 1] in ('{', '['):
            raise RedactionError('ambiguous nft credential value')
        finish = start + value.end()
        if not re.match(r'[ \t]*(?:[,;}\]\r\n#]|$)', text[finish:]):
            raise RedactionError('ambiguous nft credential value')
        token = value.group(0)
        decoded = _decoded_quote(token) if token[0] in ('"', "'") else token
        yield start, finish, decoded


def _nft_segments(text, parts):
    previous = 0
    for kind, start, end, _ in parts:
        if kind == 'comment':
            yield False, text[previous:start]
            yield True, text[start:end]
            previous = end
    yield False, text[previous:]


def redact_nft(text):
    parts = _nft_parts(text)
    replacements = list(_nft_key_values(text, parts))
    for start, end, value in reversed(replacements):
        if not _is_placeholder(value):
            text = text[:start] + json.dumps(PLACEHOLDER, ensure_ascii=False) + text[end:]
    # Only bare nft syntax opts out of JSON-object recognition. Every quoted scalar
    # still recurses through strict redact_text; comments use strict text directly.
    return ''.join(redact_text(segment, embedded=is_comment)
                   for is_comment, segment in _nft_segments(text, _nft_parts(text)))


def _verify_nft(filename, text):
    # Independent checks, not a comparison to redact_nft output.
    try:
        parts = _nft_parts(text)
        findings = ['%s:0: nft credential' % filename
                    for _, _, value in _nft_key_values(text, parts)
                    if not _is_placeholder(value)]
        for is_comment, segment in _nft_segments(text, parts):
            findings.extend(_verify_text(filename, segment, embedded=is_comment))
        return findings
    except RedactionError:
        return ['%s:0: nft parse failed' % filename]


# ---------------------------------------------------------------- verify

def _plain_host_url(value):
    token = value.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        token = token[1:-1].strip()
    return bool(re.match(r'(?i)^https?://[^/\s?#@]+$', token))


def _verify_text(filename, text, depth=0, *, embedded=True):
    """Independent classifier: never call redaction or compare against its output."""
    if depth > _MAX_NESTING:
        return ['%s:0: text nesting limit' % filename]
    findings = []
    if embedded:
        try:
            for _, _, node in _embedded_json(text):
                findings.extend(_verify_tree(filename, node, depth + 1))
        except RedactionError:
            findings.append('%s:0: embedded json parse failed' % filename)
    if _ambiguous_auth_folding(text):
        findings.append('%s:0: ambiguous authorization folding' % filename)
    # Verify full logical headers, including every SP/HTAB continuation, rather
    # than trusting a placeholder on the first physical line.
    spans = [match.span() for match in _STRINGS.finditer(text)]
    for match in _HEADER.finditer(text):
        if any(start < match.start() < end for start, end in spans):
            continue
        if not _is_placeholder(match.group(2)):
            findings.append('%s:0: authorization-header' % filename)
    for number, line in enumerate(text.split('\n'), 1):
        if _USERINFO_ANY.search(line):
            findings.append('%s:%d: url-userinfo' % (filename, number))
        for match in _SECRET_QUERY.finditer(line):
            value = re.match(_VALUE, line[match.end():])
            if value and not _is_placeholder(value.group(0)):
                findings.append('%s:%d: secret-query' % (filename, number))
        if _LONG_TOKEN.search(line) or _JWT.search(line):
            findings.append('%s:%d: long-token' % (filename, number))
    # Key/value and auth-credential spans run over the whole buffer exactly like
    # redaction, so a value or credential that continues on the next physical
    # line is judged as one span and the raw form is still refused (per-line
    # scanning would miss that fold).
    for match in _AUTH.finditer(text):
        if not _is_placeholder(match.group(2)):
            number = text.count('\n', 0, match.start()) + 1
            findings.append('%s:%d: authorization' % (filename, number))
    for match in _KEYVAL.finditer(text):
        _, key, _, value = match.groups()
        if not _key_is_secret(key):
            continue
        if _normalise_key(key) in ('url', 'subscribe-url') and _plain_host_url(value):
            continue
        if value.lower() in ('basic', 'bearer'):
            # Only a complete recognized scheme+credential is handled below.
            tail = text[match.start(4):]
            auth = _AUTH.match(tail)
            if auth and _is_placeholder(auth.group(2)):
                continue
        if not _is_placeholder(value):
            number = text.count('\n', 0, match.start()) + 1
            findings.append('%s:%d: %s' % (filename, number, _normalise_key(key)))
    for match in _STRINGS.finditer(text):
        findings.extend(_verify_text(filename, _decoded_quote(match.group(0)), depth + 1))
    return findings


def _verify_tree(filename, node, depth=0):
    if depth > _MAX_NESTING:
        return ['%s:0: document nesting limit' % filename]
    findings = []
    if isinstance(node, dict):
        for key, value in node.items():
            if _key_is_secret(key) and not (isinstance(value, str) and _is_placeholder(value)) and value is not None:
                findings.append('%s:0: %s' % (filename, _normalise_key(key)))
            findings.extend(_verify_tree(filename, value, depth + 1))
    elif isinstance(node, list):
        for value in node:
            findings.extend(_verify_tree(filename, value, depth + 1))
    elif isinstance(node, str):
        findings.extend(_verify_text(filename, node, depth + 1))
    return findings


def _verify_uci(filename, text):
    try:
        records = _uci_records(text)
    except RedactionError:
        return ['%s:0: uci parse failed' % filename]
    sections = {(parts[0], parts[1]): parts[2]
                for kind, parts, _, _ in records if kind == 'section'}
    findings = []
    for kind, parts, prefix, values in records:
        if kind != 'option':
            continue
        pkg, sec, option = parts
        for value in values:
            if not _uci_allowed(sections.get((pkg, sec)), option) and not _is_placeholder(value):
                number = text[:text.find(prefix)].count('\n') + 1
                findings.append('%s:%d: %s' % (filename, number, _normalise_key(option)))
            findings.extend(_verify_text(filename, value))
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
            text = data.decode('utf-8', 'replace')
            if name == 'nft.txt':
                findings.extend(_verify_nft(relative, text))
            elif name.endswith('.uci'):
                findings.extend(_verify_uci(relative, text))
            elif name.endswith(('.json', '.yaml', '.yml')) and text.strip():
                try:
                    node = json.loads(text, object_pairs_hook=_unique_json_object) if name.endswith('.json') else yaml.safe_load(text)
                    findings.extend(_verify_tree(relative, node))
                except (ValueError, yaml.YAMLError, RecursionError, RedactionError):
                    findings.append('%s:0: document parse failed' % relative)
            else:
                findings.extend(_verify_text(relative, text))
    return findings


# ---------------------------------------------------------------- main

_REDACTORS = {
    'uci': redact_uci,
    'yaml': redact_yaml,
    'json': redact_json,
    'text': redact_text,
    'nft': redact_nft,
}


def main(argv):
    if len(argv) < 2:
        return fail('usage: geomask-diag-redact.py redact <uci|yaml|json|text|nft> | verify <dir>')
    command = argv[1]
    if command == 'redact':
        if len(argv) != 3 or argv[2] not in _REDACTORS:
            return fail('usage: geomask-diag-redact.py redact <uci|yaml|json|text|nft>')
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
