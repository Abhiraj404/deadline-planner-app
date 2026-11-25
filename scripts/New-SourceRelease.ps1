<#
.SYNOPSIS
    Build a clean, source-only release folder and zip in .\dist.

.DESCRIPTION
    Only files matching the explicit allowlist below are copied. The build stops when:
      - a required file is missing;
      - an allow-listed file is ignored by .gitignore (it may be private);
      - a file that Git would publish is not on the allowlist (decide deliberately);
      - an allow-listed path matches the deny-list of private file types;
      - the copied release contains a likely secret, personal path or email address;
      - the zip does not contain exactly the allow-listed files.
    Personal data, Git history, virtual environments, node_modules and caches are
    never copied. The script never commits, pushes or publishes anything.

.PARAMETER OutputDirectory
    Folder for the release. Defaults to .\dist (ignored by Git).

.PARAMETER Force
    Replace an existing release of the same version inside the output folder.
#>
[CmdletBinding()]
param(
    [string] $OutputDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist'),
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root = Split-Path -Parent $PSScriptRoot
$version = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$') { throw "VERSION is not semantic: $version" }
$name = "deadline-planner-$version"

# The public release: exact files and single-folder patterns (no deeper recursion).
$allowlist = @(
    '.env.example', '.gitignore', '.prettierrc.json', 'eslint.config.js', 'package.json',
    'package-lock.json', 'pyproject.toml', 'requirements.txt', 'requirements-lock.txt',
    'requirements-dev.txt', 'requirements-dev-lock.txt', 'VERSION', 'LICENSE',
    'README.md', 'AGENTS.md', 'CHANGELOG.md', 'DEVELOPMENT_STATUS.md',
    'CHROME_EXTENSION_PLAN.md', 'SECURITY.md', 'setup.ps1', 'start.ps1', 'stop.ps1',
    'setup.cmd', 'start.cmd', 'stop.cmd',
    'index.html', 'planner.js',
    '*.py',
    'static/*.js', 'static/*.css',
    'tests/*.py', 'tests/fixtures/*.json',
    'docs/*.md', 'docs/*.json', 'docs/screenshots/*.png',
    'scripts/*.ps1', 'scripts/*.py',
    '.github/workflows/*.yml'
)

$required = @(
    'app.py', 'run_server.py', 'configuration.py', 'data_location.py', 'database.py',
    'listing.py', 'assessment_extraction.py', 'document_processing.py', 'document_import.py',
    'document_store.py', 'pdf_processing.py', 'word_processing.py', 'search_index.py',
    'demo_data.py', 'synthetic_documents.py', 'index.html', 'planner.js', 'static/planner.css',
    'requirements.txt', 'requirements-lock.txt', 'setup.ps1', 'start.ps1', 'stop.ps1',
    '.env.example', 'VERSION', 'README.md', 'LICENSE', 'CHANGELOG.md',
    'tests/fixtures/extraction_cases.json'
)

$denied = @(
    '(^|/)\.env($|\.)(?!example$)', '\.db($|-)', '\.sqlite3?$', '\.bak$', '(^|/)credentials\.json$',
    '-token\.json$', '-cache\.json$', '-session\.json$', '(^|/)assessment_index\.json$',
    '\.(pdf|docx?|docm|log|zip|pyc)$', '(^|/)(data|demo-data|dist)/'
)

$secretPatterns = [ordered]@{
    'Moodle calendar token' = 'authtoken=[0-9a-f]{8,}'
    'Moodle session cookie' = 'MoodleSession[A-Za-z0-9]*"\s*,\s*"value"'
    'Google access token'   = 'ya29\.[0-9A-Za-z_-]{20,}'
    'OAuth refresh token'   = '"refresh_token"\s*:\s*"[^"]{10,}"'
    'Client secret'         = '"client_secret"\s*:\s*"[^"]{6,}"'
    'Private key'           = '-----BEGIN [A-Z ]*PRIVATE KEY-----'
    'Windows profile path'  = '[A-Za-z]:\\Users\\[^\\\s"]+'
    'Email address'         = '[A-Za-z0-9._%+-]+@(?!example\.|.*\.test\b|.*\.invalid\b)[A-Za-z0-9-]+\.[A-Za-z]{2,}'
}

function Get-Allowlisted {
    $result = foreach ($pattern in $allowlist) {
        $folder = Split-Path -Parent $pattern
        $leaf = Split-Path -Leaf $pattern
        $directory = if ($folder) { Join-Path $root $folder } else { $root }
        if (Test-Path -LiteralPath $directory) {
            Get-ChildItem -LiteralPath $directory -Filter $leaf -File -Force | ForEach-Object {
                if ($folder) { ($folder -replace '\\', '/') + '/' + $_.Name } else { $_.Name }
            }
        }
    }
    $result | Sort-Object -Unique
}

$included = @(Get-Allowlisted)

$missing = $required | Where-Object { $included -notcontains $_ }
if ($missing) { throw "Required files are missing: $($missing -join ', ')" }

Push-Location -LiteralPath $root
try {
    $ignored = @($included | git check-ignore --stdin)
    $publishable = @(git -c core.quotepath=off ls-files --cached --others --exclude-standard) |
        Where-Object { Test-Path -LiteralPath (Join-Path $root $_) -PathType Leaf }
} finally {
    Pop-Location
}
if ($ignored) { throw "Allow-listed files are ignored by .gitignore (possibly private): $($ignored -join ', ')" }
$unlisted = $publishable | Where-Object { $included -notcontains ($_ -replace '\\', '/') }
if ($unlisted) { throw "Files Git would publish are not on the allowlist. Add or ignore them: $($unlisted -join ', ')" }
$deniedHits = $included | Where-Object { $path = $_; $denied | Where-Object { $path -match $_ } }
if ($deniedHits) { throw "Allow-listed files match the private deny-list: $($deniedHits -join ', ')" }

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$output = (Resolve-Path -LiteralPath $OutputDirectory).Path
$folder = Join-Path $output $name
$zip = "$folder.zip"
foreach ($target in $folder, $zip) {
    if (Test-Path -LiteralPath $target) {
        if (-not $Force) { throw "$target already exists. Use -Force to replace it." }
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

foreach ($file in $included) {
    $source = Get-Item -LiteralPath (Join-Path $root $file) -Force
    if ($source.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Links are not allowed: $file" }
    $copy = Join-Path $folder $file
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $copy) | Out-Null
    Copy-Item -LiteralPath $source.FullName -Destination $copy
}

# Scan the built distribution itself, not the development folder.
$findings = foreach ($file in $included) {
    if ($file -match '\.png$') { continue }
    $text = [IO.File]::ReadAllText((Join-Path $folder $file))
    foreach ($label in $secretPatterns.Keys) {
        if ($text -match $secretPatterns[$label]) { "${file}: $label" }
    }
}
if ($findings) {
    Remove-Item -LiteralPath $folder -Recurse -Force
    # Report the file and kind of finding only, never the matched value.
    throw "Possible private data found. Nothing was released:`n  $($findings -join "`n  ")"
}

$entryTime = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
$stream = [IO.File]::Open($zip, [IO.FileMode]::CreateNew)
try {
    $archive = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in $included) {
            $entry = $archive.CreateEntry("$name/$file", [IO.Compression.CompressionLevel]::Optimal)
            $entry.LastWriteTime = $entryTime
            $reader = [IO.File]::OpenRead((Join-Path $folder $file))
            $writer = $entry.Open()
            try { $reader.CopyTo($writer) } finally { $writer.Dispose(); $reader.Dispose() }
        }
    } finally {
        $archive.Dispose()
    }
} finally {
    $stream.Dispose()
}

$check = [IO.Compression.ZipFile]::OpenRead($zip)
try {
    $entries = @($check.Entries | ForEach-Object { $_.FullName.Substring($name.Length + 1) } | Sort-Object)
} finally {
    $check.Dispose()
}
if (Compare-Object $entries ($included | Sort-Object)) { throw 'The zip does not match the allowlist.' }

$size = (Get-Item -LiteralPath $zip).Length
Write-Host "Release folder: $folder"
Write-Host "Release zip:    $zip ($([Math]::Round($size / 1KB)) KB)"
Write-Host "Files: $($included.Count). Allowlist, .gitignore, deny-list and secret scan checks passed."
