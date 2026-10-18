# Verifies .env is complete and the MT5 terminal is reachable WITHOUT sending any order.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
Get-Content .env | Where-Object { $_ -match '^\s*[A-Za-z_]+=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process') }
$req = 'SUPABASE_URL','SUPABASE_SERVICE_ROLE_KEY','ENGINE_USER_ID','MT5_PATH','MT5_LOGIN'
$missing = $req | Where-Object { -not [Environment]::GetEnvironmentVariable($_, 'Process') }
if ($missing) { throw "Missing in .env: $($missing -join ', ')" }
if (-not (Test-Path ([Environment]::GetEnvironmentVariable('MT5_PATH','Process')))) { throw 'MT5_PATH does not point to terminal64.exe' }
if ([Environment]::GetEnvironmentVariable('ENGINE_ALLOW_LIVE','Process') -eq 'true') { Write-Warning 'ENGINE_ALLOW_LIVE=true: live orders are possible. Keep it false until demo tests pass.' }
.\.venv\Scripts\python -c "from scalper_engine.config import EnvSettings; e=EnvSettings.from_env(); errs=e.validate(); print('config errors:', errs or 'none'); raise SystemExit(1 if errs else 0)"
Write-Host 'Environment looks complete. The engine starts PAUSED; nothing trades until you arm it from the app.' -ForegroundColor Green
