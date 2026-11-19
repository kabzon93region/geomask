# run-canary.ps1 - canary run of a geomask build on the live router with a PC-side watchdog
#
# What it does (see docs/stand/ROUTER-CANARY-PLAN.md):
#   1. verifies the router, makes a sysupgrade backup and an overlay bundle, pulls both to
#      the PC and compares sha256 on both sides;
#   2. rehearses the restore into /tmp on the router and compares key config files;
#   3. uploads probe.sh, restore.sh and the new geomask.sh, runs a read-only --check;
#   4. with -Execute: runs the install (--auto --yes --restart) and monitors every
#      IntervalSec seconds: router reachability, SSH probe, internet from the PC through the
#      router, DNS through the router, mihomo, TUNs, ip rules, router egress;
#   5. after FailThreshold consecutive unhealthy rounds it restores both archives, removes
#      geomask files the archives did not contain, reboots, waits BootWaitSec and checks again.
#
# Without -Execute the script stops after the rehearsal and prints "PLAN ONLY": it never
# installs anything. ASCII only (project rule for scripts).
#
# Usage:
#   powershell -File tools\canary\run-canary.ps1                       # backup + rehearsal only
#   powershell -File tools\canary\run-canary.ps1 -Execute              # full canary run
#   powershell -File tools\canary\run-canary.ps1 -Execute -NoRollback  # monitor only

[CmdletBinding()]
param(
    [string]$Router = 'root@192.168.0.1',
    [string]$Key = "$env:USERPROFILE\.ssh\openwrt_final",
    [string]$Installer = 'Y:\openwrt\geomask\geomask.sh',
    [string]$OutRoot = 'Y:\openwrt\backup',
    [int]$IntervalSec = 60,
    [int]$FailThreshold = 3,
    [int]$GraceSec = 180,
    [int]$BootWaitSec = 120,
    [int]$HealthyRounds = 10,
    [switch]$Execute,
    [switch]$NoRollback
)

$ErrorActionPreference = 'Stop'
$script:SshPath = (Get-Command ssh.exe -ErrorAction SilentlyContinue).Source
if (-not $script:SshPath) { $script:SshPath = 'C:\Windows\System32\OpenSSH\ssh.exe' }
$script:ScpPath = Join-Path (Split-Path $script:SshPath) 'scp.exe'
$script:RunDir = $null
$script:LogFile = $null
$script:RouterIp = ($Router -replace '^.*@', '') -replace '[\[\]]', ''
$script:InstallerSha = ''
$script:Archives = @{}

function Log {
    param([string]$Level, [string]$Message)
    $line = '{0} {1,-5} {2}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $Level, $Message
    Write-Host $line
    if ($script:LogFile) { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding ASCII }
}

function Stop-Run {
    param([string]$Message, [int]$Code = 1)
    Log 'FAIL' $Message
    throw [System.Exception]::new($Message + ' [exit ' + $Code + ']')
}

function Invoke-Ssh {
    param([string]$Command, [int]$TimeoutSec = 30)
    $sshArgs = @('-i', $Key,
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ConnectTimeout=8',
        '-o', 'ServerAliveInterval=5',
        '-o', 'ServerAliveCountMax=2',
        $Router, $Command)
    # PS 5.1: with ErrorActionPreference=Stop a native command's stderr becomes a terminating
    # error and truncates the output (sysupgrade writes progress to stderr). Keep it non-fatal.
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & $script:SshPath @sshArgs 2>&1 | Out-String
    $code = $LASTEXITCODE
    $ErrorActionPreference = $oldEap
    return [pscustomobject]@{ Rc = $code; Out = $out.Trim() }
}

function Invoke-ScpToRouter {
    param([string]$Source, [string]$Target)
    $scpArgs = @('-i', $Key, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        $Source, ($Router + ':' + $Target))
    # PS 5.1: with ErrorActionPreference=Stop a native command's stderr becomes a terminating
    # error and truncates the output (sysupgrade writes progress to stderr). Keep it non-fatal.
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & $script:ScpPath @scpArgs 2>&1 | Out-String
    $code = $LASTEXITCODE
    $ErrorActionPreference = $oldEap
    return [pscustomobject]@{ Rc = $code; Out = $out.Trim() }
}

