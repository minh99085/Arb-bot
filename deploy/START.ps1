# One-command paper trading starter (Windows + Docker Desktop)
# Open PowerShell HERE (deploy folder) and run:  .\START.ps1

$ErrorActionPreference = "Stop"

$DeployDir = $PSScriptRoot
$RepoRoot = Split-Path $DeployDir -Parent
$ComposeFile = Join-Path $DeployDir "docker-compose.arb.yml"
$HermesDir = Join-Path $env:USERPROFILE ".hermes"
$EnvFile = Join-Path $HermesDir ".env"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Polymarket Arb Bot — Paper (Docker)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# --- Docker running? ---
Write-Host "[1/4] Checking Docker..." -ForegroundColor Yellow
try {
    docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker info failed" }
} catch {
    Write-Host "ERROR: Docker Desktop is not running." -ForegroundColor Red
    Write-Host "Start Docker Desktop, wait until it says Running, then run this script again."
    exit 1
}
Write-Host "      Docker OK" -ForegroundColor Green

# --- Host config ---
Write-Host "[2/4] Setting up %USERPROFILE%\.hermes ..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $HermesDir | Out-Null

if (-not (Test-Path $EnvFile)) {
    @"
ARB_STUDY_MODE=false
ARB_EXEC_MODE=paper
ARB_DRY_RUN=true
ARB_KILL_SWITCH=false
ARB_ALLOW_LIVE=false
ARB_MIN_EDGE_BPS=30
ARB_TAKER_FEE_BPS=10
ARB_VERIFY_TOP_N=40
ARB_WORKER_USE_WS=true
ARB_WORKER_PAPER=true
"@ | Set-Content -Encoding UTF8 $EnvFile
    Write-Host "      Created $EnvFile" -ForegroundColor Green
} else {
    Write-Host "      Using existing $EnvFile" -ForegroundColor Green
}

# --- Build & start ---
Write-Host "[3/4] Building image (first time ~3-5 min) and starting worker..." -ForegroundColor Yellow
Set-Location $RepoRoot
docker compose -f $ComposeFile up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: docker compose failed." -ForegroundColor Red
    exit 1
}
Write-Host "      Worker started (container: polymarket-arb-paper)" -ForegroundColor Green

# --- Quick health check ---
Write-Host "[4/4] Running alpha pre-flight..." -ForegroundColor Yellow
docker compose -f $ComposeFile --profile tools run --rm arb-alpha
if ($LASTEXITCODE -ne 0) {
    Write-Host "      Pre-flight had issues — worker may still be OK. Check logs." -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " BOT IS RUNNING (paper mode)" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  View live logs:   .\LOGS.ps1"
Write-Host "  Check status:     .\STATUS.ps1"
Write-Host "  Stop bot:         .\STOP.ps1"
Write-Host ""
Write-Host "  Data saved to:    $HermesDir\profiles\polymarket-arb\state\"
Write-Host ""
Write-Host "  No alpha right now is normal — keep it running to catch fleeting arbs."
Write-Host ""
