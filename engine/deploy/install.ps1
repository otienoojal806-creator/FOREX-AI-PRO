# One-time setup on the Windows VPS. Run in PowerShell from the engine folder:  .\deploy\install.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Python 3.10+ (64-bit, "Add to PATH") is required. Install it from python.org first.' }
python -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'"

if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt

if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "`nCreated .env. Fill it in with Notepad (never share or commit it): notepad .env" -ForegroundColor Yellow
}
# Only the Windows user that runs the engine may read the secrets file.
icacls .env /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

New-Item -ItemType Directory -Force state | Out-Null
Write-Host "`nRunning the offline test suite..." -ForegroundColor Cyan
.\.venv\Scripts\python -m unittest discover -s tests -t .
if ($LASTEXITCODE -ne 0) { throw 'Tests failed. Do not run the engine until they pass.' }
Write-Host "`nInstall OK. Next: edit .env, then .\deploy\check_env.ps1, then .\deploy\install_service.ps1" -ForegroundColor Green