function Invoke-ScpFromRouter {
    param([string]$Source, [string]$Target)
    $scpArgs = @('-i', $Key, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        ($Router + ':' + $Source), $Target)
    # PS 5.1: with ErrorActionPreference=Stop a native command's stderr becomes a terminating
    # error and truncates the output (sysupgrade writes progress to stderr). Keep it non-fatal.
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & $script:ScpPath @scpArgs 2>&1 | Out-String
    $code = $LASTEXITCODE
    $ErrorActionPreference = $oldEap
    return [pscustomobject]@{ Rc = $code; Out = $out.Trim() }
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-Ping {
    try { return (Test-Connection -ComputerName $script:RouterIp -Count 1 -Quiet -ErrorAction Stop) }
    catch { return $false }
}

function Test-InternetFromPc {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://connectivitycheck.gstatic.com/generate_204' -TimeoutSec 6
        return ([int]$r.StatusCode -eq 204)
    } catch { return $false }
}

function Test-DnsViaRouter {
    try {
        if (Get-Command Resolve-DnsName -ErrorAction SilentlyContinue) {
            $r = Resolve-DnsName -Name 'example.com' -Server $script:RouterIp -DnsOnly -ErrorAction Stop
            return ($null -ne $r)
        }
        $out = & nslookup.exe example.com $script:RouterIp 2>&1 | Out-String
        return ($out -match 'Address(es)?:\s+\d+\.\d+\.\d+\.\d+')
    } catch { return $false }
}

