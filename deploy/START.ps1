# One-command paper trading starter (Windows + Docker Desktop)
# Run from repo root:  .\START.ps1

$ErrorActionPreference = "Stop"

$DeployDir = $PSScriptRoot
$RepoRoot = Split-Path $DeployDir -Parent
$ComposeFile = Join-Path $DeployDir "docker-compose.arb.yml"
$HermesDir = Join-Path $env:USERPROFILE ".hermes"
$EnvFile = Join-Path $HermesDir ".env"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Polymarket Arb Bot - Paper (Docker)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/5] Checking Docker..." -ForegroundColor Yellow
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker Desktop is not running." -ForegroundColor Red
    Write-Host "Start Docker Desktop, wait until Running, then run this script again."
    exit 1
}
Write-Host "      Docker OK" -ForegroundColor Green

Write-Host "[2/5] Setting up .hermes folder (high-activity + self-tune)..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $HermesDir | Out-Null

$envLines = @(
    "ARB_STUDY_MODE=false",
    "ARB_EXEC_MODE=paper",
    "ARB_DRY_RUN=true",
    "ARB_KILL_SWITCH=false",
    "ARB_ALLOW_LIVE=false",
    "ARB_MIN_EDGE_BPS=-30",
    "ARB_TAKER_FEE_BPS=2",
    "ARB_VERIFY_TOP_N=200",
    "ARB_MIN_BOOK_DEPTH=1",
    "ARB_PAPER_GAMMA_FALLBACK=true",
    "ARB_MAX_POSITION_USD=10",
    "ARB_MAX_OPEN_POSITIONS=30",
    "ARB_MAX_DAILY_TRADES=300",
    "ARB_MAX_DAILY_LOSS_USD=100",
    "ARB_PAPER_SLIPPAGE_BPS=3",
    "ARB_WORKER_SCAN_SEC=60",
    "ARB_WORKER_LOOP_SEC=60",
    "ARB_WORKER_TRADE_LIMIT=30",
    "ARB_WORKER_USE_WS=false",
    "ARB_WORKER_PAPER=true",
    "ARB_WORKER_SELF_TUNE_SEC=1200",
    "ARB_SELF_TUNE=true",
    "ARB_SELF_TUNE_MAX_CHANGES_PER_DAY=30"
)
$envLines | Set-Content -Path $EnvFile -Encoding UTF8
Write-Host ("      Wrote " + $EnvFile) -ForegroundColor Green

Write-Host "[3/5] Removing orphans + rebuilding containers..." -ForegroundColor Yellow
Set-Location $RepoRoot
docker compose -f $ComposeFile down --remove-orphans
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: down --remove-orphans returned non-zero (continuing)." -ForegroundColor DarkYellow
}

docker compose -f $ComposeFile up -d --build --remove-orphans --force-recreate
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: docker compose up failed." -ForegroundColor Red
    exit 1
}
Write-Host "      Worker + dashboard recreated" -ForegroundColor Green

Write-Host "[4/5] Dashboard: http://localhost:8787" -ForegroundColor Yellow
docker compose -f $ComposeFile ps

Write-Host "[5/5] Alpha pre-flight (optional)..." -ForegroundColor Yellow
docker compose -f $ComposeFile --profile tools run --rm arb-alpha
if ($LASTEXITCODE -ne 0) {
    Write-Host "      Pre-flight had issues - worker may still be OK. Check logs." -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " BOT IS RUNNING (aggressive paper + self-tune)" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard:        http://localhost:8787"
Write-Host "  Self-tune:        on (every ~30 min)"
Write-Host "  View live logs:   .\LOGS.ps1"
Write-Host "  Check status:     .\STATUS.ps1"
Write-Host "  Stop bot:         .\STOP.ps1"
Write-Host "  Rebuild clean:    .\REBUILD.ps1"
Write-Host ""
Write-Host ("  Data: " + $HermesDir + "\profiles\polymarket-arb\state\")
Write-Host ""
