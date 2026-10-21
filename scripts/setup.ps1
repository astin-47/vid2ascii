# Create (or update) the project virtualenv in .venv and install all dependencies.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv ..."
    python -m venv .venv
}
$py = ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements-dev.txt --quiet
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host ""
Write-Host "Ready. Examples:"
Write-Host "  .venv\Scripts\python -m vid2ascii serve"
Write-Host "  .venv\Scripts\python -m vid2ascii clip.mp4"
Write-Host "  .venv\Scripts\python -m pytest"
