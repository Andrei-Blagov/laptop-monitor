#Requires -Version 5.1
<#
.SYNOPSIS
  Install Windows Scheduled Task: "Laptop Monitor Pipeline"

.DESCRIPTION
  Creates (or updates) a task that runs scripts\run_pipeline.ps1 every 2 hours.
  Does NOT store Telegram credentials — they stay in local .env.

  Run elevated if Register-ScheduledTask requires it on your machine.
  This script does not start an immediate pipeline run.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "Laptop Monitor Pipeline"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
$Wrapper = Join-Path $ProjectRoot "scripts\run_pipeline.ps1"

if (-not (Test-Path -LiteralPath $Wrapper)) {
    throw "Missing wrapper: $Wrapper"
}

$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $psExe)) {
    $psExe = "powershell.exe"
}

# -File with absolute path; project root is resolved inside the wrapper.
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Wrapper`""

$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument $arguments `
    -WorkingDirectory $ProjectRoot.Path

# Start soon, then every 2 hours indefinitely.
$startAt = (Get-Date).AddMinutes(2)
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $startAt `
    -RepetitionInterval (New-TimeSpan -Hours 2) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -RestartCount 0

# Run as current user, only when logged on (no stored password for this installer).
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Laptop Monitor: Regard+ANDPRO collection, alerts, Telegram delivery every 2 hours via scripts\run_pipeline.ps1"

Register-ScheduledTask `
    -TaskName $TaskName `
    -InputObject $task `
    -Force | Out-Null

Write-Host "Scheduled task registered: $TaskName"
Write-Host "Wrapper: $Wrapper"
Write-Host "Schedule: every 2 hours (StartWhenAvailable, MultipleInstances=IgnoreNew)"
Write-Host "Credentials: local .env only (not stored in the task)"
Write-Host "Next: verify with scripts\pipeline_status.ps1 after the first scheduled run"
