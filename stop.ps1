<#
.SYNOPSIS
    Stop the Deadline Planner server started from this project folder.

.DESCRIPTION
    Reads .planner-server.json written by run_server.py. It first asks the server to
    shut down using the private token in that file. Only if that fails does it end the
    recorded process, and only after confirming that the process is this project's
    run_server.py and that it started before the state file was written (so a reused
    process ID is never stopped). Other Python processes are never touched.

.PARAMETER Demo
    Stop the demo-mode server instead of the personal one.
#>
[CmdletBinding()]
param([switch] $Demo)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$dataDir = Join-Path $root $(if ($Demo) { 'demo-data' } else { 'data' })
$stateFile = Join-Path $dataDir '.planner-server.json'
$legacyState = Join-Path $root '.planner-server.json'
if (-not $Demo -and -not (Test-Path -LiteralPath $stateFile) -and (Test-Path -LiteralPath $legacyState)) {
    $stateFile = $legacyState
}

if (-not (Test-Path -LiteralPath $stateFile)) {
    Write-Host 'Deadline Planner is not running.'
    return
}

$state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
$process = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue
    Write-Host 'Deadline Planner is not running. Removed a stale state file.'
    return
}

function Test-PlannerProcess($process, $state, [string] $stateFile) {
    $info = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)" -ErrorAction SilentlyContinue
    if (-not $info -or -not $info.CommandLine) { return $false }
    $script = Join-Path $state.app_dir 'run_server.py'
    $started = $process.StartTime
    $written = (Get-Item -LiteralPath $stateFile).LastWriteTime
    return ($info.CommandLine -like "*$script*") -and ($started -le $written.AddSeconds(1))
}

try {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($state.port)/api/server/shutdown" `
        -Headers @{ 'X-Planner-Shutdown-Token' = $state.token } -TimeoutSec 5 | Out-Null
    if ($process.WaitForExit(15000)) {
        Write-Host 'Deadline Planner stopped.'
        return
    }
} catch {
    Write-Verbose "Graceful shutdown request failed: $($_.Exception.Message)"
}

if (Test-PlannerProcess $process $state $stateFile) {
    Stop-Process -Id $process.Id
    Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue
    Write-Host 'Deadline Planner stopped.'
} else {
    Write-Warning "Process $($process.Id) is not this project's planner server, so it was left running."
}
