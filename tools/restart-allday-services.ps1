[CmdletBinding()]
param(
    [switch] $StopOnly,
    [string] $DataRoot,
    [string] $SourceRoot,
    [switch] $ValidateOnly,
    [switch] $NoOpen,
    [switch] $CachedOnly
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ($DataRoot) { $ProjectRoot = (Resolve-Path -LiteralPath $DataRoot).Path }
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$RuntimePath = Join-Path $PSScriptRoot 'allday_service_runtime.py'
$startedServices = @()
$LogRoot = Join-Path $ProjectRoot 'state\runtime-logs'
$WebUrl = 'http://127.0.0.1:8765/'

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-AllDayServiceProcesses {
    $escapedRoot = [regex]::Escape($ProjectRoot)
    try {
        return @(
            Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
                $commandLine = [string] $_.CommandLine
                $_.Name -match '^(?:python(?:w|[0-9.]+)?|allday-asr)\.exe$' -and
                $commandLine -match $escapedRoot -and
                $commandLine -match '(?i)(?:allday-asr(?:\.exe)?|allday_asr|allday_service_runtime)' -and
                $commandLine -match '(?i)(?:\bweb\b|\bdesktop\b|\bdevice\s+receive\b|--role\s+\x22?receiver)'
            }
        )
    }
    catch {
        # Non-admin Windows sessions may deny process command-line queries.
        # Port scanning remains sufficient for the dedicated service ports.
        return @()
    }
}

function Get-AllDayServicePortPids {
    # These ports are reserved for this project's WebUI/receiver instances.
    # Port-based fallback handles Python launcher children whose command line
    # is hidden by Windows process permissions.
    $pids = @()
    foreach ($line in @(netstat -ano | Select-String -Pattern '^\s*TCP\s+[^\s]+:(8765|8766|8767)\s+[^\s]+\s+LISTENING\s+(\d+)\s*$')) {
        if ($line.Line -match '\s(\d+)\s*$') {
            $pids += [int] $Matches[1]
        }
    }
    return @($pids | Sort-Object -Unique)
}

