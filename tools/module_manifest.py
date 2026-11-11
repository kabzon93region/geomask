#!/usr/bin/env python3
"""Module Manifest system for geomask modular architecture."""
import json
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Any
from enum import Enum


class ModuleState(str, Enum):
    """Module lifecycle state."""
    INSTALLED = "installed"       # Files present, but not active
    ENABLED = "enabled"           # Active and running
    DISABLED = "disabled"         # Installed but stopped
    BLOCKED = "blocked"           # Cannot run due to failed deps/validation
    NOT_APPLICABLE = "not_applicable"  # Not relevant for current profile


class ModuleType(str, Enum):
    CORE = "core"           # Always required (safe_write, backup, etc.)
    PATH = "path"           # VPN, VPN-noP2P, AI
    FEATURE = "feature"     # youtube, torrent, device-modes, ai-dns
    DIAGNOSTIC = "diagnostic"  # morning-check, dnscheck, vpnwatch
    SERVICE = "service"     # backup-share, self-update, admission


@dataclass
class ModuleDependency:
    """Dependency declaration."""
    module_id: str
    required: bool = True           # If False, optional
    min_version: str = "0.0.0"      # Minimum compatible version
    reason: str = ""                # Human-readable reason


@dataclass
class ModuleOwnedResource:
    """Resource owned by this module (for cleanup on disable/uninstall)."""
    type: str                       # file, cron, nft, ipset, ip_rule, service, rpc_method
    path: str                       # Path or identifier
    description: str = ""


