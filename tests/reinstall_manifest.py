"""R03 phase 1: verifiable write-set of the transactional reinstall.

Static extraction of writer sites from `geomask.sh.in` and its transitive callees, plus
the manifest data (`tests/reinstall_write_set.json`) and the gate that compares them.

The gate is fail-closed by construction:
  * every writer site found in the sources must be covered by a manifest entry
    (an exact path, a declared dynamic pattern, or a declared runtime/effect object);
  * every manifest entry must point back at real evidence in the sources;
  * every absolute path literal of a declared transitive callee must be classified;
  * dynamic classes use concrete site patterns - a catch-all pattern would absorb
    new unresolvable writers (review D);
  * uci/net/process effects carry concrete keys/patterns - a new key, ip rule or
    killed process is a gate failure (review E);
  * every script the installer executes is a declared transitive callee or a
    documented exception (review B);
  * forbidden effects (apk/opkg during reinstall) must be declared AND guarded.

Safety: pure text analysis of repository files. No commands are executed and no live
system path is read or written by this module.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'geomask.sh.in'
MANIFEST_PATH = Path(__file__).resolve().parent / 'reinstall_write_set.json'

MESSAGE_RE = re.compile(r'^(info|warn|bad|log|_bnote|_bwarn|_bbad)\s')


# ---------------------------------------------------------------------------
# manifest


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# function spans and heredoc bodies


def function_spans(text: str) -> list[tuple[str, int, int]]:
    """[(name, start_line, end_line)] for every column-0 shell function (1-based)."""
    lines = text.splitlines()
    spans = []
    start = None
    name = None
    for number, line in enumerate(lines, 1):
        if start is None:
            match = re.match(r'^([A-Za-z_]\w*)\(\)\s*\{', line)
            if match:
                if re.search(r'\}\s*$', line.split(')', 1)[1]):
                    spans.append((match.group(1), number, number))   # one-liner
                    continue
                name = match.group(1)
                start = number
        elif re.match(r'^\}', line):
            spans.append((name, start, number))
            start = None
            name = None
    return spans


def enclosing_function(spans, line_no):
    best = '<top-level>'
    best_width = None
    for name, start, end in spans:
        if start <= line_no <= end:
            width = end - start
            if best_width is None or width < best_width:
                best, best_width = name, width
    return best


def heredoc_lines(text: str) -> set[int]:
    """1-based line numbers that are inside a heredoc body (not the opening line)."""
    inside = set()
    delimiter = None
    for number, line in enumerate(text.splitlines(), 1):
        if delimiter is None:
            match = re.search(r"<<-?\s*'([^']+)'|<<-?\s*\"([^\"]+)\"|<<-?\s*(\w+)\s*$", line)
            if match:
                delimiter = match.group(1) or match.group(2) or match.group(3)
        else:
            if line.strip() == delimiter:
                delimiter = None
            else:
                inside.add(number)
    return inside


SCRIPT_WRITE_RE = re.compile(
    r"json\.dump\(|open\([^)]*['\"](w|a)['\"]|write_text|os\.replace|shutil\.|\.unlink\("
    r"|schedule_set|save_store|save_report|save_pool|append_event|\.write\(")


# ---------------------------------------------------------------------------
# best-effort literal resolution


ASSIGN_RE = re.compile(r'^\s*(?:local\s+|export\s+)?([A-Za-z_]\w*)=(.*)$')
PATH_SHAPE_RE = re.compile(r'^/[A-Za-z0-9._/*-]+$')
PATH_PREFIXES = ('/etc/', '/opt/', '/tmp/', '/root/', '/usr/', '/var/', '/www/', '/proc/',
                 '/sys/', '/sbin/', '/bin/', '/lib/', '/mnt/', '/overlay/')


def _is_path(candidate: str) -> bool:
    return (candidate.startswith('/') and candidate != '/dev/null'
            and bool(PATH_SHAPE_RE.match(candidate)))


@dataclass
class Resolver:
    text: str
    spans: list
    var_global: dict = field(default_factory=dict)     # name -> [expr, ...]
    var_local: dict = field(default_factory=dict)      # function -> {name: [expr, ...]}
    arg_values: dict = field(default_factory=dict)     # function -> {param: [literal, ...]}

    @classmethod
    def build(cls, text: str) -> 'Resolver':
        spans = function_spans(text)
        res = cls(text=text, spans=spans)
        for number, line in enumerate(text.splitlines(), 1):
            for piece in re.split(r';(?=(?:[^\'"]*[\'"][^\'"]*[\'"])*[^\'"]*$)', line):
                match = ASSIGN_RE.match(piece)
                if not match:
                    continue
                name, value = match.group(1), match.group(2).strip()
                value = re.sub(r'\s+#.*$', '', value).strip()
                if value[:1] in ('"', "'"):
                    value = value[1:-1] if value.endswith(value[0]) else value[1:]
                if not value:
                    continue
                function = enclosing_function(spans, number)
                target = res.var_local.setdefault(function, {}) \
                    if function != '<top-level>' else res.var_global
                values = target.setdefault(name, [])
                if value not in values:
                    values.append(value)
        for name, start, end in spans:
            found = {'1': set(), '2': set()}
            pattern = re.compile(r'(?:^|[\s;|&{(])' + re.escape(name)
                                 + r'\s+("[^"]*"|\'[^\']*\'|[^\s;|&)]+)'
                                 r'(?:\s+("[^"]*"|\'[^\']*\'|[^\s;|&)]+))?')
            for number, line in enumerate(text.splitlines(), 1):
                if start <= number <= end:
                    continue
                for match in pattern.finditer(line):
                    for index, group in enumerate(match.groups(), 1):
                        if group is None:
                            continue
                        value = group.strip('"\'')
                        if value and not value.startswith('$') and not value.startswith('-'):
                            found[str(index)].add(value)
            values = {key: sorted(items) for key, items in found.items() if items}
            if values:
                res.arg_values[name] = values
        return res

    def _values(self, name: str, function: str) -> list[str]:
        local = (self.var_local.get(function) or {}).get(name)
        if local:
            return local
        return self.var_global.get(name, [])

    def _expand_subs(self, expr: str, function: str, depth: int) -> list[str]:
        """Expand every $(...) substitution, keeping multi-valued results as candidates."""
        match = re.search(r'\$\(([^()]*)\)', expr)
        if not match:
            return [expr]
        inner = match.group(1).strip()
        if inner.startswith('P '):
            values = self.resolve(inner[2:].strip(), function, depth + 1)
        else:
            values = ['*']
        if not values:
            values = ['*']
        out = []
        for value in values:
            for tail in self._expand_subs(expr[match.end():], function, depth):
                out.append(expr[:match.start()] + value + tail)
        return out

    def resolve(self, expr: str, function: str, depth: int = 0) -> list[str]:
        """Resolve an expression to literal path candidates (may contain '*')."""
        if depth > 6:
            return []
        expr = expr.strip().strip('"').strip("'")
        if not expr:
            return []
        results = []
        for candidate in self._expand_subs(expr, function, depth):
            results.extend(self._resolve_vars(candidate, function, depth))
        return [part for part in results if _is_path(part)]

    def _resolve_vars(self, expr: str, function: str, depth: int) -> list[str]:
        out = ['']
        i = 0
        while i < len(expr):
            char = expr[i]
            if char == '$':
                match = re.match(r'\$\{(\w+)\}|\$(\d)|\$(\w+)', expr[i:])
                if not match:
                    return []
                if match.group(2):
                    values = (self.arg_values.get(function) or {}).get(match.group(2), [])
                else:
                    values = self._values(match.group(1) or match.group(3), function)
                if not values:
                    return []
                expanded = []
                for base in out:
                    for value in values:
                        for resolved in (self.resolve(value, function, depth + 1) or ['*']):
                            expanded.append(base + resolved)
                out = expanded
                i += match.end()
                continue
            out = [part + char for part in out]
            i += 1
        return out


# ---------------------------------------------------------------------------
# site extraction


@dataclass
class Site:
    kind: str            # file | script | service | cron | uci | net | sysctl | process | package
    effect: str
    path: str | None
    function: str
    line_no: int
    raw: str
    extra: str = ''
    detail: str = ''     # uci key / net object / precise effect identity

    def key(self):
        return (self.kind, self.effect, self.path, self.function, self.raw.strip())


TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')
SAVE_STREAM_RE = re.compile(r'\bsave_stream\s+(/[^\s]+)\s+(\d{3,4})')
REDIRECT_RE = re.compile(r"(?<![0-9&>\-])>>?\s*(\"[^\"]*\"|'[^']*'|[/\$][^\s;|&]*)")
INITD_DIRECT_RE = re.compile(r'/etc/init\.d/([A-Za-z0-9._${}-]+)"?\s+"?([a-z]+)\b')
INITD_VAR_RE = re.compile(r'/etc/init\.d/\$(\w+)\)"?\s+"?\$(\w+)')
INITD_HELPER_RE = re.compile(r'\binitd\s+([A-Za-z0-9._-]+)\s+([a-z]+)\b')
INITD_ARG_RE = re.compile(r'\binitd\s+"?\$(\w+)"?\s+([a-z]+)\b')
UCI_RE = re.compile(r'\buci\s+(?:-[a-z]\s+)*(set|delete|add|add_list|rename|commit)\b'
                    r'\s*([^\s;|&]*)(?:\s+([^\s;|&)]+))?')
IP_WRITE_RE = re.compile(r'\bip\s+(?:-\d\s+)?(rule|route|addr|neigh|link)\s+(add|del|delete|replace|flush)\b')
NFT_RE = re.compile(r'\bnft\s+(?:-\S+\s+)*(add|delete|flush|replace|create)\b|\bnft\s+-c\b')
SYSCTL_RE = re.compile(r'\bsysctl\s+-p\b')
APK_RE = re.compile(r'\b(apk|opkg)\s+(add|install|update)\b')
PROC_RE = re.compile(r'\b(pkill|kill)\s+')
FILE_OP_RE = re.compile(r'\b(rm|cp|mv|ln|chmod|mkdir|touch)\s+(-[a-zA-Z]+\s+)*')
SED_WRITE_RE = re.compile(r'\bsed\s+-[a-zA-Z]*i')
MESSAGE_STR_RE = re.compile(r'\b(?:info|warn|bad|log|_bnote|_bwarn|_bbad|printf|echo|die)\s+'
                            r'(\'[^\']*\'|"[^"]*")')


def _strip_messages(line: str) -> str:
    """Remove message literals from a shell line so text like `warn "uci add failed"`
    is never mistaken for a real uci/package/cron writer site."""
    return MESSAGE_STR_RE.sub('""', line)


def _uci_key(verb: str, arg: str, arg2: str) -> str:
    """Canonical effect identity of a uci call: the exact key, or add:/commit: forms."""
    if verb == 'commit':
        return 'commit:' + arg
    if verb == 'add':
        return 'add:' + arg + ((' ' + arg2) if arg2 else '')
    return arg.split('=', 1)[0]


def extract_sites(text: str) -> list[Site]:
    spans = function_spans(text)
    heredocs = heredoc_lines(text)
    resolver = Resolver.build(text)
    sites: list[Site] = []
    seen = set()

    def add(site: Site):
        if site.key() not in seen:
            seen.add(site.key())
            sites.append(site)

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        function = enclosing_function(spans, number)

        for match in SAVE_STREAM_RE.finditer(line):
            add(Site('file', 'save_stream', match.group(1), function, number, line,
                     extra=match.group(2)))

        if number in heredocs:
            # shell extraction is meaningless inside a heredoc body; a python/shell body
            # that writes files is recorded as one coarse dynamic site per function.
            if SCRIPT_WRITE_RE.search(line):
                add(Site('script', 'script-write', None, function, number, line))
            continue

        if MESSAGE_RE.match(line):
            continue

        # message literals (`warn "uci add failed ..."`) are not writer sites
        line = _strip_messages(line)

        if re.search(r'\b(initd|uci|nft|sysctl|apk|opkg|pkill|kill|crontab)\b|\bip\s+(rule|route|addr)\b'
                     r'|/etc/init\.d/|(?<![0-9&>\-])>>?\s*["\'/\$]'
                     r'|\b(rm|cp|mv|ln|chmod|mkdir|touch|sed)\s+-?[/\$\w"]',
                     line):
            # redirects
            for match in REDIRECT_RE.finditer(line):
                target = match.group(1).strip('"\'')
                if target.startswith('/dev/null'):
                    continue
                resolved = resolver.resolve(target, function) or [None]
                for path in resolved:
                    add(Site('file', 'redirect-append' if match.group(0).lstrip().startswith('>>')
                             else 'redirect', path, function, number, line))
            # file operations
            op_match = FILE_OP_RE.search(line) or SED_WRITE_RE.search(line)
            if op_match:
                op = 'sed-i' if line[op_match.start():].startswith('sed') else op_match.group(1)
                tail = line[op_match.end():]
                for token in TOKEN_RE.findall(tail):
                    token = token.strip('"\'')
                    token = token.rstrip(';')
                    if '(' not in token:
                        token = token.rstrip(')')
                    if not token or token.startswith('-') or token in ('.', '..'):
                        continue
                    if not (token.startswith('/') or token.startswith('$') or '$(P' in token):
                        continue
                    for path in resolver.resolve(token, function) or [None]:
                        add(Site('file', op, path, function, number, line))
            # services
            for match in INITD_HELPER_RE.finditer(line):
                if match.group(2) not in ('enable', 'disable', 'start', 'stop', 'restart',
                                          'reload', 'restart_verified'):
                    continue
                add(Site('service', match.group(2), None, function, number, line,
                         extra=match.group(1)))
            for match in INITD_ARG_RE.finditer(line):
                add(Site('service', match.group(2), None, function, number, line,
                         extra='dynamic-name'))
            for match in INITD_DIRECT_RE.finditer(line):
                name = match.group(1)
                action = match.group(2)
                extra = 'dynamic-name' if '$' in name else name
                add(Site('service', action, None, function, number, line, extra=extra))
            for match in INITD_VAR_RE.finditer(line):
                add(Site('service', 'dynamic-action', None, function, number, line,
                         extra='dynamic-name'))
            # crontab
            if 'crontab' in line:
                effect = 'write' if re.search(r'(\|\s*crontab\b|crontab\s+("|\$|/))', line) else 'read'
                add(Site('cron', effect, None, function, number, line))
            # uci
            for match in UCI_RE.finditer(line):
                arg = match.group(2).strip('"\'')
                arg2 = (match.group(3) or '').strip('"\'')
                key = _uci_key(match.group(1), arg, arg2)
                if match.group(1) in ('commit', 'add'):
                    package = arg
                else:
                    package = key.split('.', 1)[0] if key else ''
                if package not in ('network', 'dhcp', 'ksmbd', 'firewall', 'system', 'wireless'):
                    package = '*'
                add(Site('uci', match.group(1), None, function, number, line,
                         extra=package, detail=key))
            # network runtime
            for match in IP_WRITE_RE.finditer(line):
                add(Site('net', 'ip-' + match.group(2), None, function, number, line,
                         extra=match.group(1), detail=match.group(1)))
            if NFT_RE.search(line):
                add(Site('net', 'nft', None, function, number, line, detail='nft'))
            if SYSCTL_RE.search(line):
                add(Site('sysctl', 'apply', None, function, number, line, detail='sysctl'))
            # packages
            for match in APK_RE.finditer(line):
                add(Site('package', match.group(2), None, function, number, line,
                         extra=match.group(1)))
            # processes
            if PROC_RE.search(line):
                add(Site('process', 'signal', None, function, number, line))
    return sites


# ---------------------------------------------------------------------------
# cron content extraction (markers and lines)


def cron_content(text: str) -> list[tuple[str, str]]:
    """(marker, line) pairs the installer puts into the crontab."""
    found = []
    for match in re.finditer(r"cron_ensure\s+\S+\s+'([^']+)'\s+'([^']+)'", text):
        found.append((match.group(1), match.group(2)))
    for match in re.finditer(r"cron_verify\s+\S+\s+'([^']+)'", text):
        found.append((match.group(1), ''))
    for match in re.finditer(r"echo\s+'([^']+)'\s*\)\s*\|\s*crontab", text):
        found.append(('', match.group(1)))
    return found


# ---------------------------------------------------------------------------
# transitive callees


def path_literals(text: str) -> set[str]:
    """Every absolute path literal in a callee body (read or write - all classified).

    Full-line comments are skipped: a path named in prose is not a runtime reference.
    """
    result = set()
    for line in text.splitlines():
        if line.lstrip().startswith('#'):
            continue
        for match in re.finditer(
                r'[\"\'(= \t](/(?:etc|opt|tmp|root|usr|var|www|proc|sys|sbin|bin|lib|mnt|overlay)'
                r'/[A-Za-z0-9._/*-]*)', line):
            if _is_path(match.group(1)):
                result.add(match.group(1))
    return result


def callee_texts(manifest: dict) -> dict:
    """{source label: text scope} for every declared transitive callee.

    A scope is either "all", a list of function names, or a {from,to} line-marker dict.
    The dict form extracts the region from the first line matching `from` up to (not
    including) the first later line matching `to` - this is how the geo-backend.sh IPv6
    writer cluster is scanned. A missing marker yields an empty body, which the gate
    reports as a failure instead of silently scanning nothing.
    """
    out = {}
    for callee in manifest.get('callees') or []:
        path = ROOT / callee['file']
        text = path.read_text(encoding='utf-8')
        scope = callee.get('scope', 'all')
        if scope == 'all':
            out[callee['file']] = text
            continue
        if isinstance(scope, dict):
            start_re = re.compile(scope.get('from') or '$^')
            end_re = re.compile(scope.get('to') or '$^')
            lines = text.splitlines()
            begin = end = None
            for number, line in enumerate(lines):
                if begin is None:
                    if start_re.search(line):
                        begin = number
                elif end_re.search(line):
                    end = number
                    break
            label = '%s#%s..%s' % (callee['file'], scope.get('from'), scope.get('to'))
            if begin is None:
                out[label + '!!from-marker-missing'] = ''
            elif end is None:
                out[label + '!!to-marker-missing'] = ''
            else:
                out[label] = '\n'.join(lines[begin:end])
            continue
        spans = function_spans(text)
        pieces = []
        for name in scope:
            for span_name, start, end in spans:
                if span_name == name:
                    pieces.append('\n'.join(text.splitlines()[start - 1:end]))
        out[callee['file'] + '#' + ','.join(scope)] = '\n'.join(pieces)
    return out


# ---------------------------------------------------------------------------
# the gate


def _covered_by_files(manifest: dict, path: str, allow_policy=False) -> bool:
    for entry in manifest.get('files') or []:
        if entry.get('path') == path:
            return True
    for entry in manifest.get('read_only') or []:
        if entry.get('path') == path:
            return True
    # The updater's install-root policy classifies literals inside a *callee*; it must never
    # excuse an installer write site, so only the callee pass sets allow_policy.
    if allow_policy:
        for entry in manifest.get('callee_path_policy') or []:
            if entry.get('path') == path:
                return True
    return _covered_by_dynamic(manifest, path)


def _covered_by_dynamic(manifest: dict, path: str) -> bool:
    for entry in manifest.get('dynamic') or []:
        if re.fullmatch(entry.get('path_regex', '$^'), path):
            return True
    return False


def _dynamic_entry_matches(entry: dict, site: Site) -> bool:
    functions = entry.get('functions') or [entry.get('function')]
    if site.function not in functions:
        return False
    effects = entry.get('effects') or []
    if effects and site.effect not in effects:
        return False
    pattern = entry.get('site_regex')
    if pattern and not re.search(pattern, site.raw):
        return False
    return True


def _covered_dynamic_site(manifest: dict, site: Site) -> bool:
    return any(_dynamic_entry_matches(entry, site)
               for entry in manifest.get('dynamic') or [])


# ---------------------------------------------------------------------------
# invoked scripts: an undeclared transitive callee must not stay invisible


SCRIPT_PATH_RE = (r'(/usr/(?:bin|libexec)/[\w./-]+\.(?:sh|py)'
                  r'|/usr/lib/geomask/[\w./-]+\.py|/root/[\w./-]+\.sh)')
INVOCATION_RES = (
    # "$(P <script>)" <subcommand>
    re.compile(r'\$\(P ' + SCRIPT_PATH_RE + r'\)"\s+[-A-Za-z]'),
    # sh "$(P <script>)" / python3 - "$(P <script>)"
    re.compile(r'\b(?:sh|python3)\s+(?:-\s+)?["\']?\$\(P ' + SCRIPT_PATH_RE + r'\)'),
    # VAR="$(P <script>)" (assigned now, executed later)
    re.compile(r'="\$\(P ' + SCRIPT_PATH_RE + r'\)"'),
    # python3 /usr/lib/geomask/<script> <subcommand>
    re.compile(r'\bpython3\s+' + SCRIPT_PATH_RE + r'\s+[-\w]'),
)


def invoked_scripts(text: str) -> dict:
    """{install path: [line numbers]} for every script the installer executes."""
    out: dict = {}
    heredocs = heredoc_lines(text)
    for number, raw in enumerate(text.splitlines(), 1):
        line = _strip_messages(raw.strip())
        if not line or line.startswith('#') or number in heredocs:
            continue
        for pattern in INVOCATION_RES:
            for match in pattern.finditer(line):
                out.setdefault(match.group(1), []).append(number)
    return out


def check_legend(manifest: dict) -> list[str]:
    """Every snapshot/restore code used in the manifest must be explained in meta.legend."""
    problems = []
    legend = (manifest.get('meta') or {}).get('legend') or {}
    snapshot_codes = set(legend.get('snapshot') or {})
    restore_codes = set(legend.get('restore') or {})
    sections = ((manifest.get('files') or []) + (manifest.get('services') or [])
                + (manifest.get('cron') or []) + (manifest.get('uci') or [])
                + (manifest.get('net_runtime') or []) + (manifest.get('preflight_objects') or []))
    for entry in sections:
        code = entry.get('snapshot')
        if code and code not in snapshot_codes:
            problems.append('unknown snapshot code %r in %s' % (code, entry.get('id')))
        code = entry.get('restore')
        if code and code not in restore_codes:
            problems.append('unknown restore code %r in %s' % (code, entry.get('id')))
    return problems


def _covered_runtime(manifest: dict, site: Site) -> bool:
    if site.kind == 'cron':
        return bool(manifest.get('cron'))
    if site.kind == 'package':
        for entry in manifest.get('forbidden') or []:
            if entry.get('call') in (site.extra, 'apk/opkg') and (
                    not entry.get('functions') or site.function in entry['functions']):
                return True
        return False
    if site.kind == 'process':
        return any(re.search(entry.get('pattern') or '$^', site.raw)
                   for entry in manifest.get('processes') or [])
    if site.kind in ('sysctl', 'net'):
        wanted = 'sysctl' if site.kind == 'sysctl' else (site.detail or 'nft')
        return any(entry.get('object') == wanted
                   and re.search(entry.get('pattern') or '$^', site.raw)
                   for entry in manifest.get('net_runtime') or [])
    if site.kind == 'service':
        return any(entry.get('name') in (site.extra, '*', 'dynamic-name')
                   for entry in manifest.get('services') or [])
    if site.kind == 'uci':
        # precise contract: the exact uci key (or add:/commit: form) must match a
        # declared key pattern - a new key is a gate failure, not a package wildcard
        return any(re.search(pattern, site.detail)
                   for entry in manifest.get('uci') or []
                   for pattern in entry.get('key_patterns') or [])
    return False


def check(manifest: dict) -> list[str]:
    """The gate: empty list = manifest matches the actual writer set."""
    problems: list[str] = []
    text = INSTALLER.read_text(encoding='utf-8')
    sites = extract_sites(text)

    # 1. every writer site is covered
    for site in sites:
        if site.kind == 'script':
            if not _covered_dynamic_site(manifest, site):
                problems.append('uncovered script writer in %s:%d: %s'
                                % (site.function, site.line_no, site.raw.strip()[:100]))
            continue
        if site.kind != 'file':
            if not _covered_runtime(manifest, site):
                problems.append('uncovered %s site %s (extra=%s) in %s:%d: %s'
                                % (site.kind, site.effect, site.extra,
                                   site.function, site.line_no, site.raw.strip()[:100]))
            continue
        if site.path is not None:
            if not _covered_by_files(manifest, site.path):
                problems.append('uncovered file writer %s %s in %s:%d: %s'
                                % (site.effect, site.path, site.function, site.line_no,
                                   site.raw.strip()[:100]))
        else:
            if not _covered_dynamic_site(manifest, site):
                problems.append('uncovered dynamic writer %s in %s:%d: %s'
                                % (site.effect, site.function, site.line_no,
                                   site.raw.strip()[:100]))

    # 1b. every dynamic entry matches at least one real site (no stale declarations)
    for entry in manifest.get('dynamic') or []:
        if entry.get('evidence') == 'callee':
            continue
        if not any(_dynamic_entry_matches(entry, site) for site in sites):
            problems.append('dynamic entry without any matching site: %s' % entry.get('id'))

    # 1c. dynamic classes must be precise: a catch-all site_regex absorbs any new
    #     unresolvable writer inside its functions (review D)
    for entry in manifest.get('dynamic') or []:
        pattern = entry.get('site_regex')
        if not pattern or pattern in ('.*', '(.*)', '^.*$', '.*$', '^.*', ''):
            problems.append('dynamic entry with a catch-all site_regex: %s' % entry.get('id'))

    # 2. every manifest file entry has evidence in the sources
    extracted_paths = {site.path for site in sites if site.path is not None}
    script_functions = {site.function for site in sites if site.kind == 'script'}
    for entry in manifest.get('files') or []:
        evidence = entry.get('evidence') or {}
        kind = evidence.get('kind', 'site')
        if kind == 'uci':
            hits = [s for s in sites if s.kind == 'uci' and s.extra == evidence.get('package')]
            if not hits:
                problems.append('manifest entry without uci evidence: %s' % entry['path'])
        elif kind == 'script':
            if evidence.get('function') not in script_functions:
                problems.append('manifest entry without script evidence: %s' % entry['path'])
        elif kind in ('callee', 'dynamic'):
            pass        # validated in the callee / dynamic passes
        elif entry.get('path') not in extracted_paths:
            problems.append('manifest entry without writer evidence: %s' % entry['path'])

    # 3. cron content declared
    manifest_cron = {entry.get('marker') for entry in manifest.get('cron') or []}
    manifest_cron |= {entry.get('line') for entry in manifest.get('cron') or []}
    for marker, line in cron_content(text):
        if marker and marker not in manifest_cron:
            problems.append('cron marker not in manifest: %s' % marker)
        if line and line not in manifest_cron:
            problems.append('cron line not in manifest: %s' % line)

    # 4. transitive callees fully classified
    for label, body in callee_texts(manifest).items():
        if '!!' in label:
            problems.append('callee scope markers not found: %s' % label)
            continue
        if not body.strip():
            problems.append('callee scope body is empty: %s' % label)
            continue
        for path in sorted(path_literals(body)):
            if not _covered_by_files(manifest, path, allow_policy=True):
                problems.append('unclassified path in callee %s: %s' % (label, path))
        # 4b. uci/net/sysctl/process effects inside the callee bodies are covered by
        #     concrete declarations too (review E): a new key/rule/process fails here
        for site in extract_sites(body):
            if site.kind not in ('uci', 'net', 'sysctl', 'process'):
                continue
            if not _covered_runtime(manifest, site):
                problems.append('uncovered %s effect in callee %s (key=%s) in %s:%d: %s'
                                % (site.kind, label, site.detail or '-', site.function,
                                   site.line_no, site.raw.strip()[:100]))

    # 4d. An allow-list of install roots is a policy statement, not a writer, so each
    #     entry is declared with a reason and must still name a real literal. A stale
    #     entry would silently widen the reviewed set.
    callee_literals = set()
    for body in callee_texts(manifest).values():
        callee_literals |= path_literals(body)
    for entry in manifest.get('callee_path_policy') or []:
        path = entry.get('path') or ''
        if not entry.get('reason'):
            problems.append('callee path policy without a reason: %s'
                            % (entry.get('id') or path))
        if path and path not in callee_literals:
            problems.append('callee path policy entry without any literal: %s' % path)

    # 5. forbidden package effects are declared with the reinstall guard
    package_sites = [s for s in sites if s.kind == 'package']
    forbidden = manifest.get('forbidden') or []
    if package_sites and not forbidden:
        problems.append('package install sites found but no forbidden entry in the manifest')
    for entry in forbidden:
        for guard in entry.get('guards') or []:
            if guard not in text:
                problems.append('declared guard %s missing from geomask.sh.in' % guard)

    # 6. every package call is either behind the reinstall guard or in the install-only path
    for site in package_sites:
        if site.function in ('pkg_install', 'step1_apply'):
            continue
        if 'GM_REINSTALL_NO_PKG' not in site.raw:
            problems.append('unguarded package call in %s:%d: %s'
                            % (site.function, site.line_no, site.raw.strip()[:100]))

    # 7. every script the installer EXECUTES is a declared transitive callee or a
    #    documented exception (review B: undeclared callee = invisible)
    declared = {PurePosixPath(entry['file']).name
                for entry in manifest.get('callees') or []}
    excepted = {PurePosixPath(entry['file']).name
                for entry in manifest.get('not_a_callees') or []}
    for path, line_numbers in sorted(invoked_scripts(text).items()):
        name = PurePosixPath(path).name
        if name not in declared and name not in excepted:
            problems.append('invoked script not declared as a callee: %s (%s:%d)'
                            % (path, INSTALLER.name, line_numbers[0]))
    return problems



# ---------------------------------------------------------------------------
# product table cross-checks (review 16.11.2026, non-blocker 7):
# a service or a managed UCI object that exists in the manifest but not in the product
# table was invisible to every "preflight covers the write-set" claim.


def product_service_ids(text: str | None = None) -> set[str]:
    text = text if text is not None else INSTALLER.read_text(encoding='utf-8')
    match = re.search(r'^REINSTALL_PREFLIGHT_OBJECTS="([^"]*)"', text, re.M | re.S)
    if not match:
        return set()
    ids = set()
    for line in match.group(1).splitlines():
        parts = line.split('|')
        if len(parts) >= 2 and parts[1] == 'service' and parts[0]:
            ids.add(parts[0])
    return ids


def product_service_rows(text: str | None = None) -> list[tuple[str, str, str, str, str]]:
    """(id, class, initd, observed, mode) rows of REINSTALL_PREFLIGHT_OBJECTS."""
    text = text if text is not None else INSTALLER.read_text(encoding='utf-8')
    match = re.search(r'^REINSTALL_PREFLIGHT_OBJECTS="([^"]*)"', text, re.M | re.S)
    if not match:
        return []
    rows = []
    for line in match.group(1).splitlines():
        parts = line.split('|')
        if len(parts) >= 5 and parts[1] == 'service' and parts[0]:
            rows.append((parts[0], parts[1], parts[2], parts[3], parts[4]))
    return rows


def check_service_table(manifest: dict, text: str | None = None) -> list[str]:
    """Every service row must match the manifest not only by id but by its fields.

    Round-2 review: the check compared ids only, so changing the observed process
    (hev-socks5-tunnel -> WRONG-BINARY) stayed green. initd/pid/observe are the
    contract: the gate compares the product row fields with the manifest fields.
    """
    declared = {entry['id']: entry for entry in manifest.get('services') or []
                if entry.get('id') != 'svc.dynamic-name'}
    rows = product_service_rows(text)
    if not rows:
        return ['REINSTALL_PREFLIGHT_OBJECTS has no service rows']
    product = {row[0]: row for row in rows}
    problems = []
    for object_id in sorted(set(declared) - set(product)):
        problems.append('manifest service missing from product table: %s' % object_id)
    for object_id in sorted(set(product) - set(declared)):
        problems.append('product service row not in manifest services: %s' % object_id)
    for object_id in sorted(set(product) & set(declared)):
        entry = declared[object_id]
        _id, _cls, initd, observed, mode = product[object_id]
        want_initd = {part for part in (entry.get('initd') or '').split('|') if part}
        got_initd = {part for part in initd.split('/') if part}
        if got_initd != want_initd:
            problems.append('service %s initd drift: product=%r manifest=%r'
                            % (object_id, sorted(got_initd), sorted(want_initd)))
        observe = entry.get('observe') or ''
        if mode == 'port':
            if ':' + observed not in observe:
                problems.append('service %s port drift: product=%r manifest observe=%r'
                                % (object_id, observed, observe))
        elif mode in ('pidof', 'pgrep'):
            if not observe.startswith(mode):
                problems.append('service %s observe mode drift: product=%r manifest observe=%r'
                                % (object_id, mode, observe))
            elif (entry.get('pid') or '') != observed:
                problems.append('service %s pid drift: product=%r manifest=%r'
                                % (object_id, observed, entry.get('pid')))
        else:
            problems.append('service %s has unknown observe mode %r' % (object_id, mode))
    return problems


def product_uci_rows(text: str | None = None) -> list[tuple[str, str, str, str]]:
    text = text if text is not None else INSTALLER.read_text(encoding='utf-8')
    match = re.search(r'^REINSTALL_UCI_OBJECTS="([^"]*)"', text, re.M | re.S)
    if not match:
        return []
    rows = []
    for line in match.group(1).splitlines():
        parts = line.split('|')
        if len(parts) == 4 and parts[0]:
            rows.append((parts[0], parts[1], parts[2], parts[3]))
    return rows


def check_uci_table(manifest: dict, text: str | None = None) -> list[str]:
    """Every managed UCI package/object in the product table is declared, and the core
    manifest entries really have product rows (a dropped declaration must fail)."""
    rows = product_uci_rows(text)
    if not rows:
        return ['REINSTALL_UCI_OBJECTS missing or empty in geomask.sh.in']
    declared_pkgs = {entry.get('package') for entry in manifest.get('uci') or []}
    declared_ids = {entry.get('id') for entry in manifest.get('uci') or []}
    problems = []
    actual = {'|'.join(row) for row in rows}
    for pkg, _selector, _kind, _opts in rows:
        if pkg not in declared_pkgs:
            problems.append('product uci row for undeclared package: %s' % pkg)
    expected = set()
    for entry in manifest.get('uci') or []:
        for row in entry.get('product_rows') or []:
            expected.add(row)
    for row in sorted(expected - actual):
        problems.append('manifest uci row missing from product table: %s' % row)
    for row in sorted(actual - expected):
        problems.append('product uci row not declared by any manifest entry: %s' % row)
    for required, pkg in (('uci.network', 'network'), ('uci.dhcp', 'dhcp'),
                          ('uci.ksmbd', 'ksmbd')):
        if required not in declared_ids:
            problems.append('manifest uci entry missing: %s' % required)
        if not any(row[0] == pkg for row in rows):
            problems.append('manifest %s has no product REINSTALL_UCI_OBJECTS row' % required)
    return problems

# ---------------------------------------------------------------------------
# preflight-table cross-check (product side)


def preflight_table(text: str) -> list[str]:
    """Object ids declared in the product-side REINSTALL_PREFLIGHT_OBJECTS table."""
    match = re.search(r'^REINSTALL_PREFLIGHT_OBJECTS="([^"]*)"', text, re.M | re.S)
    if not match:
        return []
    return [line.split('|', 1)[0] for line in match.group(1).splitlines() if line.strip()]


BYTE_TABLE_RE = re.compile(r'^REINSTALL_PREFLIGHT_BYTE_FILES="([^"]*)"', re.M | re.S)


def byte_table(text: str) -> dict | None:
    """{path: expected-type} from the product-side byte-snapshot object table."""
    match = BYTE_TABLE_RE.search(text)
    if not match:
        return None
    rows = {}
    for line in match.group(1).splitlines():
        if line.strip():
            path, expected = line.split('|', 1)
            rows[path] = expected
    return rows


def check_byte_table(manifest: dict) -> list[str]:
    """Every byte-snapshot write-set object must be type-checked by the product preflight.

    Review G: the preflight classified only the 28 runtime objects, so a symlink where the
    snapshot stores bytes (cp -p would follow it) went unnoticed. The product byte table
    must list exactly the manifest objects whose snapshot stores bytes.
    """
    text = INSTALLER.read_text(encoding='utf-8')
    rows = byte_table(text)
    if rows is None:
        return ['REINSTALL_PREFLIGHT_BYTE_FILES missing from geomask.sh.in']
    problems = []
    expected = {}
    for entry in manifest.get('files') or []:
        if entry.get('snapshot') in ('SB', 'SBA', 'SAP'):
            expected[entry['path']] = ('dir-or-absent' if entry.get('type') == 'dir'
                                       else 'regular-or-absent')
    for path in sorted(set(expected) - set(rows)):
        problems.append('byte-snapshot object missing from product byte table: %s' % path)
    for path in sorted(set(rows) - set(expected)):
        problems.append('product byte-table row not a byte-snapshot manifest object: %s' % path)
    for path in sorted(set(rows) & set(expected)):
        if rows[path] != expected[path]:
            problems.append('byte-table expectation mismatch for %s: product=%s manifest=%s'
                            % (path, rows[path], expected[path]))
    return problems


def check_preflight_table(manifest: dict) -> list[str]:
    text = INSTALLER.read_text(encoding='utf-8')
    problems = []
    table = set(preflight_table(text))
    if not table:
        problems.append('REINSTALL_PREFLIGHT_OBJECTS missing from geomask.sh.in')
        return problems
    declared = {entry['id'] for entry in manifest.get('preflight_objects') or []}
    for object_id in sorted(declared - table):
        problems.append('manifest preflight object missing from product table: %s' % object_id)
    for object_id in sorted(table - declared):
        problems.append('product preflight object not in manifest: %s' % object_id)
    problems += check_byte_table(manifest)
    return problems


# ---------------------------------------------------------------------------
# CLI: inventory dump (used while authoring the manifest)


def main(argv):
    if argv and argv[0] == '--inventory':
        text = INSTALLER.read_text(encoding='utf-8')
        sites = extract_sites(text)
        payload = [{
            'kind': s.kind, 'effect': s.effect, 'path': s.path, 'function': s.function,
            'line': s.line_no, 'extra': s.extra, 'raw': s.raw.strip(),
        } for s in sites]
        json.dump(payload, sys.stdout, indent=1, ensure_ascii=False)
        print()
        return 0
    if argv and argv[0] == '--callees':
        manifest = load_manifest()
        for label, body in callee_texts(manifest).items():
            for path in sorted(path_literals(body)):
                print('%s\t%s' % (label, path))
        return 0
    manifest = load_manifest()
    problems = check(manifest)
    problems += check_preflight_table(manifest)
    problems += check_service_table(manifest)
    problems += check_uci_table(manifest)
    problems += check_legend(manifest)
    for problem in problems:
        print('FAIL ' + problem)
    print('gate: %d problem(s)' % len(problems))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
