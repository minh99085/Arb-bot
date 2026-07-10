# Windows + Docker — paper trading (one command)

## What you need

1. [Docker Desktop](https://www.docker.com/products/docker-desktop/) — **running**
2. Repo cloned — e.g. `C:\Users\tieut\Arb-bot`

## Start the bot (one command)

1. Open **PowerShell**
2. Go to the repo root:

```powershell
cd C:\Users\tieut\Arb-bot
```

3. Run:

```powershell
.\START.ps1
```

**Or** double-click `START.bat` in the repo root.

That's it. The script will:

- Create `%USERPROFILE%\.hermes\.env` (paper config) if missing  
- Build the Docker image (first run only, ~3-5 min)  
- Start the 24/7 paper worker with WebSocket re-verify  
- Start the **PnL dashboard** at http://localhost:8787  
- Run a quick alpha pre-flight  

## Dashboard (PnL + trade history)

Open in your browser:

**http://localhost:8787**

Or run:

```powershell
.\DASHBOARD.ps1
```

- Realized / today PnL, wins/losses, open positions  
- **Last 50 trades** — **click any row to expand** fill details  
- Auto-refreshes every 30 seconds  

## After it's running

| What | Command (repo root) |
|------|---------------------|
| Dashboard | http://localhost:8787 or `.\DASHBOARD.ps1` |
| Live logs | `.\LOGS.ps1` |
| Status | `.\STATUS.ps1` |
| Stop | `.\STOP.ps1` |

## If PowerShell blocks the script

```powershell
powershell -ExecutionPolicy Bypass -File .\START.ps1
```

Or use `START.bat` instead.

## Data location

```
%USERPROFILE%\.hermes\profiles\polymarket-arb\state\
```

## After `git pull`

Run `.\START.ps1` again — it rebuilds and restarts automatically.

## Notes

- **Paper only** — no wallet key needed  
- **No trades yet** on dashboard is normal until the worker paper-fills  
- Main repo `docker-compose.yml` is Hermes gateway — ignore it; use `START.ps1`
