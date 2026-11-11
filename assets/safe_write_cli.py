#!/usr/bin/env python3
"""CLI wrapper for safe_write - thin wrapper calling safe_write.py."""
import json
import sys
import os
import subprocess

sys.path.insert(0, os.path.dirname(__file__))
from safe_write import (
    safe_write, nodes_render_candidate, nodes_validate_request, nodes_rollback_check,
    mihomo_validate, mihomo_rollback_check, zapret_validate, zapret_rollback_check,
    RESOURCES, backup_gate, acquire_lock, release_lock, sha256_file,
    nodes_render_candidate_with_cron, nodes_validate_request, nodes_rollback_check_with_cron
)

RESOURCE_SPECS = {
    'nodes_conf': {
        'render': nodes_render_candidate,
        'validate': nodes_validate_request,
        'rollback_check': nodes_rollback_check,
    },
    'mihomo_config': {
        'render': lambda live, editable: editable,
        'validate': mihomo_validate,
        'rollback_check': mihomo_rollback_check,
    },
    'zapret_exclude': {
        'render': lambda live, editable: editable,
        'validate': zapret_validate,
        'rollback_check': zapret_rollback_check,
    },
}


def main():
    if len(sys.argv) < 4:
        print(json.dumps({"ok": False, "error": "usage: safe_write_cli.py <resource> <live_content> <editable_json> [max_age]"}))
        sys.exit(1)

    resource = sys.argv[1]
    live_content = sys.argv[2]
    editable_json = sys.argv[3]
    max_age = int(sys.argv[4]) if len(sys.argv) > 4 else 300

    if resource not in RESOURCE_SPECS:
        print(json.dumps({"ok": False, "error": f"unknown resource: {resource}"}))
        sys.exit(1)

    try:
        editable = json.loads(editable_json)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"invalid editable JSON: {e}"}))
        sys.exit(1)

    # For nodes_conf, we need to handle cron pair
    if resource == 'nodes_conf':
        # Read current cron
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        live_cron = result.stdout if result.returncode == 0 else ''

        # Combine live_content (conf) and live_cron for the pair
        extended_live = json.dumps({
            'conf': live_content,
            'cron': live_cron
        })

        def make_candidate(old_extended):
            old_data = json.loads(old_extended) if old_extended else {'conf': '', 'cron': ''}
            new_conf = nodes_render_candidate_with_cron(old_data.get('conf', ''), editable)
            new_cron = nodes_render_candidate_with_cron(old_data.get('cron', ''), editable)  # This is wrong - need cron-specific render
            return json.dumps({'conf': new_conf, 'cron': new_cron})

        # For now, use the existing safe_write but we need to handle cron specially
        # Let's just call the new safe_write_nodes_conf function from safe_write.py
        from safe_write import safe_write_nodes_conf
        rc, result = safe_write_nodes_conf(live_content, 
            subprocess.run(['crontab', '-l'], capture_output=True, text=True).stdout if subprocess.run(['crontab', '-l'], capture_output=True).returncode == 0 else '',
            json.loads(sys.argv[3]), int(sys.argv[4]) if len(sys.argv) > 4 else 300)
        if rc == 0:
            print(json.dumps({"ok": True, "content": result}))
        else:
            print(json.dumps({"ok": False, "error": result}))
            sys.exit(rc)
        return

    spec = RESOURCE_SPECS[resource]
    editable = json.loads(sys.argv[3])

    def make_candidate(old_content):
        return spec['render'](old_content, editable)

    def validate(content):
        return spec['validate'](content)

    def rollback_check(pre, post):
        return spec['rollback_check'](pre, post)

    rc = safe_write(resource, make_candidate, validate, rollback_check, int(sys.argv[4]) if len(sys.argv) > 4 else 300)

    if rc == 0:
        from pathlib import Path
        live_path = RESOURCES[resource]['live']
        if live_path.exists():
            new_content = live_path.read_bytes()
            print(json.dumps({"ok": True, "content": new_content.decode('utf-8')}))
        else:
            print(json.dumps({"ok": True, "content": ""}))
    else:
        error_msgs = {1: "validation failed", 2: "backup gate failed", 3: "trial failed", 4: "install/reload failed", 5: "rollback failed"}
        print(json.dumps({"ok": False, "error": error_msgs.get(rc, f"unknown error {rc}")}))
        sys.exit(rc)


if __name__ == '__main__':
    main()