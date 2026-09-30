#Requires -Version 5.1
<#
.SYNOPSIS
  Read-only status for laptop-monitor pipeline / Telegram / lock / logs.

.DESCRIPTION
  Does not run the pipeline and does not mutate the database.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")
Set-Location -LiteralPath $ProjectRoot

$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Error "Missing Python venv: $PythonExe"
    exit 1
}

Write-Host "=== Pipeline status ==="
& $PythonExe "run_pipeline.py" --status
Write-Host ""
Write-Host "=== Telegram delivery ==="
& $PythonExe "deliver.py" --status
Write-Host ""

$lockPath = Join-Path $ProjectRoot "data\laptop_monitor.lock"
Write-Host "=== Lock ==="
if (Test-Path -LiteralPath $lockPath) {
    Write-Host "laptop_monitor.lock: PRESENT ($lockPath)"
    try {
        Write-Host (Get-Content -LiteralPath $lockPath -Raw -ErrorAction SilentlyContinue)
    }
    catch { }
}
else {
    Write-Host "laptop_monitor.lock: absent"
}

Write-Host ""
Write-Host "=== Logs ==="
$logsDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path -LiteralPath $logsDir)) {
    Write-Host "logs/: none yet"
}
else {
    $latest = Get-ChildItem -LiteralPath $logsDir -Filter "pipeline-*.log" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latest) {
        Write-Host "No pipeline-*.log files"
    }
    else {
        Write-Host ("Latest log: {0}" -f $latest.FullName)
        Write-Host ("LastWriteTime: {0:o}" -f $latest.LastWriteTime)
        Write-Host ("Size bytes: {0}" -f $latest.Length)
        $exitLine = Select-String -LiteralPath $latest.FullName -Pattern "^exit_code=" |
            Select-Object -Last 1
        if ($exitLine) {
            Write-Host $exitLine.Line
        }
        else {
            Write-Host "exit_code: (not found in log footer)"
        }
    }
}