@dataclass
class ModuleManifest:
    """Complete module declaration."""
    id: str
    name: str
    version: str
    schema_version: int = 1
    type: ModuleType = ModuleType.FEATURE
    description: str = ""
    author: str = "geomask"
    license: str = "MIT"
    
    # Dependencies
    dependencies: List[ModuleDependency] = field(default_factory=list)
    
    # What this module provides
    provides: List[str] = field(default_factory=list)  # e.g., "rpc:policy_get", "cron:node-scan"
    
    # Resources owned by this module
    owned_resources: List[ModuleOwnedResource] = field(default_factory=list)
    
    # Configuration
    config_files: List[str] = field(default_factory=list)
    config_schema: Dict[str, Any] = field(default_factory=dict)
    
    # Runtime
    services: List[str] = field(default_factory=list)  # systemd/procd service names
    cron_jobs: List[str] = field(default_factory=list)  # cron identifiers
    rpc_methods: List[str] = field(default_factory=list)
    
    # State
    state: ModuleState = ModuleState.INSTALLED
    enabled: bool = False
    
    # Metadata
    created_at: str = ""
    updated_at: str = ""
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        if not self.updated_at:
            self.updated_at = self.created_at
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d['type'] = self.type.value
        d['state'] = self.state.value
        d['dependencies'] = [asdict(d) for d in self.dependencies]
        d['owned_resources'] = [asdict(r) for r in self.owned_resources]
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> 'ModuleManifest':
        data = data.copy()
        data['type'] = ModuleType(data.get('type', 'feature'))
        data['state'] = ModuleState(data.get('state', 'installed'))
        data['dependencies'] = [ModuleDependency(**d) for d in data.get('dependencies', [])]
        data['owned_resources'] = [ModuleOwnedResource(**r) for r in data.get('owned_resources', [])]
        return cls(**data)
    
    def sha256(self) -> str:
        """Content hash for integrity verification."""
        canon = json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canon.encode('utf-8')).hexdigest()

    def save(self, path: Path):
        """Save manifest to file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding='utf-8')


class ModuleRegistry:
    """Central registry of all modules with dependency resolution."""
    
    def __init__(self, registry_path: Path = Path('/etc/geomask/modules')):
        self.registry_path = registry_path
        self.registry_path.mkdir(parents=True, exist_ok=True)
        self.modules: Dict[str, ModuleManifest] = {}
        self._load_all()
    
    def _load_all(self):
        """Load all module manifests from registry."""
        for manifest_file in self.registry_path.glob('*.json'):
            try:
                data = json.loads(manifest_file.read_text(encoding='utf-8'))
                module = ModuleManifest.from_dict(data)
                self.modules[module.id] = module
            except Exception as e:
                print(f"Warning: Failed to load {manifest_file}: {e}")
    
    def register(self, module: ModuleManifest) -> bool:
        """Register a new module or update existing."""
        if module.id in self.modules:
            existing = self.modules[module.id]
            if existing.sha256() == module.sha256():
                return False  # No change
        self.modules[module.id] = module
        self._save(module)
        return True
    
    def _save(self, module: ModuleManifest):
        module.updated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        path = self.registry_path / f"{module.id}.json"
        path.write_text(json.dumps(module.to_dict(), indent=2, ensure_ascii=False), encoding='utf-8')
    
    def unregister(self, module_id: str, force: bool = False) -> bool:
        """Unregister a module."""
        if module_id not in self.modules:
            return False
        module = self.modules[module_id]
        if module.state == ModuleState.ENABLED and not force:
            return False  # Cannot unregister enabled module without force
        del self.modules[module_id]
        path = self.registry_path / f"{module_id}.json"
        if path.exists():
            path.unlink()
        return True
    
    def get(self, module_id: str) -> Optional[ModuleManifest]:
        return self.modules.get(module_id)
    
    def list_modules(self, state: Optional[ModuleState] = None) -> List[ModuleManifest]:
        modules = list(self.modules.values())
        if state:
            modules = [m for m in modules if m.state == state]
        return sorted(modules, key=lambda m: m.id)
    
    def resolve_dependencies(self, module_ids: List[str]) -> List[str]:
        """Resolve dependency order for enabling modules (topological sort)."""
        # Build subgraph
        subgraph = {mid: self.modules[mid] for module_id in module_ids if module_id in self.modules}
        
        # Kahn's algorithm
        in_degree = {mid: 0 for mid in subgraph}
        for mid, module in subgraph.items():
            for dep in module.dependencies:
                if dep.module_id in subgraph:
                    in_degree[dep.module_id] += 1
        
        queue = [mid for mid, deg in in_degree.items() if deg == 0]
        result = []
        
        while queue:
            mid = queue.pop(0)
            result.append(mid)
            module = subgraph[mid]
            for dep in module.dependencies:
                if dep.module_id in in_degree:
                    in_degree[dep.module_id] -= 1
                    if in_degree[dep.module_id] == 0:
                        queue.append(dep.module_id)
        
        if len(result) != len(subgraph):
            raise ValueError("Cycle detected in module dependencies")
        
        return result
    
    def check_cycles(self) -> List[List[str]]:
        """Detect all dependency cycles."""
        # Simplified: check if any module's deps form a cycle
        cycles = []
        for mid, module in self.modules.items():
            visited = set()
            stack = [(mid, [mid])]
            while stack:
                current, path = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                if current not in self.modules:
                    continue
                for dep in self.modules[current].dependencies:
                    if dep.module_id in path:
                        cycles.append(path + [dep.module_id])
                    elif dep.module_id in self.modules:
                        stack.append((dep.module_id, path + [dep.module_id]))
        return cycles
    
    def validate_module(self, module_id: str) -> List[str]:
        """Validate a module's dependencies and resources."""
        errors = []
        if module_id not in self.modules:
            return [f"Module {module_id} not found"]
        module = self.modules[module_id]
        
        # Check dependencies exist
        for dep in module.dependencies:
            if dep.module_id not in self.modules:
                if dep.required:
                    errors.append(f"Missing required dependency: {dep.module_id}")
            else:
                dep_module = self.modules[dep.module_id]
                # Version check would go here
        
        # Check for cycles
        cycles = self.check_cycles()
        for cycle in cycles:
            if module_id in cycle:
                errors.append(f"Module {module_id} participates in dependency cycle: {' -> '.join(cycle)}")
        
        return errors
    
    def get_module_state(self, module_id: str) -> ModuleState:
        """Get effective state considering dependencies."""
        if module_id not in self.modules:
            return ModuleState.NOT_APPLICABLE
        module = self.modules[module_id]
        
        if module.state == ModuleState.BLOCKED:
            return ModuleState.BLOCKED
        
        if module.state == ModuleState.ENABLED:
            # Check if all required deps are enabled
            for dep in module.dependencies:
                if dep.required:
                    dep_state = self.get_module_state(dep.module_id)
                    if dep_state != ModuleState.ENABLED:
                        return ModuleState.BLOCKED
            return ModuleState.ENABLED
        
        return module.state
    
    def enable_module(self, module_id: str) -> tuple[bool, List[str]]:
        """Enable a module, respecting dependencies."""
        if module_id not in self.modules:
            return False, [f"Module {module_id} not found"]
        
        module = self.modules[module_id]
        errors = self.validate_module(module.id)
        if errors:
            return False, errors
        
        # Enable dependencies first
        for dep in module.dependencies:
            if dep.required:
                success, errs = self.enable_module(dep.module_id)
                if not success:
                    return False, errs
        
        # Enable this module
        module.state = ModuleState.ENABLED
        module.enabled = True
        self._save(module)
        return True, []
    
    def disable_module(self, module_id: str, force: bool = False) -> tuple[bool, List[str]]:
        """Disable a module, checking for dependents."""
        if module_id not in self.modules:
            return False, [f"Module {module_id} not found"]
        
        # Check if other enabled modules depend on this
        for mid, module in self.modules.items():
            if module.state == ModuleState.ENABLED:
                for dep in module.dependencies:
                    if dep.module_id == module_id and dep.required:
                        if not force:
                            return False, [f"Module {mid} depends on {module_id}; use force to disable"]
        
        module = self.modules[module_id]
        module.state = ModuleState.DISABLED
        module.enabled = False
        self._save(module)
        return True, []


