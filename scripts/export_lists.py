# -*- coding: utf-8 -*-
# geomask lists exporter: turn a MagiTrickle config.yaml (fetched from the router) into
# the bundled template asset + meta. Run from the repo root.
#
#   1) scp root@router:/etc/magitrickle/state/config.yaml /tmp/mt.yaml
#   2) python scripts/export_lists.py /tmp/mt.yaml
#
# Writes: assets/magitrickle-groups.yaml, templates/lists.meta.json
# Refuses to write if the groups block looks like it contains a secret (URL / hwid / token).
import hashlib, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_GROUPS = os.path.join(ROOT, "assets", "magitrickle-groups.yaml")
OUT_META = os.path.join(ROOT, "templates", "lists.meta.json")
TEMPLATE_VERSION = "1.14.2"

def log(m): print("[export]", m, flush=True)

def groups_region(lines):
    gi = None
    for i, l in enumerate(lines):
        if l == "groups:" and gi is None:
            gi = i
    if gi is None:
        return None, None
    si = len(lines)
    for i in range(gi + 1, len(lines)):
        if re.match(r"^[A-Za-z]", lines[i]):
            si = i
            break
    return gi, si

def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python scripts/export_lists.py <magitrickle-config.yaml>")
    src = sys.argv[1]
    with open(src, encoding="utf-8") as f:
        lines = f.read().split("\n")
    gi, si = groups_region(lines)
    if gi is None:
        sys.exit("no groups: section in %s" % src)
    block = lines[gi:si]
    while block and block[-1].strip() == "":
        block.pop()
    body = "\n".join(block) + "\n"

    ngroups = sum(1 for l in block if l.startswith("- id:"))
    nrules = sum(1 for l in block if l.startswith("  - id:"))
    if ngroups == 0:
        sys.exit("REFUSE: empty groups block")

    bad = re.findall(r"(?i)(x-hwid\s*:|api[_-]?key|token\s*:|password\s*:|secret\s*:|Bearer\s|https?://)", body)
    if bad:
        sys.exit("REFUSE: possible secrets in groups block: %r" % set(bad))

    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    header = (
        "# geomask MagiTrickle groups template (recovery snapshot).\n"
        "# Source of truth for domain lists: To VPN / To Not VPN / Google+AI to DE /\n"
        "# Geoblock / Block / Youtube / CDN+social groups, etc. No secrets here.\n"
        "# Restore: geomask.sh --seed-lists  (or panel card). See docs/LISTS.md.\n"
    )
    os.makedirs(os.path.dirname(OUT_GROUPS), exist_ok=True)
    os.makedirs(os.path.dirname(OUT_META), exist_ok=True)
    with open(OUT_GROUPS, "w", encoding="utf-8", newline="\n") as f:
        f.write(header + body)
    meta = {
        "template_version": TEMPLATE_VERSION,
        "exported_at": __import__("time").strftime("%Y-%m-%d"),
        "groups": ngroups,
        "rules": nrules,
        "sha256": sha,
        "source": "router /etc/magitrickle/state/config.yaml",
    }
    with open(OUT_META, "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")
    log("groups=%d rules=%d sha256=%s" % (ngroups, nrules, sha[:16]))
    log("wrote %s" % OUT_GROUPS)
    log("wrote %s" % OUT_META)

if __name__ == "__main__":
    main()
