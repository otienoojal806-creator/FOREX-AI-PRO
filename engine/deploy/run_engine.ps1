# Supervisor loop: restarts the engine if it exits or crashes. A restart ALWAYS comes back PAUSED with live disarmed
# (that is how the engine itself starts), so supervision can never re-enable live entries.
Set-Location (Split-Path $PSScriptRoot -Parent)
Get-Content .env | Where-Object { $_ -match '^\s*[A-Za-z_]+=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process') }
$delay = 5
while ($true) {
  $started = Get-Date
  & .\.venv\Scripts\python -m scalper_engine
  $code = $LASTEXITCODE
  if ($code -eq 2) { Write-Host 'Configuration error; not restarting. Fix .env.'; exit 2 }
  # back off if it keeps dying quickly, reset once it has run for a while
  if (((Get-Date) - $started).TotalSeconds -gt 300) { $delay = 5 } else { $delay = [Math]::Min($delay * 2, 120) }
  Add-Content state\supervisor.log "$(Get-Date -Format o) engine exited with $code; restarting in ${delay}s"
  Start-Sleep -Seconds $delay
}
