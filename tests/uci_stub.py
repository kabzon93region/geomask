#!/usr/bin/env python3
"""A faithful uci(1) stand-in for the geomask-dnsguard tests.

The old test runner implemented ``uci`` as a shell function whose ``set`` handler
split the value into one list line per word.  Real libuci does the opposite: an
``option`` and a ``list`` are different object types, and writing a list with
``uci set`` replaces it with one ``UCI_TYPE_STRING`` value.  That fake is why
the dnsguard bug (dnsmasq reads ``server`` with ``config_list_foreach`` and
therefore needs ``server_LENGTH``/``server_ITEMn``) survived its tests.  This
module implements the real semantics instead.

Behaviour and the libuci functions behind it
--------------------------------------------
* File syntax (``config``/``option``/``list``, quoted and unquoted values,
  comments, named and anonymous sections): ``uci_parse_line()``,
  ``uci_parse_option()``, ``uci_parse_section()``, ``uci_parse_string()``.
  Concatenated chunks such as ``'a'\\''b'`` are one token, as in libuci.
* ``pkg.@type[N]`` / ``pkg.name`` addressing: ``uci_lookup_ptr()``.  The index
  counts every section of that type in file order, named or anonymous.
* ``set``: ``uci_set()`` allocates a string (``uci_alloc_option()`` with
  ``UCI_TYPE_STRING``); on an existing list it replaces the list, it does not
  append.  Setting ``pkg.sec=type`` creates or retypes a section.
* ``add_list``: ``uci_add_list()`` appends; if the option is currently a string
  the string becomes the first item of the new list.
* ``delete``: ``uci_delete()`` / ``uci_del_option()``.  The CLI resolves the
  target through ``uci_lookup_ptr()`` first, so deleting a missing option (or a
  missing section) exits 1; deleting a missing *item* is not a thing in uci.
  ``uci -q delete`` only suppresses the message, not the status.
* ``get``: ``uci_get()``.  A list is printed on one line with the delimiter
  (``-d``, default a single space); a string is printed as-is; a missing
  option exits 1.
* ``show``: ``uci_show()``.  A string is ``pkg.sec.opt='v'``; a list is
  ``pkg.sec.opt='v1' 'v2'``; a section is ``pkg.sec=type``.
* ``export`` / on-disk format: ``uci_export()`` and ``uci_quote()``.
  ``option name 'v'`` for a string, one ``list name 'v'`` per item for a list.
* ``commit``: ``uci_commit()`` -> ``uci_file_commit()``: the file is written to
  a temporary file in the config directory and renamed over the old one.
  A package without a pending delta is left alone.
* Deltas (``-c`` config dir, ``-t`` delta dir): ``uci_save()`` /
  ``uci_load_delta()`` / ``uci_revert()``.  Mutations live in the delta
  directory until ``commit``; a new invocation with the same ``-t`` sees them.
  Like libuci, the pending file is ``<delta dir>/<package>`` (the reason
  ``/tmp/.uci/network`` exists), so a candidate copy MUST use its own delta
  directory: passing the same directory to ``-c`` and ``-t`` lets the delta
  clobber the config copy before ``commit``.

Deliberate deviations, all documented in the tests
--------------------------------------------------
* The stub never falls back to ``/etc/config``.  ``-c`` (or ``UCI_CONFIG_DIR``)
  is mandatory and paths under ``/etc`` are refused; the tests run against a
  temporary directory.  ``-t`` (or ``UCI_DELTA_DIR``) is mandatory for
  mutations so a candidate and the live config cannot share deltas by accident.
* There is no ``flock`` (``fcntl`` is unavailable under the Windows test
  interpreter); the atomic rename still gives all-or-nothing visibility.
* Failure injection (test-only, never used by the asset):
  ``UCI_FAIL_COMMIT=pkg[:n]`` fails the n-th commit of that package,
  ``UCI_FAIL_ADDLIST=value[:n]`` fails the n-th ``add_list`` of that value,
  ``UCI_FAIL_SET=key[:n]`` fails the n-th ``set`` of that key,
  ``UCI_FAIL_DELETE=key[:n]`` fails the n-th ``delete`` of that key, and
  ``UCI_LIE_COMMIT=pkg[:n]`` returns success from the n-th commit of that
  package without writing or removing the delta.
  A bare value/key (no ``:n``) fails the first matching call. Counters are kept
  in ``UCI_STUB_STATE`` when set (shared across candidate and live delta dirs,
  so ``pkg:2`` can mean "the live call"), otherwise in
  ``<delta dir>/.uci-stub-counters``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

DEFAULT_DELIMITER = ' '


class UciError(Exception):
    """An error that makes uci exit 1 (the message is printed unless -q)."""


# --------------------------------------------------------------------------- paths

_DRIVE = re.compile(r'^/([A-Za-z])(?:/(.*))?$')
_CYGDRIVE = re.compile(r'^/cygdrive/([A-Za-z])(?:/(.*))?$')
_WINDRIVE = re.compile(r'^[A-Za-z]:[\\/]')


def native_path(raw):
    """Turn a Git-Bash path (``/c/...``, ``/y/...``, ``/cygdrive/...``) into a
    Windows path.  Drive-letter Windows paths are accepted as-is (forward
    slashes); when ``cygpath`` is reachable, other POSIX paths are translated
    with it, otherwise they are returned unchanged."""
    if not raw:
        return raw
    if _WINDRIVE.match(raw):
        return raw.replace('\\', '/')
    match = _CYGDRIVE.match(raw) or _DRIVE.match(raw)
    if match:
        rest = match.group(2) or ''
        return '%s:/%s' % (match.group(1).upper(), rest)
    if raw.startswith('/'):
        cygpath = shutil.which('cygpath')
        if cygpath:
            try:
                result = subprocess.run([cygpath, '-w', raw], capture_output=True,
                                        text=True, timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except OSError:
                pass
    return raw


def _reject_etc(raw):
    text = raw.replace('\\', '/').rstrip('/').lower()
    if text == '/etc' or text == '/etc/config' or text.startswith('/etc/'):
        raise UciError('refusing to touch /etc/config; pass an explicit -c/-t directory')


def config_dir(opts):
    raw = opts['config'] or os.environ.get('UCI_CONFIG_DIR')
    if not raw:
        raise UciError('no config directory: pass -c DIR (the stub never uses /etc/config)')
    _reject_etc(raw)
    return native_path(raw)


def delta_dir(opts, required=False):
    raw = opts['delta'] or os.environ.get('UCI_DELTA_DIR')
    if not raw:
        if required:
            raise UciError('no delta directory: pass -t DIR so changes stay private')
        return None
    _reject_etc(raw)
    return native_path(raw)


def delta_path(directory, package):
    # libuci: <savedir>/<package>, e.g. /tmp/.uci/network
    return os.path.join(directory, package)


# --------------------------------------------------------------------------- model

class Option:
    def __init__(self, kind, values):
        self.kind = kind              # 'string' or 'list'
        self.values = list(values)


class Section:
    def __init__(self, stype, name=None):
        self.stype = stype
        self.name = name
        self.options = {}             # insertion ordered


class Package:
    def __init__(self, name):
        self.name = name
        self.sections = []


def add_value(section, name, value, as_list):
    """uci_set() for ``option``, uci_add_list() for ``list``."""
    if not as_list:
        section.options[name] = Option('string', [value])
        return
    option = section.options.get(name)
    if option is None:
        section.options[name] = Option('list', [value])
    elif option.kind == 'string':
        option.kind = 'list'
        option.values.append(value)
    else:
        option.values.append(value)


# --------------------------------------------------------------------------- parser

def read_value(text, pos):
    """Read one uci value starting at *pos*: quoted chunks, bare chunks and
    backslash escapes concatenate into one token (uci_parse_string)."""
    out = []
    length = len(text)
    while pos < length:
        char = text[pos]
        if char in ' \t\r' or char == '#':
            break
        if char in ("'", '"'):
            quote = char
            pos += 1
            while pos < length:
                inner = text[pos]
                if inner == '\\' and pos + 1 < length:
                    out.append(text[pos + 1])
                    pos += 2
                    continue
                if inner == quote:
                    pos += 1
                    break
                out.append(inner)
                pos += 1
            else:
                raise UciError('unterminated quote')
        elif char == '\\' and pos + 1 < length:
            out.append(text[pos + 1])
            pos += 2
        else:
            out.append(char)
            pos += 1
    return ''.join(out), pos


def tokenize(line):
    tokens = []
    pos = 0
    length = len(line)
    while pos < length:
        while pos < length and line[pos] in ' \t\r':
            pos += 1
        if pos >= length or line[pos] == '#':
            break
        value, pos = read_value(line, pos)
        tokens.append(value)
    return tokens


def parse_package(name, text):
    package = Package(name)
    section = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        tokens = tokenize(raw)
        if not tokens:
            continue
        command = tokens[0]
        if command == 'package':
            continue
        if command == 'config':
            if len(tokens) not in (2, 3):
                raise UciError('parse error at line %d' % lineno)
            section = Section(tokens[1], tokens[2] if len(tokens) == 3 else None)
            package.sections.append(section)
        elif command in ('option', 'list'):
            if section is None or len(tokens) != 3:
                raise UciError('parse error at line %d' % lineno)
            add_value(section, tokens[1], tokens[2], command == 'list')
        else:
            raise UciError('parse error at line %d' % lineno)
    return package


def read_text(path):
    try:
        with open(path, encoding='utf-8', newline=None) as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UciError(str(error))


def load_package(confdir, savedir, name):
    base = read_text(os.path.join(confdir, name))
    package = parse_package(name, base) if base is not None else Package(name)
    if savedir:
        pending = read_text(delta_path(savedir, name))
        if pending is not None:
            package = parse_package(name, pending)
    return package


def package_exists(confdir, savedir, name):
    if os.path.isfile(os.path.join(confdir, name)):
        return True
    return bool(savedir and os.path.isfile(delta_path(savedir, name)))


# --------------------------------------------------------------------------- output

def quote(value):
    """uci_quote(): single quotes, a literal quote becomes the four-char
    sequence '\\'' that the parser turns back into one quote."""
    return "'" + value.replace("'", "'\\''") + "'"


