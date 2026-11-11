#!/usr/bin/env python3
"""Generate mihomo ruleset YAML from path policies."""
import sys
from pathlib import Path
from typing import Dict, List, Any

sys.path.insert(0, str(Path(__file__).parent))
from policy_model import PolicyManifest, PathPolicy, NamedFilter, FilterRule


def filter_to_ruleset(filter_obj: NamedFilter, path_policy) -> dict:
    """Convert a NamedFilter to mihomo ruleset format."""
    payload = []
    for rule in filter_obj.rules:
        # mihomo format: "RULE_TYPE, VALUE, ACTION"
        # rule.type: DOMAIN-SUFFIX, GEOIP, DOMAIN, etc.
        # rule.value: domain, country code, etc.
        # rule.action: DIRECT, REJECT, PROXY
        if rule.type == 'GEOIP' and rule.value == '!':
            # Negative GEOIP: all except
            # mihomo doesn't support negative geoip directly, need workarounds
            continue
        item = f"{rule.type},{rule.value},{rule.action}"
        payload.append(item)

    return {
        'payload': payload
    }


def generate_path_rulesets(policy, output_dir: Path):
    """Generate ruleset YAML files for all paths."""
    output_dir.mkdir(parents=True, exist_ok=True)

    for path in policy.paths:
        if not path.enabled:
            continue

        filter_obj = policy.filters.get(path.filter_id)
        if not filter_obj:
            print(f"Warning: filter {path.filter_id} not found for path {path.id}", file=sys.stderr)
            continue

        ruleset = filter_to_ruleset(policy.filters[path.filter_id], path)
        ruleset['path_id'] = path.id
        ruleset['path_name'] = path.name

        output_file = output_dir / f"{path.id}.yaml"
        import yaml
        with output_file.open('w', encoding='utf-8') as f:
            yaml.dump(ruleset, f, allow_unicode=True, sort_keys=False)
        print(f"Generated {output_file}")


def generate_all_rulesets(policy_path: str = '/etc/geomask/policies.yaml',
                          output_dir: str = '/etc/mihomo/ruleset'):
    policy = PolicyManifest.load(Path(policy_path))
    generate_path_rulesets(policy, Path(output_dir))


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Generate mihomo rulesets from policies')
    parser.add_argument('--policy', default='/etc/geomask/policies.yaml', help='Policy file')
    parser.add_argument('--output', default='/etc/mihomo/ruleset', help='Output directory')
    parser.add_argument('--dry-run', action='store_true', help='Print to stdout instead of writing')
    args = parser.parse_args()

    policy = PolicyManifest.load(Path(args.policy))
    if args.dry_run:
        import yaml
        for path in policy.paths:
            if not path.enabled:
                continue
            filter_obj = policy.filters.get(path.filter_id)
            if filter_obj:
                ruleset = {
                    'path_id': path.id,
                    'path_name': path.name,
                    'payload': [f"{r.type},{r.value},{r.action}" for r in filter_obj.rules]
                }
                print(yaml.dump(yaml.safe_load(yaml.dump({'payload': [f"{r.type},{r.value},{r.action}" for r in policy.filters[path.filter_id].rules]})), allow_unicode=True, sort_keys=False))
    else:
        generate_all_rulesets(args.policy, args.output)


if __name__ == '__main__':
    main()