#Requires -Version 5.1
<#
.SYNOPSIS
  Remove Windows Scheduled Task: "Laptop Monitor Pipeline"

.DESCRIPTION
  Unregisters only that task. Does not delete DB, logs, or .env.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "Laptop Monitor Pipeline"
)

$ErrorActionPreference = "Stop"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Scheduled task not found: $TaskName"
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Scheduled task removed: $TaskName"
