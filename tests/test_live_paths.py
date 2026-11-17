"""Static live-path checks for the shell entry points (R06 M3).

The installer and the shipped helpers run as root on a router. Two properties are checked
here, in the sources only:

  * no fixed `/tmp/geomask-*.nft` path is opened, written or removed: a predictable name in a
    world-writable directory can be planted by a local user, so a temporary must come from
    `mktemp` (or a variable holding that result);
  * the installer does not write an absolute `/etc/...` path through a raw shell redirection
    or a raw file-writing command. Test paths go through `P()`/`$(P ...)` (or a variable that
    already carries a `P()` result); the known wrappers `save_stream` and `ruleset_stub`
    prefix their argument with `P()` internally, and their implementation is asserted here.

This is a static check, not containment. It cannot see a runtime symlink planted between
`lstat` and `open`, a namespace, `strace`, mount/chroot isolation, or a live `/tmp` path that
is built from a variable at runtime. A Linux namespace/strace job for those is pending; see
tests/BOUNDARIES.md. The check is deliberately conservative: comments and heredoc bodies are
not code and are skipped.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'geomask.sh.in'
SHELL_SOURCES = [INSTALLER] + sorted((ROOT / 'assets').glob('*.sh'))

FIXED_NFT = re.compile(r'/tmp/geomask-[A-Za-z0-9._-]*\.nft')
P_SAFE = re.compile(r'\$\(P\s+[^)]*\)')
PREFIX_SAFE = re.compile(r'\$\{GM_PREFIX\}[^\s;"\']*')
DIRECT_ETC = [
    # `> /etc/...` and `>> /etc/...` after a command separator or a shell keyword.
    re.compile(r'(?:^|[;&|]|\b(?:then|do|else)\s+)\s*>>?\s*["\']?/etc(?:/|\b)'),
    # A raw file-writing command in command position with a literal /etc target: a
    # `touch /etc/...` inside an info/printf message is text, not a write.
    re.compile(r'(?:^|[;&|]|\b(?:then|do|else|if|elif|while|until)\s+)\s*!?\s*'
               r'(?:touch|mkdir|rm|cp|mv|chmod|chown|ln|tee|install|sed\s+-i)\b'
               r'[^;\n]*?\s["\']?/etc(?:/|\b)'),
]


def code_lines(text):
    """Yield (line number, line) for shell code, skipping comments and heredoc bodies."""
    heredoc = None
    for number, line in enumerate(text.splitlines(), 1):
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        opener = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if opener is not None:
            heredoc = opener.group(1)
        yield number, line


def without_prefixed_paths(line):
    return PREFIX_SAFE.sub('PREFIX', P_SAFE.sub('PREFIX', line))


class LivePathTests(unittest.TestCase):
    def test_no_fixed_geomask_nft_path_in_shell_sources(self):
        problems = []
        for path in SHELL_SOURCES:
            text = path.read_text(encoding='utf-8')
            for number, line in code_lines(text):
                if 'mktemp' in line:
                    continue
                for match in FIXED_NFT.finditer(line):
                    problems.append('%s:%d: fixed %s (use mktemp)'
                                    % (path.name, number, match.group(0)))
        self.assertEqual([], problems, '\n'.join(problems))

    def test_installer_does_not_write_etc_behind_p(self):
        problems = []
        for number, line in code_lines(INSTALLER.read_text(encoding='utf-8')):
            checked = without_prefixed_paths(line)
            for pattern in DIRECT_ETC:
                match = pattern.search(checked)
                if match is not None:
                    problems.append('%s:%d: %s' % (INSTALLER.name, number,
                                                   match.group(0).strip()))
        self.assertEqual([], problems, '\n'.join(problems))

    def test_the_whitelisted_etc_wrappers_prefix_with_p(self):
        text = INSTALLER.read_text(encoding='utf-8')
        self.assertIn('_t="$(P "$1")"', text,
                      'save_stream no longer prefixes its target with P()')
        self.assertIn('[ -s "$(P "$2")" ]', text,
                      'ruleset_stub no longer prefixes its target with P()')
        self.assertIn('> "$(P "$2")"', text,
                      'ruleset_stub writes its target without P()')


if __name__ == '__main__':
    unittest.main()
