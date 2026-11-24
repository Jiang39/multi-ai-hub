# Install Multi-AI Hub scheduled tasks on Windows (Run as Administrator).
# Right-click this file -> "Run with PowerShell (Admin)".
# Paths are auto-detected from this script's location, no hardcoded D:\code.
$ErrorActionPreference = 'Continue'

$dir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$hub = Join-Path $dir 'hub.py'
if (-not (Test-Path $hub)) { Write-Output ("hub.py not found: " + $hub); exit 1 }

# 避开 WindowsApps 的 python.exe 占位符：优先当前 PATH 中 python 的同目录 pythonw.exe。
# pythonw 无黑框；找不到 pythonw 时回退 python.exe。
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) { Write-Output "python not found in PATH"; exit 1 }
$python = $pythonCmd.Source
if ($python -match 'WindowsApps') {
  # 商店占位符不可执行，尝试常见真实安装位置
  $candidates = @(
    "$env:LOCALAPPDATA\Programs\Python\pythonw.exe",
    "C:\Python313\pythonw.exe", "C:\Python312\pythonw.exe", "C:\Python311\pythonw.exe"
  )
  $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
  if (-not $found) { Write-Output "python is WindowsApps stub and no real pythonw found"; exit 1 }
  $python = $found
}
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }  # 回退：有黑框但能跑

function New-Task($name, $args, $trigger, $settings, $desc) {
  if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Write-Output ("$name already exists, skipped"); return
  }
  $a = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $hub + '" ' + $args) -WorkingDirectory $dir
  if (-not $settings) {
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
  }
  Register-ScheduledTask -TaskName $name -Action $a -Trigger $trigger -Settings $settings -Description $desc | Out-Null
  Write-Output ("$name created")
}

# 常驻服务：每 5 分钟重复触发 + IgnoreNew（已运行则跳过，崩了自动拉起），无执行时限。
$guardSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$every5m = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
New-Task 'MultiHubPanel'   'serve --port 8798'  $every5m $guardSettings 'Multi-AI Hub panel (8798)'
New-Task 'MultiHubGateway' 'gateway --port 8899' $every5m $guardSettings 'Multi-AI Hub gateway (8899)'

# 周期任务：无执行时限即可（各自本身很短）。
$plain = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
$every4h = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 4)
New-Task 'MultiHubCheckin' 'checkin' $every4h $plain 'Multi-AI Hub all-platform checkin (every 4h)'
$every10m = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10)
New-Task 'MultiHubSync' 'sync' $every10m $plain 'Multi-AI Hub account onboarding (every 10m)'

# 每周协议探测（及早发现版本/key 漂移）
$weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '09:00'
New-Task 'MultiHubProbe' 'probe' $weekly $plain 'Multi-AI Hub protocol model-key probe (weekly)'

Write-Output "Done. Verify: python hub.py status --service"