def render_sections(package):
    blocks = []
    for section in package.sections:
        head = 'config %s' % section.stype
        if section.name is not None:
            head += ' ' + quote(section.name)
        lines = [head]
        for name, option in section.options.items():
            if option.kind == 'string':
                lines.append('\toption %s %s' % (name, quote(option.values[0])))
            else:
                for value in option.values:
                    lines.append('\tlist %s %s' % (name, quote(value)))
        blocks.append('\n'.join(lines))
    if not blocks:
        return ''
    return '\n\n'.join(blocks) + '\n'


def render_export(package):
    return 'package %s\n\n%s' % (package.name, render_sections(package))


def write_atomic(path, text, mode=None):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix='.uci-tmp-', dir=directory)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        if mode is None:
            mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o644
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            try:
                os.unlink(temp)
            except OSError:
                pass


# --------------------------------------------------------------------------- lookup

def find_section(package, token):
    if token.startswith('@'):
        match = re.match(r'^@([A-Za-z0-9_]+)\[(\d+)\]$', token)
        if not match:
            return None
        sections = [item for item in package.sections if item.stype == match.group(1)]
        index = int(match.group(2))
        return sections[index] if index < len(sections) else None
    for section in package.sections:
        if section.name == token:
            return section
    return None


def split_key(key):
    """Split ``pkg.section.option`` (package names and section tokens may not
    contain a dot; option names may)."""
    if '.' not in key:
        return key, None
    package, rest = key.split('.', 1)
    if '.' in rest:
        section, option = rest.split('.', 1)
        return package, (section, option)
    return package, (rest, None)


