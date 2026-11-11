#!/usr/bin/env python3
"""Module Registry CLI for geomask modular system."""
import json
import sys
from pathlib import Path

sys.path.insert(0, '/usr/lib/geomask')
from module_manifest import ModuleRegistry, ModuleManifest, ModuleState

REGISTRY_PATH = Path('/etc/geomask/modules')

def main():
    if len(sys.argv) < 2:
        print("Usage: geomask-module <command> [args...]")
        print("Commands:")
        print("  list [--state STATE]     List all modules")
        print("  get <module_id>          Show module details")
        print("  enable <module_id>       Enable a module")
        print("  disable <module_id>      Disable a module")
        print("  validate <module_id>     Validate module dependencies")
        print("  cycles                   Check for dependency cycles")
        print("  resolve <module_ids...>  Resolve dependency order")
        sys.exit(1)

    registry = ModuleRegistry()
    cmd = sys.argv[1]

    if cmd == 'list':
        state_filter = None
        if '--state' in sys.argv:
            idx = sys.argv.index('--state')
            if idx + 1 < len(sys.argv):
                state_filter = ModuleState(sys.argv[idx + 1])
        
        modules = registry.list_modules(state_filter)
        for m in registry.modules.values():
            if state_filter is None or m.state == state_filter:
                print(f"{m.id:20} {m.name:25} v{m.version:8} [{m.type.value:10}] {m.state.value:12} deps={', '.join(d.module_id for d in m.dependencies)}")

    elif cmd == 'get':
        if len(sys.argv) < 3:
            print("Usage: geomask-module get <module_id>")
            sys.exit(1)
        module = registry.get(sys.argv[2])
        if not module:
            print(f"Module {sys.argv[2]} not found")
            sys.exit(1)
        print(json.dumps(module.to_dict(), indent=2, ensure_ascii=False))

    elif cmd == 'enable':
        if len(sys.argv) < 3:
            print("Usage: geomask-module enable <module_id>")
            sys.exit(1)
        success, errors = registry.enable_module(sys.argv[2])
        if success:
            print(f"Module {sys.argv[2]} enabled")
        else:
            print(f"Failed to enable: {errors}")
            sys.exit(1)

    elif cmd == 'disable':
        if len(sys.argv) < 3:
            print("Usage: geomask-module disable <module_id>")
            sys.exit(1)
        force = '--force' in sys.argv
        success, errors = registry.disable_module(sys.argv[2], force)
        if success:
            print(f"Module {sys.argv[2]} disabled")
        else:
            print(f"Failed to disable: {errors}")
            sys.exit(1)

    elif cmd == 'validate':
        if len(sys.argv) < 3:
            print("Usage: geomask-module validate <module_id>")
            sys.exit(1)
        errors = registry.validate_module(sys.argv[2])
        if errors:
            print(f"Validation errors for {sys.argv[2]}:")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)
        else:
            print(f"Module {sys.argv[2]} is valid")

    elif cmd == 'cycles':
        cycles = registry.check_cycles()
        if cycles:
            print("Dependency cycles found:")
            for cycle in cycles:
                print(f"  {' -> '.join(cycle)}")
        else:
            print("No dependency cycles found")

    elif cmd == 'resolve':
        if len(sys.argv) < 3:
            print("Usage: geomask-module resolve <module_ids...>")
            sys.exit(1)
        module_ids = sys.argv[2:]
        try:
            order = registry.resolve_dependencies(sys.argv[2:])
            print("Resolution order:", " -> ".join(order))
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

if __name__ == '__main__':
    main()