# --- Default Module Definitions ---

def create_core_manifest() -> ModuleManifest:
    """Core infrastructure module - always required."""
    return ModuleManifest(
        id="core",
        name="Geomask Core",
        version="1.0.0",
        type=ModuleType.CORE,
        description="Core infrastructure: safe_write, backup, RPC framework",
        dependencies=[],
        provides=["safe_write", "backup_gate", "rpc_framework"],
        owned_resources=[
            ModuleOwnedResource("file", "/usr/lib/geomask/safe_write.py"),
            ModuleOwnedResource("file", "/usr/lib/geomask/safe_write_cli.py"),
            ModuleOwnedResource("service", "geomask-backup"),
        ],
        config_files=["/etc/geomask/modules/core.json"],
        services=[],
        cron_jobs=[],
        rpc_methods=["backup_gate_status", "safe_write_status"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_path_vpn_manifest() -> ModuleManifest:
    """VPN path module."""
    return ModuleManifest(
        id="path_vpn",
        name="VPN Path",
        version="1.0.0",
        type=ModuleType.PATH,
        description="Standard VPN path with NL/DE/FR/SE countries",
        dependencies=[
            ModuleDependency("core", True, "1.0.0", "Core infrastructure required"),
            ModuleDependency("subscription:sub", True, "1.0.0", "Main subscription"),
        ],
        provides=["path:vpn", "filter:base_filter"],
        owned_resources=[
            ModuleOwnedResource("config", "/etc/geomask/policies.yaml#path:vpn"),
            ModuleOwnedResource("filter", "/etc/mihomo/ruleset/vpn.yaml"),
        ],
        config_files=["/etc/geomask/policies.yaml"],
        services=["mihomo"],
        cron_jobs=[],
        rpc_methods=[],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_path_vpn_nop2p_manifest() -> ModuleManifest:
    """VPN-noP2P path module."""
    return ModuleManifest(
        id="path_vpn_nop2p",
        name="VPN-noP2P Path",
        version="1.0.0",
        type=ModuleType.PATH,
        description="Restricted VPN path (DE, PL) without P2P",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
            ModuleDependency("subscription:de5", True, "1.0.0", "Restricted subscription"),
        ],
        provides=["path:vpn_nop2p", "filter:base_filter"],
        owned_resources=[
            ModuleOwnedResource("config", "/etc/geomask/policies.yaml#path:vpn_nop2p"),
            ModuleOwnedResource("filter", "/etc/mihomo/ruleset/vpn_nop2p.yaml"),
        ],
        config_files=["/etc/geomask/policies.yaml"],
        services=["mihomo"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_path_ai_manifest() -> ModuleManifest:
    """AI path module with admission control."""
    return ModuleManifest(
        id="path_ai",
        name="AI Path",
        version="1.0.0",
        type=ModuleType.PATH,
        description="AI path with US-only admission control",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
            ModuleDependency("subscription:sub", True, "1.0.0", "AI borrows main subscription"),
            ModuleDependency("admission_service", True, "1.0.0", "Admission proof required"),
        ],
        provides=["path:ai", "filter:ai_filter"],
        owned_resources=[
            ModuleOwnedResource("config", "/etc/geomask/policies.yaml#path:ai"),
            ModuleOwnedResource("filter", "/etc/mihomo/ruleset/ai.yaml"),
        ],
        config_files=["/etc/geomask/policies.yaml"],
        services=["mihomo"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_diagnostic_manifest() -> ModuleManifest:
    """Diagnostic UI and services."""
    return ModuleManifest(
        id="diagnostic",
        name="Diagnostic Services",
        version="1.0.0",
        type=ModuleType.DIAGNOSTIC,
        description="Morning check, DNS check, VPN watch, admission proof service",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
        ],
        provides=["rpc:diag_explain", "rpc:admission_cache_get", "rpc:policy_preview"],
        owned_resources=[
            ModuleOwnedResource("file", "/usr/lib/geomask/diag_status.py"),
            ModuleOwnedResource("file", "/usr/lib/geomask/admission_service.py"),
            ModuleOwnedResource("file", "/usr/lib/geomask/policy_preview.py"),
            ModuleOwnedResource("cron", "morning-check"),
            ModuleOwnedResource("cron", "dnscheck"),
            ModuleOwnedResource("cron", "vpnwatch"),
        ],
        config_files=["/etc/geomask/policies.yaml"],
        services=["ai-doh", "dnsmasq-ai"],
        cron_jobs=["morning-check", "dnscheck", "vpnwatch"],
        rpc_methods=["diag_explain", "admission_cache_get", "policy_preview", "policy_get", "policy_apply"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_youtube_manifest() -> ModuleManifest:
    """YouTube edge watchdog module."""
    return ModuleManifest(
        id="youtube",
        name="YouTube Edge Watchdog",
        version="1.0.0",
        type=ModuleType.FEATURE,
        description="YouTube stalled-edge detection and pinning",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
            ModuleDependency("path_vpn", False, "1.0.0", "Optional: pins go to VPN"),
        ],
        provides=["ytwatch", "pinned_edges"],
        owned_resources=[
            ModuleOwnedResource("file", "/usr/bin/geomask-ytwatch.sh"),
            ModuleOwnedResource("file", "/usr/lib/geomask/geomask-ytwatch.conf"),
            ModuleOwnedResource("cron", "ytwatch"),
            ModuleOwnedResource("filter", "/etc/mihomo/ruleset/ytvpn.yaml"),
        ],
        config_files=["/etc/geomask-ytwatch.conf"],
        cron_jobs=["ytwatch"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_torrent_manifest() -> ModuleManifest:
    """Torrent admin module."""
    return ModuleManifest(
        id="torrent",
        name="Torrent Admin",
        version="1.0.0",
        type=ModuleType.FEATURE,
        description="Torrent tracker ruleset and DIRECT listener",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
            ModuleDependency("path_vpn", False, "1.0.0", "Optional: torrent over VPN"),
        ],
        provides=["torrent_rules", "torrent_listener"],
        owned_resources=[
            ModuleOwnedResource("file", "/usr/bin/geomask-torrent.sh"),
            ModuleOwnedResource("filter", "/etc/mihomo/ruleset/torrentdirect.yaml"),
            ModuleOwnedResource("filter", "/etc/mihomo/ruleset/torrentsites.yaml"),
        ],
        config_files=["/etc/geomask/torrent.conf"],
        cron_jobs=["torrent_refresh"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_device_modes_manifest() -> ModuleManifest:
    """Device modes module."""
    return ModuleManifest(
        id="device_modes",
        name="Device Modes",
        version="1.0.0",
        type=ModuleType.FEATURE,
        description="Per-device routing modes (AI, VPN, VPN-noP2P, Bypass, Direct)",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
        ],
        provides=["device_routing", "mode_switch"],
        owned_resources=[
            ModuleOwnedResource("file", "/etc/geo-devices.list"),
            ModuleOwnedResource("cron", "device_sync"),
            ModuleOwnedResource("nft", "geomask_excl"),
            ModuleOwnedResource("ip_rule", "table 100/101"),
        ],
        config_files=["/etc/geo-devices.list"],
        services=["dnsmasq"],
        cron_jobs=["device_sync"],
        rpc_methods=["dev_add", "dev_del", "device_get"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_ai_dns_manifest() -> ModuleManifest:
    """AI DNS module."""
    return ModuleManifest(
        id="ai_dns",
        name="AI DNS-over-HTTPS",
        version="1.0.0",
        type=ModuleType.FEATURE,
        description="AI-mode DNS via DoH through AI tunnel",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
            ModuleDependency("path_ai", True, "1.0.0", "Requires AI path for SOCKS5h"),
        ],
        provides=["ai_doh", "dnsmasq_ai"],
        owned_resources=[
            ModuleOwnedResource("service", "ai-doh"),
            ModuleOwnedResource("service", "dnsmasq-ai"),
            ModuleOwnedResource("file", "/etc/init.d/ai-doh"),
            ModuleOwnedResource("file", "/etc/init.d/dnsmasq-ai"),
            ModuleOwnedResource("nft", "geomask_ai_dns"),
        ],
        config_files=["/etc/init.d/ai-doh", "/etc/init.d/dnsmasq-ai"],
        services=["ai-doh", "dnsmasq-ai"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_backup_share_manifest() -> ModuleManifest:
    """Backup share module."""
    return ModuleManifest(
        id="backup_share",
        name="Backup SMB Share",
        version="1.0.0",
        type=ModuleType.SERVICE,
        description="SMB share for geomask backups (read-only)",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
        ],
        provides=["smb_share:geomask-backups"],
        owned_resources=[
            ModuleOwnedResource("service", "ksmbd"),
            ModuleOwnedResource("file", "/etc/ksmbd/ksmbd.conf"),
        ],
        config_files=["/etc/ksmbd/ksmbd.conf"],
        services=["ksmbd"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_self_update_manifest() -> ModuleManifest:
    """Self-update module."""
    return ModuleManifest(
        id="self_update",
        name="Self Update",
        version="1.0.0",
        type=ModuleType.SERVICE,
        description="Automatic update from GitHub releases",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
        ],
        provides=["updater"],
        owned_resources=[
            ModuleOwnedResource("file", "/tmp/geomask-upd/"),
            ModuleOwnedResource("cron", "upd_check"),
        ],
        config_files=[],
        cron_jobs=["upd_check"],
        rpc_methods=["upd_check", "upd_status", "upd_apply"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_admission_service_manifest() -> ModuleManifest:
    """Admission proof service."""
    return ModuleManifest(
        id="admission_service",
        name="AI Admission Service",
        version="1.0.0",
        type=ModuleType.SERVICE,
        description="AI node admission proof via multi-service verification",
        dependencies=[
            ModuleDependency("core", True, "1.0.0"),
        ],
        provides=["admission_proof", "admission_cache"],
        owned_resources=[
            ModuleOwnedResource("file", "/usr/lib/geomask/admission_service.py"),
            ModuleOwnedResource("cache", "/tmp/geomask-admission-cache.json"),
        ],
        config_files=[],
        services=[],
        rpc_methods=["admission_cache_get"],
        state=ModuleState.ENABLED,
        enabled=True,
    )


def create_all_manifests() -> Dict[str, ModuleManifest]:
    """Create all standard module manifests."""
    return {
        "core": create_core_manifest(),
        "path_vpn": create_path_vpn_manifest(),
        "path_vpn_nop2p": create_path_vpn_nop2p_manifest(),
        "path_ai": create_path_ai_manifest(),
        "diagnostic": create_diagnostic_manifest(),
        "youtube": create_youtube_manifest(),
        "torrent": create_torrent_manifest(),
        "device_modes": create_device_modes_manifest(),
        "ai_dns": create_ai_dns_manifest(),
        "backup_share": create_backup_share_manifest(),
        "self_update": create_self_update_manifest(),
        "admission_service": create_admission_service_manifest(),
    }


def save_all_manifests(registry_path: Path = Path('Y:/openwrt/geomask/modules')):
    """Save all standard manifests to registry."""
    manifests = create_all_manifests()
    registry_path = Path(registry_path)
    registry_path.mkdir(parents=True, exist_ok=True)
    
    for module_id, module in create_all_manifests().items():
        path = registry_path / f'{module_id}.json'
        module.save(path)
        print(f"Saved {module_id} to {path}")


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--save':
        save_all_manifests()
        print("All manifests saved to /etc/geomask/modules/")
    else:
        manifests = create_all_manifests()
        for mid, m in manifests.items():
            print(f"{mid}: {m.name} v{m.version} [{m.type.value}] state={m.state.value} deps={[d.module_id for d in m.dependencies]}")