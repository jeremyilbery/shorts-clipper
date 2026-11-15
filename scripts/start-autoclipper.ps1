# Starts the shorts-clipper background worker, which watches the channels in
# outputs/watchdog.json every 10 minutes and clips + uploads new videos.
#
# Run it by hand, or register it to start at logon (see docs/AUTOMATION.md).
# Safe to run twice: it exits if a worker is already running.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Pick up Python/FFmpeg/yt-dlp installed after this session's PATH was built.
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
$env:PYTHONIOENCODING = "utf-8"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"

# A live worker refreshes this file every 5 seconds.
$heartbeat = Join-Path $root "outputs\worker_heartbeat.txt"
if (Test-Path $heartbeat) {
    $age = (Get-Date) - (Get-Item $heartbeat).LastWriteTime
    if ($age.TotalSeconds -lt 30) {
        Write-Host "Worker already running (heartbeat $([int]$age.TotalSeconds)s old)."
        exit 0
    }
}

New-Item -ItemType Directory -Force (Join-Path $root "outputs") | Out-Null
$python = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "outputs\worker.log"
$err = Join-Path $root "outputs\worker.err.log"

$p = Start-Process -FilePath $python -ArgumentList "-m", "shorts_clipper.core.worker" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $log -RedirectStandardError $err
Write-Host "Worker started (PID $($p.Id)). Logs: $err"
