# Stop paper worker + dashboard and remove orphans
$ComposeFile = Join-Path $PSScriptRoot "docker-compose.arb.yml"
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose -f $ComposeFile down --remove-orphans
Write-Host "Bot stopped (orphans removed)." -ForegroundColor Yellow