function Get-Probe {
    $r = Invoke-Ssh 'sh /tmp/canary/probe.sh' 25
    $values = @{}
    if ($r.Rc -eq 0) {
        foreach ($line in ($r.Out -split "`r?`n")) {
            if ($line -match '^([A-Za-z0-9_]+)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
        }
    }
    return [pscustomobject]@{ Ok = ($r.Rc -eq 0); Values = $values; Raw = $r.Out }
}

function New-RunDir {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $dir = Join-Path $OutRoot ('canary-' + $stamp)
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $script:RunDir = $dir
    $script:LogFile = Join-Path $dir 'monitor.log'
    Log 'INFO' ('run dir: ' + $dir)
}

function Assert-LocalInputs {
    if (-not (Test-Path -LiteralPath $Key)) { Stop-Run ('ssh key not found: ' + $Key) }
    if (-not (Test-Path -LiteralPath $script:ScpPath)) { Stop-Run ('scp.exe not found: ' + $script:ScpPath) }
    if (-not (Test-Path -LiteralPath $Installer)) { Stop-Run ('installer not found: ' + $Installer) }
    if ($Router -notmatch '^([A-Za-z0-9._-]+@)?[A-Za-z0-9._:\[\]%-]+$') { Stop-Run ('refusing router value: ' + $Router) }
    $script:InstallerSha = Get-Sha256 $Installer
    Log 'INFO' ('installer sha256: ' + $script:InstallerSha)
    $r = Invoke-Ssh 'cat /etc/openwrt_release 2>/dev/null | head -4; echo ---; df -h / /tmp 2>/dev/null | tail -3'
    if ($r.Rc -ne 0) { Stop-Run ('ssh to router failed: ' + $r.Out) }
    Log 'INFO' ('router: ' + ($r.Out -replace "`r?`n", ' | '))
}

function Test-OverlayPath {
    param([string]$Path)
    return ($Path -notlike '/etc/config/*' -and $Path -notlike '/tmp/*' -and $Path -notlike '/proc/*')
}

function New-Backup {
    Log 'INFO' 'backup: sysupgrade + overlay bundle'
    # /root is in this router's keep-list (geomask parks old copies for the same reason), so
    # a backup written straight into /root/geomask-backups would include itself while it is
    # being created. Create in /tmp, then move; park our previous canary archives first.
    $cmd = 'mkdir -p /root/geomask-backups /tmp/canary/park; ' +
        'mv /root/geomask-backups/canary-*.tar.gz /tmp/canary/park/ 2>/dev/null; ' +
        'echo ok'
    $r = Invoke-Ssh $cmd 30
    if ($r.Rc -ne 0) { Stop-Run 'cannot prepare /root/geomask-backups and /tmp/canary' }
    $r = Invoke-Ssh 'sysupgrade -b /tmp/canary/sysupgrade.tar.gz; echo RC=$?'
    if ($r.Out -notmatch 'RC=0') { Stop-Run ('sysupgrade -b failed: ' + $r.Out) }

    $manifest = Join-Path $PSScriptRoot '..\..\tests\reinstall_write_set.json'
    if (-not (Test-Path -LiteralPath $manifest)) { Stop-Run ('write-set manifest not found: ' + $manifest) }
    $data = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    $paths = New-Object System.Collections.Generic.List[string]
    foreach ($entry in $data.files) {
        $p = [string]$entry.path
        if ($p -and (Test-OverlayPath $p)) { $paths.Add($p) }
    }
    foreach ($extra in @('/etc/mihomo', '/etc/geomask', '/etc/crontabs/root', '/root/sync_mtrickle_mihomo.sh')) {
        if (-not $paths.Contains($extra)) { $paths.Add($extra) }
    }
    $localList = Join-Path $script:RunDir 'bundle-paths.txt'
    # LF, not CRLF: the router's read loop keeps a trailing CR in the path and finds nothing.
    $text = ($paths -join "`n") + "`n"
    [System.IO.File]::WriteAllText($localList, $text, (New-Object System.Text.ASCIIEncoding))
    $r = Invoke-ScpToRouter $localList '/tmp/canary/bundle-paths.txt'
    if ($r.Rc -ne 0) { Stop-Run ('scp of the bundle path list failed: ' + $r.Out) }

    $remote = 'while IFS= read -r p; do [ -e "$p" ] && printf ''%s\n'' "$p"; done < /tmp/canary/bundle-paths.txt > /tmp/canary/bundle.list; ' +
        'wc -l < /tmp/canary/bundle.list; ' +
        'tar czf /tmp/canary/overlay.tar.gz -C / -T /tmp/canary/bundle.list 2>/dev/null; echo RC=$?'
    $r = Invoke-Ssh $remote 180
    if ($r.Rc -ne 0 -or $r.Out -notmatch 'RC=0') { Stop-Run ('overlay bundle failed: ' + $r.Out) }
    Log 'INFO' ('overlay bundle: ' + (($r.Out -split "`r?`n")[0]) + ' paths')

    $cmd = 'mv -f /tmp/canary/sysupgrade.tar.gz /root/geomask-backups/canary-sysupgrade.tar.gz && ' +
        'mv -f /tmp/canary/overlay.tar.gz /root/geomask-backups/canary-overlay.tar.gz && ' +
        'cd /root/geomask-backups && sha256sum canary-sysupgrade.tar.gz canary-overlay.tar.gz && ' +
        'tar tzf canary-sysupgrade.tar.gz > /tmp/canary/sys.list && tar tzf canary-overlay.tar.gz > /tmp/canary/ovl.list && ' +
        'cat /tmp/canary/sys.list /tmp/canary/ovl.list | grep -v "/$" | sed "s#^/*##" | sed "s#^#/#" | sort -u > canary-restore-set.txt && ' +
        'wc -l < canary-restore-set.txt'
    $r = Invoke-Ssh $cmd 120
    if ($r.Rc -ne 0) { Stop-Run ('listing/sha256 failed: ' + $r.Out) }
    $lines = @($r.Out -split "`r?`n")
    $remoteSha = @{}
    foreach ($line in $lines) {
        if ($line -match '^([0-9a-f]{64})\s+(\S+)$') { $remoteSha[$Matches[2]] = $Matches[1] }
    }
    if (-not $remoteSha.ContainsKey('canary-sysupgrade.tar.gz')) { Stop-Run ('no remote sha256: ' + $r.Out) }
    $r = Invoke-Ssh 'tar tzf /root/geomask-backups/canary-sysupgrade.tar.gz | grep -c "canary-.*\.tar\.gz" || true'
    if (($r.Out.Trim()) -ne '0') { Stop-Run ('the sysupgrade archive contains a canary archive (nesting): ' + $r.Out) }
    $script:Archives['sysupgrade'] = 'canary-sysupgrade.tar.gz'
    $script:Archives['overlay'] = 'canary-overlay.tar.gz'
    $script:Archives['sha'] = $remoteSha

    foreach ($name in @('canary-sysupgrade.tar.gz', 'canary-overlay.tar.gz', 'canary-restore-set.txt')) {
        $local = Join-Path $script:RunDir $name
        $r = Invoke-ScpFromRouter ('/root/geomask-backups/' + $name) $local
        if ($r.Rc -ne 0) { Stop-Run ('scp of ' + $name + ' failed: ' + $r.Out) }
    }
    foreach ($name in @('canary-sysupgrade.tar.gz', 'canary-overlay.tar.gz')) {
        $localSha = Get-Sha256 (Join-Path $script:RunDir $name)
        if ($localSha -ne $remoteSha[$name]) { Stop-Run ('sha256 mismatch for ' + $name) }
        Log 'INFO' ('backup ok: ' + $name + ' ' + $localSha)
    }
    $setLocal = Join-Path $script:RunDir 'canary-restore-set.txt'
    $setCount = (Get-Content -LiteralPath $setLocal).Count
    if ($setCount -lt 50) { Stop-Run ('restore set looks too small: ' + $setCount) }
    Log 'INFO' ('restore set: ' + $setCount + ' paths')
    $cmd = 'for f in etc/config/network etc/config/firewall etc/config/dhcp; do ' +
        'tar tzf /root/geomask-backups/canary-sysupgrade.tar.gz | grep -qx "$f" && echo "HAVE $f" || echo "MISS $f"; done'
    $r = Invoke-Ssh $cmd 60
    Log 'INFO' ('keep-list check: ' + ($r.Out -replace "`r?`n", '; '))
    if ($r.Out -match 'MISS etc/config/') { Stop-Run 'the sysupgrade archive lacks a core config file' }
}

function Test-Rehearsal {
    Log 'INFO' 'restore rehearsal into /tmp (no system changes)'
    $cmd = 'rm -rf /tmp/restore-rehearsal; mkdir -p /tmp/restore-rehearsal; ' +
        'tar xzf /root/geomask-backups/canary-sysupgrade.tar.gz -C /tmp/restore-rehearsal && ' +
        'tar xzf /root/geomask-backups/canary-overlay.tar.gz -C /tmp/restore-rehearsal && echo RC=0'
    $r = Invoke-Ssh $cmd 180
    if ($r.Rc -ne 0 -or $r.Out -notmatch 'RC=0') { Stop-Run ('rehearsal extract failed: ' + $r.Out) }
    $cmd = 'for f in etc/config/network etc/config/firewall etc/config/dhcp etc/mihomo/config.yaml; do ' +
        'if [ -f "/$f" ]; then cmp -s "/$f" "/tmp/restore-rehearsal/$f" && echo "SAME $f" || echo "DIFF $f"; ' +
        'else echo "ABSENT $f"; fi; done'
    $r = Invoke-Ssh $cmd 60
    Log 'INFO' ('rehearsal: ' + ($r.Out -replace "`r?`n", '; '))
    if ($r.Out -match 'ABSENT etc/config/network') { Stop-Run 'rehearsal lacks etc/config/network' }
    if ($r.Out -match 'ABSENT etc/mihomo/config.yaml') {
        Log 'WARN' 'mihomo config is not in the archives: the canary must not rewrite it (no --replace-config)'
    }
    # The PC keeps verified copies; the overlay is small (185 MB) and geomask's next backup
    # would otherwise nest these archives into /root again.
    $r = Invoke-Ssh 'rm -f /root/geomask-backups/canary-*.tar.gz /root/geomask-backups/canary-restore-set.txt; df -h / | tail -1'
    Log 'INFO' ('router copies removed: ' + $r.Out.Trim())
}

function Send-Tools {
    Log 'INFO' 'upload probe.sh, restore.sh and the canary installer'
    $r = Invoke-Ssh 'mkdir -p /tmp/canary; echo ok' 20
    if ($r.Rc -ne 0) { Stop-Run 'cannot create /tmp/canary' }
    foreach ($pair in @(@((Join-Path $PSScriptRoot 'probe.sh'), '/tmp/canary/probe.sh'),
                        @((Join-Path $PSScriptRoot 'restore.sh'), '/tmp/canary/restore.sh'))) {
        if (-not (Test-Path -LiteralPath $pair[0])) { Stop-Run ('missing local script: ' + $pair[0]) }
        $r = Invoke-ScpToRouter $pair[0] $pair[1]
        if ($r.Rc -ne 0) { Stop-Run ('scp failed: ' + $pair[1] + ' ' + $r.Out) }
    }
    $r = Invoke-ScpToRouter $Installer '/tmp/canary/geomask.sh'
    if ($r.Rc -ne 0) { Stop-Run ('scp of the installer failed: ' + $r.Out) }
    $r = Invoke-Ssh 'chmod 755 /tmp/canary/*.sh; sh -n /tmp/canary/geomask.sh && sh /tmp/canary/probe.sh | head -20'
    if ($r.Rc -ne 0) { Stop-Run ('syntax/probe failed: ' + $r.Out) }
    Log 'INFO' ('probe before: ' + ($r.Out -replace "`r?`n", ' '))
}

function Invoke-ReadOnlyCheck {
    $r = Invoke-Ssh 'sh /tmp/canary/geomask.sh --check' 300
    $file = Join-Path $script:RunDir 'canary-check-before.txt'
    Set-Content -LiteralPath $file -Value $r.Out -Encoding ASCII
    Log 'INFO' ('read-only --check rc=' + $r.Rc + ' saved to ' + $file)
    if ($r.Rc -ne 0) { Log 'WARN' 'the read-only check reports a problem before the install' }
}

function Invoke-Install {
    Log 'INFO' 'install: --auto --yes --restart'
    $r = Invoke-Ssh 'touch /tmp/canary/install-start.ref; sh /tmp/canary/geomask.sh --auto --yes --restart > /tmp/canary/install.log 2>&1; rc=$?; tail -n 120 /tmp/canary/install.log; echo RC=$rc' 1800
    $file = Join-Path $script:RunDir 'install.log'
    Set-Content -LiteralPath $file -Value $r.Out -Encoding ASCII
    Log 'INFO' ('install rc=' + $r.Rc + ' log saved to ' + $file)
    $installed = ''
    if ($r.Out -match 'RC=(\d+)') { $installed = $Matches[1] }
    if ($installed -ne '0') { Log 'WARN' ('install reported rc=' + $installed) }
    $null = Invoke-ScpFromRouter '/tmp/canary/install.log' (Join-Path $script:RunDir 'install.log.remote')
    $v = Invoke-Ssh 'cat /etc/geomask/version 2>/dev/null' 20
    Log 'INFO' ('installed version: ' + $v.Out)
}

function Get-Round {
    $crit = New-Object System.Collections.Generic.List[string]
    $warn = New-Object System.Collections.Generic.List[string]
    $info = @{}

    $ping = Test-Ping
    $info['ping'] = $ping
    if (-not $ping) { $crit.Add('ping') }

    $probe = Get-Probe
    $info['ssh'] = $probe.Ok
    if (-not $probe.Ok) {
        $crit.Add('ssh')
    } else {
        $v = $probe.Values
        foreach ($key in @('mihomo', 'inet')) {
            $ok = ($v[$key] -eq '1')
            $info[$key] = $ok
            if (-not $ok) { $crit.Add(('probe:' + $key)) }
        }
        $tun = [string]$v['tun']
        $info['tun'] = $tun
        if ($tun -notmatch 'Mihomo') { $crit.Add('tun:Mihomo') }
        $rules = [string]$v['rules']
        $info['rules'] = $rules
        if ($rules -notmatch '100') { $crit.Add('rules:100') }
        if ($v['dns'] -ne '1') { $warn.Add('dns-router') }
        if ($v['nft'] -eq '0') { $warn.Add('nft') }
        if ($v['backupok'] -ne '1') { $warn.Add('backup-ok') }
    }

    $web = Test-InternetFromPc
    $info['web'] = $web
    if (-not $web) {
        $dns = Test-DnsViaRouter
        $info['dns'] = $dns
        if (-not $dns) { $crit.Add('web+dns') } else { $crit.Add('web') }
    } else {
        $info['dns'] = $true
    }

    return [pscustomobject]@{ Critical = $crit; Warn = $warn; Info = $info; Healthy = ($crit.Count -eq 0) }
}

function Format-Round {
    param($Round, [int]$Fails)
    $parts = @()
    foreach ($k in @('ping', 'ssh', 'web', 'dns', 'mihomo', 'tun', 'rules', 'inet')) {
        if ($Round.Info.ContainsKey($k)) { $parts += ($k + '=' + $Round.Info[$k]) }
    }
    return (($parts -join ' ') + ' fails=' + $Fails + '/' + $FailThreshold +
        $(if ($Round.Critical.Count -gt 0) { ' critical=' + ($Round.Critical -join ',') } else { '' }) +
        $(if ($Round.Warn.Count -gt 0) { ' warn=' + ($Round.Warn -join ',') } else { '' }))
}

function Wait-RouterBack {
    param([int]$TimeoutSec)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        if (Test-Ping) {
            $p = Get-Probe
            if ($p.Ok) { return $true }
        }
    }
    return $false
}

