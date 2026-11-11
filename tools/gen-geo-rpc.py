#!/usr/bin/env python3
"""Generate all four geo-sync RPC integration points from tools/geo-rpc.json.

The geo-sync RPC surface used to be maintained by hand in four places at once
(docs/PLAN-2026-11-02-geomask-v2.md, modularization step): adding one method
meant four coordinated edits and drift was easy (subs_seed was lost once).
This script regenerates every point from the single manifest:

  1. assets/geo-sync            rpcd plugin: list_methods entries + call_method cases
  2. assets/acl-geo-sync.json   ubus ACL grants (whole file is generated)
  3. assets/geo-backend.sh      dispatcher case block
  4. assets/geo-sync-page.js    rpc.declare declaration block

Regions are delimited by generated markers:

  # >>> geo-rpc generated:<region> (tools/geo-rpc.json) >>>
  ... generated content ...
  # <<< geo-rpc generated:<region> <<<

Content outside markers is never touched. acl-geo-sync.json has no markers and
is emitted whole (its entire content is derived from the manifest).

Usage:
  python3 tools/gen-geo-rpc.py            rewrite the marked regions
  python3 tools/gen-geo-rpc.py --check    report drift, exit 1 when out of sync
"""

import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.normpath(os.path.join(HERE, "..", "assets"))
MANIFEST = os.path.join(HERE, "geo-rpc.json")

BEGIN_MARK = ">>> geo-rpc generated:{region} (tools/geo-rpc.json) >>>"
END_MARK = "<<< geo-rpc generated:{region} <<<"

# file -> (regions in file order, comment prefix for markers)
TARGETS = [
    ("geo-sync", ("list", "call"), "#"),
    ("geo-backend.sh", ("dispatch",), "#"),
    ("geo-sync-page.js", ("decl",), "//"),
]
ACL_FILE = "acl-geo-sync.json"
TOTAL_STEPS = len(TARGETS) + 3  # manifest + marked files + acl + cross-checks


def pad(s, width):
    return s + " " * max(width - len(s), 0)


def render_list(methods):
    """list_methods() lines: json_add_object "name"; [args] json_close_object."""
    width = max(len('"%s";' % m["name"]) for m in methods) + 1
    lines = []
    for m in methods:
        parts = ['json_add_string "%s" "string";' % a for a in m["args"]]
        parts.append("json_close_object")
        lines.append("\tjson_add_object " + pad('"%s";' % m["name"], width) + " ".join(parts))
    return lines


def render_call(methods):
    """call_method() case lines: name) json_get_var ...; "$BACKEND" name "$arg" ;;"""
    width = max(len(m["name"]) for m in methods) + 2  # "name)" + one space
    lines = []
    for m in methods:
        gets = " ".join("json_get_var %s %s;" % (a, a) for a in m["args"])
        call = '"$BACKEND" %s' % m["name"]
        if m["args"]:
            call += " " + " ".join('"$%s"' % a for a in m["args"])
        body = (gets + " " + call) if gets else call
        lines.append("\t\t" + pad("%s)" % m["name"], width) + body + " ;;")
    return lines


def render_dispatch(methods):
    """geo-backend.sh case lines: name) cmd_name "$2" ... ;;

    A method may carry "backend_body": explicit case lines for handlers that do
    not fit the plain cmd_<name> pattern (nodescan kicks a background job).
    """
    regs = [m for m in methods if "backend_body" not in m]
    width = max(len(m["name"]) for m in regs) + 2
    lines = []
    for m in methods:
        if "backend_body" in m:
            lines.extend(m["backend_body"])
            continue
        args = " ".join('"$%d"' % (i + 2) for i in range(len(m["args"])))
        cmd = m.get("backend", "cmd_" + m["name"])
        if args:
            cmd += " " + args
        lines.append("  " + pad("%s)" % m["name"], width) + cmd + " ;;")
    return lines


