# Local health probe (use from Task Scheduler or an uptime monitor on the VPS). Exit 0 = healthy.
param([int]$Port = 8765)
try { $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5; Write-Host ($r | ConvertTo-Json -Compress); if ($r.status -eq 'ok') { exit 0 } else { exit 1 } }
catch { Write-Host "engine unreachable: $($_.Exception.Message)"; exit 2 }
