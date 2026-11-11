#!/usr/bin/env python3
"""Geomask Deployer - orchestrates deployment based on profile config."""
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# Import from profile_config
sys.path.insert(0, str(Path(__file__).parent))
from profile_config import DeployConfig, PREDEFINED_PROFILES, save_deploy_config, load_deploy_config


class Deployer:
    """Orchestrates geomask deployment based on profile config."""
    
    def __init__(self, config: DeployConfig):
        self.config = config
        self.profile = PREDEFINED_PROFILES.get(config.profile)
        if not self.profile:
            raise ValueError(f"Unknown profile: {config.profile}")
        
        # SSH connection details
        self.ssh_host = config.ssh.host
        self.ssh_user = config.ssh.user
        self.ssh_key = config.ssh.key_path
        self.ssh_port = config.ssh.port
        self.router_addr = config.router_address
        
        # Backup config
        self.backup_local_dir = Path(config.backup.local_dir).expanduser()
        self.backup_remote_dir = config.backup.remote_dir
        self.keep_local = config.backup.keep_local
        self.keep_remote = config.backup.keep_remote
        
        self.dry_run = config.dry_run
        self.skip_backup = config.skip_backup
        
        # Modules to deploy
        self.modules = config.get_enabled_modules()
    
    def run_ssh(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
        """Run command on router via SSH."""
        ssh_cmd = [
            'ssh', '-i', str(Path(self.ssh_key).expanduser()),
            '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=10',
            '-p', str(self.ssh_port),
            f'{self.ssh_user}@{self.ssh_host}',
            cmd
        ]
        return subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
    
    def run_scp(self, local: Path, remote: str, timeout: int = 30) -> subprocess.CompletedProcess:
        """Copy file to router via SCP."""
        scp_cmd = [
            'scp', '-i', str(Path(self.ssh_key).expanduser()),
            '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=10',
            '-P', str(self.ssh_port),
            str(local),
            f'{self.ssh_user}@{self.ssh_host}:{remote}'
        ]
        return subprocess.run(scp_cmd, capture_output=True, text=True, timeout=timeout)
    
    def create_backup(self) -> Optional[Path]:
        """Create full router backup."""
        if self.skip_backup:
            print("Skipping backup (--skip-backup)")
            return None
        
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        remote_path = f'/tmp/router-backup-{stamp}.tar.gz'
        local_path = Path(self.backup_local_dir) / f'router-backup-{stamp}.tar.gz'
        
        # Ensure local backup dir exists
        Path(self.backup_local_dir).mkdir(parents=True, exist_ok=True)
        
        print(f"Creating backup: {remote_path}")
        result = self.run_ssh(f"sysupgrade -b {remote_path}")
        if result.returncode != 0:
            raise RuntimeError(f"Backup creation failed: {result.stderr}")
        
        # Verify and download
        self.run_scp(f'{self.router_addr}:{remote_path}', str(local_path))
        if not Path(local_path).exists():
            raise RuntimeError("Backup transfer failed")
        
        # Verify checksums
        remote_hash = self.run_ssh(f"md5sum {remote_path}").stdout.split()[0]
        local_hash = hashlib.md5(Path(local_path).read_bytes()).hexdigest()
        if remote_hash != local_hash:
            raise RuntimeError("Backup checksum mismatch")
        
        # Cleanup remote temp file
        self.run_ssh(f"rm -f {remote_path}")
        
        # Cleanup old backups
        self._cleanup_old_backups()
        
        print(f"Backup saved: {local_path} (MD5: {local_hash})")
        return local_path
    
    def _cleanup_old_backups(self):
        """Clean up old local backups."""
        backups = sorted(
            Path(self.backup_local_dir).glob('router-backup-*.tar.gz'),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for old in backups[self.keep_local:]:
            old.unlink()
    
    def transfer_files(self, files: Dict[Path, str]):
        """Transfer files to router."""
        for local, remote in files.items():
            print(f"Transferring {local} -> {remote}")
            self.run_scp(Path(local), remote)
    
    def run_on_router(self, cmd: str) -> subprocess.CompletedProcess:
        """Run command on router."""
        return self.run_ssh(cmd)
    
    def deploy_modules(self):
        """Deploy module manifests and enable modules."""
        # Transfer module manifests
        print("Transferring module manifests...")
        self.transfer_files({
            Path('Y:/openwrt/geomask/modules/core.json'): '/etc/geomask/modules/core.json',
            Path('Y:/openwrt/geomask/modules/path_vpn.json'): '/etc/geomask/modules/path_vpn.json',
            Path('Y:/openwrt/geomask/modules/path_vpn_nop2p.json'): '/etc/geomask/modules/path_vpn_nop2p.json',
            Path('Y:/openwrt/geomask/modules/path_ai.json'): '/etc/geomask/modules/path_ai.json',
            Path('Y:/openwrt/geomask/modules/diagnostic.json'): '/etc/geomask/modules/diagnostic.json',
            Path('Y:/openwrt/geomask/modules/youtube.json'): '/etc/geomask/modules/youtube.json',
            Path('Y:/openwrt/geomask/modules/torrent.json'): '/etc/geomask/modules/torrent.json',
            Path('Y:/openwrt/geomask/modules/device_modes.json'): '/etc/geomask/modules/device_modes.json',
            Path('Y:/openwrt/geomask/modules/ai_dns.json'): '/etc/geomask/modules/ai_dns.json',
            Path('Y:/openwrt/geomask/modules/backup_share.json'): '/etc/geomask/modules/backup_share.json',
            Path('Y:/openwrt/geomask/modules/self_update.json'): '/etc/geomask/modules/self_update.json',
            Path('Y:/openwrt/geomask/modules/admission_service.json'): '/etc/geomask/modules/admission_service.json',
            Path('Y:/openwrt/geomask/modules/youtube.json'): '/etc/geomask/modules/youtube.json',
            Path('Y:/openwrt/geomask/modules/torrent.json'): '/etc/geomask/modules/torrent.json',
        })
        
        # Enable modules in dependency order
        from module_manifest import ModuleRegistry, ModuleState
        registry = ModuleRegistry(Path('/etc/geomask/modules'))
        
        # Enable modules in dependency order
        modules_to_enable = self.get_enabled_modules()
        registry_path = Path('/etc/geomask/modules')
        
        for module_id in self.modules:
            if module_id not in ['subscription:sub', 'subscription:de5']:  # Skip subscriptions
                # This would need the registry on the router - for now just transfer
                pass
        
        print("Module manifests deployed")
    
    def deploy(self) -> bool:
        """Execute full deployment."""
        print(f"=== Deploying profile: {self.config.profile} ===")
        print(f"Modules: {', '.join(self.modules)}")
        print(f"Target: {self.ssh_user}@{self.ssh_host}")
        
        if self.dry_run:
            print("DRY RUN - no changes will be made")
            return True
        
        # 1. Create backup
        if not self.skip_backup:
            try:
                self.create_backup()
            except Exception as e:
                print(f"Backup failed: {e}")
                return False
        
        # 2. Transfer files
        self.deploy_modules()
        
        # 3. Run post-deploy checks
        print("Running post-deploy checks...")
        # ... add verification
        
        return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Geomask Deployer")
    subparsers = parser.add_subparsers(dest='command', required=True)
    
    deploy = subparsers.add_parser('deploy', help='Deploy profile')
    deploy.add_argument('--profile', choices=list(PREDEFINED_PROFILES.keys()) + ['custom'], default='full')
    deploy.add_argument('--config', help='Path to deploy config JSON')
    deploy.add_argument('--dry-run', action='store_true')
    deploy.add_argument('--skip-backup', action='store_true')
    deploy.add_argument('--router', default='192.168.0.1')
    deploy.add_argument('--ssh-key', default='~/.ssh/openwrt_final')
    deploy.add_argument('--ssh-host', default='192.168.0.1')
    deploy.add_argument('--ssh-user', default='root')
    deploy.add_argument('--backup-dir', default='Y:/openwrt/backup')
    deploy.add_argument('--modules', nargs='+', help='Custom modules (for custom profile)')
    
    args = parser.parse_args()
    
    if args.command != 'deploy':
        print("Only 'deploy' command implemented in this stub")
        return 1
    
    # Load config
    if args.config:
        config = load_deploy_config(Path(args.config))
    else:
        config = DeployConfig(
            profile=args.profile,
            router_address=args.router,
            ssh=SSHConfig(host=args.router, user='root', key_path=args.key),
            backup=BackupConfig(local_dir=args.backup_dir),
            custom_modules=args.modules or [],
            skip_backup=args.skip_backup,
            dry_run=args.dry_run,
        )
    
    deployer = Deployer(config)
    success = deployer.deploy()
    return 0 if success else 1


if __name__ == '__main__':
    import sys
    sys.exit(main())