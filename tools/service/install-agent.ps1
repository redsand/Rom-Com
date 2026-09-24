<#
.SYNOPSIS
  Run the Rom-Com launch agent automatically at logon.

.DESCRIPTION
  The web app is a service, and a Windows service lives in session 0 whatever account it
  uses — session 0 has no desktop, so it can never put an emulator window on screen. The
  agent is the other half: it runs as you, in your session, and executes the launches the
  service queues.

  That makes it a *logon* task, not a service. Registering it as a service would put it right
  back in session 0 and break the one thing it exists to do.

  No elevation needed: the task is registered for the current user only.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'RomCom launch agent',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$Repo   = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Python = Join-Path $Repo '.venv\Scripts\pythonw.exe'   # pythonw: no console window
if (-not (Test-Path $Python)) { $Python = Join-Path $Repo '.venv\Scripts\python.exe' }

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed '$TaskName'." -ForegroundColor Green
    } else {
        Write-Host "'$TaskName' is not registered."
    }
    return
}

if (-not (Test-Path $Python)) { throw "Interpreter not found: $Python" }

$action  = New-ScheduledTaskAction -Execute $Python -Argument '-m romcom agent' -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Interactive so it lands in the desktop session, which is the entire point. Battery
# conditions off: a laptop on battery must still be able to launch a game.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                                          -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
                                          -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                       -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host "  runs   : $Python -m romcom agent"
Write-Host "  when   : at logon, as $env:USERNAME, in your desktop session"
Write-Host "  stop it: powershell -File tools/service/install-agent.ps1 -Remove"
Write-Host ""
Write-Host "Starting it now so you do not have to log out..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName
