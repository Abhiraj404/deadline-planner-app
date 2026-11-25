<#
.SYNOPSIS
    Start Deadline Planner on http://127.0.0.1 using the project's virtual environment.

.PARAMETER Demo
    Use the synthetic demo data in .\demo-data instead of your personal data.

.PARAMETER Open
    Open the dashboard in the default browser once the server is ready.

.PARAMETER Port
    Port to listen on. Defaults to 8000, or 8001 in demo mode.

.PARAMETER Foreground
    Run in this window (Ctrl+C stops it) instead of a hidden background process.
#>
[CmdletBinding()]
param(
    [switch] $Demo,
    [switch] $Open,
    [ValidateRange(1024, 65535)] [int] $Port = 0,
    [switch] $Foreground
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'The virtual environment is missing. Run .\setup.cmd first.'
}

$dataDir = Join-Path $root $(if ($Demo) { 'demo-data' } else { 'data' })
$stateFile = Join-Path $dataDir '.planner-server.json'
if ($Port -eq 0) { $Port = if ($Demo) { 8001 } else { 8000 } }

# Refuse a second instance for the same data folder.
if (Test-Path -LiteralPath $stateFile) {
    try {
        $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        $process = Get-Process -Id $state.pid -ErrorAction Stop
        if ($process.StartTime -gt (Get-Item -LiteralPath $stateFile).LastWriteTime.AddSeconds(1)) {
            throw 'The recorded process ID now belongs to a newer process.'
        }
        Write-Host "Deadline Planner is already running at $($state.url) (process $($process.Id))."
        if ($Open) { Start-Process $state.url }
        return
    } catch {
        # A stale file from a crashed process. run_server.py replaces it.
    }
}

$arguments = @((Join-Path $root 'run_server.py'), '--port', $Port)
if ($Demo) { $arguments += '--demo' }
if ($Open) { $arguments += '--open' }

if ($Foreground) {
    & $venvPython @arguments
    exit $LASTEXITCODE
}

$logDir = Join-Path $dataDir 'planner_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$process = Start-Process -FilePath $venvPython -ArgumentList $arguments -WorkingDirectory $root `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logDir 'server-output.log') `
    -RedirectStandardError (Join-Path $logDir 'server-error.log')

$url = "http://127.0.0.1:$Port/"
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) {
        $message = Get-Content -LiteralPath (Join-Path $logDir 'server-error.log') -Raw -ErrorAction SilentlyContinue
        throw "Deadline Planner did not start. $message"
    }
    if (Test-Path -LiteralPath $stateFile) {
        try {
            Invoke-WebRequest -Uri "$($url)api/app" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $label = if ($Demo) { 'Demo mode' } else { 'Deadline Planner' }
            Write-Host "$label is running at $url" -ForegroundColor Green
            $stopCommand = if ($Demo) { '.\stop.cmd -Demo' } else { '.\stop.cmd' }
            Write-Host "Stop it with $stopCommand"
            return
        } catch { }
    }
}
throw "The server did not respond within 30 seconds. Check $logDir."
