# Phase 5 — 24/7 Ops (Standalone Worker)

Status: **COMPLETE**

## Goal

Run the money loop **without Hermes on the hot path**. Hermes remains optional
for Telegram/Discord delivery of cron stdout and postmortem alerts.

## Delivered

| Piece | Role |
|-------|------|
| `arb/worker.py` | Daemon + `once` jobs: scan / loop / reconcile / postmortem |
| `arb/run_worker.py` | Thin entry |
| `arb/worker.env.example` | Schedule env defaults |
| `deploy/systemd/…` | systemd unit |
| `deploy/cron/…` | crontab template |
| `deploy/github-actions/…` | Manual/cloud once-runner template |
| CLI | `worker run`, `worker once`, `worker status` |

## Architecture

```
┌─────────────────────────────────────────────┐
│  ArbWorker (this host / cloud)              │
│  scan → [ws] → risk → paper → reconcile     │
│  + daily postmortem (proposals pending)     │
└──────────────────┬──────────────────────────┘
                   │ stdout / last_alert.txt
                   ▼
         Hermes gateway (optional alerts)
```

## Commands

```bash
# Long-running daemon
python -m arb worker run

# One-shot (cron / GH Actions)
python -m arb worker once --jobs scan,reconcile
python -m arb worker once --jobs loop --json
python -m arb worker once --jobs postmortem

# Status / heartbeat
python -m arb worker status
```

## systemd

```bash
sudo cp deploy/systemd/polymarket-arb-worker.service /etc/systemd/system/
# edit User, WorkingDirectory, EnvironmentFile
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-arb-worker
sudo journalctl -u polymarket-arb-worker -f
```

## Safety defaults

- Paper mode (`ARB_WORKER_PAPER=true`)
- Live still requires `ARB_ALLOW_LIVE` + key + `ARB_EXEC_MODE=live` (stub)
- Kill switch honored via `ArbConfig`
- PID + `worker_status.json` under `ARB_STATE_DIR`

## Hermes role (optional)

Use Hermes cron **only** to deliver alerts/postmortem text, or skip Hermes and
pipe logs to your own notifier. Do not put LLM agents on the scan/trade timers.
