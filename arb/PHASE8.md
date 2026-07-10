"""Phase 8 — High-activity paper + autonomous self-tune.

## What changed

| Area | Before | After |
|------|--------|-------|
| Min edge | 30 bps | **10 bps** |
| Verify top N | 40 | **100** |
| Max open | 5 | **15** |
| Daily trades | 20 | **100** |
| Scan / loop | 5m / 10m | **2m / 3m** |
| Trade limit | 3–5 | **15** |
| Book depth | 5 | **2** |
| Self-tune | human-gated only | **auto-apply in bounds** |

## Self-tune

```bash
python -m arb self-tune              # run once
python -m arb self-tune --status
python -m arb self-tune --dry-run
python -m arb worker once --jobs self-tune
```

Worker runs self-tune every `ARB_WORKER_SELF_TUNE_SEC` (default 1800s).

Rules (deterministic):
- Quiet / no fills → lower edge, raise verify_top_n, more trade attempts
- High FP rate → raise edge
- WS evaporations → longer watch
- Winning → more size / open / daily cap, slightly easier edge
- Losing → cut size, raise edge, fewer opens

Hard bounds in `arb/self_tune.py` (`BOUNDS`) — cannot enable live or disable kill switch.

Overrides persist in `state/self_tune.json` and merge on every `ArbConfig.from_env()`.

## Restart on Windows

```powershell
cd C:\\Users\\tieut\\Arb-bot
.\\STOP.ps1
.\\START.ps1
```

Dashboard shows live thresholds: http://localhost:8787
"""
