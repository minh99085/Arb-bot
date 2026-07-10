# Follow worker logs (Ctrl+C to exit — bot keeps running)
$ComposeFile = Join-Path $PSScriptRoot "docker-compose.arb.yml"
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose -f $ComposeFile logs -f arb-worker