def lookup(package, section_token, option_name):
    section = find_section(package, section_token)
    if section is None:
        return None, None
    if option_name is None:
        return section, None
    return section, section.options.get(option_name)


# --------------------------------------------------------------------------- failure injection

def parse_commit_spec(spec):
    base, separator, tail = spec.rpartition(':')
    if separator and base and tail.isdigit():
        return base, int(tail)
    return spec, 1


def parse_indexed_spec(spec):
    """``value`` / ``key`` fails the first matching call; ``value:2`` the second.

    The suffix is only treated as a counter when it is a bare number, so an IPv6 address
    without a numeric tail stays one value."""
    base, separator, tail = spec.rpartition(':')
    if separator and base and tail.isdigit():
        return base, int(tail)
    return spec, 1


def check_indexed(kind, value, counter_key, confdir, savedir):
    spec = os.environ.get(kind)
    if not spec:
        return False
    target, number = parse_indexed_spec(spec)
    if target != value:
        return False
    count = bump_counter(counter_path(confdir, savedir), counter_key)
    return count == number


def counter_path(sconfdir, savedir):
    explicit = os.environ.get('UCI_STUB_STATE')
    if explicit:
        return native_path(explicit)
    return os.path.join(savedir or sconfdir, '.uci-stub-counters')


