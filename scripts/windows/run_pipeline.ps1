#Requires -Version 5.1
<#
.SYNOPSIS
  Safe production wrapper for laptop-monitor pipeline (Task Scheduler / manual).

.DESCRIPTION
  Resolves project root from this script location, uses .venv Python only,
  writes a timestamped log, returns the same exit code as run_pipeline.py:
    0 success | 1 failed | 2 partial | 3 already running
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Write-LogLine {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Message
    )
    Add-Content -LiteralPath $Path -Value $Message -Encoding UTF8
}

try {
$ProjectRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")
Set-Location -LiteralPath $ProjectRoot

    $PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    $PipelinePy = Join-Path $ProjectRoot "run_pipeline.py"
    $EnvFile = Join-Path $ProjectRoot ".env"
    $LogsDir = Join-Path $ProjectRoot "logs"

    if (-not (Test-Path -LiteralPath $PythonExe)) {
        Write-Error "Missing Python venv: $PythonExe"
        exit 1
    }
    if (-not (Test-Path -LiteralPath $PipelinePy)) {
        Write-Error "Missing run_pipeline.py: $PipelinePy"
        exit 1
    }
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Write-Error "Missing .env (Telegram credentials). Copy .env.example to .env and fill values."
        exit 1
    }

    if (-not (Test-Path -LiteralPath $LogsDir)) {
        New-Item -ItemType Directory -Path $LogsDir | Out-Null
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $LogFile = Join-Path $LogsDir "pipeline-$stamp.log"
    $started = Get-Date

    Write-LogLine -Path $LogFile -Message ("=== pipeline start {0:o} ===" -f $started)
    Write-LogLine -Path $LogFile -Message ("cwd={0}" -f $ProjectRoot)
    Write-LogLine -Path $LogFile -Message ("python={0}" -f $PythonExe)
    Write-LogLine -Path $LogFile -Message "command=run_pipeline.py"
    Write-LogLine -Path $LogFile -Message "---"

    $exitCode = 1
    try {
        # Capture stdout+stderr; preserve pipeline exit code.
        $output = & $PythonExe $PipelinePy 2>&1
        if ($null -ne $LASTEXITCODE) {
            $exitCode = [int]$LASTEXITCODE
        }
        elseif (-not $?) {
            $exitCode = 1
        }
        else {
            $exitCode = 0
        }

        foreach ($item in @($output)) {
            if ($null -eq $item) { continue }
            $line = $item.ToString()
            # Never echo secrets if they somehow appear in process output.
            if ($line -match "(?i)TELEGRAM_BOT_TOKEN\s*=") { continue }
            Write-Host $line
            Write-LogLine -Path $LogFile -Message $line
        }
    }
    catch {
        $exitCode = 1
        $err = $_.Exception.Message
        Write-Host $err
        Write-LogLine -Path $LogFile -Message ("WRAPPER_EXCEPTION: {0}" -f $err)
    }

    $finished = Get-Date
    $duration = ($finished - $started).TotalSeconds
    Write-LogLine -Path $LogFile -Message "---"
    Write-LogLine -Path $LogFile -Message ("exit_code={0}" -f $exitCode)
    Write-LogLine -Path $LogFile -Message ("=== pipeline end {0:o} ===" -f $finished)
    Write-LogLine -Path $LogFile -Message ("duration_seconds={0:N3}" -f $duration)

    # Retention: delete pipeline logs older than 30 days (best-effort).
    try {
        $cutoff = (Get-Date).AddDays(-30)
        Get-ChildItem -LiteralPath $LogsDir -Filter "pipeline-*.log" -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
    }
    catch {
        # Must not break pipeline exit code.
    }

    exit $exitCode
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
