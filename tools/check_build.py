# -*- coding: utf-8 -*-
# Verify committed geomask.sh matches a fresh build of geomask.sh.in + assets (TD-05).
# Rebuilds in place (same as the manual "python build.py && git diff" workflow)
# and exits 1 when the committed artifact differs from the build.
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COMMITTED = os.path.join(ROOT, "geomask.sh")
BUILD = os.path.join(ROOT, "build.py")


def main():
    if not os.path.exists(COMMITTED):
        print("FAIL: geomask.sh not found - run: python build.py")
        return 1
    with open(COMMITTED, "rb") as f:
        before = f.read()
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
