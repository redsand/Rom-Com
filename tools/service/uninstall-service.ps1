<#
.SYNOPSIS
  Stop and remove the Rom-Com Windows service. Must be run elevated.
#>
[CmdletBinding()]
param(
    [string]$ServiceName = 'RomCom',
    [string]$Nssm
)

$ErrorActionPreference = 'Stop'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This script must be run from an elevated PowerShell session.'
}

if (-not $Nssm) { $Nssm = (Get-Command nssm.exe -ErrorAction SilentlyContinue).Source }
if (-not $Nssm) {
    $candidate = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\nssm.exe'
    if (Test-Path $candidate) { $Nssm = $candidate }
}
if (-not $Nssm -or -not (Test-Path $Nssm)) { throw 'nssm.exe not found. Pass -Nssm <path>.' }

if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    Write-Host "Service '$ServiceName' is not installed - nothing to do."
    return
}

& $Nssm stop $ServiceName 2>&1 | Out-Null
& $Nssm remove $ServiceName confirm 2>&1 | Out-Null
Write-Host "Removed service '$ServiceName'." -ForegroundColor Green
