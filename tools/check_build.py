# -*- coding: utf-8 -*-
# Verify committed geomask.sh matches a fresh build of geomask.sh.in + assets (TD-05).
# Rebuilds in place (same as the manual "python build.py && git diff" workflow)
# and exits 1 when the committed artifact differs from the build.
import hashlib
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COMMITTED = os.path.join(ROOT, "geomask.sh")
BUILD = os.path.join(ROOT, "build.py")


def main():
    # --verify-only builds into a scratch file. Without it this checker rewrote the very
    # artifact it was asked to verify, so a stale build looked "in sync" after the run.
    verify_only = '--verify-only' in sys.argv[1:]
    if not os.path.exists(COMMITTED):
        print("FAIL: geomask.sh not found - run: python build.py")
        return 1
    with open(COMMITTED, "rb") as f:
        before = f.read()
    if verify_only:
        fd, scratch = tempfile.mkstemp(prefix='geomask-build-', suffix='.sh')
        os.close(fd)
        try:
            rc = subprocess.call([sys.executable, BUILD, '--out', scratch], cwd=ROOT)
            if rc != 0:
                print("FAIL: build.py exited %d" % rc)
                return 1
            with open(scratch, 'rb') as f:
                after = f.read()
        finally:
            try:
                os.unlink(scratch)
            except OSError:
                pass
    else:
        rc = subprocess.call([sys.executable, BUILD], cwd=ROOT)
        if rc != 0:
            print("FAIL: build.py exited %d" % rc)
            return 1
        with open(COMMITTED, "rb") as f:
            after = f.read()
    if before == after:
        print("OK: geomask.sh matches build (sha256 %s)" % hashlib.sha256(after).hexdigest()[:16])
        return 0
    print("FAIL: committed geomask.sh differs from build of geomask.sh.in + assets")
    print("      fix: python build.py && git add geomask.sh")
    return 1


if __name__ == "__main__":
    sys.exit(main())
