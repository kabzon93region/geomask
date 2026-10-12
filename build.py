# -*- coding: utf-8 -*-
# Build zm-setup.sh: inject assets from ./assets into template placeholders.
# Usage: python build.py   -> writes ../zm-setup.sh (and ./zm-setup.sh)
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "zm-setup.sh.in")
OUT = os.path.join(HERE, "zm-setup.sh")
ASSETS = os.path.join(HERE, "assets")

MAP = {
    "geo-backend.sh":        "geo-backend.sh",
    "geo-sync":              "geo-sync",
    "acl.json":              "acl-geo-sync.json",
    "menu.json":             "menu-geo-sync.json",
    "geo-main.js":           "main.js",
    "sync_mtrickle_mihomo.sh": "sync_mtrickle_mihomo.sh",
    "ytdirect.yaml":         "ytdirect.yaml",
    "99-default-vpn-route":  "99-default-vpn-route",
}

# menu.json asset file on disk (same acl download name collision): we saved menu as second luci-app-geo-sync.json
# handle explicitly: assets dir has luci-app-geo-sync.json (acl) and menu was downloaded to the same name overwriting!
# -> build expects: assets/luci-app-geo-sync.json = ACL, assets/menu-geo-sync.json = MENU (fixed below if present)

def load(name):
    p = os.path.join(ASSETS, name)
    if not os.path.exists(p):
        raise SystemExit("asset missing: " + p)
    return open(p, encoding="utf-8").read()

def main():
    tpl = open(TPL, encoding="utf-8").read()

    def repl(m):
        key = m.group(1)
        if key not in MAP:
            raise SystemExit("unknown asset placeholder: " + key)
        data = load(MAP[key])
        # sanity: asset must not contain the heredoc delimiter line
        for delim in ("ASSET_EOF",):
            for line in data.splitlines():
                if line.strip() == delim:
                    raise SystemExit("asset %s contains delimiter %s" % (key, delim))
        return data

    out, n = re.subn(r"@@ASSET:([A-Za-z0-9._-]+)@@", repl, tpl)
    if n == 0:
        raise SystemExit("no placeholders replaced - check template")
    missing = re.findall(r"@@ASSET:[^@]+@@", out)
    if missing:
        raise SystemExit("unreplaced placeholders: %s" % missing)

    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    os.chmod(OUT, 0o755)
    print("built %s (%d bytes), %d assets injected" % (OUT, len(out.encode()), n))

if __name__ == "__main__":
    main()
