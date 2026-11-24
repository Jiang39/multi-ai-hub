# Remove Multi-AI Hub scheduled tasks (Run as Administrator).
# Only removes tasks created by install-windows-tasks.ps1; does not touch WorkBuddy tasks.
$ErrorActionPreference = 'Continue'
$names = @('MultiHubPanel','MultiHubGateway','MultiHubCheckin','MultiHubSync','MultiHubProbe')
foreach ($n in $names) {
  if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $n -Confirm:$false
    Write-Output ("$n removed")
  } else {
    Write-Output ("$n not found")
  }
}
Write-Output "Done."
