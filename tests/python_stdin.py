"""Run stdin Python with shell curl stubs on Git Bash/Windows."""
import os
import subprocess
import sys

_run = subprocess.run


def run_with_shell_stubs(args, *positional, **kwargs):
    if isinstance(args, list) and args and args[0] == 'curl':
        args = [os.environ['GM_TEST_SHELL'], '-c', 'exec curl "$@"', 'curl', *args[1:]]
    return _run(args, *positional, **kwargs)


subprocess.run = run_with_shell_stubs
sys.argv = ['-', *sys.argv[1:]]
source = sys.stdin.read()
exec(compile(source, '<stdin>', 'exec'), {'__name__': '__main__'})
