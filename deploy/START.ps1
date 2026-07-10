# One-command paper trading starter (Windows + Docker Desktop)
# Run from repo root:  .\START.ps1
# Or from deploy:      .\deploy\START.ps1

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

Write-Host "[1/4] Checking Docker..." -ForegroundColor Yellow
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker Desktop is not running." -ForegroundColor Red
    Write-Host "Start Docker Desktop, wait until Running, then run this script again."
    exit 1
}
Write-Host "      Docker OK" -ForegroundColor Green

Write-Host "[2/4] Setting up .hermes folder..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $HermesDir | Out-Null

if (-not (Test-Path $EnvFile)) {
    $envLines = @(
        "ARB_STUDY_MODE=false",
        "ARB_EXEC_MODE=paper",
        "ARB_DRY_RUN=true",
        "ARB_KILL_SWITCH=false",
        "ARB_ALLOW_LIVE=false",
        "ARB_MIN_EDGE_BPS=10",
        "ARB_TAKER_FEE_BPS=10",
        "ARB_VERIFY_TOP_N=100",
        "ARB_MIN_BOOK_DEPTH=2",
        "ARB_MAX_POSITION_USD=15",
        "ARB_MAX_OPEN_POSITIONS=15",
        "ARB_MAX_DAILY_TRADES=100",
        "ARB_MAX_DAILY_LOSS_USD=75",
        "ARB_PAPER_SLIPPAGE_BPS=5",
        "ARB_WORKER_SCAN_SEC=120",
        "ARB_WORKER_LOOP_SEC=180",
        "ARB_WORKER_TRADE_LIMIT=15",
        "ARB_WORKER_USE_WS=true",
        "ARB_WORKER_PAPER=true",
        "ARB_SELF_TUNE=true",
        "ARB_SELF_TUNE_MAX_CHANGES_PER_DAY=20"
    )
    $envLines | Set-Content -Path $EnvFile -Encoding UTF8
    Write-Host ("      Created " + $EnvFile) -ForegroundColor Green
} else {
    Write-Host ("      Using existing " + $EnvFile) -ForegroundColor Green
    # Ensure self-tune is on for existing installs
    $raw = Get-Content $EnvFile -Raw
    if ($raw -notmatch "ARB_SELF_TUNE=") {
        Add-Content -Path $EnvFile -Value "`nARB_SELF_TUNE=true`nARB_MIN_EDGE_BPS=10`nARB_VERIFY_TOP_N=100`nARB_WORKER_TRADE_LIMIT=15`nARB_WORKER_SCAN_SEC=120`nARB_WORKER_LOOP_SEC=180"
        Write-Host "      Appended high-activity + self-tune defaults" -ForegroundColor Green
    }
}

Write-Host "[3/4] Building image (first time ~3-5 min) and starting worker..." -ForegroundColor Yellow
Set-Location $RepoRoot
docker compose -f $ComposeFile up -d --build
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: docker compose failed." -ForegroundColor Red
    exit 1
}
Write-Host "      Worker started (container: polymarket-arb-paper)" -ForegroundColor Green

Write-Host "[3b/4] Starting dashboard on http://localhost:8787 ..." -ForegroundColor Yellow
docker compose -f $ComposeFile up -d arb-dashboard
if ($LASTEXITCODE -ne 0) {
    Write-Host "      Dashboard failed to start (worker is still running)." -ForegroundColor DarkYellow
} else {
    Write-Host "      Dashboard OK - open http://localhost:8787 in your browser" -ForegroundColor Green
}

Write-Host "[4/4] Running alpha pre-flight..." -ForegroundColor Yellow
docker compose -f $ComposeFile --profile tools run --rm arb-alpha
if ($LASTEXITCODE -ne 0) {
    Write-Host "      Pre-flight had issues - worker may still be OK. Check logs." -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " BOT IS RUNNING (paper mode)" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard:        http://localhost:8787"
Write-Host "  Self-tune:        on (auto-adjusts thresholds every ~30 min)"
Write-Host "  View live logs:   .\LOGS.ps1"
Write-Host "  Check status:     .\STATUS.ps1"
Write-Host "  Stop bot:         .\STOP.ps1"
Write-Host ""
Write-Host ("  Data saved to:    " + $HermesDir + "\profiles\polymarket-arb\state\")
Write-Host ""
Write-Host "  No alpha right now is normal - keep it running to catch fleeting arbs."
Write-Host ""
