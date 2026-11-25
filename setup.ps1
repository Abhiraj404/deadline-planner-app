<#
.SYNOPSIS
    Prepare Deadline Planner on Windows. Safe to run more than once.

.DESCRIPTION
    Checks for Python 3.11 or later, creates .venv if needed, installs the pinned
    dependencies, installs Playwright Chromium (used only for Moodle), and prepares the
    personal data folder (data\ with .env and the SQLite database).
    It does not need administrator rights and does not change system settings.

.PARAMETER SkipBrowser
    Skip the Playwright Chromium download. Moodle sync will not work until it is installed.

.PARAMETER Dev
    Also install the developer tools used for tests and linting.
#>
[CmdletBinding()]
param(
    [switch] $SkipBrowser,
    [switch] $Dev
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$minimum = [Version] '3.11'

function Write-Step([string] $message) {
    Write-Host "==> $message" -ForegroundColor Cyan
}

# Windows PowerShell 5.1 drops double quotes inside native arguments, so this probe
# uses single quotes only.
$versionProbe = 'import sys; print(''%d.%d.%d'' % sys.version_info[:3])'

function Find-Python {
    # Try the Python launcher, then every python.exe on PATH. A Microsoft Store alias
    # that is not really Python simply fails the probe and is skipped.
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3') }
    foreach ($command in Get-Command python -All -ErrorAction SilentlyContinue) {
        $candidates += , @($command.Source)
    }
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $arguments = @($candidate | Select-Object -Skip 1)
        try {
            $text = & $exe @arguments -c $versionProbe 2>$null
            if ($LASTEXITCODE -eq 0 -and $text) {
                $version = [Version] "$text".Trim()
                if ($version -ge $minimum) {
                    return [pscustomobject]@{ Exe = $exe; Arguments = $arguments; Version = $version }
                }
            }
        } catch { }
    }
    return $null
}

Set-Location -LiteralPath $root

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step 'Looking for Python 3.11 or later'
    $python = Find-Python
    if (-not $python) {
        throw 'Python 3.11 or later was not found. Install it from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH"), then run .\setup.cmd again.'
    }
    Write-Step "Creating .venv with Python $($python.Version)"
    & $python.Exe @($python.Arguments) -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
} else {
    Write-Step 'Using the existing .venv'
}

$version = [Version] ("$(& $venvPython -c $versionProbe)".Trim())
if ($version -lt $minimum) {
    throw ".venv uses Python $version. Delete the .venv folder and run .\setup.cmd with Python 3.11 or later."
}

Write-Step 'Installing dependencies'
& $venvPython -m pip install --disable-pip-version-check --quiet --upgrade pip
$lock = if ($Dev) { 'requirements-dev-lock.txt' } else { 'requirements-lock.txt' }
& $venvPython -m pip install --disable-pip-version-check --quiet -r (Join-Path $root $lock)
if ($LASTEXITCODE -ne 0) {
    # Pinned versions were tested on one Python release. Other releases may need the
    # compatible ranges in requirements.txt instead.
    Write-Warning "The pinned versions in $lock could not be installed on Python $version. Installing compatible versions from the requirements files instead."
    $fallback = if ($Dev) { 'requirements-dev.txt' } else { 'requirements.txt' }
    & $venvPython -m pip install --disable-pip-version-check --quiet -r (Join-Path $root $fallback)
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your internet connection and try again.' }
}

if ($SkipBrowser) {
    Write-Step 'Skipping the Playwright browser (Moodle sync needs it later)'
} else {
    Write-Step 'Installing Playwright Chromium for Moodle sync (about 150 MB, once)'
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Chromium could not be installed. Manual imports and demo mode still work. Retry later with: .\.venv\Scripts\python.exe -m playwright install chromium'
    }
}

# run_server.py creates data\ (folders, .env from .env.example, the database), moves
# personal files left in the project folder by older versions, and keeps existing files.
Write-Step 'Preparing the data folder and SQLite database'
& $venvPython (Join-Path $root 'run_server.py') --init-only
if ($LASTEXITCODE -eq 3) {
    Write-Step 'The planner is already running, so its data folder is already prepared'
} elseif ($LASTEXITCODE -eq 5) {
    throw 'Stop the running planner with .\stop.cmd, then run .\setup.cmd again.'
} elseif ($LASTEXITCODE -ne 0) {
    throw 'Data folder preparation failed. See data\planner_logs\planner.log.'
}

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host '  Start the planner:     .\start.cmd -Open'
Write-Host '  Explore the demo:      .\start.cmd -Demo -Open'
Write-Host '  Stop the planner:      .\stop.cmd'
Write-Host 'Open Setup in the planner to connect Moodle, Outlook or Gmail. All are optional.'
