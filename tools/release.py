#!/usr/bin/env python3
"""Publish and verify a geomask release, so GitHub never drifts from the router again.

The updater on the router reads `/repos/<repo>/releases/latest`, downloads
`releases/download/<tag>/geomask.sh`, and refuses the file unless `GM_VERSION` equals the
tag and the sha256 equals the release digest. So a release is only correct when all three
agree: the committed build, the asset and the tag.

    python tools/release.py status           # local version vs tag vs latest release
    python tools/release.py publish          # tests, tag, release, digest check
    python tools/release.py verify [--full]  # download the asset and prove it works
    python tools/release.py drift            # exit 1 when HEAD has no release (CI gate)

Every step prints what it checked; nothing here touches a router.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = 'kabzon93region/geomask'
INSTALLER = os.path.join(ROOT, 'geomask.sh')


def bash():
    """Git Bash on Windows is not on PATH; the workspace keeps it in Program Files."""
    candidates = [os.environ.get('GM_BASH'), r'C:\Program Files\Git\bin\bash.exe', 'bash']
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return 'bash'


def run(cmd, cwd=ROOT, timeout=900, check=False):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout)
    if check and result.returncode:
        raise SystemExit('%s failed:\n%s\n%s' % (' '.join(cmd), result.stdout, result.stderr))
    return result


def git(*args):
    return run(['git'] + list(args)).stdout.strip()


def local_version():
    with open(os.path.join(ROOT, 'geomask.sh.in'), encoding='utf-8') as handle:
        match = re.search(r'(?m)^GM_VERSION="([^"]+)"', handle.read())
    if not match:
        raise SystemExit('GM_VERSION not found in geomask.sh.in')
    return match.group(1)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return 'sha256:' + digest.hexdigest()


def tag_exists(tag):
    return run(['git', 'rev-parse', '--verify', '--quiet', 'refs/tags/' + tag]).returncode == 0


def remote_tag_exists(tag):
    out = run(['git', 'ls-remote', '--tags', 'origin', tag]).stdout.strip()
    return bool(out)


def latest_release():
    result = run(['gh', 'api', 'repos/%s/releases/latest' % REPO], timeout=120)
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def asset_digest(release, name='geomask.sh'):
    for asset in (release or {}).get('assets') or []:
        if asset.get('name') == name:
            return asset.get('digest') or ''
    return ''


def status():
    version = local_version()
    tag = 'v' + version
    release = latest_release()
    print('локальная версия : %s (тег %s)' % (version, tag))
    print('коммит           : %s %s' % (git('rev-parse', '--short', 'HEAD'),
                                        git('log', '-1', '--format=%s')))
    print('тег локально     : %s' % ('есть' if tag_exists(tag) else 'НЕТ'))
    print('тег на origin    : %s' % ('есть' if remote_tag_exists(tag) else 'НЕТ'))
    if not release:
        print('последний релиз  : нет данных (gh api не ответил)')
        return 0
    latest = release.get('tag_name', '')
    print('последний релиз  : %s (%s)' % (latest, release.get('published_at', '')))
    digest = asset_digest(release)
    local = sha256_of(INSTALLER)
    if digest:
        print('digest ассета    : %s%s' % (digest[:23], '…' if len(digest) > 23 else ''))
        print('digest локальной : %s%s%s' % (local[:23], '…' if len(local) > 23 else '',
                                            '  совпадает' if digest == local else '  ОТЛИЧАЕТСЯ'))
    else:
        print('digest ассета    : нет (у ассета не посчитан)')
    if latest == tag and digest == local:
        print('вывод            : GitHub на текущей версии')
        return 0
    print('вывод            : GitHub отстаёт — нужен релиз %s' % tag)
    return 1


def check_ready(version):
    problems = []
    if git('status', '--porcelain'):
        problems.append('в рабочем дереве есть незакоммиченные изменения')
    if git('rev-parse', '--abbrev-ref', 'HEAD') != 'master':
        problems.append('текущая ветка не master')
    if git('rev-parse', 'HEAD') != git('rev-parse', 'origin/master'):
        problems.append('master и origin/master разошлись (сначала push)')
    if run([sys.executable, 'tools/check_build.py']).returncode:
        problems.append('geomask.sh не совпадает со сборкой (build.py)')
    tag = 'v' + version
    if tag_exists(tag) or remote_tag_exists(tag):
        problems.append('тег %s уже существует' % tag)
    return problems


def run_tests():
    print('  проверки перед публикацией (это займёт пару минут)')
    steps = [
        ('сборка == исходники', [sys.executable, 'tools/check_build.py']),
        ('RPC-манифест', [sys.executable, 'tools/gen-geo-rpc.py', '--check']),
        ('python-тесты', [sys.executable, '-B', '-m', 'unittest', 'discover',
                          '-s', 'tests', '-p', 'test_*.py']),
        ('песочница', [bash(), 'tests/run_tests.sh']),
    ]
    failed = []
    for name, cmd in steps:
        result = run(cmd, timeout=1800)
        print('    %-22s %s' % (name, 'ok' if result.returncode == 0 else 'FAIL'))
        if result.returncode:
            failed.append((name, (result.stdout or '')[-1500:] + (result.stderr or '')[-800:]))
    for name, tail in failed:
        print('\n--- %s ---\n%s' % (name, tail))
    return not failed


def release_notes(version, previous_tag):
    lines = ['## geomask %s' % version, '']
    log = git('log', '--no-merges', '--format=- %s', '%s..HEAD' % previous_tag)
    commits = [line for line in log.splitlines() if line.strip()]
    if len(commits) > 40:
        lines.append('Изменений с %s: %d (последние 40)' % (previous_tag, len(commits)))
        lines.append('')
        commits = commits[-40:]
    if commits:
        lines.extend(commits)
        lines.append('')
    lines.append('Артефакт: `geomask.sh` (sha256 в digest ассета). Проверки перед публикацией: '
                 'check_build, RPC-манифест, python-тесты, песочница.')
    return '\n'.join(lines) + '\n'


def publish(skip_tests=False):
    version = local_version()
    tag = 'v' + version
    problems = check_ready(version)
    if problems:
        print('публикация остановлена:')
        for item in problems:
            print('  - ' + item)
        return 1
    if not skip_tests and not run_tests():
        print('публикация остановлена: тесты не прошли')
        return 1

    previous = git('describe', '--tags', '--abbrev=0')
    print('  тег %s на коммите %s' % (tag, git('rev-parse', '--short', 'HEAD')))
    run(['git', 'tag', '-a', tag, '-m', 'geomask ' + version], check=True)
    run(['git', 'push', 'origin', tag], check=True)

    notes = os.path.join(tempfile.mkdtemp(), 'notes.md')
    with open(notes, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(release_notes(version, previous))
    title = 'geomask %s' % version
    result = run(['gh', 'release', 'create', tag, INSTALLER, '--title', title,
                  '--notes-file', notes, '--verify-tag'], timeout=600)
    if result.returncode:
        print('gh release create failed:\n%s\n%s' % (result.stdout, result.stderr))
        return 1
    print('  релиз создан: %s' % result.stdout.strip().splitlines()[-1])

    release = latest_release()
    digest = asset_digest(release)
    local = sha256_of(INSTALLER)
    if release.get('tag_name') != tag:
        print('ВНИМАНИЕ: latest указывает на %r, а не на %r' % (release.get('tag_name'), tag))
        return 1
    if digest and digest != local:
        print('ВНИМАНИЕ: digest ассета %s != локальному %s' % (digest, local))
        return 1
    print('  digest ассета совпадает с локальной сборкой: %s' % local[:23])
    return 0


def verify(full=False):
    version = local_version()
    tag = 'v' + version
    release = latest_release()
    if not release:
        print('gh api не ответил')
        return 1
    if release.get('tag_name') != tag:
        print('latest = %s, ожидался %s' % (release.get('tag_name'), tag))
        return 1
    url = 'https://github.com/%s/releases/download/%s/geomask.sh' % (REPO, tag)
    print('скачиваю %s' % url)
    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, 'geomask.sh')
    request = urllib.request.Request(url, headers={'User-Agent': 'geomask-release-check'})
    with urllib.request.urlopen(request, timeout=120) as response, open(target, 'wb') as out:
        out.write(response.read())
    size = os.path.getsize(target)
    digest = sha256_of(target)
    local = sha256_of(INSTALLER)
    print('  размер %d байт, sha256 %s' % (size, digest[:23]))
    ok = True
    if digest != local:
        print('  ОШИБКА: ассет не совпадает с локальной сборкой')
        ok = False
    with open(target, encoding='utf-8', errors='replace') as handle:
        head = handle.read(4000)
    found = re.search(r'(?m)^GM_VERSION="([^"]+)"', head)
    if not found or found.group(1) != version:
        print('  ОШИБКА: GM_VERSION=%r, ожидался %r'
              % (found.group(1) if found else None, version))
        ok = False
    syntax = run([bash(), '-n', target])
    print('  sh -n                %s' % ('ok' if syntax.returncode == 0 else 'FAIL'))
    ok = ok and syntax.returncode == 0
    listing = run([bash(), target, '--list-modules'], timeout=120)
    modules = len([line for line in (listing.stdout or '').splitlines() if line.strip()])
    print('  --list-modules       %s (%d строк)' % ('ok' if listing.returncode == 0 else 'FAIL',
                                                    modules))
    ok = ok and listing.returncode == 0
    if full:
        print('  полная песочница на скачанном файле (416 проверок)')
        env = dict(os.environ, GM_SCRIPT=target)
        suite = subprocess.run([bash(), 'tests/run_tests.sh'], cwd=ROOT, env=env,
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=1800)
        tail = [line for line in (suite.stdout or '').splitlines() if 'SANDBOX RESULT' in line]
        print('    %s' % (tail[-1] if tail else (suite.stdout or '')[-300:]))
        ok = ok and suite.returncode == 0
    print('вывод: %s' % ('релиз %s устанавливается и совпадает со сборкой' % tag if ok
                         else 'релиз НЕ прошёл проверку'))
    return 0 if ok else 1


def drift():
    version = local_version()
    release = latest_release()
    if not release or release.get('tag_name') != 'v' + version:
        print('дрейф: HEAD %s, последний релиз %s'
              % (version, (release or {}).get('tag_name', 'нет')))
        return 1
    print('ok: HEAD %s = последний релиз' % version)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='release')
    parser.add_argument('command', choices=['status', 'publish', 'verify', 'drift'])
    parser.add_argument('--skip-tests', action='store_true')
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'status':
        return status()
    if args.command == 'publish':
        return publish(skip_tests=args.skip_tests)
    if args.command == 'verify':
        return verify(full=args.full)
    return drift()


if __name__ == '__main__':
    sys.exit(main())
