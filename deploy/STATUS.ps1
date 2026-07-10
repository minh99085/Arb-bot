# Show bot status (positions, fills, counts)
$ComposeFile = Join-Path $PSScriptRoot "docker-compose.arb.yml"
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose -f $ComposeFile --profile tools run --rm arb-status
