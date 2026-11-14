# fetch_backup.ps1 - pull a router backup (geomask --backup / self-update) to the PC
# Finds the newest router-backup-*.tar.gz on the router (or takes -Remote), copies it to
# -DstDir and verifies the checksum of the FINAL local file (md5, or sha256 when the
# router has no md5sum). Host key checking is always kept ON (never disabled); the
# router-side copy is kept unless -RemoveRemote is given explicitly.
# ASCII only (R1).
# Usage:
#   powershell -File fetch_backup.ps1
#   powershell -File fetch_backup.ps1 -Remote /root/geomask-backups/router-backup-20261114-101500.tar.gz
#   powershell -File fetch_backup.ps1 -KnownHosts "$env:USERPROFILE\.ssh\known_hosts_geomask" -RemoveRemote
param(
    [string]$Router = 'root@192.168.0.1',
    [string]$Key = "$env:USERPROFILE\.ssh\openwrt_final",
    [string]$DstDir = 'Y:\openwrt\backup',
    [string]$Remote = '',
    [string]$KnownHosts = '',
    [switch]$RemoveRemote
)
$ErrorActionPreference = 'Stop'

# Only these remote names are accepted; no spaces, no shell metacharacters, no traversal.
$RemoteRx = '^/(root/geomask-backups|tmp)/[A-Za-z0-9._-]+\.tar\.gz$'
# host or user@host (ssh config alias allowed): the first character must not be '-', so an
# option such as -Fnone/-oProxyCommand can never reach ssh/scp as a flag.
$RouterRx = '^([A-Za-z0-9._-]+@)?[A-Za-z0-9._:\[\]%][A-Za-z0-9._:\[\]%\-]*$'

$script:TmpPath = ''

function Fail([string]$Message) {
    Write-Host "FAIL: $Message"
    if ($script:TmpPath -and (Test-Path -LiteralPath $script:TmpPath)) {
        Remove-Item -LiteralPath $script:TmpPath -Force -ErrorAction SilentlyContinue
    }
    exit 1
}

# Single-quote a value for the remote POSIX shell (validated paths never contain quotes).
function QuoteSh([string]$Value) {
    return "'" + $Value.Replace("'", "'\''") + "'"
}

# ssh/scp options shared by every call: key, no interactive prompts, host keys verified.
$SshOpts = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10')
if ($KnownHosts) {
    if (-not (Test-Path -LiteralPath $KnownHosts)) {
        Fail "-KnownHosts file not found: $KnownHosts"
    }
    $KhPath = (Resolve-Path -LiteralPath $KnownHosts).Path
    $SshOpts = @('-o', "UserKnownHostsFile=$KhPath", '-o', 'StrictHostKeyChecking=yes') + $SshOpts
}

function Sh([string]$Command) {
    & ssh -i $Key @SshOpts $Router $Command
}

Write-Host '[1/5] checks and remote path'
if (-not (Test-Path -LiteralPath $Key)) { Fail "ssh key not found: $Key" }
foreach ($tool in @('ssh', 'scp')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Fail "$tool not found in PATH (OpenSSH client required)"
    }
}
if ($Router -notmatch $RouterRx) { Fail "invalid -Router (expected user@host): $Router" }
if ($Remote) {
    if ($Remote -notmatch $RemoteRx) { Fail "invalid -Remote, expected $RemoteRx : $Remote" }
    Write-Host "  remote: $Remote (from -Remote)"
} else {
    $FindCmd = "ls -1t '/root/geomask-backups/'router-backup-*.tar.gz '/tmp/'router-backup-*.tar.gz 2>/dev/null | head -1"
    foreach ($line in @(Sh $FindCmd)) {
        $probe = ([string]$line).Trim()
        if ($probe) { $Remote = $probe; break }
    }
    if (-not $Remote) { Fail 'no router-backup-*.tar.gz on router (panel creates it on update / geomask --backup)' }
    if ($Remote -notmatch $RemoteRx) { Fail "router returned an unexpected path: $Remote" }
    Write-Host "  remote: $Remote (newest found on router)"
}
$name = $Remote.Split('/')[-1]
$dst = Join-Path $DstDir $name
Write-Host "  local:  $dst"

Write-Host '[2/5] scp download to temp'
if (-not (Test-Path -LiteralPath $DstDir)) { New-Item -ItemType Directory -Path $DstDir -Force | Out-Null }
$script:TmpPath = Join-Path $env:TEMP "fetchbk_$name"
if (Test-Path -LiteralPath $script:TmpPath) { Remove-Item -LiteralPath $script:TmpPath -Force }
& scp -i $Key @SshOpts -- "${Router}:$Remote" "$($script:TmpPath)"
if ($LASTEXITCODE -ne 0) { Fail "scp failed (exit $LASTEXITCODE)" }
if (-not (Test-Path -LiteralPath $script:TmpPath)) { Fail 'scp did not create the local temp file' }

Write-Host '[3/5] copy to destination'
Copy-Item -LiteralPath $script:TmpPath -Destination $dst -Force
if (-not (Test-Path -LiteralPath $dst)) { Fail "copy to destination failed: $dst" }

Write-Host '[4/5] checksum of the FINAL local file'
$q = QuoteSh $Remote
$HashCmd = "if command -v md5sum >/dev/null 2>&1; then md5sum $q; else sha256sum $q; fi"
$remoteHash = ''
$algo = ''
foreach ($line in @(Sh $HashCmd)) {
    $token = (([string]$line).Trim() -split '\s+')[0]
    if ($token -match '^[0-9a-fA-F]{32}$') { $remoteHash = $token.ToLower(); $algo = 'MD5'; break }
    if ($token -match '^[0-9a-fA-F]{64}$') { $remoteHash = $token.ToLower(); $algo = 'SHA256'; break }
}
if ($LASTEXITCODE -ne 0 -or -not $remoteHash) {
    Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue
    Fail "cannot read the remote checksum (exit $LASTEXITCODE); unverified file removed"
}
$localHash = (Get-FileHash -LiteralPath $dst -Algorithm $algo).Hash.ToLower()
if ($localHash -ne $remoteHash) {
    Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $dst) { Fail "$($algo.ToLower()) mismatch router=$remoteHash local=$localHash; file could NOT be removed: $dst" }
    Fail "$($algo.ToLower()) mismatch router=$remoteHash local=$localHash (file NOT saved)"
}

Write-Host '[5/5] remote copy and result'
if ($RemoveRemote) {
    $null = Sh "rm -f $q"
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  WARN: remote copy was NOT removed (rm failed); delete it manually'
    } else {
        Write-Host '  router copy removed (-RemoveRemote)'
    }
} else {
    Write-Host '  router copy kept (pass -RemoveRemote to delete it after verification)'
}
Remove-Item -LiteralPath $script:TmpPath -Force -ErrorAction SilentlyContinue
$script:TmpPath = ''
$size = (Get-Item -LiteralPath $dst).Length
Write-Host "OK: $dst ($size bytes, $($algo.ToLower()) $remoteHash)"
Write-Host 'restore: LuCI System -> Backup / Flash Firmware -> Restore (upload this file from PC)'
exit 0