def bump_counter(path, key):
    data = {}
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                name, separator, value = line.rstrip('\n').partition('\t')
                if separator and name and value.isdigit():
                    data[name] = int(value)
    except FileNotFoundError:
        pass
    data[key] = data.get(key, 0) + 1
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    write_atomic(path, ''.join('%s\t%d\n' % item for item in sorted(data.items())),
                 mode=0o600)
    return data[key]


def check_commit_failure(package, confdir, savedir):
    spec = os.environ.get('UCI_FAIL_COMMIT')
    if not spec:
        return
    target, number = parse_commit_spec(spec)
    if target != package:
        return
    count = bump_counter(counter_path(confdir, savedir), 'commit:' + package)
    if count == number:
        raise UciError('commit %s failed (injected)' % package)


def check_addlist_failure(value, confdir, savedir):
    if check_indexed('UCI_FAIL_ADDLIST', value, 'addlist:' + value, confdir, savedir):
        raise UciError('add_list %s failed (injected)' % value)


def check_set_failure(key, confdir, savedir):
    if check_indexed('UCI_FAIL_SET', key, 'set:' + key, confdir, savedir):
        raise UciError('set %s failed (injected)' % key)


def check_delete_failure(key, confdir, savedir):
    if check_indexed('UCI_FAIL_DELETE', key, 'delete:' + key, confdir, savedir):
        raise UciError('delete %s failed (injected)' % key)


def check_lie_commit(package, confdir, savedir):
    """UCI_LIE_COMMIT=pkg[:n] returns success without writing anything."""
    spec = os.environ.get('UCI_LIE_COMMIT')
    if not spec:
        return False
    target, number = parse_commit_spec(spec)
    if target != package:
        return False
    count = bump_counter(counter_path(confdir, savedir), 'liecommit:' + package)
    return count == number


# --------------------------------------------------------------------------- commands

def save_delta(savedir, package):
    if savedir is None:
        raise UciError('no delta directory: pass -t DIR so changes stay private')
    write_atomic(delta_path(savedir, package.name), render_sections(package), mode=0o600)


def cmd_get(opts, args):
    if len(args) != 1:
        raise UciError('uci get: invalid argument')
    confdir = config_dir(opts)
    savedir = delta_dir(opts)
    package_name, rest = split_key(args[0])
    if rest is None:
        raise UciError('Entry not found')
    if not package_exists(confdir, savedir, package_name):
        raise UciError('Entry not found')
    package = load_package(confdir, savedir, package_name)
    section, option = lookup(package, rest[0], rest[1])
    if section is None:
        raise UciError('Entry not found')
    if rest[1] is None:
        sys.stdout.write(section.stype + '\n')
        return
    if option is None:
        raise UciError('Entry not found')
    if option.kind == 'string':
        sys.stdout.write(option.values[0] + '\n')
    else:
        sys.stdout.write(opts['delimiter'].join(option.values) + '\n')