function Stop-AllDayServices {
    $processes = Get-AllDayServiceProcesses
    $portPids = Get-AllDayServicePortPids
    $processIds = @($processes | ForEach-Object ProcessId) + $portPids
    $processIds = @($processIds | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    if ($processIds.Count -eq 0) {
        Write-Host 'No old AllDayRecording services found.'
        return
    }

    foreach ($processId in $processIds) {
        Write-Host ("Stopping old service: PID {0}" -f $processId)
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $remainingProcesses = Get-AllDayServiceProcesses
        $remainingPorts = Get-AllDayServicePortPids
        $remainingIds = @($remainingProcesses | ForEach-Object ProcessId) + $remainingPorts
        $remainingIds = @($remainingIds | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
        if ($remainingIds.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Old services did not stop within 10 seconds. Remaining PIDs: $($remainingIds -join ', ')"
}

function Test-TcpPort {
    param([Parameter(Mandatory)] [int] $Port)

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        return $task.Wait(300) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Wait-TcpPort {
    param(
        [Parameter(Mandatory)] [int] $Port,
        [int] $TimeoutSeconds = 30,
        [Diagnostics.Process] $Process,
        [string] $ServiceName = 'Service'
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ($null -ne $Process) {
            $Process.Refresh()
            if ($Process.HasExited) { throw "$ServiceName exited with code $($Process.ExitCode). See $LogRoot for stderr logs." }
        }
        if (Test-TcpPort -Port $Port) {
            return $true
        }
        Start-Sleep -Milliseconds 300
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

if ($StopOnly) {
    if (-not (Test-IsAdministrator)) {
        throw 'Administrator permission is required to stop old services.'
    }
    Stop-AllDayServices
    exit 0
}

try {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "Project Python not found: $PythonPath"
    }
    $preflightArgs = @('-B', $RuntimePath, 'preflight', '--data-root', $ProjectRoot)
    if ($SourceRoot) { $preflightArgs += @('--source-root', $SourceRoot) }
    $preflightOutput = & $PythonPath @preflightArgs
    if ($LASTEXITCODE -ne 0) { throw 'Source/database compatibility check failed; old services were not stopped.' }
    $runtime = $preflightOutput | ConvertFrom-Json
    $SourceRoot = $runtime.source_root
    Write-Host "Code: $SourceRoot"
    Write-Host "Data: $ProjectRoot"
    Write-Host "Schema: $($runtime.database_schema) / supported $($runtime.supported_schema); Device API: $($runtime.contract_version) / projection $($runtime.projection_version)"
    if ($CachedOnly) { Write-Host 'Cached-only verification: automatic processing and model jobs disabled.' }
    if ($ValidateOnly) { exit 0 }

    $oldProcesses = Get-AllDayServiceProcesses
    $oldPortPids = Get-AllDayServicePortPids
    $hasOldServices = $oldProcesses.Count -gt 0 -or $oldPortPids.Count -gt 0
    if (-not $hasOldServices) {
        Write-Host 'No old services detected; skipping administrator prompt.'
    }
    elseif (Test-IsAdministrator) {
        Stop-AllDayServices
    }
    else {
        # First stop services owned by the current user.  UAC is only needed
        # when an older elevated instance refuses the normal termination.
        try {
            Stop-AllDayServices
        }
        catch {
            Write-Host 'Requesting administrator permission to stop remaining old services...'
            $elevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru `
                -ArgumentList ('-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -StopOnly -DataRoot "{1}"' -f $PSCommandPath, $ProjectRoot)
            if ($elevated.ExitCode -ne 0) {
                throw "Stopping old services failed. Exit code: $($elevated.ExitCode)"
            }
        }
    }

    Start-Sleep -Milliseconds 500
    if (Test-TcpPort -Port 8765) {
        throw 'Port 8765 is still occupied by another program; WebUI was not started.'
    }
    if (Test-TcpPort -Port 8766) {
        throw 'Port 8766 is still occupied by another program; receiver was not started.'
    }

    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUNBUFFERED = '1'

    $commonArguments = @('-B', ('"' + $RuntimePath + '"'), 'run', '--data-root', ('"' + $ProjectRoot + '"'), '--source-root', ('"' + $SourceRoot + '"'))
    if ($CachedOnly) { $commonArguments += '--cached-only' }
    Write-Host 'Starting phone receiver on port 8766...'
    $receiver = Start-Process -FilePath $PythonPath `
        -ArgumentList ($commonArguments + @('--role', 'receiver')) `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogRoot 'receiver.stdout.log') `
        -RedirectStandardError (Join-Path $LogRoot 'receiver.stderr.log')

    $startedServices += $receiver
    Write-Host 'Starting WebUI on port 8765...'
    $web = Start-Process -FilePath $PythonPath `
        -ArgumentList ($commonArguments + @('--role', 'web')) `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogRoot 'web.stdout.log') `
        -RedirectStandardError (Join-Path $LogRoot 'web.stderr.log')

    $startedServices += $web
    if (-not (Wait-TcpPort -Port 8766 -Process $receiver -ServiceName 'Receiver')) {
        throw "Receiver startup timed out. See $LogRoot\receiver.stderr.log"
    }
    if (-not (Wait-TcpPort -Port 8765 -Process $web -ServiceName 'WebUI')) {
        throw "WebUI startup timed out. See $LogRoot\web.stderr.log"
    }

    Write-Host ''
    Write-Host 'AllDayRecording services started:' -ForegroundColor Green
    Write-Host '  Phone receiver: https://LAN-address:8766'
    Write-Host "  WebUI:          $WebUrl"
    Write-Host "  Launcher PIDs:  receiver $($receiver.Id), WebUI $($web.Id)"
    Write-Host "  Logs:           $LogRoot"

    # Browser launch is best-effort: a shell/browser association failure must
    # not make an otherwise healthy pair of background services look failed.
    try {
        if (-not $NoOpen) { Start-Process -FilePath $WebUrl -ErrorAction Stop | Out-Null }
        if (-not $NoOpen) { Write-Host 'Default browser launch requested.' }
    }
    catch {
        Write-Host "Could not open the browser automatically. Open manually: $WebUrl" -ForegroundColor Yellow
    }
}
catch {
    $startupFailure = $_.Exception.Message
    Write-Host ''
    foreach ($started in $startedServices) {
        $started.Refresh()
        if (-not $started.HasExited) {
            & taskkill.exe /PID $started.Id /T /F 2>&1 | Out-Null
        }
    }
    Write-Host ("ERROR: {0}" -f $startupFailure) -ForegroundColor Red
    exit 1
}
