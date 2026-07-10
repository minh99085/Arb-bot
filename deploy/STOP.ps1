# Stop paper worker
$ComposeFile = Join-Path $PSScriptRoot "docker-compose.arb.yml"
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose -f $ComposeFile down
Write-Host "Bot stopped." -ForegroundColor Yellow