def cmd_set(opts, args):
    if len(args) != 1 or '=' not in args[0]:
        raise UciError('uci set: invalid argument')
    key, value = args[0].split('=', 1)
    confdir = config_dir(opts)
    savedir = delta_dir(opts, required=True)
    check_set_failure(key, confdir, savedir)
    package_name, rest = split_key(key)
    if rest is None:
        raise UciError('uci set: invalid argument')
    package = load_package(confdir, savedir, package_name)
    section, option = lookup(package, rest[0], rest[1])
    if rest[1] is None:
        if rest[0].startswith('@'):
            raise UciError('uci set: cannot name an anonymous section')
        if section is None:
            package.sections.append(Section(value, rest[0]))
        else:
            section.stype = value
    else:
        if section is None:
            raise UciError('Entry not found')
        if option is None:
            section.options[rest[1]] = Option('string', [value])
        else:
            option.kind = 'string'
            option.values = [value]
    save_delta(savedir, package)


def cmd_add_list(opts, args):
    if len(args) != 1 or '=' not in args[0]:
        raise UciError('uci add_list: invalid argument')
    key, value = args[0].split('=', 1)
    confdir = config_dir(opts)
    savedir = delta_dir(opts, required=True)
    check_addlist_failure(value, confdir, savedir)
    package_name, rest = split_key(key)
    if rest is None or rest[1] is None:
        raise UciError('uci add_list: invalid argument')
    package = load_package(confdir, savedir, package_name)
    section, option = lookup(package, rest[0], rest[1])
    if section is None:
        raise UciError('Entry not found')
    if option is None:
        section.options[rest[1]] = Option('list', [value])
    elif option.kind == 'string':
        option.kind = 'list'
        option.values.append(value)
    else:
        option.values.append(value)
    save_delta(savedir, package)


def cmd_delete(opts, args):
    if len(args) != 1:
        raise UciError('uci delete: invalid argument')
    confdir = config_dir(opts)
    savedir = delta_dir(opts, required=True)
    check_delete_failure(args[0], confdir, savedir)
    package_name, rest = split_key(args[0])
    if rest is None:
        raise UciError('uci delete: invalid argument')
    package = load_package(confdir, savedir, package_name)
    section, option = lookup(package, rest[0], rest[1])
    if section is None:
        raise UciError('Entry not found')
    if rest[1] is None:
        package.sections.remove(section)
    else:
        if option is None:
            raise UciError('Entry not found')
        del section.options[rest[1]]
    save_delta(savedir, package)


def _package_names(confdir, savedir):
    names = []
    try:
        for entry in sorted(os.listdir(confdir)):
            if entry.startswith('.') or not os.path.isfile(os.path.join(confdir, entry)):
                continue
            names.append(entry)
    except FileNotFoundError:
        pass
    if savedir:
        try:
            for entry in sorted(os.listdir(savedir)):
                if _is_delta_name(entry) and entry not in names:
                    names.append(entry)
        except FileNotFoundError:
            pass
    return names


def _is_delta_name(entry):
    """A pending delta is a package file in the savedir; skip temp/hidden names
    (libuci's savedir contains only package files, but our atomic temp files use
    a leading dot)."""
    return not entry.startswith('.') and '.' not in entry


def _show_lines(package):
    counts = {}
    for section in package.sections:
        index = counts.get(section.stype, 0)
        counts[section.stype] = index + 1
        reference = section.name if section.name is not None else '@%s[%d]' % (section.stype, index)
        base = '%s.%s' % (package.name, reference)
        yield base + '=' + section.stype
        for name, option in section.options.items():
            values = ' '.join(quote(value) for value in option.values)
            yield '%s.%s=%s' % (base, name, values)


def cmd_show(opts, args):
    if len(args) > 1:
        raise UciError('uci show: invalid argument')
    confdir = config_dir(opts)
    savedir = delta_dir(opts)
    target = args[0] if args else None
    if target is not None and '.' in target:
        package_name = target.split('.', 1)[0]
        wanted = target
    else:
        package_name = target
        wanted = target
    names = [package_name] if package_name else _package_names(confdir, savedir)
    for name in names:
        if package_name and not package_exists(confdir, savedir, name):
            raise UciError('Entry not found')
        package = load_package(confdir, savedir, name)
        for line in _show_lines(package):
            if wanted is None or line == wanted or line.startswith(wanted + '.') \
                    or line.startswith(wanted + '='):
                sys.stdout.write(line + '\n')


