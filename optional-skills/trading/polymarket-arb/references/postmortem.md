---
name: arb-postmortem
description: "Daily Dutch-book postmortem: label outcomes, propose threshold changes."
version: 0.1.0
author: Arb-bot / Hermes Agent
tags: [polymarket, arbitrage, postmortem, learning]
platforms: [linux, macos, windows]
metadata:
  hermes:
    related_skills: [polymarket, polymarket-arb]
---

# Arb Postmortem (Intelligence Plane)

Runs **after** the money loop. Deterministic labeling + threshold proposals.
**Human must approve** before any config change. Never auto-applies.

## When to Use

- Daily/weekly review of scanner + paper results
- User asks why arbs evaporated or what thresholds to change
- Cron: script-only postmortem without LLM cost

## Commands

```bash
python -m arb postmortem --days 7
python -m arb proposals
python -m arb approve <proposal_id>
python -m arb reject <proposal_id>
python -m arb proposals --env-snippet   # copy into .env manually
```

Cron (no LLM):

```bash
python3 optional-skills/trading/polymarket-arb/scripts/run_postmortem.py
```

```text
cronjob(action="create", schedule="0 6 * * *", script="run_postmortem.py",
        no_agent=True, deliver="telegram")
```

## Labels

| Label | Meaning |
|-------|---------|
| `true_arb` | Verified / open path with lasting edge |
| `false_positive` | Flagged then rejected (book/edge) |
| `ws_evaporated` | Died on WebSocket re-verify |
| `risk_rejected` | Failed risk gate |
| `paper_win` / `paper_loss` | Settled paper PnL |

## Rules

1. Proposals are **pending** until `approve` / `reject`
2. Approve only prints an env snippet — **you** paste into `.env`
3. Do not let an agent merge threshold changes without human review
4. Hot path (scan/trade) stays deterministic; this skill is offline learning
