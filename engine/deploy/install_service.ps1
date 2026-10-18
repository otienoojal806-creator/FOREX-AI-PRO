# Registers the engine to start at boot and after logon (Administrator PowerShell).
# MT5 must be running and logged in for the engine to connect, so also set MT5 to start at logon
# (copy a shortcut of terminal64.exe into shell:startup) and enable Windows auto-logon on the VPS if it reboots.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\deploy\run_engine.ps1`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'ForexAI-Scalper-Engine' -Action $action -Trigger $trigger -Settings $settings -Description 'ForexAI Scalper trading engine (starts PAUSED)' -Force | Out-Null
Start-ScheduledTask -TaskName 'ForexAI-Scalper-Engine'
Write-Host 'Installed and started. It begins PAUSED. Open the app, Scalper tab, to connect.' -ForegroundColor Green