function Invoke-Rollback {
    Log 'WARN' 'rollback: restoring both archives and cleaning files the archives lack'
    $sys = Join-Path $script:RunDir 'canary-sysupgrade.tar.gz'
    $ovl = Join-Path $script:RunDir 'canary-overlay.tar.gz'
    $set = Join-Path $script:RunDir 'canary-restore-set.txt'
    foreach ($f in @($sys, $ovl, $set)) {
        if (-not (Test-Path -LiteralPath $f)) { Log 'FAIL' ('rollback asset missing: ' + $f); return 'ROLLBACK_IMPOSSIBLE' }
    }
    if ((Get-Sha256 $sys) -ne $script:Archives['sha']['canary-sysupgrade.tar.gz']) { return 'ROLLBACK_IMPOSSIBLE' }
    if ((Get-Sha256 $ovl) -ne $script:Archives['sha']['canary-overlay.tar.gz']) { return 'ROLLBACK_IMPOSSIBLE' }

    foreach ($pair in @(@($sys, '/tmp/canary/restore-sysupgrade.tar.gz'),
                        @($ovl, '/tmp/canary/restore-overlay.tar.gz'),
                        @($set, '/tmp/canary/restore-set.txt'))) {
        $r = Invoke-ScpToRouter $pair[0] $pair[1]
        if ($r.Rc -ne 0) { Log 'FAIL' ('scp for rollback failed: ' + $r.Out); return 'ROLLBACK_IMPOSSIBLE' }
    }
    $r = Invoke-Ssh 'sh /tmp/canary/restore.sh /tmp/canary/restore-sysupgrade.tar.gz /tmp/canary/restore-overlay.tar.gz /tmp/canary/restore-set.txt /tmp/canary/install-start.ref clean' 180
    Set-Content -LiteralPath (Join-Path $script:RunDir 'restore.log') -Value $r.Out -Encoding ASCII
    if ($r.Out -notmatch 'RESTORE_OK') {
        Log 'FAIL' ('restore failed: ' + ($r.Out -replace "`r?`n", ' '))
        return 'ROLLBACK_FAILED'
    }
    Log 'INFO' 'restore ok, rebooting'
    $null = Invoke-Ssh 'reboot' 20
    Start-Sleep -Seconds 5
    if (-not (Wait-RouterBack $BootWaitSec)) {
        Log 'FAIL' ('router did not come back within ' + $BootWaitSec + ' s')
        return 'ROLLBACK_FAILED'
    }
    Log 'INFO' 'router is back; checking for 5 rounds'
    $fails = 0
    for ($i = 0; $i -lt 5; $i++) {
        Start-Sleep -Seconds $IntervalSec
        $round = Get-Round
        if ($round.Healthy) { $fails = 0; Log 'INFO' ('after rollback round=' + ($i + 1) + ' healthy') }
        else { $fails++; Log 'WARN' ('after rollback round=' + ($i + 1) + ' ' + (Format-Round $round $fails)) }
        if ($fails -ge 2) { return 'ROLLBACK_FAILED' }
    }
    return 'ROLLED_BACK'
}

function Write-Report {
    param([string]$Verdict, [string]$Note = '')
    $report = Join-Path $script:RunDir 'report.md'
    $lines = @(
        '# geomask canary report',
        '',
        '- when: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'),
        '- router: ' + $Router,
        '- installer sha256: ' + $script:InstallerSha,
        '- mode: ' + $(if ($Execute) { 'execute' } else { 'plan-only' }),
        '- verdict: ' + $Verdict,
        '- note: ' + $Note,
        '',
        'See monitor.log, install.log, canary-check-before.txt, restore.log, canary-restore-set.txt.',
        'Copy this directory to the PC backup folder and record the result in',
        'docs/stand/ROUTER-CANARY-EVIDENCE-<date>.md.'
    )
    Set-Content -LiteralPath $report -Value $lines -Encoding ASCII
    Log 'INFO' ('report: ' + $report)
}

try {
    Assert-LocalInputs
    New-RunDir
    New-Backup
    Test-Rehearsal
    Send-Tools
    Invoke-ReadOnlyCheck
    if (-not $Execute) {
        Log 'INFO' 'PLAN ONLY: no install was performed (add -Execute for the full canary run)'
        Write-Report 'PLAN_ONLY' 'backup and rehearsal passed'
        exit 0
    }

    Invoke-Install
    $post = Invoke-Ssh 'sh /tmp/canary/geomask.sh --check' 300
    Set-Content -LiteralPath (Join-Path $script:RunDir 'canary-check-after.txt') -Value $post.Out -Encoding ASCII
    Log 'INFO' ('read-only --check after install rc=' + $post.Rc)
    $installTime = Get-Date
    $fails = 0
    $healthy = 0
    $rollback = 'NONE'
    while ($healthy -lt $HealthyRounds) {
        Start-Sleep -Seconds $IntervalSec
        $round = Get-Round
        $inGrace = (((Get-Date) - $installTime).TotalSeconds -lt $GraceSec)
        if ($round.Healthy) {
            $healthy++
            $fails = 0
            Log 'INFO' ('round healthy (' + $healthy + '/' + $HealthyRounds + ') ' + (Format-Round $round $fails))
            continue
        }
        if ($inGrace) {
            Log 'INFO' ('grace ' + (Format-Round $round $fails))
            $healthy = 0
            continue
        }
        $healthy = 0
        $fails++
        Log 'WARN' ('round unhealthy (' + (Format-Round $round $fails) + ')')
        if ($fails -ge $FailThreshold) {
            if ($NoRollback) {
                Log 'FAIL' 'threshold reached, -NoRollback given: stopping without a rollback'
                Write-Report 'UNHEALTHY_NO_ROLLBACK' 'monitor threshold reached'
                exit 2
            }
            if (-not $round.Info['ssh']) {
                Log 'FAIL' 'SSH is down: refusing an automatic reboot; write manual instructions'
                $manual = Join-Path $script:RunDir 'MANUAL-ACTION-REQUIRED.txt'
                @(
                    'The router is unhealthy and SSH is not reachable.',
                    'Do NOT power-cycle automatically without checking the LEDs first.',
                    '1. Try LuCI: http://' + $script:RouterIp + '/ -> System -> Backup / Flash Firmware -> Restore.',
                    '2. Upload: ' + (Join-Path $script:RunDir 'canary-sysupgrade.tar.gz'),
                    '3. After the reboot, restore the overlay: ' + (Join-Path $script:RunDir 'canary-overlay.tar.gz'),
                    '   (tar -xzf canary-overlay.tar.gz -C /) and reboot again.',
                    '4. If LuCI is also down: failsafe mode (power cycle + LAN-only) or serial console.',
                    'Logs: ' + $script:RunDir
                ) | Set-Content -LiteralPath $manual -Encoding ASCII
                Write-Report 'MANUAL_ACTION_REQUIRED' ('logs: ' + $script:RunDir)
                exit 4
            }
            $rollback = Invoke-Rollback
            if ($rollback -eq 'ROLLED_BACK') { Write-Report 'ROLLED_BACK' 'router is healthy after the restore'; exit 0 }
            Write-Report $rollback 'see restore.log and monitor.log'
            exit 3
        }
    }
    Write-Report 'HEALTHY' ('healthy rounds: ' + $healthy)
    exit 0
}
catch {
    $msg = $_.Exception.Message
    Log 'FAIL' $msg
    if ($script:RunDir) { Write-Report 'ABORTED' $msg }
    exit 1
}
