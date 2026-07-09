# Phase 2 — Execution Plane (Money Loop)

Status: **COMPLETE** (paper path). Live CLOB still stubbed.

## Goal

Close the deterministic money loop:

```
scan → detect → CLOB verify → risk gate → paper execute → reconcile
```

No LLM on this path.

## Delivered

| Module | Role |
|--------|------|
| `arb/risk.py` | Kill switch, study gate, size, open caps, daily loss/trades, duplicates |
| `arb/paper.py` | Simulated fills at book + slippage/fees |
| `arb/execute.py` | RISK_OK → ORDER_PLACED → FILLED (paper); live hard-gated |
| `arb/reconcile.py` | Expected vs realized PnL; settle paper → SETTLED → CLOSED |
| `arb/state.py` | `fills` table, transitions, open-position helpers |
| CLI | `trade --paper`, `reconcile`, `loop --paper` |

## State machine (full)

```
DISCOVERED → GAMMA_FLAG → CLOB_VERIFIED → RISK_OK → ORDER_PLACED → FILLED → SETTLED → CLOSED
                    ↘ REJECTED          ↘ REJECTED
```

## Commands

```bash
# Exit study and paper-trade verified opps
ARB_STUDY_MODE=false python -m arb trade --paper --limit 5

# Or one-shot loop
python -m arb loop --paper --limit 50 --trade-limit 5

# Reconcile / settle paper
python -m arb reconcile
python -m arb status
```

## Env (Phase 2)

| Variable | Default | Meaning |
|----------|---------|---------|
| `ARB_STUDY_MODE` | `true` | Must be `false` to trade |
| `ARB_EXEC_MODE` | `paper` | `paper` \| `live` \| `disabled` |
| `ARB_KILL_SWITCH` | `false` | Halt all new trades |
| `ARB_MAX_POSITION_USD` | `25` | Per-trade size cap |
| `ARB_MAX_OPEN_POSITIONS` | `5` | Concurrent open cap |
| `ARB_MAX_DAILY_TRADES` | `20` | Daily fill cap |
| `ARB_MAX_DAILY_LOSS_USD` | `50` | Daily realized loss halt |
| `ARB_PAPER_SLIPPAGE_BPS` | `10` | Paper fill haircut |
| `ARB_ALLOW_LIVE` | `false` | Required for live (still not implemented) |
| `POLYMARKET_PRIVATE_KEY` | — | Required for live later |

## Explicitly out of scope (later phases)

- Live `py-clob-client` order placement (stub returns `live_not_implemented`)
- WebSocket feeds (Phase 3)
- LLM postmortem (Phase 4)
- Cloud 24/7 worker (Phase 5)

## Phase 3 gate

Paper loop runs cleanly; reconcile gap ≈ 0; kill switch tested.
Then add WebSocket re-verify.
