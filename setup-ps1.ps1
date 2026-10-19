#Requires -Version 5.1
<#
.SYNOPSIS
    cachereach one-click installer (Windows PowerShell / PowerShell 7)

.DESCRIPTION
    Run this file to check the environment and install everything needed.
    Same behaviour as the .bat version; useful if you prefer PowerShell.

    Switches are passed through to setup.py:
      -Check      only verify, do not modify anything
      -Force      rebuild venv and node_modules
      -SkipVenv   do not create a virtual environment

.EXAMPLE
    .\setup-ps1.ps1
    .\setup-ps1.ps1 -Check
#>

[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Force,
    [switch]$SkipVenv
)

$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------
# Output encoding. PowerShell 5.1 defaults to the OEM code page, which
# mangles non-ASCII output. Force UTF-8 on both the console and the
# pipeline so Chinese text renders correctly everywhere.
# ------------------------------------------------------------------
try {
    $OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
    chcp 65001 > $null 2>&1
} catch {
    # Console encoding unavailable (redirected output) - not fatal.
}

# Avoid the mojibake that appears when a child process writes bytes
# using a different code page than the host decodes them with.
# Let the child detect its own stream encoding (setup.py downgrades its
# markers to ASCII when the stream is not UTF-8) - forcing PYTHONIOENCODING
# here would override that and reintroduce mixed encodings.
$env:NODE_DISABLE_COLORS = '0'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# ASCII-safe markers keep output readable even if a font/locale is odd.
function Say  ($m) { Write-Host $m }
function Head ($m) { Write-Host ''; Write-Host ('=' * 62); Write-Host "  $m"; Write-Host ('=' * 62) }
function Step ($m) { Write-Host ''; Write-Host ">> $m" }
function Good ($m) { Write-Host "   [OK] $m" }
function Warn ($m) { Write-Host "   [!]  $m" }
function Bad  ($m) { Write-Host "   [X]  $m" }
function Note ($m) { Write-Host "        $m" }

function Find-Python {
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venv) {
        return @{ Path = $venv; Args = @(); Label = 'existing virtual environment' }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @{ Path = 'py'; Args = @('-3'); Label = 'py launcher' }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        return @{ Path = 'python'; Args = @(); Label = 'system python' }
    }
    $cands = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        'C:\Python313\python.exe',
        'C:\Python312\python.exe'
    )
    foreach ($c in $cands) {
        if (Test-Path $c) { return @{ Path = $c; Args = @(); Label = $c } }
    }
    return $null
}

Head 'cachereach - one-click installer'
Say '  Export local game data from browser cache'

Step 'Checking Python'
$py = Find-Python
if (-not $py) {
    Bad 'Python not found'
    Say ''
    Say '  Install Python 3.8 or newer from:'
    Say '    https://www.python.org/downloads/'
    Say ''
    Say '  Tick "Add Python to PATH" during setup.'
    Say ''
    Read-Host 'Press Enter to close'
    exit 1
}
Good $py.Label

Step 'Running setup'
$setupArgs = @("$Root\setup.py")

# PowerShell captures a child process through a pipe, so the child has no
# console encoding to inherit and may emit bytes the host then renders as
# mojibake. -u keeps the ordering stable; CACHEREACH_ASCII tells setup.py
# to use ASCII markers, which every code page can represent.
if (-not $Check) { $setupArgs = @('-u') + $setupArgs }
if ($Check)    { $setupArgs += '--check' }
if ($Force)    { $setupArgs += '--force' }
if ($SkipVenv) { $setupArgs += '--skip-venv' }

$prevAscii = $env:CACHEREACH_ASCII
$env:CACHEREACH_ASCII = '1'
try {
    & $py.Path @($py.Args + $setupArgs)
    $rc = $LASTEXITCODE
} finally {
    $env:CACHEREACH_ASCII = $prevAscii
}

Say ''
if ($rc -eq 0) {
    Head 'Setup finished successfully'
} else {
    Head "Setup reported a problem (exit code $rc)"
}
Say ''

if ($rc -ne 0) {
    Read-Host 'Press Enter to close'
} else {
    Start-Sleep -Seconds 3
}
exit $rc
