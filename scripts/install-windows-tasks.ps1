# Install Multi-AI Hub scheduled tasks on Windows (Run as Administrator).
# Right-click this file -> "Run with PowerShell (Admin)".
# Paths are auto-detected from this script's location, no hardcoded D:\code.
$ErrorActionPreference = 'Continue'

$dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { Write-Output "python not found in PATH"; exit 1 }
$hub = Join-Path $dir 'hub.py'
if (-not (Test-Path $hub)) { Write-Output ("hub.py not found: " + $hub); exit 1 }

function New-Task($name, $args, $trigger, $desc) {
  if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Write-Output ("$name already exists, skipped"); return
  }
  $a = New-ScheduledTaskAction -Execute $python -Argument ('"' + $hub + '" ' + $args) -WorkingDirectory $dir
  $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 60)
  Register-ScheduledTask -TaskName $name -Action $a -Trigger $trigger -Settings $s -Description $desc | Out-Null
  Write-Output ("$name created")
}

# Long-running services: start at logon, restart if stopped
$atLogon = New-ScheduledTaskTrigger -AtLogOn
$svcSettings = $null
New-Task 'MultiHubPanel' 'serve --port 8798' $atLogon 'Multi-AI Hub panel (8798)'
New-Task 'MultiHubGateway' 'gateway --port 8899' $atLogon 'Multi-AI Hub gateway (8899)'

# Periodic jobs
$every4h = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 4)
New-Task 'MultiHubCheckin' 'checkin' $every4h 'Multi-AI Hub all-platform checkin (every 4h)'
$every10m = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10)
New-Task 'MultiHubSync' 'sync' $every10m 'Multi-AI Hub account onboarding (every 10m)'

# Weekly protocol probe (detects version/key drift early)
$weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '09:00'
New-Task 'MultiHubProbe' 'probe' $weekly 'Multi-AI Hub protocol model-key probe (weekly)'

Write-Output "Done. Verify: python hub.py status --service"
