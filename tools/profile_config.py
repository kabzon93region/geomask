#!/usr/bin/env python3
"""Profile configuration system for geomask installations."""
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from enum import Enum


class ProfileType(str, Enum):
    FULL = "full"                 # All modules enabled
    VPN_ONLY = "vpn_only"         # Only VPN path
    VPN_AI = "vpn_ai"             # VPN + AI paths
    CUSTOM = "custom"             # User-defined modules


@dataclass
class SSHConfig:
    host: str = "192.168.0.1"
    user: str = "root"
    key_path: str = "~/.ssh/openwrt_final"
    port: int = 22


@dataclass
class BackupConfig:
    local_dir: str = "Y:/openwrt/backup"
    remote_dir: str = "/tmp"
    keep_local: int = 10          # Number of backups to keep locally
    keep_remote: int = 1          # Number of backups to keep on router


@dataclass
class InstallProfile:
    name: str
    description: str
    modules: List[str]            # Module IDs to enable
    config_overrides: Dict[str, Any] = field(default_factory=dict)


# --- Predefined Profiles ---

PREDEFINED_PROFILES = {
    "full": InstallProfile(
        name="full",
        description="Full installation with all modules (VPN, VPN-noP2P, AI, YouTube, Torrent, Device Modes, AI-DNS, Diagnostic, Backup Share, Self-Update, Admission Service)",
        modules=[
            "core",
            "path_vpn",
            "path_vpn_nop2p",
            "path_ai",
            "diagnostic",
            "youtube",
            "torrent",
            "device_modes",
            "ai_dns",
            "backup_share",
            "self_update",
            "admission_service",
        ],
    ),
    "vpn_only": InstallProfile(
        name="vpn_only",
        description="Minimal VPN-only installation (VPN path only, no AI, no YouTube, no Torrent)",
        modules=[
            "core",
            "path_vpn",
        ],
    ),
    "vpn_ai": InstallProfile(
        name="vpn_ai",
        description="VPN + AI paths (VPN, VPN-noP2P, AI with admission, Diagnostic)",
        modules=[
            "core",
            "path_vpn",
            "path_vpn_nop2p",
            "path_ai",
            "diagnostic",
            "admission_service",
        ],
    ),
    "minimal": InstallProfile(
        name="minimal",
        description="Minimal core only (for development/testing)",
        modules=[
            "core",
        ],
    ),
}


@dataclass
class DeployConfig:
    """Complete deployment configuration."""
    profile: str = "full"
    ssh: SSHConfig = field(default_factory=SSHConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    router_address: str = "192.168.0.1"
    custom_modules: List[str] = field(default_factory=list)
    config_overrides: Dict[str, Any] = field(default_factory=dict)
    skip_backup: bool = False
    dry_run: bool = False
    
    def get_profile(self) -> InstallProfile:
        if self.profile in PREDEFINED_PROFILES:
            return PREDEFINED_PROFILES[self.profile]
        elif self.profile == "custom":
            return InstallProfile(
                name="custom",
                description="Custom module selection",
                modules=self.custom_modules,
            )
        else:
            raise ValueError(f"Unknown profile: {self.profile}")
    
    def get_enabled_modules(self) -> List[str]:
        return self.get_profile().modules
    
    def to_dict(self) -> dict:
        return {
            'profile': self.profile,
            'ssh': asdict(self.ssh),
            'backup': asdict(self.backup),
            'router_address': self.router_address,
            'custom_modules': self.custom_modules,
            'config_overrides': self.config_overrides,
            'skip_backup': self.skip_backup,
            'dry_run': self.dry_run,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'DeployConfig':
        ssh = SSHConfig(**data.get('ssh', {}))
        backup = BackupConfig(**data.get('backup', {}))
        return cls(
            profile=data.get('profile', 'full'),
            ssh=ssh,
            backup=backup,
            router_address=data.get('router_address', '192.168.0.1'),
            custom_modules=data.get('custom_modules', []),
            config_overrides=data.get('config_overrides', {}),
            skip_backup=data.get('skip_backup', False),
            dry_run=data.get('dry_run', False),
        )


def save_deploy_config(config: DeployConfig, path: Path):
    path.write_text(json.dumps(config.to_dict(), indent=2, ensure_ascii=False), encoding='utf-8')


def load_deploy_config(path: Path) -> DeployConfig:
    if not path.exists():
        return DeployConfig()
    return DeployConfig.from_dict(json.loads(path.read_text(encoding='utf-8')))


# CLI for profile management
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Geomask Profile Manager")
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    # List profiles
    subparsers.add_parser('list', help='List available profiles')
    
    # Show profile
    show = subparsers.add_parser('show', help='Show profile details')
    show.add_argument('profile', help='Profile name')
    
    # Create config
    deploy = subparsers.add_parser('deploy-config', help='Create deployment config')
    deploy.add_argument('--profile', choices=list(PREDEFINED_PROFILES.keys()) + ['custom'], default='full')
    deploy.add_argument('--output-file', help='Output config file')
    deploy.add_argument('--router', default='192.168.0.1')
    deploy.add_argument('--ssh-key', default='~/.ssh/openwrt_final')
    deploy.add_argument('--ssh-host', default='192.168.0.1')
    deploy.add_argument('--ssh-user', default='root')
    deploy.add_argument('--backup-dir', default='Y:/openwrt/backup')
    deploy.add_argument('--modules', nargs='+', help='Custom modules (for custom profile)')
    deploy.add_argument('--dry-run', action='store_true')
    deploy.add_argument('--skip-backup', action='store_true')
    deploy.add_argument('--output', required=True)
    
    args = parser.parse_args()
    
    if args.command == 'list':
        for name, profile in PREDEFINED_PROFILES.items():
            print(f"{name:12} - {profile.description}")
            print(f"  Modules: {', '.join(profile.modules)}")
            print()
    
    elif args.command == 'show':
        profile = PREDEFINED_PROFILES.get(args.profile)
        if not profile:
            print(f"Unknown profile: {args.profile}")
            return 1
        print(json.dumps(asdict(profile), indent=2, ensure_ascii=False))
    
    elif args.command == 'deploy-config':
        if args.profile == 'custom' and not args.modules:
            print("Custom profile requires --modules")
            return 1
        
        config = DeployConfig(
            profile=args.profile,
            router_address=args.router,
            ssh=SSHConfig(host=args.ssh_host, user=args.ssh_user, key_path=args.ssh_key),
            backup=BackupConfig(local_dir=args.backup_dir),
            custom_modules=args.modules or [],
            skip_backup=args.skip_backup,
            dry_run=args.dry_run,
        )
        output_path = Path(args.output_file)
        save_deploy_config(config, output_path)
        print(f"Deploy config saved to {output_path}")
    
    else:
        parser.print_help()


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    main()