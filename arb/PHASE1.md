# Phase 1 — Honest Scanner (Study Mode)

Status: **IN PROGRESS / COMPLETE when study gate passes**

## Goal

Scan every active Polymarket market, detect Dutch-book candidates, verify against
CLOB books, and **log everything**. No live trading. No paper trading.

## Delivered

- Full-universe Gamma pagination (`arb/scanner.py`)
- CLOB book verification with reject reasons
- State machine: `DISCOVERED → GAMMA_FLAG → CLOB_VERIFIED | REJECTED`
- SQLite audit + transitions (`arb/state.py`)
- Human ledger (`LEDGER.md` under state dir)
- Metrics JSON (`metrics.json`)
- CLI: `scan`, `status`, `study` — `trade` blocked
- Cron script: `optional-skills/trading/polymarket-arb/scripts/scan_dutch_book.py`
- Alerts only when CLOB-verified hits exist

## Commands

```bash
python -m arb scan --study
python -m arb scan --limit 100 --json
python -m arb status
python -m arb study --days 30
```

## Go / No-Go for Phase 2

From revised plan:

| Gate | Criterion |
|------|-----------|
| Study window | Prefer ≥7–30 days of scan logs |
| Signal density | ≥10 CLOB-verified signals / week |
| Edge quality | Hypothetical unit PnL logged; review reject breakdown |

Check with:

```bash
python -m arb study --days 30
```

**Do not start Phase 2** until `Ready for Phase 2: YES` (or you explicitly override
with evidence that verified arbs exist).

## Explicitly out of scope (Phase 1)

- Live / paper order placement
- Risk engine
- WebSocket feeds
- LLM postmortem
- 24/7 cloud worker
