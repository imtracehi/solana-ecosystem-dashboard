# Runs the Solana dashboard pipeline once and appends a log line.
# Register with Windows Task Scheduler for daily refresh:
#   Register-ScheduledTask -TaskName "SolanaDashboardRefresh" `
#     -Action (New-ScheduledTaskAction -Execute "powershell.exe" `
#       -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\run_daily.ps1`"") `
#     -Trigger (New-ScheduledTaskTrigger -Daily -At 06:00) -Force
$ErrorActionPreference = "Continue"
$py = "C:\Users\trace\Documents\AI Coder\Project 2\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py "$PSScriptRoot\pipeline.py" 2>&1 | Out-File "$PSScriptRoot\refresh.log" -Append
