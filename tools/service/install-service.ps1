<#
.SYNOPSIS
  Install (or reinstall) the Rom-Com web UI as a Windows service that starts on boot.

.DESCRIPTION
  Wraps `python -m romcom web` with NSSM. The service runs as LocalSystem with the
  repository as its working directory, which is what the app expects: ROMCOM_DB and
  the .vimm-profile path are both resolved relative to the repo root.

  Must be run elevated. Re-running is safe: an existing service is stopped and
  reconfigured in place rather than duplicated.
#>
[CmdletBinding()]
param(
    [string]$ServiceName = 'RomCom',
    [string]$BindHost    = '127.0.0.1',
    [int]   $Port        = 8927,
    [string]$Nssm,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This script must be run from an elevated PowerShell session.'
}

$Repo   = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$LogDir = Join-Path $Repo 'logs'

if (-not (Test-Path $Python)) { throw "Virtualenv interpreter not found: $Python" }

# The service runs as LocalSystem, which cannot see the interactive user's per-user
# site-packages. Fail here rather than after boot if the venv is missing dependencies.
# The import has to happen with the repo as the working directory, exactly as the
# service will run it -- `romcom` is a source tree, not an installed package.
Push-Location $Repo
try {
    $ErrorActionPreference = 'Continue'
    $probe = & $Python -c "import romcom.web" 2>&1
    $probeRc = $LASTEXITCODE
} finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
}
if ($probeRc -ne 0) {
    throw "$Python cannot import romcom.web:`n$($probe | Out-String)`nRun: $Python -m pip install -r requirements.txt"
}

# A venv built on the Microsoft Store Python resolves to a per-user app-execution alias
# under WindowsApps, which LocalSystem cannot execute at all: the service would install
# cleanly and then fail every start with "Unable to create process".
$base = (& $Python -c "import sys; print(sys._base_executable)").Trim()
if ($base -like '*\WindowsApps\*') {
    throw @"
The virtualenv is based on the Microsoft Store Python ($base), which a service account
cannot launch. Rebuild it from a machine-wide install, e.g.:
    rmdir /s /q "$Repo\.venv"
    C:\Python314\python.exe -m venv "$Repo\.venv"
    "$Python" -m pip install -r "$Repo\requirements.txt"
"@
}

# NSSM ships in the user's WinGet links dir, which is not on an elevated SYSTEM-ish PATH
# in every shell, so fall back to the well-known install location before giving up.
if (-not $Nssm) {
    $Nssm = (Get-Command nssm.exe -ErrorAction SilentlyContinue).Source
}
if (-not $Nssm) {
    $candidate = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\nssm.exe'
    if (Test-Path $candidate) { $Nssm = $candidate }
}
if (-not $Nssm -or -not (Test-Path $Nssm)) {
    throw 'nssm.exe not found. Install it (winget install NSSM.NSSM) or pass -Nssm <path>.'
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Args)
    $out = & $Nssm @Args 2>&1
    # NSSM writes its output as UTF-16; strip the NULs so the text is readable here.
    $text = ($out | Out-String) -replace "`0", ''
    if ($LASTEXITCODE -ne 0) { throw "nssm $($Args -join ' ') failed ($LASTEXITCODE): $text" }
    $text.Trim()
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service '$ServiceName' already exists - reconfiguring in place." -ForegroundColor Yellow
    if ($existing.Status -ne 'Stopped') {
        Invoke-Nssm stop $ServiceName | Out-Null
        (Get-Service $ServiceName).WaitForStatus('Stopped', '00:00:30')
    }
} else {
    Invoke-Nssm install $ServiceName $Python | Out-Null
    Write-Host "Installed service '$ServiceName'." -ForegroundColor Green
}

$appArgs = "-m romcom web --no-browser --host $BindHost --port $Port"

Invoke-Nssm set $ServiceName Application      $Python      | Out-Null
Invoke-Nssm set $ServiceName AppParameters    $appArgs     | Out-Null
Invoke-Nssm set $ServiceName AppDirectory     $Repo        | Out-Null
Invoke-Nssm set $ServiceName DisplayName      'Rom-Com Library Service' | Out-Null
Invoke-Nssm set $ServiceName Description      "Rom-Com ROM library web UI and acquisition watcher (http://$BindHost`:$Port/)" | Out-Null
Invoke-Nssm set $ServiceName ObjectName       LocalSystem  | Out-Null

# Delayed auto-start: the first connect() can build indexes over a 2 GB database, and
# there is no reason for that to compete with the rest of boot.
Invoke-Nssm set $ServiceName Start            SERVICE_DELAYED_AUTO_START | Out-Null

# Flask's dev server exits cleanly on Ctrl-C; give it time before NSSM escalates so the
# SQLite WAL gets checkpointed instead of truncated mid-write.
Invoke-Nssm set $ServiceName AppStopMethodSkip     0     | Out-Null
Invoke-Nssm set $ServiceName AppStopMethodConsole  15000 | Out-Null
Invoke-Nssm set $ServiceName AppStopMethodWindow   5000  | Out-Null
Invoke-Nssm set $ServiceName AppStopMethodThreads  5000  | Out-Null

# Restart on unexpected exit, but back off so a config error does not spin.
Invoke-Nssm set $ServiceName AppExit Default Restart | Out-Null
Invoke-Nssm set $ServiceName AppRestartDelay  10000 | Out-Null
Invoke-Nssm set $ServiceName AppThrottle      15000 | Out-Null

# Playwright keeps its browsers under the interactive user's profile; LocalSystem would
# otherwise look in its own and find none, breaking the Vimm source when it is enabled.
$browsers = Join-Path $env:LOCALAPPDATA 'ms-playwright'
$envExtra = @('PYTHONUNBUFFERED=1')
if (Test-Path $browsers) { $envExtra += "PLAYWRIGHT_BROWSERS_PATH=$browsers" }
Invoke-Nssm set $ServiceName AppEnvironmentExtra @envExtra | Out-Null

Invoke-Nssm set $ServiceName AppStdout        (Join-Path $LogDir 'service-out.log') | Out-Null
Invoke-Nssm set $ServiceName AppStderr        (Join-Path $LogDir 'service-err.log') | Out-Null
Invoke-Nssm set $ServiceName AppRotateFiles   1        | Out-Null
Invoke-Nssm set $ServiceName AppRotateOnline  1        | Out-Null
Invoke-Nssm set $ServiceName AppRotateBytes   10485760 | Out-Null

Write-Host ''
Write-Host "  command : $Python $appArgs"
Write-Host "  workdir : $Repo"
Write-Host "  account : LocalSystem"
Write-Host "  startup : Automatic (Delayed Start)"
Write-Host "  logs    : $LogDir"
Write-Host ''

if ($NoStart) {
    Write-Host 'Configured but not started (-NoStart).' -ForegroundColor Yellow
} else {
    Invoke-Nssm start $ServiceName | Out-Null
    (Get-Service $ServiceName).WaitForStatus('Running', '00:01:00')
    Write-Host "Service '$ServiceName' is running: http://$BindHost`:$Port/" -ForegroundColor Green
}