def render_decl(methods, rpc_object):
    """geo-sync-page.js rpc.declare lines, manifest order, aligned '='."""
    decls = [m for m in methods if m.get("js")]
    width = max(len("var %s" % m["js"]) for m in decls) + 1
    lines = []
    note_done = False
    for m in methods:
        js = m.get("js")
        if not js:
            continue
        if m["args"] and not note_done:
            note_done = True
            lines.append("/* params as OBJECT (not array): array form expects positional args and would")
            lines.append('   wrap {mode:...} into {"mode":{...}} breaking rpcd json_get_var */')
        params = ""
        if m["args"]:
            if m.get("js_positional"):
                params = ", params: [ " + ", ".join(repr(a) for a in m["args"]) + " ]"
            else:
                params = ", params: { " + ", ".join("%s: null" % a for a in m["args"]) + " }"
        lines.append(
            pad("var %s" % js, width) + "= rpc.declare({ object: '" + rpc_object
            + "', method: '" + m["name"] + "'" + params + ", expect: {} });"
        )
    return lines


def render_acl(manifest):
    """Whole acl-geo-sync.json content, tab-indented like the original file."""
    acl = manifest["acl"]
    read = [m["name"] for m in manifest["methods"] if m["acl"] == "read"]
    write = [m["name"] for m in manifest["methods"] if m["acl"] == "write"]

    def arr(names):
        return "[ " + ", ".join('"%s"' % n for n in names) + " ]"

    out = ["{"]
    out.append('\t"%s": {' % acl["group"])
    out.append('\t\t"description": "%s",' % acl["description"])
    out.append('\t\t"read": {')
    out.append('\t\t\t"ubus": {')
    out.append('\t\t\t\t"%s": %s,' % (manifest["rpc_object"], arr(read)))
    extra = acl.get("read_extra", {})
    for i, obj in enumerate(extra):
        tail = "," if i < len(extra) - 1 else ""
        out.append('\t\t\t\t"%s": %s%s' % (obj, arr(extra[obj]), tail))
    out.append("\t\t\t}")
    out.append("\t\t},")
    out.append('\t\t"write": {')
    out.append('\t\t\t"ubus": {')
    out.append('\t\t\t\t"%s": %s' % (manifest["rpc_object"], arr(write)))
    out.append("\t\t\t}")
    out.append("\t\t}")
    out.append("\t}")
    out.append("}")
    return "\n".join(out) + "\n"


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        data = f.read()
    if data.startswith("\ufeff"):
        raise RuntimeError("%s: UTF-8 BOM not allowed" % path)
    if "\r\n" in data:
        raise RuntimeError("%s: CRLF line endings not allowed" % path)
    return data


def write_text(path, data):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(data)


def apply_region(text, region, prefix, lines):
    """Replace the marked region; returns (new_text, changed)."""
    begin = "%s %s" % (prefix, BEGIN_MARK.format(region=region))
    end = "%s %s" % (prefix, END_MARK.format(region=region))
    if text.count(begin) != 1:
        raise RuntimeError("region %r: begin marker not found exactly once in file" % region)
    if text.count(end) != 1:
        raise RuntimeError("region %r: end marker not found exactly once in file" % region)
    i = text.index(begin)
    nl = text.index("\n", i) + 1
    j = text.index(end, i)
    k = text.rindex("\n", nl, j) + 1
    new = text[:nl] + "\n".join(lines) + "\n" + text[k:]
    return new, (new != text)


def cross_checks(manifest, files):
    """Semantic checks the region diff cannot express."""
    errors = []
    backend = files["geo-backend.sh"]
    page = files["geo-sync-page.js"]
    rpc_object = manifest["rpc_object"]
    for m in manifest["methods"]:
        if "backend_body" not in m:
            if not re.search(r"(?m)^%s\(" % re.escape(m.get("backend", "cmd_" + m["name"])), backend):
                errors.append("geo-backend.sh: function %s() not found for method %s"
                              % (m.get("backend", "cmd_" + m["name"]), m["name"]))
        expected = 1 if m.get("js") else 0
        found = page.count("object: '%s', method: '%s'" % (rpc_object, m["name"]))
        if found != expected:
            errors.append("geo-sync-page.js: method '%s' declared %d time(s), expected %d"
                          % (m["name"], found, expected))
    return errors


