# fetch_backup.ps1 - pull a router backup (geomask --backup / self-update) to the PC
# Finds the newest /tmp/router-backup-*.tar.gz on the router (or use -Remote path),
# scp + md5-verify, saves into Y:\openwrt\backup\router-backup-<ts>.tar.gz.
# ASCII only (R1). Usage:  powershell -File fetch_backup.ps1 [-Remote /tmp/router-backup-20261027-115157.tar.gz] [-Keep]
param(
    [string]$Remote = '',
    [switch]$Keep          # leave the copy on the router (default: remove after md5 verified)
)
$ErrorActionPreference = 'Stop'
$Key = "$env:USERPROFILE\.ssh\openwrt_final"
$Router = 'root@192.168.0.1'
$DstDir = 'Y:\openwrt\backup'
function Sh([string]$cmd) {
    ssh -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 $Router $cmd
}
Write-Host '[1/4] find newest backup on router'
if (-not $Remote) {
    $Remote = (Sh 'ls -t /tmp/router-backup-*.tar.gz 2>/dev/null | head -1') | Select-Object -First 1
}
if (-not $Remote) { Write-Host 'FAIL: no /tmp/router-backup-*.tar.gz on router (reboot wipes /tmp; create one: geomask --backup or panel update)'; exit 1 }
$name = $Remote.Split('/')[-1]
Write-Host "  remote: $Remote"
Write-Host '[2/4] scp'
$local = Join-Path $env:TEMP "fetchbk_$name"
Remove-Item $local -ErrorAction SilentlyContinue
scp -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "${Router}:$Remote" $local | Out-Null
if (-not (Test-Path $local)) { Write-Host 'FAIL: scp download'; exit 1 }
Write-Host '[3/4] md5 compare'
$rm = ((Sh "md5sum $Remote") -split ' ')[0]
$lm = (Get-FileHash $local -Algorithm MD5).Hash.ToLower()
if ($rm -ne $lm) { Write-Host "FAIL: md5 mismatch router=$rm local=$lm (file NOT saved)"; exit 1 }
$dst = Join-Path $DstDir $name
Copy-Item $local $dst -Force
if (-not $Keep) { Sh "rm -f $Remote" | Out-Null; Write-Host '  router copy removed (policy: keep backups on PC only)' }
Remove-Item $local -Force
Write-Host "[4/4] OK: $dst ($((Get-Item $dst).Length) bytes, md5 $lm)"
Write-Host 'restore: LuCI System -> Backup / Flash Firmware -> Restore (upload this file from PC)'
