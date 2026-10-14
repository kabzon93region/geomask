# fetch_backup.ps1 - download a router backup into the workspace backup folder
param(
    [string]$Remote = "router-backup-20261014-213247.tar.gz",
    [string]$Tag = "pre-rules-fix"
)
$Key = "$env:USERPROFILE\.ssh\openwrt_final"
$Router = "root@192.168.0.1"
$Local = "$env:TEMP\bk_$Remote"
$Dst = "Y:\openwrt\backup\router-backup-$Tag.tar.gz"
Write-Host "[1/3] scp $Remote"
scp -i $Key -o StrictHostKeyChecking=no -o ConnectTimeout=10 "${Router}:/tmp/bk.tar.gz" $Local
if (-not (Test-Path $Local)) { Write-Host "FAIL: download"; exit 1 }
Write-Host "[2/3] local size: $((Get-Item $Local).Length)"
Copy-Item $Local $Dst -Force
Write-Host "[3/3] saved: $Dst ($((Get-Item $Dst).Length) bytes)"
