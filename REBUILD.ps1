# Clean rebuild: remove orphans, rebuild images, recreate containers
# Run from repo root:  .\REBUILD.ps1

$ErrorActionPreference = "Stop"
$DeployDir = Join-Path $PSScriptRoot "deploy"
if (-not (Test-Path (Join-Path $DeployDir "docker-compose.arb.yml"))) {
    $DeployDir = $PSScriptRoot
}
$ComposeFile = Join-Path $DeployDir "docker-compose.arb.yml"
$RepoRoot = Split-Path $DeployDir -Parent
if (-not (Test-Path $ComposeFile)) {
    Write-Host "ERROR: cannot find docker-compose.arb.yml" -ForegroundColor Red
    exit 1
}

Write-Host "Removing orphans + rebuilding..." -ForegroundColor Cyan
Set-Location $RepoRoot
docker compose -f $ComposeFile down --remove-orphans
docker compose -f $ComposeFile build --no-cache
docker compose -f $ComposeFile up -d --force-recreate --remove-orphans
docker compose -f $ComposeFile ps
Write-Host ""
Write-Host "Done. Dashboard: http://localhost:8787" -ForegroundColor Green
Write-Host "Logs: .\LOGS.ps1" -ForegroundColor Green