def main():
    t0 = time.time()
    check = "--check" in sys.argv[1:]
    print("gen-geo-rpc: manifest %s (%s mode)" % (MANIFEST, "check" if check else "apply"), flush=True)

    with open(MANIFEST, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    methods = manifest["methods"]

    names = [m["name"] for m in methods]
    dups = sorted({n for n in names if names.count(n) > 1})
    js_names = [m["js"] for m in methods if m.get("js")]
    jdups = sorted({n for n in js_names if js_names.count(n) > 1})
    bad_acl = [m["name"] for m in methods if m.get("acl") not in ("read", "write")]
    if dups or jdups or bad_acl:
        raise RuntimeError("manifest invalid: duplicate names=%s js=%s acl=%s" % (dups, jdups, bad_acl))
    n_read = sum(1 for m in methods if m["acl"] == "read")
    print("[1/%d] manifest OK: %d methods (read %d / write %d)" % (TOTAL_STEPS, len(methods), n_read, len(methods) - n_read), flush=True)

    rendered = {
        "list": render_list(methods),
        "call": render_call(methods),
        "dispatch": render_dispatch(methods),
        "decl": render_decl(methods, manifest["rpc_object"]),
    }

    files = {name: read_text(os.path.join(ASSETS, name)) for name, _, _ in TARGETS}
    files[ACL_FILE] = read_text(os.path.join(ASSETS, ACL_FILE))

    drift = []
    changed = []
    step = 2
    for name, regions, prefix in TARGETS:
        new = files[name]
        touched = []
        for region in regions:
            new, ch = apply_region(new, region, prefix, rendered[region])
            if ch:
                touched.append(region)
        if touched and not check:
            write_text(os.path.join(ASSETS, name), new)
            changed.append("%s (%s)" % (name, ", ".join(touched)))
        if touched and check:
            drift.append("%s: %s" % (name, ", ".join(touched)))
        state = "DRIFT" if (touched and check) else ("rewritten" if touched else "in sync")
        print("[%d/%d] %s: %s" % (step, TOTAL_STEPS, name, state), flush=True)
        step += 1

    acl_new = render_acl(manifest)
    acl_touched = acl_new != files[ACL_FILE]
    if acl_touched and not check:
        write_text(os.path.join(ASSETS, ACL_FILE), acl_new)
        changed.append(ACL_FILE)
    print("[%d/%d] %s: %s" % (step, TOTAL_STEPS, ACL_FILE,
                              "DRIFT" if (acl_touched and check) else ("rewritten" if acl_touched else "in sync")), flush=True)
    if acl_touched and check:
        drift.append(ACL_FILE)
    step += 1

    errs = cross_checks(manifest, files)
    print("[%d/%d] cross-checks (backend functions, js declarations): %s"
          % (step, TOTAL_STEPS, "FAIL" if errs else "OK"), flush=True)
    for e in errs:
        print("  - " + e, flush=True)

    if drift or errs:
        print("RESULT: DRIFT - %d file(s) out of sync, %d cross-check error(s); run gen-geo-rpc.py to fix"
              % (len(drift), len(errs)), flush=True)
        for d in drift:
            print("  - " + d, flush=True)
        return 1
    if check:
        print("RESULT: OK - all four integration points match the manifest", flush=True)
    else:
        print("RESULT: OK - updated: %s" % ("; ".join(changed) if changed else "nothing to change"), flush=True)
    print("done in %.1fs" % (time.time() - t0), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError) as e:
        print("ERROR: %s" % e, flush=True)
        sys.exit(1)
