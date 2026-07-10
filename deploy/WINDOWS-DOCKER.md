# Windows + Docker — paper trading

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) running (WSL2 backend recommended)
- Repo cloned (GitHub Desktop → e.g. `C:\Users\You\Documents\Arb-bot`)
- **PowerShell** or **Git Bash**

## 1. Create host data folder

PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.hermes"
```

Optional paper config on the host (mounted into the container):

```powershell
@"
ARB_STUDY_MODE=false
ARB_EXEC_MODE=paper
ARB_DRY_RUN=true
ARB_KILL_SWITCH=false
ARB_ALLOW_LIVE=false
ARB_MIN_EDGE_BPS=30
ARB_TAKER_FEE_BPS=10
"@ | Set-Content -Encoding UTF8 "$env:USERPROFILE\.hermes\.env"
```

## 2. Build and start paper worker

Open PowerShell in the **repo root** (`Arb-bot`):

```powershell
cd C:\Users\You\Documents\Arb-bot
docker compose -f deploy/docker-compose.arb.yml up -d --build
```

First build may take a few minutes.

## 3. Pre-flight (alpha scan)

```powershell
docker compose -f deploy/docker-compose.arb.yml --profile tools run --rm arb-alpha
```

## 4. Watch logs

```powershell
docker compose -f deploy/docker-compose.arb.yml logs -f arb-worker
```

You should see `[scan]`, `[loop]`, `[reconcile]` lines every few minutes.

## 5. Check status

```powershell
docker compose -f deploy/docker-compose.arb.yml --profile tools run --rm arb-status
```

State DB on host:

```
%USERPROFILE%\.hermes\profiles\polymarket-arb\state\opportunities.sqlite
```

## Useful commands

| Action | Command |
|--------|---------|
| Stop | `docker compose -f deploy/docker-compose.arb.yml down` |
| Restart | `docker compose -f deploy/docker-compose.arb.yml restart arb-worker` |
| Rebuild after `git pull` | `docker compose -f deploy/docker-compose.arb.yml up -d --build` |
| One-shot scan | `docker compose -f deploy/docker-compose.arb.yml --profile tools run --rm arb-scan` |

## Troubleshooting

**`USERPROFILE` volume empty on Linux containers**  
Docker Desktop on Windows should map `%USERPROFILE%\.hermes` correctly. If status shows no DB, verify:

```powershell
dir $env:USERPROFILE\.hermes
```

**Build fails**  
Ensure you run commands from the repo root and Docker Desktop is started.

**No alpha in logs**  
Normal — markets are often efficient. The worker catches fleeting arbs; keep it running.

## Not included

- This compose runs **paper only** by default (`ARB_ALLOW_LIVE=false`).
- The main repo `docker-compose.yml` is the Hermes **gateway** — not required for arb paper trading.