def cmd_export(opts, args):
    if len(args) > 1:
        raise UciError('uci export: invalid argument')
    confdir = config_dir(opts)
    savedir = delta_dir(opts)
    names = [args[0]] if args else _package_names(confdir, savedir)
    for index, name in enumerate(names):
        if args and not package_exists(confdir, savedir, name):
            raise UciError('Entry not found')
        if index:
            sys.stdout.write('\n')
        sys.stdout.write(render_export(load_package(confdir, savedir, name)))


def _pending_packages(savedir):
    if not savedir:
        return []
    try:
        return sorted(entry for entry in os.listdir(savedir)
                      if _is_delta_name(entry)
                      and os.path.isfile(os.path.join(savedir, entry)))
    except FileNotFoundError:
        return []


def cmd_commit(opts, args):
    if len(args) > 1:
        raise UciError('uci commit: invalid argument')
    confdir = config_dir(opts)
    savedir = delta_dir(opts)
    names = args or _pending_packages(savedir)
    for name in names:
        check_commit_failure(name, confdir, savedir)
        if check_lie_commit(name, confdir, savedir):
            continue
        if not savedir:
            continue
        pending = delta_path(savedir, name)
        text = read_text(pending)
        if text is None:
            continue
        package = parse_package(name, text)
        write_atomic(os.path.join(confdir, name), render_sections(package))
        try:
            os.unlink(pending)
        except OSError:
            pass


def cmd_revert(opts, args):
    if len(args) > 1:
        raise UciError('uci revert: invalid argument')
    savedir = delta_dir(opts)
    names = args or _pending_packages(savedir)
    for name in names:
        if not savedir:
            continue
        try:
            os.unlink(delta_path(savedir, name))
        except FileNotFoundError:
            pass


COMMANDS = {
    'get': cmd_get,
    'set': cmd_set,
    'add_list': cmd_add_list,
    'delete': cmd_delete,
    'show': cmd_show,
    'export': cmd_export,
    'commit': cmd_commit,
    'revert': cmd_revert,
}


# --------------------------------------------------------------------------- cli

def parse_args(argv):
    opts = {'quiet': False, 'config': None, 'delta': None,
            'delimiter': DEFAULT_DELIMITER}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '-q':
            opts['quiet'] = True
            index += 1
        elif arg == '-c':
            if index + 1 >= len(argv):
                raise UciError('option -c requires an argument')
            opts['config'] = argv[index + 1]
            index += 2
        elif arg.startswith('-c') and len(arg) > 2:
            opts['config'] = arg[2:]
            index += 1
        elif arg == '-t':
            if index + 1 >= len(argv):
                raise UciError('option -t requires an argument')
            opts['delta'] = argv[index + 1]
            index += 2
        elif arg.startswith('-t') and len(arg) > 2:
            opts['delta'] = arg[2:]
            index += 1
        elif arg == '-d':
            if index + 1 >= len(argv):
                raise UciError('option -d requires an argument')
            opts['delimiter'] = argv[index + 1]
            index += 2
        elif arg.startswith('-d') and len(arg) > 2:
            opts['delimiter'] = arg[2:]
            index += 1
        elif arg == '--':
            index += 1
            break
        elif arg.startswith('-'):
            raise UciError('unknown option %s' % arg)
        else:
            break
    return opts, argv[index:]


def main(argv):
    quiet = '-q' in argv
    try:
        opts, args = parse_args(argv)
        quiet = opts['quiet']
        if not args:
            raise UciError('usage: uci [options] <command> [arguments]')
        command = args[0]
        handler = COMMANDS.get(command)
        if handler is None:
            raise UciError('unknown command %s' % command)
        handler(opts, args[1:])
        return 0
    except UciError as error:
        if not quiet:
            sys.stderr.write('uci: %s\n' % error)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
