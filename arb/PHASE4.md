# Phase 4 — Intelligence Plane

Status: **COMPLETE**

## Goal

Slow learning loop: **observe → label → propose → human approve → deploy**.
Never auto-applies config. No LLM required on the default path.

## Delivered

| Module | Role |
|--------|------|
| `arb/labels.py` | Label history (`true_arb`, `false_positive`, `ws_evaporated`, …) |
| `arb/proposals.py` | Pending proposals JSON; approve/reject; env snippet |
| `arb/postmortem.py` | Daily report + deterministic proposal rules |
| CLI | `postmortem`, `proposals`, `approve`, `reject` |
| Cron script | `scripts/run_postmortem.py` |
| Skill notes | `references/postmortem.md` |

## Flow

```
SQLite history → label_history → labels_*.jsonl
                              → postmortem_*.md
                              → proposals.json (pending)
Human: approve/reject → env snippet → manual .env update
```

## Proposal rules (deterministic)

| Trigger | Proposal |
|---------|----------|
| FP rate ≥ 50% and n ≥ 10 | Raise `ARB_MIN_EDGE_BPS` by 25 |
| WS evaporations ≥ 5 and ≥ lasting signals | Raise `ARB_WS_WATCH_SEC` |
| Paper losses > wins (n ≥ 5) | Halve `ARB_MAX_POSITION_USD` |
| n ≥ 20 and zero lasting signals | Optional slight edge ease (review carefully) |

## Commands

```bash
python -m arb postmortem --days 7
python -m arb proposals
python -m arb approve prop_...
python -m arb proposals --env-snippet
```

## Out of scope

- Auto-writing `.env` or restarting the bot
- LLM narrative postmortems (optional later via Hermes skill chat)
- Phase 5 cloud worker
